"""OFF-1: opt-in fixtures exercise the real delivery lifecycle."""
import json
import subprocess
import unittest

from paired_session.lifecycle_fixtures import OnModeFixtures


class OnModeFixtureTests(OnModeFixtures, unittest.TestCase):
    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.workspace)

    def assert_done(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('DONE (acceptance pending)', result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['config']['lifecycle_mode'], 'on')
        self.assertEqual((state['status'], state['lifecycle']['stage']), ('DONE', 'DONE'))
        self.assertEqual(state['lifecycle']['receipts'][-1]['stage'], 'SECURITY')
        return state

    def test_main_pipeline_security_and_uncommitted_accept(self):
        # Use non-default saved role settings too: operator helpers must restore them.
        self.assert_done(self.run_on('--auto-commit', 'false', '--gate-vendor', 'claude'))
        before = (self.git('rev-parse', 'HEAD'), self.git('ls-files', '--stage'),
                  (self.workspace / '.git' / 'index').read_bytes())
        accepted = self.run_operator_on('accept', '--reason', 'reviewed')
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn('UNCOMMITTED: no commit was made (auto_commit off)', accepted.stdout)
        self.assertEqual(before, (self.git('rev-parse', 'HEAD'), self.git('ls-files', '--stage'),
                                  (self.workspace / '.git' / 'index').read_bytes()))
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertFalse(state['config']['auto_commit'])
        self.assertEqual(state['config']['gate_vendor'], 'claude')

    def test_review_only_reaches_done(self):
        (self.workspace / 'tracked.txt').write_text('reviewed change\n')
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        self.assert_done(self.run_on('--review-only', '--auto-commit', 'false'))


if __name__ == '__main__':
    unittest.main()
