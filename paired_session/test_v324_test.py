"""V324-TEST: a persistent EXEC approval over its own completed failing test; WI-GATE1 R5: it HOLDs for attach-verification
without using an EXEC round (before: it returned to the author)."""
import hashlib
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from paired_session import operator_verification as opv
from paired_session import test_real_coordinator as trc

rc = trc.rc
HELPERS = ('tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
           'fake_codex_cli', 'fake_claude_cli', 'command')
PREFIX = 'reviewer EXEC approval lacks an observed successful configured test command'


class FailedTestApprovalTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def setUp(self):
        trc.RealCoordinatorTests.setUp(self)
        passing = patch.dict(os.environ, {'FAKE_CODEX_LONG': 'yield'})
        passing.start()
        self.addCleanup(passing.stop)

    def coordinator(self, *extra):
        self.run_dir = self.root / ('run-' + str(len(list(self.root.glob('run-*')))))
        co = rc.Coordinator(rc.parser().parse_args(self.command(
            '--reviewer-vendor', 'codex', '--gate-vendor', 'codex',
            '--shadow', 'off', '--adversarial-gate', 'off', '--skip-probe', *extra)[2:]))
        co.state.update(phase='EXEC', next='author')
        co.author_turn()
        return co

    def failing_review(self, co):
        with patch.dict(os.environ, {'FAKE_REVIEW_TEST_FAILURE': '1'}):
            co.reviewer_turn()

    def test_completed_failed_approval_holds_for_attach_verification_without_a_round(self):
        # WI-GATE1 R5: an APPROVE turned into REVISE only by the configured test failing in the reviewer's sandbox uses no
        # EXEC round and HOLDs at once for attach-verification (before: a REVISE delivered to the next author)
        co = self.coordinator()
        self.failing_review(co)
        self.assertEqual((co.state['status'], co.state['phase'], co.state['next'], co.state['hold_reason']),
                         ('HOLD', 'EXEC', 'reviewer', rc.TEST_EVIDENCE_HOLD))
        self.assertEqual(co.state['exec_rounds'], 1)
        [row] = co.blocking_open_findings()
        self.assertEqual((row['source'], row['severity'], row['status'], row['category']),
                         ('test-failure', 'MAJOR', 'open', 'correctness'))
        self.assertIn('exit status 1', row['summary'])
        self.assertIn('FAILED fake test', row['failure_scenario'])
        self.assertEqual(co.state['turns'][-1]['answer']['status'], 'APPROVE')

    def test_persistent_failure_holds_before_shadow_dispatch(self):   # WI-GATE1 R5 (before: returned to the author)
        co = self.coordinator('--shadow', 'on')
        self.failing_review(co)
        self.assertEqual((co.state['status'], co.state['next']), ('HOLD', 'reviewer'))
        self.assertEqual([turn['role'] for turn in co.state['turns']], ['author', 'reviewer'])
        self.assertEqual(co.blocking_open_findings()[0]['source'], 'test-failure')

    def test_next_round_passing_approval_closes_program_row_and_proceeds(self):
        for gate in ('off', 'on'):
            with self.subTest(gate=gate):
                co = self.coordinator('--adversarial-gate', gate)
                self.failing_review(co)
                [row] = co.blocking_open_findings()
                co.state.update(status='ACTIVE', hold_reason='')   # WI-GATE1 R5: the operator fixed it and resumed
                co.author_turn()
                self.assertEqual(co.state['exec_rounds'], 2)
                invoke = co.invoke

                def still_open(*args, **kwargs):
                    result = invoke(*args, **kwargs)
                    # The clean observed run, rather than a reviewer disposition, must close the program row.
                    for prior in result['answer'].get('prior_findings', []):
                        prior.update(disposition='still_open', evidence='not disposed by the reviewer')
                    return result

                with patch.object(co, 'invoke', side_effect=still_open):
                    co.reviewer_turn()
                self.assertEqual(row['status'], 'fixed')
                self.assertEqual(row['status_history'][-1]['evidence'],
                                 'configured test command: observed successful run')
                self.assertFalse(co.blocking_open_findings())
                if gate == 'on':
                    self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'gate'))
                else:
                    self.assertEqual(co.state['status'], 'DONE')

    def test_reverted_tree_waits_for_passing_review_to_close_test_failure(self):
        co = self.coordinator()
        path = self.workspace / 'tracked.txt'
        original = path.read_bytes()
        original_tree = rc.git_snapshot(self.workspace)[0]
        path.write_text('change that causes the test failure\n')
        self.failing_review(co)
        [row] = co.blocking_open_findings()
        self.assertEqual(row['source'], 'test-failure')

        path.write_bytes(original)
        self.assertEqual(rc.git_snapshot(self.workspace)[0], original_tree)
        self.assertEqual(row['status'], 'open')
        self.assertEqual(len(row['status_history']), 1)
        co.state.update(status='ACTIVE', hold_reason='')
        collect = co._collect

        def still_open(*args):
            parsed = collect(*args)
            [prior] = parsed[0]['prior_findings']
            self.assertEqual(prior['id'], row['id'])
            prior.update(disposition='still_open', evidence='left open by the reviewer')
            return parsed

        with patch.object(co, '_collect', side_effect=still_open):
            co.reviewer_turn()
        self.assertEqual(co.state['status'], 'DONE')
        self.assertTrue(co.configured_test_succeeded(self.receipt(co, 'reviewer')))
        self.assertEqual([entry['status'] for entry in row['status_history']], ['open', 'still_open', 'fixed'])
        self.assertEqual(row['status_history'][-1]['evidence'], 'configured test command: observed successful run')

    def test_missing_and_unfinished_runs_keep_old_hold(self):
        for env, suffix in (({'FAKE_MISSING_OBSERVED': '1'}, ''),
                            ({'FAKE_CODEX_LONG': 'started-only'},
                             '; the configured test was not observed to completion (no exit status when the turn ended)'),
                            ({'FAKE_CODEX_LONG': 'killed'},
                             '; the configured test was not observed to completion (no exit status when the turn ended)')):
            with self.subTest(env=env):
                co = self.coordinator()
                with patch.dict(os.environ, env):
                    self.assertEqual(co._drive_loop(), 'HOLD')
                self.assertEqual(co.state['hold_reason'], PREFIX + suffix + rc.TEST_EVIDENCE_GUIDANCE)   # S3 R2 F8
                self.assertFalse(co.open_findings())
                self.assertEqual(co.state['next'], 'reviewer')

    def test_shadow_and_gate_failed_approvals_keep_old_hold(self):
        for role in ('shadow', 'gate'):
            with self.subTest(role=role):
                co = self.coordinator('--shadow', 'on' if role == 'shadow' else 'off',
                                      '--adversarial-gate', 'on' if role == 'gate' else 'off')
                observe = rc.observed_events

                def failing(vendor, events):
                    rows, calls = observe(vendor, events)
                    rows = [row for row in rows
                            if not rc.command_invokes_test(row['command'], co.args.test_command)]
                    rows.append({'command': co.args.test_command, 'exit_code': 1,
                                 'error': True, 'output': 'FAILED fake test', 'source': 'command_execution'})
                    return rows, calls

                # Finish the persistent approval before injecting the fresh role's failing evidence.
                if role == 'gate':
                    co.reviewer_turn()
                with patch.object(rc, 'observed_events', side_effect=failing):
                    if role == 'shadow':
                        # Let the persistent reviewer pass; only the shadow's invocation gets the failure.
                        invoke = co.invoke

                        def fresh_failure(name, *args, **kwargs):
                            with patch.object(rc, 'observed_events', side_effect=failing if name == 'shadow' else observe):
                                return invoke(name, *args, **kwargs)

                        with patch.object(co, 'invoke', side_effect=fresh_failure):
                            self.assertEqual(co._drive_loop(), 'HOLD')
                    else:
                        self.assertEqual(co._drive_loop(), 'HOLD')
                self.assertEqual(co.state['hold_reason'],
                                 f'{role} EXEC approval lacks an observed successful configured test command'
                                 + rc.TEST_EVIDENCE_GUIDANCE)   # S3 R2 F8
                self.assertFalse(co.open_findings())

    def attach(self, co, tree=None, **over):
        log = self.root / 'host-tests.log'
        log.write_text('all tests passed on the host\n')
        args = types.SimpleNamespace(**{'command': co.args.test_command, 'cwd': None, 'exit_code': 0, 'log': str(log),
                                        'log_sha256': hashlib.sha256(log.read_bytes()).hexdigest(),
                                        'note': 'ran the configured tests on the host', **over})
        return opv.attach(co, args, tree or rc.git_snapshot(self.workspace)[0], rc.atomic_json)

    def receipt(self, co, role):
        return next(turn for turn in reversed(co.state['turns']) if turn['role'] == role)

    def test_current_passing_record_settles_failed_reviewer_approval(self):
        co = self.coordinator()
        self.attach(co)
        self.failing_review(co)
        self.assertFalse([row for row in co.open_findings() if row['source'] == 'test-failure'])
        self.assertEqual(co.state['status'], 'DONE')
        self.assertEqual(self.receipt(co, 'reviewer')['operator_test_evidence'], 'V001')

    def test_record_closes_open_test_failure_row(self):
        co = self.coordinator()
        self.failing_review(co)
        [row] = co.blocking_open_findings()
        co.author_turn()
        self.attach(co)
        invoke = co.invoke

        def still_open(*args, **kwargs):
            result = invoke(*args, **kwargs)
            # The operator record, rather than a reviewer disposition, must close the program row.
            for prior in result['answer'].get('prior_findings', []):
                prior.update(disposition='still_open', evidence='not disposed by the reviewer')
            return result

        with patch.object(co, 'invoke', side_effect=still_open):
            self.failing_review(co)
        self.assertEqual(row['status'], 'fixed')
        self.assertEqual(row['status_history'][-1]['evidence'],
                         'operator verification V001: configured test command exit 0 on this tree')
        self.assertEqual([item['id'] for item in co.state['finding_ledger'] if item['source'] == 'test-failure'], [row['id']])
        self.assertFalse(co.blocking_open_findings())

    def test_unqualified_records_still_give_test_failure(self):
        cases = (('another tree', {'tree': '0' * 64}), ('voided', {}), ('non-zero exit', {'exit_code': 1}),
                 ('other command', {'command': 'python3 -m unittest -v'}))
        for label, over in cases:
            with self.subTest(label):
                co = self.coordinator()
                self.attach(co, **over)
                if label == 'voided':
                    opv.void_stale(co, None, rc.atomic_json)
                self.failing_review(co)
                [row] = co.blocking_open_findings()
                self.assertEqual((row['source'], row['severity']), ('test-failure', 'MAJOR'))
                self.assertEqual((co.state['status'], co.state['next']), ('HOLD', 'reviewer'))   # WI-GATE1 R5
                self.assertNotIn('operator_test_evidence', self.receipt(co, 'reviewer'))

    def test_shadow_and_gate_failed_approvals_pass_with_current_record(self):
        for role in ('shadow', 'gate'):
            with self.subTest(role=role):
                co = self.coordinator('--shadow', 'on' if role == 'shadow' else 'off',
                                      '--adversarial-gate', 'on' if role == 'gate' else 'off')
                observe = rc.observed_events

                def failing(vendor, events):
                    rows, calls = observe(vendor, events)
                    rows = [row for row in rows
                            if not rc.command_invokes_test(row['command'], co.args.test_command)]
                    rows.append({'command': co.args.test_command, 'exit_code': 1,
                                 'error': True, 'output': 'FAILED fake test', 'source': 'command_execution'})
                    return rows, calls

                if role == 'gate':
                    co.reviewer_turn()
                self.attach(co)
                invoke = co.invoke

                def fresh_failure(name, *args, **kwargs):
                    with patch.object(rc, 'observed_events', side_effect=failing if name == role else observe):
                        return invoke(name, *args, **kwargs)

                with patch.object(co, 'invoke', side_effect=fresh_failure):
                    co._drive_loop()
                self.assertNotIn('lacks an observed successful configured test command', co.state.get('hold_reason') or '')
                self.assertFalse(co.open_findings())
                self.assertEqual(self.receipt(co, role)['operator_test_evidence'], 'V001')

    def test_advisory_revise_at_the_limit_exits_with_current_record(self):
        for attached in (True, False):
            with self.subTest(attached=attached):
                co = self.coordinator('--max-exec-rounds', '1')
                if attached:
                    self.attach(co)
                invoke = co.invoke

                def advisory(*args, **kwargs):
                    result = invoke(*args, **kwargs)
                    result['answer'].update(status='REVISE', full_review=[{
                        'severity': 'LOW', 'file': 'app.py', 'summary': 'simplify the helper',
                        'failure_scenario': 'the helper is longer than it needs to be'}])
                    return result

                with patch.object(co, 'invoke', side_effect=advisory):
                    self.failing_review(co)
                last = co.state['review_verdicts'][-1]
                if attached:
                    self.assertEqual((last['reviewer_raw_verdict'], last['effective_verdict']),
                                     ('REVISE', 'APPROVE_WITH_ADVISORY'))
                    self.assertNotIn('round_limit_hold', co.state)
                else:
                    # WI-GATE1 S3 R3 F8: test evidence is the only obstacle, so the HOLD asks for verification (no round used)
                    self.assertTrue(co.state['hold_reason'].startswith(rc.TEST_EVIDENCE_HOLD))

    def test_docs_review_with_failed_own_run_passes_with_current_record(self):
        co = self.coordinator()
        tree = rc.git_snapshot(self.workspace)[0]
        answer = {'status': 'APPROVE', 'full_review': [], 'observed_commands': [
            {'command': co.args.test_command, 'exit_code': 1, 'output': 'FAILED fake test', 'source': 'command_execution'}]}

        def invoke(role, phase, prompt, schema, fresh=False):
            co.state['turns'].append({'sequence': 99, 'role': role, 'phase': phase, 'snapshot_before': tree})
            return {'sequence': 99, 'answer': answer}

        def docs_review():
            with patch.object(co, 'invoke', side_effect=invoke), patch.object(co, 'materialize_review_context'), \
                    patch.object(co, '_docs_budget'), patch.object(co, 'render'):
                return co._docs_review_turn(tree, ['docs/guide.md'])

        with self.assertRaises(RuntimeError) as caught:
            docs_review()
        self.assertIn('DOCS reviewer did not observe a successful run of the configured test command', str(caught.exception))
        self.attach(co)
        review = docs_review()
        self.assertEqual((review['status'], review['observed_test'], review['operator_test_evidence']),
                         ('APPROVE', None, 'V001'))

    def test_report_mode_ignores_a_passing_record(self):
        co = self.coordinator()
        self.attach(co)
        co.state['config']['review_report'] = True
        self.assertIsNone(co._operator_test_record(rc.git_snapshot(self.workspace)[0]))

    def test_last_round_holds_for_attach_verification_not_the_round_limit(self):
        # WI-GATE1 R5: the same test-evidence HOLD at the last round; it is no round-limit HOLD (no round was used)
        co = self.coordinator('--max-exec-rounds', '1')
        self.failing_review(co)
        self.assertEqual((co.state['status'], co.state['hold_reason']), ('HOLD', rc.TEST_EVIDENCE_HOLD))
        self.assertNotIn('round_limit_hold', co.state)
        self.assertEqual(co.blocking_open_findings()[0]['source'], 'test-failure')
        self.assertFalse(co.at_round_limit_hold())

    def prompt(self, co, turn):
        return (co.evidence / f"{turn['sequence']:03d}-{turn['phase'].lower()}-{turn['role']}.prompt.txt").read_text()

    def assert_reused(self, co, turn, field, source):
        self.assertEqual(turn[field], source)
        filename = f"{turn['sequence']:03d}-{turn['phase'].lower()}-{turn['role']}.receipt.json"
        self.assertEqual(json.loads((co.evidence / filename).read_text())[field], source)
        self.assertFalse(any(rc.command_invokes_test(row['command'], co.args.test_command)
                             for row in turn['observed_commands']))
        self.assertTrue(turn['observed_commands'])
        prompt = self.prompt(co, turn)
        self.assertIn(f'The configured test passed on this tree: {co.args.test_command}', prompt)
        self.assertNotIn('Run this test command exactly as written', prompt)
        self.assertIn(rc.MISSING_HOST_TEST_RULE, prompt)

    def test_host_pass_waives_exec_rerun(self):
        co = self.coordinator('--shadow', 'on', '--adversarial-gate', 'on')
        self.attach(co)
        with patch.dict(os.environ, {'FAKE_EXEC_MINOR_REVISE': '1'}):
            self.assertEqual(co._drive_loop(), 'DONE')
        for role in ('reviewer', 'shadow', 'gate'):
            turn = self.receipt(co, role)
            self.assert_reused(co, turn, 'operator_test_evidence', 'V001')
            self.assertEqual(turn['answer']['self_run_evidence'], [{'command': 'git status --short'}])
        self.assertIn('List the commands you ran yourself; the reused test result need not be listed.',
                      self.prompt(co, self.receipt(co, 'reviewer')))
        self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')

    def test_reuse_observed_pass_on_same_tree(self):
        co = self.coordinator('--shadow', 'on', '--adversarial-gate', 'on')
        self.assertEqual(co._drive_loop(), 'DONE')
        reviewer = self.receipt(co, 'reviewer')
        for role in ('shadow', 'gate'):
            self.assert_reused(co, self.receipt(co, role), 'reused_test_evidence', reviewer['sequence'])
        co.state.update(status='ACTIVE', next='reviewer')
        with patch.dict(os.environ, {'FAKE_EXEC_NO_EVIDENCE': '1'}):
            self.assertEqual(co._drive_loop(), 'HOLD')
        self.assertEqual(co.state['hold_reason'], 'EXEC APPROVE rejected: empty self_run_evidence')
        self.assert_reused(co, self.receipt(co, 'reviewer'), 'reused_test_evidence', reviewer['sequence'])
        rejected = [turn for turn in co.state['turns'] if turn['role'] == 'reviewer'
                    and turn['sequence'] > reviewer['sequence']]
        self.assertEqual([turn['discarded'] for turn in rejected], [co.state['hold_reason']] * 2)

    def test_shadow_confirmation_reuses_the_repair_reviews_passing_test_on_the_same_tree(self):
        co = self.coordinator('--shadow', 'on', '--adversarial-gate', 'on')
        marker = str(self.root / 'gate-failed')
        with patch.dict(os.environ, {'FAKE_GATE_BLOCK_ONCE': marker, 'FAKE_SHADOW_BLOCKER_IF_FILE': marker,
                                     'FAKE_CONFIRM_SHADOW': 'withdrawn'}):
            self.assertEqual(co._drive_loop(), 'DONE', co.state.get('hold_reason'))

        [finding] = [row for row in co.state['finding_ledger'] if row['summary'] == 'shadow regression']
        turns = co.state['turns']
        shadow_index = next(index for index, turn in enumerate(turns) if turn['sequence'] == finding['origin_round'])
        repair, shadow, confirmation = turns[shadow_index - 1:shadow_index + 2]
        self.assertEqual([(turn['role'], turn['phase']) for turn in (repair, shadow, confirmation)],
                         [('reviewer', 'EXEC'), ('shadow', 'EXEC'), ('reviewer', 'EXEC')])
        self.assertEqual(confirmation['snapshot_before'], repair['snapshot_after'])
        self.assertTrue(any(rc.observed_test_succeeded(row, co.args.test_command) for row in repair['observed_commands']))
        self.assertIn('shadow finding to confirm', self.prompt(co, confirmation))
        self.assertEqual(finding['status_history'][-1], {'round': confirmation['sequence'], 'status': 'withdrawn',
                                                        'evidence': 'fake confirmation'})
        self.assert_reused(co, confirmation, 'reused_test_evidence', repair['sequence'])

    def test_invalid_or_missing_test_proof_keeps_fallback(self):
        for kind in ('missing', 'wrong-command', 'failed-host', 'incomplete'):
            with self.subTest(kind=kind):
                co = self.coordinator('--max-exec-rounds', '1')
                if kind == 'wrong-command':
                    self.attach(co, command='python3 -m unittest different')
                if kind == 'failed-host':
                    self.attach(co, exit_code=1)
                tree = rc.git_snapshot(self.workspace)[0]
                for prompt in (co._review_prompt('reviewer', tree), co._review_prompt('shadow', tree), co._gate_prompt(tree)):
                    self.assertIn(rc.MISSING_HOST_TEST_RULE, prompt)
                    self.assertIn(f'Run this test command exactly as written in one Bash call: {co.args.test_command}', prompt)
                    self.assertNotIn('The configured test passed on this tree', prompt)
                env = {'FAKE_CODEX_LONG': 'started-only'} if kind == 'incomplete' else {'FAKE_MISSING_OBSERVED': '1'}
                with patch.dict(os.environ, env):
                    self.assertEqual(co._drive_loop(), 'HOLD')
                self.assertIn(PREFIX, co.state['hold_reason'])
                self.assertFalse(co.open_findings())
                self.assertEqual(co.state['exec_rounds'], 1)
                self.assertFalse(co.at_round_limit_hold())
                self.assertFalse(co._test_proof(tree))

    def test_later_completed_failure_blocks_same_tree_reuse(self):
        for settlement in ('newer-pass', 'host'):
            with self.subTest(settlement=settlement):
                co = self.coordinator()
                self.assertEqual(co._drive_loop(), 'DONE')
                passed = self.receipt(co, 'reviewer')
                tree = rc.git_snapshot(self.workspace)[0]
                co.state.update(status='ACTIVE', next='reviewer')
                self.failing_review(co)
                failed = self.receipt(co, 'reviewer')
                self.assertGreater(failed['sequence'], passed['sequence'])
                self.assertEqual(failed['snapshot_before'], tree)
                self.assertEqual(failed['observed_commands'][0]['exit_code'], 1)
                [finding] = co.blocking_open_findings()
                for prompt in (co._review_prompt('reviewer', tree), co._review_prompt('shadow', tree), co._gate_prompt(tree)):
                    self.assertNotIn('The configured test passed on this tree', prompt)
                self.assertFalse(co._test_proof(tree))

                co.state.update(status='ACTIVE')
                with patch.dict(os.environ, {'FAKE_EXEC_MINOR_REVISE': '1', 'FAKE_CODEX_LONG': 'started-only'}):
                    co.reviewer_turn()   # the fake's model disposition alone cannot settle the program finding
                self.assertEqual(finding['status'], 'open')
                self.assertFalse(co._test_proof(tree))
                co.state.update(status='ACTIVE', next='reviewer')
                if settlement == 'host':
                    self.attach(co)
                self.assertEqual(co._drive_loop(), 'DONE')
                self.assertEqual(finding['status'], 'fixed')
                self.assertGreater(self.receipt(co, 'reviewer')['sequence'], failed['sequence'])

    def test_changed_tree_requires_fresh_proof(self):
        co = self.coordinator('--adversarial-gate', 'on')
        co.reviewer_turn()
        self.assertEqual(co.state['next'], 'gate')
        original = self.receipt(co, 'reviewer')
        self.assertEqual(co._test_proof(original['snapshot_after']), {'reused_test_evidence': original['sequence']})
        co.args.test_command += ' other'
        self.assertFalse(co._test_proof(original['snapshot_after']))
        co.args.test_command = 'python3 -m unittest'
        (self.workspace / 'new_feature.py').write_text('enabled = True\n')
        tree = rc.git_snapshot(self.workspace)[0]
        self.assertFalse(co._test_proof(tree))
        co.state.update(status='ACTIVE', next='reviewer')
        self.assertEqual(co._drive_loop(), 'DONE')
        turn = self.receipt(co, 'reviewer')
        self.assertEqual(turn['snapshot_before'], tree)
        self.assertEqual(turn['observed_commands'][0]['command'], co.args.test_command)
        self.assertNotIn('reused_test_evidence', turn)

    def test_unchanged_author_pass_is_ineligible(self):
        co = self.coordinator()
        tree = rc.git_snapshot(self.workspace)[0]
        with patch.dict(os.environ, {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1',
                                     'FAKE_AUTHOR_OBSERVED_TEST': co.args.test_command}):
            co.author_turn()
        author = self.receipt(co, 'author')
        self.assertEqual((author['snapshot_before'], author['snapshot_after']), (tree, tree))
        self.assertTrue(any(rc.observed_test_succeeded(row, co.args.test_command) for row in author['observed_commands']))
        self.assertFalse(co._test_proof(tree))
        with patch.dict(os.environ, {'FAKE_MISSING_OBSERVED': '1'}):
            self.assertEqual(co._drive_loop(), 'HOLD')
        self.assertIn(PREFIX, co.state['hold_reason'])
        self.assertNotIn('reused_test_evidence', self.receipt(co, 'reviewer'))

    def test_resume_uses_existing_test_proof_checks(self):
        for damaged in (True, False):
            with self.subTest(damaged=damaged):
                co = self.coordinator('--adversarial-gate', 'on')
                self.attach(co)
                tree = rc.git_snapshot(self.workspace)[0]
                review = co.invoke('reviewer', 'EXEC', co._review_prompt('reviewer', tree), rc.review_schema())
                co.state['status'] = 'HOLD'
                co.save()
                if damaged:
                    Path(co.state['operator_verifications'][0]['log_evidence']).unlink()
                    self.assertEqual(co.resume(), 'HOLD')
                    self.assertEqual(co.state['hold_reason'], rc.TEST_EVIDENCE_HOLD)
                    self.assertEqual(co.state['sequence'], review['sequence'])
                    self.assertIsNone(co.state['pending_reviewer_result_sequence'])
                    continue
                co.state['status'] = 'ACTIVE'
                co.reviewer_turn()
                self.assertEqual(co.state['next'], 'gate')
                gate = co._gate_answer()
                self.assertEqual(gate['answer']['verdict'], 'PASS')
                Path(co.state['operator_verifications'][0]['log_evidence']).unlink()
                co.state['status'] = 'HOLD'
                co.save()
                self.assertEqual(co.resume(), 'DONE')
                self.assertEqual([row['sequence'] for row in co._gate_turns()], [gate['sequence']])

    def test_static_report_evidence_unchanged(self):
        co = self.coordinator()
        self.attach(co)
        tree = rc.git_snapshot(self.workspace)[0]
        co.state['config']['review_report'] = True
        co.state['report_budget'] = co.args.max_invocations
        co.args.test_command = None
        prompt = co._review_prompt('reviewer', tree)
        self.assertIn('No test command is configured for this review: do not run tests', prompt)
        self.assertNotIn('The configured test passed on this tree', prompt)
        result = co.invoke('reviewer', 'EXEC', prompt, rc.review_schema())
        turn = self.receipt(co, 'reviewer')
        self.assertEqual(result['answer']['status'], 'APPROVE')
        self.assertTrue(turn['static_untested_approval'])
        self.assertNotIn('operator_test_evidence', turn)
        self.assertNotIn('reused_test_evidence', turn)

    def test_failure_detection_excludes_timeout_and_zero_exit_failure_text(self):
        command = 'python3 -m unittest'
        for row in ({'exit_code': 0, 'output': 'FAILED test'},
                    {'exit_code': -1, 'output': 'Command timed out after 1000ms',
                     'source': 'Bash tool_use/tool_result'},
                    {'exit_code': None}, {'exit_code': -1}):
            with self.subTest(row=row):
                self.assertIsNone(rc.completed_test_failure([{'command': command, **row}], command))


if __name__ == '__main__':
    unittest.main()
