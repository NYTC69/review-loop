"""Transport tests use a fake Claude executable, never a model invocation."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_claude_reviewer as wrapper


FAKE_CLAUDE = r'''
import json, os, sys, time
from pathlib import Path
Path(os.environ["CAPTURE"]).write_text(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}))
mode = os.environ["FAKE_MODE"]
def emit(event):
    print(json.dumps(event), flush=True)
if mode == "silent":
    # Release the result only after the wrapper has emitted two heartbeats.
    deadline = time.monotonic() + 10
    while not Path(os.environ["HEARTBEAT_MARKER"]).exists():
        if time.monotonic() >= deadline:
            sys.exit(8)
        time.sleep(0.001)
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
    def run_fake(self, mode="ok", *, stale=False, missing_prompt=False, missing_binary=False):
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
                if payload.get("type") == "reviewer_heartbeat":
                    heartbeat_count += 1
                    if heartbeat_count == 2:
                        marker.touch()
            env = {"PATH": str(binary_dir), "FAKE_MODE": mode,
                   "CAPTURE": str(root / "capture.json"), "HEARTBEAT_MARKER": str(marker)}
            with patch.dict(os.environ, env), contextlib.redirect_stdout(output), patch.object(wrapper, "_emit", acknowledge_heartbeat):
                rc = wrapper.run_reviewer("s", "claude-sonnet-4-6", root, heartbeat_seconds=0.05)
            self.assertNotIn("PRIVATE", output.getvalue())
            self.assertNotIn("REVIEW_TEXT", output.getvalue())
            lines = [json.loads(line) for line in output.getvalue().splitlines()]
            data = {path.name: path.read_text() for path in root.iterdir() if path.is_file()}
            return rc, lines, data

    def test_result_and_exact_command_and_prompt_delivery(self):
        rc, lines, data = self.run_fake()
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-1]["status"], "ok")
        self.assertEqual(lines[-1]["child_exit"], 0)
        self.assertTrue(lines[-1]["result_file"].endswith("s-reviewer-result.txt"))
        self.assertEqual(data["s-reviewer-result.txt"], "REVIEW_TEXT")
        self.assertEqual(json.loads(data["capture.json"]), {
            "argv": ["-p", "--no-session-persistence", "--output-format", "stream-json",
                     "--include-partial-messages", "--verbose", "--model", "claude-sonnet-4-6"],
            "stdin": "PRIVATE_PROMPT\n",
        })
        self.assertEqual(data["s-reviewer-prompt.txt"], "PRIVATE_PROMPT\n")

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
                self.assertEqual(rc, 0)
                self.assertEqual(lines[-1]["status"], "ok")
                self.assertEqual(lines[-1]["invalid_lines"], 1)
                self.assertEqual(data["s-reviewer-result.txt"], "REVIEW_TEXT")

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
        self.assertEqual(data["s-reviewer-stderr.log"], "PRIVATE_ERROR" * 100000)

    def test_silent_child_still_emits_throttled_heartbeat(self):
        rc, lines, _ = self.run_fake("silent")
        self.assertEqual(rc, 0)
        beats = lines[:-1]
        self.assertGreaterEqual(len(beats), 2)
        for beat in beats:
            self.assertEqual(beat["type"], "reviewer_heartbeat")
            self.assertEqual(beat["events"], 0)
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
        events = [json.loads(line) for line in data["s-reviewer-stream.jsonl"].splitlines()]
        self.assertEqual(len(events), 1201)
        self.assertEqual(events[0]["delta"], "PRIVATE_PARTIAL" * 50)
        self.assertTrue(any(beat["events"] == 1200 for beat in lines[:-1]))
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
        process.wait.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
