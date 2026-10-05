"""D-LG1 review-only entry, LG1-a1 (docs/review-only-entry.md §1-§2, §8 tests 1, 3, 5, 11a): `run --review-only` starts at
the EXEC review of the existing change, with its refusals, frozen values and tree check."""
import hashlib
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from paired_session import test_real_coordinator as trc
from paired_session import worktree_lifecycle as wl
from paired_session.test_worktree_lifecycle import COVERING_GITIGNORE, DONE

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

    def tree_manifest(self, rev='HEAD'):   # a commit's tree as git_snapshot values: content and mode, by path
        rows = []
        for entry in filter(None, self.git('ls-tree', '-r', '-z', rev).split('\0')):
            meta, path = entry.split('\t', 1)
            mode, _, oid = meta.split()
            data = subprocess.run(['git', 'cat-file', 'blob', oid], cwd=self.workspace, check=True, capture_output=True).stdout
            rows.append([path, 'link:' + os.fsdecode(data) if mode == '120000' else
                         ('exec:' if mode == '100755' else '') + hashlib.sha256(data).hexdigest()])
        return sorted(rows)

    def accepted_manifest(self):
        return sorted(row for row in self.state()['approved_manifest'] if row[1] != 'missing')

    # --- test 1: creation ----------------------------------------------------------------------------------------------------
    def test_refusals_leave_no_state(self):
        foreign = self.git('commit-tree', self.git('rev-parse', 'HEAD^{tree}'), '-m', 'not an ancestor')
        cases = [((), 'nothing to review: the workspace tree equals the review base'),
                 (('--base', 'no-such-ref'), '--base no-such-ref does not name a commit'),
                 (('--base', foreign), 'is not an ancestor of HEAD'),
                 (('--stop-after-plan',), 'has no PLAN phase'),
                 (('--supersedes', str(self.root / 'parent')), "a scope-change successor keeps its parent's entry")]
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

    # --- LG1-a2, test 4: a base below HEAD -----------------------------------------------------------------------------------
    def test_a_base_below_head_reviews_the_committed_part_too(self):
        (self.workspace / 'committed.py').write_text('VALUE = 1\n')
        self.git('add', 'committed.py')
        self.git('commit', '-qm', 'committed part of the change')
        (self.workspace / 'tracked.txt').write_text('base\nuncommitted\n')
        base, head = self.git('rev-parse', 'HEAD~1'), self.git('rev-parse', 'HEAD')
        co = rc.Coordinator(self.args('--base', 'HEAD~1'))
        self.assertEqual((co.state['base_commit'], co.state['review_only']['head_at_start']), (base, head))
        co.materialize_review_context()
        delta = (co.context / 'delta.patch').read_text()
        self.assertEqual(delta, self.git('diff', '--no-ext-diff', '--no-textconv', '--binary', base, '--') + '\n')
        self.assertIn('committed.py', delta)
        self.assertEqual(sorted(co._changed_paths()), ['committed.py', 'tracked.txt'])   # HEAD-relative would miss committed.py
        self.assertIn('python-reviewer', wl.specialists(co._changed_paths()))
        self.run_dir = self.root / 'w-parent'
        with mock.patch.object(rc, 'REVIEW_ONLY_LIFECYCLE_READY', True):   # LG1-c lifts the refusal; the parent is HEAD now
            w = rc.Coordinator(self.args('--base', 'HEAD~1', '--lifecycle-mode', 'on'))
        self.assertEqual((w.state['lifecycle']['parent'], w.state['base_commit']), (head, base))

    # --- LG1-a2, test 11b: a scope-change successor ---------------------------------------------------------------------------
    def test_a_scope_change_successor_keeps_the_entry_and_freezes_its_own_scope(self):
        self.change()
        parent_dir = self.run_dir
        done = self.run_coordinator('--review-only')
        self.assertIn('DONE', done.stdout, done.stdout + done.stderr)
        parent_state = self.state()
        parent = rc.Coordinator(self.args(action='reject'))
        parent.args.action = 'reject'
        start = parent.scope_change('Also reject floats.', None).split('Start: ', 1)[1].split()
        option = lambda name: start[start.index(name) + 1]
        self.assertNotIn('--review-only', start)   # the entry travels in the spec, not on the command line or in a profile
        spec = json.loads((parent_dir / 'evidence' / 'successor-spec.json').read_text())
        self.assertEqual(spec['review_only'], {'review_base': parent_state['config']['review_base'],
                                               'review_scope_sha256': parent_state['review_only']['review_scope_sha256']})
        self.git('add', '-A')
        self.git('commit', '-qm', 'the parent work, committed')   # HEAD moves; the base stays an ancestor
        self.run_dir, self.workitem = Path(option('--run-dir')), Path(option('--workitem'))
        argv = [a for a in self.command('--config', option('--config'), '--supersedes', str(parent_dir))[2:]]
        with self.assertRaisesRegex(ValueError, "keeps its parent's entry and base"):
            rc.Coordinator(rc.parser().parse_args([*argv, '--base', 'HEAD']))
        child = rc.Coordinator(rc.parser().parse_args(argv)).state
        record = child['review_only']
        self.assertEqual((child['config']['review_only'], child['config']['review_base'], child['base_commit']),
                         (True, parent_state['config']['review_base'], parent_state['config']['review_base']))
        self.assertEqual((child['phase'], child['next'], record['head_at_start']), ('EXEC', 'reviewer', self.git('rev-parse', 'HEAD')))
        self.assertEqual(record['parent_review_scope_sha256'], parent_state['review_only']['review_scope_sha256'])
        self.assertNotEqual(record['review_scope_sha256'], record['parent_review_scope_sha256'])   # its own scope
        self.assertIn('Also reject floats.', (self.run_dir / 'context' / 'plan.md').read_text())

    # --- LG1-b, test 2: no role is told about an approved plan -----------------------------------------------------------------
    def prompts(self, role):
        return [path.read_text() for path in sorted((self.run_dir / 'evidence').glob(f'*-exec-{role}.prompt.txt'))]

    def test_every_role_gets_the_review_scope_wording(self):
        self.change()
        held = self.run_coordinator('--review-only', '--max-exec-rounds', '2', env={'FAKE_EXEC_MIXED_REVISE': '1'})
        self.assertIn('EXEC round limit reached', held.stdout, held.stdout + held.stderr)
        state = self.state()
        self.assertEqual(state['turns'][0]['role'], 'reviewer')   # the first dispatch is the EXEC reviewer
        scope = 'Review scope (review-only entry; no plan was drafted or approved)'
        for role in ('reviewer', 'shadow'):
            texts = self.prompts(role)
            self.assertTrue(texts, role)
            for text in texts:
                self.assertIn(scope, text)
                self.assertNotIn('Approved/current plan', text)
        [author] = self.prompts('author')   # the first author turn fixes the existing change
        self.assertIn('The change under review is the existing work in this workspace', author)
        self.assertIn('Fix every delivered blocking finding', author)
        self.assertNotIn('Implement the approved plan', author)
        co = rc.Coordinator(self.args('--max-exec-rounds', '2', action='resume'))
        self.assertIn(scope, co._review_protocol(['sum_ints.py']))   # specialists, docs and security reviewers
        self.assertNotIn('Approved plan:', co._review_protocol(['sum_ints.py']))
        self.assertIn('Review scope (review-only entry; no plan was approved): ', co._gate_prompt('snapshot'))
        self.assertNotIn('Approved plan: ', co._gate_prompt('snapshot'))
        noun = f'the change in this worktree against the review base {state["config"]["review_base"][:12]} (commits since it included)'
        self.assertEqual(co._change_noun(), noun)
        finish = wl.finish_prompt('SCOPE', 'python3 -m unittest', '', True)
        self.assertTrue(finish.endswith('Review scope (no plan was approved):\nSCOPE'))
        self.assertNotIn('approved plan', finish)
        self.assertIn(noun, wl.specialist_prompt('python-reviewer', 'BODY', 'python3 -m unittest', [], 'PROTOCOL', noun))
        self.run_dir = self.root / 'default'
        default = rc.Coordinator(rc.parser().parse_args(self.command()[2:]))   # a default run keeps its wording
        self.assertIn('Approved plan: ', default._review_protocol([]))
        self.assertEqual(default._change_noun(), 'the uncommitted change in this worktree')
        self.assertEqual(default._change_noun('this uncommitted change'), 'this uncommitted change')   # docs/security text
        self.assertIn('The approved plan below is implemented', wl.finish_prompt('PLAN', 'python3 -m unittest', ''))

    def test_an_operator_note_to_the_fix_author_names_the_review_scope(self):
        self.change()
        held = self.run_coordinator('--review-only', '--max-exec-rounds', '3',
                                    env={'FAKE_EXEC_MIXED_REVISE': '1', 'FAKE_AUTHOR_HOLD_AFTER_WRITE': '1'})
        self.assertIn('HOLD', held.stdout, held.stdout + held.stderr)
        self.assertEqual((self.state()['phase'], self.state()['next']), ('EXEC', 'author'))
        noted = self.run_operator_action('note', '--text', 'Keep the fix inside sum_ints.py.')
        self.assertEqual(noted.returncode, 0, noted.stdout + noted.stderr)
        self.run_operator_action('resume', '--max-exec-rounds', '3')
        [prompt] = [text for text in self.prompts('author') if 'Keep the fix inside sum_ints.py.' in text]
        self.assertIn('Do not expand the review scope.', prompt)
        self.assertNotIn('approved plan', prompt)

    # --- LG1-b, test 10: category A in both modes ---------------------------------------------------------------------------------
    def test_a_read_only_edit_during_the_first_review_is_voided_and_restored_in_both_modes(self):
        for strict in (True, False):
            with self.subTest(strict=strict):
                self.run_dir = self.root / f'void-{strict}'
                self.change()
                marker = self.root / f'mutated-{strict}'
                env = {'FAKE_MUTATION': 'echo', 'FAKE_MUTATION_ONCE': str(marker)}
                command = self.command('--review-only')
                if not strict:   # efficient: the product default, no --strict and no --skip-probe
                    command = [arg for arg in command if arg != '--strict']
                    result = subprocess.run(command, cwd=self.root, env={**rc.os.environ, **env}, text=True,
                                            capture_output=True)
                else:
                    result = self.run_coordinator('--review-only', env=env)
                self.assertIn('DONE', result.stdout, result.stdout + result.stderr)
                self.assertIn('re-dispatching the reviewer turn once', result.stdout)
                state = self.state()
                [void] = [row['voided'] for row in state['turns'] if 'voided' in row]
                self.assertTrue(void['restored'])
                self.assertFalse((self.workspace / 'forbidden.txt').exists())
                self.assertEqual((self.workspace / 'sum_ints.py').read_text(), SUM_INTS)   # the change under review stays
                self.assertEqual(state['config']['safety_mode'], 'strict' if strict else 'efficient')

    # --- LG1-c, tests 6 and 9: the W lifecycle and delivery -------------------------------------------------------------------
    def w_ready(self):   # SECURITY needs the legacy sensitive patterns, as in test_worktree_lifecycle
        (self.workspace / '.gitignore').write_text(COVERING_GITIGNORE)
        self.git('commit', '-qam', 'ignore sensitive files')
        (self.workspace / 'committed.py').write_text('VALUE = 1\n')
        self.git('add', 'committed.py')
        self.git('commit', '-qm', 'committed part of the change')

    W = ('--review-only', '--lifecycle-mode', 'on', '--auto-commit', 'true', '--max-invocations', '60')

    def test_a_review_only_w_run_delivers_the_accepted_tree_on_head_at_start(self):
        self.w_ready()
        self.change()
        base, head = self.git('rev-parse', 'HEAD~1'), self.git('rev-parse', 'HEAD')
        done = self.run_coordinator(*self.W, '--base', 'HEAD~1')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertEqual([row['stage'] for row in state['lifecycle']['receipts']], ['FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])
        self.assertEqual(state['turns'][0]['role'], 'reviewer')
        baseline = json.loads(Path(state['lifecycle']['security_baseline']['path']).read_text())
        self.assertEqual((baseline['state']['head'], baseline['state']['untracked']), (base, []))   # the base tree
        [security] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
        self.assertEqual(security['preflight']['status'], 'clean')
        accepted = self.run_operator_action('accept', *self.W)
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        self.assertEqual((self.git('rev-parse', 'HEAD~1'), self.git('rev-parse', 'HEAD~2')), (head, base))   # on HEAD
        manifest = sorted(path for path, value in self.state()['approved_manifest'] if value != 'missing')
        self.assertEqual(self.git('ls-tree', '-r', '--name-only', 'HEAD').splitlines(), manifest)   # exactly the accepted tree
        self.assertEqual(self.tree_manifest(), self.accepted_manifest())   # LG1-e: by content and mode too
        self.assertEqual(self.git('show', 'HEAD:sum_ints.py') + '\n', SUM_INTS)
        self.assertEqual(self.git('show', 'HEAD:tracked.txt'), 'base\nedited')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertIn(f'Review base: {base}', self.git('log', '-1', '--format=%B'))
        report = (self.run_dir / 'delivery-report.md').read_text()
        self.assertIn(f'review base `{base}`', report)
        self.assertIn(f'审查的已有提交：{head}', report)   # base..head_at_start: the committed part of the change

    def test_a_blocked_review_only_change_is_fixed_and_the_fix_delivered(self):   # test 6: BLOCK -> fix -> approve -> gate
        self.w_ready()
        self.change()
        done = self.run_coordinator(*self.W, '--exercise-revisions')   # the first EXEC review blocks
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        exec_turns = [row['role'] for row in self.state()['turns'] if row['phase'] == 'EXEC' and row['role'] != 'shadow']
        self.assertEqual(exec_turns[:3], ['reviewer', 'author', 'reviewer'])
        self.assertIn('gate', exec_turns)
        accepted = self.run_operator_action('accept', *self.W, '--exercise-revisions')
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        manifest = sorted(path for path, value in self.state()['approved_manifest'] if value != 'missing')
        self.assertEqual(self.git('ls-tree', '-r', '--name-only', 'HEAD').splitlines(), manifest)
        self.assertIn('raise TypeError', self.git('show', 'HEAD:sum_ints.py'))   # the author's fix is delivered
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_docs_the_change_already_edits_are_pre_owned_not_a_hold(self):   # test 8, Q8
        self.w_ready()
        self.change()
        (self.workspace / 'CHANGELOG.md').write_text('# Changelog\n\n- sum_ints added\n')   # in the change under review
        done = self.run_coordinator(*self.W)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertEqual(state['lifecycle']['docs_pre_owned'], ['CHANGELOG.md'])
        [docs] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS']
        self.assertEqual((docs['status'], docs['route'], docs['review']['status']), ('READY', 'SECURITY', 'APPROVE'))
        co = rc.Coordinator(self.args(*self.W[1:], action='resume'))
        note = co._docs_reserved_note()
        self.assertIn('CHANGELOG.md', note)
        self.assertNotIn('Reserved for the DOCS stage; do not edit: CHANGELOG.md', note)

    def test_a_fully_staged_change_is_accepted_and_a_changed_index_is_refused(self):
        self.w_ready()
        self.change()
        self.git('add', 'sum_ints.py', 'tracked.txt')   # fully staged at the start: part of the reviewed tree
        done = self.run_coordinator(*self.W)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        blob = subprocess.run(['git', 'hash-object', '-w', '--stdin'], cwd=self.workspace, input='other\n', text=True,
                              check=True, capture_output=True).stdout.strip()
        index = self.workspace / '.git' / 'index'
        saved = index.read_bytes()
        self.git('update-index', '--cacheinfo', f'100644,{blob},tracked.txt')   # the index changes, the tree does not
        refused = self.run_operator_action('accept', *self.W)
        self.assertIn('auto_commit refuses an index changed since the review-only run started', refused.stdout + refused.stderr)
        index.write_bytes(saved)
        accepted = self.run_operator_action('accept', *self.W)
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        self.assertEqual(self.git('show', 'HEAD:sum_ints.py') + '\n', SUM_INTS)

    # --- LG1-e: edge cases ---------------------------------------------------------------------------------------------------
    def test_base_tree_symlinks_and_modes_are_carried_into_the_baseline_and_the_delivery(self):
        self.w_ready()
        os.symlink('committed.py', self.workspace / 'link.py')
        (self.workspace / 'tool.sh').write_text('#!/bin/sh\necho base\n')
        (self.workspace / 'tool.sh').chmod(0o755)
        self.git('add', 'link.py', 'tool.sh')
        self.git('commit', '-qm', 'a symlink and an executable in the base')
        self.change()
        (self.workspace / 'tool.sh').chmod(0o644)   # the change under review: a mode change and a retargeted symlink
        (self.workspace / 'link.py').unlink()
        os.symlink('sum_ints.py', self.workspace / 'link.py')
        done = self.run_coordinator(*self.W)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        worktree = json.loads(Path(state['lifecycle']['security_baseline']['path']).read_text())['state']['worktree']
        self.assertEqual((worktree['link.py']['mode'], worktree['tool.sh']['mode']), ('120000', '100755'))   # as committed
        [security] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
        self.assertEqual(security['preflight']['status'], 'clean')
        accepted = self.run_operator_action('accept', *self.W)
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        self.assertEqual(self.tree_manifest(), self.accepted_manifest())
        self.assertEqual(self.git('ls-tree', 'HEAD', 'tool.sh').split()[0], '100644')
        self.assertEqual(self.git('cat-file', 'blob', 'HEAD:link.py'), 'sum_ints.py')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_the_review_scope_lists_unusual_paths_one_per_line(self):   # lg1-a1 LOW: tabs, newlines, quotes
        self.change()
        (self.workspace / 'tab\tname.txt').write_text('x\n')
        self.git('add', 'tab\tname.txt')
        self.git('commit', '-qm', 'a tracked name with a tab')
        (self.workspace / 'tab\tname.txt').write_text('changed\n')
        for name in ('new\nline.txt', '"quoted.txt', 'back\\slash.txt', 'café.txt'):
            (self.workspace / name).write_text('x\n')
        scope = (rc.Coordinator(self.args()).context / 'plan.md').read_text()
        for line in ('M\t"tab\\tname.txt"\n', 'untracked: "new\\nline.txt"\n', 'untracked: "\\"quoted.txt"\n',
                     'untracked: "back\\\\slash.txt"\n', 'untracked: café.txt\n', 'untracked: sum_ints.py\n'):
            self.assertIn(line, scope)
        self.assertNotIn('new\nline.txt', scope)
        self.assertEqual(rc.review_scope_path(os.fsdecode(b'caf\xe9.txt')), '"caf\\udce9.txt"')   # a non-UTF-8 byte
        self.assertEqual(rc.review_scope_path('caf\\xe9.txt'), '"caf\\\\xe9.txt"')   # a literal backslash stays distinct
        (self.workspace / 'a, b.txt').write_text('x\n')
        self.git('add', 'a, b.txt')
        (self.workspace / 'a, b.txt').write_text('staged, then changed\n')   # partially staged, with a comma in its name
        self.run_dir = self.root / 'partial'
        with self.assertRaisesRegex(ValueError, r'refuses partially staged paths .*: "a, b.txt"; stage them fully'):
            rc.Coordinator(self.args())

    def test_a_ledger_id_or_verdict_shaped_path_is_refused_and_names_its_source(self):   # FIELD-11, kept conservative
        self.change()
        for name in ('docs/F001.md', 'APPROVE.txt'):
            with self.subTest(name=name):
                (self.workspace / name).parent.mkdir(exist_ok=True)
                (self.workspace / name).write_text('notes\n')
                with self.assertRaisesRegex(ValueError, r'refuses review history in the review scope \(a changed path or the '
                                            r'test command\): .*; the fresh shadow and gate would refuse it'):
                    rc.Coordinator(self.args())
                self.assertFalse((self.run_dir / 'state.json').exists())
                (self.workspace / name).unlink()

    def test_docs_the_change_deletes_or_renames_are_pre_owned(self):   # Q8: deletions and both ends of a rename
        (self.workspace / 'docs').mkdir()
        for name in ('CHANGELOG.md', 'docs/old.md'):
            (self.workspace / name).write_text('# notes\n')
        self.git('add', 'CHANGELOG.md', 'docs/old.md')
        self.git('commit', '-qm', 'docs')
        self.git('rm', '-q', 'CHANGELOG.md')
        self.git('mv', 'docs/old.md', 'docs/new.md')
        co = rc.Coordinator(self.args('--lifecycle-mode', 'on', '--docs-allowlist', 'docs/old.md', '--docs-allowlist', 'docs/new.md'))
        self.assertEqual(co.state['lifecycle']['docs_pre_owned'], ['CHANGELOG.md', 'docs/new.md', 'docs/old.md'])


if __name__ == '__main__':
    unittest.main()
