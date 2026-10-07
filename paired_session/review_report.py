"""LG2-b1: review-report.md of a report run, rendered from the run state and the findings ledger (never model-written).

Sections follow review-pr-port.md §2.5: one per ledger severity (CRITICAL -> Critical, SECURITY -> Security,
MAJOR -> Important, MINOR -> Suggestions), each finding with its role, file and phase; the pinned review inputs;
the roles that ran with their tool-use counts and the stages that did not complete; whether tests ran; and a fixed
Recommended Actions list. "Strengths" is not invented: the ledger has none. REPORT-DEDUPE: similar reports from several
roles (same file, severity and security flag, near-duplicate summary) share one row with every id and role, labelled as
similar reports (not claimed to be one issue); every other report's summary stays listed below it.
"""
import re
from collections import Counter

SECTIONS = (('CRITICAL', 'Critical'), ('SECURITY', 'Security'), ('MAJOR', 'Important'), ('MINOR', 'Suggestions'))
# FIELD-26: as WL normalizes specialist severities (HIGH/MEDIUM -> MAJOR, LOW -> MINOR); the EXEC reviewer and the gate keep
# theirs in the ledger. Anything else goes to "Other", so no open finding is dropped.
ALIASES = {'HIGH': 'MAJOR', 'MEDIUM': 'MAJOR', 'LOW': 'MINOR'}
ROLE_NAMES = {'persistent-reviewer': 'EXEC reviewer', 'fresh-shadow': 'shadow', 'adversarial-gate': 'gate',
              'security-reviewer': 'security reviewer', 'security-preflight': 'security preflight'}
RECOMMENDED = ('Fix every Critical and Security finding first.', 'Then fix the Important findings.',
               'Re-run review-pr on the updated change.')


STOP = frozenset({'the', 'and', 'but', 'not', 'for', 'its', 'this', 'that', 'with', 'are', 'was', 'has', 'only', 'own', 'any',
                   'all', 'under', 'from', 'into', 'than', 'then', 'there', 'which', 'what', 'does', 'new', 'still', 'also', 'even'})


def _words(text) -> set:
    """A summary's words for near-duplicate detection: lower case, without the [class: ...] label, ledger ids and stopwords."""
    text = re.sub(r'\bf\d{3,}\b', ' ', re.sub(r'\[class:[^\]]*\]', ' ', str(text).lower()))
    return {word for word in re.findall(r'[a-z0-9]+', text) if word not in STOP and (len(word) > 2 or word.isdigit())}


def _same_issue(a: set, b: set) -> bool:
    """Conservative: most of the shorter summary's words and a quarter of all words are shared (PR #6: the same issue
    scored >= 0.65 / 0.31 against its first report, the closest different issue 0.52 / 0.28)."""
    shared = len(a & b)
    return bool(a and b) and shared / min(len(a), len(b)) >= 0.6 and shared / len(a | b) >= 0.25


def _merged(rows: list) -> list:
    """REPORT-DEDUPE: one section's rows, grouped when several roles file similar reports: the same file, severity and
    security flag, and a summary close to the group's first row (never by chaining). Every id is kept; the ledger is not changed."""
    groups = []
    for row in rows:
        key, words = (row.get('file'), str(row.get('severity')).upper(), bool(row.get('security'))), _words(row.get('summary', ''))
        group = next((g for g in groups if g[0] == key and _same_issue(g[1], words)), None)
        group[2].append(row) if group else groups.append((key, words, [row]))
    return [group[2] for group in groups]


def _line(group: list, section: str) -> str:
    """One report row: every id and role of a merged group and the first report's file and summary; every other report's
    summary stays visible, indented below it, so a wrong merge hides nothing."""
    first, own = group[0], str(group[0].get('severity')).upper()
    roles = '; '.join(dict.fromkeys(f"{_role(row)}, {row.get('phase')}" for row in group))
    more = f' ({len(group)} similar reports, each listed; they may still be separate issues)' if len(group) > 1 else ''
    return '\n'.join([f"- **{', '.join(row['id'] for row in group)}** [{roles}{'' if own == section else ', ' + own}] "
                      f"`{first.get('file') or '-'}`: {first.get('summary', '')}{more}",
                      *(f"  - {row['id']} [{_role(row)}, {row.get('phase')}]: {row.get('summary', '')}" for row in group[1:])])


def _role(row: dict) -> str:
    source = row.get('owner_role') or row.get('source') or 'unknown'
    return ROLE_NAMES.get(source, source.replace('specialist:', 'specialist '))


def _section(row: dict) -> str:
    severity = str(row.get('severity')).upper()
    return ALIASES.get(severity, severity if severity in dict(SECTIONS) else 'OTHER')


def _verdicts(state: dict) -> list:
    """The last EXEC round's reviewer, shadow and gate verdicts, the last POLISH-Q specialists' and every SECURITY review's."""
    life = state.get('lifecycle') or {}
    polish = [receipt for receipt in life.get('receipts', []) if receipt.get('stage') == 'POLISH-Q']
    comparison = (state.get('exec_comparisons') or [{}])[-1]
    raw = (comparison.get('persistent') or {}).get('verdict')   # its turn's effective verdict, e.g. RF-5 APPROVE -> REVISE
    effective = next((row.get('effective_verdict') for row in state.get('review_verdicts', [])
                      if row.get('sequence') == comparison.get('review_sequence')), None)
    return [('EXEC reviewer', f'{raw} → {effective}' if raw and effective and effective != raw else raw),
            *(('specialist ' + str(turn.get('name')), turn.get('status')) for turn in (polish[-1:] or [{}])[0].get('specialist_turns') or []),
            ('shadow', (comparison.get('shadow') or {}).get('verdict')),
            ('gate', (comparison.get('gate') or {}).get('verdict')),
            *(('security reviewer', (receipt.get('review') or {}).get('status')) for receipt in life.get('receipts', [])
              if receipt.get('stage') == 'SECURITY')]


def delivery_section(state: dict) -> str:
    """L120: the legacy Step 4 summary fields for both accept routes' delivery-report.md: every ledger finding per severity
    with its final status, the PLAN/EXEC rounds, the verdicts and the token totals per vendor (usage.md has the detail)."""
    lines = ['## 发现（按严重度，最终状态）']
    for severity, title in (*SECTIONS, ('OTHER', 'Other')):
        found = [row for row in state.get('finding_ledger', []) if _section(row) == severity]
        statuses = Counter(row.get('status') for row in found)
        open_ids = [row['id'] for row in found if row.get('status') == 'open']
        if found or severity != 'OTHER':
            lines.append(f'- {title}：{len(found)} 条' + ('（' + '，'.join(f'{s} {n}' for s, n in statuses.items()) + '）' if found else '')
                         + (f"；未关闭：{', '.join(open_ids)}" if open_ids else ''))
    verdicts = [f'- {who}：{verdict}' for who, verdict in _verdicts(state) if verdict] or ['- 无']
    lines += ['', '## 轮次', f"- PLAN {state.get('plan_rounds', 0)} 轮，EXEC {state.get('exec_rounds', 0)} 轮",
              '', '## 判定（最后一轮）', *verdicts, '', '## Token 用量（按供应商，明细见 usage.md）']
    vendors = {}
    for turn in state.get('turns', []):
        row = vendors.setdefault(turn.get('vendor') or 'unknown', {'turns': 0, 'input': 0, 'cached': 0, 'output': 0, 'unknown': 0})
        row['turns'] += 1
        row['unknown'] += not turn.get('usage_requests')
        for use in turn.get('usage_requests') or []:
            for key in ('input', 'cached', 'output'):
                row[key] += use.get(key, 0)
    lines += [f"- {vendor}：{row['turns']} 次调用，输入 {row['input']}（其中缓存 {row['cached']}），输出 {row['output']}"
              + (f"；{row['unknown']} 次无用量记录" if row['unknown'] else '') for vendor, row in vendors.items()] or ['- 无']
    return '\n'.join(lines) + '\n'


def render(state: dict) -> str:
    report = state.get('report') or {}
    complete = bool(report.get('complete'))
    record, config = state.get('review_only') or {}, state.get('config') or {}
    lines = ['# Review report', '',
             'Status: ' + ('REPORTED (complete)' if complete else
                           f"incomplete, the run is on HOLD: {report.get('hold_reason') or state.get('hold_reason') or 'unknown'}")]
    if not complete and report.get('not_completed'):
        lines.append('Not completed: ' + ', '.join(report['not_completed']))
    pinned = [('Review base', config.get('review_base')), ('Head at start', record.get('head_at_start')),
              ('Reviewed tree', record.get('candidate_tree_sha256'))]
    pinned += [(label, value) for label, value in (state.get('review_pr') or {}).items()]   # LG2-d adds the PR pins
    lines += ['', '## Reviewed', *(f'- {label}: `{value}`' for label, value in pinned if value)]
    if config.get('review_aspects'):
        lines.append('- Aspects: ' + ','.join(config['review_aspects']))
    test_command = config.get('test_command')
    static = sum(1 for turn in state.get('turns', []) if turn.get('static_untested_approval'))
    lines.append('- Tests: ' + (f'roles were asked to run `{test_command}`' if test_command else 'tests not run') +
                 (f'; {static} approvals are static and untested' if static else ''))
    life = state.get('lifecycle') or {}
    # every specialist dispatches as role reviewer in POLISH-Q: name each sequence it used (retries and failures too)
    names = {int(seq): 'specialist ' + str(name) for seq, name in (life.get('specialist_sequences') or {}).items()}
    for receipt in life.get('receipts', []):
        for turn in receipt.get('specialist_turns') or []:
            names[turn.get('sequence')] = 'specialist ' + str(turn.get('name'))
    for turn in (life.get('specialist_done') or {}).values():
        names[turn.get('sequence')] = 'specialist ' + str(turn.get('name'))
    phase_names = {('reviewer', 'EXEC'): 'EXEC reviewer', ('reviewer', 'SECURITY'): 'security reviewer'}
    lines += ['', '## Roles']
    for turn in state.get('turns', []):
        if turn.get('role') in ('probe', 'gate-probe'):
            continue
        label = names.get(turn.get('sequence')) or phase_names.get((turn.get('role'), turn.get('phase'))) or turn.get('role')
        status = (' (discarded: ' + str(turn['discarded']) + ')' if turn.get('discarded') else ' (voided)' if turn.get('voided') else
                  ' (static, untested approval)' if turn.get('static_untested_approval') else '')
        lines.append(f"- {label} ({turn.get('phase')}): {len(turn.get('observed_tool_calls') or [])} tool calls{status}")
    for failure in state.get('spawn_failures', []):
        label = names.get(failure.get('sequence')) or failure.get('role')
        lines.append(f"- {label} ({failure.get('phase')}): failed to start: {failure.get('error')}")
    for receipt in life.get('receipts', []):
        if receipt.get('stage') == 'POLISH-Q' and receipt.get('skipped'):
            reason = ('skip_quality_polish' if config.get('skip_quality_polish') else
                      'no specialist selected for aspects ' + ','.join(config.get('review_aspects') or ()))
            lines.append(f'- POLISH-Q specialists: skipped ({reason})')
    lines += ['', '## Verdicts', *(f'- {who}: {verdict}' for who, verdict in _verdicts(state) if verdict)]
    rows = [row for row in state.get('finding_ledger', []) if row.get('status') not in ('withdrawn', 'fixed')]
    for severity, title in (*SECTIONS, ('OTHER', 'Other')):
        found = [row for row in rows if _section(row) == severity]
        if severity == 'OTHER' and not found:
            continue
        lines += ['', f'## {title} ({len(found)})']
        lines += [_line(group, severity) for group in _merged(found)] or ['- none']
    lines += ['', '## Recommended Actions', *(f'{index}. {text}' for index, text in enumerate(RECOMMENDED, 1)), '']
    return '\n'.join(lines)
