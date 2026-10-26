"""W5: Git Bash tests, Git modes and unchanged POSIX execution (stdlib, Python 3.9)."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paired_session import candidate_tree as ct, coordinator as rc, winplat

REPO = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout


def executor(root, command):
    co = object.__new__(rc.Coordinator)
    co.workspace = root / 'workspace'
    co.workspace.mkdir()
    git(co.workspace, 'init', '-q')
    co.run_dir = root / 'run'
    co.run_dir.mkdir()
    co.evidence = co.run_dir / 'evidence'
    co.evidence.mkdir()
    co.state = {'sequence': 1}
    co.args = SimpleNamespace(test_command=command, timeout=15)
    co.progress = lambda *a, **kw: None
    return co


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(winplat.git_bash.cache_clear)
        winplat.git_bash.cache_clear()
        self.stack = patch.object(winplat, 'IS_WINDOWS', True)
        self.stack.start()
        self.addCleanup(self.stack.stop)

    def file(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_discovery_order_and_process_cache(self):
        override = self.file('custom/bash.exe')
        bash = self.file('Git/bin/bash.exe')
        core = self.root / 'Git/mingw64/libexec/git-core'
        fallback = self.file('fallback/bin/bash.exe')
        program = self.file('Program Files/Git/bin/bash.exe')
        result = SimpleNamespace(returncode=0, stdout=str(core) + '\n')
        with patch.dict(os.environ, {'CLAUDE_CODE_GIT_BASH_PATH': str(override), 'ProgramFiles': str(program.parents[2])}), \
                patch('subprocess.run', return_value=result) as run, \
                patch('shutil.which', return_value=str(fallback.parent.parent / 'cmd/git.exe')):
            self.assertEqual(winplat.git_bash(), str(override))
            run.assert_not_called()
            override.unlink()
            self.assertEqual(winplat.git_bash(), str(override))  # frozen for this process
            winplat.git_bash.cache_clear()
            self.assertEqual(winplat.git_bash(), str(bash))
            bash.unlink()
            winplat.git_bash.cache_clear()
            self.assertEqual(winplat.git_bash(), str(fallback))
            fallback.unlink()
            winplat.git_bash.cache_clear()
            self.assertEqual(winplat.git_bash(), str(program))
            program.unlink()
            winplat.git_bash.cache_clear()
            self.assertIsNone(winplat.git_bash())

    def test_wsl_and_windowsapps_overrides_are_never_selected(self):
        for name in ('Windows/System32/bash.exe', 'user/WindowsApps/bash.exe'):
            bad = self.file(name)
            with self.subTest(name=name), patch.dict(os.environ, {'CLAUDE_CODE_GIT_BASH_PATH': str(bad),
                    'ProgramFiles': str(self.root / 'missing')}), \
                    patch('subprocess.run', side_effect=FileNotFoundError), patch('shutil.which', return_value=None):
                winplat.git_bash.cache_clear()
                self.assertIsNone(winplat.git_bash())


class WindowsSeamTests(unittest.TestCase):
    def test_missing_bash_has_the_existing_could_not_run_result(self):
        with tempfile.TemporaryDirectory() as temp:
            co = executor(Path(temp), 'echo test')
            with patch.object(winplat, 'IS_WINDOWS', True), patch.object(winplat, 'git_bash', return_value=None), \
                    patch.object(winplat, 'spawn_group') as spawn:
                result = co._test_run('missing')
            self.assertFalse(result['passed'])
            self.assertIsNone(result['exit'])
            self.assertIn('could not run:', result['reason'])
            self.assertIn('Git for Windows', result['reason'])
            self.assertIn('CLAUDE_CODE_GIT_BASH_PATH', result['reason'])
            spawn.assert_not_called()

    def test_preflight_keeps_posix_shell_words_without_execute_access(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'run_tests.sh').write_bytes(b'echo OK\n')
            (root / 'tool.exe').touch()
            with patch.object(winplat, 'IS_WINDOWS', True), patch.object(winplat, 'git_bash', return_value='git/bash.exe'), \
                    patch('shutil.which', return_value=sys.executable), patch('os.access', side_effect=AssertionError('X_OK')):
                for command in ('python -m unittest -q', './run_tests.sh', 'bash run_tests.sh', './tool.exe'):
                    self.assertTrue(rc.resolve_test_executable(root, command))
                    self.assertIsNone(rc.configured_command_issue(root, root / 'run', [command]))
                (root / 'no.txt').touch()
                with self.assertRaisesRegex(ValueError, 'missing or not on PATH'):
                    rc.resolve_test_executable(root, './no.txt')
                self.assertIn('Windows', rc.dontask_command_hint(['echo a && echo b']))

    def test_git_modes_override_synthesized_bits_and_head_is_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            co = executor(Path(temp), 'true')
            root = co.workspace
            script = root / 'run.sh'
            script.write_bytes(b'echo OK\n')
            git(root, 'add', 'run.sh')
            git(root, 'update-index', '--chmod=+x', 'run.sh')
            git(root, '-c', 'user.name=W5', '-c', 'user.email=w5@example.test', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'base')
            script.chmod(0o644)
            new = root / 'new.exe'
            new.write_bytes(b'new')
            new.chmod(0o755)
            with patch.object(winplat, 'IS_WINDOWS', True):
                modes = ct.windows_git_modes(root)
                self.assertEqual(modes['run.sh'], '100755')
                self.assertNotIn('new.exe', modes)
                manifest = dict(rc.git_snapshot(root)[1])
                self.assertTrue(manifest['run.sh'].startswith('exec:'))
                self.assertFalse(manifest['new.exe'].startswith('exec:'))
                git(root, 'update-index', '--chmod=-x', 'run.sh')
                self.assertEqual(ct.windows_git_modes(root)['run.sh'], '100644')
                self.assertFalse(dict(rc.git_snapshot(root)[1])['run.sh'].startswith('exec:'))
                git(root, 'rm', '--cached', '-q', 'run.sh')
                self.assertEqual(ct.windows_git_modes(root)['run.sh'], '100755')

    def test_candidate_windows_mode_and_regular_file_reader(self):
        with tempfile.TemporaryDirectory() as temp:
            co = executor(Path(temp), 'true')
            root = co.workspace
            (root / 'run.sh').write_bytes(b'old\n')
            git(root, 'add', 'run.sh')
            git(root, 'update-index', '--chmod=+x', 'run.sh')
            (root / 'new.exe').write_bytes(b'new\n')
            # Candidate roots deliberately contain no .git directory.
            candidate = Path(temp) / 'candidate'
            candidate.mkdir()
            (candidate / 'run.sh').write_bytes(b'changed\n')
            (candidate / 'new.exe').write_bytes(b'new\n')
            baseline = SimpleNamespace(root=candidate, root_identity=(candidate.stat().st_dev, candidate.stat().st_ino),
                git_dir=root / '.git', index=root / '.git/index', parent_entries=())
            with patch.object(winplat, 'IS_WINDOWS', True):
                files = ct._candidate_files(baseline)
            self.assertEqual(files['run.sh'], ('100755', b'changed\n'))
            self.assertEqual(files['new.exe'], ('100644', b'new\n'))

    def test_tracked_symlink_text_checkout_keeps_link_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            co = executor(Path(temp), 'true')
            root = co.workspace
            (root / 'link').write_bytes(b'target')
            oid = git(root, 'hash-object', '-w', 'link').decode().strip()
            git(root, 'update-index', '--add', '--cacheinfo', '120000', oid, 'link')
            with patch.object(winplat, 'IS_WINDOWS', True):
                self.assertEqual(dict(rc.git_snapshot(root)[1])['link'], 'link:target')
                # Windows live checkout accepts Git's plain-file symlink representation.
                self.assertEqual(ct._git(['status', '--porcelain'], cwd=root), 'A  link')

    def test_required_candidate_symlink_has_clear_privilege_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'link').write_bytes(b'target')
            with patch.object(winplat, 'IS_WINDOWS', True), patch.object(Path, 'symlink_to', side_effect=OSError('privilege')):
                with self.assertRaisesRegex(ct.CandidateError, 'enable Developer Mode or grant symlink privilege'):
                    ct._windows_checkout_links(root, [('120000', 'oid', 'link')])


@unittest.skipUnless(winplat.IS_WINDOWS, 'native Windows')
class NativeWindowsTests(unittest.TestCase):
    def test_local_executor_uses_git_bash_job_and_captures_success_and_failure(self):
        self.assertIsNotNone(winplat.git_bash(), 'windows-latest requires Git for Windows')
        for code in (0, 7):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                co = executor(Path(temp), 'printf "W5 output\\n"; exit ' + str(code))
                with patch.object(winplat, 'spawn_group', wraps=winplat.spawn_group) as spawn:
                    result = co._test_run('native')
                self.assertEqual(spawn.call_args[0][0], [winplat.git_bash(), '-c', co.args.test_command])
                self.assertEqual(result['exit'], code)
                self.assertEqual(result['passed'], code == 0)
                self.assertFalse(result['tree_changed'])
                self.assertIn(b'W5 output', Path(result['log']).read_bytes())

    def test_python_launcher_from_unrelated_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
            result = subprocess.run([sys.executable, str(REPO / 'bin/paired-session'), '--help'],
                                    cwd=temp, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'usage:', result.stdout)


@unittest.skipIf(winplat.IS_WINDOWS, 'POSIX contract')
class PosixPinTests(unittest.TestCase):
    def test_shell_vector_and_mode_snapshot_remain_posix(self):
        with tempfile.TemporaryDirectory() as temp:
            co = executor(Path(temp), 'printf "posix\\n"')
            with patch.object(winplat, 'spawn_group', wraps=winplat.spawn_group) as spawn, \
                    patch.object(winplat, 'git_bash', side_effect=AssertionError('Windows only')):
                result = co._test_run('posix')
            self.assertEqual(spawn.call_args[0][0], ['/bin/sh', '-c', co.args.test_command])
            self.assertTrue(result['passed'])
            self.assertEqual(Path(result['log']).read_bytes(), b'posix\n')
            script = co.workspace / 'run.sh'
            script.write_bytes(b'true\n')
            script.chmod(0o644)
            self.assertFalse(dict(rc.git_snapshot(co.workspace)[1])['run.sh'].startswith('exec:'))
            script.chmod(0o755)
            self.assertTrue(dict(rc.git_snapshot(co.workspace)[1])['run.sh'].startswith('exec:'))



class RepositoryAttributesTests(unittest.TestCase):
    def test_all_tracked_paths_allow_auto_commit_attributes(self):
        probe = subprocess.run(['git', '-C', str(REPO), 'rev-parse', '--is-inside-work-tree'], capture_output=True)
        if probe.returncode or probe.stdout.strip() != b'true':
            self.skipTest('not in a Git checkout')
        names = git(REPO, 'ls-files', '-z')
        result = subprocess.run(['git', 'check-attr', '-z', '--stdin', 'filter', 'text', 'eol',
                                 'working-tree-encoding'], cwd=REPO, input=names, capture_output=True, check=True)
        fields = result.stdout.rstrip(b'\0').split(b'\0')
        self.assertEqual(len(fields), len(names.rstrip(b'\0').split(b'\0')) * 4 * 3)
        for path, attr, value in zip(fields[0::3], fields[1::3], fields[2::3]):
            self.assertIn(value, (b'unspecified', b'unset'), (path, attr, value))


def delivery_fixture(root):
    co = executor(root, 'true')
    co.internal = co.run_dir / 'internal'
    co.internal.mkdir()
    co.workitem = co.run_dir / 'work-item.md'
    co.workitem.write_bytes(b'# W5 delivery\n')
    for key, value in (('user.name', 'W5'), ('user.email', 'w5@example.test'),
                       ('commit.gpgsign', 'false'), ('core.autocrlf', 'false'), ('core.fileMode', 'false')):
        git(co.workspace, 'config', key, value)
    (co.workspace / 'run.txt').write_bytes(b'base\n')
    git(co.workspace, 'add', 'run.txt')
    git(co.workspace, 'update-index', '--chmod=+x', 'run.txt')
    git(co.workspace, 'commit', '-qm', 'base')
    co.state.update(config={'auto_commit': True}, item_uuid='w5-test',
                    lifecycle={'parent': git(co.workspace, 'rev-parse', 'HEAD').decode().strip(),
                               'stage': 'DONE', 'receipts': []}, status='DONE')
    return co


def check_windows_delivery(case, autocrlf):
    with tempfile.TemporaryDirectory() as temp:
        co = delivery_fixture(Path(temp))
        root = co.workspace
        git(root, 'config', 'core.autocrlf', autocrlf)
        (root / 'run.txt').write_bytes(b'changed\r\n')
        (root / 'new.txt').write_bytes(b'new\r\n')
        (root / 'raw.dat').write_bytes(b'raw\r\n')
        (root / 'forced.dat').write_bytes(b'forced\r\n')
        (root / 'binary.dat').write_bytes(b'\0binary\r\n')
        (root / '.gitattributes').write_bytes(b'raw.dat -text\nforced.dat text eol=lf\n')
        # Compare against a real git add using a disposable copy of the pre-delivery index.
        add_index = co.internal / 'ordinary-add-index'
        shutil.copyfile(root / '.git/index', add_index)
        add_env = {**ct.git_env(), 'GIT_INDEX_FILE': str(add_index)}
        subprocess.run(['git', 'add', '-A'], cwd=root, env=add_env, check=True, capture_output=True)
        ordinary_tree = subprocess.check_output(['git', 'write-tree'], cwd=root, env=add_env).strip()
        digest, snapshot = rc.git_snapshot(root)
        intent = {'digest': 'test-intent', 'tree_sha256': digest, 'tree_snapshot': snapshot,
                  'head_ref': co._head_ref()}
        co.args.expect, co.args.override_rejection, co.args.reason = None, False, 'tested'
        co.operator_intent = lambda *args: intent
        co.save = co.write_comparison = lambda: None
        co._progress_terminal = lambda *args: None
        # Exercise the real accept, commit, journal, index sync and delivery record; stub presentation only.
        with patch.object(rc.worktree_lifecycle, 'delivery_report', return_value='delivery'), \
                patch.object(rc.review_report, 'delivery_section', return_value=''), \
                patch.object(rc.opv, 'current_for_acceptance', return_value=None):
            case.assertEqual(co._worktree_accept(), 'ACCEPTED')
        commit = co.state['acceptance']['delivery']['commit']
        case.assertTrue(commit)
        case.assertEqual(git(root, 'rev-parse', 'HEAD^{tree}').strip(), ordinary_tree)
        for name, data in (('run.txt', b'changed\n'), ('new.txt', b'new\n'),
                           ('raw.dat', b'raw\r\n'), ('forced.dat', b'forced\n'),
                           ('binary.dat', b'\0binary\r\n')):
            case.assertEqual(git(root, 'show', 'HEAD:' + name), data)
            # Ordinary git add would produce exactly the delivery blob, with the same path attributes.
            case.assertEqual(git(root, 'hash-object', '--path=' + name, name).strip(),
                             git(root, 'rev-parse', 'HEAD:' + name).strip())
        modes = ct.windows_git_modes(root)
        case.assertEqual(modes['run.txt'], '100755')
        case.assertEqual(modes['new.txt'], '100644')
        case.assertEqual(git(root, 'status', '--porcelain'), b'')
        case.assertEqual(rc.git_snapshot(root), (digest, snapshot))
        case.assertEqual((root / 'run.txt').read_bytes(), b'changed\r\n')
        journal = json.loads((co.evidence / 'delivery-commit.json').read_text(encoding='utf-8'))
        case.assertEqual(co._worktree_commit(intent, journal)['commit'], commit)


class WindowsDeliverySeamTests(unittest.TestCase):
    def test_autocrlf_delivery_and_replay(self):
        for value in ('true', 'input'):
            with self.subTest(autocrlf=value), patch.object(winplat, 'IS_WINDOWS', True):
                check_windows_delivery(self, value)

    def test_windows_refuses_non_eol_transformations(self):
        with tempfile.TemporaryDirectory() as temp:
            co = delivery_fixture(Path(temp))
            for attribute in ('filter=lfs', 'working-tree-encoding=UTF-16', 'ident'):
                with self.subTest(attribute=attribute), patch.object(winplat, 'IS_WINDOWS', True):
                    (co.workspace / '.gitattributes').write_bytes(('run.txt ' + attribute + '\n').encode())
                    with self.assertRaisesRegex(ValueError, 'auto_commit refuses content-transforming attributes: run.txt:'):
                        co._commit_refusals(['run.txt'])


@unittest.skipUnless(winplat.IS_WINDOWS, 'native Windows')
class NativeWindowsDeliveryTests(unittest.TestCase):
    def test_auto_commit_accept_with_installer_defaults(self):
        check_windows_delivery(self, 'true')


@unittest.skipIf(winplat.IS_WINDOWS, 'POSIX contract')
class PosixDeliveryPinTests(unittest.TestCase):
    def test_exact_refusal_messages_and_raw_hashing(self):
        with tempfile.TemporaryDirectory() as temp:
            co = delivery_fixture(Path(temp))
            for value in ('true', 'input'):
                git(co.workspace, 'config', 'core.autocrlf', value)
                with self.assertRaises(ValueError) as caught:
                    co._commit_refusals(['run.txt'])
                self.assertEqual(str(caught.exception), 'auto_commit refuses core.autocrlf; unset it (or set it to false in this repository) and accept again')
            git(co.workspace, 'config', 'core.autocrlf', 'false')
            with self.assertRaises(ValueError) as caught:
                co._commit_refusals(['run.txt'])
            self.assertEqual(str(caught.exception), 'auto_commit refuses core.fileMode false; set it to true and accept again, or abort and commit the tree yourself (auto_commit is frozen at run start)')
            git(co.workspace, 'config', 'core.fileMode', 'true')
            for attrs in ('text', 'text eol=lf'):
                (co.workspace / '.gitattributes').write_bytes(('run.txt ' + attrs + '\n').encode())
                with self.assertRaisesRegex(ValueError, 'auto_commit refuses content-transforming attributes:'):
                    co._commit_refusals(['run.txt'])
            (co.workspace / '.gitattributes').unlink()
            (co.workspace / 'run.txt').write_bytes(b'raw\r\n')
            tree = co._manifest_tree(rc.git_snapshot(co.workspace)[1])
            self.assertEqual(git(co.workspace, 'show', tree + ':run.txt'), b'raw\r\n')


if __name__ == '__main__':
    unittest.main()
