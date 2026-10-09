"""D-EFF (docs/efficient-mode.md, owner 2026-10-04, amended): the run-level safety_mode. Efficient (the default) drops only category C
(the permission-probe PASS before dispatch, a holding evidence guard); every sandbox flag and every category A/B check stays. Strict is
today's behaviour. The shared harness pins strict for the existing tests; these tests put the efficient default back."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_operator_roles as tor
from paired_session import test_real_coordinator as trc

rc = trc.rc


class EfficientModeTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        for module in {id(m): m for m in (rc, sys.modules.get('paired_session.coordinator')) if m}.values():
            pin = patch.object(module, 'DEFAULT_SAFETY_MODE', 'efficient')   # the product default, which the harness pins to strict
            pin.start()
            self.addCleanup(pin.stop)

    def command(self, *extra, strict=False):
        return [a for a in self.h.command(*extra) if strict or a != '--strict']

    def state(self, run_dir=None):
        return json.loads(((run_dir or self.h.run_dir) / 'state.json').read_text())

    def run_efficient(self, *extra, env=None):   # a real coordinator process, without --strict and without --skip-probe
        return subprocess.run(self.command(*extra), cwd=self.h.root, env={**os.environ, **(env or {})}, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def cli(self, action, *extra, strict=False):
        """In-process main() with the fake-harness bypass off, as on an operator machine; the drive itself is not under test."""
        command = self.command(*extra, strict=strict)
        command[2] = action
        out = io.StringIO()
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), contextlib.redirect_stdout(out), \
                patch.object(rc.Coordinator, 'drive', return_value='DONE'), patch.object(rc.Coordinator, 'resume', return_value='DONE'):
            code = rc.main(command[2:])
        return code, out.getvalue()

    def other_run(self, name):
        self.h.run_dir = self.h.root / name

    # --- selection ------------------------------------------------------------------------------------------------------------------
    def test_the_default_is_efficient_and_the_mode_is_frozen_at_creation(self):
        co = self.h.coordinator()
        self.assertFalse(co.strict)
        self.assertEqual(co.state['config']['safety_mode'], 'efficient')
        self.assertFalse(self.h.coordinator().strict)              # a saved run keeps its mode
        state = self.state()
        state['turns'] = [{'sequence': 1, 'role': 'probe', 'phase': 'PROBE'}]
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        self.assertTrue(self.h.coordinator('--strict').strict)    # only the probe has run (it creates the state): the run may start strict
        self.assertEqual(self.state()['config']['safety_mode'], 'strict')
        self.other_run('dispatched-run')
        self.h.coordinator()
        state = self.state()
        state['turns'] = [{'sequence': 1, 'role': 'author', 'phase': 'PLAN'}]
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'safety_mode is fixed for this run: it was created efficient'):
            self.h.coordinator('--strict')                         # never changed silently once a model turn ran
        self.other_run('strict-run')
        self.assertTrue(self.h.coordinator('--strict').strict)
        self.assertEqual(self.state()['config']['safety_mode'], 'strict')
        self.assertTrue(self.h.coordinator().strict)

    def test_an_operator_profile_selects_strict_and_the_workspace_config_cannot(self):
        base = ['run', '--workspace', str(self.h.workspace), '--workitem', str(self.h.workitem), '--run-dir', str(self.h.run_dir)]
        profile = self.h.root / 'operator-profile.json'
        for value, extra, desired in (('strict', [], 'strict'), ('efficient', [], 'efficient'), ('efficient', ['--strict'], 'strict')):
            with self.subTest(value=value, extra=extra):
                profile.write_text(json.dumps({'safety_mode': value}))
                argv = [*base, '--config', str(profile), *extra]
                self.assertEqual(rc.configure_parser(rc.parser(), argv).parse_args(argv).safety_mode, desired)
        profile.write_text(json.dumps({'safety_mode': 'yolo'}))
        with self.assertRaisesRegex(ValueError, 'safety_mode is set only by --strict or an operator --config profile'):
            rc.configure_parser(rc.parser(), [*base, '--config', str(profile)])
        workspace_config = self.h.workspace / '.review-loop' / 'paired-session.json'
        workspace_config.parent.mkdir()
        workspace_config.write_text(json.dumps({'safety_mode': 'strict'}))
        with self.assertRaisesRegex(ValueError, 'never by the workspace config'):
            rc.configure_parser(rc.parser(), base)

    def test_an_author_writable_profile_cannot_set_the_mode(self):   # eff-e: workspace, run dir or author temp, explicit --config too
        base = ['run', '--workspace', str(self.h.workspace), '--workitem', str(self.h.workitem), '--run-dir', str(self.h.run_dir)]
        self.h.coordinator()                                              # an efficient run that has only probed so far
        state = self.state()
        state['turns'] = [{'sequence': 1, 'role': 'probe', 'phase': 'PROBE'}]
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        (self.h.run_dir / 'author-tmp').mkdir(exist_ok=True)
        for where in (self.h.workspace / 'profile.json', self.h.run_dir / 'profile.json', self.h.run_dir / 'author-tmp' / 'profile.json'):
            for value in ('efficient', 'strict'):                         # the selection, and the probe-only upgrade
                with self.subTest(where=where, value=value):
                    where.write_text(json.dumps({'safety_mode': value}))
                    with self.assertRaisesRegex(ValueError, 'outside the workspace, run dir and author temp'):
                        rc.configure_parser(rc.parser(), [*base, '--config', str(where)])
        self.assertEqual(self.state()['config']['safety_mode'], 'efficient')   # never upgraded through such a profile
        self.assertTrue(self.h.coordinator('--strict').strict)                # the CLI still may

    def test_mode_edges_from_the_eff_a_review(self):
        use_lifecycle_on(self, self.h)
        self.other_run('active-plan')
        self.h.coordinator()
        state = self.state()
        state['active'] = {'sequence': 1, 'role': 'author', 'phase': 'PLAN'}   # a model turn that never reached `turns`
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'safety_mode is fixed for this run'):
            self.h.coordinator('--strict')
        argv = [*self.command(), '--strict', '--text', 'a note']
        argv[2] = 'note'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            co = rc.Coordinator(rc.parser().parse_args(argv[2:]))   # a command that dispatches no turn: noted, not refused
        self.assertFalse(co.strict)
        self.assertIn('NOTE: safety_mode is fixed for this run', out.getvalue())
        self.assertIn('drop safety_mode from the --config profile', out.getvalue())

    # --- category C: dropped in efficient -------------------------------------------------------------------------------------------
    def test_no_permission_probe_or_author_opt_in_is_needed_in_efficient_mode(self):
        for extra in ([], ['--author-vendor', 'claude']):
            with self.subTest(extra=extra):
                self.other_run('efficient-' + '-'.join(extra or ['codex']))
                code, out = self.cli('run', *extra)
                self.assertEqual(code, 0, out)
                self.assertNotIn('permission-probe.json is missing', out)
                self.assertNotIn(tor.REFUSAL, out)
                self.other_run('strict-' + '-'.join(extra or ['codex']))
                code, out = self.cli('run', *extra, strict=True)
                self.assertEqual(code, 2)
                self.assertTrue('permission-probe.json is missing' in out or tor.REFUSAL in out, out)

    def test_a_real_efficient_run_needs_no_probe_and_its_evidence_guard_only_logs(self):
        secret = str(self.h.run_dir / 'evidence' / 'secret.json')
        result = self.run_efficient('--shadow', 'off', '--adversarial-gate', 'off', env={'FAKE_SENSITIVE_READ': secret})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.state()
        self.assertNotIn('hold_reason', state)
        held = [t['evidence_guard']['would_hold'] for t in state['turns'] if 'would_hold' in t.get('evidence_guard', {})]
        self.assertTrue(held and all('accessed isolated evidence directory' in reason for reason in held), held)
        self.other_run('strict-run')
        strict = self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                        env={'FAKE_SENSITIVE_READ': str(self.h.run_dir / 'evidence' / 'secret.json')})
        self.assertEqual(strict.returncode, 2)
        self.assertIn('accessed isolated evidence directory', self.state()['hold_reason'])

    # --- categories A and B: the same in both modes ---------------------------------------------------------------------------------
    def test_every_role_keeps_its_sandbox_flags_in_efficient_mode(self):
        use_lifecycle_on(self, self.h)
        schema = self.h.root / 'schema.json'
        rc.atomic_json(schema, rc.review_schema())
        for vendor in ('claude', 'codex'):
            with self.subTest(vendor=vendor), patch.dict(os.environ, {'EFFMODE_SESSION_TOKEN': 'x'}):
                self.other_run('flags-' + vendor)
                co = self.h.coordinator('--author-vendor', vendor, '--reviewer-vendor', vendor, '--gate-vendor', vendor)
                roles = ('author', 'reviewer', 'gate', 'shadow', 'probe')
                efficient = {role: co.command(role, schema, True) for role in roles}
                co.args.safety_mode = 'strict'
                self.assertEqual({role: co.command(role, schema, True) for role in roles}, efficient)
                if vendor == 'claude':
                    for role in roles:
                        settings = json.loads(efficient[role][efficient[role].index('--settings') + 1])
                        self.assertIn(str(co.run_dir), settings['sandbox']['filesystem']['denyWrite'])   # A: records, B: the run dir
                        self.assertIn('EFFMODE_SESSION_TOKEN', {e['name'] for e in settings['sandbox']['credentials']['envVars']})   # B
                    self.assertIn('acceptEdits', efficient['author'])
                    self.assertIn('--restricted', efficient['reviewer'])
                else:
                    self.assertIn('sandbox_mode="workspace-write"', ' '.join(efficient['author']))
                    self.assertIn('default_permissions="paired_session_readonly"', ' '.join(efficient['reviewer']))   # v2.9.6 FIELD-1

    def test_the_run_dir_stays_outside_the_workspace_in_efficient_mode(self):
        code, out = self.cli('run', '--run-dir', str(self.h.workspace / 'run'))
        self.assertEqual(code, 2)
        self.assertIn('--run-dir must be outside --workspace', out)

    def test_a_model_claim_is_no_test_result_in_efficient_mode(self):
        result = self.run_efficient('--shadow', 'off', '--adversarial-gate', 'off', env={'FAKE_REVIEW_NO_TEST_EVENT': '1'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('observed successful configured test command', self.state()['hold_reason'])

    def test_a_global_config_change_holds_in_efficient_mode(self):
        result = self.run_efficient('--shadow', 'off', '--adversarial-gate', 'off', env={'FAKE_GLOBAL_CONFIG_WRITE': '1'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('changed global config', self.state()['hold_reason'])

    def test_a_waiver_flag_is_noted_as_unneeded_in_efficient_mode(self):
        code, out = self.cli('run', '--accept-probe-skip', '--reason', 'checked')
        self.assertEqual(code, 0, out)
        self.assertIn('needs no probe waiver', out)
        self.assertNotIn('probe_skip_override', self.state())

    def test_an_author_commit_holds_even_when_the_turn_fails(self):   # the HEAD check comes before the exit-code check
        result = self.run_efficient('--shadow', 'off', '--adversarial-gate', 'off', env={'FAKE_AUTHOR_COMMIT': '1', 'FAKE_AUTHOR_FAIL_AFTER_WRITE': '1'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('author changed HEAD or the branch', self.state()['hold_reason'])

    def test_an_author_commit_holds_in_both_modes(self):   # the git guard (B)
        result = self.run_efficient('--shadow', 'off', '--adversarial-gate', 'off', env={'FAKE_AUTHOR_COMMIT': '1'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('author changed HEAD or the branch', self.state()['hold_reason'])
        self.other_run('strict-run')
        strict = self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', env={'FAKE_AUTHOR_COMMIT': '1'})
        self.assertEqual(strict.returncode, 2)
        self.assertIn('author changed HEAD or the branch', self.state()['hold_reason'])


if __name__ == '__main__':
    unittest.main()
