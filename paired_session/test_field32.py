"""FIELD-32 (poker-tools ui02, v2.12.4): twice a legacy-format run reached DONE with the gate's HIGH/MEDIUM fix never
re-gated: once the fix landed in polish and the run went from polish to DONE; once on the last EXEC round (reviewer and shadow
APPROVE, then DONE). An operator-run review of the final tree found real gaps both times. Before DONE, a tree the gate has not
seen while its findings at MEDIUM or above were open after its last pass gets one more gate pass."""
import os
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc
from paired_session import test_real_coordinator as trc


class GateRecheckTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def coordinator(self, *extra):
        co = self.h.coordinator('--shadow', 'off', '--max-exec-rounds', '2', *extra)
        co.state.update(phase='EXEC', next='gate', exec_rounds=1)
        patcher = patch.object(co, '_author_tmp_isolated', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        co._freeze_role_dispatch()
        return co

    def gate(self, co, **env):
        with patch.dict(os.environ, env):
            co.gate_turn()

    def fix(self, co, close=True):   # the author's change after the gate, closed by the next reviewer as fixed
        (self.h.workspace / 'tracked.txt').write_text('fixed after the gate\n')
        if close:
            for row in co.state['finding_ledger']:
                if row['status'] == 'open':
                    row['status'] = 'fixed'
                    row['status_history'].append({'round': co.state['sequence'] + 1, 'status': 'fixed', 'evidence': 'author fix'})

    def gates(self, co):
        return sum(turn['role'] == 'gate' for turn in co.state['turns'])

    def test_a_last_round_fix_of_a_gate_high_is_gated_before_done(self):
        co = self.coordinator('--polish-round', 'off')
        marker = self.h.root / 'blocked-once'
        self.gate(co, FAKE_GATE_BLOCK_ONCE=str(marker))                  # HIGH; one round left
        self.assertEqual(co.state['next'], 'author')
        self.fix(co)
        co.state['exec_rounds'] = 2                                        # the author used the last round
        self.assertEqual(co.start_polish_or_done(), 'ACTIVE')              # the reviewer's APPROVE
        self.assertEqual((co.state['next'], co.state['gate_ran']), ('gate', False))
        self.gate(co, FAKE_GATE_BLOCK_ONCE=str(marker))                    # the re-gate approves the final tree
        self.assertEqual((co.state['status'], self.gates(co)), ('DONE', 2))

    def test_a_gate_rejecting_the_last_round_fix_holds(self):
        co = self.coordinator('--polish-round', 'off')
        self.gate(co, FAKE_GATE_BLOCK='1')
        self.fix(co)
        co.state['exec_rounds'] = 2
        self.assertEqual(co.start_polish_or_done(), 'ACTIVE')              # not DONE: the gate sees the fix first
        self.gate(co, FAKE_GATE_BLOCK='1')
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('EXEC round limit reached after adversarial gate', co.state['hold_reason'])

    def test_a_polish_fix_of_a_gate_medium_is_gated_before_done(self):
        co = self.coordinator()
        self.gate(co, FAKE_GATE_MINOR='1')                                 # MEDIUM: non-blocking, so polish
        self.assertTrue(co.state['polish']['active'])
        self.fix(co)
        co.state['polish'].update(active=False, completed=True)
        self.assertEqual(co.done(), 'ACTIVE')                              # the polish reviewer's APPROVE
        self.assertEqual(co.state['next'], 'gate')
        self.gate(co)
        self.assertEqual((co.state['status'], self.gates(co)), ('DONE', 2))
        self.assertEqual(len(co.state['gate_rechecks']), 1)

    def test_without_open_gate_findings_there_is_no_extra_gate(self):
        for env in ({}, {'FAKE_GATE_LOW': '1'}):                           # approve, or a LOW finding only
            with self.subTest(env=env):
                self.h.run_dir = self.h.root / ('run-' + str(len(env)))
                co = self.coordinator('--polish-round', 'off')
                self.gate(co, **env)
                self.fix(co, close=False)
                self.assertEqual(co.done(), 'DONE')
                self.assertEqual(self.gates(co), 1)

    def test_an_unchanged_tree_is_not_gated_again(self):
        co = self.coordinator()
        self.gate(co, FAKE_GATE_MINOR='1')
        co.state['polish'].update(active=False, completed=True)            # polish declined the finding: no change
        self.assertEqual(co.done(), 'DONE')
        self.assertEqual(self.gates(co), 1)

    def test_with_the_gate_off_nothing_changes(self):
        co = self.coordinator('--adversarial-gate', 'off', '--polish-round', 'off')
        self.fix(co, close=False)
        self.assertEqual(co.done(), 'DONE')

    def test_no_invocation_headroom_holds_with_the_reason(self):
        co = self.coordinator('--polish-round', 'off')
        self.gate(co, FAKE_GATE_MINOR='1')
        self.fix(co)
        co.state['invocations_used'] = co.args.max_invocations
        self.assertEqual(co.done(), 'HOLD')
        self.assertIn('no invocation is left for the final gate pass', co.state['hold_reason'])
        self.assertEqual(co.state['next'], 'gate')                         # resume runs that gate pass


if __name__ == '__main__':
    unittest.main()
