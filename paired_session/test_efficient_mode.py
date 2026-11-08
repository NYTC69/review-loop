"""D-EFF (docs/efficient-mode.md): since v3.7.0 the efficient mode is the only mode. It drops only category C (the permission-probe
PASS before dispatch, a holding evidence guard); every sandbox flag and every category A/B check stays. `--strict`, a profile's
safety_mode "strict" and a run saved strict print one NOTE and run in the default mode."""
import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_real_coordinator as trc

rc = trc.rc
NOTE = 'NOTE: strict mode was removed in v3.7.0; the run uses the default mode'


class EfficientModeTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def command(self, *extra):
        return self.h.command(*extra)

    def state(self, run_dir=None):
        return json.loads(((run_dir or self.h.run_dir) / 'state.json').read_text())

    def run_efficient(self, *extra, env=None):   # a real coordinator process, without --skip-probe
        return subprocess.run(self.command(*extra), cwd=self.h.root, env={**os.environ, **(env or {})}, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def cli(self, action, *extra):
        """In-process main() with the fake-harness bypass off, as on an operator machine; the drive itself is not under test."""
        command = self.command(*extra)
        command[2] = action
        out, err = io.StringIO(), io.StringIO()
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), \
                patch.object(rc.Coordinator, 'drive', return_value='DONE'), patch.object(rc.Coordinator, 'resume', return_value='DONE'):
            code = rc.main(command[2:])
        self.stderr = err.getvalue()
        return code, out.getvalue()

    def other_run(self, name):
        self.h.run_dir = self.h.root / name

    # --- v3.7.0: strict is accepted only as a NOTE ------------------------------------------------------------------------------------
    def test_the_default_is_efficient_and_strict_mode_is_gone(self):
        co = self.h.coordinator()
        self.assertFalse(hasattr(co, 'strict'))
        self.assertEqual(co.state['config']['safety_mode'], 'efficient')
        self.assertNotIn('--strict', rc.parser().format_help())

    def test_strict_on_the_command_line_prints_one_note_and_runs_in_the_default_mode(self):
        code, out = self.cli('run', '--strict')
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count(NOTE), 1, out)
        self.assertEqual(self.state()['config']['safety_mode'], 'efficient')

    def test_a_strict_profile_anywhere_is_a_note_and_an_unknown_value_is_still_refused(self):
        base = ['run', '--workspace', str(self.h.workspace), '--workitem', str(self.h.workitem), '--run-dir', str(self.h.run_dir)]
        profile = self.h.root / 'operator-profile.json'
        for value in ('strict', 'efficient'):
            with self.subTest(value=value):
                profile.write_text(json.dumps({'safety_mode': value}))
                argv = [*base, '--config', str(profile)]
                self.assertEqual(rc.configure_parser(rc.parser(), argv).parse_args(argv).safety_mode, value)   # parsed, then ignored
        profile.write_text(json.dumps({'safety_mode': 'yolo'}))
        with self.assertRaisesRegex(ValueError, 'safety_mode must be efficient or strict'):
            rc.configure_parser(rc.parser(), [*base, '--config', str(profile)])
        workspace_config = self.h.workspace / '.review-loop' / 'paired-session.json'   # a workspace profile may name it too now
        workspace_config.parent.mkdir()
        workspace_config.write_text(json.dumps({'safety_mode': 'strict'}))
        self.assertEqual(rc.configure_parser(rc.parser(), base).parse_args(base).safety_mode, 'strict')
        code, out = self.cli('run')
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count(NOTE), 1, out)
        self.assertEqual(self.state()['config']['safety_mode'], 'efficient')
        workspace_config.write_text(json.dumps({'safety_mode': 'efficient'}))
        self.other_run('efficient-profile')
        code, out = self.cli('run')
        self.assertEqual(code, 0, out)
        self.assertNotIn(NOTE, out)

    def test_a_run_saved_strict_resumes_in_the_default_mode_with_the_note(self):
        code, out = self.cli('run')
        self.assertEqual(code, 0, out)
        state = self.state()
        state['config']['safety_mode'] = 'strict'
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        for extra in ((), ('--strict',)):                                 # one NOTE per command, even with both sources
            with self.subTest(extra=extra):
                code, out = self.cli('resume', *extra)
                self.assertEqual(code, 0, out)
                self.assertEqual(out.count(NOTE), 1, out)
                self.assertEqual(self.state()['config']['safety_mode'], 'strict')   # the frozen config stays byte-identical

    def test_the_note_keeps_machine_readable_stdout_clean(self):
        code, out = self.cli('run')
        self.assertEqual(code, 0, out)
        code, out = self.cli('status', '--strict')
        self.assertEqual(code, 0, out)
        json.loads(out)                                                   # still valid JSON
        self.assertIn(NOTE, self.stderr)
        state = self.state()
        state['config']['safety_mode'] = 'strict'                         # a saved strict run too
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        out = io.StringIO()
        argv = self.command()
        argv[2] = 'accept'
        args = rc.parser().parse_args([*argv[2:], '--intent-only'])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()) as err:
            rc.Coordinator(args)
        self.assertNotIn(NOTE, out.getvalue())
        self.assertIn(NOTE, err.getvalue())

    def test_strict_is_not_refused_on_windows(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.h.workspace), '--workitem', str(self.h.workitem),
                                       '--run-dir', str(self.h.run_dir), '--strict'])
        with patch.object(rc.winplat, 'IS_WINDOWS', True):
            self.assertIsNone(rc.windows_refusal(args))

    # --- category C: dropped --------------------------------------------------------------------------------------------------------
    def test_no_permission_probe_or_author_opt_in_is_needed(self):
        for extra in ([], ['--author-vendor', 'claude'], ['--strict']):
            with self.subTest(extra=extra):
                self.other_run('efficient-' + '-'.join(extra or ['codex']).strip('-'))
                code, out = self.cli('run', *extra)
                self.assertEqual(code, 0, out)
                self.assertNotIn('permission-probe', out)
                self.assertNotIn('limited to the fake test harness', out)

    def test_a_real_run_needs_no_probe_and_its_evidence_guard_only_logs(self):
        for extra in ((), ('--strict',)):
            with self.subTest(extra=extra):
                self.other_run('guard-' + str(len(extra)))
                secret = str(self.h.run_dir / 'evidence' / 'secret.json')
                result = self.run_efficient('--shadow', 'off', '--adversarial-gate', 'off', *extra, env={'FAKE_SENSITIVE_READ': secret})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                state = self.state()
                self.assertNotIn('hold_reason', state)
                held = [t['evidence_guard']['would_hold'] for t in state['turns'] if 'would_hold' in t.get('evidence_guard', {})]
                self.assertTrue(held and all('accessed isolated evidence directory' in reason for reason in held), held)

    # --- categories A and B: kept ---------------------------------------------------------------------------------
    def test_every_role_keeps_its_sandbox_flags_in_efficient_mode(self):
        use_lifecycle_on(self, self.h)
        schema = self.h.root / 'schema.json'
        rc.atomic_json(schema, rc.review_schema())
        for vendor in ('claude', 'codex'):
            with self.subTest(vendor=vendor), patch.dict(os.environ, {'EFFMODE_SESSION_TOKEN': 'x'}):
                self.other_run('flags-' + vendor)
                co = self.h.coordinator('--author-vendor', vendor, '--reviewer-vendor', vendor, '--gate-vendor', vendor)
                roles = ('author', 'reviewer', 'gate', 'shadow')
                efficient = {role: co.command(role, schema, True) for role in roles}
                co.args.safety_mode = 'strict'                                    # a saved strict value changes no argv
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
                    self.assertIn('sandbox_mode="workspace-write"', ' '.join(efficient['reviewer']))   # v3.6.0: the author's shape
                    self.assertNotIn('default_permissions', ' '.join(efficient['reviewer']))

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

    def test_a_waiver_flag_is_refused_as_a_removed_option(self):
        with patch.dict(os.environ, {'PAIRED_SESSION_INTERNAL_TEST_REMOVED_OPTIONS': '0'}):
            code, out = self.cli('run', '--accept-probe-skip', '--reason', 'checked')
        self.assertEqual(code, 2, out)
        self.assertIn('removed user option: --accept-probe-skip; removed in v3.7.0', out)

    def test_an_author_commit_holds_even_when_the_turn_fails(self):   # the HEAD check comes before the exit-code check
        result = self.run_efficient('--shadow', 'off', '--adversarial-gate', 'off', env={'FAKE_AUTHOR_COMMIT': '1', 'FAKE_AUTHOR_FAIL_AFTER_WRITE': '1'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('author changed HEAD or the branch', self.state()['hold_reason'])

    def test_an_author_commit_holds_with_or_without_strict(self):   # the git guard (B)
        for extra in ((), ('--strict',)):
            with self.subTest(extra=extra):
                self.other_run('commit-' + str(len(extra)))
                result = self.run_efficient('--shadow', 'off', '--adversarial-gate', 'off', *extra, env={'FAKE_AUTHOR_COMMIT': '1'})
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('author changed HEAD or the branch', self.state()['hold_reason'])


if __name__ == '__main__':
    unittest.main()
