"""Transport tests use a fake Claude executable, never a model invocation."""

import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_claude_reviewer as wrapper
from reviewer_permissions import claude_readonly_args, REQUIRED_CLAUDE_FLAGS


FAKE_CLAUDE = r'''
import json, os, signal, sys, time
from pathlib import Path
mode = os.environ["FAKE_MODE"]
if sys.argv[1:] == ["--help"]:
    if mode == "help_hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    if mode == "help_nonzero":
        sys.stderr.write("HELP_DIAGNOSTIC\n")
        sys.exit(9)
    flags = os.environ["FAKE_HELP"]
    print(flags.replace("--safe-mode", "--safe-mode-unsupported") if mode == "unsafe" else flags)
    sys.exit(0)
Path(os.environ["CAPTURE"]).write_text(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}))
def emit(event):
    print(json.dumps(event), flush=True)
def emit_read_tool():
    block = {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {}}
    emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
          "content_block": block}})
    if mode == "tool":
        emit({"type": "assistant", "message": {"content": [block]}})
if mode in ("descendant", "descendant_hang"):
    ready_read, ready_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_read)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(os.environ["DESCENDANT_PID"]).write_text(str(os.getpid()))
        os.write(ready_write, b"ready")
        os.close(ready_write)
        time.sleep(60)
        os._exit(0)
    os.close(ready_write)
    os.read(ready_read, 5)
    os.close(ready_read)
if mode in ("hang", "result_hang", "stdout_eof_hang", "descendant_hang", "cancel"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stderr.write("HANG_DIAGNOSTIC\n")
    sys.stderr.flush()
    emit({"type": "assistant", "message": {"id": "msg-1", "usage": {"input_tokens": 17, "output_tokens": 3}}})
    if mode == "result_hang":
        emit({"type": "result", "result": "REVIEW_TEXT", "modelUsage": {"claude-test": {"inputTokens": 17, "outputTokens": 3}}, "total_cost_usd": 0.1})
    if mode == "stdout_eof_hang":
        os.close(1)
    Path(os.environ["READY"]).touch()
    time.sleep(60)
if mode == "rate_stderr":
    sys.stderr.write("HTTP 429 Too many requests\n")
    sys.exit(1)
if mode == "rate_result":
    emit({"type": "result", "result": "You've hit your limit", "is_error": True})
    sys.exit(0)
if mode in ("rate_errors", "native_error"):
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "errors": ["rate_limit_error" if mode == "rate_errors" else "CLI_ERROR"],
          "duration_ms": 429, "usage": {"output_tokens": 429}})
    sys.exit(0)
if mode == "rate_event":
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}})
    sys.exit(0)
if mode == "rate_warning":
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}})
if mode in ("ok", "tool", "notice", "nonobject_notice", "unterminated", "stderr", "nonzero", "bad_result", "result_error", "rate_warning", "flood", "descendant"):
    emit_read_tool()
if mode == "silent":
    # Release the result only after the wrapper has emitted two heartbeats.
    deadline = time.monotonic() + 10
    while not Path(os.environ["HEARTBEAT_MARKER"]).exists():
        if time.monotonic() >= deadline:
            sys.exit(8)
        time.sleep(0.001)
    emit_read_tool()
if mode == "flood":
    for i in range(1200):
        emit({"type": "stream_event", "delta": "PRIVATE_PARTIAL" * 50})
    time.sleep(0.18)
if mode == "stderr":
    sys.stderr.write("PRIVATE_ERROR" * 100000)
    sys.stderr.flush()
if mode == "nonzero":
    sys.stderr.write("CLI_DIAGNOSTIC " * 40 + "\nPRIVATE_SECOND_LINE\n")
if mode in ("malformed", "notice"):
    print("not-json", flush=True)
if mode in ("nonobject", "nonobject_notice"):
    print("[]", flush=True)
if mode in ("malformed", "nonobject"):
    pass  # No valid result: the wrapper must still classify parsing failure.
elif mode == "missing":
    emit({"type": "assistant", "content": "PRIVATE_PARTIAL"})
elif mode == "bad_result":
    emit({"type": "result", "result": {"not": "text"}})
elif mode == "unterminated":
    sys.stdout.write(json.dumps({"type": "result", "result": "REVIEW_TEXT"}))
else:
    emit({"type": "result", "result": "REVIEW_TEXT", "is_error": mode == "result_error"})
sys.exit(7 if mode == "nonzero" else 0)
'''


class ClaudeReviewerWrapperTest(unittest.TestCase):
    def run_fake(self, mode="ok", *, stale=False, missing_prompt=False, missing_binary=False,
                 timeout_seconds=4, cancel_signal=None, model="claude-sonnet-4-6"):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            fake = binary_dir / "claude"
            if not missing_binary:
                fake.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
                fake.chmod(0o755)
            prompt = root / "s-reviewer-prompt.txt"
            if not missing_prompt:
                prompt.write_text("PRIVATE_PROMPT\n")
            result = root / "s-reviewer-result.txt"
            if stale:
                result.write_text("STALE_REVIEW")
            output = io.StringIO()
            marker = root / "heartbeat-marker"
            heartbeat_count = 0
            emit = wrapper._emit
            def acknowledge_heartbeat(payload):
                nonlocal heartbeat_count
                emit(payload)
                if payload.get("type") == "reviewer_heartbeat" and payload.get("phase") == "reviewer":
                    heartbeat_count += 1
                    if heartbeat_count == 2:
                        marker.touch()
            env = {"PATH": str(binary_dir), "FAKE_MODE": mode,
                   "CAPTURE": str(root / "capture.json"), "HEARTBEAT_MARKER": str(marker),
                   "FAKE_HELP": " ".join(REQUIRED_CLAUDE_FLAGS),
                   "DESCENDANT_PID": str(root / "descendant.pid"), "READY": str(root / "ready")}
            started = time.monotonic()
            if cancel_signal is not None:
                process = subprocess.Popen(
                    [sys.executable, str(Path(wrapper.__file__).resolve()), "--session-id", "s",
                     *( ["--model", model] if model else [] ), "--tmp-dir", str(root),
                     "--timeout-seconds", str(timeout_seconds), "--heartbeat-seconds", "0.05",
                     "--role", "code-reviewer"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env={**os.environ, **env},
                )
                try:
                    while not (root / "ready").exists() and time.monotonic() - started < 5:
                        time.sleep(0.01)
                    self.assertTrue((root / "ready").exists())
                    process.send_signal(cancel_signal)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(stderr, "")
                    output.write(stdout)
                    rc = process.returncode
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)
            else:
                with patch.dict(os.environ, env), contextlib.redirect_stdout(output), patch.object(wrapper, "_emit", acknowledge_heartbeat):
                    rc = wrapper.run_reviewer("s", model, root,
                                              heartbeat_seconds=0.05, timeout_seconds=timeout_seconds)
            self.assertLess(time.monotonic() - started, timeout_seconds + 4)
            self.assertNotIn("PRIVATE", output.getvalue())
            self.assertNotIn("REVIEW_TEXT", output.getvalue())
            lines = [json.loads(line) for line in output.getvalue().splitlines()]
            data = {path.name: path.read_text() for path in root.iterdir() if path.is_file()}
            for path in root.glob("s-reviewer-*/*"):
                if path.is_file():
                    data[path.name] = path.read_text()
            usage_file = lines[-1].get("usage_file")
            if usage_file:
                data["usage_record"] = json.loads(Path(usage_file).read_text())
            if (root / "descendant.pid").exists():
                pid = int((root / "descendant.pid").read_text())
                for _ in range(100):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                else:
                    # A reparented zombie cannot run or hold pipe descriptors.
                    state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)],
                                           capture_output=True, text=True).stdout.strip()
                    if state and not state.startswith("Z"):
                        os.kill(pid, signal.SIGKILL)
                        self.fail(f"descendant survived wrapper cleanup: {state}")
            return rc, lines, data

    def test_result_and_exact_command_and_prompt_delivery(self):
        rc, lines, data = self.run_fake()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-1]["status"], "ok")
        self.assertEqual(lines[-1]["child_exit"], 0)
        self.assertEqual(lines[-1]["tool_uses"], 1)
        self.assertTrue(lines[-1]["result_file"].endswith("s-reviewer-result.txt"))
        self.assertEqual(data["s-reviewer-result.txt"], "REVIEW_TEXT")
        self.assertEqual(json.loads(data["capture.json"]), {
            "argv": ["-p", "--no-session-persistence", "--output-format", "stream-json",
                     "--include-partial-messages", "--verbose", "--model", "claude-sonnet-4-6",
                     *claude_readonly_args()],
            "stdin": "PRIVATE_PROMPT\n",
        })
        self.assertEqual(data["s-reviewer-prompt.txt"], "PRIVATE_PROMPT\n")
        self.assertEqual(data["usage_record"]["status"], "ok")

    def test_native_tool_use_count_and_default_model(self):
        rc, lines, data = self.run_fake("tool", model="")
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-1]["tool_uses"], 1)
        argv = json.loads(data["capture.json"])["argv"]
        self.assertNotIn("--model", argv)
        self.assertIsNone(data["usage_record"]["requested_model"])

    def test_invalid_stream_makes_tool_use_count_unknown(self):
        rc, lines, _ = self.run_fake("notice")
        self.assertEqual(rc, 8)
        self.assertEqual(lines[-1]["status"], "tool_use_unverified")
        self.assertIsNone(lines[-1]["tool_uses"])

    def test_zero_tool_use_never_publishes_a_report(self):
        rc, lines, data = self.run_fake("no_tool")
        self.assertEqual(rc, 8)
        self.assertEqual(lines[-1]["status"], "tool_uses_zero")
        self.assertIsNone(lines[-1]["result_file"])
        self.assertNotIn("s-reviewer-result.txt", data)

    def test_missing_result_removes_stale_output(self):
        rc, lines, data = self.run_fake("missing", stale=True)
        self.assertEqual(rc, 3)
        self.assertEqual(lines[-1]["status"], "missing_result")
        self.assertIsNone(lines[-1]["result_file"])
        self.assertNotIn("s-reviewer-result.txt", data)

    def test_nonzero_exit_rejects_even_present_result(self):
        rc, lines, data = self.run_fake("nonzero")
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1]["status"], "command_execution")
        self.assertEqual(lines[-1]["child_exit"], 7)
        self.assertEqual(lines[-1]["stderr_head"], ("CLI_DIAGNOSTIC " * 40)[:300])
        self.assertNotIn("s-reviewer-result.txt", data)

    def test_parse_failures_without_valid_result_do_not_replay_stream(self):
        for mode in ("malformed", "nonobject", "bad_result"):
            with self.subTest(mode=mode):
                rc, lines, data = self.run_fake(mode)
                self.assertEqual(rc, 2)
                self.assertEqual(lines[-1]["status"], "json_parsing")
                self.assertEqual(lines[-1]["invalid_lines"], 1)
                self.assertNotIn("s-reviewer-result.txt", data)

    def test_valid_result_survives_invalid_lines_with_count(self):
        for mode in ("notice", "nonobject_notice"):
            with self.subTest(mode=mode):
                rc, lines, data = self.run_fake(mode)
                self.assertEqual(rc, 8)
                self.assertEqual(lines[-1]["status"], "tool_use_unverified")
                self.assertEqual(lines[-1]["invalid_lines"], 1)
                self.assertIsNone(lines[-1]["tool_uses"])
                self.assertNotIn("s-reviewer-result.txt", data)

    def test_error_result_is_command_failure(self):
        rc, lines, _ = self.run_fake("result_error")
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1]["status"], "command_execution")

    def test_final_line_without_newline(self):
        rc, _, data = self.run_fake("unterminated")
        self.assertEqual(rc, 0)
        self.assertEqual(data["s-reviewer-result.txt"], "REVIEW_TEXT")

    def test_stderr_is_spooled_without_pipe_deadlock(self):
        rc, _, data = self.run_fake("stderr")
        self.assertEqual(rc, 0)
        self.assertEqual(data["stderr.log"], "PRIVATE_ERROR" * 100000)

    def test_silent_child_still_emits_throttled_heartbeat(self):
        rc, lines, _ = self.run_fake("silent")
        self.assertEqual(rc, 0)
        beats = lines[:-1]
        silent_beats = [beat for beat in beats if beat["phase"] == "reviewer" and beat["events"] == 0]
        self.assertGreaterEqual(len(silent_beats), 2)
        for beat in beats:
            self.assertEqual(beat["type"], "reviewer_heartbeat")
        for beat in silent_beats:
            self.assertEqual(beat["last_event_type"], "none")

    def test_flood_is_fully_spooled_with_rate_limited_heartbeats(self):
        timestamps = []
        emit = wrapper._emit
        def capture(payload):
            if payload.get("type") == "reviewer_heartbeat":
                timestamps.append(wrapper.time.monotonic())
            emit(payload)
        with patch.object(wrapper, "_emit", capture):
            rc, lines, data = self.run_fake("flood")
        self.assertEqual(rc, 0)
        events = [json.loads(line) for line in data["stream.jsonl"].splitlines()]
        self.assertEqual(len(events), 1202)
        self.assertEqual(events[1]["delta"], "PRIVATE_PARTIAL" * 50)
        self.assertTrue(any(beat["events"] >= 1201 for beat in lines[:-1]))
        self.assertTrue(all(b - a >= 0.045 for a, b in zip(timestamps, timestamps[1:])))

    def test_missing_prompt_or_binary_is_command_failure(self):
        for kwargs in ({"missing_prompt": True}, {"missing_binary": True}):
            with self.subTest(kwargs=kwargs):
                rc, lines, _ = self.run_fake(**kwargs)
                self.assertEqual(rc, 1)
                self.assertEqual(lines[-1]["status"], "command_execution")

    def test_rejects_session_path_traversal(self):
        with self.assertRaises(ValueError):
            wrapper.run_reviewer("../escape", "model", ".review-loop/tmp")

    def test_cleanup_permission_error_still_reaps_process(self):
        process = Mock(pid=123)
        with patch.object(wrapper.os, "killpg", side_effect=PermissionError):
            wrapper._stop_process(process)
        self.assertEqual(process.wait.call_count, 2)
        self.assertTrue(all(call.kwargs["timeout"] > 0 for call in process.wait.call_args_list))

    def test_unsupported_capabilities_fail_before_prompt_is_sent(self):
        rc, lines, data = self.run_fake("unsafe", stale=True)
        self.assertEqual(rc, 7)
        self.assertEqual(lines[-1]["status"], "unsafe_cli")
        self.assertIn("--safe-mode", lines[-1]["failure_reason"])
        self.assertNotIn("capture.json", data)
        self.assertNotIn("s-reviewer-result.txt", data)
        self.assertEqual(data["usage_record"]["status"], "unsafe_cli")

    def test_failed_help_probe_has_distinct_phase_and_preserved_diagnostics(self):
        rc, lines, data = self.run_fake("help_nonzero", stale=True)
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1]["phase"], "capability_probe")
        self.assertEqual(lines[-1]["stderr_head"], "HELP_DIAGNOSTIC")
        self.assertEqual(data["cli-help-stderr.log"], "HELP_DIAGNOSTIC\n")
        self.assertNotIn("s-reviewer-result.txt", data)

    def test_finite_total_deadline_covers_probe_and_live_result(self):
        for mode in ("help_hang", "hang", "stdout_eof_hang", "result_hang", "descendant_hang"):
            with self.subTest(mode=mode):
                # Allow both Python startups before exercising the intended hang.
                rc, lines, data = self.run_fake(mode, stale=True, timeout_seconds=1)
                self.assertEqual(rc, 4)
                self.assertEqual(lines[-1]["status"], "timeout")
                self.assertIsNone(lines[-1]["result_file"])
                self.assertNotIn("s-reviewer-result.txt", data)
                self.assertTrue(lines[-1]["cleanup_ok"])
                self.assertEqual(data["usage_record"]["status"], "timeout")
                if mode != "help_hang":
                    self.assertEqual(data["stderr.log"], "HANG_DIAGNOSTIC\n")
                    self.assertIn('"type": "assistant"', data["stream.jsonl"])
                if mode == "result_hang":
                    self.assertIn('"type": "result"', data["stream.jsonl"])
                    self.assertTrue(any(beat["last_event_type"] == "result" for beat in lines[:-1]))

    def test_parent_exit_with_live_descendant_does_not_stall_pipe_drain(self):
        rc, lines, data = self.run_fake("descendant")
        self.assertEqual(rc, 0)
        self.assertTrue(lines[-1]["cleanup_ok"])
        self.assertEqual(data["s-reviewer-result.txt"], "REVIEW_TEXT")

    def test_sigint_sigterm_sighup_cancel_and_keep_diagnostics(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                rc, lines, data = self.run_fake("cancel", stale=True, cancel_signal=sig)
                self.assertEqual(rc, 5)
                self.assertEqual(lines[-1]["status"], "cancelled")
                self.assertEqual(lines[-1]["cancel_signal"], sig)
                self.assertNotIn("s-reviewer-result.txt", data)
                self.assertEqual(data["stderr.log"], "HANG_DIAGNOSTIC\n")
                self.assertIn('"type": "assistant"', data["stream.jsonl"])
                self.assertEqual(data["usage_record"]["status"], "cancelled")
                self.assertEqual(data["usage_record"]["role"], "code-reviewer")

    def test_rate_limit_sources_are_distinct_from_cli_and_schema_failure(self):
        for mode in ("rate_stderr", "rate_result", "rate_event", "rate_errors"):
            with self.subTest(mode=mode):
                rc, lines, data = self.run_fake(mode, stale=True)
                self.assertEqual(rc, 6)
                self.assertEqual(lines[-1]["status"], "rate_limited")
                self.assertNotIn("s-reviewer-result.txt", data)
        rc, _, _ = self.run_fake("rate_warning")
        self.assertEqual(rc, 0)

    def test_native_error_result_without_text_is_cli_failure(self):
        rc, lines, _ = self.run_fake("native_error", stale=True)
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1]["status"], "command_execution")
        self.assertEqual(lines[-1]["invalid_lines"], 0)

    def test_rejects_nonfinite_or_nonpositive_deadlines_and_heartbeats(self):
        for value in (0, -1, float("inf"), float("nan")):
            for field in ("timeout_seconds", "heartbeat_seconds"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    wrapper.run_reviewer("s", "model", "unused", **{field: value})

    def test_oversized_unterminated_lines_are_bounded_without_losing_next_result(self):
        events = wrapper._Events(Mock())
        with patch.object(wrapper, "MAX_EVENT_BYTES", 32):
            events.feed(b"x" * 40)
            self.assertEqual(events.pending, b"")
            events.feed(b"x" * 40 + b"\n")
        events.feed(b'{"type":"result","result":"ok"}\n')
        self.assertEqual(events.invalid_lines, 1)
        self.assertEqual(events.result, "ok")

    def test_cleanup_waits_remain_bounded_if_the_child_cannot_be_reaped(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("claude", 1)
        with patch.object(wrapper.os, "killpg") as kill:
            self.assertFalse(wrapper._stop_process(process))
        self.assertEqual([call.args[1] for call in kill.call_args_list], [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual([call.kwargs["timeout"] for call in process.wait.call_args_list],
                         [wrapper.TERMINATE_GRACE_SECONDS, wrapper.KILL_WAIT_SECONDS])

    def test_selector_setup_failure_still_terminates_and_reaps_the_child(self):
        with patch.object(wrapper.selectors, "DefaultSelector", side_effect=OSError):
            rc, lines, data = self.run_fake(stale=True)
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1]["failure_reason"], "stream_io_failure")
        self.assertIsNotNone(lines[-1]["child_exit"])
        self.assertTrue(lines[-1]["cleanup_ok"])
        self.assertNotIn("s-reviewer-result.txt", data)

    def test_usage_write_failure_rejects_extracted_result(self):
        with patch.object(wrapper, "record_invocation", side_effect=OSError):
            rc, lines, data = self.run_fake(stale=True)
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1]["failure_reason"], "usage_record_failed")
        self.assertNotIn("s-reviewer-result.txt", data)
        self.assertNotIn("result.txt", data)
        self.assertIn('"type": "result"', data["stream.jsonl"])

    def test_retry_keeps_previous_raw_artifacts_and_usage_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary_dir = root / "bin"
            binary_dir.mkdir()
            fake = binary_dir / "claude"
            fake.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
            fake.chmod(0o755)
            prompt = root / "s-reviewer-prompt.txt"
            env = {"PATH": str(binary_dir), "FAKE_MODE": "ok",
                   "CAPTURE": str(root / "capture.json"), "FAKE_HELP": " ".join(REQUIRED_CLAUDE_FLAGS)}
            summaries = []
            prompt.write_text("FIRST_PRIVATE_PROMPT")
            for mode in ("ok", "nonzero"):
                output = io.StringIO()
                with patch.dict(os.environ, {**env, "FAKE_MODE": mode}), contextlib.redirect_stdout(output):
                    wrapper.run_reviewer("s", "model", root, stage="planning", role="go-reviewer", timeout_seconds=4)
                summaries.append(json.loads(output.getvalue().splitlines()[-1]))
                if mode == "ok":
                    first_usage = Path(summaries[-1]["usage_file"]).read_bytes()
                    first_stream = Path(summaries[-1]["stream_file"]).read_bytes()
                prompt.write_text("SECOND_PRIVATE_PROMPT")
            self.assertNotEqual(summaries[0]["invocation_id"], summaries[1]["invocation_id"])
            self.assertEqual(Path(summaries[0]["usage_file"]).read_bytes(), first_usage)
            self.assertEqual(Path(summaries[0]["stream_file"]).read_bytes(), first_stream)
            self.assertEqual((Path(summaries[0]["stream_file"]).parent / "prompt.txt").read_text(),
                             "FIRST_PRIVATE_PROMPT")
            self.assertEqual(json.loads(first_usage)["stage"], "planning")
            self.assertEqual(json.loads(first_usage)["role"], "go-reviewer")
            self.assertEqual(len(list((root / "usage").glob("*.json"))), 2)
            self.assertFalse((root / "s-reviewer-result.txt").exists())


if __name__ == "__main__":
    unittest.main()
