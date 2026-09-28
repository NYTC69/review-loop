import dataclasses
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from paired_session import candidate_tree as ct
from paired_session import finish_dispatch as fd


class FinishDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.workspace)], check=True)
        subprocess.run(['git', '-C', str(self.workspace), 'config', 'user.email', 'fake@example.test'], check=True)
        subprocess.run(['git', '-C', str(self.workspace), 'config', 'user.name', 'Fake'], check=True)
        (self.workspace / 'tracked.txt').write_text('base\n')
        subprocess.run(['git', '-C', str(self.workspace), 'add', 'tracked.txt'], check=True)
        subprocess.run(['git', '-C', str(self.workspace), 'commit', '-qm', 'base'], check=True)
        run_dir = self.root / 'run'
        run_dir.mkdir()
        scratch = self.root / 'scratch'
        scratch.mkdir()
        self.baseline = ct.prepare_candidate_baseline(
            self.workspace, run_dir, scratch, scratch, ('tracked.txt',))
        self.revision = ct.ingest_candidate_revision(self.baseline)
        approval = {'candidate_oid': self.revision.tree_oid, 'epoch': 1, 'phase': 'EXEC',
                    'run_id': run_dir.name, 'convergence_id': 'exec-1',
                    'workspace': str(self.workspace.resolve()), 'run_dir': str(run_dir.resolve()),
                    'parent_head': self.baseline.parent_head}
        self.proof = {'run_id': run_dir.name, 'convergence_id': 'exec-1', 'blocking_findings': [],
                      'reviewer': {**approval, 'role': 'reviewer', 'status': 'APPROVE'},
                      'gate': {**approval, 'role': 'gate', 'verdict': 'approve'}}

    def call(self, baseline=None, proof=None, launch=None, sandbox_stopped=None):
        return fd.dispatch(baseline or self.baseline, self.revision, proof or self.proof,
                           1, hashlib.sha256(b'frozen role').hexdigest(),
                           launch or self.fake_launch, sandbox_stopped or (lambda identity: True))

    def fake_launch(self, request):
        script = self.root / 'fake-finisher-cli.py'
        script.write_text('from pathlib import Path\nimport sys\n'
                          'Path(sys.argv[1], "tracked.txt").write_text("finished\\n")\n')
        child = subprocess.Popen([sys.executable, str(script), request['candidate_root']])
        self.assertEqual(child.wait(), 0)
        return {'sandbox_id': 'fake-' + str(child.pid),
                'request_sha256': request['sha256'], 'status': 'READY'}

    def test_unisolated_candidate_refuses_before_fake_cli(self):
        with self.assertRaisesRegex(fd.FinishDispatchError, 'not isolated'):
            self.call(launch=lambda request: self.fail('must not launch'))

    def test_fake_cli_write_invalidates_exec_and_preserves_live_workspace(self):
        isolated = dataclasses.replace(self.baseline, separate_filesystems=True)
        seen = []
        result = self.call(baseline=isolated, sandbox_stopped=lambda identity: seen.append(identity) or True)
        self.assertEqual(result['status'], 'EXEC_REVIEW_REQUIRED')
        self.assertTrue(result['requires_fresh_exec_and_gate'])
        self.assertTrue(seen)
        self.assertEqual((self.workspace / 'tracked.txt').read_text(), 'base\n')

    def test_stale_gate_refuses_before_fake_cli(self):
        isolated = dataclasses.replace(self.baseline, separate_filesystems=True)
        stale = {**self.proof, 'gate': {**self.proof['gate'], 'convergence_id': 'exec-0'}}
        with self.assertRaisesRegex(fd.FinishDispatchError, 'current-convergence'):
            self.call(baseline=isolated, proof=stale,
                      launch=lambda request: self.fail('must not launch'))

    def test_reviewer_only_approval_cannot_impersonate_gate(self):
        isolated = dataclasses.replace(self.baseline, separate_filesystems=True)
        missing = {**self.proof, 'gate': self.proof['reviewer']}
        with self.assertRaisesRegex(fd.FinishDispatchError, 'current-convergence'):
            self.call(baseline=isolated, proof=missing,
                      launch=lambda request: self.fail('must not launch'))

    def test_group_stop_proof_is_required_before_ingest(self):
        isolated = dataclasses.replace(self.baseline, separate_filesystems=True)
        with self.assertRaisesRegex(fd.FinishDispatchError, 'not proven stopped'):
            self.call(baseline=isolated, sandbox_stopped=lambda identity: False)
        self.assertEqual((self.workspace / 'tracked.txt').read_text(), 'base\n')

    def test_clean_fake_cli_turn_still_requires_tests(self):
        isolated = dataclasses.replace(self.baseline, separate_filesystems=True)
        result = self.call(baseline=isolated, launch=lambda request: {
            'sandbox_id': 'fake-clean', 'request_sha256': request['sha256'], 'status': 'READY'})
        self.assertEqual(result['status'], 'TESTS_REQUIRED')
        self.assertFalse(result['requires_fresh_exec_and_gate'])
