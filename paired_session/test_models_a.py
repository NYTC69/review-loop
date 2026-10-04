"""v2.9.7 models-a (ADR-12): the Codex default model is gpt-6.1-sol and needs codex-cli >= 0.159.2."""
import json
import os
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'coordinator')
REFUSAL = 'REFUSED: the default Codex model gpt-6.1-sol needs codex-cli >= 0.159.2'


class CodexDefaultModelTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def cli(self, action, version, *flags, env=None):
        command = self.command(*flags, '--skip-probe')
        command[2] = action
        return trc.subprocess.run(command, cwd=self.root, env={**os.environ, 'FAKE_CODEX_VERSION': version, **(env or {})},
                                  text=True, capture_output=True)

    def frozen_luna_run(self):
        held = self.cli('run', 'codex-cli 0.160.0', '--stop-after-plan')
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        state_path = self.run_dir / 'state.json'
        state = json.loads(state_path.read_text())
        for key in ('author_model', 'gate_model'):
            state['config'][key] = 'gpt-6-luna'                       # a run frozen before ADR-12
        state_path.write_text(json.dumps(state))
        return state_path

    def test_a_cli_below_the_minimum_is_refused_before_any_state(self):
        for action in ('run', 'permission-probe'):
            with self.subTest(action=action):
                refused = self.cli(action, 'codex-cli 0.159.1')
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertEqual(refused.stdout.strip(), REFUSAL + ' (this CLI: 0.159.1); upgrade the Codex CLI, '
                                 'or pass --author-model gpt-6-luna --gate-model gpt-6-luna')
                self.assertFalse(self.run_dir.exists())

    def test_the_minimum_version_and_explicit_or_claude_models_are_accepted(self):
        cases = (('codex-cli 0.159.2', (), 'gpt-6.1-sol'),
                 ('codex-cli 0.159.1', ('--author-model', 'gpt-6-luna', '--gate-model', 'gpt-6-luna'), 'gpt-6-luna'),
                 ('codex-cli 0.159.1', ('--author-vendor', 'claude', '--reviewer-vendor', 'claude'), 'claude-opus-5-5'))
        for index, (version, flags, author_model) in enumerate(cases):
            with self.subTest(version=version, flags=flags):
                self.run_dir = self.root / f'accepted-{index}'
                started = self.cli('run', version, *flags, '--stop-after-plan',
                                   *(('--accept-unverified-claude-author', '--reason', 'fake') if index == 2 else ()))
                self.assertNotIn('needs codex-cli', started.stdout + started.stderr)
                state = json.loads((self.run_dir / 'state.json').read_text())
                self.assertEqual(state['config']['author_model'], author_model, started.stdout + started.stderr)

    def test_an_unreadable_version_is_refused_when_a_codex_role_takes_the_default(self):
        refused = self.cli('run', 'codex version unknown', '--author-vendor', 'claude', '--reviewer-vendor', 'codex',
                           '--gate-vendor', 'claude')
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('(this CLI: unreadable); upgrade the Codex CLI, or pass --reviewer-model gpt-6-luna', refused.stdout)
        self.assertFalse(self.run_dir.exists())

    def test_a_frozen_run_keeps_its_saved_codex_model(self):
        state_path = self.frozen_luna_run()
        before = len(json.loads(state_path.read_text())['turns'])
        done = self.cli('resume', 'codex-cli 0.159.1', env={'FAKE_CODEX_MODEL': 'gpt-6-luna'})   # no default, no version check
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = json.loads(state_path.read_text())
        self.assertEqual((state['status'], state['config']['author_model']), ('DONE', 'gpt-6-luna'))
        resumed = [turn for turn in state['turns'][before:] if turn['vendor'] == 'codex']
        self.assertTrue(resumed and all(turn['model'] == 'gpt-6-luna' for turn in resumed))

    def test_abort_and_status_of_a_frozen_run_are_never_refused_on_an_old_cli(self):
        state_path = self.frozen_luna_run()
        for action in ('status', 'abort'):
            with self.subTest(action=action):
                result = self.cli(action, 'codex-cli 0.159.1')
                self.assertNotIn('REFUSED', result.stdout + result.stderr)
        self.assertEqual(json.loads(state_path.read_text())['status'], 'HOLD')

    def test_a_workspace_profile_codex_bin_is_refused_and_never_executed(self):
        marker = self.root / 'workspace-codex-ran'
        tool = self.workspace / 'tools' / 'codex'
        tool.parent.mkdir()
        tool.write_text(f'#!/bin/sh\ntouch {marker}\necho "codex-cli 0.160.0"\n')
        tool.chmod(0o755)
        profile = self.workspace / '.review-loop' / 'paired-session.json'
        profile.parent.mkdir()
        profile.write_text(json.dumps({'codex_bin': 'tools/codex'}))   # no model key: the Codex roles take the default
        command = [trc.sys.executable, str(trc.MODULE_PATH), 'run', '--workspace', str(self.workspace),
                   '--workitem', str(self.workitem), '--run-dir', str(self.run_dir), '--claude-bin', str(self.fake_claude_cli())]
        refused = trc.subprocess.run(command, cwd=self.workspace, text=True, capture_output=True)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('workspace profile cannot select operator programs: codex_bin', refused.stdout)
        self.assertFalse(marker.exists())
        self.assertFalse((self.run_dir / 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
