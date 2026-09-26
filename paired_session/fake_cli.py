#!/usr/bin/env python3
"""Offline protocol fake for S4 tests; never used by the real run."""
import json
import os
from pathlib import Path
import re
import shlex
import sys
from datetime import datetime, timezone
import uuid


def emit_codex(answer, session, command_events=None):
    model = os.environ.get('FAKE_CODEX_MODEL', 'gpt-6-luna')
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
        for index, evidence in enumerate(evidence_rows):
            tool_id = 'fake-tool-' + str(index)
            command = evidence['command']
            if os.environ.get('FAKE_REVIEW_NO_TEST_EVENT') and command == 'python3 -m unittest':
                continue
            forbidden = command.startswith(('echo ', 'git checkout', 'rm ', 'git diff --output=',
                                             'git log --output=', 'git show --output=')) or ' > ' in command
            test_failure = bool(os.environ.get('FAKE_REVIEW_TEST_FAILURE') and
                                command == 'python3 -m unittest')
            if (os.environ.get('FAKE_SANDBOX_WRITE') and
                    ('paired-session-claude-sandbox-' in command or
                     '.paired-session-run-dir-probe-' in command or
                     '.paired-session-context-probe-' in command) and ' > ' in command):
                import shlex
                parts = shlex.split(command)
                target = Path(parts[parts.index('>') + 1])
                target.write_text('probe escaped sandbox\n')
                forbidden = False
            sandbox_denial = (('paired-session-claude-sandbox-' in command or
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
                             'zsh: operation not permitted' if sandbox_denial else 'fake result')),
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
    elif mode == 'rm':
        target = root / 'tracked.txt'
        if target.exists():
            target.unlink()


def main():
    args = sys.argv[1:]
    prompt = sys.stdin.read()
    extra_observed_commands = []
    vendor = 'codex' if args and args[0] == 'exec' else 'claude'
    if vendor == 'claude':
        allowed = [args[index + 1] for index, value in enumerate(args[:-1])
                   if value == '--allowedTools']
        if any(len(re.findall(r'Bash\([^)]*\)', value)) > 1 for value in allowed):
            print('fake CLI contract: argument-bearing Bash rules require separate --allowedTools arguments',
                  file=sys.stderr)
            return 2
    if os.environ.get('FAKE_RATE_LIMIT'):
        print('HTTP 429 rate limit. Try again at Sep 26th 5:13 PM', file=sys.stderr)
        return 1
    session = 'fake-codex-thread'
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
        else:
            module = Path.cwd() / 'sum_ints.py'
            if os.environ.get('FAKE_DOC_DELTA'):
                (Path.cwd() / 'CLAUDE.md').write_text('Directory: sum_ints.py\n')
            if 'intentionally omit bool rejection' in prompt or 'No delivered review' in prompt:
                module.write_text('def sum_ints(values):\n    if not all(isinstance(x, int) for x in values):\n        raise TypeError("ints only")\n    return sum(values)\n')
            else:
                module.write_text('def sum_ints(values):\n    if not all(type(x) is int for x in values):\n        raise TypeError("ints only")\n    return sum(values)\n')
            body = 'Implemented sum_ints and ran fake checks.'
        answer = {'status': 'READY', 'body': body}
    elif prompt.startswith('You are an adversarial reviewer'):
        configured_test = prompt.split(
            'Run this test command exactly as written in one Bash call: ', 1)[1].splitlines()[0]
        blocking = os.environ.get('FAKE_GATE_BLOCK')
        malformed = os.environ.get('FAKE_GATE_MALFORMED')
        findings = []
        if blocking or malformed:
            body = ('Trigger: bool input. Reachability: public API. Impact: wrong sum. Likelihood: common. '
                    'Fix cost: one type check. Cheaper response: tests alone do not fix behavior.' if blocking
                    else 'Trigger: bool input only.')
            findings = [{'severity': 'high', 'file': 'sum_ints.py', 'line_start': 1, 'line_end': 3,
                         'confidence': 0.9, 'recommendation': 'reject bool', 'body': body}]
        elif os.environ.get('FAKE_GATE_MINOR'):
            findings = [{'severity': 'medium', 'file': 'sum_ints.py', 'line_start': 1,
                         'line_end': 3, 'confidence': 0.8,
                         'recommendation': 'simplify the helper',
                         'body': 'Non-blocking cleanup suggested by the fake gate.'}]
        answer = {'verdict': 'needs-attention' if findings else 'approve',
                  'findings': findings,
                  'self_run_evidence': [{'command': configured_test}]}
    elif 'Role: permission-system probe' in prompt:
        if os.environ.get('FAKE_PROBE_MUTATE'):
            (Path.cwd() / 'probe-mutation.txt').write_text('mutation\n')
        allowed = prompt.split('Allowed exact command:\n', 1)[1].splitlines()[0]
        attacks = prompt.split('Write commands expected to be denied:\n', 1)[1].split(
            '\nReturn APPROVE', 1)[0].splitlines()
        answer = {'status': 'APPROVE', 'prior_findings': [], 'full_review': [],
                  'self_run_evidence': [{'command': command} for command in [allowed, *attacks]]}
    else:
        role = ('gate' if prompt.startswith('You are an adversarial reviewer') else
                'shadow' if 'Role: shadow,' in prompt else 'reviewer')
        revise = 'Exercise rule: return REVISE' in prompt and 'Role: reviewer,' in prompt
        phase = ('POLISH' if 'Phase: POLISH' in prompt else
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
                {'severity': 'MAJOR', 'file': 'sum_ints.py', 'summary': 'major behavior issue',
                 'failure_scenario': 'wrong result is returned'},
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
        if role == 'shadow' and os.environ.get('FAKE_SHADOW_CRITICAL'):
            findings = [{'severity': 'CRITICAL', 'file': 'sum_ints.py',
                         'summary': 'fresh shadow blocker', 'failure_scenario': 'wrong result remains'}]
        if ('include one non-blocking MINOR' in prompt or
                (os.environ.get('FAKE_APPROVE_MINOR') and phase == 'PLAN')):
            findings = [{'severity': 'MINOR', 'file': 'sum_ints.py',
                         'summary': 'cheap advisory cleanup',
                         'failure_scenario': 'style remains untidy'}]
        if (os.environ.get('FAKE_POLISH_CRITICAL') and 'Phase: POLISH' in prompt and
                'polish regression' not in prompt):
            revise = True
            findings = [{'severity': 'CRITICAL', 'file': 'sum_ints.py',
                         'summary': 'polish regression',
                         'failure_scenario': 'polish broke bool rejection'}]
        disposition = ('still_open' if os.environ.get('FAKE_POLISH_DECLINE') and
                       'Phase: POLISH' in prompt else 'fixed')
        prior = [{'id': finding_id, 'disposition': disposition,
                  'evidence': 'fake verified disposition'} for finding_id in open_ids]
        if 'Role: shadow,' in prompt or 'Role: shadow,' in prompt.replace('fresh isolated ', ''):
            prior = None
        answer = {'status': 'REVISE' if revise else 'APPROVE',
                  'full_review': findings,
                  'self_run_evidence': ([{'command': configured_test or 'python3 -m unittest'}]
                                        if phase == 'EXEC' else [])}
        if 'Phase: POLISH' in prompt:
            answer['self_run_evidence'] = [{'command': configured_test or 'python3 -m unittest'}]
            if os.environ.get('FAKE_POLISH_NO_EVIDENCE'):
                answer['self_run_evidence'] = []
                extra_observed_commands = [{'command': configured_test or 'python3 -m unittest'}]
        for finding in findings:
            finding.setdefault('security', False)
        if prior is not None:
            answer['prior_findings'] = prior
        mode = os.environ.get('FAKE_MUTATION')
        if mode and 'Role: reviewer,' in prompt:
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
            if label and os.environ.get('FAKE_AUTHOR_ESCAPE_SKIP') == label:
                continue
            if label and os.environ.get('FAKE_AUTHOR_ESCAPE_MALFORMED') == label:
                exit_code = None
            if label and os.environ.get('FAKE_AUTHOR_ESCAPE_WRITE') == label:
                Path(shlex.split(command)[-1]).write_text('fake escaped\n')
                exit_code = 0
            if label and os.environ.get('FAKE_AUTHOR_ESCAPE_WRITE_THEN_DELETE') == label:
                target = Path(shlex.split(command)[-1]); target.write_text('fake escaped\n'); target.unlink()
                exit_code = 0
            if index < 2:
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

    snapshot_mode = os.environ.get('FAKE_SNAPSHOT_MODE')
    if snapshot_mode == 'wrong':
        answer['reviewed_snapshot'] = '0' * 40
    elif snapshot_mode != 'missing' and 'reviewed_snapshot' in answer:
        answer['reviewed_snapshot'] = answer['reviewed_snapshot']
    if vendor == 'codex':
        emit_codex(answer, session, locals().get('command_events'))
    else:
        emit_claude(answer, session, extra_observed_commands)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
