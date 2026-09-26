#!/usr/bin/env python3
"""Run a read-only Claude reviewer and spool its stream outside caller context.

Transport only: the caller must validate the reviewer's result schema/rubric.
Exit codes: 0 success with verified native tool use, 1 command/transport failure, 2 JSON/result-shape failure,
3 missing result, 4 timeout, 5 cancelled, 6 rate-limited, 7 unsupported safe CLI.
8 means no verifiable root-agent tool use; the report is never published.
The timeout covers capability probing and review, plus bounded cleanup grace.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import threading
import time
import uuid

from reviewer_permissions import claude_readonly_args, unsupported_claude_flags
from reviewer_usage import UsageAccumulator, record_invocation
try:
    from process_tree import observe_process_tree, terminate_process_tree
except ModuleNotFoundError:  # imported as scripts.run_claude_reviewer
    from scripts.process_tree import observe_process_tree, terminate_process_tree


DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_HEARTBEAT_SECONDS = 30.0
CAPABILITY_TIMEOUT_SECONDS = 5.0
TERMINATE_GRACE_SECONDS = 1.0
KILL_WAIT_SECONDS = 2.0
DRAIN_GRACE_SECONDS = 1.0
POLL_SECONDS = 0.05
MAX_EVENT_BYTES = 8 * 1024 * 1024
_RATE_LIMIT = re.compile(
    r"rate[ _-]?limit|too many requests|\b429\b|usage limit|hit your limit", re.I,
)


def _is_rate_limit_error(event):
    # Usage/duration counters can equal 429; only inspect error-bearing fields.
    detail = " ".join(json.dumps(event[field]) for field in (
        "result", "error", "errors", "message", "subtype", "status_code",
    ) if field in event)
    return bool(_RATE_LIMIT.search(detail))


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def _stop_process(process):
    """TERM, then KILL the cached process group, and reap with finite waits.

    start_new_session makes the leader PID the group ID. Always kill that group
    even after the leader has exited: its descendants may retain stdout/stderr.
    Descendants that deliberately create another session are outside this POSIX
    process-group boundary; draining their inherited pipes is still bounded.
    """
    signal_failed = False
    for sig, wait_seconds in (
        (signal.SIGTERM, TERMINATE_GRACE_SECONDS),
        (signal.SIGKILL, KILL_WAIT_SECONDS),
    ):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            signal_failed = True
            # A failed group signal must not prevent reaping the direct child.
            try:
                process.send_signal(sig)
            except OSError:
                pass
        try:
            process.wait(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            pass
    return process.poll() is not None and not signal_failed


@dataclass
class ProcessOutcome:
    status: str
    child_exit: int | None
    cleanup_ok: bool


def _run_process(argv, *, stdin, stream, stderr, deadline, cancelled, on_chunk=None,
                 heartbeat=None):
    """Drain stdout during execution and teardown, including leader-exit races."""
    process = subprocess.Popen(
        argv, stdin=stdin, stdout=subprocess.PIPE, stderr=stderr,
        start_new_session=True,
    )
    status = "ok"
    stopped = False
    cleanup_ok = True
    drain_deadline = None
    selector = None
    known_pids = set()
    tree_inspectable = True

    def observe_tree():
        nonlocal tree_inspectable
        observed = observe_process_tree(process.pid, known_pids)
        if observed is None:
            tree_inspectable = False
        else:
            known_pids.update(observed)

    def stop_tree():
        group_ok = _stop_process(process)
        _returncode, tree_ok, _observed = terminate_process_tree(
            process, known_pids=known_pids,
        )
        return bool(group_ok and tree_ok and tree_inspectable)

    observe_tree()
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
    except OSError:
        cleanup_ok = stop_tree()
        process.stdout.close()
        if selector is not None:
            selector.close()
        return ProcessOutcome("stream_io_failure", process.returncode, cleanup_ok)
    with process.stdout, selector:

        def drain_once(wait_seconds):
            for key, _ in selector.select(max(0.0, wait_seconds)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                stream.write(chunk)
                stream.flush()
                if on_chunk is not None:
                    on_chunk(chunk)

        try:
            while selector.get_map() or process.poll() is None:
                observe_tree()
                now = time.monotonic()
                if cancelled():
                    status = "cancelled"
                    break
                if now >= deadline:
                    status = "timeout"
                    break
                if process.poll() is not None and not stopped:
                    cleanup_ok = stop_tree()
                    stopped = True
                    drain_deadline = time.monotonic() + DRAIN_GRACE_SECONDS
                if drain_deadline is not None and now >= drain_deadline:
                    status = "drain_incomplete"
                    break
                drain_once(min(POLL_SECONDS, deadline - now))
                if heartbeat is not None:
                    heartbeat(time.monotonic())
        except OSError:
            status = "stream_io_failure"
        finally:
            if not stopped:
                cleanup_ok = stop_tree()
            # Do not discard buffered diagnostics on timeout or cancellation.
            end = time.monotonic() + DRAIN_GRACE_SECONDS
            while selector.get_map() and time.monotonic() < end:
                try:
                    drain_once(min(POLL_SECONDS, end - time.monotonic()))
                except OSError:
                    status = "stream_io_failure"
                    break
            if selector.get_map() and status == "ok":
                status = "drain_incomplete"
    if not cleanup_ok and status == "ok":
        status = "cleanup_failed"
    return ProcessOutcome(status, process.returncode, cleanup_ok)


class _Events:
    """Bound the incomplete-line buffer while retaining the entire raw stream."""

    def __init__(self, usage):
        self.usage = usage
        self.pending = bytearray()
        self.dropping_line = False
        self.result = None
        self.result_error = False
        self.invalid_result = False
        self.invalid_lines = 0
        self.event_count = 0
        self.last_type = "none"
        self.rate_limited = False
        self.tool_use_ids = set()

    def _count_tool_uses(self, event):
        """Count root-agent tool blocks without retaining their payloads."""
        blocks = []
        native = event
        if event.get("type") == "stream_event" and isinstance(event.get("event"), dict):
            native = event["event"]
        if (event.get("parent_tool_use_id") is not None
                or native.get("parent_tool_use_id") is not None):
            return
        if native.get("type") == "content_block_start":
            blocks.append(native.get("content_block"))
        elif event.get("type") == "assistant":
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), list):
                blocks.extend(message["content"])
        for index, block in enumerate(blocks):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                identity = block.get("id")
                if not isinstance(identity, str) or not identity:
                    identity = (event.get("type"), event.get("index"), self.event_count, index)
                self.tool_use_ids.add(identity)

    def consume(self, line):
        if not line.strip():
            return
        self.event_count += 1
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event is not an object")
            event_type = event.get("type", "unknown")
            self.last_type = event_type[:80] if isinstance(event_type, str) else "unknown"
            self.usage.consume(event)
            self._count_tool_uses(event)
            if event_type == "result":
                is_error = event.get("is_error", False)
                if not isinstance(is_error, bool):
                    self.invalid_result = True
                    raise ValueError("is_error is not a boolean")
                self.result_error = self.result_error or is_error
                if is_error:
                    self.rate_limited |= _is_rate_limit_error(event)
                    # Native error subtypes may provide errors[] without result.
                    if "result" not in event:
                        return
                if not isinstance(event.get("result"), str):
                    self.invalid_result = True
                    raise ValueError("result is not a string")
                try:
                    event["result"].encode("utf-8")
                except UnicodeError:
                    self.invalid_result = True
                    raise
                self.result = event["result"]
            elif event_type == "rate_limit_event":
                info = event.get("rate_limit_info", {})
                if isinstance(info, dict) and info.get("status") == "rejected":
                    self.rate_limited = True
            elif event_type == "error":
                self.rate_limited |= _is_rate_limit_error(event)
        except (ValueError, UnicodeError, RecursionError):
            self.invalid_lines += 1
            self.last_type = "invalid_json"

    def feed(self, chunk):
        self.pending.extend(chunk)
        while True:
            newline = self.pending.find(b"\n")
            if newline < 0:
                if len(self.pending) > MAX_EVENT_BYTES:
                    if not self.dropping_line:
                        self.invalid_lines += 1
                        self.last_type = "invalid_json"
                    self.dropping_line = True
                    self.pending.clear()
                return
            if not self.dropping_line:
                if newline > MAX_EVENT_BYTES:
                    self.invalid_lines += 1
                    self.last_type = "invalid_json"
                else:
                    self.consume(bytes(self.pending[:newline]))
            self.dropping_line = False
            del self.pending[:newline + 1]

    def finish(self):
        if self.pending and not self.dropping_line:
            self.consume(bytes(self.pending))
        self.pending.clear()


def _positive_finite(value, name):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def run_reviewer(session_id, model, tmp_dir, *, parent_session_id=None, heartbeat_seconds=DEFAULT_HEARTBEAT_SECONDS,
                 timeout_seconds=DEFAULT_TIMEOUT_SECONDS, stage="review", role="reviewer",
                 ledger_dir=None):
    """Run one fresh invocation, retaining its raw evidence and normalized usage."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", session_id):
        raise ValueError("session_id must be a single safe filename component")
    _positive_finite(heartbeat_seconds, "heartbeat_seconds")
    _positive_finite(timeout_seconds, "timeout_seconds")
    started = time.monotonic()
    deadline = started + timeout_seconds
    next_heartbeat = started + heartbeat_seconds
    tmp_dir = Path(tmp_dir)
    invocation_id = uuid.uuid4().hex
    invocation_dir = tmp_dir / f"{session_id}-reviewer-{invocation_id}"
    prompt_path = tmp_dir / f"{session_id}-reviewer-prompt.txt"
    captured_prompt_path = invocation_dir / "prompt.txt"
    stream_path = invocation_dir / "stream.jsonl"
    stderr_path = invocation_dir / "stderr.log"
    help_path = invocation_dir / "cli-help.txt"
    help_stderr_path = invocation_dir / "cli-help-stderr.log"
    result_path = tmp_dir / f"{session_id}-reviewer-result.txt"
    invocation_result = invocation_dir / "result.txt"
    usage = UsageAccumulator(runtime="claude")
    events = _Events(usage)
    child_exit = None
    status, exit_code = "command_execution", 1
    failure_reason = None
    phase = "capability_probe"
    cancel_signal = None
    cleanup_ok = True
    old_handlers = {}
    ledger_path = None
    usage_summary = None

    def interrupted(signum, frame):
        nonlocal cancel_signal
        cancel_signal = cancel_signal or signum

    def heartbeat(now):
        nonlocal next_heartbeat
        if now >= next_heartbeat:
            _emit({"type": "reviewer_heartbeat", "elapsed_seconds": round(now - started, 1),
                   "events": events.event_count, "last_event_type": events.last_type,
                   "phase": phase})
            next_heartbeat = now + heartbeat_seconds

    def execute(argv, prompt, stream, stderr, child_deadline, on_chunk=None):
        return _run_process(
            argv, stdin=prompt, stream=stream, stderr=stderr,
            deadline=child_deadline, cancelled=lambda: cancel_signal is not None,
            on_chunk=on_chunk, heartbeat=heartbeat,
        )

    def outcome_failure(outcome):
        nonlocal status, exit_code, failure_reason
        if outcome.status == "timeout":
            status, exit_code = "timeout", 4
        elif outcome.status == "cancelled":
            status, exit_code = "cancelled", 5
        else:
            status, exit_code = "command_execution", 1
        failure_reason = outcome.status

    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        # A failed retry must never expose an earlier result as a fresh review.
        result_path.unlink(missing_ok=True)
        invocation_dir.mkdir()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                old_handlers[sig] = signal.signal(sig, interrupted)
        shutil.copyfile(prompt_path, captured_prompt_path)
        with help_path.open("wb") as help_stream, help_stderr_path.open("wb") as help_stderr:
            outcome = execute(
                ["claude", "--help"], subprocess.DEVNULL, help_stream, help_stderr,
                min(deadline, time.monotonic() + CAPABILITY_TIMEOUT_SECONDS),
            )
        child_exit, cleanup_ok = outcome.child_exit, outcome.cleanup_ok
        if outcome.status != "ok":
            outcome_failure(outcome)
        elif child_exit != 0:
            failure_reason = "capability_probe_failed"
        else:
            with help_path.open(encoding="utf-8", errors="replace") as help_stream:
                missing_flags = unsupported_claude_flags(help_stream.read(262144))
            if missing_flags:
                status, exit_code = "unsafe_cli", 7
                failure_reason = "missing_flags:" + ",".join(missing_flags)
            elif cancel_signal is not None:
                status, exit_code = "cancelled", 5
            elif time.monotonic() >= deadline:
                status, exit_code = "timeout", 4
            else:
                phase = "reviewer"
                with captured_prompt_path.open("rb") as prompt, stream_path.open("wb") as stream, stderr_path.open("wb") as stderr:
                    outcome = execute(
                        ["claude", "-p", "--no-session-persistence", "--output-format",
                         "stream-json", "--include-partial-messages", "--verbose",
                         *( ["--model", model] if model else [] ),
                         *claude_readonly_args()],
                        prompt, stream, stderr, deadline, events.feed,
                    )
                events.finish()
                child_exit, cleanup_ok = outcome.child_exit, outcome.cleanup_ok
                if outcome.status != "ok":
                    outcome_failure(outcome)
                elif child_exit != 0 or events.result_error:
                    with stderr_path.open(encoding="utf-8", errors="replace") as stderr:
                        limited = events.rate_limited or bool(_RATE_LIMIT.search(stderr.read(65536)))
                    status, exit_code = ("rate_limited", 6) if limited else ("command_execution", 1)
                elif events.invalid_result or (events.result is None and events.invalid_lines):
                    status, exit_code = "json_parsing", 2
                elif events.result is None:
                    status, exit_code = ("rate_limited", 6) if events.rate_limited else ("missing_result", 3)
                else:
                    tool_uses = len(events.tool_use_ids) if events.invalid_lines == 0 else None
                    if not isinstance(tool_uses, int) or isinstance(tool_uses, bool):
                        status, exit_code = "tool_use_unverified", 8
                        failure_reason = "tool_use_count_unknown"
                    elif tool_uses == 0:
                        status, exit_code = "tool_uses_zero", 8
                        failure_reason = "zero_tool_uses"
                    else:
                        invocation_result.write_text(events.result, encoding="utf-8")
                        # Keep the historical result path; publish only after clean exit.
                        staging = invocation_dir / "publish-result.txt"
                        shutil.copyfile(invocation_result, staging)
                        os.replace(staging, result_path)
                        status, exit_code = "ok", 0
    except KeyboardInterrupt:
        status, exit_code = "cancelled", 5
        cancel_signal = cancel_signal or signal.SIGINT
    except OSError:
        status, exit_code = "command_execution", 1
        failure_reason = "io_or_launch_failure"
        # Exception messages may contain prompt/model output; retain raw files.
    finally:
        # Handlers only latch cancellation; repeated signals cannot interrupt reap.
        events.finish()
        if cancel_signal is not None:
            status, exit_code = "cancelled", 5
        if exit_code != 0:
            try:
                result_path.unlink(missing_ok=True)
                invocation_result.unlink(missing_ok=True)
            except OSError:
                failure_reason = "result_cleanup_failed"
        usage_summary = usage.summary()
        try:
            ledger_path = record_invocation(
                ledger_dir or tmp_dir / "usage", invocation_id=invocation_id,
                session_id=session_id, parent_session_id=parent_session_id,
                role=role, stage=stage, backend="claude",
                requested_model=model or None, status=status,
                elapsed_seconds=round(time.monotonic() - started, 3), usage=usage_summary,
                raw_artifacts=[str(path) for path in (
                    captured_prompt_path, stream_path, stderr_path, help_path,
                    help_stderr_path, invocation_result,
                ) if path.exists()],
            )
            # The ledger marks interrupted/incomplete accounting as partial.
            usage_summary = json.loads(Path(ledger_path).read_text(encoding="utf-8"))["usage"]
        except (OSError, ValueError):
            status, exit_code = "command_execution", 1
            failure_reason = "usage_record_failed"
        if exit_code != 0:
            try:
                result_path.unlink(missing_ok=True)
                invocation_result.unlink(missing_ok=True)
            except OSError:
                failure_reason = "result_cleanup_failed"
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    summary = {
        "status": status, "invocation_id": invocation_id,
        "result_file": str(result_path) if exit_code == 0 else None,
        "stream_file": str(stream_path), "stderr_file": str(stderr_path),
        "child_exit": child_exit, "invalid_lines": events.invalid_lines,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "phase": phase, "cleanup_ok": cleanup_ok,
        "usage_file": str(ledger_path) if ledger_path is not None else None,
        "usage": usage_summary,
        "tool_uses": len(events.tool_use_ids) if events.invalid_lines == 0 else None,
    }
    if failure_reason:
        summary["failure_reason"] = failure_reason
    if cancel_signal is not None:
        summary["cancel_signal"] = cancel_signal
    if child_exit is not None and child_exit != 0:
        diagnostic_path = help_stderr_path if phase == "capability_probe" else stderr_path
        try:
            with diagnostic_path.open(encoding="utf-8", errors="replace") as stderr:
                summary["stderr_head"] = stderr.readline(300).rstrip("\r\n")
        except OSError:
            summary["stderr_head"] = ""
    _emit(summary)
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--parent-session-id")
    parser.add_argument("--model", default="")
    parser.add_argument("--tmp-dir", default=".review-loop/tmp")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_HEARTBEAT_SECONDS)
    parser.add_argument("--stage", default="review")
    parser.add_argument("--role", default="reviewer")
    parser.add_argument("--ledger-dir")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.session_id):
        parser.error("--session-id must be a single safe filename component")
    for name in ("timeout_seconds", "heartbeat_seconds"):
        try:
            _positive_finite(getattr(args, name), name)
        except ValueError as exc:
            parser.error(str(exc))
    return run_reviewer(
        args.session_id, args.model, args.tmp_dir,
        parent_session_id=args.parent_session_id,
        timeout_seconds=args.timeout_seconds, heartbeat_seconds=args.heartbeat_seconds,
        stage=args.stage, role=args.role, ledger_dir=args.ledger_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
