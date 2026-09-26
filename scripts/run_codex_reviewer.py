#!/usr/bin/env python3
"""Run an isolated, read-only Codex reviewer with bounded lifetime.

Transport only; schema/rubric validation stays with the caller. This launcher
never reads the review-loop protocol to decide whether a review is approved.
Exit codes: 0 success with verified native tool use, 1 command/transport failure, 2 malformed stream,
3 missing result, 4 timeout, 5 cancelled, 6 rate-limited.
8 means no verifiable root-agent tool use; the report is never published.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from reviewer_usage import UsageAccumulator, record_invocation
try:
    from process_tree import observe_process_tree, terminate_process_tree
except ModuleNotFoundError:  # imported as scripts.run_codex_reviewer
    from scripts.process_tree import observe_process_tree, terminate_process_tree


POLL_SECONDS = 0.05
DRAIN_SECONDS = 1.0
MAX_EVENT_BYTES = 8 * 1024 * 1024
_RATE_LIMIT = re.compile(
    r"rate[ _-]?limit|usage limit|quota exceeded|too many requests|\b429\b", re.I,
)


def codex_argv(model, output):
    # Clean cwd + ignored user configuration prevents project/user MCP servers,
    # hooks and execpolicy rules from widening the shell's read-only boundary.
    argv = ["codex", "exec", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "--ephemeral", "--json", "-s", "read-only",
            "-c", "features.hooks=false", "-c", "features.plugins=false",
            "-c", "features.apps=false", "-c", "features.multi_agent=false",
            "-c", 'web_search="disabled"', "-c", "project_doc_max_bytes=0"]
    if model:
        argv += ["-m", model]
    return argv + ["-o", str(output), "-"]


def stop_group(process):
    """Bound TERM/KILL waits; kill descendants after the group leader exits.

    Descendants that deliberately create another session are outside this POSIX
    process-group boundary. Stream parsing remains bounded independently.
    """
    signal_failed = False
    for sig, grace in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            signal_failed = True
            try:
                process.send_signal(sig)
            except OSError:
                pass
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    return process.poll() is not None and not signal_failed


class _Events:
    """Parse native events without buffering unbounded lines or replaying text."""

    def __init__(self, accumulator):
        self.accumulator = accumulator
        self.pending = bytearray()
        self.dropping_line = False
        self.invalid_lines = 0
        self.event_count = 0
        self.last_type = "none"
        self.completed = False
        self.failed = False
        self.rate_limited = False
        self.tool_use_ids = set()
        self.tool_use_count_unknown = False

    def invalid(self):
        self.invalid_lines += 1
        self.last_type = "invalid_json"
        self.accumulator.consume(None)

    def consume(self, line):
        if not line.strip():
            return
        self.event_count += 1
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event is not an object")
            self.accumulator.consume(event)
            event_type = event.get("type", "unknown")
            self.last_type = event_type[:80] if isinstance(event_type, str) else "unknown"
            self.completed |= event_type == "turn.completed"
            item = event.get("item")
            if event_type in ("item.started", "item.completed") and isinstance(item, dict):
                item_type = item.get("type")
                if item_type in {"command_execution", "file_change", "mcp_tool_call",
                                 "web_search", "collab_tool_call", "tool_call",
                                 "image_view", "computer_call"}:
                    identity = item.get("id")
                    if not isinstance(identity, str) or not identity:
                        identity = (event_type, self.event_count)
                    self.tool_use_ids.add(identity)
                elif item_type not in {"agent_message", "reasoning", "plan", "todo_list", "error"}:
                    self.tool_use_count_unknown = True
            if event_type in ("turn.failed", "error"):
                self.failed = True
                # A token count of 429 is not a rate-limit error.
                detail = " ".join(json.dumps(event[field]) for field in (
                    "error", "message", "reason", "status_code",
                ) if field in event)
                self.rate_limited |= bool(_RATE_LIMIT.search(detail))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self.invalid()

    def feed(self, chunk):
        self.pending.extend(chunk)
        while True:
            newline = self.pending.find(b"\n")
            if newline < 0:
                if len(self.pending) > MAX_EVENT_BYTES:
                    if not self.dropping_line:
                        self.invalid()
                    self.dropping_line = True
                    self.pending.clear()
                return
            if not self.dropping_line:
                if newline > MAX_EVENT_BYTES:
                    self.invalid()
                else:
                    self.consume(bytes(self.pending[:newline]))
            self.dropping_line = False
            del self.pending[:newline + 1]

    def finish(self):
        if self.pending and not self.dropping_line:
            self.consume(bytes(self.pending))
        self.pending.clear()


def _drain(source, events, deadline, max_bytes=None):
    """Read an available regular-file tail, bounded by time and optional bytes."""
    consumed = 0
    while time.monotonic() < deadline:
        chunk = source.read(65536)
        if not chunk:
            return True
        events.feed(chunk)
        consumed += len(chunk)
        if max_bytes is not None and consumed >= max_bytes:
            return False
    return False


def run_reviewer(session_id, model, tmp_dir, *, parent_session_id=None, timeout_seconds=600.0,
                 heartbeat_seconds=30.0, stage="review", role="reviewer",
                 ledger_dir=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", session_id):
        raise ValueError("session_id must be a single safe filename component")
    for value in (timeout_seconds, heartbeat_seconds):
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeouts must be finite and positive")
    started = time.monotonic()
    deadline = started + timeout_seconds
    target = Path.cwd().resolve()
    tmp = Path(tmp_dir).resolve()
    invocation_id = str(uuid.uuid4())
    stem = f"{session_id}-codex-{invocation_id}"
    prompt = tmp / f"{session_id}-reviewer-prompt.txt"
    captured_prompt = tmp / f"{stem}-prompt.txt"
    stream = tmp / f"{stem}-stream.jsonl"
    errors = tmp / f"{stem}-stderr.log"
    candidate = tmp / f"{stem}-output.txt"
    publishing = tmp / f"{stem}-publish.txt"
    result = tmp / f"{session_id}-reviewer-result.txt"
    accumulator = UsageAccumulator(runtime="codex")
    events = _Events(accumulator)
    process = None
    status, code = "command_execution", 1
    child_exit = None
    handlers = {}
    cancel_signal = None
    cleanup_ok = True
    failure_reason = None
    record_path = None
    phase = "preparing"
    usage = None

    def interrupted(signum, frame):
        nonlocal cancel_signal
        cancel_signal = cancel_signal or signum

    try:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                handlers[sig] = signal.signal(sig, interrupted)
        tmp.mkdir(parents=True, exist_ok=True)
        result.unlink(missing_ok=True)
        text = prompt.read_text(encoding="utf-8")
        captured_prompt.write_text(
            "This is a self-contained read-only review, not a workflow invocation. "
            "Do not load or run any skill, plugin, or orchestration instructions. "
            f"Inspect the repository at {json.dumps(str(target))}. "
            "Use absolute paths or git -C for inspection. Do not modify files, "
            "install dependencies or run commands requiring writes. Verification "
            "evidence is supplied by the caller; request missing evidence.\n\n" + text,
            encoding="utf-8")
        if cancel_signal is not None:
            status, code = "cancelled", 5
        elif time.monotonic() >= deadline:
            status, code = "timeout", 4
        else:
            with tempfile.TemporaryDirectory(prefix="review-loop-readonly-") as clean:
                with captured_prompt.open("rb") as stdin, stream.open("wb") as stdout, errors.open("wb") as stderr, stream.open("rb") as source:
                    try:
                        phase = "reviewer"
                        process = subprocess.Popen(codex_argv(model, candidate), cwd=clean,
                                                   stdin=stdin, stdout=stdout, stderr=stderr,
                                                   start_new_session=True)
                        next_heartbeat = started + heartbeat_seconds
                        known_pids = set()
                        tree_inspectable = True
                        while process.poll() is None:
                            observed = observe_process_tree(process.pid, known_pids)
                            if observed is None:
                                tree_inspectable = False
                            else:
                                known_pids.update(observed)
                            if cancel_signal is not None:
                                status, code = "cancelled", 5
                                break
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                status, code = "timeout", 4
                                break
                            _drain(source, events, deadline, max_bytes=1024 * 1024)
                            now = time.monotonic()
                            if now >= next_heartbeat:
                                print(json.dumps({
                                    "type": "reviewer_heartbeat", "phase": phase,
                                    "elapsed_seconds": round(now-started, 1),
                                    "events": events.event_count, "last_event_type": events.last_type,
                                }), flush=True)
                                next_heartbeat = now + heartbeat_seconds
                            try:
                                process.wait(timeout=min(max(0.0, deadline - now), POLL_SECONDS))
                            except subprocess.TimeoutExpired:
                                pass
                        else:
                            status, code = ("ok", 0) if process.returncode == 0 else ("command_execution", 1)
                    finally:
                        # Teardown precedes temp cwd removal and final stream parsing.
                        if process is not None:
                            group_ok = stop_group(process)
                            _final_code, tree_ok, _observed = terminate_process_tree(
                                process, known_pids=known_pids if "known_pids" in locals() else (),
                            )
                            cleanup_ok = bool(group_ok and tree_ok and
                                              (tree_inspectable if "tree_inspectable" in locals() else True))
                            child_exit = process.poll()
                            if not cleanup_ok:
                                failure_reason = "cleanup_failed"
                                if code == 0:
                                    status, code = "command_execution", 1
                        try:
                            complete = _drain(source, events, time.monotonic() + DRAIN_SECONDS)
                        except OSError:
                            complete = False
                        if not complete:
                            failure_reason = "stream_drain_incomplete"
                            if code == 0:
                                status, code = "command_execution", 1
        events.finish()
        if code == 0 and events.failed:
            status, code = "command_execution", 1
        if code == 1:
            rate_limited = events.rate_limited
            try:
                with errors.open("rb") as stderr:
                    stderr.seek(0, os.SEEK_END)
                    stderr.seek(max(0, stderr.tell() - 8192))
                    rate_limited |= bool(_RATE_LIMIT.search(stderr.read().decode("utf-8", errors="replace")))
            except OSError:
                pass
            if rate_limited and failure_reason is None:
                status, code = "rate_limited", 6
        if code == 0 and events.invalid_lines:
            status, code = "json_parsing", 2
        if code == 0 and (not events.completed or not candidate.is_file() or not candidate.stat().st_size):
            status, code = "missing_result", 3
        if code == 0:
            tool_uses = (len(events.tool_use_ids)
                         if events.invalid_lines == 0 and not events.tool_use_count_unknown else None)
            if not isinstance(tool_uses, int) or isinstance(tool_uses, bool):
                status, code = "tool_use_unverified", 8
                failure_reason = "tool_use_count_unknown"
            elif tool_uses == 0:
                status, code = "tool_uses_zero", 8
                failure_reason = "zero_tool_uses"
        if cancel_signal is not None:
            status, code = "cancelled", 5
        if code == 0:
            # The unique candidate is raw evidence; publish the compatibility path
            # atomically only after successful exit, cleanup and stream validation.
            publishing.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
            os.replace(publishing, result)
    except KeyboardInterrupt:
        status, code = "cancelled", 5
        cancel_signal = cancel_signal or signal.SIGINT
    except (OSError, UnicodeError):
        status, code = "command_execution", 1
        failure_reason = "io_or_launch_failure"
    finally:
        events.finish()
        if cancel_signal is not None:
            status, code = "cancelled", 5
        if code != 0:
            try:
                result.unlink(missing_ok=True)
            except OSError:
                failure_reason = "result_cleanup_failed"
        try:
            publishing.unlink(missing_ok=True)
        except OSError:
            failure_reason = "result_cleanup_failed"
            status, code = "command_execution", 1
        usage = accumulator.summary()
        try:
            record_path = record_invocation(
                ledger_dir or tmp / "usage", invocation_id=invocation_id,
                session_id=session_id, parent_session_id=parent_session_id,
                role=role, stage=stage, backend="codex",
                requested_model=model or None, status=status,
                elapsed_seconds=round(time.monotonic()-started, 3), usage=usage,
                raw_artifacts=[captured_prompt, stream, errors, candidate])
            usage = json.loads(record_path.read_text(encoding="utf-8"))["usage"]
        except (OSError, ValueError):
            status, code = "command_execution", 1
            failure_reason = "usage_record_failed"
        if code != 0:
            try:
                result.unlink(missing_ok=True)
            except OSError:
                failure_reason = "result_cleanup_failed"
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    summary = {
        "status": status, "child_exit": child_exit,
        "result_file": str(result) if code == 0 else None,
        "prompt_file": str(captured_prompt), "stream_file": str(stream), "stderr_file": str(errors),
        "usage_file": str(record_path) if record_path is not None else None, "usage": usage,
        "invocation_id": invocation_id, "invalid_lines": events.invalid_lines,
        "events": events.event_count, "phase": phase, "cleanup_ok": cleanup_ok,
        "tool_uses": len(events.tool_use_ids) if events.invalid_lines == 0 and not events.tool_use_count_unknown else None,
        "elapsed_seconds": round(time.monotonic()-started, 3),
    }
    if failure_reason:
        summary["failure_reason"] = failure_reason
    if cancel_signal is not None:
        summary["cancel_signal"] = cancel_signal
    print(json.dumps(summary), flush=True)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--parent-session-id")
    parser.add_argument("--model", default="")
    parser.add_argument("--tmp-dir", default=".review-loop/tmp")
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--heartbeat-seconds", type=float, default=30)
    parser.add_argument("--stage", default="review")
    parser.add_argument("--role", default="reviewer")
    parser.add_argument("--ledger-dir")
    args = parser.parse_args()
    try:
        return run_reviewer(args.session_id, args.model, args.tmp_dir,
                            parent_session_id=args.parent_session_id,
                            timeout_seconds=args.timeout_seconds, heartbeat_seconds=args.heartbeat_seconds,
                            stage=args.stage, role=args.role, ledger_dir=args.ledger_dir)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
