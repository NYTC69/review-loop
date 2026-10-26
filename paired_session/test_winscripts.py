"""WI-WIN W8: the standalone scripts the coordinator runs (scripts/delivery_scope.py, scripts/security_preflight.py) on Windows,
where O_DIRECTORY, O_NOFOLLOW and dir_fd do not exist.

The Windows walk uses only path calls that exist on POSIX too, so most tests drive it on every OS (with
delivery_scope.IS_WINDOWS patched where a call site branches) and compare it with the POSIX descriptor walk.
CliTests run both scripts as the coordinator does (sys.executable, --repo, cwd = the repository) on the host platform.
NativeWindowsScriptTests (windows-latest CI, see windows-ci-modules.txt) use real directory junctions. PosixPinTests pin
that POSIX keeps the descriptor walk."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import security_preflight as sp

ds = sp.ds   # the module object security_preflight itself uses, so a patched IS_WINDOWS reaches its call site
IS_WINDOWS = os.name == 'nt'
REPO = Path(__file__).resolve().parents[1]
GIT_ENV = {'GIT_AUTHOR_NAME': 'w8', 'GIT_AUTHOR_EMAIL': 'w8@example.invalid', 'GIT_COMMITTER_NAME': 'w8',
           'GIT_COMMITTER_EMAIL': 'w8@example.invalid', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


def symlink_or_skip(test, target: Path, link: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        test.skipTest(f'symlinks are not permitted here: {exc}')


def git_blob(content: bytes) -> str:
    return hashlib.sha1(b'blob %d\0' % len(content) + content).hexdigest()


class TreeTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='w8 scripts ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        (self.repo / 'a' / 'b').mkdir(parents=True)
        (self.repo / 'a' / 'b' / 'file.txt').write_bytes(b'line one\r\nline two\n')
        (self.repo / 'top.txt').write_bytes(b'top\n')
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        (self.outside / 'file.txt').write_bytes(b'outside\n')


class WindowsWalkTests(TreeTest):
    def test_windows_link_reads_the_mode_and_the_reparse_attribute(self):
        self.assertTrue(ds.windows_link(SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)))
        self.assertTrue(ds.windows_link(SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_file_attributes=0x400)))
        self.assertFalse(ds.windows_link(SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_file_attributes=0x10)))
        self.assertFalse(ds.windows_link(SimpleNamespace(st_mode=stat.S_IFREG | 0o644)))

    def test_windows_leaf_walks_real_directories_only(self):
        self.assertEqual(ds.windows_leaf(str(self.repo), 'a/b/file.txt'), os.path.join(str(self.repo), 'a', 'b', 'file.txt'))
        self.assertEqual(ds.windows_leaf(str(self.repo), 'top.txt'), os.path.join(str(self.repo), 'top.txt'))
        self.assertIsNone(ds.windows_leaf(str(self.repo), 'missing/file.txt'))
        self.assertIsNone(ds.windows_leaf(str(self.repo), 'top.txt/file.txt'))   # a file where a directory was
        symlink_or_skip(self, self.outside, self.repo / 'linked')
        self.assertIsNone(ds.windows_leaf(str(self.repo), 'linked/file.txt'))

    def test_windows_disk_entry_hashes_raw_bytes_and_takes_the_index_mode(self):
        content = b'line one\r\nline two\n'
        entry = ds.windows_disk_entry(str(self.repo), 'a/b/file.txt', 'sha1')
        self.assertEqual(entry, {'mode': '100644', 'oid': git_blob(content),
                                 'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)})
        executable = [{'mode': '100755', 'oid': '0' * 40, 'stage': 0}]
        self.assertEqual(ds.windows_disk_entry(str(self.repo), 'a/b/file.txt', 'sha1', executable)['mode'], '100755')
        conflict = [{'mode': '100755', 'oid': '0' * 40, 'stage': 2}, {'mode': '100644', 'oid': '0' * 40, 'stage': 3}]
        self.assertEqual(ds.windows_disk_entry(str(self.repo), 'a/b/file.txt', 'sha1', conflict)['mode'], '100755')
        plain_link = [{'mode': '120000', 'oid': git_blob(content), 'stage': 0}]   # core.symlinks=false checkout
        self.assertEqual(ds.windows_disk_entry(str(self.repo), 'a/b/file.txt', 'sha1', plain_link),
                         {**entry, 'mode': '120000'})
        self.assertIsNone(ds.windows_disk_entry(str(self.repo), 'a/b', 'sha1'))          # a directory leaf
        self.assertIsNone(ds.windows_disk_entry(str(self.repo), 'a/gone.txt', 'sha1'))   # a missing leaf
        self.assertIsNone(ds.windows_disk_entry(str(self.repo), 'top.txt/x', 'sha1'))

    def test_windows_disk_entry_hashes_a_symlink_leaf_by_its_target_text_and_skips_a_linked_parent(self):
        symlink_or_skip(self, Path('a/b/file.txt'), self.repo / 'link')
        payload = b'a/b/file.txt'
        self.assertEqual(ds.windows_disk_entry(str(self.repo), 'link', 'sha1'),
                         {'mode': '120000', 'oid': git_blob(payload), 'sha256': hashlib.sha256(payload).hexdigest(),
                          'size': len(payload)})
        symlink_or_skip(self, self.outside, self.repo / 'linked')
        self.assertIsNone(ds.windows_disk_entry(str(self.repo), 'linked/file.txt', 'sha1'))

    def test_a_reparse_point_leaf_is_refused(self):
        leaf = os.path.join(str(self.repo), 'top.txt')
        real = os.lstat

        def lstat(path):
            info = real(path)
            if os.fspath(path) != leaf:
                return info
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o777, st_file_attributes=0x400, st_size=0, st_mtime_ns=0)

        with mock.patch.object(ds.os, 'lstat', side_effect=lstat):
            with self.assertRaisesRegex(ds.CaptureError, 'unsupported reparse point entry'):
                ds.windows_disk_entry(str(self.repo), 'top.txt', 'sha1')

    def test_a_file_changed_while_it_is_read_is_refused(self):
        real = os.fstat
        calls = []

        def fstat(fd):
            calls.append(fd)
            info = real(fd)
            return info if len(calls) == 1 else SimpleNamespace(st_size=info.st_size, st_mtime_ns=info.st_mtime_ns + 1)

        with mock.patch.object(ds.os, 'fstat', side_effect=fstat):
            with self.assertRaisesRegex(ds.CaptureError, 'file changed during capture'):
                ds.windows_disk_entry(str(self.repo), 'top.txt', 'sha1')

    @unittest.skipUnless(hasattr(os, 'O_DIRECTORY'), 'compares with the POSIX descriptor walk')
    def test_windows_disk_entry_agrees_with_the_posix_disk_entry(self):
        (self.repo / 'tool.sh').write_bytes(b'#!/bin/sh\n')
        os.chmod(self.repo / 'tool.sh', 0o755)
        symlink_or_skip(self, Path('a/b/file.txt'), self.repo / 'link')
        symlink_or_skip(self, self.outside, self.repo / 'linked')
        for path, mode in (('a/b/file.txt', '100644'), ('top.txt', '100644'), ('tool.sh', '100755'), ('link', None),
                           ('a/b', None), ('missing.txt', None), ('linked/file.txt', None), ('top.txt/x', None)):
            index = [{'mode': mode, 'oid': '0' * 40, 'stage': 0}] if mode else None
            self.assertEqual(ds.windows_disk_entry(str(self.repo), path, 'sha1', index),
                             ds.disk_entry(str(self.repo), path, 'sha1'), path)


class OpenRegularWindowsTests(TreeTest):
    """security_preflight._open_regular with IS_WINDOWS patched: path walk, binary open, no dir_fd."""

    def open(self, relative):
        real, calls = os.open, []

        def recording(*args, **kwargs):
            calls.append(kwargs)
            return real(*args, **kwargs)

        with mock.patch.object(ds, 'IS_WINDOWS', True), mock.patch.object(sp.os, 'open', side_effect=recording):
            try:
                return sp._open_regular(str(self.repo), relative)
            finally:
                self.assertTrue(all('dir_fd' not in row for row in calls), calls)

    def test_a_regular_file_is_opened_and_read_as_raw_bytes(self):
        fd = self.open('a/b/file.txt')
        try:
            self.assertEqual(os.read(fd, 100), b'line one\r\nline two\n')
        finally:
            os.close(fd)

    def test_links_directories_and_missing_parents_are_refused(self):
        with self.assertRaisesRegex(sp.ScanError, 'no longer a regular file'):
            self.open('a/b')
        with self.assertRaisesRegex(sp.ScanError, 'traverses a link or a missing directory'):
            self.open('missing/file.txt')
        with self.assertRaises(OSError):
            self.open('a/gone.txt')
        symlink_or_skip(self, self.repo / 'top.txt', self.repo / 'leaf-link')
        with self.assertRaisesRegex(sp.ScanError, 'no longer a regular file'):
            self.open('leaf-link')
        symlink_or_skip(self, self.outside, self.repo / 'linked')
        with self.assertRaisesRegex(sp.ScanError, 'traverses a link'):
            self.open('linked/file.txt')


class CliTests(unittest.TestCase):
    """Both scripts run as _security_script runs them, on the host platform: capture, an edit, manifest, preflight."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='w8 cli ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.repo = self.root / 'repo'
        (self.repo / 'pkg').mkdir(parents=True)
        (self.repo / 'pkg' / 'mod.py').write_bytes(b'VALUE = 1\n')
        (self.repo / 'README.md').write_bytes(b'readme\n')
        self.git('init', '-q')
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')
        self.out = self.root / 'out'
        self.out.mkdir()

    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.repo, env={**os.environ, **GIT_ENV}, check=True, capture_output=True)

    def script(self, name, *args):
        return subprocess.run([sys.executable, str(REPO / 'scripts' / name), '--repo', str(self.repo), *args],
                              cwd=self.repo, env={**os.environ, **GIT_ENV}, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=120)

    def test_capture_manifest_and_preflight_complete(self):
        baseline, manifest, report = self.out / 'baseline.json', self.out / 'manifest.json', self.out / 'report.json'
        done = self.script('delivery_scope.py', 'capture', '--scope', '.', '--output', str(baseline))
        self.assertEqual(done.returncode, 0, done.stderr)
        (self.repo / 'pkg' / 'mod.py').write_bytes(b'VALUE = 2\n')
        (self.repo / 'pkg' / 'new.py').write_bytes(b'NEW = 1\n')
        done = self.script('delivery_scope.py', 'manifest', '--baseline', str(baseline), '--output', str(manifest))
        self.assertEqual(done.returncode, 0, done.stderr)
        document = json.loads(manifest.read_text(encoding='utf-8'))
        self.assertEqual([row['path'] for row in document['task_delta']], ['pkg/mod.py', 'pkg/new.py'])
        self.assertEqual(document['current']['worktree']['pkg/new.py']['mode'], '100644')
        done = self.script('security_preflight.py', '--manifest', str(manifest), '--output', str(report),
                           '--run-changes-only')
        self.assertIn(done.returncode, (0, 1), done.stderr)
        result = json.loads(report.read_text(encoding='utf-8'))
        self.assertTrue(result['coverage_complete'], result)
        self.assertIn(result['status'], ('clean', 'review-required'), result)   # this bare repository has no .gitignore
        self.assertEqual(result['findings'], [])
        for path in (baseline, manifest, report):
            self.assertNotIn(b'\r\n', path.read_bytes(), path.name)


@unittest.skipUnless(IS_WINDOWS, 'native Windows directory junctions')
class NativeWindowsScriptTests(TreeTest):
    def junction(self, link: Path, target: Path) -> None:
        done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True, text=True)
        if done.returncode:
            self.skipTest('mklink /J failed: ' + (done.stdout + done.stderr).strip())
        self.assertTrue(os.lstat(link).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)

    def test_the_scripts_have_no_posix_descriptor_flags_here(self):
        self.assertTrue(ds.IS_WINDOWS)
        self.assertFalse(hasattr(os, 'O_DIRECTORY') or hasattr(os, 'O_NOFOLLOW'))

    def test_a_junction_component_is_absent_and_refused(self):
        self.junction(self.repo / 'joined', self.outside)
        self.assertIsNone(ds.windows_leaf(str(self.repo), 'joined/file.txt'))
        self.assertIsNone(ds.windows_disk_entry(str(self.repo), 'joined/file.txt', 'sha1'))
        with self.assertRaisesRegex(sp.ScanError, 'traverses a link'):
            sp._open_regular(str(self.repo), 'joined/file.txt')

    def test_a_junction_leaf_is_refused(self):
        self.junction(self.repo / 'joined', self.outside)
        with self.assertRaisesRegex(ds.CaptureError, 'unsupported reparse point entry'):
            ds.windows_disk_entry(str(self.repo), 'joined', 'sha1')
        with self.assertRaisesRegex(sp.ScanError, 'no longer a regular file'):
            sp._open_regular(str(self.repo), 'joined')


@unittest.skipIf(IS_WINDOWS, 'POSIX pin')
class PosixPinTests(TreeTest):
    def test_posix_keeps_the_descriptor_walk(self):
        self.assertFalse(ds.IS_WINDOWS)
        subprocess.run(['git', 'init', '-q'], cwd=self.repo, env={**os.environ, **GIT_ENV}, check=True)
        repo = ds.repository(str(self.repo))
        with mock.patch.object(ds, 'windows_disk_entry', side_effect=AssertionError('Windows walk on POSIX')), \
                mock.patch.object(sp, '_open_regular_windows', side_effect=AssertionError('Windows open on POSIX')):
            state = ds.read_state(repo)
            os.close(sp._open_regular(repo['worktree'], 'top.txt'))
        self.assertEqual(sorted(state['worktree']), ['a/b/file.txt', 'top.txt'])


if __name__ == '__main__':
    unittest.main()
