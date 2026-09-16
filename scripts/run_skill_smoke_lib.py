import json
import os
import re
import shlex
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


def _protocol_command(command: str):
    """Recognize only a direct loader invocation, never infer shell execution."""
    if "read_protocol.py" not in command:
        return None
    if any(char in command for char in ("\n", "`", "$")):
        raise ValueError("unsupported loader shell syntax")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    args = list(lexer)
    if not args or any(token and all(c in ";&|<>()" for c in token) for token in args):
        raise ValueError("loader must be a direct command")
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(args[0]).name):
        args = args[1:]
    root = Path(__file__).resolve().parents[1]
    if not args or Path(args[0]).name != "read_protocol.py":
        raise ValueError("unsupported loader executable")
    script = Path(args.pop(0))
    script = (root / script).resolve() if not script.is_absolute() else script.resolve()
    if script != root / "scripts/read_protocol.py":
        raise ValueError("loader is outside the captured support repository")
    options, loaded = {}, []
    while args:
        option = args.pop(0)
        if option == "--inventory":
            raise ValueError("inventory does not load instruction bodies")
        if "=" in option:
            option, value = option.split("=", 1)
        elif args:
            value = args.pop(0)
        else:
            raise ValueError("missing loader argument")
        if option == "--loaded":
            if not re.fullmatch(r"[a-z0-9_-]+@[a-f0-9]{64}", value):
                raise ValueError("invalid loaded fingerprint")
            loaded.append(value)
        elif option in ("--runtime", "--stage", "--root") and option not in options:
            options[option] = value
        else:
            raise ValueError("unknown or repeated loader argument")
    if options.get("--runtime") not in ("claude", "codex") or not options.get("--stage"):
        raise ValueError("loader runtime/stage missing or invalid")
    if "--root" in options and (root / options["--root"]).resolve() != root:
        raise ValueError("loader source root differs from capture repository")
    return options["--runtime"], options["--stage"], set(loaded)


def _protocol_units(runtime, stage):
    # Import lazily: legacy Read/Agent captures need no protocol source access.
    from scripts.read_protocol import resolve
    return resolve(Path(__file__).resolve().parents[1], runtime, stage)


def _fingerprint(unit):
    return unit["unit"] + "@" + unit["sha256"]


def _verified_protocol_load(call, block, available):
    runtime, stage, loaded = call["command"]
    if block.get("is_error", False) is not False:
        raise ValueError("loader tool_result failed")
    content = block.get("content")
    if isinstance(content, list):
        if any(not isinstance(part, dict) or part.get("type") != "text" or
               not isinstance(part.get("text"), str) for part in content):
            raise ValueError("unknown loader result content")
        content = "".join(part["text"] for part in content)
    if not isinstance(content, str):
        raise ValueError("loader tool_result has no text")
    if not loaded <= available or not loaded <= call["available_at_call"]:
        raise ValueError("--loaded fingerprints lack earlier content in this context")
    units = _protocol_units(runtime, stage)
    emitted = [unit for unit in units if _fingerprint(unit) not in loaded]
    expected = "\n".join(
        f"<!-- {_fingerprint(unit)}; source: {unit['path']} -->\n{unit['body']}"
        for unit in emitted
    )
    if content != expected:
        raise ValueError("loader result differs from complete source bodies/fingerprints")
    fingerprints = [_fingerprint(unit) for unit in units]
    return {"tool": "ProtocolLoad", "tool_use_id": call["id"],
            "runtime": runtime, "stage": stage, "context": call["context"],
            "role": call["role"], "units": fingerprints,
            "emitted": [_fingerprint(unit) for unit in emitted],
            "reused": [fp for fp in fingerprints if fp in loaded],
            "instruction_bytes": sum(unit["bytes"] for unit in emitted),
            "output_bytes": len(content.encode("utf-8"))}


def protocol_stages_loaded(payload, assertion):
    """Check observed loads, not whether the model complied with their rules."""
    runtime, stages = assertion.get("runtime"), assertion.get("stages")
    context = assertion.get("context", "root")
    if runtime not in ("claude", "codex") or not isinstance(context, str) or not context:
        return False, "runtime/context missing or invalid"
    if not isinstance(stages, list) or not stages or any(not isinstance(s, str) or not s for s in stages):
        return False, "stages must be a non-empty list of strings"
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("events"), list):
        return False, "unknown capture schema"
    if payload.get("parse_errors") != 0 or payload.get("schema_errors") != [] or payload.get("protocol_errors") != []:
        return False, "capture contains parsing, schema, or protocol verification errors"
    available, covered = {}, set()
    try:
        for stage in stages:
            _protocol_units(runtime, stage)
        for event in payload["events"]:
            if not isinstance(event, dict):
                raise ValueError("invalid normalized event")
            if event.get("tool") == "ProtocolContextReset":
                if (event.get("reason") != "compact_boundary" or
                        not isinstance(event.get("context"), str) or not event["context"]):
                    raise ValueError("invalid context reset event")
                available.pop(event["context"], None)
                continue
            if event.get("tool") != "ProtocolLoad":
                if not ((event.get("tool") == "Read" and isinstance(event.get("target"), str)) or
                        (isinstance(event.get("tool"), str) and isinstance(event.get("subagent_type"), str))):
                    raise ValueError("unknown normalized event")
                continue
            event_context = event["context"]
            if not isinstance(event_context, str) or not event_context or not isinstance(event["role"], str) or not event["role"]:
                raise ValueError("invalid load context/role")
            if event["runtime"] not in ("claude", "codex") or not isinstance(event["tool_use_id"], str) or not event["tool_use_id"]:
                raise ValueError("invalid load runtime/tool id")
            units = _protocol_units(event["runtime"], event["stage"])
            expected = [_fingerprint(unit) for unit in units]
            emitted, reused = event["emitted"], event["reused"]
            if not isinstance(emitted, list) or not isinstance(reused, list):
                raise ValueError("invalid load fingerprint lists")
            if (event["units"] != expected or len(emitted) != len(set(emitted)) or
                    len(reused) != len(set(reused)) or set(emitted) & set(reused) or
                    set(emitted) | set(reused) != set(expected)):
                raise ValueError("load does not cover exact stage prerequisites")
            seen = available.setdefault(event_context, set())
            if not set(reused) <= seen:
                raise ValueError("load reuses unobserved context fingerprints")
            delivered = [unit for unit in units if _fingerprint(unit) in emitted]
            output = "\n".join(f"<!-- {_fingerprint(unit)}; source: {unit['path']} -->\n{unit['body']}" for unit in delivered)
            if (event["instruction_bytes"] != sum(unit["bytes"] for unit in delivered) or
                    event["output_bytes"] != len(output.encode("utf-8"))):
                raise ValueError("load instruction byte counts differ")
            seen.update(emitted)
            if event_context == context and event["runtime"] == runtime:
                covered.add(event["stage"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"invalid protocol load evidence: {exc}"
    missing = [stage for stage in stages if stage not in covered]
    if missing:
        return False, f"stages not successfully loaded in {context}: {missing}"
    return True, f"all {len(stages)} stages observed as complete ProtocolLoad events in {context}"


def parse_stream_json_capture(text: str) -> tuple[dict, str]:
    tool_events = []
    result_text = ""
    assistant_seen = False
    result_seen = False
    parse_errors = 0
    protocol_errors = []
    pending = {}
    available: dict[str, set[str]] = {}
    agent_roles: dict[str, str] = {}
    call_ids = set()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if not isinstance(event, dict):
            parse_errors += 1
            continue
        etype = event.get("type")
        context = event.get("context_id") or event.get("parent_tool_use_id") or "root"
        if not isinstance(context, str):
            parse_errors += 1
            continue
        if etype == "system" and event.get("subtype") == "compact_boundary":
            available.pop(context, None)
            tool_events.append({"tool": "ProtocolContextReset", "context": context,
                                "reason": "compact_boundary"})
        elif etype == "assistant":
            assistant_seen = True
            message = event.get("message", {})
            if not isinstance(message, dict) or not isinstance(message.get("content", []), list):
                parse_errors += 1
                continue
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                tool_name = block.get("name")
                tool_input = block.get("input", {})
                if not isinstance(tool_input, dict):
                    parse_errors += 1
                    continue
                if tool_name == "Bash":
                    command = tool_input.get("command")
                    if not isinstance(command, str):
                        parse_errors += 1
                        continue
                    try:
                        parsed = _protocol_command(command)
                        if parsed is not None:
                            tool_id = block.get("id")
                            if not isinstance(tool_id, str) or not tool_id or tool_id in call_ids:
                                raise ValueError("loader tool_use id missing or duplicated")
                            call_ids.add(tool_id)
                            pending[tool_id] = {"id": tool_id, "command": parsed,
                                                "context": context,
                                                "available_at_call": set(available.get(context, set())),
                                                "role": agent_roles.get(context, "assistant")}
                    except ValueError as exc:
                        protocol_errors.append(str(exc))
                    continue
                if tool_name == "Read":
                    file_path = tool_input.get("file_path")
                    if isinstance(file_path, str) and file_path:
                        tool_events.append({"tool": "Read", "target": file_path})
                    continue
                subagent_type = tool_input.get("subagent_type")
                if isinstance(tool_name, str) and tool_name and isinstance(subagent_type, str) and subagent_type:
                    tool_events.append({"tool": tool_name, "subagent_type": subagent_type})
                    if isinstance(block.get("id"), str):
                        agent_roles[block["id"]] = subagent_type
        elif etype == "user":
            message = event.get("message", {})
            if not isinstance(message, dict) or not isinstance(message.get("content", []), list):
                parse_errors += 1
                continue
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id")
                if not isinstance(tool_id, str) or tool_id not in pending:
                    continue
                call = pending.pop(tool_id)
                try:
                    if context != call["context"]:
                        raise ValueError("loader tool_result context differs from tool_use")
                    seen = available.setdefault(context, set())
                    load = _verified_protocol_load(call, block, seen)
                    tool_events.append(load)
                    seen.update(load["emitted"])
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    protocol_errors.append(str(exc))
        elif etype == "result":
            result_seen = True
            if event.get("subtype") == "success" and isinstance(event.get("result"), str):
                result_text = event["result"]
        elif etype not in ("system", "stream_event", "rate_limit_event"):
            parse_errors += 1

    schema_errors = []
    if not assistant_seen:
        schema_errors.append("no 'type=assistant' events observed — possible CLI schema drift")
    if not result_seen:
        schema_errors.append("no 'type=result' event observed — possible truncated stream or schema drift")

    payload = {
        "schema_version": 1,
        "events": tool_events,
        "schema_errors": schema_errors,
        "parse_errors": parse_errors,
        "protocol_errors": protocol_errors + [f"loader {tool_id} has no tool_result" for tool_id in pending],
    }
    return payload, result_text


def _atomic_write_text(path: Path, content: str) -> None:
    # Truncate-in-place writes leave the destination's inode reachable to any
    # writer still holding an inherited FD. A detached descendant of the
    # smoke runner that survives SIGKILL keeps flushing buffered output to
    # that FD, extending the file past the new payload with zero-fill in
    # between (`<normalized-json>\n\x00…\x00<raw-stream-tail>`). Writing
    # through a fresh sibling and renaming over the destination orphans the
    # old inode, so any surviving writer's bytes land on the unlinked file.
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


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
        _atomic_write_text(artifact_path, json.dumps(normalized, indent=2) + "\n")

    _atomic_write_text(text_path, result_text)
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


# `finding_triage.py check` report keys the reviewer-kind smoke assertions
# consume. Access is strict: a missing key is helper/report contract drift
# and must surface as a FAIL record, never as a defaulted measurement.
TRIAGE_REPORT_KEYS = ("result", "findings", "complete", "incomplete", "summary")


def triage_report_fields(report) -> dict:
    """Strict projection of a `finding_triage.py check` JSON report onto
    `TRIAGE_REPORT_KEYS` (every finding must carry `severity`). Raises
    KeyError naming the first missing key; TypeError when the report is not
    an object."""
    if not isinstance(report, dict):
        raise TypeError(f"finding_triage.py check report must be a JSON object, got {type(report).__name__}")
    for key in TRIAGE_REPORT_KEYS:
        if key not in report:
            raise KeyError(key)
    for finding in report["findings"]:
        if not isinstance(finding, dict) or "severity" not in finding:
            raise KeyError("findings[].severity")
    return {key: report[key] for key in TRIAGE_REPORT_KEYS}
