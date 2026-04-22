import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_skill_smoke_lib import cleanup_timed_out_process, finalize_stream_capture_artifact, select_primary_session_path


def write_session(root: Path, session_id: str, entry_point: str) -> Path:
    session_path = root / ".review-loop" / "sessions" / f"{session_id}.md"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        "## Session Metadata\n"
        f"- entry_point: {entry_point}\n",
        encoding="utf-8",
    )
    return session_path.resolve()


class SelectPrimarySessionPathTest(unittest.TestCase):
    def test_prefers_review_loop_session_over_later_stdout_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            umbrella = write_session(root, "11111111-1111-1111-1111-111111111111", "review-loop")
            downstream = write_session(root, "22222222-2222-2222-2222-222222222222", "execute-from-plan")
            stdout = "\n".join(
                [
                    f"umbrella: .review-loop/sessions/{umbrella.name}",
                    f"downstream: .review-loop/sessions/{downstream.name}",
                ]
            )

            selected = select_primary_session_path(
                stdout=stdout,
                root=root,
                before_sessions=set(),
                after_sessions={umbrella, downstream},
            )

            self.assertEqual(selected, umbrella)

    def test_fallback_prefers_review_loop_session_among_new_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            downstream = write_session(root, "33333333-3333-3333-3333-333333333333", "execute-from-plan")
            umbrella = write_session(root, "44444444-4444-4444-4444-444444444444", "review-loop")

            selected = select_primary_session_path(
                stdout="",
                root=root,
                before_sessions=set(),
                after_sessions={downstream, umbrella},
            )

            self.assertEqual(selected, umbrella)


class FinalizeStreamCaptureArtifactTest(unittest.TestCase):
    def test_writes_tool_events_from_partial_stream_without_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifact_path = root / "tool-use-events.json"
            text_path = root / "stdout.txt"
            artifact_path.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Read",
                                    "input": {"file_path": "docs/protocol/execution.md"},
                                }
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            finalized = finalize_stream_capture_artifact(artifact_path, text_path)

            self.assertTrue(finalized)
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["events"], [{"tool": "Read", "target": "docs/protocol/execution.md"}])
            self.assertIn("no 'type=result' event observed", payload["schema_errors"][0])
            self.assertEqual(text_path.read_text(encoding="utf-8"), "")


def timeout_cleanup_worker(pid_path: str, queue: multiprocessing.Queue) -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import pathlib, subprocess, sys, time\n"
            "pid_path = pathlib.Path(sys.argv[1])\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
            "    start_new_session=True,\n"
            "    stdout=sys.stdout,\n"
            "    stderr=sys.stderr,\n"
            ")\n"
            "pid_path.write_text(str(child.pid), encoding='utf-8')\n"
            "time.sleep(30)\n"
        ),
        pid_path,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        process.communicate(timeout=1)
        queue.put({"status": "unexpected-pass"})
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = cleanup_timed_out_process(process, exc, terminate_grace_seconds=1)
        queue.put(
            {
                "status": "timeout-cleaned",
                "stdout": stdout,
                "stderr": stderr,
                "returncode": process.returncode,
            }
        )


class CleanupTimedOutProcessTest(unittest.TestCase):
    def test_returns_after_timeout_when_detached_descendant_keeps_pipe_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "detached.pid"
            queue: multiprocessing.Queue = multiprocessing.Queue()
            worker = multiprocessing.Process(target=timeout_cleanup_worker, args=(str(pid_path), queue))
            worker.start()
            worker.join(timeout=5)

            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=1)
                self.fail("cleanup_timed_out_process hung while pipes were still held by a detached descendant")

            try:
                result = queue.get(timeout=1)
            finally:
                if pid_path.exists():
                    try:
                        os.kill(int(pid_path.read_text(encoding="utf-8")), signal.SIGKILL)
                    except (OSError, ValueError):
                        pass

            self.assertEqual(result["status"], "timeout-cleaned")
            self.assertIsInstance(result["stdout"], str)
            self.assertIsInstance(result["stderr"], str)
            self.assertIsNotNone(result["returncode"])


class RunSkillSmokeTimeoutRegressionTest(unittest.TestCase):
    def test_best_effort_timeout_uses_partial_stdout_to_salvage_session_artifacts(self):
        case_id = "zz.timeout.partial-stdout-session-salvage"
        session_id = "88888888-8888-4888-8888-888888888888"
        case_path = ROOT / "tests/skills/smoke" / f"{case_id}.json"
        artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
        session_path = ROOT / ".review-loop/sessions" / f"{session_id}.md"
        prompt_path = ROOT / ".review-loop/tmp" / f"{session_id}-reviewer-prompt.txt"
        smoke_uuid_marker = ROOT / ".review-loop/tmp/smoke-session-uuid"
        last_run_path = ROOT / "tests/skills/.last-run.json"

        for path in (case_path, prompt_path, smoke_uuid_marker):
            if path.exists():
                path.unlink()
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(
            "## Session Metadata\n"
            "- entry_point: review-loop\n"
            "- terminal: deterministic\n",
            encoding="utf-8",
        )

        case_data = {
            "id": case_id,
            "type": "smoke",
            "target": "review-loop",
            "runtime": "shared",
            "requires": ["python3"],
            "setup": {"timeout_seconds": 1},
            "execution_policy": "best_effort",
            "artifacts": {
                "capture": {
                    "session_path": "latest_session",
                    "session_final": "latest_session",
                    "reviewer_prompt": "reviewer_prompt_file",
                },
                "required": [
                    "session_path",
                    "session_final",
                    "reviewer_prompt",
                    "assertions",
                    "meta",
                ],
            },
            "command": [
                "python3",
                "-c",
                (
                    "import pathlib, sys, time\n"
                    f"session_id = {session_id!r}\n"
                    "root = pathlib.Path(sys.argv[1])\n"
                    "tmp = root / '.review-loop' / 'tmp'\n"
                    "tmp.mkdir(parents=True, exist_ok=True)\n"
                    "(tmp / f'{session_id}-reviewer-prompt.txt').write_text('reviewer prompt\\n', encoding='utf-8')\n"
                    "print(f'.review-loop/sessions/{session_id}.md', flush=True)\n"
                    "time.sleep(30)\n"
                ),
                "__WORKTREE__",
            ],
            "assertions": ["session_created", "reviewer_prompt_exists"],
        }

        try:
            case_path.write_text(json.dumps(case_data, indent=2) + "\n", encoding="utf-8")

            run = subprocess.run(
                ["bash", "scripts/run-skill-smoke", "--case", case_id],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn(f"PASS {case_id} - assertions passed after timeout cleanup", run.stdout)

            payload = json.loads(last_run_path.read_text(encoding="utf-8"))
            record = next(candidate for candidate in payload["results"] if candidate.get("id") == case_id)
            self.assertEqual(record["status"], "pass")
            self.assertEqual(record["reason"], "assertions passed after timeout cleanup")

            artifacts = record["artifacts"]
            for key in ("session_path", "session_final", "reviewer_prompt", "assertions", "meta", "stdout"):
                self.assertIn(key, artifacts)
                self.assertTrue((ROOT / artifacts[key]).exists(), key)

            self.assertEqual(
                (ROOT / artifacts["session_path"]).read_text(encoding="utf-8").strip(),
                f".review-loop/sessions/{session_id}.md",
            )
            self.assertIn("- terminal: deterministic", (ROOT / artifacts["session_final"]).read_text(encoding="utf-8"))
            self.assertEqual((ROOT / artifacts["stdout"]).read_text(encoding="utf-8"), f".review-loop/sessions/{session_id}.md\n")

            assertion_payload = json.loads((ROOT / artifacts["assertions"]).read_text(encoding="utf-8"))
            self.assertEqual([entry["status"] for entry in assertion_payload], ["pass", "pass"])
        finally:
            if case_path.exists():
                case_path.unlink()
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)
            for path in (session_path, prompt_path, smoke_uuid_marker):
                if path.exists():
                    path.unlink()

    def test_best_effort_timeout_promotes_to_pass_when_assertions_salvage(self):
        case_id = "zz.timeout.best-effort-salvage"
        session_id = "99999999-9999-4999-8999-999999999999"
        case_path = ROOT / "tests/skills/smoke" / f"{case_id}.json"
        artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
        session_path = ROOT / ".review-loop/sessions" / f"{session_id}.md"
        prompt_path = ROOT / ".review-loop/tmp" / f"{session_id}-reviewer-prompt.txt"
        smoke_uuid_marker = ROOT / ".review-loop/tmp/smoke-session-uuid"
        last_run_path = ROOT / "tests/skills/.last-run.json"

        for path in (case_path, session_path, prompt_path, smoke_uuid_marker):
            if path.exists():
                path.unlink()
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)

        case_data = {
            "id": case_id,
            "type": "smoke",
            "target": "review-loop",
            "runtime": "shared",
            "requires": ["python3"],
            "setup": {"timeout_seconds": 1},
            "execution_policy": "best_effort",
            "artifacts": {
                "capture": {
                    "session_path": "latest_session",
                    "session_final": "latest_session",
                    "reviewer_prompt": "reviewer_prompt_file",
                },
                "required": [
                    "session_path",
                    "session_final",
                    "reviewer_prompt",
                    "assertions",
                    "meta",
                ],
            },
            "command": [
                "python3",
                "-c",
                (
                    "import pathlib, sys, time\n"
                    f"session_id = {session_id!r}\n"
                    "root = pathlib.Path(sys.argv[1])\n"
                    "sessions = root / '.review-loop' / 'sessions'\n"
                    "tmp = root / '.review-loop' / 'tmp'\n"
                    "sessions.mkdir(parents=True, exist_ok=True)\n"
                    "tmp.mkdir(parents=True, exist_ok=True)\n"
                    "session_path = sessions / f'{session_id}.md'\n"
                    "session_path.write_text('## Session Metadata\\n- entry_point: review-loop\\n', encoding='utf-8')\n"
                    "(tmp / 'smoke-session-uuid').write_text(session_id, encoding='utf-8')\n"
                    "(tmp / f'{session_id}-reviewer-prompt.txt').write_text('reviewer prompt\\n', encoding='utf-8')\n"
                    "print(f'.review-loop/sessions/{session_id}.md', flush=True)\n"
                    "time.sleep(30)\n"
                ),
                "__WORKTREE__",
            ],
            "assertions": ["session_created", "reviewer_prompt_exists"],
        }

        try:
            case_path.write_text(json.dumps(case_data, indent=2) + "\n", encoding="utf-8")

            first_run = subprocess.run(
                ["bash", "scripts/run-skill-smoke", "--case", case_id],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first_run.returncode, 0, first_run.stdout + first_run.stderr)
            self.assertIn(f"PASS {case_id} - assertions passed after timeout cleanup", first_run.stdout)

            record = None
            payload = json.loads(last_run_path.read_text(encoding="utf-8"))
            for candidate in payload["results"]:
                if candidate.get("id") == case_id:
                    record = candidate
                    break
            self.assertIsNotNone(record)
            self.assertEqual(record["status"], "pass")
            self.assertEqual(record["reason"], "assertions passed after timeout cleanup")

            artifacts = record["artifacts"]
            for key in ("session_path", "session_final", "reviewer_prompt", "assertions", "meta"):
                self.assertIn(key, artifacts)
                self.assertTrue((ROOT / artifacts[key]).exists(), key)

            assertion_payload = json.loads((ROOT / artifacts["assertions"]).read_text(encoding="utf-8"))
            self.assertEqual(
                [entry["status"] for entry in assertion_payload],
                ["pass", "pass"],
            )
        finally:
            if case_path.exists():
                case_path.unlink()
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)
            for path in (session_path, prompt_path, smoke_uuid_marker):
                if path.exists():
                    path.unlink()


if __name__ == "__main__":
    unittest.main()
