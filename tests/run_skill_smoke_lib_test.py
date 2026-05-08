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

from scripts.run_skill_smoke_lib import (
    cleanup_stale_review_loop_runtime,
    cleanup_timed_out_process,
    finalize_stream_capture_artifact,
    select_primary_session_path,
)


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

    def test_atomic_rename_isolates_finalized_payload_from_surviving_writer(self):
        # Reproduces the FD-seek artifact corruption seen in
        # tests/skills/.artifacts/execute.stop-after-before-polish.smoke.claude
        # on 2026-04-27: a detached descendant survives SIGKILL and keeps
        # writing to the inherited stdout FD past the byte at which finalize
        # truncates the artifact, leaving a normalized-json + 0x00 gap +
        # raw-stream-tail file that strict json.load cannot parse.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifact_path = root / "tool-use-events.json"
            text_path = root / "stdout.txt"

            raw_payload = (
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Read",
                                    "input": {"file_path": "docs/protocol/planning.md"},
                                }
                            ]
                        },
                    }
                )
                + "\n"
                + "x" * 8000
            )
            artifact_path.write_text(raw_payload, encoding="utf-8")
            original_inode = artifact_path.stat().st_ino

            survivor_fd = os.open(artifact_path, os.O_WRONLY)
            os.lseek(survivor_fd, 7000, os.SEEK_SET)
            try:
                finalized = finalize_stream_capture_artifact(artifact_path, text_path)
                self.assertTrue(finalized)
                os.write(survivor_fd, b"GARBAGE-FROM-SURVIVING-DESCENDANT")
            finally:
                os.close(survivor_fd)

            final_text = artifact_path.read_text(encoding="utf-8")
            payload = json.loads(final_text)
            self.assertEqual(
                payload["events"],
                [{"tool": "Read", "target": "docs/protocol/planning.md"}],
            )
            self.assertNotIn("\x00", final_text)
            self.assertNotIn("GARBAGE-FROM-SURVIVING-DESCENDANT", final_text)
            self.assertNotEqual(
                artifact_path.stat().st_ino,
                original_inode,
                "atomic rename should produce a new inode so the surviving FD writes to the orphaned file",
            )

    def test_writes_agent_subagent_type_events_from_stream(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifact_path = root / "tool-use-events.json"
            text_path = root / "stdout.txt"
            artifact_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "name": "Agent",
                                            "input": {"subagent_type": "review-loop:executor"},
                                        },
                                        {
                                            "type": "tool_use",
                                            "name": "Task",
                                            "input": {"subagent_type": "general-purpose"},
                                        },
                                    ]
                                },
                            }
                        ),
                        json.dumps({"type": "result", "subtype": "success", "result": "ok"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            finalized = finalize_stream_capture_artifact(artifact_path, text_path)

            self.assertTrue(finalized)
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload["events"],
                [
                    {"tool": "Agent", "subagent_type": "review-loop:executor"},
                    {"tool": "Task", "subagent_type": "general-purpose"},
                ],
            )
            self.assertEqual(text_path.read_text(encoding="utf-8"), "ok")


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


class CleanupStaleReviewLoopRuntimeTest(unittest.TestCase):
    def test_removes_stale_locks_and_orphaned_reviewer_prompts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sessions_dir = root / ".review-loop" / "sessions"
            tmp_dir = root / ".review-loop" / "tmp"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            tmp_dir.mkdir(parents=True, exist_ok=True)

            stale_lock = sessions_dir / "11111111-1111-4111-8111-111111111111.lock"
            stale_lock.write_text(
                json.dumps(
                    {
                        "pid": 999999,
                        "started_at": "2026-04-22T00:00:00Z",
                        "entry_point": "review-loop",
                        "stop_after": "delivery",
                    }
                ),
                encoding="utf-8",
            )
            stale_prompt = tmp_dir / "11111111-1111-4111-8111-111111111111-reviewer-prompt.txt"
            stale_prompt.write_text("stale\n", encoding="utf-8")

            live_session_id = "22222222-2222-4222-8222-222222222222"
            live_lock = sessions_dir / f"{live_session_id}.lock"
            live_lock.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "started_at": "2026-04-22T00:00:00Z",
                        "entry_point": "review-loop",
                        "stop_after": "delivery",
                    }
                ),
                encoding="utf-8",
            )
            live_prompt = tmp_dir / f"{live_session_id}-reviewer-prompt.txt"
            live_prompt.write_text("live\n", encoding="utf-8")

            malformed_lock = sessions_dir / "33333333-3333-4333-8333-333333333333.lock"
            malformed_lock.write_text("", encoding="utf-8")
            malformed_prompt = tmp_dir / "33333333-3333-4333-8333-333333333333-reviewer-prompt.txt"
            malformed_prompt.write_text("malformed\n", encoding="utf-8")

            summary = cleanup_stale_review_loop_runtime(root)

            self.assertFalse(stale_lock.exists())
            self.assertFalse(stale_prompt.exists())
            self.assertFalse(malformed_lock.exists())
            self.assertFalse(malformed_prompt.exists())
            self.assertTrue(live_lock.exists())
            self.assertTrue(live_prompt.exists())

            removed_lock_names = {path.name for path in summary["removed_locks"]}
            removed_prompt_names = {path.name for path in summary["removed_prompts"]}
            self.assertEqual(
                removed_lock_names,
                {
                    "11111111-1111-4111-8111-111111111111.lock",
                    "33333333-3333-4333-8333-333333333333.lock",
                },
            )
            self.assertEqual(
                removed_prompt_names,
                {
                    "11111111-1111-4111-8111-111111111111-reviewer-prompt.txt",
                    "33333333-3333-4333-8333-333333333333-reviewer-prompt.txt",
                },
            )


class RunSkillSmokeForbiddenSubagentTypeAssertionTest(unittest.TestCase):
    def _write_fake_claude(self, bin_dir: Path, stream_lines: list[dict]) -> None:
        script_path = bin_dir / "claude"
        payload = "\n".join(json.dumps(line) for line in stream_lines)
        script_path.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    "cat >/dev/null",
                    f"printf '%s\\n' '{payload}'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script_path.chmod(0o755)

    def _run_case_with_fake_claude(self, case_id: str, stream_lines: list[dict]):
        case_path = ROOT / "tests/skills/smoke" / f"{case_id}.json"
        artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
        last_run_path = ROOT / "tests/skills/.last-run.json"
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir)
            self._write_fake_claude(fake_bin, stream_lines)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
            case_data = {
                "id": case_id,
                "type": "smoke",
                "target": "review-loop",
                "runtime": "claude",
                "requires": ["claude"],
                "setup": {"timeout_seconds": 10},
                "execution_policy": "strict",
                "artifacts": {
                    "capture": {
                        "tool_use_events": "stream_json_read_events",
                    },
                    "required": [
                        "tool_use_events",
                        "assertions",
                        "meta",
                    ],
                },
                "command": [
                    "claude",
                    "-p",
                    "--no-session-persistence",
                    "--",
                    "Emit one synthetic Agent call for smoke-runner testing.",
                ],
                "assertions": [
                    "no_forbidden_review_loop_subagent_types_in_agent_calls",
                ],
            }

            try:
                case_path.write_text(json.dumps(case_data, indent=2) + "\n", encoding="utf-8")
                completed = subprocess.run(
                    ["bash", "scripts/run-skill-smoke", "--case", case_id],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                payload = json.loads(last_run_path.read_text(encoding="utf-8"))
                record = next(candidate for candidate in payload["results"] if candidate.get("id") == case_id)
                return completed, record
            finally:
                if case_path.exists():
                    case_path.unlink()
                if artifact_dir.exists():
                    shutil.rmtree(artifact_dir)

    def test_fails_when_agent_call_uses_review_loop_subagent_type(self):
        case_id = "zz.tool-use-events.forbidden-subagent-type"
        completed, record = self._run_case_with_fake_claude(
            case_id,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "input": {"subagent_type": "review-loop:executor"},
                            }
                        ]
                    },
                },
                {"type": "result", "subtype": "success", "result": "ok"},
            ],
        )

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("FAIL zz.tool-use-events.forbidden-subagent-type", completed.stdout)
        self.assertEqual(record["status"], "fail")
        self.assertIn("review-loop:executor", record["reason"])

    def test_passes_when_agent_calls_stay_on_general_purpose(self):
        case_id = "zz.tool-use-events.allowed-subagent-type"
        completed, record = self._run_case_with_fake_claude(
            case_id,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "input": {"subagent_type": "general-purpose"},
                            }
                        ]
                    },
                },
                {"type": "result", "subtype": "success", "result": "ok"},
            ],
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS zz.tool-use-events.allowed-subagent-type", completed.stdout)
        self.assertEqual(record["status"], "pass")
        self.assertIn("assertions passed", record["reason"])

    def test_passes_on_partial_stream_when_captured_events_satisfy_assertion(self):
        # Reproduces the 2026-04-27 main-worktree symptom: outer claude -p
        # exceeded the per-case 120s timeout before streaming `type=result`,
        # so the grader rejected `no_forbidden_review_loop_subagent_types_in_agent_calls`
        # on schema-drift grounds even though the captured Agent calls were
        # all on `general-purpose`. With the fall-back, content satisfaction
        # should pass.
        case_id = "zz.tool-use-events.partial-stream-allowed"
        completed, record = self._run_case_with_fake_claude(
            case_id,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "input": {"subagent_type": "general-purpose"},
                            }
                        ]
                    },
                },
            ],
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn(f"PASS {case_id}", completed.stdout)
        self.assertEqual(record["status"], "pass")

    def test_fails_on_partial_stream_when_captured_events_contradict_assertion(self):
        # Partial stream must not silence an actual contract violation:
        # if the captured events already include a forbidden subagent_type,
        # the assertion still fails even when the stream lacks `type=result`.
        case_id = "zz.tool-use-events.partial-stream-forbidden"
        completed, record = self._run_case_with_fake_claude(
            case_id,
            [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "input": {"subagent_type": "review-loop:executor"},
                            }
                        ]
                    },
                },
            ],
        )

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn(f"FAIL {case_id}", completed.stdout)
        self.assertEqual(record["status"], "fail")
        self.assertIn("review-loop:executor", record["reason"])


class RunSkillSmokeTimeoutRegressionTest(unittest.TestCase):
    def test_best_effort_nonzero_monthly_usage_limit_is_environment_skip(self):
        case_id = "zz.best-effort.monthly-usage-limit"
        case_path = ROOT / "tests/skills/smoke" / f"{case_id}.json"
        artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
        last_run_path = ROOT / "tests/skills/.last-run.json"

        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)

        case_data = {
            "id": case_id,
            "type": "smoke",
            "target": "review-loop",
            "runtime": "claude",
            "requires": ["python3"],
            "setup": {"timeout_seconds": 10},
            "execution_policy": "best_effort",
            "artifacts": {
                "capture": {
                    "response": "stdout",
                },
                "required": ["response", "assertions", "meta"],
            },
            "command": [
                "python3",
                "-c",
                "print(\"You've hit your org's monthly usage limit\"); raise SystemExit(1)",
            ],
            "assertions": ["shared_config_path_mentioned"],
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
            self.assertIn(f"SKIP {case_id} - best-effort smoke hit environment limitation", run.stdout)

            payload = json.loads(last_run_path.read_text(encoding="utf-8"))
            record = next(candidate for candidate in payload["results"] if candidate.get("id") == case_id)
            self.assertEqual(record["status"], "skip")
            self.assertIn("monthly usage limit", record["reason"])
        finally:
            if case_path.exists():
                case_path.unlink()
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)

    def test_guide_stage1_semantic_paraphrases_satisfy_smoke_assertions(self):
        case_id = "zz.guide.semantic-paraphrase"
        case_path = ROOT / "tests/skills/smoke" / f"{case_id}.json"
        artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
        last_run_path = ROOT / "tests/skills/.last-run.json"
        response = (
            "Codex Stage 1 `review-loop` mirrors the broad Claude Code workflow.\n"
            "Default review stays on the Claude CLI path.\n"
            "Use local Codex review only when explicitly configured.\n"
            "The lifecycle is:\n\n"
            "```text\nexec -> polish -> docs -> security -> delivery\n```\n\n"
            "Codex Stage 1 supports clean stop points at:\n"
            "- `before-polish`\n- `before-docs`\n- `before-security`\n\n"
            "`quality_focus` only matters when Step 3.5 Quality Polish actually runs.\n"
            "`skip_quality_polish: true` does not skip the rest of the lifecycle. "
            "It records `polish` as a no-op completion, then continues through docs and security.\n"
            "Codex Stage 1 uses one orchestrator-owned workspace.\n"
        )

        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)

        case_data = {
            "id": case_id,
            "type": "smoke",
            "target": "guide",
            "runtime": "shared",
            "requires": ["python3"],
            "setup": {"timeout_seconds": 10},
            "artifacts": {
                "capture": {"response": "stdout"},
                "required": ["response", "assertions", "meta"],
            },
            "command": ["python3", "-c", f"print({response!r})"],
            "assertions": [
                "codex_stage1_downstream_lifecycle_mentioned",
                "codex_stage1_downstream_stop_points_mentioned",
                "reviewer_backend_behavior_mentioned",
                "quality_focus_real_behavior_mentioned",
                "skip_quality_polish_real_behavior_mentioned",
                "workspace_authority_mentioned",
            ],
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
            self.assertIn(f"PASS {case_id} - ", run.stdout)

            payload = json.loads(last_run_path.read_text(encoding="utf-8"))
            record = next(candidate for candidate in payload["results"] if candidate.get("id") == case_id)
            self.assertEqual(record["status"], "pass")
        finally:
            if case_path.exists():
                case_path.unlink()
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)

    def test_plan_fresh_single_round_smoke_does_not_require_reviewer_approval(self):
        case_path = ROOT / "tests/skills/smoke/plan.fresh.smoke.claude.json"
        case_data = json.loads(case_path.read_text(encoding="utf-8"))

        self.assertIn("planning_round_recorded", case_data["assertions"])
        self.assertIn("reviewer_prompt_exists", case_data["assertions"])
        self.assertNotIn("plan_source_reviewer_approved", case_data["assertions"])
        self.assertLessEqual(case_data["setup"]["timeout_seconds"], 120)

    def test_live_best_effort_smoke_cases_use_short_timeout_budget(self):
        # Per ADR-4 (Branch B), each fixture's timeout_seconds is locked to a
        # tier-specific value: control fixtures stay at 120s, NEAR_DISPATCH-tier
        # truncating fixtures use 240s, IN_DOC_RECON-tier use 360s, and the
        # full-pipeline regression fixture uses 600s. review-loop infrastructure
        # cases stay on the legacy 120s budget (out of P3-2 scope).
        expected_timeout_by_fixture = {
            # Control fixture (no B3 override; inherits shared min: 1)
            "plan.fresh.smoke.claude.json": 120,
            # NEAR_DISPATCH tier (B3 override min: 1, ADR-4 tier value 240s)
            "execute.session-resume.smoke.claude.json": 240,
            "execute.stop-after-before-security.smoke.claude.json": 240,
            "execute.stop-after-polish.smoke.claude.json": 240,
            # IN_DOC_RECON tier (B3 override min: 1, ADR-4 tier value 360s)
            "execute.from-plan.smoke.claude.json": 360,
            "execute.review-only.smoke.claude.json": 360,
            "execute.stop-after-before-polish.smoke.claude.json": 360,
            # full-pipeline tier (B3 override min: 1, ADR-4 tier value 600s)
            "review-loop.regression.smoke.claude.json": 600,
            # review-loop infrastructure cases (kept on legacy 120s; not in P3-2 scope)
            "review-loop.noop.claude-default.json": 120,
            "review-loop.noop.codex-fallback.json": 120,
            "review-loop.review-only.codex-fallback.json": 120,
            "review-loop.skip-quality-polish.codex-fallback.json": 120,
        }

        smoke_dir = ROOT / "tests/skills/smoke"
        live_cases = []
        for case_path in sorted(smoke_dir.glob("*.smoke.claude.json")):
            live_cases.append(case_path)
        live_cases.extend(
            smoke_dir / name
            for name in [
                "review-loop.noop.claude-default.json",
                "review-loop.noop.codex-fallback.json",
                "review-loop.review-only.codex-fallback.json",
                "review-loop.skip-quality-polish.codex-fallback.json",
            ]
        )

        for case_path in live_cases:
            with self.subTest(case=case_path.name):
                case_data = json.loads(case_path.read_text(encoding="utf-8"))
                self.assertEqual(case_data.get("execution_policy"), "best_effort")
                self.assertIn(
                    case_path.name, expected_timeout_by_fixture,
                    f"{case_path.name}: no expected timeout pinned; update "
                    f"expected_timeout_by_fixture to cover this fixture per ADR-4"
                )
                self.assertEqual(
                    case_data["setup"]["timeout_seconds"],
                    expected_timeout_by_fixture[case_path.name],
                    f"{case_path.name}: timeout_seconds drift from ADR-4 tier value"
                )

    def test_live_best_effort_smoke_cases_b3_override_min_is_one(self):
        """AC-6 invariant: post-v2.6.32, no live `*.smoke.claude.json` may carry
        a B3 (`agent_calls_used_at_least_one_subagent`) override with `min: 0`.
        Either the fixture has no override on B3 (inherits shared `min: 1` from
        assertion-mapping.json) OR it carries an explicit override with `min: 1`.
        Per ADR-4 (Branch B), all 7 truncating fixtures carry the explicit
        override; plan.fresh inherits the shared default."""
        smoke_dir = ROOT / "tests/skills/smoke"
        for case_path in sorted(smoke_dir.glob("*.smoke.claude.json")):
            with self.subTest(case=case_path.name):
                case_data = json.loads(case_path.read_text(encoding="utf-8"))
                for assertion in case_data.get("assertions", []):
                    if (
                        isinstance(assertion, dict)
                        and assertion.get("id") == "agent_calls_used_at_least_one_subagent"
                    ):
                        overrides = assertion.get("overrides", {})
                        if "min" in overrides:
                            self.assertEqual(
                                overrides["min"], 1,
                                f"{case_path.name}: B3 override has min={overrides['min']}, "
                                f"expected 1 per AC-6/ADR-4 invariant"
                            )

    def test_review_only_execution_round_heading_counts_as_reviewer_round(self):
        case_id = "zz.review-only.execution-round-heading"
        session_id = "99999999-9999-4999-8999-999999999999"
        case_path = ROOT / "tests/skills/smoke" / f"{case_id}.json"
        artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
        session_path = ROOT / ".review-loop/sessions" / f"{session_id}.md"
        smoke_uuid_marker = ROOT / ".review-loop/tmp/smoke-session-uuid"
        last_run_path = ROOT / "tests/skills/.last-run.json"

        for path in (case_path, smoke_uuid_marker):
            if path.exists():
                path.unlink()
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(
            "## Approved Plan\n\n- Source: review-only\n\n"
            "(none — review-only mode)\n\n"
            "Scope: see `## Review Target` section below.\n\n"
            "## Review Target\n\nREVIEW_TARGET_SMOKE_TOKEN\n\n"
            "## Review History\n\n"
            "### Execution Round 1 (review-only)\n"
            "- Executor backend: skipped (review-only first round)\n"
            "- Reviewer backend: codex\n"
            "- Reviewer verdict: APPROVE\n\n"
            "## Session Metadata\n"
            "- entry_point: review-loop\n"
            "- completed_stages: [exec, polish, docs, security]\n"
            "- delivery_blocked_by: null\n",
            encoding="utf-8",
        )

        case_data = {
            "id": case_id,
            "type": "smoke",
            "target": "review-loop",
            "runtime": "shared",
            "requires": ["python3"],
            "setup": {"timeout_seconds": 10},
            "artifacts": {
                "capture": {
                    "session_path": "latest_session",
                    "session_final": "latest_session",
                },
                "required": ["session_path", "session_final", "assertions", "meta"],
            },
            "command": [
                "python3",
                "-c",
                (
                    "import pathlib, sys\n"
                    "root = pathlib.Path(sys.argv[1])\n"
                    "tmp = root / '.review-loop' / 'tmp'\n"
                    "tmp.mkdir(parents=True, exist_ok=True)\n"
                    f"(tmp / 'smoke-session-uuid').write_text({session_id!r}, encoding='utf-8')\n"
                    f"print('.review-loop/sessions/{session_id}.md')\n"
                ),
                "__WORKTREE__",
            ],
            "assertions": ["execute_round_1_reviewer_only", "reviewer_backend_codex"],
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
            self.assertIn(f"PASS {case_id} - ", run.stdout)
        finally:
            if case_path.exists():
                case_path.unlink()
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)
            for path in (session_path, smoke_uuid_marker):
                if path.exists():
                    path.unlink()

    def test_completed_stages_exec_assertion_rejects_empty_list_even_when_execution_text_exists(self):
        case_id = "zz.timeout.completed-stages-empty-list"
        session_id = "77777777-7777-4777-8777-777777777777"
        case_path = ROOT / "tests/skills/smoke" / f"{case_id}.json"
        artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
        session_path = ROOT / ".review-loop/sessions" / f"{session_id}.md"
        smoke_uuid_marker = ROOT / ".review-loop/tmp/smoke-session-uuid"
        last_run_path = ROOT / "tests/skills/.last-run.json"

        for path in (case_path, smoke_uuid_marker):
            if path.exists():
                path.unlink()
        if artifact_dir.exists():
            shutil.rmtree(artifact_dir)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session_path.write_text(
            "## Current Phase\n\nexecution\n\n"
            "## Review History\n\n"
            "### Execution Round 1 — Executor\n"
            "- no-op\n\n"
            "## Session Metadata\n"
            "- entry_point: review-loop\n"
            "- completed_stages: []\n",
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
                },
                "required": ["session_path", "session_final", "assertions", "meta"],
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
                    "(tmp / 'smoke-session-uuid').write_text(session_id, encoding='utf-8')\n"
                    "print(f'.review-loop/sessions/{session_id}.md', flush=True)\n"
                    "time.sleep(30)\n"
                ),
                "__WORKTREE__",
            ],
            "assertions": ["completed_stages_contains_exec"],
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
            self.assertIn(f"SKIP {case_id} - best-effort smoke timed out; completed_stages_contains_exec", run.stdout)

            payload = json.loads(last_run_path.read_text(encoding="utf-8"))
            record = next(candidate for candidate in payload["results"] if candidate.get("id") == case_id)
            self.assertEqual(record["status"], "skip")
            self.assertIn("completed_stages_contains_exec", record["reason"])
        finally:
            if case_path.exists():
                case_path.unlink()
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)
            for path in (session_path, smoke_uuid_marker):
                if path.exists():
                    path.unlink()

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
