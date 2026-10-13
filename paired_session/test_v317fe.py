"""V317-FE: per-turn token reporting across failures, fast retries and recovery."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as harness

rc = harness.rc


def claude_rows():
    def sub(message_id, output):
        return {'type': 'assistant', 'parent_tool_use_id': 'tool-1', 'message': {
            'id': message_id, 'usage': {'input_tokens': 9846, 'output_tokens': output}}}
    return [
        {'type': 'stream_event', 'event': {'type': 'message_start', 'message': {
            'usage': {'input_tokens': 35, 'output_tokens': 0}}}},
        sub('sub-a', 2), sub('sub-a', 9), sub('sub-a', 4), sub('sub-b', 3),
        {'type': 'stream_event', 'event': {'type': 'message_delta', 'usage': {'output_tokens': 7}}},
        {'type': 'result', 'is_error': False, 'session_id': 'claude-session',
         'structured_output': {'status': 'READY', 'body': 'plan'},
         'modelUsage': {'model': {'inputTokens': 10 ** 9}}},
    ]


def token_row(stamp, tokens):
    return {'timestamp': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'input_tokens': tokens, 'output_tokens': 2},
                'total_token_usage': {'input_tokens': tokens}}}}


class UsageAccountingTests(unittest.TestCase):
    # Use the existing offline CLI fixture; do not inherit its hundreds of test methods.
    locals().update({name: getattr(harness.RealCoordinatorTests, name) for name in (
        'setUp', 'tearDown', 'coordinator', 'fake_codex_cli', 'fake_claude_cli',
        '_guarded_test_popen', '_assert_no_real_provider_cli')})

    def test_subagents_survive_failure_timeout_and_success_on_both_routes(self):
        for lifecycle in ('off', 'on'):
            for mode in ('failed', 'timed-out', 'success'):
                with self.subTest(lifecycle=lifecycle, mode=mode):
                    self.run_dir = self.root / (lifecycle + '-' + mode)
                    co = self.coordinator('--author-vendor', 'claude', '--lifecycle-mode', lifecycle)
                    rows = claude_rows()
                    rows[-1]['session_id'] = co.state['sessions']['author']
                    # A fake Popen delivers CLI stdout. The timeout is simulated, so this test
                    # neither waits for nor signals a real child process.
                    class Child:
                        pid = 123456
                        returncode = 7 if mode == 'failed' else -9 if mode == 'timed-out' else 0

                        def communicate(self, *args, **kwargs):
                            if mode == 'timed-out':
                                raise subprocess.TimeoutExpired('fake-cli', 1)

                        def wait(self):
                            return self.returncode

                    real_popen = rc.subprocess.Popen

                    def spawn(*args, **kwargs):
                        if not hasattr(kwargs.get('stdout'), 'write'):
                            return real_popen(*args, **kwargs)
                        kwargs['stdout'].write((''.join(json.dumps(row) + '\n' for row in rows)).encode())
                        kwargs['stdout'].flush()
                        return Child()

                    with patch.object(rc.subprocess, 'Popen', spawn), patch.object(rc.os, 'killpg') as signal_child:
                        if mode == 'success':
                            co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', rc.author_schema())
                        else:
                            with self.assertRaisesRegex(RuntimeError, 'CLI exit'):
                                co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', rc.author_schema())
                        self.assertEqual(signal_child.call_count, int(mode == 'timed-out'))
                    receipt = co.state['turns'][-1]
                    self.assertEqual(receipt.get('timed_out', False), mode == 'timed-out')
                    self.assertEqual(receipt['model_requests'], 3)
                    subs = [row for row in receipt['usage_requests'] if row['source'] == 'subagent-message']
                    self.assertEqual([row['message_id'] for row in subs], ['sub-a', 'sub-b'])
                    self.assertEqual(sum(row['input'] for row in subs), 19692)
                    self.assertEqual(sorted(row['output'] for row in subs), [3, 9])
                    self.assertEqual(sum(row['output'] for row in receipt['usage_requests']), 19)
                    self.assertEqual(co.state['invocations_used'], 1)
                    co.write_usage()
                    self.assertEqual(json.loads((co.run_dir / 'usage.json').read_text())['overall']['input_tokens'], 19727)

    def test_subagent_deduplication_across_incremental_drains(self):
        path = self.root / 'incremental.jsonl'
        receipt = {'vendor': 'claude', 'usage_requests': []}
        offset, buffered, active = 0, b'', None
        for row in claude_rows():
            with path.open('a') as stream:
                stream.write(json.dumps(row) + '\n')
            offset, buffered, active, _ = rc.Coordinator._drain_usage_stream(path, offset, buffered, active, receipt)
        self.assertEqual(receipt['model_requests'], 3)
        self.assertEqual([row['output'] for row in receipt['usage_requests']], [7, 9, 3])

    def test_immediate_persistent_retry_excludes_previous_request_on_both_routes(self):
        for lifecycle in ('off', 'on'):
            with self.subTest(lifecycle=lifecycle):
                self.run_dir = self.root / ('retry-' + lifecycle)
                # Seed a role's previous turn, then dispatch it again immediately in the same session.
                now = time.time()
                path = rc.codex_sessions_dir() / 'rollout-same-session.jsonl'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(token_row(now - 0.1, 50000)) + '\n')
                cli = self.root / ('retry-cli-' + lifecycle)
                cli.write_text('#!' + harness.sys.executable + '\n' +
                    'import json, os, sys\nfrom pathlib import Path\nfrom datetime import datetime, timezone\n' +
                    "if sys.argv[1:] == ['--version']: print('codex-cli 0.160.0'); sys.exit(0)\n" +
                    'sys.stdin.read()\n' +
                    'row = ' + repr(token_row(now, 123)) + '\n' +
                    "row['timestamp'] = datetime.fromtimestamp(float(os.environ['V317_RETRY_STAMP']), timezone.utc).isoformat()\n" +
                    'with Path(' + repr(str(path)) + ").open('a') as stream: stream.write(json.dumps(row) + '\\n')\n" +
                    "print(json.dumps({'type': 'thread.started', 'thread_id': 'same-session'}))\n" +
                    "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{}'}}))\n" +
                    "print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 999999}}))\n")
                cli.chmod(0o755)
                co = self.coordinator('--lifecycle-mode', lifecycle, '--reviewer-vendor', 'codex',
                                      '--codex-bin', str(cli))
                now = time.time()
                path.write_text(json.dumps(token_row(now - 0.1, 50000)) + '\n')
                co.state['sessions']['reviewer'] = 'same-session'
                co.state['started']['reviewer'] = True
                co.state['last_end']['reviewer'] = now
                with patch.object(rc.time, 'time', return_value=now + 0.1), patch.dict(
                        os.environ, {'V317_RETRY_STAMP': str(now + 0.2)}):
                    co._invoke_once('reviewer', 'PLAN', 'Role: persistent. Phase: PLAN.', {})
                receipt = co.state['turns'][-1]
                self.assertEqual([row['input'] for row in receipt['usage_requests']], [123])
                self.assertEqual(receipt['usage_lower_bound'], now)
                # The monitor must use the identical bound on a failed turn too.
                monitored = {**receipt, 'usage_requests': [], 'model_requests': 0}
                stopped = threading.Event()
                stopped.set()
                co._monitor_provider_usage(co.evidence / '001-plan-reviewer.stdout.jsonl', monitored, stopped)
                self.assertEqual([row['input'] for row in monitored['usage_requests']], [123])
                self.assertEqual(co.state['invocations_used'], 1)

    def test_codex_home_normalization_finds_request_rows(self):
        for lifecycle in ('off', 'on'):
            for index, value in enumerate(('', '~/.codex')):
                with self.subTest(lifecycle=lifecycle, value=value), patch.dict(os.environ, {'CODEX_HOME': value}):
                    self.run_dir = self.root / ('home-' + lifecycle + '-' + str(index))
                    co = self.coordinator('--lifecycle-mode', lifecycle)
                    home = Path.home() / '.codex'
                    self.assertEqual(co.global_codex_home, home.resolve())
                    path = home / 'sessions' / 'rollout-home-session.jsonl'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    now = time.time()
                    path.write_text(json.dumps(token_row(now, 123)) + '\n')
                    self.assertEqual([row['input'] for row in rc.codex_rollout_usage('home-session', now, now)], [123])
                    self.assertEqual(rc.codex_sessions_dir(), home.resolve() / 'sessions')
                    stdout = self.root / 'home-stdout.jsonl'
                    stdout.write_text(json.dumps({'type': 'thread.started', 'thread_id': 'home-session'}) + '\n')
                    monitored = {'vendor': 'codex', 'start': now, 'usage_requests': []}
                    stopped = threading.Event()
                    stopped.set()
                    co._monitor_provider_usage(stdout, monitored, stopped)
                    self.assertEqual([row['input'] for row in monitored['usage_requests']], [123])

    def test_abandoned_archive_rereads_completed_evidence_on_both_routes(self):
        for lifecycle in ('off', 'on'):
            for vendor in ('claude', 'codex'):
                with self.subTest(lifecycle=lifecycle, vendor=vendor):
                    self.run_dir = self.root / ('archive-' + lifecycle + '-' + vendor)
                    co = self.coordinator('--lifecycle-mode', lifecycle)
                    start = time.time() - 10
                    session = 'abandoned-' + lifecycle
                    rows = claude_rows() if vendor == 'claude' else [
                        {'type': 'thread.started', 'thread_id': session},
                        {'type': 'turn.completed', 'usage': {'input_tokens': 999999, 'output_tokens': 100}}]
                    if vendor == 'codex':
                        rollout = rc.codex_sessions_dir() / ('rollout-' + session + '.jsonl')
                        rollout.parent.mkdir(parents=True, exist_ok=True)
                        rollout.write_text(''.join(json.dumps(row) + '\n' for row in [
                            token_row(start - 2, 50000), token_row(start + 1, 30), token_row(start + 2, 50)]))
                    evidence = co.evidence / '008-exec-author.stdout.jsonl'
                    # The final line need not have a newline when the child closes stdout.
                    evidence.write_text('\n'.join(json.dumps(row) for row in rows))
                    receipt = {'sequence': 8, 'role': 'author', 'phase': 'EXEC', 'vendor': vendor, 'start': start,
                               'invocation_budget_counted': True, 'usage_requests': [
                                   {'input': 1, 'cached': 0, 'output': 1, 'source': 'saved-partial'}]}
                    co.archive_abandoned_turn(receipt)
                    archived = co.state['abandoned_turns'][0]
                    expected = 19727 if vendor == 'claude' else 80
                    self.assertEqual(sum(row['input'] for row in archived['usage_requests']), expected)
                    self.assertEqual(archived['model_requests'], 3 if vendor == 'claude' else 2)
                    if vendor == 'codex':
                        self.assertEqual(archived['usage_requests'], [
                            {'input': 30, 'cached': 0, 'output': 2, 'source': str(rollout) + ':2'},
                            {'input': 50, 'cached': 0, 'output': 2, 'source': str(rollout) + ':3'}])
                    self.assertEqual(json.loads((co.evidence / '008-abandoned.receipt.json').read_text())['usage_requests'],
                                     archived['usage_requests'])
                    co.archive_abandoned_turn(receipt)
                    self.assertEqual(len(co.state['abandoned_turns']), 1)
                    co.write_usage()
                    usage = json.loads((co.run_dir / 'usage.json').read_text())
                    self.assertEqual(usage['overall']['input_tokens'], 0)  # today's design: abandoned excluded
                    self.assertEqual(co.state['invocations_used'], 0)


if __name__ == '__main__':
    unittest.main()
