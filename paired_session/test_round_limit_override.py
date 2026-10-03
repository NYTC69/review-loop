"""Batch RLO (v2.9.5): accept --override-rejection at a PLAN or EXEC round-limit HOLD."""
import json
import unittest

from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action', 'coordinator')
REASON = 'Owner ruling: the remaining findings are accepted for this tree.'


class RoundLimitOverrideTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def assert_override_accepts(self, hold_reason):
        held = self.state()
        self.assertEqual((held['status'], held['hold_reason']), ('HOLD', hold_reason))
        tree = rc.git_snapshot(self.workspace)[0]
        self.assertEqual(held['round_limit_hold']['tree_sha256'], tree)
        open_rows = [row for row in held['finding_ledger'] if row['status'] == 'open']
        self.assertTrue(open_rows)
        done = self.run_operator_action('accept', '--override-rejection', '--reason', REASON)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = self.state()
        record = json.loads((self.run_dir / 'evidence' / 'acceptance.json').read_text())
        self.assertEqual((state['status'], state['acceptance'], record['accepted_state']), ('ACCEPTED', record, 'HOLD'))
        self.assertEqual((record['override_rejection'], record['reason'], record['author'], record['rationale']), (True, REASON, 'operator', None))
        self.assertEqual((record['intent']['tree_sha256'], record['round_limit_hold']), (tree, held['round_limit_hold']))
        self.assertEqual(record['open_findings'], [{'id': row['id'], 'severity': row['severity'], 'source': row['source'], 'security': bool(row.get('security')),
                                                    'summary': ' '.join(row['summary'].split())[:200]}
                                                   for row in open_rows])
        return record

    def test_an_exec_round_limit_hold_is_accepted_with_its_open_findings(self):
        held = self.run_coordinator('--exercise-revisions', '--max-exec-rounds', '1', '--shadow', 'off', '--polish-round', 'off', '--gate-vendor', 'claude')
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        self.assertEqual(self.assert_override_accepts('EXEC round limit reached')['round_limit_hold']['phase'], 'EXEC')

    def test_a_plan_round_limit_hold_is_accepted_with_its_open_findings(self):
        held = self.run_coordinator('--max-plan-rounds', '1', env={'FAKE_PLAN_APPROVE_SECURITY': 'always'})
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        record = self.assert_override_accepts('PLAN round limit reached')
        self.assertEqual(record['round_limit_hold']['phase'], 'PLAN')

    def held(self, reason='EXEC round limit reached'):
        co = self.coordinator('--quiet-progress')
        co.state['phase'] = 'EXEC'
        co.record_findings('persistent-reviewer', 'EXEC', 1, [{'severity': 'MAJOR', 'file': 'tracked.txt', 'summary': 'author verification evidence unavailable'}])
        co.round_limit_hold(reason)
        co.args.override_rejection, co.args.reason = True, REASON
        return co

    def test_the_override_is_refused_for_a_changed_tree_another_cause_or_no_reason(self):
        co = self.held()
        co.args.reason = '  '
        with self.assertRaisesRegex(ValueError, 'non-empty --reason'):
            co.accept()
        co.args.reason = REASON
        extra = self.workspace / 'changed-after-hold.txt'
        extra.write_text('not the held tree')
        with self.assertRaisesRegex(ValueError, 'unchanged held tree'):
            co.accept()
        extra.unlink()
        for key in ('active', 'uncertain_active'):
            with self.subTest(key):
                co.state[key] = {'role': 'reviewer', 'pid': 42424242}
                with self.assertRaisesRegex(ValueError, 'override requires'):
                    co.accept()
                co.state[key] = None
        for cause in ('permission probe: author probe UNKNOWN', 'author changed git control files: .git/config',
                      'aborted by operator', 'reviewer HOLD', 'stale EXEC approval'):
            with self.subTest(cause):
                co.hold(cause)                                                  # a later HOLD on the same tree with any other cause
                with self.assertRaisesRegex(ValueError, 'override requires'):
                    co.accept()
        co.state['hold_reason'] = 'EXEC round limit reached'
        self.assertEqual(co._override_tree(), rc.git_snapshot(self.workspace)[0])
        co.state['rejected_digests'].append(co.state['round_limit_hold']['tree_sha256'])   # an operator-rejected tree stays with the rejected-tree path
        with self.assertRaisesRegex(ValueError, 'override requires'):
            co.accept()
        co.state['rejected_digests'].pop()
        saved = co.state.pop('round_limit_hold')                                # a round-limit reason with no recorded held tree
        with self.assertRaisesRegex(ValueError, 'override requires'):
            co.accept()
        co.state['round_limit_hold'] = {**saved, 'hold_reason': 'PLAN round limit reached'}   # recorded for another limit HOLD
        with self.assertRaisesRegex(ValueError, 'override requires'):
            co.accept()
        co.state['round_limit_hold'] = saved
        co.args.override_rejection = False                                      # without the override a round-limit HOLD still needs DONE
        with self.assertRaisesRegex(ValueError, 'accept requires a DONE run'):
            co.accept()
        co.args.override_rejection = True
        self.assertEqual(co.accept(), 'ACCEPTED')
        self.assertEqual([row['id'] for row in co.state['acceptance']['open_findings']], ['F001'])

    def test_the_gate_round_limit_hold_is_covered_too(self):
        co = self.held('EXEC round limit reached after adversarial gate')
        self.assertEqual(co.accept(), 'ACCEPTED')
        self.assertEqual(co.state['acceptance']['open_findings'], [{'id': 'F001', 'severity': 'MAJOR', 'source': 'persistent-reviewer', 'security': False,
                                                                     'summary': 'author verification evidence unavailable'}])


if __name__ == '__main__':
    unittest.main()
