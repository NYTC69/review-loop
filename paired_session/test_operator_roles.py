"""P0-2 (codex-cli verified-contract rule) and P0-1 (operator-configured role models, ADR-9)."""
import contextlib
import hashlib
import inspect
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import claude_author_probe as cap
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
            'status': status, 'reviewer_flags': co.reviewer_flags(),
            'reviewer_flags_digest': co.reviewer_flags_digest(),
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
            # Every real author turn runs inside _drive_loop (author_turn / polish_author_turn), which only
            # drive() enters; resume / resume_polish / reject re-enter through drive(). Refuse before any turn.
            with patch.object(co, '_drive_loop', side_effect=AssertionError('dispatched')) as loop:
                for entry in (co.drive, co.resume):
                    with self.subTest(entry=entry.__name__), \
                            self.assertRaisesRegex(ValueError, 'unverified codex sandbox contract'):
                        entry()                       # drive() directly, not via run/resume/reject
                with patch.object(co, '_codex_cli_version', return_value='UNAVAILABLE'), \
                        self.assertRaisesRegex(ValueError, 'UNAVAILABLE'):
                    co.drive()
                loop.assert_not_called()
                self.write_probe(co, 'PASS')
                with self.assertRaisesRegex(AssertionError, 'dispatched'):
                    co.drive()                        # contract holds: drive() reaches the turn loop
            self.assertIn('exec', co._codex_command('reviewer', schema, True))   # other roles unaffected
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
        self.assertEqual(rc.OPERATOR_ONLY_DESTS,
                         {'accept_unverified_codex_cli', 'accept_unverified_claude_author', 'reason'})

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
                '--run-dir', str(h.run_dir), '--codex-bin', str(h.fake_codex_cli()),
                '--claude-bin', str(h.fake_claude_cli()), *extra]
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

    def state_at(self, run_dir):
        return json.loads((run_dir / 'state.json').read_text())

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
        # P0-1b A1: a policy this run never had cannot be added on restore (was: saved models re-checked).
        self.assertIn('role policy is fixed for this run', self.main('note', '--text', 'x').stdout)
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
                           ('allowed_models', {'codex': ['gpt-6-luna']}), ('gate_model', 'gpt-6-luna'),
                           ('author_model', 'gpt-6.1-sol')):
            with self.subTest(key=key):
                self.write_profile({key: value})
                programs, issue = rc.program_snapshot(
                    self.h.workspace, self.h.run_dir, self.h.run_dir / 'author-tmp',
                    self.h.fake_codex_cli(), self.h.fake_claude_cli(), rc.DEFAULT_GATE_PROMPT, None)
                self.assertIn('workspace profile cannot select operator programs: ' + key, issue)


    # ---- P0-1b: allowlist persistence, gate permission surface, resume, profile ----
    ALLOWED = {'codex': ['gpt-6-luna', 'gpt-6.1-sol'], 'claude': ['claude-opus-5-5']}

    def allowlist_run(self, allowed=None, *extra):
        path = self.h.root / 'operator-policy.json'          # outside the workspace: operator-owned
        path.write_text(json.dumps({'allowed_models': allowed or self.ALLOWED}))
        rc.Coordinator(self.args('--config', str(path), *extra))
        return path

    def test_the_allowlist_is_saved_and_enforced_after_every_restore(self):
        path = self.allowlist_run()
        self.assertEqual(self.state()['config']['allowed_models'], self.ALLOWED)
        for action in ('accept', 'reject', 'note', 'resume', 'run'):
            with self.subTest(action=action):
                self.assertEqual(self.restored(action=action).allowed_models, self.ALLOWED)   # no --config given
        state = self.state()                     # a saved model outside the saved policy never dispatches
        state['config']['gate_model'] = 'gpt-9'
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        for action in ('note', 'reject'):
            self.assertIn('gate_model gpt-9 is not in allowed_models',
                          self.main(action, '--text', 'x').stdout)
        with self.assertRaisesRegex(ValueError, 'gate_model gpt-9 is not in allowed_models'):
            self.restored(action='resume')
        state['config']['gate_model'] = 'claude-opus-5-5'
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        other = self.h.root / 'other-policy.json'
        other.write_text(json.dumps({'allowed_models': {'codex': ['gpt-6-luna'], 'claude': ['claude-opus-5-5']}}))
        for action in ('accept', 'note', 'resume'):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'role policy is fixed'):
                self.restored('--config', str(other), action=action)
        self.assertEqual(self.restored('--config', str(path)).allowed_models, self.ALLOWED)   # same one is fine

    def test_a_saved_run_without_allowed_models_means_no_allowlist(self):
        self.h.coordinator()
        state = self.state()
        state['config'].pop('allowed_models')
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        self.assertIsNone(self.restored(action='resume').allowed_models)
        self.assertIsNone(self.restored().allowed_models)

    def test_the_scope_change_successor_inherits_the_allowlist(self):
        self.allowlist_run()
        self.assertIn('Start:', self.main('note', '--scope-change', '--text', 'narrow it').stdout)
        config = json.loads((self.h.run_dir / 'evidence' / 'successor-config.json').read_text())
        self.assertEqual(config['allowed_models'], self.ALLOWED)
        target = self.h.run_dir.with_name(self.h.run_dir.name + '-successor')
        successor = ['--workitem', str(self.h.run_dir / 'evidence' / 'successor-workitem.md'),
                     '--run-dir', str(target), '--supersedes', str(self.h.run_dir)]
        other = self.h.root / 'other-successor-policy.json'
        other.write_text(json.dumps({'allowed_models': {'codex': ['gpt-6-luna'], 'claude': ['claude-opus-5-5']}}))
        for extra in ([], ['--config', str(other)]):        # no config, or another allowlist: not this run's successor
            with self.subTest(extra=extra):
                self.assertIn('successor spec or parent state differs', self.main('run', *successor, *extra).stdout)
                self.assertFalse((target / 'state.json').exists())
        kept = self.main('run', *successor, '--config', str(self.h.run_dir / 'evidence' / 'successor-config.json'))
        self.assertNotIn('successor spec or parent state differs', kept.stdout)
        self.assertEqual(self.state_at(target)['config']['allowed_models'], self.ALLOWED)

    def test_a_plain_resume_of_a_non_default_model_run_works(self):
        self.h.coordinator('--gate-model', 'gpt-6.1-sol', '--reviewer-model', 'claude-sonnet-5-5')
        for action in ('resume', 'run'):
            a = self.restored(action=action)
            self.assertEqual((a.gate_model, a.reviewer_model), ('gpt-6.1-sol', 'claude-sonnet-5-5'))
        with self.assertRaisesRegex(ValueError, 'role models are fixed for this run: gate_model'):
            self.restored('--gate-model', 'claude-opus-5-5', action='resume', explicit={'gate_model'})
        # An allowlist naming only the saved models must not trip over the CLI defaults on a plain resume.
        self.h.run_dir = self.h.root / 'allow-resume'
        policy = self.allowlist_run({'codex': ['gpt-6.1-sol'], 'claude': ['claude-sonnet-5-5']}, '--author-model',
                           'gpt-6.1-sol', '--reviewer-model', 'claude-sonnet-5-5', '--gate-model', 'claude-sonnet-5-5')
        resumed = self.main('resume', '--config', str(policy)).stdout
        for text in ('allowed_models', 'role models are fixed', 'role policy is fixed'):
            self.assertNotIn(text, resumed)

    def test_a_gate_vendor_that_differs_from_the_reviewer_is_refused_before_state(self):
        def cli(*flags):
            self.h.run_dir = self.h.root / ('gate-' + '-'.join(flags).replace('-', ''))
            with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False):
                return self.main('run', *flags), self.h.run_dir
        refused, run_dir = cli('--gate-vendor', 'codex')          # codex author, claude reviewer
        self.assertIn('REFUSED: gate vendor codex differs from reviewer vendor claude; '
                      'its read-only surface is not covered by permission-probe', refused.stdout)
        self.assertFalse(run_dir.exists())
        for flags in ([], ['--reviewer-vendor', 'codex', '--gate-vendor', 'codex'],
                      ['--reviewer-vendor', 'claude', '--gate-vendor', 'claude']):
            with self.subTest(flags=flags):
                a = self.resolved(*flags)
                self.assertIsNone(rc.gate_surface_issue(a))      # default codex author + claude/claude gate
        a = self.resolved(*BUG_REPORT_FLAGS)
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False):
            self.assertIsNone(rc.gate_surface_issue(a))          # bug-report: gate == reviewer == codex
            a = self.resolved('--author-vendor', 'claude')        # claude author default: claude reviewer, codex gate
            self.assertIn('not covered by permission-probe', rc.gate_surface_issue(a))
        self.assertIsNone(rc.gate_surface_issue(self.resolved('--author-vendor', 'claude')))   # fake harness
        # a restore is refused as well (saved run whose gate differs from its reviewer)
        self.h.run_dir = self.h.root / 'gate-saved'
        self.h.coordinator('--gate-vendor', 'codex')
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False):
            self.assertIn('not covered by permission-probe', self.main('reject', '--text', 'x').stdout)

    def test_probe_passed_binds_the_probed_reviewer_vendor_to_the_gate_vendor(self):
        co = self.h.coordinator()
        report = {'status': 'PASS', 'reviewer_flags': co.reviewer_flags(),
                  'reviewer_flags_digest': co.reviewer_flags_digest(), 'author_flags_digest': co.author_flags_digest(),
                  'author_permission_probe': {'status': 'PASS', 'd1a_model_verdict': 'UNKNOWN',
                                              'd1b_synthetic_verdict': 'PASS'},
                  'global_config_changes': {'status': 'PASS'}}
        (co.run_dir / 'permission-probe.json').write_text(json.dumps(report))
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False):
            self.assertEqual(co.probe_passed(), (True, ''))
            co.args.gate_vendor = 'codex'                       # reviewer surface probed was claude
            self.assertEqual(co.probe_passed(), (False, 'permission probe reviewer vendor is not the gate vendor'))


OPT_IN = ['--accept-unverified-claude-author', '--reason', 'checked by hand']
REFUSAL = 'Claude author is limited to the fake test harness'
STAMP = r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$'


class ClaudeAuthorTests(unittest.TestCase):
    """P0-3a: path-scoped Claude author file rules; probe-or-explicit-opt-in replaces the hard refusal."""

    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def co(self, *extra):
        # Same flags as self.h.command() so a CLI call can restore this coordinator's saved run.
        return self.h.coordinator(*BUG_REPORT_FLAGS, '--timeout', '10', '--author-effort', 'low',
                                  '--reviewer-effort', 'low', '--gate-effort', 'low',
                                  '--test-command', 'python3 -m unittest', *extra)

    def cli(self, action, *extra):
        """In-process main() with the fake-harness bypass off, as on a real operator machine."""
        command = self.h.command(*BUG_REPORT_FLAGS, *extra)
        command[2] = action
        out = io.StringIO()
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), \
                contextlib.redirect_stdout(out):
            code = rc.main(command[2:])
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue())

    def state(self):
        return json.loads((self.h.run_dir / 'state.json').read_text())

    def author_argv(self, co):
        schema = self.h.root / 'schema.json'
        rc.atomic_json(schema, rc.review_schema())
        return co.command('author', schema, False)

    @staticmethod
    def author_deny(argv):
        """Every deny rule in force: the settings' run_dir rule plus the author's separate --disallowedTools rules."""
        settings = json.loads(argv[argv.index('--settings') + 1])
        return settings['permissions']['deny'] + [
            argv[i + 1] for i, v in enumerate(argv) if v == '--disallowedTools' and argv[i + 1].startswith('Edit(')]

    @staticmethod
    def denied(rule, path):
        """The documented rule forms: '//p/**' is everything under p, '//p/*' only the direct children of p."""
        pattern = rule[len('Edit('):-1].replace('~', str(Path.home()), 1)
        pattern = pattern[1:] if pattern.startswith('//') else pattern
        base = pattern.rsplit('/', 1)[0]
        if pattern.endswith('/**'): return str(path).startswith(base.rstrip('/') + '/')
        return str(Path(path).parent) == base

    def test_author_argv_is_path_scoped_and_denies_everything_outside_the_workspace(self):
        co = self.co()
        argv = self.author_argv(co)
        allowed = argv[argv.index('--allowedTools') + 1].split(',')
        ws, ctx = co.workspace.resolve(), co.context.resolve()
        self.assertNotIn('Edit', allowed)
        self.assertNotIn('Write', allowed)
        self.assertEqual([r for r in allowed if r.startswith(('Edit', 'Write'))],
                         ['Edit(//' + ws.as_posix().lstrip('/') + '/**)'])
        self.assertIn('Edit,Write', argv[argv.index('--tools') + 1])            # availability only
        settings = json.loads(argv[argv.index('--settings') + 1])
        deny = self.author_deny(argv)
        self.assertEqual(argv.count('--settings'), 1)
        for rule in ('Edit(//' + co.run_dir.as_posix().lstrip('/') + '/**)', 'Edit(//' + ctx.as_posix().lstrip('/') + '/**)',
                     'Edit(~/.claude/**)', 'Edit(~/.codex/**)',
                     'Edit(~/.ssh/**)', 'Edit(~/.aws/**)'):
            self.assertIn(rule, deny)
        deny_write = [Path(p) for p in settings['sandbox']['filesystem']['denyWrite']]
        self.assertTrue(any(p == co.context or p in co.context.parents for p in deny_write))   # context sits under run_dir
        schema = self.h.root / 'schema.json'                                    # other roles are unchanged
        reviewer = co._claude_command('reviewer', schema, False)
        self.assertEqual(reviewer.count('--disallowedTools'), 1)
        self.assertEqual(len(json.loads(reviewer[reviewer.index('--settings') + 1])['permissions']['deny']), 1)

    def test_no_deny_rule_matches_a_path_inside_the_workspace(self):
        co = self.co()
        ws = co.workspace.resolve()
        argv = self.author_argv(co)
        deny = self.author_deny(argv)
        for inside in (ws / 'a.py', ws / 'sub' / 'dir' / 'b.py', ws / '.git' / 'config'):
            for rule in deny:
                self.assertFalse(self.denied(rule, inside), (rule, inside))
        for outside in (co.context / 'workitem.md', co.run_dir / 'state.json',
                        Path.home() / '.ssh' / 'id', Path.home() / '.claude' / 'x'):
            self.assertTrue(any(self.denied(rule, outside) for rule in deny), outside)
        allow = co._claude_author_edit_rules()[0]                               # siblings: not allowed, no '//parent/*' deny
        self.assertFalse(any(self.denied(rule, ws.parent / 'sibling') for rule in allow))
        self.assertFalse(any(rule.endswith(ws.parent.as_posix().lstrip('/') + '/*)') for rule in deny))
        for bad in (co.workspace.parent / 'we(ird', Path.home() / '.ssh' / 'proj', co.context.parent):
            co.workspace = bad
            with self.assertRaisesRegex(ValueError, 'workspace must not'):
                co._claude_author_edit_rules()

    def test_actions_that_never_dispatch_an_author_are_not_blocked_by_a_void_opt_in(self):
        self.cli('run', '--author-vendor', 'claude', *OPT_IN)
        state = self.state()
        state['claude_author_override']['author_flags_digest'] = '0' * 64            # stale: void on the next check
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        for action, extra in (('note', ['--text', 'n']), ('accept', []), ('abort', []), ('snapshot', []),
                              ('permission-probe', [])):
            self.assertNotIn(REFUSAL, self.cli(action, *extra).stdout, action)
        self.assertIn(REFUSAL, self.cli('reject', '--text', 'x').stdout)             # dispatching actions still refuse
        self.assertIn(REFUSAL, self.cli('resume').stdout)

    def test_the_edit_rules_refuse_a_workspace_in_a_denied_root_or_the_filesystem_root(self):
        co = self.co()
        for bad in (Path('/'), co.context / 'ws', co.run_dir / 'ws', Path.home() / '.aws',
                    Path.home() / '.codex' / 'p'):
            with self.assertRaisesRegex(ValueError, 'workspace must not'):
                co._claude_author_edit_rules(bad)
        allow = co._claude_author_edit_rules(Path('/x'))[0]
        self.assertEqual(allow, ['Edit(//x/**)'])
        with patch.object(co, 'workspace', Path('/')):                          # surfaced as a clear REFUSED by main()
            with self.assertRaisesRegex(ValueError, 'filesystem root'):
                co.author_flags()

    def test_every_allow_rule_is_joined_into_the_author_allowed_tools(self):
        co = self.co()
        deny = co._claude_author_edit_rules()[1]
        with patch.object(co, '_claude_author_edit_rules', return_value=(['Edit(//a/**)', 'Edit(//b/**)'], deny)):
            argv = self.author_argv(co)
        allowed = argv[argv.index('--allowedTools') + 1].split(',')
        self.assertEqual([r for r in allowed if r.startswith('Edit(')], ['Edit(//a/**)', 'Edit(//b/**)'])

    def test_the_rules_bind_to_the_effective_workspace_override(self):
        co = self.co()
        other = (self.h.root / 'probe-ws').resolve()
        other.mkdir()
        schema = self.h.root / 'schema.json'
        rc.atomic_json(schema, rc.review_schema())
        argv = co.command('author', schema, False, other)
        allowed = argv[argv.index('--allowedTools') + 1].split(',')
        self.assertEqual([r for r in allowed if r.startswith('Edit(')], ['Edit(//' + other.as_posix().lstrip('/') + '/**)'])
        self.assertNotIn('Edit(//' + co.workspace.resolve().as_posix().lstrip('/') + '/**)', allowed)
        self.assertNotIn(co.workspace.resolve().as_posix().lstrip('/'), ' '.join(self.author_deny(argv)))

    def test_author_flags_record_the_exact_rules_and_the_digest_binds_them(self):
        co = self.co()
        flags = co.author_flags()
        allow, deny = co._claude_author_edit_rules()
        self.assertEqual(flags['claude_author_edit_rules'], (allow, deny))
        digest = co.author_flags_digest()
        with patch.object(co, '_claude_author_edit_rules', return_value=(allow, [*deny, 'Edit(//extra/**)'])):
            self.assertNotEqual(co.author_flags_digest(), digest)
        with patch.object(co, '_claude_author_edit_rules', return_value=([*allow, 'Edit(//extra/**)'], deny)):
            self.assertNotEqual(co.author_flags_digest(), digest)

    def test_a_claude_author_is_refused_without_the_opt_in_at_start_and_on_restore(self):
        started = self.cli('run', '--author-vendor', 'claude')
        self.assertEqual(started.returncode, 2)
        self.assertIn(REFUSAL, started.stdout)
        self.assertIn('--accept-unverified-claude-author', started.stdout)
        self.assertFalse((self.h.run_dir / 'state.json').exists())
        self.co()                                                               # a saved Claude-author run, no opt-in
        restored = self.cli('reject', '--text', 'x')
        self.assertEqual(restored.returncode, 2)
        self.assertIn(REFUSAL, restored.stdout)
        self.assertNotIn('claude_author_override', self.state())
        self.cli('reject', '--text', 'x', *OPT_IN)                              # the opt-in records and persists ...
        self.assertNotIn(REFUSAL, self.cli('reject', '--text', 'x').stdout)     # ... while the author flags are unchanged
        co = self.co()
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), \
                patch.object(co, '_drive_loop', side_effect=AssertionError('dispatched')):
            self.assertTrue(co.claude_author_verified()[0])
            with self.assertRaisesRegex(AssertionError, 'dispatched'):
                co.drive()                                                      # drive() directly passes with a current opt-in
            co.state['claude_author_override']['actor'] = 'author'
            with self.assertRaisesRegex(ValueError, REFUSAL):
                co.drive()                                                      # ... and refuses without one

    def test_the_opt_in_is_recorded_with_actor_reason_time_and_the_author_flags_digest(self):
        result = self.cli('run', '--author-vendor', 'claude', *OPT_IN)
        self.assertIn('permission-probe.json is missing', result.stdout)        # past the guard, at the probe gate
        record = self.state()['claude_author_override']
        self.assertEqual((record['actor'], record['reason']), ('operator', 'checked by hand'))
        self.assertRegex(record['time'], STAMP)
        self.assertEqual(record['author_flags_digest'], self.co().author_flags_digest())
        self.assertNotIn('voided', record)

    def test_the_opt_in_is_operator_only_and_needs_a_reason(self):
        self.assertIn('accept_unverified_claude_author', rc.OPERATOR_ONLY_DESTS)
        for extra in (['--accept-unverified-claude-author'], ['--accept-unverified-claude-author', '--reason', ' ']):
            self.assertIn('needs --reason', self.cli('run', '--author-vendor', 'claude', *extra).stdout)
        self.assertIn('needs --reason and run, resume or reject', self.cli(
            'permission-probe', '--author-vendor', 'claude', *OPT_IN).stdout)
        config = self.h.workspace / '.review-loop' / 'paired-session.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'accept_unverified_claude_author': True}))
        self.assertIn('unsupported paired-session config keys', self.cli('run', '--author-vendor', 'claude').stdout)
        config.unlink()
        self.co()                                                               # a saved run carrying the flag ...
        state = self.state()
        state['config'].update({'accept_unverified_claude_author': True, 'reason': 'injected'})
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        refused = self.cli('reject', '--text', 'x')                             # ... is not an opt-in
        self.assertEqual(refused.returncode, 2)
        self.assertIn(REFUSAL, refused.stdout)
        self.assertNotIn('claude_author_override', self.state())
        (self.h.run_dir / 'state.json').write_text(json.dumps({**self.state(), 'config': {
            k: v for k, v in self.state()['config'].items() if not k.startswith('accept_') and k != 'reason'}}))
        forged = self.co()                                                      # nor is a record naming another actor
        forged.state['claude_author_override'] = {'actor': 'author', 'reason': 'x',
                                                  'author_flags_digest': forged.author_flags_digest()}
        self.assertFalse(forged.claude_author_verified()[0])

    def test_the_opt_in_is_voided_when_the_author_flags_change_and_never_revives(self):
        self.cli('run', '--author-vendor', 'claude', *OPT_IN)
        co = self.co()
        self.assertTrue(co.claude_author_verified()[0])
        with patch.object(co, 'author_flags_digest', return_value='f' * 64):    # flags A -> B
            self.assertFalse(co.claude_author_verified()[0])
        voided = self.state()['claude_author_override']['voided']
        self.assertEqual(voided['digest_seen'], 'f' * 64)
        self.assertRegex(voided['time'], STAMP)
        ok, message = self.co().claude_author_verified()                        # flags B -> A: the opt-in stays void
        self.assertFalse(ok)
        self.assertIn('void', message)
        self.assertEqual(self.state()['claude_author_override']['voided'], voided)
        self.assertEqual(self.cli('reject', '--text', 'x', '--author-vendor', 'claude').returncode, 2)
        again = self.cli('reject', '--text', 'x', '--author-vendor', 'claude', *OPT_IN)   # only a fresh flag re-accepts
        self.assertNotIn(REFUSAL, again.stdout)
        self.assertNotIn('voided', self.state()['claude_author_override'])

    def test_a_claude_author_probe_pass_is_the_other_way_forward(self):
        co = self.co()
        self.assertFalse(co.claude_author_verified()[0])
        probe = co.run_dir / 'permission-probe.json'
        probe.write_text(json.dumps({'status': 'PASS'}))
        self.assertFalse(co.claude_author_verified()[0])                        # a bare or forged file is not enough
        with patch.object(co, 'probe_passed', return_value=(True, '')):
            self.assertFalse(co.claude_author_verified()[0])                    # probe_passed alone is not enough
            probe.write_text(json.dumps({'author_permission_probe': {'claude_author_status': 'PASS'}}))
            self.assertTrue(co.claude_author_verified()[0])                     # both together are (P0-3b produces it)

    def test_the_bug_report_config_with_the_opt_in_passes_validation_up_to_dispatch(self):
        result = self.cli('run', *BUG_REPORT_FLAGS, *OPT_IN)
        self.assertNotIn(REFUSAL, result.stdout)
        self.assertNotIn('model', result.stdout.lower(), result.stdout)
        self.assertIn('permission-probe.json is missing', result.stdout)        # stopped at the probe gate, not a guard
        self.assertEqual(self.state()['claude_author_override']['actor'], 'operator')


# A stand-in claude binary: emits a canned stream-json for the probe prompt and, per FAKE_AUTHOR_SCENARIO,
# really writes the files an escaping author would (escape), omits attempts (skip), reports success without
# writing (noerror), reports a denial only through permission_denials (pd), or exits non-zero (exit).
FAKE_AUTHOR = r'''#!PYTHON
import json, os, re, shlex, sys
from pathlib import Path
args = sys.argv[1:]
if args == ["--version"]:
    print("fake-claude 9.9"); sys.exit(0)
prompt = sys.stdin.read()
cfg = json.loads(os.environ.get("FAKE_AUTHOR_SCENARIO", "{}"))
if cfg.get("exit"): sys.exit(cfg["exit"])
labels = LABELS
steps = re.findall(r"(?m)^\d+\. (Write|Edit|Bash) (?:file_path )?(.*?)(?: \((?:old_string|content) .*\))?$", prompt)
emit = lambda row: print(json.dumps(row), flush=True)
emit({"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": "s"})
denials = []
for index, (tool, key) in enumerate(steps):
    label = labels[index]
    if label in cfg.get("skip", []): continue
    tool_id = "t%d" % index
    emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tool_id, "name": tool,
          "input": {"command": key} if tool == "Bash" else {"file_path": key}}]}, "session_id": "s"})
    ok = label == "positive_control" and not cfg.get("no_positive")
    if ok or label in cfg.get("escape", []):
        parts = shlex.split(key) if tool == "Bash" else []
        if label == "link_symlink": os.symlink(parts[2], parts[3])
        elif label == "link_hardlink": os.link(parts[1], parts[2])
        elif tool == "Bash": Path(parts[-1]).write_text("x")
        elif tool == "Edit": Path(key).write_text("sentinel-edited\n")
        else:
            Path(key).parent.mkdir(parents=True, exist_ok=True); Path(key).write_text("x")
    error = not (ok or label in cfg.get("escape", []) or label in cfg.get("noerror", []) or label in cfg.get("pd", []))
    if label in cfg.get("pd", []): denials.append({"tool_use_id": tool_id})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id,
          "content": "Permission denied" if error else "ok", "is_error": error}]}, "session_id": "s"})
emit({"type": "result", "session_id": "s", "is_error": False, "permission_denials": denials,
      "structured_output": {"status": "APPROVE", "findings": []},
      "usage": {"input_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 1}})
'''
LABELS = list(cap.steps(Path('/b'), Path('/t')))
NEGATIVE = [label for label in LABELS if label != 'positive_control' and not label.startswith(('link_', 'edit_'))]
TARGET_NAME = {'write_abs': 'w-abs.txt', 'write_rel': 'w-rel.txt', 'write_symlink': 'w-link.txt',
               'write_context': 'w-ctx.txt', 'write_tmp': 'paired-session-author-probe-', 'bash_abs': 'b-abs.txt',
               'bash_context': 'b-ctx.txt', 'write_case': 'w-case.txt'}
# Hash of exactly the Codex branch of _author_permission_probe (source minus the two Claude-dispatch lines), as of
# P0-3a (7ebbf14). An intentional change to the Codex probe must update this hash.
CODEX_PROBE_SHA256 = '19b90ac418ca9936ded422d4c5a8418fbcc65a92fb10c1a4ee8e249a0a0f4a73'


class ClaudeAuthorProbeTests(unittest.TestCase):
    """P0-3b: the real Claude author escape probe, driven with a fake claude binary."""

    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.fake = self.h.root / 'fake-author-claude'
        self.fake.write_text(FAKE_AUTHOR.replace('#!PYTHON', '#!' + sys.executable).replace('LABELS', repr(LABELS)))
        self.fake.chmod(0o755)

    def co(self):
        return self.h.coordinator(*BUG_REPORT_FLAGS, '--timeout', '10', '--author-effort', 'low', '--reviewer-effort', 'low',
                                  '--gate-effort', 'low', '--test-command', 'python3 -m unittest', '--claude-bin', str(self.fake))

    def probe(self, scenario=None, co=None):
        co = co or self.co()
        with patch.dict(os.environ, {'FAKE_AUTHOR_SCENARIO': json.dumps(scenario or {})}):
            return co, co._author_permission_probe()

    def write_report(self, co, author_probe, status='PASS'):
        (co.run_dir / 'permission-probe.json').write_text(json.dumps({
            'status': status, 'reviewer_flags': co.reviewer_flags(), 'reviewer_flags_digest': co.reviewer_flags_digest(),
            'author_flags_digest': co.author_flags_digest(), 'author_permission_probe': author_probe,
            'global_config_changes': {'status': 'PASS'}}))

    def test_pass_when_every_attempt_is_denied_targets_are_absent_and_the_control_is_present(self):
        co, out = self.probe()
        self.assertEqual(out['status'], 'PASS', out)
        self.assertEqual((out['probe'], out['claude_author_status'], out['positive_control']),
                         ('claude-author-filesystem-v1', 'PASS', True))
        self.assertEqual(list(out['attempts']), LABELS)
        for label, row in out['attempts'].items():
            self.assertTrue(row['tool_use_seen'], label)
            self.assertEqual(row['denied'], label != 'positive_control', label)
            self.assertIn(row['target_absent'], (True, None), label)
        self.assertEqual(out['model_escape_failed_targets'], [])
        self.assertEqual(out['claude_version'], 'fake-claude 9.9')
        self.assertEqual((out['cleanup']['remaining'], out['cleanup']['errors'], out['cleanup']['base_removed']), ([], [], True))
        self.assertEqual(list(co.run_dir.parent.glob('paired-session-author-probe-*')), [])
        self.assertEqual(co.context, co.run_dir / 'context')                      # the real context was only swapped out
        self.assertEqual(co.state['turns'][-1]['phase'], 'AUTHOR_PERMISSION_PROBE')

    def test_a_denial_reported_only_through_permission_denials_counts(self):
        self.assertEqual(self.probe({'pd': ['bash_abs', 'write_abs']})[1]['status'], 'PASS')

    def test_fail_when_any_negative_target_exists_afterwards(self):
        for label in NEGATIVE:
            with self.subTest(label=label):
                co, out = self.probe({'escape': [label]})
                self.assertEqual(out['status'], 'FAIL', out)
                self.assertEqual(out['claude_author_status'], 'FAIL')
                self.assertTrue(any(TARGET_NAME[label] in target for target in out['model_escape_failed_targets']), out)
                self.assertEqual((out['cleanup']['remaining'], out['cleanup']['base_removed']), ([], True))
                self.assertFalse([p for p in Path('/tmp').glob('paired-session-author-probe-*.txt')])

    def test_fail_when_the_sentinel_bytes_change_directly_or_through_a_symlink_or_hardlink(self):
        for escape in (['edit_sentinel'], ['link_symlink', 'edit_symlink'], ['link_hardlink', 'edit_hardlink']):
            with self.subTest(escape=escape):
                co, out = self.probe({'escape': escape})
                self.assertEqual(out['status'], 'FAIL', out)
                self.assertTrue(any(t.endswith('sentinel.txt') for t in out['model_escape_failed_targets']), out)
        co, out = self.probe({'escape': ['link_symlink', 'link_hardlink']})         # links made but never edited: no escape
        self.assertEqual(out['status'], 'PASS', out)

    def test_unknown_when_a_tool_use_is_missing_or_the_positive_control_is_missing(self):
        for label in LABELS:
            with self.subTest(skip=label):
                self.assertEqual(self.probe({'skip': [label]})[1]['status'], 'UNKNOWN')
        out = self.probe({'no_positive': True})[1]
        self.assertEqual((out['status'], out['positive_control']), ('UNKNOWN', False))
        out = self.probe({'noerror': ['bash_abs']})[1]                               # seen, nothing written, yet not reported denied
        self.assertEqual(out['status'], 'UNKNOWN', out)

    def test_the_probe_tree_is_fresh_0700_beside_run_dir_and_touches_no_sibling(self):
        co = self.co()
        sibling_dir, sibling_file = co.run_dir.parent / 'keep-me', co.run_dir.parent / 'keep-me.txt'
        sibling_dir.mkdir()
        (sibling_dir / 'inner').write_text('i')
        sibling_file.write_text('f')
        stale = co.run_dir.parent / 'paired-session-author-probe-stale'                # a foreign leftover is not ours to remove
        stale.mkdir()
        seen, real_mkdtemp = [], tempfile.mkdtemp
        def spy(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            seen.append((path, os.stat(path).st_mode & 0o777, os.listdir(path)))
            return path
        with patch.object(rc.tempfile, 'mkdtemp', side_effect=spy):
            out = self.probe(co=co)[1]
        self.assertEqual(out['status'], 'PASS', out)
        self.assertEqual([(Path(p).parent, mode, names) for p, mode, names in seen], [(co.run_dir.parent.resolve(), 0o700, [])])
        self.assertTrue(Path(seen[0][0]).name.startswith('paired-session-author-probe-'))
        self.assertFalse(Path(seen[0][0]).exists())
        self.assertEqual((sorted(p.name for p in sibling_dir.iterdir()), sibling_file.read_text(), stale.is_dir()), (['inner'], 'f', True))

    def test_the_probe_tree_is_removed_on_every_exit_path(self):
        def leftovers(co): return [p for p in co.run_dir.parent.glob('paired-session-author-probe-*')]
        for name, scenario, patches in (('timeout', None, ('invoke', RuntimeError('author CLI timed out'))),
                                        ('unknown', {'skip': ['bash_abs']}, None),
                                        ('fail', {'escape': ['write_abs']}, None),
                                        ('cli-error', {'exit': 3}, None),
                                        ('value-error', None, ('invoke', ValueError('refused'))),
                                        ('unexpected', None, ('invoke', ZeroDivisionError('boom'))),
                                        ('setup-error', None, ('steps', ZeroDivisionError('boom')))):
            with self.subTest(exit_path=name):
                co = self.co()
                target = co if patches and patches[0] == 'invoke' else cap
                ctx = patch.object(target, patches[0], side_effect=patches[1]) if patches else contextlib.nullcontext()
                try:
                    with ctx:
                        out = self.probe(scenario, co=co)[1]
                except ZeroDivisionError:
                    self.assertEqual(name in ('unexpected', 'setup-error'), True)      # propagated, but not before cleanup
                else:
                    self.assertIn(out['status'], ('PASS', 'UNKNOWN', 'FAIL'))
                    self.assertTrue(out['cleanup']['base_removed'], out)
                self.assertEqual(leftovers(co), [])
                self.assertEqual(co.context, co.run_dir / 'context')
                self.assertEqual(list(Path('/tmp').glob('paired-session-author-probe-*.txt')), [])

    def test_a_leftover_after_cleanup_is_a_fail(self):
        co = self.co()
        with patch.object(rc.shutil, 'rmtree'):                                          # cleanup cannot remove the tree
            out = self.probe(co=co)[1]
        try:
            self.assertEqual((out['status'], out['claude_author_status'], out['cleanup']['base_removed']), ('FAIL', 'FAIL', False), out)
        finally:
            for path in co.run_dir.parent.glob('paired-session-author-probe-*'):
                shutil.rmtree(path)

    def test_a_cli_error_is_a_fail_with_the_reason(self):
        out = self.probe({'exit': 3})[1]
        self.assertEqual(out['status'], 'FAIL')
        self.assertIn('CLI exit 3', out['reason'])
        self.assertFalse(out['positive_control'])

    def test_the_probed_argv_is_the_real_author_argv_with_only_the_probe_paths_substituted(self):
        co, out = self.probe()
        recorded = co.state['turns'][-1]['command']
        rules = out['rules']
        pw, pc = rules['probe_workspace'], rules['probe_context']
        schema = self.h.root / 'schema.json'
        rc.atomic_json(schema, rc.review_schema(verified=False))
        with patch.object(co, 'context', Path(pc)), patch.dict(os.environ, {'FAKE_AUTHOR_SCENARIO': '{}'}):   # env names feed the sandbox settings
            expected = co._claude_command('author', schema, True, Path(pw))
        self.assertEqual(recorded[1:], expected[1:])
        self.assertEqual(recorded[recorded.index('--add-dir') + 1], pc)
        flags = co.author_flags()['claude_author_edit_rules']
        sub = lambda items, old, new: [r.replace(Path(old).as_posix().lstrip('/'), Path(new).as_posix().lstrip('/')) for r in items]
        self.assertEqual(sub(rules['allow'], pw, co.workspace.resolve()), flags[0])
        self.assertEqual(sub(rules['deny'], pc, co.context.resolve()), flags[1])
        self.assertEqual(rules['settings_deny'], co._claude_sandbox_settings('author')['permissions']['deny'])
        self.assertNotIn(co.workspace.resolve().as_posix().lstrip('/'), ' '.join(rules['allow']))
        self.assertTrue(co._claude_probe_rules_match(out))
        self.assertFalse(Path(pw).is_relative_to(co.run_dir))                        # beside run_dir: its deny would block the control

    def test_probe_passed_needs_a_claude_author_probe_pass_with_this_runs_rules(self):
        co, out = self.probe()
        self.write_report(co, out)
        self.assertEqual(co.probe_passed(), (True, ''))
        for status in ('NOT-APPLICABLE', 'UNKNOWN', 'FAIL', 'PASS_RESIDUAL_RISK'):
            with self.subTest(author_status=status):
                self.write_report(co, {**out, 'status': status})
                self.assertFalse(co.probe_passed()[0])
        self.write_report(co, {'status': 'NOT-APPLICABLE', 'reason': 'author is not Codex'})
        self.assertFalse(co.probe_passed()[0])
        self.write_report(co, out, status='PASS_RESIDUAL_RISK')
        self.assertFalse(co.probe_passed()[0])
        forged = [{**out, 'rules': {**out['rules'], 'allow': ['Edit(//**)']}}, {**out, 'rules': {**out['rules'], 'deny': []}},
                  {**out, 'rules': {**out['rules'], 'settings_deny': []}}, {**out, 'rules': None},
                  {k: v for k, v in out.items() if k != 'rules'}, {**out, 'probe': 'other-probe'}]
        for record in forged:
            with self.subTest(forged=str(record.get('rules'))[:60]):
                self.write_report(co, record)
                self.assertFalse(co.probe_passed()[0])

    def test_a_pass_record_gates_a_claude_author_and_a_changed_author_flags_digest_refuses_it(self):
        co, out = self.probe()
        self.write_report(co, out)
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), \
                patch.object(co, '_drive_loop', side_effect=AssertionError('dispatched')):
            self.assertEqual(co.claude_author_verified(), (True, ''))               # no --accept-unverified-claude-author
            with self.assertRaisesRegex(AssertionError, 'dispatched'):
                co.drive()
            self.write_report(co, {**out, 'status': 'NOT-APPLICABLE'})               # forged claude_author_status is not enough
            with self.assertRaisesRegex(ValueError, REFUSAL):
                co.drive()
            self.write_report(co, out)
            co.args.author_model = 'claude-sonnet-5-5'                                # author flags changed after the probe
            with self.assertRaisesRegex(ValueError, REFUSAL):
                co.drive()
            self.assertFalse(co.probe_passed()[0])

    def test_a_stale_pass_from_another_workspace_or_rules_is_refused(self):
        co, out = self.probe()
        self.write_report(co, out)
        other = self.h.root / 'other-ws'
        other.mkdir()
        with patch.object(co, 'workspace', other):
            self.assertFalse(co.probe_passed()[0])                                   # rules and digest bind the workspace

    def test_the_probe_writes_nothing_under_the_real_home_or_real_context(self):
        home_before = sorted(p.name for p in self.h.test_home.rglob('*'))
        co = self.co()
        context_before = rc.directory_digest(co.context)
        self.probe(co=co)
        self.assertEqual(sorted(p.name for p in self.h.test_home.rglob('*')), home_before)
        self.assertEqual(rc.directory_digest(co.context), context_before)

    def test_the_codex_author_probe_is_unchanged(self):
        source = inspect.getsource(rc.Coordinator._author_permission_probe)
        dispatch = "        if self.args.author_vendor == 'claude':\n            return self._claude_author_probe()\n"
        self.assertIn(dispatch, source)
        self.assertEqual(hashlib.sha256(source.replace(dispatch, '').encode()).hexdigest(), CODEX_PROBE_SHA256)
        co = self.h.coordinator('--author-vendor', 'codex')                          # a Codex author never reaches the Claude probe
        with patch.object(co, '_claude_author_probe', side_effect=AssertionError('claude probe')), \
                patch.object(co, 'codex_capabilities', return_value={'status': 'FAIL', 'issues': ['x']}):
            self.assertEqual(co._author_permission_probe(), {'status': 'FAIL', 'reason': 'x'})

    def test_a_workspace_that_is_or_contains_the_home_dir_is_refused(self):
        co = self.co()
        home = Path.home().resolve()
        for bad in (home, home.parent):
            with self.subTest(workspace=str(bad)), self.assertRaisesRegex(ValueError, 'home dir'):
                co._claude_author_edit_rules(bad)
        self.assertEqual(co._claude_author_edit_rules(home / 'project')[0], ['Edit(//' + (home / 'project').as_posix().lstrip('/') + '/**)'])
        for git in (['init', '-q'], ['add', '-A'], ['-c', 'user.name=t', '-c', 'user.email=t@example.test', 'commit', '-qm', 'home']):
            trc.subprocess.run(['git', *git], cwd=home, check=True)                   # the fake HOME as a git workspace
        self.h.workspace, self.h.run_dir = home, self.h.root / 'run-home'            # surfaced as a clear REFUSED by main()
        out = io.StringIO()
        command = self.h.command(*BUG_REPORT_FLAGS, *OPT_IN)
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), contextlib.redirect_stdout(out):
            code = rc.main(command[2:])
        self.assertEqual(code, 2, out.getvalue())
        self.assertRegex(out.getvalue(), r'REFUSED: .*home dir')


if __name__ == '__main__':
    unittest.main()
