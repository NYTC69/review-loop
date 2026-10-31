"""WI-WIN W4b: the read-only role scratch lifecycle on Windows (create, validate, list, remove, timeout cleanup, leftover sweep),
the temp-dir variables of a role, and the Windows credential Read-deny rules.

Most tests drive the Windows branches on every OS with winplat.IS_WINDOWS patched (they use only path calls that exist on
POSIX too) and check that no fd-based call is made. NativeWindowsScratchTests (windows-latest CI, see windows-ci-modules.txt)
use real NTFS read-only attributes, directory junctions and Job Objects. PosixPinTests pin today's POSIX values."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc, winplat
from paired_session.test_winroles import CREDENTIAL_READ_DENIES, ROOT, fixture_coordinator


def stub_coordinator(run_dir: Path):
    """A Coordinator with only what the scratch lifecycle and invoke() up to the dispatch read; holds are recorded."""
    co = rc.Coordinator.__new__(rc.Coordinator)
    co.run_dir, co.author_temp_dir = run_dir, run_dir / 'author-tmp'
    co.args = SimpleNamespace(author_vendor='codex', reviewer_vendor='codex', gate_vendor='codex')
    co.state = {'sequence': 0, 'turns': [], 'spawn_failures': [], 'status': 'ACTIVE', 'config': {}}
    co.holds = []
    co.hold = lambda reason, *args, **kwargs: co.holds.append(reason)
    co._progress_dispatch = lambda role, phase, fresh, call: call()
    return co


def symlink_or_skip(test, target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        test.skipTest(f'symlinks are not permitted here: {exc}')


def unlink_link(path: Path) -> None:
    """Remove a directory symlink itself (Windows removes one with rmdir)."""
    (os.rmdir if os.name == 'nt' else os.unlink)(path)


class ScratchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='win scratch ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.run_dir = self.root / 'runs' / 'run 1'
        self.run_dir.mkdir(parents=True)
        self.co = stub_coordinator(self.run_dir)
        self.outside = self.root / 'outside'
        (self.outside / 'keep').mkdir(parents=True)
        (self.outside / 'keep' / 'child.txt').write_bytes(b'keep\n')

    def assert_outside_untouched(self):
        self.assertEqual((self.outside / 'keep' / 'child.txt').read_bytes(), b'keep\n')
        self.assertEqual(sorted(os.listdir(self.outside)), ['keep'])

    def leftover(self, **receipt) -> Path:
        """A scratch left by turn N (as after a crash or a kept cleanup); the next dispatch is N + 1."""
        scratch = self.co._scratch_root('reviewer')
        (scratch / 'work.txt').write_bytes(b'x')
        sequence = int(scratch.name.split('-', 1)[0])
        self.co.state['turns'].append({'sequence': sequence, 'role': 'reviewer', 'vendor': 'codex', **receipt})
        self.co.state['sequence'] = sequence
        return scratch


class WindowsBranchTests(ScratchTest):
    """The Windows branches, run on every OS with the platform flag patched: ordinary path calls only."""

    def setUp(self):
        super().setUp()
        seam = patch.object(winplat, 'IS_WINDOWS', True)
        seam.start()
        self.addCleanup(seam.stop)

    def no_fd_calls(self):
        return patch.object(rc.os, 'open', side_effect=AssertionError('an fd-based open on the Windows branch'))

    def test_create_validate_list_and_remove_use_path_calls(self):
        with self.no_fd_calls():
            scratch = self.co._scratch_root('reviewer')
            self.assertEqual(scratch, self.run_dir / 'role-tmp' / '001-reviewer')
            self.assertTrue(winplat.real_dir(os.lstat(scratch)))
            (scratch / 'sub' / 'deeper').mkdir(parents=True)
            (scratch / 'sub' / 'deeper' / 'f.txt').write_bytes(b'x')
            (scratch / 'top.txt').write_bytes(b'y')
            os.chmod(scratch / 'top.txt', stat.S_IREAD)   # a read-only file (the read-only attribute on Windows)
            names, linked = rc.scratch_listing(scratch)
            self.assertEqual((names, linked), (['sub', 'sub/deeper', 'sub/deeper/f.txt', 'top.txt'] if os.sep == '/' else
                                               ['sub', 'sub\\deeper', 'sub\\deeper\\f.txt', 'top.txt'], []))
            self.co._drop_scratch(scratch)
        self.assertFalse(os.path.lexists(scratch))
        self.assertEqual(os.listdir(self.run_dir / 'role-tmp'), [])
        self.assertIsNone(self.co._scratch_root('author'))
        with self.assertRaisesRegex(RuntimeError, 'outside role-tmp'):
            self.co._drop_scratch(self.root)

    def test_a_link_inside_the_scratch_is_removed_as_itself_and_never_listed_through(self):
        scratch = self.co._scratch_root('shadow')
        symlink_or_skip(self, self.outside, scratch / 'to-dir')
        symlink_or_skip(self, self.outside / 'keep' / 'child.txt', scratch / 'to-file')
        (scratch / 'sub').mkdir()
        symlink_or_skip(self, self.outside / 'keep', scratch / 'sub' / 'deep')
        names, _ = rc.scratch_listing(scratch)
        self.assertFalse([name for name in names if 'child.txt' in name or name.endswith('keep')], names)
        with self.no_fd_calls():
            self.co._drop_scratch(scratch)
        self.assertFalse(os.path.lexists(scratch))
        self.assert_outside_untouched()

    def test_a_linked_role_tmp_or_scratch_is_refused_and_nothing_outside_is_touched(self):
        symlink_or_skip(self, self.outside, self.run_dir / 'role-tmp')
        with self.assertRaisesRegex(RuntimeError, 'role-tmp is not a real directory'):
            self.co._scratch_root('reviewer')
        with self.assertRaisesRegex(RuntimeError, 'role-tmp is not a real directory'):
            self.co._drop_scratch(self.run_dir / 'role-tmp' / 'keep')
        self.assert_outside_untouched()
        unlink_link(self.run_dir / 'role-tmp')
        os.mkdir(self.run_dir / 'role-tmp')
        symlink_or_skip(self, self.outside, self.run_dir / 'role-tmp' / '001-reviewer')   # a leftover that is a link
        self.co.state['spawn_failures'].append({'sequence': 1, 'child_created': False})
        with self.assertRaisesRegex(RuntimeError, 'unexpected entry in run_dir/role-tmp, refused'):
            self.co._scratch_root('reviewer')
        self.assert_outside_untouched()

    def test_role_tmp_outside_the_users_roots_is_accepted(self):   # a run dir on another drive (D:\\runs) still works
        os.mkdir(self.run_dir / 'role-tmp')
        with patch.object(winplat, '_under_user_roots', return_value=False):
            scratch = self.co._scratch_root('reviewer')
        self.assertTrue(scratch.is_dir())

    def test_the_scratch_root_must_stay_a_real_directory_but_its_mode_bits_are_not_checked(self):
        scratch = self.co._scratch_root('reviewer')
        os.chmod(scratch, 0o500 if os.name != 'nt' else stat.S_IREAD)
        self.assertEqual(rc.scratch_listing(scratch), ([], []))
        os.chmod(scratch, 0o700 if os.name != 'nt' else stat.S_IWRITE)
        os.rmdir(scratch)
        symlink_or_skip(self, self.outside, scratch)
        with self.assertRaisesRegex(ValueError, 'scratch temp root changed during the turn'):
            rc.scratch_listing(scratch)
        unlink_link(scratch)

    def test_a_leftover_is_swept_once_its_job_is_gone_and_kept_while_it_is_alive(self):
        left = self.leftover(pid=4242, job='Local\\review-loop-run-001-ab')
        with patch.object(winplat, 'probe_job', return_value=None) as probe, patch.object(winplat, 'kill_group') as kill, \
                self.assertRaisesRegex(RuntimeError, 'leftover read-only role scratch .*' + left.name +
                                       ' kept: the process group of that turn still answers'):
            self.co._scratch_root('gate')
        probe.assert_called_once_with('Local\\review-loop-run-001-ab')
        kill.assert_not_called()                                                             # the sweep probes, never kills
        self.assertEqual(((left / 'work.txt').read_bytes(), os.listdir(left.parent)), (b'x', [left.name]))
        self.assertIn(str(left), self.co.holds[-1])
        with patch.object(winplat, 'probe_job', side_effect=ProcessLookupError('job gone')):
            fresh = self.co._scratch_root('gate')
        self.assertFalse(os.path.lexists(left))
        self.assertEqual(os.listdir(left.parent), [fresh.name])

    def test_a_windows_leftover_without_a_job_name_is_kept(self):
        left = self.leftover(pid=4242)
        with patch.object(winplat, 'probe_job') as probe, \
                self.assertRaisesRegex(RuntimeError, left.name + ' kept: no verifiable pid'):
            self.co._scratch_root('gate')
        probe.assert_not_called()
        self.assertTrue(left.exists())

    def test_a_turn_that_ended_is_cleaned_up_after_its_job_is_confirmed_gone(self):
        scratch = self.co._scratch_root('reviewer')
        (scratch / 'out.txt').write_bytes(b'x')
        self.co.state['turns'].append({'sequence': 1, 'role': 'reviewer', 'pid': 4242, 'job': 'Local\\review-loop-run-001-cd'})
        with patch.object(winplat, 'probe_job', side_effect=[None, ProcessLookupError('job gone')]), \
                patch.object(winplat, 'kill_group') as kill:
            self.co._stop_turn_group(1)                                                       # what invoke() runs after a (timed-out) turn
        kill.assert_called_once_with(4242, 'Local\\review-loop-run-001-cd')
        self.co._drop_scratch(scratch)
        self.assertFalse(os.path.lexists(scratch))

    def test_run_dir_marks_use_mtime_and_link_count(self):
        own = self.run_dir / 'evidence' / '001-exec-reviewer'
        (self.run_dir / 'note.txt').write_bytes(b'n')
        info = os.lstat(self.run_dir / 'note.txt')
        marks = rc.run_dir_inodes(self.run_dir, set(), own)
        self.assertEqual(marks[str(self.run_dir / 'note.txt')], (info.st_ino, info.st_mtime_ns, info.st_nlink))
        os.utime(self.run_dir / 'note.txt', ns=(info.st_atime_ns, info.st_mtime_ns + 10 ** 9))   # a write shows as a new mtime
        self.assertNotEqual(rc.run_dir_inodes(self.run_dir, set(), own), marks)


class TempEnvTests(ScratchTest):
    def test_a_codex_read_only_role_gets_tmpdir_tmp_and_temp_on_its_scratch(self):
        for windows in ((False, True) if not winplat.IS_WINDOWS else (True,)):   # the POSIX branch needs dir_fd
            with self.subTest(windows=windows), patch.object(winplat, 'IS_WINDOWS', windows):
                seen = []
                def dispatch(role, phase, prompt, schema, fresh, allow, workspace, overrides):
                    seen.append(dict(overrides))
                    raise RuntimeError('captured')
                with patch.object(self.co, '_invoke_once', side_effect=dispatch, create=True), \
                        self.assertRaisesRegex(RuntimeError, 'captured'):
                    self.co.invoke('reviewer', 'PLAN', 'Role: reviewer.', {}, env_overrides={'KEEP': '1'})
                scratch = seen[0]['TMPDIR']
                self.assertEqual(seen, [{'KEEP': '1', 'TMPDIR': scratch, 'TMP': scratch, 'TEMP': scratch}])
                self.assertEqual(Path(scratch).parent, self.run_dir / 'role-tmp')
                self.assertFalse(os.path.lexists(scratch))                                    # dropped after the failed turn

    def test_the_author_temp_dir_is_also_tmp_and_temp_on_windows(self):
        with patch.object(winplat, 'IS_WINDOWS', True):
            self.assertEqual(winplat.temp_env(self.co.author_temp_dir), {key: str(self.co.author_temp_dir) for key in ('TMPDIR', 'TMP', 'TEMP')})
            env = self.co._author_environment()
        self.assertEqual((env['TMPDIR'], env['TMP'], env['TEMP']), (str(self.co.author_temp_dir),) * 3)


class CredentialReadDenyTests(unittest.TestCase):
    def setUp(self):
        home = patch.object(rc.Path, 'home', return_value=ROOT / 'home')
        home.start()
        self.addCleanup(home.stop)

    def test_every_claude_role_denies_the_read_tool_the_credential_paths_on_windows_only(self):
        co = fixture_coordinator()
        for role in ('author', 'reviewer', 'gate', 'shadow', 'probe', 'gate-probe'):
            with self.subTest(role=role):
                with patch.object(winplat, 'IS_WINDOWS', True):
                    deny = co._claude_sandbox_settings(role)['permissions']['deny']
                    self.assertEqual(deny, [f'Edit(//{winplat.claude_rule_path(co.run_dir)}/**)', *CREDENTIAL_READ_DENIES])
                with patch.object(winplat, 'IS_WINDOWS', False):
                    self.assertFalse([rule for rule in co._claude_sandbox_settings(role)['permissions']['deny'] if rule.startswith('Read(')])

    def test_the_rules_cover_every_credential_path_the_sandbox_hid(self):
        with patch.object(winplat, 'IS_WINDOWS', True):
            settings = fixture_coordinator()._claude_sandbox_settings('reviewer')
        hidden = [row['path'] for row in settings['sandbox']['credentials']['files']]
        self.assertEqual([rule[len('Read('):-1].rsplit('/**', 1)[0] for rule in CREDENTIAL_READ_DENIES], hidden)


@unittest.skipIf(winplat.IS_WINDOWS, 'POSIX pin')
class PosixPinTests(ScratchTest):
    def test_posix_temp_env_and_author_environment_are_tmpdir_only(self):
        self.assertEqual(winplat.temp_env('/x/author-tmp'), {'TMPDIR': '/x/author-tmp'})
        self.assertEqual(self.co._author_environment(), {**os.environ, 'TMPDIR': str(self.co.author_temp_dir)})

    def test_posix_marks_are_inode_and_ctime_and_the_scratch_stays_0700(self):
        (self.run_dir / 'note.txt').write_bytes(b'n')
        info = os.lstat(self.run_dir / 'note.txt')
        self.assertEqual(rc.run_dir_inodes(self.run_dir, set(), self.run_dir / 'x')[str(self.run_dir / 'note.txt')],
                         (info.st_ino, info.st_ctime_ns))
        scratch = self.co._scratch_root('reviewer')
        self.assertEqual((stat.S_IMODE(os.lstat(scratch).st_mode), stat.S_IMODE(os.lstat(scratch.parent).st_mode)), (0o700, 0o700))
        os.chmod(scratch, 0o500)
        with self.assertRaisesRegex(ValueError, 'scratch temp root mode changed'):
            rc.scratch_listing(scratch)
        with patch.object(winplat, 'remove_tree') as windows_remove:
            self.co._drop_scratch(scratch)
        windows_remove.assert_not_called()
        self.assertFalse(scratch.exists())


@unittest.skipUnless(winplat.IS_WINDOWS, 'native Windows NTFS attributes, junctions and Job Objects')
class NativeWindowsScratchTests(ScratchTest):
    def junction(self, link: Path, target: Path) -> None:
        done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True, text=True)
        if done.returncode:
            self.skipTest('mklink /J failed: ' + (done.stdout + done.stderr).strip())
        self.assertTrue(os.lstat(link).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)

    def test_read_only_files_and_directories_and_nested_trees_are_removed(self):
        scratch = self.co._scratch_root('reviewer')
        (scratch / 'sub' / 'deeper').mkdir(parents=True)
        for path in (scratch / 'top.txt', scratch / 'sub' / 'deeper' / 'f.txt'):
            path.write_bytes(b'x')
            os.chmod(path, stat.S_IREAD)
        os.chmod(scratch / 'sub' / 'deeper', stat.S_IREAD)                                # the read-only attribute on a directory
        self.assertTrue(os.lstat(scratch / 'top.txt').st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
        self.co._drop_scratch(scratch)
        self.assertFalse(os.path.lexists(scratch))

    def test_a_junction_inside_the_scratch_is_removed_as_itself_and_its_target_survives(self):
        scratch = self.co._scratch_root('shadow')
        self.junction(scratch / 'to-outside', self.outside)
        (scratch / 'sub').mkdir()
        self.junction(scratch / 'sub' / 'deep', self.outside / 'keep')
        names, _ = rc.scratch_listing(scratch)
        self.assertEqual(names, ['sub', 'sub\\deep', 'to-outside'])                         # listed, never descended
        self.co._drop_scratch(scratch)
        self.assertFalse(os.path.lexists(scratch))
        self.assert_outside_untouched()

    def test_a_directory_symlink_inside_the_scratch_is_removed_as_itself(self):
        scratch = self.co._scratch_root('shadow')
        symlink_or_skip(self, self.outside, scratch / 'to-dir')
        symlink_or_skip(self, self.outside / 'keep' / 'child.txt', scratch / 'to-file')
        self.co._drop_scratch(scratch)
        self.assertFalse(os.path.lexists(scratch))
        self.assert_outside_untouched()

    def test_a_junction_role_tmp_is_refused(self):
        self.junction(self.run_dir / 'role-tmp', self.outside)
        with self.assertRaisesRegex(RuntimeError, 'role-tmp is not a real directory'):
            self.co._scratch_root('reviewer')
        with self.assertRaisesRegex(RuntimeError, 'role-tmp is not a real directory'):
            self.co._drop_scratch(self.run_dir / 'role-tmp' / 'keep')
        self.assert_outside_untouched()
        os.rmdir(self.run_dir / 'role-tmp')
        os.mkdir(self.run_dir / 'role-tmp')
        self.junction(self.run_dir / 'role-tmp' / '001-reviewer', self.outside)           # a leftover scratch that is a junction
        self.co.state['spawn_failures'].append({'sequence': 1, 'child_created': False})
        with self.assertRaisesRegex(RuntimeError, 'unexpected entry in run_dir/role-tmp, refused'):
            self.co._scratch_root('reviewer')
        self.assert_outside_untouched()

    def spawn(self, scratch: Path, tag: str):
        """A turn's process tree that keeps a file in its scratch open, started through the Job Object launcher."""
        code = 'import sys, time; f = open(sys.argv[1], "w"); f.write("x"); f.flush(); time.sleep(60)'
        process, job = winplat.spawn_group([sys.executable, '-c', code, str(scratch / 'open.txt')], tag, cwd=self.root,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 30
        while not (scratch / 'open.txt').exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((scratch / 'open.txt').exists())
        return process, job

    def test_a_leftover_is_kept_while_its_job_is_alive_and_swept_once_it_is_gone(self):
        left = self.co._scratch_root('reviewer')
        process, job = self.spawn(left, 'sweep')
        sequence = int(left.name.split('-', 1)[0])
        self.co.state['turns'].append({'sequence': sequence, 'role': 'reviewer', 'pid': process.pid, 'job': job})
        self.co.state['sequence'] = sequence
        with self.assertRaisesRegex(RuntimeError, 'kept: the process group of that turn still answers'):
            self.co._scratch_root('gate')                                                     # resume: the job still runs
        self.assertTrue((left / 'open.txt').exists())
        winplat.kill_group(process.pid, job)
        process.wait(timeout=30)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try: winplat.probe_job(job)
            except ProcessLookupError: break
            time.sleep(0.05)
        fresh = self.co._scratch_root('gate')                                                 # gone: swept, the new scratch made
        self.assertFalse(os.path.lexists(left))
        self.assertEqual(os.listdir(left.parent), [fresh.name])

    def test_timeout_cleanup_kills_the_job_then_removes_the_scratch(self):
        scratch = self.co._scratch_root('reviewer')
        process, job = self.spawn(scratch, 'timeout')
        self.co.state['turns'].append({'sequence': 1, 'role': 'reviewer', 'pid': process.pid, 'job': job, 'timed_out': True})
        winplat.kill_group(process.pid, job)                                                  # the timeout path in _invoke_once
        process.wait(timeout=30)
        self.co._stop_turn_group(1)                                                           # then invoke(): stop, then drop
        self.co._drop_scratch(scratch)
        self.assertFalse(os.path.lexists(scratch))


if __name__ == '__main__':
    unittest.main()
