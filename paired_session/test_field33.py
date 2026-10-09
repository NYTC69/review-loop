"""FIELD-33 (poker-news-bot WI-110..113, v2.12.4): every GG pipeline run HELD at the gate after the reviewer's APPROVE,
first on Claude Code's own sandbox scratch path (/tmp/claude-501/...), then on bare model names: the work items and the plans
are ABOUT which model reads which frame ("Claude <= 5 pp, Codex <= 3 pp", "Opus primary_state reads"). FIELD-30 exempted the
operator's work item; the author's plan names the same models and paths. In context/plan.md the scratch root is masked, and a
bare tool name passes when every use of it is a prose word (not part of a path or name) and none is an approval attribution
such as "Codex/GPT approved."; narratives, verdict words, ledger ids and attributions stay caught."""
import unittest

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_field30 as f30

GG_PLAN = ('# Plan\n1. Keep the per-model gate: Claude <= 5 pp, Codex <= 3 pp.\n2. Opus primary_state reads the frame first.\n'
           '3. Write scratch frames to /tmp/claude-501/gg-run/frames/ (and /private/tmp/claude-501/gg-run/ on macOS).\n'
           '4. Codex reads rejected frames; Claude reads accepted frames.\n'   # an approval word that is not an attribution
           '5. Keep the Opus-only path.\n6. Switch the reader to claude-opus-5-5.\n'   # FIELD-33 gate: compounds and model ids
           '7. Edit `gg/readers/opus.py` so Opus reads primary_state.\n'          # a file path naming the model, plus prose
           '8. Codex is primary; the prompt lives in `prompts/codex.md`.\n')


class PlanToolNameTests(unittest.TestCase):
    setUp, coordinator = f30.OperatorWorkItemTests.setUp, f30.OperatorWorkItemTests.coordinator

    def plan(self, text):
        co = self.coordinator(f30.POKER_TOOLS)
        co.state.update(phase='EXEC', next='reviewer')
        (co.context / 'plan.md').write_text(text)
        co.materialize_review_context()
        return co

    def test_the_gg_plan_passes_the_shadow_the_gate_and_plan_approval(self):
        use_lifecycle_on(self, self.h)
        co = self.plan(GG_PLAN)
        for role in ('shadow', 'gate'):
            co.assert_fresh_prompt(role, 'Review the delta.')
        self.assertIsNone(co._plan_history_issue())

    def test_review_history_in_the_plan_still_holds(self):
        use_lifecycle_on(self, self.h)
        for text, marker in (('# Plan\n1. Keep what the Codex reviewer approved.\n', 'reviewer approved'),
                             ('# Plan\n1. Claude/Sonnet signed off on the thresholds.\n', 'Claude'),
                             ('# Plan\n1. Codex approved the frame order.\n', 'Codex'),
                             ('# Plan\n1. Read /opt/claude-tools/frames.sh first.\n', 'claude'),
                             ('# Plan\n1. Address the previous review.\n', 'previous review')):
            with self.subTest(marker=marker):
                co = self.plan(text)
                for role in ('shadow', 'gate'):
                    with self.assertRaisesRegex(RuntimeError, f'{role} independence check rejected history in context/plan.md: {marker}'):
                        co.assert_fresh_prompt(role, 'Review the delta.')

    def test_a_tool_named_path_under_the_scratch_root_is_still_caught(self):   # FIELD-33 gate
        use_lifecycle_on(self, self.h)
        co = self.plan('# Plan\n1. Read /tmp/claude-501/codex-review/notes.md first.\n')
        for role in ('shadow', 'gate'):
            with self.assertRaisesRegex(RuntimeError, f'{role} independence check rejected history in context/plan.md: codex'):
                co.assert_fresh_prompt(role, 'Review the delta.')

    def test_ledger_ids_in_the_plan_still_hold(self):   # FIELD-11
        use_lifecycle_on(self, self.h)
        co = self.plan(GG_PLAN + '4. Keep the F008 and F009 fixes.\n')
        with self.assertRaisesRegex(RuntimeError, 'gate independence check rejected ledger ids in context/plan.md'):
            co.assert_fresh_prompt('gate', 'Review the delta.')
        self.assertIn('ledger-id-shaped tokens F008, F009', co._plan_history_issue())


if __name__ == '__main__':
    unittest.main()
