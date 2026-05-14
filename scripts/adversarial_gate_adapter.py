#!/usr/bin/env python3
"""Adapter: translate codex adversarial-review JSON output into review-loop
verdict + findings format consumed by Step 3.4 — Terminal Adversarial Gate.

CLI:
    python3 scripts/adversarial_gate_adapter.py
        [--input <path>]            # optional; absent → read stdin
        [--input-mode {plugin-json,raw}]  # default plugin-json

Input modes:
    plugin-json — the codex-companion.mjs `adversarial-review --json` output;
                  emits the schema object verbatim.
    raw         — the raw `codex exec --output-schema ...` stdout where the
                  final JSON object on stdout is the schema; we parse the
                  last balanced JSON object from stdin.

Exit codes:
    0 — APPROVE (verdict == approve, OR verdict == needs-attention with no
        findings at severity critical/high).
    1 — REQUEST_CHANGES (verdict == needs-attention with at least one
        critical-or-high finding).
    2 — Malformed input (not JSON, schema-violating, missing required keys,
        bad confidence range, bad line_start > line_end, missing next_steps).

Stdout: review-loop verdict line + bulleted issues block per
        docs/protocol/reviewer-output.md.
Stderr: human-readable diagnostic on exit 2.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


_SEVERITY_TO_TAG = {
    "critical": "CRITICAL",
    "high": "CRITICAL",  # collapsed: codex `high` blocks review-loop too
    "medium": "MINOR",
    "low": "MINOR",
}

_BLOCKING_SEVERITIES = ("critical", "high")
_ADVISORY_SEVERITIES = ("medium", "low")
_VALID_SEVERITIES = _BLOCKING_SEVERITIES + _ADVISORY_SEVERITIES
_VALID_VERDICTS = ("approve", "needs-attention")


def _err(msg: str) -> None:
    sys.stderr.write(f"adversarial-gate-adapter: {msg}\n")


def _looks_like_adversarial_schema(obj: Any) -> bool:
    """Light shape probe — full validation is downstream.

    Just enough to discriminate the real adversarial-review payload from a
    preamble blob that happens to ``raw_decode`` (e.g. ``{"note":"not
    schema"}``). Full schema validation lives in ``_validate_payload`` and
    produces precise exit-2 reasons on malformed payloads.
    """
    if not isinstance(obj, dict):
        return False
    if obj.get("verdict") not in _VALID_VERDICTS:
        return False
    return isinstance(obj.get("findings"), list)


def _extract_last_json_object(text: str) -> Any:
    """Find the LAST schema-valid JSON object embedded in `text` (raw mode).

    Codex exec stdout may include a human-readable preamble — possibly itself
    containing JSON-like substrings (metadata, debug lines) — followed by the
    final schema payload. Scan every ``{`` candidate offset via
    ``json.JSONDecoder().raw_decode``: this is string-aware (literal ``{`` /
    ``}`` inside JSON string literals are correctly skipped, see fixture
    ``raw_mode_curly_in_string.json``).

    Collect every successful decode. Return the LAST one that also passes a
    light adversarial-schema shape probe — that's the real payload even when
    the preamble shadows it with a non-schema JSON blob. If multiple
    candidates decode but none look like the schema, return the last raw
    decode anyway and let ``_validate_payload`` produce the precise exit-2
    malformed reason. If zero candidates decode, raise the canonical
    "no parseable JSON object" error.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty stdin")
    # Fast path: whole input is a JSON object.
    try:
        whole = json.loads(stripped)
    except json.JSONDecodeError:
        pass
    else:
        return whole

    decoder = json.JSONDecoder()
    candidates: list[Any] = []
    i = 0
    n = len(stripped)
    while i < n:
        if stripped[i] != "{":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(stripped, i)
        except json.JSONDecodeError:
            i += 1
            continue
        candidates.append(obj)
        # Skip past the decoded object to avoid re-scanning its interior.
        i = end

    if not candidates:
        raise ValueError("no JSON object found in stdin")

    for obj in reversed(candidates):
        if _looks_like_adversarial_schema(obj):
            return obj
    # No schema-shaped candidate — let downstream validation report why.
    return candidates[-1]


def _validate_payload(payload: Any) -> None:
    """Raise ValueError on schema mismatch."""
    if not isinstance(payload, dict):
        raise ValueError("payload is not a JSON object")
    verdict = payload.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    if "next_steps" not in payload:
        raise ValueError("missing required key: next_steps")
    if not isinstance(payload.get("findings", []), list):
        raise ValueError("findings is not a list")

    for idx, finding in enumerate(payload.get("findings", [])):
        if not isinstance(finding, dict):
            raise ValueError(f"finding[{idx}] is not an object")
        for required in (
            "severity",
            "title",
            "body",
            "file",
            "line_start",
            "line_end",
            "confidence",
            "recommendation",
        ):
            if required not in finding:
                raise ValueError(f"finding[{idx}] missing required field: {required}")
        sev = finding["severity"]
        if sev not in _VALID_SEVERITIES:
            raise ValueError(f"finding[{idx}] invalid severity: {sev!r}")
        conf = finding["confidence"]
        if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
            raise ValueError(f"finding[{idx}] confidence out of [0,1]: {conf!r}")
        ls = finding["line_start"]
        le = finding["line_end"]
        if not isinstance(ls, int) or not isinstance(le, int) or ls < 1 or le < 1:
            raise ValueError(f"finding[{idx}] invalid line_start/line_end: {ls},{le}")
        if ls > le:
            raise ValueError(
                f"finding[{idx}] line_start > line_end: {ls} > {le}"
            )


def _append_advisory_block(lines: list[str], advisory: list[dict]) -> None:
    """Append the shared `### Advisory (non-blocking)` block, if any."""
    if not advisory:
        return
    lines.append("")
    lines.append("### Advisory (non-blocking)")
    for f in advisory:
        tag = _SEVERITY_TO_TAG.get(f["severity"], "MINOR")
        lines.append(
            f"- [{tag}] {f['file']}:{f['line_start']}-{f['line_end']} "
            f"— {f['title']}: {f['body']}"
        )


def _render(payload: dict) -> tuple[str, int]:
    """Translate a validated payload into review-loop verdict text + exit code.

    Meta-dogfood Bug #2 (CRITICAL): blocking findings are AUTHORITATIVE — if
    any finding has severity in {critical, high}, we render REQUEST_CHANGES
    regardless of the top-level ``verdict`` label. A contradictory model
    response (``verdict: approve`` + critical findings) must not slip
    through; the reviewer found a blocking defect, we don't proceed.
    """
    findings = payload.get("findings", []) or []
    blocking = [f for f in findings if f.get("severity") in _BLOCKING_SEVERITIES]
    advisory = [f for f in findings if f.get("severity") in _ADVISORY_SEVERITIES]

    if not blocking:
        lines = ["adversarial-gate: APPROVE"]
        summary = (payload.get("summary") or "").strip()
        if summary:
            lines.append("")
            lines.append(f"Summary: {summary}")
        _append_advisory_block(lines, advisory)
        return "\n".join(lines) + "\n", 0

    lines = ["adversarial-gate: REQUEST_CHANGES", "", "### Issues"]
    for f in blocking:
        tag = _SEVERITY_TO_TAG.get(f["severity"], "CRITICAL")
        lines.append(
            f"- [{tag}] {f['file']}:{f['line_start']}-{f['line_end']} "
            f"(confidence={f['confidence']}) — {f['title']}: {f['body']}"
        )
        rec = (f.get("recommendation") or "").strip()
        if rec:
            lines.append(f"  Recommendation: {rec}")
    _append_advisory_block(lines, advisory)
    return "\n".join(lines) + "\n", 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Translate codex adversarial-review output → review-loop verdict.",
    )
    parser.add_argument("--input", default=None,
                        help="path to JSON input (absent → stdin)")
    parser.add_argument("--input-mode", choices=("plugin-json", "raw"),
                        default="plugin-json")
    args = parser.parse_args(argv)

    try:
        if args.input:
            with open(args.input, "rb") as fh:
                raw_bytes = fh.read()
        else:
            raw_bytes = sys.stdin.buffer.read()
        text = raw_bytes.decode("utf-8", errors="replace")
    except OSError as e:
        _err(f"cannot read input: {e}")
        return 2

    try:
        if args.input_mode == "raw":
            payload = _extract_last_json_object(text)
        else:
            payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        _err(f"malformed input: {e}")
        return 2

    # Meta-dogfood Bug #1 (CRITICAL): plugin-json mode receives the
    # codex-companion.mjs `adversarial-review --json` envelope, NOT the bare
    # schema object. The schema payload lives at `payload["result"]`. Unwrap
    # before validation; if `result` is absent/non-dict, fall back to
    # validating the top-level for backward-compat with bare-schema input.
    # Also surface a non-empty `parseError` from the envelope as an explicit
    # exit-2 diagnostic — the companion already detected malformed codex
    # output and we should not silently pretend it's a valid payload.
    if args.input_mode == "plugin-json" and isinstance(payload, dict):
        parse_error = payload.get("parseError")
        if isinstance(parse_error, str) and parse_error.strip():
            _err(f"companion parseError: {parse_error.strip()}")
            return 2
        inner = payload.get("result")
        if isinstance(inner, dict):
            payload = inner

    try:
        _validate_payload(payload)
    except ValueError as e:
        _err(f"schema violation: {e}")
        return 2

    rendered, exit_code = _render(payload)
    sys.stdout.write(rendered)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
