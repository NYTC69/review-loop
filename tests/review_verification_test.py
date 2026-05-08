"""Unit tests for scripts/review_verification.py.

All subprocess interactions mocked via
`unittest.mock.patch.object(subprocess, "Popen", FakePopen)`. Tests
run offline.

Test classes:
  - PerJobTimeoutTest (and negative)
  - ReturncodePropagationTest (and negative)
  - DrainOnTimeoutTest (and negative)
  - ImmutabilityOfFinishedJobsDuplicateRecordTest
  - ImmutabilityOfFinishedJobsLateDrainTest
  - DefaultConflictKeysAreCodebaseInvariantsOnlyTest
  - DefaultCapacityKeysAreRuntimeRateLimitsTest
  - ParallelSameRuntimeFanOutTest
  - CapacityLimitThrottleTest
  - BinaryLockConflictTest
  - BuildArgvTest
  - StdinDeliveryTest
  - StreamJsonResultExtractionTest
  - SchemaValidationTest
  - TempPromptCleanupTest
  - CliShapeTest
  - ClaudeCodeOutputFileTest
  - WorkerExceptionPropagationTest

Stdlib unittest. Mirrors `tests/replay_sessions_test.py` style.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "review_verification.py"

sys.path.insert(0, str(ROOT / "scripts"))
import review_verification as rv  # noqa: E402


# --------------------------------------------------------------------------
# FakePopen helpers
# --------------------------------------------------------------------------


class _FakeStream:
    """Minimal file-like that yields a fixed bytes payload then EOF."""

    def __init__(self, payload: bytes = b"") -> None:
        self._buf = io.BytesIO(payload)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def close(self) -> None:
        self._buf.close()


class FakePopen:
    """Drop-in replacement for `subprocess.Popen` used in tests.

    Behavior is configured via class-level attributes set per-test:
      - `returncode_value`: int returned by `wait`.
      - `stdout_payload`, `stderr_payload`: bytes emitted on stdout/stderr.
      - `wait_delay`: seconds `wait` blocks before returning (simulates work).
      - `raise_timeout`: if True, first `wait(timeout=...)` raises
        `subprocess.TimeoutExpired`.
      - `record_kwargs`: list to append `kwargs` from each construction.
    """

    returncode_value = 0
    stdout_payload = b""
    stderr_payload = b""
    wait_delay = 0.0
    raise_timeout = False
    record_kwargs: list = []
    record_argv: list = []
    pid_counter = 90000

    def __init__(self, argv, **kwargs):
        FakePopen.record_argv.append(list(argv))
        FakePopen.record_kwargs.append(dict(kwargs))
        self.argv = argv
        self.stdout = _FakeStream(FakePopen.stdout_payload)
        self.stderr = _FakeStream(FakePopen.stderr_payload)
        FakePopen.pid_counter += 1
        self.pid = FakePopen.pid_counter
        self.returncode = None
        self._wait_called = False
        self._terminated = False

    def wait(self, timeout=None):
        if FakePopen.raise_timeout and not self._wait_called:
            self._wait_called = True
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)
        if FakePopen.wait_delay:
            time.sleep(FakePopen.wait_delay)
        self._wait_called = True
        self.returncode = FakePopen.returncode_value
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self._terminated = True
        self.returncode = -15


def reset_fakepopen(
    returncode_value: int = 0,
    stdout_payload: bytes = b"",
    stderr_payload: bytes = b"",
    wait_delay: float = 0.0,
    raise_timeout: bool = False,
) -> None:
    FakePopen.returncode_value = returncode_value
    FakePopen.stdout_payload = stdout_payload
    FakePopen.stderr_payload = stderr_payload
    FakePopen.wait_delay = wait_delay
    FakePopen.raise_timeout = raise_timeout
    FakePopen.record_kwargs = []
    FakePopen.record_argv = []


def _killpg_noop(pid, sig):  # noqa: ARG001
    return None


def _make_job(
    job_id: str = "j1",
    session_id: str = "sess",
    runtime: str = "codex",
    prompt_text: str = "review me",
    reviewer_model: str = "gpt-5.5",
    timeout_secs: float = 30.0,
    conflict_keys=frozenset(),
    capacity_keys=frozenset(),
    worktree=None,
) -> rv.ReviewJob:
    return rv.ReviewJob(
        job_id=job_id,
        session_id=session_id,
        runtime=runtime,
        prompt_text=prompt_text,
        reviewer_model=reviewer_model,
        timeout_secs=timeout_secs,
        conflict_keys=conflict_keys,
        capacity_keys=capacity_keys,
        worktree=worktree,
    )


# --------------------------------------------------------------------------
# PerJobTimeoutTest
# --------------------------------------------------------------------------


class PerJobTimeoutTest(unittest.TestCase):
    def test_timeout_marks_job_timed_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_fakepopen(returncode_value=-9, raise_timeout=True)
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            job = _make_job(timeout_secs=0.01)
            with patch.object(subprocess, "Popen", FakePopen), \
                 patch.object(rv.os, "killpg", _killpg_noop):
                results = sched.submit([job])
            self.assertTrue(results["j1"].timed_out)

    def test_no_timeout_when_natural_exit(self):
        # Negative: when wait() returns naturally, timed_out=False.
        with tempfile.TemporaryDirectory() as tmp:
            reset_fakepopen(returncode_value=0, raise_timeout=False)
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            job = _make_job(timeout_secs=10.0)
            with patch.object(subprocess, "Popen", FakePopen):
                results = sched.submit([job])
            self.assertFalse(results["j1"].timed_out)


# --------------------------------------------------------------------------
# ReturncodePropagationTest
# --------------------------------------------------------------------------


class ReturncodePropagationTest(unittest.TestCase):
    def test_returncode_propagates_per_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)

            # First run: rc=7
            reset_fakepopen(returncode_value=7)
            with patch.object(subprocess, "Popen", FakePopen):
                results = sched.submit([_make_job(job_id="a")])
            self.assertEqual(results["a"].returncode, 7)

            # Second run on a fresh scheduler: rc=0
            sched2 = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            reset_fakepopen(returncode_value=0)
            with patch.object(subprocess, "Popen", FakePopen):
                results2 = sched2.submit([_make_job(job_id="b")])
            self.assertEqual(results2["b"].returncode, 0)

    def test_one_job_failure_does_not_contaminate_sibling(self):
        # Negative: rc=7 on job-a must not bleed into job-b.
        with tempfile.TemporaryDirectory() as tmp:
            # Counter-based fake: alternate returncodes per call.
            calls = {"n": 0}

            class Alt(FakePopen):
                def wait(self, timeout=None):
                    calls["n"] += 1
                    self.returncode = 7 if calls["n"] == 1 else 0
                    return self.returncode

            reset_fakepopen()
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            with patch.object(subprocess, "Popen", Alt):
                results = sched.submit(
                    [_make_job(job_id="a"), _make_job(job_id="b")]
                )
            self.assertIn(results["a"].returncode, (7, 0))
            self.assertIn(results["b"].returncode, (7, 0))
            # Both must be different — sibling not contaminated.
            self.assertNotEqual(results["a"].returncode, results["b"].returncode)


# --------------------------------------------------------------------------
# DrainOnTimeoutTest
# --------------------------------------------------------------------------


class DrainOnTimeoutTest(unittest.TestCase):
    def test_drain_on_timeout_captures_partial_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_fakepopen(
                returncode_value=-15,
                stdout_payload=b"partial-output-before-kill",
                raise_timeout=True,
            )
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            with patch.object(subprocess, "Popen", FakePopen), \
                 patch.object(rv.os, "killpg", _killpg_noop):
                results = sched.submit([_make_job(timeout_secs=0.01)])
            self.assertTrue(results["j1"].timed_out)
            self.assertIn(b"partial-output-before-kill", results["j1"].stdout_bytes)

    def test_no_drain_on_natural_exit(self):
        # Negative: natural exit does not enter the timeout path.
        with tempfile.TemporaryDirectory() as tmp:
            reset_fakepopen(returncode_value=0, stdout_payload=b"clean-exit-out")
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            killpg_calls: list = []

            def _record_killpg(pid, sig):  # noqa: ARG001
                killpg_calls.append((pid, sig))

            with patch.object(subprocess, "Popen", FakePopen), \
                 patch.object(rv.os, "killpg", _record_killpg):
                results = sched.submit([_make_job()])
            self.assertFalse(results["j1"].timed_out)
            # killpg never called on natural exit.
            self.assertEqual(killpg_calls, [])


# --------------------------------------------------------------------------
# Immutability of finished jobs
# --------------------------------------------------------------------------


class ImmutabilityOfFinishedJobsDuplicateRecordTest(unittest.TestCase):
    def test_duplicate_record_raises_and_does_not_mutate(self):
        sched = rv.Scheduler()
        first = rv.JobResult(
            job_id="j1", returncode=0, stdout_bytes=b"first",
            stderr_bytes=b"", timed_out=False,
            parsed_verdict="APPROVE", parsed_issues=[], error=None,
        )
        sched._record_result("j1", first)
        second = rv.JobResult(
            job_id="j1", returncode=1, stdout_bytes=b"second",
            stderr_bytes=b"", timed_out=False,
            parsed_verdict="REQUEST_CHANGES", parsed_issues=[], error=None,
        )
        with self.assertRaises(rv.SchedulerInvariantError):
            sched._record_result("j1", second)
        # Stored value unchanged.
        self.assertEqual(sched._results["j1"].stdout_bytes, b"first")
        self.assertEqual(sched._results["j1"].returncode, 0)


class _PhasedStream:
    """File-like stream whose bytes are released in two phases.

    Phase 1 bytes are returned by the first `read()` call. Subsequent
    `read()` calls block (in chunks of `tick`) until the
    `late_event` is set; after the event fires, phase 2 bytes plus
    EOF (`b""`) are returned. `appended_phase2` records whether the
    reader thread actually consumed the phase-2 payload.
    """

    def __init__(self, phase1: bytes, phase2: bytes, late_event: threading.Event):
        self._phase1 = phase1
        self._phase2 = phase2
        self._late_event = late_event
        self._phase1_done = False
        self._phase2_done = False
        self._closed = False
        self.appended_phase2 = False

    def read(self, n: int = -1) -> bytes:
        if self._closed:
            return b""
        if not self._phase1_done:
            self._phase1_done = True
            return self._phase1
        if self._phase2_done:
            return b""
        # Wait — non-blockingly in small ticks — for the late event.
        # If the test cleans up early, return EOF.
        while not self._late_event.is_set():
            if self._closed:
                return b""
            time.sleep(0.01)
        self._phase2_done = True
        if self._phase2:
            self.appended_phase2 = True
            return self._phase2
        return b""

    def close(self) -> None:
        self._closed = True


class ImmutabilityOfFinishedJobsLateDrainTest(unittest.TestCase):
    """Drive `_run_one` end-to-end and prove that bytes arriving after
    `_record_result` are NOT merged into `JobResult.stdout_bytes`.

    Strategy: a phased stdout stream emits `b"finalized-bytes\\n"` in
    phase 1, then blocks in `read()` until a `threading.Event` fires.
    `Scheduler._record_result` is wrapped to set the event AFTER the
    snapshot has been recorded — so phase-2 bytes (`b"late-bytes\\n"`)
    can only reach the worker-frame deque, never `_results`.
    """

    def test_late_drain_bytes_do_not_enter_snapshot(self):
        late_event = threading.Event()
        # Stream lifetimes: keep refs so we can assert phase-2 was
        # actually consumed by the reader thread.
        captured_streams: dict = {}

        class PhasedPopen:
            pid_counter = 99000

            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.stdout = _PhasedStream(
                    b"finalized-bytes\n", b"late-bytes\n", late_event
                )
                self.stderr = _FakeStream(b"")
                captured_streams["stdout"] = self.stdout
                PhasedPopen.pid_counter += 1
                self.pid = PhasedPopen.pid_counter
                self.returncode = None

            def wait(self, timeout=None):
                # Return immediately — the child "exits" before the
                # late-drain event fires.
                self.returncode = 0
                return 0

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

        with tempfile.TemporaryDirectory() as tmp:
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            job = _make_job(
                job_id="late-job",
                session_id="late-sess",
                timeout_secs=5.0,
            )

            # Wrap _record_result to (a) confirm finalization happens
            # before late bytes are released, and (b) signal the
            # phased stream to release phase 2.
            original_record = sched._record_result
            record_called = threading.Event()

            def wrapped_record(job_id, result):
                original_record(job_id, result)
                record_called.set()
                # Now release the late writer; phase-2 bytes can flow
                # into the worker-frame deque only — the snapshot has
                # already been finalized inside the JobResult above.
                late_event.set()

            sched._record_result = wrapped_record  # type: ignore[assignment]

            with patch.object(subprocess, "Popen", PhasedPopen):
                results = sched.submit([job])

            # Give the reader thread a moment to consume phase-2 bytes
            # so we can assert the late writer DID emit (proving the
            # test isn't a no-op) but the snapshot still excluded them.
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if captured_streams.get("stdout") and captured_streams["stdout"].appended_phase2:
                    break
                time.sleep(0.02)

            self.assertTrue(record_called.is_set())
            # Snapshot must contain ONLY phase-1 bytes.
            self.assertEqual(
                results["late-job"].stdout_bytes, b"finalized-bytes\n"
            )
            self.assertNotIn(b"late-bytes", results["late-job"].stdout_bytes)
            # Late bytes WERE emitted by the stream (proves the test
            # actually exercises a post-finalization writer, not theater).
            self.assertTrue(
                captured_streams["stdout"].appended_phase2,
                "phased stream did not release phase-2 bytes; "
                "test would be a no-op",
            )


# --------------------------------------------------------------------------
# Default conflict / capacity helpers
# --------------------------------------------------------------------------


class DefaultConflictKeysAreCodebaseInvariantsOnlyTest(unittest.TestCase):
    def test_default_conflict_keys_codex_no_cli_rate(self):
        keys = rv.default_conflict_keys("codex", "sess", "j1")
        self.assertEqual(keys, frozenset({"prompt_file:sess:j1"}))
        # Hard guarantee: NO cli_rate:* in default conflict keys.
        for k in keys:
            self.assertFalse(k.startswith("cli_rate:"))

    def test_default_conflict_keys_claude_code_no_cli_rate(self):
        keys = rv.default_conflict_keys("claude_code", "sess", "j2")
        self.assertEqual(keys, frozenset({"prompt_file:sess:j2"}))
        for k in keys:
            self.assertFalse(k.startswith("cli_rate:"))

    def test_default_conflict_keys_with_worktree(self):
        keys = rv.default_conflict_keys("codex", "sess", "j1", worktree="/tmp/wt")
        self.assertEqual(
            keys,
            frozenset({"prompt_file:sess:j1", "worktree:/tmp/wt"}),
        )
        for k in keys:
            self.assertFalse(k.startswith("cli_rate:"))


class DefaultCapacityKeysAreRuntimeRateLimitsTest(unittest.TestCase):
    def test_capacity_keys_codex(self):
        self.assertEqual(
            rv.default_capacity_keys("codex"),
            frozenset({"cli_rate:claude"}),
        )

    def test_capacity_keys_claude_code(self):
        self.assertEqual(
            rv.default_capacity_keys("claude_code"),
            frozenset({"cli_rate:codex"}),
        )

    def test_capacity_keys_unknown_runtime_empty(self):
        self.assertEqual(rv.default_capacity_keys("other"), frozenset())


# --------------------------------------------------------------------------
# Parallel fan-out + capacity throttle + binary lock
# --------------------------------------------------------------------------


class _ConcurrencyTrackingPopen(FakePopen):
    """FakePopen that records peak in-flight count via a class lock."""

    in_flight = 0
    peak = 0
    _lock = threading.Lock()

    def __init__(self, argv, **kwargs):
        super().__init__(argv, **kwargs)
        with _ConcurrencyTrackingPopen._lock:
            _ConcurrencyTrackingPopen.in_flight += 1
            _ConcurrencyTrackingPopen.peak = max(
                _ConcurrencyTrackingPopen.peak,
                _ConcurrencyTrackingPopen.in_flight,
            )

    def wait(self, timeout=None):
        time.sleep(0.05)
        with _ConcurrencyTrackingPopen._lock:
            _ConcurrencyTrackingPopen.in_flight -= 1
        self.returncode = FakePopen.returncode_value
        return self.returncode


def reset_concurrency_tracker() -> None:
    _ConcurrencyTrackingPopen.in_flight = 0
    _ConcurrencyTrackingPopen.peak = 0
    reset_fakepopen()


def _peak_concurrency(intervals: list) -> int:
    """Sweep merged interval list (start, end) and return peak overlap."""
    events: list = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    # Sort by time; on ties, end events (-1) come before start events
    # (+1) so a [start, end] half-open contract holds.
    events.sort(key=lambda x: (x[0], x[1]))
    peak = 0
    cur = 0
    for _, delta in events:
        cur += delta
        if cur > peak:
            peak = cur
    return peak


class ParallelSameRuntimeFanOutTest(unittest.TestCase):
    """Default-capacity-path fan-out: 5 jobs, max_parallel=4,
    `capacity_limits=None`, each job carries
    `default_capacity_keys("codex") = {"cli_rate:claude"}`.

    With the unbounded counter path, peak in-flight must reach exactly
    4. Peak is computed by sweeping the merged interval list of
    `(started_at, ended_at)` per job, NOT a thread-shared counter
    (which is racy with the dispatcher).

    Negative case: with `capacity_limits={"cli_rate:claude": 1}` over
    the same jobs, peak concurrency must be exactly 1.
    """

    def _patch_run_one_with_intervals(self, sched, intervals_by_id, lock):
        """Replace `Scheduler._run_one` so each job records a real
        wall-clock (start, end) interval and sleeps long enough that
        the dispatcher has time to spin up the next worker.
        """

        def fake_run_one(job):
            start = time.monotonic()
            time.sleep(0.05)
            end = time.monotonic()
            with lock:
                intervals_by_id[job.job_id] = (start, end)
            result = rv.JobResult(
                job_id=job.job_id,
                returncode=0,
                stdout_bytes=b"",
                stderr_bytes=b"",
                timed_out=False,
                parsed_verdict=None,
                parsed_issues=None,
                error=None,
            )
            sched._record_result(job.job_id, result)
            return result

        sched._run_one = fake_run_one  # type: ignore[assignment]

    def test_default_capacity_path_peak_concurrency_equals_max_parallel(self):
        intervals: dict = {}
        lock = threading.Lock()
        with tempfile.TemporaryDirectory() as tmp:
            sched = rv.Scheduler(
                max_parallel=4,
                capacity_limits=None,  # explicit: unbounded counter path
                tmp_dir=tmp,
            )
            self._patch_run_one_with_intervals(sched, intervals, lock)
            jobs = [
                _make_job(
                    job_id=f"j{i}",
                    conflict_keys=rv.default_conflict_keys("codex", "sess", f"j{i}"),
                    capacity_keys=rv.default_capacity_keys("codex"),
                )
                for i in range(5)
            ]
            sched.submit(jobs)

        self.assertEqual(len(intervals), 5)
        peak = _peak_concurrency(list(intervals.values()))
        self.assertEqual(peak, 4, f"expected peak=4, got {peak}; intervals={intervals}")

    def test_capacity_limit_one_caps_peak_to_one(self):
        # Negative case: cap `cli_rate:claude` to 1; same 5 jobs must
        # serialize.
        intervals: dict = {}
        lock = threading.Lock()
        with tempfile.TemporaryDirectory() as tmp:
            sched = rv.Scheduler(
                max_parallel=4,
                capacity_limits={"cli_rate:claude": 1},
                tmp_dir=tmp,
            )
            self._patch_run_one_with_intervals(sched, intervals, lock)
            jobs = [
                _make_job(
                    job_id=f"j{i}",
                    conflict_keys=rv.default_conflict_keys("codex", "sess", f"j{i}"),
                    capacity_keys=rv.default_capacity_keys("codex"),
                )
                for i in range(5)
            ]
            sched.submit(jobs)

        self.assertEqual(len(intervals), 5)
        peak = _peak_concurrency(list(intervals.values()))
        self.assertEqual(peak, 1, f"expected peak=1, got {peak}; intervals={intervals}")


class CapacityLimitThrottleTest(unittest.TestCase):
    def test_capacity_limit_caps_concurrency_to_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_concurrency_tracker()
            sched = rv.Scheduler(
                max_parallel=8,
                capacity_limits={"cli_rate:claude": 2},
                tmp_dir=tmp,
            )
            jobs = [
                _make_job(
                    job_id=f"j{i}",
                    conflict_keys=rv.default_conflict_keys("codex", "sess", f"j{i}"),
                    capacity_keys=rv.default_capacity_keys("codex"),
                )
                for i in range(5)
            ]
            with patch.object(subprocess, "Popen", _ConcurrencyTrackingPopen):
                results = sched.submit(jobs)
            self.assertEqual(len(results), 5)
            self.assertLessEqual(_ConcurrencyTrackingPopen.peak, 2)


class BinaryLockConflictTest(unittest.TestCase):
    def test_shared_worktree_serializes_jobs(self):
        # Two jobs, both holding `worktree:/x` — B must not start
        # until A finishes.
        with tempfile.TemporaryDirectory() as tmp:
            reset_concurrency_tracker()
            sched = rv.Scheduler(max_parallel=4, tmp_dir=tmp)
            jobs = [
                _make_job(
                    job_id=f"j{i}",
                    conflict_keys=rv.default_conflict_keys(
                        "codex", "sess", f"j{i}", worktree="/x"
                    ),
                )
                for i in range(2)
            ]
            with patch.object(subprocess, "Popen", _ConcurrencyTrackingPopen):
                results = sched.submit(jobs)
            self.assertEqual(len(results), 2)
            # Peak in-flight must be exactly 1 — they cannot overlap.
            self.assertEqual(_ConcurrencyTrackingPopen.peak, 1)


# --------------------------------------------------------------------------
# BuildArgvTest
# --------------------------------------------------------------------------


class BuildArgvTest(unittest.TestCase):
    def test_codex_runtime_uses_gpt_5_5(self):
        with tempfile.TemporaryDirectory() as tmp:
            sched = rv.Scheduler(tmp_dir=tmp)
            job = _make_job(runtime="codex", reviewer_model="gpt-5.5")
            argv = rv._build_argv(job, sched)
        self.assertEqual(argv[0], "claude")
        self.assertIn("-p", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--include-partial-messages", argv)
        self.assertIn("--model", argv)
        self.assertIn("gpt-5.5", argv)

    def test_codex_backstop_when_reviewer_model_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            sched = rv.Scheduler(tmp_dir=tmp)
            job = _make_job(runtime="codex", reviewer_model="")
            argv = rv._build_argv(job, sched)
        self.assertIn("claude-sonnet-4-6", argv)

    def test_claude_code_runtime_uses_codex_exec(self):
        with tempfile.TemporaryDirectory() as tmp:
            sched = rv.Scheduler(tmp_dir=tmp)
            job = _make_job(runtime="claude_code")
            argv = rv._build_argv(job, sched)
        self.assertEqual(argv[0], "codex")
        self.assertEqual(argv[1], "exec")
        self.assertIn("--full-auto", argv)
        self.assertIn("-o", argv)

    def test_unknown_runtime_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            sched = rv.Scheduler(tmp_dir=tmp)
            job = _make_job(runtime="bogus")
            with self.assertRaises(ValueError):
                rv._build_argv(job, sched)

    def test_resolve_reviewer_model_precedence(self):
        # job.reviewer_model > judgment_model > default backstop.
        job_explicit = _make_job(reviewer_model="custom-x")
        self.assertEqual(
            rv._resolve_reviewer_model(job_explicit, judgment_model="judge-y"),
            "custom-x",
        )
        job_no_explicit = _make_job(reviewer_model="")
        self.assertEqual(
            rv._resolve_reviewer_model(job_no_explicit, judgment_model="judge-y"),
            "judge-y",
        )
        self.assertEqual(
            rv._resolve_reviewer_model(job_no_explicit, judgment_model=None),
            "claude-sonnet-4-6",
        )


# --------------------------------------------------------------------------
# StdinDeliveryTest (NEW per C4)
# --------------------------------------------------------------------------


class StdinDeliveryTest(unittest.TestCase):
    def _run_and_capture(self, runtime: str, prompt: str, tmp: str) -> dict:
        reset_fakepopen()
        sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
        job = _make_job(
            runtime=runtime,
            prompt_text=prompt,
            session_id="sess1",
            job_id="jX",
        )
        if runtime == "claude_code":
            # Pre-create the output file so the parser does not record
            # a missing-file error.
            out_path = os.path.join(
                tmp, f"{job.session_id}-reviewer-output.{job.job_id}.txt"
            )
            with open(out_path, "wb") as fh:
                fh.write(b"")
        with patch.object(subprocess, "Popen", FakePopen):
            sched.submit([job])
        self.assertEqual(len(FakePopen.record_kwargs), 1)
        return FakePopen.record_kwargs[0]

    def test_codex_stdin_is_file_fd_not_pipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            kwargs = self._run_and_capture("codex", "hello-codex", tmp)
            stdin = kwargs.get("stdin")
            # (a) file-like with .read and .name
            self.assertTrue(hasattr(stdin, "read"))
            self.assertTrue(hasattr(stdin, "name"))
            # (b) basename matches per-job temp path
            self.assertEqual(
                os.path.basename(stdin.name),
                "sess1-reviewer-prompt.jX.txt",
            )
            # (e) NOT subprocess.PIPE
            self.assertIsNot(stdin, subprocess.PIPE)
            # (d) after worker exits, prompt file is gone.
            self.assertFalse(os.path.exists(stdin.name))

    def test_claude_code_stdin_is_file_fd_not_pipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            kwargs = self._run_and_capture("claude_code", "hello-cc", tmp)
            stdin = kwargs.get("stdin")
            self.assertTrue(hasattr(stdin, "read"))
            self.assertTrue(hasattr(stdin, "name"))
            self.assertEqual(
                os.path.basename(stdin.name),
                "sess1-reviewer-prompt.jX.txt",
            )
            self.assertIsNot(stdin, subprocess.PIPE)

    def test_prompt_content_written_before_spawn(self):
        # (c) before _run_one's `finally:` cleanup, the prompt path
        # contains exactly `prompt_text.encode("utf-8")`. Patch Popen
        # to read the file at construction time.
        captured: dict = {}

        class ReadingPopen(FakePopen):
            def __init__(self, argv, **kwargs):
                fp = kwargs.get("stdin")
                # Read directly from fp.name so we observe content
                # while the FD is still open.
                with open(fp.name, "rb") as ofh:
                    captured["bytes"] = ofh.read()
                super().__init__(argv, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            reset_fakepopen()
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            job = _make_job(prompt_text="exact-payload-utf8")
            with patch.object(subprocess, "Popen", ReadingPopen):
                sched.submit([job])
            self.assertEqual(captured["bytes"], b"exact-payload-utf8")

    def test_start_new_session_true_on_codex(self):
        # Confirms POSIX process-group isolation for clean killpg.
        with tempfile.TemporaryDirectory() as tmp:
            kwargs = self._run_and_capture("codex", "x", tmp)
            self.assertTrue(kwargs.get("start_new_session"))


# --------------------------------------------------------------------------
# StreamJsonResultExtractionTest
# --------------------------------------------------------------------------


def _wrap_result(result_text: str) -> bytes:
    """Wrap a reviewer result body inside a stream-json result event."""
    events = [
        {"type": "thinking_delta", "delta": "..."},
        {"type": "result", "result": result_text},
    ]
    return ("\n".join(json.dumps(e) for e in events)).encode()


class StreamJsonResultExtractionTest(unittest.TestCase):
    def test_heartbeat_tolerated_and_result_extracted(self):
        body = (
            "### VERDICT: REQUEST_CHANGES\n"
            "\n"
            "### Issues\n"
            "- [CRITICAL] something is broken — File: `a.py`, around line 1\n"
            "\n"
            "### Strengths\n"
            "- clean code\n"
        )
        verdict, issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertEqual(verdict, "REQUEST_CHANGES")
        # Issue line returned with leading bullet stripped.
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].startswith("[CRITICAL]"))
        self.assertIsNone(error)

    def test_no_result_event_returns_error(self):
        events = [{"type": "thinking_delta", "delta": "..."}]
        payload = "\n".join(json.dumps(e) for e in events).encode()
        verdict, issues, error = rv._parse_stream_json_result(payload)
        self.assertIsNone(verdict)
        self.assertIsNone(issues)
        self.assertEqual(error, "stream_json_no_result_event")

    def test_empty_stream_returns_no_events_error(self):
        verdict, issues, error = rv._parse_stream_json_result(b"")
        self.assertIsNone(verdict)
        self.assertIsNone(issues)
        self.assertEqual(error, "stream_json_no_events")

    def test_non_string_result_payload_flags_schema_violation(self):
        events = [{"type": "result", "result": {"oops": "not a string"}}]
        payload = "\n".join(json.dumps(e) for e in events).encode()
        verdict, issues, error = rv._parse_stream_json_result(payload)
        self.assertIsNone(verdict)
        self.assertEqual(error, "stream_json_result_not_string")


# --------------------------------------------------------------------------
# SchemaValidationTest — enforces docs/protocol/reviewer-output.md
# --------------------------------------------------------------------------


class SchemaValidationTest(unittest.TestCase):
    """Direct coverage of `_parse_stream_json_result` schema rules.

    Each case constructs a synthetic stream-json blob with one
    `type: result` event and asserts the expected
    `(verdict, issues, error)` triple.
    """

    # ---- Happy paths -----------------------------------------------------

    def test_approve_with_none_marker_is_valid(self):
        body = (
            "### VERDICT: APPROVE\n"
            "\n"
            "### Issues\n"
            "- None.\n"
            "\n"
            "### Strengths\n"
            "- ok\n"
        )
        verdict, issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertEqual(verdict, "APPROVE")
        self.assertEqual(issues, [])
        self.assertIsNone(error)

    def test_approve_without_issues_section_is_valid(self):
        body = (
            "### VERDICT: APPROVE\n"
            "\n"
            "### Strengths\n"
            "- looks good\n"
        )
        verdict, issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertEqual(verdict, "APPROVE")
        self.assertEqual(issues, [])
        self.assertIsNone(error)

    def test_request_changes_with_bullet_prefixed_critical_is_valid(self):
        body = (
            "### VERDICT: REQUEST_CHANGES\n"
            "\n"
            "### Issues\n"
            "- [CRITICAL] missing input validation\n"
            "  File: `a.py`, around line 10\n"
            "- [MINOR] extra blank line\n"
            "\n"
            "### Strengths\n"
            "- ok\n"
        )
        verdict, issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertEqual(verdict, "REQUEST_CHANGES")
        self.assertEqual(len(issues), 2)
        self.assertTrue(issues[0].startswith("[CRITICAL]"))
        self.assertTrue(issues[1].startswith("[MINOR]"))
        self.assertIsNone(error)

    def test_request_changes_without_bullet_prefix_is_valid(self):
        # Some reviewers omit the leading `- ` bullet. Schema allows
        # both forms in the parser per EX-C3 (d).
        body = (
            "### VERDICT: REQUEST_CHANGES\n"
            "\n"
            "### Issues\n"
            "[CRITICAL] direct severity tag without bullet\n"
            "\n"
            "### Strengths\n"
            "- ok\n"
        )
        verdict, issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertEqual(verdict, "REQUEST_CHANGES")
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].startswith("[CRITICAL]"))
        self.assertIsNone(error)

    # ---- Schema violations ----------------------------------------------

    def test_invalid_verdict_token_flags_schema_violation(self):
        body = (
            "### VERDICT: APPROVED\n"  # NOT in the canonical set
            "\n"
            "### Strengths\n"
            "- ok\n"
        )
        verdict, issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertIsNone(verdict)
        self.assertIsNone(issues)
        self.assertEqual(error, "schema_violation:invalid_verdict")

    def test_missing_strengths_flags_schema_violation(self):
        body = (
            "### VERDICT: APPROVE\n"
            "\n"
            "### Issues\n"
            "- None.\n"
        )
        verdict, issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertIsNone(verdict)
        self.assertIsNone(issues)
        self.assertEqual(error, "schema_violation:missing_strengths")

    def test_approve_with_critical_flags_inconsistency(self):
        body = (
            "### VERDICT: APPROVE\n"
            "\n"
            "### Issues\n"
            "- [CRITICAL] this should not be here\n"
            "\n"
            "### Strengths\n"
            "- ok\n"
        )
        verdict, issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertEqual(verdict, "APPROVE")
        self.assertEqual(error, "schema_violation:approve_with_critical")

    def test_request_changes_without_issues_section_flags_violation(self):
        body = (
            "### VERDICT: REQUEST_CHANGES\n"
            "\n"
            "### Strengths\n"
            "- ok\n"
        )
        verdict, _issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertEqual(verdict, "REQUEST_CHANGES")
        self.assertEqual(error, "schema_violation:request_changes_without_issues")

    def test_request_changes_with_only_none_marker_flags_violation(self):
        body = (
            "### VERDICT: REQUEST_CHANGES\n"
            "\n"
            "### Issues\n"
            "- None.\n"
            "\n"
            "### Strengths\n"
            "- ok\n"
        )
        verdict, _issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertEqual(verdict, "REQUEST_CHANGES")
        self.assertEqual(error, "schema_violation:request_changes_without_issues")

    def test_request_changes_with_only_minor_flags_violation(self):
        body = (
            "### VERDICT: REQUEST_CHANGES\n"
            "\n"
            "### Issues\n"
            "- [MINOR] tiny nit\n"
            "\n"
            "### Strengths\n"
            "- ok\n"
        )
        verdict, _issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertEqual(verdict, "REQUEST_CHANGES")
        self.assertEqual(error, "schema_violation:request_changes_minor_only")

    def test_issues_prose_placeholder_flags_violation(self):
        body = (
            "### VERDICT: APPROVE\n"
            "\n"
            "### Issues\n"
            "no issues found\n"
            "\n"
            "### Strengths\n"
            "- ok\n"
        )
        verdict, _issues, error = rv._parse_stream_json_result(_wrap_result(body))
        self.assertEqual(verdict, "APPROVE")
        self.assertEqual(error, "schema_violation:issues_prose_placeholder")

    def test_unknown_severity_rejected(self):
        # Per `docs/protocol/reviewer-output.md` §Issues section the
        # allowed severities are EXACTLY `[CRITICAL]` and `[MINOR]`.
        # Any other bracketed all-caps token must be rejected as
        # `schema_violation:invalid_severity`. Parametrized over three
        # representative unsupported severities.
        for severity in ("MAJOR", "HIGH", "WARNING"):
            with self.subTest(severity=severity):
                body = (
                    "### VERDICT: REQUEST_CHANGES\n"
                    "\n"
                    "### Issues\n"
                    f"- [{severity}] unsupported severity\n"
                    "\n"
                    "### Strengths\n"
                    "- ok\n"
                )
                verdict, issues, error = rv._parse_stream_json_result(
                    _wrap_result(body)
                )
                self.assertIsNone(
                    verdict,
                    f"verdict should be None for [{severity}], got {verdict!r}",
                )
                self.assertIsNone(
                    issues,
                    f"issues should be None for [{severity}], got {issues!r}",
                )
                self.assertEqual(
                    error,
                    "schema_violation:invalid_severity",
                    f"unexpected error for [{severity}]: {error!r}",
                )

    def test_issues_section_with_only_header_and_blank_body_rejected(self):
        # `### Issues` header followed only by a blank line (no
        # `- None.`, no `- [SEVERITY]` bullets) violates the schema.
        # Direct coverage of the `schema_violation:issues_empty_body`
        # discriminator (EX-M1).
        body = (
            "### VERDICT: REQUEST_CHANGES\n"
            "\n"
            "### Issues\n"
            "\n"
            "### Strengths\n"
            "- ok\n"
        )
        verdict, issues, error = rv._parse_stream_json_result(_wrap_result(body))
        # Implementation returns the parsed verdict alongside the
        # `issues_empty_body` discriminator (issues=None signals the
        # caller that the body was unusable).
        self.assertEqual(verdict, "REQUEST_CHANGES")
        self.assertIsNone(issues)
        self.assertEqual(error, "schema_violation:issues_empty_body")


# --------------------------------------------------------------------------
# TempPromptCleanupTest
# --------------------------------------------------------------------------


class TempPromptCleanupTest(unittest.TestCase):
    def _list_prompt_files(self, tmp: str):
        return [n for n in os.listdir(tmp) if "reviewer-prompt" in n]

    def test_cleanup_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_fakepopen(returncode_value=0)
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            with patch.object(subprocess, "Popen", FakePopen):
                sched.submit([_make_job()])
            self.assertEqual(self._list_prompt_files(tmp), [])

    def test_cleanup_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_fakepopen(returncode_value=-9, raise_timeout=True)
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            with patch.object(subprocess, "Popen", FakePopen), \
                 patch.object(rv.os, "killpg", _killpg_noop):
                sched.submit([_make_job(timeout_secs=0.01)])
            self.assertEqual(self._list_prompt_files(tmp), [])

    def test_cleanup_silent_on_filenotfounderror(self):
        # If the prompt file vanishes mid-run (e.g. external sweep),
        # `os.unlink` in `finally:` must swallow `FileNotFoundError`.
        with tempfile.TemporaryDirectory() as tmp:
            reset_fakepopen(returncode_value=0)

            class DeleteEarlyPopen(FakePopen):
                def __init__(self, argv, **kwargs):
                    super().__init__(argv, **kwargs)
                    # Delete the prompt file before _run_one's finally:
                    fp = kwargs.get("stdin")
                    if fp is not None and hasattr(fp, "name") and os.path.exists(fp.name):
                        os.unlink(fp.name)

            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            with patch.object(subprocess, "Popen", DeleteEarlyPopen):
                # Should NOT raise.
                sched.submit([_make_job()])
            self.assertEqual(self._list_prompt_files(tmp), [])


# --------------------------------------------------------------------------
# CliShapeTest
# --------------------------------------------------------------------------


class CliShapeTest(unittest.TestCase):
    def test_capacity_flag_parses_key_value(self):
        self.assertEqual(
            rv._parse_capacity(["cli_rate:claude=2", "cli_rate:codex=4"]),
            {"cli_rate:claude": 2, "cli_rate:codex": 4},
        )

    def test_capacity_flag_rejects_malformed(self):
        with self.assertRaises(ValueError):
            rv._parse_capacity(["bogus-no-equals"])

    def test_main_returns_2_on_missing_jobs_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_file = os.path.join(tmp, "does-not-exist.json")
            rc = rv.main(["--jobs", jobs_file])
            self.assertEqual(rc, 2)

    def test_main_argv_invalid_returns_2(self):
        rc = rv.main(["--jobs"])  # missing arg value
        self.assertEqual(rc, 2)

    def test_main_runs_jobs_with_tmp_dir_and_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_file = os.path.join(tmp, "jobs.json")
            output_file = os.path.join(tmp, "out.json")
            jobs_payload = [
                {
                    "job_id": "ja",
                    "session_id": "sX",
                    "runtime": "codex",
                    "prompt_text": "p",
                    "reviewer_model": "gpt-5.5",
                    "timeout_secs": 5.0,
                },
            ]
            with open(jobs_file, "w", encoding="utf-8") as fh:
                json.dump(jobs_payload, fh)

            reset_fakepopen(returncode_value=0)
            with patch.object(subprocess, "Popen", FakePopen):
                rc = rv.main([
                    "--jobs", jobs_file,
                    "--max-parallel", "2",
                    "--default-timeout", "10",
                    "--capacity", "cli_rate:claude=2",
                    "--tmp-dir", tmp,
                    "--output", output_file,
                ])
            self.assertEqual(rc, 0)
            with open(output_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("ja", data)
            self.assertEqual(data["ja"]["returncode"], 0)

    def test_main_fail_on_any_returns_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs_file = os.path.join(tmp, "jobs.json")
            with open(jobs_file, "w", encoding="utf-8") as fh:
                json.dump(
                    [
                        {
                            "job_id": "jbad",
                            "session_id": "sX",
                            "runtime": "codex",
                            "prompt_text": "p",
                            "reviewer_model": "",
                            "timeout_secs": 5.0,
                        }
                    ],
                    fh,
                )
            reset_fakepopen(returncode_value=42)
            with patch.object(subprocess, "Popen", FakePopen):
                rc = rv.main(
                    [
                        "--jobs", jobs_file,
                        "--tmp-dir", tmp,
                        "--fail-on-any",
                    ]
                )
            self.assertEqual(rc, 1)


# --------------------------------------------------------------------------
# ClaudeCodeOutputFileTest — exercise output_file_missing /
# output_file_unreadable parse-error discriminators
# --------------------------------------------------------------------------


class ClaudeCodeOutputFileTest(unittest.TestCase):
    """For `runtime="claude_code"`, the parser reads a `-o`-supplied
    output file. If the file is missing, it records
    `output_file_missing`. If reading raises `OSError`, it records
    `output_file_unreadable: <repr>`. Neither path was previously
    asserted.
    """

    def test_runtime_claude_code_missing_output_file_records_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_fakepopen(returncode_value=0)
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            job = _make_job(
                runtime="claude_code",
                session_id="sess-cc",
                job_id="jcc",
            )
            # Do NOT pre-create the output file.
            with patch.object(subprocess, "Popen", FakePopen):
                results = sched.submit([job])
            self.assertEqual(results["jcc"].error, "output_file_missing")

    def test_runtime_claude_code_unreadable_output_file_records_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            reset_fakepopen(returncode_value=0)
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            job = _make_job(
                runtime="claude_code",
                session_id="sess-cc2",
                job_id="jcc2",
            )
            # Pre-create the output file so FileNotFoundError is not
            # the discriminator we hit.
            out_path = os.path.join(
                tmp, f"{job.session_id}-reviewer-output.{job.job_id}.txt"
            )
            with open(out_path, "wb") as fh:
                fh.write(b"")

            real_open = open

            def fake_open(path, *args, **kwargs):
                # Force PermissionError on the output-file read while
                # leaving every other open() (prompt write, prompt
                # read-FD) untouched.
                if isinstance(path, str) and path == out_path:
                    raise PermissionError(13, "Permission denied", path)
                return real_open(path, *args, **kwargs)

            import builtins
            with patch.object(subprocess, "Popen", FakePopen), \
                 patch.object(builtins, "open", fake_open):
                results = sched.submit([job])
            err = results["jcc2"].error
            self.assertIsNotNone(err)
            self.assertTrue(
                err.startswith("output_file_unreadable:"),
                f"expected output_file_unreadable:* prefix, got {err!r}",
            )


# --------------------------------------------------------------------------
# WorkerExceptionPropagationTest — exercise the synthesized
# worker_exception arm in Scheduler.submit when ex.submit / Popen fails
# --------------------------------------------------------------------------


class WorkerExceptionPropagationTest(unittest.TestCase):
    """Covers the dispatcher's worker_exception arm.

    Scenario A: `subprocess.Popen.__init__` raises FileNotFoundError
    (e.g. `claude` binary not on PATH). The exception propagates up
    through `_run_one` to `fut.result()` in the dispatcher, which
    synthesizes a `worker_exception:*` JobResult. The temp prompt
    file MUST be cleaned up by `_run_one`'s `finally:` block.

    Scenario B: two jobs share a `worktree:/x` binary lock. The first
    job's Popen raises; the second job MUST still run to completion,
    proving the binary lock was released after the failure.
    """

    def test_popen_failure_yields_worker_exception_jobresult(self):
        with tempfile.TemporaryDirectory() as tmp:
            sched = rv.Scheduler(max_parallel=1, tmp_dir=tmp)
            job = _make_job(job_id="jpf", session_id="sess-pf")

            class FailingPopen:
                def __init__(self, argv, **kwargs):
                    raise FileNotFoundError(2, "claude not found", "claude")

            with patch.object(subprocess, "Popen", FailingPopen):
                results = sched.submit([job])

            r = results["jpf"]
            self.assertIsNotNone(r.error)
            self.assertTrue(
                r.error.startswith("worker_exception:"),
                f"expected worker_exception:* prefix, got {r.error!r}",
            )
            # finally: cleanup ran — temp prompt file should be gone.
            prompt_path = os.path.join(
                tmp, f"{job.session_id}-reviewer-prompt.{job.job_id}.txt"
            )
            self.assertFalse(
                os.path.exists(prompt_path),
                f"prompt file leaked: {prompt_path}",
            )

    def test_popen_failure_releases_binary_lock(self):
        # Two jobs sharing `worktree:/x`. Job-A's Popen raises;
        # Job-B must still run to natural completion.
        with tempfile.TemporaryDirectory() as tmp:
            sched = rv.Scheduler(max_parallel=2, tmp_dir=tmp)
            job_a = _make_job(
                job_id="ja",
                session_id="sess-rl",
                conflict_keys=rv.default_conflict_keys(
                    "codex", "sess-rl", "ja", worktree="/x"
                ),
            )
            job_b = _make_job(
                job_id="jb",
                session_id="sess-rl",
                conflict_keys=rv.default_conflict_keys(
                    "codex", "sess-rl", "jb", worktree="/x"
                ),
            )

            call_count = {"n": 0}

            class FirstFailsThenSucceeds(FakePopen):
                def __init__(self, argv, **kwargs):
                    call_count["n"] += 1
                    if call_count["n"] == 1:
                        raise FileNotFoundError(2, "boom", "claude")
                    super().__init__(argv, **kwargs)

            reset_fakepopen(returncode_value=0)
            with patch.object(subprocess, "Popen", FirstFailsThenSucceeds):
                results = sched.submit([job_a, job_b])

            # Job-A: worker_exception synthesized.
            self.assertIsNotNone(results["ja"].error)
            self.assertTrue(results["ja"].error.startswith("worker_exception:"))
            # Job-B: ran to completion (binary lock was released).
            # The empty FakePopen stdout makes the parser record
            # `stream_json_no_events`, which is fine — what matters is
            # that the returncode propagated (proves Popen was actually
            # called, not synthesized) and the error is NOT a
            # worker_exception (proves the lock was released and the
            # job ran on a real future).
            self.assertEqual(results["jb"].returncode, 0)
            jb_err = results["jb"].error or ""
            self.assertFalse(
                jb_err.startswith("worker_exception:"),
                f"job-b should not be a worker_exception, got {jb_err!r}",
            )


if __name__ == "__main__":
    unittest.main()
