"""D09-F2 (the D09-F gate's MINORs and NIT): the F1 count also counts a test file the writer newly touched in the leg's
input tree; an open writer replay keeps the ordinary rounds that are left; a by-path review rollback keeps the captured
permission bits."""
import os
from pathlib import Path
import stat
import unittest

from paired_session import test_worktree_lifecycle as twl

rc = twl.rc
CODE = ''.join(f'VALUE_{n} = {n}\n' for n in range(30))
RUN = ('--review-only', '--lifecycle-mode', 'on')
CASES = lambda n: 'import unittest\n\n\nclass T(unittest.TestCase):\n' + ''.join(
    f'    def test_{i}(self):\n        self.assertTrue(True)\n\n' for i in range(n))


class D09F2Tests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'git')})

    def coordinator_with_change(self):
        (self.workspace / 'new_code.py').write_text(CODE)
        return rc.Coordinator(rc.parser().parse_args(self.command(*RUN)[2:]))

    def test_a_newly_touched_test_file_cannot_hide_a_dropped_case(self):   # MINOR 1
        (self.workspace / 'tests').mkdir()
        (self.workspace / 'tests' / 'test_b.py').write_text(CASES(3))   # committed, unchanged before the leg
        self.git('add', 'tests/test_b.py')
        self.git('commit', '-qm', 'tests b')
        (self.workspace / 'tests' / 'test_a.py').write_text(CASES(2))   # the change's own test file
        co = self.coordinator_with_change()
        leg = {'writer': 'test-writer', 'capture': co._writer_capture(self.root / 'capture'), 'test_cases': co._test_case_count()}
        self.assertEqual(leg['test_cases'], {'tests/test_a.py': 2})
        (self.workspace / 'tests' / 'test_a.py').write_text(CASES(1))   # a case dropped here ...
        (self.workspace / 'tests' / 'test_b.py').write_text(CASES(3) + '# tidied\n')   # ... and test_b only touched
        self.assertTrue(co._test_cases_dropped(leg))
        self.assertEqual(leg['detail'], 'test cases 5 -> 4 in the changed test files')
        (self.workspace / 'tests' / 'test_a.py').write_text(CASES(2))
        leg.pop('detail')
        self.assertFalse(co._test_cases_dropped(leg))   # touching a file without dropping a case is fine

    def test_an_open_replay_keeps_the_ordinary_rounds_left(self):   # MINOR 2
        co = rc.Coordinator(rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--max-exec-rounds', '4')[2:]))
        co.state.update(exec_rounds=3, writer_replay_rounds={'start': 1, 'used': None})
        self.assertEqual(co.exec_round_limit(), 4)   # max(1 + 2, 4): a FINISH write at round 4 does not HOLD
        co.state.update(exec_rounds=5, writer_replay_rounds={'start': 4, 'used': None})
        self.assertEqual(co.exec_round_limit(), 6)   # at the ordinary limit the replay's own two rounds still apply
        co._close_writer_replay_rounds()
        self.assertEqual((co.state['writer_replay_rounds']['used'], co.exec_round_limit()), (1, 5))   # frozen as before
        # no double credit: a replay that ran past start + 2 on ordinary rounds (max 4, start 1: replay 2, fix 3, then an
        # ordinary round 4) freezes only its own 2; the limit is ordinary + 2 and the room left is the ordinary room left
        co.state.update(exec_rounds=4, writer_replay_rounds={'start': 1, 'used': None})
        self.assertEqual(co.exec_round_limit(), 4)
        co._close_writer_replay_rounds()
        ordinary_used = co.state['exec_rounds'] - co.state['writer_replay_rounds']['used']   # rounds 1 and 4
        self.assertEqual((co.state['writer_replay_rounds']['used'], co.exec_round_limit()), (2, 6))
        self.assertEqual(co.exec_round_limit() - co.state['exec_rounds'], 4 - ordinary_used)   # 2 ordinary rounds left
        self.assertLessEqual(co.exec_round_limit(), 4 + 2)

    def test_a_by_path_review_rollback_keeps_the_captured_mode(self):   # NIT
        co = self.coordinator_with_change()
        target = self.workspace / 'new_code.py'
        os.chmod(target, 0o600)
        keep = Path(co._writer_capture(self.root / 'base-capture'))
        target.write_text(CODE + '# simplifier edit\n')
        os.chmod(target, 0o644)
        co._revert_paths(['new_code.py'], keep, 1)
        self.assertEqual((target.read_text(), stat.S_IMODE(target.stat().st_mode)), (CODE, 0o600))


if __name__ == '__main__':
    unittest.main()
