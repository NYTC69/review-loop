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
                      'Only blocking-class scenarios (correctness, data loss, security, money) need a row with a test; '
                      'others are BACKLOG notes; N/A needs a one-line reason; a trivial change writes only "Trivial: <reason>".'])


BULLET = re.compile(r'^(?:[-+*]|\d+[.)])\s+')


def section(plan: str) -> str:
    heading = re.search(r'^ {0,3}##[ \t]+Scenario matrix(?:[ \t]+#+)?[ \t]*$', plan, re.I | re.M)
    if not heading:
        return ''
    body = re.split(r'^ {0,3}#{1,2}[ \t]+', plan[heading.end():], maxsplit=1, flags=re.M)[0]
    return heading[0].strip() + body.rstrip()


def validation_error(matrix: str) -> str:
    """Return the first syntax problem, or an empty string for a complete matrix."""
    if not matrix:
        return f'missing the {HEADING} section'
    lines = [(number, BULLET.sub('', line.strip()))
             for number, line in enumerate(matrix.splitlines()[1:], 2) if line.strip()]
    if lines and re.match(r'^Trivial\s*:', lines[0][1], re.I):
        if len(lines) == 1 and lines[0][1].split(':', 1)[1].strip():
            return ''
        return 'use a single Trivial: <reason> line for a trivial work item'

    covered = {}
    dimension = None
    for number, line in lines:
        # Subheadings and labels can include numbering, emphasis, or a trailing colon.
        label = BULLET.sub('', re.sub(r'^#{3,6}\s+', '', line)).strip('# *_`:\t')
        normalized = re.sub(r'[-_/]+', ' ', label).lower()
        matches = [name for name, pattern in DIMENSIONS.items() if re.search(pattern, normalized)] if (
            '|' not in line and not re.match(r'^N/A\s*:', line, re.I)) else []
        if len(matches) == 1:
            dimension = matches[0]
            if dimension in covered:
                return f'line {number} of the matrix repeats {dimension}'
            covered[dimension] = ''
            continue
        if dimension is None:
            return f'line {number} of the matrix must start with a dimension label'
        if re.fullmatch(r'N/A\s*:\s*\S.*', line, re.I) and not covered[dimension]:
            covered[dimension] = 'N/A'
            continue
        cells = [cell.strip() for cell in re.split(r'(?<!\\)\|', line)]
        if line.startswith('|'):
            cells = cells[1:]
        if line.endswith('|') and not line.endswith(r'\|'):
            cells = cells[:-1]
        if len(cells) != 3 or not all(cells) or covered[dimension] == 'N/A':
            return (f'{dimension}, line {number} of the matrix: expected '
                    'scenario | expected behavior | test id, or N/A: <reason> on its own')
        if [cell.strip('*_`').lower() for cell in cells] == ['scenario', 'expected behavior', 'test id']:
            continue
        if all(re.fullmatch(r':?-{2,}:?', cell) for cell in cells):
            continue
        covered[dimension] = 'rows'
    incomplete = [name for name in DIMENSIONS if not covered.get(name)]
    return ('missing rows or N/A: <reason> for ' + ', '.join(incomplete)) if incomplete else ''
