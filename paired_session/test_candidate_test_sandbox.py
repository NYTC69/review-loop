import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from paired_session import candidate_test_sandbox as sandbox


class CandidateSandboxGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / 'nest' / 'workspace'
        self.run_dir = self.base / 'run'
        self.root = self.base / 'candidate'
        for path in (self.workspace, self.run_dir / 'evidence', self.root):
            path.mkdir(parents=True)
        binaries = []
        for name in ('fake-codex', 'fake-claude'):
            path = self.base / name
            path.write_text('#!/bin/sh\n# fake_cli.py fixture\n')
            binaries.append(str(path))
        args = SimpleNamespace(workspace=str(self.workspace), run_dir=str(self.run_dir),
                               workitem=str(self.base / 'WORKITEM.md'),
                               codex_bin=binaries[0], claude_bin=binaries[1])
        self.co = SimpleNamespace(_fake_lifecycle=True, workspace=self.workspace,
                                  run_dir=self.run_dir, evidence=self.run_dir / 'evidence', args=args)

    def test_module_guard_refuses_bad_scope_even_if_outer_guard_is_bypassed(self):
        for scope in (None, '/', str(Path.home())):
            with self.subTest(scope=scope), mock.patch.dict(os.environ, clear=False):
                if scope is None:
                    os.environ.pop('FAKE_CODEX_TEST_ROOT', None)
                else:
                    os.environ['FAKE_CODEX_TEST_ROOT'] = scope
                with mock.patch.object(sandbox.spine, 'fake_dispatch_guard', return_value=True):
                    with mock.patch.object(sandbox.subprocess, 'Popen') as spawn:
                        with self.assertRaises(RuntimeError):
                            sandbox.run(self.co, None, cwd=self.root, env={}, timeout=5)
                        spawn.assert_not_called()

    def test_equal_ancestor_descendant_and_alias_roots_refuse_before_spawn(self):
        inside = self.workspace / 'inside'
        inside.mkdir()
        alias = self.base / 'alias'
        alias.symlink_to(self.workspace, target_is_directory=True)
        for root in (self.workspace, self.run_dir, self.base, self.base / 'nest', inside, alias):
            with self.subTest(root=root), mock.patch.dict(os.environ, {'FAKE_CODEX_TEST_ROOT': str(self.base)}):
                with mock.patch.object(sandbox.spine, 'fake_dispatch_guard', return_value=True):
                    with mock.patch.object(sandbox.subprocess, 'Popen') as spawn:
                        with self.assertRaises(RuntimeError):
                            sandbox.run(self.co, None, cwd=root, env={}, timeout=5)
                        spawn.assert_not_called()

    def test_broad_relative_and_spoofed_home_scopes_refuse(self):
        for scope in ('.', '/private', '/tmp', tempfile.gettempdir()):
            with self.subTest(scope=scope), mock.patch.dict(os.environ, {'FAKE_CODEX_TEST_ROOT': scope}):
                with mock.patch.object(sandbox.spine, 'fake_dispatch_guard', return_value=True):
                    with mock.patch.object(sandbox.subprocess, 'Popen') as spawn:
                        with self.assertRaises(RuntimeError):
                            sandbox.run(self.co, None, cwd=self.root, env={}, timeout=5)
                        spawn.assert_not_called()
        with mock.patch.dict(os.environ, {'FAKE_CODEX_TEST_ROOT': str(self.base), 'HOME': '/nonexistent'}):
            with mock.patch.object(sandbox.pwd, 'getpwuid', return_value=SimpleNamespace(pw_dir=str(self.base))):
                with mock.patch.object(sandbox.spine, 'fake_dispatch_guard', return_value=True):
                    with mock.patch.object(sandbox.subprocess, 'Popen') as spawn:
                        with self.assertRaises(RuntimeError):
                            sandbox.run(self.co, None, cwd=self.root, env={}, timeout=5)
                        spawn.assert_not_called()

    def test_real_wrappers_and_missing_fake_lock_refuse(self):
        for change in ('lock', 'wrapper'):
            with self.subTest(change=change), mock.patch.dict(os.environ, {'FAKE_CODEX_TEST_ROOT': str(self.base)}):
                self.co._fake_lifecycle = change != 'lock'
                if change == 'wrapper':
                    Path(self.co.args.codex_bin).write_text('#!/bin/sh\nexec codex "$@"\n')
                with mock.patch.object(sandbox.subprocess, 'Popen') as spawn:
                    with self.assertRaises(RuntimeError):
                        sandbox.run(self.co, None, cwd=self.root, env={}, timeout=5)
                    spawn.assert_not_called()

    def test_executed_candidate_control_receives_eof_and_still_writes_inside(self):
        with mock.patch.dict(os.environ, {'FAKE_CODEX_TEST_ROOT': str(self.base)}):
            result = sandbox.run(self.co, [sys.executable, '-c',
                "import os,sys,pathlib; assert os.path.samestat(os.fstat(0),os.stat('/dev/null')); assert sys.stdin.buffer.read()==b''; pathlib.Path('owned').write_text('ok')"],
                cwd=self.root, env=dict(os.environ), timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'owned').read_text(), 'ok')
        self.assertEqual(result.write_boundary['root'], str(self.root))
