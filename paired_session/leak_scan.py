"""V312-S: a cheap, deterministic scan of the lines a run's change adds for hardcoded credentials.

The scan reads context/delta.patch (tracked changes against the base plus new untracked files) and reports `path`,
`line` and the rule only, never the matched value. The rules are scripts/content_rules.py; added lines are text the run
adds, so this is an ADDED_TEXT scan and applies every rule of that table. The SECURITY stage's whole-delivery scan
(scripts/security_preflight.py) reads files the run never touched and applies only the table's WHOLE_DELIVERY rules.
"""
import fnmatch
from pathlib import Path
import re
import sys
from typing import Callable, Optional

try:
    from scripts import content_rules
except ModuleNotFoundError:   # coordinator.py run as a plain script: only paired_session/ is on sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
    import content_rules

ALLOW_RE = re.compile(r'(?m)^[ \t>*-]*secret-scan-allow:[ \t]*(\S.*?)[ \t]*$')


def scan_line(text: str) -> Optional[str]:
    """The rule of the first credential on one added line, else None."""
    found = content_rules.scan_text(text, content_rules.ADDED_TEXT)
    return found[0][0] if found else None


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
        number = 0
        for line in ('@@' + body).splitlines():
            if hunk := re.match(r'@@ -\d+(?:,\d+)? \+(\d+)', line):
                number = int(hunk.group(1)) - 1
            elif line.startswith(('+', ' ')):
                number += 1
                if line.startswith('+') and (kind := scan_line(line[1:])):
                    hits.append({'path': name, 'line': number, 'kind': kind})
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
