"""FIELD-34 (poker-news-bot WI-115, v2.12.7): the gate HELD with "gate independence check rejected history in
context/plan.md: Codex" on "...with tokens and Claude/Codex pp per video vs D084": FIELD-33's prose test read the slash as a
path ("Claude" before a "/", "Codex" after one). One slash between two plain words is prose; path segments, tool-named
directories and attributions such as "Codex/GPT approved" still HOLD."""
import unittest

from paired_session import test_field33 as f33


class SlashPairTests(unittest.TestCase):
    setUp, coordinator, plan = (f33.PlanToolNameTests.setUp, f33.PlanToolNameTests.coordinator,
                                f33.PlanToolNameTests.plan)

    def test_slash_joined_model_names_in_prose_pass(self):
        for line in ('Report runtime with tokens and Claude/Codex pp per video vs D084.',   # the field case verbatim
                     'Compare Opus/Codex on the same frames.', 'Keep the Claude/GPT split (`Claude/GPT`).',
                     'Per-model (Claude/Codex/Opus) gates.', 'Run the Claude/Codex/GPT comparison.'):   # FIELD-34b
            with self.subTest(line=line):
                co = self.plan(f'# Plan\n1. {line}\n')
                for role in ('shadow', 'gate'):
                    co.assert_fresh_prompt(role, 'Review the delta.')

    def test_paths_and_attributions_still_hold(self):
        for text, marker in (('Read a/codex/b first.', 'codex'), ('Edit gg/readers/opus.py and .codex/agents.', 'codex'),
                             ('Write to /tmp/x/codex/ first.', 'codex'), ('Codex/GPT approved the split.', 'Codex'),
                             ('Claude/Sonnet signed off on it.', 'Claude'),
                             ('Claude/Codex/GPT approved the plan.', 'Claude'), ('Read a/codex/b/opus first.', 'codex')):   # 34b
            with self.subTest(text=text):
                co = self.plan(f'# Plan\n1. {text}\n')
                with self.assertRaisesRegex(RuntimeError, f'gate independence check rejected history in context/plan.md: {marker}'):
                    co.assert_fresh_prompt('gate', 'Review the delta.')


if __name__ == '__main__':
    unittest.main()
