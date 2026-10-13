"""V316 (owner 2026-10-13; four field runs held with `EXEC round limit reached`): at the EXEC round limit, a REVISE
without any finding that only reports earlier non-blocking rows `still_open` (gate MEDIUM/LOW rows after a repair round)
ends as APPROVE_WITH_ADVISORY instead of HOLD. A round that is still available, a blocking row left open, a failing
configured test, no self-run evidence and a changed tree keep their behavior."""
import json
import unittest

from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE
LIMIT = 'EXEC round limit reached'
KEEP = 'simplify the helper'   # the fake gate's MEDIUM/LOW row; its HIGH row is 'reject bool'
OFF = ('--lifecycle-mode', 'off', '--shadow', 'off')   # the stop-after-gate route
ON = ('--lifecycle-mode', 'on', '--max-invocations', '60')


class RoundLimitStillOpenTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def gate_shaped(self, *flags, keep=KEEP):
        """The first gate blocks with a HIGH and a LOW row; every re-review after it is a REVISE without a finding that
        reports the rows named `keep` still_open and the others fixed."""
        return self.run_coordinator(*flags, env={'FAKE_GATE_BLOCK_ONCE': str(self.root / 'gate-once'),
                                                 'FAKE_GATE_BLOCK_WITH': 'low', 'FAKE_EXEC_STILL_OPEN': keep})

    def exec_verdicts(self, state):
        return [(row['reviewer_raw_verdict'], row['effective_verdict']) for row in state['review_verdicts'] if row['phase'] == 'EXEC']

    def assert_exit(self, state, severity):
        """The last EXEC review was a REVISE without a finding, taken as APPROVE_WITH_ADVISORY; the gate row it left open
        is still open and marked advisory."""
        last = [row for row in state['review_verdicts'] if row['phase'] == 'EXEC'][-1]
        self.assertEqual((last['reviewer_raw_verdict'], last['effective_verdict']), ('REVISE', 'APPROVE_WITH_ADVISORY'))
        answer = next(turn for turn in state['turns'] if turn['sequence'] == last['sequence'])['answer']
        self.assertEqual(answer['full_review'], [])
        [kept] = [row['id'] for row in answer['prior_findings'] if row['disposition'] == 'still_open']
        [row] = [row for row in state['finding_ledger'] if row.get('advisory')]
        self.assertEqual((row['id'], row['source'], row['severity'], row['status']), (kept, 'adversarial-gate', severity, 'open'))
        self.assertNotIn('round_limit_hold', state)
        return row

    def test_a_gate_low_left_open_at_the_last_round_ends_approved_with_advisory(self):   # the full-lifecycle route
        done = self.gate_shaped(*ON, '--max-exec-rounds', '2')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        row = self.assert_exit(state, 'LOW')
        self.assertEqual(state['exec_rounds'], 2)   # the repair round was the last one
        self.assertEqual([r['stage'] for r in state['lifecycle']['receipts']], ['FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])
        [high] = [r for r in state['finding_ledger'] if r['severity'] == 'HIGH']
        self.assertEqual(high['status'], 'fixed')
        # the existing rendering names the row: the ledger, the open-findings file, the comparison and the progress line
        for name in ('findings-ledger.md', 'open-findings.md'):
            [line] = [line for line in (self.run_dir / name).read_text().splitlines() if line.startswith(f"| {row['id']} ")]
            self.assertIn('| open (advisory) |', line)
        self.assertIn('| REVISE | APPROVE_WITH_ADVISORY |', (self.run_dir / 'review-comparison.md').read_text())
        self.assertIn(f"verdict · APPROVE_WITH_ADVISORY · raw REVISE · (1 open: {row['id']} LOW)", done.stdout)

    def test_the_exit_on_the_stop_after_gate_route(self):
        done = self.gate_shaped(*OFF, '--max-exec-rounds', '2')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assert_exit(state, 'LOW')
        self.assertEqual((state['exec_rounds'], 'lifecycle' in state), (2, False))
        self.assertEqual(self.exec_verdicts(state)[0], ('APPROVE', 'APPROVE'))   # the review before the gate blocked

    def test_with_a_round_left_the_author_is_dispatched_again(self):
        done = self.gate_shaped(*OFF, '--max-exec-rounds', '3')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertEqual(self.exec_verdicts(state), [('APPROVE', 'APPROVE'), ('REVISE', 'REVISE'), ('REVISE', 'APPROVE_WITH_ADVISORY')])
        authors = [turn['sequence'] for turn in state['turns'] if turn['role'] == 'author' and turn['phase'] == 'EXEC']
        self.assertEqual((len(authors), state['exec_rounds']), (3, 3))   # round 2 of 3 went to the author, as before
        row = self.assert_exit(state, 'LOW')
        prompt = (self.run_dir / 'evidence' / f'{authors[2]:03d}-exec-author.prompt.txt').read_text()
        self.assertIn(json.dumps({'id': row['id'], 'fixed': False, 'evidence': 'fake: not addressed'})[1:-1], prompt)

    def test_a_gate_high_still_open_at_the_last_round_holds(self):
        held = self.gate_shaped(*OFF, '--max-exec-rounds', '2', keep='reject bool')
        self.assertEqual(held.stdout.strip().splitlines()[-1], 'HOLD: ' + LIMIT, held.stdout + held.stderr)
        state = self.state()
        self.assertEqual(self.exec_verdicts(state)[-1], ('REVISE', 'REVISE'))
        self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))

    def test_an_operator_send_back_round_that_leaves_a_gate_medium_open_takes_the_exit(self):
        env = {'FAKE_GATE_MINOR': '1', 'FAKE_EXEC_STILL_OPEN': KEEP}   # every gate run leaves one MEDIUM row, never a blocker
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
        self.assertEqual(self.assert_exit(state, 'MEDIUM')['id'], medium['id'])
        later = [(turn['role'], turn['phase']) for turn in state['turns'] if turn['sequence'] > first['sequence']]
        self.assertEqual(later, [('author', 'EXEC'), ('reviewer', 'EXEC'), ('gate', 'EXEC')])   # the gate after a send-back

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

    def review(self, name, rows, rounds=2, limit=2, test=(0, 'Ran 1 test successfully'), **change):
        """One recorded EXEC re-review at round `rounds` of `limit`: REVISE, no finding, every row of `rows` (source,
        severity) still_open, the configured test observed as `test` (exit code, output; None: not run) on the reviewed
        tree; `change` replaces answer fields."""
        self.run_dir = self.root / name
        co = self.coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--max-exec-rounds', str(limit))
        ids = [co.record_findings(source, 'EXEC', index + 1, [{'severity': severity, 'file': 'tracked.txt', 'summary': 'left open',
                                                              'body': 'b', 'failure_scenario': 'x'}])[0]['id']
               for index, (source, severity) in enumerate(rows)]
        snapshot, command, sequence = rc.git_snapshot(co.workspace)[0], co.args.test_command, co.state['sequence'] + 1
        answer = {'status': 'REVISE', 'full_review': [], 'verified_claims': [], 'reviewed_snapshot': snapshot,
                  'prior_findings': [{'id': i, 'disposition': 'still_open', 'evidence': 'not addressed'} for i in ids],
                  'self_run_evidence': [{'command': command}],
                  'observed_commands': [{'command': command, 'exit_code': test[0], 'error': bool(test[0]), 'output': test[1]}] if test else [],
                  **change}
        co.state.update({'phase': 'EXEC', 'exec_rounds': rounds, 'pending_reviewer_result_sequence': sequence, 'sequence': sequence})
        co.state['turns'].append({'sequence': sequence, 'role': 'reviewer', 'phase': 'EXEC', 'answer': answer,
                                  'snapshot_before': snapshot, 'open_finding_ids': ids})
        co.save()
        co.reviewer_turn()
        return co.state

    def assert_held(self, state):
        self.assertEqual((state['status'], state['hold_reason']), ('HOLD', LIMIT))
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

    def test_a_failing_test_no_evidence_or_another_tree_at_the_limit_holds(self):
        rows = [('adversarial-gate', 'low')]
        state = self.review('control', rows)
        self.assertEqual((state['status'], state['review_verdicts'][-1]['effective_verdict']), ('DONE', 'APPROVE_WITH_ADVISORY'))
        for name, change in (('test-failed', {'test': (1, 'FAILED (failures=1)')}), ('test-not-run', {'test': None}),
                             ('no-evidence', {'self_run_evidence': []}), ('another-tree', {'reviewed_snapshot': '0' * 40})):
            with self.subTest(change=name):
                self.assert_held(self.review(name, rows, **change))

    def test_a_round_left_or_no_row_still_open_changes_nothing(self):
        state = self.review('round-left', [('adversarial-gate', 'low')], rounds=1)
        self.assertEqual((state['status'], state['next'], state['review_verdicts'][-1]['effective_verdict']), ('ACTIVE', 'author', 'REVISE'))
        self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))
        self.assert_held(self.review('nothing-open', []))   # a REVISE without a finding and without an open row: no exit


if __name__ == '__main__':
    unittest.main()
