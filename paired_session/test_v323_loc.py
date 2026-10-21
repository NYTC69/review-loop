"""V323-LOC (gg-pipeline WI-263m, v3.2.2): at the last EXEC round the gate held on "rejected history in context/delta.patch:
gate review" with no place to look in a 45 MB patch. The refusal now names each added line (path:line) and the next step;
the check, its prefix and the FIELD-31 author finding are unchanged."""
import unittest

from paired_session import test_real_coordinator as trc

rc = trc.rc
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli', 'fake_claude_cli', 'command')


class DeltaHistoryLocationTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def write(self, name, text):
        (self.workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (self.workspace / name).write_text(text)

    def scan(self, role):
        co = rc.Coordinator(rc.parser().parse_args(self.command()[2:]))
        co.materialize_review_context()
        co.assert_fresh_prompt(role, 'Review the delta.')

    def test_the_refusal_names_each_added_line_and_the_next_step(self):
        self.write('docs/amendment.md', 'Owner rule:\n\n(4) the final gate review checks it.\n')
        self.write('notes.py', 'x = 1\ny = 2  # see F042\n')
        for role in ('shadow', 'gate'):
            with self.subTest(role=role), self.assertRaises(RuntimeError) as caught:
                self.scan(role)
            message = str(caught.exception)
            self.assertTrue(message.startswith(f'{role} independence check rejected history in context/delta.patch: '), message)
            self.assertIn("'gate review' at docs/amendment.md:3", message)
            self.assertIn("'F042' at notes.py:2", message)
            self.assertTrue(message.endswith('; the change adds it: reword those lines in the workspace '
                                             '(no finding ids or review narrative), then resume. The edit changes the '
                                             "reviewed tree, so the run may stop once more at 'stale EXEC approval'; "
                                             'resume again and the reviewer and the gate review the edited tree'), message)

    def test_more_than_five_places_are_counted(self):
        self.write('notes.md', ''.join(f'line {n}: gate review\n' for n in range(1, 8)))
        with self.assertRaises(RuntimeError) as caught:
            self.scan('gate')
        message = str(caught.exception)
        self.assertIn("'gate review' at notes.md:5, +2 more)", message)
        self.assertNotIn('notes.md:6', message)

    def test_other_inputs_keep_the_old_message(self):
        self.write('notes.md', 'clean\n')
        co = rc.Coordinator(rc.parser().parse_args(self.command()[2:]))
        co.materialize_review_context()
        (co.context / 'status.txt').write_text('the gate review said so\n')
        with self.assertRaisesRegex(RuntimeError, r'^gate independence check rejected history in context/status\.txt: '
                                                  r'gate review$'):
            co.assert_fresh_prompt('gate', 'Review the delta.')


if __name__ == '__main__':
    unittest.main()
