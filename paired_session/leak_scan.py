"""V312-S: a cheap, deterministic scan of the lines a run's change adds for hardcoded credentials.

The scan reads context/delta.patch (tracked changes against the base plus new untracked files) and reports `path`,
`line` and the rule only, never the matched value. The rules are scripts/content_rules.py; added lines are text the run
adds, so this is an ADDED_TEXT scan and applies every rule of that table. The SECURITY stage's whole-delivery scan
(scripts/security_preflight.py) reads files the run never touched and applies only the table's WHOLE_DELIVERY rules.
"""
import fnmatch
import importlib.util
from pathlib import Path
import re
from typing import Callable, Optional

# Loaded by file path: this repository's scripts/ is a namespace package, so `from scripts import content_rules` would
# resolve to any regular `scripts` package on sys.path (a user's project on PYTHONPATH) and fail at coordinator start.
_spec = importlib.util.spec_from_file_location('paired_session_content_rules',
                                               Path(__file__).resolve().parents[1] / 'scripts' / 'content_rules.py')
content_rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(content_rules)

ALLOW_RE = re.compile(r'(?m)^[ \t>*-]*secret-scan-allow:[ \t]*(\S.*?)[ \t]*$')


def scan_block(text: str) -> list:
    """(line, rule) of the first credential on each line of one block of added lines, lines counted from 1. A private-key
    marker and the key body on the next added line are one hit, at the marker's line."""
    first = {}
    for rule, line in content_rules.scan_text(text, content_rules.ADDED_TEXT):
        first.setdefault(line, rule)
    return sorted(first.items())


def scan_line(text: str) -> Optional[str]:
    """The rule of the first credential on one added line, else None."""
    found = scan_block(text)
    return found[0][1] if found else None


def scan_patch(patch: str, unquote: Callable[[str], Optional[str]] = lambda body: body) -> list[dict]:
    """Every added line of a git patch that carries a credential, as {'path', 'line', 'kind'}; never the value."""
    hits = []
    for section in re.split(r'(?m)^(?=diff --git )', patch):
        head, _, body = section.partition('\n@@')
        # git ends the header of a path that contains a space with a tab ("+++ b/new file.py\t"): a separator, not the name
        name = next((line[4:].rstrip('\t') for line in head.splitlines() if line.startswith('+++ ')), None)
        if not body or name is None or name == '/dev/null':
            continue
        if name.startswith('"') and name.endswith('"'):
            name = unquote(name[1:-1]) or name
        name = name[2:] if name.startswith('b/') else name
        number, runs = 0, []                                         # runs: [first line, lines] per run of consecutive added lines
        for line in ('@@' + body).splitlines():
            if hunk := re.match(r'@@ -\d+(?:,\d+)? \+(\d+)', line):
                number = int(hunk.group(1)) - 1
            elif line.startswith(('+', ' ')):
                number += 1
                if line.startswith('+') and runs and runs[-1][0] + len(runs[-1][1]) == number:
                    runs[-1][1].append(line[1:])
                elif line.startswith('+'):
                    runs.append([number, [line[1:]]])
        for first, lines in runs:
            hits += [{'path': name, 'line': first + line - 1, 'kind': kind} for line, kind in scan_block('\n'.join(lines))]
    return hits


def allow_patterns(workitem: str) -> list[str]:
    """The `secret-scan-allow: <path or glob>` lines of a work item (one per line; backticks or quotes stripped)."""
    return [match.group(1).strip('`"\'') for match in ALLOW_RE.finditer(workitem) if match.group(1).strip('`"\'')]


def exempt(path: str, patterns: list[str]) -> bool:
    """A path matches a listed path, a directory prefix or a glob."""
    return any(path == pattern or path.startswith(pattern.rstrip('/') + '/') or fnmatch.fnmatchcase(path, pattern)
               for pattern in patterns)


def where(hit: dict) -> str:
    return f"{hit['path']}:{hit['line']} ({hit['kind']})"
