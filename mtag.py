#!/usr/bin/python3
# SPDX-License-Identifier: BSD0
#
# Copyright (c) 2021 Andrew Deason <adeason@dson.org>
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
# REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
# FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
# INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
# LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
# OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
# PERFORMANCE OF THIS SOFTWARE.

# mtag.py - Tool for managing metadata tags of git commits (for the OpenAFS
# IPL -> IPL/GPL relicensing effort).
#
# Example usage:
#
# $ ./mtag.py ~/src/openafs.git --apply-tags commits.yaml
# [...]
# $ ./mtag.py ~/src/openafs.git --print-tags commits.yaml --exclude tiny ignore org:some-company.com \
#                                                         --include linux-kernel license:ibm
# [...]
# author:adeason@dson.org, 15 commits:
#  08c769967ca12f1ac99c736789f1925763d8a115: author:adeason@dson.org license:ibm linux-kernel 
#  13acb6fbefd6c4f4af951270ca07a1a5541052fa: author:adeason@dson.org license:ibm linux-kernel 
# [...]

import argparse
import contextlib
import git
import glob
import itertools
import os.path
import pathlib
import time
import yaml
import subprocess
import sys

# Says what tags to apply manually for each file, author, etc.
CONFIG_FILE = 'mtag.yaml'

# An arbitrary non-trivial commit that should be included in the tagged
# commits.
TEST_COMMIT = 'faa9d8f11f28232000446d787ebf53ab9345eb89'

# How many 'git log' pids to spawn in parallel.
PARALLEL = 4

def grouper(iterable, n, fillvalue=None):
    # "Collect data into fixed-length chunks or blocks"
    # grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx"
    args = [iter(iterable)] * n
    return itertools.zip_longest(*args, fillvalue=fillvalue)

def glob_path(args, path):
    paths = glob.glob(os.path.join(args.repo, path))
    if not paths:
        raise Exception(f"path/pattern {path} does not match any files")

    # strip the repo part of the path, to get a path relative to
    # top_src (e.g. src/afs/afs_cell.c)
    for path in paths:
        path = str(path)
        if not path.startswith(args.repo):
            raise Exception("internal error")
        yield path[len(args.repo)+1:]

def parse_gitlog(path, cmd):
    # Each commit in the 'git log' output should have a line for the sha1 and
    # email, then a line for --numstat, then a blank line (except for the last
    # commit, where we just hit EOF).
    sha = None
    email = None
    lines_added = None

    for line in cmd.stdout:
        line = line.decode('ascii').rstrip()

        if sha is None:
            sha,email = line.split(',')
            continue

        if lines_added is None:
            if line:
                # The line for --numstat, which looks like:
                # <n_added>\t<n_del>\t<path>
                added, _, _ = line.split('\t')
                lines_added = int(added)
                continue
            else:
                # If the next line is blank, there is no numstat line, which
                # means the commit changed no lines (this can happen if the
                # commit only changed whitespace; we ignore whitespace-only
                # changes).
                lines_added = 0

        if line:
            raise Exception(f"extra git log data for {path}: {line}")

        yield sha,email,lines_added
        sha = None
        email = None
        lines_added = None

    yield sha,email,lines_added

class CommitData:
    def __init__(self, sha, email):
        self.sha = sha
        self.email = email
        self.lines_added = 0
        self.paths = set()
        self.tags = set()

class Config:
    data = None
    file_tags = None
    addrmap = None
    args = None
    commits = None

    def __init__(self, args, path):
        self.args = args
        self.commits = {}

        with open(path, 'r') as fh:
            self.data = yaml.safe_load(fh)

        self.tiny_thresh = int(self.data['line_tags']['tiny_lines'])
        self.tiny_tags = set(self.data['line_tags']['tiny_tags'])

        self.addrmap = {}
        for author, aliases in self.data['author_aliases'].items():
            for alias in aliases:
                self.addrmap[alias.lower()] = author

        self.author_tags = {}
        for author, tags in self.data['author_tags'].items():
            self.author_tags[author.lower()] = set(list(tags))

        # self.file_tags['src/path/file.c'] = ['tag1', 'tag2', etc]
        self.file_tags = {}
        for pat, tags in self.data['file_tags'].items():
            paths = glob_path(self.args, pat)
            for path in paths:
                self.file_tags.setdefault(path, set())
                self.file_tags[path].update(tags)

        self.commit_tags = {}
        for sha, tags in self.data['commit_tags'].items():
            self.commit_tags[sha] = set(tags)

    def export(self, path, top_sha):
        data = {
            'top': top_sha,
            'commits': []
        }

        for sha in sorted(self.commit_tags.keys()):
            data['commits'].append({
                'sha': sha,
                'tags': sorted(self.commit_tags[sha]),
            })

        with open(path, 'w') as fh:
            yaml.dump(data, fh)

    def get_paths(self):
        return self.file_tags.keys()

    def get_filetags(self, path):
        return self.file_tags.get(path, [])

    def get_linetags(self, lines_added):
        if lines_added <= self.tiny_thresh:
            return self.tiny_tags
        return []

    def get_authortags(self, email):
        tags = set()
        email = email.lower()
        email = self.addrmap.get(email, email)

        tags.add(f"author:{email}")
        tags.update(self.author_tags.get(email, set()))

        return tags

    def add_tags(self, sha, tags):
        if not tags:
            return

        self.commit_tags.setdefault(sha, set())
        self.commit_tags[sha].update(tags)

    def process_commit(self, sha, email, path, lines_added):
        if sha not in self.commits:
            self.commits[sha] = CommitData(sha, email)
        cdata = self.commits[sha]
        cdata.lines_added += lines_added
        cdata.paths.add(path)

    def apply_tags(self):
        for commit in self.commits.values():
            tags = set()
            tags.update(self.get_linetags(commit.lines_added))
            tags.update(self.get_authortags(commit.email))

            for path in commit.paths:
                tags.update(self.get_filetags(path))

            self.add_tags(commit.sha, tags)

def apply_tags(args, data, out_path):
    repo = git.Repo(args.repo)
    if repo.is_dirty():
        raise Exception(f"Repo {args.repo} is dirty")
    top_sha = repo.commit('HEAD').hexsha

    n_files = len(data.get_paths())

    print(f"Running git log --follow on {n_files} files ({PARALLEL} threads)...")
    start = time.time()

    commits = set()

    # Get PARALLEL paths at a time, so we spawn PARALLEL 'git log' processes in
    # parallel
    for paths in grouper(data.get_paths(), PARALLEL):
        paths = [path for path in paths if path is not None]
        with contextlib.ExitStack() as stack:
            gitlogs = []
            for path in paths:
                argv = ['git', '-C', args.repo, 'log',
                        '--pretty=format:%H,%ae',
                        '--ignore-all-space',
                        '--numstat',
                        '--follow', path]
                print("+ " + ' '.join(argv))
                child = subprocess.Popen(argv, stdout=subprocess.PIPE)
                stack.enter_context(child)
                gitlogs.append((path,child))

            for path, cmd in gitlogs:
                for sha, email, lines_added in parse_gitlog(path, cmd):
                    data.process_commit(sha, email, path, lines_added)

    end = time.time()
    print(f"Processed files in {int(end-start)} seconds")
    print(f"Found {len(data.commits)} commits")

    if TEST_COMMIT not in data.commits:
        raise Exception(f"Error: did not find commit {TEST_COMMIT} ?")

    print("Processing commit tags...")

    start = time.time()
    data.apply_tags()
    end = time.time()

    print(f"Processed commits in {int(end-start)} seconds")
    print(f"Saving data...")

    data.export(out_path, top_sha)
    print(f"Data saved to {out_path}")

def print_tags(args, data, in_path):
    with open(in_path) as fh:
        data = yaml.safe_load(fh)

    by_author = {}
    by_sha = {}

    include = set()
    exclude = set()
    if args.include:
        include.update(args.include)
    if args.exclude:
        exclude.update(args.exclude)

    # Go through the commits in the yaml, skipping commits based on the
    # include/exclude tags, and grouping by org or author.
    for commit in data['commits']:
        sha = commit['sha']
        tags = set(commit['tags'])

        if include and tags.isdisjoint(include):
            # we have some 'include' tags, and this commit doesn't have any of
            # them, so skip this commit
            continue
        if not tags.isdisjoint(exclude):
            # at least one of this commit's tags is excluded, so skip this
            # commit
            continue

        org = None
        author = None

        # If the commit has an org: tag, group by that org. Otherwise, just
        # group by the author.
        for tag in tags:
            if tag.startswith('org:'):
                org = tag
                break
            if tag.startswith('author:'):
                author = tag

        if org is not None:
            key = org
        else:
            key = author

        by_author.setdefault(key, [])
        by_author[key].append(sha)

        tagstr = ' '.join(sorted(commit['tags']))
        by_sha[sha] = tagstr

    # Sort authors by how many commits are by that author, so we print out the
    # 'smallest' authors first.
    authors = sorted(by_author.keys(), key=lambda x: len(by_author[x]))

    for author in authors:
        commits = by_author[author]
        print(f"{author}, {len(commits)} commits:")

        for sha in sorted(commits):
            tagstr = by_sha[sha]
            print(f" {sha}: {tagstr} ")

def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('repo')
    parser.add_argument('--apply-tags')
    parser.add_argument('--print-tags')
    parser.add_argument('--include', nargs='+')
    parser.add_argument('--exclude', nargs='+')

    args = parser.parse_args(argv)

    config = Config(args, CONFIG_FILE)

    if args.apply_tags:
        apply_tags(args, config, args.apply_tags)
    if args.print_tags:
        print_tags(args, config, args.print_tags)

if __name__ == '__main__':
    main(sys.argv[1:])
