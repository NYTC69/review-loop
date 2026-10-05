"""SMOKE-RUNNER-KILL: a killed smoke runner leaves no child process alive (SIGTERM/SIGINT, repeated too) and no temp_config
behind; after an uncatchable SIGKILL the next run (holding the runner lock) restores the config and only reports the dead
runner's process group. One runner at a time; a malformed marker refuses the run and changes nothing."""
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import run_skill_smoke_lib as lib

CONFIG = ROOT / ".review-loop/config.md"
MARKER = ROOT / lib.RUNNER_MARKER
SMOKE_DIR = ROOT / "tests/skills/smoke"
RUNNER_ENV = {**os.environ, "PAIRED_SESSION_TEST_TIMEOUT_SCALE": "1"}   # case timeouts as written


def wait_for(predicate, seconds=30.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):   # macOS: a group left with only unreaped zombies answers EPERM
        return False
    return True


def marker(config=None, child_pgid=None):
    return json.dumps({"version": 1, "config": config, "child_pgid": child_pgid})


class RecoverStaleRunnerTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = self.root / ".review-loop/config.md"
        self.marker = self.root / lib.RUNNER_MARKER
        self.marker.parent.mkdir(parents=True)

    def test_a_dead_runner_config_is_restored_byte_for_byte(self):
        original = b"entry: legacy\r\nreviewer_model: x\r\n"   # CRLF survives
        for backup, expected in (({"had_existing": True, "data_b64": base64.b64encode(original).decode()}, original),
                                 ({"had_existing": False, "data_b64": None}, None)):
            with self.subTest(backup=backup):
                self.config.write_text("entry: smoke-temp\n", encoding="utf-8")   # what the dead runner applied
                self.marker.write_text(marker(backup))
                self.assertEqual(lib.recover_stale_runner(self.root),
                                 ["restored .review-loop/config.md left by a dead smoke runner"])
                self.assertEqual(self.config.read_bytes() if self.config.exists() else None, expected)
                self.assertFalse(self.marker.exists())
        self.assertEqual(lib.recover_stale_runner(self.root), [])   # nothing left: a clean start

    def test_a_malformed_marker_refuses_and_changes_nothing(self):
        self.config.write_text("entry: smoke-temp\n", encoding="utf-8")
        for text in ('{"config": {}}', marker({"had_existing": True}), marker({"had_existing": True, "data_b64": None}),
                     marker(None, "12"), marker(None, 1), "[]", "not json",
                     marker({"had_existing": True, "data_b64": "!!!"}),        # b64decode alone would yield b""
                     marker({"had_existing": True, "data_b64": "abc"}),        # bad padding
                     marker({"had_existing": True, "data_b64": "\u00e9"})):   # not ASCII
            with self.subTest(marker=text):
                self.marker.write_text(text)
                with self.assertRaisesRegex(RuntimeError, "(malformed|unreadable) smoke runner marker"):
                    lib.recover_stale_runner(self.root)
                self.assertEqual(self.config.read_text(encoding="utf-8"), "entry: smoke-temp\n")
                self.assertTrue(self.marker.exists())

    def test_a_live_group_of_a_dead_runner_is_reported_not_signalled(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        self.marker.write_text(marker(None, sleeper.pid))
        self.assertEqual(lib.recover_stale_runner(self.root),
                         [f"process group {sleeper.pid} of that runner may still be alive; it was not signalled, stop it by hand"])
        self.assertIsNone(sleeper.poll())   # never killed by the recovery

    def test_the_interrupt_backstop_stops_the_case_group_and_restores_the_config(self):
        original = b"entry: legacy\n"
        self.config.write_text("entry: smoke-temp\n", encoding="utf-8")
        self.marker.write_text(marker({"had_existing": True, "data_b64": base64.b64encode(original).decode()}))
        case = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(lambda: case.poll() is None and (case.kill(), case.wait()))
        before = (self.config.read_bytes(), self.marker.read_bytes())
        self.assertEqual(lib.interrupt_backstop(self.root, None, holds_lock=False), [])   # failed to take the lock:
        self.assertEqual((self.config.read_bytes(), self.marker.read_bytes()), before)   # another runner's marker stays
        notes = lib.interrupt_backstop(self.root, case, holds_lock=True)   # as if a finally had been cut short
        self.assertIsNotNone(case.returncode)
        self.assertFalse(group_alive(case.pid))
        self.assertEqual((notes, self.config.read_bytes(), self.marker.exists()),
                         (["restored .review-loop/config.md left by a dead smoke runner"], original, False))
        self.assertEqual(lib.interrupt_backstop(self.root, case, holds_lock=True), [])   # idempotent

    def test_one_runner_lock_at_a_time(self):
        fd = lib.acquire_runner_lock(self.root)
        with self.assertRaisesRegex(RuntimeError, "another smoke runner holds"):
            lib.acquire_runner_lock(self.root)
        os.close(fd)   # a dead runner's lock is released with its process
        os.close(lib.acquire_runner_lock(self.root))


class LoadScaledTimeoutTest(unittest.TestCase):   # SMOKE-TIMEOUT: the paired-session tscale rule, fixtures unchanged
    def test_the_case_timeout_follows_the_shared_load_factor(self):
        from paired_session import timeout_scale
        for override, expected in (("2", (1200, 2.0)), ("1", (600, 1.0)), ("50", (3600, 6.0))):
            with self.subTest(override=override), patch.dict(os.environ, {timeout_scale.ENV: override}):
                self.assertEqual(lib.load_scaled_timeout(600), expected)
        with patch.dict(os.environ, {timeout_scale.ENV: ""}), patch.object(os, "getloadavg", return_value=(1e9, 0, 0)):
            self.assertEqual(lib.load_scaled_timeout(600), (3600, 6.0))   # clamped like the paired-session tests


class RunnerKillTest(unittest.TestCase):
    """The real runner on a fake case: the case applies a temp_config and leaves a background child in its group."""

    def setUp(self):
        try:   # a real smoke runner holds the lock: these tests would touch its config, case dir and marker
            os.close(lib.acquire_runner_lock(ROOT))
        except RuntimeError:
            self.skipTest("a smoke runner is running in this checkout")
        self.case_id = "smokefix.runner-kill.fake"
        self.case_path = SMOKE_DIR / f"{self.case_id}.json"
        self.artifact_dir = ROOT / "tests/skills/.artifacts" / self.case_id
        self.original = CONFIG.read_bytes() if CONFIG.exists() else None
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.pgid_file = Path(temp.name) / "pgid"
        self.addCleanup(self.cleanup)
        self.write_case()

    def write_case(self, timeout=300, child_ignores_term=False):
        child = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)" if child_ignores_term
                 else "import time; time.sleep(120)")
        script = ("import os, pathlib, signal, subprocess, sys, time\n"
                  "def term(*_):\n"   # proof that the runner's SIGTERM reached the case (not only the later SIGKILL)
                  "    pathlib.Path(sys.argv[1] + '.term').write_text('1')\n"
                  "    sys.exit(0)\n"
                  "signal.signal(signal.SIGTERM, term)\n"
                  f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"   # stays in this group
                  "time.sleep(0.5)\n"
                  "pathlib.Path(sys.argv[1]).write_text(str(os.getpgrp()))\n"
                  "time.sleep(120)\n")
        case = {"id": self.case_id, "type": "smoke", "target": "review-loop", "runtime": "shared", "requires": ["git"],
                "execution_policy": "best_effort",
                "setup": {"temp_config": "entry: smokefix-temp\n", "timeout_seconds": timeout},
                "artifacts": {"capture": {}, "required": ["meta"]},
                "command": [sys.executable, "-c", script, str(self.pgid_file)],
                "assertions": ["session_created"]}
        self.case_path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")

    def cleanup(self):
        self.case_path.unlink(missing_ok=True)
        if self.artifact_dir.exists():
            shutil.rmtree(self.artifact_dir)
        if self.pgid_file.exists() and group_alive(int(self.pgid_file.read_text())):
            os.killpg(int(self.pgid_file.read_text()), signal.SIGKILL)
        if self.original is None:
            CONFIG.unlink(missing_ok=True)
        else:
            CONFIG.write_bytes(self.original)
        try:   # only while no runner holds the lock: never delete a live runner's SIGKILL safety net
            fd = lib.acquire_runner_lock(ROOT)
        except RuntimeError:
            return
        MARKER.unlink(missing_ok=True)
        os.close(fd)

    def run_runner(self, *args):
        return subprocess.run(["bash", "scripts/run-skill-smoke", *args], cwd=ROOT, capture_output=True, text=True,
                              env=RUNNER_ENV, timeout=180)

    def start_runner(self, ignored=()):
        def preexec():   # like nohup: the runner starts with these signals ignored
            for signum in ignored:
                signal.signal(signum, signal.SIG_IGN)
        runner = subprocess.Popen(["bash", "scripts/run-skill-smoke", "--case", self.case_id], cwd=ROOT, env=RUNNER_ENV,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                                  preexec_fn=preexec)
        self.addCleanup(lambda: runner.poll() is None and (os.killpg(runner.pid, signal.SIGKILL), runner.wait()))
        self.assertTrue(wait_for(self.pgid_file.exists), "the fake case never started")
        self.assertEqual(CONFIG.read_text(encoding="utf-8"), "entry: smokefix-temp\n")
        self.assertTrue(MARKER.exists())
        return runner, int(self.pgid_file.read_text())

    def test_sigterm_sigint_and_a_repeated_signal_stop_the_case_group_and_restore_the_config(self):
        for signals in ((signal.SIGTERM,), (signal.SIGINT,), (signal.SIGTERM, signal.SIGTERM, signal.SIGINT)):
            with self.subTest(signals=signals):
                self.pgid_file.unlink(missing_ok=True)
                Path(str(self.pgid_file) + '.term').unlink(missing_ok=True)
                runner, pgid = self.start_runner()
                for signum in signals:
                    runner.send_signal(signum)
                    time.sleep(0.05)
                out, err = runner.communicate(timeout=60)
                self.assertEqual(runner.returncode, 128 + signals[0], out + err)
                self.assertIn(f"INTERRUPTED - signal {signals[0]}", out)
                self.assertTrue(wait_for(lambda: not group_alive(pgid), 10), "a child of the case outlived the runner")
                self.assertTrue(Path(str(self.pgid_file) + '.term').exists(), "the case never received SIGTERM")
                self.assertEqual(CONFIG.read_bytes() if CONFIG.exists() else None, self.original)
                self.assertFalse(MARKER.exists())

    def test_an_inherited_ignored_sighup_stays_ignored(self):
        runner, pgid = self.start_runner(ignored=(signal.SIGHUP,))
        runner.send_signal(signal.SIGHUP)   # a closed terminal under nohup
        time.sleep(1)
        self.assertIsNone(runner.poll(), "a nohup runner was interrupted by SIGHUP")
        self.assertEqual(CONFIG.read_text(encoding="utf-8"), "entry: smokefix-temp\n")
        self.assertTrue(group_alive(pgid))
        runner.send_signal(signal.SIGTERM)   # still stoppable on purpose
        out, err = runner.communicate(timeout=60)
        self.assertEqual(runner.returncode, 128 + signal.SIGTERM, out + err)
        self.assertTrue(wait_for(lambda: not group_alive(pgid), 10))
        self.assertEqual(CONFIG.read_bytes() if CONFIG.exists() else None, self.original)

    def test_after_sigkill_the_next_run_restores_the_config_and_only_reports_the_group(self):
        runner, pgid = self.start_runner()
        runner.kill()   # uncatchable: nothing runs in the runner; its lock goes with it
        runner.communicate(timeout=30)
        self.assertEqual(CONFIG.read_text(encoding="utf-8"), "entry: smokefix-temp\n")
        self.assertTrue(group_alive(pgid))
        nxt = self.run_runner("--case", "smokefix.no-such-case")
        self.assertIn("NOTE restored .review-loop/config.md left by a dead smoke runner", nxt.stdout, nxt.stdout + nxt.stderr)
        self.assertIn(f"NOTE process group {pgid} of that runner may still be alive; it was not signalled", nxt.stdout)
        self.assertEqual(CONFIG.read_bytes() if CONFIG.exists() else None, self.original)
        self.assertFalse(MARKER.exists())
        self.assertTrue(group_alive(pgid))   # not ours to kill; the cleanup stops it

    def test_a_held_lock_or_a_malformed_marker_refuses_the_run_without_touching_the_config(self):
        fd = lib.acquire_runner_lock(ROOT)
        try:
            busy = self.run_runner("--case", self.case_id)
        finally:
            os.close(fd)
        self.assertEqual(busy.returncode, 1, busy.stdout + busy.stderr)
        self.assertIn("FAIL runner-lock - another smoke runner holds", busy.stdout)
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text('{"config": {}}')
        bad = self.run_runner("--case", self.case_id)
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)
        self.assertIn("FAIL runner-lock - malformed smoke runner marker", bad.stdout)
        self.assertTrue(MARKER.exists())
        self.assertFalse(self.pgid_file.exists())   # the case never started
        self.assertEqual(CONFIG.read_bytes() if CONFIG.exists() else None, self.original)

    def test_a_finished_case_leaves_no_background_descendant(self):
        script = ("import os, pathlib, subprocess, sys\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], stdin=subprocess.DEVNULL,"
                  " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"   # in the group, pipes released
                  "pathlib.Path(sys.argv[1]).write_text(str(os.getpgrp()))\n")   # the leader exits at once
        case = json.loads(self.case_path.read_text(encoding="utf-8"))
        case["command"] = [sys.executable, "-c", script, str(self.pgid_file)]
        self.case_path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
        done = self.run_runner("--case", self.case_id)
        self.assertIn(self.case_id, done.stdout, done.stdout + done.stderr)
        pgid = int(self.pgid_file.read_text())
        self.assertTrue(wait_for(lambda: not group_alive(pgid), 10), "a background child outlived its finished case")
        self.assertEqual(CONFIG.read_bytes() if CONFIG.exists() else None, self.original)
        self.assertFalse(MARKER.exists())

    def test_a_timeout_stops_a_descendant_that_ignores_sigterm(self):
        self.write_case(timeout=3, child_ignores_term=True)
        done = self.run_runner("--case", self.case_id)
        self.assertIn(f"{self.case_id}", done.stdout, done.stdout + done.stderr)
        pgid = int(self.pgid_file.read_text())
        self.assertTrue(wait_for(lambda: not group_alive(pgid), 10), "the SIGTERM-ignoring descendant outlived the timeout")
        self.assertEqual(CONFIG.read_bytes() if CONFIG.exists() else None, self.original)
        self.assertFalse(MARKER.exists())


if __name__ == "__main__":
    unittest.main()
