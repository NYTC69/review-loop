"""Unit v210-field (v2.10.0): operator friction from poker-tools field runs (ACCEPT-ACTIVE, HELP-GROUP, FIELD-10)."""
import contextlib
import io
import json
import re
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action', 'coordinator')
AV_FLAGS = ('--command', '--cwd', '--exit-code', '--log', '--log-sha256', '--note')


class V210FieldTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def test_accept_refuses_while_a_turn_is_active_or_uncertain_in_legacy_and_worktree_runs(self):   # ACCEPT-ACTIVE
        self.coordinator().done()
        for key, phase, fix in (('active', 'EXEC', 'resume --retry-uncertain'),
                                ('uncertain_active', 'EXEC', 'resume --retry-uncertain'),
                                ('uncertain_active', 'PROBE', 'permission-probe --retry-uncertain')):   # a probe re-run on DONE that crashed
            with self.subTest(key=key, phase=phase):
                co = self.coordinator()
                co.state[key] = {'sequence': 7, 'role': 'author', 'phase': phase, 'pid': 4242}
                co.save()
                refused = self.run_operator_action('accept')
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertIn('REFUSED: accept refused: CLI turn 7 is active or uncertain; settle it first (' + fix, refused.stdout)
                co = self.coordinator()
                self.assertEqual(co.state['status'], 'DONE')
                with patch.object(rc.worktree_lifecycle, 'is_worktree', return_value=True), \
                        patch.object(co, '_worktree_accept', side_effect=AssertionError('W accept reached')):
                    with self.assertRaisesRegex(ValueError, 'accept refused: CLI turn 7'):
                        co.accept()
                co.state[key] = None
                co.save()
        accepted = self.run_operator_action('accept')                               # settled: accept works again
        self.assertEqual(accepted.stdout.splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)

    def test_help_groups_every_attach_verification_flag_in_one_section(self):     # HELP-GROUP
        text = rc.parser().format_help()
        header = text.index('\nattach-verification:\n')
        section = re.split(r'\n\n(?=\S)', text[header + 1:])[0]                     # up to the next section heading
        for flag in AV_FLAGS:
            with self.subTest(flag=flag):
                listed = [m.start() for m in re.finditer(r'\n  ' + re.escape(flag) + r'(?=[ \n])', text)]
                self.assertEqual(len(listed), 1)                                     # described once, inside the group
                self.assertGreater(listed[0], header)
                self.assertIn('\n  ' + flag, section)

    def test_a_reject_intent_on_done_never_runs_the_author_dispatch_precondition(self):   # FIELD-10
        self.coordinator('--author-vendor', 'claude').done()
        argv = self.command('--author-vendor', 'claude')[2:]
        argv[0] = 'reject'
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), \
                patch.object(rc.Coordinator, 'claude_author_verified', autospec=True,
                             return_value=(False, 'probe reason named')) as verified:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rc.main([*argv, '--text', 'redo it', '--intent-only'])
            self.assertEqual(code, 0, out.getvalue())
            digest = json.loads(out.getvalue())['digest']
            verified.assert_not_called()                                            # the intent preview dispatches nothing
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = rc.main([*argv, '--text', 'redo it', '--expect', digest])
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn('probe reason named', out.getvalue())                        # the dispatching reject keeps the gate
        self.assertEqual(verified.call_count, 1)


if __name__ == '__main__':
    unittest.main()
