"""D-LG1 review-only entry, LG1-a1 (docs/review-only-entry.md §1-§2, §8 tests 1, 3, 5, 11a): `run --review-only` starts at
the EXEC review of the existing change, with its refusals, frozen values and tree check."""
import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action', 'coordinator')
SUM_INTS = 'def sum_ints(values):\n    return sum(values)\n'


class ReviewOnlyEntryTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.workspace, check=True, capture_output=True, text=True).stdout.strip()

    def args(self, *extra, action='run'):
        command = self.command('--review-only', *extra)
        command[2] = action
        return rc.parser().parse_args(command[2:])

    def change(self):   # the existing work under review: a new file and an edit of a tracked one
        (self.workspace / 'sum_ints.py').write_text(SUM_INTS)
        (self.workspace / 'tracked.txt').write_text('base\nedited\n')

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    # --- test 1: creation ----------------------------------------------------------------------------------------------------
    def test_refusals_leave_no_state(self):
        foreign = self.git('commit-tree', self.git('rev-parse', 'HEAD^{tree}'), '-m', 'not an ancestor')
        cases = [((), 'nothing to review: the workspace tree equals the review base'),
                 (('--base', 'no-such-ref'), '--base no-such-ref does not name a commit'),
                 (('--base', foreign), 'is not an ancestor of HEAD'),
                 (('--stop-after-plan',), 'has no PLAN phase'),
                 (('--lifecycle-mode', 'on'), 'runs with --lifecycle-mode off until'),
                 (('--supersedes', str(self.root / 'parent')), 'cannot start a scope-change successor yet')]
        for extra, message in cases:
            with self.subTest(extra=extra):
                if extra:
                    self.change()
                with self.assertRaisesRegex(ValueError, message):
                    rc.Coordinator(self.args(*extra))
                self.assertFalse((self.run_dir / 'state.json').exists())
        with self.assertRaisesRegex(ValueError, '--base needs --review-only'):
            rc.Coordinator(rc.parser().parse_args([*self.command('--base', 'HEAD')[2:]]))
        self.git('add', 'tracked.txt')
        (self.workspace / 'tracked.txt').write_text('base\nedited again\n')   # staged, then changed again
        with self.assertRaisesRegex(ValueError, 'refuses partially staged paths .*: tracked.txt'):
            rc.Coordinator(self.args())
        self.git('add', 'tracked.txt')
        blob = self.git('rev-parse', ':tracked.txt')
        subprocess.run(['git', 'update-index', '--index-info'], cwd=self.workspace, check=True, text=True,
                       input=f'0 {"0" * 40}\ttracked.txt\n100644 {blob} 1\ttracked.txt\n100644 {blob} 2\ttracked.txt\n')
        with self.assertRaisesRegex(ValueError, 'unmerged entries'):
            rc.Coordinator(self.args())
        self.git('reset', '-q')
        self.git('mv', 'tracked.txt', 'moved.txt')   # a staged rename the worktree undoes: the index differs from both
        (self.workspace / 'moved.txt').rename(self.workspace / 'tracked.txt')
        with self.assertRaisesRegex(ValueError, 'refuses partially staged paths .*moved.txt'):
            rc.Coordinator(self.args())
        self.git('reset', '-q')
        self.workitem.write_text('# Toy\nRecheck F001 from the earlier review.\n')   # FIELD-11, checked before any state
        with self.assertRaisesRegex(ValueError, 'refuses review history in the work item: ledger-id-shaped tokens F001'):
            rc.Coordinator(self.args())
        self.assertFalse((self.run_dir / 'state.json').exists())

    def test_the_start_is_frozen_and_the_first_dispatch_is_the_exec_reviewer(self):
        self.change()
        before, head = rc.git_snapshot(self.workspace)[0], self.git('rev-parse', 'HEAD')
        co = rc.Coordinator(self.args())
        state = co.state
        record = state['review_only']
        self.assertEqual((state['config']['review_only'], state['config']['review_base'], state['base_commit']), (True, head, head))
        self.assertEqual((state['phase'], state['next'], state['exec_rounds'], state['plan_rounds']), ('EXEC', 'reviewer', 1, 0))
        self.assertEqual((record['candidate_tree_sha256'], record['head_at_start'], record['plan_skipped']), (before, head, True))
        listing = subprocess.run(['git', 'ls-files', '-s', '-z'], cwd=self.workspace, check=True, capture_output=True).stdout
        self.assertEqual(record['index_at_start'], hashlib.sha256(listing).hexdigest())
        scope = (co.context / 'plan.md').read_text()
        self.assertEqual(record['review_scope_sha256'], hashlib.sha256(scope.encode()).hexdigest())
        for needle in ('No plan was drafted or approved; review the change itself', f'Review base: {head}',
                       'Create sum_ints; reject booleans.', 'M\ttracked.txt\n', 'untracked: sum_ints.py'):
            self.assertIn(needle, scope)   # Q7: everything the run will review is listed
        mirror = Path(record['mirror'])
        self.assertEqual(((mirror / 'sum_ints.py').read_text(), (mirror / 'tracked.txt').read_text()), (SUM_INTS, 'base\nedited\n'))
        long = 'a/' + 'deeply-nested-directory-name/' * 6 + 'file-with-a-long-name.txt'   # --stat would shorten it
        (self.workspace / long).parent.mkdir(parents=True)
        (self.workspace / long).write_text('x\n')
        self.git('add', long)
        self.git('commit', '-qm', 'long path')
        self.git('mv', long, 'renamed.txt')
        self.git('commit', '-qm', 'rename')   # a rename between the base and the tree is listed as both full paths
        self.run_dir = self.root / 'renames'
        scope = (rc.Coordinator(self.args('--base', 'HEAD~1')).context / 'plan.md').read_text()
        self.assertIn(f'D\t{long}\n', scope)
        self.assertIn('A\trenamed.txt\n', scope)
        default = rc.Coordinator(rc.parser().parse_args(self.command()[2:] + ['--run-dir', str(self.root / 'default')]))
        self.assertNotIn('review_only', default.state['config'])   # a default run is unchanged
        self.assertEqual((default.state['phase'], default.state['exec_rounds']), ('PLAN', 0))

    # --- test 3: the tree before the first review --------------------------------------------------------------------------
    def test_a_tree_change_before_the_first_review_holds_until_restored(self):
        self.change()
        mirror = Path(rc.Coordinator(self.args()).state['review_only']['mirror'])
        (self.workspace / 'sum_ints.py').write_text(SUM_INTS + '# drifted\n')
        held = self.run_operator_action('resume', '--review-only')
        self.assertIn('the tree changed before the first review; restore it from', held.stdout, held.stdout + held.stderr)
        self.assertEqual([row for row in self.state()['turns'] if row['phase'] == 'EXEC'], [])   # nothing dispatched
        shutil.copy2(mirror / 'sum_ints.py', self.workspace / 'sum_ints.py')   # the operator restores the frozen tree
        plan = self.run_dir / 'context' / 'plan.md'
        frozen = plan.read_text()
        plan.write_text(frozen + 'Also review the deploy scripts.\n')   # the scope is frozen too
        tampered = self.run_operator_action('resume')
        self.assertIn('the review scope (context/plan.md) changed before the first review', tampered.stdout)
        plan.write_text(frozen)
        done = self.run_operator_action('resume')
        self.assertIn('DONE', done.stdout, done.stdout + done.stderr)
        turns = [(row['role'], row['phase']) for row in self.state()['turns']]
        self.assertEqual(turns[0], ('reviewer', 'EXEC'))   # no PLAN turn and no author turn before the first review
        self.assertNotIn(('author', 'PLAN'), turns)

    # --- test 5: round caps -------------------------------------------------------------------------------------------------
    def test_max_exec_rounds_counts_reviews_of_the_existing_change(self):
        for rounds, reviews in ((1, 1), (3, 3)):
            with self.subTest(rounds=rounds):
                self.run_dir = self.root / f'rounds-{rounds}'
                self.change()
                held = self.run_coordinator('--review-only', '--max-exec-rounds', str(rounds), '--adversarial-gate', 'off',
                                            env={'FAKE_EXEC_MIXED_REVISE': '1'})
                self.assertIn('EXEC round limit reached', held.stdout, held.stdout + held.stderr)
                state = self.state()
                self.assertEqual(sum(row['role'] == 'reviewer' and row['phase'] == 'EXEC' for row in state['turns']), reviews)
                self.assertEqual(sum(row['role'] == 'author' for row in state['turns']), reviews - 1)

    # --- test 11a: resume keeps the frozen values ---------------------------------------------------------------------------
    def test_a_resume_keeps_the_frozen_entry_and_refuses_another(self):
        self.change()
        rc.Coordinator(self.args())
        self.git('add', '-A')
        self.git('commit', '-qm', 'later work')   # HEAD moves: an omitted --base keeps the frozen one, a new one is refused
        kept = rc.Coordinator(self.args(action='resume'))
        self.assertEqual((kept.args.review_only, kept.args.review_base), (True, self.git('rev-parse', 'HEAD~1')))
        omitted = rc.Coordinator(rc.parser().parse_args(['resume', *self.command()[3:]]))
        self.assertTrue(omitted.args.review_only)
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: review_base'):
            rc.Coordinator(self.args('--base', 'HEAD', action='resume'))
        self.assertTrue(rc.Coordinator(self.args('--base', 'HEAD~1', action='resume')).args.review_only)   # the same OID
        self.run_dir = self.root / 'default-run'
        rc.Coordinator(rc.parser().parse_args(self.command()[2:]))
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: review_only'):
            rc.Coordinator(self.args(action='resume'))


if __name__ == '__main__':
    unittest.main()
