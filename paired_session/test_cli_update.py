"""CLI-UPD: efficient release updates preserve the frozen-record contract."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from paired_session import coordinator as rc


def executable(path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{version}"\n')
    path.chmod(0o755)
    return path


@pytest.fixture
def bound(tmp_path, monkeypatch):
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
    monkeypatch.setenv('PATH', str(link.parent) + os.pathsep + '/usr/bin:/bin')
    co.args = SimpleNamespace(action='run', safety_mode='efficient', codex_bin=str(codex),
                              claude_bin=str(link), gate_prompt=str(gate), config=None)
    co.state = {'status': 'RUNNING'}
    co.state_path = co.run_dir / 'state.json'
    co._save_lock = threading.Lock()
    co.hold = Mock(side_effect=lambda issue: co.state.update(status='HOLD', hold_reason=issue))
    assert co._program_state()[1] is None
    return co, tmp_path, link


def upgrade(root, link, version='2.1.293', install='install/claude', output=None):
    target = executable(root / install / 'versions' / version, output or f'{version} (Claude Code)')
    link.unlink()
    link.symlink_to(target)


def test_claude_upgrade_records_and_refreezes(bound):
    co, root, link = bound
    before = co.state['operator_programs']['claude_bin'].copy()
    upgrade(root, link)
    Path(before['path']).unlink()  # old release need not remain installed
    current, issue = co._program_state(hold=True)
    assert issue is None
    co.hold.assert_not_called()
    saved = json.loads(co.state_path.read_text())
    assert saved['operator_programs'] == current
    assert saved['program_updates'] == [{'program': 'claude_bin', 'old': before,
        'new': current['claude_bin'], 'old_version': '2.1.281', 'new_version': '2.1.293'}]
    assert set(current['claude_bin']) == {'path', 'sha256'}
    assert co._program_state(hold=True)[1] is None
    assert len(co.state['program_updates']) == 1


def test_codex_in_place_update(bound):
    co, _, _ = bound
    before = co.state['operator_programs']['codex_bin'].copy()
    executable(Path(co.args.codex_bin), 'codex-cli 0.101.0')
    current, issue = co._program_state(hold=True)
    assert issue is None
    co.hold.assert_not_called()
    assert co.state['operator_programs'] == current
    update = co.state['program_updates'][0]
    assert update['old'] == before
    assert update['new']['path'] == before['path']
    assert update['new']['sha256'] != before['sha256']
    assert update['new_version'] == '0.101.0'
    assert update['old_version'] is None  # replaced binary is gone


@pytest.mark.parametrize('change', ['downgrade', 'other_install', 'codex_path', 'path_env',
                                   'gate_prompt', 'unknown_version', 'codex_unknown', 'version_mismatch'])
def test_other_changes_hold_with_efficient_recovery_text(bound, monkeypatch, change):
    co, root, link = bound
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
            monkeypatch.setenv('PATH', os.environ['PATH'] + ':/extra')
        else:
            Path(co.args.gate_prompt).write_text('changed gate')
    _, issue = co._program_state(hold=True)
    assert co.state['status'] == 'HOLD'
    assert co.state['operator_programs'] == frozen
    assert 'program_updates' not in co.state
    assert 'operator program or PATH changed since run start' in issue
    assert 'run `permission-probe` to re-freeze, then resume' in issue
    assert 'since permission probe' not in issue


@pytest.mark.parametrize('program', ['claude', 'codex'])
def test_strict_upgrade_keeps_original_hold(bound, program):
    co, root, link = bound
    co.args.safety_mode = 'strict'
    if program == 'claude':
        upgrade(root, link)
    else:
        executable(Path(co.args.codex_bin), 'codex-cli 0.101.0')
    _, issue = co._program_state(hold=True)
    assert co.state['status'] == 'HOLD'
    assert issue == f'configured operator program or PATH changed since permission probe (changed: {program}_bin)'
    assert 'program_updates' not in co.state


def test_efficient_permission_probe_can_refreeze_an_unrelated_change(bound):
    co, root, link = bound
    upgrade(root, link, install='other/claude')
    assert co._program_state(hold=True)[1] is not None
    co.args.action = 'permission-probe'
    current, issue = co._program_state()
    assert issue is None
    assert co.state['operator_programs'] == current
    co.args.action = 'resume'
    assert co._program_state()[1] is None
