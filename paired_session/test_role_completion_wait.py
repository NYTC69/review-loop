"""W11: real process waits and existing answer collection, on Windows and POSIX."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc
from paired_session import winplat


class RoleCompletionWaitTests(unittest.TestCase):
    def run_cli(self, vendor, terminal=True, linger=True, split=False, event=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout, stderr = root / 'stdout.jsonl', root / 'stderr.log'
            answer = {'verdict': 'HOLD', 'self_run_evidence': 'shell calls never returned'}
            rows = ([{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': json.dumps(answer)}}]
                    if vendor == 'codex' else [])
            if terminal:
                rows.append({'type': event or ('turn.completed' if vendor == 'codex' else 'result'),
                             'structured_output': answer})
            payload = ''.join(json.dumps(row) + '\n' for row in rows)
            script = root / 'cli.py'
            script.write_text('import sys, time\nsys.stdin.buffer.read()\n'
                              + ('sys.stdout.write("not-json\\n[]\\n"); sys.stdout.flush()\n' if split else '')
                              + 'payload = ' + repr(payload) + '\n'
                              + ('sys.stdout.write(payload[:-1]); sys.stdout.flush(); time.sleep(.1)\n'
                                 'sys.stdout.write(payload[-1:]); sys.stdout.flush()\n' if split else
                                 'sys.stdout.write(payload); sys.stdout.flush()\n')
                              + ('time.sleep(30)\n' if linger else ''), encoding='utf-8')
            receipt = {'vendor': vendor, 'role': 'reviewer'}
            output = io.StringIO()
            with stdout.open('wb') as out, stderr.open('wb') as err:
                process, job = winplat.spawn_group([sys.executable, str(script)], 'w11-test',
                                                   stdin=subprocess.PIPE, stdout=out, stderr=err)
            started = time.monotonic()
            try:
                with patch.object(rc, 'EXIT_AFTER_COMPLETION_GRACE_SECONDS', .2), \
                        patch.object(rc, 'ROLE_WAIT_STEP_SECONDS', .03), contextlib.redirect_stdout(output):
                    if terminal:
                        rc.wait_role_turn(process, b'prompt', stdout, 3, receipt, job)
                    else:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            rc.wait_role_turn(process, b'prompt', stdout, .35, receipt, job)
                elapsed = time.monotonic() - started
                if terminal:
                    collected = rc.Coordinator._collect(None, vendor, stdout, stderr)[0]
                    self.assertEqual(collected, answer)
                if terminal and linger:
                    self.assertTrue(receipt['exit_after_completion']['killed'])
                    self.assertGreaterEqual(receipt['exit_after_completion']['waited_s'], .19)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn("reviewer CLI did not exit", output.getvalue())
                    self.assertIn("kept the turn's answer", output.getvalue())
                    if winplat.IS_WINDOWS and vendor == 'codex':
                        self.assertIn('restarting Windows clears it', output.getvalue())
                else:
                    self.assertNotIn('exit_after_completion', receipt)
                    self.assertEqual(output.getvalue(), '')
                if not terminal:
                    self.assertGreaterEqual(elapsed, .34)
                self.assertLess(elapsed, 2.5)
            finally:
                if process.poll() is None:
                    winplat.kill_group(process.pid, job)
                process.wait()
                if process.stdin:
                    process.stdin.close()

    def test_codex_completion_lingers(self):
        self.run_cli('codex')

    def test_claude_result_lingers(self):
        self.run_cli('claude')

    def test_codex_failed_is_terminal(self):
        self.run_cli('codex', event='turn.failed')

    def test_partial_rows_are_buffered(self):
        self.run_cli('codex', split=True)

    def test_no_terminal_event_keeps_outer_timeout(self):
        self.run_cli('codex', terminal=False)

    def test_normal_exit_has_no_receipt_key(self):
        for vendor in ('codex', 'claude'):
            with self.subTest(vendor=vendor):
                self.run_cli(vendor, linger=False)
