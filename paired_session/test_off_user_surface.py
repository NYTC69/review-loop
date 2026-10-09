"""OFF-2 public admission and the shared internal off fixture, with fake providers only."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from paired_session import test_worktree_lifecycle as twl

rc = twl.rc
INTERNAL_OFF = 'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF'


class OffUserSurfaceTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name)
                     for name in (*twl._HELPERS, 'setUp')})

    def public_argv(self, action='run', *extra):
        command = self.command('--lifecycle-mode', 'on', *extra)
        command[2] = action
        return command[2:]

    def call_public(self, argv):
        output = io.StringIO()
        with patch.dict(os.environ, {INTERNAL_OFF: '0'}), contextlib.redirect_stdout(output):
            code = rc.main(argv)
        return code, output.getvalue()

    def test_cli_off_and_each_removed_flag_are_refused_before_state(self):
        cases = ((('--lifecycle-mode', 'off'), 'use --lifecycle-mode on'),
                 (('--adversarial-gate', 'off'), 'keep --adversarial-gate on'),
                 (('--override-rejection',), 'use resume --add-rounds N or note --scope-change'),
                 (('--accept-unverified-claude-author',), 'run permission-probe'),
                 (('--accept-probe-skip',), 'run permission-probe'))
        for options, alternative in cases:
            with self.subTest(options=options):
                code, output = self.call_public(self.public_argv('run', *options))
                self.assertEqual(code, 2, output)
                self.assertIn('removed user option: ' + ' '.join(options), output)
                self.assertIn(alternative, output)
                self.assertFalse((self.run_dir / 'state.json').exists())
                with patch.dict(os.environ, {INTERNAL_OFF: '0'}):
                    with self.assertRaisesRegex(ValueError, 'removed user option'):
                        rc.Coordinator(rc.parser().parse_args(self.public_argv('run', *options)))

    def test_saved_off_continuation_is_refused_but_inspection_and_stop_work(self):
        held = self.run_coordinator('--stop-after-plan')
        self.assertIn('stopped by --stop-after-plan', held.stdout, held.stdout + held.stderr)
        path = self.run_dir / 'state.json'
        before = path.read_bytes()
        for action in ('run', 'resume', 'accept', 'reject', 'permission-probe', 'note', 'abort'):
            with self.subTest(action=action):
                code, output = self.call_public(self.public_argv(action))
                self.assertEqual(code, 2, output)
                self.assertIn(rc.OFF_STATE_REFUSAL, output)
                self.assertIn('~/paired-runs/review-loop-v3.0.4', output)
                with patch.dict(os.environ, {INTERNAL_OFF: '0'}):
                    with self.assertRaisesRegex(ValueError, 'pinned.*v3.0.4'):
                        rc.Coordinator(rc.parser().parse_args(self.public_argv(action)))
                self.assertEqual(path.read_bytes(), before)
        for action, extra in (('status', ()), ('status', ('--brief',)), ('stop', ()), ('snapshot', ())):
            with self.subTest(action=action, extra=extra):
                code, output = self.call_public(self.public_argv(action, *extra))
                self.assertEqual(code, 0, output)
                self.assertNotIn('REFUSED', output)
                self.assertEqual(path.read_bytes(), before)

    def test_saved_off_python_actions_and_successor_parent_are_refused(self):
        co = self.coordinator()
        before = co.state_path.read_bytes()
        with patch.dict(os.environ, {INTERNAL_OFF: '0'}):
            for operation in (co.drive, co.resume, co.accept, lambda: co.reject('feedback', None),
                              lambda: co.operator_intent('accept', None, None)):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(ValueError, 'pinned.*v3.0.4'):
                        operation()
                    self.assertEqual(co.state_path.read_bytes(), before)
            parent = self.run_dir
            self.run_dir = self.root / 'successor'
            argv = self.public_argv('run', '--supersedes', str(parent))
            with self.assertRaisesRegex(ValueError, 'pinned.*v3.0.4'):
                rc.Coordinator(rc.parser().parse_args(argv))
        code, output = self.call_public(argv)
        self.assertEqual(code, 2, output)
        self.assertIn(rc.OFF_STATE_REFUSAL, output)
        self.assertFalse(self.run_dir.exists())

    def test_off_from_an_operator_profile_is_refused(self):
        profile = self.root / 'operator.json'
        profile.write_text(json.dumps({'lifecycle_mode': 'off'}))
        argv = self.command('--config', str(profile))[2:]
        code, output = self.call_public(argv)
        self.assertEqual(code, 2, output)
        self.assertIn('profile ' + str(profile) + ': lifecycle_mode=off', output)
        self.assertIn('use --lifecycle-mode on', output)
        self.assertFalse((self.run_dir / 'state.json').exists())

    def test_off_profiles_preserve_inspection_and_name_the_source_on_refusal(self):
        co = self.coordinator()
        before = co.state_path.read_bytes()
        profiles = (self.root / 'operator.json', self.workspace / '.review-loop' / 'paired-session.json')
        for profile in profiles:
            profile.parent.mkdir(exist_ok=True)
            for key in ('lifecycle_mode', 'adversarial_gate'):
                with self.subTest(profile=profile, key=key):
                    profile.write_text(json.dumps({key: 'off'}))
                    for action in ('status', 'stop', 'snapshot'):
                        argv = self.public_argv(action, '--config', str(profile))
                        index = argv.index('--lifecycle-mode')
                        del argv[index:index + 2]
                        code, output = self.call_public(argv)
                        self.assertEqual(code, 0, output)
                        self.assertNotIn('REFUSED', output)
                        self.assertEqual(co.state_path.read_bytes(), before)
                    # A fresh run reaches profile admission rather than the saved-off gate.
                    argv = self.public_argv('run', '--config', str(profile), '--run-dir', str(self.root / 'new-run'))
                    index = argv.index('--lifecycle-mode')
                    del argv[index:index + 2]
                    code, output = self.call_public(argv)
                    self.assertEqual(code, 2, output)
                    self.assertIn('profile ' + str(profile) + ': ' + key + '=off', output)
                    self.assertFalse((self.root / 'new-run').exists())
            profile.unlink()

    def test_status_reads_corrupt_or_non_object_state_without_off_precheck_refusal(self):
        self.run_dir.mkdir()
        for raw in ('not JSON', '[]', 'null', '"text"'):
            with self.subTest(raw=raw):
                (self.run_dir / 'state.json').write_text(raw)
                code, output = self.call_public(self.public_argv('status'))
                self.assertEqual(code, 0, output)
                self.assertEqual(output.strip(), raw)

    def test_python_default_and_review_only_default_use_lifecycle_on(self):
        for review_only in (False, True):
            with self.subTest(review_only=review_only):
                self.run_dir = self.root / ('api-review' if review_only else 'api-main')
                if review_only: (self.workspace / 'tracked.txt').write_text('change to review\n')
                argv = self.public_argv('run', *(['--review-only'] if review_only else []))
                index = argv.index('--lifecycle-mode')
                del argv[index:index + 2]
                with patch.dict(os.environ, {INTERNAL_OFF: '0'}):
                    co = rc.Coordinator(rc.parser().parse_args(argv))
                self.assertEqual(co.state['config']['lifecycle_mode'], 'on')
                self.assertEqual(co.state['lifecycle']['format'], 'worktree')
                self.assertEqual(co.state['config']['auto_commit'], review_only)

    def test_codex_cli_override_remains_supported(self):
        # Admission and creation run normally; only provider dispatch is stubbed.
        argv = self.public_argv('run', '--accept-unverified-codex-cli', '--reason', 'owner version ruling', '--skip-probe')
        with patch.object(rc.Coordinator, 'drive', return_value='DONE'):
            code, output = self.call_public(argv)
        self.assertEqual(code, 0, output)
        self.assertNotIn('removed user option', output)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['config']['lifecycle_mode'], 'on')

    def test_internal_fixture_runs_off_and_keeps_removed_flag_admission(self):
        self.assertEqual(os.environ.get(INTERNAL_OFF), '1')
        done = self.run_coordinator('--adversarial-gate', 'off')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(twl.DONE, done.stdout)
        saved = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(saved['config']['lifecycle_mode'], 'off')
        self.assertNotIn('lifecycle', saved)
        args = rc.parser().parse_args(self.command('--override-rejection', '--accept-unverified-claude-author',
                                                 '--accept-probe-skip')[2:])
        self.assertTrue(rc.refuse_user_off(args))

    def test_public_help_only_describes_supported_modes_and_overrides(self):
        help_text = rc.parser().format_help()
        self.assertIn('--lifecycle-mode on', help_text)
        self.assertIn('--accept-unverified-codex-cli', help_text)
        for flag in ('--accept-unverified-claude-author', '--accept-probe-skip', '--override-rejection'):
            self.assertNotIn(flag, help_text)
        self.assertNotIn(INTERNAL_OFF, help_text)


if __name__ == '__main__':
    unittest.main()
