"""D-EFF category A (docs/efficient-mode.md §4a), both modes: a reviewer, gate or shadow turn that changes the workspace is void, its
change is saved as evidence and undone from the coordinator's own pre-turn record when the undo verifies, and the turn is re-dispatched
once; a second change, or an undo that does not verify, holds the run."""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import readonly_guard as rg
from paired_session import timeout_scale as tsc
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

    def test_a_changed_or_missing_ignored_entry_is_reported_and_never_restored(self):   # eff-e
        (self.ws / 'cache.log').write_text('kept\n')
        recorded = rg.capture(self.ws, self.keep)
        self.assertEqual(sorted(recorded['ignored_meta']), ['build.log', 'cache.log'])
        self.assertEqual(rg.ignored_changes(self.ws, recorded), [])
        (self.ws / 'build.log').write_text('review edit\n')
        (self.ws / 'cache.log').unlink()
        self.assertEqual(rg.ignored_changes(self.ws, recorded), ['build.log', 'cache.log'])
        self.assertIsNone(rg.restore(self.ws, recorded, self.keep))   # the undo of tracked content leaves ignored entries alone
        self.assertEqual((self.ws / 'build.log').read_text(), 'review edit\n')
        self.assertFalse((self.ws / 'cache.log').exists())
        (self.ws / 'cache.log').write_text('again\n')                 # two ignored entries again
        with patch.object(rg, 'IGNORED_LIMIT', 1):                    # bounded: too many entries are not checked, and say so
            bounded = rg.capture(self.ws, self.keep)
        self.assertIsNone(bounded['ignored_meta'])
        self.assertIn('ignored set too large: not checked', bounded['ignored_note'])
        self.assertEqual(rg.ignored_changes(self.ws, bounded), [])

    def test_reading_and_git_status_are_no_change(self):
        recorded = rg.capture(self.ws, self.keep)
        self.git('status')                                     # refreshes the index's stat cache
        self.git('diff')
        (self.ws / 'a.txt').read_text()
        self.assertFalse(rg.moved(self.ws, recorded))
        self.assertEqual(rg.state(self.ws, self.keep)['tree'], recorded['tree'])

    def test_a_permission_bit_change_is_detected_undone_and_verified(self):   # roperm: 0644 -> 0600, invisible to git
        for name in ('a.txt', 'new.py'):
            (self.ws / name).chmod(0o644)
        (self.ws / ' lead.txt').write_text('x\n')   # sorts first in ls-files -z: its leading space must survive
        (self.ws / ' lead.txt').chmod(0o644)
        self.git('add', ' lead.txt')
        recorded = rg.capture(self.ws, self.keep)
        (self.ws / ' lead.txt').chmod(0o600)
        self.assertEqual(rg.perm_changes(self.ws, recorded), [' lead.txt'])
        (self.ws / ' lead.txt').chmod(0o644)
        self.assertEqual((recorded['perms']['a.txt'], rg.moved(self.ws, recorded)), (0o644, False))   # untouched: no change
        (self.ws / 'a.txt').chmod(0o600)
        self.assertEqual((rg.perm_changes(self.ws, recorded), rg.moved(self.ws, recorded)), (['a.txt'], True))
        rg.evidence(self.ws, recorded, self.keep, self.root / 'change.diff')
        self.assertIn('mode: a.txt 0644 -> 0600', (self.root / 'change.diff').read_text())
        with patch.object(rg.os, 'chmod'):   # an undo whose chmod does not take does not verify
            self.assertEqual(rg.restore(self.ws, recorded, self.keep), 'permission bits still differ after the restore')
        self.assertIsNone(rg.restore(self.ws, recorded, self.keep))
        self.assertEqual((stat.S_IMODE((self.ws / 'a.txt').stat().st_mode), (self.ws / 'a.txt').read_text()), (0o644, 'author change\n'))
        self.assertFalse(rg.moved(self.ws, recorded))

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

    def checkout(self, path):   # smokefix: git writes 0644 whatever the caller's umask (a 077 umask would write 0600)
        old = os.umask(0o022)
        try: subprocess.run(['git', 'checkout', '--', path], cwd=self.h.workspace, check=True)
        finally: os.umask(old)

    def test_a_review_that_only_changed_permission_bits_is_void_restored_and_re_dispatched(self):   # roperm, both modes
        tracked = self.h.workspace / 'tracked.txt'
        tracked.chmod(0o644)   # smokefix: not the umask's mode, which may already be 0600
        mode = stat.S_IMODE(tracked.stat().st_mode)
        for strict in (False, True):
            with self.subTest(strict=strict):
                self.h.run_dir = self.h.root / f'perm-{strict}'
                marker = self.h.root / f'perm-marker-{strict}'
                result = self.launch({'FAKE_MUTATION': 'chmod600', 'FAKE_MUTATION_ONCE': str(marker)}, strict)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('re-dispatching the reviewer turn once', result.stdout)
                [voided] = self.voided()
                self.assertTrue(voided['restored'])
                self.assertIn(f'mode: tracked.txt {mode:04o} -> 0600', Path(voided['evidence']).read_text())
                self.assertEqual((stat.S_IMODE(tracked.stat().st_mode), self.state()['status']), (mode, 'DONE'))

    def test_an_unrestored_permission_change_blocks_until_the_bits_are_back(self):   # roperm
        co = self.h.coordinator()
        tracked = self.h.workspace / 'tracked.txt'
        tracked.chmod(0o644)   # smokefix: not the umask's mode, which may already be 0600
        mode, keep = stat.S_IMODE(tracked.stat().st_mode), self.h.root / 'keep'
        recorded, before = rg.capture(self.h.workspace, keep), rc.git_snapshot(self.h.workspace)[0]
        tracked.chmod(0o600)
        with patch.object(co, '_stop_turn_group'), patch.object(rc.readonly_guard, 'restore', return_value='simulated failure'):
            voided = co._void_readonly_turn('reviewer', {'sequence': 9}, self.h.workspace, recorded, keep, self.h.root / '009-x', before)
        self.assertFalse(voided.restored)
        self.assertEqual(co.state['unrestored_readonly_turn']['perms'], {'tracked.txt': mode})
        self.assertIn(f'file modes: tracked.txt {mode:04o}', co.unrestored_workspace_issue())   # content and marks match; bits do not
        tracked.chmod(mode)
        self.assertEqual(co.unrestored_workspace_issue(), '')
        self.assertNotIn('unrestored_readonly_turn', co.state)
        tracked.chmod(0o600)   # a 0600 file the turn deleted: content restored by hand at 0644 is not enough
        recorded, before = rg.capture(self.h.workspace, keep), rc.git_snapshot(self.h.workspace)[0]
        tracked.unlink()
        with patch.object(co, '_stop_turn_group'), patch.object(rc.readonly_guard, 'restore', return_value='simulated failure'):
            co._void_readonly_turn('reviewer', {'sequence': 10}, self.h.workspace, recorded, keep, self.h.root / '010-x', before)
        self.assertEqual(co.state['unrestored_readonly_turn']['perms'], {'tracked.txt': 0o600})
        self.checkout('tracked.txt')
        self.assertNotEqual(stat.S_IMODE(tracked.stat().st_mode), 0o600)
        self.assertIn('file modes: tracked.txt 0600', co.unrestored_workspace_issue())
        tracked.chmod(0o600)
        self.assertEqual(co.unrestored_workspace_issue(), '')
        other = self.h.workspace / 'other.txt'   # a chmod during the group stop and a file turned into a symlink are both kept
        other.write_text('o\n')
        other.chmod(0o644)   # smokefix: the stop's chmod 0600 must be a change
        subprocess.run(['git', 'add', 'other.txt'], cwd=self.h.workspace, check=True)
        recorded, before = rg.capture(self.h.workspace, keep), rc.git_snapshot(self.h.workspace)[0]
        tracked.unlink()
        tracked.symlink_to('other.txt')
        with patch.object(co, '_stop_turn_group', side_effect=lambda sequence: other.chmod(0o600)), \
                patch.object(rc.readonly_guard, 'restore', return_value='simulated failure'):
            co._void_readonly_turn('reviewer', {'sequence': 11}, self.h.workspace, recorded, keep, self.h.root / '011-x', before)
        self.assertEqual(co.state['unrestored_readonly_turn']['perms'], {'other.txt': recorded['perms']['other.txt'], 'tracked.txt': 0o600})
        tracked.unlink()
        self.checkout('tracked.txt')
        other.chmod(recorded['perms']['other.txt'])
        self.assertIn(f"file modes: other.txt {recorded['perms']['other.txt']:04o}, tracked.txt 0600",
                      co.unrestored_workspace_issue())   # regular again, bits not yet
        tracked.chmod(0o600)
        self.assertEqual(co.unrestored_workspace_issue(), '')

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
        co = self.h.coordinator('--timeout', tsc.scaled_arg(10), '--author-effort', 'low', '--reviewer-effort', 'low', '--gate-effort', 'low',
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
        co = self.h.coordinator('--timeout', tsc.scaled_arg(10), '--author-effort', 'low', '--reviewer-effort', 'low', '--gate-effort', 'low',
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

    def plan_review(self, env, *flags):
        co = self.h.coordinator('--timeout', tsc.scaled_arg(10), '--author-effort', 'low', '--reviewer-effort', 'low', '--gate-effort', 'low',
                                '--test-command', 'python3 -m unittest', *flags)   # the same flags as h.command()
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        with patch.dict(os.environ, env), self.assertRaises(RuntimeError) as raised:
            co.invoke('reviewer', 'PLAN', co._review_prompt('reviewer', 'snapshot'), rc.review_schema())
        return co, str(raised.exception)

    def test_a_review_that_edits_an_ignored_file_is_void_and_holds(self):   # eff-e
        with (self.h.workspace / '.git' / 'info' / 'exclude').open('a') as handle:
            handle.write('.env\n')
        for vendor in ('claude', 'codex'):   # a Codex reviewer also gets a scratch TMPDIR and its run-dir link check
            with self.subTest(vendor=vendor):
                self.h.run_dir = self.h.root / f'run-ignored-{vendor}'
                (self.h.workspace / '.env').write_text('SECRET=1\n')
                co, message = self.plan_review({'FAKE_PLAN_REVIEWER_TOUCH_IGNORED': '.env'}, '--reviewer-vendor', vendor)
                self.assertIn('reviewer changed ignored files in the workspace', message)
                self.assertIn('.env', message)
                [turn] = [t for t in co.state['turns'] if t.get('role') == 'reviewer']   # no re-dispatch
                self.assertEqual(turn['voided']['ignored_changed'], ['.env'])
                self.assertNotIn('answer', turn)                                          # the verdict is void
                self.assertEqual((self.h.workspace / '.env').read_text(), 'SECRET=1\nreviewer edit\n')   # untouched by the coordinator

    def test_a_review_that_breaks_the_index_is_unverifiable_and_blocks_resume(self):   # eff-e
        co, message = self.plan_review({'FAKE_PLAN_REVIEWER_CORRUPT_INDEX': '1'})
        self.assertTrue(message.startswith('read-only turn changed the workspace and it could not be restored; manual restore needed'), message)
        self.assertIn('post-turn workspace snapshot failed', message)
        self.assertIn('post-turn workspace snapshot failed', self.saved_record()['reason'])
        self.assertIsNone(co.state.get('active'))                                 # recorded, not left uncertain
        command = self.h.command('--retry-uncertain')
        command[2] = 'resume'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(rc.main(command[2:]), 2)
        self.assertIn('manual restore needed', out.getvalue())
        (self.h.workspace / '.git' / 'index').unlink()                            # the operator rebuilds the index from HEAD
        subprocess.run(['git', 'reset', '-q'], cwd=self.h.workspace, check=True)
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

    def test_the_turn_process_group_is_stopped_before_the_restore(self):   # INT-2c: no child left in it re-dirties the restored tree
        co = self.h.coordinator()
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        prompt = co._review_prompt('reviewer', 'snapshot')
        calls, pids, stop, restore, probe = [], [], rc.Coordinator._stop_turn_group, rc.readonly_guard.restore, rc.retry_killpg_eperm
        with patch.dict(os.environ, {'FAKE_PLAN_REVIEWER_MUTATE': '1'}), \
                patch.object(rc.Coordinator, '_stop_turn_group', autospec=True,
                             side_effect=lambda me, *a, **k: calls.append(('stop', a[0])) or stop(me, *a, **k)), \
                patch.object(rc, 'retry_killpg_eperm', side_effect=lambda pid, *a, **k: pids.append(pid) or probe(pid, *a, **k)), \
                patch.object(rc.readonly_guard, 'restore', side_effect=lambda *a: calls.append('restore') or restore(*a)):
            with self.assertRaisesRegex(RuntimeError, 'reviewer mutated workspace again after one re-dispatch'):
                co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())
        voids = [t for t in co.state['turns'] if 'voided' in t]
        self.assertEqual(len(voids), 2)
        for turn in voids:
            self.assertEqual(calls[calls.index(('stop', turn['sequence'])) + 1], 'restore')
            self.assertIn(turn['pid'], pids)   # the stop found the turn's group, not a missing pid
        self.assertEqual(calls.count('restore'), 2)
        with patch.dict(os.environ, {'FAKE_PLAN_REVIEWER_MUTATE': '1'}), \
                patch.object(rc.Coordinator, '_stop_turn_group', side_effect=RuntimeError('the group outlived its turn')), \
                patch.object(rc.readonly_guard, 'restore', side_effect=AssertionError('restored while the group may still write')):
            with self.assertRaisesRegex(RuntimeError, r'reviewer mutated workspace and its process group could not be stopped \(the group'):
                co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())
        self.assertFalse(co.state['turns'][-1]['voided']['restored'])
        record = self.saved_record()   # INT-3a: not restored either, so recorded at once like eff-d's failed undo
        self.assertEqual(record['sequence'], co.state['turns'][-1]['sequence'])
        self.assertTrue(record['reason'].startswith('process group could not be stopped (stop every process of that turn first): '))
        self.assertIn('head', record['marks'])
        self.assertIn('manual restore needed', co.unrestored_workspace_issue())
        subprocess.run(['git', 'checkout', '--', 'tracked.txt'], cwd=self.h.workspace, check=True)   # the operator restores
        self.assertEqual(co.unrestored_workspace_issue(), '')
        self.assertIsNone(self.saved_record())


if __name__ == '__main__':
    unittest.main()
