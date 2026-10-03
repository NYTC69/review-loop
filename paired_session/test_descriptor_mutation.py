import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from paired_session import descriptor_mutation as mutation


class DescriptorMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'workspace'
        self.store = self.base / 'quarantine'
        self.root.mkdir()
        self.store.mkdir()
        self.target = self.root / 'item'
        self.target.write_bytes(b'reviewed')
        self.allowed = [('100644', b'reviewed'), ('100644', b'new')]
        self.real_move = mutation._move

    def replace(self):
        return mutation.mutate(self.root, 'item', self.allowed, ('100644', b'new'), self.store)

    def test_normal_replace_create_delete_and_retained_originals(self):
        record = self.replace()
        self.assertEqual(self.target.read_bytes(), b'new')
        self.assertEqual((Path(record['originals']) / 'old').read_bytes(), b'reviewed')
        self.assertFalse(self.replace()['changed'])
        record = mutation.mutate(self.root, 'item', self.allowed, None, self.store)
        self.assertFalse(self.target.exists())
        self.assertEqual((Path(record['originals']) / 'old').read_bytes(), b'new')
        mutation.mutate(self.root, 'nested/new', [None], ('100644', b'created'), self.store)
        self.assertEqual((self.root / 'nested/new').read_bytes(), b'created')

    def test_leaf_symlink_and_hardlink_swaps_refuse_without_overwriting_foreign_bytes(self):
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind):
                if self.target.exists() or self.target.is_symlink():
                    self.target.unlink()
                self.target.write_bytes(b'reviewed')
                sentinel = self.base / ('outside-' + kind)
                sentinel.write_bytes(b'foreign')
                fired = False
                def swap(source_fd, source, target_fd, target):
                    nonlocal fired
                    if source == 'item' and target == 'old' and not fired:
                        fired = True
                        self.target.rename(self.base / ('reviewed-' + kind))
                        if kind == 'symlink':
                            self.target.symlink_to(sentinel)
                        else:
                            os.link(sentinel, self.target)
                    return self.real_move(source_fd, source, target_fd, target)
                with mock.patch.object(mutation, '_move', side_effect=swap):
                    with self.assertRaisesRegex(ValueError, 'target swapped'):
                        self.replace()
                self.assertTrue(fired)
                self.assertEqual(sentinel.read_bytes(), b'foreign')
                self.assertEqual(self.target.read_bytes(), b'foreign')
                self.assertEqual((self.base / ('reviewed-' + kind)).read_bytes(), b'reviewed')

    def test_directory_swap_writes_only_the_held_directory_inode(self):
        nested = self.root / 'nested'
        nested.mkdir()
        (nested / 'item').write_bytes(b'reviewed')
        identity = nested.stat()
        detached = self.base / 'detached'
        fired = False
        def swap(source_fd, source, target_fd, target):
            nonlocal fired
            if source == 'item' and target == 'old' and not fired:
                fired = True
                nested.rename(detached)
                nested.mkdir()
                (nested / 'item').write_bytes(b'foreign')
            return self.real_move(source_fd, source, target_fd, target)
        with mock.patch.object(mutation, '_move', side_effect=swap):
            record = mutation.mutate(self.root, 'nested/item', self.allowed, ('100644', b'new'), self.store)
        self.assertTrue(fired)
        self.assertEqual(record['parent_identity'], [identity.st_dev, identity.st_ino])
        self.assertEqual((detached / 'item').read_bytes(), b'new')
        self.assertEqual((nested / 'item').read_bytes(), b'foreign')

    def test_new_foreign_entry_between_moves_is_never_replaced(self):
        def collide(source_fd, source, target_fd, target):
            if source == 'new' and target == 'item':
                self.target.write_bytes(b'foreign')
            return self.real_move(source_fd, source, target_fd, target)
        with mock.patch.object(mutation, '_move', side_effect=collide):
            with self.assertRaisesRegex(ValueError, 'inspect/recover or abort'):
                self.replace()
        self.assertEqual(self.target.read_bytes(), b'foreign')
        originals = list(self.store.glob('*/old'))
        self.assertEqual(len(originals), 1)
        self.assertEqual(originals[0].read_bytes(), b'reviewed')

    def test_unknown_bytes_refuse_without_mutation(self):
        self.target.write_bytes(b'foreign')
        with self.assertRaisesRegex(ValueError, 'foreign mutation bytes'):
            self.replace()
        self.assertEqual(self.target.read_bytes(), b'foreign')
        self.assertEqual(list(self.store.iterdir()), [])
