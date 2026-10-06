"""Worktree lifecycle W (ADR-11, docs/e2e-6): activation preconditions, receipts and stage routing."""
import json
from pathlib import Path

try:
    from paired_session import docs_policy, lifecycle_spine
except ModuleNotFoundError:
    import docs_policy
    import lifecycle_spine

FORMAT = 'worktree'
DOCS_HOLD_PARTS = {*docs_policy.PROTECTED_PARTS, '.claude-plugin', '.codex-plugin', 'plugin.json', 'marketplace.json'}
LANGUAGE_AGENTS = {'.go': 'go-reviewer', '.rs': 'rust-reviewer', '.py': 'python-reviewer',   # legacy Step 3.5.1
                   **dict.fromkeys(('.ts', '.tsx', '.js', '.jsx', '.html', '.vue', '.svelte'),
                                   'frontend-security-reviewer')}
QUALITY_AGENTS = ('code-reviewer', 'silent-failure-hunter', 'pr-test-analyzer')   # legacy Steps 3.5.3 and 3.5.5
SEVERITY = {'CRITICAL': 'CRITICAL', 'HIGH': 'MAJOR', 'MAJOR': 'MAJOR', 'MEDIUM': 'MAJOR', 'SECURITY': 'SECURITY',
            'MINOR': 'MINOR', 'LOW': 'MINOR'}   # doc 1 taxonomy: MEDIUM blocks, unknown labels HOLD
WAIVERS = (('accept_unverified_claude_author', '--accept-unverified-claude-author'),
           ('accept_probe_skip', '--accept-probe-skip'))
PROFILE_KEYS = ('docs_file', 'docs_allowlist', 'skip_globs', 'skip_quality_polish', 'polish_round',   # E-4: operator-only
                'auto_commit', 'external_delivery')


def refuse_waivers(args):
    """D-7 (strict only, D-EFF): a strict lifecycle run needs a real probe PASS; the operator waivers stay real-EXEC only.
    An efficient run has no probe gate, so main() notes a waiver as unneeded and records none.
    D-4/E-4 (both modes): lifecycle keys come from the CLI or an operator profile, never an author-writable one."""
    used = [flag for dest, flag in WAIVERS if getattr(args, dest, False)]
    if used and getattr(args, 'safety_mode', 'strict') != 'efficient':   # Coordinator.strict; resolved before this is called
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
    refuse_waivers(args)   # also on reject/accept/note, whose lifecycle_mode and safety_mode come from the saved config
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


def finish_prompt(plan, test_command, reserved, review_only=False):
    subject, label = (('The change described by the review scope below', 'Review scope (no plan was approved)') if review_only
                      else ('The approved plan below', 'Approved plan'))   # LG1-b
    return ('Role: finisher, fresh. Phase: FINISH.\n'
            f'{subject} is implemented and has passed review in this worktree. Check that it is '
            f'ready to deliver: run the test command ({test_command}) and fix only defects inside the '
            f'{"review scope" if review_only else "approved plan"} that block delivery. Do not commit, stage, push or change branches, refs or the index. Do not '
            f'expand the scope, edit outside the workspace or edit run state or review files. {reserved} '
            'Do not load review-loop skills and do not invoke or wait for '
            'another model. Answer READY when nothing needed changing or your fixes are complete; answer HOLD '
            'only when a decision is missing or the environment cannot recover, with the reason. Return only '
            'JSON matching the supplied schema.\n\n'
            f'{label}:\n' + plan)


def docs_prompt(docs_file, allowlist, run_id, workitem):
    return ('Role: docs writer, fresh. Phase: DOCS.\n'
            'The reviewed change in this worktree is ready for delivery. Bring the documentation in line with it '
            '(legacy review-loop Step 3.6): update docs that describe changed behavior, APIs or logic, and fix stale '
            'comments in the changed files. Documentation paths you may write: ' + (', '.join(allowlist) or 'none') +
            '. Any other write (a comment or code fix) sends the change back through EXEC review and the gate; agent, '
            'skill, protocol, config, manifest and Git files and symlinks are refused. ' +
            (f'Add one entry for run {run_id} to {docs_file} with the work item, the changes and the review results, '
             'replacing an earlier entry for this run. ' if docs_file else '') +
            'Do not commit, stage, push or change branches, refs or the index. Do not edit outside the workspace or '
            'edit run state or review files. Do not load review-loop skills and do not invoke or wait for another '
            'model. Answer READY when the docs are consistent; answer HOLD only when a decision is missing or the '
            'environment cannot recover. Return only JSON matching the supplied schema.\n\nWork item:\n' + workitem)


def docs_denied(path, value):
    """The DOCS HOLD set (doc 6): protected names at any depth, docs/protocol, and any symlink write."""
    parts = tuple(part.casefold() for part in path.split('/'))
    return str(value).startswith('link:') or bool(DOCS_HOLD_PARTS & set(parts)) or parts[:2] == ('docs', 'protocol')


REPORT_ASPECTS = ('code', 'errors', 'comments', 'types', 'tests')   # LG2-b1: legacy review-pr's aspect names
TYPE_SUFFIXES = ('.ts', '.tsx', '.py', '.rs', '.go', '.java', '.kt', '.swift')
COMMENT_MARKERS = ('#', '//', '/*', '*', '"""', "'''", '<!--', '--')


def report_specialists(paths, aspects=REPORT_ASPECTS, comment_lines=False):
    """LG2-b1 (review-pr-port.md §2.4): a report run's specialists. Language reviewers by suffix always run; the
    aspects select code-reviewer, silent-failure-hunter and pr-test-analyzer, and the two report-only analyzers:
    comment-analyzer when a changed path is docs-like or the change touches comment lines, type-design-analyzer when
    a changed path has a type-bearing suffix (deterministic, no model judgment)."""
    languages = sorted({LANGUAGE_AGENTS[ext] for ext in (Path(path).suffix for path in paths) if ext in LANGUAGE_AGENTS})
    docs_like = any(path.endswith('.md') or path.startswith('docs/') or '/docs/' in path for path in paths)
    chosen = {'code': 'code-reviewer', 'errors': 'silent-failure-hunter', 'tests': 'pr-test-analyzer',
              'comments': 'comment-analyzer' if docs_like or comment_lines else None,
              'types': 'type-design-analyzer' if any(Path(path).suffix in TYPE_SUFFIXES for path in paths) else None}
    return (*languages, *(chosen[aspect] for aspect in REPORT_ASPECTS if aspect in aspects and chosen[aspect]))


def touches_comment_lines(diff_text):
    """True when an added or removed line of a unified diff is a comment line (by its leading marker)."""
    return any(line[:1] in '+-' and not line.startswith(('+++', '---')) and line[1:].lstrip().startswith(COMMENT_MARKERS)
               for line in diff_text.splitlines())


def specialists(paths):
    """Language reviewers for the changed paths (legacy 3.5.1 map), then the code and test quality reviewers."""
    languages = sorted({LANGUAGE_AGENTS[ext] for ext in (Path(path).suffix for path in paths) if ext in LANGUAGE_AGENTS})
    return (*languages, *QUALITY_AGENTS)


def normalized_findings(name, findings, label=None):
    rows = []
    for finding in findings:
        severity = SEVERITY.get(str(finding.get('severity', '')).upper())
        if severity is None:
            raise RuntimeError(f'{label or "specialist " + name} returned an unknown severity: {finding.get("severity")!r}')
        rows.append({**finding, 'severity': severity})
    return rows


def agent_body(raw):
    """The agent body without its YAML front matter, as legacy review-loop inlines it."""
    text = raw.decode('utf-8')
    if text.startswith('---\n') and '\n---\n' in text[4:]:
        text = text[4:].split('\n---\n', 1)[1]
    return text.strip()


def delivery_report(state, run_id, workitem, delivery):
    """The Chinese delivery report (doc 6): stage outcomes, SECURITY, delivery and totals, never secrets."""
    life = state['lifecycle']
    last = {row['stage']: row for row in life['receipts']}
    security = last.get('SECURITY', {})
    preflight, review = security.get('preflight') or {}, security.get('review') or {}
    open_ids = [row['id'] for row in state['finding_ledger'] if row.get('status') == 'open']
    minutes = (state.get('completed_at') or state['started_at']) - state['started_at']
    title = next((line.lstrip('# ').strip() for line in workitem.splitlines() if line.strip()), '')
    lines = ['# 交付报告（worktree lifecycle）', '',
             f'- 运行：`{run_id}`；工作项：{title}',
             f"- 结论：已接受（ACCEPTED），{state.get('accepted_at', '')}",
             ('- 交付：auto_commit 开启，本地提交 `' + str(delivery.get('commit')) + '`（父提交 `' + str(delivery.get('head'))
              + '`），未推送。' if delivery.get('auto_commit') else '- 交付：auto_commit 关闭，没有改动任何 ref 或 index。'),
             *([f"- review-only：review base `{state['config']['review_base']}`，开始时 HEAD `{state['review_only']['head_at_start']}`；"
                f"审查的已有提交：{', '.join(delivery.get('reviewed_commits') or []) or '无（只有未提交的改动）'}"]
               if state.get('review_only') else []),
             '- 外部交付（push、PR、merge）：未执行（D8）。',
             '- 各阶段（最后一条 receipt）：' + '；'.join(
                 f"{stage} {row.get('status')}/{row.get('route', '-')}（epoch {row.get('epoch')}）"
                 for stage, row in last.items()),
             f"- SECURITY：敏感路径 {len(security.get('sensitive_paths') or [])} 个；preflight {preflight.get('status')}，"
             f"扫描 {preflight.get('scanned_files')} 个文件；安全评审 {review.get('status')}",
             f"- 未关闭的发现：{len(open_ids)} 条" + (f"（{', '.join(open_ids)}）" if open_ids else ''),
             f"- 用量：调用 {state.get('invocations_used')} 次，epoch {life.get('epoch')}，用时约 {minutes / 60:.0f} 分钟", '']
    if written := state.get('ignored_config_written'):   # F3: outside the review snapshot, so no reviewer saw them
        lines += ['## Ignored executable-config files written by the author', '',
                  '被 git 忽略、不在审查快照里的可执行配置文件（编辑器任务、MCP、shell hook 等）；在任何工具运行它们之前先检查：',
                  *(f'- `{name}`' for name in written), '']
    return '\n'.join(lines)


def owned_ledger(owned):
    if not owned:
        return ''
    return (f'Open finding ledger ({len(owned)} open, owned by you): give each a prior_findings disposition '
            '(fixed, withdrawn or still_open) with evidence from the current tree, and do not repeat these '
            'findings in full_review.\n' + ''.join(f"- {row['id']}: {row['summary']}\n" for row in owned))


def specialist_prompt(name, body, test_command, owned, protocol, change='the uncommitted change in this worktree'):
    ledger = owned_ledger(owned)
    return (body + '\n\n'
            f'Role: specialist {name}, fresh. Phase: POLISH-Q.\n'
            'You are a report-only quality specialist (legacy review-loop Step 3.5). Review only the changed paths of '
            f'{change}, with the instructions above. Do not modify any file. Report '
            'every finding in full_review using only the schema severities: report HIGH and MEDIUM as MAJOR and LOW '
            'as MINOR; CRITICAL and MAJOR block delivery.\n' + protocol + '\n' +
            (f'Run this test command exactly as written in one Bash call: {test_command}\n' if test_command else
             'No test command is configured for this review: do not run tests; mark any claim that needs a test run as '
             'unverified.\n') + ledger +
            'Return only JSON matching the supplied schema.')


def delivered_findings(source, rows):
    """The delivered review for an author repair turn (same shape as the gate's)."""
    return json.dumps({'source': source, 'status': 'REVISE', 'findings': [
        {key: row.get(key, '') for key in ('id', 'severity', 'file', 'summary', 'failure_scenario')} for row in rows]},
        ensure_ascii=False)
