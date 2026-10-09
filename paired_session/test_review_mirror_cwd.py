"""v297-cwd: the review-mirror diff runs in the workspace, not in the coordinator's process cwd."""
import os
import shutil
import tempfile
import unittest

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'coordinator')


class ReviewMirrorCwdTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def test_the_since_last_review_diff_works_when_launched_outside_any_git_repo(self):
        use_lifecycle_on(self, self)
        co = self.coordinator()
        co.capture_review_baseline()                                 # the first review's mirror (reviews_completed > 0)
        self.assertTrue((co.internal / 'last-review').exists())
        (self.workspace / 'tracked.txt').write_text('base\nchanged after the review\n')
        outside = tempfile.mkdtemp()                                 # the system temp dir: not inside any git repository
        self.addCleanup(shutil.rmtree, outside, True)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(outside)                                            # as when launched from ~/paired-runs/<dir> (not a repo)
        co.materialize_review_context()
        since = (co.context / 'delta-since-last-review.patch').read_text()
        self.assertIn('+changed after the review', since)
        self.assertIn('tracked.txt', since)


if __name__ == '__main__':
    unittest.main()
