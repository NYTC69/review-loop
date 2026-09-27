import hashlib
from pathlib import Path
import tempfile
import subprocess
import unittest

from paired_session import codex_capability_guard as guard


class CodexCapabilityGuardTests(unittest.TestCase):
    def test_user_config_toml_key_forms_fail_and_clean_config_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'codex-home'
            home.mkdir()
            workspace = root / 'workspace'
            workspace.mkdir()
            config = home / 'config.toml'
            variants = (
                ('[mcp_servers.evil]\ncommand = "touch"\n', 'MCP servers configured'),
                ('mcp_servers.evil.command = "touch"\n', 'MCP servers configured'),
                ('["mcp_servers".evil]\ncommand = "touch"\n', 'MCP servers configured'),
                ('[ mcp_servers . evil ]\ncommand = "touch"\n', 'MCP servers configured'),
                ('notify = ["touch"]\n', 'notify configured'),
                ('"notify" = ["touch"]\n', 'notify configured'),
                ('profile = "unsafe"\n', 'profile configured'),
                ('"profile" = "unsafe"\n', 'profile configured'),
            )
            for contents, reason in variants:
                with self.subTest(reason=reason, contents=contents):
                    config.write_text(contents)
                    before = hashlib.sha256(config.read_bytes()).hexdigest()
                    result = guard.inspect(home, workspace)
                    self.assertEqual(result['status'], 'FAIL', result)
                    self.assertTrue(any(reason in issue for issue in result['issues']), result)
                    self.assertNotIn(contents.strip(), repr(result))
                    self.assertEqual(hashlib.sha256(config.read_bytes()).hexdigest(), before)
            config.write_text('model = "gpt-6-luna"\n')
            self.assertEqual(guard.inspect(home, workspace)['status'], 'PASS')
            (home / 'managed_config.toml').write_text('notify = ["managed"]\n')
            self.assertEqual(guard.inspect(home, workspace)['status'], 'FAIL')
            (home / 'managed_config.toml').unlink()
            (home / 'requirements.toml').write_text('sandbox_mode = "danger-full-access"\n')
            self.assertEqual(guard.inspect(home, workspace)['status'], 'FAIL')

    def test_project_root_config_is_checked_for_nested_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'repo'
            root.mkdir()
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            home = Path(directory) / 'codex-home'
            home.mkdir()
            workspace = root / 'packages' / 'app'
            workspace.mkdir(parents=True)
            project = root / '.codex' / 'config.toml'
            project.parent.mkdir()
            project.write_text('mcp_servers.evil.command = "tool"\n')
            result = guard.inspect(home, workspace)
            self.assertEqual(result['status'], 'FAIL', result)
            self.assertTrue(any('MCP servers configured' in issue for issue in result['issues']))

    def test_project_config_ignores_notify_profile_but_detects_mcp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'codex-home'; home.mkdir()
            workspace = root / 'workspace'; workspace.mkdir()
            project = workspace / '.codex' / 'config.toml'
            project.parent.mkdir()
            project.write_text('notify = ["ignored"]\nprofile = "ignored"\n')
            self.assertEqual(guard.inspect(home, workspace)['status'], 'PASS')
            project.write_text('[mcp_servers.project]\ncommand = "tool"\n')
            result = guard.inspect(home, workspace)
            self.assertEqual(result['status'], 'FAIL', result)
            self.assertTrue(any('MCP servers configured' in issue for issue in result['issues']))


if __name__ == '__main__':
    unittest.main()
