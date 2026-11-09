"""V325-NOTE (gg-pipeline WI-272b, v3.2.4): `note --scope-change --note "..."` was refused with "scope chain limit or note
arguments invalid", which named neither cause nor flag (`--note` belongs to attach-verification; the note text comes from
--text or --file). The refusal is now split: the chain limit and the text each have their own message, and a `--note`
without --text/--file is told to use --text. `--note` is not an alias."""
import contextlib
import io
import json
import types
import unittest
from unittest.mock import patch

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_real_coordinator as trc
from paired_session import test_worktree_lifecycle as twl

rc = trc.rc
TEXT = 'REFUSED: the scope-change note needs exactly one of --text "<note>" or --file <path>'
HINT = '; --note is for attach-verification, so use --text'
CHAIN = ('REFUSED: this run is already a scope-change successor, so it cannot start another; '
         'abort and start a new run')


class ScopeChangeNoteRefusalTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        use_lifecycle_on(self, self.h)
        argv = ['run', '--workspace', str(self.h.workspace), '--workitem', str(self.h.workitem),
                '--run-dir', str(self.h.run_dir), '--codex-bin', str(self.h.fake_codex_cli()),
                '--claude-bin', str(self.h.fake_claude_cli())]
        rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))   # an idle ACTIVE run
        self.source = self.h.root / 'scope-note.txt'
        self.source.write_text('Narrow the scope.\n')

    def main(self, action, *extra):
        command = self.h.command(*extra)
        command[2] = action
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rc.main(command[2:])
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue())

    def state(self):
        return json.loads((self.h.run_dir / 'state.json').read_text())

    def assert_refused(self, result, line):
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(result.stdout.strip().splitlines()[-1], line)
        self.assertEqual(self.state()['status'], 'ACTIVE')
        self.assertNotIn('scope_change_intent', self.state())

    def test_note_flag_is_named_as_attach_verification_and_not_taken_as_text(self):
        self.assert_refused(self.main('note', '--scope-change', '--note', 'Narrow the scope.'), TEXT + HINT)

    def test_neither_or_both_text_sources_get_the_text_refusal(self):
        for flags in ((), ('--text', 'Narrow the scope.', '--file', str(self.source))):
            with self.subTest(flags=flags):
                self.assert_refused(self.main('note', '--scope-change', *flags), TEXT)

    def test_a_successor_run_gets_the_chain_limit_refusal(self):
        state = self.state()
        state['scope_chain_depth'] = 1
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        self.assert_refused(self.main('note', '--scope-change', '--text', 'Narrow the scope.'), CHAIN)

    def test_text_still_starts_the_successor(self):
        result = self.main('note', '--scope-change', '--text', 'Narrow the scope.')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('Start:', result.stdout)
        self.assertEqual((self.state()['status'], self.state()['abort_kind']), ('ABORTED', 'scope-change'))

    def test_help_names_note_and_reject_with_scope_change(self):
        text = ' '.join(rc.parser().format_help().split())
        self.assertIn('operator note text for note or reject, including --scope-change', text)
        self.assertIn('the note text comes from --text or --file', text)

class QueuedNoteTests(unittest.TestCase):
    """v3.7.2 (field: a note was refused while the run waited for FINISH, and at the EXEC round-limit and POLISH-Q HOLDs):
    a note is queued for the next newly dispatched author-role turn and `note` says which turn that is."""
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in twl._HELPERS})
    setUp = twl.WorktreeLifecycleActivationTests.setUp

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def note(self, text):
        return self.run_operator_action('note', '--lifecycle-mode', 'on', '--text', text)

    def assert_delivered_to(self, phase, text):
        state = self.state()
        [row] = state['operator_notes']
        self.assertEqual(row['status'], 'delivered', row)
        turn = next(t for t in state['turns'] if t['sequence'] == row['delivered_sequence'])
        self.assertEqual((turn['role'], turn['phase'], turn.get('operator_note_id')), ('author', phase, 'N001'))
        prompt = (self.run_dir / 'evidence' / f"{turn['sequence']:03d}-{phase.lower()}-author.prompt.txt").read_text()
        self.assertIn('Operator in-scope clarification [N001]', prompt)
        self.assertIn(text, prompt)
        self.assertNotEqual(state.get('pending_author_result_sequence'), turn['sequence'])   # the writer keeps its stage flow
        self.assertNotIn('pending_operator_note_id', state)

    def test_a_note_at_the_finish_hold_reaches_the_finisher(self):
        held = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_FINISH_HOLD': '1'})
        self.assertIn('HOLD: finisher: Fake finisher held.', held.stdout, held.stdout + held.stderr)
        self.assertEqual(self.state()['next'], 'finish')
        noted = self.note('Keep the finish summary short.')
        self.assertEqual(noted.returncode, 0, noted.stdout + noted.stderr)
        self.assertIn('NOTE: N001 queued; it will be delivered to the FINISH writer', noted.stdout)
        self.assertEqual(self.state()['operator_notes'][0]['status'], 'pending')
        done = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        self.assert_delivered_to('FINISH', 'Keep the finish summary short.')

    def test_a_note_at_a_docs_writer_hold_reaches_the_next_docs_writer(self):
        held = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_HOLD': '1'})
        self.assertIn('HOLD: docs writer: Fake docs writer held.', held.stdout, held.stdout + held.stderr)
        self.assertEqual(self.state()['next'], 'docs')
        noted = self.note('Name the new flag in the changelog.')
        self.assertIn('NOTE: N001 queued; it will be delivered to the DOCS writer', noted.stdout, noted.stdout + noted.stderr)
        done = self.run_operator_action('resume', '--lifecycle-mode', 'on', env={'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'})
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        self.assert_delivered_to('DOCS', 'Name the new flag in the changelog.')

    def test_a_note_at_a_docs_review_hold_waits_for_a_newly_dispatched_author_turn(self):
        env = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_REVIEW_NO_TEST_ONCE': str(self.root / 'no-test')}
        held = self.run_coordinator('--lifecycle-mode', 'on', env=env)
        self.assertIn('HOLD: DOCS reviewer did not observe a successful run', held.stdout, held.stdout + held.stderr)
        self.assertEqual(self.state()['next'], 'docs')
        noted = self.note('Mention the migration.')
        self.assertIn('if one is newly dispatched (a recorded turn reused on resume takes no note)', noted.stdout,
                      noted.stdout + noted.stderr)
        done = self.run_operator_action('resume', '--lifecycle-mode', 'on', env=env)
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        writers = [t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'DOCS']
        self.assertEqual(len(writers), 1)                                           # the recorded writer was reused
        self.assertEqual((state['operator_notes'][0]['status'], state['pending_operator_note_id']), ('pending', 'N001'))
        self.assertFalse(any(t.get('operator_note_id') for t in state['turns']))   # no turn received it

    def test_a_note_at_the_exec_round_limit_hold_is_queued_and_delivered_after_add_rounds(self):
        held = self.run_coordinator('--lifecycle-mode', 'on', '--max-exec-rounds', '1', env={'FAKE_FINISH_WRITE': '1'})
        self.assertIn('HOLD: EXEC round limit reached after a FINISH write', held.stdout, held.stdout + held.stderr)
        noted = self.run_operator_action('note', '--lifecycle-mode', 'on', '--max-exec-rounds', '1', '--text', 'Prefer the smaller fix.')
        self.assertEqual(noted.returncode, 0, noted.stdout + noted.stderr)
        self.assertIn('after resume --add-rounds N at this EXEC round-limit HOLD', noted.stdout)
        done = self.run_operator_action('resume', '--lifecycle-mode', 'on', '--max-exec-rounds', '1', '--add-rounds', '1')
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        self.assert_delivered_to('FINISH', 'Prefer the smaller fix.')   # the reviewer and gate approved, so the finisher got it

    def test_a_polish_q_wait_queues_the_note_and_a_writer_turn_receives_it(self):
        co = self.coordinator('--lifecycle-mode', 'on')
        co.state.update(phase='EXEC', next='polish-simplify')
        co.hold('POLISH-Q simplifier held')
        self.assertEqual(co.note('Keep the helper name.', None), 'N001')
        row = co.state['operator_notes'][0]
        self.assertEqual((row['target_phase'], row['any_author_turn']), ('EXEC', True))
        self.assertTrue(row['deliver_to'].startswith('the POLISH-Q simplifier'))

        class Captured(Exception):
            pass
        co.state.update(status='ACTIVE', hold_reason='')
        for role, phase, fresh in (('shadow', 'POLISH-Q', True), ('author', 'POLISH-Q', True)):
            with patch.object(co, 'assert_fresh_prompt', side_effect=Captured) as checked, \
                    patch.object(co, '_program_state', return_value=(None, None)), self.assertRaises(Captured):
                co._invoke_once(role, phase, 'Simplify.', {}, fresh=fresh)
            self.assertEqual('Operator in-scope clarification [N001]' in checked.call_args.args[1], role == 'author')


if __name__ == '__main__':
    unittest.main()
