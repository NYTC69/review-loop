"""LG2-a1: report entry and frozen policy; report transitions belong to LG2-a2."""
import json
import shlex
import subprocess
import unittest
from unittest import mock

from paired_session import test_real_coordinator as trc

rc = trc.rc
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
           'fake_codex_cli', 'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action')


class ReviewReportEntryTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def args(self, *extra, action='run'):
        command = self.command(*extra)
        command[2] = action
        return rc.parser().parse_args(command[2:])

    def create(self, *extra):
        (self.workspace / 'tracked.txt').write_text('base\nreview this change\n')
        return rc.Coordinator(self.args('--review-only', '--review-report', *extra))

    def state_bytes(self):
        return (self.run_dir / 'state.json').read_bytes()

    def test_report_flag_freezes_true_only_and_starts_at_exec_review(self):
        self.assertIsNone(self.args().review_report)
        co = self.create('--lifecycle-mode', 'on')
        self.assertTrue(co.state['config']['review_report'])
        self.assertTrue(co.state['config']['review_only'])
        self.assertEqual((co.state['phase'], co.state['next'], co.state['exec_rounds']), ('EXEC', 'reviewer', 1))
        self.assertEqual(co.state['turns'], [])
        self.run_dir = self.root / 'ordinary'
        ordinary = rc.Coordinator(self.args())
        self.assertNotIn('review_report', ordinary.state['config'])
        self.run_dir = self.root / 'review-only'
        review_only = rc.Coordinator(self.args('--review-only'))
        self.assertNotIn('review_report', review_only.state['config'])

    def test_creation_refusals_leave_no_state(self):
        cases = [(('--review-report',), 'needs --review-only'),
                 (('--review-only', '--review-report', '--auto-commit', 'true'), 'refuses --auto-commit true'),
                 (('--review-only', '--review-report', '--stop-after-plan'), 'refuses --stop-after-plan')]
        (self.workspace / 'tracked.txt').write_text('changed\n')
        for flags, message in cases:
            with self.subTest(flags=flags):
                with self.assertRaisesRegex(ValueError, message):
                    rc.Coordinator(self.args(*flags))
                result = self.run_coordinator(*flags)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('REFUSED: --review-report ' + message, result.stdout)
                self.assertFalse((self.run_dir / 'state.json').exists())

    def test_report_mode_refuses_a_review_only_scope_change_successor(self):
        (self.workspace / 'tracked.txt').write_text('changed\n')
        co = rc.Coordinator(self.args('--review-only'))
        result = self.run_operator_action('note', '--scope-change', '--text', 'Review the expanded scope')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        start = next(line.removeprefix('Start: ') for line in result.stdout.splitlines() if line.startswith('Start: '))
        before = self.state_bytes()
        spec = json.loads((co.evidence / 'successor-spec.json').read_text())
        self.assertIn('review_only', spec)   # the successor inherits the entry without an explicit --review-only
        result = subprocess.run([*shlex.split(start), '--review-report', '--skip-probe'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('--review-report starts a fresh review request; it takes no --supersedes', result.stdout)
        self.assertFalse((self.root / 'run-successor' / 'state.json').exists())
        self.assertFalse((co.evidence / 'successor-claim.json').exists())
        self.assertEqual(self.state_bytes(), before)

    def test_operator_profile_auto_commit_true_is_refused(self):
        profile = self.root / 'operator.json'
        profile.write_text(json.dumps({'auto_commit': True}))
        (self.workspace / 'tracked.txt').write_text('changed\n')
        result = self.run_coordinator('--review-only', '--review-report', '--config', str(profile))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('--review-report refuses --auto-commit true', result.stdout)
        self.assertFalse((self.run_dir / 'state.json').exists())

    def test_note_reject_and_scope_change_refuse_at_every_stage_without_mutation(self):
        co = self.create('--lifecycle-mode', 'on')
        stages = [('ACTIVE', 'EXEC', 'reviewer'), ('ACTIVE', 'EXEC', 'gate'),
                  ('ACTIVE', 'POLISH-Q', 'polish-q'), ('ACTIVE', 'SECURITY', 'security'),
                  ('HOLD', 'EXEC', 'author'), ('DONE', 'DONE', 'author'),
                  ('REPORTED', 'REPORTED', 'reviewer'), ('ABORTED', 'EXEC', 'author')]
        for status, stage, next_role in stages:
            co.state.update(status=status, next=next_role)
            co.state['lifecycle']['stage'] = stage
            co.save()
            before = self.state_bytes()
            for action in ('note', 'reject'):
                for scope in (False, True):
                    with self.subTest(status=status, stage=stage, action=action, scope=scope):
                        flags = ('--scope-change',) if scope else ()
                        with self.assertRaisesRegex(ValueError, 'report mode refuses note and reject'):
                            rc.Coordinator(self.args('--lifecycle-mode', 'on', *flags, action=action))
                        method = co.scope_change if scope else getattr(co, action)
                        with self.assertRaisesRegex(ValueError, 'report mode refuses note and reject'):
                            method('Please change the scope', None)
                        self.assertEqual(self.state_bytes(), before)
            self.assertFalse((co.evidence / 'successor-spec.json').exists())
            self.assertFalse((self.root / 'run-successor').exists())

    def test_cli_feedback_and_intent_previews_cannot_create_a_successor(self):
        co = self.create()
        co.state.update(status='HOLD', next='author')
        co.save()
        before = self.state_bytes()
        for action in ('note', 'reject'):
            for flags in ((), ('--scope-change',), ('--intent-only',), ('--scope-change', '--intent-only')):
                with self.subTest(action=action, flags=flags):
                    result = self.run_operator_action(action, '--text', 'New instructions', *flags)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn('report mode refuses note and reject, including --scope-change', result.stdout)
                    self.assertEqual(self.state_bytes(), before)
        self.assertFalse((co.evidence / 'successor-spec.json').exists())

    def test_resume_omission_and_matching_flag_keep_report_mode(self):
        co = self.create()
        co.hold('waiting for the report sequence')
        for flags in ((), ('--review-report',)):
            with self.subTest(flags=flags):
                resumed = rc.Coordinator(self.args(*flags, action='resume'))
                self.assertTrue(resumed.args.review_report)
                self.assertTrue(resumed.args.review_only)
                self.assertTrue(resumed._config()['review_report'])
                with mock.patch.object(resumed, '_drive_loop', return_value='HOLD') as drive:
                    self.assertEqual(resumed.resume(), 'HOLD')
                    drive.assert_called_once_with()
                self.assertTrue(json.loads(self.state_bytes())['config']['review_report'])

    def test_resume_mismatches_are_refused_without_mutation(self):
        co = self.create()
        before = self.state_bytes()
        args = self.args(action='resume')
        args.review_report = False   # explicit false at the Namespace boundary; CLI exposes only the positive flag
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: review_report'):
            rc.Coordinator(args)
        self.assertEqual(self.state_bytes(), before)
        cases = [(('--auto-commit', 'true'), '--review-report refuses --auto-commit true'),
                 (('--stop-after-plan',), '--review-report refuses --stop-after-plan'),
                 (('--polish',), 'report mode refuses resume --polish')]
        for flags, message in cases:
            with self.subTest(flags=flags):
                result = self.run_operator_action('resume', *flags)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('REFUSED: ' + message, result.stdout)
                self.assertEqual(self.state_bytes(), before)
        self.run_dir = self.root / 'ordinary'
        rc.Coordinator(self.args())
        before = self.state_bytes()
        result = self.run_operator_action('resume', '--review-report')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('resume configuration differs: review_report', result.stdout)
        self.assertEqual(self.state_bytes(), before)

    def test_strict_combined_refusals_preserve_efficient_report_state_bytes(self):
        cases = [('reject', ('--strict',), 'report mode refuses note and reject'),
                 ('reject', ('--strict', '--scope-change'), 'report mode refuses note and reject'),
                 ('resume', ('--strict', '--auto-commit', 'true'), '--review-report refuses --auto-commit true')]
        (self.workspace / 'tracked.txt').write_text('changed\n')
        for status in ('ACTIVE', 'HOLD'):
            for index, (action, flags, message) in enumerate(cases):
                with self.subTest(status=status, action=action, flags=flags):
                    self.run_dir = self.root / f'report-{status}-{index}'
                    args = self.args('--review-only', '--review-report')
                    args.safety_mode = 'efficient'
                    co = rc.Coordinator(args)
                    if status == 'HOLD': co.hold('temporary report hold')
                    self.assertEqual(co.state['turns'], [])
                    self.assertEqual(co.state['config']['safety_mode'], 'efficient')
                    before = self.state_bytes()
                    command = self.command(*flags, '--skip-probe')
                    command[2] = action
                    result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn('REFUSED: ' + message, result.stdout)
                    self.assertEqual(self.state_bytes(), before)

    def test_report_mode_refuses_resume_polish(self):   # the a1 dispatch guard is gone: LG2-a2 transition tests
        co = self.create()
        with self.assertRaisesRegex(ValueError, 'report mode refuses resume --polish'):
            co.resume_polish()

    def test_status_and_brief_work_without_workspace_or_workitem(self):
        co = self.create()
        for status in ('HOLD', 'REPORTED'):
            co.state['status'] = status
            co.save()
            before = self.state_bytes()
            command = self.command()   # prepare fake binaries before removing the workspace
            command[2] = 'status'
            moved = self.root / 'removed-workspace'
            self.workspace.rename(moved)
            text = self.workitem.read_text()
            self.workitem.unlink()
            try:
                result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)['status'], status)
                self.assertTrue(json.loads(result.stdout)['config']['review_report'])
                brief = subprocess.run([*command, '--brief', '5'], cwd=self.root, capture_output=True, text=True)
                self.assertEqual(brief.returncode, 0, brief.stdout + brief.stderr)
                self.assertEqual(self.state_bytes(), before)
            finally:
                moved.rename(self.workspace)
                self.workitem.write_text(text)


if __name__ == '__main__':
    unittest.main()
