"""Worktree lifecycle W (ADR-11, docs/e2e-6): activation preconditions, receipts and stage routing."""
from pathlib import Path

try:
    from paired_session import lifecycle_spine
except ModuleNotFoundError:
    import lifecycle_spine

FORMAT = 'worktree'
DOCS_PENDING = 'worktree lifecycle stage DOCS not implemented yet (W2b)'
LANGUAGE_AGENTS = {'.go': 'go-reviewer', '.rs': 'rust-reviewer', '.py': 'python-reviewer',   # legacy Step 3.5.1
                   **dict.fromkeys(('.ts', '.tsx', '.js', '.jsx', '.html', '.vue', '.svelte'),
                                   'frontend-security-reviewer')}
QUALITY_AGENTS = ('code-reviewer', 'silent-failure-hunter', 'pr-test-analyzer')   # legacy Steps 3.5.3 and 3.5.5
SEVERITY = {'CRITICAL': 'CRITICAL', 'HIGH': 'MAJOR', 'MAJOR': 'MAJOR', 'MEDIUM': 'MAJOR', 'SECURITY': 'SECURITY',
            'MINOR': 'MINOR', 'LOW': 'MINOR'}   # doc 1 taxonomy: MEDIUM blocks, unknown labels HOLD
WAIVERS = (('accept_unverified_claude_author', '--accept-unverified-claude-author'),
           ('accept_probe_skip', '--accept-probe-skip'))
PROFILE_KEYS = ('docs_file', 'docs_allowlist', 'skip_globs', 'skip_quality_polish', 'polish_round')   # E-4: operator-only


def refuse_waivers(args):
    """D-7: a lifecycle run needs a real probe PASS; the operator waivers stay real-EXEC only.
    D-4/E-4: lifecycle keys come from the CLI or an operator profile, never an author-writable one."""
    used = [flag for dest, flag in WAIVERS if getattr(args, dest, False)]
    if used:
        raise ValueError('worktree lifecycle refuses ' + ' and '.join(used) + '; run permission-probe until it passes')
    if (keys := getattr(args, 'workspace_lifecycle_keys', None)) and getattr(args, 'action', 'run') in (
            'run', 'resume', 'permission-probe'):   # abort/status/note/accept/reject never read these keys
        raise ValueError('worktree lifecycle refuses lifecycle keys from a workspace profile: ' + ', '.join(keys) +
                         '; set them on the command line or in an operator --config')


def initial(item_uuid, parent):
    return {**lifecycle_spine.initial(item_uuid, parent), 'format': FORMAT}


def is_worktree(state):
    life = state.get('lifecycle')
    return isinstance(life, dict) and life.get('format') == FORMAT


def refuse_saved(state, args):
    """Only W-format state resumes on the real path; fake, legacy and pre-lifecycle states stay refused."""
    if not is_worktree(state):
        if 'on' in (state.get('config', {}).get('lifecycle_mode'), getattr(args, 'lifecycle_mode', None)):
            raise ValueError('saved lifecycle run cannot resume: it is not a worktree-lifecycle run')
        return
    refuse_waivers(args)   # also on reject/accept/note, whose lifecycle_mode comes from the saved config
    if state.get('claude_author_override') or state.get('probe_skip_override'):
        raise ValueError('worktree lifecycle refuses a recorded author waiver or probe-skip acceptance')


def reviewed_turns(turns):
    """The last error-free EXEC reviewer and gate turns; their snapshot_before is the tree they inspected."""
    return tuple(next((turn for turn in reversed(turns)
                       if turn.get('role') == role and turn.get('phase') == 'EXEC' and not turn.get('error')), {})
                 for role in ('reviewer', 'gate'))


def stage_request(life, role):
    """Spine request for the current stage; attempts in one epoch get distinct, replay-stable IDs."""
    attempt = sum(row['stage'] == life['stage'] and row['epoch'] == life['epoch'] for row in life['receipts'])
    request = {key: life[key] for key in ('item_uuid', 'stage', 'epoch', 'candidate_oid', 'parent')}
    request.update(request_id=f"w-{life['stage']}-{life['epoch']}-{attempt}", role=role)
    return request


def after_finish(life, receipt):
    """W router: a READY FINISH that changed the tree opens a new EXEC convergence, else POLISH-Q."""
    if receipt['status'] != 'READY':
        return life
    if receipt['output_oid'] != life['candidate_oid']:
        return {**life, 'stage': 'EXEC', 'epoch': life['epoch'] + 1, 'candidate_oid': None}
    return {**life, 'stage': 'POLISH-Q'}


def finish_prompt(plan, test_command, docs_file):
    return ('Role: finisher, fresh. Phase: FINISH.\n'
            'The approved plan below is implemented and has passed review in this worktree. Check that it is '
            f'ready to deliver: run the test command ({test_command}) and fix only defects inside the approved '
            'plan that block delivery. Do not commit, stage, push or change branches, refs or the index. Do not '
            'expand the scope, edit outside the workspace, edit run state or review files, or edit the docs file '
            f'({docs_file or "none configured"}). Do not load review-loop skills and do not invoke or wait for '
            'another model. Answer READY when nothing needed changing or your fixes are complete; answer HOLD '
            'only when a decision is missing or the environment cannot recover, with the reason. Return only '
            'JSON matching the supplied schema.\n\n'
            'Approved plan:\n' + plan)


def specialists(paths):
    """Language reviewers for the changed paths (legacy 3.5.1 map), then the code and test quality reviewers."""
    languages = sorted({LANGUAGE_AGENTS[ext] for ext in (Path(path).suffix for path in paths) if ext in LANGUAGE_AGENTS})
    return (*languages, *QUALITY_AGENTS)


def normalized_findings(name, findings):
    rows = []
    for finding in findings:
        severity = SEVERITY.get(str(finding.get('severity', '')).upper())
        if severity is None:
            raise RuntimeError(f'specialist {name} returned an unknown severity: {finding.get("severity")!r}')
        rows.append({**finding, 'severity': severity})
    return rows


def agent_body(raw):
    """The agent body without its YAML front matter, as legacy review-loop inlines it."""
    text = raw.decode('utf-8')
    if text.startswith('---\n') and '\n---\n' in text[4:]:
        text = text[4:].split('\n---\n', 1)[1]
    return text.strip()


def specialist_prompt(name, body, test_command, owned, protocol):
    ledger = ''
    if owned:
        ledger = (f'Open finding ledger ({len(owned)} open, owned by you): give each a prior_findings disposition '
                  '(fixed, withdrawn or still_open) with evidence from the current tree, and do not repeat these '
                  'findings in full_review.\n' + ''.join(f"- {row['id']}: {row['summary']}\n" for row in owned))
    return (body + '\n\n'
            f'Role: specialist {name}, fresh. Phase: POLISH-Q.\n'
            'You are a report-only quality specialist (legacy review-loop Step 3.5). Review only the changed paths of '
            'the uncommitted change in this worktree, with the instructions above. Do not modify any file. Report '
            'every finding in full_review using only the schema severities: report HIGH and MEDIUM as MAJOR and LOW '
            'as MINOR; CRITICAL and MAJOR block delivery.\n' + protocol + '\n'
            f'Run this test command exactly as written in one Bash call: {test_command}\n' + ledger +
            'Return only JSON matching the supplied schema.')
