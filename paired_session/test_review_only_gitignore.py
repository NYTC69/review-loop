"""V3-FIX-1: in a review-only run the reviewed change is the run's own (its baseline is the review base), so a covering
.gitignore edit in that change counts at SECURITY although the start warning (HEAD's .gitignore only) fires."""
import contextlib
import io
import unittest

from paired_session import test_real_coordinator as trc
from paired_session.test_worktree_lifecycle import COVERING_GITIGNORE

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action', 'coordinator')


class ReviewOnlyGitignoreTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def args(self, *extra, action='run'):
        command = self.command('--lifecycle-mode', 'on', '--review-only', *extra)
        command[2] = action
        return rc.parser().parse_args(command[2:])

    def test_a_covering_gitignore_edit_in_the_reviewed_change_warns_at_start_and_counts_at_security(self):
        (self.workspace / '.gitignore').write_text('__pycache__/\n*.cache\n.review-loop/\n')   # the base lacks coverage
        rc.subprocess.run(['git', 'add', '.gitignore'], cwd=self.workspace, check=True)
        rc.subprocess.run(['git', 'commit', '-qm', 'narrow ignore rules'], cwd=self.workspace, check=True)
        (self.workspace / '.gitignore').write_text(COVERING_GITIGNORE)   # the change under review restores it
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            co = rc.Coordinator(self.args())
        self.assertIn('WARNING: the tracked .gitignore does not cover', out.getvalue())
        self.assertTrue(co.state['lifecycle']['ignore_coverage_at_start']['uncovered'])
        result = co._security_preflight('w-SECURITY-0-0')
        self.assertEqual((result['status'], result['uncovered_ignore'], result['reason']), ('clean', [], None))


if __name__ == '__main__':
    unittest.main()
