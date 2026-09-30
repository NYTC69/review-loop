import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import hook_inventory_scan as scan
from paired_session.hook_inventory_git import HookInventoryError


class HookInventoryScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'XDG_CONFIG_HOME': str(self.home / 'xdg')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        template = self.root / 'empty-template'
        template.mkdir()
        subprocess.run(['git', 'init', '-q', '--template=' + str(template), str(self.workspace)], check=True)
        self.hooks = self.workspace / '.git/hooks'
        self.hooks.mkdir()

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.workspace), *args], check=True)

    def test_hook_content_and_effective_config_revalidate(self):
        hook = self.hooks / 'pre-commit'
        hook.write_text('#!/bin/sh\nexit 0\n')
        hook.chmod(0o755)
        frozen = scan.inventory(self.workspace)
        self.assertEqual(scan.require_unchanged(self.workspace, frozen), frozen)
        hook.write_text('#!/bin/sh\nexit 1\n')
        with self.assertRaisesRegex(HookInventoryError, 'changed'):
            scan.require_unchanged(self.workspace, frozen)
        hook.write_text('#!/bin/sh\nexit 0\n')
        self.git('config', 'core.hooksPath', 'elsewhere')
        with self.assertRaisesRegex(HookInventoryError, 'changed'):
            scan.require_unchanged(self.workspace, frozen)

    def test_symlink_and_unsupported_active_hook_fail_closed(self):
        (self.hooks / 'post-commit').write_text('#!/bin/sh\nexit 0\n')
        (self.hooks / 'post-commit').chmod(0o755)
        with self.assertRaisesRegex(HookInventoryError, 'unsupported active'):
            scan.inventory(self.workspace)
        (self.hooks / 'post-commit').unlink()
        (self.hooks / 'pre-commit').symlink_to(self.root / 'outside')
        with self.assertRaisesRegex(HookInventoryError, 'non-file hook'):
            scan.inventory(self.workspace)

    def test_fsmonitor_config_is_recorded_without_execution(self):
        marker = self.root / 'fsmonitor-ran'
        monitor = self.root / 'monitor.sh'
        monitor.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n')
        monitor.chmod(0o755)
        self.git('config', 'core.fsmonitor', str(monitor))
        frozen = scan.inventory(self.workspace)
        self.assertFalse(marker.exists())
        self.git('config', 'core.fsmonitor', 'false')
        with self.assertRaisesRegex(HookInventoryError, 'changed'):
            scan.require_unchanged(self.workspace, frozen)

    def test_config_defined_hook_fails_closed(self):
        self.git('config', 'hook.custom.command', '/usr/bin/true')
        with self.assertRaisesRegex(HookInventoryError, 'config-defined'):
            scan.inventory(self.workspace)

    def test_config_value_with_hook_text_is_not_a_hook_key(self):
        self.git('config', 'remote.origin.url', 'https://example.test/webhook.git')
        self.assertEqual(scan.inventory(self.workspace)['entries'], [])

    def test_dev_null_hooks_path_is_empty(self):
        self.git('config', 'core.hooksPath', '/dev/null')
        result = scan.inventory(self.workspace)
        self.assertEqual(result['hooks_dir'], '/dev/null')
        self.assertEqual(result['entries'], [])

    def test_custom_hooks_path_is_scanned(self):
        custom = self.workspace / 'custom'
        custom.mkdir()
        hook = custom / 'post-commit'
        hook.write_text('#!/bin/sh\nexit 0\n')
        hook.chmod(0o755)
        self.git('config', 'core.hooksPath', 'custom')
        with self.assertRaisesRegex(HookInventoryError, 'unsupported active'):
            scan.inventory(self.workspace)
        hook.unlink()
        self.assertEqual(Path(scan.inventory(self.workspace)['hooks_dir']).resolve(), custom.resolve())
