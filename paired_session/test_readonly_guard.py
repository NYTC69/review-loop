"""D-EFF category A (docs/efficient-mode.md §4a), both modes: a reviewer, gate or shadow turn that changes the workspace is void, its
change is saved as evidence and undone from the coordinator's own pre-turn record when the undo verifies, and the turn is re-dispatched
once; a second change, or an undo that does not verify, holds the run."""
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
