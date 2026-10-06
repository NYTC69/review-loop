"""FIELD-25 (poker-tools WI-BOB-UI, review-only on v2.12.1): the accept next command, the files an accept leaves
uncommitted, the untracked files in a review-only scope, and an open finding re-reported by a later reviewer."""
import json
from pathlib import Path
import shlex
import subprocess
import unittest

from paired_session import test_review_only_entry as roe
from paired_session import worktree_lifecycle as wl
from paired_session.test_worktree_lifecycle import DONE

rc = roe.rc
F003 = ('The tie-break in numberHandsByTimestamp compares hand_id by JavaScript code units. The server orders by '
        '`h.end_timestamp, h.hand_id` under the database collation, so the two orders could disagree.')


class Field25Tests(unittest.TestCase):
    locals().update({name: getattr(roe.ReviewOnlyEntryTests, name) for name in (*roe._HELPERS, 'git', 'change', 'state')})

    def run_cli(self, argv):
        return subprocess.run(argv, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_review_only_start_and_accept_name_the_untracked_files_and_the_next_command(self):   # items 1-3
        self.change()
        stray = self.workspace / '.compass' / 'CHECKPOINT.md'
        stray.parent.mkdir()
        stray.write_text('session notes\n')
        done = self.run_coordinator('--review-only')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        self.assertIn('2 untracked file(s) in the review scope: .compass/CHECKPOINT.md, sum_ints.py', done.stdout)
        self.assertIn('add it to .gitignore or remove it, then start a new run', done.stdout)
        self.assertEqual(self.state()['review_only']['untracked_at_start'], ['.compass/CHECKPOINT.md', 'sum_ints.py'])
        bare = self.command('--review-only', '--skip-probe', '--reason', 'looks good')
        bare[2] = 'accept'
        refused = self.run_cli(bare)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('intent is stale or missing: accept needs --expect', refused.stdout)
        issued = self.run_cli([*bare, '--intent-only'])
        self.assertEqual(issued.returncode, 0, issued.stdout + issued.stderr)
        intent = json.loads(issued.stdout)
        self.assertIn('NEXT: ' + intent['next_command'], issued.stderr)
        self.assertIn('run: ' + intent['next_command'], refused.stdout)   # the bare refusal names the same command
        argv = shlex.split(intent['next_command'])
        self.assertEqual((argv[-2:], '--intent-only' in argv, argv[argv.index('--reason') + 1]),
                         (['--expect', intent['digest']], False, 'looks good'))
        accepted = self.run_cli(argv)
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        listed = 'tracked.txt, untracked: .compass/CHECKPOINT.md, untracked: sum_ints.py'
        self.assertIn('UNCOMMITTED: no commit was made (auto_commit off); commit these yourself: ' + listed, accepted.stdout)
        self.assertIn('Uncommitted after acceptance (auto_commit off): ' + listed,
                      (self.run_dir / 'review-comparison.md').read_text())

    def test_the_w_delivery_report_lists_the_uncommitted_files(self):   # item 2, lifecycle on with auto_commit off
        state = {'lifecycle': {'receipts': [], 'epoch': 1}, 'finding_ledger': [], 'started_at': 0, 'completed_at': 60,
                 'config': {'review_base': 'b' * 40}, 'review_only': {'head_at_start': 'b' * 40}, 'invocations_used': 5,
                 'acceptance': {'uncommitted': ['tracked.txt', 'untracked: new_test.py']}}
        report = wl.delivery_report(state, 'run-01', '# item\n', {'auto_commit': False, 'reviewed_commits': []})
        self.assertIn('- 未提交的文件（请自行提交）：tracked.txt、untracked: new_test.py', report)
        del state['acceptance']
        self.assertNotIn('未提交的文件', wl.delivery_report(state, 'run-01', '# item\n', {'auto_commit': False}))

    def test_an_open_finding_re_reported_by_the_polish_reviewer_keeps_its_id(self):   # item 4, the field F003/F006 shape
        co = self.coordinator()
        finding = {'severity': 'LOW', 'file': 'shared/js/hand-renderer.js', 'summary': F003, 'failure_scenario': 'ties'}
        [first] = co.record_findings('fresh-shadow', 'EXEC', 2, [finding])
        self.assertEqual(first['id'], 'F001')
        again = {**finding, 'summary': 'F001 (still open, advisory): numberHandsByTimestamp breaks ties by code-unit order.'}
        verbatim = {**finding, 'summary': '[class: ordering] ' + F003.upper().replace(' ', '  ')}
        recorded = co.record_findings('persistent-reviewer', 'POLISH', 5, [again, verbatim])
        self.assertEqual([row['id'] for row in recorded], ['F001', 'F001'])
        [row] = co.state['finding_ledger']
        self.assertEqual((row['status'], co.state['next_finding_id']), ('open', 2))
        self.assertIn('re-reported by persistent-reviewer in POLISH', row['status_history'][-1]['evidence'])
        others = [{**again, 'file': 'shared/js/other.js'}, {**again, 'severity': 'MEDIUM'},
                  {**again, 'summary': 'F0010 is a different claim.'}, {**again, 'security': True},
                  {**again, 'summary': 'Unlike F001, hands without a timestamp are left unnumbered.'}]
        self.assertEqual([r['id'] for r in co.record_findings('persistent-reviewer', 'POLISH', 6, others)],
                         ['F002', 'F003', 'F004', 'F005', 'F006'])   # a cross-reference or a security escalation is new
        self.assertEqual(co.record_findings('specialist:code-reviewer', 'POLISH-Q', 7, [again])[0]['id'], 'F007')
        self.assertEqual(co.record_findings('adversarial-gate', 'EXEC', 8, [verbatim])[0]['id'], 'F008')
        row['status'] = 'fixed'
        self.assertEqual(co.record_findings('persistent-reviewer', 'POLISH', 9, [again])[0]['id'], 'F009')

    def test_a_review_only_w_accept_without_auto_commit_lists_the_uncommitted_files(self):   # item 2, lifecycle on
        roe.ReviewOnlyEntryTests.w_ready(self)
        self.change()
        w = ('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '60', '--auto-commit', 'false')   # explicit off wins
        done = self.run_coordinator(*w)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        self.assertIs(self.state()['config']['auto_commit'], False)
        accepted = self.run_operator_action('accept', *w)
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        self.assertIn('commit these yourself: tracked.txt, untracked: sum_ints.py', accepted.stdout)
        self.assertIn('未提交的文件（请自行提交）：tracked.txt、untracked: sum_ints.py',
                      (self.run_dir / 'delivery-report.md').read_text())

    def test_a_review_only_w_run_commits_by_default_and_names_the_commit(self):   # owner decision (a), 2026-10-06
        roe.ReviewOnlyEntryTests.w_ready(self)
        self.change()
        parent = self.git('rev-parse', 'HEAD')
        w = ('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '60')   # no --auto-commit
        done = self.run_coordinator(*w)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        self.assertIs(self.state()['config']['auto_commit'], True)
        accepted = self.run_operator_action('accept', *w)
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        commit = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.git('rev-parse', 'HEAD~1'), parent)
        self.assertIn(f'COMMIT: {commit} (auto_commit, parent {parent[:12]}); not pushed; '
                      'untracked files committed: sum_ints.py', accepted.stdout)   # FIELD-25 gate MINOR
        self.assertNotIn('UNCOMMITTED', accepted.stdout)
        self.assertIn('auto_commit 一并提交的未跟踪文件：sum_ints.py', (self.run_dir / 'delivery-report.md').read_text())

    def test_the_default_source_is_frozen_and_re_derived_by_a_successor(self):   # FIELD-25 gate
        self.change()
        w = ('--review-only', '--lifecycle-mode', 'on')
        parent_dir = self.run_dir
        parent = rc.Coordinator(rc.parser().parse_args(self.command(*w)[2:]))
        self.assertEqual((parent.state['config']['auto_commit'], parent.state['config']['auto_commit_source']),
                         (True, 'review-only-default'))
        resumed = rc.Coordinator(rc.parser().parse_args(['resume', *self.command(*w)[3:]]))
        self.assertEqual(resumed.args.auto_commit_source, 'review-only-default')   # kept on resume, no config difference
        parent.args.action = 'note'   # an ACTIVE run's scope change
        start = parent.scope_change('Also reject floats.', None).split('Start: ', 1)[1].split()
        option = lambda name: start[start.index(name) + 1]
        self.assertNotIn('auto_commit', json.loads(Path(option('--config')).read_text()))   # re-derived, not made explicit
        self.run_dir, self.workitem = Path(option('--run-dir')), Path(option('--workitem'))
        argv = self.command('--lifecycle-mode', 'on', '--config', option('--config'), '--supersedes', str(parent_dir))[2:]
        child = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv)).state['config']
        self.assertEqual((child['review_only'], child['auto_commit'], child['auto_commit_source']), (True, True, 'review-only-default'))
        argv = self.command(*w, '--auto-commit', 'true', '--run-dir', str(self.root / 'explicit'))[2:]   # the last --run-dir wins
        self.assertNotIn('auto_commit_source', rc.Coordinator(rc.parser().parse_args(argv)).state['config'])

    def test_a_defaulted_auto_commit_the_checks_refuse_delivers_uncommitted(self):   # FIELD-25 gate BLOCKER
        roe.ReviewOnlyEntryTests.w_ready(self)
        (self.workspace / '.gitattributes').write_text('* text=auto\n')
        self.git('add', '.gitattributes')
        self.git('commit', '-qm', 'normalize line endings')
        self.change()
        parent = self.git('rev-parse', 'HEAD')
        runs = {'default': (), 'explicit': ('--auto-commit', 'true')}
        for name, extra in runs.items():
            with self.subTest(name):
                self.run_dir = self.root / name
                w = ('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '60', *extra)
                done = self.run_coordinator(*w)
                self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
                self.assertEqual(self.state()['config'].get('auto_commit_source'),
                                 'review-only-default' if name == 'default' else None)
                accepted = self.run_operator_action('accept', *w)
                self.assertEqual(self.git('rev-parse', 'HEAD'), parent)   # no commit either way
                if name == 'explicit':   # the owner's own choice keeps refusing, as before
                    self.assertIn('REFUSED: auto_commit refuses content-transforming attributes', accepted.stdout)
                    continue
                self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
                self.assertIn('UNCOMMITTED: no commit was made (auto_commit skipped: auto_commit refuses content-transforming '
                              'attributes: ', accepted.stdout)
                self.assertIn('commit these yourself: tracked.txt, untracked: sum_ints.py', accepted.stdout)
                self.assertIn('auto_commit 已跳过（auto_commit refuses content-transforming attributes',
                              (self.run_dir / 'delivery-report.md').read_text())
                self.assertIn('Uncommitted after acceptance (auto_commit skipped: ', (self.run_dir / 'review-comparison.md').read_text())

    def test_the_auto_commit_default_by_entry(self):   # owner decision (a): explicit values win; other runs unchanged
        self.change()
        profile = self.root / 'operator-profile.json'
        profile.write_text(json.dumps({'auto_commit': False}))
        cases = [(('--review-only', '--lifecycle-mode', 'on'), True),
                 (('--review-only', '--lifecycle-mode', 'on', '--auto-commit', 'false'), False),
                 (('--review-only', '--lifecycle-mode', 'on', '--config', str(profile)), False),
                 (('--review-only',), False),   # lifecycle off never commits (the uncommitted list covers it)
                 (('--lifecycle-mode', 'on'), False), ((), False), (('--auto-commit', 'true', '--lifecycle-mode', 'on'), True)]
        for index, (extra, expected) in enumerate(cases):
            with self.subTest(extra=extra):
                argv = self.command(*extra)[2:]
                argv[argv.index('--run-dir') + 1] = str(self.root / f'run-{index}')
                args = rc.configure_parser(rc.parser(), argv).parse_args(argv)
                self.assertIs(rc.Coordinator(args).state['config']['auto_commit'], expected)


if __name__ == '__main__':
    unittest.main()
