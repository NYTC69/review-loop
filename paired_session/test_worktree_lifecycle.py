"""Batch W1a (ADR-11, docs/e2e-6): worktree lifecycle activation on the real path."""
import hashlib
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

    def test_finish_polish_q_and_a_no_op_docs_writer_advance_to_the_security_hold(self):
        result = self.run_coordinator('--lifecycle-mode', 'on')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        tree = rc.git_snapshot(self.workspace)[0]
        self.assertEqual((state['status'], state['hold_reason'], state['lifecycle']['stage'], state['next']),
                         ('HOLD', wl.SECURITY_PENDING, 'SECURITY', 'security'))
        [docs] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS']
        self.assertEqual((docs['status'], docs['route'], docs['docs_paths'], docs['candidate_oid'], docs['output_oid']),
                         ('READY', 'SECURITY', [], tree, tree))
        writer = next(row for row in state['turns'] if row['sequence'] == docs['sequence'])
        self.assertEqual((writer['role'], writer['phase'], writer['fresh']), ('author', 'DOCS', True))
        prompt = (self.run_dir / 'evidence' / f"{docs['sequence']:03d}-docs-author.prompt.txt").read_text()
        self.assertIn('Role: docs writer, fresh. Phase: DOCS.', prompt)
        self.assertIn('Documentation paths you may write: CHANGELOG.md.', prompt)
        self.assertEqual(state['config']['docs_file'], str((self.workspace / 'CHANGELOG.md').resolve()))   # doc 6 W default
        for row in (next(row for row in state['turns'] if row['phase'] == 'FINISH'),
                    next(row for row in state['turns'] if row['role'] == 'author' and row['phase'] == 'EXEC')):
            prompt = (self.run_dir / 'evidence' / f"{row['sequence']:03d}-{row['phase'].lower()}-author.prompt.txt").read_text()
            self.assertIn('Reserved for the DOCS stage; do not edit: CHANGELOG.md.', prompt)   # earlier writers know
        [polish] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'POLISH-Q']
        names = ['python-reviewer', 'code-reviewer', 'silent-failure-hunter', 'pr-test-analyzer']   # sum_ints.py is Python
        self.assertEqual((polish['status'], polish['specialists'], polish['skipped'], polish['output_oid']),
                         ('READY', names, False, tree))
        turns = [row for row in state['turns'] if row['phase'] == 'POLISH-Q']
        self.assertEqual([(row['role'], row['fresh']) for row in turns], [('reviewer', True)] * 4)
        prompt = (self.run_dir / 'evidence' / f"{turns[0]['sequence']:03d}-polish-q-reviewer.prompt.txt").read_text()
        self.assertIn('Role: specialist python-reviewer, fresh. Phase: POLISH-Q.', prompt)
        self.assertIn('# Python Code Review', prompt)   # the frozen agent body is inlined without front matter
        self.assertNotIn('tier: cheap', prompt)
        for needle in ('Changed paths: sum_ints.py', 'delta.patch', 'Commands you may run exactly as written',
                       'Return verified_claims as an array', 'that is not a failure and not a reason to HOLD'):
            self.assertIn(needle, prompt)   # the EXEC reviewer protocol travels with the inlined body
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
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, again.stdout)
        self.assertEqual(len(json.loads((self.run_dir / 'state.json').read_text())['turns']), len(state['turns']))

    def test_a_finish_write_reopens_exec_review_and_gate_before_finishing_again(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_FINISH_WRITE': '1'})
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, result.stdout, result.stdout + result.stderr)
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
        self.assertEqual(crashed.state['hold_reason'], wl.SECURITY_PENDING)
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
        violating = next(row for row in state['turns'] if row['phase'] == 'FINISH')
        self.assertEqual(violating['discarded'], 'git guard')
        rc.subprocess.run(['git', 'reset', '-q', '--mixed', 'HEAD~1'], cwd=self.workspace, check=True)   # operator restore
        restored = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, restored.stdout, restored.stdout + restored.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        [finish] = self.finish_rows(state)
        self.assertNotEqual(finish['sequence'], violating['sequence'])   # a fresh FINISH, not the violator's READY
        self.assertEqual(finish['request_id'], 'w-FINISH-0-0')

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
        self.assertEqual(state['round_limit_hold']['hold_reason'], 'EXEC round limit reached after a FINISH write')
        self.assertIn(state['round_limit_hold']['hold_reason'], rc.ROUND_LIMIT_REASONS)   # _override_tree knows it (W accept: W3b)

    def test_a_crash_after_a_git_guard_violation_never_reuses_that_finish_turn(self):
        co = rc.Coordinator(self.args())
        tree = rc.git_snapshot(self.workspace)[0]
        co.state['lifecycle'].update(stage='FINISH', candidate_oid=tree)
        request = wl.stage_request(co.state['lifecycle'], 'finisher')
        co.state['lifecycle']['writer_git'] = {**co._writer_git_state(), 'sequence': co.state['sequence']}
        co.state['lifecycle'] = rc.lifecycle_spine.begin(co.state['lifecycle'], request)
        co.state['turns'].append({'role': 'author', 'phase': 'FINISH', 'sequence': co.state['sequence'] + 1,
                                  'answer': {'status': 'READY', 'body': 'done'}, 'snapshot_after': tree})
        (self.workspace / 'tracked.txt').write_text('staged by the finisher\n')
        rc.subprocess.run(['git', 'add', 'tracked.txt'], cwd=self.workspace, check=True)   # the index moved
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('dispatched past the guard')), \
                self.assertRaisesRegex(RuntimeError, 'finisher changed HEAD, refs or the index'):
            co.worktree_finish_turn()   # the crash came before the post-invoke check
        self.assertEqual(co.state['turns'][-1]['discarded'], 'git guard')
        rc.subprocess.run(['git', 'reset', '-q', 'tracked.txt'], cwd=self.workspace, check=True)   # operator restore
        co.context.mkdir(parents=True, exist_ok=True)
        (co.context / 'plan.md').write_text('plan\n')
        with mock.patch.object(co, 'invoke', side_effect=RuntimeError('fresh FINISH dispatch')), \
                self.assertRaisesRegex(RuntimeError, 'fresh FINISH dispatch'):
            co.worktree_finish_turn()

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

    def test_a_specialist_blocker_goes_to_the_fix_leg_and_only_its_owner_can_close_it(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60',
                                      env={'FAKE_SPECIALIST_BLOCK': 'code-reviewer'})
        self.assertIn('HOLD: POLISH-Q blockers remain after the fix: ', result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        first, second = [row for row in state['finding_ledger'] if row['source'] == 'specialist:code-reviewer']
        self.assertEqual((first['status'], first['owner_role'], first['severity']), ('fixed', 'specialist:code-reviewer', 'CRITICAL'))
        self.assertEqual(second['status'], 'open')   # the owner re-reviewed the fixed tree and found a new blocker
        phases = [(row['role'], row['phase']) for row in state['turns'] if row['sequence'] > first['origin_round']]
        self.assertEqual(phases[:2], [('reviewer', 'POLISH-Q')] * 2)   # silent-failure-hunter, pr-test-analyzer
        self.assertEqual(phases[2:4], [('author', 'EXEC'), ('reviewer', 'POLISH-Q')])   # the fix, then the owner
        self.assertIn('# specialist fix', (self.workspace / 'sum_ints.py').read_text())   # the author got the blocker
        self.assertEqual((state['lifecycle']['stage'], state['next']), ('POLISH-Q', 'polish-fix'))
        co = rc.Coordinator(self.args('--max-invocations', '60', action='resume'))
        for role in (None, 'persistent-reviewer', 'specialist:python-reviewer'):   # None: the real persistent path
            with self.subTest(role=role), self.assertRaisesRegex(RuntimeError, 'requires its owning specialist'):
                co.apply_dispositions([{'id': second['id'], 'disposition': 'fixed', 'evidence': 'not mine'}],
                                      999, [second['id']], role)
        self.assertNotIn(second['id'], co.open_findings_prompt())   # the persistent reviewer is not asked to close it
        self.assertNotIn(second['id'], [row['id'] for row in co._reviewer_open_findings()])
        again = self.run_operator_action('resume', '--lifecycle-mode', 'on', '--max-invocations', '60')
        # a second fix round on a new tree; its owner closes F002, the write replays EXEC + gate + FINISH + POLISH-Q
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, again.stdout, again.stdout + again.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual([row['status'] for row in state['finding_ledger'] if row['id'] == second['id']], ['fixed'])
        first_fix, second_fix = state['lifecycle']['polish_fixes']
        self.assertEqual((first_fix['epoch'], second_fix['epoch'], second_fix['base_tree']), (0, 0, first_fix['tree']))
        self.assertEqual((state['lifecycle']['epoch'], state['lifecycle']['counts_epoch'],
                          state['lifecycle']['specialist_counts']['code-reviewer']), (1, 1, 1))   # caps are per epoch

    def test_a_fix_that_leaves_the_tree_unchanged_cannot_close_a_blocker(self):
        tree = rc.git_snapshot(self.workspace)[0]
        for candidate in (tree, 'tree-of-an-earlier-epoch-round'):   # first fix round; a later round on its own base
            with self.subTest(candidate=candidate):
                self.run_dir = self.root / ('first' if candidate == tree else 'later')
                co = rc.Coordinator(self.args())
                [finding] = co.record_findings('specialist:code-reviewer', 'POLISH-Q', 1, [
                    {'severity': 'CRITICAL', 'file': 'tracked.txt', 'summary': 'held blocker'}])
                co.state['lifecycle'].update(stage='POLISH-Q', candidate_oid=candidate, fix_base=tree)
                co.state.update(next='polish-recheck', phase='EXEC', delivered_review='')   # author_turn cleared it
                with mock.patch.object(co, 'invoke', side_effect=AssertionError('re-reviewed an unchanged tree')):
                    co.worktree_polish_recheck_turn()
                self.assertEqual((co.state['status'], co.state['next']), ('HOLD', 'polish-fix'))
                self.assertIn('POLISH-Q fix left the tree unchanged', co.state['hold_reason'])
                self.assertIn('Delivered review:', co._author_prompt())   # the next fix round sees the blocker
                self.assertEqual(json.loads(co.state['delivered_review'])['findings'][0]['id'], finding['id'])

    def test_a_recheck_replay_keeps_the_owners_already_done(self):
        co = rc.Coordinator(self.args())
        tree = rc.git_snapshot(self.workspace)[0]
        for name in ('code-reviewer', 'pr-test-analyzer'):
            co.record_findings('specialist:' + name, 'POLISH-Q', 1, [
                {'severity': 'CRITICAL', 'file': 'tracked.txt', 'summary': name + ' blocker'}])
        done = {'name': 'code-reviewer', 'sequence': 7}
        co.state['lifecycle'].update(stage='POLISH-Q', candidate_oid='old', fix_base='old', recheck={
            'tree': tree, 'owners': ['code-reviewer', 'pr-test-analyzer'], 'done': {'code-reviewer': done}})
        calls = []
        with mock.patch.object(co, 'materialize_review_context'), \
                mock.patch.object(co, '_specialist_turn', side_effect=lambda name, *_: calls.append(name) or {'name': name}):
            co.worktree_polish_recheck_turn()
        self.assertEqual(calls, ['pr-test-analyzer'])
        [fix] = co.state['lifecycle']['polish_fixes']
        self.assertEqual((fix['recheck_turns'], fix['base_tree'], fix['tree']), ([done, {'name': 'pr-test-analyzer'}], 'old', tree))
        self.assertIn('POLISH-Q blockers remain after the fix', co.state['hold_reason'])
        self.assertEqual((co.state['lifecycle']['fix_base'], co.state['next']), (tree, 'polish-fix'))
        self.assertNotIn('recheck', co.state['lifecycle'])

    def test_the_fix_leg_checks_round_limit_and_owner_budget_before_the_author_writes(self):
        co = rc.Coordinator(self.args())
        co.record_findings('specialist:code-reviewer', 'POLISH-Q', 1, [
            {'severity': 'CRITICAL', 'file': 'tracked.txt', 'summary': 'held blocker'}])
        co.state['lifecycle'].update(stage='POLISH-Q', specialist_counts={'code-reviewer': 3})
        co.state['next'] = 'polish-fix'
        with mock.patch.object(co, 'author_turn', side_effect=AssertionError('author wrote')), \
                self.assertRaisesRegex(RuntimeError, 'POLISH-Q specialist budget exhausted: code-reviewer'):
            co.worktree_polish_fix_turn()
        co.record_findings('specialist:pr-test-analyzer', 'POLISH-Q', 1, [
            {'severity': 'CRITICAL', 'file': 'tracked.txt', 'summary': 'second owner'}])
        co.state['lifecycle'].update(specialist_counts={}, polish_calls=30)   # each owner fits, both together do not
        with mock.patch.object(co, 'author_turn', side_effect=AssertionError('author wrote')), \
                self.assertRaisesRegex(RuntimeError, 'POLISH-Q call budget cannot cover the re-reviews'):
            co.worktree_polish_fix_turn()
        co.state['lifecycle']['polish_calls'] = 0
        co.args.max_invocations = co.state['invocations_used'] + 2
        with mock.patch.object(co, 'author_turn', side_effect=AssertionError('author wrote')), \
                self.assertRaisesRegex(RuntimeError, 'the POLISH-Q fix needs at least 3 more invocations'):
            co.worktree_polish_fix_turn()
        co.state['exec_rounds'] = co.exec_round_limit()
        with mock.patch.object(co, 'author_turn', side_effect=AssertionError('author wrote')):
            co.worktree_polish_fix_turn()
        self.assertEqual((co.state['status'], co.state['round_limit_hold']['hold_reason']),
                         ('HOLD', 'EXEC round limit reached'))

    def test_a_fix_author_that_stopped_before_routing_still_goes_to_the_owners(self):
        co = rc.Coordinator(self.args())
        co.state['lifecycle']['stage'] = 'POLISH-Q'
        co.state.update(next='reviewer', phase='EXEC')   # author_turn saved next=reviewer, then the process died
        with mock.patch.object(co, 'reviewer_turn', side_effect=AssertionError('persistent reviewer')), \
                mock.patch.object(co, 'worktree_polish_recheck_turn', side_effect=lambda: co.hold('rechecked')):
            self.assertEqual(co._drive_loop(), 'HOLD')
        self.assertEqual((co.state['next'], co.state['hold_reason']), ('polish-recheck', 'rechecked'))

    def test_a_fixed_specialist_blocker_replays_exec_and_gate_then_finish_and_polish_q(self):
        env = {'FAKE_SPECIALIST_BLOCK': 'code-reviewer', 'FAKE_SPECIALIST_BLOCK_ONCE': str(self.root / 'block-once')}
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60', env=env)
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        [finding] = [row for row in state['finding_ledger'] if row['source'] == 'specialist:code-reviewer']
        self.assertEqual(finding['status'], 'fixed')
        first, second = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'POLISH-Q']
        self.assertEqual((first['status'], first['epoch'], second['status'], second['epoch']), ('HOLD', 0, 'READY', 1))
        [fix] = state['lifecycle']['polish_fixes']
        self.assertEqual(([row['name'] for row in fix['recheck_turns']], fix['tree']), (['code-reviewer'], second['candidate_oid']))
        between = [(row['role'], row['phase']) for row in state['turns']
                   if fix['recheck_turns'][0]['sequence'] < row['sequence'] < second['specialist_turns'][0]['sequence']]
        for step in (('reviewer', 'EXEC'), ('gate', 'EXEC'), ('author', 'FINISH')):   # the write replays EXEC and gate
            self.assertIn(step, between)
        self.assertIn('# specialist fix', (self.workspace / 'sum_ints.py').read_text())

    def test_a_same_tree_re_review_of_a_held_blocker_is_refused(self):
        co = rc.Coordinator(self.args())
        tree = rc.git_snapshot(self.workspace)[0]
        [finding] = co.record_findings('specialist:code-reviewer', 'POLISH-Q', 1, [
            {'severity': 'CRITICAL', 'file': 'tracked.txt', 'summary': 'held blocker'}])
        co.state['lifecycle'].update(stage='POLISH-Q', candidate_oid=tree, receipts=[
            {'stage': 'POLISH-Q', 'status': 'HOLD', 'epoch': 0, 'candidate_oid': tree, 'request_id': 'w-POLISH-Q-0-0'}])
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('re-reviewed the same tree')), \
                self.assertRaisesRegex(RuntimeError, 'need a fix on a new tree, not a re-review: ' + finding['id']):
            co.worktree_polish_turn()

    def test_a_specialist_without_tool_calls_is_retried_once_then_holds(self):
        marker = self.root / 'no-tools-once'
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_SPECIALIST_NO_TOOLS_ONCE': str(marker)})
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, result.stdout, result.stdout + result.stderr)   # the retry made tool calls
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['lifecycle']['specialist_counts']['python-reviewer'], 2)
        self.assertEqual([row.get('discarded') for row in state['turns'] if row['phase'] == 'POLISH-Q'][:2],
                         ['no tool calls', None])   # the discarded turn stays marked, the retry counts
        self.run_dir = self.root / 'no-tools'
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_SPECIALIST_NO_TOOLS': 'code-reviewer'})
        self.assertIn('HOLD: specialist code-reviewer made no tool calls after one retry', result.stdout,
                      result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((state['lifecycle']['specialist_counts']['code-reviewer'], state['lifecycle']['stage']), (2, 'POLISH-Q'))
        self.assertEqual(state['lifecycle']['pending']['stage'], 'POLISH-Q')   # no receipt for an incomplete POLISH-Q

    def test_a_specialist_hold_or_unknown_severity_never_passes(self):
        for env, reason in (({'FAKE_SPECIALIST_HOLD': 'silent-failure-hunter'},
                             'specialist silent-failure-hunter returned HOLD without a usable review'),
                            ({'FAKE_SPECIALIST_BLOCK': 'pr-test-analyzer', 'FAKE_SPECIALIST_SEVERITY': 'BLOCKER'},
                             "specialist pr-test-analyzer returned an unknown severity: 'BLOCKER'")):
            with self.subTest(env=env):
                self.run_dir = self.root / ('hold' if 'FAKE_SPECIALIST_HOLD' in env else 'severity')
                result = self.run_coordinator('--lifecycle-mode', 'on', env=env)
                self.assertIn('HOLD: ' + reason, result.stdout, result.stdout + result.stderr)
                state = json.loads((self.run_dir / 'state.json').read_text())
                self.assertEqual((state['lifecycle']['stage'], state['lifecycle']['pending']['stage']), ('POLISH-Q', 'POLISH-Q'))
                self.assertFalse([row for row in state['finding_ledger'] if row['phase'] == 'POLISH-Q'])

    def test_a_polish_q_replay_after_a_mid_stage_hold_reuses_completed_specialists(self):
        env = {'FAKE_SPECIALIST_BLOCK': 'code-reviewer', 'FAKE_SPECIALIST_NO_TOOLS': 'silent-failure-hunter'}
        held = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60', env=env)
        self.assertIn('HOLD: specialist silent-failure-hunter made no tool calls after one retry', held.stdout,
                      held.stdout + held.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(sorted(state['lifecycle']['specialist_done']), ['code-reviewer', 'python-reviewer'])
        [blocker] = [row['id'] for row in state['finding_ledger'] if row['status'] == 'open']
        first_code = next(row['sequence'] for row in state['turns'] if row['phase'] == 'POLISH-Q' and
                          'Role: specialist code-reviewer,' in (self.run_dir / 'evidence' /
                          f"{row['sequence']:03d}-polish-q-reviewer.prompt.txt").read_text())
        again = self.run_operator_action('resume', '--lifecycle-mode', 'on', '--max-invocations', '60')
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, again.stdout, again.stdout + again.stderr)   # fix leg, replay, clean
        state = json.loads((self.run_dir / 'state.json').read_text())
        attempt = next(row for row in state['lifecycle']['receipts'] if row['request_id'] == 'w-POLISH-Q-0-0')
        self.assertEqual((attempt['status'], [row['name'] for row in attempt['specialist_turns']]),
                         ('HOLD', attempt['specialists']))
        self.assertEqual(next(row['sequence'] for row in attempt['specialist_turns'] if row['name'] == 'code-reviewer'),
                         first_code)   # the completed specialist was reused, not re-dispatched, in the replay
        self.assertEqual([row['status'] for row in state['finding_ledger'] if row['id'] == blocker], ['fixed'])
        self.assertEqual((state['lifecycle']['epoch'], state['lifecycle']['specialist_counts']['silent-failure-hunter']), (1, 1))

    def test_skip_quality_polish_records_a_no_op_receipt(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', '--skip-quality-polish', 'true')
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        [polish] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'POLISH-Q']
        self.assertEqual((polish['status'], polish['specialists'], polish['skipped']), ('READY', [], True))
        self.assertFalse([row for row in state['turns'] if row['phase'] == 'POLISH-Q'])

    def docs_rows(self, state):
        return [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS']

    def test_an_allowlisted_docs_write_waits_for_the_docs_review(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'})
        self.assertIn('HOLD: ' + wl.DOCS_REVIEW_PENDING, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        [docs] = self.docs_rows(state)
        digest = hashlib.sha256((self.workspace / 'CHANGELOG.md').read_bytes()).hexdigest()
        self.assertEqual((docs['status'], docs['route'], docs['docs_paths'], docs['docs_written']),
                         ('HOLD', 'HOLD', ['CHANGELOG.md'], {'CHANGELOG.md': digest}))

    def test_a_docs_comment_write_replays_exec_review_and_gate(self):
        env = {'FAKE_DOCS_CODE_ONCE': str(self.root / 'docs-code-once')}
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60', env=env)
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        first, second = self.docs_rows(state)
        self.assertEqual((first['status'], first['route'], first['docs_paths'], first['docs_written'], first['epoch']),
                         ('READY', 'EXEC', ['sum_ints.py'], {}, 0))
        self.assertEqual((second['route'], second['docs_paths'], second['epoch'], second['candidate_oid']),
                         ('SECURITY', [], 1, first['output_oid']))
        between = [(row['role'], row['phase']) for row in state['turns']
                   if first['sequence'] < row['sequence'] < second['sequence']]
        for step in (('reviewer', 'EXEC'), ('gate', 'EXEC'), ('author', 'FINISH'), ('reviewer', 'POLISH-Q')):
            self.assertIn(step, between)   # the comment fix is reviewed and gated like any code write
        self.assertIn('# docs comment fix', (self.workspace / 'sum_ints.py').read_text())

    def test_a_docs_write_past_the_exec_round_limit_holds(self):
        env = {'FAKE_DOCS_CODE_ONCE': str(self.root / 'docs-code-once')}
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-exec-rounds', '1', env=env)
        self.assertIn('HOLD: EXEC round limit reached after a DOCS write', result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['round_limit_hold']['hold_reason'], 'EXEC round limit reached after a DOCS write')
        self.assertEqual((state['lifecycle']['stage'], state['lifecycle']['epoch'], state['next']), ('EXEC', 1, 'reviewer'))

    def test_a_docs_write_to_a_protected_path_holds_until_it_is_restored(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_LIFECYCLE_DOCS_FILE': 'CLAUDE.md'})
        self.assertIn('HOLD: DOCS writer changed protected paths: CLAUDE.md; restore them or abort', result.stdout,
                      result.stdout + result.stderr)
        again = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertIn('HOLD: DOCS tree differs from the POLISH-Q-approved tree', again.stdout, again.stdout + again.stderr)
        (self.workspace / 'CLAUDE.md').unlink()   # operator restore
        restored = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, restored.stdout, restored.stdout + restored.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual([(row['request_id'], row['route']) for row in self.docs_rows(state)],
                         [('w-DOCS-0-0', 'HOLD'), ('w-DOCS-0-1', 'SECURITY')])

    def test_docs_own_entry_survives_a_replay_and_may_be_rewritten(self):
        env = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_CODE_ONCE': str(self.root / 'docs-code-once')}
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60', env=env)
        self.assertIn('HOLD: ' + wl.SECURITY_PENDING, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        first, second = self.docs_rows(state)
        digest = hashlib.sha256((self.workspace / 'CHANGELOG.md').read_bytes()).hexdigest()
        self.assertEqual((first['route'], first['docs_paths'], first['docs_written']),
                         ('EXEC', ['CHANGELOG.md', 'sum_ints.py'], {'CHANGELOG.md': digest}))
        self.assertEqual(state['lifecycle']['docs_owned'], {'CHANGELOG.md': digest})
        self.assertEqual((second['route'], second['docs_paths'], second['epoch']), ('SECURITY', [], 1))   # entry check passed

    def test_docs_paths_are_exact_documentation_files_and_an_empty_docs_file_turns_the_default_off(self):
        co = rc.Coordinator(self.args('--docs-file', ''))
        self.assertEqual((co.state['config']['docs_file'], co.state['config']['docs_allowlist']), ('', []))
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs' / 'link.md').symlink_to(self.workspace / 'sum_ints.py')
        for extra, message in ((('--docs-allowlist', 'sum_ints.py'), 'not a documentation path: sum_ints.py'),
                               (('--docs-allowlist', 'docs/link.md'), 'reached without symlinks: docs/link.md'),
                               (('--docs-allowlist', 'CLAUDE.md'), 'not a documentation path: CLAUDE.md'),
                               (('--docs-allowlist', '.claude-plugin/README.md'),
                                'not a documentation path: .claude-plugin/README.md')):
            with self.subTest(extra=extra):
                self.run_dir = self.root / extra[1].replace('/', '-')
                with self.assertRaisesRegex(ValueError, message):
                    rc.Coordinator(self.args(*extra))

    def start_docs_attempt(self, co):
        tree, manifest = rc.git_snapshot(self.workspace)
        co.state['lifecycle'].update(stage='DOCS', candidate_oid=tree)
        request = wl.stage_request(co.state['lifecycle'], 'docs-writer')
        co.state['lifecycle']['writer_git'] = {**co._writer_git_state(), 'sequence': co.state['sequence']}
        co.state['lifecycle'] = rc.lifecycle_spine.begin(co.state['lifecycle'], request)
        co.evidence.mkdir(parents=True, exist_ok=True)
        return request, co.evidence / f"{request['request_id']}-docs-base.json", manifest

    def test_a_recorded_docs_writer_turn_is_reused_and_a_deletion_replays_exec(self):
        co = rc.Coordinator(self.args())
        request, base, manifest = self.start_docs_attempt(co)
        rc.atomic_json(base, manifest)
        (self.workspace / 'tracked.txt').unlink()   # the writer deleted a tracked file, then the coordinator crashed
        co.state['turns'].append({'role': 'author', 'phase': 'DOCS', 'sequence': co.state['sequence'] + 1,
                                  'answer': {'status': 'READY', 'body': 'done'},
                                  'snapshot_after': rc.git_snapshot(self.workspace)[0]})
        rounds = co.state['exec_rounds']
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('second docs writer dispatch')):
            co.worktree_docs_turn()
        [docs] = self.docs_rows(co.state)
        self.assertEqual((docs['request_id'], docs['route'], docs['docs_paths'], docs['sequence']),
                         (request['request_id'], 'EXEC', ['tracked.txt'], co.state['sequence'] + 1))
        self.assertEqual((co.state['next'], co.state['exec_rounds'], co.state['lifecycle']['epoch']), ('reviewer', rounds + 1, 1))

    def test_a_missing_or_foreign_docs_base_holds_before_the_writer(self):
        for write, message in ((None, 'DOCS base manifest is unreadable'),
                               ([['other.md', 'x']], 'DOCS base manifest differs from the POLISH-Q-approved tree')):
            with self.subTest(message=message):
                self.run_dir = self.root / ('missing' if write is None else 'foreign')
                co = rc.Coordinator(self.args())
                _, base, _ = self.start_docs_attempt(co)
                if write is not None:
                    rc.atomic_json(base, write)
                with mock.patch.object(co, 'invoke', side_effect=AssertionError('docs writer dispatched')), \
                        self.assertRaisesRegex(RuntimeError, message):
                    co.worktree_docs_turn()

    def test_the_docs_hold_set(self):
        for path, value, denied in (('CLAUDE.md', 'x', True), ('pkg/AGENTS.md', 'x', True), ('agents/new.md', 'x', True),
                                    ('docs/protocol/loading.md', 'x', True), ('sub/.claude/settings.json', 'x', True),
                                    ('.review-loop/config.md', 'x', True), ('.gitignore', 'x', True),
                                    ('.claude-plugin/plugin.json', 'x', True), ('docs/link.md', 'link:../../x', True),
                                    ('README.md', 'x', False), ('docs/guide.md', 'x', False), ('src/a.py', 'x', False),
                                    ('CHANGELOG.md', None, False)):
            with self.subTest(path=path):
                self.assertEqual(wl.docs_denied(path, value), denied)

    def test_docs_refuses_an_exec_change_that_already_touches_its_allowlist(self):
        co = rc.Coordinator(self.args())
        (self.workspace / 'CHANGELOG.md').write_text('written by the EXEC author\n')
        tree, manifest = rc.git_snapshot(self.workspace)
        co.state['lifecycle'].update(stage='DOCS', candidate_oid=tree)
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('docs writer dispatched')), \
                self.assertRaisesRegex(RuntimeError, 'already touches docs allowlist paths: CHANGELOG.md'):
            co.worktree_docs_turn()
        co.state['lifecycle']['docs_owned'] = {'CHANGELOG.md': dict(manifest)['CHANGELOG.md']}
        with mock.patch.object(co, 'invoke', side_effect=RuntimeError('docs writer dispatched')), \
                self.assertRaisesRegex(RuntimeError, 'docs writer dispatched'):
            co.worktree_docs_turn()   # DOCS's own entry, reviewed by the EXEC replay, may be rewritten

    def test_docs_refuses_a_staged_deletion_or_rename_of_a_reserved_path(self):
        git = ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test']
        (self.workspace / 'CHANGELOG.md').write_text('# Changelog\n')
        rc.subprocess.run(['git', 'add', 'CHANGELOG.md'], cwd=self.workspace, check=True)
        rc.subprocess.run([*git, 'commit', '-qm', 'changelog'], cwd=self.workspace, check=True)
        for command in (['git', 'rm', '-q', 'CHANGELOG.md'], ['git', 'mv', 'CHANGELOG.md', 'HISTORY.md']):
            with self.subTest(command=command[1]):
                self.run_dir = self.root / command[1]
                co = rc.Coordinator(self.args())
                rc.subprocess.run(command, cwd=self.workspace, check=True)   # the EXEC author's git operation
                co.state['lifecycle'].update(stage='DOCS', candidate_oid=rc.git_snapshot(self.workspace)[0])
                with mock.patch.object(co, 'invoke', side_effect=AssertionError('docs writer dispatched')), \
                        self.assertRaisesRegex(RuntimeError, 'already touches docs allowlist paths: CHANGELOG.md'):
                    co.worktree_docs_turn()
                rc.subprocess.run(['git', 'reset', '-q', '--hard', 'HEAD'], cwd=self.workspace, check=True)

    def test_a_docs_writer_that_moves_the_index_holds(self):
        co = rc.Coordinator(self.args())
        _, base, manifest = self.start_docs_attempt(co)
        rc.atomic_json(base, manifest)
        (self.workspace / 'tracked.txt').write_text('staged by the docs writer\n')
        rc.subprocess.run(['git', 'add', 'tracked.txt'], cwd=self.workspace, check=True)
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('dispatched past the guard')), \
                self.assertRaisesRegex(RuntimeError, 'docs writer changed HEAD, refs or the index'):
            co.worktree_docs_turn()

    def test_specialist_caps_hold_before_dispatch(self):
        for counts, calls in (({'code-reviewer': 4}, 0), ({'code-reviewer': 3}, 0), ({}, 31)):   # room for 2 dispatches
            with self.subTest(counts=counts, calls=calls):
                self.run_dir = self.root / f'cap-{calls}'
                co = rc.Coordinator(self.args())
                co.state['lifecycle'].update(stage='POLISH-Q', candidate_oid=rc.git_snapshot(self.workspace)[0],
                                             specialist_counts=dict(counts), polish_calls=calls)
                with mock.patch.object(co, 'invoke', side_effect=AssertionError('dispatched past the cap')), \
                        self.assertRaisesRegex(RuntimeError, 'POLISH-Q specialist budget exhausted: code-reviewer'):
                    co.worktree_polish_turn()

    def test_specialist_reservations_are_reconciled_and_the_budget_is_preflighted(self):
        co = rc.Coordinator(self.args())
        co.state['lifecycle'].update(stage='POLISH-Q', candidate_oid=rc.git_snapshot(self.workspace)[0])
        with mock.patch.object(co, 'invoke', side_effect=RuntimeError('invocation limit reached')), \
                self.assertRaisesRegex(RuntimeError, 'invocation limit reached'):
            co.worktree_polish_turn()   # refused before any dispatch: the reservation is given back
        self.assertEqual((co.state['lifecycle']['specialist_counts']['code-reviewer'], co.state['lifecycle']['polish_calls']), (0, 0))
        co.state['lifecycle']['pending'] = None
        co.args.max_invocations = co.state['invocations_used'] + 2
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('dispatched without budget')), \
                self.assertRaisesRegex(RuntimeError, 'POLISH-Q needs at least 3 more invocations'):
            co.worktree_polish_turn()

    def test_accept_is_refused_for_worktree_runs_even_on_override_paths(self):
        co = rc.Coordinator(self.args())
        for status, kind in (('DONE', None), ('HOLD', 'rejection_limit')):
            with self.subTest(status=status):
                co.state.update(status=status, terminal_hold_kind=kind)
                with self.assertRaisesRegex(ValueError, 'worktree lifecycle accept is not available before W3b'):
                    co.accept()
                self.assertNotEqual(co.state['status'], 'ACCEPTED')

    def test_open_blocker_at_convergence_starts_a_repair_round(self):
        co = rc.Coordinator(self.args())
        co.state['gate_ran'] = True
        with mock.patch.object(co, 'blocking_open_findings', return_value=[{'id': 'F007', 'summary': 'gate blocker'}]):
            self.assertEqual(co.start_polish_or_done(), 'ACTIVE')   # the author repairs; reviewer and a new gate follow
        self.assertEqual((co.state['next'], co.state['phase'], co.state['gate_ran']), ('author', 'EXEC', False))
        self.assertEqual(json.loads(co.state['delivered_review'])['findings'][0]['id'], 'F007')
        self.assertEqual((co.state['lifecycle']['stage'], co.state['lifecycle']['candidate_oid']), ('EXEC', None))
        co.state.update(gate_ran=True, exec_rounds=co.exec_round_limit())
        with mock.patch.object(co, 'blocking_open_findings', return_value=[{'id': 'F007', 'summary': 'gate blocker'}]):
            self.assertEqual(co.start_polish_or_done(), 'HOLD')
        self.assertEqual((co.state['hold_reason'], co.state['round_limit_hold']['hold_reason']),
                         ('EXEC round limit reached', 'EXEC round limit reached'))

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
