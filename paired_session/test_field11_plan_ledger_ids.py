"""FIELD-11 (poker-news-bot WI-85B, v2.9.4): the final gate refused with "gate independence check rejected ledger ids
in context/plan.md" after the whole EXEC phase, because the approved plan cited reviewer finding ids (F001-F007).

The refusal itself is intended (assert_fresh_prompt: a fresh shadow or gate must not see review history, and the
plan author is told not to cite reviewer ids). The defect is the timing: the plan is approved in PLAN and the
violation only surfaces at the gate. The plan approval now sends such a plan back to the author for a restatement,
and holds early when the work item carries the history or no PLAN round is left.
"""
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc
from paired_session import test_real_coordinator as trc

PLAN_WITH_IDS = '# Plan\n## Steps\n1. Validate input (F001).\n2. Handle the empty list per F003.\n## Verification\nnpm test\n'
PLAN_WITH_NARRATIVE = '# Plan\n## Changes since previous review\n1. Validate input.\n## Verification\nnpm test\n'
PLAN_CLEAN = '# Plan\n## Steps\n1. Validate input.\n2. Handle the empty list.\n## Verification\nnpm test\n'


class PlanLedgerIdTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def coordinator(self, *extra, plan=PLAN_CLEAN, plan_rounds=1):
        args = rc.parser().parse_args(['run', '--workspace', str(self.h.workspace), '--workitem', str(self.h.workitem),
                                       '--run-dir', str(self.h.run_dir), *extra])
        co = rc.Coordinator(args)
        co.state.update(phase='PLAN', next='reviewer', plan_rounds=plan_rounds)
        (co.context / 'plan.md').write_text(plan)
        co.save()
        return co

    def approve(self, co, sequence=2):
        snap = rc.git_snapshot(self.h.workspace)[0]
        answer = {'status': 'APPROVE', 'reviewed_snapshot': snap, 'prior_findings': [], 'full_review': [],
                  'self_run_evidence': []}
        result = {'answer': answer, 'snapshot': snap, 'sequence': sequence, 'role': 'reviewer'}
        with patch.object(co, 'invoke', return_value=result), patch.object(co, 'render'):
            co.reviewer_turn()
        return co

    def test_an_approved_plan_that_cites_ledger_ids_goes_back_to_the_author(self):
        co = self.approve(self.coordinator(plan=PLAN_WITH_IDS))
        self.assertEqual(co.state['status'], 'ACTIVE')
        self.assertEqual((co.state['phase'], co.state['next']), ('PLAN', 'author'))
        self.assertIn('ledger-id-shaped tokens F001, F003', co.state['delivered_review'])
        self.assertIn('independent shadow and gate', co.state['delivered_review'])
        self.assertIsNone(co.state['pending_reviewer_result_sequence'])
        self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'REVISE')   # the audit shows the refusal
        self.assertEqual(co.state['review_verdicts'][-1]['reviewer_raw_verdict'], 'APPROVE')
        self.assertIn('PLAN APPROVE rejected: plan: ledger-id-shaped tokens F001, F003', co.state['approve_refusals'][-1]['reason'])

    def test_after_the_restatement_a_clean_plan_moves_to_exec(self):
        co = self.approve(self.coordinator(plan=PLAN_WITH_IDS))
        (co.context / 'plan.md').write_text(PLAN_CLEAN)                 # the author's restated plan
        co.state.update(plan_rounds=2, next='reviewer')
        self.approve(co, sequence=4)
        self.assertEqual((co.state['status'], co.state['phase'], co.state['next']), ('ACTIVE', 'EXEC', 'author'))

    def test_review_history_wording_is_caught_too(self):
        co = self.approve(self.coordinator(plan=PLAN_WITH_NARRATIVE))
        self.assertEqual((co.state['phase'], co.state['next']), ('PLAN', 'author'))
        self.assertIn("review-history wording 'previous review'", co.state['delivered_review'])

    def test_without_a_plan_round_left_it_holds_now_not_after_exec(self):
        co = self.coordinator(plan=PLAN_WITH_IDS)
        co.state['plan_rounds'] = co.args.max_plan_rounds
        co.save()
        self.approve(co)
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertEqual(co.state['phase'], 'PLAN')
        self.assertIn('approved plan input carries review history (plan: ledger-id-shaped tokens F001, F003)', co.state['hold_reason'])
        self.assertIn('no PLAN round is left', co.state['hold_reason'])
        self.assertNotIn('round_limit_hold', co.state)          # not a round-limit HOLD an owner override could accept as is

    def test_a_work_item_with_ledger_ids_holds_at_plan_because_the_author_cannot_fix_it(self):
        co = self.coordinator()
        (co.context / 'workitem.md').write_text('Fix the crash reported as F123.\n')
        self.approve(co)
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('work item: ledger-id-shaped tokens F123', co.state['hold_reason'])

    def test_without_shadow_or_gate_nothing_scans_so_the_plan_moves_on(self):
        co = self.approve(self.coordinator('--shadow', 'off', '--adversarial-gate', 'off', plan=PLAN_WITH_IDS))
        self.assertEqual((co.state['phase'], co.state['next']), ('EXEC', 'author'))

    def test_a_gate_forced_by_an_operator_note_still_triggers_the_check(self):
        co = self.coordinator('--shadow', 'off', '--adversarial-gate', 'off', plan=PLAN_WITH_IDS)
        co.state['force_gate_after_reject'] = True
        co.save()
        self.approve(co)
        self.assertEqual((co.state['phase'], co.state['next']), ('PLAN', 'author'))

    def test_a_verdict_word_in_the_plan_is_caught_as_the_gate_would(self):
        co = self.approve(self.coordinator(plan='# Plan\n## Addressing REVISE notes\n1. Validate input.\n'))
        self.assertEqual((co.state['phase'], co.state['next']), ('PLAN', 'author'))
        self.assertIn("review-history wording 'REVISE'", co.state['delivered_review'])

    def test_the_progress_log_does_not_call_it_an_rf5_conversion(self):
        co = self.coordinator(plan=PLAN_WITH_IDS)
        with patch.object(co, 'progress') as progress:
            self.approve(co)
        verdicts = [c.kwargs for c in progress.call_args_list if c.args and c.args[0] == 'verdict']
        self.assertEqual(verdicts[-1]['verdict'], 'REVISE')
        self.assertFalse(verdicts[-1]['rf5_converted'])

    def test_a_clean_plan_still_moves_to_exec(self):
        co = self.approve(self.coordinator())
        self.assertEqual((co.state['phase'], co.state['next']), ('EXEC', 'author'))

    def test_the_gate_still_refuses_a_plan_that_cites_ledger_ids(self):
        co = self.approve(self.coordinator())
        (co.context / 'plan.md').write_text(PLAN_WITH_IDS)
        with self.assertRaisesRegex(RuntimeError, 'gate independence check rejected ledger ids in context/plan.md'):
            co.assert_fresh_prompt('gate', 'Review the delta.')


if __name__ == '__main__':
    unittest.main()
