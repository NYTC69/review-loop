import hashlib
import unittest
from paired_session import closeout_adapter as ca
from paired_session import closeout_policy as cp
from paired_session import test_closeout_policy as fixtures


class CloseoutAdapterTests(unittest.TestCase):
    setUp = fixtures.CloseoutPolicyTests.setUp
    tearDown = fixtures.CloseoutPolicyTests.tearDown
    git = fixtures.CloseoutPolicyTests.git
    write_view = fixtures.CloseoutPolicyTests.write_view

    def close(self, raw=None, frozen=None, ref='a' * 40, day='2026-09-30'):
        return ca.close_blob(raw if raw is not None else self.backlog.read_bytes(),
                             frozen if frozen is not None else cp.freeze_item(self.workspace, 1), ref, day)

    def test_exact_move_preserves_child_bullets_and_ref_without_writing(self):
        raw = self.backlog.read_bytes()
        output = self.close()
        self.assertEqual(self.backlog.read_bytes(), raw)
        text = output.decode()
        p1 = text.split('## P1\n')[1].split('## P2\n')[0]
        self.assertEqual(p1.strip(), '(none)')
        done = text.split('## Done\n')[1]
        self.assertIn('- ~~Fix sums. (added 2026-09-29)~~ (closed 2026-09-30, see ' + 'a' * 40 + ')', done)
        self.assertIn('  - Keep details.', done)
        self.assertEqual(done.count('- ~~Fix sums'), 1)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_five_done_blocks_retention_trims_oldest_with_children(self):
        source = self.backlog.read_text().replace('## Done\n\n(none)\n', '## Done\n\n' + ''.join(
            f'- ~~Old {i}~~ (closed 2026-09-28, see deadbeef)\n  - Old detail {i}\n\n' for i in range(6)))
        self.backlog.write_text(source)
        self.git('add', 'BACKLOG.md')
        self.git('commit', '-qm', 'done fixture')
        done = self.close().decode().split('## Done\n')[1]
        self.assertNotIn('Old 0', done)
        self.assertNotIn('Old 1', done)
        self.assertNotIn('Old detail 0', done)
        self.assertNotIn('Old detail 1', done)
        for i in range(2, 6):
            self.assertIn('Old detail ' + str(i), done)
        self.assertEqual(sum(line.startswith('- ') for line in done.splitlines()), 5)

    def test_foreign_or_modified_frozen_backlog_refuses(self):
        frozen = cp.freeze_item(self.workspace, 1)
        with self.assertRaisesRegex(ValueError, 'changed frozen BACKLOG'):
            self.close(self.backlog.read_bytes() + b'foreign', frozen)
        with self.assertRaisesRegex(ValueError, 'missing or ambiguous'):
            self.close(frozen={**frozen, 'title': 'missing'})
        with self.assertRaisesRegex(ValueError, 'open source'):
            self.close(frozen={**frozen, 'section': 'Done'})

    def test_bad_ref_or_date_refuses(self):
        for ref, day in [('short', '2026-09-30'), ('a' * 40, '2026-02-30'), ('a' * 40, '20260930')]:
            with self.subTest(ref=ref, day=day), self.assertRaises(ValueError):
                self.close(ref=ref, day=day)

    def test_malformed_sections_mixed_sentinel_and_header_refuse(self):
        original = self.backlog.read_bytes()
        variants = [original.replace(b'## P2', b'## Extra'),
                    original.replace(b'## P1\n\n', b'## P1\n\n(none)\n'),
                    original.replace(b'**Last updated**:', b'Unknown header:'),
                    original.replace(b'**Last updated**: 2026-09-30',
                                     b'**Last updated**: 2026-09-30\n**Last updated**: 2026-09-30')]
        frozen = cp.freeze_item(self.workspace, 1)
        for raw in variants:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.close(raw=raw, frozen={**frozen, 'backlog_sha256': hashlib.sha256(raw).hexdigest()})

    def test_other_open_item_stays_byte_identical_and_timestamp_updates(self):
        self.backlog.write_text(self.backlog.read_text().replace('## P2\n\n(none)',
            '## P2\n\n- Other issue. (added 2026-09-29)\n  - Keep me exactly.'))
        self.git('add', 'BACKLOG.md')
        self.git('commit', '-qm', 'other open fixture')
        self.assertIn('**Last updated**: 2026-10-01', self.close(day='2026-10-01').decode())
        source = self.backlog.read_text().split('## P2\n')[1].split('## P3\n')[0]
        result = self.close().decode().split('## P2\n')[1].split('## P3\n')[0]
        self.assertEqual(result, source)
