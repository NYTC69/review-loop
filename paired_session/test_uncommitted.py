"""Acceptance without a commit lists the remaining files for either pipeline."""
import json
import unittest

from paired_session import test_worktree_lifecycle as twl


class UncommittedTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name)
                     for name in (*twl._HELPERS, 'setUp')})

    def check_acceptance(self, mode, review_only=False):
        options = ['--lifecycle-mode', mode, '--auto-commit', 'false']
        if review_only:
            (self.workspace / 'tracked.txt').write_text('reviewed change\n')
            (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
            options.append('--review-only')
        done = self.run_coordinator(*options)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        accepted = self.run_operator_action('accept', *options)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED')
        state = json.loads((self.run_dir / 'state.json').read_text())
        files = ['tracked.txt', 'untracked: sum_ints.py'] if review_only else ['untracked: sum_ints.py']
        self.assertEqual(state['acceptance']['uncommitted'], files)
        evidence = json.loads((self.run_dir / 'evidence' / 'acceptance.json').read_text())
        self.assertEqual(evidence['uncommitted'], files)
        self.assertIn('UNCOMMITTED: no commit was made (auto_commit off); commit these yourself: '
                      + ', '.join(files), accepted.stdout)
        self.assertNotIn('COMMIT:', accepted.stdout.replace('UNCOMMITTED:', ''))
        self.assertIn('- 未提交的文件（请自行提交）：' + '、'.join(files),
                      (self.run_dir / 'delivery-report.md').read_text())
        again = self.run_operator_action('accept', *options)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(again.stdout.strip().splitlines()[-3:], accepted.stdout.strip().splitlines()[-3:])

    def test_main_pipeline_lifecycle_off(self):
        self.check_acceptance('off')

    def test_main_pipeline_lifecycle_on(self):
        self.check_acceptance('on')

    def test_review_only_lifecycle_off_unchanged(self):
        self.check_acceptance('off', review_only=True)

    def test_review_only_lifecycle_on_unchanged(self):
        self.check_acceptance('on', review_only=True)


if __name__ == '__main__':
    unittest.main()
