"""LG2-a3: report SECURITY findings, the REPORTED terminal, incomplete HOLDs and the role-count budget."""
import json
import unittest
from unittest import mock

from paired_session import test_real_coordinator as trc
from paired_session import worktree_lifecycle as wl

rc = trc.rc
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
           'fake_codex_cli', 'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action')
FLAGS = ('--review-only', '--review-report', '--lifecycle-mode', 'on')


class ReviewReportTerminalTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def change(self, text='def sum_ints(values):\n    return sum(values)\n'):
        (self.workspace / 'sum_ints.py').write_text(text)

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def args(self, *extra, action='run'):
        command = self.command(*FLAGS, *extra)
        command[2] = action
        return rc.parser().parse_args(command[2:])

    def test_a_preflight_secret_is_a_critical_finding_and_the_run_still_reports(self):
        self.change('TOKEN = "ghp_' + 'A' * 36 + '"\n')
        result = self.run_coordinator(*FLAGS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual(state['status'], 'REPORTED')
        rows = [row for row in state['finding_ledger'] if row['source'] == 'security-preflight']
        critical = [row for row in rows if row['severity'] == 'CRITICAL']
        self.assertEqual(len(critical), 1, [(row['severity'], row['summary']) for row in rows])
        [secret] = critical
        self.assertTrue(all(row['severity'] == 'MINOR' and row['file'] == '.gitignore' for row in rows if row is not secret))
        self.assertEqual((secret['severity'], secret['security'], secret['status'], secret['file']),
                         ('CRITICAL', True, 'open', 'sum_ints.py'))
        self.assertTrue(secret['summary'].startswith('[class: secret] github-token in sum_ints.py'))
        self.assertNotIn('ghp_', json.dumps(state['finding_ledger']))   # the value never enters the ledger
        self.assertEqual(sum(t['phase'] == 'SECURITY' for t in state['turns']), 1)   # the reviewer still runs
        self.assertTrue(all(t['role'] != 'author' for t in state['turns']))

    def test_committed_secrets_are_deduplicated_and_scoped_to_the_change(self):
        import subprocess

        def git(*args):
            subprocess.run(['git', *args], cwd=self.workspace, check=True, capture_output=True)
        token = lambda letter: 'TOKEN = "ghp_' + letter * 36 + '"\n'
        (self.workspace / 'fixtures').mkdir()
        (self.workspace / 'fixtures' / 'old.py').write_text(token('A'))   # already in the base
        git('add', '-A')
        git('commit', '-qm', 'base with an old fixture secret')
        (self.workspace / 'added.py').write_text(token('B'))          # carried by the change, committed
        git('add', '-A')
        git('commit', '-qm', 'the change')
        result = self.run_coordinator(*FLAGS, '--base', 'HEAD~1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [row for row in self.state()['finding_ledger'] if row['source'] == 'security-preflight']
        critical = [row for row in rows if row['severity'] == 'CRITICAL']
        self.assertEqual([row['file'] for row in critical], ['added.py'], rows)   # once, though worktree and index both match
        old = [row for row in rows if row['file'] == 'fixtures/old.py']
        self.assertEqual([(row['severity'], row['summary'].split(']')[0]) for row in old],
                         [('MINOR', '[class: secret-preexisting')], rows)
        self.assertNotIn('ghp_', json.dumps(rows))

    def removed_secret_rows(self, move_to=None):
        import subprocess
        (self.workspace / 'fixtures').mkdir()
        old = self.workspace / 'fixtures' / 'old.py'
        old.write_text('TOKEN = "ghp_' + 'C' * 36 + '"\n')
        for args in (('add', '-A'), ('commit', '-qm', 'base with a secret')):
            subprocess.run(['git', *args], cwd=self.workspace, check=True, capture_output=True)
        if move_to:
            old.rename(self.workspace / move_to)   # an unstaged rename
        else:
            old.unlink()                           # an unstaged delete
        result = self.run_coordinator(*FLAGS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [row for row in self.state()['finding_ledger'] if row['source'] == 'security-preflight']

    def test_an_unstaged_delete_of_a_secret_is_not_reported_as_published(self):
        rows = self.removed_secret_rows()
        self.assertFalse([row for row in rows if row['severity'] == 'CRITICAL'], rows)
        old = [row for row in rows if row['file'] == 'fixtures/old.py']
        self.assertEqual([(row['severity'], row['summary'].split(']')[0]) for row in old],
                         [('MINOR', '[class: secret-removed')], rows)

    def test_an_unstaged_rename_does_not_report_the_old_path_as_published(self):
        rows = self.removed_secret_rows(move_to='fixtures/moved.py')
        self.assertFalse([row for row in rows if row['severity'] == 'CRITICAL' and row['file'] == 'fixtures/old.py'], rows)
        self.assertIn(('MINOR', 'fixtures/old.py'), [(row['severity'], row['file']) for row in rows])
        self.assertIn(('CRITICAL', 'fixtures/moved.py'), [(row['severity'], row['file']) for row in rows])   # the tree carries it there

    def test_security_reviewer_findings_are_report_content(self):
        co, _ = self.drive_to_security()
        with mock.patch.object(co, '_security_review_turn', return_value={
                'status': 'REQUEST_CHANGES', 'finding_ids': ['F099'], 'reason': 'security reviewer findings: F099'}):
            self.assertEqual(co.drive(), 'REPORTED')
        self.assertIs(co.state['report']['complete'], True)

    def test_refusals_on_reported(self):
        self.change()
        self.assertEqual(self.run_coordinator(*FLAGS).returncode, 0)
        before = (self.run_dir / 'state.json').read_bytes()
        for action, extra, message in (('accept', ('--intent-only',), 'report mode has nothing to accept'),
                                       ('accept', ('--expect', '0' * 64), 'report mode has nothing to accept'),
                                       ('reject', ('--text', 'x'), 'report mode refuses note and reject'),
                                       ('note', ('--text', 'x'), 'report mode refuses note and reject')):
            with self.subTest(action=action, extra=extra):
                result = self.run_operator_action(action, *extra)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('REFUSED: ' + message, result.stdout)
                self.assertEqual((self.run_dir / 'state.json').read_bytes(), before)
        result = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertEqual((result.returncode, self.state()['status']), (0, 'REPORTED'))
        result = self.run_operator_action('abort', '--lifecycle-mode', 'on')   # terminal: nothing to abort, and no stale HOLD reason
        self.assertEqual((result.returncode, result.stdout.strip().splitlines()[-1]), (0, 'REPORTED'), result.stdout + result.stderr)
        self.assertEqual(self.state()['status'], 'REPORTED')
        co = rc.Coordinator(self.args(action='resume'))
        self.assertEqual(co.hold('late'), 'REPORTED')   # a HOLD never reopens a REPORTED run

    def assert_incomplete(self, co, reason_part, missing):
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn(reason_part, co.state['hold_reason'])
        report = co.state['report']
        self.assertEqual((report['complete'], report['hold_reason']), (False, co.state['hold_reason']))
        self.assertEqual(report['not_completed'], missing)

    def drive_to_security(self):
        """A report run driven through EXEC, the gate and POLISH-Q, stopped right before SECURITY."""
        self.change()
        co = rc.Coordinator(self.args())
        real = co.worktree_security_turn
        with mock.patch.object(co, 'worktree_security_turn', side_effect=lambda: co.hold('stop before SECURITY')):
            co.drive()
        co.state.update(status='ACTIVE')
        co.state.pop('hold_reason', None)
        return co, real

    def test_hold_classes_stay_holds_and_mark_the_report_incomplete(self):
        co, security = self.drive_to_security()
        with mock.patch.object(co, '_security_preflight', return_value={'status': 'unavailable', 'reason': 'security preflight unavailable: x'}):
            self.assertEqual(co.drive(), 'HOLD')
        self.assert_incomplete(co, 'security preflight unavailable', ['SECURITY'])
        co.state.update(status='ACTIVE')
        with mock.patch.object(co, '_security_review_turn', return_value={
                'status': 'HOLD', 'finding_ids': [], 'reason': 'security reviewer made no tool calls after one retry'}):
            self.assertEqual(co.drive(), 'HOLD')
        self.assert_incomplete(co, 'no tool calls after one retry', ['SECURITY'])
        co.state.update(status='ACTIVE', active={'role': 'reviewer', 'sequence': 99})
        self.assertEqual(co.drive(), 'HOLD')   # an uncertain turn
        self.assert_incomplete(co, 'uncertain', ['SECURITY'])
        self.run_dir = self.root / 'early'
        self.change()
        early = rc.Coordinator(self.args())
        early.state['invocations_used'] = early._report_budget()   # the budget runs out before the first review
        self.assertEqual(early.drive(), 'HOLD')
        self.assert_incomplete(early, 'invocation limit reached (report budget',
                               ['EXEC reviewer and shadow', 'adversarial gate', 'POLISH-Q specialists', 'SECURITY'])
        self.assertEqual(early.state['turns'], [])

    def test_a_counted_review_that_held_is_not_a_completed_stage(self):
        self.change()
        co = rc.Coordinator(self.args())
        co.state['reviews_completed'] = 1   # capture_review_baseline counts a review before its HOLD check
        co.hold('reviewer HOLD')
        self.assertEqual(co.state['report']['not_completed'],
                         ['EXEC reviewer and shadow', 'adversarial gate', 'POLISH-Q specialists', 'SECURITY'])

    def test_the_budget_refuses_to_freeze_on_a_drifted_tree(self):
        self.change()
        co = rc.Coordinator(self.args())
        (self.workspace / 'drift.txt').write_text('x\n')
        with self.assertRaisesRegex(RuntimeError, 'report tree changed'):
            co._report_budget()
        self.assertNotIn('report_budget', co.state)

    def test_the_budget_follows_the_role_count(self):
        self.change()
        co = rc.Coordinator(self.args())
        specialists = len(wl.report_specialists(['sum_ints.py']))
        self.assertEqual(co._report_budget(), 2 * ((1 + 1 + 1 + specialists + 1) + 2))   # gate codex != reviewer claude
        self.assertEqual(co.state['report_budget'], co._report_budget())   # frozen
        self.run_dir = self.root / 'same-vendor-no-shadow'
        co = rc.Coordinator(self.args('--gate-vendor', 'claude', '--shadow', 'off', '--skip-quality-polish', 'true'))
        self.assertEqual(co._report_budget(), 2 * ((1 + 1 + 1) + 1))
        self.run_dir = self.root / 'ordinary'
        ordinary = rc.Coordinator(rc.parser().parse_args(self.command('--lifecycle-mode', 'on')[2:]))
        with mock.patch.object(ordinary, '_report_budget', side_effect=AssertionError('ordinary runs have no report budget')):
            ordinary.state['invocations_used'] = ordinary.args.max_invocations
            with self.assertRaisesRegex(RuntimeError, r'^invocation limit reached$'):
                ordinary.invoke('reviewer', 'EXEC', 'x', rc.review_schema())


if __name__ == '__main__':
    unittest.main()
