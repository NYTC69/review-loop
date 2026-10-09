"""OFF-R supported gate-only route and independent removed-option contracts."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from paired_session import test_worktree_lifecycle as twl

rc = twl.rc
INTERNAL_OPTIONS = 'PAIRED_SESSION_INTERNAL_TEST_REMOVED_OPTIONS'


class OffUserSurfaceTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name)
                     for name in (*twl._HELPERS, 'setUp')})

    def public_argv(self, action='run', *extra):
        command = self.command('--lifecycle-mode', 'off', *extra)
        command[2] = action
        return command[2:]

    def call_public(self, argv):
        output = io.StringIO()
        with patch.dict(os.environ, {INTERNAL_OPTIONS: '0', 'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '0'}), contextlib.redirect_stdout(output):
            code = rc.main(argv)
        return code, output.getvalue()

    def test_four_removed_flags_are_refused_without_claiming_off_is_removed(self):
        cases = ((('--adversarial-gate', 'off'), 'keep --adversarial-gate on'),
                 (('--override-rejection',), 'use resume --add-rounds N or note --scope-change'),
                 (('--accept-unverified-claude-author',), 'run permission-probe'),
                 (('--accept-probe-skip',), 'run permission-probe'))
        for options, alternative in cases:
            with self.subTest(options=options):
                argv = self.public_argv('run', *options)
                code, output = self.call_public(argv)
                self.assertEqual(code, 2, output)
                self.assertIn('removed user option: ' + ' '.join(options), output)
                self.assertIn(alternative, output)
                self.assertNotIn('lifecycle-off was removed', output)
                self.assertFalse((self.run_dir / 'state.json').exists())
                with patch.dict(os.environ, {INTERNAL_OPTIONS: '0', 'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '0'}):
                    with self.assertRaisesRegex(ValueError, 'removed user option'):
                        rc.Coordinator(rc.parser().parse_args(argv))

    def test_fresh_public_off_reaches_done_after_gate_and_accept_is_uncommitted(self):
        head = rc.subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.workspace)
        code, output = self.call_public(self.public_argv('run', '--skip-probe', '--auto-commit', 'false'))
        self.assertEqual(code, 0, output)
        self.assertIn(twl.DONE, output)
        saved = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(saved['config']['lifecycle_mode'], 'off')
        self.assertNotIn('lifecycle', saved)
        phases = {turn['phase'] for turn in saved['turns']}
        self.assertEqual(saved['turns'][-1]['role'], 'gate')
        self.assertFalse(phases & {'FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'})
        self.assertEqual(saved['status'], 'DONE')
        dirty = rc.subprocess.check_output(['git', 'status', '--porcelain'], cwd=self.workspace)
        self.assertTrue(dirty.strip())
        with patch.dict(os.environ, {INTERNAL_OPTIONS: '0', 'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '0'}):
            accepted = self.run_operator_action('accept', '--auto-commit', 'false')
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn('ACCEPTED', accepted.stdout)
        self.assertEqual(rc.subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.workspace), head)
        self.assertEqual(rc.subprocess.check_output(['git', 'status', '--porcelain'], cwd=self.workspace), dirty)
        self.assertFalse(rc.subprocess.check_output(['git', 'diff', '--cached', '--name-only'], cwd=self.workspace).strip())

    def test_saved_off_resumes_with_no_explicit_mode_and_python_admission_works(self):
        code, output = self.call_public(self.public_argv('run', '--skip-probe', '--stop-after-plan'))
        self.assertEqual(code, 2, output)
        self.assertIn('stopped by --stop-after-plan', output)
        code, output = self.call_public(self.public_argv('note', '--text', 'Keep the change within the approved plan.'))
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['operator_notes'][-1]['status'], 'pending')
        argv = self.public_argv('resume', '--skip-probe')
        index = argv.index('--lifecycle-mode')
        del argv[index:index + 2]
        with patch.dict(os.environ, {INTERNAL_OPTIONS: '0', 'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '0'}):
            co = rc.Coordinator(rc.parser().parse_args(argv))
            self.assertEqual(co.state['config']['lifecycle_mode'], 'off')
        code, output = self.call_public(argv)
        self.assertEqual(code, 0, output)
        self.assertIn(twl.DONE, output)

    def test_saved_off_reject_and_abort_remain_public_actions(self):
        code, output = self.call_public(self.public_argv('run', '--skip-probe'))
        self.assertEqual(code, 0, output)
        with patch.dict(os.environ, {INTERNAL_OPTIONS: '0', 'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '0'}):
            rejected = self.run_operator_action('reject', '--text', 'Also handle negative values.')
        self.assertNotIn('REFUSED', rejected.stdout, rejected.stdout + rejected.stderr)
        saved = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(len(saved['rejections']), 1)
        self.assertEqual(saved['config']['lifecycle_mode'], 'off')
        self.assertNotIn('lifecycle', saved)
        code, output = self.call_public(self.public_argv('abort'))
        self.assertEqual(code, 2, output)
        self.assertIn('HOLD: aborted by operator', output)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['status'], 'HOLD')

    def test_test_only_knob_preserves_python_default_without_waiving_removed_flags(self):
        argv = self.public_argv('run')
        index = argv.index('--lifecycle-mode')
        del argv[index:index + 2]
        with patch.dict(os.environ, {INTERNAL_OPTIONS: '0', 'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '1'}):
            co = rc.Coordinator(rc.parser().parse_args(argv))
            self.assertEqual(co.state['config']['lifecycle_mode'], 'off')
            with self.assertRaisesRegex(ValueError, 'removed user option'):
                rc.Coordinator(rc.parser().parse_args([*argv, '--adversarial-gate', 'off']))

    def test_operator_and_workspace_profiles_can_select_off(self):
        for workspace_profile in (False, True):
            with self.subTest(workspace_profile=workspace_profile):
                self.run_dir = self.root / ('workspace-run' if workspace_profile else 'operator-run')
                profile = (self.workspace / '.review-loop' / 'paired-session.json' if workspace_profile
                           else self.root / 'operator.json')
                profile.parent.mkdir(exist_ok=True)
                profile.write_text(json.dumps({'lifecycle_mode': 'off'}))
                argv = self.public_argv('run', '--config', str(profile), '--skip-probe', '--stop-after-plan')
                index = argv.index('--lifecycle-mode')
                del argv[index:index + 2]
                code, output = self.call_public(argv)
                self.assertEqual(code, 2, output)
                self.assertIn('stopped by --stop-after-plan', output)
                self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['config']['lifecycle_mode'], 'off')

    def test_gate_off_profile_still_names_its_source(self):
        profile = self.root / 'operator.json'
        profile.write_text(json.dumps({'lifecycle_mode': 'off', 'adversarial_gate': 'off'}))
        code, output = self.call_public(self.public_argv('run', '--config', str(profile)))
        self.assertEqual(code, 2, output)
        self.assertIn('profile ' + str(profile) + ': adversarial_gate=off', output)
        self.assertNotIn('lifecycle_mode=off', output)

    def test_python_default_and_review_only_default_use_lifecycle_on(self):
        for review_only in (False, True):
            with self.subTest(review_only=review_only):
                self.run_dir = self.root / ('api-review' if review_only else 'api-main')
                if review_only:
                    (self.workspace / 'tracked.txt').write_text('change to review\n')
                argv = self.public_argv('run', *(['--review-only'] if review_only else []))
                index = argv.index('--lifecycle-mode')
                del argv[index:index + 2]
                with patch.dict(os.environ, {INTERNAL_OPTIONS: '0', 'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '0'}):
                    co = rc.Coordinator(rc.parser().parse_args(argv))
                self.assertEqual(co.state['config']['lifecycle_mode'], 'on')
                self.assertEqual(co.state['lifecycle']['format'], 'worktree')

    def test_public_help_describes_both_modes_and_supported_override(self):
        help_text = rc.parser().format_help()
        self.assertIn('--lifecycle-mode {off,on}', help_text)
        self.assertIn('stop after gate', ' '.join(help_text.split()))
        self.assertIn('--accept-unverified-codex-cli', help_text)
        for flag in ('--accept-unverified-claude-author', '--accept-probe-skip', '--override-rejection', INTERNAL_OPTIONS):
            self.assertNotIn(flag, help_text)

    def test_saved_off_inspection_and_stop_preserve_state_with_off_profiles(self):
        with patch.dict(os.environ, {INTERNAL_OPTIONS: '0', 'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '0'}):
            co = rc.Coordinator(rc.parser().parse_args(self.public_argv()))
        before = co.state_path.read_bytes()
        profile = self.root / 'operator.json'
        profile.write_text(json.dumps({'lifecycle_mode': 'off', 'adversarial_gate': 'off'}))
        for action, extra in (('status', ()), ('status', ('--brief',)), ('stop', ()), ('snapshot', ())):
            with self.subTest(action=action, extra=extra):
                code, output = self.call_public(self.public_argv(action, '--config', str(profile), *extra))
                self.assertEqual(code, 0, output)
                self.assertNotIn('REFUSED', output)
                self.assertEqual(co.state_path.read_bytes(), before)

    def test_status_reads_corrupt_or_non_object_state_without_off_precheck_refusal(self):
        self.run_dir.mkdir()
        for raw in ('not JSON', '[]', 'null', '"text"'):
            with self.subTest(raw=raw):
                (self.run_dir / 'state.json').write_text(raw)
                code, output = self.call_public(self.public_argv('status'))
                self.assertEqual(code, 0, output)
                self.assertEqual(output.strip(), raw)

    def test_codex_cli_override_remains_supported(self):
        # Admission and creation run normally; only provider dispatch is stubbed.
        argv = self.public_argv('run', '--lifecycle-mode', 'on', '--accept-unverified-codex-cli', '--reason', 'owner version ruling', '--skip-probe')
        with patch.object(rc.Coordinator, 'drive', return_value='DONE'):
            code, output = self.call_public(argv)
        self.assertEqual(code, 0, output)
        self.assertNotIn('removed user option', output)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['config']['lifecycle_mode'], 'on')


if __name__ == '__main__':
    unittest.main()
