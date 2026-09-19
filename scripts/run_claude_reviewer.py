#!/usr/bin/env python3
"""Spool a Claude reviewer stream without flooding the orchestrator context.

Transport only: the caller must validate the result's reviewer schema and rubric.
Exit codes: 0 success, 1 command execution, 2 JSON parsing, 3 missing result.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def _stop_process(process):
    """Also terminate descendants holding inherited stream descriptors open."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()


def run_reviewer(session_id, model, tmp_dir, *, heartbeat_seconds=30.0):
    """Run once; short heartbeat intervals are injectable for fake-CLI tests."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", session_id):
        raise ValueError("session_id must be a single safe filename component")
    if heartbeat_seconds <= 0:
        raise ValueError("heartbeat_seconds must be positive")
    tmp_dir = Path(tmp_dir)
    prompt_path = tmp_dir / f"{session_id}-reviewer-prompt.txt"
    stream_path = tmp_dir / f"{session_id}-reviewer-stream.jsonl"
    stderr_path = tmp_dir / f"{session_id}-reviewer-stderr.log"
    result_path = tmp_dir / f"{session_id}-reviewer-result.txt"
    process = None
    result = None
    invalid_lines = 0
    result_seen = False
    result_error = False
    event_count = 0
    last_type = "none"
    child_exit = None
    status = "command_execution"
    exit_code = 1
    old_handlers = {}

    def interrupted(signum, frame):
        raise InterruptedError(f"signal {signum}")

    def consume(line):
        nonlocal result, invalid_lines, result_seen, result_error, event_count, last_type
        if not line.strip():
            return
        event_count += 1
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event is not an object")
            event_type = event.get("type", "unknown")
            last_type = event_type[:80] if isinstance(event_type, str) else "unknown"
            if event_type == "result":
                result_seen = True
                if not isinstance(event.get("result"), str):
                    raise ValueError("result is not a string")
                event["result"].encode("utf-8")
                result = event["result"]
                result_error = result_error or bool(event.get("is_error", False))
        except (ValueError, UnicodeError):
            invalid_lines += 1
            last_type = "invalid_json"

    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        # An unsuccessful new round must never expose a previous result as fresh.
        result_path.unlink(missing_ok=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, interrupted)
        with prompt_path.open("rb") as prompt, stream_path.open("wb") as stream, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                ["claude", "-p", "--no-session-persistence", "--output-format",
                 "stream-json", "--include-partial-messages", "--verbose", "--model", model],
                stdin=prompt, stdout=subprocess.PIPE, stderr=stderr,
                start_new_session=True,
            )
            started = time.monotonic()
            next_heartbeat = started + heartbeat_seconds
            pending = bytearray()
            with process.stdout, selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map() or process.poll() is None:
                    if process.poll() is not None:
                        _stop_process(process)
                    timeout = max(0.0, next_heartbeat - time.monotonic())
                    for key, _ in selector.select(min(timeout, 1.0)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        stream.write(chunk)
                        stream.flush()
                        pending.extend(chunk)
                        while True:
                            newline = pending.find(b"\n")
                            if newline < 0:
                                break
                            consume(bytes(pending[:newline]))
                            del pending[:newline + 1]
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        if not result_seen and selector.get_map():
                            _emit({"type": "reviewer_heartbeat", "elapsed_seconds": round(now - started, 1),
                                   "events": event_count, "last_event_type": last_type})
                        next_heartbeat = now + heartbeat_seconds
                if pending:
                    consume(bytes(pending))
            child_exit = process.wait()
            if child_exit != 0 or result_error:
                status, exit_code = "command_execution", 1
            elif result is None and invalid_lines:
                status, exit_code = "json_parsing", 2
            elif result is None:
                status, exit_code = "missing_result", 3
            else:
                result_path.write_text(result, encoding="utf-8")
                status, exit_code = "ok", 0
    except (OSError, KeyboardInterrupt):
        status, exit_code = "command_execution", 1
        # Do not print exception text: it may contain model output or prompt data.
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass
    finally:
        # Ignore repeat interrupts during child cleanup, then restore the caller.
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if process is not None:
                _stop_process(process)
                child_exit = process.returncode
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
    summary = {"status": status, "result_file": str(result_path) if exit_code == 0 else None,
               "stream_file": str(stream_path), "stderr_file": str(stderr_path),
               "child_exit": child_exit, "invalid_lines": invalid_lines}
    if child_exit is not None and child_exit != 0:
        try:
            with stderr_path.open(encoding="utf-8", errors="replace") as stderr:
                summary["stderr_head"] = stderr.readline(300).rstrip("\r\n")
        except OSError:
            summary["stderr_head"] = ""
    _emit(summary)
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--tmp-dir", default=".review-loop/tmp")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.session_id):
        parser.error("--session-id must be a single safe filename component")
    return run_reviewer(args.session_id, args.model, args.tmp_dir)


if __name__ == "__main__":
    raise SystemExit(main())
