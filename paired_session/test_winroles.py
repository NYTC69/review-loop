"""W4a: role settings and refusals on Windows (D1 sandbox off, D2 no --detach); POSIX settings, rules and flag digests pinned."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc, readonly_guard as rg, winplat

ROOT = Path('/nonexistent-wiwin-fixture')


def fixture_coordinator(workspace: Path = ROOT / 'work space' / 'repo', run_dir: Path = ROOT / 'runs' / 'run-1'):
    """A Coordinator with only what the Claude settings, edit rules and read-only flags read (no env-derived secret names)."""
    co = rc.Coordinator.__new__(rc.Coordinator)
    co.workspace, co.run_dir, co.context = workspace, run_dir, run_dir / 'context'
    co._probe_env_names, co._probe_sandbox_commands = [], None
    co.args = SimpleNamespace(reviewer_vendor='claude', reviewer_model='claude-opus-5-5', reviewer_effort='high',
                              gate_vendor='claude', gate_model='claude-opus-5-5', gate_effort='max',
                              claude_bin='claude', codex_bin='codex')
    co.reviewer_commands = lambda: ['python3 -m pytest -q']
    return co


def settings_sha(settings: dict) -> str:
    return hashlib.sha256(json.dumps(settings, separators=(',', ':')).encode()).hexdigest()


class RulePathTests(unittest.TestCase):
    def test_windows_form_is_lower_drive_without_colon_and_forward_slashes(self):
        with patch.object(winplat, 'IS_WINDOWS', True):
            self.assertEqual(winplat.claude_rule_path(r'C:\Users\Some User\work\repo'), 'c/Users/Some User/work/repo')
            self.assertEqual(winplat.claude_rule_path(r'D:\runs\run 1'), 'd/runs/run 1')
            self.assertEqual(winplat.claude_rule_path('C:/Users/x'), 'c/Users/x')
            path = winplat.claude_rule_path(r'C:\Users\Some User\work\repo')
            self.assertEqual(f'Edit(//{path}/**)', 'Edit(//c/Users/Some User/work/repo/**)')

    def test_posix_form_is_the_path_without_its_leading_slash(self):
        with patch.object(winplat, 'IS_WINDOWS', False):
            self.assertEqual(winplat.claude_rule_path(Path('/Users/a b/repo')), 'Users/a b/repo')


class SettingsTests(unittest.TestCase):
    def setUp(self):
        home = patch.object(rc.Path, 'home', return_value=ROOT / 'home')
        home.start()
        self.addCleanup(home.stop)

    def test_windows_settings_turn_the_sandbox_off_and_keep_the_shape(self):
        co = fixture_coordinator()
        for role in ('author', 'reviewer', 'gate', 'shadow'):
            with patch.object(winplat, 'IS_WINDOWS', False):
                posix = co._claude_sandbox_settings(role)
            with patch.object(winplat, 'IS_WINDOWS', True):
                windows = co._claude_sandbox_settings(role)
            self.assertFalse(windows['sandbox']['enabled'])
            self.assertFalse(windows['sandbox']['failIfUnavailable'])
            with patch.object(winplat, 'IS_WINDOWS', True):
                self.assertEqual(windows['permissions'], {'deny': [f'Edit(//{winplat.claude_rule_path(co.run_dir)}/**)']})
            posix['sandbox'].update(enabled=False, failIfUnavailable=False)
            posix['permissions'] = windows['permissions']   # the same on POSIX; on native Windows the drive form differs
            self.assertEqual(windows, posix)   # credentials (env vars read by helpers), filesystem and permissions kept
            self.assertIn({'name': 'ANTHROPIC_API_KEY', 'mode': 'deny'}, windows['sandbox']['credentials']['envVars'])

    def test_metacharacter_check_names_no_windows_separator(self):
        # The check runs on str(workspace): an ordinary Windows path has `\` and `:`, neither of which is refused.
        with patch.object(rc.re, 'search', wraps=rc.re.search) as search:
            fixture_coordinator()._claude_author_edit_rules()
        pattern = search.call_args_list[0].args[0]
        self.assertIsNone(rc.re.search(pattern, r'C:\Users\Some User\work\repo'))
        self.assertIsNotNone(rc.re.search(pattern, r'C:\Users\a(b)\repo'))


@unittest.skipIf(winplat.IS_WINDOWS, 'POSIX pin')
class PosixPinTests(unittest.TestCase):
    """The values below were computed from HEAD 8cc7116 (before W4a) with the same fixture; macOS must not move."""

    def test_settings_rules_and_flag_digests_are_unchanged(self):
        co = fixture_coordinator()
        with patch.object(rc.Path, 'home', return_value=ROOT / 'home'):
            for role in ('author', 'reviewer', 'gate'):
                settings = co._claude_sandbox_settings(role)
                self.assertTrue(settings['sandbox']['enabled'] and settings['sandbox']['failIfUnavailable'])
                self.assertEqual(settings_sha(settings), 'e8970399290271eac97f84761803d385276e5dee6128716c07be94a88a66220f')
            self.assertEqual(co._claude_author_edit_rules(), (
                ['Edit(//nonexistent-wiwin-fixture/work space/repo/**)'],
                ['Edit(//nonexistent-wiwin-fixture/runs/run-1/context/**)',
                 'Edit(//nonexistent-wiwin-fixture/home/.cache/review-loop/probe-pass/**)',
                 'Edit(~/.claude/**)', 'Edit(~/.codex/**)', 'Edit(~/.ssh/**)', 'Edit(~/.aws/**)']))
            self.assertEqual(co.reviewer_flags_digest(), '683d6b9dca40ab85194e2bc3b42e026dfba5605fd7118744528594dea6acf6de')
            self.assertEqual(co.gate_flags_digest(), 'ec716e4f9628a65bc7d174c1eabfbf6443932f9fca99aeae34a7c52ca19d471e')

    def test_no_refusal_on_posix(self):
        for args in (SimpleNamespace(detach=True, action='run', safety_mode='strict', run_dir='/nonexistent'),
                     SimpleNamespace(detach=False, action='permission-probe', safety_mode=None, run_dir='/nonexistent')):
            self.assertIsNone(rc.windows_refusal(args))


@unittest.skipUnless(winplat.IS_WINDOWS, 'native Windows paths')
class WindowsRuleTests(unittest.TestCase):
    def test_author_edit_rules_use_the_documented_windows_form(self):
        home = Path(r'C:\Users\Some User')
        co = fixture_coordinator(home / 'work' / 'repo', home / 'runs' / 'run-1')
        with patch.object(rc.Path, 'home', return_value=home):
            allow, deny = co._claude_author_edit_rules()
            settings = co._claude_sandbox_settings('reviewer')
        self.assertEqual(allow, ['Edit(//c/Users/Some User/work/repo/**)'])
        self.assertEqual(deny[:2], ['Edit(//c/Users/Some User/runs/run-1/context/**)',
                                    'Edit(//c/Users/Some User/.cache/review-loop/probe-pass/**)'])
        self.assertEqual(settings['permissions']['deny'], ['Edit(//c/Users/Some User/runs/run-1/**)'])
        self.assertFalse(settings['sandbox']['enabled'])


class RefusalTests(unittest.TestCase):
    """Each refusal goes through the real main() before any lease, run dir, state write or child process."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='win roles ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.workitem = self.root / 'workitem.md'
        self.workitem.write_bytes(b'# work\n')
        self.run_dir = self.root / 'runs' / 'run-1'
        seam = patch.object(winplat, 'IS_WINDOWS', True)
        seam.start()
        self.addCleanup(seam.stop)

    def main(self, action, *extra):
        out = io.StringIO()
        with patch.object(rc.subprocess, 'Popen') as spawn, patch.object(rc, 'run_lease') as lease, \
                patch.object(rc, 'Coordinator') as coordinator, contextlib.redirect_stdout(out):
            code = rc.main([action, '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                            '--run-dir', str(self.run_dir), *extra])
        for mock in (spawn, lease, coordinator):
            mock.assert_not_called()
        self.assertEqual(code, 2)
        return out.getvalue()

    def test_strict_flag_and_profile_are_refused_before_the_run_dir_exists(self):
        profile = self.root / 'profile.json'
        profile.write_text(json.dumps({'safety_mode': 'strict'}), encoding='utf-8')
        for extra in (('--strict',), ('--config', str(profile))):
            for action in ('run', 'resume', 'reject'):
                out = self.main(action, *extra)
                self.assertIn('REFUSED: strict safety mode (--strict, or safety_mode "strict" in the --config profile) is not '
                              'supported on Windows', out)
                self.assertFalse(self.run_dir.exists())

    def test_a_saved_strict_run_is_refused_and_left_untouched(self):
        self.run_dir.mkdir(parents=True)
        state = self.run_dir / 'state.json'
        state.write_text(json.dumps({'version': rc.STATE_VERSION, 'config': {'safety_mode': 'strict'}}), encoding='utf-8')
        before = state.read_bytes()
        for action in ('resume', 'run', 'reject', 'accept'):
            out = self.main(action)
            self.assertIn('REFUSED: strict safety mode is not supported on Windows: this run was created strict', out)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.run_dir.iterdir()), ['state.json'])
        state.write_text(json.dumps({'version': rc.STATE_VERSION, 'config': {'safety_mode': 'efficient'}}), encoding='utf-8')
        args = SimpleNamespace(detach=False, action='resume', safety_mode=None, run_dir=str(self.run_dir))
        self.assertIsNone(rc.windows_refusal(args))

    def test_permission_probe_is_refused(self):
        for extra in ((), ('--strict',)):
            out = self.main('permission-probe', *extra)
            self.assertIn('REFUSED: permission-probe is not supported on Windows', out)
            self.assertFalse(self.run_dir.exists())

    def test_detach_is_refused_on_every_action_that_accepts_it(self):
        for action in rc.DETACH_ACTIONS:
            out = self.main(action, '--detach')
            self.assertIn('REFUSED: --detach is not supported on Windows; run the command in the foreground', out)
            self.assertFalse(self.run_dir.exists())

    def test_efficient_run_is_not_refused(self):
        args = SimpleNamespace(detach=False, action='run', safety_mode=None, run_dir=str(self.run_dir))
        self.assertIsNone(rc.windows_refusal(args))


class ReadOnlyGuardWindowsTests(unittest.TestCase):
    """NTFS has no POSIX permission bits: on Windows nothing is recorded, compared or chmod-ed; content still is."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ws = Path(temp.name) / 'ws'
        self.ws.mkdir()
        self.keep = Path(temp.name) / 'keep'
        for args in (('init', '-q'), ('config', 'user.email', 'a@example.test'), ('config', 'user.name', 'A'),
                     ('config', 'core.autocrlf', 'false')):
            self.git(*args)
        (self.ws / 'a.txt').write_bytes(b'base\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        seam = patch.object(winplat, 'IS_WINDOWS', True)
        seam.start()
        self.addCleanup(seam.stop)

    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.ws, check=True, capture_output=True)

    def test_mode_is_not_recorded_and_content_is_restored_without_chmod(self):
        recorded = rg.capture(self.ws, self.keep)
        self.assertEqual(recorded['perms'], {})
        os.chmod(self.ws / 'a.txt', 0o600)   # a mode change alone is not a change on Windows
        self.assertFalse(rg.moved(self.ws, recorded))
        self.assertEqual(rg.perm_changes(self.ws, recorded, missing=True), [])
        (self.ws / 'a.txt').write_bytes(b'review edit\n')
        self.assertNotEqual(rg.state(self.ws, self.keep)['tree'], recorded['tree'])
        with patch.object(rg.os, 'chmod') as chmod:
            self.assertIsNone(rg.restore(self.ws, recorded, self.keep))
        worktree = [c for c in chmod.call_args_list if '.git' not in Path(c.args[0]).parts and self.keep not in Path(c.args[0]).parents]
        self.assertEqual(worktree, [])   # shutil.copy2 of the index copies its mode; no worktree file is chmod-ed
        self.assertEqual((self.ws / 'a.txt').read_bytes(), b'base\n')
        self.assertTrue(stat.S_ISREG(os.lstat(self.ws / 'a.txt').st_mode))


if __name__ == '__main__':
    unittest.main()
