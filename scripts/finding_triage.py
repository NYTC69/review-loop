#!/usr/bin/env python3
"""Finding triage helper for review-loop (P4 proportional blocking review).

Mandatory post-parse semantic gate on every blocking review output, on both
runtimes (Claude Code and Codex Stage 1) and on all three parser paths:

  1. syntactic parse as today — `_validate_reviewer_output_schema` (imported
     from `scripts/review_verification.py`, never copied) for reviewer-shaped
     output; the adapter-rendered banner + `### Issues` shape for Step 3.4
     gate output; neither parser is modified here;
  2. `finding_triage.py check` — every `[CRITICAL]` must carry the six
     blocking-rubric fields, each non-empty: `Trigger:`, `Reachability:`,
     `Impact:`, `Likelihood:`, `Fix cost:`, `Cheaper response:`. The fields
     may be indented continuation lines under the bullet (normal Reviewer
     path) or inline `Label:` segments inside the finding body (gate path;
     the adapter renders each finding on one line). Both forms are accepted
     equivalently. `[MINOR]` findings and `APPROVE` outputs are complete
     without inspection.

Subcommands
-----------
  check       partition every `[CRITICAL]` of one review output into complete
              / incomplete findings; `--record-pending` appends each incomplete
              finding to the session's pending-revalidation list
  dispute     record the orchestrator's rubric-based dispute of one complete
              `[CRITICAL]` (state `awaiting-concurrence`); never overrides
              the verdict
  concur      read the next independent Reviewer output against a dispute →
              `concurred` | `re-asserted` | `incomplete`
  revalidate  read a revalidation-round Reviewer output against a pending
              gate entry → `re-asserted` | `concurred` | `incomplete`
  status      list open disputes / pending entries (exit 0 when none are open)

Exit codes
----------
  check:               0 complete / 1 incomplete, or an infrastructure failure
                       present / 2 malformed input
  dispute:             0 recorded / 2 usage or validation error (an
                       infrastructure failure is not disputable), nothing written
  concur, revalidate:  0 concurred / 1 re-asserted / 4 incomplete (the review
                       output is discarded as malformed; the per-backend retry
                       row applies) / 2 usage error, nothing written
  status:              0 nothing open / 1 open entries remain / 2 usage error
  3 from any command is a storage failure or an internal error (`INTERNAL-
  ERROR` + traceback on stderr); nothing written — parity with
  `evidence_ledger.py`. Exit 3 never means `incomplete`.

Infrastructure failures
-----------------------
`scripts/adversarial_gate_invoke.py` emits a synthetic `adversarial-gate:
REQUEST_CHANGES` for its own runtime failures (fallback-config cleanup,
uncertain stdout capture, adapter launch / malformed output / missing
banner, producer non-zero after adapter APPROVE). Each is one `[CRITICAL]`
anchored at `scripts/adversarial_gate_invoke.py:…` or
`scripts/adversarial_gate_adapter.py:…`, stamped `(confidence=1.0)`, with
no rubric field by construction. A gate `[CRITICAL]` is classified as
`infrastructure` only when all three hold — anchor in
`INFRASTRUCTURE_ANCHORS`, no rubric label present at all (inline or
continuation), and `(confidence=1.0)` on the bullet; a gate finding at
those paths that carries any rubric label, or another confidence, is an
ordinary finding (complete or incomplete by the normal rule), so a real
review finding about the invoker or the adapter is dispatched and
disputable like any other. An infrastructure block is not a review
finding: `check` reports it under `infrastructure` (exempt from the rubric,
exit 1), never partitions it as incomplete, never appends it with
`--record-pending`, never feeds it to the Executor, and `dispute` refuses
it. `exec` stays withheld until the runtime failure is resolved and Step 3
re-runs.

Triage state
------------
Persisted as the `triage` object inside the session's `## Evidence Ledger`
JSON block — `{"next_id": n, "disputes": [...], "pending_rubric_incomplete":
[...]}` — read and written through the same session / ledger helpers as
`scripts/evidence_ledger.py` (`load_ledger` / `store_ledger`), so there is
one machine-owned block and one writer (the orchestrator). `evidence_ledger.py`
preserves the key untouched. The orchestrator's in-memory
`loop_state.pending_rubric_incomplete` mirrors the persisted list.

Matching a later output to an entry: a `[CRITICAL]` in the later output
matches the entry when the entry's match key occurs in the finding's full
text (bullet + continuation lines). The key is `--key` when given at
`dispute` time, else the finding's file anchor (`File:` path on the Reviewer
path, `path:start-end` on the gate path). An entry without any key matches
every `[CRITICAL]` (fail closed: nothing can be silently concurred).
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evidence_ledger import (  # noqa: E402
    StorageError,
    UsageError,
    load_ledger,
    now_iso,
    read_session,
    resolve_repo,
    session_path,
    store_ledger,
    write_session,
)
from review_verification import _validate_reviewer_output_schema  # noqa: E402

RUBRIC_FIELDS: Tuple[str, ...] = (
    "Trigger",
    "Reachability",
    "Impact",
    "Likelihood",
    "Fix cost",
    "Cheaper response",
)
# `Cheaper response insufficient:` is an accepted spelling of the sixth label.
_LABEL_ALTERNATION = r"Trigger|Reachability|Impact|Likelihood|Fix cost|Cheaper response(?: insufficient)?"
# Annotation labels end a rubric value without starting one.
_ANNOTATIONS = r"File|Recommendation"
_FIELD_RE = re.compile(rf"(?<![A-Za-z])(?P<label>{_LABEL_ALTERNATION}|{_ANNOTATIONS})\s*:")
_SEVERITY_RE = re.compile(r"^\[(CRITICAL|MINOR)\]")
_GATE_BANNER = "adversarial-gate:"
_REVIEWER_ANCHOR_RE = re.compile(r"File:\s*`?([^`,\s]+)`?")
_GATE_ANCHOR_RE = re.compile(r"^\[(?:CRITICAL|MINOR)\]\s+(\S+?):\d+-\d+")
_VALUE_NOISE = " \t\r\n.,;:—–-*_"

TRIAGE_KEY = "triage"
AUTHOR_ROUTES = ("executor", "orchestrator-direct")
# A gate `[CRITICAL]` anchored at the invoker or the adapter is a synthetic
# infrastructure-failure block (see module docstring) only together with
# the emitters' signature: `(confidence=1.0)` on the bullet and no rubric
# label anywhere in the finding. Anchor alone never classifies.
INFRASTRUCTURE_ANCHORS = ("scripts/adversarial_gate_invoke.py", "scripts/adversarial_gate_adapter.py")
INFRASTRUCTURE_CONFIDENCE = "(confidence=1.0)"


def is_infrastructure(finding: dict, shape: str) -> bool:
    """Gate-shape `[CRITICAL]` at an infrastructure anchor, `(confidence=1.0)`
    on the bullet, and not one rubric label in the whole finding text."""
    if shape != "gate" or finding["severity"] != "CRITICAL" or finding["anchor"] not in INFRASTRUCTURE_ANCHORS:
        return False
    if INFRASTRUCTURE_CONFIDENCE not in finding["text"].split("\n", 1)[0]:
        return False
    _missing, values = rubric_status(finding["text"])
    return not values


# --------------------------------------------------------------- parsing


def _canonical_label(label: str) -> str:
    return "Cheaper response" if label.startswith("Cheaper response") else label


def rubric_status(text: str) -> Tuple[List[str], Dict[str, str]]:
    """(missing fields in rubric order, {field: value}) for one finding's text."""
    matches = list(_FIELD_RE.finditer(text))
    values: Dict[str, str] = {}
    for idx, match in enumerate(matches):
        label = match.group("label")
        if label in ("File", "Recommendation"):
            continue
        field = _canonical_label(label)
        if field in values and values[field]:
            continue  # skip if field already populated
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        value = text[match.end():end].strip(_VALUE_NOISE)
        values[field] = value
    missing = [field for field in RUBRIC_FIELDS if not values.get(field)]
    return missing, values


def _section_lines(text: str, name: str) -> Optional[List[str]]:
    """Lines of the `### <name>` section (case-insensitive), or None."""
    lines: Optional[List[str]] = None
    collecting = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("### "):
            key = stripped[4:].split(":", 1)[0].strip().lower()
            collecting = key == name.lower()
            if collecting and lines is None:
                lines = []
            continue
        if collecting and lines is not None:
            lines.append(raw)
    return lines


def group_findings(issue_lines: List[str]) -> List[dict]:
    """Group `### Issues` lines into findings: a `[CRITICAL]` / `[MINOR]` bullet
    plus its indented continuation lines. Unindented lines never attach."""
    findings: List[dict] = []
    current: Optional[dict] = None
    for raw in issue_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        # Extract bullet body: "- text" or "text"
        body = stripped[2:].strip() if stripped.startswith("- ") else stripped
        # Severity marker (new finding) or "None." (end marker)
        match = _SEVERITY_RE.match(body)
        if body == "None.":
            current = None
        elif match:
            current = {"severity": match.group(1), "lines": [body]}
            findings.append(current)
        elif current is not None and raw.startswith((" ", "\t")):
            current["lines"].append(stripped)
        else:
            current = None
    return findings


def _anchor(finding: dict, shape: str) -> Optional[str]:
    text = "\n".join(finding["lines"])
    if shape == "gate":
        match = _GATE_ANCHOR_RE.match(finding["lines"][0])
        if match:
            return match.group(1)
    match = _REVIEWER_ANCHOR_RE.search(text)
    return match.group(1) if match else None


def parse_review(text: str) -> dict:
    """Shape-detect and parse one review output.

    Returns {shape, verdict, findings: [{n, severity, text, anchor}], schema_error}.
    Raises UsageError (exit 2) on malformed input."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    has_verdict_header = any(line.strip().upper().startswith("### VERDICT") for line in text.splitlines())
    if has_verdict_header:
        shape = "reviewer"
        verdict, issues, error = _validate_reviewer_output_schema(text)
        if error is not None:
            raise UsageError(f"reviewer output rejected by the schema parser: {error}")
        findings = group_findings(_section_lines(text, "issues") or [])
        bodies = [f["lines"][0] for f in findings]
        if bodies != list(issues or []):
            raise UsageError("triage grouping disagrees with the schema parser's issue list; "
                             "refusing to inspect an ambiguous `### Issues` section")
    elif first.startswith(_GATE_BANNER):
        shape = "gate"
        verdict = first[len(_GATE_BANNER):].strip().split()[0] if first[len(_GATE_BANNER):].strip() else ""
        if verdict not in ("APPROVE", "REQUEST_CHANGES", "SKIP"):
            raise UsageError(f"unknown adversarial-gate banner verdict {verdict!r}")
        findings = group_findings(_section_lines(text, "issues") or []) if verdict == "REQUEST_CHANGES" else []
        if verdict == "REQUEST_CHANGES" and not any(f["severity"] == "CRITICAL" for f in findings):
            raise UsageError("adversarial-gate REQUEST_CHANGES carries no `[CRITICAL]` finding")
    else:
        raise UsageError("unrecognized review output shape: expected a `### VERDICT:` header "
                         "or an `adversarial-gate:` banner")
    return {
        "shape": shape,
        "verdict": verdict,
        "findings": [
            {"n": idx + 1, "severity": f["severity"], "text": "\n".join(f["lines"]), "anchor": _anchor(f, shape)}
            for idx, f in enumerate(findings)
        ],
    }


def triage(text: str) -> dict:
    """Partition every `[CRITICAL]` of one review output into complete / incomplete."""
    parsed = parse_review(text)
    inspected = parsed["verdict"] == "REQUEST_CHANGES"
    findings = []
    for finding in parsed["findings"]:
        entry = dict(finding)
        entry["infrastructure"] = False
        if inspected and is_infrastructure(finding, parsed["shape"]):
            # invoker-synthetic runtime failure: exempt from the rubric,
            # never incomplete, never a finding to partition or dispute
            entry["infrastructure"] = True
            entry["missing"] = []
            entry["rubric"] = None
            entry["complete"] = True
        elif inspected and finding["severity"] == "CRITICAL":
            missing, values = rubric_status(finding["text"])
            entry["missing"] = missing
            entry["rubric"] = {field: values.get(field, "") for field in RUBRIC_FIELDS}
            entry["complete"] = not missing
        else:
            entry["missing"] = []
            entry["rubric"] = None
            entry["complete"] = True
        findings.append(entry)
    label = "gate finding" if parsed["shape"] == "gate" else "finding"
    incomplete = [f for f in findings if not f["complete"]]
    infrastructure = [f for f in findings if f["infrastructure"]]
    complete_criticals = [f for f in findings if f["severity"] == "CRITICAL" and f["complete"] and not f["infrastructure"]]
    return {
        "shape": parsed["shape"],
        "verdict": parsed["verdict"],
        "inspected": inspected,
        "result": "incomplete" if incomplete else "complete",
        "findings": findings,
        "complete": [f["n"] for f in complete_criticals],
        "incomplete": [f["n"] for f in incomplete],
        # Synthetic invoker failures: blocking (exec withheld until the
        # runtime failure is resolved), not partitioned, not revalidated.
        "infrastructure": [f["n"] for f in infrastructure],
        "summary": (
            [f"rubric_incomplete: {label} #{f['n']} missing {', '.join(f['missing'])}" for f in incomplete]
            + [f"infrastructure_failure: {label} #{f['n']} at {f['anchor']} — not a finding; "
               "resolve the runtime failure and re-run Step 3" for f in infrastructure]
        ),
        # Executor feedback may carry complete findings only; an incomplete
        # or infrastructure finding never appears in an Executor prompt.
        "complete_findings_text": "\n".join(_render_finding(f) for f in complete_criticals),
        "executor_dispatch": bool(complete_criticals),
    }


def _render_finding(finding: dict) -> str:
    lines = finding["text"].split("\n")
    return "- " + lines[0] + "".join("\n  " + line for line in lines[1:])


# ---------------------------------------------------------------- state


def _read_input(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    try:
        with open(source, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise UsageError(f"cannot read {source}: {exc}") from exc


def empty_triage() -> dict:
    return {"next_id": 1, "disputes": [], "pending_rubric_incomplete": []}


def load_triage(ledger: dict) -> dict:
    state = ledger.get(TRIAGE_KEY)
    if state is None:
        state = empty_triage()
        ledger[TRIAGE_KEY] = state
    if not isinstance(state, dict):
        raise UsageError("`triage` object in the `## Evidence Ledger` block is malformed")
    for key, default in empty_triage().items():
        state.setdefault(key, default)
    return state


def _next_id(state: dict) -> int:
    ident = int(state["next_id"])
    state["next_id"] = ident + 1
    return ident


def _find_entry(entries: List[dict], ident: int, what: str) -> dict:
    for entry in entries:
        if entry.get("id") == ident:
            return entry
    raise UsageError(f"unknown {what} id {ident}")


def _open_session(args) -> Tuple[str, str, dict, dict]:
    path = session_path(args)
    text = read_session(path)
    ledger = load_ledger(text)
    return path, text, ledger, load_triage(ledger)


def _save(path: str, text: str, ledger: dict) -> None:
    write_session(path, store_ledger(text, ledger))


def _matching_criticals(report: dict, key: Optional[str]) -> List[dict]:
    """CRITICAL findings, optionally filtered by key. No key: all CRITICALs (fail closed)."""
    criticals = [f for f in report["findings"] if f["severity"] == "CRITICAL"]
    if key is None:
        return criticals
    return [f for f in criticals if key in f["text"]]


def _outcome(report: dict, key: Optional[str]) -> Tuple[str, List[dict]]:
    """concurred (no matching CRITICAL) / re-asserted (all matching complete)
    / incomplete (a matching CRITICAL lacks rubric fields)."""
    matches = _matching_criticals(report, key)
    if not matches:
        return "concurred", matches
    if all(f["complete"] for f in matches):
        return "re-asserted", matches
    return "incomplete", matches


# exit 3 is reserved for storage / internal failures (parity with evidence_ledger.py)
_OUTCOME_EXIT = {"concurred": 0, "re-asserted": 1, "incomplete": 4}


# ----------------------------------------------------------- subcommands


def cmd_check(args) -> int:
    if args.record_pending:
        # the session is resolved and read up front: a missing or malformed
        # session is exit 2 even when the review turns out to be complete
        path, text, ledger, state = _open_session(args)
    report = triage(_read_input(args.input))
    if args.record_pending:
        if report["incomplete"]:
            ids = []
            for finding in report["findings"]:
                if finding["complete"]:
                    continue
                ident = _next_id(state)
                state["pending_rubric_incomplete"].append({
                    "id": ident,
                    "source": args.source or report["shape"],
                    "round": args.round,
                    "finding": finding["n"],
                    "missing": finding["missing"],
                    "text": finding["text"],
                    "anchor": finding["anchor"],
                    "state": "awaiting-revalidation",
                    "recorded_at": now_iso(),
                })
                ids.append(ident)
            _save(path, text, ledger)
            report["pending"] = ids
        else:
            report["pending"] = []
    print(json.dumps(report, indent=2))
    if report["infrastructure"]:
        return 1  # blocking: exec withheld until the runtime failure is resolved
    return 0 if report["result"] == "complete" else 1


def cmd_dispute(args) -> int:
    report = triage(_read_input(args.review))
    finding = next((f for f in report["findings"] if f["n"] == args.finding), None)
    if finding is None:
        raise UsageError(f"finding #{args.finding} does not exist in the review output")
    if finding["severity"] != "CRITICAL":
        raise UsageError(f"finding #{args.finding} is [{finding['severity']}]; only a [CRITICAL] can be disputed")
    if finding["infrastructure"]:
        raise UsageError(f"finding #{args.finding} ({finding['anchor']}) is a synthetic gate runtime failure; "
                         "an infrastructure failure is not disputable — resolve it and re-run Step 3")
    if not finding["complete"]:
        raise UsageError(f"finding #{args.finding} failed triage (missing {', '.join(finding['missing'])}); "
                         "it is discarded as malformed, not disputed")
    rationale = _read_input(args.rationale).strip()
    if not rationale:
        raise UsageError("the dispute rationale is empty; a rubric-based written rationale is required")
    path, text, ledger, state = _open_session(args)
    watermark = max((int(r.get("id", 0)) for r in ledger.get("records") or []), default=0)
    ident = _next_id(state)
    entry = {
        "id": ident,
        "finding": args.finding,
        "source": report["shape"],
        "round": args.round,
        "text": finding["text"],
        "anchor": finding["anchor"],
        "key": args.key or finding["anchor"],
        "rationale": rationale,
        "author_route": args.author_route,
        "ledger_watermark": watermark,
        "state": "awaiting-concurrence",
        "recorded_at": now_iso(),
        "outcome": None,
        "resolved_at": None,
    }
    state["disputes"].append(entry)
    _save(path, text, ledger)
    first_line = rationale.splitlines()[0]
    print(json.dumps({
        "dispute": entry,
        "packet_line": f"Triage: disputed CRITICAL #{args.finding} → MINOR/follow-up (dispute {ident}) — {first_line}",
        "history_line": f"Triage: disputed CRITICAL #{args.finding} (dispute {ident}, awaiting-concurrence); not implemented",
    }, indent=2))
    return 0


def _require_later_reviewer_record(ledger: dict, entry: dict, record_id: Optional[int]) -> None:
    if entry.get("author_route") != "orchestrator-direct":
        return
    if record_id is None:
        raise UsageError("the disputed change was authored orchestrator-direct: pass --reviewer-record <id> "
                         "naming the concurring round's reviewer_approve ledger record")
    record = next((r for r in ledger.get("records") or [] if r.get("id") == record_id), None)
    if record is None or record.get("check") != "reviewer_approve":
        raise UsageError(f"--reviewer-record {record_id} is not a reviewer_approve record in the ledger")
    if record_id <= int(entry.get("ledger_watermark", 0)):
        raise UsageError(f"--reviewer-record {record_id} predates the dispute (ledger watermark "
                         f"{entry.get('ledger_watermark')}); concurrence must come from a later independent round")


def cmd_concur(args) -> int:
    path, text, ledger, state = _open_session(args)
    entry = _find_entry(state["disputes"], args.dispute, "dispute")
    if entry.get("state") != "awaiting-concurrence":
        raise UsageError(f"dispute {args.dispute} is {entry.get('state')}, not awaiting-concurrence")
    _require_later_reviewer_record(ledger, entry, args.reviewer_record)
    report = triage(_read_input(args.review))
    outcome, matches = _outcome(report, entry.get("key"))
    if outcome != "incomplete":
        entry["state"] = outcome
        entry["outcome"] = outcome
        entry["resolved_at"] = now_iso()
        entry["concurring_record"] = args.reviewer_record
        _save(path, text, ledger)
    print(json.dumps({
        "outcome": outcome,
        "dispute": args.dispute,
        "state": entry["state"],
        "matched": [f["n"] for f in matches],
        "missing": {str(f["n"]): f["missing"] for f in matches if not f["complete"]},
        "summary": [s for s in report["summary"] if any(f"#{f['n']} " in s for f in matches)],
        "proceed_without_implementing": outcome == "concurred",
    }, indent=2))
    return _OUTCOME_EXIT[outcome]


def cmd_revalidate(args) -> int:
    path, text, ledger, state = _open_session(args)
    entry = _find_entry(state["pending_rubric_incomplete"], args.pending, "pending entry")
    if entry.get("state") != "awaiting-revalidation":
        raise UsageError(f"pending entry {args.pending} is {entry.get('state')}, not awaiting-revalidation")
    report = triage(_read_input(args.review))
    outcome, matches = _outcome(report, entry.get("anchor"))
    if outcome == "re-asserted":
        entry["state"] = "re-asserted"
    elif outcome == "concurred":
        entry["state"] = "dropped"
    if outcome != "incomplete":
        entry["outcome"] = outcome
        entry["resolved_at"] = now_iso()
        _save(path, text, ledger)
    print(json.dumps({
        "outcome": outcome,
        "pending": args.pending,
        "state": entry["state"],
        "matched": [f["n"] for f in matches],
        "missing": {str(f["n"]): f["missing"] for f in matches if not f["complete"]},
        "history_line": (
            f"rubric revalidation: gate finding #{entry.get('finding')} (pending {args.pending}) "
            + {"re-asserted": "re-asserted with a complete rubric; enters the ordinary repair loop",
               "concurred": "dropped by the Reviewer; closed without implementation",
               "incomplete": "re-asserted without a full rubric; output discarded as malformed"}[outcome]
        ),
    }, indent=2))
    return _OUTCOME_EXIT[outcome]


def cmd_status(args) -> int:
    _path, _text, _ledger, state = _open_session(args)
    open_disputes = [d["id"] for d in state["disputes"] if d.get("state") == "awaiting-concurrence"]
    awaiting = [p["id"] for p in state["pending_rubric_incomplete"] if p.get("state") == "awaiting-revalidation"]
    re_asserted = [p["id"] for p in state["pending_rubric_incomplete"] if p.get("state") == "re-asserted"]
    report = {
        "open_disputes": open_disputes,
        "awaiting_revalidation": awaiting,
        "re_asserted_pending": re_asserted,
        "dropped": [p["id"] for p in state["pending_rubric_incomplete"] if p.get("state") == "dropped"],
        # `exec` may be minted only when nothing awaits revalidation and no
        # dispute awaits concurrence; a re-asserted entry is repaired through
        # the ordinary loop, whose later APPROVE is what mints `exec`.
        "revalidation_clear": not awaiting and not open_disputes,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["revalidation_clear"] else 1


# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finding_triage.py",
        description="review-loop blocking-rubric triage (mandatory post-parse gate on every [CRITICAL]).",
    )
    parser.add_argument("--repo", default=".", help="path inside the git repository (default: cwd)")
    parser.add_argument("--sessions-dir", default=".review-loop/sessions",
                        help="session directory relative to the repo root")
    parser.add_argument("--session", help="session uuid (file: <sessions-dir>/<uuid>.md)")
    parser.add_argument("--session-file", help="explicit session file path (overrides --session lookup)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="partition every [CRITICAL] into complete / incomplete "
                                     "(exit 0 complete, 1 incomplete or infrastructure failure, 2 malformed)")
    p.add_argument("--input", required=True, help="review output file, or `-` for stdin")
    p.add_argument("--record-pending", action="store_true",
                   help="append each incomplete finding to the session's pending-revalidation list")
    p.add_argument("--source", choices=("gate", "reviewer"), help="pending-entry source (default: detected shape)")
    p.add_argument("--round", type=int, help="round number to stamp on pending entries")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("dispute", help="record a rubric-based dispute of one complete [CRITICAL]")
    p.add_argument("--finding", type=int, required=True, help="finding number (1-based, in `### Issues` order)")
    p.add_argument("--rationale", required=True, help="file holding the written rubric-based rationale, or `-`")
    p.add_argument("--review", required=True, help="the review output the finding came from, or `-`")
    p.add_argument("--key", help="match key for the next Reviewer output (default: the finding's file anchor)")
    p.add_argument("--author-route", choices=AUTHOR_ROUTES, default="executor",
                   help="who authored the disputed change (orchestrator-direct requires a later "
                        "reviewer_approve record at concur time)")
    p.add_argument("--round", type=int)
    p.set_defaults(func=cmd_dispute)

    p = sub.add_parser("concur", help="next independent Reviewer output vs a dispute "
                                      "(exit 0 concurred, 1 re-asserted, 4 incomplete)")
    p.add_argument("--dispute", type=int, required=True)
    p.add_argument("--review", required=True, help="the next Reviewer output, or `-`")
    p.add_argument("--reviewer-record", type=int,
                   help="ledger id of that round's reviewer_approve record (required for orchestrator-direct)")
    p.set_defaults(func=cmd_concur)

    p = sub.add_parser("revalidate", help="revalidation-round Reviewer output vs a pending gate entry "
                                          "(exit 0 concurred/dropped, 1 re-asserted, 4 incomplete)")
    p.add_argument("--pending", type=int, required=True)
    p.add_argument("--review", required=True, help="the revalidation-round Reviewer output, or `-`")
    p.set_defaults(func=cmd_revalidate)

    p = sub.add_parser("status", help="open disputes / pending entries (exit 0 when none are open)")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.session_file is None and args.session is not None:
            # `--session <uuid>` is looked up under the repository toplevel,
            # like evidence_ledger.py; pure-text commands need no repository.
            args.repo = resolve_repo(os.path.abspath(args.repo))
        return args.func(args)
    except UsageError as exc:
        print(f"finding-triage: error: {exc}", file=sys.stderr)
        return 2
    except StorageError as exc:
        print(f"finding-triage: storage failure: {exc} (fail closed; nothing written)", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 — a bug fails closed as exit 3, never as a triage outcome
        traceback.print_exc(file=sys.stderr)
        print(f"finding-triage: INTERNAL-ERROR {type(exc).__name__}: {exc} (fail closed; nothing written)",
              file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
