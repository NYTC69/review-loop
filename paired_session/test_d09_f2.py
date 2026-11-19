"""D09-F2 (the D09-F gate's MINORs and NIT): the F1 count also counts a test file the writer newly touched in the leg's
input tree; a writer replay uses no EXEC round (WI-GATE1 R5); a by-path review rollback keeps the captured
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

    def test_a_writer_replay_uses_no_round_so_the_limit_stays_ordinary(self):   # MINOR 2, replaced by WI-GATE1 R5
        co = rc.Coordinator(rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--max-exec-rounds', '4')[2:]))
        for rounds, replay in ((3, {'start': 1, 'used': None}), (5, {'start': 4, 'used': None}), (4, {'start': 1, 'used': 2})):
            co.state.update(exec_rounds=rounds, writer_replay_rounds=replay)   # a v3.8.x state: the record is ignored
            self.assertEqual(co.exec_round_limit(), 4)

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
