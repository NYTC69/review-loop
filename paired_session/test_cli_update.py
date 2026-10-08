"""CLI-UPD: efficient release updates preserve the frozen-record contract."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from paired_session import coordinator as rc


def executable(path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{version}"\n')
    path.chmod(0o755)
    return path


def upgrade(root, link, version='2.1.293', install='install/claude', output=None):
    target = executable(root / install / 'versions' / version, output or f'{version} (Claude Code)')
    link.unlink()
    link.symlink_to(target)


class CliUpdateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        tmp_path = Path(temp.name)
        co = rc.Coordinator.__new__(rc.Coordinator)
        co.workspace = tmp_path / 'workspace'
        co.run_dir = tmp_path / 'run'
        co.author_temp_dir = co.run_dir / 'author-tmp'
        for path in (co.workspace, co.author_temp_dir):
            path.mkdir(parents=True)
        claude = executable(tmp_path / 'install/claude/versions/2.1.281', '2.1.281 (Claude Code)')
        link = tmp_path / 'bin/claude'
        link.parent.mkdir()
        link.symlink_to(claude)
        codex = executable(tmp_path / 'bin/codex', 'codex-cli 0.100.0')
        gate = tmp_path / 'gate.md'
        gate.write_text('gate')
        env = patch.dict(os.environ, {'PATH': str(link.parent) + os.pathsep + '/usr/bin:/bin'})
        env.start()
        self.addCleanup(env.stop)
        co.args = SimpleNamespace(action='run', safety_mode='efficient', codex_bin=str(codex),
                                  claude_bin=str(link), gate_prompt=str(gate), config=None)
        co.state = {'status': 'RUNNING'}
        co.state_path = co.run_dir / 'state.json'
        co._save_lock = threading.Lock()
        co.hold = Mock(side_effect=lambda issue: co.state.update(status='HOLD', hold_reason=issue))
        self.assertIsNone(co._program_state()[1])
        self.bound = co, tmp_path, link

    def test_claude_upgrade_records_and_refreezes(self):
        co, root, link = self.bound
        before = co.state['operator_programs']['claude_bin'].copy()
        upgrade(root, link)
        Path(before['path']).unlink()  # old release need not remain installed
        current, issue = co._program_state(hold=True)
        self.assertIsNone(issue)
        co.hold.assert_not_called()
        saved = json.loads(co.state_path.read_text())
        self.assertEqual(saved['operator_programs'], current)
        self.assertEqual(saved['program_updates'], [{'program': 'claude_bin', 'old': before,
            'new': current['claude_bin'], 'old_version': '2.1.281', 'new_version': '2.1.293'}])
        self.assertEqual(set(current['claude_bin']), {'path', 'sha256'})
        self.assertIsNone(co._program_state(hold=True)[1])
        self.assertEqual(len(co.state['program_updates']), 1)

    def test_codex_in_place_update(self):
        co, _, _ = self.bound
        before = co.state['operator_programs']['codex_bin'].copy()
        executable(Path(co.args.codex_bin), 'codex-cli 0.101.0')
        current, issue = co._program_state(hold=True)
        self.assertIsNone(issue)
        co.hold.assert_not_called()
        self.assertEqual(co.state['operator_programs'], current)
        update = co.state['program_updates'][0]
        self.assertEqual(update['old'], before)
        self.assertEqual(update['new']['path'], before['path'])
        self.assertNotEqual(update['new']['sha256'], before['sha256'])
        self.assertEqual(update['new_version'], '0.101.0')
        self.assertIsNone(update['old_version'])  # replaced binary is gone

    def _assert_other_change_holds(self, change):
        co, root, link = self.bound
        frozen = co.state['operator_programs'].copy()
        if change == 'downgrade':
            upgrade(root, link, '2.1.280')
        elif change == 'other_install':
            upgrade(root, link, install='other/claude')
        elif change == 'codex_path':
            co.args.codex_bin = str(executable(root / 'other/codex', 'codex-cli 0.101.0'))
        elif change == 'codex_unknown':
            executable(Path(co.args.codex_bin), 'unknown')
        elif change == 'unknown_version':
            upgrade(root, link, output='unknown')
        elif change == 'version_mismatch':
            upgrade(root, link, output='2.1.280 (Claude Code)')
        else:
            upgrade(root, link)
            if change == 'path_env':
                os.environ['PATH'] += ':/extra'
            else:
                Path(co.args.gate_prompt).write_text('changed gate')
        _, issue = co._program_state(hold=True)
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertEqual(co.state['operator_programs'], frozen)
        self.assertNotIn('program_updates', co.state)
        self.assertIn('operator program or PATH changed since run start', issue)
        self.assertIn('run `permission-probe` to re-freeze, then resume', issue)
        self.assertNotIn('since permission probe', issue)

    def _assert_strict_upgrade_holds(self, program):
        co, root, link = self.bound
        co.args.safety_mode = 'strict'
        if program == 'claude':
            upgrade(root, link)
        else:
            executable(Path(co.args.codex_bin), 'codex-cli 0.101.0')
        _, issue = co._program_state(hold=True)
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertEqual(issue, f'configured operator program or PATH changed since permission probe (changed: {program}_bin)')
        self.assertNotIn('program_updates', co.state)

    def test_efficient_permission_probe_can_refreeze_an_unrelated_change(self):
        co, root, link = self.bound
        upgrade(root, link, install='other/claude')
        self.assertIsNotNone(co._program_state(hold=True)[1])
        co.args.action = 'permission-probe'
        current, issue = co._program_state()
        self.assertIsNone(issue)
        self.assertEqual(co.state['operator_programs'], current)
        co.args.action = 'resume'
        self.assertIsNone(co._program_state()[1])

    def test_downgrade_holds_with_efficient_recovery_text(self):
        self._assert_other_change_holds('downgrade')

    def test_other_install_holds_with_efficient_recovery_text(self):
        self._assert_other_change_holds('other_install')

    def test_codex_path_holds_with_efficient_recovery_text(self):
        self._assert_other_change_holds('codex_path')

    def test_path_env_holds_with_efficient_recovery_text(self):
        self._assert_other_change_holds('path_env')

    def test_gate_prompt_holds_with_efficient_recovery_text(self):
        self._assert_other_change_holds('gate_prompt')

    def test_unknown_version_holds_with_efficient_recovery_text(self):
        self._assert_other_change_holds('unknown_version')

    def test_codex_unknown_holds_with_efficient_recovery_text(self):
        self._assert_other_change_holds('codex_unknown')

    def test_version_mismatch_holds_with_efficient_recovery_text(self):
        self._assert_other_change_holds('version_mismatch')

    def test_strict_claude_upgrade_keeps_original_hold(self):
        self._assert_strict_upgrade_holds('claude')

    def test_strict_codex_upgrade_keeps_original_hold(self):
        self._assert_strict_upgrade_holds('codex')


if __name__ == '__main__':
    unittest.main()
