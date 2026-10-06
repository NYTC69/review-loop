"""LG2-b1: review-report.md from the ledger, the report-only analyzers and the aspect subset."""
import json
import unittest

from paired_session import review_report
from paired_session import test_real_coordinator as trc
from paired_session import worktree_lifecycle as wl

rc = trc.rc
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
           'fake_codex_cli', 'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action')
FLAGS = ('--review-only', '--review-report', '--lifecycle-mode', 'on')


class SelectionAndRenderTests(unittest.TestCase):
    def test_report_specialists_follow_aspects_paths_and_comment_lines(self):
        self.assertEqual(wl.report_specialists(['a.py']),
                         ('python-reviewer', 'code-reviewer', 'silent-failure-hunter', 'type-design-analyzer', 'pr-test-analyzer'))
        self.assertEqual(wl.report_specialists(['README.md']),
                         ('code-reviewer', 'silent-failure-hunter', 'comment-analyzer', 'pr-test-analyzer'))
        self.assertIn('comment-analyzer', wl.report_specialists(['tool.sh'], comment_lines=True))
        self.assertNotIn('comment-analyzer', wl.report_specialists(['tool.sh']))
        self.assertEqual(wl.report_specialists(['a.py', 'docs/x.md'], aspects=('code',)), ('python-reviewer', 'code-reviewer'))
        self.assertEqual(wl.report_specialists(['a.go'], aspects=('types', 'comments')), ('go-reviewer', 'type-design-analyzer'))

    def test_comment_lines_are_detected_in_added_or_removed_lines_only(self):
        self.assertTrue(wl.touches_comment_lines('+++ b/a.py\n+    # why\n'))
        self.assertTrue(wl.touches_comment_lines('-// gone\n'))
        self.assertFalse(wl.touches_comment_lines('--- a/a.py\n+++ b/a.py\n+x = 1\n # context only\n'))

    def test_render_maps_ledger_severities_and_marks_an_incomplete_report(self):
        ledger = [{'id': 'F001', 'severity': 'CRITICAL', 'source': 'security-preflight', 'phase': 'SECURITY', 'file': 'a.py', 'summary': 'secret', 'status': 'open'},
                  {'id': 'F002', 'severity': 'SECURITY', 'source': 'security-reviewer', 'owner_role': 'security-reviewer', 'phase': 'SECURITY', 'file': 'b.py', 'summary': 'sqli', 'status': 'open'},
                  {'id': 'F003', 'severity': 'MAJOR', 'source': 'specialist:code-reviewer', 'owner_role': 'specialist:code-reviewer', 'phase': 'POLISH-Q', 'file': 'c.py', 'summary': 'bug', 'status': 'open'},
                  {'id': 'F004', 'severity': 'MINOR', 'source': 'fresh-shadow', 'phase': 'EXEC', 'file': '', 'summary': 'nit', 'status': 'open'},
                  {'id': 'F005', 'severity': 'MAJOR', 'source': 'adversarial-gate', 'phase': 'EXEC', 'file': 'd.py', 'summary': 'gone', 'status': 'withdrawn'}]
        state = {'status': 'REPORTED', 'report': {'complete': True}, 'finding_ledger': ledger, 'turns': [
            {'role': 'reviewer', 'phase': 'EXEC', 'sequence': 1, 'observed_tool_calls': [1, 2]}, {'role': 'probe', 'phase': 'PROBE', 'sequence': 2},
            {'role': 'reviewer', 'phase': 'POLISH-Q', 'sequence': 3, 'observed_tool_calls': [1]},
            {'role': 'reviewer', 'phase': 'POLISH-Q', 'sequence': 4, 'observed_tool_calls': [1, 2, 3]},
            {'role': 'reviewer', 'phase': 'POLISH-Q', 'sequence': 5, 'discarded': 'no tool calls'}],
            'spawn_failures': [{'role': 'reviewer', 'phase': 'POLISH-Q', 'sequence': 6, 'error': 'OSError: no such file'}],
            'lifecycle': {'specialist_sequences': {'5': 'type-design-analyzer', '6': 'type-design-analyzer'}, 'receipts': [{'stage': 'POLISH-Q', 'specialist_turns': [{'name': 'code-reviewer', 'sequence': 3},
                                                                                  {'name': 'comment-analyzer', 'sequence': 4}]},
                                       {'stage': 'SECURITY', 'review': {'status': 'REQUEST_CHANGES'}}]},
            'exec_comparisons': [{'persistent': {'verdict': 'REVISE'}, 'shadow': {'verdict': 'APPROVE'}, 'gate': {'verdict': 'needs-attention'}}],
            'config': {'review_base': 'abc', 'test_command': None}, 'review_only': {'candidate_tree_sha256': 'tree1'}}
        text = review_report.render(state)
        for line in ('Status: REPORTED (complete)', '## Critical (1)', '- **F001** [security preflight, SECURITY] `a.py`: secret',
                     '## Security (1)', '- **F002** [security reviewer, SECURITY]', '## Important (1)',
                     '- **F003** [specialist code-reviewer, POLISH-Q] `c.py`: bug', '## Suggestions (1)', '`-`: nit',
                     '- Tests: tests not run', '- EXEC reviewer (EXEC): 2 tool calls', '- specialist code-reviewer (POLISH-Q): 1 tool calls',
                     '- specialist comment-analyzer (POLISH-Q): 3 tool calls', '- specialist type-design-analyzer (POLISH-Q): failed to start: OSError: no such file',
                     '- specialist type-design-analyzer (POLISH-Q): 0 tool calls (discarded: no tool calls)',
                     '- EXEC reviewer: REVISE', '- shadow: APPROVE', '- gate: needs-attention', '- security reviewer: REQUEST_CHANGES', '- Reviewed tree: `tree1`', '## Recommended Actions'):
            self.assertIn(line, text)
        self.assertNotIn('F005', text)   # withdrawn
        self.assertNotIn('probe (', text)
        state['report'] = {'complete': False, 'hold_reason': 'invocation limit reached', 'not_completed': ['SECURITY']}
        text = review_report.render(state)
        self.assertIn('Status: incomplete, the run is on HOLD: invocation limit reached', text)
        self.assertIn('Not completed: SECURITY', text)


class AspectAndReportRunTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def args(self, *extra, action='run'):
        command = self.command(*FLAGS, *extra)
        command[2] = action
        return rc.parser().parse_args(command[2:])

    def change(self):
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    # add them\n    return sum(values)\n')
        (self.workspace / 'NOTES.md').write_text('notes\n')

    def test_aspects_are_frozen_and_validated(self):
        self.change()
        co = rc.Coordinator(self.args('--aspects', 'tests,code'))
        self.assertEqual(co.state['config']['review_aspects'], ['code', 'tests'])   # canonical order
        self.assertEqual(rc.Coordinator(self.args(action='resume')).args.review_aspects, ['code', 'tests'])
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: review_aspects'):
            rc.Coordinator(self.args('--aspects', 'code', action='resume'))
        self.run_dir = self.root / 'default'
        self.assertEqual(rc.Coordinator(self.args()).state['config']['review_aspects'], list(wl.REPORT_ASPECTS))
        for flags, message in ((FLAGS + ('--aspects', 'code,style'), '--aspects takes a comma list'),
                               (('--aspects', 'code'), '--aspects needs --review-report')):
            with self.subTest(flags=flags):
                self.run_dir = self.root / ('bad-' + flags[-1].replace(',', '-'))
                command = self.command(*flags)
                with self.assertRaisesRegex(ValueError, message):
                    rc.Coordinator(rc.parser().parse_args(command[2:]))

    def test_a_report_run_writes_review_report_md_with_the_selected_analyzers(self):
        self.change()
        result = self.run_coordinator(*FLAGS, env={'FAKE_SPECIALIST_BLOCK': 'code-reviewer'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        [polish] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'POLISH-Q']
        self.assertEqual(polish['specialists'], list(wl.report_specialists(['NOTES.md', 'sum_ints.py'], comment_lines=True)))
        self.assertIn('comment-analyzer', polish['specialists'])
        self.assertIn('type-design-analyzer', polish['specialists'])
        text = (self.run_dir / 'review-report.md').read_text()
        self.assertIn('Status: REPORTED (complete)', text)
        self.assertIn('[specialist code-reviewer, POLISH-Q]', text)
        for name in polish['specialists']:   # each specialist's own turn, by name
            self.assertIn(f'- specialist {name} (POLISH-Q): ', text)
        self.assertIn('## Verdicts', text)
        self.assertEqual(text, review_report.render(state))

    def test_a_failed_specialist_dispatch_keeps_its_name(self):
        self.change()
        co = rc.Coordinator(self.args())

        def failed_dispatch(*_args, **_kwargs):
            co.state['sequence'] += 2   # a re-dispatch inside invoke, then a failure
            raise RuntimeError('launcher failed')
        from unittest import mock
        with mock.patch.object(co, 'invoke', side_effect=failed_dispatch), self.assertRaises(RuntimeError):
            co._specialist_turn('comment-analyzer', ['NOTES.md'], rc.git_snapshot(self.workspace)[0])
        last = co.state['sequence']
        self.assertEqual(co.state['lifecycle']['specialist_sequences'], {str(last - 1): 'comment-analyzer', str(last): 'comment-analyzer'})

    def test_a_hold_writes_an_incomplete_report(self):
        self.change()
        co = rc.Coordinator(self.args('--aspects', 'code'))
        co.hold('stopped for the test')
        text = (self.run_dir / 'review-report.md').read_text()
        self.assertIn('Status: incomplete, the run is on HOLD: stopped for the test', text)
        self.assertIn('Not completed: EXEC reviewer and shadow, adversarial gate, POLISH-Q specialists, SECURITY', text)


if __name__ == '__main__':
    unittest.main()
