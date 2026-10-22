"""V325-NOTE (gg-pipeline WI-272b, v3.2.4): `note --scope-change --note "..."` was refused with "scope chain limit or note
arguments invalid", which named neither cause nor flag (`--note` belongs to attach-verification; the note text comes from
--text or --file). The refusal is now split: the chain limit and the text each have their own message, and a `--note`
without --text/--file is told to use --text. `--note` is not an alias."""
import contextlib
import io
import json
import types
import unittest

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_real_coordinator as trc

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


if __name__ == '__main__':
    unittest.main()
