#!/usr/bin/env python3
"""Conflict-aware parallel CR (code-review) scheduler for review-loop.

Fans out independent reviewer rounds through the bounded, isolated reviewer
wrappers. Native streams and usage evidence stay in per-invocation files;
only capped wrapper status output is retained in scheduler results.

POSIX-only: relies on `start_new_session=True` + `os.killpg` for clean
process-group teardown on per-job timeout. macOS / Linux only.

Only successful wrapper artifacts enter the shared reviewer-output schema
validator. The orchestrator remains responsible for the full review rubric.
Runtime labels retain their established orchestrator meaning: `codex` selects
the Claude CLI wrapper and `claude_code` selects the Codex CLI wrapper. In-process
Agent-tool dispatch is outside this CLI scheduler.

Stdlib-only.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time

try:
    from process_tree import terminate_process_tree, wait_with_process_tree
except ModuleNotFoundError:  # imported as scripts.review_verification
    from scripts.process_tree import terminate_process_tree, wait_with_process_tree
from dataclasses import dataclass, field


# Pattern for any bracketed all-caps severity-shaped token at the start of a
# bullet body, e.g. `[CRITICAL]`, `[MINOR]`, `[MAJOR]`, `[HIGH]`, `[WARNING]`.
# Used to detect unsupported severities that the schema rejects.
_BRACKETED_SEVERITY_RE = re.compile(r"^\[([A-Z]+)\]")
WRAPPER_CLEANUP_GRACE_SECONDS = 10.0
STATUS_CAPTURE_BYTES = 65536


class SchedulerInvariantError(Exception):
    """Raised when an invariant of the Scheduler is violated.

    The most important case is duplicate finalization of a job —
    once a job has a `JobResult`, the scheduler refuses to overwrite
    it. See `Scheduler._record_result`.
    """


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewJob:
    """One reviewer-round invocation.

    Each job is self-describing: it carries its own `session_id` so the
    scheduler can compute the per-job temp prompt path without the
    caller threading it through `Scheduler` state. `runtime` selects
    which CLI shell-out path (`"codex"` for Codex Stage 1's `claude -p`
    fan-out, `"claude_code"` for the Claude Code orchestrator's
    `codex exec` fan-out — note the labels reflect which orchestrator
    is invoking the scheduler, not which CLI is spawned).

    `conflict_keys` are *binary* locks (codebase invariants only).
    `capacity_keys` are *capacity counters* checked against the
    scheduler's `capacity_limits` map (external rate throttles, opt-in).
    """

    job_id: str
    session_id: str
    runtime: str
    prompt_text: str
    reviewer_model: str
    timeout_secs: float
    conflict_keys: frozenset = field(default_factory=frozenset)
    capacity_keys: frozenset = field(default_factory=frozenset)
    extra_argv: tuple = ()
    worktree: "str | None" = None
    stage: str = "review"
    role: str = "reviewer"


@dataclass
class JobResult:
    """Final outcome for one `ReviewJob`.

    `Scheduler._record_result` finalizes one of these per job exactly
    once. Late drain bytes (e.g. partial stdout that arrives after the
    snapshot was taken) are NEVER merged back in — see
    `ImmutabilityOfFinishedJobsLateDrainTest`.
    """

    job_id: str
    returncode: "int | None"
    stdout_bytes: bytes
    stderr_bytes: bytes
    timed_out: bool
    parsed_verdict: "str | None"
    parsed_issues: "list | None"
    error: "str | None"
    status: "str | None" = None
    result_file: "str | None" = None
    usage_file: "str | None" = None
    invocation_id: "str | None" = None
    tool_uses: "int | None" = None


# --------------------------------------------------------------------------
# Default conflict / capacity helpers
# --------------------------------------------------------------------------


def default_conflict_keys(
    runtime: str,
    session_id: str,
    job_id: str,
    worktree: "str | None" = None,
) -> frozenset:
    """Default *binary* conflict keys for a job.

    Codebase invariants only — NO `cli_rate:*` here. The per-job
    prompt-file slot is always claimed (defends against caller
    misconfiguration that would alias two jobs onto the same temp
    file). `worktree:{path}` is opt-in for future writer jobs that
    must not share a working tree.
    """
    keys = {f"prompt_file:{session_id}:{job_id}"}
    if worktree is not None:
        keys.add(f"worktree:{worktree}")
    return frozenset(keys)


def default_capacity_keys(runtime: str) -> frozenset:
    """Default *capacity-counter* keys for a job.

    Per-runtime CLI rate limit. Unbounded by default — only counted
    when the scheduler's `capacity_limits` dict supplies a max for the
    key. For `runtime="codex"` (Codex Stage 1 fans out `claude -p`),
    the rate-limited backend is the Claude API. For
    `runtime="claude_code"` (Claude Code orchestrator fans out
    `codex exec`), the rate-limited backend is the Codex CLI.
    """
    if runtime == "codex":
        return frozenset({"cli_rate:claude"})
    if runtime == "claude_code":
        return frozenset({"cli_rate:codex"})
    return frozenset()


# --------------------------------------------------------------------------
# argv builder + reviewer-model resolution
# --------------------------------------------------------------------------


def _resolve_reviewer_model(
    job: ReviewJob,
    judgment_model: "str | None" = None,
) -> str:
    """Resolve the per-job reviewer model.

    Precedence per planning.md §Shared model-tier contract:
        job.reviewer_model > judgment_model > "claude-opus-5-5"
    """
    if job.reviewer_model:
        return job.reviewer_model
    if judgment_model:
        return judgment_model
    return "claude-opus-5-5"


def _job_session_id(job: ReviewJob) -> str:
    for value in (job.session_id, job.job_id):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
            raise ValueError("session_id and job_id must be safe filename components of at most 80 characters")
    return f"{job.session_id}.{job.job_id}"


def _build_argv(job: ReviewJob, scheduler: "Scheduler", *, tmp_dir=None) -> list:
    """Select a permission-constrained wrapper; arbitrary native flags are forbidden."""
    if job.extra_argv:
        raise ValueError("extra_argv is unsupported; use explicit reviewer configuration fields")
    wrappers = {"codex": "run_claude_reviewer.py", "claude_code": "run_codex_reviewer.py"}
    if job.runtime not in wrappers:
        raise ValueError(f"unknown runtime: {job.runtime!r}")
    timeout = job.timeout_secs or scheduler.default_timeout
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    argv = [sys.executable, str(Path(__file__).resolve().with_name(wrappers[job.runtime])),
            "--session-id", _job_session_id(job), "--parent-session-id", job.session_id,
            "--tmp-dir", tmp_dir or scheduler.tmp_dir,
            "--timeout-seconds", str(timeout), "--stage", job.stage, "--role", job.role,
            "--ledger-dir", os.path.join(scheduler.tmp_dir, "usage")]
    model = _resolve_reviewer_model(job) if job.runtime == "codex" else job.reviewer_model
    if model:
        argv.extend(["--model", model])
    return argv


def _wrapper_summary(stdout_bytes):
    for line in reversed(stdout_bytes.splitlines()):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(event, dict) and isinstance(event.get("status"), str):
            return event
    return None


def _descendants(pid):
    """Compatibility diagnostic used by older scheduler tests and callers."""
    try:
        listing = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid="], timeout=1,
            stderr=subprocess.DEVNULL,
        )
        pairs = [tuple(map(int, line.split())) for line in listing.splitlines()
                 if len(line.split()) == 2]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return set()
    found, frontier = set(), {pid}
    while frontier:
        frontier = {child for child, parent in pairs
                    if parent in frontier and child not in found}
        found.update(frontier)
    return found


def _stop_wrapper(process, known_pids=()):
    """Allow cancellation evidence, then clean nested sessions fail-closed."""
    returncode, cleanup_ok, _observed = terminate_process_tree(
        process, known_pids=known_pids,
    )
    return returncode if cleanup_ok else None


# --------------------------------------------------------------------------
# Stream-json best-effort parser
# --------------------------------------------------------------------------


_VALID_VERDICTS = ("APPROVE", "REQUEST_CHANGES")
_PROSE_PLACEHOLDERS = ("no issues found", "n/a", "none found", "no issue")


def _parse_stream_json_result(stdout_bytes: bytes) -> tuple:
    """Parse a `claude -p --output-format stream-json` stream and validate
    its result body against the shared reviewer-output.md schema.

    Walks line-delimited JSON events; tolerates heartbeat events
    (`thinking_delta`, rate-limit events, etc.) and extracts the
    `result` field of the event whose `type == "result"`. The result
    string is then validated against the rules in
    `docs/protocol/reviewer-output.md`:

    - Verdict must be EXACTLY `APPROVE` or `REQUEST_CHANGES`.
    - `### Strengths` section is required.
    - Issue lines may carry an optional leading `- ` bullet prefix and
      must use severity `[CRITICAL]` or `[MINOR]`. Any other bracketed
      severity token (`[MAJOR]`, `[HIGH]`, `[WARNING]`, `[INFO]`,
      `[NITPICK]`, …) is rejected as
      `schema_violation:invalid_severity`.
    - Verdict / issues consistency:
        * `APPROVE` + any `[CRITICAL]` → schema_violation:approve_with_critical
        * `REQUEST_CHANGES` with no `### Issues` or only `- None.` →
          schema_violation:request_changes_without_issues
        * `REQUEST_CHANGES` with only `[MINOR]` →
          schema_violation:request_changes_minor_only
        * `### Issues` present but empty (no `- None.`, no entries) →
          schema_violation:issues_empty_body
        * Prose placeholder ("no issues found", "N/A", ...) inside
          `### Issues` body → schema_violation:issues_prose_placeholder

    Returns `(verdict, issues, error)`:
      - `verdict`: `"APPROVE"` or `"REQUEST_CHANGES"`, or None on error.
      - `issues`: list of issue lines stripped of any leading `- `
        bullet, or None on error.
      - `error`: None on happy path, otherwise either a transport-level
        error (`stream_json_no_events`, `stream_json_no_result_event`,
        `stream_json_result_not_string`) or a schema discriminator of
        the form `schema_violation:<reason>`.
    """
    text = stdout_bytes.decode("utf-8", errors="replace")
    result_payload = None
    saw_any_event = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Heartbeat / partial line — skip silently.
            continue
        saw_any_event = True
        if isinstance(event, dict) and event.get("type") == "result":
            result_payload = event.get("result")
            break
    if result_payload is None:
        if not saw_any_event:
            return (None, None, "stream_json_no_events")
        return (None, None, "stream_json_no_result_event")
    if not isinstance(result_payload, str):
        return (None, None, "stream_json_result_not_string")

    return _validate_reviewer_output_schema(result_payload)


def _validate_reviewer_output_schema(result_text: str) -> tuple:
    """Validate one reviewer result body against reviewer-output.md.

    Splits the body into `### `-prefixed sections, validates the
    `VERDICT` token, requires `### Strengths`, and enforces the
    verdict/issues consistency rules. See `_parse_stream_json_result`
    for the full discriminator list.
    """
    # ---- Section split ----------------------------------------------------
    # Walk lines once, attributing each to either the preamble (lines
    # before the first `### ` header) or the most recently opened
    # section. Section keys are normalized to lowercase
    # (`verdict`, `issues`, `strengths`, ...).
    sections: dict = {}
    current_key = None
    current_lines: list = []
    verdict_header_value = None
    for raw in result_text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("### "):
            if current_key is not None:
                sections[current_key] = current_lines
            header_body = stripped[4:]
            # `### VERDICT: APPROVE` is a header+value combo per schema.
            if header_body.upper().startswith("VERDICT"):
                # split on the first colon after the VERDICT word.
                if ":" in header_body:
                    verdict_header_value = header_body.split(":", 1)[1].strip()
                else:
                    verdict_header_value = ""
                current_key = "verdict"
                current_lines = []
            else:
                # `### Issues`, `### Strengths`, ...
                current_key = header_body.split(":", 1)[0].strip().lower()
                current_lines = []
        else:
            if current_key is not None:
                current_lines.append(raw)
    if current_key is not None:
        sections[current_key] = current_lines

    # ---- Verdict validation ----------------------------------------------
    if verdict_header_value is None:
        return (None, None, "schema_violation:missing_verdict")
    if verdict_header_value not in _VALID_VERDICTS:
        return (None, None, "schema_violation:invalid_verdict")
    verdict = verdict_header_value

    # ---- Strengths required ----------------------------------------------
    if "strengths" not in sections:
        return (None, None, "schema_violation:missing_strengths")

    # ---- Issues parsing --------------------------------------------------
    issues: list = []
    issues_present = "issues" in sections
    issues_none_marker = False
    issues_has_prose_placeholder = False
    issues_invalid_severity = False
    issues_has_any_content = False
    last_was_valid_severity = False
    if issues_present:
        for raw in sections["issues"]:
            line = raw.strip()
            if not line:
                # Blank lines do not break a bullet's continuation —
                # they also do not contribute "content".
                continue
            issues_has_any_content = True
            # Continuation lines for the previous bullet (e.g. the
            # `File: ... line N` indented annotation) are emitted by the
            # reviewer as additional indented lines — `raw` retains its
            # leading whitespace. Tolerate them when the immediately
            # preceding bullet was a valid severity entry.
            is_indented_continuation = (
                last_was_valid_severity
                and raw.startswith((" ", "\t"))
                and not line.startswith("- ")
            )
            if is_indented_continuation:
                continue
            # Strip optional leading bullet so `- [CRITICAL] ...` and
            # `[CRITICAL] ...` both match.
            if line.startswith("- "):
                body = line[2:].strip()
            else:
                body = line
            if body == "None.":
                issues_none_marker = True
                last_was_valid_severity = False
                continue
            if body.startswith("[CRITICAL]") or body.startswith("[MINOR]"):
                issues.append(body)
                last_was_valid_severity = True
                continue
            # Detect bracketed severity tokens that are NOT in the
            # `{CRITICAL, MINOR}` allowlist. Per
            # `docs/protocol/reviewer-output.md` §Issues section, any
            # other bracketed severity (`[MAJOR]`, `[HIGH]`,
            # `[WARNING]`, `[INFO]`, `[NITPICK]`, …) is invalid.
            sev_match = _BRACKETED_SEVERITY_RE.match(body)
            if sev_match is not None:
                # First offending line wins as source-of-truth.
                if not issues_invalid_severity:
                    issues_invalid_severity = True
                last_was_valid_severity = False
                continue
            # Anything else inside `### Issues` body is a prose
            # placeholder (per schema, the body must be entirely
            # `- None.` or a list of `- [SEVERITY] ...` bullets).
            low = body.lower()
            if any(p in low for p in _PROSE_PLACEHOLDERS):
                issues_has_prose_placeholder = True
            last_was_valid_severity = False

    if issues_present and issues_invalid_severity:
        # Hard schema failure — caller should treat the body as
        # unusable, so we drop both verdict and issues.
        return (None, None, "schema_violation:invalid_severity")
    if issues_present and issues_has_prose_placeholder:
        return (verdict, None, "schema_violation:issues_prose_placeholder")
    if issues_present and not issues_has_any_content:
        # `### Issues` header with empty body — per schema must be
        # exactly `- None.` if present.
        return (verdict, None, "schema_violation:issues_empty_body")

    has_critical = any(b.startswith("[CRITICAL]") for b in issues)
    has_minor = any(b.startswith("[MINOR]") for b in issues)

    # ---- Verdict / issues consistency ------------------------------------
    if verdict == "APPROVE" and has_critical:
        return (verdict, issues, "schema_violation:approve_with_critical")

    if verdict == "REQUEST_CHANGES":
        # No Issues section at all, OR only `- None.`, → invalid.
        if not issues_present or (issues_none_marker and not issues):
            return (
                verdict,
                issues,
                "schema_violation:request_changes_without_issues",
            )
        if has_minor and not has_critical:
            return (verdict, issues, "schema_violation:request_changes_minor_only")

    return (verdict, issues, None)


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------


class Scheduler:
    """Conflict-aware parallel scheduler for reviewer-round jobs.

    Two orthogonal mechanisms:

    1. **Hard conflict locks** (binary): each job holds its
       `conflict_keys` for the duration of its run. A second job whose
       `conflict_keys` overlaps an in-flight job's held set is queued
       until the in-flight job finishes.

    2. **Capacity throttle** (counted): each `capacity_keys` entry
       increments a counter when a job starts; a job whose
       `capacity_keys` would push any counter above
       `capacity_limits[key]` is queued. Keys absent from
       `capacity_limits` are unbounded.

    `tmp_dir` defaults to `.review-loop/tmp` and houses per-job prompt
    files under a unique `{session_id}.{job_id}.*` directory, including the
    retained prompt, wrapper status, raw stream, result, and usage evidence.
    """

    def __init__(
        self,
        max_parallel: int = 2,
        default_timeout: float = 300.0,
        capacity_limits: "dict | None" = None,
        tmp_dir: "str | os.PathLike" = ".review-loop/tmp",
    ) -> None:
        if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or max_parallel <= 0:
            raise ValueError("max_parallel must be a positive integer")
        if not math.isfinite(default_timeout) or default_timeout <= 0:
            raise ValueError("default_timeout must be finite and positive")
        self.max_parallel = max_parallel
        self.default_timeout = default_timeout
        self.capacity_limits = dict(capacity_limits) if capacity_limits else {}
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
               for value in self.capacity_limits.values()):
            raise ValueError("capacity limits must be positive integers")
        self.tmp_dir = os.path.abspath(os.fspath(tmp_dir))
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._held_binary_keys: set = set()
        self._capacity_in_use: dict = collections.defaultdict(int)
        self._results: dict = {}
        self._processes: dict = {}
        self._cancelled = threading.Event()
        self._cleanup_threads: list = []
        self._stopping_pids: set = set()

    def cancel(self) -> None:
        """Stop active wrappers and prevent queued jobs from launching a CLI."""
        self._cancelled.set()
        with self._lock:
            for process in self._processes.values():
                if process.pid in self._stopping_pids:
                    continue
                self._stopping_pids.add(process.pid)
                # Cleanup is independent per job; do not serialize grace periods.
                thread = threading.Thread(target=_stop_wrapper, args=(process,), daemon=True)
                self._cleanup_threads.append(thread)
                thread.start()

    # ---- key accounting ----------------------------------------------------

    def _can_start(self, job: ReviewJob) -> bool:
        """Caller must hold `self._lock`."""
        if self._held_binary_keys & job.conflict_keys:
            return False
        for k in job.capacity_keys:
            limit = self.capacity_limits.get(k, math.inf)
            if self._capacity_in_use.get(k, 0) >= limit:
                return False
        return True

    def _claim(self, job: ReviewJob) -> None:
        """Caller must hold `self._lock`."""
        self._held_binary_keys |= set(job.conflict_keys)
        for k in job.capacity_keys:
            self._capacity_in_use[k] = self._capacity_in_use.get(k, 0) + 1

    def _release_locked(self, job: ReviewJob) -> None:
        """Caller must hold `self._lock`. Releases binary + capacity keys
        and signals waiters. Used both by `_release` (which acquires the
        lock) and by the in-dispatcher revert path on `ex.submit` failure
        (which is already inside `with self._cond:`).
        """
        self._held_binary_keys -= set(job.conflict_keys)
        for k in job.capacity_keys:
            self._capacity_in_use[k] = max(0, self._capacity_in_use.get(k, 0) - 1)
        self._cond.notify_all()

    def _release(self, job: ReviewJob) -> None:
        with self._cond:
            self._release_locked(job)

    # ---- finalization invariant -------------------------------------------

    @staticmethod
    def _worker_exception_result(job_id: str, exc: BaseException) -> JobResult:
        """Synthesize a `worker_exception:*` JobResult for the dispatcher.

        Used both when `ex.submit` itself raises (executor shutdown, OS
        thread limit) and when an in-flight `fut.result()` raises (e.g.
        `Popen.__init__` raised before `_run_one` could record).
        """
        return JobResult(
            job_id=job_id,
            returncode=None,
            stdout_bytes=b"",
            stderr_bytes=b"",
            timed_out=False,
            parsed_verdict=None,
            parsed_issues=None,
            error=f"worker_exception: {exc!r}",
        )

    def _finalize_if_unrecorded(self, job_id: str, result: JobResult) -> None:
        """Record `result` for `job_id` if not already finalized.

        Tolerates the duplicate-finalization invariant — the dispatcher
        races against `_run_one`'s own `_record_result` call on the
        happy path.
        """
        if job_id not in self._results:
            try:
                self._record_result(job_id, result)
            except SchedulerInvariantError:
                pass

    def _record_result(self, job_id: str, result: JobResult) -> None:
        """Finalize a job's result. Raises on duplicate write.

        Late-drain bytes that arrive after this call MUST stay in
        worker-frame buffers — `_run_one` snapshots the deque ONCE
        and never re-enters this method for the same `job_id`.
        """
        with self._lock:
            if job_id in self._results:
                raise SchedulerInvariantError(
                    f"duplicate _record_result for job_id={job_id!r}"
                )
            self._results[job_id] = result

    # ---- public submit -----------------------------------------------------

    def submit(self, jobs) -> dict:
        """Run all jobs to completion. Returns `{job_id: JobResult}`.

        Workers are taken from a `ThreadPoolExecutor(max_workers=
        self.max_parallel)`. A dispatcher loop releases queued jobs as
        soon as `_can_start(job)` flips True.
        """
        jobs = list(jobs)
        pending = collections.deque(jobs)
        active_futures: dict = {}

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_parallel
        ) as ex:
            while pending or active_futures:
                # Try to start as many pending jobs as possible.
                synthesized_results: list = []
                with self._cond:
                    progressed = True
                    while progressed:
                        progressed = False
                        for _ in range(len(pending)):
                            job = pending.popleft()
                            if self._can_start(job) and len(active_futures) < self.max_parallel:
                                self._claim(job)
                                try:
                                    fut = ex.submit(self._run_one, job)
                                except Exception as exc:  # noqa: BLE001
                                    # ex.submit failed (e.g. executor
                                    # shutdown, OS thread limit). Revert
                                    # the claim so we don't leak the
                                    # binary lock / capacity slot, then
                                    # synthesize a worker_exception
                                    # result for this job.
                                    self._release_locked(job)
                                    synthesized_results.append(
                                        (job, self._worker_exception_result(job.job_id, exc))
                                    )
                                    progressed = True
                                    continue
                                active_futures[fut] = job
                                progressed = True
                            else:
                                pending.append(job)
                # Record any synthesized worker_exception results outside
                # the condition lock (`_record_result` re-acquires it).
                for job, syn_result in synthesized_results:
                    self._finalize_if_unrecorded(job.job_id, syn_result)
                if not active_futures:
                    # Nothing started AND nothing in flight — should not
                    # happen unless caller passed an empty job list.
                    break
                # Wait for at least one in-flight future to complete.
                done, _ = concurrent.futures.wait(
                    list(active_futures.keys()),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    job = active_futures.pop(fut)
                    try:
                        result = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        result = self._worker_exception_result(job.job_id, exc)
                    # `_run_one` already records on the happy path; only
                    # the worker-exception path needs the extra finalize.
                    self._finalize_if_unrecorded(job.job_id, result)
                    self._release(job)
            with self._lock:
                cleanup_threads = list(self._cleanup_threads)
            for thread in cleanup_threads:
                thread.join(timeout=6.0)
            return dict(self._results)

    # ---- per-job worker ----------------------------------------------------

    def _run_one(self, job: ReviewJob) -> JobResult:
        """Run a wrapper, keeping its native evidence out of scheduler memory."""
        if self._cancelled.is_set():
            result = JobResult(job.job_id, None, b"", b"", False, None, None,
                               "reviewer_cancelled", status="cancelled")
            self._record_result(job.job_id, result)
            return result
        # Validate before creating artifacts. A unique directory also separates
        # session/job pairs whose dotted display names happen to be identical.
        _build_argv(job, self)
        os.makedirs(self.tmp_dir, exist_ok=True)
        job_tmp = tempfile.mkdtemp(prefix=_job_session_id(job) + ".", dir=self.tmp_dir)
        prompt_path = os.path.join(job_tmp, _job_session_id(job) + "-reviewer-prompt.txt")
        argv = _build_argv(job, self, tmp_dir=job_tmp)
        with open(prompt_path, "wb") as prompt:
            prompt.write(job.prompt_text.encode("utf-8"))

        proc = None
        # Wrappers emit bounded status/heartbeats. Retain only the last 64 KiB
        # even if a broken wrapper violates that contract.
        stdout_buf = collections.deque(maxlen=STATUS_CAPTURE_BYTES // 4096)
        stderr_buf = collections.deque(maxlen=STATUS_CAPTURE_BYTES // 4096)

        def drain(stream, buf):
            try:
                for chunk in iter(lambda: stream.read(4096), b""):
                    buf.append(chunk)
            except Exception as error:  # Reader failures cannot escape the worker.
                buf.append(("[reader_error:" + type(error).__name__ + "]\n").encode())
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        try:
            with open(prompt_path, "rb") as prompt:
                proc = subprocess.Popen(
                    argv, stdin=prompt, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True, cwd=job.worktree,
                )
                with self._lock:
                    self._processes[job.job_id] = proc
                stdout_thread = threading.Thread(target=drain, args=(proc.stdout, stdout_buf), daemon=True)
                stderr_thread = threading.Thread(target=drain, args=(proc.stderr, stderr_buf), daemon=True)
                stdout_thread.start()
                stderr_thread.start()
                timed_out = False
                cleanup_failed = False
                known_pids = ()
                tree_inspectable = True
                try:
                    if self._cancelled.is_set():
                        returncode = _stop_wrapper(proc)
                    else:
                        # The wrapper's own deadline leaves time to clean up and
                        # publish usage before this emergency outer deadline.
                        timeout = (job.timeout_secs or self.default_timeout) + WRAPPER_CLEANUP_GRACE_SECONDS
                        returncode, timed_out, known_pids, tree_inspectable = wait_with_process_tree(
                            proc, timeout,
                        )
                        if timed_out:
                            returncode = _stop_wrapper(proc, known_pids)
                        else:
                            final_code, cleanup_ok, _observed = terminate_process_tree(
                                proc, known_pids=known_pids,
                            )
                            if returncode is None:
                                returncode = final_code
                            if not cleanup_ok or not tree_inspectable:
                                # Classified separately from a deadline expiry.
                                cleanup_failed = True
                except BaseException:
                    _stop_wrapper(proc, known_pids)
                    raise
                stdout_thread.join(timeout=2.0)
                stderr_thread.join(timeout=2.0)
                # Snapshot once; late reader bytes cannot alter finalized jobs.
                stdout_bytes = b"".join(stdout_buf)
                stderr_bytes = b"".join(stderr_buf)
                summary = _wrapper_summary(stdout_bytes)
                verdict = issues = None
                result_file = usage_file = invocation_id = None
                status = summary.get("status") if summary else None
                if summary:
                    usage_file = summary.get("usage_file")
                    invocation_id = summary.get("invocation_id")
                timed_out = timed_out or status == "timeout"
                if self._cancelled.is_set():
                    status, error = "cancelled", "reviewer_cancelled"
                elif timed_out:
                    error = "reviewer_timeout"
                elif cleanup_failed:
                    error = "reviewer_cleanup_failed"
                elif returncode != 0:
                    error = "reviewer_" + (status if status and status != "ok" else "command_execution")
                elif summary is None:
                    error = "wrapper_status_missing"
                elif status != "ok":
                    error = "reviewer_" + status
                elif (not isinstance(summary.get("tool_uses"), int)
                      or isinstance(summary.get("tool_uses"), bool)):
                    error = "reviewer_tool_uses_missing"
                elif summary["tool_uses"] <= 0:
                    error = "reviewer_tool_uses_zero"
                else:
                    candidate = summary.get("result_file")
                    if not isinstance(candidate, str) or not candidate:
                        error = "output_file_missing"
                    else:
                        try:
                            with open(candidate, "r", encoding="utf-8") as output:
                                verdict, issues, error = _validate_reviewer_output_schema(output.read())
                            if job.role != "reviewer":
                                # Specialists return raw reports; the parsed
                                # verdict stays best-effort metadata only.
                                error = None
                            result_file = candidate
                        except FileNotFoundError:
                            error = "output_file_missing"
                        except (OSError, UnicodeError) as exc:
                            error = "output_file_unreadable:" + type(exc).__name__
                result = JobResult(
                    job_id=job.job_id, returncode=returncode, stdout_bytes=stdout_bytes,
                    stderr_bytes=stderr_bytes, timed_out=timed_out,
                    parsed_verdict=verdict, parsed_issues=issues, error=error,
                    status=status, result_file=result_file, usage_file=usage_file,
                    invocation_id=invocation_id,
                    tool_uses=summary.get("tool_uses") if summary else None,
                )
        finally:
            with self._lock:
                self._processes.pop(job.job_id, None)
            # Prompts, native streams, result and ledger evidence remain under
            # the unique job directory/common ledger for offline verification.
        self._record_result(job.job_id, result)
        return result


# --------------------------------------------------------------------------
# CLI front door
# --------------------------------------------------------------------------


def _parse_capacity(values) -> dict:
    """Parse `--capacity key=N` repeated flags into `{key: int}`."""
    out: dict = {}
    for entry in values or []:
        if "=" not in entry:
            raise ValueError(f"--capacity expects key=N, got: {entry!r}")
        key, raw = entry.split("=", 1)
        key = key.strip()
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ValueError(f"--capacity expects a positive integer, got: {entry!r}") from exc
        if not key or value <= 0:
            raise ValueError(f"--capacity expects a positive integer, got: {entry!r}")
        out[key] = value
    return out


def _load_jobs(path: str) -> list:
    """Load a jobs file (JSON list of dicts) into `[ReviewJob, ...]`."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise ValueError("--jobs file must be a JSON list")
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("--jobs entries must be JSON objects")
        runtime = entry.get("runtime", "codex")
        session_id = entry["session_id"]
        job_id = entry["job_id"]
        worktree = entry.get("worktree")
        conflict_keys = frozenset(
            entry.get("conflict_keys")
            or default_conflict_keys(runtime, session_id, job_id, worktree)
        )
        capacity_keys = frozenset(
            entry.get("capacity_keys") or default_capacity_keys(runtime)
        )
        out.append(
            ReviewJob(
                job_id=job_id,
                session_id=session_id,
                runtime=runtime,
                prompt_text=entry.get("prompt_text", ""),
                reviewer_model=entry.get("reviewer_model", ""),
                timeout_secs=float(entry.get("timeout_secs", 300.0)),
                conflict_keys=conflict_keys,
                capacity_keys=capacity_keys,
                extra_argv=tuple(entry.get("extra_argv") or ()),
                worktree=worktree,
                stage=entry.get("stage", "review"),
                role=entry.get("role", "reviewer"),
            )
        )
    return out


def _result_to_dict(r: JobResult) -> dict:
    return {
        "job_id": r.job_id,
        "returncode": r.returncode,
        "stdout": r.stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": r.stderr_bytes.decode("utf-8", errors="replace"),
        "timed_out": r.timed_out,
        "parsed_verdict": r.parsed_verdict,
        "parsed_issues": r.parsed_issues,
        "error": r.error,
        "status": r.status,
        "result_file": r.result_file,
        "usage_file": r.usage_file,
        "invocation_id": r.invocation_id,
        "tool_uses": r.tool_uses,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Parallel CR scheduler for review-loop reviewer rounds."
    )
    parser.add_argument("--jobs", required=True, help="Path to JSON jobs file.")
    parser.add_argument("--max-parallel", type=int, default=2)
    parser.add_argument("--default-timeout", type=float, default=300.0)
    parser.add_argument("--output", default=None, help="Write JSON results to this path.")
    parser.add_argument("--text", action="store_true", help="Emit a human-readable summary.")
    parser.add_argument(
        "--fail-on-any",
        action="store_true",
        help="Exit 1 if any job has nonzero returncode.",
    )
    parser.add_argument(
        "--capacity",
        action="append",
        default=[],
        help="Repeatable capacity limit, e.g. --capacity cli_rate:claude=2.",
    )
    parser.add_argument(
        "--tmp-dir",
        default=".review-loop/tmp",
        help="Directory for per-job prompt and output files.",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on parse error — preserve.
        return int(exc.code) if exc.code is not None else 2

    try:
        capacity_limits = _parse_capacity(args.capacity)
    except ValueError as exc:
        sys.stderr.write(f"review_verification: {exc}\n")
        return 2

    try:
        jobs = _load_jobs(args.jobs)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"review_verification: failed to load jobs: {exc}\n")
        return 2

    try:
        scheduler = Scheduler(
            max_parallel=args.max_parallel,
            default_timeout=args.default_timeout,
            capacity_limits=capacity_limits,
            tmp_dir=args.tmp_dir,
        )
    except ValueError as exc:
        sys.stderr.write(f"review_verification: {exc}\n")
        return 2

    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(signum, frame):
            # Do not acquire the dispatcher's lock inside a Python signal handler.
            scheduler._cancelled.set()
            threading.Thread(target=scheduler.cancel, daemon=True).start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, interrupted)
    try:
        results = scheduler.submit(jobs)
    except SchedulerInvariantError as exc:
        sys.stderr.write(f"review_verification: scheduler invariant violation: {exc}\n")
        return 3
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

    payload = {jid: _result_to_dict(r) for jid, r in results.items()}

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
    elif args.text:
        for jid in sorted(payload):
            r = payload[jid]
            sys.stdout.write(
                f"{jid}: rc={r['returncode']} timed_out={r['timed_out']} "
                f"verdict={r['parsed_verdict']} error={r['error']}\n"
            )
    else:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    if scheduler._cancelled.is_set():
        return 130
    if args.fail_on_any:
        for r in payload.values():
            if r["returncode"] != 0 or r["timed_out"] or r["error"]:
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
