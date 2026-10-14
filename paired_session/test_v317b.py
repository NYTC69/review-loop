"""Operator rulings persist independently of rejection, review and writer replay."""
import copy
import json
import os
import unittest
from unittest import mock

from paired_session import review_report
from paired_session import test_d09_c1b2 as writers
from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE
REASON = 'Expanded export scope is approved and stays.'


class WithdrawalTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name)
                    for name in (*twl._HELPERS, 'setUp')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def row(self, co, source='adversarial-gate', severity='MEDIUM', **extra):
        [row] = co.record_findings(source, 'EXEC', co.state['sequence'], [
            {'severity': severity, 'file': 'sum_ints.py', 'summary': 'expanded export scope',
             'body': 'scope', 'failure_scenario': 'scope', **extra}])
        return next(item for item in co.state['finding_ledger'] if item['id'] == row['id'])

    def ready(self, route='off'):
        co = self.coordinator('--lifecycle-mode', route)
        co.state.update(status='DONE', acceptance_state='PENDING')
        row = self.row(co)
        co.args.finding, co.args.reason = [row['id']], REASON
        co.save()
        return co, row

    def withdraw(self, co):
        co.args.expect = co.operator_intent('withdraw-findings', None, None)['digest']
        return co.withdraw_findings()

    def cli_withdraw(self, ids, *flags, reason=REASON):
        command = self.command(*flags, '--reason', reason, '--skip-probe')
        command[2] = 'withdraw-findings'
        for finding_id in ids: command.extend(['--finding', finding_id])
        issued = rc.subprocess.run([*command, '--intent-only'], cwd=self.root, text=True, capture_output=True)
        self.assertEqual(issued.returncode, 0, issued.stdout + issued.stderr)
        intent = json.loads(issued.stdout)
        self.assertIn('--expect ' + intent['digest'], intent['next_command'])
        result = rc.subprocess.run([*command, '--expect', intent['digest']], cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.state()
        ruling = state['operator_rulings'][-1]
        self.assertIn(f"WITHDRAWN {ruling['id']}: {', '.join(ruling['finding_ids'])} (evidence: {ruling['evidence']})", result.stdout)
        status = (DONE if state['status'] == 'DONE' else 'HOLD: ' + state['hold_reason'])
        self.assertEqual(result.stdout.strip().splitlines()[-1], status)
        if state['status'] == 'HOLD' and (state.get('round_limit_hold') or {}).get('hold_reason') == state['hold_reason']:
            self.assertIn('NEXT: resume re-reviews the same tree', result.stdout)
        else:
            self.assertNotIn('NEXT: resume re-reviews the same tree', result.stdout)
        return result

    def test_done_multiselect_evidence_ledger_report_and_resume_both_routes(self):
        for route in ('off', 'on'):
            with self.subTest(route=route):
                self.run_dir = self.root / ('done-' + route)
                flags = ('--lifecycle-mode', route, '--max-invocations', '80')
                done = self.run_coordinator(*flags, env={'FAKE_GATE_MINOR': '1'})
                self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
                args = rc.parser().parse_args(self.command(*flags)[2:]); args.action = 'resume'
                co = rc.Coordinator(args)
                second = self.row(co, 'fresh-shadow', 'MINOR')
                co.save()
                before, tree = self.state(), rc.git_snapshot(self.workspace)[0]
                ids = [row['id'] for row in before['finding_ledger'] if row['status'] == 'open']
                self.assertIn(second['id'], ids)
                self.cli_withdraw(ids, *flags)
                after = self.state()
                self.assertEqual(rc.git_snapshot(self.workspace)[0], tree)
                for key in ('status', 'acceptance_state', 'next', 'sequence', 'turns', 'exec_rounds', 'rejections',
                            'rejected_digests', 'lifecycle', 'gate_ran'):
                    self.assertEqual(after.get(key), before.get(key), key)
                [ruling] = after['operator_rulings']
                self.assertEqual((ruling['id'], ruling['author'], ruling['finding_ids'], ruling['reason']),
                                 ('O001', 'operator', ids, REASON))
                self.assertEqual(json.loads(rc.Path(ruling['evidence']).read_text()), ruling)
                for row in after['finding_ledger']:
                    if row['id'] in ids:
                        self.assertEqual(row['status'], 'withdrawn')
                        history = row['status_history'][-1]
                        self.assertEqual((history['actor'], history['ruling_id']), ('operator', 'O001'))
                        self.assertIn(REASON, history['evidence'])
                events = [json.loads(line) for line in (self.run_dir / 'progress.jsonl').read_text().splitlines()]
                self.assertEqual([event['id'] for event in events if event['kind'] == 'finding'
                                  and event.get('status') == 'withdrawn'], ids)
                for name in ('findings-ledger.md', 'open-findings.md'):
                    text = (self.run_dir / name).read_text()
                    for value in ('O001', REASON, *ids): self.assertIn(value, text)
                self.assertNotIn('No disposition is due', (self.run_dir / 'open-findings.md').read_text())
                self.assertNotIn('same objection again', (self.run_dir / 'open-findings.md').read_text())
                resumed = self.run_operator_action('resume', *flags)
                self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
                self.assertEqual(self.state()['operator_rulings'], after['operator_rulings'])
                accepted = self.run_operator_action('accept', *flags)
                self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
                report = (self.run_dir / 'delivery-report.md').read_text()
                for value in ('O001', 'operator', REASON, 'withdrawn', *ids): self.assertIn(value, report)
                self.assertIn('## 操作者撤回（operator rulings）', report)
                self.assertNotIn('## Operator withdrawals', report)

    def test_accepts_only_the_named_states_and_refuses_each_excluded_row(self):
        co, row = self.ready()
        for status, reason, phase in [('DONE', '', 'EXEC'), ('HOLD', 'PLAN round limit reached', 'PLAN'),
                                      ('HOLD', 'EXEC round limit reached', 'EXEC'), ('HOLD', 'rejected-tree', 'EXEC')]:
            with self.subTest(status=status, reason=reason):
                co.state.update(status=status, hold_reason=reason, phase=phase,
                                round_limit_hold={'hold_reason': reason, 'phase': phase})
                self.assertEqual(co._withdrawal_rows(), [row])
        for status, reason in [('ACTIVE', ''), ('ACCEPTED', ''), ('HOLD', 'reviewer HOLD'),
                               ('REPORTED', ''), ('ABORTED', '')]:
            co.state.update(status=status, hold_reason=reason)
            with self.assertRaisesRegex(ValueError, 'requires DONE'): co._withdrawal_rows()
        co.state.update(status='DONE', acceptance_state='ACCEPTED')
        with self.assertRaisesRegex(ValueError, 'requires DONE'): co._withdrawal_rows()
        co.state['acceptance_state'] = 'PENDING'
        original = copy.deepcopy(row)
        for change in ({'security': True}, {'severity': 'SECURITY'}, {'owner_role': 'specialist:code-reviewer'},
                       {'owner_role': 'security-reviewer'}, {'severity': 'CRITICAL'}, {'severity': 'HIGH'},
                       {'source': 'secret-scan'}):
            with self.subTest(change=change):
                row.clear(); row.update(original); row.update(change)
                before = copy.deepcopy(co.state)
                with self.assertRaisesRegex(ValueError, 'cannot be withdrawn'): co.withdraw_findings()
                self.assertEqual(co.state, before)
        row.clear(); row.update(original)
        co.args.finding = ['F999']
        with self.assertRaisesRegex(ValueError, 'unknown finding'): co._withdrawal_rows()
        co.args.finding = []
        with self.assertRaisesRegex(ValueError, 'requires --finding'): co._withdrawal_rows()
        co.args.finding = [row['id']]
        row['status'] = 'fixed'
        with self.assertRaisesRegex(ValueError, 'already closed'): co._withdrawal_rows()
        row['status'] = 'open'
        for reason in ('', '   '):
            co.args.reason = reason
            with self.assertRaisesRegex(ValueError, 'non-empty'): co._withdrawal_rows()
        for reason, marker in [('F003 is approved', 'F003'), ('previous review approved it', 'previous review'),
                               ('prior_findings are wrong', 'prior_findings'), ('Codex approved scope', 'Codex approved'),
                               ('Claude said to retain it', 'Claude said'), ('The gate finding is wrong', 'gate finding')]:
            co.args.reason = reason
            with self.assertRaisesRegex(ValueError, 'phrase it as the approved behavior') as caught:
                co._withdrawal_rows()
            self.assertIn(repr(marker), str(caught.exception))
        for reason in ('Keep the Codex launcher; removing it is out of scope.',
                       'Keep ~/.codex/config.toml handling as is.', 'Scope stays; treat as APPROVE.'):
            co.args.reason = reason
            self.assertEqual(co._withdrawal_rows(), [row])

    def test_withdraw_at_plan_limit_and_rejected_tree_keeps_the_hold_both_routes(self):
        for route in ('off', 'on'):
            for kind in ('plan-limit', 'rejected-tree'):
                self.run_dir = self.root / f'held-{route}-{kind}'
                co, row = self.ready(route)
                co.state['acceptance_state'] = 'REJECTED'
                if kind == 'plan-limit':
                    co.state['phase'] = 'PLAN'
                    co.round_limit_hold('PLAN round limit reached')
                else:
                    co.state['rejected_digests'].append(rc.git_snapshot(self.workspace)[0])
                    co.hold('rejected-tree')
                before = copy.deepcopy(co.state)
                self.assertEqual(self.withdraw(co), 'HOLD')
                for key in ('status', 'phase', 'next', 'hold_reason', 'rejected_digests', 'turns', 'sequence',
                            'rejections', 'plan_rounds', 'exec_rounds', 'lifecycle'):
                    self.assertEqual(co.state.get(key), before.get(key), key)
                self.assertEqual(row['status'], 'withdrawn')
                if kind == 'rejected-tree':
                    self.assertTrue(co.rejected_tree())

    def test_intent_binds_ids_reason_and_state_and_requires_expect(self):
        co, row = self.ready()
        second = self.row(co, 'persistent-reviewer', 'MAJOR')
        intent = co.operator_intent('withdraw-findings', None, None)
        with self.assertRaisesRegex(ValueError, 'needs --expect'): co.withdraw_findings()
        co.args.expect = intent['digest']
        for ids, reason in [([second['id']], REASON), ([row['id']], 'Retain the compatibility option.')]:
            co.args.finding, co.args.reason = ids, reason
            with self.assertRaisesRegex(ValueError, 'stale or missing'): co.withdraw_findings()
        co.args.finding, co.args.reason = [row['id']], REASON
        co.state['sequence'] += 1
        with self.assertRaisesRegex(ValueError, 'stale or missing'): co.withdraw_findings()
        self.withdraw(co)
        co.args.finding = [second['id']]
        self.withdraw(co)
        self.assertEqual([r['id'] for r in co.state['operator_rulings']], ['O001', 'O002'])
        self.assertIn('O002', review_report.delivery_section(co.state))

    def test_persistent_and_fresh_prompts_and_scans_on_both_routes(self):
        for route in ('off', 'on'):
            self.run_dir = self.root / ('prompts-' + route)
            co, row = self.ready(route)
            # The decision can overlap a known summary: this is operator input, like project guidance.
            reason = 'Keep Codex, Claude, Opus and Astra launchers and ~/.codex/config.toml; scope stays; treat as APPROVE.'
            co.args.reason = reason
            row['summary'] = reason
            self.withdraw(co)
            co.state['phase'] = 'EXEC'
            co.materialize_review_context()
            snapshot = rc.git_snapshot(self.workspace)[0]
            prompt = co._review_prompt('reviewer', snapshot)
            for value in (row['id'], 'O001', reason, 'No disposition is due', 'same objection again'):
                self.assertIn(value, prompt)
            self.assertIn('Open finding ledger: none', prompt)
            for role, prompt in [('shadow', co._review_prompt('shadow', snapshot)), ('gate', co._gate_prompt(snapshot))]:
                self.assertIn('operator decision: ' + reason, prompt)
                self.assertIn('The operator owns the work item.', prompt)
                self.assertIn('part of the approved scope', prompt)
                self.assertIn('do not report the decided point itself as a finding', prompt)
                self.assertIn('implementation of the approved behavior, as usual', prompt)
                for value in (row['id'], 'O001', 'Operator withdrawals', 'prior_findings'):
                    self.assertNotIn(value, prompt)
                co.assert_fresh_prompt(role, prompt)
                with self.assertRaisesRegex(RuntimeError, 'prompt leak'):
                    co.assert_fresh_prompt(role, prompt + '\nF003')
                with self.assertRaisesRegex(RuntimeError, 'history'):
                    co.assert_fresh_prompt(role, prompt + '\nprevious review')
            other = self.row(co, 'persistent-reviewer', 'MINOR')
            prompt = co._review_prompt('reviewer', snapshot)
            self.assertIn('## Operator withdrawals', prompt)
            self.assertIn(f"O001: {row['id']}: {reason}", prompt)
            self.assertIn('No disposition is due', prompt)
            self.assertIn('Open finding ledger (', prompt)
            self.assertIn(f"- {other['id']}: [source=persistent-reviewer; severity=MINOR] {other['summary']}", prompt)
            self.assertIn(rc.LEDGER_DISPOSITION_GUIDANCE, prompt)

    def test_stale_operator_dispositions_are_ignored_in_receipts_including_retry_and_replay(self):
        for route in ('off', 'on'):
            for retry in (False, True):
                with self.subTest(route=route, retry=retry):
                    self.run_dir = self.root / f'ignored-{route}-{retry}'
                    co, withdrawn = self.ready(route)
                    co.args.shadow = 'off'
                    self.withdraw(co)
                    history = copy.deepcopy(withdrawn['status_history'])
                    other = self.row(co, 'persistent-reviewer', 'MINOR')
                    snapshot = rc.git_snapshot(self.workspace)[0]
                    stale = {'id': withdrawn['id'], 'disposition': 'still_open', 'evidence': 'remembered objection'}
                    fixed = {'id': other['id'], 'disposition': 'fixed', 'evidence': 'verified fix'}
                    command = co.args.test_command
                    answer = {'status': 'APPROVE', 'full_review': [],
                              'verified_claims': [{'claim': 'The tracked fixture file exists.', 'file': 'tracked.txt', 'line': 1}],
                              'reviewed_snapshot': snapshot, 'prior_findings': [stale] if retry else [stale, fixed],
                              'self_run_evidence': [{'command': command}],
                              'observed_commands': [{'command': command, 'exit_code': 0, 'error': False,
                                                     'output': 'Ran 1 test successfully'}]}
                    sequence = co.state['sequence'] + 1
                    receipt = {'sequence': sequence, 'role': 'reviewer', 'phase': 'EXEC', 'answer': answer,
                               'snapshot_before': snapshot, 'open_finding_ids': [withdrawn['id'], other['id']]}
                    co.state.update(status='ACTIVE', acceptance_state='IN_PROGRESS', phase='EXEC', next='reviewer',
                                    exec_rounds=1, sequence=sequence, pending_reviewer_result_sequence=sequence)
                    co.state['turns'].append(receipt)
                    retry_answer = {**answer, 'prior_findings': [stale, fixed]}
                    retry_receipt = {**receipt, 'sequence': sequence + 1, 'answer': retry_answer,
                                     'open_finding_ids': [other['id']]}

                    def protocol_retry(*args, **kwargs):
                        co.state['sequence'] += 1
                        co.state['turns'].append(retry_receipt)
                        return {'answer': retry_answer, 'snapshot': snapshot, 'sequence': sequence + 1,
                                'open_finding_ids': [other['id']]}

                    with mock.patch.object(co, 'invoke', side_effect=protocol_retry) as invoke:
                        co.reviewer_turn()
                    self.assertEqual(invoke.call_count, int(retry))
                    self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'gate'))
                    self.assertEqual((withdrawn['status'], withdrawn['status_history']), ('withdrawn', history))
                    self.assertEqual(other['status'], 'fixed')
                    used = [receipt, retry_receipt] if retry else [receipt]
                    for turn in used:
                        self.assertIn(stale, turn['answer']['prior_findings'])  # original response remains auditable
                        self.assertEqual(turn['ignored_prior_findings'], [{**stale, 'ignored_reason': 'operator-withdrawn'}])
                        evidence = json.loads((co.evidence / f"{turn['sequence']:03d}-exec-reviewer.receipt.json").read_text())
                        self.assertEqual(evidence['ignored_prior_findings'], turn['ignored_prior_findings'])
                        self.assertIn(stale, evidence['answer']['prior_findings'])
                    saved = self.state()
                    self.assertEqual(saved['turns'][-1]['ignored_prior_findings'], used[-1]['ignored_prior_findings'])
                    # Replay the durable response: stale rows stay ignored and fixed history is not duplicated.
                    other_history = copy.deepcopy(other['status_history'])
                    co.state['pending_reviewer_result_sequence'] = used[-1]['sequence']
                    co.reviewer_turn()
                    self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'gate'))
                    self.assertEqual(other['status_history'], other_history)
                    self.assertEqual(withdrawn['status_history'], history)

    @mock.patch.dict(os.environ, {'FAKE_AUTHOR_WRITE_NEW_TREE': ''})  # AUTH names are frozen from the first turn
    def test_field_case_send_back_still_open_withdraw_resume_done_both_routes(self):
        for route in ('off', 'on'):
            with self.subTest(route=route):
                self.run_dir = self.root / ('field-' + route)
                flags = ('--lifecycle-mode', route, '--max-exec-rounds', '1', '--max-invocations', '100')
                env = {'FAKE_GATE_SCOPE': REASON, 'FAKE_EXEC_STILL_OPEN': 'expanded export scope'}
                done = self.run_coordinator(*flags)
                self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
                sent_back = self.run_operator_action('reject', *flags, '--text', 'Add export requirements.', env=env)
                self.assertIn(DONE, sent_back.stdout, sent_back.stdout + sent_back.stderr)
                self.assertTrue(any(row['source'] == 'adversarial-gate' and row['severity'] == 'MEDIUM'
                                    for row in self.state()['finding_ledger']))
                # V316 now exits a pure still_open re-review with successful test evidence. Missing observed
                # test evidence retains the historical round-limit HOLD, without undoing that completed batch.
                # The second send-back also makes an incidental write, so the rejected-tree exit stays distinct.
                held = self.run_operator_action('reject', *flags, '--text', 'Expanded scope is approved and stays. Update export help.',
                                                env={**env, 'FAKE_PERSISTENT_REVIEW_NO_TEST_EVENT': '1',
                                                     'FAKE_AUTHOR_WRITE_NEW_TREE': '1'})
                self.assertIn('HOLD: EXEC round limit reached', held.stdout, held.stdout + held.stderr)
                before = self.state()
                reviewer = next(t for t in reversed(before['turns']) if t['role'] == 'reviewer' and t['phase'] == 'EXEC')
                self.assertEqual(reviewer['answer']['full_review'], [])
                self.assertTrue(any(d['disposition'] == 'still_open' for d in reviewer['answer']['prior_findings']))
                ids = [row['id'] for row in before['finding_ledger'] if row['status'] == 'open']
                self.cli_withdraw(ids, *flags)
                withdrawn = self.state()
                self.assertEqual(withdrawn['turns'], before['turns'])
                resumed = self.run_operator_action('resume', *flags, env=env)
                self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
                after = self.state()
                later = [t for t in after['turns'] if t['sequence'] > before['sequence']]
                self.assertEqual(later[0]['role'], 'reviewer')
                self.assertEqual(later[0]['answer']['prior_findings'], [])
                self.assertFalse(any(t['role'] == 'author' and t['phase'] == 'EXEC' for t in later))
                self.assertEqual((after['exec_rounds'], len(after['rejections'])), (before['exec_rounds'], 2))
                self.assertEqual(after['operator_rulings'], withdrawn['operator_rulings'])
                self.assertFalse(any(row['status'] == 'open' for row in after['finding_ledger']))
                for role in ('shadow', 'gate'):
                    turns = [t for t in later if t['role'] == role]
                    self.assertTrue(turns)
                    text = (self.run_dir / 'evidence' / f"{turns[0]['sequence']:03d}-exec-{role}.prompt.txt").read_text()
                    self.assertIn('operator decision: ' + REASON, text)

    def test_fake_cli_writer_replay_withdrawal_during_hold_survives_tree_rollback(self):
        base = writers.D09C1b2Tests.change(self)
        co = rc.Coordinator(rc.parser().parse_args(self.command(*writers.RUN)[2:]))
        original = co._writer_replay
        withdrawn = []

        def pause_replay():
            row = self.row(co)
            original()  # captures the row's open status before the operator ruling
            co.round_limit_hold('EXEC round limit reached after a writer pass')
            co.args.finding, co.args.reason = [row['id']], REASON
            self.withdraw(co)
            withdrawn.append(row['id'])

        env = {'FAKE_WRITER_WRITE': 'simplifier', 'FAKE_WRITER_FILE': 'writer_note.py',
               'FAKE_EXEC_REVISE_IF_FILE': str(self.workspace / 'writer_note.py')}
        with mock.patch.dict(os.environ, env), mock.patch.object(co, '_writer_local_check', return_value=True), \
                mock.patch.object(co, '_writer_replay', side_effect=pause_replay):
            self.assertEqual(co.drive(), 'HOLD', co.state.get('hold_reason'))
            held = copy.deepcopy(co.state)
            self.assertEqual(co.resume(), 'DONE', co.state.get('hold_reason'))
        row = next(row for row in co.state['finding_ledger'] if row['id'] == withdrawn[0])
        self.assertEqual(row['status'], 'withdrawn')
        self.assertEqual(row['status_history'][-1]['actor'], 'operator')
        self.assertEqual(co.state['operator_rulings'], held['operator_rulings'])
        self.assertEqual(rc.git_snapshot(self.workspace)[0], base)
        self.assertEqual(co.state['lifecycle']['quality_writers']['simplifier']['state'], 'rolled-back:review')
        for role in ('reviewer', 'shadow'):
            turn = next(t for t in co.state['turns'] if t['sequence'] > held['sequence'] and t['role'] == role)
            prompt = (self.run_dir / 'evidence' / f"{turn['sequence']:03d}-exec-{role}.prompt.txt").read_text()
            self.assertIn(REASON, prompt)
            if role == 'shadow': self.assertNotIn(withdrawn[0], prompt)

    def test_writer_replay_rollback_and_durable_dispositions_keep_operator_withdrawal(self):
        co, row = self.ready('on')
        replay = {'statuses': {row['id']: 'open'}}
        # Withdrawal during a paused writer replay must survive its earlier open-status snapshot.
        co.state.update(status='HOLD', hold_reason='EXEC round limit reached', phase='EXEC',
                        round_limit_hold={'hold_reason': 'EXEC round limit reached', 'phase': 'EXEC'})
        self.withdraw(co)
        history = copy.deepcopy(row['status_history'])
        co._revert_replay_findings(replay, 44, 'writer rollback')
        self.assertEqual((row['status'], row['status_history']), ('withdrawn', history))
        # A ruling made before replay is likewise captured as withdrawn and survives rollback.
        co._revert_replay_findings({'statuses': {row['id']: 'withdrawn'}}, 45, 'writer rollback')
        self.assertEqual(row['status'], 'withdrawn')
        co.save()
        co.args.action = 'resume'
        restored = rc.Coordinator(co.args)
        self.assertEqual(restored.state['operator_rulings'], co.state['operator_rulings'])
        self.assertEqual(restored.state['finding_ledger'][0]['status'], 'withdrawn')


if __name__ == '__main__': unittest.main()
