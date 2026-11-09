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
from paired_session import test_real_coordinator as trc


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

class ResumeTestCommandTests(unittest.TestCase):
    """v3.7.2 (field: range-build-tool): resume without --test-command uses the saved command; only an explicitly
    different value is refused, and the refusal names the saved and the given value."""
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.argv = self.h.command()[2:]   # --test-command 'python3 -m unittest'
        rc.Coordinator(rc.configure_parser(rc.parser(), self.argv).parse_args(self.argv))   # an idle ACTIVE run

    def resume_argv(self, *test_command):
        at = self.argv.index('--test-command')
        return ['resume', *self.argv[1:at], *self.argv[at + 2:], *test_command]

    def coordinator(self, *test_command):
        argv = self.resume_argv(*test_command)
        return rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))

    def test_resume_without_the_flag_uses_the_saved_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            co = self.coordinator()
        self.assertEqual(co.args.test_command, 'python3 -m unittest')
        self.assertIn("NOTE: using the saved test command 'python3 -m unittest'", out.getvalue())

    def test_an_explicit_equal_value_resumes(self):
        self.assertEqual(self.coordinator('--test-command', 'python3 -m unittest').args.test_command, 'python3 -m unittest')

    def test_main_resume_without_the_flag_skips_the_preflight_of_a_missing_configured_executable(self):
        profile = self.h.root / 'operator.json'   # the configured value names an executable that is not installed
        profile.write_text(json.dumps({'test_command': 'no-such-test-tool-v372 run'}))
        seen = {}

        def execute(args):   # stop after main()'s preflight: the resumed run gets the saved command
            seen['test_command'] = args.test_command
            return 0
        out = io.StringIO()
        with patch.object(rc, '_execute_locked', side_effect=execute), contextlib.redirect_stdout(out):
            code = rc.main(self.resume_argv('--config', str(profile)))
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(seen['test_command'], 'python3 -m unittest')
        self.assertIn("NOTE: using the saved test command 'python3 -m unittest'; the configured value "
                      "'no-such-test-tool-v372 run' is not used on resume", out.getvalue())
        self.assertNotIn('REFUSED', out.getvalue())

    def test_an_explicit_different_value_is_refused_naming_both(self):   # an executable every host has: the preflight passes
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rc.main(self.resume_argv('--test-command', 'python3 -m pytest -q'))
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn("REFUSED: resume configuration differs: test_command (saved: 'python3 -m unittest'; given: 'python3 -m pytest -q'); "
                      'omit --test-command to use the saved value, or start a new run', out.getvalue())
        with self.assertRaisesRegex(ValueError, r"test_command \(saved: 'python3 -m unittest'; given: 'python3 -m pytest -q'\)"):
            self.coordinator('--test-command', 'python3 -m pytest -q')


if __name__ == '__main__':
    unittest.main()
