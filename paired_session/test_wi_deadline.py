"""F2 (v2.9.7): the optional whole work-item deadline `--wi-deadline`."""
import json
import subprocess
import time
import unittest

from paired_session import test_real_coordinator as trc
from paired_session import timeout_scale as tsc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action', 'coordinator')
PROMPT = 'Role: persistent. Phase: PLAN.'


class WiDeadlineTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def flags(self, *extra):
        return ['--timeout', tsc.scaled_arg(10), *extra]

    def resumed(self, *extra):
        return rc.Coordinator(rc.parser().parse_args(['resume', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir), *self.flags(*extra),
            '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli())]))

    def test_off_by_default_saves_no_key_and_never_holds(self):
        co = self.coordinator(*self.flags())
        self.assertNotIn('wi_deadline', co.state['config'])
        co.state['started_at'] -= 10 ** 7
        self.assertIsNone(co._wi_deadline_issue())

    def test_only_a_positive_deadline_is_accepted(self):
        for value in ('0', '-5'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'positive number of seconds'):
                self.coordinator(*self.flags('--wi-deadline', value))

    def test_a_running_turn_keeps_its_timeout_and_the_next_dispatch_holds(self):
        co = self.coordinator(*self.flags('--wi-deadline', '100'))
        self.assertEqual(co.state['config']['wi_deadline'], 100)
        turn_timeout = int(tsc.scaled_arg(10))
        co.state['started_at'] = time.time() - (100 - turn_timeout / 2)   # half a turn timeout left
        co._invoke_once('author', 'PLAN', PROMPT, rc.author_schema())
        self.assertEqual(co.state['turns'][-1]['timeout_seconds'], turn_timeout)
        co.state['started_at'] = time.time() - 200
        with self.assertRaisesRegex(RuntimeError, r'whole-WI deadline reached: 2\d\ds .*--wi-deadline 100s'):
            co._invoke_once('author', 'PLAN', PROMPT, rc.author_schema())
        self.assertEqual((co.state['sequence'], co.state['invocations_used'], len(co.state['turns'])), (1, 1, 1))
        self.assertIsNone(co.state['active'])

    def test_resume_keeps_the_saved_deadline_and_refuses_a_change(self):
        self.coordinator(*self.flags('--wi-deadline', '100'))
        self.assertEqual(self.resumed().args.wi_deadline, 100)
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: wi_deadline'):
            self.resumed('--wi-deadline', '200')
        self.run_dir = self.root / 'no-deadline'
        self.coordinator(*self.flags())
        with self.assertRaisesRegex(ValueError, 'fixed at run start'):
            self.resumed('--wi-deadline', '100')
        self.assertIsNone(self.resumed().args.wi_deadline)

    def test_a_held_run_past_its_deadline_holds_again_on_resume_without_a_turn(self):
        done = self.run_coordinator('--wi-deadline', '100000', '--stop-after-plan')
        state_path = self.run_dir / 'state.json'
        state = json.loads(state_path.read_text())
        self.assertEqual((done.returncode, state['status']), (2, 'HOLD'), done.stdout + done.stderr)
        state['started_at'] -= 100001                               # the HOLD and the time after it count
        state_path.write_text(json.dumps(state))
        command = self.command('--wi-deadline', '100000', '--skip-probe')
        command[2] = 'resume'
        for _ in range(2):
            held = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
            after = json.loads(state_path.read_text())
            self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
            self.assertIn('whole-WI deadline reached', after['hold_reason'])
            self.assertEqual((after['sequence'], after['invocations_used']), (state['sequence'], state['invocations_used']))

    def test_a_done_run_past_its_deadline_refuses_reject_and_stays_acceptable(self):
        done = self.run_coordinator('--wi-deadline', '100000', '--shadow', 'off', '--polish-round', 'off')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state_path = self.run_dir / 'state.json'
        state = json.loads(state_path.read_text())
        state['started_at'] -= 100001
        state_path.write_text(json.dumps(state))
        polish = self.command('--shadow', 'off', '--polish-round', 'off', '--skip-probe', '--polish')
        polish[2] = 'resume'
        for action, run in (('reject', lambda: self.run_operator_action('reject', '--text', 'redo it')),
                            ('resume --polish', lambda: subprocess.run(polish, cwd=self.root, text=True, capture_output=True))):
            with self.subTest(action=action):
                refused = run()
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn(action + ' refused: whole-WI deadline reached', refused.stdout + refused.stderr)
                after = json.loads(state_path.read_text())
                self.assertEqual((after['status'], after['rejected_digests'], after['sequence']),
                                 ('DONE', state['rejected_digests'], state['sequence']))
        accepted = self.run_operator_action('accept')
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(json.loads(state_path.read_text())['status'], 'ACCEPTED')

    def test_before_the_deadline_reject_and_resume_polish_still_work(self):
        done = self.run_coordinator('--wi-deadline', '100000', '--shadow', 'off', '--polish-round', 'off')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state_path = self.run_dir / 'state.json'
        polish = self.command('--shadow', 'off', '--polish-round', 'off', '--skip-probe', '--polish')
        polish[2] = 'resume'
        polished = subprocess.run(polish, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(polished.returncode, 0, polished.stdout + polished.stderr)
        before = json.loads(state_path.read_text())
        rejected = self.run_operator_action('reject', '--text', 'redo it')   # intent-only, then --expect: the digest must still match
        self.assertNotIn('refused', rejected.stdout + rejected.stderr)
        self.assertNotIn('stale', rejected.stdout + rejected.stderr)
        after = json.loads(state_path.read_text())
        self.assertEqual(len(after['rejected_digests']), len(before['rejected_digests']) + 1, rejected.stdout + rejected.stderr)
        self.assertGreater(after['sequence'], before['sequence'])

    def test_a_wall_clock_moved_back_holds_and_never_refunds(self):
        co = self.coordinator(*self.flags('--wi-deadline', '100'))
        co.state['wi_clock'] = time.time() + 3600
        self.assertIn('wall clock moved back', co._wi_deadline_issue())
        co.state['started_at'] = time.time() - 70
        co.state['wi_clock'] = seen = time.time() + 40               # a small step back is tolerated; elapsed uses the later time
        self.assertIn('whole-WI deadline reached', co._wi_deadline_issue())
        self.assertEqual(co.state['wi_clock'], seen)


if __name__ == '__main__':
    unittest.main()
