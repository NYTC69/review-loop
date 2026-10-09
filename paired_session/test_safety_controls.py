"""Failing-test anchors for 1c-safety-controls rows 3c and 21b."""
import json
import unittest
from pathlib import Path

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'coordinator')


class SafetyControlTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def test_provider_hooks_disabled_and_credentials_denied(self):
        use_lifecycle_on(self, self)
        co = self.coordinator('--author-vendor', 'claude', '--reviewer-vendor', 'codex')
        for role in ('author', 'reviewer', 'probe'):
            settings = co._claude_sandbox_settings(role)
            self.assertIs(settings.get('disableAllHooks'), True, role)
            creds = settings['sandbox']['credentials']
            env_names = {row['name'] for row in creds['envVars']}
            self.assertTrue({'ANTHROPIC_API_KEY', 'GITHUB_TOKEN', 'AWS_SECRET_ACCESS_KEY',
                             'SSH_AUTH_SOCK'} <= env_names, role)
            paths = {row['path'] for row in creds['files']}
            self.assertTrue({'~/.aws', '~/.ssh', '~/.netrc', '~/.git-credentials'} <= paths, role)
            self.assertTrue(all(row['mode'] == 'deny' for row in creds['envVars'] + creds['files']))
        schema = Path(self.root) / 'schema.json'
        for role in ('author', 'reviewer'):
            command = co._codex_command(role, schema, False)
            pairs = list(zip(command, command[1:]))
            self.assertIn(('-c', 'features.hooks=false'), pairs, role)

    def test_invocation_cap_stops_run_without_extra_provider_turn(self):
        for cap in (1, 2):
            with self.subTest(cap=cap):
                self.run_dir = self.root / f'cap-run-{cap}'
                result = self.run_coordinator('--max-invocations', str(cap), '--run-dir', str(self.run_dir))
                state = json.loads((self.run_dir / 'state.json').read_text())
                self.assertEqual(state['invocations_used'], cap, result.stdout + result.stderr)
                self.assertEqual(len(state['turns']), cap)
                self.assertEqual(state['sequence'], cap)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('invocation limit reached', result.stdout + result.stderr)
