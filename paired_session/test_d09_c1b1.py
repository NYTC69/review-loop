"""D09 C1-b1 (paired_session/docs/d09-cap1-writer-passes.md §2, §5, §6 batch C1-b1): the POLISH-Q writer legs through the
FINISH writer path, with input binding, captures, the tree-over-answer judgment, the rollback of a HOLD-with-writes or a
failed attempt, and the zero-tool retry. The replay (C1-b2) and the local check (C1-b3) are not built yet, so a kept
write is rolled back as `rolled-back:pending-replay`: nothing unreviewed reaches DOCS."""
import json
from pathlib import Path
import unittest
from unittest import mock

from paired_session import test_worktree_lifecycle as twl
from paired_session import worktree_lifecycle as wl

rc, DONE = twl.rc, twl.DONE
CODE = ''.join(f'VALUE_{n} = {n}\n' for n in range(30))
RUN = ('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '80')


class D09C1b1Tests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'git')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def eligible_change(self):   # 30 new code lines and a changed test file: both writers pass every skip rule
        (self.workspace / 'new_code.py').write_text(CODE)
        (self.workspace / 'tests').mkdir()
        (self.workspace / 'tests' / 'test_new_code.py').write_text('import new_code\n')

    def run_writers(self, **env):
        self.eligible_change()
        before = rc.git_snapshot(self.workspace)[0]
        done = self.run_coordinator(*RUN, env=env)
        state = self.state()
        return done, state, before

    def legs(self, state):
        return [row for row in state['lifecycle']['receipts'] if row.get('role') in ('simplifier', 'test-writer')]

    # --- §5 test 2 (interim): both legs write; one binding chain; nothing unreviewed reaches DOCS ----------------------------
    def test_both_writers_write_and_are_rolled_back_until_the_replay_lands(self):
        self.eligible_change()
        self.git('add', 'tests/test_new_code.py')   # a staged entry from before the run: the restore keeps the index
        staged = self.git('diff', '--cached', '--name-only')
        before = rc.git_snapshot(self.workspace)[0]
        done = self.run_coordinator(*RUN, env={'FAKE_WRITER_WRITE': 'simplifier,test-writer'})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        marker = state['lifecycle']['quality_writers']
        self.assertEqual([marker[w]['state'] for w in ('simplifier', 'test-writer')], ['rolled-back:pending-replay'] * 2)
        self.assertEqual((marker['base_oid'], marker['simplifier']['input_oid'], marker['simplifier']['output_oid'],
                          marker['test-writer']['input_oid'], marker['test-writer']['output_oid']), (before,) * 5)
        self.assertTrue(Path(marker['base_capture'], 'record.json').is_file())
        for writer in ('simplifier', 'test-writer'):
            self.assertIn(f'# {writer} edit', Path(marker[writer]['diff']).read_text())   # the rolled-back change as evidence
        self.assertEqual([row['stage'] for row in state['lifecycle']['receipts']],
                         ['FINISH', 'POLISH-Q', 'POLISH-Q', 'POLISH-Q', 'DOCS', 'SECURITY'])
        self.assertEqual([(row['role'], row['writer_state']) for row in self.legs(state)],
                         [('simplifier', 'rolled-back:pending-replay'), ('test-writer', 'rolled-back:pending-replay')])
        [docs] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS']
        self.assertEqual((docs['candidate_oid'], state['lifecycle']['epoch'], state['exec_rounds']), (before, 0, 1))
        self.assertNotIn('# simplifier edit', (self.workspace / 'sum_ints.py').read_text() if (self.workspace / 'sum_ints.py').exists() else '')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), staged)
        writers = [t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'POLISH-Q']
        self.assertEqual(len(writers), 2)
        self.assertTrue(all(t['fresh'] for t in writers))
        prompts = [(self.run_dir / 'evidence' / f"{t['sequence']:03d}-polish-q-author.prompt.txt").read_text() for t in writers]
        self.assertIn('Role: simplifier, fresh. Phase: POLISH-Q.', prompts[0])
        self.assertIn('Only refine code that has been recently modified', prompts[0])   # the inlined agent body
        self.assertIn('Consolidate the changed test files only: tests/test_new_code.py.', prompts[1])
        self.assertIn('Do not add tests for uncovered logic.', prompts[1])
        self.assertEqual(state['lifecycle']['writer_counts'], {'simplifier': 1, 'test-writer': 1})
        self.assertNotIn('writer_leg', state['lifecycle'])
        self.assertIn('质量 writer simplifier：rolled-back:pending-replay', wl.delivery_report(
            {**state, 'accepted_at': ''}, 'run', '# item\n', {'auto_commit': False}))

    # --- §5 test 3: no write, whatever the answer ---------------------------------------------------------------------------
    def test_writers_that_change_nothing_are_no_op_even_on_hold(self):
        done, state, before = self.run_writers(FAKE_WRITER_HOLD='test-writer')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        marker = state['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('no-op', 'no-op'))
        self.assertNotIn('diff', marker['simplifier'])
        self.assertEqual(rc.git_snapshot(self.workspace)[0], before)

    # --- §5 test 5: HOLD with a changed tree --------------------------------------------------------------------------------
    def test_a_hold_with_a_write_is_rolled_back_and_the_test_writer_still_runs(self):
        done, state, before = self.run_writers(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_HOLD='simplifier')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        marker = state['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('rolled-back:hold', 'no-op'))
        self.assertEqual((marker['test-writer']['input_oid'], rc.git_snapshot(self.workspace)[0]), (before, before))

    # --- §5 test 6: failed attempts and the zero-tool retry ------------------------------------------------------------------
    def test_a_failed_attempt_is_rolled_back_as_exhausted(self):
        done, state, before = self.run_writers(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FAIL='simplifier')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        marker = state['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['simplifier']['attempts'], marker['test-writer']['state']),
                         ('exhausted', 1, 'no-op'))
        self.assertEqual(rc.git_snapshot(self.workspace)[0], before)

    def test_a_turn_without_tool_calls_is_retried_once(self):
        done, state, _ = self.run_writers(FAKE_WRITER_NO_TOOLS_ONCE=str(self.root / 'no-tools-once'))
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        marker = state['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['simplifier']['attempts']), ('no-op', 2))
        self.assertEqual(len([t for t in state['turns'] if t['phase'] == 'POLISH-Q' and t['role'] == 'author'
                              and t.get('discarded') == 'no tool calls']), 1)
        self.assertEqual(state['lifecycle']['polish_calls'],   # every POLISH-Q dispatch counted once, the retry included
                         len([t for t in state['turns'] if t['phase'] == 'POLISH-Q']))
        self.assertEqual(state['lifecycle']['writer_counts'], {'simplifier': 2, 'test-writer': 1})

    def test_a_rate_limited_writer_gives_its_count_back_and_keeps_its_retry(self):   # §3 counts, as the specialists
        env = {'FAKE_RATE_LIMIT': '1', 'FAKE_RATE_LIMIT_MATCH': 'Role: simplifier, fresh.',
               'FAKE_RATE_LIMIT_ONCE': str(self.root / 'limited-once'), 'FAKE_WRITER_NO_TOOLS_ONCE': str(self.root / 'no-tools')}
        (self.root / 'no-tools').write_text('not yet\n')   # the zero-tool answer comes after the resume
        held, state, _ = self.run_writers(**env)
        self.assertEqual((state['status'], state.get('hold_kind')), ('HOLD', 'rate_limited'), held.stdout)
        self.assertEqual((state['lifecycle']['writer_counts'], state['lifecycle']['writer_leg']['attempts']), ({'simplifier': 0}, 0))
        (self.root / 'no-tools').unlink()
        resumed = self.run_operator_action('resume', *RUN, env=env)
        self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        state = self.state()
        marker = state['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['simplifier']['attempts']), ('no-op', 2))   # retried, not exhausted
        self.assertEqual(state['lifecycle']['writer_counts'], {'simplifier': 2, 'test-writer': 1})
        self.assertEqual(state['lifecycle']['polish_calls'],
                         len([t for t in state['turns'] if t['phase'] == 'POLISH-Q' and t.get('error_kind') != 'rate_limited']))

    def test_two_turns_without_tool_calls_exhaust_the_writer(self):
        done, state, _ = self.run_writers(FAKE_WRITER_NO_TOOLS='simplifier')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        marker = state['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['simplifier']['attempts'], marker['test-writer']['state']),
                         ('exhausted', 2, 'no-op'))
        self.assertEqual(state['lifecycle']['writer_counts'], {'simplifier': 2, 'test-writer': 1})

    # --- §5 test 11: the boundary -------------------------------------------------------------------------------------------
    def test_a_writer_that_moves_the_index_holds(self):   # the FINISH writer guard, unchanged
        done, state, _ = self.run_writers(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_STAGE='simplifier')
        self.assertEqual(state['status'], 'HOLD', done.stdout)
        self.assertIn('POLISH-Q simplifier changed HEAD, refs or the index', state['hold_reason'])
        self.assertNotIn('simplifier', state['lifecycle']['quality_writers'])

    def test_a_reserved_docs_or_review_loop_config_write_never_reaches_docs(self):   # §2 write boundary, as for FINISH
        done, state, before = self.run_writers(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='CHANGELOG.md')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        marker = state['lifecycle']['quality_writers']
        self.assertEqual(marker['simplifier']['state'], 'rolled-back:pending-replay')   # the reserved DOCS file is put back
        self.assertIn('CHANGELOG.md', Path(marker['simplifier']['diff']).read_text())
        [docs] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS']
        self.assertEqual(docs['candidate_oid'], before)
        self.run_dir = self.root / 'config-write'
        done = self.run_coordinator(*RUN, env={'FAKE_WRITER_WRITE': 'simplifier', 'FAKE_WRITER_FILE': '.review-loop/x.json'})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()   # .review-loop/ is ignored: outside the reviewed tree, so nothing to deliver, as for FINISH
        self.assertEqual((state['lifecycle']['quality_writers']['simplifier']['state'], rc.git_snapshot(self.workspace)[0]),
                         ('no-op', before))
        self.assertEqual(state['config']['quality_writers'], 'both')   # the frozen config is untouched

    def test_a_stale_input_holds_before_dispatch_and_an_unverified_restore_holds(self):
        self.eligible_change()
        co = rc.Coordinator(rc.parser().parse_args(self.command(*RUN)[2:]))
        tree = rc.git_snapshot(self.workspace)[0]
        co.state['lifecycle'].update(stage='POLISH-Q', candidate_oid=tree, writer_queue=['simplifier'],
                                     quality_writers={'base_oid': tree})
        co.state['next'] = 'polish-simplify'
        (self.workspace / 'new_code.py').write_text(CODE + 'X = 1\n')   # the tree moved after POLISH-Q
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('dispatched')):
            with self.assertRaisesRegex(RuntimeError, 'POLISH-Q simplifier: the tree differs from its input'):
                co.worktree_writer_turn()
        (self.workspace / 'new_code.py').write_text(CODE)
        keep = self.root / 'capture'
        leg = {'writer': 'simplifier', 'input_oid': tree, 'capture': co._writer_capture(keep)}
        (self.workspace / 'new_code.py').write_text(CODE + '# simplifier edit\n')
        with mock.patch.object(rc.readonly_guard, 'restore', return_value='cannot restore'):
            with self.assertRaisesRegex(RuntimeError, 'could not be rolled back \\(cannot restore\\)'):
                co._writer_rollback('simplifier', leg, 7)
        record = co.state['unrestored_readonly_turn']
        self.assertEqual((record['role'], record['snapshot'], record['reason']), ('simplifier', tree, 'cannot restore'))
        self.assertIn('could not be restored', co.unrestored_workspace_issue())
        co._writer_rollback('simplifier', leg, 7)   # the real restore verifies
        self.assertEqual(rc.git_snapshot(self.workspace)[0], tree)


if __name__ == '__main__':
    unittest.main()
