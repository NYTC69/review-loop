"""Batch W1a (ADR-11, docs/e2e-6): worktree lifecycle activation on the real path."""
import json
import unittest
from unittest import mock

from paired_session import test_real_coordinator as trc
from paired_session import worktree_lifecycle as wl

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action', 'coordinator')


class WorktreeLifecycleActivationTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def args(self, *extra, action='run'):
        command = self.command('--lifecycle-mode', 'on', *extra)
        command[2] = action
        return rc.parser().parse_args(command[2:])

    def test_cli_lifecycle_on_creates_and_resumes_worktree_state_on_the_real_path(self):
        co = rc.Coordinator(self.args())
        life = co.state['lifecycle']
        self.assertFalse(co._fake_lifecycle)
        self.assertEqual((life['format'], life['stage'], life['epoch'], life['candidate_oid'], life['receipts']),
                         ('worktree', 'EXEC', 0, None, []))
        self.assertEqual((life['parent'], co.state['config']['lifecycle_mode']), (co.state['base_commit'], 'on'))
        co._verify_frozen_role_dispatch()   # the real-EXEC author TMP is accepted for W
        self.assertEqual(rc.Coordinator(self.args(action='resume')).state['lifecycle'], life)

    def test_waivers_gate_off_and_resume_polish_are_refused_before_state(self):
        cases = ((('--accept-unverified-claude-author', '--reason', 'owner opt-in'),
                  'worktree lifecycle refuses --accept-unverified-claude-author'),
                 (('--accept-probe-skip', '--reason', 'owner accepted'), 'worktree lifecycle refuses --accept-probe-skip'),
                 (('--adversarial-gate', 'off'), 'lifecycle refuses --adversarial-gate off'))
        for extra, message in cases:
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, message):
                    rc.Coordinator(self.args(*extra))
                self.assertFalse((self.run_dir / 'state.json').exists())
        with self.assertRaisesRegex(ValueError, 'lifecycle refuses resume --polish'):
            rc.Coordinator(self.args('--polish', action='resume'))

    def test_operator_profile_enables_and_author_writable_profiles_stay_refused(self):
        operator = self.root / 'operator.json'
        operator.write_text(json.dumps({'lifecycle_mode': 'on'}))
        argv = self.command('--config', str(operator))[2:]
        self.assertEqual(rc.configure_parser(rc.parser(), argv).parse_args(argv).lifecycle_mode, 'on')
        self.run_dir.mkdir()
        for profile in (self.workspace / 'ops.json', self.run_dir / 'ops.json', self.run_dir / 'author-tmp' / 'ops.json'):
            with self.subTest(profile=profile):
                profile.parent.mkdir(exist_ok=True)
                profile.write_text(json.dumps({'lifecycle_mode': 'on'}))
                argv = self.command('--config', str(profile))[2:]
                with self.assertRaisesRegex(ValueError, 'lifecycle remains disabled from a workspace profile'):
                    rc.configure_parser(rc.parser(), argv)
                explicit = self.command('--config', str(profile), '--lifecycle-mode', 'on')[2:]
                self.assertEqual(rc.configure_parser(rc.parser(), explicit).parse_args(explicit).lifecycle_mode, 'on')

    def test_fake_format_and_recorded_waiver_states_are_refused_on_the_real_path(self):
        co = rc.Coordinator(self.args())
        for change, message in ((lambda state: state['lifecycle'].pop('format'), 'not a worktree-lifecycle run'),
                                (lambda state: state.update(probe_skip_override={'actor': 'operator'}),
                                 'recorded author waiver or probe-skip acceptance')):
            with self.subTest(message=message):
                saved = json.loads(co.state_path.read_text())
                change(saved)
                co.state_path.write_text(json.dumps(saved))
                with self.assertRaisesRegex(ValueError, message):
                    rc.Coordinator(self.args(action='resume'))
                co.save()

    def test_exec_convergence_holds_before_finish_and_resume_dispatches_nothing(self):
        result = self.run_coordinator('--lifecycle-mode', 'on')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('HOLD: ' + wl.FINISH_PENDING, result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((state['status'], state['hold_reason'], state['lifecycle']['stage']),
                         ('HOLD', wl.FINISH_PENDING, 'FINISH'))
        self.assertEqual(state['lifecycle']['candidate_oid'], rc.git_snapshot(self.workspace)[0])   # tree bound at convergence
        self.assertEqual(state['acceptance_state'], 'IN_PROGRESS')
        self.assertTrue(any(turn['role'] == 'gate' for turn in state['turns']))
        again = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertEqual(again.returncode, 2, again.stdout + again.stderr)
        self.assertIn('HOLD: ' + wl.FINISH_PENDING, again.stdout)
        self.assertEqual(len(json.loads((self.run_dir / 'state.json').read_text())['turns']), len(state['turns']))

    def test_accept_is_refused_for_worktree_runs_even_on_override_paths(self):
        co = rc.Coordinator(self.args())
        for status, kind in (('DONE', None), ('HOLD', 'rejection_limit')):
            with self.subTest(status=status):
                co.state.update(status=status, terminal_hold_kind=kind)
                with self.assertRaisesRegex(ValueError, 'worktree lifecycle accept is not available before W3b'):
                    co.accept()
                self.assertNotEqual(co.state['status'], 'ACCEPTED')

    def test_open_blocker_at_convergence_holds_without_entering_finish(self):
        co = rc.Coordinator(self.args())
        co.state['gate_ran'] = True
        with mock.patch.object(co, 'blocking_open_findings', return_value=[{'id': 'F007'}]):
            self.assertEqual(co.start_polish_or_done(), 'HOLD')
        self.assertIn('open blocking findings: F007', co.state['hold_reason'])
        self.assertFalse(co.state['gate_ran'])   # the repair convergence runs its own gate
        self.assertEqual((co.state['lifecycle']['stage'], co.state['lifecycle']['candidate_oid']), ('EXEC', None))

    def test_saved_config_paths_still_refuse_waivers_and_pre_lifecycle_states(self):
        rc.Coordinator(self.args())
        reject = self.command('--accept-probe-skip', '--reason', 'skip')
        reject[2] = 'reject'
        with self.assertRaisesRegex(ValueError, 'worktree lifecycle refuses --accept-probe-skip'):
            rc.Coordinator(rc.parser().parse_args(reject[2:]))   # lifecycle_mode comes from the saved config here
        self.run_dir = self.root / 'pre-lifecycle'
        legacy = self.coordinator()
        legacy.state['config'].pop('lifecycle_mode', None)
        legacy.save()
        with self.assertRaisesRegex(ValueError, 'not a worktree-lifecycle run'):
            rc.Coordinator(self.args(action='resume'))

    def test_a_profile_in_a_relocated_author_tmp_is_still_refused(self):
        outside = self.root / 'isolated-tmp'
        outside.mkdir()
        self.run_dir.mkdir()
        (self.run_dir / 'author-tmp').symlink_to(outside, target_is_directory=True)
        (outside / 'ops.json').write_text(json.dumps({'lifecycle_mode': 'on'}))
        argv = self.command('--config', str(outside / 'ops.json'))[2:]
        with self.assertRaisesRegex(ValueError, 'lifecycle remains disabled from a workspace profile'):
            rc.configure_parser(rc.parser(), argv)

    def test_a_case_alias_of_the_workspace_profile_is_still_refused(self):
        profile = self.workspace / 'ops.json'
        profile.write_text(json.dumps({'lifecycle_mode': 'on'}))
        alias = self.workspace.parent / self.workspace.name.upper() / 'ops.json'
        if not alias.exists():
            self.skipTest('case-sensitive filesystem')
        argv = self.command('--config', str(alias))[2:]
        with self.assertRaisesRegex(ValueError, 'lifecycle remains disabled from a workspace profile'):
            rc.configure_parser(rc.parser(), argv)
