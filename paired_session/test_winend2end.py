"""W7: real entry, offline providers, native Windows only (no Git Bash parent).

The adapter reuses fake_cli's answers but executes every reported command. It
lives here so the existing fake's POSIX behavior stays byte-for-byte unchanged.
"""
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

IS_WINDOWS = os.name == 'nt'
REPO = Path(__file__).resolve().parents[1]
# unittest writes its progress to stderr. Capture it inside Python so Windows
# PowerShell 5.1 cannot turn redirected native stderr into NativeCommandError.
TEST_COMMAND = ('python -c "import subprocess, sys; '
                "result = subprocess.run([sys.executable, '-m', 'unittest', '-q'], "
                'stdout=subprocess.PIPE, stderr=subprocess.STDOUT); '
                'sys.stdout.buffer.write(result.stdout); sys.exit(result.returncode)"')


def without_bash_context(environment):
    """Keep Windows/Python/Git prerequisites; remove inherited MSYS shell state."""
    excluded = {'PYTHONPATH', 'CLAUDE_CODE_GIT_BASH_PATH', 'MSYSTEM', 'MSYSTEM_PREFIX',
                'MSYS', 'MSYS2_PATH_TYPE', 'SHELL', 'BASH', 'BASH_ENV', 'ENV', 'SHLVL',
                'CHERE_INVOKING', 'CLAUDECODE'}
    result = {key: value for key, value in environment.items()
              if key.upper() not in excluded and not key.upper().startswith(('FAKE_', 'PAIRED_SESSION_INTERNAL_TEST_'))}
    paths = []
    for entry in result.get('PATH', '').split(os.pathsep):
        # Git's cmd directory stays; its bin/usr/bin/mingw64/bin directories do
        # not. bash.exe must be discovered from git.exe's installation.
        path = Path(entry)
        system_alias = path.name.lower() in ('system32', 'windowsapps')
        if entry and (system_alias or not any((path / name).is_file() for name in ('bash.exe', 'sh.exe'))):
            paths.append(entry)
    result['PATH'] = os.pathsep.join(paths)
    return result


def codex_command_events(commands, execute):
    """Pure adapter seam: actual completion/exit/output, Windows wrapper text."""
    events, argv_rows = [], []
    for command in commands:
        argv = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command]
        completed = execute(argv)
        events.append({'command': subprocess.list2cmdline(argv),
                       'exit_code': completed.returncode,
                       'output': completed.stdout + completed.stderr})
        argv_rows.append(argv)
    return events, argv_rows


def windows_fake():
    """Called by the npm Python entry; no installed CLI or shell shim is run."""
    if not IS_WINDOWS:
        raise RuntimeError('the W7 provider adapter requires native Windows')
    prompt = sys.stdin.read()
    sys.stdin = io.StringIO(prompt)
    if 'implementer. Phase: EXEC.' in prompt:
        # Both quality writers must be eligible before _green_baseline runs:
        # >=20 changed code lines and a changed, discoverable test file.
        Path('w7_extra.py').write_text(''.join(f'VALUE_{n} = {n}\n' for n in range(30)), encoding='utf-8')
        Path('test_w7_extra.py').write_text(
            'import unittest\nimport w7_extra\n\n'
            'class ExtraTest(unittest.TestCase):\n'
            '    def test_values(self):\n'
            '        self.assertEqual(sum(getattr(w7_extra, f"VALUE_{n}") for n in range(30)), 435)\n',
            encoding='utf-8')
    telemetry = Path(os.environ['W7_TELEMETRY'])
    ready, release = telemetry / 'provider-ready', telemetry / 'provider-release'
    if os.environ.get('W7_PAUSE_PROVIDER') and not ready.exists():
        ready.touch()
        deadline = time.monotonic() + 40
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not release.exists():
            raise RuntimeError('W7 provider barrier was not released')
    namespace = runpy.run_path(str(REPO / 'paired_session/fake_cli.py'))
    globals_ = namespace['main'].__globals__
    original_codex, original_claude = globals_['emit_codex'], globals_['emit_claude']
    platform = runpy.run_path(str(REPO / 'paired_session/winplat.py'))
    bash = platform['git_bash']()
    if not bash:
        raise RuntimeError('W7 requires discoverable Git for Windows')

    def execute(argv):
        result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=60,
                                env={**os.environ, 'W7_FAKE_PROVIDER': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
        record = {'argv': argv, 'cwd': str(Path.cwd()), 'exit': result.returncode,
                  'stdout': result.stdout, 'stderr': result.stderr}
        (telemetry / (uuid.uuid4().hex + '.command.json')).write_text(json.dumps(record), encoding='utf-8')
        return result

    def emit_codex(answer, session, command_events=None):
        commands = [row['command'] for row in (command_events or answer.get('self_run_evidence', []))]
        events, argv_rows = codex_command_events(commands, execute)
        # W4c rollout cwd/argv evidence uses native drive-letter paths. Fresh
        # thread IDs prevent unrelated turns sharing one rollout.
        stamp = datetime.now(timezone.utc).isoformat()
        rows = [{'timestamp': stamp, 'type': 'event_msg',
                 'payload': {'type': 'task_started', 'turn_id': session}},
                {'timestamp': stamp, 'type': 'turn_context', 'payload': {'cwd': str(Path.cwd())}}]
        rows += [{'timestamp': stamp, 'type': 'event_msg', 'payload': {
            'type': 'item_completed', 'turn_id': session, 'item': {
                'type': 'CommandExecution', 'command': argv, 'cwd': str(Path.cwd())}}} for argv in argv_rows]
        rollout = Path(os.environ['CODEX_HOME']) / 'sessions' / ('rollout-' + session + '.jsonl')
        rollout.parent.mkdir(parents=True, exist_ok=True)
        with rollout.open('a', encoding='utf-8', newline='') as stream:
            stream.write(''.join(json.dumps(row) + '\n' for row in rows))
        original_codex(answer, session, events)

    def emit_claude(answer, session, extra_commands=None):
        # Keep the established usage/model/session envelopes, replace every
        # synthetic Bash result with the real Git Bash result.
        if answer.get('status') == 'READY':
            # The shared fake supplies writer command_events for Codex only.
            # Claude writers also inspect the tree, so a no-op writer has real
            # tool evidence instead of exhausting the empty-tool retry budget.
            extra_commands = [{'command': 'git diff --stat'}, *(extra_commands or [])]
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            original_claude(answer, session, extra_commands)
        pending = {}
        for line in captured.getvalue().splitlines():
            row = json.loads(line)
            for block in row.get('message', {}).get('content', []):
                if block.get('type') == 'tool_use' and block.get('name') == 'Bash':
                    pending[block['id']] = execute([bash, '-c', block['input']['command']])
                elif block.get('type') == 'tool_result' and block.get('tool_use_id') in pending:
                    result = pending[block['tool_use_id']]
                    block.update(content=result.stdout + result.stderr, is_error=result.returncode != 0)
            print(json.dumps(row))

    globals_.update(emit_codex=emit_codex, emit_claude=emit_claude)
    return namespace['main']()


# The tiny repository test also provides a deterministic pause in _test_run,
# after a model turn settled and before another starts. Provider tests never
# pause. Instrumentation is outside both the reviewed tree and the run dir.
FIXTURE_TEST = '''import ctypes
from ctypes import wintypes
import json, os, pathlib, time, unittest

class TinyTest(unittest.TestCase):
    def test_sum(self):
        from sum_ints import sum_ints
        self.assertEqual(sum_ints([2, 3]), 5)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
        inside = wintypes.BOOL()
        self.assertTrue(kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(inside)))
        self.assertTrue(inside.value)
        provider = os.environ.get('W7_FAKE_PROVIDER') == '1'
        row = {'pid': os.getpid(), 'in_job': bool(inside.value), 'provider': provider,
               'shlvl': os.environ.get('SHLVL'), 'cwd': str(pathlib.Path.cwd())}
        root = pathlib.Path(os.environ['W7_TELEMETRY'])
        (root / (str(os.getpid()) + '.test.json')).write_text(json.dumps(row), encoding='utf-8')
        print('W7_REAL_UNITTEST_OK', flush=True)
        ready, release = root / 'local-ready.json', root / 'local-release'
        if not provider and os.environ.get('W7_PAUSE_LOCAL') and not ready.exists():
            temporary = root / 'ready.tmp'
            temporary.write_text(json.dumps(row), encoding='utf-8')
            os.replace(temporary, ready)
            deadline = time.monotonic() + 80
            while not release.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(release.exists(), 'W7 local-test barrier was not released')
'''


class AdapterHelperTests(unittest.TestCase):
    def test_codex_wrapper_keeps_actual_failure_and_output(self):
        seen = []

        def execute(argv):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 7, 'real stdout\n', 'real stderr\n')

        events, argv_rows = codex_command_events([TEST_COMMAND], execute)
        self.assertEqual(seen, argv_rows)
        self.assertEqual(events, [{'command': subprocess.list2cmdline(seen[0]), 'exit_code': 7,
                                   'output': 'real stdout\nreal stderr\n'}])
        from paired_session import winplat
        self.assertEqual(winplat.windows_wrapped_command(winplat.windows_split(events[0]['command'])), TEST_COMMAND)

    def test_environment_drops_bash_and_import_overrides_keeps_windows_prerequisites(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'bash.exe').touch()
            result = without_bash_context({'PATH': str(root), 'PYTHONPATH': 'bad', 'MSYSTEM': 'MINGW64',
                'SHLVL': '3', 'CLAUDE_CODE_GIT_BASH_PATH': 'bad', 'FAKE_GATE_BLOCK': '1',
                'SystemRoot': 'C:\\Windows', 'PATHEXT': '.EXE;.CMD'})
            self.assertEqual(result, {'PATH': '', 'SystemRoot': 'C:\\Windows', 'PATHEXT': '.EXE;.CMD'})


@unittest.skipUnless(IS_WINDOWS, 'native Windows end-to-end CLI, Git Bash and Job Objects')
class WindowsEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.deadline = time.monotonic() + 840
        self.temp = tempfile.TemporaryDirectory(prefix='W7 windows e2e ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        self.assertNotIn(REPO, self.outside.parents, 'CLI cwd must be outside the support repository')
        self.telemetry = self.root / 'telemetry'
        self.telemetry.mkdir()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.run_dir = self.root / 'run'
        home = self.root / 'home'
        (home / '.codex').mkdir(parents=True)
        (home / '.claude/plugins').mkdir(parents=True)
        (home / '.codex/config.toml').write_text('model = "gpt-6.1-sol"\n', encoding='utf-8')
        (home / '.claude/settings.json').write_text('{}\n', encoding='utf-8')
        (home / '.claude/plugins/installed_plugins.json').write_text('{"plugins": []}\n', encoding='utf-8')
        self.env = without_bash_context(os.environ)
        self.env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / '.codex'),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                        W7_TELEMETRY=str(self.telemetry), FAKE_UNIQUE_CODEX_THREAD='1',
                        FAKE_LIFECYCLE_DOCS_FILE='CHANGELOG.md')
        # Copy the interpreter and its adjacent DLLs, and name it node.exe.
        # PYTHONHOME supplies its stdlib; this needs no symlink privilege and
        # does not put a Python script or cmd shim in CreateProcess argv[0].
        npm = self.root / 'npm providers'
        npm.mkdir()
        shutil.copy2(sys.executable, npm / 'node.exe')
        for directory in {Path(sys.executable).parent, Path(sys.base_prefix)}:
            for dll in directory.glob('python*.dll'):
                shutil.copy2(dll, npm / dll.name)
        self.env['PYTHONHOME'] = sys.base_prefix
        self.env['PATH'] = os.pathsep.join((str(npm), str(Path(sys.executable).parent), self.env['PATH']))
        self.bins = {}
        for name, package, entry in (('codex', '@openai/codex', 'bin/codex.js'),
                                     ('claude', '@anthropic-ai/claude-code', 'cli.js')):
            shim = npm / (name + '.cmd')
            shim.write_text('@echo W7 shim must never execute\n@exit /b 99\n', encoding='utf-8')
            script = npm / 'node_modules' / package / entry
            script.parent.mkdir(parents=True)
            version = 'codex-cli 0.160.0' if name == 'codex' else '2.1.0 (Claude Code)'
            script.write_text('import runpy, sys\n'
                              'if sys.argv[1:] == ["--version"]:\n'
                              '    print(' + repr(version) + '); raise SystemExit(0)\n'
                              'raise SystemExit(runpy.run_path(' + repr(str(Path(__file__).resolve())) +
                              ')["windows_fake"]())\n', encoding='utf-8')
            self.bins[name] = shim
        for entry in self.env['PATH'].split(os.pathsep):
            if Path(entry).name.lower() not in ('system32', 'windowsapps'):
                self.assertFalse((Path(entry) / 'bash.exe').is_file(), 'parent PATH must not supply Git Bash')
        self.assertNotIn('PYTHONPATH', self.env)
        self.git('init', '-q', '-b', 'main')
        for key, value in (('user.name', 'W7 Fake'), ('user.email', 'w7@example.invalid'),
                           ('core.autocrlf', 'true'), ('core.fileMode', 'false')):
            self.git('config', key, value)
        # Same coverage as the established worktree lifecycle fixture, without
        # importing its POSIX harness on Windows.
        ignore = '\n'.join(('__pycache__/', '*.cache', '.review-loop/', '.env', '.env.*', '*.env',
            '!.env.example', '!.env.sample', '*.pem', '*.key', '*.crt', '*.cert', '*.cer', '*.p12', '*.pfx',
            '*.jks', '*.keystore', '*.ppk', 'id_rsa*', 'id_dsa*', 'id_ecdsa*', 'id_ed25519*', '*.asc',
            '*.gpg', '*.pgp', '*credentials*', '!*credentials.example*', '!*credentials.sample*',
            'service-account*.json', '.aws/', '.gcloud/', '*secret*', '!*secret.example*', '!*secret.sample*',
            'secrets.*', '!secrets.example*', '!secrets.sample*', '*.sqlite', '*.sqlite3', '*.db', '*.dump',
            '*.sql.gz', '*.map', '*.tfstate', '*.tfstate.*', '*.tfvars', '!*.tfvars.example', '.terraform/',
            '*.log', 'logs/', ''))
        (self.workspace / '.gitignore').write_bytes(ignore.encode())
        (self.workspace / 'tracked.txt').write_bytes(b'base\n')
        (self.workspace / 'sum_ints.py').write_bytes(b'def sum_ints(values):\n    return sum(values)\n')
        (self.workspace / 'test_tiny.py').write_bytes(FIXTURE_TEST.encode())
        self.git('add', '-A')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.workitem = self.root / 'WORKITEM.md'
        self.workitem.write_text('# Toy\nImplement sum_ints; verify the sum and document it.\n', encoding='utf-8')
        self.processes = []
        self.addCleanup(self.stop_children)

    def remaining(self, cap=180):
        remaining = self.deadline - time.monotonic()
        self.assertGreater(remaining, 0, 'W7 test exceeded its fourteen-minute budget')
        return min(cap, remaining)

    def run_process(self, argv, expected=0, cap=180):
        result = subprocess.run(argv, cwd=self.outside, env=self.env, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=self.remaining(cap))
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.workspace), *args], env=self.env,
                                capture_output=True, text=True, encoding='utf-8', timeout=self.remaining(30))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def command(self, action='run', *extra, run_dir=None):
        return [sys.executable, str(REPO / 'bin/paired-session'), action,
                '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                '--run-dir', str(run_dir or self.run_dir), '--lifecycle-mode', 'on',
                '--codex-bin', str(self.bins['codex']), '--claude-bin', str(self.bins['claude']),
                '--author-vendor', 'claude', '--reviewer-vendor', 'codex',
                '--author-effort', 'low', '--reviewer-effort', 'low', '--gate-effort', 'low',
                '--timeout', '90', '--exec-turn-timeout', '90', '--max-invocations', '50',
                '--test-command', TEST_COMMAND, *extra]

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text(encoding='utf-8'))

    def wait_for(self, predicate, description, cap=120):
        deadline = time.monotonic() + self.remaining(cap)
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.2)
        self.fail('timed out waiting for ' + description)

    def stop_children(self):
        (self.telemetry / 'local-release').touch()
        (self.telemetry / 'provider-release').touch()
        for process in self.processes:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        from paired_session import winplat
        if (self.run_dir / 'state.json').exists():
            state = self.state()
            for row in [*state['turns'], state.get('active') or {}, state.get('uncertain_active') or {}]:
                if row.get('job'):
                    try:
                        winplat.kill_group(row['pid'], row['job'])
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        ready = self.telemetry / 'local-ready.json'
        if ready.exists():
            pid = json.loads(ready.read_text(encoding='utf-8'))['pid']
            deadline = time.monotonic() + 10
            while winplat.pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)

    def load_entry_bundle(self):
        from paired_session import winplat
        bundle = Path(tempfile.gettempdir()) / ('review-loop-protocol-' + winplat.user_token()) / (uuid.uuid4().hex + '.md')
        self.addCleanup(lambda: bundle.unlink(missing_ok=True))
        loaded = self.run_process([sys.executable, str(REPO / 'scripts/read_protocol.py'), '--runtime', 'claude',
                                   '--stage', 'entry-paired-session', '--output', str(bundle)])
        metadata = json.loads(loaded.stdout)
        data = bundle.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), metadata['sha256'])
        self.assertEqual(len(data), metadata['bytes'])
        self.assertNotIn(b'\r\n', data)
        self.assertIn(b'paired-session', data)
        self.assertFalse((self.run_dir / 'state.json').exists())

    def assert_turns_and_artifacts(self, state):
        self.assertEqual(state['config']['safety_mode'], 'efficient')
        self.assertEqual(state['lifecycle']['format'], 'worktree')
        self.assertFalse((self.run_dir / 'permission-probe.json').exists())
        self.assertFalse({'probe_skip_override', 'claude_author_override'} & state.keys())
        self.assertTrue(state['turns'])
        for name in ('codex', 'claude'):
            binding = state['operator_programs'][name + '_bin']
            self.assertEqual(Path(binding['node']['path']), self.bins[name].parent / 'node.exe')
            self.assertTrue(Path(binding['path']).is_relative_to(self.bins[name].parent / 'node_modules'))
        from paired_session import winplat
        for turn in state['turns']:
            self.assertIsInstance(turn['pid'], int)
            self.assertTrue(turn['job'].startswith('Local\\review-loop-'))
            with self.assertRaises(ProcessLookupError):
                winplat.probe_job(turn['job'])
            prefix = f"{turn['sequence']:03d}-{turn['phase'].lower()}-{turn['role']}"
            receipt = json.loads((self.run_dir / 'evidence' / (prefix + '.receipt.json')).read_text(encoding='utf-8'))
            self.assertEqual(receipt['job'], turn['job'])
            self.assertNotIn(b'\r\n', (self.run_dir / 'evidence' / (prefix + '.receipt.json')).read_bytes())
            self.assertTrue((self.run_dir / 'evidence' / (prefix + '.stdout.jsonl')).is_file())
        reviewer = next(turn for turn in state['turns'] if (turn['role'], turn['phase']) == ('reviewer', 'EXEC'))
        observed = next(row for row in reviewer['observed_commands'] if row['command'] == TEST_COMMAND)
        self.assertEqual(observed['exit_code'], 0)
        self.assertIn('powershell.exe', observed['raw_command'])
        self.assertIn('W7_REAL_UNITTEST_OK', observed['output'])
        self.assertGreater(reviewer['evidence_guard']['codex_cwd_proven'], 0)
        records = [json.loads(path.read_text(encoding='utf-8')) for path in self.telemetry.glob('*.command.json')]
        self.assertTrue(records)
        for record in records:
            self.assertEqual(record['exit'], 0, record)
        self.assertTrue(any(row['argv'][0] == 'powershell.exe' and row['argv'][-1] == TEST_COMMAND
                            and 'W7_REAL_UNITTEST_OK' in row['stdout'] for row in records))
        self.assertTrue(any(Path(row['argv'][0]).name.lower() == 'bash.exe' and row['argv'][-1] == TEST_COMMAND
                            and 'W7_REAL_UNITTEST_OK' in row['stdout'] for row in records))
        tests = [json.loads(path.read_text(encoding='utf-8')) for path in self.telemetry.glob('*.test.json')]
        self.assertTrue(tests)
        self.assertTrue(all(row['in_job'] for row in tests))
        return records

    def test_full_lifecycle_interrupt_resume_done_and_auto_commit_accepted(self):
        self.load_entry_bundle()
        self.env.update(W7_PAUSE_LOCAL='1', W7_PAUSE_PROVIDER='1')
        flags = ('--auto-commit', 'true', '--quality-writers', 'both')
        # File output lets kill/wait finish without waiting for inherited pipe
        # handles still held by the intentionally surviving local test job.
        with (self.root / 'interrupted.log').open('wb') as output:
            process = subprocess.Popen(self.command('run', *flags), cwd=self.outside, env=self.env,
                                       stdout=output, stderr=subprocess.STDOUT)
            self.processes.append(process)
            self.wait_for(lambda: (self.telemetry / 'provider-ready').exists(), 'first provider launch', cap=35)
            from paired_session import winplat
            active = self.wait_for(lambda: (self.state().get('active') or {}).get('job')
                                   and self.state()['active'], 'persisted live Job Object', cap=5)
            winplat.probe_job(active['job'])
            self.assertTrue(winplat.pid_alive(active['pid']))
            (self.telemetry / 'provider-release').touch()
            ready = self.telemetry / 'local-ready.json'
            def barrier():
                if process.poll() is not None:
                    self.fail((self.root / 'interrupted.log').read_text(encoding='utf-8'))
                return ready.exists()
            self.wait_for(barrier, 'local test between model turns', cap=300)
            interrupted = self.state()
            self.assertIsNone(interrupted.get('active'))
            self.assertIsNone(interrupted.get('uncertain_active'))
            self.assertTrue(interrupted['turns'])
            # Real CLI contenders, same run and a different run on the same workspace.
            same = self.run_process(self.command('resume', *flags), expected=2)
            self.assertIn('another coordinator currently owns this run', same.stdout + same.stderr)
            other = self.run_process(self.command('run', *flags, run_dir=self.root / 'contender'), expected=2)
            self.assertIn('another coordinator currently owns this workspace', other.stdout + other.stderr)
            row = json.loads(ready.read_text(encoding='utf-8'))
            self.assertTrue(row['in_job'])
            self.assertGreater(int(row['shlvl']), 0, 'local command must have entered Git Bash')
            process.kill()
            process.wait(timeout=10)
        from paired_session import winplat
        self.assertTrue(winplat.pid_alive(row['pid']), 'local job survives coordinator restart')
        (self.telemetry / 'local-release').touch()
        self.wait_for(lambda: not winplat.pid_alive(row['pid']), 'released local test to exit', cap=15)
        resumed = self.run_process(self.command('resume', *flags), cap=300)
        self.assertIn('DONE (acceptance pending)', resumed.stdout)
        done = self.state()
        self.assertEqual((done['status'], done['lifecycle']['stage']), ('DONE', 'DONE'))
        identities = lambda rows: [(row['sequence'], row['role'], row['job']) for row in rows]
        self.assertEqual(identities(done['turns'][:len(interrupted['turns'])]), identities(interrupted['turns']))
        stages = [row['stage'] for row in done['lifecycle']['receipts']]
        self.assertTrue({'FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'} <= set(stages))
        self.assertTrue(all(row['status'] == 'READY' for row in done['lifecycle']['receipts']))
        self.assertTrue(all(row['request_id'] and row['output_oid'] for row in done['lifecycle']['receipts']))
        for writer in ('simplifier', 'test-writer'):
            self.assertEqual(done['lifecycle']['quality_writers'][writer]['state'], 'no-op')
        self.assertTrue({('author', 'PLAN'), ('reviewer', 'PLAN'), ('author', 'EXEC'), ('reviewer', 'EXEC'),
                         ('gate', 'EXEC')} <= {(row['role'], row['phase']) for row in done['turns']})
        baseline = done['lifecycle']['quality_writers']['baseline']
        self.assertTrue(baseline['passed'], baseline)
        self.assertEqual(baseline['exit'], 0)
        self.assertIn('W7_REAL_UNITTEST_OK', Path(baseline['log']).read_text(encoding='utf-8'))
        self.assert_turns_and_artifacts(done)
        intent = self.run_process(self.command('accept', *flags, '--intent-only'))
        digest = json.loads(intent.stdout)['digest']
        accepted = self.run_process(self.command('accept', *flags, '--expect', digest))
        self.assertIn('ACCEPTED', accepted.stdout)
        state = self.state()
        self.assertEqual(state['status'], 'ACCEPTED')
        delivery = state['acceptance']['delivery']
        self.assertEqual(self.git('rev-parse', 'HEAD'), delivery['commit'])
        self.assertEqual(self.git('rev-parse', 'HEAD~1'), self.base)
        journal = json.loads((self.run_dir / 'evidence/delivery-commit.json').read_text(encoding='utf-8'))
        self.assertEqual(journal['commit'], delivery['commit'])
        report = (self.run_dir / 'delivery-report.md').read_bytes()
        self.assertIn(delivery['commit'].encode(), report)
        self.assertNotIn(b'\r\n', report)
        manifest = sorted(path for path, value in state['approved_manifest'] if value != 'missing')
        self.assertEqual(self.git('ls-tree', '-r', '--name-only', 'HEAD').splitlines(), manifest)
        for name in manifest:
            blob = subprocess.check_output(['git', '-C', str(self.workspace), 'show', 'HEAD:' + name], env=self.env)
            self.assertNotIn(b'\r\n', blob, name)
        self.assertIn(b'\r\n', (self.workspace / 'sum_ints.py').read_bytes(), 'author fixture exercises CRLF cleaning')
        for name in ('w7_extra.py', 'test_w7_extra.py'):
            self.assertIn(name, manifest)
            self.assertIn(b'\r\n', (self.workspace / name).read_bytes(), name)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('config', 'core.autocrlf'), 'true')
        self.assertEqual(self.git('config', 'core.fileMode'), 'false')

    def tree_record(self):
        return {'head': self.git('rev-parse', 'HEAD'), 'index': self.git('ls-files', '--stage'),
                'status': self.git('status', '--porcelain'),
                'files': {str(path.relative_to(self.workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in self.workspace.rglob('*') if path.is_file()
                          and '.git' not in path.relative_to(self.workspace).parts}}

    def test_materialized_ref_review_report_reaches_reported_without_tree_changes(self):
        self.git('checkout', '-qb', 'topic')
        (self.workspace / 'sum_ints.py').write_bytes(b'def sum_ints(values):\n    # add the values\n    return sum(values)\n')
        self.git('commit', '-qam', 'topic')
        source = self.tree_record()
        materialized = self.run_process([sys.executable, str(REPO / 'scripts/materialize_pr.py'),
            'topic', '--repo', str(self.workspace), '--base', 'main', '--root', str(self.root / 'materialized')])
        pins = json.loads(materialized.stdout)
        self.assertEqual(pins['kind'], 'ref')
        self.assertEqual(pins['head']['oid'], source['head'])
        operator_workspace = self.workspace
        self.workspace = Path(pins['workspace'])
        self.assertNotEqual(self.workspace, operator_workspace)
        self.assertEqual(self.git('rev-parse', 'HEAD'), pins['head']['oid'])
        before = self.tree_record()
        self.load_entry_bundle()
        flags = ('--review-only', '--review-report', '--base', pins['merge_base'])
        reported = self.run_process(self.command('run', *flags))
        self.assertIn('REPORTED', reported.stdout)
        state = self.state()
        self.assertEqual(state['status'], 'REPORTED')
        self.assertTrue(state['report']['complete'])
        self.assertTrue(state['config']['review_only'])
        self.assertTrue(state['config']['review_report'])
        self.assertTrue(all(row['role'] != 'author' for row in state['turns']))
        self.assert_turns_and_artifacts(state)
        report = (self.run_dir / 'review-report.md').read_bytes()
        self.assertIn(b'Status: REPORTED (complete)', report)
        self.assertIn(b'## Verdicts', report)
        self.assertNotIn(b'\r\n', report)
        from paired_session import review_report
        self.assertEqual(report.decode(), review_report.render(state))
        self.assertEqual(self.tree_record(), before)
        self.run_process(self.command('resume', *flags))
        self.assertEqual(self.tree_record(), before)
        self.workspace = operator_workspace
        self.assertEqual(self.tree_record(), source)


if __name__ == '__main__':
    unittest.main()
