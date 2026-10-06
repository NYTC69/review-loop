"""FIELD-30 (poker-tools WI-PLAYERS-CANCEL run-01, v2.12.4): the run passed PLAN r1 and the author's EXEC r1, then HELD when
the fresh shadow launched: "shadow independence check rejected history in original-workitem: Codex". The operator's work
item said where its issue came from ("found by Codex during the blank-Players investigation"). A tool name in the
operator's own work item is operator input, not this run's review history; what the scan still rejects in the work item
is now refused at run creation, before any author or reviewer turn."""
import unittest

from paired_session import coordinator as rc
from paired_session import test_real_coordinator as trc

POKER_TOOLS = ('# WI-PLAYERS-CANCEL\n\nBacklog P1 (added 2026-10-06), found by Codex during the blank-Players investigation.\n'
               'Cancelling the Players sheet leaves the page blank; restore the list.\n')


class OperatorWorkItemTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def coordinator(self, text, *extra):
        self.h.workitem.write_text(text)
        args = rc.parser().parse_args(['run', '--workspace', str(self.h.workspace), '--workitem', str(self.h.workitem),
                                       '--run-dir', str(self.h.run_dir), *extra])
        return rc.Coordinator(args)

    def scan(self, co):
        co.state.update(phase='EXEC', next='reviewer')
        (co.context / 'plan.md').write_text('# Plan\n1. Restore the Players list after a cancel.\n')
        co.materialize_review_context()
        for role in ('shadow', 'gate'):
            co.assert_fresh_prompt(role, 'Review the delta.')

    def test_the_poker_tools_work_item_passes_creation_and_both_fresh_scans(self):
        co = self.coordinator(POKER_TOOLS)
        self.assertIn('found by Codex', (co.context / 'workitem.md').read_text())
        self.scan(co)

    def test_a_tool_named_directory_in_the_work_item_passes(self):
        self.scan(self.coordinator('# Toy\nThe check script lives in /opt/claude-tools/; create sum_ints.\n'))

    def test_a_review_narrative_in_the_plan_is_still_caught(self):   # FIELD-33 (supervisor decision): was the bare-name case
        co = self.coordinator(POKER_TOOLS)
        co.state.update(phase='EXEC', next='reviewer')
        (co.context / 'plan.md').write_text('# Plan\n1. Keep what the Codex reviewer approved.\n')
        with self.assertRaisesRegex(RuntimeError, 'shadow independence check rejected history in context/plan.md: '):
            co.assert_fresh_prompt('shadow', 'Review the delta.')

    def test_the_same_sentence_in_the_plan_now_passes(self):   # FIELD-33: a bare tool name in the plan is not review history
        co = self.coordinator(POKER_TOOLS)
        co.state.update(phase='EXEC', next='reviewer')
        (co.context / 'plan.md').write_text('# Plan\n1. Fix the issue found by Codex.\n')
        co.assert_fresh_prompt('shadow', 'Review the delta.')

    def test_what_still_blocks_in_the_work_item_is_refused_at_creation(self):
        for text, marker in (('# Toy\nCodex approved this approach; create sum_ints.\n', 'Codex'),
                             ('# Toy\nAddress the previous review; create sum_ints.\n', 'previous review'),
                             ('# Toy\nThe reviewer returned REVISE; create sum_ints.\n', 'REVISE')):
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(ValueError, 'independence check rejected history in original-workitem: ' + marker):
                    self.coordinator(text)
                self.assertFalse((self.h.run_dir / 'state.json').exists())   # before any state, so before any turn

    def test_without_a_fresh_role_creation_does_not_scan(self):
        co = self.coordinator('# Toy\nCodex approved this approach; create sum_ints.\n', '--shadow', 'off', '--adversarial-gate', 'off')
        self.assertTrue((co.run_dir / 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
