"""P0-2: the exact codex-cli pin became a verified-contract rule with an operator override."""
import contextlib
import io
import json
import os
import types
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
UNVERIFIED = 'codex-cli 0.159.2'
OTHER = 'codex-cli 0.160.0'


class CodexContractTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real-coordinator fixture (fake CLIs, HOME, guards) without collecting its tests.
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def co(self):
        # Same flags as self.h.command() so a CLI call can restore this coordinator's saved run.
        return self.h.coordinator('--timeout', '10', '--author-effort', 'low', '--reviewer-effort', 'low',
                                  '--gate-effort', 'low', '--test-command', 'python3 -m unittest')

    def write_probe(self, co, status, version):
        (co.run_dir / 'permission-probe.json').write_text(
            json.dumps({'status': status, 'author_flags': {'codex_cli_version': version}}))

    def cli(self, action, version, *extra):
        """In-process main() with the fake-harness bypass off, as on a real operator machine."""
        command = self.h.command(*extra)
        command[2] = action
        out = io.StringIO()
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': version}), \
                patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), \
                contextlib.redirect_stdout(out):
            code = rc.main(command[2:])
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr='')

    def recorded_override(self):
        return json.loads((self.h.run_dir / 'state.json').read_text()).get('codex_cli_override')

    def test_verified_version_passes(self):
        co = self.co()
        self.assertEqual(co.codex_contract_verified(), (True, ''))
        args = co._codex_sandbox_profile_args()
        self.assertEqual(args[:2], ['-P', 'paired_session_author'])
        self.assertEqual(args[4:], ['--config', 'permissions.paired_session_author.network.enabled=false'])

    def test_synthetic_profile_arguments_unchanged_for_verified_version(self):
        co = self.co()
        tmp = json.dumps(str(co.author_temp_dir.resolve()))
        expected = ('permissions.paired_session_author.filesystem={":root"="read", ":tmpdir"="write", '
                    + tmp + '="write", ":workspace_roots"={"."="write", ".git"="read", ".agents"="read", '
                    '".codex"="read", ".aws"="read"}}')
        self.assertEqual(co._codex_sandbox_profile_args()[3], expected)

    def test_unverified_version_without_probe_or_override_is_refused(self):
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            co = self.co()
            ok, why = co.codex_contract_verified()
            self.assertFalse(ok)
            self.assertIn(UNVERIFIED, why)
            self.assertIn('permission-probe', why)
            self.assertIn('--accept-unverified-codex-cli', why)
            with self.assertRaisesRegex(ValueError, 'unverified codex sandbox contract'):
                co._codex_sandbox_profile_args()
        refused = self.cli('run', UNVERIFIED)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('unverified codex sandbox contract', refused.stdout)

    def test_unverified_version_with_matching_probe_pass_is_accepted(self):
        for status in ('PASS', 'PASS_RESIDUAL_RISK'):
            with self.subTest(status=status), patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
                co = self.co()
                self.write_probe(co, status, UNVERIFIED)
                self.assertEqual(co.codex_contract_verified(), (True, ''))
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            self.write_probe(co, 'FAIL', UNVERIFIED)
            self.assertFalse(co.codex_contract_verified()[0])

    def test_probe_pass_from_a_different_version_is_refused(self):
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            co = self.co()
            self.write_probe(co, 'PASS', OTHER)
            self.assertFalse(co.codex_contract_verified()[0])
            (co.run_dir / 'permission-probe.json').write_text(json.dumps({'status': 'PASS'}))
            self.assertFalse(co.codex_contract_verified()[0])

    def test_unavailable_version_never_matches_a_probe_or_override(self):
        co = self.co()
        self.write_probe(co, 'PASS', 'UNAVAILABLE')
        co.state['codex_cli_override'] = {'version': 'UNAVAILABLE', 'actor': 'operator'}
        with patch.object(co, '_codex_cli_version', return_value='UNAVAILABLE'):
            self.assertFalse(co.codex_contract_verified()[0])

    def test_override_is_recorded_and_a_version_change_voids_it_on_restore(self):
        refused = self.cli('run', UNVERIFIED, '--accept-unverified-codex-cli', '--reason', 'checked by hand')
        # The contract guard passes; the run then stops at the (separate) missing-probe gate.
        self.assertIn('permission-probe.json is missing', refused.stdout, refused.stdout + refused.stderr)
        override = self.recorded_override()
        self.assertEqual((override['version'], override['reason'], override['actor']),
                         (UNVERIFIED, 'checked by hand', 'operator'))
        self.assertRegex(override['time'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        same = self.cli('resume', UNVERIFIED)
        self.assertNotIn('unverified codex sandbox contract', same.stdout)
        changed = self.cli('resume', OTHER)
        self.assertEqual(changed.returncode, 2, changed.stdout + changed.stderr)
        self.assertIn(OTHER, changed.stdout)
        self.assertIn(f'override for {UNVERIFIED} is void', changed.stdout)
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': OTHER}):
            self.assertFalse(self.co().codex_contract_verified()[0])
        # Only a fresh operator flag re-accepts the new version.
        again = self.cli('resume', OTHER, '--accept-unverified-codex-cli', '--reason', 'rechecked')
        self.assertNotIn('unverified codex sandbox contract', again.stdout)
        self.assertEqual(self.recorded_override()['version'], OTHER)

    def test_override_needs_a_reason_and_cannot_come_from_saved_state_or_config(self):
        for extra in (['--accept-unverified-codex-cli'], ['--reason', 'why'],
                      ['--accept-unverified-codex-cli', '--reason', '  ']):
            with self.subTest(extra=extra):
                result = self.cli('run', UNVERIFIED, *extra)
                self.assertEqual(result.returncode, 2)
                self.assertIn('needs --reason', result.stdout)
        probe = self.cli('permission-probe', UNVERIFIED, '--accept-unverified-codex-cli', '--reason', 'x')
        self.assertIn('needs --reason and run, resume or reject', probe.stdout)
        # A saved override naming another actor or version is not an acceptance.
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            co = self.co()
            co.state['codex_cli_override'] = {'version': UNVERIFIED, 'reason': 'x', 'actor': 'author'}
            self.assertFalse(co.codex_contract_verified()[0])
        config = self.h.workspace / '.review-loop' / 'paired-session.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'accept_unverified_codex_cli': True}))
        self.assertIn('unsupported paired-session config keys', self.cli('run', UNVERIFIED).stdout)

    def test_permission_probe_runs_on_an_unverified_version(self):
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            co = self.co()
            report = co._author_permission_probe()
        self.assertEqual(report['model_probe'], 'ATTEMPTED', report)
        self.assertTrue(all(row['status'] == 'CONTRACT-FAIL' for row in
                            report['advisory_direct_controls']['checks'].values()))
        result = self.cli('permission-probe', UNVERIFIED)
        self.assertNotIn('unverified codex sandbox contract', result.stdout + result.stderr)
        recorded = json.loads((self.h.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(recorded['author_flags']['codex_cli_version'], UNVERIFIED)
        if recorded['status'] in ('PASS', 'PASS_RESIDUAL_RISK'):
            after = self.cli('resume', UNVERIFIED)
            self.assertNotIn('unverified codex sandbox contract', after.stdout)


if __name__ == '__main__':
    unittest.main()
