import json
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Optional


SESSION_PATH_PATTERN = re.compile(r"\.review-loop/sessions/[0-9a-fA-F-]{8,}\.md")


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
    partial_stdout = timeout_exc.stdout or ""
    partial_stderr = timeout_exc.stderr or ""

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
