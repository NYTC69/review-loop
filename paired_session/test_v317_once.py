"""One advisory-only repair per phase, including durable reviewer-result replay."""
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
                done = self.run_coordinator(*flags, '--max-exec-rounds', '4', env={
                    'FAKE_GATE_BLOCK_ONCE': str(self.root / (name + '-gate')),
                    'FAKE_GATE_BLOCK_WITH': 'low', 'FAKE_EXEC_STILL_OPEN': 'simplify the helper'})
                self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
                state = json.loads((self.run_dir / 'state.json').read_text())
                verdicts = [r['effective_verdict'] for r in state['review_verdicts'] if r['phase'] == 'EXEC']
                self.assertEqual(verdicts, ['APPROVE', 'REVISE', 'APPROVE_WITH_ADVISORY'])
                self.assertEqual(state['exec_rounds'], 3)
                v316.RoundLimitStillOpenTests.assert_exit(self, state, 'LOW')
                self.assertEqual(len([t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC']), 3)

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

    def start(self, phase):
        co = self.coordinator(*OFF, '--adversarial-gate', 'off', '--max-exec-rounds', '4')
        (co.context / 'plan.md').write_text('Implement the helper.\n')
        co.state[phase.lower() + '_rounds'] = 1
        sequence = self.record_review(co, phase, 'MINOR')
        co.reviewer_turn()
        self.assertEqual((co.state['next'], co.state['review_verdicts'][-1]['effective_verdict']), ('author', 'REVISE'))
        self.assertEqual(co.state['advisory_repair_attempts'], {phase: sequence})
        return co, sequence

    def test_own_advisories_and_prior_only_exit_in_both_phases(self):
        for phase in ('PLAN', 'EXEC'):
            for severity in ('LOW', None):
                with self.subTest(phase=phase, severity=severity):
                    self.run_dir = self.root / (phase + str(severity))
                    co, first = self.start(phase)
                    co.state[phase.lower() + '_rounds'] = 2
                    self.record_review(co, phase, severity)
                    co.reviewer_turn()
                    self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
                    self.assertEqual(co.state[phase.lower() + '_rounds'], 2)
                    self.assertEqual(co.state['advisory_repair_attempts'], {phase: first})
                    self.assertTrue(all(r['status'] == 'open' and r.get('advisory') for r in co.state['finding_ledger']))

    def test_blocking_second_review_does_not_reset_attempt(self):
        for phase in ('PLAN', 'EXEC'):
            for severity, security in (('MAJOR', False), ('LOW', True)):
                with self.subTest(phase=phase, severity=severity):
                    self.run_dir = self.root / (phase + severity)
                    co, first = self.start(phase)
                    co.state[phase.lower() + '_rounds'] = 2
                    self.record_review(co, phase, severity, security)
                    co.reviewer_turn()
                    self.assertEqual((co.state['next'], co.state['review_verdicts'][-1]['effective_verdict']), ('author', 'REVISE'))
                    self.assertEqual(co.state['advisory_repair_attempts'], {phase: first})
                    # Closing the blocker does not grant another advisory repair.
                    for row in co.blocking_open_findings():
                        row['status'] = 'fixed'
                    self.record_review(co, phase)
                    co.reviewer_turn()
                    self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')

    def test_exec_early_exit_keeps_evidence_and_snapshot_requirements(self):
        for name, changes in (('not-run', {'observed_commands': []}),
                              ('failed', {'observed_commands': [{'command': 'python3 -m unittest', 'exit_code': 1,
                                                                 'error': True, 'output': 'FAILED (failures=1)'}]}),
                              ('no-evidence', {'self_run_evidence': []}),
                              ('stale', {'reviewed_snapshot': '0' * 40})):
            with self.subTest(name=name):
                self.run_dir = self.root / name
                co, first = self.start('EXEC')
                co.state['exec_rounds'] = 2
                self.record_review(co, 'EXEC', **changes)
                co.reviewer_turn()
                self.assertEqual(co.state['next'], 'author')
                self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'REVISE')
                self.assertEqual(co.state['advisory_repair_attempts'], {'EXEC': first})

    def test_resume_replays_first_repair_and_second_exit_without_dispatch(self):
        for phase in ('PLAN', 'EXEC'):
            with self.subTest(phase=phase):
                self.run_dir = self.root / phase
                co, first = self.start(phase)
                for second in (False, True):
                    if second:
                        co.state[phase.lower() + '_rounds'] = 2
                        sequence = self.record_review(co, phase)
                    else:
                        sequence = first
                        co.state.update(phase=phase, next='reviewer', pending_reviewer_result_sequence=first)
                        co.save()
                    resumed = rc.Coordinator(co.args)
                    with patch.object(resumed, 'invoke') as invoke:
                        resumed.reviewer_turn()
                        invoke.assert_not_called()
                    self.assertEqual(resumed.state['review_verdicts'][-1]['effective_verdict'],
                                     'APPROVE_WITH_ADVISORY' if second else 'REVISE')
                    self.assertEqual(resumed.state['advisory_repair_attempts'], {phase: first})
                    # Replay again as if advancement was interrupted, with the same durable result.
                    resumed.state.update(phase=phase, next='reviewer', pending_reviewer_result_sequence=sequence)
                    resumed.save()
                    replay = rc.Coordinator(co.args)
                    with patch.object(replay, 'invoke') as invoke:
                        replay.reviewer_turn()
                        invoke.assert_not_called()
                    self.assertEqual(replay.state['review_verdicts'][-1]['effective_verdict'],
                                     'APPROVE_WITH_ADVISORY' if second else 'REVISE')
                    co = replay


if __name__ == '__main__':
    unittest.main()
