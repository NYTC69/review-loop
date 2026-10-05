"""detach (FIELD-17 follow-up, docs/detach.md): `run|resume|reject|permission-probe --detach` leaves the host's process tree
(setsid + a second fork) so a host that ends its turn (Claude Code: SIGTERM to the group; Codex: SIGKILL) cannot kill a run;
`stop` ends it the way Ctrl-C does. Real coordinator processes on the fake CLIs."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

from paired_session import coordinator as rc
from paired_session import test_real_coordinator as trc
from paired_session import timeout_scale as tsc


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()[:1] not in ('Z', '')


class DetachTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.addCleanup(self.forget)

    def forget(self):
        record, _ = rc.detach_paths(self.h.run_dir)
        for path in record.parent.glob(record.stem + '*'):
            path.unlink(missing_ok=True)

    def call(self, *extra, action='run', env=None):
        command = self.h.command('--shadow', 'off', '--adversarial-gate', 'off', *extra) + ['--skip-probe']
        command[2] = action
        return subprocess.run(command, cwd=self.h.root, env={**os.environ, **(env or {})}, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def record(self):
        path, _ = rc.detach_paths(self.h.run_dir)
        return json.loads(path.read_text()) if path.exists() else {}

    def wait(self, ready, seconds=60):
        deadline = time.monotonic() + tsc.scaled(seconds)
        while time.monotonic() < deadline:
            if ready():
                return True
            time.sleep(0.2)
        return False

    def state(self):
        path = self.h.run_dir / 'state.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def test_a_detached_run_returns_at_once_outlives_its_caller_and_leaves_no_process(self):
        started = time.monotonic()
        result = self.call('--detach')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.startswith('DETACHED: pid '), result.stdout)
        pid = int(result.stdout.split()[2].rstrip(';'))
        self.assertLess(time.monotonic() - started, tsc.scaled(10))           # the caller does not wait for the run
        self.assertNotEqual(os.getsid(pid) if alive(pid) else None, os.getsid(0))   # a new session, outside the caller's
        self.assertTrue(self.wait(lambda: self.record().get('status') == 'exited'), self.record())
        self.assertEqual(self.record()['exit_code'], 0, Path(self.record()['log']).read_text())
        self.assertEqual(self.state()['status'], 'DONE')
        self.assertFalse(alive(pid))
        for turn in self.state()['turns']:   # every turn's process group is gone after the run
            self.assertFalse(alive(turn['pid']), turn)

    def test_stop_ends_a_hanging_turn_and_its_group_and_keeps_the_turn_active(self):
        env = {'FAKE_STREAM_TWO_USAGE_THEN_HANG': '1'}
        result = self.call('--detach', '--timeout', '600', env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.wait(lambda: (self.state().get('active') or {}).get('pid')), self.state())
        turn_pid = self.state()['active']['pid']
        again = self.call('--detach', '--timeout', '600', env=env)              # one detached command per run dir
        self.assertEqual(again.returncode, 2)
        self.assertIn('a detached command for this run dir is still running', again.stdout)
        stopped = self.call(action='stop')
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn('STOPPED: detached command pid', stopped.stdout)
        self.assertEqual(self.record()['exit_code'], 130)
        self.assertIn('STOPPED: `stop` ended this detached command', Path(self.record()['log']).read_text())
        self.assertTrue(self.wait(lambda: not alive(turn_pid), 10))          # the coordinator killed the turn's group first
        self.assertEqual(self.state()['active']['pid'], turn_pid)          # the interrupted turn stays active: uncertain on resume
        self.assertIn('NOTE: no detached command is running', self.call(action='stop').stdout)

    def test_two_simultaneous_detaches_start_exactly_one(self):
        env = {**os.environ, 'FAKE_STREAM_TWO_USAGE_THEN_HANG': '1'}
        command = self.h.command('--shadow', 'off', '--adversarial-gate', 'off', '--detach', '--timeout', '600') + ['--skip-probe']
        calls = [subprocess.Popen(command, cwd=self.h.root, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
        outputs = [call.communicate()[0] for call in calls]
        self.assertEqual(sorted(out.split(':', 1)[0] for out in outputs), ['DETACHED', 'REFUSED'], outputs)
        self.assertTrue(self.wait(lambda: (self.state().get('active') or {}).get('pid')), self.state())
        self.assertIn('STOPPED: detached command pid', self.call(action='stop').stdout)

    def test_a_relative_run_dir_shares_the_record_and_lock(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.h.run_dir.parent)
        self.assertEqual(rc.detach_paths(self.h.run_dir.name), rc.detach_paths(self.h.run_dir))

    def test_a_start_that_fails_after_the_fork_is_reported_and_releases_the_lock(self):
        record, _ = rc.detach_paths(self.h.run_dir)
        now = time.time()
        for offset in range(8):   # the grandchild cannot open its log (a directory holds each name it could pick)
            blocker = record.with_name(record.stem + time.strftime('-%Y%m%dT%H%M%S.log', time.localtime(now + offset)))
            blocker.mkdir(exist_ok=True)
            self.addCleanup(blocker.rmdir)
        result = self.call('--detach')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('REFUSED: the detached command did not start: IsADirectoryError', result.stdout)
        self.assertIsNone(rc.detach_holder(self.h.run_dir))
        self.assertFalse((self.h.run_dir / 'state.json').exists())   # nothing ran

    def hold_lock(self, record_body):
        """Another holder: this test process holds the lock, with the given record."""
        record, lock = rc.detach_paths(self.h.run_dir)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        rc.fcntl.flock(fd, rc.fcntl.LOCK_EX)
        record.write_text(json.dumps(record_body))
        sleeper = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.addCleanup(lambda: sleeper.poll() is None and sleeper.kill())
        return sleeper

    def test_stop_never_signals_an_older_run_s_pid_or_a_reused_one(self):
        sleeper = self.hold_lock({})
        for body, expected in (({'status': 'exited', 'pid': sleeper.pid, 'sid': os.getsid(sleeper.pid)}, 'HOLD: a detached command '
                                'for this run dir is starting'),   # an older run's record while a new holder starts
                               ({'status': 'running', 'pid': sleeper.pid, 'sid': os.getsid(sleeper.pid) + 99991},
                                'HOLD: the detached command')):   # a pid reused by another session
            with self.subTest(body=body):
                rc.detach_paths(self.h.run_dir)[0].write_text(json.dumps(body))
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(rc.stop_detached(str(self.h.run_dir), wait=0.5), 2)
                self.assertIn(expected, out.getvalue())
                self.assertIsNone(sleeper.poll())   # never signalled

    def test_stop_handles_a_holder_that_exited_before_the_signal(self):
        gone = subprocess.Popen(['true']); gone.wait()
        self.hold_lock({'status': 'running', 'pid': gone.pid, 'sid': 1})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(rc.stop_detached(str(self.h.run_dir), wait=0.5), 2)   # no ProcessLookupError escapes
        self.assertIn('has not exited after', out.getvalue())   # the lock (held here) decides, not the pid

    def test_the_stop_handler_kills_the_turn_group_before_it_unwinds(self):
        sleeper = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.addCleanup(lambda: sleeper.poll() is None and sleeper.kill())
        rc.DETACH_TURN['group'] = sleeper.pid
        self.addCleanup(rc.DETACH_TURN.update, group=None)
        rc.DETACH_TURN['spawning'] = True   # a stop while the turn is being spawned is only noted
        self.addCleanup(rc.DETACH_TURN.update, spawning=False)
        self.assertIsNone(rc.stop_turn_group())
        self.assertTrue(rc.DETACH_TURN.pop('stop'))
        self.assertIsNone(sleeper.poll())
        rc.DETACH_TURN['spawning'] = False
        with self.assertRaises(rc.DetachStop):
            rc.stop_turn_group()
        self.assertEqual(sleeper.wait(timeout=10), -9)

    def test_detach_is_refused_for_other_actions(self):
        result = self.call('--detach', action='status')
        self.assertEqual(result.returncode, 2)
        self.assertIn('REFUSED: --detach is only for run, resume, reject, permission-probe', result.stdout)


if __name__ == '__main__':
    unittest.main()
