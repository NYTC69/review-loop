"""V326-SCOPE: scope-change trials scan the work item under its successor context name.

Operator CLI constraints pass; ledger ids and review history in the scope note remain refused.
The shared offline harness installs fake CLIs and guards against real provider calls.
"""
import contextlib
import io
import json
import unittest

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_real_coordinator as trc

rc = trc.rc


class ScopeChangeWorkitemScanTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        use_lifecycle_on(self, self.h)
        self.h.workitem.write_text('# Toy\nCreate sum_ints; reject booleans.\n'
                                   'Constraint: no `claude`/`codex` reader invocations.\n')
        argv = ['run', '--workspace', str(self.h.workspace), '--workitem', str(self.h.workitem),
                '--run-dir', str(self.h.run_dir), '--codex-bin', str(self.h.fake_codex_cli()),
                '--claude-bin', str(self.h.fake_claude_cli()), '--shadow', 'off']
        rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))
        self.assertEqual(self.state()['status'], 'ACTIVE')
        self.assertFalse(self.state().get('active'))

    def state(self):
        return json.loads((self.h.run_dir / 'state.json').read_text())

    def scope_change(self, text):
        command = self.h.command('--scope-change', '--text', text)
        command[2] = 'note'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rc.main(command[2:])
        return code, out.getvalue()

    def test_cli_constraint_allows_scope_change(self):
        code, output = self.scope_change('Narrow the scope to the parser.')
        self.assertEqual(code, 0, output)
        self.assertIn('Start:', output)
        state = self.state()
        self.assertEqual((state['status'], state['abort_kind']), ('ABORTED', 'scope-change'))

    def test_scope_note_review_history_is_still_refused(self):
        for note, marker in (('Address F001 in the parser.', 'F001'),
                             ('Delivered review: narrow the parser.', 'Delivered review:')):
            with self.subTest(note=note):
                code, output = self.scope_change(note)
                self.assertEqual(code, 2, output)
                self.assertIn('REFUSED: fresh-role input scan:', output)
                self.assertIn(marker, output)
                self.assertEqual(self.state()['status'], 'ACTIVE')
                self.assertNotIn('scope_change_intent', self.state())
                self.assertFalse((self.h.run_dir / 'evidence' / 'successor-spec.json').exists())
                if marker == 'F001':   # the hint names the scope note, not the trial's temp file
                    self.assertIn('the scope note contains finding ids: F001', output)
                    self.assertIn('run note --scope-change again', output)
                    self.assertNotIn('Edit the work-item file', output)
                    self.assertNotIn('/workitem.md', output)   # neither the temp path nor context/workitem.md


if __name__ == '__main__':
    unittest.main()
