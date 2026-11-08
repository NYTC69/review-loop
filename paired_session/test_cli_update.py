"""CLI-UPD: efficient release updates preserve the frozen-record contract."""
import contextlib
import io
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

    def _assert_other_change_refreezes(self, change):   # v3.7.0: recorded with one NOTE instead of a HOLD that named permission-probe
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
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            current, issue = co._program_state(hold=True)
        self.assertIsNone(issue)
        co.hold.assert_not_called()
        self.assertNotEqual(current, frozen)
        self.assertEqual(co.state['operator_programs'], current)
        self.assertEqual(json.loads(co.state_path.read_text())['operator_programs'], current)
        changed = sorted(k for k in set(frozen) | set(current) if frozen.get(k) != current.get(k))
        self.assertEqual([row['changed'] for row in co.state['program_refreezes']], [changed])
        self.assertEqual(out.getvalue().count('NOTE: operator program or PATH changed since run start (changed: '
                                              + ', '.join(changed) + '); the run continues with the new state'), 1, out.getvalue())
        self.assertNotIn('permission-probe', out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as again:
            self.assertIsNone(co._program_state(hold=True)[1])      # recorded once: no second NOTE
        self.assertEqual(again.getvalue(), '')

    def test_a_missing_program_still_holds(self):
        co, _, _ = self.bound
        Path(co.args.codex_bin).unlink()
        _, issue = co._program_state(hold=True)
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('operator program or PATH changed since run start (changed: codex_bin', issue)
        self.assertNotIn('program_refreezes', co.state)

    def test_downgrade_refreezes_with_a_note(self):
        self._assert_other_change_refreezes('downgrade')

    def test_other_install_refreezes_with_a_note(self):
        self._assert_other_change_refreezes('other_install')

    def test_codex_path_refreezes_with_a_note(self):
        self._assert_other_change_refreezes('codex_path')

    def test_path_env_refreezes_with_a_note(self):
        self._assert_other_change_refreezes('path_env')

    def test_gate_prompt_refreezes_with_a_note(self):
        self._assert_other_change_refreezes('gate_prompt')

    def test_unknown_version_refreezes_with_a_note(self):
        self._assert_other_change_refreezes('unknown_version')

    def test_codex_unknown_refreezes_with_a_note(self):
        self._assert_other_change_refreezes('codex_unknown')

    def test_version_mismatch_refreezes_with_a_note(self):
        self._assert_other_change_refreezes('version_mismatch')

if __name__ == '__main__':
    unittest.main()
