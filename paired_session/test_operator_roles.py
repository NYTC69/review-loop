"""Operator surfaces: role models (P0-1, ADR-9), the Claude author file rules, and the v3.7.0 removal of permission-probe and its waivers."""
import contextlib
import io
import json
import os
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from paired_session.lifecycle_test_helpers import use_lifecycle_on

from paired_session import timeout_scale as tsc
from paired_session import test_real_coordinator as trc

rc = trc.rc
UNVERIFIED = 'codex-cli 0.159.2'   # a codex-cli version the removed verified-version list never named


class OperatorCliTests(unittest.TestCase):
    """The operator command line on a real operator machine (the fake-harness bypass off). Other modules reuse setUp, co, cli
    and state."""

    def setUp(self):
        # Reuse the real-coordinator fixture (fake CLIs, HOME, guards) without collecting its tests.
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def co(self, *extra):
        # Same flags as self.h.command() so a CLI call can restore this coordinator's saved run.
        return self.h.coordinator('--gate-vendor', 'claude', *extra, '--timeout', tsc.scaled_arg(10), '--author-effort', 'low',
                                  '--reviewer-effort', 'low', '--gate-effort', 'low', '--test-command', 'python3 -m unittest')

    def cli(self, action, *extra, drive=False):
        """In-process main() with the fake-harness bypass off, as on a real operator machine; drive/resume are stubbed unless drive."""
        command = self.h.command('--gate-vendor', 'claude', *extra)   # explicit gate: default moved by owner decision 2026-09-30
        command[2] = action
        out, err = io.StringIO(), io.StringIO()
        stubs = contextlib.ExitStack()
        if not drive:
            stubs.enter_context(patch.object(rc.Coordinator, 'drive', return_value='DONE'))
            stubs.enter_context(patch.object(rc.Coordinator, 'resume', return_value='DONE'))
        with stubs, patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rc.main(command[2:])
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())

    def state(self):
        path = self.h.run_dir / 'state.json'
        return json.loads(path.read_text()) if path.exists() else {}

    # ---- v3.7.0: permission-probe and its waivers are gone ----

    def test_permission_probe_is_refused_before_any_state_or_run_dir(self):
        for extra in ((), ('--detach',), ('--strict',)):
            with self.subTest(extra=extra):
                result = self.cli('permission-probe', *extra)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(result.stdout.strip(), 'REFUSED: permission-probe was removed in v3.7.0; runs do not need it')
                self.assertFalse(self.h.run_dir.exists())

    def test_permission_probe_on_a_saved_run_is_refused_and_records_nothing(self):
        self.co()
        before = (self.h.run_dir / 'state.json').read_bytes()
        result = self.cli('permission-probe')
        self.assertEqual(result.returncode, 2)
        self.assertIn('permission-probe was removed in v3.7.0', result.stdout)
        self.assertEqual((self.h.run_dir / 'state.json').read_bytes(), before)
        self.assertFalse((self.h.run_dir / 'permission-probe.json').exists())

    def test_the_removed_waivers_are_refused_with_the_new_text(self):
        for flag in ('--accept-unverified-codex-cli', '--accept-unverified-claude-author', '--accept-probe-skip'):
            with self.subTest(flag=flag), patch.dict(os.environ, {'PAIRED_SESSION_INTERNAL_TEST_REMOVED_OPTIONS': '0'}):
                result = self.cli('run', flag, '--reason', 'x')
                self.assertEqual(result.returncode, 2)
                self.assertIn(f'REFUSED: removed user option: {flag}; removed in v3.7.0; runs need no probe or waiver', result.stdout)
                self.assertFalse((self.h.run_dir / 'state.json').exists())

    def test_skip_probe_is_an_accepted_hidden_no_op_on_a_real_command_line(self):
        result = self.cli('run', '--skip-probe')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('--skip-probe', rc.parser().format_help())
        self.assertNotIn('permission-probe', rc.parser().format_help())

    def test_a_real_run_dispatches_without_any_probe_or_codex_version_check(self):
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': UNVERIFIED}):
            result = self.cli('run', '--shadow', 'off', '--adversarial-gate', 'off', drive=True)
        self.assertNotIn('permission', result.stdout)
        self.assertNotIn('unverified codex sandbox contract', result.stdout)
        self.assertTrue(self.state()['turns'], result.stdout)

    def test_operator_only_flags_are_never_restored_from_saved_state(self):
        co = self.co()
        state = json.loads((co.run_dir / 'state.json').read_text())
        state['config'].update({'accept_unverified_codex_cli': True, 'reason': 'injected', 'accept_future': True})
        (co.run_dir / 'state.json').write_text(json.dumps(state))
        argv = self.h.command('--gate-vendor', 'claude')[2:]
        argv[0] = 'note'
        args = rc.normalize_cli_paths(rc.configure_parser(rc.parser(), argv + ['--text', 'x']).parse_args(argv + ['--text', 'x']))
        rc.Coordinator(args)
        self.assertFalse(args.accept_unverified_codex_cli)
        self.assertIsNone(args.reason)
        self.assertEqual(rc.OPERATOR_ONLY_DESTS,
                         {'accept_unverified_codex_cli', 'accept_unverified_claude_author', 'accept_probe_skip', 'reason'})

    def test_operator_only_keys_are_refused_in_a_config_file(self):
        config = self.h.workspace / '.review-loop' / 'paired-session.json'
        config.parent.mkdir()
        for key in ('accept_unverified_codex_cli', 'reason', 'accept_anything'):
            with self.subTest(key=key):
                config.write_text(json.dumps({key: 'x'}))
                self.assertIn('unsupported paired-session config keys', self.cli('run').stdout)

    def test_reason_is_only_for_accept_and_withdraw_findings(self):
        for action in ('run', 'resume'):
            with self.subTest(action=action):
                result = self.cli(action, '--reason', 'why')
                self.assertEqual(result.returncode, 2)
                self.assertIn('REFUSED: --reason is only for accept and withdraw-findings', result.stdout)

    def test_a_run_saved_with_probe_records_resumes_and_ignores_them(self):
        for mode in ('off', 'on'):
            with self.subTest(lifecycle=mode):
                self.h.run_dir = self.h.root / ('saved-' + mode)
                co = self.co('--lifecycle-mode', mode)
                state = json.loads((co.run_dir / 'state.json').read_text())
                state.update({'permission_probe': {'sha256': '0' * 64, 'turn': 1}, 'permission_probe_superseded': {'sha256': '1' * 64},
                              'probe_skip_override': {'actor': 'operator', 'reason': 'x'},
                              'claude_author_override': {'actor': 'operator', 'reason': 'x'}, 'residual_risk': 'old text'})
                state['config']['safety_mode'] = 'strict'
                (co.run_dir / 'state.json').write_text(json.dumps(state))
                result = self.cli('resume', '--lifecycle-mode', mode)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout.count('NOTE: strict mode was removed in v3.7.0; the run uses the default mode'), 1)
                self.assertNotIn('REFUSED', result.stdout)
                self.assertNotIn('probe', result.stdout.replace('probe_skip_override', ''))


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
        use_lifecycle_on(self, self.h)
        args = self.resolved(*BUG_REPORT_FLAGS)
        rc.validate_role_models(args)
        self.assertEqual((args.author_model, args.reviewer_model, args.gate_model, args.gate_vendor),
                         ('claude-opus-5-5', 'gpt-6.1-sol', 'gpt-6.1-sol', 'codex'))
        # main() may still refuse the Claude author (P0-3), but never on a model.
        refused = self.main('run', *BUG_REPORT_FLAGS)
        self.assertNotIn('model', refused.stdout.lower().replace('claude author', ''), refused.stdout)

    def test_defaults_are_unchanged(self):
        for flags, expected in (
                ([], ('codex', 'gpt-6.1-sol', 'claude', 'claude-opus-5-5', 'codex', 'gpt-6.1-sol')),   # gate: 2026-09-30; Codex model: ADR-12
                (['--author-vendor', 'claude', '--reviewer-vendor', 'codex'],
                 ('claude', 'claude-opus-5-5', 'codex', 'gpt-6.1-sol', 'claude', 'claude-opus-5-5'))):   # gate: 2026-09-30; Codex model: ADR-12
            with self.subTest(flags=flags):
                a = self.resolved(*flags)
                rc.validate_role_models(a)
                self.assertEqual((a.author_vendor, a.author_model, a.reviewer_vendor, a.reviewer_model,
                                  a.gate_vendor, a.gate_model), expected)

    def test_gate_vendor_may_equal_the_reviewer_or_the_author(self):
        for flags, vendor, model in ((['--gate-vendor', 'codex'], 'codex', 'gpt-6.1-sol'),
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
        with self.assertRaisesRegex(ValueError, 'reviewer_model gpt-6.1-sol is not in allowed_models'):
            rc.validate_role_models(a)
        for bad in ([], 'gpt-6', {'codex': 'gpt-6-luna'}, {'codex': [1]}, {'mistral': ['x']}, {'codex': None}):   # gemini is a vendor key since ADR-21 (WI-GEM B1)
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
                                                 (['--gate-vendor', 'claude'], 'claude'), ([], 'codex'))):   # default moved by owner decision 2026-09-30
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
        use_lifecycle_on(self, self.h)
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
        use_lifecycle_on(self, self.h)
        path = self.allowlist_run(None, '--gate-vendor', 'claude')   # explicit: the gate_model swap below needs a claude gate
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
        use_lifecycle_on(self, self.h)
        self.h.coordinator()
        state = self.state()
        state['config'].pop('allowed_models')
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        self.assertIsNone(self.restored(action='resume').allowed_models)
        self.assertIsNone(self.restored().allowed_models)

    def test_the_scope_change_successor_inherits_the_allowlist(self):
        use_lifecycle_on(self, self.h)
        self.allowlist_run()
        self.assertIn('Start:', self.main('note', '--scope-change', '--text', 'narrow it').stdout)
        config = json.loads((self.h.run_dir / 'evidence' / 'successor-config.json').read_text())
        self.assertEqual(config['allowed_models'], self.ALLOWED)
        target = self.h.run_dir.with_name(self.h.run_dir.name + '-successor')
        successor = ['--workitem', str(self.h.run_dir / 'evidence' / 'successor-workitem.md'),
                     '--run-dir', str(target), '--supersedes', str(self.h.run_dir)]
        other = self.h.root / 'other-successor-policy.json'
        other.write_text(json.dumps({'allowed_models': {'codex': ['gpt-6.1-sol'], 'claude': ['claude-opus-5-5']}}))
        for extra in ([], ['--config', str(other)]):        # no config, or another allowlist: not this run's successor
            with self.subTest(extra=extra):
                self.assertIn('successor spec or parent state differs', self.main('run', *successor, *extra).stdout)
                self.assertFalse((target / 'state.json').exists())
        kept = self.main('run', *successor, '--config', str(self.h.run_dir / 'evidence' / 'successor-config.json'))
        self.assertNotIn('successor spec or parent state differs', kept.stdout)
        self.assertEqual(self.state_at(target)['config']['allowed_models'], self.ALLOWED)

    def test_a_plain_resume_of_a_non_default_model_run_works(self):
        use_lifecycle_on(self, self.h)
        self.h.coordinator('--gate-model', 'gpt-6.1-sol', '--reviewer-model', 'claude-sonnet-5-5')
        for action in ('resume', 'run'):
            a = self.restored(action=action)
            self.assertEqual((a.gate_model, a.reviewer_model), ('gpt-6.1-sol', 'claude-sonnet-5-5'))
        with self.assertRaisesRegex(ValueError, 'role models are fixed for this run: gate_model'):
            self.restored('--gate-model', 'claude-opus-5-5', action='resume', explicit={'gate_model'})
        # An allowlist naming only the saved models must not trip over the CLI defaults on a plain resume.
        self.h.run_dir = self.h.root / 'allow-resume'
        policy = self.allowlist_run({'codex': ['gpt-6.1-sol'], 'claude': ['claude-sonnet-5-5']}, '--author-model',
                           'gpt-6.1-sol', '--reviewer-model', 'claude-sonnet-5-5', '--gate-model', 'claude-sonnet-5-5', '--gate-vendor', 'claude')   # explicit: the default gate moved to the author's vendor
        resumed = self.main('resume', '--config', str(policy)).stdout
        for text in ('allowed_models', 'role models are fixed', 'role policy is fixed'):
            self.assertNotIn(text, resumed)

    def test_a_gate_vendor_that_differs_from_the_reviewer_is_not_refused(self):   # v3.7.0: no gate probe exists to cover it
        for flags in ([], ['--gate-vendor', 'codex'], ['--reviewer-vendor', 'codex', '--gate-vendor', 'codex'],
                      ['--reviewer-vendor', 'claude', '--gate-vendor', 'claude']):
            with self.subTest(flags=flags):
                self.assertIsNone(rc.gate_surface_issue(self.resolved(*flags)))
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False):
            self.assertIsNone(rc.gate_surface_issue(self.resolved(*BUG_REPORT_FLAGS)))
            self.assertIsNone(rc.gate_surface_issue(self.resolved('--author-vendor', 'claude')))
        self.assertEqual(set(self.h.coordinator('--gate-vendor', 'claude').gate_flags()) - set(self.h.coordinator('--gate-vendor', 'claude').reviewer_flags()),
                         {'gate_vendor', 'gate_model', 'gate_effort', 'gate_commands', 'gate_binary'})

    # ---- G-b / ADR-10: the gate defaults to the author's vendor ----
    def test_the_gate_defaults_to_the_authors_vendor_and_records_the_source(self):   # G-b M1/M2
        policy = self.h.root / 'gate-policy.json'
        policy.write_text(json.dumps({'gate_vendor': 'claude'}))
        for index, (flags, expected) in enumerate((
                ([], ('codex', 'gpt-6.1-sol', 'default')),
                (['--author-vendor', 'claude', '--reviewer-vendor', 'codex'], ('claude', 'claude-opus-5-5', 'default')),
                (['--gate-vendor', 'claude'], ('claude', 'claude-opus-5-5', 'operator')),
                (['--author-vendor', 'claude', '--reviewer-vendor', 'codex', '--gate-vendor', 'codex'], ('codex', 'gpt-6.1-sol', 'operator')),
                (['--config', str(policy)], ('claude', 'claude-opus-5-5', 'operator')))):      # a config key is an operator choice too
            with self.subTest(flags=flags):
                self.h.run_dir = self.h.root / f'gate-default-{index}'
                co = rc.Coordinator(self.args(*flags))
                config = co.state['config']
                self.assertEqual((config['gate_vendor'], config['gate_model'], config['gate_vendor_source']), expected)
                self.assertEqual((co._role_vendor('gate'), co.args.gate_model), expected[:2])
        self.h.run_dir = self.h.root / 'gate-default-e2e'
        result = self.h.run_coordinator('--shadow', 'off')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('GATE: codex gpt-6.1-sol (gate_vendor_source: default)', result.stdout)
        gates = [t for t in self.state()['turns'] if t['role'] == 'gate']
        self.assertTrue(gates and all(t['vendor'] == 'codex' and t['model'] == 'gpt-6.1-sol' for t in gates))

    def test_a_gate_model_of_the_other_vendor_is_refused_before_state_unless_the_vendor_is_explicit(self):   # G-b M4
        use_lifecycle_on(self, self.h)
        bob = ['--author-vendor', 'claude', '--reviewer-vendor', 'codex', '--gate-model', 'gpt-6.1-sol']
        message = ("REFUSED: gate_model gpt-6.1-sol belongs to codex, but the gate now defaults to the author's vendor claude; "
                   "pass --gate-vendor codex to keep it")
        for action in ('run', 'resume'):   # v3.7.0: permission-probe is gone (refused by name before this check)
            with self.subTest(action=action):
                refused = self.main(action, *bob)
                self.assertEqual(refused.returncode, 2, refused.stdout)
                self.assertIn(message, refused.stdout)
                self.assertFalse(self.h.run_dir.exists())
        for flags, text in ((['--gate-vendor', 'claude', '--gate-model', 'gpt-6.1-sol'], 'belongs to codex, but the gate vendor is claude'),
                            (['--author-vendor', 'claude', '--gate-vendor', 'codex', '--gate-model', 'claude-opus-5-5'],
                             'belongs to claude, but the gate vendor is codex')):
            with self.subTest(flags=flags):
                refused = self.main('run', *flags)
                self.assertEqual(refused.returncode, 2, refused.stdout)
                self.assertIn(text, refused.stdout)
                self.assertFalse(self.h.run_dir.exists())
        policy = self.h.root / 'known-ids.json'      # an id the allowlist assigns to the other vendor counts as known
        policy.write_text(json.dumps({'allowed_models': {'codex': ['house-model'], 'claude': ['claude-opus-5-5']}, 'gate_model': 'house-model'}))
        with self.assertRaisesRegex(ValueError, 'belongs to codex'):
            self.resolved('--config', str(policy), '--author-vendor', 'claude')
        a = self.resolved(*bob, '--gate-vendor', 'codex')
        self.assertEqual((a.gate_vendor, a.gate_model, a.gate_vendor_source), ('codex', 'gpt-6.1-sol', 'operator'))
        accepted = self.main('run', *bob, '--gate-vendor', 'codex')
        self.assertNotIn('belongs to', accepted.stdout)
        self.assertTrue((self.h.run_dir / 'state.json').exists(), accepted.stdout)




class ClaudeAuthorTests(unittest.TestCase):
    """P0-3a: path-scoped Claude author file rules; since v3.7.0 a real Claude author needs no probe or opt-in."""

    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def co(self, *extra):
        # Same flags as self.h.command() so a CLI call can restore this coordinator's saved run.
        return self.h.coordinator(*BUG_REPORT_FLAGS, '--timeout', tsc.scaled_arg(10), '--author-effort', 'low',
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
        use_lifecycle_on(self, self.h)
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
        use_lifecycle_on(self, self.h)
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


    def test_the_edit_rules_refuse_a_workspace_in_a_denied_root_or_the_filesystem_root(self):
        use_lifecycle_on(self, self.h)
        co = self.co()
        for bad in (Path('/'), co.context / 'ws', co.run_dir / 'ws', Path.home() / '.aws',
                    Path.home() / '.codex' / 'p'):
            with self.assertRaisesRegex(ValueError, 'workspace must not'):
                co._claude_author_edit_rules(bad)
        allow = co._claude_author_edit_rules(Path('/x'))[0]
        self.assertEqual(allow, ['Edit(//x/**)'])
        with patch.object(co, 'workspace', Path('/')):                          # surfaced as a clear REFUSED by main()
            with self.assertRaisesRegex(ValueError, 'filesystem root'):
                co._claude_author_edit_rules()

    def test_every_allow_rule_is_joined_into_the_author_allowed_tools(self):
        use_lifecycle_on(self, self.h)
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

    def test_a_real_claude_author_needs_no_probe_or_opt_in(self):   # v3.7.0: the probe and the fake-harness-only rule are gone
        result = self.cli('run', '--shadow', 'off', '--adversarial-gate', 'off')
        self.assertNotIn('limited to the fake test harness', result.stdout)
        self.assertNotIn('permission-probe', result.stdout)
        self.assertTrue(self.state()['turns'], result.stdout)


if __name__ == '__main__':
    unittest.main()
