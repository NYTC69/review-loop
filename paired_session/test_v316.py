"""V316 (owner 2026-10-13; four field runs held with `EXEC round limit reached`): at the EXEC round limit, a REVISE
without any finding that only reports earlier non-blocking rows `still_open` (gate MEDIUM/LOW rows after a repair round)
ends as APPROVE_WITH_ADVISORY instead of HOLD. A round that is still available, a blocking row left open, a failing
configured test, no self-run evidence and a changed tree keep their behavior.
WI-GATE1: the gate reports no MEDIUM/LOW rows any more, so the driven runs take their non-blocking row from the EXEC
reviewer (a MINOR on its first approval) next to the gate's one FAIL item."""
import json
import os
import unittest
from unittest import mock

from paired_session import test_worktree_lifecycle as twl
from paired_session.fake_cli import FAKE_SCENARIO_MATRIX

rc, DONE = twl.rc, twl.DONE
LIMIT = 'EXEC round limit reached'
KEEP = 'advisory naming'   # the first EXEC reviewer approval's MINOR row; the gate's FAIL item is 'reject bool'
SHAPED = {'FAKE_GATE_BLOCK': '1', 'FAKE_EXEC_APPROVE_MINOR': '1'}
OFF = ('--lifecycle-mode', 'off', '--shadow', 'off')   # the stop-after-gate route
ON = ('--lifecycle-mode', 'on', '--max-invocations', '60')


class RoundLimitStillOpenTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'git')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def gate_shaped(self, *flags, keep=KEEP):
        """The reviewer approves with a MINOR row and the gate fails with one item; every re-review after it is a REVISE
        without a finding that reports the rows named `keep` still_open and the others fixed."""
        return self.run_coordinator(*flags, env={**SHAPED, 'FAKE_EXEC_STILL_OPEN': keep})

    def gate_in_process(self, *flags):
        with mock.patch.dict(os.environ, {**SHAPED, 'FAKE_EXEC_STILL_OPEN': KEEP}):
            co = rc.Coordinator(rc.parser().parse_args(self.command(*flags)[2:]))
            return co, co.drive()

    def exec_verdicts(self, state):
        return [(row['reviewer_raw_verdict'], row['effective_verdict']) for row in state['review_verdicts'] if row['phase'] == 'EXEC']

    def assert_exit(self, state, severity):
        """The last EXEC review was a REVISE without a finding, taken as APPROVE_WITH_ADVISORY; the advisory row it left
        open is still open and marked advisory."""
        last = [row for row in state['review_verdicts'] if row['phase'] == 'EXEC'][-1]
        self.assertEqual((last['reviewer_raw_verdict'], last['effective_verdict']), ('REVISE', 'APPROVE_WITH_ADVISORY'))
        answer = next(turn for turn in state['turns'] if turn['sequence'] == last['sequence'])['answer']
        self.assertEqual(answer['full_review'], [])
        [kept] = [row['id'] for row in answer['prior_findings'] if row['disposition'] == 'still_open']
        [row] = [row for row in state['finding_ledger'] if row.get('advisory')]
        self.assertEqual((row['id'], row['source'], row['severity'], row['status']), (kept, 'persistent-reviewer', severity, 'open'))
        self.assertNotIn('round_limit_hold', state)
        return row

    def test_a_gate_low_left_open_at_the_last_round_ends_approved_with_advisory(self):   # the full-lifecycle route
        done = self.gate_shaped(*ON, '--max-exec-rounds', '2')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        row = self.assert_exit(state, 'MINOR')
        self.assertEqual(state['exec_rounds'], 2)   # the repair round was the last one
        self.assertEqual([r['stage'] for r in state['lifecycle']['receipts']], ['FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])
        [high] = [r for r in state['finding_ledger'] if r['severity'] == 'HIGH']
        self.assertEqual(high['status'], 'fixed')
        # the existing rendering names the row: the ledger, the open-findings file, the comparison and the progress line
        for name in ('findings-ledger.md', 'open-findings.md'):
            [line] = [line for line in (self.run_dir / name).read_text().splitlines() if line.startswith(f"| {row['id']} ")]
            self.assertIn('| open (advisory) |', line)
        self.assertIn('| REVISE | APPROVE_WITH_ADVISORY |', (self.run_dir / 'review-comparison.md').read_text())
        self.assertIn(f"verdict · APPROVE_WITH_ADVISORY · raw REVISE · (1 open: {row['id']} MINOR)", done.stdout)

    def test_the_exit_on_the_stop_after_gate_route(self):
        done = self.gate_shaped(*OFF, '--max-exec-rounds', '2')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assert_exit(state, 'MINOR')
        self.assertEqual((state['exec_rounds'], 'lifecycle' in state), (2, False))
        self.assertEqual(self.exec_verdicts(state)[0], ('APPROVE', 'APPROVE'))   # the review before the gate blocked

    def test_first_advisory_revise_repairs_and_second_exits_with_a_round_left(self):
        for route in (OFF, ON):
            with self.subTest(route=route):
                (self.root / 'gate-once').unlink(missing_ok=True)
                self.run_dir = self.root / ('second-advisory-' + route[1])
                done = self.gate_shaped(*route, '--max-exec-rounds', '4')
                self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
                state = self.state()
                # WI-GATE1 S3: the first advisory-only REVISE (after the gate repair) exits at once, no repair round
                self.assertEqual(self.exec_verdicts(state), [('APPROVE', 'APPROVE'), ('REVISE', 'APPROVE_WITH_ADVISORY')])
                authors = [turn['sequence'] for turn in state['turns'] if turn['role'] == 'author' and turn['phase'] == 'EXEC']
                self.assertEqual((len(authors), state['exec_rounds']), (2, 2))
                self.assert_exit(state, 'MINOR')

    def test_a_gate_high_still_open_at_the_last_round_holds(self):   # WI-GATE1 R2: after the two post-gate fix rounds
        held = self.gate_shaped(*OFF, '--auto-split-rounds', 'false', '--max-exec-rounds', '2', keep='reject bool')
        self.assertEqual(held.stdout.strip().splitlines()[-1], 'HOLD: ' + rc.GATE_SPLIT_REASON, held.stdout + held.stderr)
        state = self.state()
        self.assertEqual(self.exec_verdicts(state)[-1], ('REVISE', 'REVISE'))
        self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))

    def test_an_operator_send_back_round_that_leaves_an_advisory_row_open_takes_the_exit(self):
        env = {'FAKE_EXEC_APPROVE_MINOR': '1', 'FAKE_EXEC_STILL_OPEN': KEEP}   # one MINOR row, never a blocker; the gate passes
        flags = (*OFF, '--max-exec-rounds', '1')
        done = self.run_coordinator(*flags, env=env)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        first = self.state()
        self.assertEqual(self.exec_verdicts(first), [('APPROVE', 'APPROVE')])
        [medium] = [row for row in first['finding_ledger'] if row['status'] == 'open']
        rejected = self.run_operator_action('reject', *flags, '--text', 'Also reject floats.', env=env)
        self.assertIn(DONE, rejected.stdout, rejected.stdout + rejected.stderr)
        state = self.state()
        self.assertEqual((state['exec_rounds'], state['config']['max_exec_rounds'], len(state['rejections'])), (2, 1, 1))
        self.assertEqual(self.assert_exit(state, 'MINOR')['id'], medium['id'])
        later = [(turn['role'], turn['phase']) for turn in state['turns'] if turn['sequence'] > first['sequence']]
        self.assertEqual(later, [('author', 'EXEC'), ('reviewer', 'EXEC')])   # WI-GATE1: no gate after a send-back

    def test_the_advisory_fix_round_re_review_that_only_reports_rows_still_open_takes_the_exit(self):
        """ADVFIX: "one fix round, never a second for advisory findings" also when the re-review has no finding of its own
        (before V316 this REVISE went back to the author while ordinary rounds were left)."""
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        mark = self.root / 'advisory-ran'
        done = self.run_coordinator('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '80', '--advisory-fix-round', 'true',
                                    env={'FAKE_EXEC_APPROVE_MINOR': '1', 'FAKE_AUTHOR_ADVISORY_TOUCH': str(mark),
                                         'FAKE_EXEC_STILL_OPEN': 'advisory naming', 'FAKE_EXEC_STILL_OPEN_IF_FILE': str(mark)})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertTrue(mark.exists())
        self.assertEqual(len([t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC']), 1)
        self.assertEqual(self.exec_verdicts(state)[-1], ('REVISE', 'APPROVE_WITH_ADVISORY'))
        self.assertEqual(state['exec_rounds'], 2)   # 1 + the round; 3 ordinary rounds were left, none used
        [row] = [row for row in state['finding_ledger'] if row.get('advisory')]
        self.assertEqual((row['source'], row['severity'], row['status']), ('persistent-reviewer', 'MINOR', 'open'))

    def review(self, name, rows, rounds=2, limit=2, test=(0, 'Ran 1 test successfully'), phase='EXEC', prepare=None, flags=(), run=True, **change):
        """One recorded re-review at round `rounds` of `limit`: REVISE, no finding, every row of `rows` (source,
        severity) still_open, the configured test observed as `test` (exit code, output; None: not run) on the reviewed
        tree; `change` replaces answer fields."""
        self.run_dir = self.root / name
        lifecycle_on = '--lifecycle-mode' in flags and flags[flags.index('--lifecycle-mode') + 1] == 'on'
        co = self.coordinator('--auto-split-rounds', 'false', '--shadow', 'off', '--adversarial-gate', 'on' if lifecycle_on else 'off',
                              '--max-exec-rounds', str(limit), '--max-plan-rounds', str(limit), *flags)
        if prepare:
            prepare(co)
        if phase == 'PLAN':
            (co.context / 'plan.md').write_text('Implement the helper.\n' + FAKE_SCENARIO_MATRIX)
        ids = [co.record_findings(source, phase, index + 1, [{'severity': severity, 'file': 'tracked.txt', 'summary': 'left open',
                                                              'body': 'b', 'failure_scenario': 'x'}])[0]['id']
               for index, (source, severity) in enumerate(rows)]
        snapshot, command, sequence = rc.git_snapshot(co.workspace)[0], co.args.test_command, co.state['sequence'] + 1
        answer = {'status': 'REVISE', 'full_review': [], 'verified_claims': [], 'reviewed_snapshot': snapshot,
                  'prior_findings': [{'id': i, 'disposition': 'still_open', 'evidence': 'not addressed'} for i in ids],
                  'self_run_evidence': [{'command': command}],
                  'observed_commands': [{'command': command, 'exit_code': test[0], 'error': bool(test[0]), 'output': test[1]}] if test else [],
                  **change}
        co.state.update({'phase': phase, f'{phase.lower()}_rounds': rounds, 'pending_reviewer_result_sequence': sequence, 'sequence': sequence})
        co.state['turns'].append({'sequence': sequence, 'role': 'reviewer', 'phase': phase, 'answer': answer,
                                  'snapshot_before': snapshot, 'open_finding_ids': ids})
        co.save()
        if not run:
            return co
        co.reviewer_turn()
        return co.state

    def test_plan_still_open_minor_at_limit_advances_with_advisory(self):
        # V317-PLAN permits this exit; test_v317_plan separately pins the blocking cap.
        for route in ('off', 'on'):
            with self.subTest(route=route):
                state = self.review('plan-' + route, [('persistent-reviewer', 'MINOR')], phase='PLAN',
                                    flags=('--lifecycle-mode', route))
                self.assertEqual((state['status'], state.get('hold_reason')), ('ACTIVE', None))
                self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
                self.assertTrue(all(row.get('advisory') and row['status'] == 'open' for row in state['finding_ledger']))
                self.assertEqual((state['phase'], state['next']), ('EXEC', 'author'))

    def test_review_only_gate_low_at_limit_reaches_done(self):
        for route in (OFF, ON):
            with self.subTest(route=route):
                (self.root / 'gate-once').unlink(missing_ok=True)
                self.run_dir = self.root / ('review-only-' + route[1])
                (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
                co, status = self.gate_in_process('--review-only', *route, '--max-exec-rounds', '2')
                self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
                self.assert_exit(self.state(), 'MINOR')
                self.assertEqual(self.state()['exec_rounds'], 2)
                (self.root / 'gate-once').unlink(missing_ok=True)

    def test_secret_still_present_with_low_at_limit_holds(self):
        (self.workspace / 'settings.txt').write_text('aws_key = ' + 'AKIA' + 'ABCDEFGHIJKLMNOP' + '\n')
        for route in ('off', 'on'):
            with self.subTest(route=route):
                state = self.review('secret-' + route, [('adversarial-gate', 'LOW')], flags=('--lifecycle-mode', route))
                self.assertEqual(state['status'], 'HOLD')
                self.assertTrue(state['hold_reason'].startswith('secret scan: the change still adds a hardcoded credential'))
                self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'REVISE')
                self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))

    def test_resume_reuses_the_exit_without_reviewer_or_shadow_dispatch(self):
        for route in ('off', 'on'):
            with self.subTest(route=route):
                co = self.review('replay-' + route, [('adversarial-gate', 'LOW')],
                                 flags=('--lifecycle-mode', route, '--shadow', 'on'), run=False)
                # A recorded clean shadow exists after the pending reviewer, as after a crash.
                sequence = co.state['sequence'] + 1
                co.state['sequence'] = sequence
                co.state['turns'].append({'sequence': sequence, 'role': 'shadow', 'phase': 'EXEC',
                                         'answer': {'status': 'APPROVE', 'full_review': [],
                                                    'verified_claims': [{'claim': 'checked', 'file': 'tracked.txt', 'line': 1}]},
                                         'snapshot_before': rc.git_snapshot(co.workspace)[0]})
                co.save()
                def crash_after_exit(*args):
                    co.save()
                    raise RuntimeError('crash after exit')
                with mock.patch.object(co, 'capture_review_baseline', side_effect=crash_after_exit):
                    with self.assertRaisesRegex(RuntimeError, 'crash after exit'):
                        co.reviewer_turn()
                saved = json.loads(co.state_path.read_text())
                self.assertEqual(saved['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
                resumed = rc.Coordinator(co.args)
                with mock.patch.object(resumed, 'invoke', side_effect=AssertionError('new dispatch')):
                    resumed.reviewer_turn()
                self.assertEqual(resumed.state['exec_rounds'], saved['exec_rounds'])
                self.assertEqual(resumed.state['sequence'], saved['sequence'])
                self.assertEqual(resumed.state['review_verdicts'], saved['review_verdicts'])
                self.assertEqual(resumed.state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')

    def test_security_blocker_and_gate_low_exit_exec_but_hold_security(self):
        # A sensitive delivery path is a SECURITY blocker, independently of the EXEC gate LOW.
        (self.workspace / 'deploy.pem').write_text('not a real key\n')
        self.git('add', '-f', 'deploy.pem')
        # Leave it staged: SP holds only for a path added or changed by this run.
        co, status = self.gate_in_process(*ON, '--max-exec-rounds', '2')
        self.assertEqual(status, 'HOLD')
        self.assertIn('SECURITY sensitive paths: deploy.pem', co.state['hold_reason'])
        state = self.state()
        self.assert_exit(state, 'MINOR')
        self.assertEqual((state['status'], state['lifecycle']['stage']), ('HOLD', 'SECURITY'))
        self.assertEqual(state['lifecycle']['receipts'][-1]['status'], 'HOLD')

    def test_second_writer_review_at_limit_keeps_the_remaining_write(self):
        def partial_rollback(co):
            base = rc.git_snapshot(co.workspace)[0]
            capture = co._writer_capture(self.root / 'writer-base')
            life = co.state['lifecycle']
            life['quality_writers'] = {'base_oid': base, 'base_capture': capture,
                                      'simplifier': {'state': 'wrote', 'files': ['simplifier.py'], 'sequence': 1},
                                      'test-writer': {'state': 'wrote', 'files': ['test_writer.py'], 'sequence': 2}}
            co.state['exec_rounds'] = 0
            co._writer_replay()
            (self.workspace / 'simplifier.py').write_text('# simplifier edit\n')
            (self.workspace / 'test_writer.py').write_text('# kept writer edit\n')
            co.record_findings('persistent-reviewer', 'EXEC', 3,
                               [{'severity': 'MAJOR', 'file': 'simplifier.py', 'summary': 'bad simplification',
                                 'body': 'b', 'failure_scenario': 'x'}])
            self.assertTrue(co._writer_review_rollback('reviewer', 3))
            self.assertTrue(co.state['lifecycle']['writer_replay']['partial'])
            self.assertFalse((self.workspace / 'simplifier.py').exists())
            self.assertEqual(co.state['exec_rounds'], 0)   # WI-GATE1 R5: a writer replay and rollback use no EXEC round
        co = self.review('writer-second', [('adversarial-gate', 'LOW')], limit=1,
                         flags=('--lifecycle-mode', 'on'), prepare=partial_rollback, run=False)
        co.reviewer_turn()   # replay requires the fake shadow even with --shadow off
        self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
        self.assertEqual(co.state['next'], 'gate')
        marker = co.state['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('rolled-back:review', 'wrote'))
        self.assertEqual((self.workspace / 'test_writer.py').read_text(), '# kept writer edit\n')
        self.assertEqual(co.state['exec_rounds'], 2)   # as the recorded review set it: the replay review adds none

    def assert_held(self, state, reason=LIMIT):
        self.assertEqual((state['status'], state['hold_reason']), ('HOLD', reason))
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'REVISE')
        self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))

    def test_only_non_blocking_rows_take_the_exit_and_a_blocking_row_holds(self):
        low, medium = ('adversarial-gate', 'low'), ('adversarial-gate', 'medium')
        state = self.review('control', [low, medium, ('persistent-reviewer', 'MINOR')])
        self.assertEqual((state['status'], state['review_verdicts'][-1]['effective_verdict']), ('DONE', 'APPROVE_WITH_ADVISORY'))
        self.assertEqual([(row['status'], row.get('advisory')) for row in state['finding_ledger']], [('open', True)] * 3)
        for name, blocker in (('reviewer-major', ('persistent-reviewer', 'MAJOR')), ('reviewer-critical', ('persistent-reviewer', 'CRITICAL')),
                              ('gate-high', ('adversarial-gate', 'high')), ('gate-critical', ('adversarial-gate', 'critical'))):
            with self.subTest(blocker=name):
                self.assert_held(self.review(name, [low, blocker]))

    def test_mixed_new_and_prior_advisories_are_marked_at_an_ordinary_exit(self):
        state = self.review('mixed-advisories', [('adversarial-gate', 'low')], full_review=[
            {'severity': 'MINOR', 'file': 'tracked.txt', 'summary': 'new wording nit', 'failure_scenario': 'odd comment'}])
        self.assertEqual((state['status'], state['review_verdicts'][-1]['effective_verdict'],
                          [(r['status'], r.get('advisory')) for r in state['finding_ledger']]),
                         ('DONE', 'APPROVE_WITH_ADVISORY', [('open', True), ('open', True)]))

    def test_a_failing_test_no_evidence_or_another_tree_at_the_limit_holds(self):
        rows = [('adversarial-gate', 'low')]
        state = self.review('control', rows)
        self.assertEqual((state['status'], state['review_verdicts'][-1]['effective_verdict']), ('DONE', 'APPROVE_WITH_ADVISORY'))
        for name, change, reason in (('test-failed', {'test': (1, 'FAILED (failures=1)')},
                                     rc.TEST_EVIDENCE_HOLD + '. The configured test command failed (exit status 1).'
                                     ' Test output — FAILED (failures=1)'),
                                     ('test-not-run', {'test': None}, rc.TEST_EVIDENCE_HOLD),   # WI-GATE1 S3 R2 F8: only
                                     ('no-evidence', {'self_run_evidence': []}, rc.TEST_EVIDENCE_HOLD),   # the evidence is missing
                                     ('another-tree', {'reviewed_snapshot': '0' * 40}, LIMIT)):
            with self.subTest(change=name):
                self.assert_held(self.review(name, rows, **change), reason)

    def test_a_round_left_also_exits_and_no_row_still_open_changes_nothing(self):
        state = self.review('round-left', [('adversarial-gate', 'low')], rounds=1)   # WI-GATE1 S3: exits in any round
        self.assertEqual((state['status'], state['review_verdicts'][-1]['effective_verdict']), ('DONE', 'APPROVE_WITH_ADVISORY'))
        self.assertTrue(all(row.get('advisory') for row in state['finding_ledger']))
        self.assert_held(self.review('nothing-open', []))   # a REVISE without a finding and without an open row: no exit


if __name__ == '__main__':
    unittest.main()
