"""WI-WIN W2b: the Job Object launcher (winjob.py) and the winplat process-tree helpers.

The POSIX tests pin each helper's POSIX branch to the code it replaced (start_new_session, killpg SIGKILL,
retry_killpg_eperm) and drive the launcher's run() with a fake kernel, so its exit-code and stdin paths run on every OS.
The Windows tests (windows-latest CI, see windows-ci-modules.txt) start real job trees from an unrelated cwd with no
PYTHONPATH and a support path containing spaces."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc, winjob, winplat

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / 'paired_session' / 'winjob.py'
POSIX = not winplat.IS_WINDOWS
FLAGS = 0 if POSIX else subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
# The target spawns a grandchild that sleeps, publishes both pids, then sleeps itself (or exits with argv[3] at once).
CHILD = '''import os, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
tmp = sys.argv[1] + '.tmp'
with open(tmp, 'w') as out:
    out.write(f'{os.getpid()} {grandchild.pid}')
os.replace(tmp, sys.argv[1])
if len(sys.argv) > 2:
    sys.exit(int(sys.argv[2]))
time.sleep(60)
'''


class TempDirTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='winjob tests ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.unrelated = self.root / 'unrelated cwd'; self.unrelated.mkdir()
        self.support = self.root / 'support dir'; self.support.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}


class FakeKernel:
    def __init__(self, fail=None, active=1):
        self.fail, self.active_count, self.calls = fail, active, []

    def create(self, name):
        self.calls.append(('create', name))
        if self.fail == 'create':
            raise OSError('CreateJobObjectW failed (WinError 5)')
        return 'job'

    def assign_self(self, job):
        self.calls.append(('assign', job))
        if self.fail == 'assign':
            raise OSError('AssignProcessToJobObject failed (WinError 5)')

    def active(self, job):
        self.calls.append(('active', job))
        return self.active_count

    def terminate(self, job, code):
        self.calls.append(('terminate', job, code))

    def close(self, job):
        self.calls.append(('close', job))


@unittest.skipUnless(POSIX, 'POSIX branches')
class PosixBranchTests(TempDirTest):
    def test_spawn_group_is_popen_with_start_new_session_and_no_job(self):
        with patch('subprocess.Popen') as popen:
            process, job = winplat.spawn_group(['cli', '-x'], 'run-001', cwd='/w', stdin=subprocess.PIPE)
        popen.assert_called_once_with(['cli', '-x'], cwd='/w', stdin=subprocess.PIPE, start_new_session=True)
        self.assertIs(process, popen.return_value)
        self.assertIsNone(job)

    def test_kill_group_is_killpg_sigkill_and_probe_group_is_retry_killpg_eperm(self):
        with patch('os.killpg') as killpg:
            winplat.kill_group(4242, None)
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        with patch.object(rc, 'retry_killpg_eperm', side_effect=ProcessLookupError) as probe, \
                self.assertRaises(ProcessLookupError):
            rc.probe_group(4242, 'ignored on POSIX')
        probe.assert_called_once_with(4242)

    def test_a_real_group_with_a_grandchild_is_killed_and_probed_gone(self):
        pids = self.root / 'pids'
        process, job = winplat.spawn_group([sys.executable, '-c', CHILD, str(pids)], 'posix', cwd=self.unrelated,
                                           env=self.env, stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 15
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child, grandchild = map(int, pids.read_text().split())
        self.assertTrue(winplat.pid_alive(grandchild))
        rc.probe_group(process.pid, job)   # alive: returns
        winplat.kill_group(process.pid, job)
        process.wait(timeout=15)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                rc.probe_group(process.pid, job)
            except (ProcessLookupError, PermissionError):   # macOS: a zombie-only group answers EPERM (as _stop_turn_group)
                break
            time.sleep(0.05)
        else:
            self.fail('the process group still answers after SIGKILL')

    def test_the_dispatch_receipts_carry_no_job_key(self):
        from paired_session import test_real_coordinator as trc
        case = trc.RealCoordinatorTests('test_workspace_lease_path_is_stable_and_outside_workspace')
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.addCleanup(case.tearDown)
        done = case.run_coordinator('--polish-round', 'off')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = rc.json.loads((case.run_dir / 'state.json').read_text(encoding='utf-8'))
        self.assertTrue(state['turns'])
        self.assertTrue(all(type(turn.get('pid')) is int and 'job' not in turn for turn in state['turns']))


class LauncherLogicTests(TempDirTest):
    """winjob.run with a fake kernel: the order of job calls and every exit code, on every OS."""

    def test_job_name_is_unique_safe_and_accepted_by_the_launcher(self):
        first, second = winplat.job_name('run dir/ü:001'), winplat.job_name('run dir/ü:001')
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith('Local\\review-loop-run_dir___001-'), first)
        self.assertTrue(winjob.NAME_RE.fullmatch(first), first)

    def test_create_or_assignment_failure_is_exit_125_and_starts_nothing(self):
        for fail in ('create', 'assign'):
            with self.subTest(fail), patch.object(winjob.subprocess, 'Popen') as popen, \
                    patch('sys.stderr', new_callable=__import__('io').StringIO) as err:
                kernel = FakeKernel(fail=fail)
                self.assertEqual(winjob.run('Local\\review-loop-x', ['target'], kernel), winjob.EXIT_JOB)
            popen.assert_not_called()
            self.assertIn('winjob: cannot ' + ('create' if fail == 'create' else 'join'), err.getvalue())
            if fail == 'assign':
                self.assertEqual(kernel.calls[-1], ('close', 'job'))

    def test_a_target_that_cannot_start_is_exit_127(self):
        with patch('sys.stderr', new_callable=__import__('io').StringIO) as err:
            code = winjob.run('Local\\review-loop-x', [str(self.root / 'missing-binary')], FakeKernel())
        self.assertEqual(code, winjob.EXIT_START)
        self.assertIn('winjob: cannot start', err.getvalue())

    def test_exit_code_propagates_and_leftovers_are_ended_with_it(self):
        for active, ended in ((1, False), (3, True)):
            with self.subTest(active=active):
                kernel = FakeKernel(active=active)
                code = winjob.run('Local\\review-loop-x', [sys.executable, '-c', 'raise SystemExit(7)'], kernel)
                self.assertEqual(code, 7)
                self.assertEqual(kernel.calls[:2], [('create', 'Local\\review-loop-x'), ('assign', 'job')])
                self.assertEqual(('terminate', 'job', 7) in kernel.calls, ended)

    def test_standalone_file_passes_stdin_and_output_from_an_unrelated_cwd(self):
        launcher = self.support / 'winjob copy.py'
        launcher.write_bytes(LAUNCHER.read_bytes())
        driver = self.support / 'driver.py'
        driver.write_text('''import importlib.util, sys
spec = importlib.util.spec_from_file_location('winjob', sys.argv[1])
winjob = importlib.util.module_from_spec(spec); spec.loader.exec_module(winjob)
class Kernel:
    create = lambda self, name: 'job'
    assign_self = close = lambda self, job: None
    active = lambda self, job: 1
sys.exit(winjob.run('Local\\\\review-loop-x', sys.argv[2:], Kernel()))
''', encoding='utf-8')
        out = self.root / 'out.txt'
        target = [sys.executable, '-c', 'import sys; sys.stdout.write(sys.stdin.read().upper()); sys.exit(3)']
        with open(out, 'wb') as stream:
            done = subprocess.run([sys.executable, '-I', str(driver), str(launcher), *target], cwd=self.unrelated,
                                  env=self.env, input=b'the prompt', stdout=stream, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(out.read_bytes(), b'THE PROMPT')

    def test_main_refuses_a_malformed_command_line_and_posix(self):
        for argv in ([], ['Local\\review-loop-x', 'target'], ['Global\\other', '--', 'target'],
                     ['Local\\review-loop-a b', '--', 'target']):
            with self.subTest(argv=argv), patch('sys.stderr', new_callable=__import__('io').StringIO):
                self.assertEqual(winjob.main(argv), winjob.EXIT_USAGE)
        if POSIX:
            done = subprocess.run([sys.executable, '-I', str(LAUNCHER), 'Local\\review-loop-x', '--', 'true'],
                                  cwd=self.unrelated, env=self.env, capture_output=True, text=True, timeout=30)
            self.assertEqual((done.returncode, done.stderr.strip()),
                             (winjob.EXIT_JOB, 'winjob: Job Objects need native Windows'))


class CoordinatorBranchTests(unittest.TestCase):
    """_stop_turn_group on Windows uses the receipt's job name; a Windows receipt without one is unverifiable."""

    def owner(self, **receipt):
        return types.SimpleNamespace(state={'turns': [{'sequence': 7, **receipt}], 'spawn_failures': []})

    def test_windows_probe_and_kill_use_the_job_name(self):
        with patch.object(winplat, 'IS_WINDOWS', True), \
                patch.object(winplat, 'probe_job', side_effect=[None, ProcessLookupError]) as probe, \
                patch.object(winplat, 'kill_group') as kill:
            rc.Coordinator._stop_turn_group(self.owner(pid=4242, job='Local\\review-loop-r-007-ab'), 7)
        self.assertEqual(probe.call_args_list, [unittest.mock.call('Local\\review-loop-r-007-ab')] * 2)
        kill.assert_called_once_with(4242, 'Local\\review-loop-r-007-ab')
        with patch.object(winplat, 'IS_WINDOWS', True), \
                patch.object(winplat, 'probe_job', side_effect=ProcessLookupError) as probe:
            rc.Coordinator._stop_turn_group(self.owner(pid=4242, job='Local\\review-loop-r-007-ab'), 7, kill=False)
        probe.assert_called_once_with('Local\\review-loop-r-007-ab')

    def test_a_windows_receipt_without_a_job_is_unverifiable(self):
        with patch.object(winplat, 'IS_WINDOWS', True), patch.object(winplat, 'probe_job') as probe, \
                patch.object(winplat, 'kill_group') as kill:
            with self.assertRaisesRegex(RuntimeError, 'no verifiable pid'):
                rc.Coordinator._stop_turn_group(self.owner(pid=4242), 7, kill=False)
            self.assertIsNone(rc.Coordinator._stop_turn_group(self.owner(pid=4242), 7))
        probe.assert_not_called()
        kill.assert_not_called()

    def test_windows_kill_and_probe_need_a_job_name(self):
        with patch.object(winplat, 'IS_WINDOWS', True):
            with self.assertRaises(ProcessLookupError):
                winplat.kill_group(4242, None)
            with self.assertRaises(OSError):
                winplat.probe_job(None)


@unittest.skipUnless(winplat.IS_WINDOWS, 'native Windows Job Objects')
class WindowsJobTests(TempDirTest):
    def setUp(self):
        super().setUp()
        self.launcher = self.support / 'paired_session' / 'winjob.py'
        self.launcher.parent.mkdir()
        self.launcher.write_bytes(LAUNCHER.read_bytes())
        self.child = self.support / 'child script.py'
        self.child.write_text(CHILD, encoding='utf-8')
        self.pids = self.root / 'pids'

    def launch(self, name, *extra):
        process = subprocess.Popen([sys.executable, '-I', str(self.launcher), name, '--', sys.executable,
                                    str(self.child), str(self.pids), *extra], cwd=self.unrelated, env=self.env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=FLAGS)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def wait_pids(self, process):
        deadline = time.monotonic() + 30
        while not self.pids.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.pids.exists(), process.poll())
        return [int(pid) for pid in self.pids.read_text().split()]

    def wait_gone(self, pids):
        deadline = time.monotonic() + 15
        while any(winplat.pid_alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse([pid for pid in pids if winplat.pid_alive(pid)])

    def test_a_restarted_coordinator_probes_and_kills_child_and_grandchild_by_name(self):
        name = winplat.job_name('restart')
        process = self.launch(name)
        pids = self.wait_pids(process)
        self.assertTrue(all(winplat.pid_alive(pid) for pid in pids))
        fresh = ('import sys; sys.path.insert(0, sys.argv[1]); from paired_session import winplat; '
                 'winplat.probe_job(sys.argv[2]); winplat.kill_group(0, sys.argv[2])')
        done = subprocess.run([sys.executable, '-c', fresh, str(REPO), name], cwd=self.unrelated, env=self.env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        process.communicate(timeout=30)
        self.assertEqual(process.returncode, 1)   # the TerminateJobObject code
        self.wait_gone(pids)
        with self.assertRaises(ProcessLookupError):
            winplat.probe_job(name)
        with self.assertRaises(ProcessLookupError):
            winplat.kill_group(0, name)

    def test_leftovers_end_with_the_target_and_its_exit_code_propagates(self):
        name = winplat.job_name('leftover')
        process = self.launch(name, '4')
        process.communicate(timeout=30)
        self.assertEqual(process.returncode, 4)
        self.wait_gone(self.wait_pids(process))
        with self.assertRaises(ProcessLookupError):
            winplat.probe_job(name)

    def test_spawn_group_passes_stdin_and_output_and_the_exit_code(self):
        out = self.root / 'out.txt'
        with open(out, 'wb') as stream:
            process, job = winplat.spawn_group(
                [sys.executable, '-c', 'import sys; sys.stdout.write(sys.stdin.read().upper()); sys.exit(5)'], 'stdin',
                cwd=self.unrelated, env=self.env, stdin=subprocess.PIPE, stdout=stream, stderr=subprocess.STDOUT)
            process.communicate(b'the prompt', timeout=30)
        self.assertEqual((process.returncode, out.read_bytes()), (5, b'THE PROMPT'))
        self.assertTrue(winjob.NAME_RE.fullmatch(job), job)
        with self.assertRaises(ProcessLookupError):
            rc.probe_group(process.pid, job)
        with self.assertRaises(FileNotFoundError):
            winplat.spawn_group([str(self.root / 'missing.exe')], 'missing', cwd=self.unrelated)

    def test_a_taken_job_name_is_exit_125(self):
        kernel = winjob.Kernel()
        name = winplat.job_name('taken')
        held = kernel.create(name)
        self.addCleanup(kernel.close, held)
        process = self.launch(name)
        _, err = process.communicate(timeout=30)
        self.assertEqual(process.returncode, winjob.EXIT_JOB)
        self.assertIn(b'winjob: cannot create job', err)
        self.assertFalse(self.pids.exists())


if __name__ == '__main__':
    unittest.main()
