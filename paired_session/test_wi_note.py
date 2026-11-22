"""WI-NOTE: persistent EXEC ruling windows and queued note delivery."""
from contextlib import contextmanager
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc
from paired_session import test_worktree_lifecycle as twl

rc = trc.rc
RULING = 'REVISE_NEEDS_OPERATOR'
QUESTION = '[class: input-validation] Should bool be rejected explicitly?'
RULING_NOTE = 'NOTE: the reviewer also requests an operator ruling: ' + QUESTION
OFF = ('--lifecycle-mode', 'off', '--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')
WITH_GATE = ('--lifecycle-mode', 'off', '--shadow', 'off', '--adversarial-gate', 'on', '--polish-round', 'off')
ON = ('--lifecycle-mode', 'on', '--max-invocations', '60')


class OperatorNoteTests(unittest.TestCase):
    @contextmanager
    def harness(self, lifecycle=False):
        h = trc.RealCoordinatorTests()
        try:
            (twl.WorktreeLifecycleActivationTests.setUp if lifecycle else trc.RealCoordinatorTests.setUp)(h)
            yield h
        finally:
            h.tearDown()
            h.doCleanups()

    def state(self, h):
        return json.loads((h.run_dir / 'state.json').read_text())

    def prompt(self, h, turn):
        return (h.run_dir / 'evidence' /
                f"{turn['sequence']:03d}-{turn['phase'].lower()}-{turn['role']}.prompt.txt").read_text()

    def assert_result(self, result, code, line):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertIn(line, result.stdout, result.stdout + result.stderr)

    def test_ruling_question_falls_back_to_an_open_blocker(self):
        """Gate R2 (AC1): a ruling about an existing blocker names it in the HOLD and in the cap/stop-rule NOTE."""
        prefix = 'NOTE: the reviewer also requests an operator ruling'
        open_row = {'id': 'F003', 'summary': QUESTION}
        stub = type('Stub', (), {'blocking_open_findings': lambda self: [open_row, {'summary': 'later'}]})()
        self.assertEqual(rc.Coordinator._ruling_question(stub, prefix, []), RULING_NOTE)
        self.assertEqual(rc.Coordinator._ruling_question(stub, prefix, [{'summary': 'new question'}]),
                         prefix + ': new question')
        empty = type('Stub', (), {'blocking_open_findings': lambda self: []})()
        self.assertEqual(rc.Coordinator._ruling_question(empty, prefix, []), prefix + ': see the review receipt')

    def test_reviewer_ruling_window(self):
        with self.harness() as h:
            latest = 'Reject bool explicitly before the integer check. The previous review raised this question.'
            self.assertIsNotNone(rc.FRESH_HISTORY_RE.search(latest))
            env = {'FAKE_EXEC_RULING_ONCE': str(h.root / 'ruling'), 'FAKE_GATE_SCOPE': latest}
            held = h.run_coordinator(*WITH_GATE, env=env)
            self.assert_result(held, 2, 'HOLD: reviewer requests an operator ruling: ' + QUESTION)
            self.assertIn('note --text or note --file, then resume', held.stdout)
            state = self.state(h)
            review = state['turns'][-1]
            self.assertEqual((review['role'], review['answer']['status']), ('reviewer', RULING))
            self.assertIn(RULING, self.prompt(h, review))
            self.assertEqual((state['next'], state['pending_reviewer_result_sequence']), ('author', None))
            verdict = state['review_verdicts'][-1]
            self.assertEqual((verdict['reviewer_raw_verdict'], verdict['effective_verdict']), (RULING, 'REVISE'))
            self.assertEqual(state['exec_comparisons'][-1]['persistent']['verdict'], RULING)
            self.assert_result(h.run_operator_action('note', *WITH_GATE, '--text', 'Original ruling.'), 0, 'N001 queued')
            self.assert_result(h.run_operator_action('note', *WITH_GATE, '--text', latest), 0, 'N002 queued')
            status = h.run_operator_action('status', *WITH_GATE, '--brief', '1')
            self.assert_result(status, 0, 'Pending operator note N002')
            self.assert_result(h.run_operator_action('resume', *WITH_GATE, env=env), 0, twl.DONE)
            after = self.state(h)
            first = after['turns'][len(state['turns'])]
            self.assertEqual((first['role'], first['phase'], first['answer']['status'], first['operator_note_id']),
                             ('author', 'EXEC', 'READY', 'N002'))
            prompt = self.prompt(h, first)
            self.assertIn(QUESTION, prompt)
            self.assertIn(latest, prompt)
            self.assertNotIn('Original ruling.', prompt)
            self.assertIn('## Operator in-scope clarification [N002]', prompt)
            review = after['turns'][len(state['turns']) + 1]
            self.assertEqual((review['role'], review['phase']), ('reviewer', 'EXEC'))
            gate = next(turn for turn in after['turns'] if turn['role'] == 'gate')
            self.assertEqual(gate['answer']['verdict'], 'PASS')
            for turn, decision in ((review, 'N002: ' + latest), (gate, 'operator decision: ' + latest)):
                prompt = self.prompt(h, turn)
                self.assertIn(decision, prompt.split('## Operator decisions\n', 1)[1])
                self.assertNotIn('Original ruling.', prompt)
            self.assertIn('Do not request an operator ruling again on a decided point.', self.prompt(h, review))
            self.assertNotIn('N002', self.prompt(h, gate))
            self.assertNotIn('pending_operator_note_id', after)
            self.assertEqual([n['status'] for n in after['operator_notes']], ['replaced', 'delivered'])
            self.assertNotIn('Pending operator note', h.run_operator_action('status', *WITH_GATE).stderr)
            self.assertNotIn('Pending operator note', h.run_operator_action('status', *WITH_GATE, '--brief').stdout)
        with self.harness() as h:
            self.assert_result(h.run_coordinator(*OFF, '--exercise-revisions'), 0, twl.DONE)
            exec_turns = [row for row in self.state(h)['turns'] if row['phase'] == 'EXEC']
            self.assertEqual([(t['role'], t['answer']['status']) for t in exec_turns[:3]],
                             [('author', 'READY'), ('reviewer', 'REVISE'), ('author', 'READY')])

        with self.subTest(kind='advisory'), self.harness() as h:
            env = {'FAKE_EXEC_RULING_ONCE': str(h.root / 'ruling'),
                   'FAKE_FINDING_FIELDS': json.dumps({QUESTION: {'category': 'other', 'reachability': ''}})}
            self.assert_result(h.run_coordinator(*OFF, env=env), 0, twl.DONE)
            state = self.state(h)
            self.assertEqual(state['status'], 'DONE')
            verdict = state['review_verdicts'][-1]
            self.assertEqual((verdict['reviewer_raw_verdict'], verdict['effective_verdict']),
                             (RULING, 'APPROVE_WITH_ADVISORY'))
            advisory = next(row for row in state['finding_ledger'] if row.get('advisory'))
            self.assertEqual((advisory['category'], advisory['summary']), ('other', QUESTION))
            self.assertTrue(any(row['id'] == advisory['id'] and row['summary'] == QUESTION
                                for row in state['backlog']))

        for kind in ('cap', 'cap extension', 'stop rule', 'structural'):
            with self.subTest(kind=kind), self.harness() as h:
                flags = OFF if kind == 'structural' else (*OFF, '--max-exec-rounds', '1')
                self.assert_result(h.run_coordinator(*flags, '--stop-after-plan'), 2, 'HOLD: ' + rc.PLAN_STOP_REASON)
                state = self.state(h)
                if kind == 'stop rule':
                    state['stop_rules'] = {'sequence': 0, 'still_open': [], 'new_blocking_streak': 1, 'approved': True}
                if kind == 'structural':
                    state['block_class_events'] = [
                        {'sequence': n, 'source': 'reviewer', 'block': True,
                         'classes': {'input-validation': [f'F{n:03d}']}} for n in (1, 2)]
                rc.atomic_json(h.run_dir / 'state.json', state)
                env = {'FAKE_EXEC_RULING_ONCE': str(h.root / 'ruling')}
                held = h.run_operator_action('resume', *flags, env=env)
                self.assert_result(held, 2, RULING_NOTE)
                self.assertEqual(held.stdout.splitlines().count(RULING_NOTE), 1)
                state = self.state(h)
                reason = state['hold_reason']
                self.assertNotIn(QUESTION, reason)
                if kind == 'structural':
                    self.assertTrue(reason.startswith('structural fix / re-scope needed: finding class input-validation'))
                    self.assertEqual(state['next'], 'author')
                    self.assert_result(h.run_operator_action('resume', *flags, env=env), 0, twl.DONE)
                else:
                    self.assertEqual(state['round_limit_hold']['hold_reason'], reason)
                    if kind in ('cap', 'cap extension'):
                        self.assertEqual(reason, 'EXEC round limit reached')
                        if kind == 'cap':
                            self.assert_result(h.run_operator_action('resume', *flags, env=env), 0, twl.DONE)
                            first = self.state(h)['turns'][len(state['turns'])]
                            self.assertEqual((first['role'], first['snapshot_before']),
                                             ('reviewer', state['round_limit_hold']['tree_sha256']))
                            continue
                    else:
                        self.assertTrue(reason.startswith(rc.NEW_BLOCKERS_REASON + ':'))
                        self.assert_result(h.run_operator_action('resume', *flags, env=env), 2, 'HOLD: ' + reason)
                        self.assertEqual(self.state(h)['turns'], state['turns'])
                    self.assert_result(h.run_operator_action('resume', *flags, '--add-rounds', '1', env=env), 0, twl.DONE)
                self.assertEqual(self.state(h)['turns'][len(state['turns'])]['role'], 'author')

    def test_delivered_ruling_reaches_lifecycle_writers(self):
        with self.harness(lifecycle=True) as h:
            (h.workspace / 'test_feature.py').write_text(
                'import unittest\n\nclass Smoke(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n')
            flags = (*ON, '--quality-writers', 'tests')
            env = {'FAKE_EXEC_RULING_ONCE': str(h.root / 'ruling'), 'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'}
            decision = 'Reject bool explicitly before the integer check.'
            self.assert_result(h.run_coordinator(*flags, env=env), 2, 'HOLD: reviewer requests an operator ruling: ' + QUESTION)
            self.assert_result(h.run_operator_action('note', *flags, '--text', decision), 0, 'N001 queued')
            self.assert_result(h.run_operator_action('resume', *flags, env=env), 0, twl.DONE)
            state = self.state(h)
            self.assertEqual(state['operator_notes'][0]['status'], 'delivered')
            phases = ['FINISH', 'POLISH-Q', 'DOCS']
            writers = [turn for turn in state['turns'] if turn['role'] == 'author' and turn['phase'] in phases]
            self.assertEqual([turn['phase'] for turn in writers], phases)
            for turn in writers:
                with self.subTest(phase=turn['phase']):
                    self.assertIn('## Operator decisions\n'
                                  'Implement and document these in-scope decisions as decided; do not reopen them.\n'
                                  '- ' + decision, self.prompt(h, turn))

    def test_ruling_without_new_blocker_names_open_gate_row(self):
        with self.harness() as h:
            self.assert_result(h.run_coordinator(*WITH_GATE, '--stop-after-plan'), 2, 'HOLD: ' + rc.PLAN_STOP_REASON)
            argv = h.command(*WITH_GATE)[2:]
            argv[0] = 'resume'
            argv.append('--skip-probe')
            co = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))
            invoke = co.invoke

            def rule_after_gate(role, phase, *args, **kwargs):
                if role == 'reviewer' and co.state.get('gate_ran'):
                    with patch.dict(os.environ, {'FAKE_EXEC_RULING_ONCE': str(h.root / 'gate-ruling')}):
                        return invoke(role, phase, *args, **kwargs)
                return invoke(role, phase, *args, **kwargs)

            env = {'FAKE_GATE_BLOCK': '1', 'FAKE_EXEC_STILL_OPEN': '[gate FAIL]'}
            with patch.dict(os.environ, env), patch.object(co, 'invoke', side_effect=rule_after_gate):
                self.assertEqual(co.resume(), 'HOLD')
            state = self.state(h)
            self.assertEqual(state['turns'][-1]['answer']['status'], RULING)
            [row] = [row for row in state['finding_ledger'] if row['source'] == 'adversarial-gate']
            self.assertEqual(row['status'], 'open')
            self.assertTrue(state['hold_reason'].startswith('reviewer requests an operator ruling: ' + row['summary']))

    def test_recorded_ruling_receipt_parks_on_resume(self):
        class SimulatedCrash(BaseException):
            pass

        with self.harness() as h:
            self.assert_result(h.run_coordinator(*OFF, '--stop-after-plan'), 2, 'HOLD: ' + rc.PLAN_STOP_REASON)
            argv = h.command(*OFF)[2:]
            argv[0] = 'resume'
            argv.append('--skip-probe')
            args = rc.configure_parser(rc.parser(), argv).parse_args(argv)
            co = rc.Coordinator(args)
            with patch.dict(os.environ, {'FAKE_EXEC_RULING_ONCE': str(h.root / 'ruling')}), \
                    patch.object(co, 'hold', side_effect=SimulatedCrash) as hold, self.assertRaises(SimulatedCrash):
                co.resume()
            self.assertIn(QUESTION, hold.call_args.args[0])
            interrupted = self.state(h)
            review = interrupted['turns'][-1]
            self.assertEqual(interrupted['pending_reviewer_result_sequence'], review['sequence'])
            self.assertEqual(review['answer']['status'], RULING)
            self.assert_result(h.run_operator_action('resume', *OFF), 2,
                               'HOLD: reviewer requests an operator ruling: ' + QUESTION)
            recovered = self.state(h)
            self.assertEqual(recovered['turns'], interrupted['turns'])
            self.assertEqual((recovered['next'], recovered['pending_reviewer_result_sequence']), ('author', None))
            self.assertIn(QUESTION, recovered['delivered_review'])

    def done_with_note(self, h):
        env = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_REVIEW_NO_TEST_ONCE': str(h.root / 'no-test')}
        held = h.run_coordinator(*ON, env=env)
        self.assert_result(held, 2, 'HOLD: DOCS reviewer did not observe a successful run')
        note_text = 'Mention the migration in the changelog.'
        self.assert_result(h.run_operator_action('note', *ON, '--text', note_text), 0, 'N001 queued')
        self.assert_result(h.run_operator_action('resume', *ON, env=env), 0, twl.DONE)
        state = self.state(h)
        self.assertEqual(state['pending_operator_note_id'], 'N001')
        self.assertEqual(sum(t['role'] == 'author' and t['phase'] == 'DOCS' for t in state['turns']), 1)
        self.assertFalse(any(t.get('operator_note_id') for t in state['turns']))
        return state, note_text, env

    def test_done_note_status_after_reused_docs_writer(self):
        with self.harness(lifecycle=True) as h:
            done, note_text, env = self.done_with_note(h)
            [note] = done['operator_notes']
            state_path = h.run_dir / 'state.json'
            saved = state_path.read_bytes()
            status_cmd = h.command(*ON)
            status_cmd[2] = 'status'
            plain = subprocess.run(status_cmd, cwd=h.root, capture_output=True, check=True)
            self.assertEqual(plain.stdout, saved + b'\n')
            (h.run_dir / 'progress.jsonl').write_text('')
            with rc.run_lease(h.run_dir):
                brief = h.run_operator_action('status', *ON, '--brief', '1')
            self.assert_result(brief, 0, 'no progress events yet')
            for summary in (plain.stderr.decode(), brief.stdout):
                for expected in ('Pending operator note N001', note['deliver_to'], note['timestamp'],
                                 'accept and plain resume leave N001 undelivered',
                                 'sends N001 together with the rejection to the next EXEC author',
                                 'reject --intent-only --text "<rejection feedback>"',
                                 'reject --expect <digest> --text "<rejection feedback>"'):
                    self.assertIn(expected, summary)
            self.assertEqual(state_path.read_bytes(), saved)
            del note['timestamp'], note['deliver_to']
            rc.atomic_json(state_path, done)
            self.assertIn('recipient unknown; created unknown', h.run_operator_action('status', *ON).stderr)
            self.assert_result(h.run_operator_action('status', *ON, '--brief'), 0, 'recipient unknown; created unknown')
            state_path.write_bytes(saved)
            done = self.state(h)

            self.assert_result(h.run_operator_action('resume', *ON), 0, twl.DONE)
            self.assertEqual(self.state(h)['turns'], done['turns'])
            self.assertEqual(self.state(h)['pending_operator_note_id'], 'N001')
            feedback = 'Also reject floating point inputs.'
            issued = h.issue_operator_intent('reject', *ON, '--text', feedback)
            self.assertEqual(issued.returncode, 0, issued.stdout + issued.stderr)
            digest = json.loads(issued.stdout)['digest']
            self.assertEqual(self.state(h)['turns'], done['turns'])
            reject = h.command(*ON, '--text', feedback, '--expect', digest, '--skip-probe')
            reject[2] = 'reject'
            reopened = subprocess.run(reject, cwd=h.root, env={**os.environ, **env}, text=True, capture_output=True)
            self.assert_result(reopened, 0, twl.DONE)
            state = self.state(h)
            author = state['turns'][len(done['turns'])]
            self.assertEqual((author['phase'], author['role'], author['answer']['status']), ('EXEC', 'author', 'READY'))
            self.assertEqual((author['rejection_id'], author['operator_note_id']), ('R001', 'N001'))
            self.assertIn(feedback, self.prompt(h, author))
            self.assertIn(note_text, self.prompt(h, author))
            self.assertNotIn('pending_operator_note_id', state)
            self.assertEqual(state['operator_notes'][0]['status'], 'delivered')

        with self.harness(lifecycle=True) as h:
            done, _, _ = self.done_with_note(h)
            self.assert_result(h.run_operator_action('accept', *ON), 0, 'ACCEPTED')
            accepted = self.state(h)
            self.assertEqual(accepted['turns'], done['turns'])
            self.assertEqual(accepted['pending_operator_note_id'], 'N001')
            self.assertEqual(accepted['operator_notes'], done['operator_notes'])


if __name__ == '__main__':
    unittest.main()
