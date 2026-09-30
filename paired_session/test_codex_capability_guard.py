import hashlib
import base64
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
                ('[permissions.wide]\nnetwork = true\n', 'permissions configured'),
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

    def test_trusted_git_project_notify_and_default_profile_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'codex-home'; home.mkdir()
            workspace = root / 'workspace'; workspace.mkdir()
            subprocess.run(['git', 'init', '-q'], cwd=workspace, check=True)
            project = workspace / '.codex/config.toml'
            project.parent.mkdir()
            for content in ('notify = ["hook"]\n', 'default_permissions = "wide"\n'):
                project.write_text(content)
                result = guard.inspect(home, workspace, platform='linux')
                self.assertEqual(result['status'], 'FAIL', result)

    def test_plugin_bundled_mcp_fails_closed_without_exposing_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'codex-home'; home.mkdir()
            workspace = root / 'workspace'; workspace.mkdir()
            plugin = home / 'plugins/cache/market/plugin/1.0'; plugin.mkdir(parents=True)
            manifest = plugin / '.codex-plugin/plugin.json'; manifest.parent.mkdir()
            manifest.write_text('{"mcpServers":"./.mcp.json"}')
            mcp = plugin / '.mcp.json'; mcp.write_text('{"mcpServers":{"unsafe":{"command":"node"}}}')
            result = guard.inspect(home, workspace, platform='linux')
            self.assertEqual(result['status'], 'FAIL')
            self.assertTrue(any('plugin MCP or app bundle' in row for row in result['issues']))
            self.assertNotIn('node', repr(result))
            self.assertIn(str(mcp.resolve()), result['sources'])

    def test_macos_mdm_payload_and_unreadable_domain_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'codex-home'; home.mkdir()
            workspace = root / 'workspace'; workspace.mkdir()
            payload = base64.b64encode(b'[mcp_servers.evil]\ncommand="node"\n')
            def mdm_read(argv, *, capture_output, timeout):
                if argv[-1] == 'config_toml_base64':
                    return subprocess.CompletedProcess(argv, 0, payload, b'')
                return subprocess.CompletedProcess(argv, 1, b'', b'does not exist')
            result = guard.inspect(home, workspace, platform='darwin', mdm_run=mdm_read)
            self.assertEqual(result['status'], 'FAIL')
            self.assertTrue(any('macOS MDM Codex policy configured' in row for row in result['issues']))
            self.assertNotIn('node', repr(result))
            def refused(argv, *, capture_output, timeout):
                return subprocess.CompletedProcess(argv, 2, b'', b'permission denied')
            unknown = guard.inspect(home, workspace, platform='darwin', mdm_run=refused)
            self.assertEqual(unknown['status'], 'FAIL')
            self.assertTrue(any('cannot verify macOS MDM' in row for row in unknown['issues']))
            managed = root / 'managed'
            managed.mkdir()
            (managed / 'com.openai.codex.plist').write_bytes(b'opaque MDM payload')
            policy = guard.inspect(home, workspace, platform='darwin', managed_root=managed, mdm_run=mdm_read)
            self.assertTrue(any('managed Codex preferences present' in row for row in policy['issues']))


if __name__ == '__main__':
    unittest.main()
