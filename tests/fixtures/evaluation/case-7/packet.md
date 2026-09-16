# Review packet — evaluation case 7 (live Reviewer smoke)

There is no session file for this smoke run. This packet is the complete context;
do not look for `.review-loop/sessions/`. The post-change tree is under
`tests/fixtures/evaluation/case-7/repo/` plus the new file quoted below. Return
exactly the structured verdict format from your instructions above
(`### VERDICT:` / `### Issues` / `### Strengths`) and nothing else. Every
`[CRITICAL]` must carry the six rubric fields as indented continuation lines.

## Intent and acceptance criteria

Work item: add a command-line entry point for the retention sweep. Acceptance
criteria:
1. `pkg/cli.py` parses `--keep-days` (integer, default 30) and calls
   `purge(directory, ages, keep_days)`.
2. The existing `purge()` behaviour is reused as is.

Operators run this CLI by hand and from shell scripts; `--keep-days` is
sometimes filled from an environment variable.

## Attributable Delta

`pkg/retention.py` (post-change, docstring touched only):

```python
"""Retention sweep (used by pkg.cli)."""
import os


def expired(entries, keep_days):
    """Entries older than keep_days."""
    return [e for e in entries if e["age_days"] > keep_days]


def purge(directory, ages, keep_days):
    """Delete files older than keep_days (keep_days <= 0 deletes everything)."""
    for name, age in ages.items():
        if keep_days <= 0 or age > keep_days:
            os.remove(os.path.join(directory, name))
```

`pkg/cli.py` (new):

```python
"""Command line entry point for the retention sweep."""
import argparse

from pkg.retention import purge


def main(argv, directory, ages):
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-days", type=int, default=30)
    args = parser.parse_args(argv)
    purge(directory, ages, args.keep_days)
    return 0
```

## Evidence

| id | claim | disposition | result | provenance | valid |
|---|---|---|---|---|---|
| 1 | `tests:python3 -m unittest discover -q -s tests -t .` | executed | PASS | fresh | yes |

## Unresolved findings + author response

None.

## Declared deviations

None.

## Your Task

Review the delta above against the acceptance criteria and the blocking
rubric in your instructions. Anchor each finding with `File:` and a line range
(for example ``File: `pkg/retention.py`, lines 12-14``).
