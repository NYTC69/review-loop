"""LG2-b1: review-report.md of a report run, rendered from the run state and the findings ledger (never model-written).

Sections follow review-pr-port.md §2.5: one per ledger severity (CRITICAL -> Critical, SECURITY -> Security,
MAJOR -> Important, MINOR -> Suggestions), each finding with its role, file and phase; the pinned review inputs;
the roles that ran with their tool-use counts and the stages that did not complete; whether tests ran; and a fixed
Recommended Actions list. "Strengths" is not invented: the ledger has none.
"""
SECTIONS = (('CRITICAL', 'Critical'), ('SECURITY', 'Security'), ('MAJOR', 'Important'), ('MINOR', 'Suggestions'))
ROLE_NAMES = {'persistent-reviewer': 'EXEC reviewer', 'fresh-shadow': 'shadow', 'adversarial-gate': 'gate',
              'security-reviewer': 'security reviewer', 'security-preflight': 'security preflight'}
RECOMMENDED = ('Fix every Critical and Security finding first.', 'Then fix the Important findings.',
               'Re-run review-pr on the updated change.')


def _role(row: dict) -> str:
    source = row.get('owner_role') or row.get('source') or 'unknown'
    return ROLE_NAMES.get(source, source.replace('specialist:', 'specialist '))


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
    lines.append('- Tests: ' + (f'roles were asked to run `{test_command}`' if test_command else 'tests not run'))
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
        status = ' (discarded: ' + str(turn['discarded']) + ')' if turn.get('discarded') else ' (voided)' if turn.get('voided') else ''
        lines.append(f"- {label} ({turn.get('phase')}): {len(turn.get('observed_tool_calls') or [])} tool calls{status}")
    for failure in state.get('spawn_failures', []):
        label = names.get(failure.get('sequence')) or failure.get('role')
        lines.append(f"- {label} ({failure.get('phase')}): failed to start: {failure.get('error')}")
    for receipt in life.get('receipts', []):
        if receipt.get('stage') == 'POLISH-Q' and receipt.get('skipped'):
            lines.append('- POLISH-Q specialists: skipped (skip_quality_polish)')
    comparison = (state.get('exec_comparisons') or [{}])[-1]
    verdicts = [('EXEC reviewer', (comparison.get('persistent') or {}).get('verdict')),
                ('shadow', (comparison.get('shadow') or {}).get('verdict')),
                ('gate', (comparison.get('gate') or {}).get('verdict')),
                *(('security reviewer', (receipt.get('review') or {}).get('status')) for receipt in life.get('receipts', [])
                  if receipt.get('stage') == 'SECURITY')]
    lines += ['', '## Verdicts', *(f'- {who}: {verdict}' for who, verdict in verdicts if verdict)]
    rows = [row for row in state.get('finding_ledger', []) if row.get('status') not in ('withdrawn', 'fixed')]
    for severity, title in SECTIONS:
        found = [row for row in rows if str(row.get('severity')).upper() == severity]
        lines += ['', f'## {title} ({len(found)})']
        lines += [f"- **{row['id']}** [{_role(row)}, {row.get('phase')}] `{row.get('file') or '-'}`: {row.get('summary', '')}"
                  for row in found] or ['- none']
    lines += ['', '## Recommended Actions', *(f'{index}. {text}' for index, text in enumerate(RECOMMENDED, 1)), '']
    return '\n'.join(lines)
