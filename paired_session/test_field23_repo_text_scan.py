"""FIELD-23 (poker-tools run-02, v2.11.1): the fresh shadow held at EXEC r1 with "shadow independence check rejected
history in context/delta-since-last-review.patch: gate finding". The words came from a comment that already existed in the
repository (`// The gate finding: ...` in TabRoutingTests.swift); the author's diff touched that line, so the old and new
lines both showed it. Rewording the comment did not help: the old line stayed in the patch as a "-" line.

The scan prevents accidental carry-over using base_commit marker text, not positional provenance.
Deliberate evasion and reusing a base marker anywhere in its file are documented residuals. Second part:
`note` is accepted in an EXEC HOLD that waits for the reviewer, so an operator change made there is on record.
"""
import subprocess
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc
from paired_session import test_real_coordinator as trc

SWIFT = 'ios/PokerToolsCoreTests/TabRoutingTests.swift'
BASE_SWIFT = ('import XCTest\n\nfinal class TabRoutingTests: XCTestCase {\n'
              '    // The gate finding: the homepage\'s Note Taker card while Notes holds a page.\n'
              '    func testRootRouteIntoLoadedNotesReusesThePage() {\n        XCTAssertTrue(true)\n    }\n}\n')
AUTHOR_SWIFT = BASE_SWIFT.replace('while Notes holds', 'while Players holds').replace('LoadedNotes', 'LoadedPlayers')
REWORDED_SWIFT = AUTHOR_SWIFT.replace('// The gate finding:', '// Regression case:')
QUOTED_LINE = "// The gate finding: the homepage's Note Taker card while Notes holds a page."


class RepoTextScanTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.ws = self.h.workspace

    def commit(self, files: dict):
        for name, text in files.items():
            (self.ws / name).parent.mkdir(parents=True, exist_ok=True)
            (self.ws / name).write_text(text)
        subprocess.run(['git', 'add', *files], cwd=self.ws, check=True)
        subprocess.run(['git', 'commit', '-qm', 'repo content'], cwd=self.ws, check=True)

    def coordinator(self, files=None):
        self.commit(files or {SWIFT: BASE_SWIFT})
        args = rc.parser().parse_args(['run', '--workspace', str(self.ws), '--workitem', str(self.h.workitem),
                                       '--run-dir', str(self.h.run_dir)])
        co = rc.Coordinator(args)
        co.state.update(phase='EXEC', next='reviewer')
        (co.context / 'plan.md').write_text('# Plan\n1. Route Players like Notes.\n')
        co.capture_review_baseline()   # the last review (PLAN) saw the base tree, as in run-02
        co.save()
        return co

    def write(self, name, text):
        (self.ws / name).parent.mkdir(parents=True, exist_ok=True)
        (self.ws / name).write_text(text)

    def scan(self, co, role='shadow'):
        co.materialize_review_context()
        co.assert_fresh_prompt(role, 'Review the delta.')

    # --- regression: repository wording is not review history ---------------------------------------------------------
    def test_the_author_editing_a_pre_existing_gate_finding_comment_does_not_hold(self):   # the run-02 HOLD at 11:37:52Z
        co = self.coordinator()
        self.write(SWIFT, AUTHOR_SWIFT)
        self.scan(co)
        for name in ('delta.patch', 'delta-since-last-review.patch'):   # the scan did see the words, on both sides
            patch_text = (co.context / name).read_text()
            self.assertIn('-    // The gate finding:', patch_text)
            self.assertIn('+    // The gate finding:', patch_text)
        self.scan(co, 'gate')

    def test_rewording_the_comment_leaves_a_removed_line_that_does_not_hold(self):   # the run-02 HOLD at 11:39:04Z
        co = self.coordinator()
        self.write(SWIFT, AUTHOR_SWIFT)
        co.materialize_review_context()
        co.capture_review_baseline()   # the EXEC reviewer saw the author's version
        self.write(SWIFT, REWORDED_SWIFT)
        self.scan(co)
        self.assertIn('-    // The gate finding:', (co.context / 'delta-since-last-review.patch').read_text())

    def test_a_pre_existing_path_named_like_history_does_not_hold(self):
        co = self.coordinator({SWIFT: BASE_SWIFT, 'docs/prior-review.md': 'Notes on the prior-review format.\n'})
        self.write('docs/prior-review.md', 'Notes on the prior-review format, revised.\n')
        self.scan(co)
        self.assertIn('docs/prior-review.md', (co.context / 'delta.stat').read_text())

    def test_a_plan_quoting_a_whole_repository_line_in_backticks_does_not_hold(self):
        co = self.coordinator({SWIFT: BASE_SWIFT, 'flags.txt': 'F123 legacy flag\n'})
        (co.context / 'plan.md').write_text(f'# Plan\n1. Update `{QUOTED_LINE}` for Players.\n'
                                            '2. Keep the flag:\n```\nF123 legacy flag\n```\n')
        self.write(SWIFT, AUTHOR_SWIFT)
        self.scan(co)
        self.assertIsNone(co._plan_history_issue())   # the PLAN approval check agrees with the gate (FIELD-11)

    # --- negative controls: text the run introduces still holds ------------------------------------------------------
    def assert_holds(self, co, needle, role='shadow'):
        with self.assertRaisesRegex(RuntimeError, rf'{role} independence check rejected (history|ledger ids) in .*{needle}'):
            self.scan(co, role)

    def test_a_ledger_id_in_a_new_line_holds(self):
        co = self.coordinator()
        self.write(SWIFT, AUTHOR_SWIFT.replace('        XCTAssertTrue(true)\n', '        XCTAssertTrue(true)   // F042\n'))
        self.assert_holds(co, 'F042')

    def test_a_review_narrative_in_a_new_line_holds(self):
        co = self.coordinator()
        self.write(SWIFT, AUTHOR_SWIFT + '// Changed after the previous review.\n')
        self.assert_holds(co, 'previous review', 'gate')

    def test_documented_residual_base_marker_is_exempt_anywhere_in_the_same_file(self):
        co = self.coordinator()
        self.write(SWIFT, AUTHOR_SWIFT + '// The GATE FINDING also covers Players.\n')
        self.scan(co)
        self.scan(co, 'gate')

    def test_history_in_a_new_file_holds(self):
        co = self.coordinator()
        self.write('notes.txt', 'prior_findings: none\n')
        self.assert_holds(co, 'prior_findings')

    def test_unquoted_plan_narrative_holds_even_when_base_contains_the_marker(self):
        co = self.coordinator()
        (co.context / 'plan.md').write_text('# Plan\nAddress the gate finding.\n')
        self.assert_holds(co, 'gate finding')
        self.assertIsNotNone(co._plan_history_issue())

    def test_partial_quote_is_exempt_but_new_marker_in_that_quote_holds(self):
        co = self.coordinator()
        (co.context / 'plan.md').write_text('# Plan\nUpdate `GATE FINDING`.\n')
        self.scan(co)
        self.assertIsNone(co._plan_history_issue())
        (co.context / 'plan.md').write_text('# Plan\nUpdate `gate finding after previous review`.\n')
        self.assert_holds(co, 'previous review')
        self.assertIsNotNone(co._plan_history_issue())

    def test_removed_and_context_lines_are_never_scanned(self):
        co = self.coordinator()
        text = 'diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1,2 +1,2 @@\n-F042\n previous review\n+plain text\n'
        for name in ('delta.patch', 'delta-since-last-review.patch'):
            self.assertEqual(co._introduced_history('context/' + name, text), [])

    def test_rename_uses_pre_image_base_file(self):
        stable = ''.join(f'// Stable routing detail {n}\n' for n in range(20))
        co = self.coordinator({SWIFT: BASE_SWIFT + stable})
        renamed = 'ios/renamed.swift'
        subprocess.run(['git', 'mv', SWIFT, renamed], cwd=self.ws, check=True)
        self.write(renamed, AUTHOR_SWIFT + stable)
        self.scan(co)
        self.assertIn('rename from ' + SWIFT, (co.context / 'delta.patch').read_text())

    def test_binary_base_has_no_exemption(self):
        co = self.coordinator({'binary.txt': '\x00gate finding\n'})
        text = 'diff --git a/binary.txt b/binary.txt\n--- a/binary.txt\n+++ b/binary.txt\n@@ -1 +1 @@\n+gate finding\n'
        self.assertIn('gate finding', co._introduced_history('context/delta.patch', text))

    def test_prompt_and_gate_template_never_get_exemptions(self):
        co = self.coordinator()
        for name in ('prompt', 'gate-template'):
            self.assertIn('gate finding', co._introduced_history(name, '`gate finding`'))

    def test_review_only_creation_uses_the_same_quote_rule(self):
        co = self.coordinator()
        base = co.state['base_commit']
        texts = {'work item': '`gate finding`', 'plan': 'Change routing.'}
        self.assertIsNone(co._plan_history_issue(texts, base))
        texts['work item'] = 'Address gate finding.'
        self.assertIsNotNone(co._plan_history_issue(texts, base))

    def test_added_line_starting_with_three_pluses_is_not_a_patch_header(self):
        co = self.coordinator()
        text = 'diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -0,0 +1 @@\n+++ F042\n'
        self.assertIn('F042', co._introduced_history('context/delta.patch', text))

    def test_existing_paths_with_spaces_and_git_quoting_are_exempt(self):
        paths = ['docs/previous review.md', 'docs/prior-review-é.md', 'docs/prior-review-\t.md']
        co = self.coordinator({path: 'notes\n' for path in paths})
        for path in paths:
            self.write(path, 'updated notes\n')
        self.scan(co)
        for name in ('delta.stat', 'status.txt'):
            self.assertIn('review', (co.context / name).read_text())

    def test_new_history_like_path_holds(self):
        co = self.coordinator()
        self.write('docs/previous-review.md', 'notes\n')
        self.assert_holds(co, 'previous-review')

    def test_unclosed_fenced_code_and_longer_closing_fence_exempt_base_markers(self):
        co = self.coordinator()
        for text in ('~~~text\ngate finding\n', '```text\ngate finding\n````\n'):
            (co.context / 'plan.md').write_text(text)
            self.scan(co)
            self.assertIsNone(co._plan_history_issue())

    def test_multiline_matched_text_in_fenced_code_requires_the_same_base_text(self):
        co = self.coordinator({'notes.txt': 'reviewer\nrequested a routing change\n'})
        (co.context / 'plan.md').write_text('```\nreviewer\nrequested a routing change\n```\n')
        self.scan(co)
        self.assertIsNone(co._plan_history_issue())
        (co.context / 'plan.md').write_text('```\nreviewer\n\nrequested a routing change\n```\n')
        self.assert_holds(co, 'reviewer')
        self.assertIsNotNone(co._plan_history_issue())

    def test_new_path_with_an_existing_base_filename_prefix_still_holds(self):
        co = self.coordinator({'docs/previous review.md': 'notes\n'})
        self.write('docs/previous review.md extra', 'notes\n')
        self.assert_holds(co, 'previous review')

    def test_without_a_base_commit_everything_is_scanned_as_before(self):
        co = self.coordinator()
        self.write(SWIFT, AUTHOR_SWIFT)
        co.materialize_review_context()
        co.state['base_commit'] = None
        with self.assertRaisesRegex(RuntimeError, 'rejected history in .*gate finding'):
            co.assert_fresh_prompt('shadow', 'Review the delta.')


class NoteWhileAwaitingReviewerTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def held(self, phase, waiting, exec_rounds=1):
        co = self.h.coordinator()
        co.state.update(phase=phase, next=waiting, exec_rounds=exec_rounds)
        co.hold('shadow independence check rejected history in context/delta-since-last-review.patch: gate finding')
        return co

    def test_an_exec_hold_waiting_for_the_reviewer_records_the_note_for_the_next_author_turn(self):
        co = self.held('EXEC', 'reviewer')
        with patch.object(co, 'invoke') as invoke:
            note_id = co.note('Operator reworded the TabRoutingTests comment.', None)
            invoke.assert_not_called()
        row = co.state['operator_notes'][-1]
        self.assertEqual((note_id, row['status'], row['target_phase'], row['while_next']), ('N001', 'pending', 'EXEC', 'reviewer'))
        self.assertEqual((co.state['status'], co.state['next'], co.state['pending_operator_note_id']), ('HOLD', 'reviewer', 'N001'))
        self.assertIn('Operator note: undelivered N001', (co.run_dir / 'review-comparison.md').read_text())

        class Captured(Exception):
            pass
        co.state.update(status='ACTIVE', next='author')   # the reviewer's REVISE hands EXEC back to the author
        with patch.object(co, 'assert_fresh_prompt', side_effect=Captured) as fresh, \
                patch.object(co, '_program_state', return_value=(None, None)):
            with self.assertRaises(Captured):
                co._invoke_once('author', 'EXEC', 'Fix the routing.', {})
        prompt = fresh.call_args.args[1]
        self.assertIn('Operator in-scope clarification [N001]', prompt)
        self.assertIn('Operator reworded the TabRoutingTests comment.', prompt)
        with patch.object(co, 'assert_fresh_prompt', side_effect=Captured) as fresh, \
                patch.object(co, '_program_state', return_value=(None, None)):
            with self.assertRaises(Captured):
                co._invoke_once('shadow', 'EXEC', 'Review the delta.', {}, fresh=True)
        self.assertNotIn('N001', fresh.call_args.args[1])   # never to a review role

    def test_a_plan_hold_waiting_for_the_reviewer_refuses_and_says_how_to_record_the_change(self):
        co = self.held('PLAN', 'reviewer')
        with self.assertRaisesRegex(ValueError, r'waiting for reviewer in PLAN.*--stop-after-plan.*note --scope-change'):
            co.note('clarify', None)
        self.assertNotIn('operator_notes', co.state)

    def test_other_waiting_roles_refuse_and_say_how_to_record_the_change(self):
        co = self.held('EXEC', 'gate')
        with self.assertRaisesRegex(ValueError, r'waiting for gate; no author turn is due.*note --scope-change'):
            co.note('clarify', None)
        self.assertNotIn('operator_notes', co.state)

    def test_no_author_round_left_refuses_as_before(self):
        co = self.held('EXEC', 'reviewer', exec_rounds=10 ** 6)
        with self.assertRaisesRegex(ValueError, 'next author turn is unavailable'):
            co.note('clarify', None)


if __name__ == '__main__':
    unittest.main()
