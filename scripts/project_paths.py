#!/usr/bin/env python3
"""Project-local persistent output roots (scratch remains external)."""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def project_repo(workspace):
    try:
        result = subprocess.run(
            ['git', '-C', str(workspace), 'rev-parse', '--is-bare-repository',
             '--path-format=absolute', '--git-common-dir'],
            capture_output=True, text=True, check=True)
        bare, common_path = result.stdout.strip().split('\n', 1)
        common = Path(common_path)
        if bare == 'false' and common.is_absolute() and common.name == '.git':
            parent = subprocess.run(
                ['git', '-C', str(common.parent), 'rev-parse', '--is-inside-work-tree'],
                capture_output=True, text=True, check=True)
            if parent.stdout.strip() == 'true':
                return common.parent
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass
    return None


def ensure_ignore(repo):
    ignore = Path(repo) / '.review-loop' / '.gitignore'
    ignore.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ignore.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write('*\n!config.md\n')
    except FileExistsError:
        pass


def default_root(workspace):
    repo = project_repo(workspace)
    if repo is not None:
        ensure_ignore(repo)
        return repo / '.review-loop'
    state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state')
    if not state.is_absolute():
        state = Path.home() / '.local/state'
    root = state / 'review-loop'
    print('NOTE: main project checkout unavailable (not a Git repo, a bare repo or a submodule/separate-git-dir checkout); '
          'falling back to ' + str(root), file=sys.stderr)
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace', nargs='?', default='.')
    args = parser.parse_args()
    print(default_root(args.workspace))


if __name__ == '__main__':
    main()
