"""WI-OPVVOID: replaying a reviewer receipt preserves operator proof for the live tree."""
import json
import unittest
from unittest.mock import patch

from paired_session import test_operator_verification as tov
from paired_session import test_real_coordinator as trc

rc = trc.rc


class OperatorVerificationReplayTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in tov._HELPERS})
    log = tov.OperatorVerificationTests.log
    attach = tov.OperatorVerificationTests.attach

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def test_resume_voids_an_old_record_before_dispatch(self):
        co = self.coordinator()
        co.state.update(phase='EXEC', next='reviewer')
        self.attach(co)
        (self.workspace / 'tracked.txt').write_text('operator edit\n')
        live_tree = rc.git_snapshot(self.workspace)[0]
        with patch.object(co, 'drive', return_value='ACTIVE'):
            self.assertEqual(co.resume(), 'ACTIVE')
        [record] = self.state()['operator_verifications']
        self.assertEqual((record['status'], record['voided']['reason'], record['voided']['tree_seen']),
                         ('voided', 'tree changed', live_tree))

    OPTIONS = ('--lifecycle-mode', 'off', '--shadow', 'on', '--adversarial-gate', 'off',
               '--polish-round', 'off', '--test-command', 'python3 -m unittest')

    def approve_then_change_tree(self):
        co = self.coordinator(*self.OPTIONS)
        co.state.update(phase='EXEC', next='reviewer', exec_rounds=1)
        co.save()
        stray = self.workspace / 'stray.txt'
        stray.write_text('untracked file\n')
        co.materialize_review_context()
        old_tree = rc.git_snapshot(self.workspace)[0]
        reviewed = co.invoke('reviewer', 'EXEC', co._review_prompt('reviewer', old_tree), rc.review_schema())
        self.assertEqual((reviewed['answer']['status'], reviewed['answer']['reviewed_snapshot']), ('APPROVE', old_tree))
        self.assertEqual(self.state()['pending_reviewer_result_sequence'], reviewed['sequence'])
        co.hold('fixture pause after approval')
        stray.unlink()
        live_tree = rc.git_snapshot(self.workspace)[0]
        self.assertNotEqual(live_tree, old_tree)
        return reviewed, live_tree

    def test_second_stale_reviewer_replay_on_the_same_tree_holds(self):
        reviewed, live_tree = self.approve_then_change_tree()
        state = self.state()
        state['stale_rereview_trees'] = [live_tree]   # this tree was already re-routed once
        (self.run_dir / 'state.json').write_text(json.dumps(state))
        resumed = self.run_operator_action('resume', *self.OPTIONS)
        held = self.state()
        self.assertIn('HOLD: stale EXEC approval', resumed.stdout, resumed.stdout + resumed.stderr)
        self.assertNotIn('NOTE: the tree changed after the EXEC reviewer approved', resumed.stdout)
        self.assertEqual((held['status'], held['hold_reason'], held['next'], held['pending_reviewer_result_sequence'],
                          held['stale_rereview_trees']), ('HOLD', 'stale EXEC approval', 'reviewer', None, [live_tree]))
        self.assertEqual([turn['sequence'] for turn in held['turns'] if turn['role'] == 'reviewer'], [reviewed['sequence']])
        self.assertEqual(held['exec_comparisons'][-1]['effective_verdict'], 'HOLD')

    def test_stale_reviewer_replay_keeps_live_operator_proof_and_rereviews(self):
        options = self.OPTIONS
        reviewed, live_tree = self.approve_then_change_tree()
        log, digest = self.log('Ran 1 test\nOK\n')
        attached = self.run_operator_action('attach-verification', *options, '--command', 'python3 -m unittest',
                                            '--exit-code', '0', '--log', str(log), '--log-sha256', digest,
                                            '--note', 'ran on the host')
        self.assertEqual(attached.returncode, 0, attached.stdout + attached.stderr)
        self.assertIn(f'VERIFICATION: V001 bound to tree {live_tree}', attached.stdout)

        env = {'FAKE_MISSING_OBSERVED': '1'}
        resumed = self.run_operator_action('resume', *options, env=env)
        finished = self.state()

        self.assertNotIn('HOLD: stale EXEC approval', resumed.stdout)
        self.assertIn('NOTE: the tree changed after the EXEC reviewer approved', resumed.stdout)
        [record] = finished['operator_verifications']
        self.assertEqual((record['status'], record['tree_sha256']), ('current', live_tree),
                         (record.get('voided'), finished['hold_reason']))
        self.assertIsNone(finished['pending_reviewer_result_sequence'])
        self.assertEqual([(turn['snapshot_before'], turn.get('operator_test_evidence'))
                          for turn in finished['turns'] if turn['role'] == 'shadow'],
                         [(live_tree, 'V001'), (live_tree, 'V001')])
        self.assertEqual(finished['status'], 'DONE', finished['hold_reason'])
        stale = finished['exec_comparisons'][0]   # v3.16.5: the re-routed approval is not shown as effective
        self.assertEqual((stale['review_sequence'], stale['effective_verdict']), (reviewed['sequence'], 'STALE'))
        self.assertIn('Effective workflow verdict: **STALE**', (self.run_dir / 'review-comparison.md').read_text())
        first, second = [turn for turn in finished['turns'] if turn['role'] == 'reviewer']
        self.assertEqual((first['sequence'], second['answer']['reviewed_snapshot'],
                          second.get('operator_test_evidence')), (reviewed['sequence'], live_tree, 'V001'))
