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

    def test_stale_reviewer_replay_keeps_live_operator_proof_and_rereviews(self):
        options = ('--lifecycle-mode', 'off', '--shadow', 'on', '--adversarial-gate', 'off',
                   '--polish-round', 'off', '--test-command', 'python3 -m unittest')
        co = self.coordinator(*options)
        co.state.update(phase='EXEC', next='reviewer', exec_rounds=1)
        co.save()
        stray = self.workspace / 'stray.txt'
        stray.write_text('untracked file\n')
        co.materialize_review_context()
        old_tree = rc.git_snapshot(self.workspace)[0]
        reviewed = co.invoke('reviewer', 'EXEC', co._review_prompt('reviewer', old_tree), rc.review_schema())
        self.assertEqual((reviewed['answer']['status'], reviewed['answer']['reviewed_snapshot']), ('APPROVE', old_tree))
        self.assertEqual(self.state()['pending_reviewer_result_sequence'], reviewed['sequence'])

        stray.unlink()
        live_tree = rc.git_snapshot(self.workspace)[0]
        self.assertNotEqual(live_tree, old_tree)
        log, digest = self.log('Ran 1 test\nOK\n')
        attached = self.run_operator_action('attach-verification', *options, '--command', co.args.test_command,
                                            '--exit-code', '0', '--log', str(log), '--log-sha256', digest,
                                            '--note', 'ran on the host')
        self.assertEqual(attached.returncode, 0, attached.stdout + attached.stderr)
        self.assertIn(f'VERIFICATION: V001 bound to tree {live_tree}', attached.stdout)

        env = {'FAKE_MISSING_OBSERVED': '1'}
        self.run_operator_action('resume', *options, env=env)
        replay = self.state()
        self.run_operator_action('resume', *options, env=env)
        finished = self.state()

        for state in (replay, finished):
            with self.subTest(sequence=state['sequence']):
                [record] = state['operator_verifications']
                self.assertEqual((record['status'], record['tree_sha256']), ('current', live_tree),
                                 (record.get('voided'), state.get('hold_reason')))
        self.assertEqual((replay['status'], replay['hold_reason'], replay['next'],
                          replay['pending_reviewer_result_sequence']),
                         ('HOLD', 'stale EXEC approval', 'reviewer', None))
        [shadow] = [turn for turn in replay['turns'] if turn['role'] == 'shadow']
        self.assertEqual((shadow['snapshot_before'], shadow.get('operator_test_evidence')), (live_tree, 'V001'))
        self.assertEqual(finished['status'], 'DONE', finished['hold_reason'])
        first, second = [turn for turn in finished['turns'] if turn['role'] == 'reviewer']
        self.assertEqual((first['sequence'], second['answer']['reviewed_snapshot'],
                          second.get('operator_test_evidence')), (reviewed['sequence'], live_tree, 'V001'))
