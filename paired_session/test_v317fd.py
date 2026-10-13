"""Operator diagnostics and timeout recovery, using local fake CLIs only."""
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc


class OperatorFailureTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in
                    ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'coordinator', 'fake_codex_cli', 'fake_claude_cli')})

    def failing_coordinator(self, mode, vendor, stdout='', stderr='', code=1):
        self.run_dir = self.root / ('run-' + mode + '-' + vendor)
        cli = self.root / ('failure-' + mode + '-' + vendor)
        cli.write_text(f'#!{sys.executable}\n# fake_cli.py diagnostic fixture\nimport sys\nsys.stdin.read()\n'
                       f'sys.stdout.write({stdout!r})\nsys.stderr.write({stderr!r})\nsys.exit({code})\n')
        cli.chmod(0o755)
        co = self.coordinator('--lifecycle-mode', mode, '--author-vendor', vendor,
                              '--' + vendor + '-bin', str(cli))
        return co

    def assert_hold(self, co, expected):
        self.assertEqual(co.drive(), 'HOLD')
        self.assertEqual(co.state['hold_reason'], expected)
        receipt = json.loads((co.evidence / '001-plan-author.receipt.json').read_text())
        self.assertEqual(receipt['error'], expected)
        return receipt

    def test_codex_authentication_error_uses_last_event_message(self):
        for mode in ('off', 'on'):
            with self.subTest(mode=mode):
                text = '401 Unauthorized: run codex login'
                stdout = '\n'.join(json.dumps(row) for row in (
                    {'type': 'error', 'message': 'earlier error'},
                    {'type': 'turn.failed', 'error': {'message': text}})) + '\n'
                co = self.failing_coordinator(mode, 'codex', stdout, 'rollout bookkeeping failed\n')
                self.assert_hold(co, 'CLI exit 1: ' + text)

    def test_claude_error_result_on_nonzero_and_zero_exit(self):
        for mode in ('off', 'on'):
            for code in (1, 0):
                with self.subTest(mode=mode, code=code):
                    text = 'Invalid API key - Please run /login'
                    co = self.failing_coordinator(mode, 'claude',
                        json.dumps({'type': 'result', 'is_error': True, 'result': text}) + '\n', code=code)
                    prefix = 'CLI exit 1' if code else 'Claude emitted no successful result'
                    self.assert_hold(co, prefix + ': ' + text)
                    # Each subcase has its own saved run.
                    self.run_dir.rename(self.root / ('done-' + mode + '-' + str(code)))

    def test_empty_failure_output_keeps_bare_prefix(self):
        for mode in ('off', 'on'):
            with self.subTest(mode=mode):
                self.assert_hold(self.failing_coordinator(mode, 'codex', code=7), 'CLI exit 7')

    def test_stderr_fallback_is_last_line_and_bounded_without_controls(self):
        for mode in ('off', 'on'):
            with self.subTest(mode=mode):
                co = self.failing_coordinator(mode, 'codex', 'not a JSON event\n',
                    'earlier\n\x1b[31mLogin\x1b[0m\tfailed\x00 ' + 'x' * 250 + '\n\n')
                self.assert_hold(co, 'CLI exit 1: ' + ('Login failed ' + 'x' * 250)[:200])
        reason = rc.cli_failure_reason('CLI exit 1', json.dumps(
            {'type': 'error', 'message': 'first\nsecond\r\x00\t' + 'x' * 250}))
        self.assertEqual(reason, 'CLI exit 1: ' + ('first second ' + 'x' * 250)[:200])

    def test_recognized_limit_keeps_typed_hold(self):
        for mode in ('off', 'on'):
            with self.subTest(mode=mode):
                co = self.failing_coordinator(mode, 'codex', stderr='HTTP 429 rate limit\n')
                self.assertEqual(co.drive(), 'HOLD')
                receipt = json.loads((co.evidence / '001-plan-author.receipt.json').read_text())
                self.assertEqual(receipt['error_kind'], 'rate_limited')
                self.assertEqual(co.state['hold_reason'], 'rate_limited: provider rejected the call; '
                    'it did not consume the invocation budget; reset time unavailable')
                self.assertFalse(receipt['invocation_budget_counted'])

    def test_timeout_names_duration_without_using_output(self):
        # Simulate a timeout rather than sleeping or signalling a real process.
        class TimeoutProcess:
            pid = 12345
            returncode = None

            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired('fake-cli', kwargs['timeout'])

            def wait(self):
                self.returncode = -9
                return -9

        real_popen = rc.subprocess.Popen

        def popen(*args, **kwargs):
            if not hasattr(kwargs.get('stderr'), 'write'):
                return real_popen(*args, **kwargs)
            kwargs['stderr'].write(b'401 Unauthorized\n')
            return TimeoutProcess()

        for mode in ('off', 'on'):
            with self.subTest(mode=mode):
                co = self.failing_coordinator(mode, 'codex')
                co.args.timeout = 17
                with patch.object(rc.subprocess, 'Popen', side_effect=popen), patch.object(rc.os, 'killpg'):
                    receipt = self.assert_hold(co, 'CLI exit -9 (timed out after 17 s)')
                self.assertTrue(receipt['timed_out'])

    def test_resume_timeout_above_old_cap_is_used_and_cannot_decrease(self):
        for mode in ('off', 'on'):
            with self.subTest(mode=mode):
                co = self.failing_coordinator(mode, 'codex')
                co.state['config']['timeout'] = 9000
                co.state.update(status='HOLD', hold_reason='operator pause')
                co.save()
                argv = ['resume', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                    '--run-dir', str(self.run_dir), '--lifecycle-mode', mode,
                    '--codex-bin', co.args.codex_bin, '--claude-bin', co.args.claude_bin]
                with self.assertRaisesRegex(ValueError, '--resume-timeout must be between'):
                    rc.Coordinator(rc.parser().parse_args(argv + ['--resume-timeout', '8999']))
                resumed = rc.Coordinator(rc.parser().parse_args(argv + ['--resume-timeout', '12000']))
                self.assertEqual(resumed.args.timeout, 12000)
                # A PLAN dispatch uses the increased timeout and persists it.
                with patch.object(resumed, 'drive', side_effect=lambda: resumed._invoke_once(
                        'author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})):
                    with self.assertRaisesRegex(RuntimeError, 'CLI exit 1'):
                        resumed.resume()
                receipt = json.loads((resumed.evidence / '001-plan-author.receipt.json').read_text())
                self.assertEqual(receipt['timeout_seconds'], 12000)
                self.assertEqual(json.loads(resumed.state_path.read_text())['config']['timeout'], 12000)


if __name__ == '__main__':
    unittest.main()
