"""RR2 defaults and marker-based clone removal; stdlib unittest, Python 3.9."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import materialize_pr as mp
from scripts import project_paths as pp


class RootFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'project'
        self.repo.mkdir()

    def git_result(self, bare=False):
        return subprocess.CompletedProcess([], 0, ('true' if bare else 'false') + '\n' +
                                           str(self.repo / '.git') + '\n')


class ProjectPathsTests(RootFixture):
    def test_main_checkout_and_lane_use_same_root_and_create_ignore_once(self):
        with patch.object(pp.subprocess, 'run', return_value=self.git_result()) as run:
            for workspace in (self.repo, self.root / 'lane'):
                self.assertEqual(pp.default_root(workspace), self.repo / '.review-loop')
                self.assertEqual(run.call_args[0][0], ['git', '-C', str(workspace), 'rev-parse',
                    '--is-bare-repository', '--path-format=absolute', '--git-common-dir'])
        ignore = self.repo / '.review-loop/.gitignore'
        self.assertEqual(ignore.read_bytes(), b'*\n!config.md\n')
        ignore.write_text('custom\n')
        with patch.object(pp.subprocess, 'run', return_value=self.git_result()):
            pp.default_root(self.repo)
        self.assertEqual(ignore.read_text(), 'custom\n')
        self.assertFalse((self.repo / '.review-loop/codex-home').exists())

    def test_bare_nonrepo_unavailable_git_and_relative_xdg_fallback_notice(self):
        cases = [self.git_result(True), subprocess.CalledProcessError(128, 'git'), OSError('no git')]
        for outcome in cases:
            for xdg in (str(self.root / 'state'), 'relative', ''):
                with self.subTest(outcome=type(outcome).__name__, xdg=xdg):
                    kwargs = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                    with patch.object(pp.subprocess, 'run', **kwargs), \
                            patch.dict(os.environ, {'XDG_STATE_HOME': xdg}), \
                            patch.object(pp.Path, 'home', return_value=self.root / 'home'), \
                            contextlib.redirect_stderr(io.StringIO()) as err:
                        expected = Path(xdg) if xdg and Path(xdg).is_absolute() else self.root / 'home/.local/state'
                        self.assertEqual(pp.default_root(self.repo), expected / 'review-loop')
                    self.assertIn('falling back to ' + str(expected / 'review-loop'), err.getvalue())
        self.assertFalse((self.repo / '.review-loop').exists())

    def test_root_script_prints_absolute_root(self):
        with patch.object(pp.subprocess, 'run', return_value=self.git_result()), \
                patch.object(sys, 'argv', ['project_paths.py', str(self.root / 'lane')]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            pp.main()
        self.assertEqual(out.getvalue().strip(), str(self.repo / '.review-loop'))


class MaterializeDefaultsTests(RootFixture):
    def resolution(self):
        oid = 'a' * 40
        pin = {'oid': oid, 'source': str(self.repo), 'fetch_ref': None}
        return {'operator_repo': str(self.repo), 'target_url': str(self.repo),
                'head': pin, 'base': pin, 'kind': 'ref'}

    @contextlib.contextmanager
    def fake_git(self):
        def command(argv, cwd, **kwargs):
            if 'merge-base' in argv:
                return 'a' * 40
            return ''

        def step(what, argv, cwd, **kwargs):
            if what == 'clone':
                (Path(argv[-1]) / '.git').mkdir()
            if what.endswith('verify'):
                return 'a' * 40
            return ''

        with patch.object(pp.subprocess, 'run', return_value=self.git_result()), \
                patch.object(mp, 'operator_areas', return_value={self.repo, self.repo / '.git'}), \
                patch.object(mp, 'git_step', side_effect=step), \
                patch.object(mp, 'command', side_effect=command):
            yield

    def test_default_materialize_new_root_ignore_and_remove_new_and_old(self):
        with self.fake_git():
            new = mp.materialize(self.resolution())
            old = mp.materialize(self.resolution(), self.root / 'state/review-loop')
        new_path, old_path = (Path(row['workspace']) for row in (new, old))
        self.assertEqual(new_path.parent, self.repo / '.review-loop/pr')
        self.assertEqual(old_path.parent, self.root / 'state/review-loop/pr')
        self.assertEqual((self.repo / '.review-loop/.gitignore').read_bytes(), b'*\n!config.md\n')
        for path in (new_path, old_path):
            self.assertEqual(mp.remove(path), str(path))
            self.assertFalse(path.exists())

    def test_existing_ignore_is_preserved_and_nonignored_clone_refused(self):
        pp.ensure_ignore(self.repo)
        ignore = self.repo / '.review-loop/.gitignore'
        ignore.write_text('custom\n')
        with self.fake_git():
            made = mp.materialize(self.resolution())
        self.assertEqual(ignore.read_text(), 'custom\n')
        mp.remove(made['workspace'])
        with self.fake_git(), patch.object(mp, 'command', side_effect=mp.ResolutionError('not ignored')):
            with self.assertRaisesRegex(mp.MaterializeError, 'not gitignored'):
                mp.materialize(self.resolution())
        self.assertEqual(list((self.repo / '.review-loop/pr').iterdir()), [])

    def test_remove_refuses_unmarked_directories_under_both_roots(self):
        for root in (self.repo / '.review-loop', self.root / 'state/review-loop'):
            path = root / 'pr' / ('b' * 32)
            (path / '.git').mkdir(parents=True)
            with self.assertRaisesRegex(mp.MaterializeError, 'marker'):
                mp.remove(path)
            self.assertTrue(path.exists())

    def test_cli_root_without_value_uses_default_explicit_root_wins_and_remove_works(self):
        for args, expected in ((['--root'], self.repo / '.review-loop/pr'),
                               (['--root', str(self.root / 'chosen')], self.root / 'chosen/pr')):
            with self.fake_git(), patch.object(mp, 'resolve', return_value=self.resolution()), \
                    patch.object(sys, 'argv', ['materialize_pr.py', 'topic', *args]), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(mp.main(), 0)
            workspace = Path(json.loads(out.getvalue())['workspace'])
            self.assertEqual(workspace.parent, expected)
            with patch.object(sys, 'argv', ['materialize_pr.py', '--remove', str(workspace)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mp.main(), 0)
            self.assertFalse(workspace.exists())


class RealGitDefaultsTests(RootFixture):
    def git(self, repo, *args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True,
            env={**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}).strip()

    def test_real_lane_default_clone_ignore_preservation_and_cli_remove(self):
        # All writes belong to disposable test repos; never the user's repository.
        self.git(self.repo, 'init', '-q')
        (self.repo / 'file').write_text('content\n')
        self.git(self.repo, 'add', 'file')
        self.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'fixture')
        lane = self.root / 'lane'
        self.git(self.repo, 'worktree', 'add', '-q', '--detach', str(lane))
        self.assertEqual(pp.default_root(lane), self.repo / '.review-loop')
        ignore = self.repo / '.review-loop/.gitignore'
        self.assertEqual(ignore.read_bytes(), b'*\n!config.md\n')
        ignore.write_text('*\n!config.md\n# preserve me\n')
        oid = self.git(lane, 'rev-parse', 'HEAD')
        pin = {'oid': oid, 'source': str(lane), 'fetch_ref': 'HEAD'}
        made = mp.materialize({'operator_repo': str(lane), 'target_url': str(lane),
                               'head': pin, 'base': pin})
        path = Path(made['workspace'])
        self.assertEqual(path.parent, self.repo / '.review-loop/pr')
        self.assertEqual(self.git(path, 'rev-parse', 'HEAD'), oid)
        self.assertEqual(self.git(self.repo, 'check-ignore', str(path)), str(path))
        self.assertEqual(ignore.read_text(), '*\n!config.md\n# preserve me\n')
        result = subprocess.run([sys.executable, str(Path(mp.__file__).resolve()), '--remove', str(path)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertFalse((lane / '.review-loop').exists())

    def test_real_bare_repository_uses_fallback(self):
        bare = self.root / 'bare.git'
        self.git(self.root, 'init', '-q', '--bare', str(bare))
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(self.root / 'state')}), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pp.default_root(bare), self.root / 'state/review-loop')
        self.assertIn('falling back', err.getvalue())


if __name__ == '__main__':
    unittest.main()
