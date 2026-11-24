"""WI-I delivery round policy with fake provider executables."""
import json
import os
import unittest
from unittest import mock

from paired_session import test_gate_once as gate
from paired_session import test_worktree_lifecycle as lifecycle

rc = lifecycle.rc


class RoundSplitTests(unittest.TestCase):
    locals().update({name: getattr(lifecycle.WorktreeLifecycleActivationTests, name)
                     for name in (*lifecycle._HELPERS, 'setUp', 'args')})
    state, clean = gate.GateOnceTests.state, gate.GateOnceTests.clean

    def test_plan_cap_continues_then_approves_or_cuts(self):
        result = self.run_coordinator(*gate.OFF, '--adversarial-gate', 'off',
                                      env={'FAKE_PLAN_APPROVE_SECURITY': 'always', 'FAKE_PLAN_STILL_OPEN': '1'})
        self.assertIn(lifecycle.DONE, result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.state()['plan_rounds'], 5)

    def test_exec_splits_at_five_across_configured_limits(self):
        for cap in (3, 5, 7):
            with self.subTest(cap=cap):
                self.run_dir = self.root / f'cap-{cap}'
                result = self.run_coordinator(*gate.OFF, '--adversarial-gate', 'off',
                                              '--max-exec-rounds', str(cap),
                                              env={'FAKE_EXEC_MIXED_REVISE': '1'})
                self.assertIn(lifecycle.DONE, result.stdout, result.stdout + result.stderr)
                self.assertEqual(self.state()['exec_rounds'], 5)
                self.clean()

    def test_gate_fail_leftovers_split_without_regating(self):
        result = self.run_coordinator(*gate.OFF, env={**gate.FAIL, 'FAKE_EXEC_STILL_OPEN': '[gate FAIL]'})
        self.assertIn(lifecycle.DONE, result.stdout, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual(state['exec_rounds'], 5)
        [gate_turn] = gate.gates(state)
        self.assertEqual(gate_turn['answer']['verdict'], 'FAIL')
        split = state['round_splits']['EXEC']
        self.assertEqual(split['ids'], [item['id'] for item in state['gate_fail_items']])
        self.assertEqual(split['gate_sequence'], gate_turn['sequence'])
        for turn in (t for t in state['turns'] if t['sequence'] > gate_turn['sequence'] and t['role'] == 'reviewer'):
            self.assertIn('## Mandatory gate checks', self.prompt(turn))
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'APPROVE')

    def script(self, phase, *steps):
        path = self.root / f'{self.run_dir.name}-{phase}.txt'
        path.write_text('\n'.join(steps) + '\n')
        return {f'FAKE_WI_{phase}_REVIEWS': str(path)}

    def prompt(self, turn):
        name = f"{turn['sequence']:03d}-{turn['phase'].lower()}-{turn['role']}.prompt.txt"
        return (self.run_dir / 'evidence' / name).read_text()

    def test_plan_new_blocker_streak_cuts_once(self):
        for cap, steps, cut in (
                (3, ('correctness:first blocker', 'security:second blocker', 'money:third blocker'), 3),
                (3, ('correctness:first blocker', 'repeat', 'security:third blocker', 'money:fourth blocker'), 4),
                (5, ('correctness:first blocker',) + ('keep',) * 6, 7)):
            with self.subTest(cap=cap, cut=cut):
                self.run_dir = self.root / f'cut-{cut}'
                flags = (*gate.OFF, '--adversarial-gate', 'off', '--max-plan-rounds', str(cap))
                env = self.script('PLAN', *steps)
                if cut == 4:
                    co = rc.Coordinator(rc.parser().parse_args(self.command(*flags)[2:]))
                    capture = co.capture_review_baseline

                    def interrupt(sequence, phase):
                        capture(sequence, phase)
                        if phase == 'PLAN' and co.state['plan_rounds'] == 3:
                            raise InterruptedError('interrupt after recording round three')

                    with mock.patch.dict(os.environ, env), mock.patch.object(co, 'capture_review_baseline', interrupt):
                        with self.assertRaises(InterruptedError):
                            co.drive()
                    before = self.state()
                    self.assertEqual(before['plan_blocker_streak']['count'], 0)
                    self.assertIsNotNone(before['pending_reviewer_result_sequence'])
                    result = self.run_operator_action('resume', *flags, env=env)
                else:
                    result = self.run_coordinator(*flags, env=env)
                self.assertIn(lifecycle.DONE, result.stdout, result.stdout + result.stderr)
                state = self.state()
                self.assertEqual((state['plan_rounds'], state['plan_reviews']), (cut, cut))
                self.assertEqual(len([turn for turn in state['turns'] if turn['phase'] == 'PLAN']), cut * 2 + 2)
                self.assertEqual(state['round_splits']['PLAN']['trigger'], 'new-blocker streak' if cut < 7 else 'round cap')
                self.clean()

    def test_ordinary_approval_in_either_automatic_plan_round(self):
        for rounds in (4, 5):
            with self.subTest(rounds=rounds):
                self.run_dir = self.root / f'approve-{rounds}'
                env = self.script('PLAN', 'correctness:first blocker', *(['keep'] * (rounds - 2)), 'approve')
                result = self.run_coordinator(*gate.OFF, '--adversarial-gate', 'off', env=env)
                self.assertIn(lifecycle.DONE, result.stdout, result.stdout + result.stderr)
                self.assertEqual(self.state()['plan_rounds'], rounds)
                self.assertNotIn('round_splits', self.state())
                self.clean()

    def test_reduced_plan_is_reviewed_once_and_binds_exec_scope(self):
        flags = ('--lifecycle-mode', 'off')
        env = {**self.script('PLAN', 'correctness:first blocker', 'security:second blocker', 'money:third blocker'),
               'FAKE_REDUCED_REVIEW': 'advisory'}
        original = self.workitem.read_bytes()
        result = self.run_coordinator(*flags, env=env)
        self.assertIn(lifecycle.DONE, result.stdout, result.stdout + result.stderr)
        state = self.state()
        proposal = state['reduced_plan']
        self.assertEqual(self.workitem.read_bytes(), original)
        effective = (self.run_dir / 'context/workitem.md').read_text()
        self.assertIn('## Acceptance criteria', effective)
        self.assertIn('Reject bool.', effective)
        self.assertNotRegex(effective, rc.LEDGER_ID_RE)
        self.assertEqual(proposal['status'], 'approved')
        review = next(turn for turn in state['turns'] if 'Reduced PLAN review.' in self.prompt(turn))
        self.assertEqual(review['open_finding_ids'], [])
        self.assertEqual(state['review_verdicts'][3]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
        self.assertIn('Retained input contract needs clarification', (self.run_dir / 'backlog.md').read_text())
        for turn in (turn for turn in state['turns'] if turn['role'] in ('shadow', 'gate')):
            audit = self.run_dir / 'evidence' / f"{turn['sequence']:03d}-{turn['role']}.independence-inputs.json"
            inputs = json.loads(audit.read_text())['inputs']
            self.assertEqual(inputs['context/workitem.md']['content'], effective)
        self.assertIn('Approved plan:', self.prompt(next(t for t in state['turns'] if t['phase'] == 'EXEC')))

    def test_reduced_plan_blocking_revise_holds_with_core_ids(self):
        flags = (*gate.OFF, '--adversarial-gate', 'off', '--max-plan-rounds', '1')
        env = {**self.script('PLAN', 'correctness:first blocker', 'money:second blocker'),
               'FAKE_REDUCED_REVIEW': 'block'}
        result = self.run_coordinator(*flags, env=env)
        before = self.state()
        core = next(row for row in before['finding_ledger'] if 'Retained input' in row['summary'])
        self.assertIn(f"Reduced PLAN HOLD: {core['id']}", result.stdout)
        self.assertIn('use note --scope-change to start a successor run', result.stdout)
        self.assertNotIn('round_splits', before)
        self.assertTrue(all(t['phase'] == 'PLAN' for t in before['turns']))
        for action, extra, expected in (
                ('resume', (), 'Reduced PLAN HOLD'),
                ('resume', ('--add-rounds', '1'), '--add-rounds needs a PLAN or EXEC round-limit HOLD'),
                ('note', ('--text', 'Keep this decision in the audit.'), 'N001'),
                ('resume', (), 'Reduced PLAN HOLD')):
            response = self.run_operator_action(action, *flags, *extra)
            self.assertIn(expected, response.stdout + response.stderr)
            self.assertEqual(self.state()['turns'], before['turns'])
        self.assertEqual(self.state()['operator_notes'][0]['status'], 'pending')
        changed = self.run_operator_action('note', *flags, '--scope-change', '--text', 'Keep the integer helper only.')
        self.assertIn('Start:', changed.stdout)
        self.assertEqual(self.state()['status'], 'ABORTED')

    def test_reduced_plan_no_core_or_invalid_scope(self):
        for name, settings, reason in (
                ('no-core', {'FAKE_REDUCED_NO_CORE': '1'}, 'cannot be separated'),
                ('invalid', {'FAKE_RETAINED_WORKITEM': 'Work item missing criteria'}, 'invalid retained work item'),
                ('history', {'FAKE_RETAINED_WORKITEM': '# Work item\n\n## Acceptance criteria\n\n- Fix F123.'},
                 'ledger-id-shaped tokens')):
            with self.subTest(name=name):
                self.run_dir = self.root / name
                env = {**self.script('PLAN', 'correctness:first blocker', 'money:second blocker'), **settings}
                result = self.run_coordinator(*gate.OFF, '--adversarial-gate', 'off', '--max-plan-rounds', '1', env=env)
                self.assertIn(reason, result.stdout, result.stdout + result.stderr)
                state = self.state()
                self.assertEqual((state['status'], state['reduced_plan']['status']), ('HOLD', 'held'))
                self.assertEqual(len(state['turns']), 5)
                self.assertNotIn('round_splits', state)

    def test_unusable_reduced_verdict_spends_the_single_review(self):
        flags = (*gate.OFF, '--adversarial-gate', 'off', '--max-plan-rounds', '1')
        env = {**self.script('PLAN', 'correctness:first blocker', 'money:second blocker'),
               'FAKE_REDUCED_REVIEW': 'invalid'}
        held = self.run_coordinator(*flags, env=env)
        self.assertIn('Reduced PLAN HOLD', held.stdout)
        state = self.state()
        self.assertEqual(len(state['turns']), 6)
        self.assertNotIn('round_splits', state)
        resumed = self.run_operator_action('resume', *flags)
        self.assertIn('Reduced PLAN HOLD', resumed.stdout)
        self.assertEqual(self.state()['turns'], state['turns'])

    def test_malformed_reduced_review_schema_spends_the_single_review(self):
        flags = (*gate.OFF, '--adversarial-gate', 'off', '--max-plan-rounds', '1')
        env = {**self.script('PLAN', 'correctness:first blocker', 'money:second blocker'),
               'FAKE_REDUCED_REVIEW': 'invalid-schema'}
        result = self.run_coordinator(*flags, env=env)
        self.assertIn('Reduced PLAN HOLD: invalid reduced PLAN review schema', result.stdout,
                      result.stdout + result.stderr)
        before = self.state()
        self.assertEqual(before['reduced_plan']['status'], 'held')
        resumed = self.run_operator_action('resume', *flags)
        self.assertIn('Reduced PLAN HOLD', resumed.stdout, resumed.stdout + resumed.stderr)
        self.assertEqual(self.state()['turns'], before['turns'])

    def test_failed_reduced_review_is_retried_on_resume(self):
        flags = (*gate.OFF, '--adversarial-gate', 'off', '--max-plan-rounds', '1')
        env = {**self.script('PLAN', 'correctness:first blocker', 'money:second blocker'),
               'FAKE_PROVIDER_FAIL': 'segmentation fault during reduced review',
               'FAKE_PROVIDER_FAIL_MATCH': 'Reduced PLAN review.',
               'FAKE_PROVIDER_FAIL_ONCE': str(self.root / 'provider-failed')}
        held = self.run_coordinator(*flags, env=env)
        self.assertIn('HOLD: CLI exit 1', held.stdout, held.stdout + held.stderr)
        before = self.state()
        self.assertEqual(before['reduced_plan']['status'], 'reviewer')
        self.assertNotIn('answer', before['turns'][-1])
        self.assertNotIn('round_splits', before)
        resumed = self.run_operator_action('resume', *flags, env=env)
        self.assertIn(lifecycle.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        state = self.state()
        retry = state['turns'][len(before['turns'])]
        self.assertIn('Reduced PLAN review.', self.prompt(retry))
        self.assertEqual(retry['answer']['status'], 'APPROVE')
        self.assertEqual(state['reduced_plan']['status'], 'approved')
        self.assertTrue(any(turn['phase'] == 'EXEC' for turn in state['turns']))

    def test_automatic_rounds_fit_the_default_invocation_budget(self):
        flags = ('--lifecycle-mode', 'off')
        env = {**self.script('PLAN', 'security:deferred input path', *(['keep'] * 4)),
               'FAKE_EXEC_MIXED_REVISE': '1'}
        co = rc.Coordinator(rc.parser().parse_args(self.command(*flags)[2:]))
        invoke = co.invoke

        def interrupt(role, phase, *args, **kwargs):
            result = invoke(role, phase, *args, **kwargs)
            if role == 'reviewer' and phase == 'EXEC' and co.state['exec_rounds'] == 4:
                raise RuntimeError('interrupted after the paid reviewer receipt')
            return result

        with mock.patch.dict(os.environ, env), mock.patch.object(co, 'invoke', side_effect=interrupt):
            self.assertEqual(co.drive(), 'HOLD')
        before = self.state()
        self.assertEqual(before['hold_reason'], 'interrupted after the paid reviewer receipt')
        self.assertIn('EXEC round 4', before['automatic_invocation_allowances'])
        pending = before['pending_reviewer_result_sequence']
        result = self.run_operator_action('resume', *flags, env=env)
        self.assertIn(lifecycle.DONE, result.stdout, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual((state['config']['max_invocations'], state['plan_rounds'], state['exec_rounds']), (25, 5, 5))
        self.assertGreater(state['invocations_used'], 25)
        self.assertEqual(state['invocations_used'], len(state['turns']))
        self.assertEqual(sum(t['sequence'] == pending for t in state['turns']), 1)
        self.assertEqual(state['turns'][len(before['turns'])]['role'], 'shadow')
        self.assertEqual(state['automatic_invocation_allowances'], {
            'PLAN round 4': 2, 'PLAN round 5': 2, 'reduced PLAN': 2, 'EXEC round 4': 4, 'EXEC round 5': 4, 'EXEC gate': 1})
        self.assertNotIn('invocation_cap_raises', state)
        self.assertEqual(len(gate.gates(state)), 1)
        self.clean()
        self.run_dir = self.root / 'ordinary-budget'
        control = self.run_coordinator(*flags, '--max-invocations', '3')
        self.assertIn('HOLD: invocation limit reached', control.stdout)
        self.assertEqual(self.state()['invocations_used'], 3)
        self.assertNotIn('automatic_invocation_allowances', self.state())

    def test_exec_split_requires_current_evidence_and_valid_gate(self):
        for name, extra, reason in (
                ('failure', {'FAKE_REVIEW_TEST_FAILURE': '1'}, rc.TEST_EVIDENCE_HOLD),
                ('no-self-run', {'FAKE_EXEC_NO_EVIDENCE': '1'}, rc.TEST_EVIDENCE_HOLD),
                ('invalid-gate', {'FAKE_GATE_MALFORMED': 'evidence'}, 'malformed gate verdict')):
            with self.subTest(name=name):
                self.run_dir = self.root / name
                flags = gate.OFF
                result = self.run_coordinator(*flags, env={'FAKE_EXEC_MIXED_REVISE': '1', **extra})
                self.assertIn(reason, result.stdout, result.stdout + result.stderr)
                state = self.state()
                self.assertEqual((state['status'], state['exec_rounds']), ('HOLD', 5))
                self.assertNotIn('round_splits', state)
                self.assertTrue(any(row['status'] == 'open' and rc.blocks_by_severity(row) for row in state['finding_ledger']))
                self.clean()

    def test_exec_split_holds_when_the_tree_changes_after_the_review(self):
        co = rc.Coordinator(rc.parser().parse_args(self.command(*gate.OFF)[2:]))
        capture = co.capture_review_baseline

        def edit_after_review(sequence, phase):
            capture(sequence, phase)
            if phase == 'EXEC' and co.state['exec_rounds'] == 5:
                (self.workspace / 'tracked.txt').write_text('Edited after the review.\n')

        with mock.patch.dict(os.environ, {'FAKE_EXEC_MIXED_REVISE': '1'}), \
                mock.patch.object(co, 'capture_review_baseline', side_effect=edit_after_review):
            self.assertEqual(co.drive(), 'HOLD')
        state = self.state()
        self.assertTrue(state['config']['auto_split_rounds'])
        self.assertEqual(state['exec_rounds'], 5)
        self.assertEqual(state['hold_reason'], 'stale EXEC review at automatic split')
        self.assertNotIn('round_splits', state)
        self.assertEqual(gate.gates(state), [])

    def test_exec_split_holds_on_a_secret_scan_finding(self):
        co = rc.Coordinator(rc.parser().parse_args(self.command(*gate.OFF)[2:]))
        invoke = co.invoke

        def write_secret_on_last_round(role, phase, *args, **kwargs):
            if (role, phase) == ('author', 'EXEC') and co.state['exec_rounds'] + 1 == 5:
                with mock.patch.dict(os.environ, {'FAKE_AUTHOR_SECRET': 'config_local.py'}):
                    return invoke(role, phase, *args, **kwargs)
            return invoke(role, phase, *args, **kwargs)

        with mock.patch.dict(os.environ, {'FAKE_EXEC_MIXED_REVISE': '1'}), \
                mock.patch.object(co, 'invoke', side_effect=write_secret_on_last_round):
            self.assertEqual(co.drive(), 'HOLD')
        state = self.state()
        self.assertTrue(state['config']['auto_split_rounds'])
        self.assertEqual(state['exec_rounds'], 5)
        self.assertIn('secret scan: the change still adds a hardcoded credential', state['hold_reason'])
        self.assertNotIn('round_splits', state)
        self.assertEqual(gate.gates(state), [])

    def test_lifecycle_reviews_receive_transferred_scope(self):
        flags = (*gate.ON, '--quality-writers', 'both', '--auto-commit', 'true')
        (self.workspace / 'new_code.py').write_text(''.join(f'VALUE_{n} = {n}\n' for n in range(30)))
        exec_steps = ['correctness:unresolved reviewer blocker'] * 5
        env = {**self.script('EXEC', *exec_steps), 'FAKE_GATE_SCOPE': 'different scope',
               'FAKE_WRITER_WRITE': 'simplifier', 'FAKE_WRITER_FILE': 'writer_note.py',
               'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'}
        with mock.patch.dict(os.environ, env), mock.patch.object(rc.Coordinator, '_writer_local_check', return_value=True):
            co = rc.Coordinator(rc.parser().parse_args(self.command(*flags)[2:]))
            outcome = co.drive()
        self.assertEqual(outcome, 'DONE', co.state.get('hold_reason'))
        state = self.state()
        [gate_turn] = gate.gates(state)
        self.assertEqual(gate_turn['answer']['verdict'], 'FAIL')
        self.assertEqual(state['gate_fail_round']['round'], 5)
        split = state['round_splits']['EXEC']
        self.assertIn(gate_turn['answer']['criteria'][0]['criterion'], split['summary'])
        writer = state['lifecycle']['quality_writers']['simplifier']
        self.assertEqual(writer['state'], 'wrote')
        after_writer = [turn for turn in state['turns'] if turn['sequence'] > writer['sequence']]
        self.assertTrue({'reviewer', 'shadow'} <= {t['role'] for t in after_writer if t['phase'] == 'EXEC'})
        self.assertTrue({'POLISH-Q', 'DOCS', 'SECURITY'} <= {t['phase'] for t in state['turns'] if t['role'] == 'reviewer'})
        for turn in [t for t in after_writer if t['phase'] == 'EXEC' and t['role'] in ('reviewer', 'shadow')] + [
                t for t in state['turns'] if t['phase'] in ('POLISH-Q', 'DOCS', 'SECURITY') and t['role'] == 'reviewer']:
            self.assertIn('Transferred scope is out of scope, including under a new finding ID.', self.prompt(turn))
            self.assertIn('unresolved reviewer blocker', self.prompt(turn))
        rejected = self.run_operator_action('reject', *flags, '--text', 'Check revised input behavior.',
                                            env={'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'})
        self.assertIn(lifecycle.DONE, rejected.stdout, rejected.stdout + rejected.stderr)
        after = self.state()
        self.assertEqual(after['round_splits']['EXEC'], split)
        self.assertEqual(len(gate.gates(after)), 1)
        reviews = [t for t in after['turns'] if t['sequence'] > state['sequence'] and t['phase'] == 'EXEC'
                   and t['role'] in ('reviewer', 'shadow')]
        self.assertEqual({t['role'] for t in reviews}, {'reviewer', 'shadow'})
        for turn in reviews:
            self.assertIn('Transferred scope is out of scope', self.prompt(turn))

        accepted = self.run_operator_action('accept', *flags)
        self.assertIn('ACCEPTED', accepted.stdout, accepted.stdout + accepted.stderr)
        backlog = (self.workspace / 'BACKLOG.md').read_text()
        self.assertEqual(backlog.count(split['target']), 1)
        journal = json.loads((self.run_dir / 'evidence/delivery-commit.json').read_text())
        (self.run_dir / 'state.json').write_text(json.dumps(after))
        command = self.command(*flags, '--skip-probe', '--expect', journal['intent']['digest'])
        command[2] = 'accept'
        replay = rc.subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertIn('ACCEPTED', replay.stdout, replay.stdout + replay.stderr)
        self.assertEqual((self.workspace / 'BACKLOG.md').read_text(), backlog)
        self.assertIn('### Unmet criteria (gate FAIL)', (self.run_dir / 'delivery-report.md').read_text())

    def test_split_and_accept_replay_keep_one_prioritized_followup(self):
        flags = (*gate.OFF, '--adversarial-gate', 'off')
        findings = 'money:shared issue|correctness:shared issue|security:security issue|data-loss:data issue'
        env = self.script('EXEC', *([findings] * 5))
        with mock.patch.dict(os.environ, env), mock.patch.object(rc.Coordinator, '_render_round_splits',
                                                               side_effect=InterruptedError('publication interrupted')):
            co = rc.Coordinator(rc.parser().parse_args(self.command(*flags)[2:]))
            with self.assertRaises(InterruptedError):
                co.drive()
        done = self.state()
        split = done['round_splits']['EXEC']
        self.assertEqual(len(split['ids']), 4)
        # Simulate the interrupted publisher leaving a missing artifact.
        (self.run_dir / 'backlog.md').unlink(missing_ok=True)
        restored = self.run_operator_action('resume', *flags)
        self.assertIn(lifecycle.DONE, restored.stdout, restored.stdout + restored.stderr)
        self.assertEqual(self.state()['turns'], done['turns'])
        self.assertIn(split['target'], (self.run_dir / 'backlog.md').read_text())
        for _ in range(2):
            accepted = self.run_operator_action('accept', *flags)
            self.assertIn('ACCEPTED', accepted.stdout, accepted.stdout + accepted.stderr)
        repo = (self.workspace / 'BACKLOG.md').read_text()
        self.assertEqual(repo.count(split['target']), 1)
        report = (self.run_dir / 'delivery-report.md').read_text()
        self.assertLess(report.index('**Data loss**'), report.index('**Security**'))
        self.assertLess(report.index('**Security**'), report.index('**Money**'))
        for finding_id in split['ids']:
            self.assertIn(f'{finding_id} → {split["target"]}', report)
            row = next(row for row in done['finding_ledger'] if row['id'] == finding_id)
            self.assertEqual(row['status_history'][-1]['actor'], 'coordinator')
            self.assertEqual(row['status_history'][-1]['reason'], 'auto-split by rule')
        self.assertNotIn('operator_rulings', done)

    def test_saved_default_wins_over_a_later_workspace_profile(self):
        flags = (*gate.OFF, '--adversarial-gate', 'off')
        result = self.run_coordinator(*flags)
        self.assertIn(lifecycle.DONE, result.stdout, result.stdout + result.stderr)
        self.assertTrue(self.state()['config']['auto_split_rounds'])
        profile = self.workspace / '.review-loop' / 'paired-session.json'
        profile.parent.mkdir()
        profile.write_text('{"auto_split_rounds": false}')

        resumed = self.run_operator_action('resume', *flags)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn(lifecycle.DONE, resumed.stdout)
        # Reopen the DONE run and pause its author turn so it can receive a note.
        held = self.run_operator_action('reject', *flags, '--text', 'Recheck input behavior.',
                                        env={'FAKE_PROVIDER_FAIL': 'segmentation fault during author turn'})
        self.assertIn('HOLD: CLI exit 1', held.stdout, held.stdout + held.stderr)
        note = self.run_operator_action('note', *flags, '--text', 'Record the input check.')
        self.assertEqual(note.returncode, 0, note.stdout + note.stderr)
        self.assertIn('N001', note.stdout)
        resumed = self.run_operator_action('resume', *flags)
        self.assertIn(lifecycle.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        accepted = self.run_operator_action('accept', *flags)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn('ACCEPTED', accepted.stdout)
        self.assertTrue(self.state()['config']['auto_split_rounds'])

    def test_legacy_round_limit_contract_is_preserved(self):
        flags = (*gate.OFF, '--adversarial-gate', 'off')
        profile = self.root / 'legacy.json'
        profile.write_text('{"auto_split_rounds": false}')
        env = {'FAKE_EXEC_MIXED_REVISE': '1'}
        result = self.run_coordinator(*flags, '--config', str(profile), env=env)
        self.assertIn('HOLD: EXEC round limit reached', result.stdout)
        self.assertFalse(self.state()['config']['auto_split_rounds'])
        conflict = self.run_operator_action('resume', *flags, '--auto-split-rounds', 'true', env=env)
        self.assertIn('resume configuration differs: auto_split_rounds', conflict.stdout + conflict.stderr)
        saved = self.state()
        del saved['config']['auto_split_rounds']
        (self.run_dir / 'state.json').write_text(json.dumps(saved))
        resumed = self.run_operator_action('resume', *flags, env=env)
        self.assertIn('HOLD: EXEC round limit reached', resumed.stdout)
        self.assertEqual(self.state()['exec_rounds'], 3)
        profile.write_text('{"auto_split_rounds": true}')
        resumed = self.run_operator_action('resume', *flags, '--config', str(profile), env=env)
        self.assertIn('HOLD: EXEC round limit reached', resumed.stdout, resumed.stdout + resumed.stderr)
        self.assertEqual(self.state()['exec_rounds'], 3)
        self.assertNotIn('auto_split_rounds', self.state()['config'])

    def test_initial_exec_continues_after_the_r4_streaks(self):
        cases = (
            ('still-open', (*gate.OFF, '--adversarial-gate', 'off', '--exercise-revisions'),
             {'FAKE_EXEC_STILL_OPEN': 'required exercise'}),
            ('new-regressions', gate.OFF, {**gate.FAIL, 'FAKE_POST_GATE_FINDING': 'regression: the fix drops a case'}),
        )
        for name, flags, env in cases:
            with self.subTest(name=name):
                self.run_dir = self.root / name
                result = self.run_coordinator(*flags, env=env)
                self.assertIn(lifecycle.DONE, result.stdout, result.stdout + result.stderr)
                state = self.state()
                self.assertEqual(state['exec_rounds'], 5)
                self.assertIn('EXEC', state['round_splits'])
                self.assertNotIn('round_extensions', state)
                self.assertNotIn('round_limit_hold', state)
                self.clean()

    def test_terminal_post_gate_shadow_blocker_gets_same_tree_confirmation(self):
        marker = self.root / 'gate-failed'
        env = {'FAKE_GATE_BLOCK_ONCE': str(marker), 'FAKE_EXEC_STILL_OPEN': '[gate FAIL]',
               'FAKE_SHADOW_BLOCKER_IF_FILE': str(marker), 'FAKE_SHADOW_BLOCKER_REPEATS': '4',
               'FAKE_CONFIRM_SHADOW': 'still_open'}
        result = self.run_coordinator('--lifecycle-mode', 'off', env=env)
        self.assertIn(lifecycle.DONE, result.stdout, result.stdout + result.stderr)
        state = self.state()
        shadow, confirmation = state['turns'][-2:]
        self.assertEqual((shadow['role'], confirmation['role']), ('shadow', 'reviewer'))
        self.assertEqual(shadow['snapshot_before'], confirmation['snapshot_before'])
        self.assertIn('shadow finding to confirm', self.prompt(confirmation))
        self.assertEqual((state['exec_rounds'], len(gate.gates(state))), (5, 1))
        terminal_shadow = next(row for row in state['finding_ledger'] if row['origin_round'] == shadow['sequence'])
        self.assertEqual(terminal_shadow['confirmed_regression'], confirmation['sequence'])
        self.assertIn(terminal_shadow['id'], state['round_splits']['EXEC']['ids'])
