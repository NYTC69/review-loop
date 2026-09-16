"""Tests for scripts/finding_triage.py — the P4 blocking-rubric triage gate.

Pure text in, JSON + exit code out; the state-machine cases drive the CLI
against a throwaway session file (no git needed: the triage state lives in
the `## Evidence Ledger` JSON block and is read/written through the same
helpers `scripts/evidence_ledger.py` uses).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TRIAGE = REPO_ROOT / "scripts" / "finding_triage.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import adversarial_gate_adapter as adapter  # noqa: E402
import adversarial_gate_invoke as invoker  # noqa: E402
import evidence_ledger as ledger_mod  # noqa: E402
import finding_triage as ft  # noqa: E402

RUBRIC = {
    "Trigger": "upstream returns HTTP 429 during the nightly batch",
    "Reachability": "every nightly batch hits the rate limit at least once",
    "Impact": "the batch aborts and partial writes remain on disk",
    "Likelihood": "high — observed on three of the last five runs",
    "Fix cost": "small; honour Retry-After inside the existing loop",
    "Cheaper response": "a MINOR would leave nightly data loss in place",
}

SESSION_TEMPLATE = """## Problem Description
Throwaway session for finding-triage tests.

## Current Phase
execution

## Approved Plan

- Source: reviewer-approved

## Current Review Packet

### Author route
executor

## Review History

## Files Changed
None.

## Timing Log
| Phase | Round | Role | Duration |
|---|---|---|---|

## Session Metadata
- entry_point: plan
- plan_source: reviewer-approved
- completed_stages: []
"""


# ---------- builders ----------


def continuation_critical(missing=(), blank=(), anchor="src/client.py", desc="retry loop drops 429 responses"):
    lines = [f"- [CRITICAL] {desc} — must be resolved before proceeding"]
    for field, value in RUBRIC.items():
        if field in missing:
            continue
        lines.append(f"  {field}: " + ("" if field in blank else value))
    if anchor:
        lines.append(f"  File: `{anchor}`, around line 42")
    return "\n".join(lines)


def inline_critical(anchor="src/client.py", missing=()):
    body = " ".join(f"{f}: {v}." for f, v in RUBRIC.items() if f not in missing)
    return f"- [CRITICAL] {anchor}:40-44 (confidence=0.9) — retry loop drops 429 responses: {body}"


def review(verdict="REQUEST_CHANGES", issues=None, strengths="- ok"):
    text = f"### VERDICT: {verdict}\n\n"
    if issues is not None:
        text += "### Issues\n" + "\n".join(issues) + "\n\n"
    return text + f"### Strengths\n{strengths}\n"


def gate(*criticals, advisory=()):
    text = "adversarial-gate: REQUEST_CHANGES\n\n### Issues\n" + "\n".join(criticals) + "\n"
    if advisory:
        text += "\n### Advisory (non-blocking)\n" + "\n".join(advisory) + "\n"
    return text


def write(tmp_path: Path, name: str, text: str) -> str:
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    return str(target)


def ledger_record(ident: int, check: str, path: str = "src/a.py") -> dict:
    # the minimal shape `evidence_ledger.load_ledger` accepts
    return {"id": ident, "claim_id": f"{check}:{path}", "check": check, "superseded_by": None,
            "recorded_at": "2026-09-15T00:00:00+00:00"}


def session(tmp_path: Path, records=()) -> Path:
    target = tmp_path / "session.md"
    text = SESSION_TEMPLATE
    if records:
        ledger = ledger_mod.empty_ledger()
        ledger["records"] = list(records)
        text = ledger_mod.store_ledger(text, ledger)
    target.write_text(text, encoding="utf-8")
    return target


def run(*args: str, session_file: Path | None = None, expect: int = 0, stdin: str | None = None):
    cmd = [sys.executable, str(TRIAGE)]
    if session_file is not None:
        cmd += ["--session-file", str(session_file)]
    cmd += list(args)
    completed = subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    assert completed.returncode == expect, (
        f"exit {completed.returncode} != {expect}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
    return completed


def run_json(*args: str, session_file: Path | None = None, expect: int = 0, stdin: str | None = None) -> dict:
    return json.loads(run(*args, session_file=session_file, expect=expect, stdin=stdin).stdout)


def check(tmp_path: Path, text: str, expect: int, *extra: str, session_file: Path | None = None) -> dict:
    return run_json("check", "--input", write(tmp_path, "review.md", text), *extra,
                    session_file=session_file, expect=expect)


def triage_state(session_file: Path) -> dict:
    return ledger_mod.load_ledger(session_file.read_text(encoding="utf-8"))["triage"]


# ---------- check: partition matrix ----------


def test_six_continuation_fields_are_complete(tmp_path):
    report = check(tmp_path, review(issues=[continuation_critical()]), 0)
    assert report["shape"] == "reviewer" and report["inspected"] is True
    assert report["result"] == "complete" and report["complete"] == [1] and report["incomplete"] == []
    assert report["findings"][0]["rubric"] == RUBRIC
    assert report["findings"][0]["anchor"] == "src/client.py"
    assert report["executor_dispatch"] is True
    assert report["complete_findings_text"] == continuation_critical()


def test_six_continuation_fields_with_mixed_minor_are_complete(tmp_path):
    report = check(tmp_path, review(issues=[continuation_critical(), "- [MINOR] extra blank line"]), 0)
    assert report["result"] == "complete"
    assert [f["severity"] for f in report["findings"]] == ["CRITICAL", "MINOR"]
    assert report["findings"][1]["rubric"] is None and report["findings"][1]["complete"] is True
    assert "[MINOR]" not in report["complete_findings_text"]


def test_six_inline_fields_are_complete(tmp_path):
    report = check(tmp_path, gate(inline_critical()), 0)
    assert report["shape"] == "gate" and report["verdict"] == "REQUEST_CHANGES"
    assert report["result"] == "complete" and report["complete"] == [1]
    assert report["findings"][0]["anchor"] == "src/client.py"
    assert report["findings"][0]["rubric"]["Cheaper response"] == RUBRIC["Cheaper response"]


@pytest.mark.parametrize("field", list(RUBRIC))
def test_any_one_missing_field_is_incomplete_naming_the_field(tmp_path, field):
    report = check(tmp_path, review(issues=[continuation_critical(missing=(field,))]), 1)
    assert report["result"] == "incomplete" and report["incomplete"] == [1]
    assert report["findings"][0]["missing"] == [field]
    assert report["summary"] == [f"rubric_incomplete: finding #1 missing {field}"]
    assert report["executor_dispatch"] is False and report["complete_findings_text"] == ""
    inline = check(tmp_path, gate(inline_critical(missing=(field,))), 1)
    assert inline["findings"][0]["missing"] == [field]
    assert inline["summary"] == [f"rubric_incomplete: gate finding #1 missing {field}"]


def test_empty_value_counts_as_missing_even_when_followed_by_an_annotation(tmp_path):
    # `Cheaper response:` with nothing after it, followed by the `File:` line
    report = check(tmp_path, review(issues=[continuation_critical(blank=("Cheaper response",))]), 1)
    assert report["findings"][0]["missing"] == ["Cheaper response"]
    # a value that is only punctuation is also missing
    text = continuation_critical().replace(f"  Impact: {RUBRIC['Impact']}", "  Impact: —")
    assert check(tmp_path, review(issues=[text]), 1)["findings"][0]["missing"] == ["Impact"]


def test_unindented_continuation_lines_do_not_attach(tmp_path):
    # an unindented line ends the finding (as the schema parser does), so the
    # lines after it detach too
    text = continuation_critical().replace("\n  Likelihood:", "\nLikelihood:")
    report = check(tmp_path, review(issues=[text]), 1)
    assert report["findings"][0]["missing"] == ["Likelihood", "Fix cost", "Cheaper response"]


def test_cheaper_response_insufficient_alias_is_accepted(tmp_path):
    text = continuation_critical().replace("  Cheaper response:", "  Cheaper response insufficient:")
    assert check(tmp_path, review(issues=[text]), 0)["result"] == "complete"


def test_minor_only_and_approve_are_complete_without_inspection(tmp_path):
    for text in (
        review(verdict="APPROVE", issues=["- [MINOR] nit"]),
        review(verdict="APPROVE", issues=["- None."]),
        review(verdict="APPROVE"),
    ):
        report = check(tmp_path, text, 0)
        assert report["result"] == "complete" and report["inspected"] is False
        assert report["complete"] == [] and report["incomplete"] == []
        assert report["executor_dispatch"] is False
    approve_gate = check(tmp_path, "adversarial-gate: APPROVE\n\nSummary: fine\n", 0)
    assert approve_gate["shape"] == "gate" and approve_gate["inspected"] is False


def test_adapter_rendered_bare_body_is_incomplete(tmp_path):
    payload = {
        "verdict": "needs-attention",
        "summary": "s",
        "findings": [{
            "severity": "critical", "file": "src/a.py", "line_start": 10, "line_end": 12,
            "confidence": 0.9, "title": "unchecked input", "body": "the handler trusts the header",
            "recommendation": "validate it",
        }],
    }
    rendered, code = adapter._render(payload)
    assert code == 1
    report = check(tmp_path, rendered, 1)
    assert report["shape"] == "gate" and report["findings"][0]["missing"] == list(RUBRIC)
    assert report["findings"][0]["anchor"] == "src/a.py"
    assert report["summary"] == ["rubric_incomplete: gate finding #1 missing " + ", ".join(RUBRIC)]


def test_adapter_rendered_inline_rubric_body_is_complete_and_advisory_is_ignored(tmp_path):
    body = " ".join(f"{f}: {v}." for f, v in RUBRIC.items())
    payload = {
        "verdict": "needs-attention",
        "findings": [
            {"severity": "high", "file": "src/a.py", "line_start": 1, "line_end": 2,
             "confidence": 0.8, "title": "t", "body": body},
            {"severity": "low", "file": "src/b.py", "line_start": 1, "line_end": 2,
             "confidence": 0.5, "title": "advisory", "body": "no rubric on purpose"},
        ],
    }
    rendered, code = adapter._render(payload)
    assert code == 1 and "### Advisory (non-blocking)" in rendered
    report = check(tmp_path, rendered, 0)
    assert report["result"] == "complete" and len(report["findings"]) == 1


def test_partition_complete_and_incomplete_gate_findings(tmp_path):
    text = gate(inline_critical(anchor="src/ok.py"), inline_critical(anchor="src/bad.py", missing=("Impact",)))
    report = check(tmp_path, text, 1)
    assert report["complete"] == [1] and report["incomplete"] == [2]
    assert "src/ok.py" in report["complete_findings_text"] and "src/bad.py" not in report["complete_findings_text"]
    assert report["executor_dispatch"] is True


def test_malformed_inputs_exit_2(tmp_path):
    for text, needle in (
        (review(verdict="REQUEST_CHANGES"), "schema_violation:request_changes_without_issues"),
        (review(verdict="APPROVE", issues=[continuation_critical()]), "schema_violation:approve_with_critical"),
        (review(issues=["- [MAJOR] nope"]), "schema_violation:invalid_severity"),
        ("adversarial-gate: REQUEST_CHANGES\n\n### Issues\n- None.\n", "no `[CRITICAL]` finding"),
        ("adversarial-gate: MAYBE\n", "unknown adversarial-gate banner"),
        ("just some prose\n", "unrecognized review output shape"),
    ):
        completed = run("check", "--input", write(tmp_path, "bad.md", text), expect=2)
        assert needle in completed.stderr, completed.stderr


def test_check_reads_stdin(tmp_path):
    report = run_json("check", "--input", "-", stdin=review(issues=[continuation_critical()]))
    assert report["result"] == "complete"


def test_rubric_status_is_importable_and_pure():
    missing, values = ft.rubric_status("[CRITICAL] x Trigger: a. Reachability: b. Impact:  Likelihood: d. "
                                       "Fix cost: e. Cheaper response: f. File: `x.py`")
    assert missing == ["Impact"] and values["Cheaper response"] == "f"


# ---------- revalidation subflow: pending entries ----------


def test_record_pending_serializes_incomplete_findings_as_awaiting_revalidation(tmp_path):
    sess = session(tmp_path)
    text = gate(inline_critical(anchor="src/ok.py"), inline_critical(anchor="src/bad.py", missing=("Fix cost",)))
    report = check(tmp_path, text, 1, "--record-pending", "--round", "3", session_file=sess)
    assert report["pending"] == [1]
    state = triage_state(sess)
    assert state["disputes"] == [] and state["next_id"] == 2
    (entry,) = state["pending_rubric_incomplete"]
    assert entry["id"] == 1 and entry["source"] == "gate" and entry["round"] == 3
    assert entry["finding"] == 2 and entry["missing"] == ["Fix cost"] and entry["anchor"] == "src/bad.py"
    assert entry["state"] == "awaiting-revalidation" and "src/bad.py" in entry["text"]
    # the ledger block round-trips through evidence_ledger's own loader with the triage key intact
    loaded = ledger_mod.load_ledger(sess.read_text(encoding="utf-8"))
    assert loaded["records"] == [] and loaded["triage"]["pending_rubric_incomplete"][0]["id"] == 1
    # status: not clear while an entry awaits revalidation
    status = run_json("status", session_file=sess, expect=1)
    assert status["awaiting_revalidation"] == [1] and status["revalidation_clear"] is False
    # a complete-only output records nothing
    clean = check(tmp_path, gate(inline_critical()), 0, "--record-pending", session_file=sess)
    assert clean["pending"] == [] and len(triage_state(sess)["pending_rubric_incomplete"]) == 1


def test_no_complete_findings_partition_yields_executor_less_round(tmp_path):
    sess = session(tmp_path)
    report = check(tmp_path, gate(inline_critical(missing=("Trigger",))), 1, "--record-pending", session_file=sess)
    assert report["executor_dispatch"] is False and report["complete_findings_text"] == ""
    assert report["pending"] == [1]


def test_revalidate_re_asserted_dropped_and_incomplete(tmp_path):
    sess = session(tmp_path)
    check(tmp_path, gate(inline_critical(anchor="src/bad.py", missing=("Impact",)),
                         inline_critical(anchor="src/other.py", missing=("Impact",))),
          1, "--record-pending", session_file=sess)
    assert [p["id"] for p in triage_state(sess)["pending_rubric_incomplete"]] == [1, 2]
    # incomplete: re-asserted without a full rubric → exit 4, state unchanged
    out = run_json("revalidate", "--pending", "1", "--review",
                   write(tmp_path, "r.md", review(issues=[continuation_critical(anchor="src/bad.py", missing=("Likelihood",))])),
                   session_file=sess, expect=4)
    assert out["outcome"] == "incomplete" and out["state"] == "awaiting-revalidation"
    assert out["missing"] == {"1": ["Likelihood"]}
    assert triage_state(sess)["pending_rubric_incomplete"][0]["state"] == "awaiting-revalidation"
    # re-asserted with a complete rubric → exit 1, state re-asserted
    out = run_json("revalidate", "--pending", "1", "--review",
                   write(tmp_path, "r.md", review(issues=[continuation_critical(anchor="src/bad.py")])),
                   session_file=sess, expect=1)
    assert out["outcome"] == "re-asserted" and out["matched"] == [1]
    assert triage_state(sess)["pending_rubric_incomplete"][0]["state"] == "re-asserted"
    # dropped: restated as MINOR (or absent) → exit 0, state dropped
    out = run_json("revalidate", "--pending", "2", "--review",
                   write(tmp_path, "r.md", review(verdict="APPROVE", issues=["- [MINOR] src/other.py could be tidier"])),
                   session_file=sess, expect=0)
    assert out["outcome"] == "concurred" and out["state"] == "dropped"
    # a resolved entry cannot be revalidated again
    run("revalidate", "--pending", "2", "--review", write(tmp_path, "r.md", review(verdict="APPROVE")),
        session_file=sess, expect=2)
    status = run_json("status", session_file=sess, expect=0)
    assert status["re_asserted_pending"] == [1] and status["dropped"] == [2] and status["revalidation_clear"] is True


def test_revalidate_matches_by_anchor_so_an_unrelated_critical_does_not_re_assert(tmp_path):
    sess = session(tmp_path)
    check(tmp_path, gate(inline_critical(anchor="src/bad.py", missing=("Impact",))), 1, "--record-pending",
          session_file=sess)
    # a complete CRITICAL about another file: the pending entry is dropped (absent from this output)
    out = run_json("revalidate", "--pending", "1", "--review",
                   write(tmp_path, "r.md", review(issues=[continuation_critical(anchor="src/elsewhere.py")])),
                   session_file=sess, expect=0)
    assert out["outcome"] == "concurred" and out["matched"] == []


# ---------- dispute flow ----------


def test_dispute_then_concur_state_machine(tmp_path):
    sess = session(tmp_path)
    first = write(tmp_path, "first.md", review(issues=[continuation_critical(anchor="src/cache.py"), "- [MINOR] nit"]))
    rationale = write(tmp_path, "why.md", "Disproportionate: Likelihood is 'rare' and Impact is a cache miss.\nMore.\n")
    out = run_json("dispute", "--finding", "1", "--rationale", rationale, "--review", first, "--round", "2",
                   session_file=sess, expect=0)
    entry = out["dispute"]
    assert entry["id"] == 1 and entry["state"] == "awaiting-concurrence" and entry["key"] == "src/cache.py"
    assert entry["author_route"] == "executor" and entry["ledger_watermark"] == 0
    assert out["packet_line"].startswith("Triage: disputed CRITICAL #1 → MINOR/follow-up (dispute 1) — Disproportionate")
    assert triage_state(sess)["disputes"][0]["rationale"].startswith("Disproportionate")
    assert run_json("status", session_file=sess, expect=1)["open_disputes"] == [1]
    # incomplete re-assertion → exit 4, dispute still open
    out = run_json("concur", "--dispute", "1", "--review",
                   write(tmp_path, "n.md", review(issues=[continuation_critical(anchor="src/cache.py", missing=("Fix cost",))])),
                   session_file=sess, expect=4)
    assert out["outcome"] == "incomplete" and out["missing"] == {"1": ["Fix cost"]}
    assert out["summary"] == ["rubric_incomplete: finding #1 missing Fix cost"]
    assert triage_state(sess)["disputes"][0]["state"] == "awaiting-concurrence"
    # concurred: downgraded to MINOR → exit 0, proceed without implementing
    out = run_json("concur", "--dispute", "1", "--review",
                   write(tmp_path, "n.md", review(verdict="APPROVE", issues=["- [MINOR] src/cache.py: consider a follow-up"])),
                   session_file=sess, expect=0)
    assert out["outcome"] == "concurred" and out["proceed_without_implementing"] is True
    assert triage_state(sess)["disputes"][0]["state"] == "concurred"
    # a resolved dispute cannot be concurred again
    run("concur", "--dispute", "1", "--review", write(tmp_path, "n.md", review(verdict="APPROVE")),
        session_file=sess, expect=2)
    assert run_json("status", session_file=sess, expect=0)["open_disputes"] == []


def test_concur_re_asserted_keeps_blocking(tmp_path):
    sess = session(tmp_path)
    first = write(tmp_path, "first.md", review(issues=[continuation_critical(anchor="src/cache.py")]))
    run("dispute", "--finding", "1", "--rationale", write(tmp_path, "why.md", "rationale"), "--review", first,
        session_file=sess)
    out = run_json("concur", "--dispute", "1", "--review",
                   write(tmp_path, "n.md", review(issues=[continuation_critical(anchor="src/cache.py")])),
                   session_file=sess, expect=1)
    assert out["outcome"] == "re-asserted" and out["proceed_without_implementing"] is False
    assert triage_state(sess)["disputes"][0]["state"] == "re-asserted"


def test_concur_without_key_treats_any_critical_as_re_asserting(tmp_path):
    sess = session(tmp_path)
    first = write(tmp_path, "first.md", review(issues=[continuation_critical(anchor=None)]))  # plan review: no anchor
    out = run_json("dispute", "--finding", "1", "--rationale", write(tmp_path, "why.md", "r"), "--review", first,
                   session_file=sess)
    assert out["dispute"]["key"] is None
    out = run_json("concur", "--dispute", "1", "--review",
                   write(tmp_path, "n.md", review(issues=[continuation_critical(anchor="src/unrelated.py")])),
                   session_file=sess, expect=1)
    assert out["outcome"] == "re-asserted"


def test_dispute_rejects_minor_incomplete_unknown_and_empty_rationale(tmp_path):
    sess = session(tmp_path)
    text = write(tmp_path, "r.md", review(issues=[continuation_critical(missing=("Impact",)), "- [MINOR] nit"]))
    why = write(tmp_path, "why.md", "rationale")
    for args, needle in (
        (("--finding", "1"), "failed triage"),
        (("--finding", "2"), "only a [CRITICAL] can be disputed"),
        (("--finding", "3"), "does not exist"),
    ):
        c = run("dispute", *args, "--rationale", why, "--review", text, session_file=sess, expect=2)
        assert needle in c.stderr, c.stderr
    complete = write(tmp_path, "c.md", review(issues=[continuation_critical()]))
    c = run("dispute", "--finding", "1", "--rationale", write(tmp_path, "e.md", "  \n"), "--review", complete,
            session_file=sess, expect=2)
    assert "rationale is empty" in c.stderr
    assert "## Evidence Ledger" not in sess.read_text(encoding="utf-8")  # nothing written on any rejection


def test_orchestrator_direct_dispute_requires_a_later_reviewer_approve_record(tmp_path):
    records = [ledger_record(1, "reviewer_approve"), ledger_record(2, "gate")]
    sess = session(tmp_path, records)
    first = write(tmp_path, "first.md", review(issues=[continuation_critical(anchor="src/a.py")]))
    out = run_json("dispute", "--finding", "1", "--rationale", write(tmp_path, "why.md", "r"), "--review", first,
                   "--author-route", "orchestrator-direct", session_file=sess)
    assert out["dispute"]["ledger_watermark"] == 2
    concurring = write(tmp_path, "n.md", review(verdict="APPROVE", issues=["- [MINOR] src/a.py fine"]))
    c = run("concur", "--dispute", "1", "--review", concurring, session_file=sess, expect=2)
    assert "pass --reviewer-record" in c.stderr
    c = run("concur", "--dispute", "1", "--review", concurring, "--reviewer-record", "1", session_file=sess, expect=2)
    assert "predates the dispute" in c.stderr
    c = run("concur", "--dispute", "1", "--review", concurring, "--reviewer-record", "2", session_file=sess, expect=2)
    assert "not a reviewer_approve record" in c.stderr
    assert triage_state(sess)["disputes"][0]["state"] == "awaiting-concurrence"
    # append a later reviewer_approve record (as evidence_ledger.py record would) → concurrence accepted
    text = sess.read_text(encoding="utf-8")
    ledger = ledger_mod.load_ledger(text)
    ledger["records"].append(ledger_record(3, "reviewer_approve"))
    sess.write_text(ledger_mod.store_ledger(text, ledger), encoding="utf-8")
    out = run_json("concur", "--dispute", "1", "--review", concurring, "--reviewer-record", "3",
                   session_file=sess, expect=0)
    assert out["outcome"] == "concurred"
    assert triage_state(sess)["disputes"][0]["concurring_record"] == 3


def test_usage_errors_for_unknown_ids_and_missing_session(tmp_path):
    sess = session(tmp_path)
    c = run("concur", "--dispute", "9", "--review", write(tmp_path, "n.md", review(verdict="APPROVE")),
            session_file=sess, expect=2)
    assert "unknown dispute id 9" in c.stderr
    c = run("revalidate", "--pending", "9", "--review", write(tmp_path, "n.md", review(verdict="APPROVE")),
            session_file=sess, expect=2)
    assert "unknown pending entry id 9" in c.stderr
    c = run("status", session_file=tmp_path / "missing.md", expect=2)
    assert "cannot read session file" in c.stderr
    c = run("check", "--input", str(tmp_path / "nope.md"), expect=2)
    assert "cannot read" in c.stderr


# ---------- quality-polish fixes ----------


def _emitted(fn, *args, capsys) -> str:
    """stdout of an invoker `_emit_*` helper (it writes the block and exits 1)."""
    with pytest.raises(SystemExit) as info:
        fn(*args)
    assert info.value.code == 1
    return capsys.readouterr().out


def test_invoker_synthetic_request_changes_is_infrastructure_not_a_finding(tmp_path, capsys):
    """Fix 1: every synthetic invoker block (cleanup / capture / adapter /
    banner / producer failure) is blocking (exit 1) but exempt from the
    rubric: never incomplete, never pending, never fed to the Executor."""
    sess = session(tmp_path)
    texts = {
        "cleanup": _emitted(invoker._emit_cleanup_request_changes, "rename failed", capsys=capsys),
        "capture": _emitted(invoker._emit_uncertain_stdout_request_changes, "drain-incomplete", b"{}", capsys=capsys),
        "adapter-malformed": invoker._format_adapter_malformed_request_changes("bad json"),
        "adapter-launch": invoker._format_adapter_launch_failure_request_changes("spawn failed"),
        "banner-missing": invoker._format_adapter_verdict_missing_request_changes(0, b""),
        "producer-nonzero": invoker._format_producer_nonzero_after_adapter_approve_request_changes(2, b"boom"),
    }
    for label, text in texts.items():
        report = check(tmp_path, text, 1, "--record-pending", "--round", "2", session_file=sess)
        assert report["shape"] == "gate" and report["verdict"] == "REQUEST_CHANGES", label
        assert report["infrastructure"] == [1] and report["incomplete"] == [] and report["complete"] == [], label
        (finding,) = report["findings"]
        assert finding["infrastructure"] is True and finding["complete"] is True and finding["rubric"] is None, label
        assert finding["anchor"] in ("scripts/adversarial_gate_invoke.py", "scripts/adversarial_gate_adapter.py"), label
        assert report["executor_dispatch"] is False and report["complete_findings_text"] == "", label
        assert report["pending"] == [] and report["result"] == "complete", label
        assert report["summary"] == [f"infrastructure_failure: gate finding #1 at {finding['anchor']} — "
                                     "not a finding; resolve the runtime failure and re-run Step 3"], label
    assert "## Evidence Ledger" not in sess.read_text(encoding="utf-8")   # nothing was ever recorded as pending
    # not disputable
    c = run("dispute", "--finding", "1", "--rationale", write(tmp_path, "why.md", "r"),
            "--review", write(tmp_path, "infra.md", texts["cleanup"]), session_file=sess, expect=2)
    assert "infrastructure failure is not disputable" in c.stderr
    assert "## Evidence Ledger" not in sess.read_text(encoding="utf-8")
    # a real complete finding alongside an infrastructure block: only the real one reaches the Executor
    infra_line = texts["adapter-malformed"].split("### Issues\n", 1)[1].strip()
    mixed = check(tmp_path, gate(infra_line, inline_critical(anchor="src/ok.py")), 1)
    assert mixed["infrastructure"] == [1] and mixed["complete"] == [2] and mixed["executor_dispatch"] is True
    assert "adversarial_gate_adapter" not in mixed["complete_findings_text"] and "src/ok.py" in mixed["complete_findings_text"]
    # the exemption is gate-shape only: a Reviewer finding *about* the invoker is an ordinary finding
    rv = check(tmp_path, review(issues=[continuation_critical(anchor="scripts/adversarial_gate_invoke.py",
                                                              missing=("Impact",))]), 1)
    assert rv["infrastructure"] == [] and rv["incomplete"] == [1]


def test_malformed_triage_next_id_is_a_usage_error(tmp_path):
    """Fix 3: ledger shape is validated once at load time (exit 2, named key)."""
    sess = session(tmp_path)
    text = sess.read_text(encoding="utf-8")
    ledger = ledger_mod.empty_ledger()
    ledger["triage"] = {"next_id": "abc", "disputes": [], "pending_rubric_incomplete": []}
    sess.write_text(ledger_mod.store_ledger(text, ledger), encoding="utf-8")
    c = run("status", session_file=sess, expect=2)
    assert "`triage.next_id` must be an integer, got 'abc'" in c.stderr and "Traceback" not in c.stderr


def test_storage_failure_exits_3_and_leaves_triage_state_unchanged(tmp_path):
    """Fix 5: exit 3 is a storage failure (nothing written), never `incomplete`."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory write permissions")
    sess = session(tmp_path)
    first = write(tmp_path, "first.md", review(issues=[continuation_critical(anchor="src/cache.py")]))
    why = write(tmp_path, "why.md", "rationale")
    run("dispute", "--finding", "1", "--rationale", why, "--review", first, session_file=sess)
    before = sess.read_text(encoding="utf-8")
    tmp_path.chmod(0o500)                       # the atomic-write temp file cannot be created
    try:
        c = run("dispute", "--finding", "1", "--rationale", why, "--review", first, session_file=sess, expect=3)
    finally:
        tmp_path.chmod(0o700)
    assert "finding-triage: storage failure:" in c.stderr and "(fail closed; nothing written)" in c.stderr
    assert sess.read_text(encoding="utf-8") == before
    assert [d["id"] for d in triage_state(sess)["disputes"]] == [1]


def test_session_lookup_resolves_repo_to_the_toplevel(tmp_path):
    """Fix 8: `--repo <subdirectory> --session <uuid>` finds the session under
    the repository toplevel, like evidence_ledger.py."""
    repo = tmp_path / "repo"
    (repo / "pkg" / "deep").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    uuid = "33333333-4444-5555-6666-777777777777"
    target = repo / ".review-loop" / "sessions" / f"{uuid}.md"
    target.parent.mkdir(parents=True)
    target.write_text(SESSION_TEMPLATE, encoding="utf-8")
    out = run_json("--repo", str(repo / "pkg" / "deep"), "--session", uuid, "status")
    assert out["revalidation_clear"] is True
    c = run("--repo", str(tmp_path), "--session", uuid, "status", expect=2)   # outside any repository
    assert "not a git repository" in c.stderr


# ---------- quality-polish round 2 ----------


def _gate_critical_at(anchor: str, confidence: str, missing=()) -> str:
    body = " ".join(f"{f}: {v}." for f, v in RUBRIC.items() if f not in missing)
    return f"- [CRITICAL] {anchor}:40-44 (confidence={confidence}) — unchecked return value: {body}".rstrip()


@pytest.mark.parametrize("confidence", ["0.85", "1.0"])
def test_rubric_complete_gate_finding_at_an_infrastructure_anchor_is_an_ordinary_finding(tmp_path, confidence):
    """H-1: anchor alone never classifies. A gate `[CRITICAL]` about the
    invoker / adapter that carries the rubric is dispatched and disputable
    whatever its confidence; only the emitters' signature (`confidence=1.0`
    AND no rubric label at all) is an infrastructure block."""
    sess = session(tmp_path)
    for anchor in ft.INFRASTRUCTURE_ANCHORS:
        text = gate(_gate_critical_at(anchor, confidence))
        report = check(tmp_path, text, 0, "--record-pending", "--round", "1", session_file=sess)
        assert report["infrastructure"] == [] and report["incomplete"] == [] and report["complete"] == [1], anchor
        (finding,) = report["findings"]
        assert finding["infrastructure"] is False and finding["complete"] is True and finding["anchor"] == anchor
        assert finding["rubric"]["Trigger"] == RUBRIC["Trigger"]
        assert report["executor_dispatch"] is True and anchor in report["complete_findings_text"]
        assert report["pending"] == [] and report["summary"] == []
        out = run_json("dispute", "--finding", "1", "--rationale", write(tmp_path, "why.md", "not reachable"),
                       "--review", write(tmp_path, "review.md", text), session_file=sess)
        assert out["dispute"]["anchor"] == anchor and out["dispute"]["state"] == "awaiting-concurrence"
    assert [d["anchor"] for d in triage_state(sess)["disputes"]] == list(ft.INFRASTRUCTURE_ANCHORS)


def test_infrastructure_classification_requires_all_three_signature_parts(tmp_path):
    """H-1: at an infrastructure anchor, `confidence=1.0` with one rubric label
    is an ordinary incomplete finding, and no rubric with another confidence
    is an ordinary incomplete finding — neither is exempt from the rubric."""
    anchor = ft.INFRASTRUCTURE_ANCHORS[0]
    partial = check(tmp_path, gate(_gate_critical_at(anchor, "1.0", missing=tuple(ft.RUBRIC_FIELDS[1:]))), 1)
    assert partial["infrastructure"] == [] and partial["incomplete"] == [1]
    assert partial["findings"][0]["missing"] == list(ft.RUBRIC_FIELDS[1:])
    bare = check(tmp_path, gate(f"- [CRITICAL] {anchor}:1-1 (confidence=0.9) — adapter output was malformed"), 1)
    assert bare["infrastructure"] == [] and bare["incomplete"] == [1]
    assert bare["findings"][0]["missing"] == list(ft.RUBRIC_FIELDS)
    real = check(tmp_path, gate(f"- [CRITICAL] {anchor}:1-1 (confidence=1.0) — adapter output was malformed"), 1)
    assert real["infrastructure"] == [1] and real["incomplete"] == []


def test_record_pending_resolves_the_session_eagerly(tmp_path):
    """M-2: `--record-pending` needs a session even when the review is
    complete; a missing or unreadable session is exit 2 before any output."""
    complete = write(tmp_path, "complete.md", review(issues=[continuation_critical()]))
    c = run("check", "--input", complete, "--record-pending", expect=2)
    assert "--session <uuid> is required" in c.stderr and c.stdout == ""
    c = run("check", "--input", complete, "--record-pending", session_file=tmp_path / "missing.md", expect=2)
    assert "cannot read session file" in c.stderr and c.stdout == ""
    sess = session(tmp_path)
    report = check(tmp_path, review(issues=[continuation_critical()]), 0, "--record-pending", session_file=sess)
    assert report["pending"] == [] and "## Evidence Ledger" not in sess.read_text(encoding="utf-8")
