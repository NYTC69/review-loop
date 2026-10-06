"""HYGIENE-1 (BACKLOG triage 2026-10-06): a warning when Codex roles run with CODEX_HOME unset (the 2026-10-06 P0 cause: the
default ~/.codex), a Claude CLI alias refused at start instead of a HOLD after a wasted turn, and project_root_markers in
the Codex capability guard."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from paired_session import codex_capability_guard as guard
from paired_session import test_real_coordinator as trc

rc = trc.rc
WARNING = 'WARNING: CODEX_HOME is unset, so Codex uses the default ~/.codex'
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli', 'fake_claude_cli',
           'command', 'run_coordinator')


class HygieneTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def probe(self, *extra, unset=False):
        command = self.command(*extra)
        command[2] = 'permission-probe'
        env = {key: value for key, value in os.environ.items() if not (unset and key == 'CODEX_HOME')}
        return subprocess.run(command, cwd=self.root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_codex_roles_with_codex_home_unset_print_one_warning(self):
        unset = self.probe(unset=True)   # the coordinator falls back to ~/.codex (the harness HOME's, which exists)
        self.assertEqual(unset.stdout.count(WARNING), 1, unset.stdout + unset.stderr)
        self.assertIn('use an isolated absolute CODEX_HOME per run', unset.stdout)
        self.run_dir = self.root / 'set-run'
        explicit = self.probe()   # an explicit CODEX_HOME (the harness sets one) is the operator's choice
        self.assertNotIn('WARNING: CODEX_HOME', explicit.stdout)

    def test_an_all_claude_run_prints_no_warning(self):
        command = self.command('--author-vendor', 'claude', '--reviewer-vendor', 'claude', '--gate-vendor', 'claude',
                               '--accept-unverified-claude-author', '--reason', 'hygiene test', '--skip-probe')
        env = {key: value for key, value in os.environ.items() if key != 'CODEX_HOME'}
        result = subprocess.run(command, cwd=self.root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotIn('WARNING: CODEX_HOME', result.stdout)

    def test_a_claude_cli_alias_is_refused_at_start(self):
        for key in ('--reviewer-model', '--gate-model'):
            for alias in ('opus', 'Sonnet', 'opus[1m]'):
                with self.subTest(key=key, alias=alias):
                    command = self.command('--gate-vendor', 'claude', key, alias)
                    with self.assertRaisesRegex(ValueError, r'is a Claude CLI alias, not a full model id .*for example claude-opus-5-5'):
                        rc.validate_role_models(rc.resolve_role_model_defaults(rc.parser().parse_args(command[2:])))
        result = self.run_coordinator('--reviewer-model', 'opus')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("REFUSED: reviewer_model 'opus' is a Claude CLI alias", result.stdout)
        self.assertFalse((self.run_dir / 'state.json').exists())   # before any state, so no turn is spent
        for model in ('claude-sonnet-5-5', 'house-model'):   # a full id, or any other well-formed id, passes as before
            command = self.command('--reviewer-model', model)
            rc.validate_role_models(rc.resolve_role_model_defaults(rc.parser().parse_args(command[2:])))


class ProjectRootMarkersTests(unittest.TestCase):
    def test_project_root_markers_is_a_regular_key_not_a_capability_issue(self):   # HYGIENE-1 gate: refusing it was a false HOLD
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        home, workspace = root / 'codex-home', root / 'ws'
        home.mkdir()
        (workspace / '.git').mkdir(parents=True)
        (home / 'config.toml').write_text('model = "gpt-6.1-sol"\n')
        clean = guard.inspect(home, workspace, platform='linux')
        (home / 'config.toml').write_text('model = "gpt-6.1-sol"\nproject_root_markers = [".hg"]\n')
        marked = guard.inspect(home, workspace, platform='linux')
        self.assertEqual(marked['issues'], clean['issues'])
        self.assertEqual(marked['status'], clean['status'])
        self.assertFalse([i for i in marked['issues'] if 'project_root_markers' in i])


if __name__ == '__main__':
    unittest.main()
