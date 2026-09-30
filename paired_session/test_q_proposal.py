import dataclasses
import hashlib
from pathlib import Path
import unittest
from paired_session import candidate_tree as ct
from paired_session import closeout_policy as cp
from paired_session import q_proposal as qp
from paired_session import test_closeout_policy as fixtures


class QProposalTests(unittest.TestCase):
    tearDown = fixtures.CloseoutPolicyTests.tearDown
    git = fixtures.CloseoutPolicyTests.git
    write_view = fixtures.CloseoutPolicyTests.write_view

    def setUp(self):
        fixtures.CloseoutPolicyTests.setUp(self)
        self.counter = 0
        self.prepare()

    def prepare(self, raw=None):
        if raw is not None:
            self.backlog.write_bytes(raw)
        (self.workspace / 'code.py').write_text('value = 1\n')
        self.git('add', 'BACKLOG.md', 'code.py')
        if self.git('status', '--porcelain'):
            self.git('commit', '-qm', 'Q source fixture')
        self.frozen = cp.freeze_item(self.workspace, 1)
        self.counter += 1
        run = self.root / ('run-' + str(self.counter))
        run.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, run, self.root, self.root, ('code.py',))
        self.baseline = dataclasses.replace(baseline, separate_filesystems=True)
        (self.baseline.root / 'code.py').write_text('value = 2\n')
        self.revision = ct.ingest_candidate_revision(self.baseline)
        self.env = ct._git_env(GIT_DIR=str(self.baseline.git_dir),
                               GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.test',
                               GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.test')
        self.c1 = self.commit(self.revision.tree_oid, self.baseline.parent_head)

    def commit(self, tree, *parents):
        args = ['commit-tree', tree]
        for parent in parents:
            args += ['-p', parent]
        return ct._git_bytes(args, env=self.env, input_bytes=b'Fixture C1\n').decode().strip()

    def materialize(self, frozen=None, c1=None):
        return qp.materialize(self.baseline, self.revision,
                               frozen if frozen is not None else self.frozen,
                               c1 if c1 is not None else self.c1, '2026-09-30')

    def test_q_object_is_backlog_only_unreviewed_and_does_not_change_live_or_p(self):
        live = self.backlog.read_bytes()
        index = self.baseline.index.read_bytes()
        refs = ct._git(['for-each-ref'], env=self.env)
        root_backlog = (self.baseline.root / 'BACKLOG.md').read_bytes()
        result = self.materialize()
        self.assertEqual(result['status'], 'UNREVIEWED')
        self.assertEqual(result['p_oid'], self.revision.tree_oid)
        self.assertEqual(result['parent'], self.baseline.parent_head)
        self.assertEqual(result['c1'], self.c1)
        self.assertEqual(ct._git(['diff-tree', '--no-commit-id', '--name-only', '-r',
                                  result['p_oid'], result['q_oid']], env=self.env), 'BACKLOG.md')
        blob = ct._git_bytes(['cat-file', 'blob', result['backlog_blob']], env=self.env)
        self.assertIn(('see ' + self.c1).encode(), blob)
        self.assertIn(b'  - Keep details.', blob)
        self.assertEqual(self.backlog.read_bytes(), live)
        self.assertEqual((self.baseline.root / 'BACKLOG.md').read_bytes(), root_backlog)
        self.assertEqual(self.baseline.index.read_bytes(), index)
        self.assertEqual(ct._git(['for-each-ref'], env=self.env), refs)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.frozen['head'])
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(result['c1_sha256'],
                         hashlib.sha256(ct._git_bytes(['cat-file', 'commit', self.c1], env=self.env)).hexdigest())

    def test_repeat_object_proposal_is_identical_and_never_an_approval(self):
        first = self.materialize()
        self.assertEqual(self.materialize(), first)
        self.assertNotIn('approval', first)
        self.assertNotIn('receipt', first)
        self.assertNotIn('accepted', first)
        self.assertEqual(first['status'], 'UNREVIEWED')

    def test_wrong_c1_tree_parent_or_multiple_parents_refuse(self):
        older = self.git('rev-parse', self.baseline.parent_head + '^')
        wrong = [self.commit(self.baseline.tree_oid, self.baseline.parent_head),
                 self.commit(self.revision.tree_oid, older),
                 self.commit(self.revision.tree_oid, self.baseline.parent_head, older)]
        for c1 in wrong:
            with self.subTest(c1=c1), self.assertRaisesRegex(ValueError, 'C1 tree or parent'):
                self.materialize(c1=c1)

    def test_bad_or_unknown_object_id_refuses(self):
        for c1 in ('bad', '--help', 'a' * 40, 'A' * 40, 'a' * 64, None, True):
            with self.subTest(c1=c1), self.assertRaises(ValueError):
                qp.materialize(self.baseline, self.revision, self.frozen, c1, '2026-09-30')

    def test_frozen_parent_adapter_or_backlog_drift_refuses(self):
        for field in ('head', 'close_adapter_sha256', 'backlog_sha256'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.materialize(frozen={**self.frozen, field: '0' * 64})

    def test_missing_newline_and_unindented_child_text_refuse(self):
        original = self.backlog.read_bytes()
        for raw in (original.rstrip(b'\n'),
                    original.replace(b'  - Keep details.', b'### Orphan notes'),
                    original.replace(b'  - Keep details.', b'orphan text')):
            self.prepare(raw)
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, 'final newline and indented child'):
                self.materialize()

    def test_changed_p_root_is_refused_before_q_object_mutation(self):
        refs = ct._git(['for-each-ref'], env=self.env)
        (self.baseline.root / 'code.py').write_text('changed after review\n')
        with self.assertRaises(ValueError):
            self.materialize()
        self.assertEqual(ct._git(['for-each-ref'], env=self.env), refs)
