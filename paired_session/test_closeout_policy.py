import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest
from paired_session import closeout_policy as cp


class CloseoutPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / 'ws'
        self.workspace.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.test')
        self.backlog = self.workspace / 'BACKLOG.md'
        self.backlog.write_text('# Backlog\n\n**Last updated**: 2026-09-30\n\n## P0\n\n(none)\n\n'
                                '## P1\n\n- Fix sums. (added 2026-09-29)\n  - Keep details.\n\n'
                                '## P2\n\n(none)\n\n## P3\n\n(none)\n\n## Done\n\n(none)\n')
        (self.workspace / '.gitignore').write_text('.compass/\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        self.view = self.workspace / '.compass/backlog-last-view.json'
        self.view.parent.mkdir()
        self.data = {'generated_at': datetime.now(timezone.utc).isoformat(),
                     'source_path': str(self.backlog), 'filter_text': '',
                     'items': [{'id': 1, 'section': 'P1', 'title_span': 'Fix sums',
                                'normalized_title': 'fix sums', 'preview': 'Fix sums'}]}
        self.write_view()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.workspace), *args], stderr=subprocess.STDOUT).decode().strip()

    def write_view(self):
        self.view.write_text(json.dumps(self.data))

    def test_freeze_records_exact_committed_blob_and_adapter_without_mutation(self):
        before = self.backlog.read_bytes()
        result = cp.freeze_item(self.workspace, 1)
        self.assertEqual(result['title'], 'Fix sums')
        self.assertEqual(result['section'], 'P1')
        self.assertEqual(result['head'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(result['backlog_blob'], self.git('rev-parse', 'HEAD:BACKLOG.md'))
        self.assertEqual(result['backlog_sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(result['view_sha256'], hashlib.sha256(self.view.read_bytes()).hexdigest())
        self.assertEqual(result['adapter_sha256'], hashlib.sha256(Path(cp.__file__).read_bytes()).hexdigest())
        self.assertEqual(self.backlog.read_bytes(), before)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_dirty_index_workspace_and_backlog_refuse(self):
        for action in ('untracked', 'backlog', 'index'):
            with self.subTest(action=action):
                if action == 'untracked':
                    (self.workspace / 'foreign.txt').write_text('user work')
                else:
                    self.backlog.write_text(self.backlog.read_text() + '\nforeign\n')
                    if action == 'index':
                        self.git('add', 'BACKLOG.md')
                with self.assertRaisesRegex(ValueError, 'dirty'):
                    cp.freeze_item(self.workspace, 1)
                if action == 'untracked':
                    (self.workspace / 'foreign.txt').unlink()
                else:
                    self.git('restore', '--staged', '--worktree', 'BACKLOG.md')

    def test_ambiguous_title_wrong_section_and_stale_title_refuse(self):
        for change in ('section', 'title', 'duplicate'):
            with self.subTest(change=change):
                if change == 'section':
                    self.data['items'][0]['section'] = 'P2'
                elif change == 'title':
                    self.data['items'][0].update(section='P1', title_span='Missing')
                else:
                    self.data['items'][0]['title_span'] = 'Fix sums'
                    self.backlog.write_text(self.backlog.read_text().replace('## P2\n\n(none)',
                                                                           '## P2\n\n- FIX SUMS!'))
                    self.git('add', 'BACKLOG.md')
                    self.git('commit', '-qm', 'duplicate fixture')
                self.write_view()
                with self.assertRaisesRegex(ValueError, 'stale or ambiguous'):
                    cp.freeze_item(self.workspace, 1)

    def test_malformed_view_and_bad_id_refuse(self):
        for item_id in (0, -1, True, '1', 2):
            with self.subTest(item_id=item_id), self.assertRaises(ValueError):
                cp.freeze_item(self.workspace, item_id)
        for value in (None, {}, {'items': []}, {'source_path': str(self.backlog), 'generated_at': 'bad'}):
            self.view.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                cp.freeze_item(self.workspace, 1)

    def test_old_future_and_naive_view_timestamps_refuse(self):
        for stamp in (datetime.now(timezone.utc) - timedelta(minutes=11),
                      datetime.now(timezone.utc) + timedelta(minutes=1), datetime.now()):
            self.data['generated_at'] = stamp.isoformat()
            self.write_view()
            with self.subTest(stamp=stamp), self.assertRaisesRegex(ValueError, 'fresh Compass view'):
                cp.freeze_item(self.workspace, 1)

    def test_view_source_and_symlinks_refuse(self):
        self.data['source_path'] = str(self.root / 'BACKLOG.md')
        self.write_view()
        with self.assertRaisesRegex(ValueError, 'another BACKLOG'):
            cp.freeze_item(self.workspace, 1)
        self.data['source_path'] = str(self.backlog)
        self.write_view()
        for path in (self.view, self.backlog):
            raw = path.read_bytes()
            path.unlink()
            target = self.root / ('external-' + path.name)
            target.write_bytes(raw)
            path.symlink_to(target)
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'regular repo-root'):
                cp.freeze_item(self.workspace, 1)
            path.unlink()
            path.write_bytes(raw)

    def test_git_worktree_backlog_freezes_without_assuming_dot_git_directory(self):
        other = self.root / 'worktree'
        self.git('worktree', 'add', '-q', '-b', 'fixture-worktree', str(other))
        other_view = other / '.compass/backlog-last-view.json'
        other_view.parent.mkdir()
        self.data['source_path'] = str(other / 'BACKLOG.md')
        other_view.write_text(json.dumps(self.data))
        self.assertEqual(cp.freeze_item(other, 1)['backlog_blob'], self.git('rev-parse', 'HEAD:BACKLOG.md'))
