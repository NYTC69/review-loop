"""LG2-b2: report runs without a test command, the `post` step, and the b1 gate MINORs in review-report.md."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paired_session import review_post, review_report
from paired_session import test_real_coordinator as trc

rc = trc.rc
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
           'fake_codex_cli', 'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action')
FLAGS = ('--review-only', '--review-report', '--lifecycle-mode', 'on')
NO_TEST = (*FLAGS, '--no-test-command')
PREFLIGHT = Path(__file__).resolve().parent.parent / 'scripts' / 'security_preflight.py'
SECRET = 'AKIA' + 'Q7' * 8   # built at runtime: an AWS access key id pattern, no real key


class NoTestReportRunTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def notest(self, *extra, action='run'):
        command = self.command(*extra)
        at = command.index('--test-command')
        del command[at:at + 2]   # no test command at all: not even the parser default reaches a precheck
        command[2] = action
        return command

    def cli(self, *extra, action='run', env=None, skip_probe=True):
        command = self.notest(*extra, action=action) + (['--skip-probe'] if skip_probe else [])
        return subprocess.run(command, cwd=self.root, env={**os.environ, **(env or {})}, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def coordinator(self, *extra, action='run'):
        return rc.Coordinator(rc.parser().parse_args(self.notest(*extra, action=action)[2:]))

    def change(self):
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    # add them\n    return sum(values)\n')

    def test_a_no_test_report_run_reaches_reported_with_static_untested_approvals(self):
        self.change()
        path = os.pathsep.join(p for p in ('/usr/bin', '/bin') if Path(p).is_dir())
        no_npm = shutil.which('npm', path=path) is None and shutil.which('git', path=path) is not None
        result = self.cli(*NO_TEST, env={'PATH': path} if no_npm else None)   # the default `npm test` is never resolved
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertIsNone(state['config']['test_command'])
        self.assertEqual(state['config']['reviewer_command'], [])
        static = [turn for turn in state['turns'] if turn.get('static_untested_approval')]
        self.assertTrue(static)
        self.assertTrue({('reviewer', 'EXEC'), ('gate', 'EXEC')} <= {(turn['role'], turn['phase']) for turn in static})
        text = (self.run_dir / 'review-report.md').read_text()
        self.assertIn('Status: REPORTED (complete)', text)
        self.assertIn(f'- Tests: tests not run; {len(static)} approvals are static and untested', text)
        self.assertIn('(static, untested approval)', text)
        self.assertIn('- specialist code-reviewer: APPROVE', text)   # gate MINOR 3: specialist verdicts are listed
        prompts = [path.read_text() for path in (self.run_dir / 'evidence').glob('*.prompt.txt')]
        self.assertTrue(prompts)
        self.assertFalse([p for p in prompts if 'Run this test command' in p])
        self.assertTrue(all('No test command is configured for this review' in p for p in prompts if 'Phase: SECURITY' not in p))
        resumed = rc.Coordinator(rc.parser().parse_args(self.notest(*FLAGS, action='resume')[2:]))   # omission keeps the null
        self.assertIsNone(resumed.args.test_command)
        self.assertEqual(resumed.reviewer_commands(), [])   # no allow rule from any source, after resume too

    def test_no_test_refusals_and_the_resume_freeze(self):
        self.change()
        with self.assertRaisesRegex(ValueError, '--no-test-command needs --review-report'):
            self.coordinator('--no-test-command')
        self.run_dir = self.root / 'reviewer-command'
        with self.assertRaisesRegex(ValueError, '--no-test-command refuses --reviewer-command'):
            self.coordinator(*NO_TEST, '--reviewer-command', 'python3 audit.py')
        self.run_dir = self.root / 'workitem'
        original = self.workitem.read_text()
        self.workitem.write_text(original + '\n```reviewer-commands\npython3 audit.py\n```\n')
        with self.assertRaisesRegex(ValueError, 'refuses a work item that declares reviewer-commands'):
            self.coordinator(*NO_TEST)
        self.workitem.write_text(original)
        self.run_dir = self.root / 'explicit'
        refused = self.cli(*NO_TEST, '--test-command', 'python3 -m unittest')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('REFUSED: --test-command conflicts with a no-test report run', refused.stdout)
        self.run_dir = self.root / 'frozen'
        self.coordinator(*NO_TEST)
        refused = self.cli(*FLAGS, '--test-command', 'python3 -m unittest', action='resume')
        self.assertIn('REFUSED: --test-command conflicts with a no-test report run', refused.stdout)
        self.run_dir = self.root / 'tested'
        rc.Coordinator(rc.parser().parse_args(self.command(*FLAGS)[2:]))
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: test_command'):
            self.coordinator(*NO_TEST, action='resume')

    def test_a_strict_no_test_report_probe_passes_without_an_allowed_command(self):
        self.change()
        result = self.cli(*NO_TEST, action='permission-probe', skip_probe=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'PASS', report)
        self.assertFalse([reason for reason in report.get('failure_reasons', []) if reason.startswith('not-attempted')])
        prompts = [path.read_text() for path in (self.run_dir / 'evidence').glob('*probe*.prompt.txt')]
        self.assertTrue(prompts)
        self.assertFalse([p for p in prompts if 'Allowed exact command:' in p or '--help > forbidden-test-help' in p])


class PostStepTests(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir, True)
        self.state = {'config': {'review_report': True, 'test_command': None}, 'report': {'complete': True},
                      'review_pr': {'Target repository': 'octo/repo', 'PR URL': 'https://github.com/octo/repo/pull/7',
                                    'Head': 'a' * 40}}
        self.write('# Review report\n\nStatus: REPORTED (complete)\n\n## Critical (0)\n- none\n')

    def write(self, body, **state):
        (self.run_dir / 'state.json').write_text(json.dumps({**self.state, **state}))
        (self.run_dir / 'review-report.md').write_text(body)

    def runner(self, head):
        calls = []

        def run(argv, **_kwargs):
            calls.append(argv)
            out = json.dumps({'headRefOid': head}) if argv[:3] == ['gh', 'pr', 'view'] else ''
            return subprocess.CompletedProcess(argv, 0, out, '')
        return run, calls

    def test_a_planted_secret_refuses_the_post_and_the_report_stays_local(self):
        self.write('# Review report\n\nquoted from the diff:\n' + SECRET + '\n')
        with self.assertRaises(review_post.PostRefused) as caught:
            review_post.prepare(self.run_dir)
        self.assertIn('aws-access-key-id at line 4', str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        run, calls = self.runner('a' * 40)
        with self.assertRaises(review_post.PostRefused):
            review_post.post(self.run_dir, 'any', runner=run)
        self.assertEqual(calls, [])
        self.assertTrue((self.run_dir / 'review-report.md').exists())

    def test_the_single_file_scan_names_rule_and_line_never_the_value(self):
        target = self.run_dir / 'body.md'
        target.write_text('fine\n' + SECRET + '\n')
        found = subprocess.run([sys.executable, str(PREFLIGHT), '--file', str(target)], capture_output=True, text=True)
        self.assertEqual(found.returncode, 1, found.stderr)
        self.assertEqual(json.loads(found.stdout)['findings'], [{'rule': 'aws-access-key-id', 'line': 2}])
        self.assertNotIn(SECRET, found.stdout + found.stderr)
        target.write_text('fine\n')
        clean = subprocess.run([sys.executable, str(PREFLIGHT), '--file', str(target)], capture_output=True, text=True)
        self.assertEqual((clean.returncode, json.loads(clean.stdout)['findings']), (0, []))

    def test_the_confirmation_holds_the_full_body_and_posts_only_with_its_digest(self):
        plan = review_post.prepare(self.run_dir)
        shown = review_post.confirmation(plan)
        self.assertIn((self.run_dir / 'review-report.md').read_text(), shown)
        for part in ('Target repository: octo/repo', 'PR: https://github.com/octo/repo/pull/7 (number 7)',
                     'Reviewed head: ' + 'a' * 40, 'gh pr review https://github.com/octo/repo/pull/7 --comment --body-file',
                     '--confirm ' + plan['digest']):
            self.assertIn(part, shown)
        with mock.patch('builtins.print') as printed:
            self.assertEqual(review_post.main(['--run-dir', str(self.run_dir)]), 0)
        self.assertEqual(printed.call_args[0][0], shown)
        run, calls = self.runner('a' * 40)
        with self.assertRaisesRegex(review_post.PostRefused, 'does not match the body and command shown'):
            review_post.post(self.run_dir, 'wrong', runner=run)
        self.assertEqual(calls, [])
        self.assertEqual(review_post.post(self.run_dir, plan['digest'], runner=run), 'https://github.com/octo/repo/pull/7')
        self.assertEqual(calls[-1], plan['command'])
        self.assertFalse({'--approve', '--request-changes'} & set(calls[-1]))

    def test_a_moved_head_refuses_the_post(self):
        plan = review_post.prepare(self.run_dir)
        run, calls = self.runner('b' * 40)
        with self.assertRaisesRegex(review_post.PostRefused, 'the PR head moved since the review'):
            review_post.post(self.run_dir, plan['digest'], runner=run)
        self.assertEqual([argv[:3] for argv in calls], [['gh', 'pr', 'view']])   # never the review command

    def test_only_a_complete_report_run_with_a_pinned_pr_is_posted(self):
        for state, message in (({'review_pr': {}}, 'no pinned PR URL and head'),
                               ({'report': {'complete': False}}, 'only a complete report run'),
                               ({'config': {'review_report': False}}, 'only a complete report run')):
            with self.subTest(message=message):
                self.write('# Review report\n', **state)
                with self.assertRaisesRegex(review_post.PostRefused, message):
                    review_post.prepare(self.run_dir)


class GateMinorRenderTests(unittest.TestCase):
    def render(self, **state):
        return review_report.render({'report': {'complete': True}, 'turns': [], **state})

    def test_the_skip_reason_names_the_aspects_when_no_specialist_was_selected(self):
        life = {'receipts': [{'stage': 'POLISH-Q', 'skipped': True, 'specialist_turns': []}]}
        text = self.render(lifecycle=life, config={'review_aspects': ['comments'], 'skip_quality_polish': False})
        self.assertIn('- POLISH-Q specialists: skipped (no specialist selected for aspects comments)', text)
        self.assertNotIn('skip_quality_polish', text)
        text = self.render(lifecycle=life, config={'review_aspects': ['comments'], 'skip_quality_polish': True})
        self.assertIn('- POLISH-Q specialists: skipped (skip_quality_polish)', text)

    def test_verdicts_show_the_effective_exec_verdict_and_the_specialists(self):
        life = {'receipts': [{'stage': 'POLISH-Q', 'specialist_turns': [{'name': 'code-reviewer', 'sequence': 5, 'status': 'REVISE'},
                                                                        {'name': 'comment-analyzer', 'sequence': 6, 'status': 'APPROVE'}]}]}
        text = self.render(lifecycle=life, config={}, exec_comparisons=[{'review_sequence': 3, 'persistent': {'verdict': 'APPROVE'}}],
                           review_verdicts=[{'sequence': 3, 'reviewer_raw_verdict': 'APPROVE', 'effective_verdict': 'REVISE'}])
        for line in ('- EXEC reviewer: APPROVE → REVISE', '- specialist code-reviewer: REVISE', '- specialist comment-analyzer: APPROVE'):
            self.assertIn(line, text)
        text = self.render(lifecycle={}, config={}, exec_comparisons=[{'review_sequence': 3, 'persistent': {'verdict': 'REVISE'}}],
                           review_verdicts=[{'sequence': 3, 'effective_verdict': 'REVISE'}])
        self.assertIn('- EXEC reviewer: REVISE\n', text)


if __name__ == '__main__':
    unittest.main()
