"""FIELD-13 (docs/field13-concurrent-probes.md): concurrent Claude author probes. The probe tree name is outside the host-wide /tmp probe
glob (E), and one POSIX lock per run-dir parent (D), taken in main before run_lease, keeps every other lock-respecting probe and every
fresh run dir out of a Claude author probe's window. The lock never excuses a listing difference: escapes still FAIL."""
import argparse
import contextlib
import errno
import fcntl
import glob
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import claude_author_probe as cap
from paired_session import test_operator_roles as tor
from paired_session import test_real_coordinator as trc

rc = trc.rc
# Another process holding the parent lock as a Claude author permission-probe of a sibling run dir would, until a release file appears.
HOLDER = r'''
import argparse, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from paired_session import coordinator as rc
run_dir, ready, release, workspace = map(Path, sys.argv[2:6])
args = argparse.Namespace(action='permission-probe', author_vendor='claude', timeout=10, run_dir=str(run_dir))
with rc.ProbeParentLock(args, workspace, run_dir) as lock:
    lock.after_mkdir()
    ready.write_text(str(lock.path))
    while not release.exists(): time.sleep(0.02)
'''


def lock_args(run_dir, action='permission-probe', vendor='claude'):
    return argparse.Namespace(action=action, author_vendor=vendor, timeout=10, run_dir=str(run_dir))


class ProbeParentLockTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.parent = self.h.run_dir.parent
        for name, value in (('PROBE_LOCK_POLL_SECONDS', 0.02), ('PROBE_LOCK_WAIT_FACTOR', 0.003)):   # about 1 s against --timeout 10
            patcher = patch.object(rc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def hold(self, run_dir):
        """A concurrent probe in another process; returns (process, release path, lock path)."""
        run_dir.mkdir(parents=True, exist_ok=True)
        ready, release = self.h.root / f'ready-{run_dir.name}', self.h.root / f'release-{run_dir.name}'
        proc = subprocess.Popen([sys.executable, '-c', HOLDER, str(Path(trc.MODULE_PATH).parent.parent), str(run_dir), str(ready), str(release),
                                 str(self.h.workspace)])
        def stop():
            release.touch()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
        self.addCleanup(stop)
        deadline = time.monotonic() + 20
        while not ready.exists():
            self.assertIsNone(proc.poll(), 'holder exited early')
            self.assertLess(time.monotonic(), deadline, 'holder never took the lock')
            time.sleep(0.02)
        return proc, release, Path(ready.read_text())

    def main(self, action, run_dir, *extra):
        command = self.h.command(*extra)
        command[2], command[command.index('--run-dir') + 1] = action, str(run_dir)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rc.main(command[2:])
        return code, out.getvalue(), err.getvalue()

    def test_a_concurrent_claude_probe_in_a_sibling_run_dir_refuses_a_fresh_probe_cleanly(self):
        proc, _, _ = self.hold(self.parent / 'sibling-run')
        self.h.command()                                                     # writes the fake CLIs into the parent first
        before = sorted(os.listdir(self.parent))
        code, out, err = self.main('permission-probe', self.parent / 'new-run', *tor.BUG_REPORT_FLAGS)
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith('REFUSED: another command holds the probe lock of '), out)
        self.assertIn(f'unverified holder pid {proc.pid}, run_dir {(self.parent / "sibling-run").resolve()}', out)
        self.assertIn('waiting for the probe lock of ', err)
        self.assertEqual(sorted(os.listdir(self.parent)), before)            # no run dir, no state, no probe tree

    def test_an_existing_claude_author_run_reprobes_under_the_lock_without_the_vendor_flag(self):   # R3 MEDIUM (a)
        use_lifecycle_on(self, self.h)
        co = self.h.coordinator(*tor.BUG_REPORT_FLAGS)                       # saved author vendor: claude
        (co.run_dir / 'permission-probe.json').write_text('{"status": "PASS"}\n')
        state, report = (co.run_dir / 'state.json').read_bytes(), (co.run_dir / 'permission-probe.json').read_bytes()
        listing = sorted(os.listdir(co.run_dir))
        self.hold(self.parent / 'sibling-run')
        code, out, _ = self.main('permission-probe', co.run_dir)            # the CLI default author vendor is codex
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith('REFUSED: another command holds the probe lock of '), out)
        self.assertEqual(((co.run_dir / 'state.json').read_bytes(), (co.run_dir / 'permission-probe.json').read_bytes()), (state, report))
        self.assertEqual(sorted(os.listdir(co.run_dir)), listing)            # nothing superseded

    def test_the_lock_plan_follows_the_saved_author_vendor(self):
        run_dir = self.parent / 'planned'
        self.assertEqual((rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir).whole,
                          rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir).fresh), (True, True))
        self.assertFalse(rc.ProbeParentLock(lock_args(run_dir, vendor='codex'), self.h.workspace, run_dir).whole)
        self.assertFalse(rc.ProbeParentLock(lock_args(run_dir, action='run'), self.h.workspace, run_dir).whole)
        run_dir.mkdir()
        for saved, given, whole in (('claude', 'codex', True), ('codex', 'claude', False), (None, 'codex', True), ('other', 'codex', True)):
            with self.subTest(saved=saved, given=given):
                (run_dir / 'state.json').write_text(json.dumps({'config': {'author_vendor': saved, 'timeout': 20}}) if saved else '{not json')
                lock = rc.ProbeParentLock(lock_args(run_dir, vendor=given), self.h.workspace, run_dir)
                self.assertEqual((lock.whole, lock.fresh), (whole, False))
        (run_dir / 'state.json').write_text('{not json')
        self.assertEqual(rc.ProbeParentLock(lock_args(run_dir, vendor='codex'), self.h.workspace, run_dir).estimate, 3 * 10 + 300)   # unreadable: args
        (run_dir / 'state.json').write_text(json.dumps({'config': {'author_vendor': 'claude', 'timeout': 20}}))
        self.assertEqual(rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir).estimate, 3 * 20 + 300)       # the saved timeout

    def test_a_fresh_run_dir_waits_for_a_probe_in_its_parent_then_proceeds(self):
        _, release, _ = self.hold(self.parent / 'sibling-run')
        new, seen = self.parent / 'new-run', []
        def later():
            seen.append(new.exists())                                       # still absent while the probe holds the lock
            release.touch()
        timer = threading.Timer(1.0, later)
        timer.start()
        self.addCleanup(timer.cancel)
        with patch.object(rc, 'PROBE_LOCK_WAIT_FACTOR', 3):
            code, out, err = self.main('status', new)
        self.assertEqual(seen, [False])
        self.assertTrue(new.is_dir())
        self.assertIn('waiting for the probe lock of ', err)
        self.assertFalse(out.startswith('REFUSED'), out)

    def test_a_mkdir_only_hold_ends_before_the_command_runs(self):
        ident, seen = self.parent.stat(), []
        path = rc.probe_lock_dir() / (hashlib.sha256(f'{ident.st_dev}:{ident.st_ino}'.encode()).hexdigest() + '.lock')
        def spy(args):
            seen.append((args._probe_lock, path.exists(), (self.parent / 'new-run').is_dir()))
            return 0
        with patch.object(rc, '_execute_locked', side_effect=spy):
            self.main('status', self.parent / 'new-run')
        self.assertEqual(seen, [(None, False, True)])                       # made under the lock, released (and unlinked) before the command

    def test_a_missing_parent_is_keyed_on_its_nearest_existing_ancestor(self):    # R3 MEDIUM (b)
        self.hold(self.parent / 'sibling-run')
        code, out, _ = self.main('status', self.parent / 'new-group' / 'run')   # its first new entry, new-group, would land in the parent
        self.assertTrue(out.startswith('REFUSED: another command holds the probe lock of ' + str(self.parent.resolve())), out)
        self.assertFalse((self.parent / 'new-group').exists())
        (self.parent / 'other').mkdir()
        code, out, err = self.main('status', self.parent / 'other' / 'run')   # another parent: no wait
        self.assertTrue((self.parent / 'other' / 'run').is_dir())
        self.assertNotIn('waiting for the probe lock', err)

    def test_a_waiter_keyed_on_an_ancestor_moves_to_the_parent_that_appeared_meanwhile(self):     # f13 R1 MEDIUM
        _, release_d, _ = self.hold(self.parent / 'd-run')                   # D holds the parent's key
        run2 = self.parent / 'new' / 'run2'
        command = self.h.command()
        command[2], command[command.index('--run-dir') + 1] = 'status', str(run2)
        log = self.h.root / 'waiter.err'
        with log.open('w') as err:
            waiter = subprocess.Popen(command, cwd=self.h.root, stdout=subprocess.DEVNULL, stderr=err)   # new/ is missing: keyed on the parent
        self.addCleanup(lambda: waiter.poll() is None and (waiter.kill(), waiter.wait()))
        def waits_on(key):
            deadline = time.monotonic() + 30
            while f'waiting for the probe lock of {key.resolve()} (' not in log.read_text():
                self.assertIsNone(waiter.poll(), log.read_text())
                self.assertLess(time.monotonic(), deadline, log.read_text())
                time.sleep(0.05)
        waits_on(self.parent)
        _, release_a, _ = self.hold(self.parent / 'new' / 'a-run')           # A makes new/ and holds its key
        release_d.touch()
        waits_on(self.parent / 'new')                                        # it got the parent's key and moved on to new/
        self.assertFalse(run2.exists())
        release_a.touch()
        waiter.wait(timeout=60)
        self.assertTrue(run2.is_dir(), log.read_text())

    def test_a_dead_holder_frees_the_lock(self):
        proc, _, _ = self.hold(self.parent / 'sibling-run')
        proc.kill(); proc.wait()
        code, out, err = self.main('status', self.parent / 'new-run')
        self.assertTrue((self.parent / 'new-run').is_dir())
        self.assertNotIn('REFUSED', out)

    def test_the_holder_owns_a_non_inheritable_fd_and_a_replaced_lock_file_is_not_intact(self):
        run_dir = self.parent / 'held-run'
        run_dir.mkdir()
        with rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir) as lock:
            self.assertEqual((lock.key_dir, lock.intact(), os.get_inheritable(lock.fd)), (run_dir.parent.resolve(), True, False))
            self.assertEqual(json.loads(os.pread(lock.fd, 4096, 0))['pid'], os.getpid())   # never a second open: it would drop the lock
            lock.path.unlink()
            self.assertFalse(lock.intact())                                  # unlinked only
            lock.path.write_text('')
            self.assertFalse(lock.intact())                                  # replaced by another file
        self.assertTrue(lock.path.exists())                                  # release never removes a file it does not hold
        lock.path.unlink()
        with rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir) as lock:
            path = lock.path
        self.assertFalse(path.exists())                                      # its own file is removed while still locked
        self.assertIsNone(lock.fd)

    def test_a_lock_file_replaced_between_open_and_lock_is_reopened(self):
        run_dir, inodes, real = self.parent / 'held-run', [], fcntl.lockf
        run_dir.mkdir()
        lock = rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir)
        def lockf(fd, operation):
            inodes.append(os.fstat(fd).st_ino)
            if len(inodes) == 1:                                             # another holder's release unlinks it, a newcomer creates it again
                lock.path.unlink(); os.close(os.open(lock.path, os.O_CREAT | os.O_WRONLY, 0o600))
            return real(fd, operation)
        with patch.object(rc.fcntl, 'lockf', lockf), lock:
            self.assertTrue(lock.intact())
            self.assertEqual(len(inodes), 2)
            self.assertNotEqual(*inodes)

    def test_a_lock_path_that_is_not_private_or_lies_in_the_workspace_is_refused(self):
        run_dir, locks = self.parent / 'held-run', self.h.root / 'locks'
        run_dir.mkdir()
        with patch.object(rc, 'probe_lock_dir', return_value=locks):
            locks.mkdir(mode=0o755); locks.chmod(0o755)
            with self.assertRaisesRegex(rc.RunLeaseError, 'not a private directory'):
                rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir).__enter__()
            locks.chmod(0o700)
            lock = rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir)
            with lock: path = lock.path
            path.write_text(''); path.chmod(0o644)
            with self.assertRaisesRegex(rc.RunLeaseError, 'not a private regular file'):
                rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir).__enter__()
            path.unlink(); path.symlink_to(self.h.root / 'elsewhere')
            with self.assertRaises(OSError):
                rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir).__enter__()
            self.assertFalse((self.h.root / 'elsewhere').exists())
        with patch.object(rc, 'probe_lock_dir', return_value=self.h.workspace / 'locks'), \
                self.assertRaisesRegex(rc.RunLeaseError, 'inside the workspace'):
            rc.ProbeParentLock(lock_args(run_dir), self.h.workspace, run_dir).__enter__()

    def test_a_read_only_fd_cannot_take_the_lock(self):          # macOS: an flock through such an fd still blocks it (report residual)
        path = self.h.root / 'x.lock'
        path.write_text('')
        fd = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        with self.assertRaises(OSError) as caught:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(caught.exception.errno, errno.EBADF)


class ProbeUnderTheLockTests(unittest.TestCase):
    locals().update({name: getattr(tor.ClaudeAuthorProbeTests, name) for name in ('setUp', 'co', 'probe')})

    def held(self, co):
        lock = rc.ProbeParentLock(lock_args(co.run_dir), co.workspace, co.run_dir)
        lock.__enter__()
        self.addCleanup(lock.release)
        co.args._probe_lock = lock                                           # as main sets it for a Claude author permission-probe
        return lock

    def during_turn(self, co, change):
        real = co.invoke
        def invoke(*args, **kwargs):
            change()
            return real(*args, **kwargs)
        return patch.object(co, 'invoke', new=invoke)

    def test_the_probe_tree_is_outside_the_tmp_glob_so_a_tree_in_tmp_changes_no_verdict(self):   # E
        name = Path(f'/tmp/{cap.TREE_PREFIX}other-{os.getpid()}')         # another run's tree, its run dir directly in /tmp
        self.addCleanup(lambda: name.rmdir() if name.is_dir() else None)
        co = self.co()
        real = types.SimpleNamespace(glob=lambda pattern: [n for n in glob.glob(pattern) if n == str(name)])   # the real pattern, no other run's names
        with patch.object(cap, 'glob', real), self.during_turn(co, name.mkdir):
            out = self.probe(co=co)[1]
        self.assertEqual(out['status'], 'PASS', out)

    def test_a_tampered_lock_fails_the_probe(self):
        for name, change in (('unlinked', lambda lock: lock.path.unlink()), ('replaced', lambda lock: (lock.path.unlink(), lock.path.write_text('')))):
            with self.subTest(change=name):
                co = self.co()
                lock = self.held(co)
                with self.during_turn(co, lambda: change(lock)):
                    out = self.probe(co=co)[1]
                self.assertEqual((out['status'], out.get('reason')), ('FAIL', 'probe-lock-tampered'), out)
                self.assertIn(str(lock.path), out['model_escape_failed_targets'])
                lock.release(); lock.path.unlink(missing_ok=True)

    def test_escapes_still_fail_and_a_clean_probe_passes_while_the_lock_is_held(self):
        for scenario, status in (({}, 'PASS'), ({'escape': ['write_abs']}, 'FAIL'), ({'silent_write': ['{parent}/dropped.txt']}, 'FAIL')):
            with self.subTest(scenario=scenario):
                co = self.co()
                lock = self.held(co)
                try: out = self.probe(scenario, co=co)[1]
                finally: (co.run_dir.parent / 'dropped.txt').unlink(missing_ok=True); lock.release()
                self.assertEqual(out['status'], status, out)
                if 'silent_write' in scenario: self.assertEqual(out['changed_beside_run_dir'], [str(co.run_dir.parent / 'dropped.txt')])

    def test_a_fresh_sibling_run_dir_waits_until_the_probe_is_done(self):
        co = self.co()
        lock = self.held(co)
        sibling, seen, procs = co.run_dir.parent / 'sibling-run', [], []
        command = self.h.command()
        command[2], command[command.index('--run-dir') + 1] = 'status', str(sibling)
        def start():
            procs.append(proc := subprocess.Popen(command, cwd=self.h.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.communicate()))
            time.sleep(1.0)
            seen.append(sibling.exists())
        with self.during_turn(co, start):
            out = self.probe(co=co)[1]
        self.assertEqual((out['status'], seen), ('PASS', [False]), out)
        lock.release()
        _, err = procs[0].communicate(timeout=60)
        self.assertTrue(sibling.is_dir())
        self.assertIn('waiting for the probe lock of ', err)
        co = self.co()                                                       # the same sibling creation with no lock held: an escape
        with self.during_turn(co, lambda: (co.run_dir.parent / 'unlocked-run').mkdir()):
            self.assertEqual(self.probe(co=co)[1]['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
