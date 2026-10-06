"""D09-F (owner decisions 2026-10-06, after the ws25/ws26 review-only field runs):
F1 the test writer must not remove test cases (a lower count of test functions in the changed test files is a failed
local test); F3 the writer replay round and its one fix round have their own budget, not --max-exec-rounds; F2 when
the two writers changed disjoint files and the replay's blocking findings name one of them, only that writer is rolled
back and the other is reviewed again in the replay's fix round."""
import json
from pathlib import Path
import unittest

from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE
CODE = ''.join(f'VALUE_{n} = {n}\n' for n in range(30))
RUN = ('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '100')
SMOKE = 'import unittest\n\n\nclass Smoke(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n'
TWO = ('import unittest\n\n\nclass SpreadTests(unittest.TestCase):\n    def test_empty(self):\n        self.assertTrue(True)\n\n'
       '    def test_single(self):\n        self.assertTrue(True)\n')


class D09FTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'git')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def run_writers(self, *extra, **env):
        (self.workspace / 'new_code.py').write_text(CODE)
        (self.workspace / 'tests').mkdir()
        (self.workspace / 'tests' / 'test_new_code.py').write_text(TWO)
        (self.workspace / 'test.py').write_text(SMOKE)   # a green baseline on any Python (not a test path)
        base = rc.git_snapshot(self.workspace)[0]
        done = self.run_coordinator(*RUN, *extra, env=env)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        return state, state['lifecycle']['quality_writers'], base

    # --- F1 --------------------------------------------------------------------------------------------------------------
    def test_the_test_case_pattern_counts_each_language(self):
        text = ('def test_a():\n    pass\n    async def test_b(self):\n        pass\ndef helper_test():\n    pass\n'
                'func TestThing(t *testing.T) {}\n#[test]\nfn works() {}\nit("does x", () => {})\ntest(\'y\', fn)\n'
                'describe("group", () => {})\n')
        self.assertEqual(len(rc.TEST_CASE_RE.findall(text)), 6)

    def test_a_test_writer_that_drops_a_test_case_is_rolled_back(self):   # ws26: SpreadTests.test_single deleted
        state, marker, base = self.run_writers(FAKE_WRITER_WRITE='test-writer',
                                               FAKE_WRITER_FILE_TEST_WRITER='tests/test_new_code.py',
                                               FAKE_WRITER_CONTENT_TEST_WRITER=TWO.split('\n\n    def test_single')[0] + '\n')
        writer = marker['test-writer']
        self.assertEqual((writer['state'], writer['detail']), ('rolled-back:tests', 'test cases 2 -> 1 in the changed test files'))
        self.assertNotIn('test', writer)   # no local run was needed
        self.assertEqual(((self.workspace / 'tests' / 'test_new_code.py').read_text(), rc.git_snapshot(self.workspace)[0]),
                         (TWO, base))
        prompts = [p.read_text() for p in (self.run_dir / 'evidence').glob('*-polish-q-author.prompt.txt')]
        [prompt] = [text for text in prompts if 'Role: test consolidator' in text]
        self.assertIn('never delete, skip or fold away a distinct test', prompt)

    # --- F3 --------------------------------------------------------------------------------------------------------------
    def test_a_run_at_its_exec_round_limit_still_gets_the_writer_replay(self):   # ws25: EXEC 4/4 skipped both writers
        state, marker, base = self.run_writers('--max-exec-rounds', '1', FAKE_WRITER_WRITE='simplifier',
                                               FAKE_WRITER_FILE='writer_note.py')
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('wrote', 'no-op'))
        self.assertEqual((state['exec_rounds'], state['writer_replay_rounds']), (2, {'start': 1, 'used': 1}))
        self.assertTrue((self.workspace / 'writer_note.py').is_file())
        report = rc.worktree_lifecycle.writer_report_lines(state)
        self.assertIn('- 质量 writer 回放：1 轮（单独计数，不占 --max-exec-rounds）', report)

    def test_the_replay_credit_is_two_while_open_and_what_was_used_after(self):
        co = rc.Coordinator(rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--max-exec-rounds', '4')[2:]))
        self.assertEqual(co.exec_round_limit(), 4)   # an ordinary run: unchanged
        co.state.update(exec_rounds=5, writer_replay_rounds={'start': 4, 'used': None})
        self.assertEqual(co.exec_round_limit(), 6)   # the replay round and one fix round
        co._close_writer_replay_rounds()
        self.assertEqual((co.state['writer_replay_rounds']['used'], co.exec_round_limit()), (1, 5))   # no fix round used
        co.state.update(exec_rounds=6, writer_replay_rounds={'start': 4, 'used': None})
        co._close_writer_replay_rounds()
        self.assertEqual((co.state['writer_replay_rounds']['used'], co.exec_round_limit()), (2, 6))
        co.state.update(exec_rounds=3, writer_replay_rounds={'start': 1, 'used': None})   # ordinary room left (1 of 4 used)
        self.assertEqual(co.exec_round_limit(), 3)   # the open replay still gets only its own two rounds
        co.state['exec_rounds'] = 4   # a third replay round (say a FINISH write) is over the replay's budget
        self.assertGreater(co.state['exec_rounds'], co.exec_round_limit())
        co.state['exec_rounds'] = 3
        co._close_writer_replay_rounds()
        self.assertEqual((co.state['writer_replay_rounds']['used'], co.exec_round_limit()), (2, 6))   # 1 ordinary + 2 replay

    # --- F2 --------------------------------------------------------------------------------------------------------------
    def test_disjoint_writers_roll_back_only_the_one_the_finding_names(self):   # ws26: the simplifier's fixes survive
        flag = str(self.workspace / 'tests' / 'test_extra.py')
        state, marker, base = self.run_writers(FAKE_WRITER_WRITE='simplifier,test-writer',
                                               FAKE_WRITER_FILE_SIMPLIFIER='writer_note.py',
                                               FAKE_WRITER_FILE_TEST_WRITER='tests/test_extra.py', FAKE_EXEC_REVISE_IF_FILE=flag)
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('wrote', 'rolled-back:review'))
        self.assertEqual((marker['simplifier']['files'], marker['test-writer']['files']), (['writer_note.py'], ['tests/test_extra.py']))
        self.assertTrue((self.workspace / 'writer_note.py').is_file())
        self.assertFalse(Path(flag).exists())
        self.assertIn('tests/test_extra.py', Path(marker['test-writer']['review_diff']).read_text())
        self.assertEqual((state['exec_rounds'], state['writer_replay_rounds']), (3, {'start': 1, 'used': 2}))   # replay + fix
        raised = [row for row in state['finding_ledger'] if row['file'] == 'tests/test_extra.py']
        self.assertTrue(raised and all(row['status'] == 'withdrawn' for row in raised))
        reviews = [t for t in state['turns'] if t['role'] == 'reviewer' and t['phase'] == 'EXEC'
                   and t['sequence'] > marker['test-writer']['sequence']]
        self.assertEqual(len(reviews), 2)   # the failed replay review, then the kept change reviewed again
        self.assertNotIn('writer_replay', state['lifecycle'])

    def test_writers_that_share_a_file_are_both_rolled_back(self):
        flag = str(self.workspace / 'writer_note.py')
        state, marker, base = self.run_writers(FAKE_WRITER_WRITE='simplifier,test-writer', FAKE_WRITER_FILE='writer_note.py',
                                               FAKE_EXEC_REVISE_IF_FILE=flag)
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('rolled-back:review',) * 2)
        self.assertEqual(rc.git_snapshot(self.workspace)[0], base)

    def test_the_targets_need_disjoint_files_and_findings_inside_one_set(self):
        co = rc.Coordinator(rc.parser().parse_args(self.command('--lifecycle-mode', 'on')[2:]))
        marker = {'simplifier': {'files': ['a.py']}, 'test-writer': {'files': ['tests/test_a.py']}}
        replay = {'writers': ['simplifier', 'test-writer'], 'sequence': 10}
        finding = lambda file, seq=11: co.record_findings('persistent-reviewer', 'EXEC', seq, [
            {'severity': 'MAJOR', 'file': file, 'summary': 'regression ' + str(file), 'failure_scenario': 'x'}])
        finding('tests/test_a.py:12')
        self.assertEqual(co._writer_review_targets(replay, marker), ['test-writer'])
        self.assertEqual(co._writer_review_targets({**replay, 'partial': True}, marker), [])   # the second failure: both
        self.assertEqual(co._writer_review_targets(replay, {**marker, 'test-writer': {'files': ['a.py']}}), [])   # overlap
        finding('elsewhere.py', 12)   # a finding outside both sets
        self.assertEqual(co._writer_review_targets(replay, marker), [])
        co.state['finding_ledger'][-1]['status'] = 'fixed'
        finding('', 13)   # a finding with no file
        self.assertEqual(co._writer_review_targets(replay, marker), [])
        co.state['finding_ledger'][-1]['status'] = 'fixed'
        finding('a.py', 14)   # findings naming both writers
        self.assertEqual(co._writer_review_targets(replay, marker), [])


if __name__ == '__main__':
    unittest.main()
