"""L100 (owner 2026-10-07, legacy soft_limit_plan / soft_limit_exec "continue?"): at a PLAN or EXEC round-limit HOLD,
`resume --add-rounds N` continues the same run with N more rounds of that phase. The frozen cap stays as saved; the
extension is a separate `round_extensions` record that adds to the effective limit."""
import json
import unittest
from unittest import mock

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import review_report
from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE


class AddRoundsTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def held(self, *flags, reason):
        held = self.run_coordinator('--exercise-revisions', *flags)   # the first review of each phase returns REVISE
        self.assertIn('NEXT: resume --add-rounds N (1-10) continues this run', held.stdout, held.stdout + held.stderr)
        self.assertEqual(held.stdout.strip().splitlines()[-1], 'HOLD: ' + reason)
        return self.state()

    def continue_to_done(self, flags, phase, before):
        resumed = self.run_operator_action('resume', '--exercise-revisions', *flags, '--add-rounds', '1')
        self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        state = self.state()
        [extension] = state['round_extensions']
        self.assertEqual((extension['phase'], extension['n']), (phase, 1))
        nxt = next(turn for turn in state['turns'] if turn['sequence'] > before['sequence'])
        self.assertEqual((nxt['role'], nxt['phase']), ('author', phase))   # the author turn the HOLD stopped before
        prompt = self.run_dir / 'evidence' / f"{nxt['sequence']:03d}-{phase.lower()}-author.prompt.txt"
        self.assertIn('round-limit-extension', prompt.read_text())
        self.assertIn(before['finding_ledger'][-1]['id'], prompt.read_text())   # the held review's finding is delivered
        return state

    def test_a_plan_round_limit_hold_continues_with_one_more_plan_round(self):
        before = self.held('--max-plan-rounds', '1', reason='PLAN round limit reached')
        state = self.continue_to_done(('--max-plan-rounds', '1'), 'PLAN', before)
        self.assertEqual((state['plan_rounds'], state['config']['max_plan_rounds']), (2, 1))   # the frozen cap stays

    def test_an_exec_round_limit_hold_continues_on_both_routes(self):
        for route in ('off', 'on'):
            with self.subTest(route=route):
                self.run_dir = self.root / ('exec-' + route)
                flags = ('--max-exec-rounds', '1', '--lifecycle-mode', route)
                before = self.held(*flags, reason='EXEC round limit reached')
                state = self.continue_to_done(flags, 'EXEC', before)
                self.assertEqual((state['exec_rounds'], state['config']['max_exec_rounds']), (2, 1))
                self.assertIn('操作员追加轮次（resume --add-rounds）：EXEC +1', review_report.delivery_section(state))

    def w_hold_then_continue(self, env, reason):
        flags = ('--max-exec-rounds', '1', '--lifecycle-mode', 'on', '--max-invocations', '60')
        held = self.run_coordinator(*flags, env=env)
        self.assertEqual(held.stdout.strip().splitlines()[-1], 'HOLD: ' + reason, held.stdout + held.stderr)
        before = self.state()
        resumed = self.run_operator_action('resume', *flags, '--add-rounds', '1', env=env)
        self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        state = self.state()
        after = [turn for turn in state['turns'] if turn['sequence'] > before['sequence']]
        self.assertEqual(after[0]['role'], 'author')   # the fix author first: no review or gate re-dispatched
        return before, state, after

    def test_a_w_gate_hold_goes_to_the_fix_author_then_the_gate_runs_again(self):
        before, state, after = self.w_hold_then_continue({'FAKE_GATE_BLOCK_ONCE': str(self.root / 'gate-once')},
                                                         'EXEC round limit reached after adversarial gate')
        self.assertEqual(before['next'], 'gate')
        roles = [(turn['role'], turn['phase']) for turn in after]
        self.assertLess(roles.index(('author', 'EXEC')), roles.index(('gate', 'EXEC')))   # fixed, then gated again
        [gate_row] = [row for row in state['finding_ledger'] if row['source'] == 'adversarial-gate' and row['severity'].upper() == 'HIGH']
        prompt = self.run_dir / 'evidence' / f"{after[0]['sequence']:03d}-exec-author.prompt.txt"
        self.assertIn(gate_row['id'], prompt.read_text())

    def test_a_w_polish_fix_hold_keeps_its_fix_turn(self):
        before, state, after = self.w_hold_then_continue(
            {'FAKE_SPECIALIST_BLOCK': 'code-reviewer', 'FAKE_SPECIALIST_BLOCK_ONCE': str(self.root / 'block-once')},
            'EXEC round limit reached')
        self.assertEqual(before['next'], 'polish-fix')   # the HOLD stopped before the POLISH-Q fix author
        [finding] = [row for row in state['finding_ledger'] if row['source'] == 'specialist:code-reviewer']
        self.assertEqual(finding['status'], 'fixed')
        prompt = (self.run_dir / 'evidence' / f"{after[0]['sequence']:03d}-{after[0]['phase'].lower()}-author.prompt.txt").read_text()
        self.assertNotIn('round-limit-extension', prompt)   # the fix leg's own delivered blockers, not a redirected author turn
        self.assertIn(finding['id'], prompt)
        self.assertIn(('gate', 'EXEC'), [(turn['role'], turn['phase']) for turn in after])   # the fix replays EXEC and gate

    def test_report_mode_refuses_and_changes_nothing(self):
        co = self.coordinator('--max-exec-rounds', '1')
        reason = 'EXEC round limit reached'
        co.state.update(status='HOLD', hold_reason=reason, round_limit_hold={'hold_reason': reason, 'phase': 'EXEC'})
        co.state['config']['review_report'] = True
        co.save()
        co.args.add_rounds = 1
        with self.assertRaisesRegex(ValueError, r'--add-rounds needs a PLAN or EXEC round-limit HOLD \(not report mode\)'):
            co.resume()
        saved = self.state()
        self.assertEqual((saved['status'], saved['hold_reason'], 'round_extensions' in saved), ('HOLD', reason, False))

    def test_add_rounds_is_refused_anywhere_else(self):
        stopped = self.run_coordinator('--stop-after-plan')   # a HOLD, but not a round-limit HOLD
        refused = self.run_operator_action('resume', '--add-rounds', '2')
        self.assertIn('REFUSED: --add-rounds needs a PLAN or EXEC round-limit HOLD', refused.stdout, stopped.stdout + refused.stdout)
        self.assertNotIn('round_extensions', self.state())
        for argv in (['run', '--add-rounds', '1'], ['resume', '--polish', '--add-rounds', '1']):
            command = self.command(*argv[1:]); command[2] = argv[0]
            result = rc.subprocess.run([*command, '--skip-probe'], cwd=self.root, text=True, capture_output=True)
            self.assertIn('REFUSED: --add-rounds is only for resume', result.stdout, result.stdout + result.stderr)
        for bad in ('0', '11'):
            with self.assertRaises(SystemExit), mock.patch('sys.stderr'):
                rc.parser().parse_args(['resume', '--workspace', '.', '--workitem', 'w', '--run-dir', 'r', '--add-rounds', bad])

    def test_an_extension_saved_before_a_crash_is_not_applied_twice(self):
        self.held('--max-exec-rounds', '1', reason='EXEC round limit reached')
        args = rc.parser().parse_args(self.command('--exercise-revisions', '--max-exec-rounds', '1', '--add-rounds', '1',
                                                   '--skip-probe')[2:])
        args.action = 'resume'
        co = rc.Coordinator(args)
        with mock.patch.object(co, 'drive', side_effect=RuntimeError('crash after the save')), self.assertRaises(RuntimeError):
            co.resume()
        saved = self.state()
        self.assertEqual((saved['status'], len(saved['round_extensions']), saved['next']), ('ACTIVE', 1, 'author'))
        again = self.run_operator_action('resume', '--exercise-revisions', '--max-exec-rounds', '1', '--add-rounds', '1')
        self.assertIn('REFUSED: --add-rounds needs a PLAN or EXEC round-limit HOLD', again.stdout, again.stdout + again.stderr)
        self.assertEqual(len(self.state()['round_extensions']), 1)

    def test_a_gate_hold_goes_to_the_author_and_a_write_hold_keeps_its_review(self):
        use_lifecycle_on(self, self)
        co = self.coordinator('--max-exec-rounds', '1')
        [row] = co.record_findings('adversarial-gate', 'EXEC', 7, [{'severity': 'high', 'file': 'sum_ints.py', 'body': 'b',
                                                                    'summary': 'gate blocker', 'failure_scenario': 'x'}])
        for reason, nxt, expected in (('EXEC round limit reached after adversarial gate', 'gate', 'author'),
                                      ('EXEC round limit reached after a writer pass', 'reviewer', 'reviewer')):   # D09 holds too
            with self.subTest(reason=reason):
                co.state.update(status='HOLD', hold_reason=reason, next=nxt, phase='EXEC', delivered_review='',
                                round_limit_hold={'hold_reason': reason, 'phase': 'EXEC'})
                self.assertTrue(co.at_round_limit_hold())
                co._add_rounds(2)
                self.assertEqual(co.state['next'], expected)
                self.assertEqual(row['id'] in co.state['delivered_review'], expected == 'author')
        self.assertEqual(co.exec_round_limit(), 5)   # 1 + 2 + 2

    def test_the_limits_count_an_extension_once_next_to_the_writer_replay(self):
        use_lifecycle_on(self, self)
        co = self.coordinator('--max-exec-rounds', '1', '--max-plan-rounds', '2')
        co.state['round_extensions'] = [{'phase': 'EXEC', 'n': 2}, {'phase': 'PLAN', 'n': 3}]
        self.assertEqual((co.exec_round_limit(), co.plan_round_limit()), (3, 5))
        co.state['writer_replay_rounds'] = {'start': 3, 'used': None}   # open replay: max(start + 2, ordinary - ext) + ext
        self.assertEqual(co.exec_round_limit(), 7)
        co.state['writer_replay_rounds']['start'] = 1   # gate LOW 1: start=1, ordinary=1, ext=1 -> 4, not swallowed by the max
        co.state['round_extensions'] = [{'phase': 'EXEC', 'n': 1}]
        self.assertEqual(co.exec_round_limit(), 4)
        co.state['round_extensions'] = [{'phase': 'EXEC', 'n': 2}, {'phase': 'PLAN', 'n': 3}]
        co.state['writer_replay_rounds']['start'] = 3
        co.state['writer_replay_rounds']['used'] = 2   # frozen at DOCS: ordinary + used
        self.assertEqual(co.exec_round_limit(), 5)
        self.assertEqual(co._config()['max_exec_rounds'], 1)   # the resume comparison sees the saved cap only


if __name__ == '__main__':
    unittest.main()
