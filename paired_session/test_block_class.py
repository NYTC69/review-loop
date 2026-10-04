"""FIELD-5 (v2.9.7): three consecutive EXEC reviewer/gate BLOCKs of one explicitly labeled finding class HOLD."""
import json
import os
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action', 'coordinator')
LABEL = {'FAKE_EXEC_MIXED_REVISE': '1', 'FAKE_FINDING_CLASS': 'evidence-path-leak'}
FLAGS = ('--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')


class BlockClassTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def test_three_same_class_reviewer_blocks_hold_with_the_class_history(self):
        held = self.run_coordinator(*FLAGS, '--max-exec-rounds', '5', env=LABEL)
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        state = self.state()
        self.assertRegex(state['hold_reason'], r'^structural fix / re-scope needed: finding class evidence-path-leak '
                                               r'blocked 3 consecutive reviews \(reviewer #\d+: F\d+; reviewer #\d+: F\d+; reviewer #\d+: F\d+\)')
        record = state['structural_holds'][-1]
        self.assertEqual((record['classes'], [event['source'] for event in record['events']]), (['evidence-path-leak'], ['reviewer'] * 3))
        blockers = [row for row in state['finding_ledger'] if row['severity'] == 'MAJOR']
        self.assertEqual((len(blockers), blockers[-1]['status']), (3, 'open'))   # the HOLD closes nothing: the open blocker stays open
        self.assertEqual([row['id'] for row in blockers],
                         [i for event in record['events'] for i in event['classes']['evidence-path-leak']])
        self.assertEqual((state['status'], state['next'], state['exec_rounds']), ('HOLD', 'author', 3))
        self.assertEqual(state.get('unlabeled_blocking_findings'), 0)

    def test_unlabeled_blockers_are_counted_and_never_hold(self):
        held = self.run_coordinator(*FLAGS, '--max-exec-rounds', '4', env={'FAKE_EXEC_MIXED_REVISE': '1'})
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        state = self.state()
        self.assertEqual(state['hold_reason'], 'EXEC round limit reached')
        self.assertNotIn('structural_holds', state)
        self.assertEqual(state['unlabeled_blocking_findings'], 4)
        reviews = [row for row in state['turns'] if row['role'] == 'reviewer' and row['phase'] == 'EXEC']
        self.assertEqual([row.get('unlabeled_blocking_findings') for row in reviews], [1, 1, 1, 1])

    def test_the_round_limit_hold_keeps_precedence_and_its_owner_override(self):
        held = self.run_coordinator(*FLAGS, '--max-exec-rounds', '3', env=LABEL)
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        state = self.state()
        self.assertEqual(state['hold_reason'], 'EXEC round limit reached')   # three labeled blocks, but the round limit wins
        self.assertEqual(len([event for event in state['block_class_events'] if event['block']]), 3)
        self.assertNotIn('structural_holds', state)   # no record of a HOLD that did not happen
        done = self.run_operator_action('accept', '--override-rejection', '--reason', 'Owner ruling: accepted for this tree.')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.state()['status'], 'ACCEPTED')

    def test_gate_blocks_between_reviewer_approvals_hold_on_the_third(self):
        co = self.coordinator('--shadow', 'off', '--polish-round', 'off', '--max-exec-rounds', '9')
        co.state.update(phase='EXEC', next='gate', exec_rounds=1)
        co.state['config']['lifecycle_mode'] = 'on'   # the gate re-runs after every reviewer approval (the WI-81 loop)
        with patch.object(co, '_author_tmp_isolated', return_value=True):
            co._freeze_role_dispatch()
            with patch.dict(os.environ, {'FAKE_GATE_BLOCK': '1', 'FAKE_FINDING_CLASS': 'evidence-path-leak'}):
                for _ in range(2):
                    co.gate_turn()
                    self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'author'))
                    co.author_turn()
                    co.reviewer_turn()
                    self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'gate'))   # an approval routed to the gate is neutral
                co.gate_turn()
        state = co.state
        self.assertEqual((state['status'], state['next']), ('HOLD', 'author'))
        self.assertRegex(state['hold_reason'], r'finding class evidence-path-leak blocked 3 consecutive reviews '
                                               r'\(gate #\d+: F\d+; gate #\d+: F\d+; gate #\d+: F\d+\)')
        gate_rows = [row for row in state['finding_ledger'] if row['source'] == 'adversarial-gate']
        self.assertTrue(all(row['summary'].startswith('[class: evidence-path-leak] ') for row in gate_rows))   # visible to the reviewer

    def test_labels_are_explicit_and_mixed_sources_share_a_class(self):
        co = self.coordinator()
        rows = lambda *texts: [{'id': f'F{index}', 'summary': text} for index, text in enumerate(texts)]
        self.assertIsNone(co._structural_block_hold('reviewer', 1, rows('[class: path-leak] a', 'leak through a path')))
        self.assertIsNone(co._structural_block_hold('gate', 2, [{'id': 'F9', 'body': '[class: path-leak] Trigger: x'}]))
        self.assertIsNone(co._structural_block_hold('reviewer', 2, rows('[class: other] b')))   # a replayed verdict counts once
        self.assertIsNone(co._structural_block_hold('reviewer', 3, rows('[class: Path-Leak] c', 'path-leak d')))   # not a label
        self.assertEqual(co.state['unlabeled_blocking_findings'], 3)
        self.assertIsNone(co._structural_block_hold('reviewer', 4, rows('x', '[class: path-leak] e')))   # sequence 3 broke the run
        self.assertIsNone(co._structural_block_hold('gate', 5, [{'id': 'F7', 'body': ' [class: path-leak] f'}]))
        self.assertIsNone(co._structural_block_hold('reviewer', 6, []))   # a review-ending approval breaks the run
        for sequence in (7, 8):
            self.assertIsNone(co._structural_block_hold('reviewer', sequence, rows('[class: path-leak] g', '[class: z] h')))
        found = co._structural_block_hold('gate', 9, [{'id': 'F5', 'body': '[class: z] i'}, {'id': 'F6', 'body': '[class: path-leak] j'}])
        self.assertEqual(found['classes'], ['path-leak', 'z'])
        self.assertNotIn('structural_holds', co.state)   # recorded only when the HOLD happens
        self.assertEqual(co._structural_hold(found), 'HOLD')
        self.assertEqual(co.state['hold_reason'], 'structural fix / re-scope needed: finding class path-leak, z blocked 3 consecutive '
                         'reviews (reviewer #7: F0, F1; reviewer #8: F0, F1; gate #9: F6, F5); all blockers stay open; '
                         'note a structural plan and resume, note --scope-change, or abort')
        self.assertEqual(len(co.state['structural_holds']), 1)
        self.assertIsNone(co._structural_block_hold('reviewer', 10, rows('[class: z] k')))   # after a HOLD the count starts afresh


if __name__ == '__main__':
    unittest.main()
