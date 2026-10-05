"""Batch W1a (ADR-11, docs/e2e-6): worktree lifecycle activation on the real path."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

from paired_session import test_real_coordinator as trc
from paired_session import worktree_lifecycle as wl

rc = trc.rc
DONE = 'DONE (acceptance pending)'
COVERING_GITIGNORE = '\n'.join((   # the legacy sensitive patterns (tests/security_preflight_test.py), so preflight is clean
    '__pycache__/', '*.cache', '.review-loop/', '.env', '.env.*', '*.env', '!.env.example', '!.env.sample', '*.pem',
    '*.key', '*.crt', '*.cert', '*.cer', '*.p12', '*.pfx', '*.jks', '*.keystore', '*.ppk', 'id_rsa*', 'id_dsa*',
    'id_ecdsa*', 'id_ed25519*', '*.asc', '*.gpg', '*.pgp', '*credentials*', '!*credentials.example*',
    '!*credentials.sample*', 'service-account*.json', '.aws/', '.gcloud/', '*secret*', '!*secret.example*',
    '!*secret.sample*', 'secrets.*', '!secrets.example*', '!secrets.sample*', '*.sqlite', '*.sqlite3', '*.db', '*.dump',
    '*.sql.gz', '*.map', '*.tfstate', '*.tfstate.*', '*.tfvars', '!*.tfvars.example', '.terraform/', '*.log', 'logs/', ''))
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action', 'coordinator')


class WorktreeLifecycleActivationTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def setUp(self):
        trc.RealCoordinatorTests.setUp(self)
        (self.workspace / '.gitignore').write_text(COVERING_GITIGNORE)
        rc.subprocess.run(['git', 'commit', '-qam', 'ignore sensitive files'], cwd=self.workspace, check=True)

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

    def test_d7_refuses_waivers_only_in_strict_mode(self):   # D-EFF: an efficient run needs no waiver and records none
        waivers = ('--accept-unverified-claude-author', '--accept-probe-skip', '--reason', 'owner opt-in')

        def default_mode(*extra, action='run'):   # neither --strict nor a profile mode: the product default decides
            args = self.args(*extra, action=action)
            args.safety_mode = None
            return args
        for module in {id(m): m for m in (rc, sys.modules.get('paired_session.coordinator')) if m}.values():
            pin = mock.patch.object(module, 'DEFAULT_SAFETY_MODE', 'efficient')
            pin.start()
            self.addCleanup(pin.stop)
        argv = [a for a in self.command('--lifecycle-mode', 'on', *waivers) if a != '--strict'][2:]
        out = io.StringIO()
        with mock.patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), \
                mock.patch.object(rc.Coordinator, 'drive', return_value='DONE'), contextlib.redirect_stdout(out):
            code = rc.main(argv)   # main() is where a strict run would record the waivers
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('needs no probe waiver', out.getvalue())
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((state['config']['safety_mode'], state['config']['lifecycle_mode']), ('efficient', 'on'))
        self.assertFalse({'claude_author_override', 'probe_skip_override'} & state.keys())
        rc.Coordinator(default_mode(*waivers, action='resume'))   # the saved efficient run is not refused either
        self.run_dir = self.root / 'strict-run'
        rc.Coordinator(self.args())   # --strict from the harness command
        with self.assertRaisesRegex(ValueError, 'worktree lifecycle refuses --accept-unverified-claude-author'):
            rc.Coordinator(default_mode(*waivers, action='resume'))   # a saved strict run keeps D-7 without --strict
        self.run_dir = self.root / 'profile-strict-run'
        profile = self.root / 'strict-profile.json'
        profile.write_text(json.dumps({'safety_mode': 'strict'}))
        argv = [a for a in self.command('--lifecycle-mode', 'on', '--config', str(profile), *waivers) if a != '--strict'][2:]
        with self.assertRaisesRegex(ValueError, 'worktree lifecycle refuses --accept-unverified-claude-author'):
            rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))   # the operator profile selects strict
        self.assertFalse((self.run_dir / 'state.json').exists())

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

    # --- INT-2c: D-7 is strict only (D-EFF, docs/efficient-mode.md §6) ---------------------------------------------------------
    def efficient_command(self, *extra, action='run'):
        command = [arg for arg in self.command('--lifecycle-mode', 'on', *extra) if arg != '--strict']
        command[2] = action
        return command

    def test_a_strict_worktree_run_refuses_a_waiver_on_resume_too(self):
        rc.Coordinator(self.args())
        with self.assertRaisesRegex(ValueError, 'worktree lifecycle refuses --accept-probe-skip'):
            rc.Coordinator(self.args('--accept-probe-skip', '--reason', 'owner accepted', action='resume'))

    def test_an_efficient_worktree_run_notes_a_waiver_and_records_none(self):
        for module in {id(m): m for m in (rc, sys.modules.get('paired_session.coordinator')) if m}.values():
            pin = mock.patch.object(module, 'DEFAULT_SAFETY_MODE', 'efficient')   # the product default, which the harness pins
            pin.start()
            self.addCleanup(pin.stop)
        for action, flag in (('run', '--accept-probe-skip'), ('resume', '--accept-unverified-claude-author')):
            with self.subTest(action=action), mock.patch.object(rc.Coordinator, 'drive', return_value='DONE'), \
                    mock.patch.object(rc.Coordinator, 'resume', return_value='DONE'), \
                    mock.patch('sys.stdout', new_callable=io.StringIO) as out:
                code = rc.main(self.efficient_command(flag, '--reason', 'checked', action=action)[2:])
                self.assertEqual(code, 0, out.getvalue())
                self.assertIn('needs no probe waiver', out.getvalue())
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((state['config']['safety_mode'], state['lifecycle']['format']), ('efficient', 'worktree'))
        self.assertFalse({'probe_skip_override', 'claude_author_override'} & state.keys())
        with self.assertRaisesRegex(ValueError, 'worktree lifecycle refuses --accept-probe-skip'):   # no turn yet: --strict may upgrade
            rc.Coordinator(self.args('--accept-probe-skip', '--reason', 'checked'))
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['config']['safety_mode'], 'efficient')   # refused: unchanged

    def test_an_efficient_worktree_run_needs_no_probe_and_reaches_done(self):
        result = rc.subprocess.run(self.efficient_command(), cwd=self.root, text=True, stdout=rc.subprocess.PIPE,
                                   stderr=rc.subprocess.PIPE)   # no --strict, no --skip-probe, no permission-probe
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(DONE, result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((state['config']['safety_mode'], state['lifecycle']['stage']), ('efficient', 'DONE'))
        self.assertFalse((self.run_dir / 'permission-probe.json').exists())
        self.assertEqual([row['stage'] for row in state['lifecycle']['receipts']], ['FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])

    def test_a_w_read_only_turn_that_edits_the_workspace_is_voided_restored_and_re_dispatched(self):   # category A in W
        marker = self.root / 'mutated-once'
        result = self.run_coordinator('--lifecycle-mode', 'on', env={
            'FAKE_MUTATION': 'echo', 'FAKE_MUTATION_ROLE': 'Role: security reviewer,', 'FAKE_MUTATION_ONCE': str(marker)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('re-dispatching the reviewer turn once', result.stdout)
        self.assertTrue(marker.exists())
        self.assertFalse((self.workspace / 'forbidden.txt').exists())
        state = json.loads((self.run_dir / 'state.json').read_text())
        void, again = [row for row in state['turns'] if row['phase'] == 'SECURITY']
        self.assertEqual((void['voided']['restored'], 'voided' in again), (True, False))
        [security] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
        self.assertEqual((security['status'], security['review']['sequence']), ('READY', again['sequence']))

    def test_a_w_re_dispatch_counts_against_the_stage_cap(self):   # INT-2c LOW-1
        co = rc.Coordinator(self.args())
        voided = RuntimeError('reviewer mutated workspace; the turn is void and the workspace was restored')
        co.state['turns'] += [{'phase': 'SECURITY'}, {'phase': 'SECURITY'}]
        co._redispatch_budget('SECURITY', voided)   # 2 + this one fit the cap of 3
        co.state['spawn_failures'] = [{'phase': 'SECURITY', 'invocation_budget_counted': False}]   # counted, as _security_review_turn does
        with self.assertRaisesRegex(RuntimeError, r'SECURITY budget has no room to re-dispatch the void turn \(reviewer mutated'):
            co._redispatch_budget('SECURITY', voided)
        cap = rc.budget_policy.BUDGET_CAPS['POLISH-Q'][0]
        co.state['turns'] += [{'phase': 'POLISH-Q'}] * (cap - 1) + [{'phase': 'POLISH-Q', 'invocation_budget_counted': False}]
        co._redispatch_budget('POLISH-Q', voided)   # POLISH-Q skips refunded rows, like polish_calls
        co.state['turns'].append({'phase': 'POLISH-Q'})
        with self.assertRaisesRegex(RuntimeError, 'POLISH-Q budget has no room'):
            co._redispatch_budget('POLISH-Q', voided)
        co._redispatch_budget('EXEC', voided)   # only the W stages have a stage cap here

    def finish_rows(self, state):
        return [row for row in state['lifecycle']['receipts'] if row['stage'] == 'FINISH']

    # --- FIELD-19: .gitignore coverage is known at start; the SECURITY HOLD names a recovery that works ------------------------
    def test_an_uncovered_gitignore_warns_at_start_and_an_in_run_edit_recovers_the_security_hold(self):
        (self.workspace / '.gitignore').write_text('__pycache__/\n*.cache\n.review-loop/\n')
        rc.subprocess.run(['git', 'commit', '-qam', 'narrow ignore rules'], cwd=self.workspace, check=True)
        held = self.run_coordinator('--lifecycle-mode', 'on')   # the run proceeds: a warning, not a gate
        missing = ['environment-and-config', 'keys-and-certificates', 'ssh-private-keys']
        self.assertIn('WARNING: the tracked .gitignore does not cover environment-and-config, keys-and-certificates, '
                      'ssh-private-keys', held.stdout, held.stdout + held.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        start = state['lifecycle']['ignore_coverage_at_start']
        self.assertTrue(start['checked'])
        self.assertTrue(set(missing) <= set(start['uncovered']))
        [security] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
        self.assertEqual((state['status'], security['preflight']['status']), ('HOLD', 'review-required'))
        self.assertEqual(security['preflight']['uncovered_ignore'], start['uncovered'])   # one computation, one answer
        self.assertIn('To recover, add the patterns to the tracked .gitignore in the worktree, neither committed nor staged',
                      state['hold_reason'])
        (self.workspace / '.gitignore').write_text(COVERING_GITIGNORE)   # the operator's in-run edit, not committed
        resumed = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        security = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY'][-1]
        self.assertEqual((security['preflight']['status'], security['preflight']['uncovered_ignore']), ('clean', []))
        self.assertEqual(rc.subprocess.run(['git', 'status', '--porcelain', '.gitignore'], cwd=self.workspace, check=True,
                                           capture_output=True, text=True).stdout.strip(), 'M .gitignore')   # ships with the work

    def test_a_covering_gitignore_gives_no_start_warning(self):
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            co = rc.Coordinator(self.args())
        self.assertEqual(co.state['lifecycle']['ignore_coverage_at_start'], {'checked': True, 'uncovered': []})
        self.assertNotIn('.gitignore', out.getvalue())
        script = rc.Coordinator._security_script   # a failed check only notes it; the run is created

        def failing(me, name, *args):
            if name == 'security_preflight.py':
                raise RuntimeError('security_preflight.py could not run: no interpreter')
            return script(me, name, *args)
        out, self.run_dir = io.StringIO(), self.root / 'unchecked'
        with mock.patch.object(rc.Coordinator, '_security_script', autospec=True, side_effect=failing), mock.patch('sys.stdout', out):
            co = rc.Coordinator(self.args())
        self.assertEqual(co.state['lifecycle']['ignore_coverage_at_start'], {'checked': False})
        self.assertIn('NOTE: .gitignore coverage could not be checked at start', out.getvalue())

    def test_a_no_op_worktree_run_goes_through_every_stage_and_reaches_done_only_after_security(self):
        result = self.run_coordinator('--lifecycle-mode', 'on')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(DONE, result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        tree = rc.git_snapshot(self.workspace)[0]
        self.assertEqual((state['status'], state['acceptance_state'], state['lifecycle']['stage'], state['next']),
                         ('DONE', 'PENDING', 'DONE', 'done'))
        self.assertEqual([row['stage'] for row in state['lifecycle']['receipts']],
                         ['FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])   # DONE only after SECURITY
        [security] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
        self.assertEqual((security['status'], security['route'], security['review']['status'], security['review']['finding_ids']),
                         ('READY', 'DONE', 'APPROVE', []))
        review = next(row for row in state['turns'] if row['sequence'] == security['review']['sequence'])
        self.assertEqual((review['role'], review['phase'], review['fresh'], review['snapshot_before']),
                         ('reviewer', 'SECURITY', True, tree))
        prompt = (self.run_dir / 'evidence' / f"{review['sequence']:03d}-security-reviewer.prompt.txt").read_text()
        for needle in ('Role: security reviewer, fresh. Phase: SECURITY.', 'delta.patch', 'any finding stops delivery'):
            self.assertIn(needle, prompt)
        [docs] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS']
        self.assertEqual((docs['status'], docs['route'], docs['docs_paths'], docs['candidate_oid'], docs['output_oid']),
                         ('READY', 'SECURITY', [], tree, tree))
        writer = next(row for row in state['turns'] if row['sequence'] == docs['sequence'])
        self.assertEqual((writer['role'], writer['phase'], writer['fresh']), ('author', 'DOCS', True))
        prompt = (self.run_dir / 'evidence' / f"{docs['sequence']:03d}-docs-author.prompt.txt").read_text()
        self.assertIn('Role: docs writer, fresh. Phase: DOCS.', prompt)
        self.assertIn('Documentation paths you may write: CHANGELOG.md.', prompt)
        self.assertEqual(state['config']['docs_file'], str((self.workspace / 'CHANGELOG.md').resolve()))   # doc 6 W default
        [security] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
        preflight = security['preflight']   # a no-op run still scans
        self.assertEqual((security['sensitive_paths'], preflight['exit'], preflight['status'], preflight['reason']),
                         ([], 0, 'clean', None))
        self.assertGreaterEqual(preflight['scanned_files'], 3)
        self.assertTrue(Path(preflight['report']).is_file())
        baseline = json.loads(Path(state['lifecycle']['security_baseline']['path']).read_text())
        self.assertNotIn('sum_ints.py', baseline['state']['worktree'])   # captured before the author's first write
        self.assertNotIn('docs_file', json.loads((Path(rc.__file__).parent / 'paired-session-config.example.json').read_text()))
        for row in (next(row for row in state['turns'] if row['phase'] == 'FINISH'),
                    next(row for row in state['turns'] if row['role'] == 'author' and row['phase'] == 'EXEC')):
            prompt = (self.run_dir / 'evidence' / f"{row['sequence']:03d}-{row['phase'].lower()}-author.prompt.txt").read_text()
            self.assertIn('Reserved for the DOCS stage; do not edit: CHANGELOG.md.', prompt)   # earlier writers know
        [polish] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'POLISH-Q']
        names = ['python-reviewer', 'code-reviewer', 'silent-failure-hunter', 'pr-test-analyzer']   # sum_ints.py is Python
        self.assertEqual({row['observed_test'] for row in polish['specialist_turns']},
                         {state['config']['test_command']})   # W2a-1 L-3: recorded, not enforced
        self.assertNotIn('review', [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS'][0])
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
        again = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn(DONE, again.stdout)
        self.assertEqual(len(json.loads((self.run_dir / 'state.json').read_text())['turns']), len(state['turns']))

    def test_a_finish_write_reopens_exec_review_and_gate_before_finishing_again(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_FINISH_WRITE': '1'})
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
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
            self.assertEqual(crashed.resume(retry_uncertain=True), 'DONE')
        rows = self.finish_rows(crashed.state)
        self.assertEqual([(row['request_id'], row['status']) for row in rows],
                         [('w-FINISH-0-0', 'HOLD'), (request['request_id'], 'READY')])
        self.assertEqual(request['request_id'], 'w-FINISH-0-1')
        self.assertIsNone(crashed.state['lifecycle']['pending'])
        self.assertEqual(crashed.state['abandoned_turns'][-1]['phase'], 'FINISH')

    def test_a_finisher_that_commits_holds_without_a_receipt(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_FINISH_COMMIT': '1'})
        self.assertIn('HOLD: finisher changed HEAD, refs or the index', result.stdout, result.stdout + result.stderr)
        self.assertIn('(the turn failed: author changed HEAD or the branch', result.stdout)   # INT-2c: the original error stays
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
        self.assertIn(DONE, restored.stdout, restored.stdout + restored.stderr)
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
        self.assertIn(DONE, again.stdout, again.stdout + again.stderr)
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
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
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
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)   # the retry made tool calls
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
        self.assertIn(DONE, again.stdout, again.stdout + again.stderr)   # fix leg, replay, clean
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
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        [polish] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'POLISH-Q']
        self.assertEqual((polish['status'], polish['specialists'], polish['skipped']), ('READY', [], True))
        self.assertFalse([row for row in state['turns'] if row['phase'] == 'POLISH-Q'])

    def docs_rows(self, state):
        return [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS']

    def test_an_allowlisted_docs_write_gets_a_fresh_docs_review_with_an_observed_test_and_advances(self):
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'})
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        [docs] = self.docs_rows(state)
        digest = hashlib.sha256((self.workspace / 'CHANGELOG.md').read_bytes()).hexdigest()
        self.assertEqual((docs['status'], docs['route'], docs['docs_paths'], docs['docs_written'], docs['output_oid']),
                         ('READY', 'SECURITY', ['CHANGELOG.md'], {'CHANGELOG.md': digest}, rc.git_snapshot(self.workspace)[0]))
        review = docs['review']
        self.assertEqual((review['status'], review['observed_test'], review['finding_ids']),
                         ('APPROVE', state['config']['test_command'], []))
        self.assertEqual(state['lifecycle']['docs_owned'], {'CHANGELOG.md': digest})   # approved: DOCS owns it
        turn = next(row for row in state['turns'] if row['sequence'] == review['sequence'])
        self.assertEqual((turn['role'], turn['phase'], turn['fresh'], turn['snapshot_before']),
                         ('reviewer', 'DOCS', True, docs['output_oid']))
        prompt = (self.run_dir / 'evidence' / f"{review['sequence']:03d}-docs-reviewer.prompt.txt").read_text()
        for needle in ('Role: docs reviewer, fresh. Phase: DOCS.', 'Documentation written by the DOCS stage: CHANGELOG.md',
                       'delta.patch', 'Run this test command exactly as written in one Bash call: ' +
                       state['config']['test_command']):
            self.assertIn(needle, prompt)

    def test_a_docs_review_without_the_retest_holds_and_resume_reviews_again(self):
        env = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_REVIEW_NO_TEST_ONCE': str(self.root / 'no-test')}
        held = self.run_coordinator('--lifecycle-mode', 'on', env=env)
        self.assertIn('HOLD: DOCS reviewer did not observe a successful run of the configured test command', held.stdout,
                      held.stdout + held.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((self.docs_rows(state), state['lifecycle']['pending']['stage']), ([], 'DOCS'))   # no receipt
        again = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertIn(DONE, again.stdout, again.stdout + again.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        [docs] = self.docs_rows(state)
        writers = [row for row in state['turns'] if row['role'] == 'author' and row['phase'] == 'DOCS']
        reviews = [row for row in state['turns'] if row['role'] == 'reviewer' and row['phase'] == 'DOCS']
        self.assertEqual((len(writers), len(reviews), docs['sequence'], docs['review']['sequence']),
                         (1, 2, writers[0]['sequence'], reviews[1]['sequence']))   # the writer is reused, not re-run

    def test_the_exec_author_can_fix_a_docs_finding_in_a_docs_owned_entry(self):
        env = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_REVIEW_BLOCK_ONCE': str(self.root / 'docs-block'),
               'FAKE_DOCS_FINDING_STILL_OPEN_ONCE': str(self.root / 'docs-open')}
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60', '--max-exec-rounds', '6',
                                      env=env)
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        first, second = self.docs_rows(state)
        fix = next(turn for turn in state['turns'] if turn['role'] == 'author' and turn['phase'] == 'EXEC'
                   and first['review']['sequence'] < turn['sequence'] < second['sequence'])
        prompt = (self.run_dir / 'evidence' / f"{fix['sequence']:03d}-exec-author.prompt.txt").read_text()
        self.assertIn('Written by the DOCS stage; edit only to fix a delivered docs finding: CHANGELOG.md.', prompt)
        self.assertNotIn('Reserved for the DOCS stage', prompt)   # CHANGELOG.md is the whole allowlist and DOCS owns it
        self.assertEqual((second['route'], second['review']['status']), ('SECURITY', 'APPROVE'))   # entry check passed
        [row] = [row for row in state['finding_ledger'] if row['id'] in first['review']['finding_ids']]
        self.assertEqual(row['status'], 'fixed')

    def docs_review_with(self, answer, findings_before=()):
        co = rc.Coordinator(self.args())
        tree = rc.git_snapshot(self.workspace)[0]
        def invoke(role, phase, prompt, schema, fresh=False):
            co.state['sequence'] += 1
            co.state['turns'].append({'sequence': co.state['sequence'], 'role': role, 'phase': phase, 'snapshot_before': tree})
            return {'answer': answer, 'sequence': co.state['sequence'], 'snapshot': tree, 'role': role}
        with mock.patch.object(co, 'materialize_review_context'), mock.patch.object(co, 'render'), \
                mock.patch.object(co, 'invoke', side_effect=invoke):
            return co, co._docs_review_turn(tree, ['CHANGELOG.md'])

    def test_docs_review_verdicts_follow_the_ledger_blocking_rule(self):
        test = {'command': self.args().test_command, 'exit_code': 0, 'output': 'OK'}
        minor = {'severity': 'MINOR', 'file': 'CHANGELOG.md', 'summary': 'wording', 'failure_scenario': 'none'}
        for status, finding, verdict in (('APPROVE', {**minor, 'security': True}, 'REVISE'),   # a security flag blocks
                                         ('REVISE', minor, 'APPROVE')):   # a MINOR-only REVISE is advisory
            with self.subTest(status=status, finding=finding):
                self.run_dir = self.root / verdict
                co, review = self.docs_review_with({'status': status, 'full_review': [finding],
                                                    'observed_commands': [test]})
                self.assertEqual(review['status'], verdict)
                [row] = [row for row in co.state['finding_ledger'] if row['id'] in review['finding_ids']]
                self.assertEqual((row['source'], row.get('owner_role')), ('docs-reviewer', None))
        self.run_dir = self.root / 'empty'
        with self.assertRaisesRegex(RuntimeError, 'docs reviewer returned REVISE without a usable review'):
            self.docs_review_with({'status': 'REVISE', 'full_review': [], 'observed_commands': [test]})

    def test_the_docs_budget_caps_writer_and_review_dispatches(self):
        co = rc.Coordinator(self.args())
        co.state['turns'].extend({'sequence': 900 + index, 'role': 'author', 'phase': 'DOCS'} for index in range(7))
        with mock.patch.object(co, 'materialize_review_context'), \
                mock.patch.object(co, 'invoke', side_effect=AssertionError('dispatched past the DOCS budget')), \
                self.assertRaisesRegex(RuntimeError, r'DOCS budget exhausted \(7 writer and review calls'):
            co._docs_review_turn(rc.git_snapshot(self.workspace)[0], ['CHANGELOG.md'])

    def test_a_docs_review_revise_replays_exec_with_its_findings_then_reviews_again(self):
        env = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_REVIEW_BLOCK_ONCE': str(self.root / 'docs-block')}
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60', env=env)
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        first, second = self.docs_rows(state)
        [finding] = first['review']['finding_ids']
        self.assertEqual((first['route'], first['review']['status'], second['route'], second['review']['status']),
                         ('EXEC', 'REVISE', 'SECURITY', 'APPROVE'))   # the owned entry is reviewed again
        [row] = [row for row in state['finding_ledger'] if row['id'] == finding]
        self.assertEqual((row['source'], row['severity'], row['status']), ('docs-reviewer', 'MAJOR', 'fixed'))
        self.assertEqual(list(state['lifecycle']['docs_owned']), ['CHANGELOG.md'])
        between = [(turn['role'], turn['phase']) for turn in state['turns']
                   if first['review']['sequence'] < turn['sequence'] < second['sequence']]
        for step in (('reviewer', 'EXEC'), ('gate', 'EXEC'), ('author', 'FINISH')):
            self.assertIn(step, between)   # the persistent reviewer disposes the docs finding, then a new gate

    def test_a_docs_comment_write_replays_exec_review_and_gate(self):
        env = {'FAKE_DOCS_CODE_ONCE': str(self.root / 'docs-code-once')}
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60', env=env)
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
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
        self.assertIn(DONE, restored.stdout, restored.stdout + restored.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual([(row['request_id'], row['route']) for row in self.docs_rows(state)],
                         [('w-DOCS-0-0', 'HOLD'), ('w-DOCS-0-1', 'SECURITY')])

    def test_docs_own_entry_survives_a_replay_and_may_be_rewritten(self):
        env = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_CODE_ONCE': str(self.root / 'docs-code-once')}
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60', env=env)
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        first, second = self.docs_rows(state)
        digest = hashlib.sha256((self.workspace / 'CHANGELOG.md').read_bytes()).hexdigest()
        self.assertEqual((first['route'], first['docs_paths'], first['docs_written']),
                         ('EXEC', ['CHANGELOG.md', 'sum_ints.py'], {'CHANGELOG.md': digest}))
        self.assertEqual(state['lifecycle']['docs_owned'], {'CHANGELOG.md': digest})
        self.assertEqual((second['route'], second['docs_paths'], second['epoch']), ('SECURITY', [], 1))   # entry check passed
        self.assertEqual(second['review']['status'], 'APPROVE')   # a no-op DOCS still reviews its owned entry

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

    def test_a_docs_allowlist_path_under_a_symlinked_parent_holds_at_the_docs_entry(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs' / 'guide.md').write_text('# Guide\n')
        co = rc.Coordinator(self.args('--docs-allowlist', 'docs/guide.md'))
        outside = self.root / 'outside'
        outside.mkdir()
        (self.workspace / 'docs' / 'guide.md').rename(outside / 'guide.md')
        (self.workspace / 'docs').rmdir()
        (self.workspace / 'docs').symlink_to(outside)   # the EXEC author's replacement of the parent directory
        co.state['lifecycle'].update(stage='DOCS', candidate_oid=rc.git_snapshot(self.workspace)[0])
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('docs writer dispatched')), \
                self.assertRaisesRegex(RuntimeError, 'now a directory or reached through a symlink: docs/guide.md'):
            co.worktree_docs_turn()

    def test_security_holds_on_a_sensitive_path_and_on_secret_content(self):
        key = 'AKIA' + 'ABCDEFGHIJKLMNOP'   # an AWS access key id shape, built at runtime
        for name, content, needle in (('deploy.pem', 'not a real key\n', 'SECURITY sensitive paths: deploy.pem (key-certificate)'),
                                      ('settings.txt', f'aws_key = {key}\n', 'aws-access-key-id in settings.txt')):
            with self.subTest(name=name):
                self.run_dir = self.root / name
                (self.workspace / name).write_text(content)
                rc.subprocess.run(['git', 'add', '-f', name], cwd=self.workspace, check=True)
                rc.subprocess.run(['git', 'commit', '-qm', 'add ' + name], cwd=self.workspace, check=True)
                result = self.run_coordinator('--lifecycle-mode', 'on')
                self.assertIn(needle, result.stdout, result.stdout + result.stderr)
                self.assertNotIn(key, result.stdout + result.stderr)   # values are never printed
                state = json.loads((self.run_dir / 'state.json').read_text())
                [security] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
                self.assertEqual((security['status'], state['status']), ('HOLD', 'HOLD'))
                self.assertEqual(security['preflight']['status'], 'blocked')
                rc.subprocess.run(['git', 'rm', '-q', name], cwd=self.workspace, check=True)
                rc.subprocess.run(['git', 'commit', '-qm', 'drop ' + name], cwd=self.workspace, check=True)

    def at_security(self):
        co = rc.Coordinator(self.args())
        tree = rc.git_snapshot(self.workspace)[0]
        co.state['lifecycle'].update(stage='SECURITY', candidate_oid=tree, receipts=[
            {'stage': 'DOCS', 'epoch': 0, 'request_id': 'w-DOCS-0-0', 'route': 'SECURITY', 'output_oid': tree}])
        co.state.update(next='security', phase='EXEC')
        return co, tree

    def test_a_tree_changed_during_security_holds_and_resume_replays_exec(self):
        co, tree = self.at_security()
        def late_write(request_id):
            (self.workspace / 'late.txt').write_text('written during SECURITY\n')
            return {'status': 'clean', 'reason': None}
        with mock.patch.object(co, '_security_preflight', side_effect=late_write):
            co.worktree_security_turn()
        self.assertIn('SECURITY tree changed during the stage', co.state['hold_reason'])
        [security] = [row for row in co.state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
        self.assertEqual((security['status'], security['candidate_oid']), ('HOLD', tree))
        rounds = co.state['exec_rounds']
        co.state['status'] = 'ACTIVE'
        co.worktree_security_turn()   # resume: the changed tree is reviewed and gated again
        self.assertEqual((co.state['next'], co.state['gate_ran'], co.state['exec_rounds'], co.state['lifecycle']['stage'],
                          co.state['lifecycle']['epoch']), ('reviewer', False, rounds + 1, 'EXEC', 1))

    def test_security_preflight_fails_closed(self):
        co, _ = self.at_security()
        failed = rc.subprocess.CompletedProcess([], 3, '', '{"error": "scanner exploded", "kind": "scan"}')
        real = co._security_script
        with mock.patch.object(co, '_security_script', side_effect=lambda script, *args: (
                failed if script == 'security_preflight.py' else real(script, *args))):
            result = co._security_preflight('w-SECURITY-0-0')
        self.assertEqual((result['exit'], result['status']), (3, 'unknown'))
        self.assertIn('security preflight unknown (exit 3); {"error": "scanner exploded"', result['reason'])
        co.state['lifecycle'].pop('security_baseline')   # a W run created before W3a
        self.assertIn('no delivery baseline (the run started before W3a)', co._security_preflight('w-SECURITY-0-1')['reason'])

    def test_the_delivery_baseline_is_captured_when_the_state_is_created(self):
        probe = rc.Coordinator(self.args(action='permission-probe'))   # before any probe turn
        baseline = probe.state['lifecycle']['security_baseline']
        self.assertEqual((probe.state['sequence'], Path(baseline['path']).parent), (0, probe.evidence))
        self.assertEqual(hashlib.sha256(Path(baseline['path']).read_bytes()).hexdigest(), baseline['sha256'])
        self.run_dir = self.root / 'refused'
        failed = rc.subprocess.CompletedProcess([], 3, '', '{"error": "nested repository", "kind": "capture"}')
        with mock.patch.object(rc.Coordinator, '_security_script', return_value=failed), \
                self.assertRaisesRegex(ValueError, r'cannot capture its delivery baseline \(delivery_scope exit 3\)'):
            rc.Coordinator(self.args())
        self.assertFalse((self.run_dir / 'state.json').exists())   # refused at the start, not at SECURITY

    def test_a_blocking_security_finding_holds_and_only_a_fix_on_a_new_tree_reaches_done(self):
        env = {'FAKE_Q_MINOR': '1', 'FAKE_Q_SEVERITY': 'CRITICAL', 'FAKE_Q_SECURITY_FLAG': '1'}
        held = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60', env=env)
        self.assertIn('HOLD: security reviewer findings: ', held.stdout, held.stdout + held.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        [finding] = [row for row in state['finding_ledger'] if row['source'] == 'security-reviewer']
        self.assertEqual((finding['owner_role'], finding['severity'], finding['security'], finding['status']),
                         ('security-reviewer', 'CRITICAL', True, 'open'))
        self.assertEqual((state['status'], state['lifecycle']['stage']), ('HOLD', 'SECURITY'))
        reviews = len([row for row in state['turns'] if row['phase'] == 'SECURITY'])
        again = self.run_operator_action('resume', '--lifecycle-mode', 'on', '--max-invocations', '60')
        self.assertIn('HOLD: security findings need a fix on a new tree, not a re-review: ' + finding['id'], again.stdout,
                      again.stdout + again.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(len([row for row in state['turns'] if row['phase'] == 'SECURITY']), reviews)
        (self.workspace / 'NOTES.md').write_text('operator fix outside the run\n')
        fixed = self.run_operator_action('resume', '--lifecycle-mode', 'on', '--max-invocations', '60')
        # the blocking security finding does not gate EXEC, POLISH-Q or DOCS; its owner closes it at SECURITY
        self.assertIn(DONE, fixed.stdout, fixed.stdout + fixed.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual([row['status'] for row in state['finding_ledger'] if row['id'] == finding['id']], ['fixed'])
        routes = [row['route'] for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
        self.assertEqual(routes, ['HOLD', 'EXEC', 'DONE'])
        self.assertIsNone(state['lifecycle']['security_hold_tree'])

    def security_turn_stub(self, co, tree, answer, tool_calls=1):
        def invoke(role, phase, prompt, schema, fresh=False):
            co.state['sequence'] += 1
            co.state['turns'].append({'sequence': co.state['sequence'], 'role': role, 'phase': phase, 'answer': answer,
                                      'snapshot_before': tree, 'observed_tool_calls': tool_calls})
            return {'answer': answer, 'sequence': co.state['sequence'], 'snapshot': tree, 'role': role}
        return invoke

    def test_a_security_reviewer_without_tool_calls_is_retried_once_and_never_touches_the_ledger(self):
        co, tree = self.at_security()
        [owned] = co.record_findings('security-reviewer', 'SECURITY', 1, [
            {'severity': 'CRITICAL', 'file': 'sum_ints.py', 'summary': 'earlier security finding'}])
        co.state['lifecycle']['security_hold_tree'] = 'the tree the finding was raised on'
        answer = {'status': 'APPROVE', 'full_review': [{'severity': 'MAJOR', 'file': 'x', 'summary': 'invented'}],
                  'prior_findings': [{'id': owned['id'], 'disposition': 'withdrawn', 'evidence': 'never looked'}]}
        ledger = json.dumps(co.state['finding_ledger'])
        with mock.patch.object(co, 'materialize_review_context'), mock.patch.object(co, 'render'), \
                mock.patch.object(co, 'invoke', side_effect=self.security_turn_stub(co, tree, answer, tool_calls=0)):
            review = co._security_review_turn(tree, 'w-SECURITY-0-0')
        self.assertEqual(review['reason'], 'security reviewer made no tool calls after one retry')
        self.assertEqual(json.dumps(co.state['finding_ledger']), ledger)   # no disposition, no invented finding
        self.assertEqual([row.get('discarded') for row in co.state['turns'] if row['phase'] == 'SECURITY'],
                         ['no tool calls'] * 2)
        self.assertEqual(co.state['lifecycle']['security_hold_tree'], 'the tree the finding was raised on')

    def test_only_the_recorded_security_turn_is_reused_and_a_malformed_one_is_dispatched_again(self):
        co, tree = self.at_security()
        good = {'status': 'APPROVE', 'full_review': [], 'prior_findings': []}
        stub = self.security_turn_stub(co, tree, good)
        stub('reviewer', 'SECURITY', '', {})   # a turn invoke rejected for its evidence contract
        co.state['turns'][-1]['verified_claims_error'] = 'claim not observed'
        calls = []
        with mock.patch.object(co, 'materialize_review_context'), mock.patch.object(co, 'render'), \
                mock.patch.object(co, 'invoke', side_effect=lambda *a, **k: calls.append(1) or stub(*a, **k)):
            first = co._security_review_turn(tree, 'w-SECURITY-0-0')
        self.assertEqual((len(calls), first['status'], first['reason']), (1, 'APPROVE', None))   # never the rejected turn
        self.assertEqual(co._security_review_turn(tree, 'w-SECURITY-0-0'), first)   # stored review, no re-apply
        co.state['lifecycle'].pop('security_review')
        co.state['lifecycle']['security_turn'] = {'request_id': 'w-SECURITY-0-1', 'sequence': first['sequence']}
        with mock.patch.object(co, 'render'), \
                mock.patch.object(co, 'invoke', side_effect=AssertionError('second security review')):
            self.assertEqual(co._security_review_turn(tree, 'w-SECURITY-0-1')['status'], 'APPROVE')   # crash replay

    def test_a_malformed_security_review_is_discarded_and_reviewed_again(self):
        co, tree = self.at_security()
        [owned] = co.record_findings('security-reviewer', 'SECURITY', 1, [
            {'severity': 'CRITICAL', 'file': 'sum_ints.py', 'summary': 'earlier security finding'}])
        ledger = json.dumps(co.state['finding_ledger'])
        omitted = {'status': 'APPROVE', 'full_review': [], 'prior_findings': []}   # no disposition for its finding
        with mock.patch.object(co, 'materialize_review_context'), mock.patch.object(co, 'render'), \
                mock.patch.object(co, 'invoke', side_effect=self.security_turn_stub(co, tree, omitted)), \
                self.assertRaisesRegex(RuntimeError, 'omitted dispositions for its findings: .*; resume reviews again'):
            co._security_review_turn(tree, 'w-SECURITY-0-0')
        self.assertEqual((co.state['turns'][-1]['discarded'][:40], co.state['lifecycle'].get('security_turn')),
                         ('security reviewer omitted dispositions f', None))
        self.assertEqual(json.dumps(co.state['finding_ledger']), ledger)
        fixed = {'status': 'APPROVE', 'full_review': [],
                 'prior_findings': [{'id': owned['id'], 'disposition': 'fixed', 'evidence': 'removed the secret'}]}
        with mock.patch.object(co, 'materialize_review_context'), mock.patch.object(co, 'render'), \
                mock.patch.object(co, 'invoke', side_effect=self.security_turn_stub(co, tree, fixed)):
            review = co._security_review_turn(tree, 'w-SECURITY-0-0')   # resume dispatches a fresh review
        self.assertEqual((review['finding_ids'], co.state['lifecycle']['security_hold_tree']), ([], None))

    def test_the_security_budget_caps_reviewer_dispatches(self):
        co, tree = self.at_security()
        co.state['turns'].extend({'sequence': 900 + index, 'role': 'reviewer', 'phase': 'SECURITY', 'discarded': 'x'}
                                 for index in range(3))
        with mock.patch.object(co, 'materialize_review_context'), \
                mock.patch.object(co, 'invoke', side_effect=AssertionError('dispatched past the SECURITY budget')), \
                self.assertRaisesRegex(RuntimeError, r'SECURITY budget exhausted \(3 security reviews'):
            co._security_review_turn(tree, 'w-SECURITY-0-0')

    def test_a_worktree_done_binds_the_scanned_tree_in_one_save(self):
        co, tree = self.at_security()
        self.assertEqual(co.done(expected='another tree'), 'HOLD')
        self.assertEqual(co.state['hold_reason'], 'the tree changed before DONE; resume replays EXEC review and gate')
        co.state['status'] = 'ACTIVE'
        self.assertEqual(co.done(expected=tree, lifecycle_stage='DONE'), 'DONE')
        self.assertEqual((co.state['lifecycle']['stage'], co.state['next']), ('DONE', 'done'))   # one save with the status

    def git_state(self):
        refs = rc.subprocess.run(['git', 'for-each-ref', '--format=%(refname) %(objectname)'], cwd=self.workspace,
                                 check=True, capture_output=True, text=True).stdout
        head = rc.subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.workspace, check=True, capture_output=True,
                                 text=True).stdout
        return head, refs, hashlib.sha256((self.workspace / '.git' / 'index').read_bytes()).hexdigest()

    def test_accept_on_a_worktree_done_changes_no_ref_or_index_and_writes_the_delivery_report(self):
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on').stdout)
        before = self.git_state()
        accepted = self.run_operator_action('accept', '--lifecycle-mode', 'on', '--reason', 'looks right')
        self.assertEqual((accepted.returncode, accepted.stdout.strip().splitlines()[-1]), (0, 'ACCEPTED'),
                         accepted.stdout + accepted.stderr)
        self.assertEqual(self.git_state(), before)   # auto_commit false: no ref, no index change
        state = json.loads((self.run_dir / 'state.json').read_text())
        record = json.loads((self.run_dir / 'evidence' / 'acceptance.json').read_text())
        receipts = hashlib.sha256(json.dumps(state['lifecycle']['receipts'], sort_keys=True).encode()).hexdigest()
        self.assertEqual((state['status'], record['delivery']['auto_commit'], record['delivery']['commit'],
                          record['intent']['receipts_sha256']), ('ACCEPTED', False, None, receipts))
        report = (self.run_dir / 'delivery-report.md').read_text()
        for needle in ('# 交付报告（worktree lifecycle）', 'auto_commit 关闭，没有改动任何 ref 或 index',
                       '外部交付（push、PR、merge）：未执行', 'SECURITY：敏感路径 0 个；preflight clean'):
            self.assertIn(needle, report)
        again = self.run_operator_action('accept', '--lifecycle-mode', 'on', '--reason', 'looks right')
        self.assertEqual(again.stdout.strip().splitlines()[-1], 'ACCEPTED')

    def test_worktree_accept_refusals(self):
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on').stdout)
        for change, message in (
                (lambda co: setattr(co.args, 'override_rejection', True), 'refuses accept --override-rejection'),
                (lambda co: co.state['config'].update(external_delivery=True), 'refuses external delivery'),
                (lambda co: co.state['lifecycle'].update(stage='SECURITY'), 'requires status DONE and stage DONE'),
                (lambda co: co.state.update(status='HOLD'), 'requires status DONE and stage DONE')):
            with self.subTest(message=message):
                co = rc.Coordinator(self.args(action='accept'))
                change(co)
                with self.assertRaisesRegex(ValueError, message):
                    co.accept()
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['status'], 'DONE')

    def test_a_moved_head_holds_the_accept_and_security_runs_again_after_the_restore(self):
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on').stdout)
        git = ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test']
        rc.subprocess.run([*git, 'commit', '-q', '--allow-empty', '-m', 'someone else'], cwd=self.workspace, check=True)
        held = self.run_operator_action('accept', '--lifecycle-mode', 'on')
        self.assertIn('HEAD moved since the run started', held.stdout, held.stdout + held.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((state['status'], state['lifecycle']['stage'], state['next']), ('HOLD', 'SECURITY', 'security'))
        rc.subprocess.run(['git', 'reset', '-q', '--soft', 'HEAD~1'], cwd=self.workspace, check=True)   # operator restore
        self.assertIn(DONE, self.run_operator_action('resume', '--lifecycle-mode', 'on').stdout)
        accepted = self.run_operator_action('accept', '--lifecycle-mode', 'on')
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        routes = [row['route'] for row in json.loads((self.run_dir / 'state.json').read_text())['lifecycle']['receipts']
                  if row['stage'] == 'SECURITY']
        self.assertEqual(routes, ['DONE', 'DONE'])

    def test_reject_on_a_worktree_done_reopens_exec_and_runs_every_stage_again(self):
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60').stdout)
        first = json.loads((self.run_dir / 'state.json').read_text())
        rejected = self.run_operator_action('reject', '--lifecycle-mode', 'on', '--max-invocations', '60',
                                            '--text', 'Also reject floats.')
        self.assertIn(DONE, rejected.stdout, rejected.stdout + rejected.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((len(state['rejections']), state['lifecycle']['epoch'], state['acceptance_state']),
                         (1, first['lifecycle']['epoch'] + 1, 'PENDING'))
        later = [row['stage'] for row in state['lifecycle']['receipts'][len(first['lifecycle']['receipts']):]]
        self.assertEqual(later, ['FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])
        reopened = [(row['role'], row['phase']) for row in state['turns'] if row['sequence'] > first['sequence']]
        self.assertEqual(reopened[0], ('author', 'EXEC'))
        self.assertIn(('gate', 'EXEC'), reopened)
        self.assertIn('# rejection applied', (self.workspace / 'sum_ints.py').read_text())

    def test_a_successor_inherits_its_parent_delivery_baseline(self):
        parent = rc.Coordinator(self.args())
        base = parent.state['lifecycle']['security_baseline']
        child_dir = self.root / 'child'
        with mock.patch.object(rc.Coordinator, '_capture_security_baseline', side_effect=AssertionError('captured')):
            self.run_dir = child_dir
            child = rc.Coordinator.__new__(rc.Coordinator)   # only the inheritance step, not the successor checks
            child.evidence = child_dir / 'evidence'
            child.evidence.mkdir(parents=True)
            inherited = child._inherited_security_baseline(parent.run_dir)
        self.assertEqual((inherited['sha256'], inherited['inherited_from']), (base['sha256'], str(parent.run_dir)))
        self.assertEqual(Path(inherited['path']).read_bytes(), Path(base['path']).read_bytes())
        Path(base['path']).write_text('{}')   # the parent copy changed
        with self.assertRaisesRegex(ValueError, 'the parent delivery baseline changed'):
            child._inherited_security_baseline(parent.run_dir)

    def git(self, *args):
        return rc.subprocess.run(['git', *args], cwd=self.workspace, check=True, capture_output=True, text=True).stdout.strip()

    def test_auto_commit_makes_one_hook_free_commit_of_exactly_the_accepted_manifest(self):
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on', '--auto-commit', 'true').stdout)
        hooks = ('pre-commit', 'commit-msg', 'post-commit', 'reference-transaction', 'post-index-change', 'post-checkout')
        for name in hooks:
            hook = self.workspace / '.git' / 'hooks' / name
            hook.write_text(f'#!/bin/sh\ntouch "{self.root}/hook-{name}"\n')
            hook.chmod(0o755)
        parent = self.git('rev-parse', 'HEAD')
        accepted = self.run_operator_action('accept', '--lifecycle-mode', 'on', '--auto-commit', 'true')
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        commit = self.git('rev-parse', 'HEAD')
        self.assertEqual((self.git('rev-parse', 'HEAD~1'), state['acceptance']['delivery']['commit']), (parent, commit))
        manifest = sorted(path for path, value in state['approved_manifest'] if value != 'missing')
        self.assertEqual(self.git('ls-tree', '-r', '--name-only', 'HEAD').splitlines(), manifest)   # exactly the manifest
        self.assertEqual(self.git('show', 'HEAD:sum_ints.py') + '\n', (self.workspace / 'sum_ints.py').read_text())
        self.assertEqual(self.git('status', '--porcelain'), '')   # the index follows the new HEAD
        self.assertEqual([name for name in hooks if (self.root / f'hook-{name}').exists()], [])   # no hook ran
        self.assertIn(f'本地提交 `{commit}`', (self.run_dir / 'delivery-report.md').read_text())
        again = self.run_operator_action('accept', '--lifecycle-mode', 'on', '--auto-commit', 'true')
        self.assertEqual((again.stdout.strip().splitlines()[-1], self.git('rev-parse', 'HEAD')), ('ACCEPTED', commit))

    def test_an_auto_commit_replay_finishes_the_journaled_commit_and_a_moved_head_holds(self):
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on', '--auto-commit', 'true').stdout)
        co = rc.Coordinator(self.args('--auto-commit', 'true', action='accept'))
        intent, parent = co.operator_intent('accept', None, None), self.git('rev-parse', 'HEAD')
        commit = co._worktree_commit(intent, None)['commit']   # the ref moved, then the coordinator crashed
        journal = json.loads((co.evidence / 'delivery-commit.json').read_text())
        self.assertEqual((self.git('rev-parse', 'HEAD'), journal['commit'], journal['parent']), (commit, commit, parent))
        self.assertEqual(co._worktree_commit(intent, journal)['commit'], commit)   # idempotent: no second commit
        self.assertEqual(self.git('rev-parse', 'HEAD~1'), parent)
        other = self.git('commit-tree', parent + '^{tree}', '-p', parent, '-m', 'someone else')
        self.git('update-ref', 'HEAD', other)
        held = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        self.assertEqual(held.accept(), 'HOLD')
        self.assertIn('auto_commit: HEAD is ', held.state['hold_reason'])
        self.assertTrue(held.state['hold_reason'].endswith('accept --expect ' + intent['digest']))
        with self.assertRaisesRegex(ValueError, 'an auto_commit delivery is pending; restore HEAD and accept --expect'):
            rc.Coordinator(self.args('--auto-commit', 'true', action='resume')).resume()
        self.git('update-ref', 'HEAD', parent)   # the operator restores the parent; the replay moves HEAD by CAS
        replay = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        self.assertEqual(replay.accept(), 'ACCEPTED')
        self.assertEqual((self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD~1')), (commit, parent))
        self.assertEqual(replay.state['acceptance']['delivery']['commit'], commit)
        self.assertNotIn('delivery_pending', replay.state)

    def test_auto_commit_w04_refusals(self):
        co = rc.Coordinator(self.args())
        paths = ['tracked.txt', 'sum_ints.py']
        (self.workspace / 'staged.txt').write_text('staged before the run\n')
        self.git('add', 'staged.txt')
        with self.assertRaisesRegex(ValueError, 'refuses work staged before the run: staged.txt'):
            co._commit_refusals(paths)
        self.git('rm', '-q', '--cached', 'staged.txt')
        for value in ('input', 'yes'):
            self.git('config', 'core.autocrlf', value)
            with self.assertRaisesRegex(ValueError, 'refuses core.autocrlf'):
                co._commit_refusals(paths)
        self.git('config', '--unset', 'core.autocrlf')
        self.git('update-index', '--skip-worktree', 'tracked.txt')
        with self.assertRaisesRegex(ValueError, 'refuses skip-worktree'):
            co._commit_refusals(paths)
        self.git('update-index', '--no-skip-worktree', 'tracked.txt')
        self.git('update-index', '--add', '--cacheinfo', '160000,' + self.git('rev-parse', 'HEAD') + ',vendor/lib')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'a submodule')
        with self.assertRaisesRegex(ValueError, 'refuses a repository with submodules'):
            co._commit_refusals(paths)
        self.git('rm', '-q', '--cached', 'vendor/lib')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '-qm', 'no submodule')
        with self.assertRaisesRegex(ValueError, 'would rewrite'):
            co._manifest_tree([['"quoted"', 'x']])
        (self.workspace / '.gitattributes').write_text('*.py filter=lfs\n')
        with self.assertRaisesRegex(ValueError, 'content-transforming attributes: sum_ints.py: filter=lfs'):
            co._commit_refusals(paths)
        (self.workspace / '.gitattributes').unlink()
        co._commit_refusals(paths)   # nothing left to refuse

    def test_a_reject_at_the_limit_still_reopens_exec_and_budgets_grow_with_rejects(self):
        co = rc.Coordinator(self.args())
        co.state.update(status='DONE', rejections=[{'id': 'R001'}, {'id': 'R002'}])
        co.state['lifecycle'].update(stage='DONE')
        self.assertEqual((co._worktree_run_cap('SECURITY'), co._worktree_run_cap('DOCS')), (9, 21))
        co.args.expect = co.operator_intent('reject', 'again', None)['digest']
        self.assertEqual(co.reject('again', None), 'HOLD')
        self.assertEqual((co.state['hold_reason'], co.state['lifecycle']['stage'], co.state['lifecycle']['epoch']),
                         ('rejected-tree; post-DONE rejection limit reached; note and resume, reject --scope-change, or abort',
                          'EXEC', 1))

    def test_security_holds_before_any_review_when_head_moved(self):
        co, tree = self.at_security()
        self.git('commit', '-q', '--allow-empty', '-m', 'someone else')
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('security review dispatched')), \
                self.assertRaisesRegex(RuntimeError, 'HEAD moved since the run started'):
            co.worktree_security_turn()
        self.assertIsNone(co.state['lifecycle']['pending'])

    def journaled_commit(self):
        """A W DONE with auto_commit, whose commit was journaled and HEAD moved, then the coordinator crashed."""
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on', '--auto-commit', 'true').stdout)
        co = rc.Coordinator(self.args('--auto-commit', 'true', action='accept'))
        intent, parent = co.operator_intent('accept', None, None), self.git('rev-parse', 'HEAD')
        return intent, parent, co._worktree_commit(intent, None)['commit']

    def test_a_run_superseded_on_a_delivery_hold_cannot_be_replayed_into_accepted(self):
        intent, parent, commit = self.journaled_commit()
        self.git('update-ref', 'HEAD', parent)
        foreign = self.git('commit-tree', parent + '^{tree}', '-p', parent, '-m', 'someone else')
        self.git('update-ref', 'HEAD', foreign)
        held = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        self.assertEqual(held.accept(), 'HOLD')   # a delivery HOLD: delivery_pending is set
        note = rc.Coordinator(self.args('--auto-commit', 'true', action='note'))
        note.args.action = 'note'
        self.assertIn('Start: ', note.scope_change('Narrow the scope to integers only.', None))
        self.git('update-ref', 'HEAD', parent)   # even with the parent restored, the superseded run stays superseded
        for action in ('accept', 'resume'):   # the CLI refuses every non-scope action on a superseded run
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'scope change is pending'):
                rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action=action))
        note.args.expect, note.args.override_rejection = intent['digest'], False
        with self.assertRaisesRegex(ValueError, 'requires status DONE and stage DONE'):
            note._worktree_accept()   # and the replay itself refuses an ABORTED run
        self.assertEqual((self.git('rev-parse', 'HEAD'), json.loads((self.run_dir / 'state.json').read_text())['status']),
                         (parent, 'ABORTED'))

    def test_an_abort_after_a_crash_in_the_commit_window_is_recoverable_from_the_journal(self):
        intent, parent, commit = self.journaled_commit()   # HEAD is the commit; ACCEPTED was never saved
        aborted = self.run_operator_action('abort', '--lifecycle-mode', 'on', '--auto-commit', 'true')
        self.assertIn('HOLD: aborted by operator', aborted.stdout, aborted.stdout + aborted.stderr)
        stale = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        stale.state['lifecycle']['receipts'].append({'stage': 'DOCS', 'request_id': 'w-DOCS-9-0', 'epoch': 9})
        with self.assertRaisesRegex(ValueError, 'requires status DONE and stage DONE'):
            stale.accept()   # a journal from other receipts (an earlier epoch) is never replayed
        replay = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        self.assertEqual(replay.accept(), 'ACCEPTED')
        self.assertEqual((self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'HEAD~1')), (commit, parent))
        record = json.loads((self.run_dir / 'evidence' / 'acceptance.json').read_text())
        self.assertEqual(record['delivery']['commit'], commit)

    # --- rel210-fixA: the delivery binds the index, the branch and the file modes ---------------------------------------------
    def test_a_replay_holds_on_staged_changes_that_are_not_this_delivery_and_leaves_them(self):
        intent, parent, commit = self.journaled_commit()
        self.git('reset', '-q', parent)   # a crash after the journal write, before the ref moved
        partial = rc.subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=self.workspace, input='a partial edit\n',
                                    check=True, capture_output=True, text=True).stdout.strip()
        self.git('update-index', '--cacheinfo', f'100644,{partial},tracked.txt')   # the user stages a different change
        staged = self.git('ls-files', '-s', 'tracked.txt')
        held = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        self.assertEqual(held.accept(), 'HOLD')
        self.assertIn('auto_commit: the index holds staged changes that are neither', held.state['hold_reason'])
        self.assertEqual((self.git('ls-files', '-s', 'tracked.txt'), self.git('rev-parse', 'HEAD')), (staged, parent))
        self.git('reset', '-q')   # the operator unstages; the replay finishes the journaled commit
        replay = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        self.assertEqual((replay.accept(), self.git('rev-parse', 'HEAD')), ('ACCEPTED', commit))
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_a_replay_after_a_branch_switch_holds_and_moves_no_branch(self):
        intent, parent, commit = self.journaled_commit()
        branch = self.git('symbolic-ref', 'HEAD')
        self.assertEqual(intent['head_ref'], branch)
        self.git('update-ref', branch, parent)   # a crash before the ref moved
        self.git('branch', 'other', parent)
        self.git('symbolic-ref', 'HEAD', 'refs/heads/other')   # the user switches to another branch on the same parent
        held = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        self.assertEqual(held.accept(), 'HOLD')
        self.assertIn(f'auto_commit: HEAD is refs/heads/other, not {branch} as at the journaled accept', held.state['hold_reason'])
        self.assertEqual((self.git('rev-parse', 'refs/heads/other'), self.git('rev-parse', branch)), (parent, parent))
        self.git('symbolic-ref', 'HEAD', branch)
        replay = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        self.assertEqual(replay.accept(), 'ACCEPTED')
        self.assertEqual((self.git('rev-parse', branch), self.git('rev-parse', 'refs/heads/other')), (commit, parent))

    def test_a_detached_replay_moves_only_head_and_a_replay_after_the_ref_moved_finishes_the_index(self):
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on', '--auto-commit', 'true').stdout)
        branch, parent = self.git('symbolic-ref', 'HEAD'), self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', '--detach')   # the operator accepts on a detached HEAD
        co = rc.Coordinator(self.args('--auto-commit', 'true', action='accept'))
        self.git('config', 'core.fileMode', 'false')   # no reliable mode bits: no commit
        with self.assertRaisesRegex(ValueError, 'auto_commit refuses core.fileMode false'):
            co._commit_refusals(['tracked.txt'])
        self.git('config', 'core.fileMode', 'true')
        intent = co.operator_intent('accept', None, None)
        self.assertIsNone(intent['head_ref'])
        commit = co._worktree_commit(intent, None)['commit']
        self.assertEqual((self.git('rev-parse', 'HEAD'), self.git('rev-parse', branch)), (commit, parent))   # the branch stays
        self.git('read-tree', parent)   # a crash after the ref moved, before the index sync: the index is the state before
        replay = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        self.assertEqual(replay.accept(), 'ACCEPTED')
        self.assertEqual((self.git('rev-parse', 'HEAD'), self.git('status', '--porcelain')), (commit, ''))
        journal = co.evidence / 'delivery-commit.json'
        old = json.loads(journal.read_text())
        rc.atomic_json(journal, {key: value for key, value in old.items() if key not in ('index_before', 'index_target')})
        with self.assertRaisesRegex(rc.WorktreeDeliveryHold, 'the journal predates the index and branch binding'):
            co._worktree_commit(intent, json.loads(journal.read_text()))
        self.git('symbolic-ref', 'HEAD', branch)   # a detached journal never moves a branch
        with self.assertRaisesRegex(rc.WorktreeDeliveryHold, f'auto_commit: HEAD is {branch}, not detached'):
            co._worktree_commit(intent, old)
        self.assertEqual(self.git('rev-parse', branch), parent)

    def test_a_mode_only_change_after_security_invalidates_the_accept_and_the_commit_takes_the_accepted_mode(self):
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on', '--auto-commit', 'true').stdout)
        co = rc.Coordinator(self.args('--auto-commit', 'true', action='accept'))
        intent = co.operator_intent('accept', None, None)
        (self.workspace / 'sum_ints.py').chmod(0o755)   # content unchanged
        self.assertEqual(dict(rc.git_snapshot(self.workspace)[1])['sum_ints.py'][:5], 'exec:')
        self.assertNotEqual(rc.git_snapshot(self.workspace)[0], intent['tree_sha256'])
        with self.assertRaisesRegex(ValueError, 'stale: 0 tracked, 1 untracked drift'):   # sum_ints.py is the author's new file
            co.operator_intent('accept', None, None)
        stale = rc.Coordinator(self.args('--auto-commit', 'true', '--expect', intent['digest'], action='accept'))
        with self.assertRaisesRegex(ValueError, 'stale'):
            stale.accept()
        self.assertEqual(self.git('rev-parse', 'HEAD'), intent['head'])   # nothing committed
        tree = co._manifest_tree(intent['tree_snapshot'])   # the accepted manifest's mode, not the live one
        self.assertTrue(self.git('ls-tree', tree, 'sum_ints.py').startswith('100644 '))

    def test_a_read_only_turn_that_only_changes_a_mode_is_voided_and_restored(self):
        marker = self.root / 'chmod-once'
        result = self.run_coordinator('--lifecycle-mode', 'on', env={
            'FAKE_MUTATION': 'chmod', 'FAKE_MUTATION_ROLE': 'Role: security reviewer,', 'FAKE_MUTATION_ONCE': str(marker)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('re-dispatching the reviewer turn once', result.stdout)
        self.assertTrue(marker.exists())
        self.assertFalse((self.workspace / 'tracked.txt').stat().st_mode & 0o111)   # the mode is back
        state = json.loads((self.run_dir / 'state.json').read_text())
        [void] = [row for row in state['turns'] if row['phase'] == 'SECURITY' and 'voided' in row]
        self.assertTrue(void['voided']['restored'])

    def test_a_successor_inherits_the_baseline_and_scopes_delivery_to_the_original_tree(self):
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60').stdout)
        parent_dir = self.run_dir
        parent = rc.Coordinator(self.args(action='reject'))
        parent.args.action = 'reject'
        command = parent.scope_change('Also reject floats.', None)
        start = command.split('Start: ', 1)[1].split()
        option = lambda name: start[start.index(name) + 1]
        self.run_dir, self.workitem = Path(option('--run-dir')), Path(option('--workitem'))
        result = self.run_coordinator('--lifecycle-mode', 'on', '--max-invocations', '60', '--config', option('--config'),
                                      '--supersedes', str(parent_dir))
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        inherited = state['lifecycle']['security_baseline']
        original = json.loads((parent_dir / 'state.json').read_text())['lifecycle']['security_baseline']
        self.assertEqual((inherited['inherited_from'], inherited['sha256']), (str(parent_dir), original['sha256']))
        security = next(row for row in reversed(state['lifecycle']['receipts']) if row['stage'] == 'SECURITY')
        manifest = json.loads(Path(security['preflight']['report'].replace('-preflight.json', '-manifest.json')).read_text())
        [row] = [row for row in manifest['task_delta'] if row['path'] == 'sum_ints.py']
        # the parent's work is task delta against the tree before the work item, not a dirty baseline
        self.assertEqual((row['ownership'], row['baseline_dirty']), ('declared-post-baseline', False))
        self.assertEqual(manifest['baseline_changes']['untracked'], [])

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
        self.run_dir = self.root / 'symlinked'
        co = rc.Coordinator(self.args())
        (self.workspace / 'CHANGELOG.md').unlink()
        (self.workspace / 'CHANGELOG.md').symlink_to(self.root / 'outside.md')   # the EXEC author's replacement
        co.state['lifecycle'].update(stage='DOCS', candidate_oid=rc.git_snapshot(self.workspace)[0],
                                     docs_owned={'CHANGELOG.md': 'an earlier digest'})
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('docs writer dispatched')), \
                self.assertRaisesRegex(RuntimeError, 'now a directory or reached through a symlink: CHANGELOG.md'):
            co.worktree_docs_turn()

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

    def test_accept_is_refused_before_the_worktree_done_stage_even_on_override_paths(self):
        co = rc.Coordinator(self.args())
        for status, kind in (('DONE', None), ('HOLD', 'rejection_limit')):   # stage EXEC: FINISH..SECURITY not run
            with self.subTest(status=status):
                co.state.update(status=status, terminal_hold_kind=kind)
                with self.assertRaisesRegex(ValueError, 'worktree lifecycle accept requires status DONE and stage DONE'):
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
