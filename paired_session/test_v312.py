"""V312: gate follow-ups - the off route's auto_commit note, refuse_removed_options and first-component model ids.

Bounded normal-use regressions (owner rule: users and models are assumed benign), not a malicious-evasion boundary.
"""
import inspect
import json
import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from paired_session import test_off_user_surface as tou
from paired_session import test_v303a as v303a
from paired_session import test_worktree_lifecycle as twl

rc = twl.rc
PUBLIC = {tou.INTERNAL_OPTIONS: '0', 'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '0'}


class OffNeverCommitsNoteTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name)
                     for name in (*twl._HELPERS, 'setUp')})
    public_argv = tou.OffUserSurfaceTests.public_argv
    call_public = tou.OffUserSurfaceTests.call_public

    def git(self, *args):
        return rc.subprocess.check_output(['git', *args], cwd=self.workspace)

    def run_and_accept(self, auto_commit):
        code, output = self.call_public(self.public_argv('run', '--skip-probe', '--auto-commit', auto_commit))
        self.assertEqual(code, 0, output)
        self.assertIn(twl.DONE, output)
        with patch.dict(os.environ, PUBLIC):
            accepted = self.run_operator_action('accept', '--auto-commit', auto_commit)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED')
        return output, accepted.stdout, (self.run_dir / 'delivery-report.md').read_text()

    def test_off_with_auto_commit_true_notes_it_at_start_and_in_the_report(self):
        head = self.git('rev-parse', 'HEAD')
        output, accepted, report = self.run_and_accept('true')
        self.assertEqual(output.count('NOTE: ' + rc.OFF_NEVER_COMMITS_NOTE), 1)
        self.assertTrue(json.loads((self.run_dir / 'state.json').read_text())['config']['auto_commit'])
        self.assertIn('UNCOMMITTED: no commit was made (lifecycle off never commits); commit these yourself: ', accepted)
        self.assertNotIn('COMMIT:', accepted.replace('UNCOMMITTED:', ''))
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertIn('- 注意：auto_commit 为 true，但 lifecycle off 从不提交，该设置被忽略。', report)

    def test_off_with_auto_commit_false_prints_no_note(self):
        output, accepted, report = self.run_and_accept('false')
        self.assertNotIn(rc.OFF_NEVER_COMMITS_NOTE, output)
        self.assertIn('UNCOMMITTED: no commit was made (auto_commit off)', accepted)
        self.assertNotIn('注意：auto_commit', report)


class RemovedOptionsTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name)
                     for name in (*twl._HELPERS, 'setUp')})
    public_argv = tou.OffUserSurfaceTests.public_argv
    call_public = tou.OffUserSurfaceTests.call_public

    def test_the_renamed_helper_takes_only_args_and_still_refuses(self):
        self.assertFalse(hasattr(rc, 'refuse_user_off'))
        self.assertEqual(list(inspect.signature(rc.refuse_removed_options).parameters), ['args'])
        with patch.dict(os.environ, PUBLIC):
            for dest, value, flag in (('adversarial_gate', 'off', '--adversarial-gate off'),
                                      ('override_rejection', True, '--override-rejection'),
                                      ('accept_unverified_claude_author', True, '--accept-unverified-claude-author'),
                                      ('accept_probe_skip', True, '--accept-probe-skip')):
                with self.subTest(dest=dest):
                    with self.assertRaisesRegex(ValueError, 'removed user option: ' + re.escape(flag)):
                        rc.refuse_removed_options(SimpleNamespace(**{dest: value}))
            self.assertFalse(rc.refuse_removed_options(SimpleNamespace(adversarial_gate='on', lifecycle_mode='off')))
            profile = SimpleNamespace(adversarial_gate='off', off_profile_options={'adversarial_gate': '/ops/p.json'})
            with self.assertRaisesRegex(ValueError, re.escape('profile /ops/p.json: adversarial_gate=off')):
                rc.refuse_removed_options(profile)
            profile.action = 'status'   # inspection keeps working on a profile that sets a removed option
            self.assertFalse(rc.refuse_removed_options(profile))
        with patch.dict(os.environ, {tou.INTERNAL_OPTIONS: '1'}):
            self.assertTrue(rc.refuse_removed_options(SimpleNamespace(adversarial_gate='off')))

    def test_a_saved_run_still_refuses_a_removed_flag(self):
        code, output = self.call_public(self.public_argv('run', '--skip-probe', '--stop-after-plan'))
        self.assertEqual(code, 2, output)
        self.assertIn('stopped by --stop-after-plan', output)
        before = (self.run_dir / 'state.json').read_bytes()
        for flag in (('--override-rejection',), ('--adversarial-gate', 'off')):
            with self.subTest(flag=flag):
                code, output = self.call_public(self.public_argv('resume', '--skip-probe', *flag))
                self.assertEqual(code, 2, output)
                self.assertIn('REFUSED: removed user option: ' + ' '.join(flag), output)
                self.assertEqual((self.run_dir / 'state.json').read_bytes(), before)


class FirstComponentModelIdTests(unittest.TestCase):
    setUp = v303a.FrozenProductNameTests.setUp
    freeze = v303a.FrozenProductNameTests.freeze
    markers = v303a.FrozenProductNameTests.markers

    def test_every_vendor_component_of_a_model_id_is_exempt(self):
        for workitem in ('Implement claude-opus-5-5.', 'Switch the reader to Claude-Opus-5-5 (owner pick).'):
            with self.subTest(workitem=workitem):
                self.freeze(workitem)
                self.assertEqual(self.markers('CLAUDE = "claude"'), [])
                self.assertEqual(self.markers('OPUS = "opus"'), [])
        self.freeze('Implement gpt-6-astra.')
        self.assertEqual(self.markers('ASTRA = "astra"'), [])

    def test_prose_and_path_forms_name_nothing(self):
        for workitem, line, marker in (('A pre-Opus migration.', 'OPUS = "opus"', 'OPUS'),
                                       ('A Claude-based tool.', 'CLAUDE = "claude"', 'CLAUDE'),
                                       ('Install /opt/claude-opus-5-5/bin.', 'CLAUDE = "claude"', 'CLAUDE'),
                                       ('Read claude-opus-5-5.md first.', 'CLAUDE = "claude"', 'CLAUDE'),
                                       ('Use claude-opus-5-5/ as the cache.', 'CLAUDE = "claude"', 'CLAUDE')):
            with self.subTest(workitem=workitem):
                self.freeze(workitem)
                self.assertIn(marker, self.markers(line))

    def test_attribution_and_directories_stay_visible(self):
        self.freeze('Implement claude-opus-5-5.')
        for line, marker in (('Claude approved this', 'Claude'), ('/opt/claude-tools/', 'claude'),
                             ('readers/opus/cache', 'opus'), ('Opus APPROVE', 'APPROVE')):
            with self.subTest(line=line):
                self.assertIn(marker, self.markers(line))


if __name__ == '__main__':
    unittest.main()
