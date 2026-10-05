#!/usr/bin/env python3
"""Opt-in native lifecycle regression in disposable repositories.

The candidate workflow is the system under test, never this change's review
controller. Exit 0 means all selected behavioral assertions passed, 1 failure,
2 invalid invocation, 3 missing native CLI. No timeout or missing runtime is PASS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

try:
    from process_tree import terminate_process_tree, wait_with_process_tree
except ModuleNotFoundError:  # imported as scripts.run_runtime_regression
    from scripts.process_tree import terminate_process_tree, wait_with_process_tree

ROOT = Path(__file__).resolve().parents[1]
CASES = ("plan", "execute", "review-only", "stop-resume", "failure")
TASK = "Change double(n) in calc.py to return n * 2. Verify with python3 verify.py. Keep user.txt unchanged."
PLAN = "Implement double(n) as n * 2 in calc.py; run python3 verify.py; preserve user.txt."
VERIFY = "import runpy\nf = runpy.run_path('calc.py')['double']\nassert [f(n) for n in (-17, -2, 0, 3, 18)] == [-34, -4, 0, 6, 36]\n"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT)


def safe_file(root, path):
    """Do not stat/read through leaf or parent symlinks in candidate support."""
    relative = path.relative_to(root)
    cursor = root
    for component in relative.parts:
        if component in {"..", ".compass", ".worktrees"}:
            return False
        cursor = cursor/component
        if cursor.is_symlink():
            return False
    return path.is_file()


def source_inventory(root):
    """Copy only tracked support files, plus explicit new runtime helpers.

    Never traverse ignored Compass results, other worktrees or the plugin's
    self-referential symlink. In particular no paired-session spike is read.
    """
    names = set(os.fsdecode(git(root, "ls-files", "-z")).split("\0")) - {""}
    for directory in ("scripts", "docs/protocol"):
        parent = root/directory
        if any((root/Path(*parent.relative_to(root).parts[:n])).is_symlink()
               for n in range(1, len(parent.relative_to(root).parts) + 1)):
            continue
        names.update(str(p.relative_to(root)) for p in parent.glob("*") if safe_file(root, p))
    for name in sorted(names):
        path = root/name
        if (name.startswith(("scripts/", "docs/protocol/", "skills/", ".agents/skills/", ".codex/agents/", "agents/"))
                and safe_file(root, path)):
            yield name, path


def file_inventory(root, *, fixture=False):
    """Bind actual files, including ignored source files; do not follow symlinks."""
    if root.is_symlink():
        raise ValueError("inventory root must not be a symlink")
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in list(dirs):
            path = current/name
            relative = path.relative_to(root)
            if fixture and (relative.parts[0] in {".git", ".review-loop"} or name == "__pycache__"):
                dirs.remove(name)
            elif path.is_symlink():
                names.append(name)
                dirs.remove(name)
        for name in names:
            path = current/name
            relative = str(path.relative_to(root))
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                files[relative] = ("symlink", os.readlink(path))
            elif stat.S_ISREG(mode):
                files[relative] = (mode & 0o777, hashlib.sha256(path.read_bytes()).hexdigest())
            else:
                files[relative] = ("unsupported", stat.S_IFMT(mode))
    return files


def prepare_fixture(base, support_root, case, runtime):
    repo = base / "workspace"
    repo.mkdir(parents=True)
    support = base / "support"
    for name, path in source_inventory(support_root):
        target = support/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    # Custom role instructions are copied as test configuration with their shipped
    # default model. Production role definitions are untouched.
    for source in (support/".codex/agents").glob("*.toml"):
        target = repo/".codex/agents"/source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())
    (repo/"calc.py").write_text("def double(n):\n    return n * " + ("2" if case == "review-only" else "1") + "\n")
    (repo/"verify.py").write_text("from calc import double\nassert double(3) == 6\nassert double(-2) == -4\nprint('verified')\n")
    (repo/"user.txt").write_text("existing user content\n")
    if not safe_file(support_root, support_root/".gitignore"):
        raise ValueError("support .gitignore must be a regular file without symlink ancestors")
    (repo/".gitignore").write_text(".review-loop/\n__pycache__/\n"+(support_root/".gitignore").read_text())
    git(repo, "init", "-q")
    git(repo, "-c", "user.name=Regression Fixture", "-c", "user.email=fixture@example.invalid", "add", ".")
    git(repo, "-c", "user.name=Regression Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    # Pre-existing unrelated user work must survive every entry and resume.
    (repo/"user.txt").write_text("existing user content\nUNRELATED USER EDIT\n")
    config = repo/".review-loop/config.md"
    config.parent.mkdir()
    reviewer_config = (
        "reviewer: subagent\nreviewer_model: claude-opus-5-5\n"
        if runtime == "claude" else
        "reviewer: codex\ncodex_reviewer_backend: codex\ncodex_reviewer_model: gpt-6.1-sol\n"
    )
    config.write_text(
        reviewer_config + "soft_limit_plan: 2\nsoft_limit_exec: 2\n"
        "auto_commit: false\ndocs_file: \"\"\nskip_quality_polish: true\n"
        # Lifecycle cases test execution/review/resume boundaries. The gate has
        # its own adapter suite and would add another long paid model call to
        # every fixture without increasing coverage of those transitions.
        "adversarial_gate_skip_paths:\n  - \"calc.py\"\n"
    )
    return repo, support


def build_command(runtime, model, prompt_file):
    if runtime == "codex":
        # Keep generated files and Git refs inside the disposable fixture.
        return ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--json",
                "-s", "workspace-write", "-c", "features.hooks=false", "-c", "features.plugins=false",
                "-c", "features.apps=false", "-m", model, "-"]
    sandbox = {
        "sandbox": {
            "enabled": True,
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "filesystem": {
                "denyRead": ["~/.ssh", "~/.aws", "~/.config", "~/.codex", "~/.claude"],
            },
        },
    }
    return ["claude", "-p", "--safe-mode", "--no-session-persistence", "--output-format", "stream-json",
            "--include-partial-messages", "--verbose", "--model", model,
            "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", json.dumps(sandbox, separators=(",", ":")),
            "--permission-mode", "acceptEdits"]


def invoke(runtime, model, repo, support, request, directory, timeout):
    directory.mkdir(parents=True)
    prefix = "skills" if runtime == "claude" else ".agents/skills"
    entry = "plan" if request.startswith("plan ") else "execute"
    prompt = (
        "You are running ONE lifecycle regression on this disposable fixture. "
        "The candidate workflow is the test subject; do not review or modify the support repository. "
        "Do not commit, push, install packages, create worktrees, or access any paired-session artifacts. "
        "No user questions are necessary: the complete task is below, and the session config is authoritative. "
        f"Read {support/prefix/entry/'SKILL.md'} and use its instructions to execute this request: {request}\n"
        "Use the copied support path for scripts; keep all task/session writes inside this fixture workspace. "
        "Preserve the pre-existing user.txt edit. At the requested stop boundary, release the lock and exit. "
        "Do not claim success after a CLI, permission or evidence error; report the actual failure.\n")
    prompt_file = directory/"prompt.txt"
    prompt_file.write_text(prompt)
    started = time.monotonic()
    timed_out = False
    code, known, inspectable = None, (), True
    with prompt_file.open("rb") as stdin, (directory/"stream.jsonl").open("wb") as stdout, (directory/"stderr.log").open("wb") as stderr:
        process = subprocess.Popen(build_command(runtime, model, prompt_file), cwd=repo,
                                   stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=True)
        try:
            code, timed_out, known, inspectable = wait_with_process_tree(
                process, timeout,
            )
        finally:
            # Reviewer wrappers create their own sessions.  Repeated process-
            # table scans are required; killing only this orchestrator's group
            # can strand those independent descendants.
            final_code, cleanup_ok, _observed = terminate_process_tree(
                process, known_pids=known,
            )
            if code is None:
                code = final_code
            if not cleanup_ok or not inspectable:
                timed_out = True
    return {"returncode": code, "timed_out": timed_out,
            "elapsed_seconds": round(time.monotonic()-started, 3), "artifact_dir": str(directory)}


def section(text, title):
    """Read one unfenced canonical section, rejecting duplicate headings."""
    sections, selected, fence = [], None, None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token[0], len(token)
            elif token[0] == fence[0] and len(token) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        if line.startswith("## "):
            selected = [] if line == "## "+title else None
            if selected is not None:
                sections.append(selected)
        elif selected is not None:
            selected.append(line)
    return "\n".join(sections[0]) if len(sections) == 1 else None


def completed_stages(text):
    metadata = section(text, "Session Metadata")
    values = re.findall(r"^- completed_stages:\s*\[([^\]\n]*)\][ \t]*(?:#.*)?$", metadata or "", re.M)
    if len(values) != 1:
        return None
    stages = [value.strip().strip("\"'") for value in values[0].split(",") if value.strip()]
    return stages if len(set(stages)) == len(stages) else None


def rejection_evidence(runtime, directory):
    """Require native assistant/result output rejecting the requested flag.

    Tool output and user/prompt echoes are not model rejection evidence. This
    evidence complements the physical no-session/no-source-write assertions.
    """
    path = directory/"stream.jsonl"
    if not safe_file(directory, path):
        return False
    messages = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if runtime == "codex" and event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                messages.append(item["text"])
        elif runtime == "claude" and event.get("type") == "result" and not event.get("is_error"):
            if isinstance(event.get("result"), str):
                messages.append(event["result"])
    for text in messages:
        if ("invalid-stage" in text and "--stop-after" in text
                and re.search(r"invalid|unsupported|unknown|reject|not (?:a )?valid|不支持|无效|不合法|拒绝",
                              text.replace("invalid-stage", ""), re.I)):
            return True
    return False


def inspect_fixture(repo, case, original_head, *,
                    source_before, index_before, expected_stages=None, session_before=None):
    """Independent assertions over actual files, never the model's summary."""
    failures = []
    if repo.is_symlink() or (repo/".git").is_symlink():
        return ["fixture/Git root became a symlink"], None
    current = file_inventory(repo, fixture=True)
    allowed = {"calc.py"} if case in {"execute", "stop-resume"} else set()
    unexpected = sorted(path for path in set(source_before) | set(current)
                        if path not in allowed and source_before.get(path) != current.get(path))
    if unexpected:
        failures.append("unexpected fixture source changes: "+", ".join(unexpected))
    if current.get("user.txt") != source_before.get("user.txt"):
        failures.append("unrelated user work changed")
    if git(repo, "ls-files", "--stage", "-z") != index_before:
        failures.append("user index changed")
    if git(repo, "rev-parse", "HEAD").strip() != original_head:
        failures.append("unauthorized commit")
    session_dir = repo/".review-loop/sessions"
    if (repo/".review-loop").is_symlink() or session_dir.is_symlink():
        return failures+["session directory is a symlink"], None
    sessions = list(session_dir.glob("*.md"))
    if list(session_dir.glob("*.lock")):
        failures.append("lock not released")
    if case == "failure":
        if sessions:
            failures.append("invalid flag created session state")
        return failures, None
    if len(sessions) != 1:
        return failures+[f"expected one session, found {len(sessions)}"], None
    session = sessions[0]
    if not safe_file(repo, session):
        return failures+["session must be a regular file without symlink ancestors"], None
    text = session.read_text()
    if case == "plan":
        approved = section(text, "Approved Plan")
        if not re.search(r"^\s*-\s*Source:\s*reviewer-approved\s*$", approved or "", re.M):
            failures.append("no reviewer-approved plan")
        if completed_stages(text) != []:
            failures.append("plan-only session already claims execution stages")
    else:
        verification = None
        if safe_file(repo, repo/"calc.py"):
            try:
                verification = subprocess.run([sys.executable, "-I", "-B", "-c", VERIFY], cwd=repo,
                                              capture_output=True, timeout=10)
            except subprocess.TimeoutExpired:
                pass
        if verification is None or verification.returncode != 0:
            failures.append("actual implementation verification failed")
        if completed_stages(text) != expected_stages:
            failures.append("session did not stop at the exact requested stage boundary")
        if session_before is not None and session.name != session_before:
            failures.append("resume created a different session")
    return failures, session.stem


def run_case(runtime, case, output, support_root, model, timeout):
    directory = output/f"{runtime}-{case}"
    if directory.exists():
        raise ValueError(f"case output already exists: {directory}; choose a fresh --output")
    directory.mkdir(parents=True)
    repo, support = prepare_fixture(directory, support_root, case, runtime)
    head = git(repo, "rev-parse", "HEAD").strip()
    source_before, index_before = file_inventory(repo, fixture=True), git(repo, "ls-files", "--stage", "-z")
    support_before = file_inventory(support)
    requests = {"plan": "plan "+TASK,
                "execute": f'execute --plan "{PLAN}" --title regression --stop-after before-polish',
                "review-only": "execute --review-only --description Verify double(n) returns twice n --stop-after before-polish",
                "stop-resume": f'execute --plan "{PLAN}" --title regression --stop-after before-polish',
                "failure": f'execute --plan "{PLAN}" --title regression --stop-after invalid-stage'}
    calls = [invoke(runtime, model, repo, support, requests[case], directory/"call-1", timeout)]
    failures, session = inspect_fixture(repo, case, head, source_before=source_before,
                                        index_before=index_before, expected_stages=["exec"])
    if case == "failure" and not rejection_evidence(runtime, directory/"call-1"):
        failures.append("no native rejection evidence for --stop-after invalid-stage")
    if any(c["timed_out"] or c["returncode"] != 0 for c in calls):
        failures.append("native invocation did not complete successfully")
    if case == "stop-resume" and not failures:
        calls.append(invoke(runtime, model, repo, support,
                            f"execute --session {session} --stop-after before-docs", directory/"call-2", timeout))
        more, _ = inspect_fixture(repo, case, head, source_before=source_before,
                                  index_before=index_before, expected_stages=["exec", "polish"],
                                  session_before=session+".md")
        failures.extend(more)
        if calls[-1]["timed_out"] or calls[-1]["returncode"] != 0:
            failures.append("native resume did not complete successfully")
    if file_inventory(support) != support_before:
        failures.append("candidate modified its support copy")
    record = {"runtime": runtime, "case": case, "measurement": "native CLI execution",
              "status": "fail" if failures else "pass", "failures": failures, "calls": calls,
              "fixture": str(repo), "model": model}
    (directory/"result.json").write_text(json.dumps(record, indent=2)+"\n")
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("claude", "codex"), required=True)
    parser.add_argument("--case", choices=CASES, action="append")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--support-root", type=Path, default=ROOT)
    parser.add_argument("--model")
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--live", action="store_true", help="Explicitly spend model tokens on native regression")
    args = parser.parse_args()
    if not args.live:
        parser.error("native execution requires --live; unit tests do not call models")
    if args.timeout_seconds <= 0:
        parser.error("timeout must be positive")
    if not shutil.which(args.runtime):
        print(json.dumps({"status": "unavailable", "runtime": args.runtime}))
        return 3
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    model = args.model or ("gpt-6.1-sol" if args.runtime == "codex" else "claude-opus-5-5")
    records = []
    try:
        for case in args.case or CASES:
            record = run_case(args.runtime, case, args.output, args.support_root.resolve(), model, args.timeout_seconds)
            records.append(record)
            print(json.dumps({k: record[k] for k in ("runtime", "case", "status", "failures")}), flush=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}))
        return 1
    (args.output/f"{args.runtime}-summary.json").write_text(json.dumps(records, indent=2)+"\n")
    return int(any(r["status"] != "pass" for r in records))


if __name__ == "__main__":
    raise SystemExit(main())
