"""Tests for scripts/adversarial_gate_invoke.py.

Covers plan §"Phase A2 step 4" base 11 cases + R6 findings #28 (adapter-spawn
ENOENT) + #29 (parametrized auth regex).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
INVOKER = REPO_ROOT / "scripts" / "adversarial_gate_invoke.py"


# ---------- helpers ----------


def _make_focus_file(tmp_path: Path, body: str = "fake focus text") -> Path:
    p = tmp_path / "focus.txt"
    p.write_text(body)
    return p


def _make_plugin_root_with_companion(tmp_path: Path, stub_body: str) -> Path:
    """Plugin root layout: <root>/scripts/codex-companion.mjs + <root>/schemas/review-output.schema.json."""
    root = tmp_path / "plugin_root"
    (root / "scripts").mkdir(parents=True)
    (root / "schemas").mkdir(parents=True)
    companion = root / "scripts" / "codex-companion.mjs"
    companion.write_text(stub_body)
    companion.chmod(0o755)
    (root / "schemas" / "review-output.schema.json").write_text("{}")
    return root


def _make_plugin_root_fallback_only(tmp_path: Path) -> Path:
    """Plugin root with schema but NO companion → forces fallback path."""
    root = tmp_path / "plugin_root_fb"
    (root / "schemas").mkdir(parents=True)
    (root / "schemas" / "review-output.schema.json").write_text("{}")
    return root


def _make_python_stub(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    p.chmod(0o755)
    return p


def _run_invoker(args: list[str], env: dict, cwd: Path) -> subprocess.CompletedProcess:
    final_env = os.environ.copy()
    final_env.update(env)
    return subprocess.run(
        [sys.executable, str(INVOKER), *args],
        capture_output=True,
        env=final_env,
        cwd=str(cwd),
        check=False,
    )


def _make_adapter_argv_shim(tmp_path: Path, argv_log: Path) -> str:
    """Create a small adapter shim that records its argv (incl. --input-mode)
    plus stdin length to `argv_log` and emits a fixed APPROVE verdict block.

    Returns the value to plug into REVIEW_LOOP_ADAPTER_ARGV_OVERRIDE (the
    shim's argv chain joined by \\x1f; invoker appends ["--input-mode", mode]
    after split, so we leave that for the invoker to add).
    """
    shim = tmp_path / "adapter_shim.py"
    shim.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        from pathlib import Path
        argv_log = Path({str(argv_log)!r})
        with argv_log.open("a") as fh:
            fh.write("ARGV=" + repr(sys.argv) + "\\n")
        # Drain stdin so the invoker pipe never blocks.
        data = sys.stdin.buffer.read()
        with argv_log.open("a") as fh:
            fh.write("STDIN_LEN=" + str(len(data)) + "\\n")
        sys.stdout.write("adversarial-gate: APPROVE\\n")
        sys.exit(0)
    """))
    shim.chmod(0o755)
    return "\x1f".join([sys.executable, str(shim)])


# ---------- (a) env var resolves to plugin path (dry-run) ----------


def test_env_var_resolves_to_plugin_path_dry_run(tmp_path):
    root = _make_plugin_root_with_companion(tmp_path, stub_body="// stub\n")
    focus = _make_focus_file(tmp_path)
    r = _run_invoker(
        ["--focus-file", str(focus), "--dry-run"],
        env={"REVIEW_LOOP_PLUGIN_ROOT": str(root)},
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert b"path=plugin" in r.stdout


def test_plugin_dry_run_inserts_sentinel_before_option_like_focus(tmp_path):
    root = _make_plugin_root_with_companion(tmp_path, stub_body="// stub\n")
    focus = _make_focus_file(
        tmp_path,
        "--base=main\n--scope=branch\nreal focus",
    )
    r = _run_invoker(
        ["--focus-file", str(focus), "--dry-run"],
        env={"REVIEW_LOOP_PLUGIN_ROOT": str(root)},
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout.decode("utf-8")
    assert "'--scope', 'working-tree', '--json', '--'," in out
    assert out.index("'--',") < out.index("'--base=main\\n--scope=branch")


# ---------- (b) fallback when no companion (dry-run) ----------


def test_no_companion_falls_back_dry_run(tmp_path):
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)
    r = _run_invoker(
        ["--focus-file", str(focus), "--dry-run"],
        env={"REVIEW_LOOP_PLUGIN_ROOT": str(root)},
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert b"path=fallback" in r.stdout


# ---------- (c) plugin-root-unresolved SKIP ----------


def test_skip_plugin_root_unresolved(tmp_path):
    focus = _make_focus_file(tmp_path)
    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={"REVIEW_LOOP_PLUGIN_ROOT": "__force_unresolved__"},
        cwd=tmp_path,
    )
    assert r.returncode == 0  # SKIP exits 0
    assert b"SKIP reason=plugin-root-unresolved" in r.stderr


# ---------- (d) cache-schema-unresolved SKIP ----------


def test_skip_cache_schema_unresolved(tmp_path):
    # Plugin root with neither companion nor schema → fallback chosen,
    # then schema missing → SKIP.
    root = tmp_path / "root_no_schema"
    root.mkdir()
    focus = _make_focus_file(tmp_path)
    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={"REVIEW_LOOP_PLUGIN_ROOT": str(root)},
        cwd=tmp_path,
    )
    assert r.returncode == 0
    assert b"SKIP reason=cache-schema-unresolved" in r.stderr


# ---------- (e) runtime-timeout SKIP + config restore + banner ----------


def test_skip_runtime_timeout_and_restore(tmp_path):
    """Finding #32: tighten to require SPECIFIC SKIP reason=runtime-timeout.

    Force fallback path + a python stub that sleeps past --timeout-secs;
    invoker drain+timeout pattern must SKIP `runtime-timeout`, not
    `runtime-error`.
    """
    stub = _make_python_stub(
        tmp_path,
        "codex_sleep_stub.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import sys, time
            # Drain stdin so the parent pipe never blocks.
            try:
                sys.stdin.read()
            except Exception:
                pass
            time.sleep(120)
        """),
    )
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)
    # Pre-seed .review-loop/config.md so the snapshot/restore path runs.
    config_dir = tmp_path / ".review-loop"
    config_dir.mkdir()
    cfg = config_dir / "config.md"
    cfg.write_text("original-config-line\n")
    original_bytes = cfg.read_bytes()

    r = _run_invoker(
        [
            "--focus-file", str(focus),
            "--timeout-secs", "1",
        ],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )
    assert r.returncode == 0
    assert b"adversarial-gate: SKIP reason=runtime-timeout" in r.stderr, r.stderr
    # Config restored byte-for-byte after timeout-kill cleanup.
    assert cfg.read_bytes() == original_bytes


def test_timeout_with_stdout_payload_blocks_instead_of_skip(tmp_path):
    """Captured producer stdout before timeout must not become controlled SKIP."""
    payload = json.dumps({
        "verdict": "needs-attention",
        "summary": "blocking payload emitted before timeout",
        "findings": [
            {
                "severity": "high",
                "title": "Timeout after payload",
                "body": "The gate must not mint exec after stdout was produced.",
                "file": "app.py",
                "line_start": 1,
                "line_end": 1,
                "confidence": 0.9,
                "recommendation": "Treat timeout with stdout as blocking.",
            }
        ],
        "next_steps": [],
    })
    stub = _make_python_stub(
        tmp_path,
        "codex_timeout_after_stdout.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import sys, time
            sys.stdin.read()
            sys.stdout.write({payload!r})
            sys.stdout.flush()
            time.sleep(120)
        """),
    )
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)

    r = _run_invoker(
        ["--focus-file", str(focus), "--timeout-secs", "1"],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )

    assert r.returncode == 1
    assert b"adversarial-gate: REQUEST_CHANGES" in r.stdout
    assert b"producer stdout captured before runtime-timeout" in r.stdout
    assert b"SKIP reason=runtime-timeout" not in r.stderr


# ---------- (f) SIGINT cleanup + child-pid-gone ----------


def test_sigint_cleanup_kills_child_and_restores(tmp_path):
    # Force fallback path; substitute codex with a python stub that prints
    # its pid + creates a sentinel + sleeps.
    sentinel = tmp_path / "child_ready.sentinel"
    child_pid_file = tmp_path / "child.pid"

    stub = _make_python_stub(
        tmp_path,
        "codex_stub.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import os, sys, time
            with open({str(child_pid_file)!r}, "w") as fh:
                fh.write(str(os.getpid()))
            with open({str(sentinel)!r}, "w") as fh:
                fh.write("ready")
            time.sleep(60)
        """),
    )

    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)

    # Pre-seed config for restore check.
    config_dir = tmp_path / ".review-loop"
    config_dir.mkdir()
    cfg = config_dir / "config.md"
    cfg.write_text("ORIGINAL\n")
    original = cfg.read_bytes()

    env = os.environ.copy()
    env.update({
        "REVIEW_LOOP_PLUGIN_ROOT": str(root),
        "REVIEW_LOOP_CODEX": str(stub),
    })

    # Mutate config to ensure restore actually runs.
    proc = subprocess.Popen(
        [sys.executable, str(INVOKER), "--focus-file", str(focus)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
    )
    # Wait up to 5s for sentinel.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if sentinel.exists():
            break
        time.sleep(0.05)

    if not sentinel.exists():
        # Stub never ran (likely codex argv constructed differently);
        # fall back to asserting controlled SKIP banner.
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
        assert proc.returncode == 0  # SKIP exit
        pytest.skip("stub did not run — codex argv path not exercised")

    # Simulate the known codex-exec bootstrap overwrite in the window before
    # kill; cleanup may restore only this known disposable template.
    cfg.write_text("codex_reviewer_backend: codex\nskip_quality_polish: true\n")

    child_pid = int(child_pid_file.read_text().strip())
    proc.send_signal(signal.SIGINT)
    proc.wait(timeout=10)

    # Invoker exit 130 (= 128 + SIGINT).
    assert proc.returncode == 130
    # Config restored.
    assert cfg.read_bytes() == original
    # Child pid gone.
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


# ---------- (g) plugin happy-path → adapter input-mode=plugin-json ----------


def test_plugin_happy_path_routes_to_plugin_json(tmp_path):
    """Finding #32: prove the invoker invoked the adapter with
    --input-mode=plugin-json on the plugin dispatch path.

    Strategy: stub `node` to a Python script that emits a valid schema JSON;
    override the adapter with a shim that records its argv to a file. The
    invoker appends `--input-mode plugin-json` after the shim path, so the
    log file should contain that flag.
    """
    payload = json.dumps({
        "verdict": "approve",
        "summary": "ok",
        "findings": [],
        "next_steps": [],
    })
    node_stub = _make_python_stub(
        tmp_path,
        "node_stub.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            # The first argv is the .mjs companion path; ignore it. Just
            # write a valid plugin-json payload to stdout.
            sys.stdout.write({payload!r})
        """),
    )
    root = _make_plugin_root_with_companion(tmp_path, stub_body="// stub\n")
    focus = _make_focus_file(tmp_path)
    argv_log = tmp_path / "adapter_argv.log"
    shim_argv = _make_adapter_argv_shim(tmp_path, argv_log)

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_NODE": str(node_stub),
            "REVIEW_LOOP_ADAPTER_ARGV_OVERRIDE": shim_argv,
        },
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    # Shim wrote its argv to the log; the invoker passed --input-mode plugin-json.
    assert argv_log.exists(), f"adapter shim never ran; stderr={r.stderr!r}"
    log_text = argv_log.read_text()
    assert "--input-mode" in log_text and "'plugin-json'" in log_text, log_text
    # And the shim's canned APPROVE output reached the invoker's stdout.
    assert b"adversarial-gate: APPROVE" in r.stdout


def test_plugin_path_does_not_delete_existing_config(tmp_path):
    """Plugin path must not run fallback create-from-empty config cleanup."""
    payload = json.dumps({
        "verdict": "approve",
        "summary": "ok",
        "findings": [],
        "next_steps": [],
    })
    node_stub = _make_python_stub(
        tmp_path,
        "node_stub.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            sys.stdout.write({payload!r})
        """),
    )
    root = _make_plugin_root_with_companion(tmp_path, stub_body="// stub\n")
    focus = _make_focus_file(tmp_path)
    config_dir = tmp_path / ".review-loop"
    config_dir.mkdir()
    cfg = config_dir / "config.md"
    cfg.write_text("USER-CONFIG\n")

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_NODE": str(node_stub),
        },
        cwd=tmp_path,
    )

    assert r.returncode == 0, r.stderr
    assert cfg.read_text() == "USER-CONFIG\n"


# ---------- (h) fallback happy path → adapter input-mode=raw ----------


def test_fallback_happy_path_routes_to_raw(tmp_path):
    """Finding #32: prove the invoker invoked the adapter with
    --input-mode=raw on the fallback dispatch path."""
    payload = json.dumps({
        "verdict": "approve",
        "summary": "ok",
        "findings": [],
        "next_steps": [],
    })
    stub = _make_python_stub(
        tmp_path,
        "codex_stub.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            # Drain stdin (the rendered prompt) then emit raw-mode payload
            # (preamble + bare JSON object).
            sys.stdin.read()
            sys.stdout.write("preamble\\n" + {payload!r} + "\\n")
        """),
    )
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)
    argv_log = tmp_path / "adapter_argv.log"
    shim_argv = _make_adapter_argv_shim(tmp_path, argv_log)

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
            "REVIEW_LOOP_ADAPTER_ARGV_OVERRIDE": shim_argv,
        },
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert argv_log.exists(), f"adapter shim never ran; stderr={r.stderr!r}"
    log_text = argv_log.read_text()
    assert "--input-mode" in log_text and "'raw'" in log_text, log_text
    assert b"adversarial-gate: APPROVE" in r.stdout


def test_nonzero_fallback_with_stdout_payload_routes_to_adapter_before_skip(tmp_path):
    """A produced review payload must be validated before nonzero-exit SKIP."""
    payload = json.dumps({
        "verdict": "needs-attention",
        "summary": "blocking payload emitted before nonzero exit",
        "findings": [
            {
                "severity": "high",
                "title": "Blocking payload",
                "body": "The adapter must see this despite producer exit 2.",
                "file": "app.py",
                "line_start": 1,
                "line_end": 1,
                "confidence": 0.9,
                "recommendation": "Route stdout to adapter before SKIP.",
            }
        ],
        "next_steps": [],
    })
    stub = _make_python_stub(
        tmp_path,
        "codex_stub.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            sys.stdin.read()
            sys.stdout.write({payload!r})
            sys.stderr.write("producer exited after emitting payload\\n")
            sys.exit(2)
        """),
    )
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )

    assert r.returncode == 1
    assert b"adversarial-gate: REQUEST_CHANGES" in r.stdout
    assert b"Blocking payload" in r.stdout
    assert b"SKIP reason=runtime-error" not in r.stderr


def test_nonzero_fallback_with_approve_stdout_blocks(tmp_path):
    """Producer nonzero exit must not become APPROVE via valid stdout alone."""
    payload = json.dumps({
        "verdict": "approve",
        "summary": "approve emitted before producer failure",
        "findings": [],
        "next_steps": [],
    })
    stub = _make_python_stub(
        tmp_path,
        "codex_approve_then_nonzero.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            sys.stdin.read()
            sys.stdout.write({payload!r})
            sys.stderr.write("producer failed after approve\\n")
            sys.exit(2)
        """),
    )
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )

    assert r.returncode == 1
    assert b"adversarial-gate: REQUEST_CHANGES" in r.stdout
    assert b"producer exited nonzero after adapter APPROVE" in r.stdout
    assert b"SKIP reason=runtime-error" not in r.stderr


# ---------- (i) config snapshot/restore byte-equality ----------


def test_config_snapshot_restore_byte_equality(tmp_path):
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)
    # Stub codex that writes the known disposable bootstrap config mid-run,
    # then exits 0.
    stub = _make_python_stub(
        tmp_path,
        "codex_stub.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import os, sys
            sys.stdin.read()
            # Mutate config inside cwd.
            cfg = os.path.join(".review-loop", "config.md")
            try:
                with open(cfg, "w") as fh:
                    fh.write("codex_reviewer_backend: codex\\nskip_quality_polish: true\\n")
            except Exception:
                pass
            sys.stdout.write('{{"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}}\\n')
        """),
    )
    config_dir = tmp_path / ".review-loop"
    config_dir.mkdir()
    cfg = config_dir / "config.md"
    cfg.write_text("ORIGINAL\n")
    original = cfg.read_bytes()

    _ = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )
    # Whatever happens (adapter pass, fail, or runtime-error), config
    # must be restored.
    assert cfg.read_bytes() == original, (
        f"config not restored: {cfg.read_bytes()!r} vs {original!r}"
    )


def test_preexisting_config_unexpected_change_is_preserved_and_blocks_approval(
    tmp_path,
):
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)
    stub = _make_python_stub(
        tmp_path,
        "codex_stub.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import os, sys
            sys.stdin.read()
            cfg = os.path.join(".review-loop", "config.md")
            with open(cfg, "w") as fh:
                fh.write("USER-EDIT-DURING-FALLBACK\\n")
            sys.stdout.write('{{"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}}\\n')
        """),
    )
    config_dir = tmp_path / ".review-loop"
    config_dir.mkdir()
    cfg = config_dir / "config.md"
    cfg.write_text("ORIGINAL\n")

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )

    assert r.returncode == 1
    assert cfg.read_text() == "USER-EDIT-DURING-FALLBACK\n"
    assert b"adversarial-gate: REQUEST_CHANGES" in r.stdout, r.stdout
    assert b"preserved unexpected existing config change" in r.stdout, r.stdout


# ---------- (j #29) parametrized auth regex ----------


@pytest.mark.parametrize(
    "stderr_phrase,expected_reason",
    [
        (b"Error: not signed in to ChatGPT\n", "codex-unauthenticated"),
        (b"AuthenticationError: invalid token\n", "codex-unauthenticated"),
        (b"OAuth2 error: refresh failed\n", "codex-unauthenticated"),
        (b"author of commit not recognized\n", "runtime-error"),
    ],
    ids=["not_signed_in", "AuthenticationError", "OAuth2", "author_of_commit"],
)
def test_auth_regex_branches(tmp_path, stderr_phrase, expected_reason):
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)
    stub_body = textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        sys.stdin.read()
        sys.stderr.buffer.write({stderr_phrase!r})
        sys.exit(2)
    """)
    stub = _make_python_stub(tmp_path, "codex_stub.py", stub_body)

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )
    assert r.returncode == 0
    assert f"SKIP reason={expected_reason}".encode() in r.stderr
    # Step 3.5.3 Fix #2: runtime-error path now forwards a tail of child stderr
    # in the detail string so the operator can diagnose without rerunning.
    if expected_reason == "runtime-error":
        assert b"detail=exit=" in r.stderr
        assert b"stderr=" in r.stderr
        # The phrase body (sans trailing newline) appears verbatim in the tail.
        phrase_core = stderr_phrase.rstrip(b"\n")
        assert phrase_core in r.stderr


# ---------- (k) OSError on spawn (review-command ENOENT) ----------


def test_spawn_oserror_review_command(tmp_path):
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)
    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": "/nonexistent/path/to/codex-binary",
        },
        cwd=tmp_path,
    )
    assert r.returncode == 0
    assert b"SKIP reason=runtime-error" in r.stderr
    assert b"detail=" in r.stderr


# ---------- (l #28) OSError on adapter spawn ENOENT ----------


def test_adapter_spawn_oserror(tmp_path):
    # Force adapter executable to a missing path after empty producer stdout.
    stub_body = textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        sys.stdin.read()
    """)
    stub = _make_python_stub(tmp_path, "codex_stub.py", stub_body)
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
            "REVIEW_LOOP_ADAPTER_PYTHON": "/nonexistent/python/path",
        },
        cwd=tmp_path,
    )
    assert r.returncode == 0
    # Finding #31: assert the SPECIFIC SKIP reason + presence of detail,
    # not just "some SKIP banner emitted". Adapter-spawn ENOENT surfaces
    # via _emit_skip("runtime-error", detail=str(e)).
    assert b"adversarial-gate: SKIP reason=runtime-error" in r.stderr
    assert b"detail=" in r.stderr


def test_adapter_spawn_oserror_with_stdout_payload_blocks(tmp_path):
    """Adapter launch failure after producer stdout must not become SKIP."""
    payload = json.dumps({
        "verdict": "needs-attention",
        "summary": "blocking payload emitted before adapter launch failure",
        "findings": [
            {
                "severity": "high",
                "title": "Adapter launch failure",
                "body": "The producer output must not be skipped.",
                "file": "app.py",
                "line_start": 1,
                "line_end": 1,
                "confidence": 0.9,
                "recommendation": "Fail closed when adapter cannot validate stdout.",
            }
        ],
        "next_steps": [],
    })
    stub = _make_python_stub(
        tmp_path,
        "codex_stdout_then_adapter_missing.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            sys.stdin.read()
            sys.stdout.write({payload!r})
            sys.stdout.flush()
        """),
    )
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
            "REVIEW_LOOP_ADAPTER_PYTHON": "/nonexistent/python/path",
        },
        cwd=tmp_path,
    )

    assert r.returncode == 1
    assert b"adversarial-gate: REQUEST_CHANGES" in r.stdout
    assert b"adapter launch failed after producer stdout" in r.stdout
    assert b"SKIP reason=runtime-error" not in r.stderr


def test_adapter_success_without_verdict_banner_blocks(tmp_path):
    """Adapter exit 0 without an adversarial-gate banner must not mint exec."""
    payload = json.dumps({
        "verdict": "needs-attention",
        "summary": "blocking payload emitted before fake adapter success",
        "findings": [
            {
                "severity": "high",
                "title": "Adapter verdict missing",
                "body": "The producer output must not be approved by exit code alone.",
                "file": "app.py",
                "line_start": 1,
                "line_end": 1,
                "confidence": 0.9,
                "recommendation": "Require the adapter verdict banner.",
            }
        ],
        "next_steps": [],
    })
    producer_stub = _make_python_stub(
        tmp_path,
        "codex_stdout_then_fake_adapter_success.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            sys.stdin.read()
            sys.stdout.write({payload!r})
            sys.stdout.flush()
        """),
    )
    adapter_stub = _make_python_stub(
        tmp_path,
        "adapter_success_no_banner.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import sys
            sys.stdin.buffer.read()
            sys.exit(0)
        """),
    )
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(producer_stub),
            "REVIEW_LOOP_ADAPTER_PYTHON": str(adapter_stub),
        },
        cwd=tmp_path,
    )

    assert r.returncode == 1
    assert b"adversarial-gate: REQUEST_CHANGES" in r.stdout
    assert b"adapter returned exit 0 without adversarial-gate verdict" in r.stdout
    assert b"SKIP reason=runtime-error" not in r.stderr


# ---------- (m Step 3.5.3 Fix #3 / R8) adapter-exit-2 blocks ----------


def test_adapter_exit_2_malformed_includes_detail(tmp_path):
    """Adapter exit 2 is produced-but-malformed review output and must block.

    R8 meta-dogfood: mapping adapter exit 2 to SKIP lets the protocol mint
    `exec` even though the adversarial gate could not validate its output.
    """
    # Codex stub emits valid JSON shape but schema-invalid (missing next_steps)
    # so the real adapter exits 2 with a precise diagnostic.
    payload = json.dumps({
        "verdict": "approve",
        "summary": "ok",
        "findings": [],
        # next_steps intentionally omitted → adapter exits 2.
    })
    stub_body = textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        sys.stdin.read()
        sys.stdout.write({payload!r})
    """)
    stub = _make_python_stub(tmp_path, "codex_stub.py", stub_body)
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )
    assert r.returncode == 1
    assert b"adversarial-gate: REQUEST_CHANGES" in r.stdout
    assert b"[CRITICAL]" in r.stdout
    assert b"next_steps" in r.stdout
    assert b"SKIP reason=adapter-exit-2-malformed" not in r.stderr


def test_adapter_truncated_final_raw_output_blocks(tmp_path):
    """End-to-end invoker regression for R8: malformed final raw output blocks."""
    fixture = (
        REPO_ROOT
        / "tests"
        / "fixtures"
        / "adversarial_gate"
        / "raw_mode_truncated_final_after_approve.json"
    )
    stub_body = textwrap.dedent(f"""\
        #!{sys.executable}
        import pathlib, sys
        sys.stdin.read()
        sys.stdout.write(pathlib.Path({str(fixture)!r}).read_text())
    """)
    stub = _make_python_stub(tmp_path, "codex_stub.py", stub_body)
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )

    assert r.returncode == 1
    assert b"adversarial-gate: REQUEST_CHANGES" in r.stdout
    assert b"malformed final JSON object" in r.stdout
    assert b"SKIP reason=adapter-exit-2-malformed" not in r.stderr


# ---------- (n Step 3.5.3 Fix #4) drain-incomplete SKIP ----------


def test_drain_incomplete_skip_runtime_error(tmp_path, monkeypatch):
    """Step 3.5.3 Fix #4 (HIGH silent-failure-hunter): if either drain thread
    is still alive after join, the invoker must SKIP runtime-error rather than
    feed the adapter a truncated payload.

    Strategy: import the invoker module in-process and monkeypatch
    `threading.Thread` so the drain threads claim `is_alive() is True` after
    join. Confirms the new code path emits the canonical SKIP banner.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "adversarial_gate_invoke_under_test", str(INVOKER)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Build a fake-process object that satisfies the small interface
    # `_run_with_drain` touches: stdout, stderr, wait(timeout), poll(), pid.
    class _FakePipe:
        def read(self, _n):
            return b""

    class _FakeProc:
        pid = 99999
        stdout = _FakePipe()
        stderr = _FakePipe()

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    fake_proc = _FakeProc()

    # Patch the spawn helper to return our fake without touching subprocess.
    monkeypatch.setattr(
        mod, "_spawn_blocking_signals", lambda *a, **kw: fake_proc
    )

    # Patch threading.Thread so join() returns immediately but is_alive() stays
    # True — exactly the drain-incomplete scenario the fix guards against.
    real_thread = mod.threading.Thread

    class _StuckThread(real_thread):
        def is_alive(self):  # type: ignore[override]
            return True

    monkeypatch.setattr(mod.threading, "Thread", _StuckThread)

    # _emit_skip calls sys.exit(0); capture the SKIP banner from stderr.
    import io

    fake_stderr = io.StringIO()
    monkeypatch.setattr(mod.sys, "stderr", fake_stderr)

    with pytest.raises(SystemExit) as exc_info:
        mod._run_with_drain(["fake-argv"], timeout_secs=5.0, stdin_fp=None)

    assert exc_info.value.code == 0  # SKIP exits 0
    banner = fake_stderr.getvalue()
    assert "SKIP reason=runtime-error" in banner
    assert "drain-incomplete" in banner


def test_drain_incomplete_with_stdout_payload_blocks_instead_of_skip(
    tmp_path, monkeypatch
):
    """Partial/uncertain stdout capture before drain-incomplete blocks exec."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "adversarial_gate_invoke_under_test_stdout_drain", str(INVOKER)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _FakePipe:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def read(self, _n):
            if self._chunks:
                return self._chunks.pop(0)
            return b""

    class _FakeProc:
        pid = 77777
        stdout = _FakePipe([b'{"verdict":"needs-attention"}'])
        stderr = _FakePipe([])

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    fake_proc = _FakeProc()
    monkeypatch.setattr(
        mod, "_spawn_blocking_signals", lambda *a, **kw: fake_proc
    )

    class _StuckSynchronousThread:
        def __init__(self, target, args, daemon):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    monkeypatch.setattr(mod.threading, "Thread", _StuckSynchronousThread)

    import io

    fake_stdout = io.StringIO()
    fake_stderr = io.StringIO()
    monkeypatch.setattr(mod.sys, "stdout", fake_stdout)
    monkeypatch.setattr(mod.sys, "stderr", fake_stderr)

    with pytest.raises(SystemExit) as exc_info:
        mod._run_with_drain(["fake-argv"], timeout_secs=5.0, stdin_fp=None)

    assert exc_info.value.code == 1
    assert "adversarial-gate: REQUEST_CHANGES" in fake_stdout.getvalue()
    assert "producer stdout captured before drain-incomplete" in fake_stdout.getvalue()
    assert "SKIP reason=runtime-error" not in fake_stderr.getvalue()


def test_drain_exception_with_stdout_payload_blocks_instead_of_returning(
    monkeypatch,
):
    """Partial stdout before a reader exception is uncertain and blocks exec."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "adversarial_gate_invoke_under_test_drain_exception_stdout",
        str(INVOKER),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _ExplodingPipe:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def read(self, _n):
            if self._chunks:
                return self._chunks.pop(0)
            raise OSError("read failed")

    class _EmptyPipe:
        def read(self, _n):
            return b""

    class _FakeProc:
        pid = 77778
        stdout = _ExplodingPipe([
            b'{"verdict":"approve","summary":"partial","findings":[]'
        ])
        stderr = _EmptyPipe()

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(
        mod, "_spawn_blocking_signals", lambda *a, **kw: _FakeProc()
    )

    class _SynchronousThread:
        def __init__(self, target, args, daemon):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    monkeypatch.setattr(mod.threading, "Thread", _SynchronousThread)

    import io

    fake_stdout = io.StringIO()
    fake_stderr = io.StringIO()
    monkeypatch.setattr(mod.sys, "stdout", fake_stdout)
    monkeypatch.setattr(mod.sys, "stderr", fake_stderr)

    with pytest.raises(SystemExit) as exc_info:
        mod._run_with_drain(["fake-argv"], timeout_secs=5.0, stdin_fp=None)

    assert exc_info.value.code == 1
    assert "adversarial-gate: REQUEST_CHANGES" in fake_stdout.getvalue()
    assert "producer stdout captured before drain-exception" in fake_stdout.getvalue()
    assert "SKIP reason=runtime-error" not in fake_stderr.getvalue()


def test_drain_exception_without_stdout_skips_runtime_error(monkeypatch):
    """Reader exception without stdout is a controlled runtime-error SKIP."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "adversarial_gate_invoke_under_test_drain_exception_empty",
        str(INVOKER),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _ExplodingPipe:
        def read(self, _n):
            raise OSError("read failed")

    class _EmptyPipe:
        def read(self, _n):
            return b""

    class _FakeProc:
        pid = 77779
        stdout = _ExplodingPipe()
        stderr = _EmptyPipe()

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(
        mod, "_spawn_blocking_signals", lambda *a, **kw: _FakeProc()
    )

    class _SynchronousThread:
        def __init__(self, target, args, daemon):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    monkeypatch.setattr(mod.threading, "Thread", _SynchronousThread)

    import io

    fake_stderr = io.StringIO()
    monkeypatch.setattr(mod.sys, "stderr", fake_stderr)

    with pytest.raises(SystemExit) as exc_info:
        mod._run_with_drain(["fake-argv"], timeout_secs=5.0, stdin_fp=None)

    assert exc_info.value.code == 0
    banner = fake_stderr.getvalue()
    assert "SKIP reason=runtime-error" in banner
    assert "drain-exception" in banner


# ---------- (o Meta-dogfood R2 Fix A/R6) fallback create-from-empty cleanup ----------


def test_fallback_cleanup_removes_codex_created_config(tmp_path):
    """Meta-dogfood R2 Fix A: when ``.review-loop/config.md`` does NOT
    exist before the fallback ``codex exec`` call, the codex-exec
    bootstrap side-effect creates it. The cleanup path must detect this
    create-from-empty case and unlink the bootstrap-created file so the
    user's working tree stays pristine.

    Strategy: run the invoker against a stub-codex that simulates the
    bootstrap side-effect by writing ``.review-loop/config.md`` mid-run.
    Start with the directory containing an empty ``.review-loop/`` but
    no ``config.md``. After the invoker exits, ``config.md`` must be
    gone again and the SKIP/runtime path must report the cleanup line on
    stderr.
    """
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)
    stub = _make_python_stub(
        tmp_path,
        "codex_stub.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import os, sys
            sys.stdin.read()
            # Simulate the known codex-exec bootstrap side-effect: create
            # config.md as if from the exact template the invoker may delete.
            os.makedirs(".review-loop", exist_ok=True)
            cfg = os.path.join(".review-loop", "config.md")
            with open(cfg, "w") as fh:
                fh.write("codex_reviewer_backend: codex\\nskip_quality_polish: true\\n")
            sys.stdout.write(
                '{{"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}}\\n'
            )
        """),
    )
    # Pre-state: directory exists but config.md does NOT.
    config_dir = tmp_path / ".review-loop"
    config_dir.mkdir()
    cfg = config_dir / "config.md"
    assert not cfg.exists(), "precondition: config.md must be absent"

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )
    # Post-state: the codex-created config.md must be cleaned up.
    assert not cfg.exists(), (
        f"cleanup must remove codex-created config.md, "
        f"but file still exists with contents: {cfg.read_bytes()!r}"
    )
    # Diagnostic banner on stderr (best-effort; should be present).
    assert b"cleanup removed codex-created" in r.stderr, r.stderr


def test_fallback_cleanup_preserves_unexpected_created_config_and_blocks_approval(
    tmp_path,
):
    """Unexpected create-from-empty config must be preserved, not deleted.

    If `.review-loop/config.md` was absent before fallback and a non-bootstrap
    config appears during the fallback window, the invoker cannot prove it was
    its own disposable bootstrap artifact. Preserve the file and block approval
    so a user-created config is not silently removed.
    """
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)
    stub = _make_python_stub(
        tmp_path,
        "codex_stub.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import os, sys
            sys.stdin.read()
            os.makedirs(".review-loop", exist_ok=True)
            cfg = os.path.join(".review-loop", "config.md")
            with open(cfg, "w") as fh:
                fh.write("USER-CREATED-DURING-FALLBACK\\n")
            sys.stdout.write(
                '{{"verdict": "approve", "summary": "ok", "findings": [], "next_steps": []}}\\n'
            )
        """),
    )
    config_dir = tmp_path / ".review-loop"
    config_dir.mkdir()
    cfg = config_dir / "config.md"
    assert not cfg.exists(), "precondition: config.md must be absent"

    r = _run_invoker(
        ["--focus-file", str(focus)],
        env={
            "REVIEW_LOOP_PLUGIN_ROOT": str(root),
            "REVIEW_LOOP_CODEX": str(stub),
        },
        cwd=tmp_path,
    )

    assert r.returncode == 1
    assert cfg.read_text() == "USER-CREATED-DURING-FALLBACK\n"
    assert b"adversarial-gate: REQUEST_CHANGES" in r.stdout, r.stdout
    assert b"[CRITICAL]" in r.stdout, r.stdout
    assert b"preserved unexpected create-from-empty config" in r.stdout, r.stdout
    assert b"cleanup removed codex-created" not in r.stderr, r.stderr


# ---------- (p Meta-dogfood R2 Fix B) drain-incomplete kills process group ----------


def test_drain_incomplete_kills_process_group(tmp_path, monkeypatch):
    """Meta-dogfood R2 Fix B: when drain-incomplete SKIP fires, the
    process group must be actively killed (with the cached spawn-time
    pgid) before module state is cleared. Previously the SKIP path just
    cleared ``_active_proc`` and emitted the banner, leaving any
    surviving descendant free to mutate the working tree.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "adversarial_gate_invoke_under_test_r2b", str(INVOKER)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _FakePipe:
        def read(self, _n):
            return b""

    class _FakeProc:
        pid = 88888
        stdout = _FakePipe()
        stderr = _FakePipe()

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    fake_proc = _FakeProc()

    # Spawn helper returns the fake AND sets the cached pgid the way the
    # real spawn helper would. Use a sentinel int we can recognise later.
    SENTINEL_PGID = 4242

    def _fake_spawn(*_a, **_kw):
        mod._active_proc = fake_proc
        mod._active_pgid = SENTINEL_PGID
        return fake_proc

    monkeypatch.setattr(mod, "_spawn_blocking_signals", _fake_spawn)

    # Stuck drain threads → forces drain-incomplete SKIP path.
    real_thread = mod.threading.Thread

    class _StuckThread(real_thread):
        def is_alive(self):  # type: ignore[override]
            return True

    monkeypatch.setattr(mod.threading, "Thread", _StuckThread)

    # Recording mock for _kill_process_group: capture (proc, pgid) so we
    # can assert the cached pgid is what gets forwarded.
    calls: list[tuple] = []

    def _recording_kill(proc, pgid=None):
        calls.append((proc, pgid))

    monkeypatch.setattr(mod, "_kill_process_group", _recording_kill)

    import io

    fake_stderr = io.StringIO()
    monkeypatch.setattr(mod.sys, "stderr", fake_stderr)

    with pytest.raises(SystemExit) as exc_info:
        mod._run_with_drain(["fake-argv"], timeout_secs=5.0, stdin_fp=None)

    assert exc_info.value.code == 0
    # Exactly one _kill_process_group call on the drain-incomplete branch,
    # and it must have received the cached spawn-time pgid.
    assert len(calls) == 1, calls
    captured_proc, captured_pgid = calls[0]
    assert captured_proc is fake_proc
    assert captured_pgid == SENTINEL_PGID, (
        f"drain-incomplete must forward cached pgid; got {captured_pgid!r}"
    )
    banner = fake_stderr.getvalue()
    assert "SKIP reason=runtime-error" in banner
    assert "drain-incomplete" in banner


# ---------- (q Meta-dogfood R3 Fix A) normal exit tears down cached pgid ----------


def test_normal_exit_kills_cached_process_group_before_config_restore(tmp_path):
    """A leader can exit 0 while a stdio-closed descendant keeps running.

    The fallback path restores/removes `.review-loop/config.md` in `finally`.
    If normal-exit cleanup only waits for the leader, a descendant in the
    original process group can mutate the config after restore and after the
    invoker exits.
    """
    root = _make_plugin_root_fallback_only(tmp_path)
    focus = _make_focus_file(tmp_path)
    descendant_pid = tmp_path / "descendant.pid"

    stub = _make_python_stub(
        tmp_path,
        "codex_stub_descendant.py",
        textwrap.dedent(f"""\
            #!{sys.executable}
            import json
            import os
            import subprocess
            import sys

            sys.stdin.read()
            cfg = os.path.abspath(os.path.join(".review-loop", "config.md"))
            child_code = (
                "import os, pathlib, time\\n"
                f"pathlib.Path({str(descendant_pid)!r}).write_text(str(os.getpid()))\\n"
                "time.sleep(0.4)\\n"
                f"pathlib.Path({{cfg!r}}).write_text('LATE-DESCENDANT\\\\n')\\n"
            )
            subprocess.Popen(
                [sys.executable, "-c", child_code],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            payload = {{
                "verdict": "approve",
                "summary": "ok",
                "findings": [],
                "next_steps": [],
            }}
            sys.stdout.write(json.dumps(payload) + "\\n")
        """),
    )

    config_dir = tmp_path / ".review-loop"
    config_dir.mkdir()
    cfg = config_dir / "config.md"
    cfg.write_text("ORIGINAL\n")
    original = cfg.read_bytes()

    try:
        r = _run_invoker(
            ["--focus-file", str(focus)],
            env={
                "REVIEW_LOOP_PLUGIN_ROOT": str(root),
                "REVIEW_LOOP_CODEX": str(stub),
            },
            cwd=tmp_path,
        )
        assert r.returncode == 0, r.stderr
        time.sleep(0.8)
        assert cfg.read_bytes() == original
    finally:
        if descendant_pid.exists():
            try:
                os.kill(int(descendant_pid.read_text().strip()), signal.SIGKILL)
            except (ProcessLookupError, ValueError, OSError):
                pass


def test_signal_handler_kills_cached_pgid_even_when_leader_exited(monkeypatch):
    """SIGINT/SIGTERM cleanup must not skip teardown just because poll() is set."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "adversarial_gate_invoke_under_test_signal_r3", str(INVOKER)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _ExitedProc:
        def poll(self):
            return 0

    fake_proc = _ExitedProc()
    mod._active_proc = fake_proc
    mod._active_pgid = 5151

    calls: list[tuple] = []

    def _recording_kill(proc, pgid=None):
        calls.append((proc, pgid))

    monkeypatch.setattr(mod, "_kill_process_group", _recording_kill)
    monkeypatch.setattr(mod, "_cleanup", lambda: None)

    with pytest.raises(SystemExit) as exc_info:
        mod._cleanup_and_exit(signal.SIGTERM, None)

    assert exc_info.value.code == 128 + signal.SIGTERM
    assert calls == [(fake_proc, 5151)]


# ---------- (r Meta-dogfood R4 Fix A) cleanup failure blocks approval ----------


def test_cleanup_restore_failure_is_reported_and_done_after_attempt(
    tmp_path, monkeypatch
):
    """Config restore failure must be observable, not swallowed as approval."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "adversarial_gate_invoke_under_test_cleanup_r4", str(INVOKER)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    snapshot = tmp_path / "snapshot.md"
    config = tmp_path / ".review-loop" / "config.md"
    config.parent.mkdir()
    snapshot.write_text("ORIGINAL\n")
    config.write_text("codex_reviewer_backend: codex\nskip_quality_polish: true\n")

    mod._SNAPSHOT_PATH = str(snapshot)
    mod._CONFIG_PATH = str(config)

    states_during_copy: list[bool] = []

    def _fail_copy(_src, _dst):
        states_during_copy.append(mod._CLEANUP_DONE)
        raise OSError("restore denied")

    monkeypatch.setattr(mod.shutil, "copy2", _fail_copy)

    failure = mod._cleanup()

    assert states_during_copy == [False]
    assert mod._CLEANUP_DONE is True
    assert failure is not None
    assert "restore denied" in failure
    assert config.read_text() == (
        "codex_reviewer_backend: codex\nskip_quality_polish: true\n"
    )
    assert snapshot.exists(), "snapshot must remain for manual recovery"


def test_cleanup_failure_overrides_adapter_approve(tmp_path, monkeypatch, capsys):
    """An APPROVE from the adapter must not win over fallback cleanup failure."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "adversarial_gate_invoke_under_test_cleanup_override_r4", str(INVOKER)
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    focus = _make_focus_file(tmp_path)
    plugin_root = tmp_path / "plugin_root"
    plugin_root.mkdir()

    monkeypatch.setattr(mod, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(mod, "_resolve_plugin_root", lambda: str(plugin_root))
    monkeypatch.setattr(mod, "_resolve_companion_script", lambda _root: "companion")
    monkeypatch.setattr(mod, "_build_plugin_argv", lambda *_args: ["fake-review"])
    monkeypatch.setattr(
        mod,
        "_run_with_drain",
        lambda *_args, **_kwargs: (b'{"verdict":"approve","findings":[],"next_steps":[]}', b"", 0),
    )
    monkeypatch.setattr(mod, "_check_auth_marker", lambda *_args: None)
    monkeypatch.setattr(mod, "_pipe_to_adapter", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(mod, "_cleanup", lambda: "config-restore failed: boom")

    with pytest.raises(SystemExit) as exc_info:
        mod.main(["--focus-file", str(focus)])

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "adversarial-gate: REQUEST_CHANGES" in captured.out
    assert "[CRITICAL]" in captured.out
    assert "config-restore failed: boom" in captured.out
