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

    def finish_rows(self, state):
        return [row for row in state['lifecycle']['receipts'] if row['stage'] == 'FINISH']

    def test_finish_no_op_advances_to_polish_q_and_resume_dispatches_nothing(self):
        result = self.run_coordinator('--lifecycle-mode', 'on')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('HOLD: ' + wl.POLISH_PENDING, result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        tree = rc.git_snapshot(self.workspace)[0]
        self.assertEqual((state['status'], state['hold_reason'], state['lifecycle']['stage'], state['next']),
                         ('HOLD', wl.POLISH_PENDING, 'POLISH-Q', 'polish-q'))
        self.assertEqual(state['lifecycle']['candidate_oid'], tree)   # bound to the last reviewed tree
        [finish] = self.finish_rows(state)
        self.assertEqual((finish['status'], finish['candidate_oid'], finish['output_oid'], finish['request_id'], finish['epoch']),
                         ('READY', tree, tree, 'w-FINISH-0-0', 0))
        turn = next(row for row in state['turns'] if row['sequence'] == finish['sequence'])
        self.assertTrue(any(row['role'] == 'gate' and row['snapshot_before'] == tree for row in state['turns']))
        self.assertEqual((turn['role'], turn['phase'], turn['fresh']), ('author', 'FINISH', True))
        self.assertEqual(state['acceptance_state'], 'IN_PROGRESS')
        again = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertEqual(again.returncode, 2, again.stdout + again.stderr)
        self.assertIn('HOLD: ' + wl.POLISH_PENDING, again.stdout)
        self.assertEqual(len(json.loads((self.run_dir / 'state.json').read_text())['turns']), len(state['turns']))

    def test_a_finish_write_reopens_exec_review_and_gate_before_finishing_again(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_FINISH_WRITE': '1'})
        self.assertIn('HOLD: ' + wl.POLISH_PENDING, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        first, second = self.finish_rows(state)
        self.assertNotEqual(first['output_oid'], first['candidate_oid'])   # the finisher changed the reviewed tree
        self.assertEqual((first['epoch'], second['epoch'], state['lifecycle']['epoch']), (0, 1, 1))
        self.assertEqual((second['candidate_oid'], second['output_oid']), (first['output_oid'], first['output_oid']))
        between = [(row['role'], row['phase']) for row in state['turns']
                   if first['sequence'] < row['sequence'] < second['sequence']]
        self.assertIn(('reviewer', 'EXEC'), between)
        self.assertIn(('gate', 'EXEC'), between)   # a new convergence spends its own gate
        self.assertIn('# finisher fix', (self.workspace / 'sum_ints.py').read_text())

    def test_a_finisher_hold_and_an_uncertain_finish_turn_resume_with_stable_requests(self):
        held = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_FINISH_HOLD': '1'})
        self.assertIn('HOLD: finisher: Fake finisher held.', held.stdout, held.stdout + held.stderr)
        co = rc.Coordinator(self.args(action='resume'))
        [hold_row] = self.finish_rows(co.state)
        self.assertEqual((hold_row['status'], co.state['lifecycle']['stage'], co.state['next']), ('HOLD', 'FINISH', 'finish'))
        request = wl.stage_request(co.state['lifecycle'], 'finisher')   # the attempt a crashed CLI left in flight
        co.state['lifecycle']['writer_git'] = {**co._writer_git_state(), 'sequence': co.state['sequence']}
        co.state['lifecycle'] = rc.lifecycle_spine.begin(co.state['lifecycle'], request)
        co.state.update(status='ACTIVE', hold_reason='', active={'pid': 43220, 'role': 'author', 'phase': 'FINISH',
                                                                  'sequence': co.state['sequence'] + 1})
        co.save()
        crashed = rc.Coordinator(self.args(action='resume'))
        self.assertEqual(crashed.resume(), 'HOLD')
        self.assertIn('uncertain in-flight', crashed.state['hold_reason'])
        with mock.patch.object(rc, 'retry_killpg_eperm', side_effect=ProcessLookupError):
            self.assertEqual(crashed.resume(retry_uncertain=True), 'HOLD')
        self.assertEqual(crashed.state['hold_reason'], wl.POLISH_PENDING)
        rows = self.finish_rows(crashed.state)
        self.assertEqual([(row['request_id'], row['status']) for row in rows],
                         [('w-FINISH-0-0', 'HOLD'), (request['request_id'], 'READY')])
        self.assertEqual(request['request_id'], 'w-FINISH-0-1')
        self.assertIsNone(crashed.state['lifecycle']['pending'])
        self.assertEqual(crashed.state['abandoned_turns'][-1]['phase'], 'FINISH')

    def test_a_finisher_that_commits_holds_without_a_receipt(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_FINISH_COMMIT': '1'})
        self.assertIn('HOLD: finisher changed HEAD, refs or the index', result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((state['lifecycle']['stage'], self.finish_rows(state)), ('FINISH', []))
        self.assertEqual(state['lifecycle']['pending']['request_id'], 'w-FINISH-0-0')   # never completed
        again = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertIn('HOLD: finisher changed HEAD, refs or the index', again.stdout, again.stdout + again.stderr)
        self.assertEqual(len(json.loads((self.run_dir / 'state.json').read_text())['turns']), len(state['turns']))

    def test_finish_binds_the_last_reviewed_snapshot_and_refuses_a_stale_tree(self):
        co = rc.Coordinator(self.args())
        tree = rc.git_snapshot(self.workspace)[0]
        co.state['turns'] = [{'role': 'reviewer', 'phase': 'EXEC', 'snapshot_before': tree, 'sequence': 1},
                             {'role': 'gate', 'phase': 'EXEC', 'snapshot_before': tree, 'sequence': 2}]
        self.assertEqual(co.start_polish_or_done(), 'ACTIVE')
        self.assertEqual((co.state['lifecycle']['stage'], co.state['lifecycle']['candidate_oid'], co.state['next']),
                         ('FINISH', tree, 'finish'))
        self.assertEqual(co.state['lifecycle']['exec_convergence'],
                         {'tree': tree, 'reviewer_sequence': 1, 'gate_sequence': 2})
        other = 'f' * 64
        for turns in ([{'role': 'gate', 'phase': 'EXEC', 'snapshot_before': tree, 'sequence': 1}],   # gate alone
                      [{'role': 'reviewer', 'phase': 'EXEC', 'snapshot_before': other, 'sequence': 1},
                       {'role': 'gate', 'phase': 'EXEC', 'snapshot_before': tree, 'sequence': 2}],   # reviewer saw another tree
                      [{'role': 'reviewer', 'phase': 'EXEC', 'snapshot_before': other, 'sequence': 1},
                       {'role': 'gate', 'phase': 'EXEC', 'snapshot_before': other, 'sequence': 2}]):   # both stale
            with self.subTest(turns=turns):
                stale = rc.Coordinator(self.args(action='resume'))
                stale.state['lifecycle'].update(stage='EXEC', candidate_oid=None)
                stale.state.update(turns=turns, gate_ran=True, next='gate')
                self.assertEqual(stale.start_polish_or_done(), 'HOLD')
                self.assertIn('stale EXEC approval', stale.state['hold_reason'])
                self.assertEqual((stale.state['lifecycle']['stage'], stale.state['lifecycle']['candidate_oid'],
                                  stale.state['gate_ran'], stale.state['next']), ('EXEC', None, False, 'reviewer'))

    def test_lifecycle_keys_are_operator_only_for_worktree_runs(self):
        profile = self.workspace / '.review-loop' / 'paired-session.json'
        profile.parent.mkdir()
        profile.write_text(json.dumps({'docs_file': 'CHANGELOG.md', 'skip_quality_polish': 'true'}))
        argv = self.command('--lifecycle-mode', 'on')[2:]
        args = rc.configure_parser(rc.parser(), argv).parse_args(argv)
        self.assertEqual(args.workspace_lifecycle_keys, ['docs_file', 'skip_quality_polish'])
        with self.assertRaisesRegex(ValueError, 'lifecycle keys from a workspace profile: docs_file, skip_quality_polish'):
            rc.Coordinator(args)
        self.assertFalse((self.run_dir / 'state.json').exists())
        legacy = self.command()[2:]   # lifecycle off: the workspace profile keeps working as before
        self.assertEqual(rc.Coordinator(rc.configure_parser(rc.parser(), legacy).parse_args(legacy)).state['config']['docs_file'],
                         str(self.workspace / 'CHANGELOG.md'))
        operator = self.root / 'operator.json'
        operator.write_text(json.dumps({'docs_file': 'CHANGELOG.md', 'lifecycle_mode': 'on'}))
        self.run_dir = self.root / 'operator-run'
        argv = self.command('--config', str(operator))[2:]
        co = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))
        self.assertEqual((co.state['config']['docs_file'], co.state['lifecycle']['format']),
                         (str(self.workspace / 'CHANGELOG.md'), 'worktree'))

    def test_a_recorded_finish_turn_is_reused_instead_of_a_second_dispatch(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                self.run_dir = self.root / ('reuse-changed' if changed else 'reuse')
                self.check_recorded_finish_reuse(changed)

    def check_recorded_finish_reuse(self, changed):
        co = rc.Coordinator(self.args())
        tree = rc.git_snapshot(self.workspace)[0]
        (co.context / 'plan.md').write_text('Plan\n')
        co.state['lifecycle'].update(stage='FINISH', candidate_oid=tree)
        request = wl.stage_request(co.state['lifecycle'], 'finisher')
        co.state['lifecycle']['writer_git'] = {**co._writer_git_state(), 'sequence': co.state['sequence']}
        co.state['lifecycle'] = rc.lifecycle_spine.begin(co.state['lifecycle'], request)
        co.state['turns'].append({'role': 'author', 'phase': 'FINISH', 'sequence': co.state['sequence'] + 1,
                                  'answer': {'status': 'READY', 'body': 'done'}, 'snapshot_after': tree})
        if changed:   # the tree moved before the replay (e.g. an operator restore): the receipt binds the tree now
            (self.workspace / 'tracked.txt').write_text('changed before replay\n')
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('second FINISH dispatch')):
            co.worktree_finish_turn()   # the coordinator crashed after the turn was recorded, before its receipt
        [row] = self.finish_rows(co.state)
        now = rc.git_snapshot(self.workspace)[0]
        self.assertEqual((row['request_id'], row['status'], row['sequence'], row['output_oid']),
                         (request['request_id'], 'READY', co.state['sequence'] + 1, now))
        self.assertEqual((co.state['lifecycle']['stage'], co.state['next']),
                         ('EXEC', 'reviewer') if changed else ('POLISH-Q', 'polish-q'))
        self.assertNotIn('writer_git', co.state['lifecycle'])

    def test_a_finish_write_at_the_exec_round_limit_holds(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-exec-rounds', '1', env={'FAKE_FINISH_WRITE': '1'})
        self.assertIn('HOLD: EXEC round limit reached after a FINISH write', result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((state['lifecycle']['stage'], state['lifecycle']['epoch'], state['next']), ('EXEC', 1, 'reviewer'))

    def test_explicit_cli_values_and_resume_follow_the_operator_only_rule(self):
        profile = self.workspace / '.review-loop' / 'paired-session.json'
        profile.parent.mkdir()
        profile.write_text(json.dumps({'docs_file': 'CHANGELOG.md'}))
        argv = self.command('--lifecycle-mode', 'on', '--docs-file', 'CHANGELOG.md')[2:]   # the CLI value wins
        co = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))
        self.assertEqual(co.state['config']['docs_file'], str(self.workspace / 'CHANGELOG.md'))
        profile.write_text(json.dumps({'docs_file': 'CHANGELOG.md', 'skip_quality_polish': 'true'}))   # written later
        resume = self.command('--lifecycle-mode', 'on', '--docs-file', 'CHANGELOG.md')
        resume[2] = 'resume'
        with self.assertRaisesRegex(ValueError, 'lifecycle keys from a workspace profile: skip_quality_polish'):
            rc.Coordinator(rc.configure_parser(rc.parser(), resume[2:]).parse_args(resume[2:]))
        profile.write_text(json.dumps({'docs_file': 'CHANGELOG.md'}))   # same value as saved, from the profile
        abort = self.command('--lifecycle-mode', 'on')
        abort[2] = 'abort'   # operator actions that never read these keys still work
        rc.Coordinator(rc.configure_parser(rc.parser(), abort[2:]).parse_args(abort[2:]))

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
