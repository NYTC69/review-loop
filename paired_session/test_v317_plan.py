"""V317: PLAN prior-only advisory REVISE takes the final-round exit, preserving rewrite-only failure."""
import json
import unittest
from unittest.mock import patch

from paired_session import test_worktree_lifecycle as twl
from paired_session.test_field11_plan_ledger_ids import PLAN_CLEAN, PLAN_WITH_IDS

rc = twl.rc


class PlanStillOpenTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})

    def review(self, severity='MINOR', rounds=3, rewrite=False, rows=True):
        co = self.coordinator('--lifecycle-mode', 'off', '--shadow', 'off', '--adversarial-gate', 'off')
        self.assertEqual(co.args.max_plan_rounds, 3)
        (co.context / 'plan.md').write_text('Implement the helper and run unittest.\n')
        findings = co.record_findings('persistent-reviewer', 'PLAN', 1, [
            {'severity': severity, 'file': 'plan.md', 'summary': 'plan advisory', 'failure_scenario': 'detail missing'}]) if rows else []
        sequence = co.state['sequence'] + 1
        snapshot = rc.git_snapshot(co.workspace)[0]
        # PLAN deliberately has no observed test, self-run evidence or reviewed-snapshot precondition.
        answer = {'status': 'REVISE', 'full_review': [], 'verified_claims': [], 'self_run_evidence': [],
                  'observed_commands': [], 'reviewed_snapshot': '0' * 40,
                  'prior_findings': [{'id': row['id'], 'disposition': 'still_open', 'evidence': 'not addressed'}
                                     for row in findings]}
        co.state.update(phase='PLAN', next='reviewer', plan_rounds=rounds, sequence=sequence,
                        pending_reviewer_result_sequence=sequence)
        if rewrite:
            co.state['plan_history_rewrite'] = {'status': 'written'}
        co.state['turns'].append({'sequence': sequence, 'role': 'reviewer', 'phase': 'PLAN', 'answer': answer,
                                  'snapshot_before': snapshot, 'open_finding_ids': [row['id'] for row in findings]})
        co.save()
        co.reviewer_turn()
        return co.state

    def test_at_the_plan_limit_advances_to_exec_with_open_advisory(self):
        state = self.review()
        self.assertEqual((state['status'], state['phase'], state['next']), ('ACTIVE', 'EXEC', 'author'))
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
        [row] = state['finding_ledger']
        self.assertEqual((row['status'], row.get('advisory')), ('open', True))
        self.assertIn(row['id'], state['delivered_review'])

    def test_fake_cli_run_continues_into_exec_with_the_row_open_and_advisory(self):
        done = self.run_coordinator('--lifecycle-mode', 'off', '--shadow', 'off', '--adversarial-gate', 'off',
                                    '--max-exec-rounds', '1', env={'FAKE_PLAN_MINOR_REVISE': '1',
                                    'FAKE_PLAN_STILL_OPEN': '1', 'FAKE_EXEC_STILL_OPEN': 'plan advisory'})
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['plan_rounds'], 3)
        self.assertEqual([(r['reviewer_raw_verdict'], r['effective_verdict']) for r in state['review_verdicts']
                          if r['phase'] == 'PLAN'], [('REVISE', 'REVISE')] * 2 + [('REVISE', 'APPROVE_WITH_ADVISORY')])
        [row] = state['finding_ledger']
        self.assertEqual((row['phase'], row['status'], row.get('advisory')), ('PLAN', 'open', True))
        [author] = [t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC']
        prompt = (self.run_dir / 'evidence' / ('%03d-exec-author.prompt.txt' % author['sequence'])).read_text()
        self.assertIn(row['id'], prompt)
        self.assertIn('"status": "ADVISORY"', prompt)
        self.assertIn('These findings are explicitly non-blocking.', prompt)

    def test_one_round_left_returns_to_the_plan_author(self):
        state = self.review(rounds=2)
        self.assertEqual((state['status'], state['phase'], state['next']), ('ACTIVE', 'PLAN', 'author'))
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'REVISE')
        self.assertFalse(state['finding_ledger'][0].get('advisory'))
        self.assertIn('"fixed": false', state['delivered_review'])

    def test_major_still_open_at_the_limit_holds(self):
        state = self.review(severity='MAJOR')
        self.assertEqual((state['status'], state['hold_reason']), ('HOLD', 'PLAN round limit reached'))
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'REVISE')
        self.assertFalse(state['finding_ledger'][0].get('advisory'))

    def test_rewrite_only_raw_revise_still_fails_after_advisory_conversion(self):
        state = self.review(rewrite=True)
        self.assertEqual((state['status'], state['phase']), ('HOLD', 'PLAN'))
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
        self.assertEqual(state['plan_history_rewrite']['status'], 'failed')
        self.assertEqual(state['plan_history_rewrite']['failed_history'], 'PLAN reviewer REVISE')
        self.assertIn('rewrite-only PLAN turn was not approved', state['hold_reason'])
        self.assertTrue(state['finding_ledger'][0]['advisory'])

    def test_history_refusal_requests_rewrite_then_approve_or_raw_revise(self):
        for status in ('APPROVE', 'REVISE'):
            with self.subTest(rewrite_verdict=status):
                self.run_dir = self.root / ('history-' + status.lower())
                co = self.coordinator('--lifecycle-mode', 'off')
                self.assertEqual((co.args.shadow, co.args.adversarial_gate), ('on', 'on'))
                co.state.update(phase='PLAN', next='reviewer', plan_rounds=co.args.max_plan_rounds)
                (co.context / 'plan.md').write_text(PLAN_WITH_IDS)
                (co.run_dir / 'plan-03.md').write_text(PLAN_WITH_IDS)
                [row] = co.record_findings('persistent-reviewer', 'PLAN', 1, [
                    {'severity': 'MINOR', 'file': 'plan.md', 'summary': 'plan advisory',
                     'failure_scenario': 'detail missing'}])
                snapshot = rc.git_snapshot(co.workspace)[0]

                def review(sequence, verdict):
                    answer = {'status': verdict, 'reviewed_snapshot': snapshot, 'full_review': [],
                              'self_run_evidence': [], 'prior_findings': [
                                  {'id': row['id'], 'disposition': 'still_open', 'evidence': 'not addressed'}]}
                    result = {'answer': answer, 'snapshot': snapshot, 'sequence': sequence, 'role': 'reviewer'}
                    with patch.object(co, 'invoke', return_value=result) as invoke, patch.object(co, 'render'):
                        co.reviewer_turn()
                    return invoke.call_args.args[2]

                review(10, 'REVISE')
                [row] = co.state['finding_ledger']
                self.assertTrue(row['advisory'])  # conversion happened before FIELD-11 refused the history
                self.assertEqual((co.state['status'], co.state['phase'], co.state['next']), ('ACTIVE', 'PLAN', 'author'))
                self.assertEqual(co.state['plan_history_rewrite']['status'], 'requested')
                self.assertEqual(co.state['review_verdicts'][-1]['reviewer_raw_verdict'], 'REVISE')
                self.assertIn('PLAN APPROVE rejected: plan: ledger-id-shaped tokens', co.state['approve_refusals'][-1]['reason'])
                self.assertIn('The coordinator accepted the plan in substance in the last PLAN round, including any advisory findings left open',
                              co.state['delivered_review'])
                co.state['turns'].append({'sequence': 11, 'role': 'author', 'phase': 'PLAN'})
                result = {'answer': {'status': 'READY', 'body': PLAN_CLEAN}, 'snapshot': snapshot,
                          'sequence': 11, 'role': 'author'}
                with patch.object(co, 'invoke', return_value=result), patch.object(co, 'render'):
                    co.author_turn()
                self.assertEqual(co.state['plan_rounds'], 3)
                self.assertEqual(co.state['plan_history_rewrite']['status'], 'written')
                prompt = review(12, status)
                self.assertIn('the coordinator accepted the previous plan in substance, including any advisory findings left open', prompt)
                self.assertIn('Rows already recorded as advisory may stay still_open without that alone being a reason for REVISE; '
                              'approve when the rewritten plan is the same in substance.', prompt)
                self.assertEqual((row['status'], row['advisory']), ('open', True))
                if status == 'APPROVE':
                    self.assertEqual((co.state['status'], co.state['phase'], co.state['next']), ('ACTIVE', 'EXEC', 'author'))
                    self.assertEqual(co.state['plan_history_rewrite']['status'], 'passed')
                    self.assertIn('"status": "ADVISORY"', co.state['delivered_review'])
                else:
                    self.assertEqual((co.state['status'], co.state['phase']), ('HOLD', 'PLAN'))
                    self.assertEqual(co.state['plan_history_rewrite']['status'], 'failed')
                    self.assertEqual(co.state['plan_history_rewrite']['failed_history'], 'PLAN reviewer REVISE')
                    self.assertIn('rewrite-only PLAN turn was not approved', co.state['hold_reason'])
                    self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')

    def test_no_prior_row_still_open_keeps_the_limit_hold(self):
        state = self.review(rows=False)
        self.assertEqual((state['status'], state['hold_reason']), ('HOLD', 'PLAN round limit reached'))


if __name__ == '__main__':
    unittest.main()
