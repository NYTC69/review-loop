import json
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Optional


SESSION_PATH_PATTERN = re.compile(r"\.review-loop/sessions/[0-9a-fA-F-]{8,}\.md")
LOCK_PATH_PATTERN = re.compile(r"^(?P<session_id>.+)\.lock$")
REVIEWER_PROMPT_PATTERN = re.compile(r"^(?P<session_id>.+)-reviewer-prompt\.txt$")


def _coerce_timeout_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return ""


def _extract_lock_pid(lock_path: Path) -> Optional[int]:
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        pid = payload.get("pid")
        return pid if isinstance(pid, int) else None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key.strip() != "pid":
            continue
        value = value.strip()
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _pid_is_alive(pid: Optional[int]) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cleanup_stale_review_loop_runtime(root: Path) -> dict[str, list[Path]]:
    sessions_dir = root / ".review-loop" / "sessions"
    tmp_dir = root / ".review-loop" / "tmp"
    removed_locks: list[Path] = []
    removed_prompts: list[Path] = []
    live_session_ids: set[str] = set()

    if sessions_dir.exists():
        for lock_path in sorted(sessions_dir.glob("*.lock")):
            match = LOCK_PATH_PATTERN.match(lock_path.name)
            session_id = match.group("session_id") if match else None
            pid = _extract_lock_pid(lock_path)
            if session_id and _pid_is_alive(pid):
                live_session_ids.add(session_id)
                continue
            try:
                lock_path.unlink()
                removed_locks.append(lock_path)
            except OSError:
                continue

    if tmp_dir.exists():
        for prompt_path in sorted(tmp_dir.glob("*-reviewer-prompt.txt")):
            match = REVIEWER_PROMPT_PATTERN.match(prompt_path.name)
            session_id = match.group("session_id") if match else None
            if session_id and session_id in live_session_ids:
                continue
            try:
                prompt_path.unlink()
                removed_prompts.append(prompt_path)
            except OSError:
                continue

    return {
        "removed_locks": removed_locks,
        "removed_prompts": removed_prompts,
    }


def session_paths_from_stdout(stdout: str, root: Path) -> list[Path]:
    matches = []
    for match in SESSION_PATH_PATTERN.finditer(stdout):
        candidate = (root / match.group(0)).resolve()
        if candidate.is_file() and candidate not in matches:
            matches.append(candidate)
    return matches


def session_has_entry_point(path: Path, entry_point: str) -> bool:
    if not path.is_file():
        return False
    needle = f"- entry_point: {entry_point}"
    try:
        return needle in path.read_text(encoding="utf-8")
    except OSError:
        return False


def select_primary_session_path(stdout: str, root: Path, before_sessions: set[Path], after_sessions: set[Path]) -> Optional[Path]:
    stdout_candidates = session_paths_from_stdout(stdout, root)
    review_loop_candidates = [path for path in stdout_candidates if session_has_entry_point(path, "review-loop")]
    if review_loop_candidates:
        return review_loop_candidates[0]
    if stdout_candidates:
        return stdout_candidates[0]

    new_sessions = sorted(after_sessions - before_sessions, key=lambda path: (path.stat().st_mtime_ns, path.name))
    review_loop_new = [path for path in new_sessions if session_has_entry_point(path, "review-loop")]
    if review_loop_new:
        return review_loop_new[0]
    if new_sessions:
        return new_sessions[0]
    return None


def parse_stream_json_capture(text: str) -> tuple[dict, str]:
    read_events = []
    result_text = ""
    assistant_seen = False
    result_seen = False
    parse_errors = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] != "{":
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "assistant":
            assistant_seen = True
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "Read":
                    file_path = (block.get("input") or {}).get("file_path")
                    if isinstance(file_path, str) and file_path:
                        read_events.append({"tool": "Read", "target": file_path})
        elif etype == "result":
            result_seen = True
            if event.get("subtype") == "success" and isinstance(event.get("result"), str):
                result_text = event["result"]

    schema_errors = []
    if not assistant_seen:
        schema_errors.append("no 'type=assistant' events observed — possible CLI schema drift")
    if not result_seen:
        schema_errors.append("no 'type=result' event observed — possible truncated stream or schema drift")

    payload = {
        "schema_version": 1,
        "events": read_events,
        "schema_errors": schema_errors,
        "parse_errors": parse_errors,
    }
    return payload, result_text


def finalize_stream_capture_artifact(artifact_path: Path, text_path: Path) -> bool:
    if not artifact_path.exists():
        return False

    text = artifact_path.read_text(encoding="utf-8")
    normalized = None
    try:
        candidate = json.loads(text)
    except json.JSONDecodeError:
        candidate = None
    if isinstance(candidate, dict) and {"schema_version", "events", "schema_errors", "parse_errors"} <= set(candidate.keys()):
        normalized = candidate
        result_text = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
    else:
        normalized, result_text = parse_stream_json_capture(text)
        artifact_path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")

    text_path.write_text(result_text, encoding="utf-8")
    return True


def cleanup_timed_out_process(
    process: subprocess.Popen,
    timeout_exc: subprocess.TimeoutExpired,
    terminate_grace_seconds: int = 5,
) -> tuple[str, str]:
    partial_stdout = _coerce_timeout_text(timeout_exc.stdout)
    partial_stderr = _coerce_timeout_text(timeout_exc.stderr)

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass

    try:
        process.wait(timeout=terminate_grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()

    return partial_stdout, partial_stderr
