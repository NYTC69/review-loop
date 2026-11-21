"""Read and validate the scenario matrix in a PLAN draft."""
from pathlib import Path
import re

CHECKLIST = Path(__file__).resolve().parent.parent / 'docs' / 'protocol' / 'scenario-matrix.md'
HEADING = '## Scenario matrix'
DIMENSIONS = {
    'Input boundaries': r'\binput\b|\bboundar(?:y|ies)\b',
    'Dependency failure': r'\bdependenc(?:y|ies)\b',
    'Repetition and concurrency': r'\brepetition\b|\bconcurren\w*\b',
    'Ordering and state transitions': r'\bordering\b|\bstate transitions?\b',
    'Partial failure and recovery': r'\bpartial failure\b|\brecovery\b',
    'Permissions and identity': r'\bpermissions?\b|\bidentity\b',
    'Compatibility': r'\bcompatibility\b',
}
def prompt_checklist() -> str:
    """A compact checklist for role prompts (one line per dimension); the full rules stay in CHECKLIST."""
    lines = [line.strip() for line in CHECKLIST.read_text(encoding='utf-8').splitlines() if re.match(r'^\d+\. \*\*', line.strip())]
    return '\n'.join([f'Scenario matrix checklist (full rules: {CHECKLIST}):', *lines,
                      'Use one label line per dimension, followed by scenario | expected behavior | test id rows, '
                      'or put N/A: <reason> on the label line or the next line.',
                      'Only blocking-class scenarios (correctness, data loss, security, money) need a row with a test; '
                      'others are BACKLOG notes; N/A needs a one-line reason; a trivial change writes only "Trivial: <reason>".'])


BULLET = re.compile(r'^(?:[-+*]|\d+[.)])\s+')
NA_PREFIX = re.compile(r'N/A\s*(?::|$)', re.I)
NA = re.compile(r'N/A\s*:\s*\S.*', re.I)


def section(plan: str) -> str:
    heading = re.search(r'^ {0,3}##[ \t]+Scenario matrix(?:[ \t]+#+)?[ \t]*$', plan, re.I | re.M)
    if not heading:
        return ''
    body = re.split(r'^ {0,3}#{1,2}[ \t]+', plan[heading.end():], maxsplit=1, flags=re.M)[0]
    return heading[0].strip() + body.rstrip()


def _label_and_content(line: str) -> tuple[str, str]:
    if NA_PREFIX.match(line):
        return '', line
    label = BULLET.sub('', re.sub(r'^#{1,6}\s+', '', line))
    label = re.sub(r'^([*_]{1,3})([^|]+?):\1', r'\1\2\1:', label)
    prefix, *suffix = re.split(r'\s*(?:[:—–]|\s+-)\s*', label, maxsplit=1)
    label = BULLET.sub('', prefix.strip(' *_`')).strip(' *_`')
    normalized = re.sub(r'[-_/]+', ' ', label).lower()
    if '|' in label or len(normalized.split()) > 6:
        return '', line
    inline = suffix[0] if suffix else ''
    matches = [name for name, pattern in DIMENSIONS.items() if re.search(pattern, normalized)]
    if len(matches) == 1:
        name = matches[0]
        # Require a name or alias before an inline row so ordinary row prose stays under its label.
        if '|' not in inline or normalized == name.lower() or re.fullmatch(DIMENSIONS[name], normalized):
            return name, inline
    return '', line


def validation_error(matrix: str) -> str:
    """Check that the matrix has content for every dimension or a trivial reason."""
    if not matrix:
        return f'missing the {HEADING} section'
    lines = [BULLET.sub('', line.strip()) for line in matrix.splitlines()[1:] if line.strip()]
    if lines and re.match(r'^Trivial\s*:', lines[0], re.I):
        if lines[0].split(':', 1)[1].strip():
            return ''
        return 'use Trivial: <reason> for a trivial work item'

    covered = set()
    dimension = None
    for line in lines:
        label, line = _label_and_content(line)
        if label:
            dimension = label
        if dimension is None:
            continue
        cells = [cell.strip() for cell in re.split(r'(?<!\\)\|', line)]
        if len(cells) == 1 and NA.fullmatch(line):
            covered.add(dimension)
            continue
        if line.startswith('|'):
            cells = cells[1:]
        if line.endswith('|') and not line.endswith(r'\|'):
            cells = cells[:-1]
        if len(cells) != 3 or not all(cells):
            continue
        if [cell.strip('*_`').lower() for cell in cells] == ['scenario', 'expected behavior', 'test id']:
            continue
        if all(re.fullmatch(r':?-{2,}:?', cell) for cell in cells):
            continue
        if any(NA_PREFIX.match(cell) and not NA.fullmatch(cell) for cell in cells):
            continue
        covered.add(dimension)
    incomplete = [name for name in DIMENSIONS if name not in covered]
    return ('missing rows or N/A: <reason> for ' + ', '.join(incomplete)) if incomplete else ''
