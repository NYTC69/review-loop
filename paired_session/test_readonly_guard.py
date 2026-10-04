"""D-EFF category A (docs/efficient-mode.md §4a), both modes: a reviewer, gate or shadow turn that changes the workspace is void, its
change is saved as evidence and undone from the coordinator's own pre-turn record when the undo verifies, and the turn is re-dispatched
once; a second change, or an undo that does not verify, holds the run."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import readonly_guard as rg
from paired_session import test_real_coordinator as trc

rc = trc.rc


class ReadOnlyGuardUnitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ws = self.root / 'ws'
        self.ws.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'a@example.test')
        self.git('config', 'user.name', 'A')
        (self.ws / 'a.txt').write_text('base\n')
        (self.ws / '.gitignore').write_text('*.log\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        (self.ws / 'a.txt').write_text('author change\n')    # the author's uncommitted work, which a review must leave alone
        (self.ws / 'new.py').write_text('x = 1\n')
        self.git('add', 'new.py')
        (self.ws / 'build.log').write_text('ignored\n')
        self.keep = self.root / 'keep'

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.ws, check=True, capture_output=True, text=True).stdout.strip()

    def test_every_kind_of_change_is_undone_and_verified(self):
        recorded = rg.capture(self.ws, self.keep)
        head, status = self.git('rev-parse', 'HEAD'), self.git('status', '--short')
        (self.ws / 'a.txt').write_text('review edit\n')
        (self.ws / 'new.py').unlink()
        (self.ws / 'extra.txt').write_text('x\n')
        (self.ws / 'build.log').write_text('changed but ignored\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'review commit')
        self.git('checkout', '-qb', 'other')
        now = rg.evidence(self.ws, recorded, self.keep, self.root / 'change.diff')
        self.assertEqual([k for k in ('head', 'branch', 'index', 'tree') if recorded[k] != now[k]], ['head', 'branch', 'index', 'tree'])
        self.assertIn('extra.txt', (self.root / 'change.diff').read_text())
        self.assertIsNone(rg.restore(self.ws, recorded, self.keep))
        self.assertEqual((self.git('rev-parse', 'HEAD'), self.git('symbolic-ref', 'HEAD'), self.git('status', '--short')),
                         (head, recorded['branch'], status))
        self.assertEqual((self.ws / 'a.txt').read_text(), 'author change\n')
        self.assertFalse((self.ws / 'extra.txt').exists())
        self.assertEqual((self.ws / 'build.log').read_text(), 'changed but ignored\n')   # ignored files are not recorded (a known limit)

    def test_an_ignored_file_survives_a_turn_that_changed_the_ignore_rules(self):   # eff-b R1 MAJOR
        (self.ws / '.gitignore').write_text('*.log\n.env\nnode_modules/\n')   # the author's uncommitted ignore rules
        (self.ws / '.env').write_text('SECRET=1\n')
        (self.ws / 'node_modules' / 'pkg').mkdir(parents=True)
        (self.ws / 'node_modules' / 'pkg' / 'index.js').write_text('x\n')
        recorded = rg.capture(self.ws, self.keep)
        self.git('checkout', '--', '.gitignore')                # the review drops them: .env and node_modules now look untracked
        (self.ws / 'a.txt').write_text('review edit\n')
        self.assertIsNone(rg.restore(self.ws, recorded, self.keep))
        self.assertEqual((self.ws / '.env').read_text(), 'SECRET=1\n')
        self.assertTrue((self.ws / 'node_modules' / 'pkg' / 'index.js').exists())
        self.assertIn('.env', (self.ws / '.gitignore').read_text())
        self.assertEqual((self.ws / 'a.txt').read_text(), 'author change\n')

    def test_an_unborn_head_gets_its_new_branch_removed(self):
        fresh = self.root / 'fresh'
        fresh.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=fresh, check=True)
        (fresh / 'a.txt').write_text('x\n')
        recorded = rg.capture(fresh, self.keep)
        run = lambda *a: subprocess.run(['git', '-c', 'user.email=a@example.test', '-c', 'user.name=A', *a], cwd=fresh, check=True, capture_output=True)
        run('add', '-A')
        run('commit', '-qm', 'review commit')
        self.assertIsNone(rg.restore(fresh, recorded, self.keep))
        self.assertNotEqual(subprocess.run(['git', 'rev-parse', '--verify', '-q', 'HEAD'], cwd=fresh).returncode, 0)   # unborn again

    def test_a_turn_that_touched_thousands_of_paths_is_undone_through_stdin(self):   # eff-c: no E2BIG
        for i in range(2000):
            (self.ws / f'f{i:04d}.txt').write_text('base\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'many files')
        recorded = rg.capture(self.ws, self.keep)
        for i in range(2000):
            (self.ws / f'f{i:04d}.txt').write_text('review edit\n')
        with patch.object(rg, '_git', wraps=rg._git) as git:
            self.assertIsNone(rg.restore(self.ws, recorded, self.keep))
        [checkout] = [c for c in git.call_args_list if 'checkout-index' in c.args]
        self.assertIn('--stdin', checkout.args)
        self.assertEqual(checkout.kwargs['input'].count('\0'), 2000)   # the paths, never on the command line
        self.assertEqual({(self.ws / f'f{i:04d}.txt').read_text() for i in range(2000)}, {'base\n'})

    def test_reading_and_git_status_are_no_change(self):
        recorded = rg.capture(self.ws, self.keep)
        self.git('status')                                     # refreshes the index's stat cache
        self.git('diff')
        (self.ws / 'a.txt').read_text()
        self.assertFalse(rg.moved(self.ws, recorded))
        self.assertEqual(rg.state(self.ws, self.keep)['tree'], recorded['tree'])

    def test_an_undo_that_cannot_use_its_record_reports_why(self):
        recorded = rg.capture(self.ws, self.keep)
        (self.ws / 'a.txt').write_text('review edit\n')
        self.assertIn('RuntimeError', rg.restore(self.ws, {**recorded, 'tree': '0' * 40}, self.keep))


class ReadOnlyTurnTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def state(self):
        return json.loads((self.h.run_dir / 'state.json').read_text())

    def launch(self, env, strict):   # a real coordinator process; efficient needs neither --strict nor --skip-probe
        if strict: return self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', env=env)
        command = [a for a in self.h.command('--shadow', 'off', '--adversarial-gate', 'off') if a != '--strict']
        return subprocess.run(command, cwd=self.h.root, env={**os.environ, **env}, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def voided(self):
        return [t['voided'] for t in self.state()['turns'] if t.get('role') == 'reviewer' and 'voided' in t]

    def test_a_review_that_changed_the_workspace_once_is_void_restored_and_re_dispatched(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                self.h.run_dir = self.h.root / f'once-{strict}'
                marker = self.h.root / f'marker-{strict}'
                result = self.launch({'FAKE_MUTATION': 'echo', 'FAKE_MUTATION_ONCE': str(marker)}, strict)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('re-dispatching the reviewer turn once', result.stdout)
                [voided] = self.voided()
                self.assertTrue(voided['restored'])
                self.assertIn('forbidden.txt', Path(voided['evidence']).read_text())
                self.assertFalse((self.h.workspace / 'forbidden.txt').exists())
                self.assertEqual(self.state()['status'], 'DONE')
                prompts = sorted((self.h.run_dir / 'evidence').glob('*-reviewer.prompt.txt'))
                self.assertTrue(any('your previous answer to this request changed the workspace' in f.read_text() for f in prompts))
                self.assertEqual(len(list((self.h.run_dir / 'internal' / 'readonly').iterdir())), 1)   # clean turns leave no undo record
                self.assertEqual(self.state()['config']['safety_mode'], 'strict' if strict else 'efficient')

    def test_a_second_change_holds_and_both_are_undone(self):
        for strict, mode in ((False, 'checkout'), (True, 'checkout'), (False, 'commit')):
            with self.subTest(strict=strict, mode=mode):
                self.h.run_dir = self.h.root / f'twice-{strict}-{mode}'
                head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.h.workspace, capture_output=True, text=True).stdout
                result = self.launch({'FAKE_MUTATION': mode}, strict)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('reviewer mutated workspace again after one re-dispatch', self.state()['hold_reason'])
                self.assertEqual([v['restored'] for v in self.voided()], [True, True])
                self.assertEqual((self.h.workspace / 'tracked.txt').read_text(), 'base\n')
                self.assertEqual(subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.h.workspace, capture_output=True, text=True).stdout, head)

    def test_a_change_is_undone_even_when_the_turn_fails_for_another_reason(self):   # eff-b R1 MEDIUM
        co = self.h.coordinator()
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        prompt = co._review_prompt('reviewer', 'snapshot')
        with patch.dict(os.environ, {'FAKE_PLAN_REVIEWER_MUTATE': '1'}), \
                patch.object(rc.Coordinator, '_collect', side_effect=ValueError('collect failed')):
            with self.assertRaisesRegex(RuntimeError, 'collect failed'):
                co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())
        self.assertEqual((self.h.workspace / 'tracked.txt').read_text(), 'base\n')
        self.assertEqual([t['voided']['restored'] for t in co.state['turns'] if t.get('role') == 'reviewer'], [True])   # no re-dispatch

    def test_a_failed_undo_leads_the_hold_and_blocks_resume_until_the_workspace_is_back(self):   # eff-c
        co = self.h.coordinator('--timeout', '10', '--author-effort', 'low', '--reviewer-effort', 'low', '--gate-effort', 'low',
                                '--test-command', 'python3 -m unittest')   # the same flags as h.command(), so resume restores it
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        prompt = co._review_prompt('reviewer', 'snapshot')
        with patch.dict(os.environ, {'FAKE_PLAN_REVIEWER_MUTATE': '1', 'FAKE_PLAN_REVIEWER_FAIL': '1'}), \
                patch.object(rc.readonly_guard, 'restore', return_value='simulated failure'):
            with self.assertRaises(RuntimeError) as raised:
                co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())
        self.assertTrue(str(raised.exception).startswith(
            'read-only turn changed the workspace and it could not be restored; manual restore needed: '), str(raised.exception))
        self.assertIn('simulated failure', str(raised.exception))
        self.assertIn('also: CLI exit 1', str(raised.exception))   # the other reason follows
        self.assertIn('manual restore needed', co.unrestored_workspace_issue())
        command = self.h.command()
        command[2] = 'resume'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(rc.main(command[2:]), 2)
        self.assertIn('REFUSED: read-only turn', out.getvalue())
        self.assertIn('manual restore needed', out.getvalue())
        subprocess.run(['git', 'checkout', '--', 'tracked.txt'], cwd=self.h.workspace, check=True)   # the operator's manual restore
        self.assertEqual(co.unrestored_workspace_issue(), '')
        self.assertNotIn('unrestored_readonly_turn', json.loads((self.h.run_dir / 'state.json').read_text()))

    def failed_undo(self, **patches):   # a PLAN reviewer write whose undo fails; `patches` break the rest of the turn
        co = self.h.coordinator('--timeout', '10', '--author-effort', 'low', '--reviewer-effort', 'low', '--gate-effort', 'low',
                                '--test-command', 'python3 -m unittest')   # the same flags as h.command()
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        prompt = co._review_prompt('reviewer', 'snapshot')
        with patch.dict(os.environ, {'FAKE_PLAN_REVIEWER_MUTATE': '1'}), \
                patch.object(rc.readonly_guard, 'restore', return_value='simulated failure'), \
                patch.object(rc.Coordinator, '_collect', **patches):
            co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())

    def saved_record(self):
        return json.loads((self.h.run_dir / 'state.json').read_text()).get('unrestored_readonly_turn')

    def test_an_unexpected_exception_after_a_failed_undo_keeps_the_record_and_the_reason(self):   # eff-d
        with self.assertRaises(RuntimeError) as raised:
            self.failed_undo(side_effect=OSError('disk went away'))
        self.assertTrue(str(raised.exception).startswith(
            'read-only turn changed the workspace and it could not be restored; manual restore needed: '), str(raised.exception))
        self.assertIn('also: disk went away', str(raised.exception))
        self.assertEqual(self.saved_record()['reason'], 'simulated failure')
        command = self.h.command()
        command[2] = 'resume'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(rc.main(command[2:]), 2)
        self.assertIn('manual restore needed', out.getvalue())
        subprocess.run(['git', 'checkout', '--', 'tracked.txt'], cwd=self.h.workspace, check=True)   # undo by hand before the next run
        self.h.run_dir = self.h.root / 'interrupted'
        with self.assertRaises(KeyboardInterrupt):   # not even an Exception: the record was saved when it was made
            self.failed_undo(side_effect=KeyboardInterrupt)
        self.assertEqual(self.saved_record()['reason'], 'simulated failure')

    def test_accept_and_a_scope_change_refuse_while_a_failed_undo_stands(self):   # eff-d
        with self.assertRaises(RuntimeError):
            self.failed_undo(side_effect=ValueError('stop after the undo'))
        for action, extra in (('accept', ('--reason', 'looks fine')), ('accept', ('--override-rejection', '--reason', 'take it')),
                              ('reject', ('--scope-change', '--text', 'narrow it'))):
            with self.subTest(action=action, extra=extra):
                result = self.h.run_operator_action(action, *extra)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('REFUSED: read-only turn', result.stdout)
                self.assertIn('restore the workspace by hand', result.stdout)
                self.assertIn('clears this record by itself', result.stdout)
                self.assertIn('otherwise abort the run', result.stdout)
        subprocess.run(['git', 'checkout', '--', 'tracked.txt'], cwd=self.h.workspace, check=True)   # the manual restore
        result = self.h.run_operator_action('accept', '--reason', 'looks fine')
        self.assertNotIn('REFUSED: read-only turn', result.stdout)
        self.assertIsNone(self.saved_record())

    def test_a_failed_capture_still_sees_a_commit(self):   # eff-c: HEAD, branch and index are recorded apart from the tree
        co = self.h.coordinator()
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        prompt = co._review_prompt('reviewer', 'snapshot')
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.h.workspace, capture_output=True, text=True).stdout.strip()
        with patch.dict(os.environ, {'FAKE_PLAN_REVIEWER_COMMIT': '1'}), \
                patch.object(rc.readonly_guard, 'capture', side_effect=RuntimeError('no tree')):
            with self.assertRaisesRegex(RuntimeError, 'manual restore needed: reviewer mutated workspace; there is no verified pre-turn record'):
                co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())
        self.assertIn('manual restore needed', co.unrestored_workspace_issue())
        subprocess.run(['git', 'reset', '-q', '--soft', head], cwd=self.h.workspace, check=True)
        self.assertEqual(co.unrestored_workspace_issue(), '')

    def test_an_unreadable_index_counts_as_a_change(self):
        co = self.h.coordinator()
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        prompt = co._review_prompt('reviewer', 'snapshot')
        with patch.object(rc.readonly_guard, 'moved', side_effect=RuntimeError('git ls-files failed')):
            with self.assertRaisesRegex(RuntimeError, 'reviewer mutated workspace again after one re-dispatch'):
                co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())

    def test_an_undo_that_does_not_verify_holds_without_a_re_dispatch(self):
        co = self.h.coordinator()
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        prompt = co._review_prompt('reviewer', 'snapshot')
        with patch.dict(os.environ, {'FAKE_PLAN_REVIEWER_MUTATE': '1'}), patch.object(rc.readonly_guard, 'restore', return_value='simulated failure'):
            with self.assertRaisesRegex(RuntimeError, r'reviewer mutated workspace and the coordinator could not restore it \(simulated failure\)'):
                co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())
        self.assertEqual([t['voided']['restored'] for t in co.state['turns'] if t.get('role') == 'reviewer'], [False])
        subprocess.run(['git', 'checkout', '--', 'tracked.txt'], cwd=self.h.workspace, check=True)   # the failed undo left the change
        with patch.dict(os.environ, {'FAKE_PLAN_REVIEWER_MUTATE': '1'}), patch.object(rc.readonly_guard, 'capture', side_effect=RuntimeError('no git')):
            with self.assertRaisesRegex(RuntimeError, 'no verified pre-turn record to restore it from'):
                co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())


if __name__ == '__main__':
    unittest.main()
