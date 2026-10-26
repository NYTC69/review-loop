"""WI-WIN W2a: real process lease contention and Windows lock seams (stdlib only)."""
import argparse
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc, winplat

REPO = Path(__file__).resolve().parents[1]
HELPER = '''import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from paired_session import coordinator as rc
kind, workspace, run, ready, mode = sys.argv[2:]
lease = rc.run_lease(Path(run)) if kind == 'run' else rc.workspace_lease(Path(workspace), Path(run))
try:
    with lease:
        if mode == 'hold':
            Path(ready + '.tmp').write_bytes(str(os.getpid()).encode('ascii'))
            os.replace(ready + '.tmp', ready)   # published whole: the parent never reads an empty pid file
            sys.stdin.buffer.read(1)
        print(json.dumps({'acquired': True}))
except rc.RunLeaseError as exc:
    print(json.dumps({'refused': str(exc)}))
'''


class LeaseProcessTests(unittest.TestCase):
    def test_two_processes_contend_release_and_reacquire(self):
        with tempfile.TemporaryDirectory(prefix='lease tests ') as temp:
            root = Path(temp).resolve()
            workspace = root / 'workspace'; workspace.mkdir()
            helper = root / 'lease helper.py'; helper.write_text(HELPER, encoding='utf-8')
            env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
            for kind in ('run', 'workspace'):
                with self.subTest(kind=kind):
                    run = root / (kind + '-run')
                    ready = root / (kind + '-ready')
                    base = [sys.executable, str(helper), str(REPO), kind, str(workspace), str(run), str(ready)]
                    holder = subprocess.Popen(base + ['hold'], cwd=root, env=env, stdin=subprocess.PIPE,
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        deadline = time.monotonic() + 15
                        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertTrue(ready.exists(), 'holder did not acquire the lease')
                        pid = int(ready.read_bytes())
                        contender_run = root / 'second-run' if kind == 'workspace' else run
                        command = base.copy(); command[5] = str(contender_run)
                        def contend():
                            result = subprocess.run(command + ['try'], cwd=root, env=env, capture_output=True,
                                                    text=True, encoding='utf-8', timeout=15, check=True)
                            return json.loads(result.stdout)
                        detail = (f'another coordinator currently owns this run (owner pid {pid})' if kind == 'run' else
                                  f'another coordinator currently owns this workspace (owner pid {pid}, run_dir {run})')
                        self.assertEqual(contend(), {'refused': detail})
                        out, err = holder.communicate(b'x', timeout=15)
                        self.assertEqual(holder.returncode, 0, err)
                        self.assertEqual(json.loads(out), {'acquired': True})
                        lock = run / '.coordinator.lock' if kind == 'run' else rc.workspace_lease_path(workspace)
                        if winplat.IS_WINDOWS:
                            self.assertTrue(lock.is_file(), 'Windows lease files persist after release')
                        self.assertEqual(contend(), {'acquired': True})
                    finally:
                        if holder.poll() is None:
                            holder.kill()
                        holder.communicate(timeout=15)
            # The per-workspace test entry is outside this temporary directory.
            rc.workspace_lease_path(workspace).unlink(missing_ok=True)


@unittest.skipUnless(winplat.IS_WINDOWS, 'native Windows byte-range locks')
class WindowsDiscoveryTests(unittest.TestCase):
    def test_discovery_reads_both_held_lease_records(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / 'other-workspace'; workspace.mkdir()
            own = root / 'own-workspace'; own.mkdir()
            run = root / 'run'
            try:
                with rc.run_lease(run), rc.workspace_lease(workspace, run):
                    (run / 'state.json').write_text(json.dumps({'workspace': str(workspace), 'status': 'ACTIVE'}),
                                                   encoding='utf-8')
                    self.assertEqual(rc.concurrent_run_workspaces(own), {str(workspace): run.name})
            finally:
                rc.workspace_lease_path(workspace).unlink(missing_ok=True)


class WindowsSeamTests(unittest.TestCase):
    def test_lock_and_unlock_seek_to_byte_zero(self):
        fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=lambda *a: None)
        with patch.object(winplat, 'IS_WINDOWS', True), patch.dict(sys.modules, msvcrt=fake), \
                patch.object(winplat.os, 'lseek') as seek, patch.object(fake, 'locking') as locking:
            winplat.lock_exclusive(42)
            winplat.unlock(42)
            self.assertEqual(seek.call_args_list, [unittest.mock.call(42, 0, os.SEEK_SET)] * 2)
            self.assertEqual(locking.call_args_list, [unittest.mock.call(42, 2, 1), unittest.mock.call(42, 0, 1)])

    def test_windows_contention_is_blocking_io_error_eagain(self):
        fake = types.SimpleNamespace(LK_NBLCK=2, locking=lambda *a: None)
        for code in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EBADF):
            with self.subTest(errno=code), patch.object(winplat, 'IS_WINDOWS', True), \
                    patch.dict(sys.modules, msvcrt=fake), patch.object(winplat.os, 'lseek'), \
                    patch.object(fake, 'locking', side_effect=OSError(code, 'busy')):
                with self.assertRaises(OSError) as got:
                    winplat.lock_exclusive(42)
                self.assertEqual(got.exception.errno, errno.EBADF if code == errno.EBADF else errno.EAGAIN)
                if code != errno.EBADF:
                    self.assertIsInstance(got.exception, BlockingIOError)

    def test_holder_json_uses_unlocked_range_and_diagnostics_are_optional(self):
        with tempfile.TemporaryFile() as stream:
            winplat.write_holder(stream.fileno(), '{"pid": 123}')
            self.assertEqual(winplat.read_holder(stream.fileno(), 4096), '{"pid": 123}')
            winplat.write_holder(stream.fileno(), '{}')
            stream.seek(0)
            self.assertEqual(stream.read(), b'\0{}')
        with patch.object(winplat.os, 'read', side_effect=OSError), patch.object(winplat.os, 'lseek'):
            self.assertEqual(winplat.read_holder(42, 4096), '')

    def test_fresh_probe_lock_is_noop_and_whole_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = argparse.Namespace(action='run', author_vendor='claude', timeout=10, run_dir=str(root / 'fresh'))
            with patch.object(winplat, 'IS_WINDOWS', True), \
                    patch.object(rc.ProbeParentLock, '_acquire', side_effect=AssertionError('no POSIX acquisition')):
                with rc.ProbeParentLock(args, root, Path(args.run_dir)) as lock:
                    lock.after_mkdir()
                    self.assertIsNone(lock.fd)
                    self.assertFalse(Path(args.run_dir).exists())
                args.action = 'permission-probe'
                with self.assertRaisesRegex(ValueError, 'not supported on Windows'):
                    with rc.ProbeParentLock(args, root, Path(args.run_dir)):
                        self.fail('whole probe entered')
                self.assertIsNone(rc.detach_holder(args.run_dir))
                with self.assertRaisesRegex(ValueError, 'not supported on Windows'):
                    rc.detach([], args.run_dir)

    def test_replace_retries_transient_permission_error(self):
        with patch.object(winplat, 'IS_WINDOWS', True), \
                patch.object(winplat.os, 'replace', side_effect=[PermissionError('busy'), None]) as replace, \
                patch.object(winplat.time, 'sleep') as sleep:
            winplat.replace('src', 'dst')
            self.assertEqual(replace.call_args_list, [unittest.mock.call('src', 'dst')] * 2)
            sleep.assert_called_once_with(0.05)

    def test_replace_exhaustion_reraises_last_error(self):
        error = PermissionError('still busy')
        with patch.object(winplat, 'IS_WINDOWS', True), \
                patch.object(winplat.os, 'replace', side_effect=error) as replace, \
                patch.object(winplat.time, 'sleep') as sleep:
            with self.assertRaises(PermissionError) as got:
                winplat.replace('src', 'dst')
            self.assertIs(got.exception, error)
            self.assertEqual(replace.call_count, 10)
            self.assertEqual(sleep.call_count, 9)


if __name__ == '__main__':
    unittest.main()
