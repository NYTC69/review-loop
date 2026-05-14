"""Tests for scripts/adversarial_gate_adapter.py.

13 fixture-translation cases (run via --input + --input-mode) + 2 stdin-mode
plumbing cases (input bytes piped via Popen.communicate, no --input flag).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER = REPO_ROOT / "scripts" / "adversarial_gate_adapter.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "adversarial_gate"


def _run_with_input_flag(fixture: str, mode: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(ADAPTER),
            "--input",
            str(FIXTURES / fixture),
            "--input-mode",
            mode,
        ],
        capture_output=True,
        check=False,
    )


def _run_with_stdin(fixture: str, mode: str) -> subprocess.CompletedProcess:
    data = (FIXTURES / fixture).read_bytes()
    return subprocess.run(
        [sys.executable, str(ADAPTER), "--input-mode", mode],
        input=data,
        capture_output=True,
        check=False,
    )


# -------- Plugin-json mode --------


def test_approve_empty():
    r = _run_with_input_flag("approve_empty.json", "plugin-json")
    assert r.returncode == 0
    assert b"adversarial-gate: APPROVE" in r.stdout


def test_approve_with_summary():
    r = _run_with_input_flag("approve_with_summary.json", "plugin-json")
    assert r.returncode == 0
    assert b"APPROVE" in r.stdout
    # Low-severity finding should appear in advisory block, not block exit.
    assert b"Advisory" in r.stdout


def test_needs_attention_critical():
    r = _run_with_input_flag("needs_attention_critical.json", "plugin-json")
    assert r.returncode == 1
    assert b"REQUEST_CHANGES" in r.stdout
    assert re.search(rb"\[CRITICAL\].*Race on shared counter", r.stdout)


def test_needs_attention_high():
    r = _run_with_input_flag("needs_attention_high.json", "plugin-json")
    assert r.returncode == 1
    assert b"REQUEST_CHANGES" in r.stdout
    # `high` severity collapsed onto [CRITICAL] tag.
    assert b"[CRITICAL]" in r.stdout


def test_needs_attention_mixed():
    r = _run_with_input_flag("needs_attention_mixed.json", "plugin-json")
    assert r.returncode == 1
    assert b"REQUEST_CHANGES" in r.stdout
    assert b"Auth bypass" in r.stdout
    # Medium severity still shown but in advisory block.
    assert b"Advisory" in r.stdout
    assert b"Slow path" in r.stdout


def test_needs_attention_advisory_only_approves():
    # `verdict: needs-attention` but no critical/high findings → APPROVE.
    r = _run_with_input_flag("needs_attention_advisory.json", "plugin-json")
    assert r.returncode == 0
    assert b"APPROVE" in r.stdout
    assert b"Advisory" in r.stdout


def test_malformed_not_json():
    r = _run_with_input_flag("malformed_not_json.json", "plugin-json")
    assert r.returncode == 2
    assert b"malformed" in r.stderr.lower() or b"json" in r.stderr.lower()


def test_missing_next_steps():
    r = _run_with_input_flag("missing_next_steps.json", "plugin-json")
    assert r.returncode == 2
    assert b"next_steps" in r.stderr


def test_missing_finding_field():
    r = _run_with_input_flag("missing_finding_field.json", "plugin-json")
    assert r.returncode == 2
    assert b"recommendation" in r.stderr or b"missing" in r.stderr


def test_bad_confidence():
    r = _run_with_input_flag("bad_confidence.json", "plugin-json")
    assert r.returncode == 2
    assert b"confidence" in r.stderr


def test_bad_line_range():
    r = _run_with_input_flag("bad_line_range.json", "plugin-json")
    assert r.returncode == 2
    assert b"line_start" in r.stderr


# -------- Raw mode --------


def test_raw_mode_approve():
    r = _run_with_input_flag("raw_mode_approve.json", "raw")
    assert r.returncode == 0
    assert b"APPROVE" in r.stdout


def test_raw_mode_malformed():
    r = _run_with_input_flag("raw_mode_malformed.json", "raw")
    assert r.returncode == 2
    assert b"no JSON" in r.stderr or b"malformed" in r.stderr


def test_raw_mode_preamble_with_json():
    """Finding #33: raw-mode extraction must return the LAST schema-valid
    JSON object, not the first parseable one.

    The fixture ships a preamble that itself contains a JSON blob
    (``{"note":"not schema..."}``) followed by the real adversarial-review
    schema object. A first-match implementation would return the preamble
    blob and exit 2 (schema violation: missing verdict). The last-schema-valid
    pass returns the trailing schema object and exits 0 APPROVE.
    """
    r = _run_with_input_flag("raw_mode_preamble_with_json.json", "raw")
    assert r.returncode == 0, r.stderr
    assert b"adversarial-gate: APPROVE" in r.stdout
    assert b"preamble-shadowing test" in r.stdout


def test_raw_mode_curly_in_string():
    """Finding #30: JSON extraction must be string-aware.

    Preamble + bare schema object whose `summary` contains literal `{...}`
    characters. A naive brace-counting scanner would treat the inner curlies
    as nested objects, mis-balance, and fail. `json.JSONDecoder().raw_decode`
    correctly skips curlies inside string literals.
    """
    r = _run_with_input_flag("raw_mode_curly_in_string.json", "raw")
    assert r.returncode == 0, r.stderr
    assert b"adversarial-gate: APPROVE" in r.stdout
    # Summary content (with the embedded curlies preserved) reaches stdout.
    assert b"{embedded} {curlies}" in r.stdout


# -------- Stdin plumbing cases --------


def test_stdin_plumbing_plugin_json():
    """No --input flag; payload on stdin."""
    r = _run_with_stdin("approve_empty.json", "plugin-json")
    assert r.returncode == 0
    assert b"APPROVE" in r.stdout


def test_stdin_plumbing_raw_mode():
    r = _run_with_stdin("raw_mode_approve.json", "raw")
    assert r.returncode == 0
    assert b"APPROVE" in r.stdout


# -------- Meta-dogfood regression cases --------


def test_plugin_json_unwraps_real_companion_payload():
    """Meta-dogfood Bug #1 (CRITICAL): plugin-json mode must unwrap
    ``payload["result"]`` before validating against the schema.

    The real ``codex-companion.mjs adversarial-review --json`` output is a
    *wrapped* envelope:

        {"review": ..., "result": {"verdict": ..., "findings": [...]}, ...}

    Validating the top-level object directly produced
    ``schema violation: invalid verdict: None`` → exit 2 →
    invoker SKIPped ``adapter-exit-2-malformed`` and the preferred plugin
    path silently failed to gate.

    Asserting exit ∈ {0, 1} (not 2) and that the rendered verdict matches
    the inner ``result.verdict`` proves the unwrap works on the real shape.
    """
    r = _run_with_input_flag("plugin_json_real_companion.json", "plugin-json")
    assert r.returncode in (0, 1), (
        f"adapter exited 2 — unwrap regressed; stderr={r.stderr!r}"
    )
    # The bundled real-companion sample has verdict=needs-attention with 3
    # findings; whether they render APPROVE or REQUEST_CHANGES is driven by
    # blocking-finding presence per Bug #2's fix. Either way, we must NOT
    # see the legacy "invalid verdict: None" schema-violation line.
    assert b"invalid verdict" not in r.stderr
    # And the verdict banner must be one of the two render outputs (never
    # silent absence).
    assert (b"adversarial-gate: APPROVE" in r.stdout) ^ (
        b"adversarial-gate: REQUEST_CHANGES" in r.stdout
    )


def test_plugin_json_surfaces_companion_parse_error():
    """Meta-dogfood Bug #1 follow-on: if the companion envelope reports a
    non-empty ``parseError`` (codex output was malformed upstream), the
    adapter must exit 2 with the parseError forwarded as the diagnostic
    rather than silently validating a ``null`` result."""
    r = _run_with_input_flag(
        "plugin_json_envelope_parse_error.json", "plugin-json"
    )
    assert r.returncode == 2
    assert b"parseError" in r.stderr or b"Unexpected end of JSON" in r.stderr


def test_approve_with_high_severity_renders_request_changes():
    """Meta-dogfood Bug #2 (CRITICAL): blocking-finding presence is
    AUTHORITATIVE over the top-level ``verdict`` label.

    Fixture: ``verdict: approve`` + a single ``severity: high`` finding.
    Pre-fix: adapter saw ``verdict == "approve"`` and short-circuited into
    the APPROVE branch — the critical defect was dropped.
    Post-fix: any critical/high finding triggers REQUEST_CHANGES regardless
    of the verdict label.
    """
    r = _run_with_input_flag("approve_with_high.json", "plugin-json")
    assert r.returncode == 1, (
        f"approve+high must render REQUEST_CHANGES (exit 1); got "
        f"{r.returncode}, stdout={r.stdout!r}"
    )
    assert b"adversarial-gate: REQUEST_CHANGES" in r.stdout
    assert b"[CRITICAL]" in r.stdout  # `high` collapses to CRITICAL tag.
    assert b"Auth bypass" in r.stdout
