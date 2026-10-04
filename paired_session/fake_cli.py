#!/usr/bin/env python3
"""Offline protocol fake for S4 tests; never used by the real run."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
import uuid


def readonly_profile(args):
    """b295-f1: the filesystem table of the profile that `default_permissions` selects in a Codex argv ({} when absent; the last
    --config wins, as in Codex)."""
    selected = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg in ('-c', '--config') and args[i + 1].startswith('default_permissions=')]
    if not selected:
        return {}
    key = 'permissions.' + selected[-1].split('=', 1)[1].strip('"') + '.filesystem='
    raw = next((arg[len(key):] for arg in args if arg.startswith(key)), '')
    try: return json.loads(raw.replace('"=', '":'))
    except ValueError: return {}


def emit_codex(answer, session, command_events=None):
    model = os.environ.get('FAKE_CODEX_MODEL', 'gpt-6.1-sol')
    print(json.dumps({'type': 'thread.started', 'thread_id': session, 'model': model}))
    print(json.dumps({'type': 'turn.started', 'model': model}))
    if os.environ.get('FAKE_STREAM_TWO_USAGE_THEN_HANG'):
        sys.stdout.flush()
        codex_home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        rollout = codex_home / 'sessions' / datetime.now(timezone.utc).strftime('%Y/%m/%d') / (
            'rollout-' + session + '.jsonl')
        rollout.parent.mkdir(parents=True, exist_ok=True)
        totals = {'input_tokens': 0, 'cached_input_tokens': 0, 'output_tokens': 0}
        with rollout.open('a') as handle:
            for input_tokens, cached_tokens, output_tokens in ((31, 7, 9), (17, 3, 5)):
                last = {'input_tokens': input_tokens, 'cached_input_tokens': cached_tokens,
                        'output_tokens': output_tokens}
                for key, value in last.items():
                    totals[key] += value
                row = {'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                       'type': 'event_msg',
                       'payload': {'type': 'token_count', 'info': {
                           'last_token_usage': last, 'total_token_usage': dict(totals)}}}
                handle.write(json.dumps(row) + '\n')
                handle.flush()
            handle.write(json.dumps({'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None}}) + '\n')
            handle.write(json.dumps(row) + '\n')
            handle.flush()
        import time
        time.sleep(30)
    if command_events and not os.environ.get('FAKE_MISSING_OBSERVED'):
        for index, event in enumerate(command_events):
            print(json.dumps({'type': 'item.completed', 'item': {
                'id': 'fake-command-' + str(index), 'type': 'command_execution',
                'command': event['command'], 'exit_code': event['exit_code'],
                'status': 'completed' if event['exit_code'] == 0 else 'failed',
                'aggregated_output': event.get('output', 'fake permission result'),
            }}))
    if command_events and os.environ.get('FAKE_CODEX_ROLLOUT_CWD'):   # v297-eg-cwd: Codex's own record of where each command ran
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        rows = [{'timestamp': now, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'fake-turn-' + session}},
                {'timestamp': now, 'type': 'turn_context', 'payload': {'cwd': str(Path.cwd().resolve())}}]
        rows += [{'timestamp': now, 'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': 'fake-turn-' + session, 'item': {
            'type': 'CommandExecution', 'command': ['/bin/zsh', '-lc', event['command']], 'cwd': Path.cwd().resolve().as_uri()}}}
                 for event in command_events]
        rollout = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'sessions' / ('rollout-' + session + '.jsonl')
        rollout.parent.mkdir(parents=True, exist_ok=True)
        with rollout.open('a') as handle:
            handle.write(''.join(json.dumps(row) + '\n' for row in rows))
    if os.environ.get('FAKE_MALFORMED_MODEL_STREAM') == 'codex':
        print('{malformed model metadata')
    print(json.dumps({'type': 'item.completed', 'item': {'id': 'fake', 'type': 'agent_message',
                                                         'text': json.dumps(answer)}}))
    print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 100,
                                                          'cached_input_tokens': 60,
                                                          'output_tokens': 20}}))


def emit_claude(answer, session, extra_commands=None):
    model = os.environ.get('FAKE_CLAUDE_MODEL', 'claude-opus-5-5')
    print(json.dumps({'type': 'system', 'subtype': 'init', 'model': model,
                      'session_id': session}))
    print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_start',
          'message': {'id': 'fake-message', 'usage': {'input_tokens': 10,
          'cache_creation_input_tokens': 5, 'cache_read_input_tokens': 20, 'output_tokens': 0}}},
          'session_id': session}))
    print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_delta',
          'usage': {'output_tokens': 12}}, 'session_id': session}))
    if os.environ.get('FAKE_STREAM_TWO_USAGE_THEN_HANG'):
        sys.stdout.flush()
        print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_start',
              'message': {'id': 'fake-message-2', 'usage': {'input_tokens': 7,
              'cache_creation_input_tokens': 2, 'cache_read_input_tokens': 4,
              'output_tokens': 0}}}, 'session_id': session}), flush=True)
        print(json.dumps({'type': 'stream_event', 'event': {'type': 'message_delta',
              'usage': {'output_tokens': 8}}, 'session_id': session}), flush=True)
        import time
        time.sleep(30)
    if not os.environ.get('FAKE_MISSING_OBSERVED'):
        evidence_rows = [*answer.get('self_run_evidence', []), *(extra_commands or [])]
        evidence_rows.sort(key=lambda row: not row.get('force_failure', False))
        for index, evidence in enumerate(evidence_rows):
            tool_id = 'fake-tool-' + str(index)
            command = evidence['command']
            if os.environ.get('FAKE_REVIEW_NO_TEST_EVENT') and command == 'python3 -m unittest':
                continue
            forbidden = command.startswith(('echo ', 'git checkout', 'git --literal-pathspecs checkout', 'rm ', 'git diff --output=',
                                             'git log --output=', 'git show --output=')) or ' > ' in command
            test_failure = bool(evidence.get('force_failure') or
                                (os.environ.get('FAKE_REVIEW_TEST_FAILURE') and command == 'python3 -m unittest'))
            if (os.environ.get('FAKE_SANDBOX_WRITE') and
                    ('paired-session-claude-sandbox-' in command or
                     '.paired-session-run-dir-probe-' in command or
                     '.paired-session-context-probe-' in command) and ' > ' in command):
                parts = shlex.split(command)
                target = Path(parts[parts.index('>') + 1])
                target.write_text('probe escaped sandbox\n')
                forbidden = False
            sandbox_denial = (('paired-session-claude-sandbox-' in command or
                               '.paired-session-os-probe-' in command or
                               '.paired-session-run-dir-probe-' in command or
                               '.paired-session-context-probe-' in command) and ' > ' in command
                              and forbidden)
            if os.environ.get('FAKE_WRAPPED_TEST') and not forbidden:
                command += ' 2>&1; echo "EXIT_CODE: $?"'
            print(json.dumps({'type': 'assistant', 'message': {'content': [{
                'type': 'tool_use', 'id': tool_id, 'name': 'Bash',
                'input': {'command': command}}]}, 'session_id': session}))
            print(json.dumps({'type': 'user', 'message': {'content': [{
                'type': 'tool_result', 'tool_use_id': tool_id,
                'content': (('line\n' * 20000) if os.environ.get('FAKE_HUGE_OUTPUT') else
                            'FAILED fake configured test' if test_failure else
                            ('fake sandbox failure without OS marker' if sandbox_denial and os.environ.get('FAKE_SANDBOX_NO_OS_MARKER') else
                             'zsh: operation not permitted: ' + command.split()[-1] if sandbox_denial else 'fake result')),
                'is_error': forbidden or test_failure}]},
                'session_id': session}))
    if os.environ.get('FAKE_MALFORMED_MODEL_STREAM') == 'claude':
        print('{malformed model metadata')
    sensitive = os.environ.get('FAKE_SENSITIVE_READ')
    if sensitive:
        print(json.dumps({'type': 'assistant', 'message': {'content': [{
            'type': 'tool_use', 'id': 'fake-sensitive', 'name': 'Read',
            'input': {'file_path': sensitive}}]}, 'session_id': session}))
        print(json.dumps({'type': 'user', 'message': {'content': [{
            'type': 'tool_result', 'tool_use_id': 'fake-sensitive', 'content': 'secret',
            'is_error': False}]}, 'session_id': session}))
    print(json.dumps({'type': 'result', 'session_id': session, 'is_error': False,
          'structured_output': answer, 'usage': {'input_tokens': 10,
          'cache_creation_input_tokens': 5, 'cache_read_input_tokens': 20, 'output_tokens': 12}}))


def mutate(mode):
    root = Path.cwd()
    if mode == 'echo':
        (root / 'forbidden.txt').write_text('x\n')
    elif mode == 'checkout':
        (root / 'tracked.txt').write_text('checkout mutation\n')
    elif mode == 'commit':   # D-EFF: HEAD moves, the files stay the same
        subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'reviewer commit'], check=True)
    elif mode == 'rm':
        target = root / 'tracked.txt'
        if target.exists():
            target.unlink()


def main():
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    append_commands = os.environ.get('FAKE_APPEND_REVIEWER_COMMAND')
    if append_commands and 'implementer. Phase: EXEC.' in prompt:
        with Path(os.environ['FAKE_APPEND_REVIEWER_COMMAND_FILE']).open('a') as target:
            target.write('\n```reviewer-commands\n' + append_commands + '\n```\n')
    extra_observed_commands = []
    vendor = 'codex' if args and args[0] == 'exec' else 'claude'
    rejected = next((arg for arg in args if arg.startswith('-P') or arg.split('=', 1)[0] == '--permission-profile'), None)
    if vendor == 'codex' and rejected:   # b296-f1e: like codex-cli 0.160.0, `codex exec` has no -P/--permission-profile
        flag = '-P' if rejected.startswith('-P') else '--permission-profile'
        print(f"error: unexpected argument '{flag}' found", file=sys.stderr)
        return 2
    if vendor == 'claude':
        allowed = [args[index + 1] for index, value in enumerate(args[:-1])
                   if value == '--allowedTools']
        if any(len(re.findall(r'Bash\([^)]*\)', value)) > 1 for value in allowed):
            print('fake CLI contract: argument-bearing Bash rules require separate --allowedTools arguments',
                  file=sys.stderr)
            return 2
    if os.environ.get('FAKE_TMP_LOG'):   # b295-f1: what a tmp_path-style test sees as its temp dir in this dispatch
        import tempfile
        made = tempfile.mkdtemp()
        with open(os.environ['FAKE_TMP_LOG'], 'a') as log:
            log.write(json.dumps({'vendor': vendor, 'prompt': prompt[:40], 'TMPDIR': os.environ.get('TMPDIR'), 'TMP': os.environ.get('TMP'),
                                  'TEMP': os.environ.get('TEMP'), 'mkdtemp': made}) + '\n')
    if os.environ.get('FAKE_CODEX_SCRATCH_HARDLINK') and vendor == 'codex' and os.environ.get('TMPDIR'):
        os.link(os.environ['FAKE_CODEX_SCRATCH_HARDLINK'], Path(os.environ['TMPDIR']) / 'linked')
    if os.environ.get('FAKE_CODEX_LINK_WRITE_UNLINK') and vendor == 'codex' and os.environ.get('TMPDIR'):   # b296-f1: the same-volume residual
        through = Path(os.environ['TMPDIR']) / 'through'
        os.link(os.environ['FAKE_CODEX_LINK_WRITE_UNLINK'], through)
        through.write_bytes(through.read_bytes())   # the same bytes: only the inode's ctime records the write
        through.unlink()
    if os.environ.get('FAKE_RATE_LIMIT'):
        print('HTTP 429 rate limit. Try again at Sep 26th 5:13 PM', file=sys.stderr)
        return 1
    session = 'fake-codex-thread'
    if vendor == 'codex' and os.environ.get('FAKE_UNIQUE_CODEX_THREAD') and 'resume' not in args:
        session = str(uuid.uuid4())
    if vendor == 'codex' and 'resume' in args:
        session = args[args.index('resume') + 1]
    elif vendor == 'claude' and '--session-id' in args:
        session = args[args.index('--session-id') + 1]
    elif vendor == 'claude' and '--resume' in args:
        session = args[args.index('--resume') + 1]
    elif vendor == 'claude':
        session = str(uuid.uuid4())

    if 'Role: persistent' in prompt and 'Phase: POLISH' in prompt:
        if 'for every delivered id answer' in prompt:
            ids = sorted(set(__import__('re').findall(r'\bF\d{3,}\b', prompt)))
            disposition = 'declined' if os.environ.get('FAKE_POLISH_DECLINE') else 'fixed'
            answer = {'status': 'READY', 'body': 'Polish choices completed.',
                      'findings': [{'id': finding_id, 'disposition': disposition,
                                    'reason': 'deferred by fake' if disposition == 'declined'
                                              else 'cheap fake fix applied'} for finding_id in ids]}
        else:
            answer = {'status': 'READY', 'body': 'Fixed the polish regression.'}
    elif 'Role: persistent' in prompt:
        if 'Phase: PLAN' in prompt:
            if os.environ.get('FAKE_PLAN_MUTATE'):
                (Path.cwd() / 'plan-leak.txt').write_text('forbidden during plan\n')
            body = ('Plan\n\n1. Add sum_ints.\n2. Handle invalid input.' if 'No delivered review' in prompt
                    else 'Plan\n\n1. Add sum_ints.\n2. Reject bool and nested input.\n3. Verification: run unittest.')
        elif 'Phase: DOCS' in prompt:
            target = Path.cwd() / os.environ['FAKE_LIFECYCLE_DOCS_FILE']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('# Fake lifecycle guide\n')
            body = 'Updated the configured documentation file.'
        else:
            module = Path.cwd() / 'sum_ints.py'
            if not os.environ.get('FAKE_AUTHOR_NO_REJECTION_CHANGE'):
                if os.environ.get('FAKE_DOC_DELTA'):
                    (Path.cwd() / 'CLAUDE.md').write_text('Directory: sum_ints.py\n')
                if 'intentionally omit bool rejection' in prompt or 'No delivered review' in prompt:
                    module.write_text('def sum_ints(values):\n    if not all(isinstance(x, int) for x in values):\n        raise TypeError("ints only")\n    return sum(values)\n')
                else:
                    module.write_text('def sum_ints(values):\n    if not all(type(x) is int for x in values):\n        raise TypeError("ints only")\n    return sum(values)\n')
            if ('## Operator rejection for current EXEC scope' in prompt and
                    not os.environ.get('FAKE_AUTHOR_NO_REJECTION_CHANGE')):
                module.write_text(module.read_text() + '# rejection applied\n')
            if os.environ.get('FAKE_AUTHOR_WRITE_NEW_TREE'):
                module.write_text(module.read_text() + '# resumed author output\n')
            if 'docs entry still wrong' in prompt:   # worktree-lifecycle: the EXEC author fixes a DOCS-owned entry
                changelog = Path.cwd() / 'CHANGELOG.md'
                changelog.write_text(changelog.read_text() + '# corrected entry\n')
            if 'specialist blocker' in prompt:   # worktree-lifecycle POLISH-Q fix leg: one new tree per fixed id set
                ids = ' '.join(re.findall(r'"id": "F(\d+)"', prompt))   # no finding ids in code: shadow independence
                module.write_text(module.read_text() + f'# specialist fix {ids}\n')
            body = 'Implemented sum_ints and ran fake checks.'
        answer = {'status': 'READY', 'body': os.environ.get('FAKE_AUTHOR_RATIONALE', body)}
        if os.environ.get('FAKE_AUTHOR_HOLD_AFTER_WRITE'):
            answer = {'status': 'HOLD', 'body': 'Fake author held after writing.'}
        if os.environ.get('FAKE_AUTHOR_COMMIT') and 'Phase: EXEC' in prompt:   # D-EFF git guard: an author that commits its change
            subprocess.run(['git', 'add', '-A'], check=True)
            subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'fake author commit'], check=True)
        if os.environ.get('FAKE_GLOBAL_CONFIG_WRITE') and 'Phase: EXEC' in prompt:   # D-EFF: a turn that changes the global Codex config
            with (Path(os.environ['CODEX_HOME']) / 'config.toml').open('a') as handle:
                handle.write('\nmodel_verbosity = "high"\n')
        if os.environ.get('FAKE_AUTHOR_FAIL_AFTER_WRITE') and 'Phase: EXEC' in prompt:   # v2.9.7 OPV: a CLI that fails after changing the tree
            print('fake author failed after writing', file=sys.stderr)
            return 1
    elif 'Role: finisher' in prompt or 'Role: docs writer' in prompt:   # worktree-lifecycle fresh writers (ADR-11)
        module = Path.cwd() / 'sum_ints.py'
        if ('Phase: FINISH' in prompt and os.environ.get('FAKE_FINISH_WRITE') and
                '# finisher fix' not in module.read_text()):   # one fix; the next FINISH finds nothing to change
            module.write_text(module.read_text() + '# finisher fix\n')
        if 'Phase: FINISH' in prompt and os.environ.get('FAKE_FINISH_COMMIT'):   # a writer committing on its own
            for command in (['git', 'add', '-A'], ['git', '-c', 'user.name=Fake', '-c', 'user.email=fake@example.test',
                                                   'commit', '-qm', 'finisher commit', '--allow-empty']):
                __import__('subprocess').run(command, check=True)
        if 'Phase: DOCS' in prompt and os.environ.get('FAKE_LIFECYCLE_DOCS_FILE'):
            target = Path.cwd() / os.environ['FAKE_LIFECYCLE_DOCS_FILE']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('# Fake lifecycle guide\n')
        if 'Phase: DOCS' in prompt and (marker := os.environ.get('FAKE_DOCS_CODE_ONCE')) and not Path(marker).exists():
            Path(marker).write_text('written\n')   # one comment fix outside the docs allowlist: EXEC replays
            module.write_text(module.read_text() + '# docs comment fix\n')
        answer = ({'status': 'HOLD', 'body': 'Fake finisher held.'} if os.environ.get('FAKE_FINISH_HOLD') else
                  {'status': 'READY', 'body': 'Checked readiness; ' + ('fixed a defect.' if
                   os.environ.get('FAKE_FINISH_WRITE') else 'nothing needed changing.')})
    elif prompt.startswith('You are an adversarial reviewer'):
        configured_test = prompt.split(
            'Run this test command exactly as written in one Bash call: ', 1)[1].splitlines()[0]
        blocking = os.environ.get('FAKE_GATE_BLOCK')
        block_once = os.environ.get('FAKE_GATE_BLOCK_ONCE')
        if block_once:
            marker = Path(block_once)
            if marker.exists():
                blocking = None
            else:
                marker.write_text('blocked\n')
                blocking = '1'
        malformed = os.environ.get('FAKE_GATE_MALFORMED')
        findings = []
        if blocking or malformed:
            body = ('Trigger: bool input. Reachability: public API. Impact: wrong sum. Likelihood: common. '
                    'Fix cost: one type check. Cheaper response: tests alone do not fix behavior.' if blocking
                    else 'Trigger: bool input only.')
            if os.environ.get('FAKE_FINDING_CLASS'):
                body = '[class: ' + os.environ['FAKE_FINDING_CLASS'] + '] ' + body
            findings = [{'severity': 'high', 'file': 'sum_ints.py', 'line_start': 1, 'line_end': 3,
                         'confidence': 0.9, 'recommendation': 'reject bool', 'body': body}]
        elif os.environ.get('FAKE_GATE_MINOR') or os.environ.get('FAKE_GATE_LOW'):
            findings = [{'severity': 'low' if os.environ.get('FAKE_GATE_LOW') else 'medium',
                         'file': 'sum_ints.py', 'line_start': 1,
                         'line_end': 3, 'confidence': 0.8,
                         'recommendation': 'simplify the helper',
                         'body': 'Non-blocking cleanup suggested by the fake gate.'}]
        answer = {'verdict': 'needs-attention' if findings else 'approve',
                  'findings': findings,
                  'self_run_evidence': [{'command': configured_test}]}
        command_events = [{'command': configured_test, 'exit_code': 0, 'output': 'OK'}] if vendor == 'codex' else None   # G-b: a Codex gate observes the configured test like the Claude fake
    elif 'Role: permission-system probe' in prompt:
        if os.environ.get('FAKE_PROBE_MUTATE') and os.environ.get('FAKE_PROBE_MUTATE_VENDOR', vendor) == vendor:   # G-a: the vendor filter lets a test mutate in one probe turn only
            (Path.cwd() / 'probe-mutation.txt').write_text('mutation\n')
        allowed = prompt.split('Allowed exact command:\n', 1)[1].splitlines()[0]
        attacks = prompt.split('Write commands expected to be denied:\n', 1)[1].split(
            '\nReturn APPROVE', 1)[0].splitlines()
        answer = {'status': 'APPROVE', 'prior_findings': [], 'full_review': [],
                  'self_run_evidence': [{'command': command} for command in [allowed, *attacks]]}
        if vendor == 'codex':   # G-a: a Codex probe turn observes the allowed command (exit 0) and denies every write attempt
            command_events = [{'command': command, 'exit_code': 0 if command == allowed else 1,   # b296-f1b: an explicit OS denial
                               'output': 'fake permission result' if command == allowed else 'fake: Operation not permitted'}
                              for command in [allowed, *attacks]]
            for row in command_events:   # b296-f1f/g: as in the field (codex-cli 0.160.0, P1/P2): rm of a file missing on disk is "No such
                if row['command'].startswith('rm '):   # file", not a denial; checkout takes index.lock before it checks the pathspec, so a working
                    name = shlex.split(row['command'])[-1]   # sandbox denies it whether or not the name is tracked or on disk
                    if not os.path.lexists(name): row.update(output=f'rm: {name}: No such file or directory\n')
                elif row['command'].startswith('git --literal-pathspecs checkout -- '):
                    row.update(exit_code=128, output=f"fatal: Unable to create '{Path.cwd() / '.git' / 'index.lock'}': Operation not permitted\n")
            for marker, code, text in (('FAKE_CODEX_PROBE_NOT_FOUND', 127, 'zsh: command not found: ln'), ('FAKE_CODEX_PROBE_OTHER_ERROR', 1, 'ln: invalid option')):
                if os.environ.get(marker):   # b296-f1b: an error that is not a sandbox denial, for the command containing that substring
                    command_events = [{**row, 'exit_code': code, 'output': text} if os.environ[marker] in row['command'] else row for row in command_events]
            fs = readonly_profile(args)   # b295-f1: the read-only profile in this argv decides the scratch and workspace writes
            if 'Scratch write expected to succeed:\n' in prompt:
                scratch = prompt.split('Scratch write expected to succeed:\n', 1)[1].splitlines()[0]
                writable = fs.get(':tmpdir') == 'write' and os.environ.get('TMPDIR')
                if writable and not os.environ.get('FAKE_CODEX_SKIP_SCRATCH'):
                    (Path(os.environ['TMPDIR']) / 'paired-session-scratch-probe').write_text('probe')
                command_events.append({'command': scratch, 'exit_code': 0 if writable else 1,
                                       'output': '' if writable else 'fake: Operation not permitted'})
            for command in attacks:   # b296-f1 R1: a profile that lets the escape writes or the hard link land (selected by substring)
                if not os.environ.get('FAKE_CODEX_PROBE_ESCAPE') or os.environ['FAKE_CODEX_PROBE_ESCAPE'] not in command:
                    continue
                words = shlex.split(command.replace('$TMPDIR', os.environ.get('TMPDIR', '')))
                if words[0] == 'ln': os.link(words[1], words[2])
                elif words[0] == 'rm': Path(words[1]).unlink()   # b296-f1f: the tracked-file delete lands
                elif words[0] == 'git': subprocess.run(words, check=True, capture_output=True)   # b296-f1g: the real checkout runs
                elif command.startswith('printf probe > '): Path(words[-1]).write_text('probe')
                command_events = [{**row, 'exit_code': 0, 'output': ''} if row['command'] == command else row for row in command_events]
            if fs.get(':workspace_roots', {}).get('.') == 'write':   # a profile that grants the workspace: the attacks land
                (Path.cwd() / 'forbidden-probe').write_text('x\n')
                command_events = [{**row, 'exit_code': 0, 'output': ''} if row['command'] in attacks else row for row in command_events]
    else:
        role = ('gate' if prompt.startswith('You are an adversarial reviewer') else
                'shadow' if 'Role: shadow,' in prompt else 'reviewer')
        revise = 'Exercise rule: return REVISE' in prompt and 'Role: reviewer,' in prompt
        phase = ('SECURITY' if 'Q-SECURITY:' in prompt else
                 'Q' if 'Q-FINAL:' in prompt or 'Phase: Q.' in prompt else
                 'SECURITY' if 'Phase: SECURITY' in prompt else
                 'POLISH' if 'Phase: POLISH' in prompt else
                 'DOCS' if 'Role: docs reviewer, fresh. Phase: DOCS.' in prompt else
                 'EXEC' if 'Phase: EXEC' in prompt else 'PLAN')
        configured_test = ('Run this test command exactly as written in one Bash call: '
                           in prompt and prompt.split(
                               'Run this test command exactly as written in one Bash call: ', 1)[1].splitlines()[0])
        open_ids = []
        if 'Open finding ledger (' in prompt:
            block = prompt.split('Open finding ledger (', 1)[1].split('\n', 1)[1]
            for line in block.splitlines():
                if line.startswith('- F'):
                    open_ids.append(line[2:].split(':', 1)[0])
                elif open_ids:
                    break
        if os.environ.get('FAKE_OMIT_DISPOSITION') and 'one allowed protocol retry' not in prompt:
            open_ids = open_ids[:-1]
        if os.environ.get('FAKE_ALWAYS_OMIT_DISPOSITION'):
            open_ids = open_ids[:-1]
        findings = ([{'severity': 'CRITICAL',
                      'file': 'plan.md' if phase == 'PLAN' else 'sum_ints.py',
                      'summary': 'required exercise revision',
                      'failure_scenario': ('verification is absent from the plan' if phase == 'PLAN'
                                           else 'bool is accepted as int')}]
                    if revise else [])
        if role == 'reviewer' and phase == 'EXEC' and os.environ.get('FAKE_EXEC_MINOR_REVISE'):
            revise = True
            findings = [{'severity': 'MINOR', 'file': 'sum_ints.py',
                         'summary': 'advisory exec polish', 'failure_scenario': 'style remains untidy'}]
        if role == 'reviewer' and phase == 'EXEC' and os.environ.get('FAKE_EXEC_MIXED_REVISE'):
            revise = True
            findings = [
                {'severity': 'MINOR', 'file': 'sum_ints.py', 'summary': 'small cleanup',
                 'failure_scenario': 'style remains untidy'},
                {'severity': 'MAJOR', 'file': 'sum_ints.py', 'failure_scenario': 'wrong result is returned',
                 'summary': ('[class: ' + os.environ['FAKE_FINDING_CLASS'] + '] ' if os.environ.get('FAKE_FINDING_CLASS') else '')
                 + 'major behavior issue'},
            ]
        if role == 'reviewer' and phase == 'EXEC' and os.environ.get('FAKE_EXEC_LOW_REVISE'):
            revise = True
            findings = [{'severity': 'LOW', 'file': 'sum_ints.py', 'summary': 'low-risk note',
                         'failure_scenario': 'minor ergonomics issue'}]
        if role == 'reviewer' and phase == 'EXEC' and os.environ.get('FAKE_EXEC_SECURITY_REVISE'):
            revise = True
            findings = [{'severity': 'MINOR', 'security': True, 'file': 'sum_ints.py',
                         'summary': 'security-specific note', 'failure_scenario': 'unsafe input path'}]
        if role == 'reviewer' and phase == 'POLISH' and os.environ.get('FAKE_POLISH_MINOR_REVISE'):
            revise = True
            findings = [{'severity': 'MINOR', 'file': 'sum_ints.py',
                         'summary': 'advisory polish item', 'failure_scenario': 'style remains untidy'}]
        if os.environ.get('FAKE_PLAN_CODE_FINDING') and phase == 'PLAN':
            revise = True
            findings = [{'severity': 'CRITICAL', 'file': 'workspace',
                         'summary': 'implementation files are missing',
                         'failure_scenario': 'no Python source exists during plan review'}]
        if role == 'reviewer' and phase == 'PLAN' and os.environ.get('FAKE_PLAN_MINOR_REVISE'):
            revise = True
            findings = [{'severity': 'MINOR', 'file': 'plan.md', 'summary': 'plan advisory',
                         'failure_scenario': 'plan omits a small design detail'}]
        if (role == 'reviewer' and phase == 'PLAN' and os.environ.get('FAKE_PLAN_APPROVE_SECURITY')   # APPROVE that opens a MINOR security finding: first review only, or every review with 'always'
                and (not open_ids or os.environ['FAKE_PLAN_APPROVE_SECURITY'] == 'always')):
            findings = [{'severity': 'MINOR', 'security': True, 'file': 'plan.md', 'summary': 'plan security note ' + str(len(open_ids)),
                         'failure_scenario': 'plan leaves an unsafe path'}]
        if role == 'shadow' and os.environ.get('FAKE_SHADOW_CRITICAL'):
            findings = [{'severity': 'CRITICAL', 'file': 'sum_ints.py',
                         'summary': 'fresh shadow blocker', 'failure_scenario': 'wrong result remains'}]
        if ('include one non-blocking MINOR' in prompt or
                (os.environ.get('FAKE_APPROVE_MINOR') and phase == 'PLAN') or
                (os.environ.get('FAKE_Q_MINOR') and phase in ('Q', 'SECURITY'))):
            findings = [{'severity': 'MINOR', 'file': 'sum_ints.py',
                         'summary': 'cheap advisory cleanup',
                         'failure_scenario': 'style remains untidy'}]
        if (os.environ.get('FAKE_POLISH_CRITICAL') and 'Phase: POLISH' in prompt and
                'polish regression' not in prompt):
            revise = True
            findings = [{'severity': 'CRITICAL', 'file': 'sum_ints.py',
                         'summary': 'polish regression',
                         'failure_scenario': 'polish broke bool rejection'}]
        specialist = prompt.split('Role: specialist ', 1)[1].split(',', 1)[0] if 'Role: specialist ' in prompt else None
        block_once = os.environ.get('FAKE_SPECIALIST_BLOCK_ONCE')
        if (specialist and specialist == os.environ.get('FAKE_SPECIALIST_BLOCK') and   # worktree-lifecycle POLISH-Q
                not (block_once and Path(block_once).exists())):
            if block_once:
                Path(block_once).write_text('blocked once\n')
            revise = True
            findings = [{'severity': os.environ.get('FAKE_SPECIALIST_SEVERITY', 'CRITICAL'), 'file': 'sum_ints.py',
                         'summary': 'specialist blocker', 'failure_scenario': 'a specialist found a blocking defect'}]
        docs_block = os.environ.get('FAKE_DOCS_REVIEW_BLOCK_ONCE')   # worktree-lifecycle DOCS review
        if 'Role: docs reviewer,' in prompt and docs_block and not Path(docs_block).exists():
            Path(docs_block).write_text('blocked once\n')
            revise = True
            findings = [{'severity': 'MAJOR', 'file': 'CHANGELOG.md', 'summary': 'docs describe the wrong behavior',
                         'failure_scenario': 'a reader trusts the stale changelog entry'}]
        disposition = ('still_open' if os.environ.get('FAKE_POLISH_DECLINE') and
                       'Phase: POLISH' in prompt else 'fixed')
        prior = [{'id': finding_id, 'disposition': disposition,
                  'evidence': 'fake verified disposition'} for finding_id in open_ids]
        docs_open = os.environ.get('FAKE_DOCS_FINDING_STILL_OPEN_ONCE')   # the EXEC reviewer keeps a docs finding
        if (docs_open and role == 'reviewer' and phase == 'EXEC' and 'docs describe the wrong behavior' in prompt and
                not Path(docs_open).exists()):
            Path(docs_open).write_text('still open\n')
            revise = True
            prior = [{'id': finding_id, 'disposition': 'still_open', 'evidence': 'docs entry still wrong'}
                     for finding_id in open_ids]
        if 'Role: shadow,' in prompt or 'Role: shadow,' in prompt.replace('fresh isolated ', ''):
            prior = None
        answer = {'status': 'REVISE' if revise else 'APPROVE',
                  'full_review': findings,
                  'self_run_evidence': ([{'command': configured_test or 'python3 -m unittest'}]
                                        if phase in ('EXEC', 'SECURITY', 'Q', 'DOCS') else [])}
        no_test = os.environ.get('FAKE_DOCS_REVIEW_NO_TEST_ONCE')
        if 'Role: docs reviewer,' in prompt and no_test and not Path(no_test).exists():
            Path(no_test).write_text('no test\n')
            answer['self_run_evidence'] = []   # a docs review without the retest
        if 'Phase: POLISH' in prompt:
            answer['self_run_evidence'] = [{'command': configured_test or 'python3 -m unittest'}]
            if os.environ.get('FAKE_POLISH_NO_EVIDENCE'):
                answer['self_run_evidence'] = []
                extra_observed_commands = [{'command': configured_test or 'python3 -m unittest'}]
            once = os.environ.get('FAKE_SPECIALIST_NO_TOOLS_ONCE')
            if specialist and (specialist in os.environ.get('FAKE_SPECIALIST_NO_TOOLS', '').split(',') or
                               (once and not Path(once).exists() and Path(once).write_text('no tools\n') > 0)):
                answer['self_run_evidence'] = []   # a turn that made no tool calls
            if specialist and specialist == os.environ.get('FAKE_SPECIALIST_HOLD'):
                answer['status'] = 'HOLD'
        if phase in ('Q', 'SECURITY'):
            for finding in findings:
                finding['severity'] = os.environ.get('FAKE_Q_SEVERITY', finding['severity'])
                if 'FAKE_Q_SECURITY_FLAG' in os.environ:
                    finding['security'] = bool(os.environ['FAKE_Q_SECURITY_FLAG'])
        if os.environ.get('FAKE_Q_REVISE') and phase in ('Q', 'SECURITY'):
            answer['status'] = 'REVISE'
            if os.environ.get('FAKE_Q_EMPTY_REVISE'):
                answer['full_review'] = []
        for finding in findings:
            finding.setdefault('security', False)
        if prior is not None:
            answer['prior_findings'] = prior
        mode = os.environ.get('FAKE_MUTATION')
        once = os.environ.get('FAKE_MUTATION_ONCE')   # D-EFF: a marker file; only the first reviewer turn mutates
        if mode and os.environ.get('FAKE_MUTATION_ROLE', 'Role: reviewer,') in prompt and not (once and Path(once).exists()):
            if once: Path(once).write_text('mutated\n')
            mutate(mode)
        command_events = ([{'command': configured_test, 'exit_code': 1, 'output': 'FAILED fake test'}]
                          if vendor == 'codex' and role == 'reviewer' and phase == 'EXEC'
                          and configured_test and os.environ.get('FAKE_REVIEW_TEST_FAILURE') else None)
    if prompt.startswith('Role: author workspace-write permission probe'):
        encoded = prompt.split('PROBE_COMMANDS_JSON: ', 1)[1].splitlines()[0]
        commands = json.loads(encoded)
        events = []
        output_commands = []
        for index, command in enumerate(commands):
            exit_code = 0 if index < 2 else 126
            label = next((name for name in ('external_tmpdir', 'slash_tmp', 'private_tmp', 'home', 'workspace_parent')
                          if 'paired-session-escape-' + name + '-' in command), None)
            if label and os.environ.get('FAKE_AUTHOR_ESCAPE_SKIP') in (label, 'all'):
                continue
            if label and os.environ.get('FAKE_AUTHOR_ESCAPE_MALFORMED') == label:
                exit_code = None
            if label and os.environ.get('FAKE_AUTHOR_ESCAPE_WRITE') == label:
                Path(shlex.split(command)[-1]).write_text('fake escaped\n')
                exit_code = 0
            if label and os.environ.get('FAKE_AUTHOR_ESCAPE_WRITE_THEN_DELETE') == label:
                target = Path(shlex.split(command)[-1]); target.write_text('fake escaped\n'); target.unlink()
                exit_code = 0
            if label and os.environ.get('FAKE_AUTHOR_ESCAPE_WRITE_DELETE_DENIED') == label:
                target = Path(shlex.split(command)[-1]); target.write_text('fake escaped\n'); target.unlink()
            if label and os.environ.get('FAKE_AUTHOR_ESCAPE_FOREIGN') == label:
                (Path(shlex.split(command)[-1]).parent / 'foreign-file').write_text('not a coordinator sentinel\n')
            if index < 2 and os.environ.get('FAKE_AUTHOR_POSITIVE_FAIL') == ('workspace' if index == 0 else 'tmpdir'):
                exit_code = 1
            if index < 2 and exit_code == 0:
                parts = shlex.split(command)
                target_arg = parts[parts.index('>') + 1]
                target = (Path(os.environ['TMPDIR']) / target_arg[len('$TMPDIR/'):]
                          if target_arg.startswith('$TMPDIR/') else Path(target_arg))
                target.write_text('probe\n')
            events.append({'command': command, 'exit_code': exit_code,
                           'output': ('generic failure' if label and label == os.environ.get('FAKE_AUTHOR_ESCAPE_NO_OS') else
                                      'zsh: operation not permitted: ' + shlex.split(command)[-1]
                                      if label and exit_code == 126 else 'fake permission result')})
            if label and label == os.environ.get('FAKE_AUTHOR_ESCAPE_DUPLICATE'):
                events.append(dict(events[-1]))
            output_commands.append({'command': command})
        answer = {'status': 'APPROVE', 'prior_findings': [], 'full_review': [],
                  'self_run_evidence': output_commands}
        emit_codex(answer, session, events)
        return 0

    if (os.environ.get('FAKE_REVIEW_HOLD') and 'Role: reviewer,' in prompt and
            'Phase: PLAN' in prompt):
        answer = {'status': 'HOLD', 'prior_findings': [], 'full_review': [],
                  'self_run_evidence': []}
    if 'Role: persistent' not in prompt and 'Role: permission-system probe' not in prompt:
        role = ('gate' if prompt.startswith('You are an adversarial reviewer') else
                'shadow' if 'Role: shadow,' in prompt else 'reviewer')
        answer['verified_claims'] = [{'claim': 'Checked existing source contract', 'file': 'tracked.txt', 'line': 1}]
        if (os.environ.get('FAKE_EMPTY_CLAIMS') == role and
                ('Evidence contract retry:' not in prompt or os.environ.get('FAKE_ALWAYS_EMPTY_CLAIMS'))):
            answer['verified_claims'] = []
        if os.environ.get('FAKE_MALFORMED_CLAIMS') == role:
            answer['verified_claims'] = [{'claim': ' ', 'file': 'tracked.txt', 'line': True}]
        if 'Phase: PLAN' in prompt and os.environ.get('FAKE_PLAN_INSPECT'):
            text = (Path.cwd() / 'tracked.txt').read_text()
            if vendor == 'codex':
                print(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution',
                    'command': "sed -n '1,20p' tracked.txt", 'exit_code': 0, 'aggregated_output': text}}))
        if 'Phase: PLAN' in prompt and os.environ.get('FAKE_PLAN_REVIEWER_MUTATE'):
            (Path.cwd() / 'tracked.txt').write_text('forbidden plan review write\n')
            if os.environ.get('FAKE_PLAN_REVIEWER_FAIL'):   # D-EFF eff-c: a write, then a CLI that exits non-zero
                return 1
        if 'Phase: PLAN' in prompt and os.environ.get('FAKE_PLAN_REVIEWER_COMMIT'):   # D-EFF eff-c: HEAD moves, files stay the same
            subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'plan review commit'], check=True)   # the module import: a local one would shadow it in all of main()

    snapshot_mode = os.environ.get('FAKE_SNAPSHOT_MODE')
    if snapshot_mode == 'wrong':
        answer['reviewed_snapshot'] = '0' * 40
    elif snapshot_mode != 'missing' and 'reviewed_snapshot' in answer:
        answer['reviewed_snapshot'] = answer['reviewed_snapshot']
    if locals().get('phase') == 'Q' and os.environ.get('FAKE_Q_FAILED_THEN_PASSED'):
        command_events = [{'command': configured_test, 'exit_code': 1, 'output': 'FAILED first test'},
                          {'command': configured_test, 'exit_code': 0, 'output': 'passed retry'}]
        extra_observed_commands = [{'command': configured_test, 'force_failure': True},
                                   *(extra_observed_commands or [])]
    if vendor == 'codex':
        emit_codex(answer, session, locals().get('command_events'))
    else:
        emit_claude(answer, session, extra_observed_commands)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
