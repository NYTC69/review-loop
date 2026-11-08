"""Unit v210-field (v2.10.0): operator friction from poker-tools field runs (ACCEPT-ACTIVE, HELP-GROUP, FIELD-10)."""
import json
import re
import subprocess
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
                                ('uncertain_active', 'PROBE', 'resume --retry-uncertain')):   # a probe turn saved by v3.6.0: resume settles it
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

    def test_a_saved_probe_turn_on_a_done_run_is_settled_by_resume_and_the_run_stays_done(self):   # v3.7.0
        flags = ('--shadow', 'off', '--adversarial-gate', 'off')
        done = self.run_coordinator(*flags)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        live = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        gone = subprocess.Popen(['true'], start_new_session=True)
        gone.wait()
        path = self.run_dir / 'state.json'
        for key in ('active', 'uncertain_active'):
            with self.subTest(key=key):
                state = json.loads(path.read_text())
                state[key] = {'sequence': 7, 'role': 'probe', 'phase': 'PROBE', 'vendor': 'claude', 'pid': live.pid}
                path.write_text(json.dumps(state))
                before = path.read_bytes()
                for extra, text in (((), 'settle it with resume --retry-uncertain'), (('--retry-uncertain',), 'is still alive')):
                    refused = self.run_operator_action('resume', *flags, *extra)
                    self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                    self.assertIn(text, refused.stdout)
                    self.assertEqual(path.read_bytes(), before)                       # refused: nothing recorded, still DONE
                state[key]['pid'] = gone.pid
                path.write_text(json.dumps(state))
                settled = self.run_operator_action('resume', *flags, '--retry-uncertain')
                self.assertEqual(settled.returncode, 0, settled.stdout + settled.stderr)
                self.assertIn('NOTE: the probe turn 7 saved by an earlier release was archived; the run stays DONE', settled.stdout)
                self.assertEqual(settled.stdout.splitlines()[-1], 'DONE (acceptance pending)')
                state = json.loads(path.read_text())
                self.assertEqual((state['status'], state.get('active'), state.get('uncertain_active')), ('DONE', None, None))
                self.assertEqual(state['abandoned_turns'][-1]['sequence'], 7)
        accepted = self.run_operator_action('accept', *flags)                       # settled: accept works
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

if __name__ == '__main__':
    unittest.main()
