"""FIELD-11b (owner 2026-10-04, "give it a chance"): when the plan is approved in the LAST PLAN round but carries review history
that the fresh shadow/gate would refuse, the author gets ONE extra rewrite-only turn outside the PLAN round cap. The same check then
runs again on the reviewer's re-review: a clean, unchanged-in-substance plan moves to EXEC; a second failure or a REVISE holds as
before. A work item that carries review history still holds at once. The turn is recorded in state and in the audit trail, and a
resume in the middle of it neither counts it twice nor grants a second chance."""
import json
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc
from paired_session import test_real_coordinator as trc
from paired_session.test_field11_plan_ledger_ids import PLAN_CLEAN, PLAN_WITH_IDS


class PlanRewriteTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def last_round(self, plan=PLAN_WITH_IDS):
        args = rc.parser().parse_args(['run', '--workspace', str(self.h.workspace), '--workitem', str(self.h.workitem),
                                       '--run-dir', str(self.h.run_dir)])
        co = rc.Coordinator(args)
        co.state.update(phase='PLAN', next='reviewer', plan_rounds=co.args.max_plan_rounds)
        (co.context / 'plan.md').write_text(plan)
        (co.run_dir / f'plan-{co.args.max_plan_rounds:02d}.md').write_text(plan)   # as author_turn writes the approved plan
        co.save()
        return co

    def review(self, co, sequence, status='APPROVE', severity='MAJOR', file='plan.md'):
        snap = rc.git_snapshot(self.h.workspace)[0]
        answer = {'status': status, 'reviewed_snapshot': snap, 'prior_findings': [], 'self_run_evidence': [],
                  'full_review': [] if status != 'REVISE' else [{'id': 'F009', 'severity': severity, 'file': file, 'line': 1,
                                                                 'summary': 'the rewrite dropped the verification step',
                                                                 'failure_scenario': 'x', 'evidence': 'x', 'security': False}]}
        result = {'answer': answer, 'snapshot': snap, 'sequence': sequence, 'role': 'reviewer'}
        with patch.object(co, 'invoke', return_value=result) as invoke, patch.object(co, 'render'):
            co.reviewer_turn()
        return invoke

    def rewrite(self, co, sequence, body=PLAN_CLEAN):
        snap = rc.git_snapshot(self.h.workspace)[0]
        co.state['turns'].append({'sequence': sequence, 'role': 'author', 'phase': 'PLAN'})   # the receipt invoke would record
        result = {'answer': {'status': 'READY', 'body': body}, 'snapshot': snap, 'sequence': sequence, 'role': 'author'}
        with patch.object(co, 'invoke', return_value=result) as invoke, patch.object(co, 'render'):
            co.author_turn()
        return invoke

    def test_the_rewrite_succeeds_and_the_plan_moves_to_exec(self):
        co = self.last_round()
        self.review(co, 10)
        self.assertEqual((co.state['next'], co.state['plan_history_rewrite']['status']), ('author', 'requested'))
        invoke = self.rewrite(co, 11)
        prompt = invoke.call_args.args[2]
        self.assertIn('Rewrite-only turn', prompt)                                 # the prompt states it
        self.assertIn('Do not change the scope', prompt)
        self.assertEqual(co.state['plan_history_rewrite']['status'], 'written')
        self.assertEqual((co.context / 'plan.md').read_text(), PLAN_CLEAN)
        self.assertEqual((co.run_dir / 'plan-rewrite.md').read_text(), PLAN_CLEAN)
        invoke = self.review(co, 12)
        self.assertIn('Rewrite-only re-review', invoke.call_args.args[2])        # the normal PLAN reviewer judges the substance
        self.assertIn('F001', invoke.call_args.args[2])                           # against the plan it approved
        self.assertEqual((co.state['status'], co.state['phase'], co.state['next']), ('ACTIVE', 'EXEC', 'author'))
        self.assertEqual(co.state['plan_history_rewrite']['status'], 'passed')

    def test_the_extra_turn_is_not_counted_against_the_plan_round_cap(self):
        co = self.last_round()
        self.review(co, 10)
        self.rewrite(co, 11)
        self.assertEqual(co.state['plan_rounds'], co.args.max_plan_rounds)
        self.assertFalse((co.run_dir / f'plan-{co.args.max_plan_rounds + 1:02d}.md').exists())

    def test_a_second_failure_holds(self):
        co = self.last_round()
        self.review(co, 10)
        self.rewrite(co, 11, body=PLAN_WITH_IDS)                                  # the history is still there
        self.review(co, 12)
        self.assertEqual((co.state['status'], co.state['phase']), ('HOLD', 'PLAN'))
        self.assertIn('the one rewrite-only PLAN turn did not remove it', co.state['hold_reason'])
        self.assertEqual(co.state['plan_history_rewrite']['status'], 'failed')
        self.assertNotIn('round_limit_hold', co.state)

    def test_a_rewrite_that_changes_the_plan_in_substance_holds(self):
        co = self.last_round()
        self.review(co, 10)
        self.rewrite(co, 11, body='# Plan\n## Steps\n1. Validate input.\n')     # verification dropped
        self.review(co, 12, status='REVISE')
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('rewrite-only PLAN turn was not approved', co.state['hold_reason'])
        self.assertEqual(co.state['plan_history_rewrite']['failed_history'], 'PLAN reviewer REVISE')

    def test_a_revise_on_the_rewrite_holds_even_when_advisory_or_out_of_phase_findings_would_turn_it_into_approve(self):   # R1 MAJOR
        for severity, file in (('MINOR', 'plan.md'), ('LOW', 'plan.md'), ('MAJOR', 'plan-03.md')):
            with self.subTest(severity=severity, file=file):
                co = self.last_round()
                self.review(co, 10)
                self.rewrite(co, 11)
                self.review(co, 12, status='REVISE', severity=severity, file=file)
                self.assertEqual((co.state['status'], co.state['phase']), ('HOLD', 'PLAN'))
                self.assertEqual(co.state['plan_history_rewrite']['status'], 'failed')

    def test_a_resume_after_a_failed_rewrite_holds_again_without_a_new_review(self):   # R1 MEDIUM
        co = self.last_round()
        self.review(co, 10)
        self.rewrite(co, 11, body=PLAN_WITH_IDS)
        self.review(co, 12)
        reason = co.state['hold_reason']
        resumed = rc.Coordinator(co.args)
        resumed.state.update(status='ACTIVE', hold_reason='')                     # as resume reopens a HOLD before the drive loop
        with patch.object(resumed, 'invoke') as invoke:
            resumed.reviewer_turn()
        invoke.assert_not_called()
        self.assertEqual((resumed.state['status'], resumed.state['hold_reason']), ('HOLD', reason))

    def test_a_crash_after_the_rewrite_receipt_but_before_its_state_reruns_it_without_counting(self):   # R1 LOW: the real crash point
        co = self.last_round()
        self.review(co, 10)
        co.state['turns'].append({'sequence': 11, 'role': 'author', 'phase': 'PLAN'})   # invoke saved the receipt, author_turn did not run
        co.save()
        resumed = rc.Coordinator(co.args)
        self.assertEqual((resumed.state['next'], resumed.state['plan_history_rewrite']['status']), ('author', 'requested'))
        self.rewrite(resumed, 12)                                                   # no pending sequence: the turn is invoked again
        record = resumed.state['plan_history_rewrite']
        self.assertEqual((resumed.state['plan_rounds'], record['status'], record['author_sequence']), (resumed.args.max_plan_rounds, 'written', 12))

    def test_an_author_hold_in_the_rewrite_is_audited_and_keeps_the_one_chance(self):   # R1 LOW
        co = self.last_round()
        self.review(co, 10)
        self.rewrite(co, 11, body=PLAN_CLEAN)
        self.assertEqual(co.state['plan_history_rewrite']['attempt_sequences'], [11])
        co2 = self.last_round()
        self.review(co2, 10)
        snap = rc.git_snapshot(self.h.workspace)[0]
        held = {'answer': {'status': 'HOLD', 'body': 'blocked'}, 'snapshot': snap, 'sequence': 13, 'role': 'author'}
        with patch.object(co2, 'invoke', return_value=held), patch.object(co2, 'render'):
            co2.author_turn()
        self.assertEqual((co2.state['status'], co2.state['plan_history_rewrite']['status']), ('HOLD', 'requested'))
        self.assertEqual(co2.state['plan_history_rewrite']['attempt_sequences'], [13])

    def test_the_progress_label_of_the_rewrite_is_not_a_plan_round(self):   # R1 LOW
        co = self.last_round()
        self.review(co, 10)
        with patch.object(co, 'progress'):
            co._progress_phase('author', 'PLAN')
        self.assertEqual(co._progress_label, 'PLAN rewrite')

    def test_a_work_item_with_review_history_still_holds_at_once_in_the_last_round(self):
        co = self.last_round(plan=PLAN_CLEAN)
        (co.context / 'workitem.md').write_text('Fix the crash reported as F123.\n')
        self.review(co, 10)
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertNotIn('plan_history_rewrite', co.state)

    def test_the_turn_is_recorded_in_state_and_the_audit_trail(self):
        co = self.last_round()
        with patch.object(co, 'progress') as progress:
            self.review(co, 10)
            self.rewrite(co, 11)
            self.review(co, 12)
        record = co.state['plan_history_rewrite']
        self.assertEqual((record['requested_sequence'], record['author_sequence'], record['passed_sequence']), (10, 11, 12))
        self.assertIn('ledger-id-shaped tokens F001, F003', record['history'])
        self.assertTrue(next(t for t in co.state['turns'] if t['sequence'] == 11)['plan_rewrite'])
        self.assertIn('PLAN APPROVE rejected', co.state['approve_refusals'][-1]['reason'])
        self.assertEqual([c.kwargs['status'] for c in progress.call_args_list if c.args and c.args[0] == 'plan_rewrite'],
                         ['requested', 'written', 'passed'])
        saved = json.loads((co.run_dir / 'state.json').read_text())
        self.assertEqual(saved['plan_history_rewrite']['status'], 'passed')

    def test_resume_in_the_middle_of_the_rewrite(self):
        co = self.last_round()
        self.review(co, 10)
        resumed = rc.Coordinator(co.args)                                         # a new process after a crash: state from disk
        self.assertEqual((resumed.state['next'], resumed.state['plan_history_rewrite']['status']), ('author', 'requested'))
        self.review(resumed, 10)                                                  # the same approval replayed: no second chance spent
        self.assertEqual((resumed.state['status'], resumed.state['plan_history_rewrite']['status']), ('ACTIVE', 'requested'))
        snap = rc.git_snapshot(self.h.workspace)[0]                               # the author turn finished, its state was not saved
        resumed.state['turns'].append({'sequence': 11, 'role': 'author', 'phase': 'PLAN', 'snapshot_after': snap,
                                       'answer': {'status': 'READY', 'body': PLAN_CLEAN}})
        resumed.state['pending_author_result_sequence'] = 11
        resumed.save()
        again = rc.Coordinator(co.args)
        with patch.object(again, 'invoke') as invoke, patch.object(again, 'render'):
            again.author_turn()
        invoke.assert_not_called()                                                # the recorded turn is reused, not run again
        self.assertEqual((again.state['plan_rounds'], again.state['plan_history_rewrite']['status']), (again.args.max_plan_rounds, 'written'))
        self.review(again, 12)
        self.assertEqual((again.state['phase'], again.state['plan_history_rewrite']['status']), ('EXEC', 'passed'))


if __name__ == '__main__':
    unittest.main()
