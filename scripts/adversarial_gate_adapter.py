#!/usr/bin/env python3
"""Adapter: translate codex adversarial-review JSON output into review-loop
verdict + findings format consumed by Step 3.4 — Terminal Adversarial Gate.

CLI:
    python3 scripts/adversarial_gate_adapter.py
        [--input <path>]            # optional; absent → read stdin
        [--input-mode {plugin-json,raw}]  # default plugin-json

Input modes:
    plugin-json — the codex-companion.mjs `adversarial-review --json` output,
                  or a bare schema object for backward compatibility.
                  Companion envelopes re-parse rawOutput / codex.stdout before
                  trusting the already-parsed result object.
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

Historical `Finding #N` / `Meta-dogfood R*` labels in comments are provenance
for the v2.7.7/v2.7.8 adversarial-gate audit trail; the durable contract is
the protocol text plus the regression tests.
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
_ALLOWED_PAYLOAD_KEYS = {"verdict", "summary", "findings", "next_steps"}
_ALLOWED_FINDING_KEYS = {
    "severity",
    "title",
    "body",
    "file",
    "line_start",
    "line_end",
    "confidence",
    "recommendation",
}


def _err(msg: str) -> None:
    sys.stderr.write(f"adversarial-gate-adapter: {msg}\n")


def _reject_extra_keys(obj: dict, allowed: set[str], label: str) -> None:
    extras = sorted(str(k) for k in obj if k not in allowed)
    if extras:
        if label == "top-level":
            raise ValueError(f"unexpected top-level property: {extras[0]}")
        raise ValueError(f"unexpected {label} property: {extras[0]}")


def _require_non_empty_string(value: Any, label: str) -> None:
    if not isinstance(value, str) or value == "":
        if label == "summary":
            raise ValueError("summary must be a non-empty string")
        raise ValueError(f"{label} must be a non-empty string")


def _require_string(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")


def _object_pairs_no_duplicate_keys(
    pairs: list[tuple[str, Any]]
) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate key: {key}")
        obj[key] = value
    return obj


def _json_loads_no_duplicate_keys(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_object_pairs_no_duplicate_keys)


def _json_decoder_no_duplicate_keys() -> json.JSONDecoder:
    return json.JSONDecoder(object_pairs_hook=_object_pairs_no_duplicate_keys)


def _companion_raw_output(payload: dict[str, Any]) -> str | None:
    raw_output = payload.get("rawOutput")
    if isinstance(raw_output, str) and raw_output.strip():
        return raw_output

    codex = payload.get("codex")
    if isinstance(codex, dict):
        stdout = codex.get("stdout")
        if isinstance(stdout, str) and stdout.strip():
            return stdout

    return None


def _previous_non_ws(text: str, start: int) -> int | None:
    j = start - 1
    while j >= 0 and text[j].isspace():
        j -= 1
    return j if j >= 0 else None


def _has_unclosed_json_container_prefix(text: str, end: int) -> bool:
    object_depth = 0
    array_depth = 0
    in_string = False
    escaped = False

    for ch in text[:end]:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            object_depth += 1
        elif ch == "}":
            object_depth = max(0, object_depth - 1)
        elif ch == "[":
            array_depth += 1
        elif ch == "]":
            array_depth = max(0, array_depth - 1)

    return object_depth > 0 or array_depth > 0


def _is_nested_json_object_candidate(text: str, start: int) -> bool:
    """Return True when a ``{`` candidate is visibly inside JSON structure."""
    if _has_unclosed_json_container_prefix(text, start):
        return True
    prev = _previous_non_ws(text, start)
    if prev is None:
        return False
    if text[prev] in "[{,":
        return True
    if text[prev] != ":":
        return False
    key_end = _previous_non_ws(text, prev)
    # Avoid rejecting prose labels such as "Final payload:\n{...}" while still
    # rejecting object-member values like {"result": {"verdict": ...}.
    return key_end is not None and text[key_end] == '"'


def _extract_last_json_object(text: str) -> Any:
    """Find the LAST decoded JSON object embedded in `text` (raw mode).

    Codex exec stdout may include a human-readable preamble — possibly itself
    containing JSON-like substrings (metadata, debug lines) — followed by the
    final schema payload. Scan every ``{`` candidate offset via
    ``json.JSONDecoder().raw_decode``: this is string-aware (literal ``{`` /
    ``}`` inside JSON string literals are correctly skipped, see fixture
    ``raw_mode_curly_in_string.json``).

    Collect every successful decode and return the LAST decoded object. If a
    later object start exists but cannot be decoded, fail closed with
    ``malformed final JSON object after last valid object`` instead of
    returning an earlier schema-shaped APPROVE. If any non-whitespace content
    remains after the last decoded object, fail closed with
    ``trailing content after final JSON object`` because the fallback Codex CLI
    contract says the final JSON object is authoritative. Nested object
    candidates inside arrays or object-member values are ignored; a truncated
    wrapper must not let an inner schema-shaped object approve. Let
    ``_validate_payload`` produce the precise exit-2 malformed reason for a
    decoded final object. If zero candidates decode, raise the canonical "no
    parseable JSON object" error.
    """
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty stdin")
    # Contract needle: Nested JSON object candidates.
    # Fast path: whole input is a JSON object.
    try:
        whole = _json_loads_no_duplicate_keys(stripped)
    except json.JSONDecodeError:
        pass
    else:
        return whole

    decoder = _json_decoder_no_duplicate_keys()
    candidates: list[Any] = []
    last_success_end = -1
    malformed_after_last_success: json.JSONDecodeError | None = None
    i = 0
    n = len(stripped)
    while i < n:
        if stripped[i] != "{":
            i += 1
            continue
        if _is_nested_json_object_candidate(stripped, i):
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(stripped, i)
        except json.JSONDecodeError as e:
            if candidates and i >= last_success_end:
                malformed_after_last_success = e
            i += 1
            continue
        candidates.append(obj)
        last_success_end = end
        malformed_after_last_success = None
        # Skip past the decoded object to avoid re-scanning its interior.
        i = end

    if not candidates:
        raise ValueError("no JSON object found in stdin")
    if malformed_after_last_success is not None:
        raise ValueError(
            "malformed final JSON object after last valid object: "
            f"{malformed_after_last_success.msg}"
        )
    if stripped[last_success_end:].strip():
        raise ValueError("trailing content after final JSON object")

    return candidates[-1]


def _validate_payload(payload: Any) -> None:
    """Raise ValueError on schema mismatch."""
    if not isinstance(payload, dict):
        raise ValueError("payload is not a JSON object")
    _reject_extra_keys(payload, _ALLOWED_PAYLOAD_KEYS, "top-level")
    verdict = payload.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"invalid verdict: {verdict!r}")
    if "summary" not in payload:
        raise ValueError("missing required key: summary")
    _require_non_empty_string(payload.get("summary"), "summary")
    if "next_steps" not in payload:
        raise ValueError("missing required key: next_steps")
    if not isinstance(payload.get("next_steps"), list):
        raise ValueError("next_steps is not a list")
    for idx, item in enumerate(payload["next_steps"]):
        _require_non_empty_string(item, f"next_steps[{idx}]")
    if "findings" not in payload:
        raise ValueError("missing required key: findings")
    if not isinstance(payload.get("findings"), list):
        raise ValueError("findings is not a list")

    for idx, finding in enumerate(payload["findings"]):
        if not isinstance(finding, dict):
            raise ValueError(f"finding[{idx}] is not an object")
        _reject_extra_keys(finding, _ALLOWED_FINDING_KEYS, f"finding[{idx}]")
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
        _require_non_empty_string(finding["title"], f"finding[{idx}] title")
        _require_non_empty_string(finding["body"], f"finding[{idx}] body")
        _require_non_empty_string(finding["file"], f"finding[{idx}] file")
        _require_string(
            finding["recommendation"], f"finding[{idx}] recommendation"
        )
        conf = finding["confidence"]
        if (
            isinstance(conf, bool)
            or not isinstance(conf, (int, float))
            or not (0.0 <= float(conf) <= 1.0)
        ):
            raise ValueError(f"finding[{idx}] confidence out of [0,1]: {conf!r}")
        ls = finding["line_start"]
        le = finding["line_end"]
        if (
            isinstance(ls, bool)
            or isinstance(le, bool)
            or not isinstance(ls, int)
            or not isinstance(le, int)
            or ls < 1
            or le < 1
        ):
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
        text = raw_bytes.decode("utf-8")
    except OSError as e:
        _err(f"cannot read input: {e}")
        return 2
    except UnicodeDecodeError as e:
        _err(f"malformed input: invalid utf-8: {e}")
        return 2

    try:
        if args.input_mode == "raw":
            payload = _extract_last_json_object(text)
        else:
            payload = _json_loads_no_duplicate_keys(text)
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
        raw_output = _companion_raw_output(payload)
        if raw_output is not None:
            try:
                payload = _extract_last_json_object(raw_output)
            except (json.JSONDecodeError, ValueError) as e:
                _err(f"malformed companion raw output: {e}")
                return 2
        else:
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
