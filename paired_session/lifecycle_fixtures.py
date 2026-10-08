"""Opt-in real-worktree fixtures; legacy test helpers keep their defaults."""
import json
import os
import subprocess
import sys

from paired_session import test_real_coordinator as trc
from paired_session import test_worktree_lifecycle as twl


class OnModeFixtures:
    """Mix into unittest.TestCase without inheriting the legacy test methods.

    New runs explicitly select lifecycle on. Operator commands supply run
    identity, saved fake binaries (for CLI harness admission), and operator
    options. Frozen roles and auto_commit are restored from saved config.
    """

    locals().update({name: getattr(trc.RealCoordinatorTests, name)
                     for name in twl._HELPERS if name != 'setUp'})

    def setUp(self):
        twl.WorktreeLifecycleActivationTests.setUp(self)

    def command_on(self, *extra):
        return self.command('--lifecycle-mode', 'on', *extra)

    def coordinator_on(self, *extra):
        argv = self.command_on(*extra)[2:]
        return trc.rc.Coordinator(trc.rc.configure_parser(trc.rc.parser(), argv).parse_args(argv))

    def run_on(self, *extra, env=None, skip_probe=True):
        command = self.command_on(*extra)
        if skip_probe:
            command.append('--skip-probe')
        return self._run_on_command(command, env)

    def _run_on_command(self, command, env=None):
        return subprocess.run(command, cwd=self.root, env={**os.environ, **(env or {})},
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def operator_command_on(self, action, *extra):
        config = json.loads((self.run_dir / 'state.json').read_text())['config']
        return [sys.executable, str(trc.MODULE_PATH), action,
                '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                '--run-dir', str(self.run_dir),
                '--codex-bin', config['codex_bin'], '--claude-bin', config['claude_bin'],
                '--skip-probe', *extra]

    def issue_operator_intent_on(self, action, *extra, env=None):
        return self._run_on_command(self.operator_command_on(action, *extra, '--intent-only'), env)

    def run_operator_on(self, action, *extra, env=None):
        command = self.operator_command_on(action, *extra)
        if action in ('accept', 'reject') and '--scope-change' not in extra:
            issued = self.issue_operator_intent_on(action, *extra, env=env)
            if issued.returncode:
                return issued
            command.extend(['--expect', json.loads(issued.stdout)['digest']])
        return self._run_on_command(command, env)
