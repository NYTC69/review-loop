import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from paired_session import author_write_guard as guard


class AuthorWriteGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / 'workspace'
        self.workspace.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'config', 'user.email', 'f3@example.test'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'config', 'user.name', 'F3 Test'], cwd=self.workspace, check=True)
        (self.workspace / '.gitignore').write_text('*.cache\n')
        (self.workspace / 'tracked.txt').write_text('tracked\n')
        subprocess.run(['git', 'add', '.gitignore', 'tracked.txt'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'base'], cwd=self.workspace, check=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_lists_ignored_metadata_without_file_contents(self):
        hidden = self.workspace / 'unreviewed-hook.cache'
        hidden.write_text('secret file content')
        result = guard.snapshot(self.workspace)
        row = result[str(hidden.resolve())]
        self.assertEqual(row, {'size': len('secret file content'),
                               'sha256': hashlib.sha256(b'secret file content').hexdigest()})
        self.assertNotIn('secret file content', repr(result))

    def test_snapshot_includes_git_config_hook_and_info_entries(self):
        config = self.workspace / '.git/config'
        worktree_config = self.workspace / '.git/config.worktree'
        worktree_config.write_text('[core]\nfsmonitor = /tmp/unsafe\n')
        hook = self.workspace / '.git/hooks/pre-commit'
        info = self.workspace / '.git/info/exclude'
        hook.write_text('#!/bin/sh\nexit 0\n')
        info.write_text('private exclude rule\n')
        result = guard.snapshot(self.workspace)
        for path in (config, worktree_config, hook, info):
            self.assertEqual(result[str(path.resolve())]['size'], path.stat().st_size)
            self.assertEqual(result[str(path.resolve())]['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_ignored_nested_repository_directory_is_expanded(self):
        (self.workspace / '.gitignore').write_text('*.cache\n.claude/\n')
        subprocess.run(['git', 'add', '.gitignore'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'ignore nested settings'], cwd=self.workspace, check=True)
        nested = self.workspace / '.claude'
        nested.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=nested, check=True)
        settings = nested / 'settings.local.json'
        settings.write_text('{"hooks":{"preToolUse":[]}}')
        result = guard.snapshot(self.workspace)
        self.assertIn(str((nested / '.git/config').resolve()), result)
        self.assertIn(str(settings.resolve()), result)

    def test_unignored_nested_repository_directory_is_expanded(self):
        nested = self.workspace / 'untracked-settings'
        nested.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=nested, check=True)
        settings = nested / 'settings.local.json'
        settings.write_text('{"commands":["unsafe"]}')
        result = guard.snapshot(self.workspace)
        self.assertIn(str((nested / '.git/config').resolve()), result)
        self.assertIn(str(settings.resolve()), result)

    def test_indexed_submodule_gitlink_fails_closed(self):
        nested = self.workspace / 'subproject'
        nested.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=nested, check=True)
        (nested / 'settings.local.json').write_text('{"commands":["unsafe"]}')
        subprocess.run(['git', '-c', 'user.name=F3', '-c', 'user.email=f3@example.test',
                        'add', 'settings.local.json'], cwd=nested, check=True)
        subprocess.run(['git', '-c', 'user.name=F3', '-c', 'user.email=f3@example.test',
                        'commit', '-qm', 'nested'], cwd=nested, check=True)
        subprocess.run(['git', 'add', '-f', 'subproject'], cwd=self.workspace,
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with self.assertRaisesRegex(RuntimeError, 'submodule gitlink cannot be inventoried'):
            guard.snapshot(self.workspace)

    def test_git_inventory_disables_fsmonitor_and_hooks(self):
        output = type('Result', (), {'returncode': 0, 'stdout': b'ok', 'stderr': b''})()
        with patch.object(guard.subprocess, 'run', return_value=output) as run:
            self.assertEqual(guard._git(self.workspace, 'ls-files'), b'ok')
        command = run.call_args.args[0]
        self.assertIn('core.fsmonitor=false', command)
        self.assertIn('core.hooksPath=/dev/null', command)

    def test_git_warning_with_zero_exit_fails_closed(self):
        output = type('Result', (), {'returncode': 0, 'stdout': b'',
                                     'stderr': b'could not open directory .claude'})()
        with patch.object(guard.subprocess, 'run', return_value=output):
            with self.assertRaisesRegex(RuntimeError, 'metadata inventory Git command failed'):
                guard._git(self.workspace, 'ls-files')

    def test_unreadable_ignored_directory_fails_closed(self):
        (self.workspace / '.gitignore').write_text('private/\n')
        hidden = self.workspace / 'private'
        hidden.mkdir()
        (hidden / 'settings.json').write_text('{}')
        def fail_walk(path, *, onerror, followlinks):
            self.assertFalse(followlinks)
            onerror(PermissionError('cannot enumerate ignored directory'))
            return iter(())
        with patch.object(guard.os, 'walk', side_effect=fail_walk):
            with self.assertRaisesRegex(PermissionError, 'cannot enumerate ignored directory'):
                guard._expand([hidden])


if __name__ == '__main__':
    unittest.main()
