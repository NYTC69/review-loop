"""V317-ONCE replaced by WI-GATE1 S3 (owner 2026-11-19, Q2): an advisory-only REVISE is an approval with advisory rows in
every round and phase; no advisory repair round, no per-phase attempt. The EXEC exit keeps its tree and test-evidence checks."""
import json
import unittest
from unittest.mock import patch

from paired_session import test_worktree_lifecycle as twl
from paired_session import test_v316 as v316

rc, OFF, ON = twl.rc, v316.OFF, v316.ON


class AdvisoryOnceTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})

    def test_exec_prior_only_exits_with_rounds_left_on_both_routes(self):
        for name, flags in (('off', OFF), ('on', ON)):
            with self.subTest(route=name):
                self.run_dir = self.root / name
                done = self.run_coordinator(*flags, '--max-exec-rounds', '4', env={   # WI-GATE1: a reviewer MINOR row
                    **v316.SHAPED, 'FAKE_EXEC_STILL_OPEN': v316.KEEP})
                self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
                state = json.loads((self.run_dir / 'state.json').read_text())
                verdicts = [r['effective_verdict'] for r in state['review_verdicts'] if r['phase'] == 'EXEC']
                self.assertEqual(verdicts, ['APPROVE', 'APPROVE_WITH_ADVISORY'])   # the gate repair's review exits at once
                self.assertEqual(state['exec_rounds'], 2)
                v316.RoundLimitStillOpenTests.assert_exit(self, state, 'MINOR')
                self.assertEqual(len([t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC']), 2)

    def record_review(self, co, phase, severity=None, security=False, **changes):
        snapshot = rc.git_snapshot(co.workspace)[0]
        sequence = co.state['sequence'] + 1
        ids = [r['id'] for r in co.open_findings()]
        answer = {'status': 'REVISE', 'full_review': ([{'severity': severity, 'security': security,
                  'file': 'plan.md' if phase == 'PLAN' else 'tracked.txt', 'summary': 'advisory note',
                  'failure_scenario': 'small detail'}] if severity else []),
                  'prior_findings': [{'id': i, 'disposition': 'still_open', 'evidence': 'not addressed'} for i in ids],
                  'verified_claims': [], 'reviewed_snapshot': snapshot,
                  'self_run_evidence': [{'command': co.args.test_command}],
                  'observed_commands': [{'command': co.args.test_command, 'exit_code': 0,
                                         'error': False, 'output': 'Ran 1 test successfully'}], **changes}
        co.state.update(phase=phase, next='reviewer', sequence=sequence, pending_reviewer_result_sequence=sequence)
        co.state['turns'].append({'sequence': sequence, 'role': 'reviewer', 'phase': phase,
                                 'answer': answer, 'snapshot_before': snapshot, 'open_finding_ids': ids})
        co.save()
        return sequence

    def coordinator_at(self, phase):
        co = self.coordinator(*OFF, '--adversarial-gate', 'off', '--max-exec-rounds', '4')
        (co.context / 'plan.md').write_text('Implement the helper.\n')
        co.state[phase.lower() + '_rounds'] = 1
        return co

    def test_own_advisories_and_prior_only_exit_in_both_phases_in_the_first_round(self):
        for phase in ('PLAN', 'EXEC'):
            for severity in ('MINOR', 'LOW', None):
                with self.subTest(phase=phase, severity=severity):
                    self.run_dir = self.root / (phase + str(severity))
                    co = self.coordinator_at(phase)
                    if severity is None:   # only an earlier row left open
                        co.record_findings('persistent-reviewer', phase, 1, [{'severity': 'MINOR', 'file': 'plan.md',
                                           'summary': 'earlier note', 'failure_scenario': 'small'}])
                    self.record_review(co, phase, severity)
                    co.reviewer_turn()
                    self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
                    self.assertEqual(co.state[phase.lower() + '_rounds'], 1)
                    self.assertNotIn('advisory_repair_attempts', co.state)
                    self.assertTrue(all(r['status'] == 'open' and r.get('advisory') for r in co.state['finding_ledger']))

    def test_a_blocking_review_still_goes_to_the_author_and_its_fix_review_exits(self):
        for phase in ('PLAN', 'EXEC'):
            for security in (False, True):   # v3.8.1: LOW+security is advisory, MAJOR blocks either way
                with self.subTest(phase=phase, security=security):
                    self.run_dir = self.root / (phase + str(security))
                    co = self.coordinator_at(phase)
                    self.record_review(co, phase, 'MAJOR', security)
                    co.reviewer_turn()
                    self.assertEqual((co.state['next'], co.state['review_verdicts'][-1]['effective_verdict']), ('author', 'REVISE'))
                    for row in co.blocking_open_findings():
                        row['status'] = 'fixed'
                    co.state[phase.lower() + '_rounds'] = 2
                    self.record_review(co, phase, 'MINOR')
                    co.reviewer_turn()
                    self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')

    def test_exec_early_exit_keeps_evidence_and_snapshot_requirements(self):
        # WI-GATE1 S3 R2 F8: missing test evidence alone HOLDs for attach-verification (no author round); a stale tree
        # still goes to the author
        for name, changes, route in (('not-run', {'observed_commands': []}, 'hold'),
                                     ('other-command', {'observed_commands': [{'command': 'python3 -m unittest', 'exit_code': 1,
                                                        'error': True, 'output': 'FAILED (failures=1)'}]}, 'hold'),
                                     ('no-evidence', {'self_run_evidence': []}, 'hold'),
                                     ('stale', {'reviewed_snapshot': '0' * 40}, 'author')):
            with self.subTest(name=name):
                self.run_dir = self.root / name
                co = self.coordinator_at('EXEC')
                self.record_review(co, 'EXEC', 'MINOR', **changes)
                co.reviewer_turn()
                self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'REVISE')
                self.assertEqual(co.state['exec_rounds'], 1)
                if route == 'hold':
                    self.assertEqual((co.state['status'], co.state['hold_reason'], co.state['next']),
                                     ('HOLD', rc.TEST_EVIDENCE_HOLD, 'reviewer'))
                else:
                    self.assertEqual(co.state['next'], 'author')

    def test_resume_replays_the_exit_without_dispatch(self):
        for phase in ('PLAN', 'EXEC'):
            with self.subTest(phase=phase):
                self.run_dir = self.root / phase
                co = self.coordinator_at(phase)
                self.record_review(co, phase, 'MINOR')   # durable, not yet advanced: a crash before the state moved on
                resumed = rc.Coordinator(co.args)
                with patch.object(resumed, 'invoke') as invoke:
                    resumed.reviewer_turn()
                    invoke.assert_not_called()
                self.assertEqual(resumed.state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')

    def test_an_operator_send_back_review_with_only_advisories_exits_at_once(self):
        for route, flags in (('off', OFF), ('on', ON)):
            with self.subTest(route=route):
                self.run_dir = self.root / route
                flags = (*flags, '--max-exec-rounds', '6', '--max-invocations', '80')
                done = self.run_coordinator(*flags, env={'FAKE_EXEC_MINOR_REVISE': '1'})
                self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
                co = rc.Coordinator(rc.parser().parse_args(self.command(*flags)[2:]))
                self.assertEqual(co.state['exec_rounds'], 1)   # no advisory repair round
                text = 'Fix the remaining advisory.'
                co.args.expect = co.operator_intent('reject', text, None)['digest']
                self.assertEqual(co.reject(text, None), 'ACTIVE')
                with (co.workspace / 'tracked.txt').open('a') as stream:
                    stream.write('repair after operator feedback\n')
                co.state['exec_rounds'] = 2
                self.record_review(co, 'EXEC')
                co.reviewer_turn()
                self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
                self.assertLess(co.state['exec_rounds'], co.exec_round_limit())


if __name__ == '__main__':
    unittest.main()
