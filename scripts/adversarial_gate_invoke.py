#!/usr/bin/env python3
"""Terminal Adversarial Gate invoker for review-loop Step 3.4.

Drain-thread + timeout pattern: faithful port of the core pattern in
Scheduler._run_one in scripts/review_verification.py. Reader-exception
sentinel bytes and the wait_after_kill_timed_out diagnostic flag are
intentionally NOT ported — runtime failures with empty stdout surface as
controlled SKIP with a detail string, while runtime failures after producer
stdout fail closed as REQUEST_CHANGES.
Drain-thread exceptions and incomplete drains are stricter local divergences:
empty stdout SKIPs with diagnostic detail, but any captured stdout blocks.

Two dispatch paths:

  plugin-path  (preferred) — `node ${CODEX_PLUGIN_ROOT}/scripts/codex-companion.mjs
                 adversarial-review --scope working-tree --json -- <focus>` — no
                 sandbox bootstrap side-effect, does NOT mutate
                 `.review-loop/config.md`.
  fallback-path           — `codex exec --output-schema <schema> --sandbox read-only`
                 with rendered prompt on stdin; this DOES trigger the bootstrap
                 side-effect that overwrites `.review-loop/config.md`. We
                 snapshot the file before spawn and restore in `finally:`.

Exits 0 on every controlled SKIP. Exits with the adapter's clean verdict
returncode (0 = APPROVE, 1 = REQUEST_CHANGES); adapter malformed output
(exit 2) is converted to blocking REQUEST_CHANGES.

Historical `Finding #N` / `Meta-dogfood R*` labels in comments are provenance
for the v2.7.7/v2.7.8 adversarial-gate audit trail; the durable contract is
the protocol text plus the regression tests.
"""

from __future__ import annotations

import argparse
import atexit
import filecmp
import glob
import os
import re
import shutil
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
from typing import NoReturn, Optional


# Module-level state for signal-safe cleanup. See Finding #21 + #26.
_active_proc: Optional[subprocess.Popen] = None
# Meta-dogfood R2 Fix B: cache the pgid at spawn time, not at cleanup-call
# time. ``os.getpgid(proc.pid)`` is unreliable once the leader has exited,
# so the spawn-time value is the only thing we can trust during teardown.
_active_pgid: Optional[int] = None
_CLEANUP_DONE: bool = False
_CLEANUP_IN_PROGRESS: bool = False
_CLEANUP_FAILURE: Optional[str] = None
_SNAPSHOT_PATH: Optional[str] = None
_PROMPT_PATH: Optional[str] = None
# Meta-dogfood R2 Fix A: tracks whether ``.review-loop/config.md`` existed
# before we spawned the fallback ``codex exec``. ``codex exec`` bootstraps
# the file as a side effect even when absent; without this flag the cleanup
# path has no way to tell ``codex created it`` from ``user already had it``
# and would leave a stray config behind.
_CONFIG_EXISTED_PRE_CALL: bool = False
_FALLBACK_CONFIG_CLEANUP_ACTIVE: bool = False
_CONFIG_PATH: str = ".review-loop/config.md"
_SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
_EXPECTED_CODEX_BOOTSTRAP_CONFIG: str = (
    "codex_reviewer_backend: codex\n"
    "skip_quality_polish: true\n"
)

# Timeouts (seconds) for process-group teardown — see Finding #21.
_SIGTERM_GRACE_SECS: float = 2.0
_SIGTERM_POLL_INTERVAL: float = 0.05
_KILL_WAIT_TIMEOUT: float = 5.0
_DRAIN_JOIN_TIMEOUT: float = 2.0

# Broadened auth marker per Finding #19 + #24. `authentication` and `oauth`
# intentionally lack a right boundary so they catch concatenated forms
# (AuthenticationError, OAuth2). Slight FP risk on `authenticator` /
# `oauthkit` is acceptable per "diagnostic-precision only" — both branches
# SKIP, only the banner reason differs.
AUTH_RE = re.compile(
    r"(?i)(?:unauthenticated|not signed in|login required|authentication|oauth|unauthorized)"
)


# ---------- skip emit ----------


def _emit_skip(reason: str, detail: Optional[str] = None) -> NoReturn:
    """Print SKIP banner to stderr and exit 0 (fail-silently contract)."""
    msg = f"adversarial-gate: SKIP reason={reason}"
    if detail:
        safe_detail = detail.replace("\n", " | ")[:300]
        msg += f" detail={safe_detail}"
    sys.stderr.write(msg + "\n")
    sys.exit(0)


def _emit_cleanup_request_changes(detail: str) -> NoReturn:
    """Block approval when fallback config cleanup cannot prove success."""
    safe_detail = detail.replace("\n", " | ")[:300]
    sys.stdout.write(
        "adversarial-gate: REQUEST_CHANGES\n\n"
        "### Issues\n"
        "- [CRITICAL] scripts/adversarial_gate_invoke.py:156-211 "
        "(confidence=1.0) — fallback cleanup failed before the gate could "
        f"prove `.review-loop/config.md` was restored/removed: {safe_detail}\n"
        "  Recommendation: inspect `.review-loop/config.md` and any preserved "
        "adversarial-gate-config-* snapshot, restore the intended config state, "
        "then rerun Step 3.\n"
    )
    sys.exit(1)


def _emit_runtime_warning(detail: str) -> None:
    """Best-effort stderr diagnostic; never changes gate outcome."""
    safe_detail = detail.replace("\n", " | ")[:300]
    try:
        sys.stderr.write(f"adversarial-gate: warning {safe_detail}\n")
    except Exception:  # noqa: BLE001
        pass


def _unlink_partial_temp(path: Optional[str], label: str) -> None:
    """Best-effort cleanup for temp files not yet owned by _cleanup()."""
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        _emit_runtime_warning(f"{label} unlink failed: {e}")


def _emit_uncertain_stdout_request_changes(
    detail: str, stdout_bytes: bytes
) -> NoReturn:
    """Block approval when producer stdout exists but capture is uncertain."""
    safe_detail = detail.replace("\n", " | ")[:300]
    stdout_len = len(stdout_bytes)
    sys.stdout.write(
        "adversarial-gate: REQUEST_CHANGES\n\n"
        "### Issues\n"
        "- [CRITICAL] scripts/adversarial_gate_invoke.py:449-519 "
        "(confidence=1.0) — adversarial-review producer stdout was captured "
        "before a runtime failure, but the capture may be partial or the "
        f"producer did not exit cleanly: {safe_detail}; stdout_bytes={stdout_len}\n"
        "  Recommendation: inspect the producer/runtime failure, rerun Step 3.4, "
        "and do not mint `exec` from a controlled SKIP when stdout was produced.\n"
    )
    sys.exit(1)


def _format_adapter_malformed_request_changes(detail: str) -> str:
    """Render produced-but-malformed adversarial output as blocking CR."""
    safe_detail = detail.replace("\n", " | ")[:300]
    return (
        "adversarial-gate: REQUEST_CHANGES\n\n"
        "### Issues\n"
        "- [CRITICAL] scripts/adversarial_gate_adapter.py:1-1 "
        "(confidence=1.0) — adapter output was malformed, so Step 3.4 "
        "could not validate the adversarial-review verdict and must not "
        f"mint `exec`: {safe_detail}\n"
        "  Recommendation: inspect the adapter diagnostic, fix the malformed "
        "adversarial-review output or schema handling, then rerun Step 3.\n"
    )


def _format_adapter_launch_failure_request_changes(detail: str) -> str:
    """Render adapter launch failure after producer stdout as blocking CR."""
    safe_detail = detail.replace("\n", " | ")[:300]
    return (
        "adversarial-gate: REQUEST_CHANGES\n\n"
        "### Issues\n"
        "- [CRITICAL] scripts/adversarial_gate_invoke.py:567-588 "
        "(confidence=1.0) — adapter launch failed after producer stdout "
        "was captured, so Step 3.4 could not validate the "
        f"adversarial-review verdict and must not mint `exec`: {safe_detail}\n"
        "  Recommendation: fix the adapter runtime invocation, then rerun "
        "Step 3.4 so the produced adversarial-review output is validated.\n"
    )


def _format_adapter_verdict_missing_request_changes(
    returncode: int, stdout_bytes: bytes
) -> str:
    """Render adapter exit without matching verdict banner as blocking CR."""
    stdout_len = len(stdout_bytes)
    return (
        "adversarial-gate: REQUEST_CHANGES\n\n"
        "### Issues\n"
        "- [CRITICAL] scripts/adversarial_gate_invoke.py:597-639 "
        "(confidence=1.0) — adapter returned exit "
        f"{returncode} without adversarial-gate verdict banner, so Step 3.4 "
        "could not prove the adapter approved or requested changes and must "
        f"not mint `exec`; adapter_stdout_bytes={stdout_len}\n"
        "  Recommendation: inspect the adapter invocation/override, require the "
        "canonical adversarial-gate verdict banner for exit 0/1, then rerun "
        "Step 3.4.\n"
    )


def _format_producer_nonzero_after_adapter_approve_request_changes(
    returncode: int, stderr_bytes: bytes
) -> str:
    """Render producer nonzero + adapter APPROVE as blocking CR."""
    stderr_tail = (
        stderr_bytes.decode("utf-8", errors="replace")
        .strip()
        .replace("\n", " | ")[-300:]
    )
    return (
        "adversarial-gate: REQUEST_CHANGES\n\n"
        "### Issues\n"
        "- [CRITICAL] scripts/adversarial_gate_invoke.py:704-711 "
        "(confidence=1.0) — producer exited nonzero after adapter APPROVE, "
        "so Step 3.4 cannot prove the reviewer completed cleanly and must not "
        f"mint `exec`: exit={returncode} stderr={stderr_tail!r}\n"
        "  Recommendation: inspect the producer failure, rerun Step 3.4, and "
        "only accept APPROVE when the producer exits cleanly after emitting a "
        "validated approve verdict.\n"
    )


# ---------- process-group kill (Finding #21) ----------


def _kill_process_group(
    proc: subprocess.Popen, pgid: Optional[int] = None
) -> None:
    """SIGTERM → 2s grace → unconditional SIGKILL on cached pgid → wait(5.0).

    Meta-dogfood Bug #3 (HIGH): the previous implementation only sent SIGKILL
    when ``proc.poll() is None`` (leader still alive). If the leader exited
    during the grace window while a descendant ignored SIGTERM, the loop
    broke and SIGKILL was skipped — the descendant survived and could mutate
    ``.review-loop/config.md`` after the cleanup-restore step ran.

    Defensive fix: cache the pgid eagerly (one ``getpgid`` call up front),
    then ALWAYS deliver SIGKILL to that cached pgid at the end of the grace
    window, regardless of leader liveness. Leader-exit is not proof the
    process group is empty. Best-effort, never raises.

    Meta-dogfood R2 Fix B: callers may pass an already-cached ``pgid`` from
    the spawn site. By that time the leader may already have exited (and
    ``os.getpgid(proc.pid)`` would raise / return wrong data), so trusting
    the spawn-time pgid is the only reliable option. When ``pgid is None``,
    we fall back to a fresh ``os.getpgid(proc.pid)`` lookup, preserving the
    existing behaviour for any caller that has not been updated yet.

    Descendant verification (e.g. enumerating the pgroup after SIGKILL) is
    deliberately out of scope — POSIX gives no portable, race-free primitive
    for "wait for every member of a pgroup to exit". The unconditional
    pgroup-wide SIGKILL is the strongest guarantee available here; a
    descendant that has already escaped the pgroup (setsid'd away) is out
    of reach for any caller and is documented as a known limitation.
    """
    if pgid is None:
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            return  # leader already gone and pgid unknown — nothing to kill.

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass

    grace_deadline = time.monotonic() + _SIGTERM_GRACE_SECS
    while time.monotonic() < grace_deadline:
        if proc.poll() is not None:
            # Leader exited; descendants may still be alive. Fall through to
            # the unconditional SIGKILL below rather than short-circuiting.
            break
        time.sleep(_SIGTERM_POLL_INTERVAL)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=_KILL_WAIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        pass


# ---------- cleanup (Finding #22 + #27) ----------


def _is_expected_codex_bootstrap_config(path: str) -> tuple[bool, Optional[str]]:
    """Return (matches, read_failure) for the disposable fallback bootstrap."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        return False, f"config-read failed: {e}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, None
    return text == _EXPECTED_CODEX_BOOTSTRAP_CONFIG, None


def _cleanup() -> Optional[str]:
    """Restore config snapshot (if any) and unlink tempfiles.

    Idempotent via _CLEANUP_DONE; exception-safe per-action. Unlink of the
    snapshot only runs AFTER restore is confirmed (Finding #27). `restore_ok`
    means either byte-identical, restored from snapshot, or intentionally
    absent-to-restored after fallback bootstrap cleanup. Returns a
    failure detail when config restore/delete cannot be proven successful.
    """
    global _CLEANUP_DONE, _CLEANUP_FAILURE, _CLEANUP_IN_PROGRESS
    if _CLEANUP_DONE:
        return _CLEANUP_FAILURE
    if _CLEANUP_IN_PROGRESS:
        return _CLEANUP_FAILURE or "cleanup re-entered before completion"

    _CLEANUP_IN_PROGRESS = True
    cleanup_failure: Optional[str] = None
    blocked = [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]
    try:
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    except (AttributeError, OSError, ValueError):
        old_mask = None

    try:
        if _SNAPSHOT_PATH and os.path.exists(_SNAPSHOT_PATH):
            restore_ok = False
            try:
                if os.path.exists(_CONFIG_PATH) and filecmp.cmp(
                    _SNAPSHOT_PATH, _CONFIG_PATH, shallow=False
                ):
                    restore_ok = True  # already byte-identical
                elif os.path.exists(_CONFIG_PATH):
                    is_bootstrap, read_failure = _is_expected_codex_bootstrap_config(
                        _CONFIG_PATH
                    )
                    if read_failure:
                        cleanup_failure = cleanup_failure or read_failure
                    elif is_bootstrap:
                        shutil.copy2(_SNAPSHOT_PATH, _CONFIG_PATH)
                        restore_ok = True
                    else:
                        cleanup_failure = (
                            cleanup_failure
                            or f"preserved unexpected existing config change: {_CONFIG_PATH}"
                        )
                        try:
                            sys.stderr.write(
                                "adversarial-gate: cleanup preserved unexpected "
                                f"existing {_CONFIG_PATH}; snapshot={_SNAPSHOT_PATH}\n"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    shutil.copy2(_SNAPSHOT_PATH, _CONFIG_PATH)
                    restore_ok = True
            except Exception as e:  # noqa: BLE001
                cleanup_failure = cleanup_failure or f"config-restore failed: {e}"
                try:
                    sys.stderr.write(
                        f"adversarial-gate: cleanup restore failed: {e}\n"
                    )
                except Exception:  # noqa: BLE001
                    pass
            if restore_ok:
                try:
                    os.unlink(_SNAPSHOT_PATH)
                except Exception as e:  # noqa: BLE001
                    _emit_runtime_warning(f"cleanup snapshot unlink failed: {e}")
            # If restore_ok is False, snapshot is left on disk for user recovery.

        # Meta-dogfood R2 Fix A + R6: if the config did NOT exist before our
        # fallback ``codex exec`` call but exists now, delete it only when it is
        # byte-for-byte the known disposable Codex bootstrap template. Anything
        # else may be a user-created config that appeared during the fallback
        # window; preserve it and block approval instead of silently unlinking.
        if (
            _FALLBACK_CONFIG_CLEANUP_ACTIVE
            and not _CONFIG_EXISTED_PRE_CALL
            and _SNAPSHOT_PATH is None
            and os.path.exists(_CONFIG_PATH)
        ):
            is_bootstrap, read_failure = _is_expected_codex_bootstrap_config(
                _CONFIG_PATH
            )
            if read_failure:
                cleanup_failure = cleanup_failure or read_failure
            elif is_bootstrap:
                try:
                    os.unlink(_CONFIG_PATH)
                    try:
                        sys.stderr.write(
                            "adversarial-gate: cleanup removed codex-created "
                            f"{_CONFIG_PATH}\n"
                        )
                    except Exception:  # noqa: BLE001
                        pass
                except Exception as e:  # noqa: BLE001
                    cleanup_failure = cleanup_failure or f"config-unlink failed: {e}"
            else:
                cleanup_failure = (
                    cleanup_failure
                    or f"preserved unexpected create-from-empty config: {_CONFIG_PATH}"
                )
                try:
                    sys.stderr.write(
                        "adversarial-gate: cleanup preserved unexpected "
                        f"create-from-empty {_CONFIG_PATH}\n"
                    )
                except Exception:  # noqa: BLE001
                    pass

        if _PROMPT_PATH and os.path.exists(_PROMPT_PATH):
            try:
                os.unlink(_PROMPT_PATH)
            except Exception:  # noqa: BLE001
                pass
    finally:
        if old_mask is not None:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            except (OSError, ValueError):
                pass
        _CLEANUP_FAILURE = cleanup_failure
        _CLEANUP_IN_PROGRESS = False
        _CLEANUP_DONE = True

    return cleanup_failure


def _cleanup_and_exit(signo, frame):
    """Signal handler: kill active child group FIRST, then restore."""
    global _active_proc, _active_pgid
    if _active_proc is not None:
        _kill_process_group(_active_proc, pgid=_active_pgid)
    _active_proc = None
    _active_pgid = None
    cleanup_failure = _cleanup()
    if cleanup_failure:
        try:
            sys.stderr.write(
                f"adversarial-gate: cleanup failed during signal: {cleanup_failure}\n"
            )
        except Exception:  # noqa: BLE001
            pass
    sys.exit(128 + signo)


def _install_signal_handlers() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _cleanup_and_exit)
        except (OSError, ValueError):
            pass  # SIGHUP unavailable in some contexts; best-effort.
    atexit.register(_cleanup)


# ---------- resolution helpers ----------


def _resolve_plugin_root() -> Optional[str]:
    """Resolve openai-codex plugin root.

    1. $CODEX_PLUGIN_ROOT env var (if set + exists).
    2. ~/.claude/plugins/cache/openai-codex/codex/*/ glob, take newest.
    Returns the root dir path, or None if neither path resolves.
    """
    env_root = os.environ.get("CODEX_PLUGIN_ROOT", "").strip()
    if env_root and os.path.isdir(env_root):
        return env_root
    home = os.path.expanduser("~")
    pattern = os.path.join(
        home, ".claude", "plugins", "cache", "openai-codex", "codex", "*"
    )
    candidates = sorted(
        (p for p in glob.glob(pattern) if os.path.isdir(p)),
        reverse=True,
    )
    return candidates[0] if candidates else None


def _resolve_companion_script(root: str) -> Optional[str]:
    """Resolve codex-companion.mjs inside the plugin root, if present."""
    candidate = os.path.join(root, "scripts", "codex-companion.mjs")
    return candidate if os.path.isfile(candidate) else None


def _resolve_schema_path(root: str) -> Optional[str]:
    """Resolve the cached review-output schema path."""
    candidate = os.path.join(root, "schemas", "review-output.schema.json")
    return candidate if os.path.isfile(candidate) else None


def _schema_path_detail(root: str) -> str:
    return os.path.join(root, "schemas", "review-output.schema.json")


# ---------- prompt rendering ----------


def _render_fallback_prompt(focus_text: str, review_target_desc: str) -> str:
    template_path = os.path.join(_SCRIPT_DIR, "adversarial_gate_fallback_prompt.txt")
    with open(template_path, "rb") as fh:
        template_src = fh.read().decode("utf-8")
    return string.Template(template_src).safe_substitute(
        FOCUS_TEXT=focus_text,
        REVIEW_TARGET_DESC=review_target_desc,
    )


# ---------- spawn + drain ----------


def _spawn_blocking_signals(argv, **popen_kwargs):
    """Popen wrapped in pthread_sigmask block (Finding #26).

    Blocks SIGINT/SIGTERM/SIGHUP across the Popen + assignment block so a
    pending signal handler cannot fire in the window between Popen
    returning and `_active_proc = proc` completing.
    """
    global _active_proc, _active_pgid
    blocked = [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]
    try:
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    except (AttributeError, OSError, ValueError):
        old_mask = None
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
        _active_proc = proc
        # Meta-dogfood R2 Fix B: cache the pgid eagerly at spawn time. The
        # leader can exit before any cleanup path runs, after which a fresh
        # ``os.getpgid`` lookup is unreliable. ``None`` here is acceptable
        # — ``_kill_process_group`` will fall back to its own lookup or
        # return early when no group is reachable.
        try:
            _active_pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            _active_pgid = None
    finally:
        if old_mask is not None:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            except (OSError, ValueError):
                pass
    return proc


def _run_with_drain(
    argv,
    timeout_secs: float,
    stdin_fp=None,
) -> tuple[bytes, bytes, int]:
    """Spawn argv with drain threads + wait timeout, return (stdout, stderr, returncode).

    On timeout: kill process group, SKIP runtime-timeout (no return).
    On OSError on Popen: SKIP runtime-error with detail.
    """
    global _active_proc, _active_pgid

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    drain_errors: list[str] = []
    drain_errors_lock = threading.Lock()

    def _drain(stream_name, stream, buf):
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                buf.extend(chunk)
        except Exception as e:  # noqa: BLE001
            with drain_errors_lock:
                drain_errors.append(f"{stream_name}: {type(e).__name__}: {e}")

    try:
        proc = _spawn_blocking_signals(
            argv,
            stdin=stdin_fp,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:
        _emit_skip("runtime-error", detail=str(e))

    stdout_thread = threading.Thread(
        target=_drain, args=("stdout", proc.stdout, stdout_buf), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain, args=("stderr", proc.stderr, stderr_buf), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    try:
        returncode = proc.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        # Meta-dogfood R2 Fix B: pass the spawn-time pgid. By the time we
        # get here, the leader may already be a zombie and a fresh getpgid
        # lookup is unreliable, but descendants in the original group may
        # still be alive and able to mutate state (e.g. ``.review-loop/
        # config.md``).
        _kill_process_group(proc, pgid=_active_pgid)
        stdout_thread.join(timeout=_DRAIN_JOIN_TIMEOUT)
        stderr_thread.join(timeout=_DRAIN_JOIN_TIMEOUT)
        _active_proc = None
        _active_pgid = None
        if stdout_buf.strip():
            _emit_uncertain_stdout_request_changes(
                "producer stdout captured before runtime-timeout",
                bytes(stdout_buf),
            )
        _emit_skip("runtime-timeout")

    stdout_thread.join(timeout=_DRAIN_JOIN_TIMEOUT)
    stderr_thread.join(timeout=_DRAIN_JOIN_TIMEOUT)

    # Finding #4 (HIGH silent-failure-hunter): if either drain thread is still
    # alive after join, returned bytes are truncated. SKIP rather than feed the
    # adapter a partial payload (which could exit 2 or, worse, encode wrong
    # verdict in raw mode).
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        # Meta-dogfood R2 Fix B: actively kill the process group before
        # clearing module state. Previously we just cleared ``_active_proc``
        # and SKIPped, leaving any surviving descendant free to mutate the
        # working tree after we returned.
        _kill_process_group(proc, pgid=_active_pgid)
        _active_proc = None
        _active_pgid = None
        if stdout_buf.strip():
            _emit_uncertain_stdout_request_changes(
                "producer stdout captured before drain-incomplete",
                bytes(stdout_buf),
            )
        _emit_skip("runtime-error", detail="drain-incomplete")

    if drain_errors:
        _kill_process_group(proc, pgid=_active_pgid)
        _active_proc = None
        _active_pgid = None
        detail = "drain-exception: " + "; ".join(drain_errors)
        if stdout_buf.strip():
            _emit_uncertain_stdout_request_changes(
                "producer stdout captured before drain-exception",
                bytes(stdout_buf),
            )
        _emit_skip("runtime-error", detail=detail)

    # Meta-dogfood R3 Fix A: normal leader exit is not proof that the process
    # group is empty. A stdio-closed descendant can survive the leader, then
    # mutate `.review-loop/config.md` after fallback cleanup restores/removes
    # it. Tear down the cached pgid on every completed invocation before
    # module state is cleared and before caller-side cleanup can run.
    _kill_process_group(proc, pgid=_active_pgid)

    _active_proc = None  # clear AFTER normal wait + drain join + pgroup teardown.
    _active_pgid = None
    return bytes(stdout_buf), bytes(stderr_buf), returncode


# ---------- adapter handoff (Finding #22 + #28) ----------


def _adapter_stdout_has_expected_verdict(
    stdout_bytes: bytes, returncode: int
) -> bool:
    expected = {
        0: b"adversarial-gate: APPROVE",
        1: b"adversarial-gate: REQUEST_CHANGES",
    }.get(returncode)
    if expected is None:
        return False
    for line in stdout_bytes.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped == expected
    return False


def _pipe_to_adapter(
    stdout_bytes: bytes,
    mode: str,
    producer_returncode: int = 0,
    producer_stderr: bytes = b"",
) -> int:
    """Pipe codex stdout into the adapter; emit adapter's stdout/stderr."""
    adapter_path = os.path.join(_SCRIPT_DIR, "adversarial_gate_adapter.py")
    adapter_exec = os.environ.get(
        "REVIEW_LOOP_ADAPTER_PYTHON", sys.executable
    )
    adapter_argv_override = os.environ.get(
        "REVIEW_LOOP_ADAPTER_ARGV_OVERRIDE"
    )
    if adapter_argv_override:
        argv = adapter_argv_override.split("\x1f") + ["--input-mode", mode]
    else:
        argv = [adapter_exec, adapter_path, "--input-mode", mode]
    try:
        result = subprocess.run(
            argv,
            input=stdout_bytes,
            capture_output=True,
            check=False,
        )
    except OSError as e:
        if stdout_bytes.strip():
            sys.stdout.write(
                _format_adapter_launch_failure_request_changes(str(e))
            )
            return 1
        _emit_skip("runtime-error", detail=str(e))
    if result.returncode == 2:
        # R8 meta-dogfood: produced-but-malformed adversarial output is a
        # blocking gate failure, not a controlled SKIP. Otherwise Step 3.4 can
        # mint exec after the adapter explicitly rejected the payload.
        sys.stderr.buffer.write(result.stderr)
        last_err_lines = (
            result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        )
        reason_detail = last_err_lines[-1] if last_err_lines else "no diagnostic"
        sys.stdout.write(
            _format_adapter_malformed_request_changes(reason_detail)
        )
        return 1
    if not _adapter_stdout_has_expected_verdict(result.stdout, result.returncode):
        sys.stderr.buffer.write(result.stderr)
        sys.stdout.write(
            _format_adapter_verdict_missing_request_changes(
                result.returncode, result.stdout
            )
        )
        return 1
    if result.returncode == 0 and producer_returncode != 0:
        sys.stderr.buffer.write(result.stderr)
        sys.stdout.write(
            _format_producer_nonzero_after_adapter_approve_request_changes(
                producer_returncode, producer_stderr
            )
        )
        return 1
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode


# ---------- auth marker ----------


def _check_auth_marker(stderr_bytes: bytes, returncode: int) -> None:
    """If returncode != 0, branch on auth-marker vs runtime-error.

    Finding #2 (HIGH silent-failure-hunter): include a truncated tail of the
    child's stderr in the `runtime-error` SKIP detail so callers can actually
    diagnose what `node`/`codex` reported.
    """
    if returncode == 0:
        return
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    if AUTH_RE.search(stderr_text):
        _emit_skip("codex-unauthenticated")
    tail = stderr_text.strip().replace("\n", " | ")[-200:]
    _emit_skip("runtime-error", detail=f"exit={returncode} stderr={tail!r}")


def _handle_producer_result(
    stdout_bytes: bytes, stderr_bytes: bytes, returncode: int, mode: str
) -> int:
    """Route produced stdout through the adapter before nonzero-exit SKIP."""
    if stdout_bytes.strip():
        return _pipe_to_adapter(
            stdout_bytes,
            mode=mode,
            producer_returncode=returncode,
            producer_stderr=stderr_bytes,
        )
    _check_auth_marker(stderr_bytes, returncode)
    return _pipe_to_adapter(stdout_bytes, mode=mode)


# ---------- argv builders ----------


def _build_plugin_argv(
    companion_script: str, focus_text: str
) -> list[str]:
    """Plugin path: node companion adversarial-review --scope working-tree --json -- <focus>.

    `--base` is intentionally OMITTED — git.mjs:resolveReviewTarget short-circuits
    on any --base value, ignoring --scope working-tree. The `--` sentinel keeps
    option-like focus text from overriding the fixed working-tree target.
    """
    node_exec = os.environ.get("REVIEW_LOOP_NODE", "node")
    return [
        node_exec,
        companion_script,
        "adversarial-review",
        "--scope",
        "working-tree",
        "--json",
        "--",
        focus_text,
    ]


def _build_fallback_argv(schema_path: str) -> list[str]:
    codex_exec = os.environ.get("REVIEW_LOOP_CODEX", "codex")
    return [
        codex_exec,
        "exec",
        "--output-schema",
        schema_path,
        "--sandbox",
        "read-only",
    ]


# ---------- main ----------


def main(argv: list[str]) -> int:
    global _SNAPSHOT_PATH, _PROMPT_PATH, _CONFIG_EXISTED_PRE_CALL
    global _FALLBACK_CONFIG_CLEANUP_ACTIVE

    parser = argparse.ArgumentParser(
        description="Step 3.4 terminal adversarial gate invoker.",
    )
    parser.add_argument("--focus-file", required=True,
                        help="path to focus text describing this round")
    parser.add_argument("--review-target-desc", default="working-tree changes")
    parser.add_argument("--timeout-secs", type=float, default=600.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve path and print argv; no spawn")
    args = parser.parse_args(argv)

    _install_signal_handlers()

    try:
        focus_text = ""
        try:
            with open(args.focus_file, "rb") as fh:
                focus_text = fh.read().decode("utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as e:
            _emit_skip("runtime-error", detail=str(e))

        # --- plugin-path test hook ---
        plugin_root_override = os.environ.get("REVIEW_LOOP_PLUGIN_ROOT")
        if plugin_root_override == "__force_unresolved__":
            _emit_skip(
                "plugin-root-unresolved",
                detail="REVIEW_LOOP_PLUGIN_ROOT=__force_unresolved__",
            )
        plugin_root = plugin_root_override or _resolve_plugin_root()
        if not plugin_root:
            _emit_skip(
                "plugin-root-unresolved",
                detail="CODEX_PLUGIN_ROOT unset/invalid and plugin cache root not found",
            )

        companion = _resolve_companion_script(plugin_root)
        force_fallback = os.environ.get("REVIEW_LOOP_FORCE_FALLBACK") == "1"

        if companion and not force_fallback:
            # Plugin path
            argv_to_run = _build_plugin_argv(companion, focus_text)
            if args.dry_run:
                sys.stdout.write(
                    "adversarial-gate: dry-run path=plugin argv="
                    + repr(argv_to_run)
                    + "\n"
                )
                return 0
            stdout_bytes, stderr_bytes, returncode = _run_with_drain(
                argv_to_run, timeout_secs=args.timeout_secs, stdin_fp=None
            )
            return _handle_producer_result(
                stdout_bytes, stderr_bytes, returncode, mode="plugin-json"
            )

        # Fallback path
        schema_path = _resolve_schema_path(plugin_root)
        if not schema_path:
            _emit_skip(
                "cache-schema-unresolved",
                detail=f"missing {_schema_path_detail(plugin_root)}",
            )

        # Meta-dogfood R2 Fix A: record whether ``.review-loop/config.md``
        # existed BEFORE we spawn ``codex exec``. ``codex exec`` will
        # bootstrap the file as a side effect even when the user started
        # with a clean tree; without this flag, ``_cleanup`` cannot tell
        # ``codex created it`` from ``user already had it`` and would
        # leave a stray config file behind.
        _FALLBACK_CONFIG_CLEANUP_ACTIVE = True
        _CONFIG_EXISTED_PRE_CALL = os.path.exists(_CONFIG_PATH)

        # Snapshot config if present
        if _CONFIG_EXISTED_PRE_CALL:
            snap: Optional[str] = None
            try:
                fd, snap = tempfile.mkstemp(prefix="adversarial-gate-config-")
                os.close(fd)
                shutil.copy2(_CONFIG_PATH, snap)
                _SNAPSHOT_PATH = snap
            except OSError as e:
                _unlink_partial_temp(snap, "partial snapshot")
                _emit_skip("runtime-error", detail=str(e))

        # Render prompt to tempfile
        prompt_path: Optional[str] = None
        tmp = None
        try:
            prompt_text = _render_fallback_prompt(
                focus_text=focus_text,
                review_target_desc=args.review_target_desc,
            )
            tmp = tempfile.NamedTemporaryFile(
                mode="wb", delete=False, prefix="adversarial-gate-prompt-"
            )
            prompt_path = tmp.name
            tmp.write(prompt_text.encode("utf-8"))
            tmp.close()
            _PROMPT_PATH = prompt_path
        except (OSError, UnicodeDecodeError, ValueError) as e:
            if tmp is not None:
                try:
                    tmp.close()
                except Exception:  # noqa: BLE001
                    pass
            _unlink_partial_temp(prompt_path, "partial prompt")
            _emit_skip("runtime-error", detail=str(e))

        argv_to_run = _build_fallback_argv(schema_path)
        if args.dry_run:
            sys.stdout.write(
                "adversarial-gate: dry-run path=fallback argv="
                + repr(argv_to_run)
                + " prompt="
                + str(_PROMPT_PATH)
                + "\n"
            )
            return 0

        with open(_PROMPT_PATH, "rb") as stdin_fp:
            stdout_bytes, stderr_bytes, returncode = _run_with_drain(
                argv_to_run,
                timeout_secs=args.timeout_secs,
                stdin_fp=stdin_fp,
            )
        return _handle_producer_result(
            stdout_bytes, stderr_bytes, returncode, mode="raw"
        )

    finally:
        cleanup_failure = _cleanup()
        if cleanup_failure:
            _emit_cleanup_request_changes(cleanup_failure)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
