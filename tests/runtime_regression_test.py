"""Negative controls for the native runner, using fake model invocations only."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1]/"scripts/run_runtime_regression.py"
SPEC = importlib.util.spec_from_file_location("runtime_regression", SCRIPT)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)
SESSION = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def support(tmp_path, monkeypatch):
    for key in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    root = tmp_path/"source"
    (root/"scripts").mkdir(parents=True)
    (root/"scripts/helper.py").write_text("# fixture support\n")
    (root/".gitignore").write_text(".compass/\n")
    RUNNER.git(root, "init", "-q")
    RUNNER.git(root, "add", ".")
    return root


def write_session(repo, stages, *, session=SESSION, approved=True, extra=""):
    directory = repo/".review-loop/sessions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/(session+".md")
    path.write_text("## Approved Plan\n- Source: "+("reviewer-approved" if approved else "user-supplied")+
                    "\n\nChange double to multiply by two.\n\n## Session Metadata\n"
                    "- completed_stages: ["+", ".join(stages)+"]\n"+extra)
    return path


def correct_code(repo):
    (repo/"calc.py").write_text("def double(n):\n    return n * 2\n")


def rejection(directory, runtime, text="Unsupported --stop-after value invalid-stage; request rejected."):
    directory.mkdir(parents=True, exist_ok=True)
    event = ({"type": "item.completed", "item": {"type": "agent_message", "text": text}}
             if runtime == "codex" else {"type": "result", "is_error": False, "result": text})
    (directory/"stream.jsonl").write_text(json.dumps(event)+"\n")


def run_case(tmp_path, support, monkeypatch, case, action, *, runtime="codex", code=0, timed_out=False):
    calls = []

    def fake(runtime, model, repo, copied, request, directory, timeout):
        calls.append(request)
        directory.mkdir(parents=True, exist_ok=True)
        action(repo, copied, request, directory, len(calls))
        return {"returncode": code, "timed_out": timed_out, "artifact_dir": str(directory), "elapsed_seconds": 0}

    monkeypatch.setattr(RUNNER, "invoke", fake)
    result = RUNNER.run_case(runtime, case, tmp_path/"results", support, "fake-model", 1)
    return result, calls


@pytest.mark.parametrize("case", ["plan", "execute", "review-only", "stop-resume", "failure"])
def test_correct_fake_lifecycle_passes_without_invoking_models(tmp_path, support, monkeypatch, case):
    def action(repo, copied, request, directory, number):
        if case == "failure":
            rejection(directory, "codex")
        else:
            if case != "plan":
                correct_code(repo)
            write_session(repo, [] if case == "plan" else ["exec"] if number == 1 else ["exec", "polish"])

    result, calls = run_case(tmp_path, support, monkeypatch, case, action)
    assert result["status"] == "pass", result["failures"]
    assert len(calls) == (2 if case == "stop-resume" else 1)
    if case == "stop-resume":
        assert "--session "+SESSION in calls[1]


@pytest.mark.parametrize("path", ["calc.py", "verify.py", "new-source.py", "user.txt"])
def test_plan_cannot_write_source(tmp_path, support, monkeypatch, path):
    def action(repo, copied, request, directory, number):
        write_session(repo, [])
        (repo/path).write_text("unauthorized source write\n")

    result, _ = run_case(tmp_path, support, monkeypatch, "plan", action)
    assert result["status"] == "fail"
    assert any("unexpected fixture source changes" in failure for failure in result["failures"])


def test_plan_cannot_stage_preexisting_user_work(tmp_path, support, monkeypatch):
    def action(repo, copied, request, directory, number):
        write_session(repo, [])
        RUNNER.git(repo, "add", "user.txt")

    result, _ = run_case(tmp_path, support, monkeypatch, "plan", action)
    assert "user index changed" in result["failures"]


@pytest.mark.parametrize("mutation", ["noop", "session", "source", "echo", "tool-output"])
def test_invalid_flag_requires_rejection_and_no_state_or_source(tmp_path, support, monkeypatch, mutation):
    def action(repo, copied, request, directory, number):
        if mutation != "noop":
            rejection(directory, "codex")
        if mutation == "session":
            write_session(repo, [])
        elif mutation == "source":
            correct_code(repo)
        elif mutation == "echo":
            rejection(directory, "codex", "Received execute --stop-after invalid-stage")
        elif mutation == "tool-output":
            (directory/"stream.jsonl").write_text(json.dumps({"type": "item.completed", "item": {
                "type": "command_execution", "aggregated_output": "Unsupported --stop-after invalid-stage"}})+"\n")

    result, _ = run_case(tmp_path, support, monkeypatch, "failure", action)
    assert result["status"] == "fail"
    if mutation in {"noop", "echo", "tool-output"}:
        assert any("no native rejection evidence" in failure for failure in result["failures"])


def test_claude_failure_accepts_explicit_native_result(tmp_path, support, monkeypatch):
    result, _ = run_case(tmp_path, support, monkeypatch, "failure",
                         lambda r, s, q, d, n: rejection(d, "claude"), runtime="claude")
    assert result["status"] == "pass"


@pytest.mark.parametrize("claims", [["exec"], ["exec", "polish"]])
def test_session_claim_cannot_replace_actual_implementation(tmp_path, support, monkeypatch, claims):
    def action(repo, copied, request, directory, number):
        write_session(repo, claims)
        (repo/"verify.py").write_text("print('verified')\n")

    result, _ = run_case(tmp_path, support, monkeypatch, "execute", action)
    assert "actual implementation verification failed" in result["failures"]
    assert any("verify.py" in failure for failure in result["failures"])


@pytest.mark.parametrize("mutation", ["already-complete", "no-transition", "new-session", "overrun"])
def test_stop_resume_requires_actual_same_session_transition(tmp_path, support, monkeypatch, mutation):
    def action(repo, copied, request, directory, number):
        correct_code(repo)
        stages = ["exec", "polish"] if mutation == "already-complete" or number == 2 else ["exec"]
        if mutation == "no-transition":
            stages = ["exec"]
        if mutation == "overrun" and number == 2:
            stages += ["docs"]
        if mutation == "new-session" and number == 2:
            (repo/".review-loop/sessions"/(SESSION+".md")).unlink()
            write_session(repo, stages, session="22222222-2222-4222-8222-222222222222")
        else:
            write_session(repo, stages)

    result, calls = run_case(tmp_path, support, monkeypatch, "stop-resume", action)
    assert result["status"] == "fail"
    assert len(calls) == (1 if mutation == "already-complete" else 2)
    if mutation == "new-session":
        assert "resume created a different session" in result["failures"]


@pytest.mark.parametrize("code,timed_out", [(7, False), (0, True), (None, True)])
def test_real_invocation_failure_never_passes_on_valid_files(tmp_path, support, monkeypatch, code, timed_out):
    def action(repo, copied, request, directory, number):
        correct_code(repo)
        write_session(repo, ["exec"])

    result, _ = run_case(tmp_path, support, monkeypatch, "execute", action, code=code, timed_out=timed_out)
    assert result["status"] == "fail"
    assert "native invocation did not complete successfully" in result["failures"]


def test_resume_nonzero_cannot_pass_on_valid_transition(tmp_path, support, monkeypatch):
    count = 0

    def fake(runtime, model, repo, copied, request, directory, timeout):
        nonlocal count
        count += 1
        correct_code(repo)
        write_session(repo, ["exec"] if count == 1 else ["exec", "polish"])
        return {"returncode": 0 if count == 1 else 9, "timed_out": False}

    monkeypatch.setattr(RUNNER, "invoke", fake)
    result = RUNNER.run_case("codex", "stop-resume", tmp_path/"results", support, "fake", 1)
    assert "native resume did not complete successfully" in result["failures"]


@pytest.mark.parametrize("mutation", ["support", "lock", "review-source"])
def test_independent_preservation_guards(tmp_path, support, monkeypatch, mutation):
    def action(repo, copied, request, directory, number):
        write_session(repo, ["exec"])
        if mutation == "support":
            (copied/"scripts/helper.py").write_text("altered support\n")
        elif mutation == "lock":
            (repo/".review-loop/sessions"/(SESSION+".lock")).write_text("locked")
        else:
            (repo/"calc.py").write_text("def double(n):\n    return n + n\n")

    result, _ = run_case(tmp_path, support, monkeypatch, "review-only", action)
    assert result["status"] == "fail"


def test_canonical_metadata_rejects_fenced_or_duplicate_stage_proof():
    assert RUNNER.completed_stages("## Current Review Packet\n```md\n## Session Metadata\n- completed_stages: [exec]\n```\n") is None
    assert RUNNER.completed_stages("## Session Metadata\n- completed_stages: [exec]\n## Session Metadata\n- completed_stages: [exec, polish]\n") is None
    assert RUNNER.completed_stages("## Session Metadata\n- completed_stages: [exec, exec]\n") is None
    assert RUNNER.completed_stages("## Session Metadata\n- completed_stages: [exec, polish] # verified\n") == ["exec", "polish"]


def test_source_inventory_never_traverses_ignored_artifacts_or_symlinks(support, monkeypatch):
    # This is a synthetic path under pytest's temporary root, never the actual
    # explicitly excluded artifact tree in the project.
    excluded = support/".compass/results/excluded-spike/real-run"
    excluded.mkdir(parents=True)
    (excluded/"forbidden.py").write_text("must never read\n")
    (support/"scripts/alias.py").symlink_to(excluded/"forbidden.py")
    (support/"docs").symlink_to(excluded, target_is_directory=True)
    (support/"skills").symlink_to(excluded, target_is_directory=True)
    actual_git = RUNNER.git
    actual_stat = Path.stat
    actual_glob = Path.glob

    def guarded_stat(path, *args, **kwargs):
        if path == excluded or excluded in path.parents:
            raise AssertionError("traversed excluded tree")
        return actual_stat(path, *args, **kwargs)

    def guarded_glob(path, pattern):
        if path == support/"docs/protocol":
            raise AssertionError("traversed directory symlink")
        return actual_glob(path, pattern)

    def fake_git(repo, *args):
        if args == ("ls-files", "-z"):
            return actual_git(repo, *args)+b"skills/forbidden.py\0.compass/results/excluded-spike/real-run/forbidden.py\0"
        return actual_git(repo, *args)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    monkeypatch.setattr(Path, "glob", guarded_glob)
    monkeypatch.setattr(RUNNER, "git", fake_git)
    assert [name for name, _ in RUNNER.source_inventory(support)] == ["scripts/helper.py"]


def test_native_subprocess_timeout_is_reported_and_tree_cleanup_required(tmp_path, monkeypatch):
    class Process:
        pid = 123456

        def wait(self, timeout):
            if timeout == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            return -9

    monkeypatch.setattr(RUNNER.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        RUNNER, "wait_with_process_tree",
        lambda process, timeout: (None, True, (process.pid,), True),
    )
    monkeypatch.setattr(
        RUNNER, "terminate_process_tree",
        lambda process, **kwargs: (None, True, (process.pid, 123457)),
    )
    repo = tmp_path/"workspace"
    repo.mkdir()
    result = RUNNER.invoke("codex", "fake", repo, tmp_path/"support", "plan test", tmp_path/"call", 1)
    assert result["timed_out"] is True and result["returncode"] is None


def test_native_subprocess_fails_closed_when_tree_cleanup_unproven(tmp_path, monkeypatch):
    class Process:
        pid = 123456

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(RUNNER.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(
        RUNNER, "wait_with_process_tree",
        lambda process, timeout: (0, False, (process.pid,), True),
    )
    monkeypatch.setattr(
        RUNNER, "terminate_process_tree",
        lambda process, **kwargs: (0, False, (process.pid, 123457)),
    )
    repo = tmp_path/"workspace"
    repo.mkdir()
    result = RUNNER.invoke("codex", "fake", repo, tmp_path/"support", "plan test", tmp_path/"call", 1)
    assert result["timed_out"] is True and result["returncode"] == 0


def test_disposable_codex_orchestrator_can_write_ledger_objects_and_refs(tmp_path):
    command = RUNNER.build_command("codex", "fake-model", tmp_path/"prompt.txt")
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("-s")+1] == "workspace-write"
    assert "--ignore-user-config" in command and "--ignore-rules" in command
    assert "--ephemeral" in command
    assert all(value in command for value in ("features.hooks=false", "features.plugins=false", "features.apps=false"))


def test_claude_live_command_requires_os_sandbox(tmp_path):
    command = RUNNER.build_command("claude", "sonnet", tmp_path/"prompt.txt")
    settings = json.loads(command[command.index("--settings") + 1])
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
