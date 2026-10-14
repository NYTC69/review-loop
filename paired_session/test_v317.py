"""V317: eligible advisory REVISE keeps quality-writer output on EXEC replay."""
import os
import unittest
from unittest import mock

from paired_session import test_d09_c1b2 as d09

rc = d09.rc


class InterruptedReplay(Exception):
    pass


class WriterReplayAdvisoryTests(unittest.TestCase):
    locals().update({name: getattr(d09.D09C1b2Tests, name) for name in
                    (*d09.twl._HELPERS, 'setUp', 'git', 'state', 'change', 'stages', 'drive')})

    def run_replay(self, prior=False, mixed=False, change=None, interrupt=False):
        base = self.change()
        note = self.workspace / 'writer_note.py'
        env = {'FAKE_WRITER_WRITE': 'simplifier', 'FAKE_WRITER_FILE': note.name,
               'FAKE_EXEC_MINOR_REVISE_IF_FILE': str(note)}
        if prior:
            env.update(FAKE_EXEC_APPROVE_MINOR='1', FAKE_EXEC_STILL_OPEN='advisory naming',
                       FAKE_EXEC_STILL_OPEN_IF_FILE=str(note))
        args = rc.parser().parse_args(self.command(*d09.RUN)[2:])
        with mock.patch.dict(os.environ, env), mock.patch.object(rc.Coordinator, '_writer_local_check', return_value=True):
            co = rc.Coordinator(args)
            invoke = co.invoke

            def review(*a, **kw):
                result = invoke(*a, **kw)
                if a[0] == 'reviewer' and co.state['lifecycle'].get('writer_replay'):
                    if mixed:
                        result['answer']['full_review'] = [{'severity': 'LOW', 'file': note.name,
                                                           'summary': 'wording nit', 'failure_scenario': 'odd comment'}]
                    if change:
                        change(result['answer'])
                return result

            dispositions = co.apply_dispositions

            def pause(*a, **kw):
                if co.state['lifecycle'].get('writer_replay'):
                    raise InterruptedReplay('after the replay reviewer receipt was saved')
                return dispositions(*a, **kw)

            with mock.patch.object(co, 'invoke', side_effect=review):
                if interrupt:
                    with mock.patch.object(co, 'apply_dispositions', side_effect=pause):
                        with self.assertRaises(InterruptedReplay):
                            co.drive()
                    saved = self.state()
                    pending = saved['pending_reviewer_result_sequence']
                    self.assertIsNotNone(pending)
                    reviewer_count = len([t for t in saved['turns'] if t['role'] == 'reviewer' and t['phase'] == 'EXEC'])
                    co = rc.Coordinator(args)
                    self.assertEqual(co.resume(), 'DONE', co.state.get('hold_reason'))
                    self.assertEqual(len([t for t in co.state['turns'] if t['role'] == 'reviewer' and t['phase'] == 'EXEC']), reviewer_count)
                else:
                    self.assertEqual(co.drive(), 'DONE', co.state.get('hold_reason'))
        return co.state, base

    def assert_kept(self, state):
        writer = state['lifecycle']['quality_writers']['simplifier']
        self.assertEqual(writer['state'], 'wrote')
        self.assertTrue((self.workspace / 'writer_note.py').is_file())
        self.assertEqual(rc.git_snapshot(self.workspace)[0], writer['output_oid'])
        self.assertEqual((state['exec_rounds'], state['writer_replay_rounds']), (2, {'start': 1, 'used': 1}))
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
        self.assertEqual(state['review_verdicts'][-1]['reviewer_raw_verdict'], 'REVISE')
        rows = [r for r in state['finding_ledger'] if r['status'] == 'open']
        self.assertTrue(rows)
        self.assertTrue(all(r.get('advisory') for r in rows))
        self.assertNotIn('writer_replay', state['lifecycle'])
        replay = [(t['role'], t['phase']) for t in state['turns'] if t['sequence'] > writer['sequence']]
        for step in (('reviewer', 'EXEC'), ('shadow', 'EXEC'), ('gate', 'EXEC'), ('author', 'FINISH')):
            self.assertIn(step, replay)
        self.assertEqual([s[0] for s in self.stages(state)][-2:], ['DOCS', 'SECURITY'])
        return rows

    def test_new_advisory_revise_keeps_the_writer_change(self):
        state, _ = self.run_replay()
        self.assert_kept(state)

    def test_prior_only_advisory_revise_keeps_the_writer_change(self):
        state, _ = self.run_replay(prior=True)
        self.assert_kept(state)

    def test_new_and_prior_advisories_are_all_marked(self):
        state, _ = self.run_replay(prior=True, mixed=True)
        rows = self.assert_kept(state)
        self.assertEqual({r['summary'] for r in rows}, {'advisory naming', 'wording nit'})

    def test_advisory_second_review_keeps_the_writer_after_partial_rollback(self):
        self.change()
        kept = self.workspace / 'writer_note.py'
        reverted = self.workspace / 'tests' / 'test_extra.py'
        co, status = self.drive(FAKE_WRITER_WRITE='simplifier,test-writer',
                                FAKE_WRITER_FILE_SIMPLIFIER=kept.name,
                                FAKE_WRITER_FILE_TEST_WRITER='tests/test_extra.py',
                                FAKE_EXEC_REVISE_IF_FILE=str(reverted),
                                FAKE_EXEC_MINOR_REVISE_IF_FILE=str(kept))
        state = co.state
        marker = state['lifecycle']['quality_writers']
        self.assertEqual(status, 'DONE', state.get('hold_reason'))
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']),
                         ('wrote', 'rolled-back:review'))
        self.assertTrue(kept.is_file())
        self.assertFalse(reverted.exists())
        self.assertEqual((state['exec_rounds'], state['writer_replay_rounds'], state['config']['max_exec_rounds']),
                         (3, {'start': 1, 'used': 2}, 4))   # ordinary room remains on the second replay review
        self.assertEqual([(r['reviewer_raw_verdict'], r['effective_verdict']) for r in state['review_verdicts']
                          if r['phase'] == 'EXEC'],
                         [('APPROVE', 'APPROVE'), ('REVISE', 'REVISE'), ('REVISE', 'APPROVE_WITH_ADVISORY')])
        self.assertEqual([(r['severity'], r['status'], r.get('advisory', False)) for r in state['finding_ledger']],
                         [('MAJOR', 'withdrawn', False), ('MINOR', 'open', True)])

    def test_resume_after_recording_replay_review_keeps_the_writer_change(self):
        state, _ = self.run_replay(prior=True, interrupt=True)
        self.assert_kept(state)

    def test_stop_after_gate_still_accepts_eligible_advisories_without_writers(self):
        done = self.run_coordinator('--lifecycle-mode', 'off', '--max-exec-rounds', '1',
                                    env={'FAKE_EXEC_MINOR_REVISE': '1'})
        self.assertIn(d09.DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertNotIn('lifecycle', state)
        self.assertNotIn('writer_replay_rounds', state)
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
        self.assertTrue(all(r.get('advisory') for r in state['finding_ledger'] if r['status'] == 'open'))

    def assert_rollback(self, state, base):
        self.assertEqual(state['lifecycle']['quality_writers']['simplifier']['state'], 'rolled-back:review')
        self.assertFalse((self.workspace / 'writer_note.py').exists())
        self.assertEqual(rc.git_snapshot(self.workspace)[0], base)
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'REVISE')
        self.assertEqual((state['exec_rounds'], state['writer_replay_rounds']), (2, {'start': 1, 'used': 1}))
        self.assertTrue(all(r['status'] == 'withdrawn' for r in state['finding_ledger']))

    def test_major_replay_finding_rolls_back(self):
        def major(answer):
            answer['full_review'][0].update(severity='MAJOR', file='writer_note.py')
            # WR retains blockers outside writer files; this test pins rollback of a writer-file blocker.
        state, base = self.run_replay(change=major)
        self.assert_rollback(state, base)

    def test_failing_test_rolls_back_advisory_revise(self):
        def failing(answer):
            for row in answer['observed_commands']:
                if rc.command_invokes_test(row['command'], 'python3 -m unittest'):
                    row.update(exit_code=1, error=True, output='FAILED (failures=1)')
        state, base = self.run_replay(change=failing)
        self.assert_rollback(state, base)

    def test_unobserved_test_rolls_back(self):
        state, base = self.run_replay(change=lambda a: a.update(observed_commands=[]))
        self.assert_rollback(state, base)

    def test_missing_run_evidence_rolls_back(self):
        state, base = self.run_replay(change=lambda a: a.update(self_run_evidence=[]))
        self.assert_rollback(state, base)

    def test_another_reviewed_tree_rolls_back(self):
        state, base = self.run_replay(change=lambda a: a.update(reviewed_snapshot='0' * 40))
        self.assert_rollback(state, base)


if __name__ == '__main__':
    unittest.main()
