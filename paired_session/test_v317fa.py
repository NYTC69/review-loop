"""V317-FA: ordinary Git names, useful report details and stable operator waivers."""
import contextlib
import copy
import io
import json
import os
import shlex
import subprocess
import unittest
from unittest.mock import patch

from paired_session import review_report
from paired_session import test_worktree_lifecycle as twl
from paired_session import test_operator_roles as tor
from paired_session import test_field31 as f31

rc = twl.rc


class FilenameAndReportTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})

    def names_run(self, *flags):
        tracked = self.workspace / '既存.md'
        tracked.write_text('before\n')
        # Fixture setup writes only the disposable test repository.
        subprocess.run(['git', 'add', '--', tracked.name], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'test unicode tracked name'], cwd=self.workspace, check=True)
        tracked.write_text('tracked unicode changed\n')
        names = ('docs/日本語.md', '文档.md', 'with space.md', 'with"quote.md')
        for name in names:
            path = self.workspace / name
            path.parent.mkdir(exist_ok=True)
            path.write_text('new content for ' + name + '\n')
        done = self.run_coordinator(*flags, '--max-invocations', '60')
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        delta = (self.run_dir / 'context' / 'delta.patch').read_text()
        stat = (self.run_dir / 'context' / 'delta.stat').read_text()
        for name in names:
            self.assertIn('+new content for ' + name, delta)
            self.assertIn('UNTRACKED ' + name, stat)
        self.assertIn('+tracked unicode changed', delta)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE')
        self.assertGreater(state['reviews_completed'], 0)

    def test_names_full_lifecycle(self):
        self.names_run('--lifecycle-mode', 'on')

    def test_names_stop_after_gate(self):
        self.names_run('--lifecycle-mode', 'off')

    def test_names_review_only(self):
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        self.names_run('--review-only', '--lifecycle-mode', 'on')

    def test_staged_refusal_names_are_not_quoted_or_split(self):
        co = self.coordinator('--lifecycle-mode', 'off')
        names = ('文档.md', 'with space.md', 'with"quote.md')
        for name in names:
            (self.workspace / name).write_text('staged content\n')
        subprocess.run(['git', 'add', '--', *names], cwd=self.workspace, check=True)
        with self.assertRaises(ValueError) as caught:
            co._commit_refusals([])
        for name in names:
            self.assertIn(name, str(caught.exception))

    def test_linked_worktree_with_unicode_and_quotes_is_recognized(self):
        from paired_session import candidate_tree as ct
        linked = self.root / '日本語 " linked'
        subprocess.run(['git', 'worktree', 'add', '--detach', str(linked), 'HEAD'],
                       cwd=self.workspace, check=True, capture_output=True)
        scratch = self.root / 'candidate'; scratch.mkdir()
        with self.assertRaisesRegex(ct.CandidateError, 'outside workspace/worktrees'):
            ct.prepare_candidate_baseline(self.workspace, self.run_dir, linked, scratch, ('tracked.txt',))

    def test_report_run_keeps_gate_and_reviewer_explanations(self):
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        done = self.run_coordinator('--review-only', '--review-report', '--lifecycle-mode', 'on',
                                    env={'FAKE_GATE_MINOR': '1', 'FAKE_EXEC_APPROVE_MINOR': '1'})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'REPORTED')
        gate = next(row for row in state['finding_ledger'] if row['source'] == 'adversarial-gate')
        self.assertEqual((gate['line_start'], gate['line_end']), (1, 3))
        text = (self.run_dir / 'review-report.md').read_text()
        self.assertIn('`sum_ints.py:1-3`: simplify the helper\n  Non-blocking cleanup suggested by the fake gate.', text)
        self.assertIn('`sum_ints.py`: advisory naming\n  the name hides the intent', text)


class HistoryFilenameTests(unittest.TestCase):
    locals().update({name: getattr(f31.AuthorDeltaHistoryTests, name) for name in ('setUp', 'invoke', 'review', 'scan_rows')})

    def test_history_diagnostic_names_quoted_git_paths(self):
        name = 'web/日本語 " list.test.js'
        self.t.write(name, f31.WITH_IDS)
        self.review(shadow_scans=True)
        [row] = self.scan_rows()
        self.assertEqual(row['file'], name + ':1')
        self.assertIn(name + ':2', row['summary'])


class RenderingTests(unittest.TestCase):
    def test_merged_rows_keep_each_range_and_explanation_without_duplicate_text(self):
        rows = [{'id': 'F001', 'source': 'adversarial-gate', 'phase': 'EXEC', 'severity': 'HIGH', 'file': 'a.py',
                 'line_start': 2, 'line_end': 4, 'summary': 'reject bool input', 'body': 'Boolean values silently change the sum.'},
                {'id': 'F002', 'source': 'persistent-reviewer', 'phase': 'EXEC', 'severity': 'HIGH', 'file': 'a.py',
                 'line_start': 8, 'line_end': 9, 'summary': 'reject bool input', 'failure_scenario': 'The public caller passes True.'}]
        text = review_report.render({'finding_ledger': rows})
        self.assertIn('`a.py:2-4`', text)
        self.assertIn('`a.py:8-9`', text)
        self.assertIn('  Boolean values silently change the sum.', text)
        self.assertIn('    The public caller passes True.', text)
        rows[0]['body'] = rows[0]['summary']
        rows[1]['failure_scenario'] = rows[1]['summary']
        self.assertEqual(review_report.render({'finding_ledger': rows}).count('reject bool input'), 2)

    def test_scanner_merge_requires_same_path_line_rule_and_keeps_ledger(self):
        scan = {'id': 'F001', 'source': 'secret-scan', 'phase': 'EXEC', 'severity': 'SECURITY', 'file': 'a.py:2',
                'summary': 'the change adds a hardcoded credential at a.py:2 (jwt)'}
        preflight = {'id': 'F002', 'source': 'security-preflight', 'phase': 'SECURITY', 'severity': 'CRITICAL',
                     'file': 'a.py', 'summary': '[class: secret] jwt in a.py:2'}
        rows = [scan, preflight]
        before = copy.deepcopy(rows)
        text = review_report.render({'finding_ledger': rows})
        self.assertEqual(rows, before)
        self.assertEqual(sum(line.startswith('- **') for line in text.splitlines()), 1)
        self.assertIn('security preflight, SECURITY; secret-scan, EXEC', text)
        for summary in ('[class: secret] jwt in b.py:2', '[class: secret] jwt in a.py:3', '[class: secret] private-key in a.py:2'):
            with self.subTest(summary=summary):
                text = review_report.render({'finding_ledger': [scan, dict(preflight, summary=summary)]})
                self.assertEqual(sum(line.startswith('- **') for line in text.splitlines()), 2)


class RejectWaiverTests(unittest.TestCase):
    locals().update({name: getattr(tor.CodexContractTests, name) for name in ('setUp', 'cli', 'recorded_override')})

    def reject_with_waiver(self, mode):
        if mode == 'on':
            (self.h.workspace / '.gitignore').write_text(twl.COVERING_GITIGNORE)
            subprocess.run(['git', 'add', '--', '.gitignore'], cwd=self.h.workspace, check=True)
            subprocess.run(['git', 'commit', '-qm', 'test ignore coverage'], cwd=self.h.workspace, check=True)
        flags = ('--lifecycle-mode', mode, '--max-invocations', '60', '--accept-unverified-codex-cli', '--reason', 'checked by hand')
        with patch.object(rc.Coordinator, 'probe_gate', return_value=(True, '')):
            done = self.cli('run', tor.UNVERIFIED, *flags)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn(twl.DONE, done.stdout)
        before = json.loads((self.h.run_dir / 'state.json').read_text())
        issued = self.cli('reject', tor.UNVERIFIED, *flags, '--text', 'Also reject floats.', '--intent-only')
        self.assertEqual(issued.returncode, 0, issued.stdout)
        argv = shlex.split(json.loads(issued.stdout)['next_command'])
        self.assertIn('--accept-unverified-codex-cli', argv)
        out = io.StringIO()
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': tor.UNVERIFIED}), \
                patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), \
                patch.object(rc.Coordinator, 'probe_gate', return_value=(True, '')), contextlib.redirect_stdout(out):
            code = rc.main(argv[1:])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn(twl.DONE, out.getvalue())
        after = json.loads((self.h.run_dir / 'state.json').read_text())
        self.assertEqual(after['codex_cli_override'], before['codex_cli_override'])
        self.assertEqual(after.get('codex_cli_override_history', []), before.get('codex_cli_override_history', []))
        self.assertEqual(len(after['rejections']), 1)
    def test_printed_next_reject_with_same_waiver_preserves_intent_and_history(self):
        self.reject_with_waiver('off')

    def test_reject_with_same_waiver_on_full_lifecycle(self):
        self.reject_with_waiver('on')


if __name__ == '__main__':
    unittest.main()
