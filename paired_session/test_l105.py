"""L105 (owner 2026-10-07: port): review_focus, review_style and quality_focus from .review-loop/config.md reach the review
roles. The entry maps them to --review-focus / --review-style / --quality-focus; the run freezes them; review_focus and
review_style go to the persistent reviewer, the shadow and the gate, quality_focus and review_style to the POLISH-Q
specialists, never to the author. The text is operator configuration, not review history, for the fresh scan."""
import unittest
from unittest import mock

from paired_session import test_review_report_b1 as b1
from paired_session import test_real_coordinator as trc

rc = trc.rc
FOCUS, STYLE, QUALITY = 'Security first: auth checks; the previous review missed one.', 'Be terse, strict like Codex.', 'strict clippy lints'
GUIDE = ('--review-focus', FOCUS, '--review-style', STYLE, '--quality-focus', QUALITY)
BLOCK = '## Project review guidance (operator settings from .review-loop/config.md)'


class GuidanceTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in b1.HELPERS})

    def coordinator(self, *extra, action='run'):
        command = self.command(*extra)
        command[2] = action
        return rc.Coordinator(rc.parser().parse_args(command[2:]))

    def prompts(self, co):
        snap = rc.git_snapshot(self.workspace)[0]
        co.state.update(phase='PLAN')
        plan = co._review_prompt('reviewer', snap)
        co.state.update(phase='EXEC', next='reviewer')
        (co.context / 'plan.md').write_text('# Plan\n1. Add sum_ints.\n')
        return {'plan reviewer': plan, 'reviewer': co._review_prompt('reviewer', snap), 'shadow': co._review_prompt('shadow', snap),
                'gate': co._gate_prompt(snap), 'author': co._author_prompt()}

    def test_review_roles_carry_focus_and_style_and_the_author_does_not(self):
        co = self.coordinator(*GUIDE)
        self.assertEqual({key: co.state['config'][key] for key in ('review_focus', 'review_style', 'quality_focus')},
                         {'review_focus': FOCUS, 'review_style': STYLE, 'quality_focus': QUALITY})
        for role, prompt in self.prompts(co).items():
            with self.subTest(role=role):
                if role == 'author':
                    self.assertNotIn(BLOCK, prompt)
                    continue
                self.assertIn(BLOCK + f'\nReview focus:\n{FOCUS}\nReview style:\n{STYLE}\n', prompt)
                self.assertNotIn(QUALITY, prompt)                                   # quality_focus is for the specialists

    def test_unset_keys_add_nothing(self):
        co = self.coordinator()
        self.assertEqual([co.state['config'][key] for key in ('review_focus', 'review_style', 'quality_focus')], ['', '', ''])
        for role, prompt in self.prompts(co).items():
            with self.subTest(role=role):
                self.assertNotIn('Project review guidance', prompt)

    def test_the_guidance_is_not_review_history_for_the_fresh_scan(self):
        co = self.coordinator(*GUIDE)
        prompts = self.prompts(co)
        co.materialize_review_context()
        co.assert_fresh_prompt('shadow', prompts['shadow'])                     # "Codex" and "previous review" inside it
        co.assert_fresh_prompt('gate', prompts['gate'])
        with self.assertRaisesRegex(RuntimeError, 'shadow independence check rejected history in prompt: Codex'):
            co.assert_fresh_prompt('shadow', prompts['shadow'] + '\nCodex approved this.')   # other text is still scanned

    def test_overlapping_guidance_texts_stay_exempt(self):   # L105 R1: "security" inside the style text
        co = self.coordinator('--review-focus', 'security', '--review-style', 'Be strict like Codex about security.')
        prompts = self.prompts(co)
        co.materialize_review_context()
        for role in ('shadow', 'gate'):
            co.assert_fresh_prompt(role, prompts[role])

    def test_guidance_naming_a_leak_phrase_passes_the_prompt_checks(self):   # L105 R2: the pre-checks see it masked too
        co = self.coordinator('--review-style', 'Do not include prior_findings; flag F123-style ids.')
        prompts = self.prompts(co)
        co.materialize_review_context()
        for role in ('shadow', 'gate'):
            co.assert_fresh_prompt(role, prompts[role])
        with self.assertRaisesRegex(RuntimeError, 'shadow independence check rejected prompt leak: prior_findings'):
            co.assert_fresh_prompt('shadow', prompts['shadow'] + '\nprior_findings: none')   # outside the guidance: refused

    def test_the_guidance_is_frozen_at_run_start(self):
        self.coordinator(*GUIDE)
        resumed = self.coordinator(action='resume')                               # no flag: the saved text stays
        self.assertEqual(resumed.args.review_focus, FOCUS)
        self.assertEqual(resumed.state['config']['review_style'], STYLE)
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: review_focus'):
            self.coordinator('--review-focus', 'something else', action='resume')


class SpecialistGuidanceTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in b1.HELPERS})
    args, change = b1.AspectAndReportRunTests.args, b1.AspectAndReportRunTests.change

    def test_specialists_carry_quality_focus_and_style(self):
        for extra, expected in ((GUIDE, f'{BLOCK}\nReview style:\n{STYLE}\nQuality focus:\n{QUALITY}\n'), ((), None)):
            with self.subTest(guidance=bool(extra)):
                self.run_dir = self.root / ('run-' + str(len(extra)))
                self.change()
                co = rc.Coordinator(self.args(*extra))
                seen = []
                with mock.patch.object(co, 'invoke', side_effect=lambda role, phase, prompt, *a, **k: seen.append(prompt) or
                                       (_ for _ in ()).throw(RuntimeError('captured'))), self.assertRaises(RuntimeError):
                    co._specialist_turn('code-reviewer', ['sum_ints.py'], rc.git_snapshot(self.workspace)[0])
                if expected:
                    self.assertIn(expected, seen[0])
                    self.assertNotIn(FOCUS, seen[0])                                 # review_focus is for the reviews
                else:
                    self.assertNotIn('Project review guidance', seen[0])


if __name__ == '__main__':
    unittest.main()
