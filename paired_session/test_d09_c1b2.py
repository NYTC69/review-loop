"""D09 C1-b2 (paired_session/docs/d09-cap1-writer-passes.md §2 "Reviewer after a writer" and "One chance in review", §5):
a kept writer change replays EXEC review, shadow, gate, FINISH and the specialists before DOCS (exec_rounds + 1, like a
FINISH write); the replay's first non-APPROVE review or a failing gate rolls the tree back to base_oid instead of a fix
round. A kept change needs a passing local check, whose executor is C1-b3: until then `_writer_local_check` returns None
(the interim rollback), so these tests drive the run in process with the check patched."""
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE
CODE = ''.join(f'VALUE_{n} = {n}\n' for n in range(30))
RUN = ('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '80')


class D09C1b2Tests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'git')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def change(self, small=False):   # a changed test file, and 30 new code lines unless small (the simplifier then skips)
        (self.workspace / 'new_code.py').write_text('A = 1\n' if small else CODE)
        (self.workspace / 'tests').mkdir()
        (self.workspace / 'tests' / 'test_new_code.py').write_text('import new_code\n')
        return rc.git_snapshot(self.workspace)[0]

    def drive(self, check=True, extra=(), **env):   # in process, so the C1-b3 local check can be stood in for
        with mock.patch.dict(os.environ, env), mock.patch.object(rc.Coordinator, '_writer_local_check', return_value=check):
            co = rc.Coordinator(rc.parser().parse_args(self.command(*RUN, *extra)[2:]))
            return co, co.drive()

    def stages(self, state):
        return [(row['stage'], row.get('role')) for row in state['lifecycle']['receipts']]

    # --- §5 tests 1, 2 and 12: a kept write is reviewed on the replay, then DOCS ---------------------------------------------
    def test_a_kept_write_replays_review_gate_finish_and_specialists_before_docs(self):
        base = self.change()
        co, status = self.drive(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py')
        self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
        state, note = self.state(), self.workspace / 'writer_note.py'
        marker = state['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('wrote', 'no-op'))
        written = marker['simplifier']['output_oid']
        self.assertNotEqual(written, base)
        self.assertEqual((marker['test-writer']['input_oid'], rc.git_snapshot(self.workspace)[0]), (written, written))
        self.assertTrue(note.is_file())   # the kept write is delivered with the change
        self.assertEqual(self.stages(state), [('FINISH', 'finisher'), ('POLISH-Q', 'specialists'), ('POLISH-Q', 'simplifier'),
                                              ('POLISH-Q', 'test-writer'), ('FINISH', 'finisher'), ('POLISH-Q', 'specialists'),
                                              ('DOCS', 'docs-writer'), ('SECURITY', 'security')])
        self.assertEqual((state['lifecycle']['epoch'], state['exec_rounds']), (1, 2))   # one replay epoch, counted like FINISH
        replay = [t for t in state['turns'] if t['sequence'] > marker['test-writer']['sequence']]
        roles = [(t['role'], t['phase']) for t in replay]
        for step in (('reviewer', 'EXEC'), ('shadow', 'EXEC'), ('gate', 'EXEC'), ('author', 'FINISH')):
            self.assertIn(step, roles)
        self.assertNotIn(('author', 'POLISH-Q'), roles)   # the writers do not run again in the replay
        self.assertEqual(state['lifecycle']['writer_counts'], {'simplifier': 1, 'test-writer': 1})
        exec_review = next(t for t in replay if t['role'] == 'reviewer' and t['phase'] == 'EXEC')
        self.assertEqual(exec_review['snapshot_before'], written)   # the reviewer reviews the writer's tree
        self.assertNotIn('writer_replay', state['lifecycle'])

    def test_a_writer_replay_runs_the_shadow_even_with_shadow_off(self):   # §2: reviewer, shadow and gate on the replay
        self.change()
        co, status = self.drive(extra=('--shadow', 'off'), FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py')
        self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
        state = self.state()
        writer = state['lifecycle']['quality_writers']['simplifier']
        shadows = [t['sequence'] > writer['sequence'] for t in state['turns'] if t['role'] == 'shadow']
        self.assertEqual(shadows, [True])   # none before the writers (shadow off), one on the replay
        self.assertEqual(writer['state'], 'wrote')

    def test_a_failing_local_check_rolls_the_write_back_without_a_replay(self):
        base = self.change()
        co, status = self.drive(check=False, FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py')
        self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
        state = self.state()
        self.assertEqual(state['lifecycle']['quality_writers']['simplifier']['state'], 'rolled-back:tests')
        self.assertEqual((rc.git_snapshot(self.workspace)[0], state['lifecycle']['epoch'], state['exec_rounds']), (base, 0, 1))

    # --- §5 test 7: one chance in review -------------------------------------------------------------------------------------
    def rolled_back(self, co, status, base, source, writers):
        self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
        state = self.state()
        marker = state['lifecycle']['quality_writers']
        for writer in writers:
            self.assertEqual((marker[writer]['state'], marker[writer]['review'], marker[writer]['output_oid']),
                             ('rolled-back:review', source, base))
        self.assertEqual(rc.git_snapshot(self.workspace)[0], base)   # restored from base_capture
        raised = [row for row in state['finding_ledger'] if row['origin_round'] > max(marker[w]['sequence'] for w in writers)
                  and row['phase'] == 'EXEC']
        self.assertTrue(raised)
        self.assertTrue(all(row['status'] == 'withdrawn' and 'rolled-back:review' in row['status_history'][-1]['evidence']
                            for row in raised))
        [rollback] = [row for row in state['lifecycle']['receipts'] if row.get('role') == 'writer-rollback']
        self.assertEqual((rollback['stage'], rollback['epoch'], rollback['candidate_oid'], rollback['output_oid']),
                         ('POLISH-Q', 1, base, base))
        convergence = state['lifecycle']['exec_convergence']   # the previous epoch's, on the same digest
        self.assertEqual(convergence['tree'], base)
        self.assertEqual(rollback['cites'], {'exec_reviewer': convergence['reviewer_sequence'], 'gate': convergence['gate_sequence'],
                                             'receipts': ['w-FINISH-0-0', 'w-POLISH-Q-0-0']})   # FINISH, clean specialists
        after = [(t['role'], t['phase']) for t in state['turns'] if t['sequence'] > max(marker[w]['sequence'] for w in writers)]
        self.assertNotIn(('author', 'EXEC'), after)   # no fix round
        self.assertEqual([row[0] for row in self.stages(state)][-2:], ['DOCS', 'SECURITY'])
        self.assertEqual(state['exec_rounds'], 2)
        self.assertTrue(Path(marker[writers[0]]['review_diff']).read_text())
        return state

    def test_the_replay_reviewer_asking_for_changes_rolls_back_to_base(self):
        base = self.change()
        co, status = self.drive(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py',
                                FAKE_EXEC_REVISE_IF_FILE=str(self.workspace / 'writer_note.py'))
        self.rolled_back(co, status, base, 'reviewer', ['simplifier'])
        self.assertFalse((self.workspace / 'writer_note.py').exists())

    def test_a_replay_reviewer_hold_rolls_back_to_base(self):   # HOLD is not APPROVE either
        base = self.change()
        co, status = self.drive(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py',
                                FAKE_EXEC_HOLD_IF_FILE=str(self.workspace / 'writer_note.py'))
        self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
        state = self.state()
        self.assertEqual((state['lifecycle']['quality_writers']['simplifier']['state'], rc.git_snapshot(self.workspace)[0]),
                         ('rolled-back:review', base))
        self.assertEqual([row[0] for row in self.stages(state)][-3:], ['POLISH-Q', 'DOCS', 'SECURITY'])

    def test_both_kept_writes_share_one_replay(self):   # §5 test 2
        base = self.change()
        co, status = self.drive(FAKE_WRITER_WRITE='simplifier,test-writer', FAKE_WRITER_FILE='writer_note.py')
        self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
        state = self.state()
        marker = state['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('wrote', 'wrote'))
        self.assertEqual(marker['test-writer']['input_oid'], marker['simplifier']['output_oid'])
        self.assertNotIn(base, (marker['simplifier']['output_oid'], marker['test-writer']['output_oid']))
        self.assertEqual((state['lifecycle']['epoch'], state['exec_rounds']), (1, 2))   # one replay epoch for both
        self.assertEqual((self.workspace / 'writer_note.py').read_text(), '# simplifier edit\n# test-writer edit\n')

    def test_a_review_only_run_with_a_fix_round_and_a_finish_write_counts_every_round(self):   # §5 test 12
        self.change()
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        co, status = self.drive(extra=('--exercise-revisions', '--max-exec-rounds', '6', '--max-invocations', '120'),
                                FAKE_FINISH_WRITE='1', FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py')
        self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
        state = self.state()
        # the existing change (1), the exercise fix round (2), the FINISH write (3), the writer replay (4)
        self.assertEqual((state['exec_rounds'], state['lifecycle']['epoch']), (4, 2))
        self.assertEqual(state['lifecycle']['quality_writers']['simplifier']['state'], 'wrote')
        self.assertEqual([row[0] for row in self.stages(state)],
                         ['FINISH', 'FINISH', 'POLISH-Q', 'POLISH-Q', 'POLISH-Q', 'FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])
        self.assertTrue((self.workspace / 'writer_note.py').is_file())

    def test_a_failing_replay_gate_rolls_back_to_base(self):
        base = self.change()
        co, status = self.drive(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py',
                                FAKE_GATE_BLOCK_IF_FILE=str(self.workspace / 'writer_note.py'))
        self.rolled_back(co, status, base, 'gate', ['simplifier'])

    def test_a_test_writer_change_alone_is_restored_from_base_capture(self):   # the simplifier skipped (small change)
        base = self.change(small=True)
        target = 'tests/test_writer_note.py'
        co, status = self.drive(FAKE_WRITER_WRITE='test-writer', FAKE_WRITER_FILE=target,
                                FAKE_EXEC_REVISE_IF_FILE=str(self.workspace / target))
        state = self.rolled_back(co, status, base, 'reviewer', ['test-writer'])
        self.assertEqual(state['lifecycle']['quality_writers']['simplifier']['state'], 'skipped:small')

    def test_the_rollback_reopens_what_the_replay_closed_and_withdraws_what_it_raised(self):
        base = self.change()
        co = rc.Coordinator(rc.parser().parse_args(self.command(*RUN)[2:]))
        [old] = co.record_findings('persistent-reviewer', 'EXEC', 1, [
            {'severity': 'LOW', 'file': 'new_code.py', 'summary': 'advisory naming', 'failure_scenario': 'x'}])
        life = co.state['lifecycle']
        life['quality_writers'] = {'base_oid': base, 'base_capture': co._writer_capture(self.root / 'base-capture'),
                                   'simplifier': {'state': 'wrote', 'sequence': 5}}
        life['receipts'] = [{'stage': 'POLISH-Q', 'epoch': 0, 'candidate_oid': base, 'output_oid': base, 'request_id': 'w-POLISH-Q-0-0'}]
        co._writer_replay()
        (self.workspace / 'new_code.py').write_text(CODE + '# simplifier edit\n')
        row = next(r for r in co.state['finding_ledger'] if r['id'] == old['id'])
        row['status'] = 'fixed'   # the replay reviewer closed it on the writer's diff
        [new] = co.record_findings('persistent-reviewer', 'EXEC', 50, [   # the replay review's own round
            {'severity': 'MAJOR', 'file': 'new_code.py', 'summary': 'regression', 'failure_scenario': 'y'}])
        self.assertNotEqual(new['id'], old['id'])
        self.assertTrue(co._writer_review_rollback('reviewer', 50))
        statuses = {r['id']: r['status'] for r in co.state['finding_ledger']}
        self.assertEqual((statuses[old['id']], statuses[new['id']]), ('open', 'withdrawn'))
        self.assertEqual((co.state['lifecycle']['stage'], co.state['next'], rc.git_snapshot(self.workspace)[0]),
                         ('DOCS', 'docs', base))
        self.assertFalse(co._writer_review_rollback('reviewer', 99))   # no replay: the normal fix round

    # --- gate LOW: a zero-tool turn that wrote is exhausted and rolled back, with no retry ---------------------------------
    def test_a_zero_tool_turn_that_wrote_is_exhausted_without_a_retry(self):
        base = self.change()
        done = self.run_coordinator(*RUN, env={'FAKE_WRITER_WRITE': 'simplifier', 'FAKE_WRITER_NO_TOOLS': 'simplifier'})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        simplifier = self.state()['lifecycle']['quality_writers']['simplifier']
        self.assertEqual((simplifier['state'], simplifier['attempts']), ('exhausted', 1))
        self.assertEqual(rc.git_snapshot(self.workspace)[0], base)
        self.assertIn('# simplifier edit', Path(simplifier['diff']).read_text())


if __name__ == '__main__':
    unittest.main()
