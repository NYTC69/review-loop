import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
from paired_session import safe_temp


class SafeTempTests(unittest.TestCase):
    def test_symlink_targets_preserve_contents_modes_and_flags(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            outside = root / 'outside'
            outside.mkdir()
            sentinel = outside / 'sentinel'
            sentinel.write_text('keep')
            sentinel.chmod(0o400)
            before = sentinel.stat()
            with safe_temp.directory(dir=root) as scratch:
                path = Path(scratch)
                (path / 'file-link').symlink_to(sentinel)
                (path / 'dir-link').symlink_to(outside, target_is_directory=True)
                nested = path / 'nested'
                nested.mkdir()
                (nested / 'dangling').symlink_to(outside / 'missing')
                (nested / 'ordinary').write_text('owned')
            self.assertFalse(path.exists())
            self.assertEqual(sentinel.read_text(), 'keep')
            self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), stat.S_IMODE(before.st_mode))
            self.assertEqual(getattr(sentinel.stat(), 'st_flags', 0), getattr(before, 'st_flags', 0))

    def test_permission_error_never_resets_link_target_permissions(self):
        with tempfile.TemporaryDirectory() as base:
            sentinel = Path(base) / 'outside'
            sentinel.write_text('keep')
            sentinel.chmod(0o400)
            manager = safe_temp.directory(dir=base)
            scratch = Path(manager.__enter__())
            (scratch / 'link').symlink_to(sentinel)
            with mock.patch.object(safe_temp.os, 'unlink', side_effect=PermissionError('deny')):
                with self.assertRaises(PermissionError):
                    manager.__exit__(None, None, None)
            self.assertEqual(sentinel.read_text(), 'keep')
            self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o400)
            (scratch / 'link').unlink()
            scratch.rmdir()

    def test_replaced_root_is_refused_without_touching_foreign_tree(self):
        with tempfile.TemporaryDirectory() as base:
            manager = safe_temp.directory(dir=base)
            scratch = Path(manager.__enter__())
            moved = Path(base) / 'moved'
            scratch.rename(moved)
            scratch.mkdir()
            sentinel = scratch / 'foreign'
            sentinel.write_text('keep')
            with self.assertRaisesRegex(RuntimeError, 'root replaced'):
                manager.__exit__(None, None, None)
            self.assertEqual(sentinel.read_text(), 'keep')
