"""V319-RULE: the long-command maximum leaves the role time to answer, an appended rule never reads as operator text, and
the DOCS reviewer HOLD names a command-tool timeout like the EXEC approval HOLD."""
import hashlib
import os
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc
from paired_session import test_v318cap as cap
from paired_session import test_worktree_lifecycle as twl

rc = trc.rc
TIMEOUT_OUTPUT = 'Exit code 143\nCommand timed out after 10m 0s\n...'


class CommandMaximumTests(unittest.TestCase):
    def test_answer_margin_is_ten_percent_capped_at_ten_minutes_above_the_one_hour_floor(self):
        for turn, expected in ((0, 3600000), (2700, 3600000), (3600, 3600000), (4000, 3600000), (4001, 3601000),
                               (5000, 4500000), (6000, 5400000), (7200, 6600000), (10800, 10200000), (14400, 13800000),
                               (86400, 85800000)):
            with self.subTest(turn=turn):
                self.assertEqual(rc.command_timeout_ms(turn), expected)

    def test_child_environment_uses_the_same_maximum(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rc.cli_env(7200)['BASH_MAX_TIMEOUT_MS'], '6600000')
            self.assertEqual(rc.cli_env(3600)['BASH_MAX_TIMEOUT_MS'], '3600000')
            self.assertEqual(rc.cli_env()['BASH_MAX_TIMEOUT_MS'], '3600000')


class RulePlacementTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in twl._HELPERS})
    prepare = cap.DispatchBudgetTests.prepare

    def dispatched_prompt(self, co, role, phase, prompt):
        """The final prompt _invoke_once builds (the one it scans and records), captured before any CLI starts."""
        class Stop(Exception):
            pass
        seen = []

        def capture(_role, final):
            seen.append(final)
            raise Stop()
        co.state['phase'] = phase
        with patch.object(co, 'assert_fresh_prompt', side_effect=capture), self.assertRaises(Stop):
            co._invoke_once(role, phase, prompt, rc.author_schema() if role == 'author' else rc.review_schema())
        return seen[0]

    def pend_rejection(self, co, text):
        co.state['rejections'] = [{'id': 'R001', 'sha256': hashlib.sha256(text.encode()).hexdigest(), 'text': text,
                                   'status': 'pending'}]
        co.state['pending_rejection_id'] = 'R001'

    def pend_note(self, co, phase, text):
        path = co.evidence / 'operator-note-N001.txt'
        path.write_text(text)
        co.state['operator_notes'] = [{'id': 'N001', 'author': 'operator', 'target_phase': phase, 'status': 'pending',
                                       'sha256': hashlib.sha256(text.encode()).hexdigest(), 'evidence': str(path)}]
        co.state['pending_operator_note_id'] = 'N001'

    def assert_rule_after_operator_text(self, prompt, operator_text, maximum):
        rule = rc.LONG_COMMAND_RULE.replace('gets 3600000', f'gets {maximum}')
        self.assertTrue(prompt.endswith(operator_text + rc.LONG_COMMAND_RULE_HEADING + rule), prompt[-800:])
        self.assertEqual(prompt.count(rc.LONG_COMMAND_RULE_HEADING), 1)

    def test_rule_appended_to_a_rejection_or_clarification_has_its_own_heading(self):
        text = 'Keep the public function name; rename only the helper.'
        for kind in ('rejection', 'clarification'):
            with self.subTest(kind=kind):
                self.run_dir = self.root / kind
                co = self.prepare()   # default --exec-turn-timeout 7200: the EXEC author prompt has no rule of its own
                self.pend_rejection(co, text) if kind == 'rejection' else self.pend_note(co, 'EXEC', text)
                base = co._author_prompt()
                self.assertNotIn(rc.LONG_COMMAND_RULE, base)
                prompt = self.dispatched_prompt(co, 'author', 'EXEC', base)
                self.assertIn('## Operator rejection for current EXEC scope' if kind == 'rejection' else
                              '## Operator in-scope clarification [N001]', prompt)
                self.assert_rule_after_operator_text(prompt, text, 6600000)

    def test_plan_clarification_with_a_long_general_timeout_gets_the_same_heading(self):
        text = 'The CLI flag stays optional.'
        co = self.prepare('--timeout', '7200')
        self.pend_note(co, 'PLAN', text)
        prompt = self.dispatched_prompt(co, 'author', 'PLAN', 'Role: persistent implementer. Phase: PLAN.')
        self.assert_rule_after_operator_text(prompt, text, 6600000)

    def test_without_operator_text_the_appended_rule_is_headed_too(self):
        co = self.prepare()
        base = co._author_prompt()
        prompt = self.dispatched_prompt(co, 'author', 'EXEC', base)
        self.assertEqual(prompt, base + rc.LONG_COMMAND_RULE_HEADING +
                         rc.LONG_COMMAND_RULE.replace('gets 3600000', 'gets 6600000'))

    def test_one_hour_author_turn_with_a_rejection_is_unchanged(self):
        text = 'Keep the public function name.'
        co = self.prepare('--timeout', '3600', '--exec-turn-timeout', '3600')
        self.pend_rejection(co, text)
        base = co._author_prompt()
        prompt = self.dispatched_prompt(co, 'author', 'EXEC', base)
        self.assertEqual(prompt, base + '\n\n## Operator rejection for current EXEC scope\n'
                         'Address this in-scope acceptance feedback; do not expand scope.\n'
                         f"[R001 sha256={hashlib.sha256(text.encode()).hexdigest()}]\n{text}")

    def test_a_prompt_that_carries_the_rule_is_expanded_in_place_without_a_heading(self):
        co = self.prepare('--timeout', '7200')
        base = co._review_prompt('reviewer', 'snapshot')
        prompt = self.dispatched_prompt(co, 'reviewer', 'EXEC', base)
        self.assertEqual(prompt, base.replace('gets 3600000 for a long command.', 'gets 6600000 for a long command.'))


class DocsReviewCauseTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in twl._HELPERS})
    prepare = cap.DispatchBudgetTests.prepare
    PREFIX = 'DOCS reviewer did not observe a successful run of the configured test command'

    def docs_hold(self, row):
        co = self.prepare()
        co.args.test_command = 'python3 -m unittest'
        tree = rc.git_snapshot(self.workspace)[0]
        answer = {'status': 'APPROVE', 'full_review': [], 'observed_commands': [{'command': co.args.test_command, **row}]}

        def invoke(role, phase, prompt, schema, fresh=False):
            co.state['turns'].append({'sequence': 99, 'role': role, 'phase': phase, 'snapshot_before': tree})
            return {'sequence': 99, 'answer': answer}
        with patch.object(co, 'invoke', side_effect=invoke), patch.object(co, 'materialize_review_context'), \
                patch.object(co, '_docs_budget'), self.assertRaises(RuntimeError) as caught:
            co._docs_review_turn(tree, ['docs/guide.md'])
        return str(caught.exception)

    def test_command_tool_timeout_is_named_like_the_exec_approval_hold(self):
        reason = self.docs_hold({'exit_code': -1, 'error': True, 'output': TIMEOUT_OUTPUT,
                                 'source': 'Bash tool_use/tool_result'})
        self.assertEqual(reason, self.PREFIX + " (the command timed out after 10m 0s in the role's command tool)"
                         '; resume reviews again')

    def test_unfinished_and_failed_runs_keep_their_reasons(self):
        self.assertEqual(self.docs_hold({'exit_code': None, 'output': '', 'source': 'item.started'}),
                         self.PREFIX + ' (the configured test was not observed to completion: no exit status when the turn '
                         'ended); resume reviews again')
        self.assertEqual(self.docs_hold({'exit_code': 1, 'output': 'FAILED (failures=1)\n', 'source': 'item.completed'}),
                         self.PREFIX + '; resume reviews again')

    def test_shared_cause_matches_both_holds(self):
        configured = 'python3 -m unittest'
        timed_out = {'command': configured, 'exit_code': -1, 'error': True, 'output': TIMEOUT_OUTPUT,
                     'source': 'Bash tool_use/tool_result'}
        other = {**timed_out, 'command': 'python3 -m pytest'}
        self.assertEqual(rc.configured_test_shortfall([timed_out], configured),
                         (" (the command timed out after 10m 0s in the role's command tool)", False))
        self.assertEqual(rc.configured_test_shortfall([other], configured), ('', False))   # only the configured command counts
        self.assertEqual(rc.configured_test_shortfall([{'command': configured, 'exit_code': None}], configured), ('', True))


if __name__ == '__main__':
    unittest.main()
