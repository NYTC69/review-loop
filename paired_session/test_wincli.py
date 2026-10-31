"""W3: shell-free Windows CLI binding; POSIX record and argument contracts."""
import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from paired_session import coordinator as rc, program_binding as pb, winplat


class WindowsResolutionTests(unittest.TestCase):
    # Exercise the Windows seam on POSIX too; native CreateProcess coverage is below.
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cli layout ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bin = self.root / 'npm'
        self.bin.mkdir()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.gate = self.root / 'gate.md'
        self.gate.write_bytes(b'gate\n')
        self.node = self.put(self.bin / 'node.exe', b'node')
        self.shims = {}
        self.scripts = {}
        for name, package, entry in (('claude', '@anthropic-ai/claude-code', 'cli.js'),
                                     ('codex', '@openai/codex', 'bin/codex.js')):
            self.shims[name] = self.put(self.bin / (name + '.cmd'), b'@echo off\n')
            self.scripts[name] = self.put(self.bin / 'node_modules' / package / entry, b'// entry\n')
        seam = patch.object(winplat, 'IS_WINDOWS', True)
        seam.start()
        self.addCleanup(seam.stop)
        env = patch.dict(os.environ, {'PATH': str(self.bin), 'PATHEXT': '.EXE;.CMD;.PS1;.BAT'})
        env.start()
        self.addCleanup(env.stop)

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def snapshot(self, codex=None, claude=None):
        return pb.snapshot(self.workspace, self.root / 'run', self.root / 'run/author-tmp',
                           str(codex or self.shims['codex']), str(claude or self.shims['claude']),
                           self.gate, None)

    def test_pathext_and_every_npm_shim_suffix(self):
        for name in self.shims:
            self.assertEqual(pb.resolve_binary(name, self.workspace, str(self.bin)), self.shims[name])
            for suffix in ('.cmd', '.ps1', '.bat', ''):
                shim = self.put(self.bin / (name + suffix), b'shim')
                self.assertEqual(pb.launch_vector(str(shim)), [str(self.node), str(self.scripts[name])])

    def test_native_exe_and_native_claude_beside_shim(self):
        native = self.put(self.bin / 'claude.exe', b'native')
        self.assertEqual(pb.launch_vector('claude'), [str(native)])
        self.assertEqual(pb.launch_vector(str(self.shims['claude'])), [str(native)])
        for version in ('2.1.293', '2.1.293.exe'):
            release = self.put(self.root / '.local/share/claude/versions' / version, b'native')
            self.assertEqual(pb.launch_vector(str(release)), [str(release)])
        arbitrary = self.put(self.root / 'custom.exe', b'custom')
        self.assertEqual(pb.launch_vector(str(arbitrary)), [str(arbitrary)])

    def test_npm_manifest_bin_entries_and_fallbacks(self):
        for name in self.shims:
            package = self.bin / 'node_modules' / ('@anthropic-ai/claude-code' if name == 'claude' else '@openai/codex')
            manifest = package / 'package.json'
            custom = self.put(package / 'dist/custom entry.js', b'// custom\n')
            for declared in ({name: 'dist/custom entry.js'}, 'dist/custom entry.js'):
                manifest.write_text(json.dumps({'bin': declared}), encoding='utf-8')
                records, issue = self.snapshot()
                self.assertIsNone(issue)
                self.assertEqual(records[name + '_bin']['path'], str(custom))
                self.assertEqual(pb.program_argv(records[name + '_bin']), [str(self.node), str(custom)])
            for metadata in ({}, {'bin': {'other': 'unused.js'}}):
                manifest.write_text(json.dumps(metadata), encoding='utf-8')
                self.assertEqual(pb.launch_vector(str(self.shims[name])), [str(self.node), str(self.scripts[name])])
            manifest.write_text(json.dumps({'bin': {name: 'missing.js'}}), encoding='utf-8')
            self.assertIn('missing.js', self.snapshot()[1])
            manifest.unlink()

    def test_missing_cli_binding_reports_path_search(self):
        for name in self.shims:
            self.shims[name].unlink()
            # A workspace file must not turn a failed PATH search into an npm layout error.
            self.put(self.workspace / name, b'workspace shim')
            kwargs = {name: name}
            self.assertEqual(self.snapshot(**kwargs)[1], name + ' not found on PATH')
            self.put(self.shims[name], b'shim')

    def test_node_from_path_and_missing_node_or_script(self):
        self.node.unlink()
        other = self.put(self.root / 'node install/node.exe', b'node on PATH')
        with patch.dict(os.environ, {'PATH': str(other.parent)}):
            self.assertEqual(pb.launch_vector(str(self.shims['codex'])), [str(other), str(self.scripts['codex'])])
        with self.assertRaisesRegex(ValueError, 'needs node.exe'):
            pb.launch_vector(str(self.shims['codex']))
        self.put(self.node, b'node')
        self.scripts['codex'].unlink()
        self.assertIn('needs node.exe', self.snapshot()[1])

    def test_binding_hashes_script_and_node_and_detects_each_change(self):
        before, issue = self.snapshot()
        self.assertIsNone(issue)
        record = before['codex_bin']
        self.assertEqual(record, {'path': str(self.scripts['codex']),
            'sha256': hashlib.sha256(b'// entry\n').hexdigest(),
            'node': {'path': str(self.node), 'sha256': hashlib.sha256(b'node').hexdigest()}})
        self.assertEqual(pb.program_argv(record), [str(self.node), str(self.scripts['codex'])])
        self.node.write_bytes(b'updated node')
        after, issue = self.snapshot()
        self.assertIsNone(issue)
        self.assertNotEqual(record['node'], after['codex_bin']['node'])
        self.scripts['codex'].write_bytes(b'updated script')
        newer, issue = self.snapshot()
        self.assertIsNone(issue)
        self.assertNotEqual(after['codex_bin']['sha256'], newer['codex_bin']['sha256'])

    def test_every_bound_file_is_checked_against_author_roots(self):
        unsafe_node = self.put(self.workspace / 'node.exe', b'unsafe node')
        with patch.object(pb, 'launch_vector', return_value=[str(unsafe_node), str(self.scripts['codex'])]):
            self.assertIn('codex_bin.node is missing or inside an author-writable root', self.snapshot()[1])

    def test_script_inside_workspace_is_refused(self):
        script = self.put(self.workspace / 'cli.js', b'unsafe script')
        self.put(self.workspace / 'node.exe', b'node')
        self.assertIn('author-writable root', self.snapshot(codex=script)[1])

    def test_safe_path_fallback_and_current_directory_exclusion(self):
        system = self.root / 'Windows'
        with patch.dict(os.environ, {'SystemRoot': str(system)}):
            self.assertEqual(pb.safe_path(str(self.workspace), (self.workspace,)),
                             os.pathsep.join((str(system / 'System32'), str(system))))
        with patch.dict(os.environ, {'PATH': '.'}):
            with self.assertRaises(ValueError): pb.launch_vector('codex')

    def test_versions_and_resume_use_frozen_node_and_script(self):
        records, issue = self.snapshot()
        self.assertIsNone(issue)
        codex, claude = records['codex_bin'], records['claude_bin']
        with patch.object(rc.subprocess, 'run', return_value=SimpleNamespace(stdout='codex-cli 0.160.0', returncode=0)) as run:
            self.assertEqual(rc.codex_cli_semver(codex), (0, 160, 0))
            self.assertEqual(run.call_args.args[0], pb.program_argv(codex) + ['--version'])
            co = rc.Coordinator.__new__(rc.Coordinator)
            co.state = {'operator_programs': records, 'started': {'reviewer': True}, 'sessions': {'reviewer': 'session-123'}}
            co._program_state = Mock(return_value=(records, None))
            self.assertEqual(co._codex_cli_version(), 'codex-cli 0.160.0')
            self.assertEqual(run.call_args.args[0], pb.program_argv(codex) + ['--version'])
            rc.claude_cli_version(claude)
            self.assertEqual(run.call_args.args[0], pb.program_argv(claude) + ['--version'])
        co.args = SimpleNamespace(codex_bin='codex')
        co._role_vendor = Mock(return_value='codex')
        co._model_effort = Mock(return_value=('model', 'high'))
        command = co.command('reviewer', self.gate, False)
        self.assertEqual(command[:2], pb.program_argv(codex))
        self.assertEqual(command[-3:], ['resume', 'session-123', '-'])
        co._role_vendor.return_value = 'claude'
        co._claude_command = Mock(return_value=['claude', '--resume', 'session-456'])
        self.assertEqual(co.command('reviewer', self.gate, False), pb.program_argv(claude) + ['--resume', 'session-456'])

    def test_cli_updates_accept_exe_versions_and_keep_codex_path_rule(self):
        folder = self.root / '.local/share/claude/versions'
        old = {'claude_bin': {'path': str(folder / '2.1.281.exe'), 'sha256': 'old'}}
        new = {'claude_bin': {'path': str(folder / '2.1.293.exe'), 'sha256': 'new'}}
        with patch.object(rc, 'claude_cli_version', return_value='2.1.293 (Claude Code)'):
            update = rc.normal_cli_updates(old, new)
            self.assertEqual(update[0]['new_version'], '2.1.293')
            self.assertIsNone(rc.normal_cli_updates(new, old))
        records, _ = self.snapshot()
        old = {'codex_bin': records['codex_bin']}
        new = {'codex_bin': {**old['codex_bin'], 'sha256': 'updated'}}
        with patch.object(rc, 'claude_cli_version', return_value='codex-cli 0.160.0') as version:
            self.assertIsNotNone(rc.normal_cli_updates(old, new))
            self.assertEqual(version.call_args.args[0], new['codex_bin'])
            new['codex_bin']['path'] += '.other'
            self.assertIsNone(rc.normal_cli_updates(old, new))
            new['codex_bin'] = {**old['codex_bin'], 'node': {**old['codex_bin']['node'], 'sha256': 'new node'}}
            self.assertIsNone(rc.normal_cli_updates(old, new))

    def test_createprocess_limit_counts_quoting_and_utf16(self):
        argv = ['node.exe', 'script with spaces.js', '--settings', json.dumps({'path': 'a "quoted" path'}), '--json-schema', '😀' * 9000]
        size = winplat.check_command_line(argv)
        self.assertGreater(size, 8191)
        self.assertEqual(size, len(subprocess.list2cmdline(argv).encode('utf-16-le')) // 2 + 1)
        with self.assertRaisesRegex(ValueError, 'CreateProcess limit 32767'):
            winplat.check_command_line(argv + ['x' * 32767])
        with patch.object(winplat, 'job_name', return_value='Local\\review-loop-w3'), \
                patch('shutil.which', return_value='node.exe'), \
                patch('subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError, 'CreateProcess limit 32767'):
                winplat.spawn_group(argv + ['x' * 32767], 'too-large')
            spawn.assert_not_called()

    def test_native_claude_same_path_update(self):
        path = str(self.root / '.local/bin/claude.exe')
        old = {'claude_bin': {'path': path, 'sha256': 'old'}}
        new = {'claude_bin': {'path': path, 'sha256': 'new'}}
        with patch.object(rc, 'claude_cli_version', return_value='2.1.293 (Claude Code)') as version:
            update = rc.normal_cli_updates(old, new)
            self.assertEqual(update[0]['new_version'], '2.1.293')
            self.assertIsNone(update[0]['old_version'])
            self.assertEqual(version.call_args.args[0], new['claude_bin'])
            old['claude_bin']['version'] = '2.1.281 (Claude Code)'
            self.assertEqual(rc.normal_cli_updates(old, new)[0]['old_version'], '2.1.281')
            for frozen in ('2.1.293', '2.1.294'):
                old['claude_bin']['version'] = frozen
                self.assertIsNone(rc.normal_cli_updates(old, new))
            del old['claude_bin']['version']
            with patch.object(winplat, 'IS_WINDOWS', False):
                self.assertIsNone(rc.normal_cli_updates(old, new))
            new['claude_bin']['path'] = str(self.root / 'other/claude.exe')
            self.assertIsNone(rc.normal_cli_updates(old, new))
            new['claude_bin']['path'] = path
        for output in ('unknown', '2.1.294-beta', '2.1.294-rc.1'):
            with patch.object(rc, 'claude_cli_version', return_value=output):
                self.assertIsNone(rc.normal_cli_updates(old, new))
        new['claude_bin']['node'] = {'path': str(self.node), 'sha256': 'node'}
        self.assertIsNone(rc.normal_cli_updates(old, new))

    def test_oversize_command_is_refused_with_measured_size(self):
        records, issue = self.snapshot()
        self.assertIsNone(issue)
        co = rc.Coordinator.__new__(rc.Coordinator)
        co.state = {'operator_programs': records}
        co._role_vendor = Mock(return_value='claude')
        co._claude_command = Mock(side_effect=lambda *args: ['claude', '--settings', 'x' * 32767])
        argv = pb.program_argv(records['claude_bin']) + ['--settings', 'x' * 32767]
        size = len(subprocess.list2cmdline(argv).encode('utf-16-le')) // 2 + 1
        out = io.StringIO()
        # Exercise the real main() error boundary without dispatching a child or taking platform locks.
        lock = SimpleNamespace(after_mkdir=lambda: None, whole=False)
        with patch.object(rc, 'ProbeParentLock', return_value=contextlib.nullcontext(lock)), \
                patch.object(rc, 'run_lease', return_value=contextlib.nullcontext()), \
                patch.object(rc, '_execute_locked', side_effect=lambda args: co.command('reviewer', self.gate, True)), \
                patch.object(rc.subprocess, 'Popen') as spawn, contextlib.redirect_stdout(out):
            code = rc.main(['status', '--workspace', str(self.workspace), '--run-dir', str(self.root / 'run'),
                            '--workitem', str(self.gate), '--gate-prompt', str(self.gate)])
        self.assertEqual(code, 2)
        self.assertIn(f'REFUSED: Windows CLI command line is {size} UTF-16 units (CreateProcess limit 32767)', out.getvalue())
        spawn.assert_not_called()

    def test_cli_env_preserves_windows_requirements(self):
        names = ('SystemRoot PATHEXT USERPROFILE APPDATA LOCALAPPDATA ComSpec TEMP TMP HOMEDRIVE HOMEPATH '
                 'CLAUDE_CODE_GIT_BASH_PATH').split()
        values = {name: 'value ' + name for name in names}
        with patch.dict(os.environ, values):
            env = rc.cli_env()
        upper = {key.upper(): value for key, value in env.items()}
        self.assertEqual({name: upper[name.upper()] for name in names}, values)


@unittest.skipIf(winplat.IS_WINDOWS, 'POSIX contract')
class PosixContracts(unittest.TestCase):
    def test_resolution_records_and_argv_remain_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            workspace = root / 'workspace'
            workspace.mkdir()
            binary = root / 'cli'
            binary.write_bytes(b'#!/bin/sh\nexit 0\n')
            binary.chmod(0o755)
            gate = root / 'gate'
            gate.write_bytes(b'gate')
            with patch.dict(os.environ, {'PATH': str(root)}):
                self.assertEqual(pb.resolve_binary('cli', workspace, str(root)), binary)
                records, issue = pb.snapshot(workspace, root / 'run', root / 'tmp', 'cli', 'cli', gate, None)
            self.assertIsNone(issue)
            expected = {'path': str(binary), 'sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}
            self.assertEqual(records, {'codex_bin': expected, 'claude_bin': expected,
                'gate_prompt': {'path': str(gate), 'sha256': hashlib.sha256(b'gate').hexdigest()}, 'path_env': str(root)})
            self.assertEqual(pb.program_argv(expected), [str(binary)])
            self.assertEqual(pb.launch_vector('cli'), ['cli'])
            self.assertEqual(pb.safe_path('', (workspace,)), '/usr/bin:/bin')


@unittest.skipUnless(winplat.IS_WINDOWS, 'native Windows CreateProcess and Job Objects')
class WindowsLaunchTests(unittest.TestCase):
    def test_python_standin_preserves_long_inline_json_stdin_and_resume(self):
        with tempfile.TemporaryDirectory(prefix='cli launch ') as temp:
            root = Path(temp).resolve()
            shim = root / 'npm/codex.cmd'
            shim.parent.mkdir()
            shim.write_bytes(b'@echo off')
            script = shim.parent / 'node_modules/@openai/codex/bin/codex.js'
            script.parent.mkdir(parents=True)
            script.write_text('import json,sys\nprint(json.dumps([sys.argv[1:], sys.stdin.read()]))\n', encoding='utf-8')
            # A real Python executable stands in for Node, without copying python.exe away from its DLLs.
            with patch.object(pb, '_windows_which', return_value=Path(sys.executable)):
                argv = pb.launch_vector(str(shim))
            record = {'node': pb._record(Path(argv[0])), **pb._record(Path(argv[1]))}
            payload = json.dumps({'path': 'C:\\Users\\a b', 'text': '"hello"' * 1600})
            args = ['--settings', payload, '--json-schema', '{"type":"object"}', '--resume', 'session-123']
            process, job = winplat.spawn_group(pb.program_argv(record) + args, 'w3-cli', cwd=temp,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = process.communicate(b'prompt\n', timeout=30)
            self.assertEqual(process.returncode, 0, err)
            self.assertEqual(json.loads(out), [args, 'prompt\n'])
            self.assertTrue(job)


if __name__ == '__main__':
    unittest.main()
