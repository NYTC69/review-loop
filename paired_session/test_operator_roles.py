"""P0-2 (codex-cli verified-contract rule) and P0-1 (operator-configured role models, ADR-9)."""
import contextlib
import io
import json
import os
import types
import unittest
from pathlib import Path
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

    def write_probe(self, co, status):
        """A record probe_passed() accepts for the CURRENT fake codex version (digests bind the version)."""
        (co.run_dir / 'permission-probe.json').write_text(json.dumps({
            'status': status, 'reviewer_flags_digest': co.reviewer_flags_digest(),
            'author_flags_digest': co.author_flags_digest(),
            'author_permission_probe': {'status': status, 'd1a_model_verdict': 'UNKNOWN',
                                        'd1b_synthetic_verdict': 'PASS'},
            'global_config_changes': {'status': 'PASS'}}))

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
                self.write_probe(co, status)
                self.assertEqual(co.codex_contract_verified(), (True, ''))
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            self.write_probe(co, 'FAIL')
            self.assertFalse(co.codex_contract_verified()[0])

    def test_probe_pass_from_a_different_version_is_refused(self):
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            co = self.co()
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': OTHER}):
            self.write_probe(co, 'PASS')            # a full record, but produced on another version
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            self.assertFalse(co.codex_contract_verified()[0])

    def test_a_probe_file_that_probe_passed_rejects_never_satisfies_the_contract(self):
        path = self.h.run_dir / 'permission-probe.json'
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            co = self.co()
            self.write_probe(co, 'PASS')
            good = json.loads(path.read_text())
            forged = [{'status': 'PASS', 'author_flags': {'codex_cli_version': UNVERIFIED}},
                      {**good, 'author_flags_digest': '0' * 64}, {**good, 'reviewer_flags_digest': '0' * 64},
                      {**good, 'author_permission_probe': {'status': 'FAIL'}},
                      {**good, 'global_config_changes': {'status': 'FAIL'}}]
            for report in forged:
                with self.subTest(report=report):
                    path.write_text(json.dumps(report))
                    self.assertFalse(co.probe_passed()[0])
                    self.assertFalse(co.codex_contract_verified()[0])
            path.write_text(json.dumps(good))
            self.assertEqual((co.probe_passed()[0], co.codex_contract_verified()[0]), (True, True))

    def test_every_real_codex_author_dispatch_passes_the_contract(self):
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}), \
                patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False):
            co = self.co()
            schema = self.h.root / 'schema.json'
            with self.assertRaisesRegex(ValueError, 'unverified codex sandbox contract'):
                co._codex_command('author', schema, True)
            with self.assertRaisesRegex(ValueError, 'unverified codex sandbox contract'):
                co.command('author', schema, True)
            with self.assertRaisesRegex(ValueError, 'unverified codex sandbox contract'):
                co.drive()                            # not via run/resume/reject
            self.assertIn('exec', co._codex_command('reviewer', schema, True))   # other roles unaffected
            self.write_probe(co, 'PASS')
            self.assertIn('exec', co._codex_command('author', schema, True))
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):             # fake harness unchanged
            self.assertIn('exec', self.co()._codex_command('author', schema, True))

    def test_unavailable_version_never_matches_a_probe_or_override(self):
        co = self.co()
        self.write_probe(co, 'PASS')
        co.state['codex_cli_override'] = {'version': 'UNAVAILABLE', 'actor': 'operator'}
        with patch.object(co, '_codex_cli_version', return_value='UNAVAILABLE'):
            ok, why = co.codex_contract_verified()
            self.assertFalse(ok)
            self.assertIn('re-run permission-probe', why)
            self.assertNotIn('--accept-unverified-codex-cli', why)
            self.assertNotIn('voided', co.state['codex_cli_override'])   # an unreadable version is not a change

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

    def test_a_voided_override_never_revives_when_the_version_returns(self):
        self.cli('run', UNVERIFIED, '--accept-unverified-codex-cli', '--reason', 'checked by hand')
        self.assertNotIn('voided', self.recorded_override())
        away = self.cli('resume', OTHER)                                   # A -> B
        self.assertEqual(away.returncode, 2, away.stdout)
        voided = self.recorded_override()['voided']
        self.assertEqual(voided['version_seen'], OTHER)
        self.assertRegex(voided['time'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        back = self.cli('resume', UNVERIFIED)                              # B -> A
        self.assertEqual(back.returncode, 2, back.stdout)
        self.assertIn(f'override for {UNVERIFIED} is void', back.stdout)
        self.assertEqual(self.recorded_override()['voided'], voided)       # the record is not rewritten
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            self.assertFalse(self.co().codex_contract_verified()[0])
        again = self.cli('resume', UNVERIFIED, '--accept-unverified-codex-cli', '--reason', 'rechecked')
        self.assertNotIn('unverified codex sandbox contract', again.stdout)
        self.assertNotIn('voided', self.recorded_override())

    def test_operator_only_flags_are_never_restored_from_saved_state(self):
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            co = self.co()
        state = json.loads((co.run_dir / 'state.json').read_text())
        state['config'].update({'accept_unverified_codex_cli': True, 'reason': 'injected', 'accept_future': True})
        (co.run_dir / 'state.json').write_text(json.dumps(state))
        refused = self.cli('reject', UNVERIFIED, '--text', 'x')
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn('unverified codex sandbox contract', refused.stdout)
        self.assertIsNone(self.recorded_override())
        self.assertEqual(rc.OPERATOR_ONLY_DESTS, {'accept_unverified_codex_cli', 'reason'})

    def test_operator_only_keys_are_refused_in_a_config_file(self):
        config = self.h.workspace / '.review-loop' / 'paired-session.json'
        config.parent.mkdir()
        for key in ('accept_unverified_codex_cli', 'reason', 'accept_anything'):
            with self.subTest(key=key):
                config.write_text(json.dumps({key: 'x'}))
                self.assertIn('unsupported paired-session config keys', self.cli('run', UNVERIFIED).stdout)

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
        # Real (non-fake-harness) dispatch path: the author probe turn must not hit the K1 choke point.
        result = self.cli('permission-probe', UNVERIFIED)
        self.assertNotIn('unverified codex sandbox contract', result.stdout + result.stderr)
        recorded = json.loads((self.h.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(recorded['author_flags']['codex_cli_version'], UNVERIFIED)
        author = recorded['author_permission_probe']
        self.assertEqual(author.get('model_probe'), 'ATTEMPTED', author)
        self.assertNotIn('reason', author)         # a refused dispatch is reported as {'status': 'FAIL', 'reason': ...}
        self.assertTrue(author['observed_commands'])
        if recorded['status'] in ('PASS', 'PASS_RESIDUAL_RISK'):
            after = self.cli('resume', UNVERIFIED)
            self.assertNotIn('unverified codex sandbox contract', after.stdout)


BUG_REPORT_FLAGS = ['--author-vendor', 'claude', '--author-model', 'claude-opus-5-5',
                    '--reviewer-vendor', 'codex', '--reviewer-model', 'gpt-6.1-sol',
                    '--gate-model', 'gpt-6.1-sol', '--gate-vendor', 'codex']


class RoleModelTests(unittest.TestCase):
    """P0-1 / ADR-9: role vendors and models are operator-configured."""

    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def args(self, *extra, action='run'):
        h = self.h
        argv = [action, '--workspace', str(h.workspace), '--workitem', str(h.workitem),
                '--run-dir', str(h.run_dir), *extra]
        return rc.configure_parser(rc.parser(), argv).parse_args(argv)

    def resolved(self, *extra):
        return rc.resolve_role_model_defaults(self.args(*extra))

    def write_profile(self, values):
        path = self.h.workspace / '.review-loop' / 'paired-session.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(values))

    def main(self, action, *extra):
        command = self.h.command(*extra)
        command[2] = action
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rc.main(command[2:])
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue())

    def state(self):
        return json.loads((self.h.run_dir / 'state.json').read_text())

    def restored(self, *extra, action='accept', explicit=()):
        args = self.args(*extra, action=action)
        args.explicit_role_flags = set(explicit)
        rc.Coordinator(args)
        return args

    def test_bug_report_config_passes_the_model_layer(self):
        args = self.resolved(*BUG_REPORT_FLAGS)
        rc.validate_role_models(args)
        self.assertEqual((args.author_model, args.reviewer_model, args.gate_model, args.gate_vendor),
                         ('claude-opus-5-5', 'gpt-6.1-sol', 'gpt-6.1-sol', 'codex'))
        # main() may still refuse the Claude author (P0-3), but never on a model.
        refused = self.main('run', *BUG_REPORT_FLAGS)
        self.assertNotIn('model', refused.stdout.lower().replace('claude author', ''), refused.stdout)

    def test_defaults_are_unchanged(self):
        for flags, expected in (
                ([], ('codex', 'gpt-6-luna', 'claude', 'claude-opus-5-5', 'claude', 'claude-opus-5-5')),
                (['--author-vendor', 'claude', '--reviewer-vendor', 'codex'],
                 ('claude', 'claude-opus-5-5', 'codex', 'gpt-6-luna', 'codex', 'gpt-6-luna'))):
            with self.subTest(flags=flags):
                a = self.resolved(*flags)
                rc.validate_role_models(a)
                self.assertEqual((a.author_vendor, a.author_model, a.reviewer_vendor, a.reviewer_model,
                                  a.gate_vendor, a.gate_model), expected)

    def test_gate_vendor_may_equal_the_reviewer_or_the_author(self):
        for flags, vendor, model in ((['--gate-vendor', 'codex'], 'codex', 'gpt-6-luna'),
                                     (['--gate-vendor', 'claude', '--reviewer-vendor', 'claude'],
                                      'claude', 'claude-opus-5-5')):
            with self.subTest(flags=flags):
                a = self.resolved(*flags)
                self.assertEqual((a.gate_vendor, a.gate_model), (vendor, model))

    def test_malformed_model_ids_are_refused(self):
        for bad in ('', ' ', '-x', '.x', 'a b', 'a;b', 'a$(b)', 'a\n', 'x' * 129, 'gpt\x00'):
            for role in ('author', 'reviewer', 'gate'):
                with self.subTest(role=role, bad=bad):
                    a = self.resolved()
                    setattr(a, role + '_model', bad)
                    with self.assertRaisesRegex(ValueError, f'{role}_model is not a well-formed model id'):
                        rc.validate_role_models(a)
        for good in ('gpt-6.1-sol', 'claude-opus-5-5', 'a', 'o3:high', 'org/model_v1[1m]', 'x' * 128):
            with self.subTest(good=good):
                a = self.resolved()
                a.author_model = good
                rc.validate_role_models(a)
        refused = self.main('run', '--author-model', 'bad model')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('author_model is not a well-formed model id', refused.stdout)
        self.assertFalse(self.h.run_dir.exists())

    def test_allowlist_is_enforced_when_set_and_malformed_ones_are_refused(self):
        allowed = {'codex': ['gpt-6-luna', 'gpt-6.1-sol'], 'claude': ['claude-opus-5-5']}
        a = self.resolved()
        a.allowed_models = allowed
        rc.validate_role_models(a)
        a.reviewer_model = 'claude-sonnet-5-5'
        with self.assertRaisesRegex(ValueError, 'reviewer_model claude-sonnet-5-5 is not in allowed_models'):
            rc.validate_role_models(a)
        a = self.resolved('--author-vendor', 'claude', '--reviewer-vendor', 'codex')
        a.allowed_models = {'claude': ['claude-opus-5-5']}      # codex missing: nothing is allowed for it
        with self.assertRaisesRegex(ValueError, 'reviewer_model gpt-6-luna is not in allowed_models'):
            rc.validate_role_models(a)
        for bad in ([], 'gpt-6', {'codex': 'gpt-6-luna'}, {'codex': [1]}, {'gemini': ['x']}, {'codex': None}):
            with self.subTest(bad=bad):
                a = self.resolved()
                a.allowed_models = bad
                with self.assertRaisesRegex(ValueError, 'allowed_models must be an object'):
                    rc.validate_role_models(a)
        self.assertIsNone(self.resolved().allowed_models)       # unset: no allowlist
        self.write_profile({'allowed_models': allowed, 'author_model': 'gpt-6.1-sol'})
        self.assertEqual(self.args().allowed_models, allowed)
        self.assertIn('not in allowed_models', self.main('run', '--reviewer-model', 'claude-sonnet-5-5').stdout)
        self.assertFalse(self.h.run_dir.exists())
        self.write_profile({'allowed_models': ['gpt-6']})
        self.assertIn('allowed_models must be an object', self.main('run').stdout)
        self.assertFalse(self.h.run_dir.exists())

    def test_the_gate_dispatch_uses_the_selected_gate_vendor(self):
        schema = self.h.root / 'gate-schema.json'
        rc.atomic_json(schema, rc.review_schema())
        for index, (flags, vendor) in enumerate(((['--gate-vendor', 'codex'], 'codex'),
                                                 (['--gate-vendor', 'claude'], 'claude'), ([], 'claude'))):
            with self.subTest(flags=flags):
                self.h.run_dir = self.h.root / f'gate-{index}'
                co = self.h.coordinator(*flags)
                self.assertEqual(co._role_vendor('gate'), vendor)
                command = co.command('gate', schema, True)
                self.assertEqual(Path(command[0]), Path(getattr(co.args, vendor + '_bin')))
                self.assertIn(co.args.gate_model, command)
                self.assertEqual(co.state['config']['gate_vendor'], vendor)
        self.h.run_dir = self.h.root / 'gate-e2e'
        result = self.h.run_coordinator('--author-vendor', 'claude', '--reviewer-vendor', 'claude',
                                        '--gate-vendor', 'codex', '--gate-model', 'gpt-6.1-sol',
                                        '--shadow', 'off', env={'FAKE_CODEX_MODEL': 'gpt-6.1-sol'})
        gates = [t for t in self.state()['turns'] if t['role'] == 'gate']
        self.assertTrue(gates, result.stdout + result.stderr)
        self.assertTrue(all(t['vendor'] == 'codex' and t['model'] == 'gpt-6.1-sol' for t in gates))

    def test_a_restore_keeps_the_saved_roles_and_refuses_a_switch(self):
        saved_flags = ['--gate-vendor', 'codex', '--gate-model', 'gpt-6.1-sol',
                       '--reviewer-model', 'claude-sonnet-5-5']
        self.h.coordinator(*saved_flags)
        saved = self.state()['config']
        self.assertEqual((saved['gate_vendor'], saved['gate_model'], saved['reviewer_model']),
                         ('codex', 'gpt-6.1-sol', 'claude-sonnet-5-5'))
        for action in ('accept', 'reject', 'note'):
            with self.subTest(action=action):
                a = self.restored(action=action)      # no role flags: the CLI defaults must not win
                self.assertEqual((a.gate_vendor, a.gate_model, a.reviewer_model),
                                 ('codex', 'gpt-6.1-sol', 'claude-sonnet-5-5'))
                for flag, value in (('--gate-vendor', 'claude'), ('--gate-model', 'claude-opus-5-5'),
                                    ('--reviewer-model', 'claude-opus-5-5'), ('--author-model', 'gpt-6-sol'),
                                    ('--reviewer-vendor', 'codex'), ('--author-vendor', 'claude')):
                    switched = self.main(action, flag, value, '--text', 'x')
                    self.assertEqual(switched.returncode, 2, (action, flag, switched.stdout))
                    self.assertIn('role models are fixed for this run', switched.stdout)
        same = self.main('note', '--gate-model', 'gpt-6.1-sol', '--text', 'x')     # repeating it is no switch
        self.assertNotIn('role models are fixed', same.stdout)
        self.write_profile({'allowed_models': {'codex': ['gpt-6-luna'], 'claude': ['claude-opus-5-5']}})
        self.assertIn('not in allowed_models', self.main('note', '--text', 'x').stdout)    # saved models re-checked
        (self.h.workspace / '.review-loop' / 'paired-session.json').unlink()
        for flag, value in (('--gate-vendor', 'claude'), ('--gate-model', 'claude-opus-5-5'),
                            ('--reviewer-model', 'claude-opus-5-5')):
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, 'role models are fixed'):
                self.h.coordinator(*saved_flags, flag, value)      # run/resume compare every role key

    def test_a_saved_run_without_gate_vendor_derives_it_the_old_way(self):
        flags = ['--author-vendor', 'claude', '--reviewer-vendor', 'codex']
        self.h.coordinator(*flags)
        state = self.state()
        self.assertEqual(state['config']['gate_vendor'], 'codex')
        del state['config']['gate_vendor']
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        # The CLI author default (codex) would derive gate=claude; the saved author (claude) says codex.
        restored = self.restored()
        self.assertEqual((restored.author_vendor, restored.gate_vendor), ('claude', 'codex'))
        self.assertEqual(self.h.coordinator(*flags)._role_vendor('gate'), 'codex')
        with self.assertRaisesRegex(ValueError, 'role models are fixed for this run: gate_vendor'):
            self.h.coordinator(*flags, '--gate-vendor', 'claude', '--gate-model', 'gpt-6-luna')
        with self.assertRaisesRegex(ValueError, 'role models are fixed for this run: gate_vendor'):
            self.restored('--gate-vendor', 'claude', explicit={'gate_vendor'})

    def test_a_cli_reported_model_mismatch_holds_the_run(self):
        result = self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                        env={'FAKE_CODEX_MODEL': 'gpt-6-astra'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual(state['status'], 'HOLD')
        self.assertIn('model identity mismatch', state['hold_reason'])
        self.assertEqual(state['turns'][-1]['model_identity'], 'MISMATCH')
        # A configured non-default model that the CLI confirms runs normally.
        self.h.run_dir = self.h.root / 'match'
        ok = self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                    '--author-model', 'gpt-6.1-sol', env={'FAKE_CODEX_MODEL': 'gpt-6.1-sol'})
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        turns = [t for t in self.state()['turns'] if t['vendor'] == 'codex']
        self.assertTrue(turns and all(t['model'] == 'gpt-6.1-sol' and t['model_identity'] == 'MATCH'
                                      for t in turns))
        self.assertTrue(all('gpt-6.1-sol' in t['command'] for t in turns))


    def test_a_model_mismatch_does_not_hide_a_workspace_mutation(self):
        result = self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                        env={'FAKE_CLAUDE_MODEL': 'claude-opus-5', 'FAKE_MUTATION': 'echo'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual(state['status'], 'HOLD')
        self.assertIn('mutated workspace', state['hold_reason'])
        self.assertEqual(state['turns'][-1]['model_identity'], 'MISMATCH')
        self.assertTrue(state['turns'][-1]['observed_commands'] is not None)

    def test_an_author_writable_profile_cannot_select_role_vendors_or_models(self):
        for key, value in (('gate_vendor', 'codex'), ('reviewer_model', 'gpt-6-luna'),
                           ('allowed_models', {'codex': ['gpt-6-luna']}), ('gate_model', 'gpt-6-luna')):
            with self.subTest(key=key):
                self.write_profile({key: value})
                programs, issue = rc.program_snapshot(
                    self.h.workspace, self.h.run_dir, self.h.run_dir / 'author-tmp',
                    self.h.fake_codex_cli(), self.h.fake_claude_cli(), rc.DEFAULT_GATE_PROMPT, None)
                self.assertIn('workspace profile cannot select operator programs: ' + key, issue)


if __name__ == '__main__':
    unittest.main()
