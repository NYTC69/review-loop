"""Slice 4 — eight evaluation cases replayed under the old rule and the new rule.

Fixtures under `tests/fixtures/evaluation/case-{1..8}/` are **inputs only**: a
scripted repo, the sequence of writes, the orchestrator-declared Route Facts
and closures per round, `seeded_defects[]`, and the recorded Reviewer output
for every replayed round (the same text is fed to both rules). They contain no
expected dispatch counts, routes, stage sets or defect counts; every number
below is derived by the replay and asserted as a *property*.

Old rule — a small in-test model of today's semantics, cited to the audit
(`.compass/results/2026-09-15_orchestration-overhead-audit.md`, refs against
commit 75e28da):
  * Executor-always: every code write is an Executor dispatch (U:120-122,
    E:131-152, E:772-775); docs writes are the orchestrator carve-out
    (E:804-811).
  * All-clear + any write → full replay: `completed_stages` is all-or-nothing
    (S:377-378); any write clears it and replays from `exec` (S:400-409,
    E:562-564, E:951-953), re-triggering the Step 3.4 gate (E:377-379) and the
    3.5.5 tests with no test/code change (audit §P1 (b)(c), E:734-779).
  * Full history dump: the Reviewer re-reads the whole session file — every
    prior `## Review History` entry and the plan — every round (audit fact 3,
    P:235-241, E round step 5); review scope = the accumulated `## Files
    Changed` set (E:124-130).
  * Every `[CRITICAL]` is implemented (R:113-117, audit §P4): no triage, no
    dispute; a REQUEST_CHANGES with no further recorded input is a stuck /
    soft-limit pause (E:1021, P:411-416).

New rule — every decision comes from the production helpers via subprocess:
`scripts/evidence_ledger.py snapshot/record/check/classify/delta/route` and
`scripts/finding_triage.py check/dispute/concur/revalidate`. The harness never
hashes, mints or triages in prose.

Both replays run in throwaway git repositories under `tmp_path`; the real
repository's `.git` is never touched. The report is written to
`tests/skills/.artifacts/evaluation-cases.md` (gitignored).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER = REPO_ROOT / "scripts" / "evidence_ledger.py"
TRIAGE = REPO_ROOT / "scripts" / "finding_triage.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "evaluation"
ARTIFACTS = REPO_ROOT / "tests" / "skills" / ".artifacts"
REPORT = ARTIFACTS / "evaluation-cases.md"
SMOKE_CASES = {"case-6": "reviewer.case6.smoke.claude", "case-7": "reviewer.case7.smoke.claude"}

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import evidence_ledger as ledger_mod  # noqa: E402
import finding_triage as triage_mod  # noqa: E402
from review_verification import _validate_reviewer_output_schema  # noqa: E402

UUID = "e0a1ca5e-0000-4000-8000-000000000001"
NA = "N/A"
RULES = ("old", "new")
DECISION_PAUSE_KINDS = {"product", "risk", "scope", "authorization"}

# Every field a case × rule row must carry (plan §Mandatory measurement fields).
MEASUREMENT_FIELDS = (
    "executor_dispatches", "reviewer_dispatches", "gate_runs",
    "reuse", "checks_rerun", "checks_reused",
    "repeated_reads", "history_entries_total",
    "unchanged_content_reviewed", "unchanged_lines_reviewed",
    "tests_rerun", "tests_reused",
    "user_pauses", "pauses_without_decision",
    "elapsed_s", "model", "tokens_in", "tokens_out", "cost_usd",
    "blocking_rejected_or_downgraded", "stale_evidence_reused",
    "defects_found", "defects_missed", "theoretical_complexity_added",
)
OLD_RULE_REASON = "old rule: any write clears completed_stages and replays every check (S:400-409, E:951-953)"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}

SESSION_TEMPLATE = """## Problem Description
Evaluation replay of {title}.

## Context
- synthetic fixture {case_id} ({variant}); rule: {rule}

## Acceptance Criteria
- per fixture

## Current Phase
execution

## Approved Plan

- Source: reviewer-approved

{title}

## Current Review Packet

### Author route
executor

## Review History

## Files Changed
None.

## Key Related Files
None.

## Timing Log
| Phase | Round | Role | Duration |
|---|---|---|---|

## Session Metadata
- entry_point: execute
- plan_source: reviewer-approved
- base_head: 0000000000000000000000000000000000000000
- base_dirty: {{}}
- last_verified_head: 0000000000000000000000000000000000000000
- last_verified_dirty: {{}}
- session_commits: []
- completed_stages: []
- delivery_blocked_by: null
"""


class HarnessError(AssertionError):
    """A replay could not proceed; the case or the helpers are inconsistent."""


# ------------------------------------------------------------------ helpers


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(GIT_ENV)
    return subprocess.run(["git", *args], cwd=str(repo), env=env, capture_output=True, text=True, check=check)


def _apply_writes(repo: Path, writes: Dict[str, Optional[str]]) -> None:
    for rel, content in writes.items():
        target = repo / rel
        if content is None:
            if target.exists():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _tree(repo: Path, commit: str) -> Dict[str, str]:
    out = _git(repo, "ls-tree", "-r", "-z", commit).stdout
    paths: Dict[str, str] = {}
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        paths[path] = meta.split(" ")[2]
    return paths


def _blob_lines(repo: Path, blob: str) -> int:
    text = _git(repo, "cat-file", "-p", blob).stdout
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


_ANCHOR_RE = re.compile(
    r"File:\s*`?(?P<path>[^`\s,]+)`?(?:[^\n]*?(?:around\s+)?lines?\s+(?P<a>\d+)(?:\s*[-–]\s*(?P<b>\d+))?)?",
    re.IGNORECASE,
)


def finding_anchor(text: str) -> Optional[Tuple[str, int, int]]:
    match = _ANCHOR_RE.search(text)
    if not match or match.group("a") is None:
        return None
    a = int(match.group("a"))
    b = int(match.group("b") or a)
    return match.group("path"), a, b


def load_case(case_dir: Path) -> dict:
    return json.loads((case_dir / "case.json").read_text(encoding="utf-8"))


def case_variants(case_dir: Path) -> List[Tuple[str, dict, list]]:
    data = load_case(case_dir)
    out = [("main", data, data["steps"])]
    for name, variant in (data.get("variants") or {}).items():
        out.append((name, data, variant["steps"]))
    return out


def all_variants() -> List[Tuple[str, str]]:
    ids = []
    for case_dir in sorted(FIXTURES.glob("case-*")):
        for name, _data, _steps in case_variants(case_dir):
            ids.append((case_dir.name, name))
    return ids


# ------------------------------------------------------------------- replay


class Replay:
    """One case × rule replay in a throwaway repo. Trace fields are derived
    from helper output (new rule) or the audited old-rule model (old rule)."""

    def __init__(self, case_dir: Path, variant: str, rule: str, root: Path):
        self.case_dir = case_dir
        self.data = load_case(case_dir)
        self.variant = variant
        self.steps = self.data["steps"] if variant == "main" else self.data["variants"][variant]["steps"]
        self.rule = rule
        self.repo = root / f"{case_dir.name}-{variant}-{rule}"
        self.session = self.repo / ".review-loop" / "sessions" / f"{UUID}.md"
        self.trace: dict = {
            "case": case_dir.name, "variant": variant, "rule": rule,
            "executor_dispatches": 0, "reviewer_dispatches": 0, "gate_runs": 0,
            "repeated_reads": 0, "unchanged_content_reviewed": 0, "unchanged_lines_reviewed": 0,
            "tests_rerun": 0, "tests_reused": 0, "tests_events": [],
            "user_pauses": [], "blocking_rejected_or_downgraded": 0, "stale_evidence_reused": 0,
            "rounds": [], "implemented_findings": [], "fates": {}, "revalidation_rounds": [],
            "pending_incomplete": [], "route_decisions": [], "classify": [], "records_after_dispute": [],
            "elapsed_s": NA, "model": NA, "tokens_in": NA, "tokens_out": NA, "cost_usd": NA,
        }
        self.history: List[str] = []           # entry ids in order
        self.files_changed: set = set()        # old rule: accumulated `## Files Changed`
        self.convergence_paths: set = set()    # new rule: review-target paths of the open convergence
        self.closure: Dict[str, set] = {"inputs": set(), "deps": set()}  # declared closure of the open convergence
        self.reviewed_at: Optional[int] = None  # snapshot the previous packet was reviewed at
        self.prev_presented: set = set()
        self.exec_minted = False
        self.old_gate_spent = False  # old rule: Step 3.4 is single-pass per convergence (E:366-376)
        self.gate_spent = False      # new rule: same single-pass rule, per exec-invalidating convergence
        self.pending_gate_repair = False
        self.last_review: Optional[dict] = None
        self.ended = False

    # ---- infrastructure ------------------------------------------------

    def setup(self) -> None:
        self.repo.mkdir(parents=True)
        _git(self.repo, "init", "-q")
        _apply_writes(self.repo, self.data["repo"])
        (self.repo / ".gitignore").write_text(".review-loop/\n__pycache__/\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "base")
        self.session.parent.mkdir(parents=True)
        self.session.write_text(SESSION_TEMPLATE.format(
            title=self.data["title"], case_id=self.data["id"], variant=self.variant, rule=self.rule), encoding="utf-8")
        self.snapshot("init")  # snap/0: the verified worktree

    def ledger(self, *args: str, expect=(0,)) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(GIT_ENV)
        completed = subprocess.run([sys.executable, str(LEDGER), "--repo", str(self.repo), "--session", UUID, *args],
                                   capture_output=True, text=True, env=env, check=False)
        if completed.returncode not in expect:
            raise HarnessError(f"evidence_ledger {' '.join(args)} exit {completed.returncode}\n{completed.stdout}\n{completed.stderr}")
        return completed

    def ledger_json(self, *args: str, expect=(0,)) -> dict:
        return json.loads(self.ledger(*args, expect=expect).stdout)

    def triage(self, *args: str, expect=(0,)) -> subprocess.CompletedProcess:
        completed = subprocess.run([sys.executable, str(TRIAGE), "--session-file", str(self.session), *args],
                                   capture_output=True, text=True, check=False)
        if completed.returncode not in expect:
            raise HarnessError(f"finding_triage {' '.join(args)} exit {completed.returncode}\n{completed.stdout}\n{completed.stderr}")
        return completed

    def triage_json(self, *args: str, expect=(0,)) -> Tuple[dict, int]:
        completed = self.triage(*args, expect=expect)
        return json.loads(completed.stdout), completed.returncode

    def fixture_text(self, ref: str) -> str:
        return (self.case_dir / ref).read_text(encoding="utf-8")

    def fixture_file(self, ref: str) -> str:
        return str(self.case_dir / ref)

    def snapshot(self, label: str) -> int:
        return self.ledger_json("snapshot", "--label", label)["snapshot"]

    def snapshots(self) -> List[dict]:
        return ledger_mod.load_ledger(self.session.read_text(encoding="utf-8"))["snapshots"]

    def records(self) -> List[dict]:
        return ledger_mod.load_ledger(self.session.read_text(encoding="utf-8"))["records"]

    def replace_section(self, heading: str, body: str) -> None:
        text = self.session.read_text(encoding="utf-8")
        spans = ledger_mod.canonical_section_candidates(text, heading)
        if len(spans) != 1:
            raise HarnessError(f"section {heading} not uniquely identifiable in the replay session")
        start, end = spans[0]
        self.session.write_text(text[:start] + f"{heading}\n\n{body.rstrip()}\n\n" + text[end:], encoding="utf-8")

    def write_packet(self, route_facts: Optional[dict], referenced: List[str]) -> None:
        lines = ["### Referenced history entries", ", ".join(referenced) or "none", ""]
        if route_facts:
            lines += ["### Route Facts", "", "| fact | value | rationale |", "|---|---|---|"]
            lines += [f"| {name} | {value} | {why} |" for name, (value, why) in route_facts.items()]
            lines.append("")
        lines += ["### Author route", "pending"]
        self.replace_section(ledger_mod.PACKET_HEADING, "\n".join(lines))

    def add_history(self, entry_id: str, text: str) -> None:
        self.history.append(entry_id)
        body = self.session.read_text(encoding="utf-8")
        # the harness owns this session: the two headings are unique by construction
        assert body.count("## Review History\n") == 1 and body.count("## Files Changed\n") == 1
        start = body.index("## Review History\n")
        end = body.index("## Files Changed\n")
        section = body[start:end].rstrip("\n") + f"\n\n### {entry_id}\n\n{text.rstrip()}\n\n"
        self.session.write_text(body[:start] + section + body[end:], encoding="utf-8")

    # ---- measurement points ------------------------------------------

    def run_tests_boundary(self, checks: Dict[str, dict]) -> None:
        """At every write boundary. Old rule: every declared check reruns
        (audit §P1 (b)(c)). New rule: rerun only when `check` says the active
        record is invalid or absent; otherwise record `reused-from` with why."""
        if not checks:
            return
        report = self.ledger_json("check", "--no-write")
        for _name, spec in checks.items():
            command = spec["command"]
            claim_id = f"tests:{command}"
            claim = report["claims"].get(claim_id)
            valid = bool(claim and claim["valid"])
            reasons = claim["reasons"] if claim else ["no record"]
            inputs_changed = not valid
            common = ["record", "--check", "tests", "--scope", command, "--env-command", command,
                      "--closure", "declared", "--stage", "polish"]
            for path in spec.get("inputs", []):
                common += ["--input", path]
            for path in spec.get("deps", []):
                common += ["--dep", path]
            for selector in spec.get("selectors", []):
                common += ["--selector", selector]
            if self.rule == "old" or not valid:
                completed = subprocess.run(shlex.split(command), cwd=str(self.repo), capture_output=True, text=True)
                result = "PASS" if completed.returncode == 0 else "FAIL"
                self.ledger_json(*common, "--result", result)
                self.trace["tests_rerun"] += 1
                self.trace["tests_events"].append({"claim_id": claim_id, "provenance": "fresh", "result": result,
                                                   "inputs_changed": inputs_changed,
                                                   "reason": "; ".join(reasons) if self.rule == "new" else OLD_RULE_REASON,
                                                   "helper_reason": "; ".join(reasons)})
            else:
                why = f"inputs, deps and selector members byte-identical to record {claim['id']} ({'; '.join(reasons)})"
                self.ledger_json(*common, "--result", "PASS", "--provenance", f"reused-from:{claim['id']}", "--why", why)
                self.trace["tests_reused"] += 1
                self.trace["tests_events"].append({"claim_id": claim_id, "provenance": f"reused-from:{claim['id']}",
                                                   "result": "PASS", "inputs_changed": False, "reason": "; ".join(reasons),
                                                   "helper_reason": "; ".join(reasons)})

    def author(self, step: dict) -> Tuple[str, List[str]]:
        writes = step.get("writes") or {}
        kind = step.get("write_kind", "code")
        if not writes:
            return "n/a", []
        paths = sorted(writes)
        if kind == "docs":
            _apply_writes(self.repo, writes)   # orchestrator carve-out under both rules (E:804-811)
            self.add_history(f"D{len(self.history) + 1} orchestrator docs write", "paths: " + ", ".join(paths))
            return "orchestrator-docs", paths
        if self.rule == "old" or not step.get("route_facts"):
            _apply_writes(self.repo, writes)
            self.trace["executor_dispatches"] += 1
            self.add_history(f"E{len(self.history) + 1} Executor", "paths: " + ", ".join(paths))
            return "executor", paths
        self.write_packet(step["route_facts"], [])
        decision = self.ledger_json("route", *sum((["--path", p] for p in paths), []), expect=(0, 1))
        self.trace["route_decisions"].append({"paths": paths, "route": decision["route"], "reasons": decision["reasons"]})
        _apply_writes(self.repo, writes)
        if decision["route"] == "orchestrator-direct":
            self.add_history(f"DIR{len(self.history) + 1} Direct Implementation Record", "- Author route: orchestrator-direct\npaths: " + ", ".join(paths))
            return "orchestrator-direct", paths
        self.trace["executor_dispatches"] += 1
        self.add_history(f"E{len(self.history) + 1} Executor", "paths: " + ", ".join(paths))
        return "executor", paths

    def review_round(self, review_ref: str, scope: List[str], post: int, referenced: List[str],
                     executor_dispatched: bool = True, revalidation: bool = False) -> dict:
        text = self.fixture_text(review_ref)
        self.trace["reviewer_dispatches"] += 1
        if self.rule == "old":
            presented = set(self.history) | {"plan"}
        else:
            presented = set(referenced)
        repeated = len(presented & self.prev_presented)
        self.trace["repeated_reads"] += repeated
        unchanged: List[str] = []
        unchanged_lines = 0
        if self.reviewed_at is not None:
            snaps = {s["n"]: s for s in self.snapshots()}
            pre_tree = _tree(self.repo, snaps[self.reviewed_at]["commit"])
            post_tree = _tree(self.repo, snaps[post]["commit"])
            for path in sorted(scope):
                if path in pre_tree and path in post_tree and pre_tree[path] == post_tree[path]:
                    unchanged.append(path)
                    unchanged_lines += _blob_lines(self.repo, post_tree[path])
        self.trace["unchanged_content_reviewed"] += len(unchanged)
        self.trace["unchanged_lines_reviewed"] += unchanged_lines
        verdict, _issues, error = _validate_reviewer_output_schema(text)
        if error is not None:
            raise HarnessError(f"recorded review {review_ref} is schema-invalid: {error}")
        parsed = triage_mod.parse_review(text)
        findings = []
        for finding in parsed["findings"]:
            anchor = finding_anchor(finding["text"])
            findings.append({"n": finding["n"], "severity": finding["severity"], "anchor": anchor,
                             "text": finding["text"]})
        entry_id = f"R{len(self.history) + 1} Reviewer"
        self.add_history(entry_id, text)
        round_record = {
            "n": len(self.trace["rounds"]) + 1, "review": review_ref, "verdict": verdict, "entry": entry_id,
            "scope": sorted(scope), "pre_snapshot": self.reviewed_at, "post_snapshot": post,
            "presented": sorted(presented), "previously_presented": sorted(self.prev_presented),
            "repeated_reads": repeated, "unchanged_paths": unchanged, "unchanged_lines": unchanged_lines,
            "findings": findings, "executor_dispatched": executor_dispatched, "revalidation_round": revalidation,
            "review_text": text,
        }
        self.trace["rounds"].append(round_record)
        self.prev_presented = presented
        self.reviewed_at = post
        self.last_review = {"ref": review_ref, "text": text, "verdict": verdict, "findings": findings,
                            "file": self.fixture_file(review_ref), "entry": entry_id}
        return round_record

    # ---- new-rule ledger bookkeeping -----------------------------------

    def closure_args(self, closure: Optional[dict] = None) -> List[str]:
        """`--input` / `--dep` flags of the declared closure (deleted paths bind as tombstones)."""
        inputs = set(closure["inputs"]) if closure else set(self.closure["inputs"])
        deps = set(closure.get("deps", [])) if closure else set(self.closure["deps"])
        args: List[str] = []
        for path in sorted(inputs):
            args += ["--input", path]
        for path in sorted(deps - inputs):
            args += ["--dep", path]
        return args

    def record_review(self, result: str, scope: List[str], author_route: str, closure: Optional[dict] = None) -> dict:
        args = ["record", "--check", "reviewer_approve", "--closure", "declared", "--result", result,
                "--author-route", author_route if author_route in ("executor", "orchestrator-direct") else "n/a"]
        for path in sorted(scope):
            args += ["--scope-path", path]
        args += self.closure_args(closure)
        new_key = "|".join(sorted(set(scope)))
        supersedes = [str(r["id"]) for r in self.records()
                      if r["check"] == "reviewer_approve" and r.get("superseded_by") is None
                      and r["claim_id"] != f"reviewer_approve:{new_key}"
                      and set(r["scope"].split("|")) <= set(scope)]
        if supersedes and result == "PASS":
            args += ["--supersedes", ",".join(supersedes)]
        return self.ledger_json(*args)

    def record_gate(self, result: str, scope: List[str], assumption: Optional[str] = None) -> dict:
        args = ["record", "--check", "gate", "--closure", "declared", "--result", result]
        for path in sorted(scope):
            args += ["--scope-path", path]
        args += self.closure_args()
        if assumption:
            args += ["--assumption", assumption]
        return self.ledger_json(*args)

    def stages(self) -> List[str]:
        return self.ledger_json("check")["completed_stages"]

    def delta_scope(self, post: int) -> List[str]:
        """Paths of the packet's Attributable Delta: snap/{reviewed_at} → snap/{post} (empty when equal)."""
        pre = self.reviewed_at if self.reviewed_at is not None else 0
        if post <= pre:
            return []
        return [row["path"] for row in self.ledger_json("delta", "--format", "json", "--pre", str(pre), "--post", str(post))["rows"]]

    # ---- the round model -------------------------------------------------

    def run(self) -> dict:
        self.setup()
        steps = list(self.steps)
        i = 0
        while i < len(steps) and not self.ended:
            step = steps[i]
            kind = step["kind"]
            if kind == "write":
                self.handle_write(step)
            elif kind == "dispute":
                self.handle_dispute(step)
            elif kind == "retry":
                self.handle_retry(step)
            else:
                raise HarnessError(f"unknown step kind {kind}")
            i += 1
        if not self.ended and self.rule == "old" and self.last_review and self.last_review["verdict"] == "REQUEST_CHANGES":
            # no further recorded input: today's stuck / soft-limit pause (E:1021, P:411-416)
            self.trace["user_pauses"].append({"kind": "other", "reason": "soft_limit_exec / stuck: CRITICAL remains with no further input"})
        self.finish()
        return self.trace

    def note_implemented(self) -> None:
        """A repair write implements every complete CRITICAL of the review it answers."""
        review = self.last_review
        if not review or review["verdict"] != "REQUEST_CHANGES" or review.get("discarded"):
            return
        for finding in review["findings"]:
            if finding["severity"] == "CRITICAL":
                self.trace["implemented_findings"].append([review["ref"], finding["n"]])

    def handle_write(self, step: dict) -> None:
        if step.get("writes"):
            self.note_implemented()
        route, paths = self.author(step)
        classify = None
        if self.rule == "new" and paths:
            # convergence decision on the worktree vs the last stored snapshot, before this write's snapshot
            classify = self.ledger_json("classify", expect=(0, 1))
            self.trace["classify"].append({"paths": paths, "exec": classify["exec"], "reasons": classify["reasons"]})
        post = self.snapshot(f"after {route}")
        if self.rule == "new":
            self.new_rule_after_write(step, route, paths, post, classify)
        else:
            self.old_rule_after_write(step, route, paths, post)

    # -- old rule ----------------------------------------------------------

    def old_rule_after_write(self, step: dict, route: str, paths: List[str], post: int) -> None:
        # a write after `exec` was minted clears the whole set and starts a new convergence with its own
        # gate pass (S:400-409, E:377-379); a repair write answering REQUEST_CHANGES stays inside the
        # convergence and never re-runs the gate (E:372-376)
        if self.exec_minted:
            self.exec_minted = False
            self.old_gate_spent = False
        self.files_changed |= set(paths)
        scope = sorted(self.files_changed) if paths else list(step.get("review_target", []))
        self.run_tests_boundary(step.get("checks") or {})
        rec = self.review_round(step["review"], scope, post, [])
        self.old_rule_verdict(step, rec)

    def old_rule_verdict(self, step: dict, rec: dict) -> None:
        if rec["verdict"] == "APPROVE":
            gate_ref = step.get("gate")
            if gate_ref and not self.old_gate_spent:
                self.old_gate_spent = True
                self.trace["gate_runs"] += 1
                gate_text = self.fixture_text(gate_ref)
                self.add_history(f"G{len(self.history) + 1} gate", gate_text)
                if gate_text.startswith("adversarial-gate: REQUEST_CHANGES"):
                    parsed = triage_mod.parse_review(gate_text)
                    self.old_rule_repair(step.get("old_rule_repair_writes") or {}, step.get("checks") or {},
                                         step["revalidation_review"], gate_ref, parsed["findings"], step)
                    return
            self.exec_minted = True
            self.trace.setdefault("old_convergences", 0)
            self.trace["old_convergences"] += 1
        # REQUEST_CHANGES: the next step supplies the repair (or the case ends stuck)

    def old_rule_repair(self, writes: dict, checks: dict, review_ref: str, source_ref: str,
                        findings: List[dict], step: dict) -> None:
        """Old rule implements every CRITICAL it was given (R:113-117)."""
        for finding in findings:
            if finding["severity"] == "CRITICAL":
                self.trace["implemented_findings"].append([source_ref, finding["n"]])
        if self.last_review is not None:
            self.last_review = dict(self.last_review, verdict="APPROVE")  # consumed; never double-counted
        _apply_writes(self.repo, writes)
        self.trace["executor_dispatches"] += 1
        self.add_history(f"E{len(self.history) + 1} Executor (repair)", "paths: " + ", ".join(sorted(writes)))
        self.files_changed |= set(writes)
        post = self.snapshot("after repair")
        self.run_tests_boundary(checks)
        rec = self.review_round(review_ref, sorted(self.files_changed), post, [])
        self.old_rule_verdict(step, rec)

    # -- new rule ----------------------------------------------------------

    def new_rule_after_write(self, step: dict, route: str, paths: List[str], post: int, classify) -> None:
        review_only = not paths
        declared = step.get("review_closure") or {"inputs": paths, "deps": []}
        if review_only:
            scope = list(step.get("review_target", []))
            self.convergence_paths = set(scope)
            self.closure = {"inputs": set(declared["inputs"]), "deps": set(declared.get("deps", []))}
            new_convergence = True
        else:
            invalidating = classify["exec"] == "invalidated"
            if invalidating and self.exec_minted:
                self.convergence_paths = set()
                self.closure = {"inputs": set(), "deps": set()}
                self.exec_minted = False
                self.gate_spent = False
            new_convergence = invalidating or not self.exec_minted
            if new_convergence:
                self.convergence_paths |= set(paths)
                self.closure["inputs"] |= set(declared["inputs"])
                self.closure["deps"] |= set(declared.get("deps", []))
            scope = self.delta_scope(post)
        self.run_tests_boundary(step.get("checks") or {})
        self.write_packet(step.get("route_facts"), [])
        rec = self.review_round(step["review"], scope, post, [], executor_dispatched=(route == "executor"))
        self.new_rule_verdict(step, rec, route, scope, new_convergence)

    def new_rule_triage(self, rec: dict) -> dict:
        report, code = self.triage_json("check", "--input", self.last_review["file"], expect=(0, 1))
        for finding in report["findings"]:
            if finding["severity"] == "CRITICAL":
                fate = "blocking" if finding["complete"] else "failed-check"
                self.trace["fates"][f"{rec['review']}#{finding['n']}"] = fate
        if code == 1:
            self.trace["pending_incomplete"].append({"review": rec["review"], "summary": report["summary"]})
            self.add_history(f"T{len(self.history) + 1} triage", "\n".join(report["summary"]))
        return report

    def new_rule_verdict(self, step: dict, rec: dict, route: str, scope: List[str], new_convergence: bool) -> None:
        report = self.new_rule_triage(rec)
        if report["result"] == "incomplete":
            self.last_review["discarded"] = True
            return  # discarded as malformed; the next step must be `retry`
        if rec["verdict"] == "REQUEST_CHANGES":
            self.record_review("FAIL", sorted(self.convergence_paths), route)
            return
        # APPROVE
        if step.get("write_kind") == "docs" and not new_convergence:
            self.ledger_json("record", "--check", "docs_consistency", "--closure", "declared", "--result", "PASS",
                             *sum((["--input", p] for p in scope), []))
        elif new_convergence:
            self.record_review("PASS", sorted(self.convergence_paths), route)
        else:
            declared = step.get("review_closure") or {"inputs": scope, "deps": []}
            self.record_review("PASS", scope, route, closure=declared)
        outcome = "replay"
        if new_convergence:
            if not self.gate_spent:
                outcome = self.new_rule_gate(step)
            else:
                # gate already spent for this convergence: the repair round's APPROVE mints exec
                self.record_gate("PASS", sorted(self.convergence_paths),
                                 "gate findings repaired through the ordinary loop and approved by the normal Reviewer; gate single-pass, not re-run")
                self.pending_gate_repair = False
                outcome = "minted"
        stages = self.stages()
        if "exec" in stages:
            self.exec_minted = True
        elif outcome != "repair-pending":
            raise HarnessError(f"exec not present after APPROVE + gate: {self.ledger_json('check')['stages']['exec']}")

    def new_rule_gate(self, step: dict) -> str:
        """Run the recorded gate once for this convergence. Returns `minted`
        (gate PASS / controlled-skip / findings dropped on revalidation) or
        `repair-pending` (a re-asserted finding needs an Executor round)."""
        self.gate_spent = True
        gate_ref = step.get("gate")
        scope = sorted(self.convergence_paths)
        if not gate_ref:
            self.ledger_json("record", "--check", "gate", "--disposition", "controlled-skip",
                             "--reason", "adversarial-gate: SKIP reason=review-only-first-round", "--closure", "declared",
                             *sum((["--scope-path", p] for p in scope), []), *self.closure_args())
            return "minted"
        self.trace["gate_runs"] += 1
        gate_text = self.fixture_text(gate_ref)
        gate_entry = f"G{len(self.history) + 1} gate"
        self.add_history(gate_entry, gate_text)
        if gate_text.startswith("adversarial-gate: APPROVE"):
            self.record_gate("PASS", scope)
            return "minted"
        report, _code = self.triage_json("check", "--input", self.fixture_file(gate_ref), "--record-pending",
                                         "--source", "gate", "--round", str(len(self.trace["rounds"])), expect=(0, 1))
        for finding in report["findings"]:
            if finding["severity"] == "CRITICAL":
                self.trace["fates"][f"{gate_ref}#{finding['n']}"] = "blocking" if finding["complete"] else "failed-check"
        self.record_gate("FAIL", scope)
        if report["executor_dispatch"]:
            raise HarnessError("fixture gate output has complete findings; the harness models incomplete-only gate outputs")
        # Executor-less revalidation round: Reviewer first (§Gate rubric revalidation)
        pending_ids = report["pending"]
        self.trace["revalidation_rounds"].append({"pending": pending_ids, "executor_dispatches": 0})
        post = self.snapshots()[-1]["n"]
        self.write_packet(None, [gate_entry])
        rec = self.review_round(step["revalidation_review"], self.delta_scope(post), post, [gate_entry],
                                executor_dispatched=False, revalidation=True)
        outcomes = []
        for pending in pending_ids:
            result, code = self.triage_json("revalidate", "--pending", str(pending), "--review", self.last_review["file"],
                                            expect=(0, 1, 3))
            outcomes.append(result["outcome"])
            self.trace["fates"][f"{gate_ref}#{pending}"] = {"concurred": "dropped", "re-asserted": "re-asserted",
                                                            "incomplete": "failed-check"}[result["outcome"]]
        self.trace["revalidation_rounds"][-1]["outcomes"] = outcomes
        for finding in triage_mod.parse_review(self.last_review["text"])["findings"]:
            if finding["severity"] == "CRITICAL":
                self.trace["fates"].setdefault(f"{rec['review']}#{finding['n']}", "blocking")
        if all(o == "concurred" for o in outcomes):
            self.trace["blocking_rejected_or_downgraded"] += len(outcomes)
            if rec["verdict"] != "APPROVE":
                raise HarnessError("all pending entries dropped but the revalidation output is not APPROVE")
            self.record_review("PASS", scope, "executor")
            self.record_gate("PASS", scope, "gate findings dropped by the Reviewer on revalidation; gate single-pass, not re-run")
            return "minted"
        if rec["verdict"] == "REQUEST_CHANGES":
            self.record_review("FAIL", scope, "executor")
            self.pending_gate_repair = True
            return "repair-pending"
        raise HarnessError(f"unexpected revalidation outcomes {outcomes} with verdict {rec['verdict']}")

    def handle_dispute(self, step: dict) -> None:
        if self.rule == "old":
            writes = step.get("old_rule_repair_writes")
            if not writes:
                return  # old rule cannot dispute; the next `write` step is its repair
            self.old_rule_repair(writes, self.last_step_checks(), step["review"], self.last_review["ref"],
                                 triage_mod.parse_review(self.last_review["text"])["findings"], step)
            return
        finding = step["finding"]
        result, _code = self.triage_json("dispute", "--finding", str(finding), "--rationale", self.fixture_file(step["rationale"]),
                                         "--review", self.last_review["file"], "--round", str(len(self.trace["rounds"])))
        dispute_id = result["dispute"]["id"]
        watermark = result["dispute"]["ledger_watermark"]
        self.add_history(f"T{len(self.history) + 1} dispute", result["history_line"])
        post = self.snapshots()[-1]["n"]
        self.write_packet(None, [self.last_review["entry"]])
        disputed_ref = self.last_review["ref"]
        rec = self.review_round(step["review"], self.delta_scope(post), post, [self.last_review["entry"]],
                                executor_dispatched=False)
        outcome, code = self.triage_json("concur", "--dispute", str(dispute_id), "--review", self.last_review["file"],
                                         expect=(0, 1, 3))
        self.trace["fates"][f"{disputed_ref}#{finding}"] = {"concurred": "concurred", "re-asserted": "re-asserted",
                                                            "incomplete": "failed-check"}[outcome["outcome"]]
        for f in triage_mod.parse_review(self.last_review["text"])["findings"]:
            if f["severity"] == "CRITICAL":
                self.trace["fates"].setdefault(f"{rec['review']}#{f['n']}", "blocking" if f.get("n") else "blocking")
        if outcome["outcome"] == "concurred":
            self.trace["blocking_rejected_or_downgraded"] += 1
            if rec["verdict"] != "APPROVE":
                raise HarnessError("concurred but the concurrence round is not APPROVE")
            new_rec = self.record_review("PASS", sorted(self.convergence_paths), "executor")
            self.trace["records_after_dispute"].append({"id": new_rec["id"], "watermark": watermark, "provenance": "fresh"})
            if not self.gate_spent:
                self.new_rule_gate(step)
            if "exec" not in self.stages():
                raise HarnessError("exec absent after concurrence")
            self.exec_minted = True
        elif outcome["outcome"] == "re-asserted":
            self.record_review("FAIL", sorted(self.convergence_paths), "executor")
            if step.get("on_reassert") == "pause:risk":
                self.trace["user_pauses"].append({"kind": "risk", "reason": "re-asserted complete CRITICAL; risk-tolerance decision"})
                self.ended = True
        else:
            raise HarnessError("concurrence output incomplete: fixture must model the retry table explicitly")

    def handle_retry(self, step: dict) -> None:
        if self.rule == "old":
            self.old_rule_repair(step.get("old_rule_repair_writes") or {}, self.last_step_checks(), step["review"],
                                 self.last_review["ref"], triage_mod.parse_review(self.last_review["text"])["findings"], step)
            return
        if not self.last_review.get("discarded"):
            raise HarnessError("retry step without a discarded (incomplete) review")
        post = self.snapshots()[-1]["n"]
        self.write_packet(None, [])
        rec = self.review_round(step["review"], self.delta_scope(post), post, [], executor_dispatched=False)
        self.new_rule_verdict(step, rec, "executor", sorted(self.convergence_paths), True)

    def last_step_checks(self) -> dict:
        for step in reversed(self.steps):
            if step.get("checks"):
                return step["checks"]
        return {}

    # ---- derived fields --------------------------------------------------

    def finish(self) -> None:
        t = self.trace
        t["history_entries_total"] = len(self.history)
        t["pauses_without_decision"] = sum(1 for p in t["user_pauses"] if p["kind"] not in DECISION_PAUSE_KINDS)
        final = self.ledger_json("check")
        claims = final["claims"]
        reuse = []
        for rec in self.records():
            claim = claims.get(rec["claim_id"]) if claims.get(rec["claim_id"], {}).get("id") == rec["id"] else None
            reuse.append({
                "id": rec["id"], "claim_id": rec["claim_id"], "provenance": rec["provenance"], "why": rec.get("why"),
                "reason": "; ".join(claim["reasons"]) if claim else ("superseded" if rec.get("superseded_by") else "inactive"),
                "result": rec.get("result"), "disposition": rec["disposition"], "valid": claim["valid"] if claim else None,
            })
            if rec["provenance"].startswith("reused-from:") and self.rule == "old":
                raise HarnessError("old rule must never reuse")
        t["reuse"] = reuse
        t["checks_rerun"] = sum(1 for r in reuse if r["provenance"] == "fresh")
        t["checks_reused"] = sum(1 for r in reuse if r["provenance"].startswith("reused-from:"))
        # stale_evidence_reused: a reuse whose source was not valid at reuse time is refused by the helper (exit 2);
        # every reuse the harness recorded was preceded by a `check` reporting the source valid.
        t["stale_evidence_reused"] = sum(1 for e in t["tests_events"] if e["provenance"] != "fresh" and e["inputs_changed"])
        t["completed_stages"] = final["completed_stages"]
        t["ledger_records"] = self.records()
        t["session_file"] = str(self.session)
        t["repo"] = str(self.repo)
        # defects found: seeded defects flagged in a round whose presented scope contains the file
        seeded = self.data["seeded_defects"]
        found = set()
        for rnd in t["rounds"]:
            for finding in rnd["findings"]:
                anchor = finding["anchor"]
                if not anchor:
                    continue
                path, a, b = anchor
                if path not in rnd["scope"]:
                    continue
                for idx, defect in enumerate(seeded):
                    if defect["file"] == path and a <= defect["line_end"] and b >= defect["line_start"]:
                        found.add(idx)
        t["defects_found"] = len(found)
        t["defects_missed"] = len(seeded) - len(found)
        t["snapshots"] = [{"n": s["n"], "commit": s["commit"]} for s in self.snapshots()]


def replay_pair(case_dir: Path, variant: str, root: Path) -> Dict[str, dict]:
    traces = {rule: Replay(case_dir, variant, rule, root).run() for rule in RULES}
    fates = traces["new"]["fates"]
    for rule in RULES:
        t = traces[rule]
        # a CRITICAL an implementation was dispatched for, although the new-rule replay of the same recorded
        # output failed triage, was concurred after a dispute, or was dropped on gate revalidation
        t["theoretical_complexity_added"] = sum(
            1 for ref, n in t["implemented_findings"] if fates.get(f"{ref}#{n}") in ("failed-check", "concurred", "dropped"))
    return traces


# ------------------------------------------------------------ session-scoped run


@pytest.fixture(scope="module")
def traces(tmp_path_factory) -> Dict[Tuple[str, str], Dict[str, dict]]:
    root = tmp_path_factory.mktemp("evaluation")
    out = {}
    only = {c for c in os.environ.get("EVAL_CASES", "").split(",") if c}  # developer filter; unset = every case
    for case_dir in sorted(FIXTURES.glob("case-*")):
        if only and case_dir.name not in only:
            continue
        for name, _data, _steps in case_variants(case_dir):
            out[(case_dir.name, name)] = replay_pair(case_dir, name, root)
    return out


def _row(traces, case: str, variant: str, rule: str) -> dict:
    if (case, variant) not in traces:
        pytest.skip(f"{case} filtered out by EVAL_CASES")
    return traces[(case, variant)][rule]


# ----------------------------------------------------------------- assertions


@pytest.mark.parametrize("case,variant", all_variants())
@pytest.mark.parametrize("rule", RULES)
def test_row_schema_and_na_fields(traces, case, variant, rule):
    row = _row(traces, case, variant, rule)
    for field in MEASUREMENT_FIELDS:
        assert field in row, f"{case}/{variant}/{rule} lacks {field}"
    for field in ("elapsed_s", "model", "tokens_in", "tokens_out", "cost_usd"):
        assert row[field] == NA
    for field in ("executor_dispatches", "reviewer_dispatches", "gate_runs", "repeated_reads", "unchanged_content_reviewed",
                  "unchanged_lines_reviewed", "tests_rerun", "tests_reused", "checks_rerun", "checks_reused",
                  "defects_found", "defects_missed", "theoretical_complexity_added", "pauses_without_decision",
                  "history_entries_total", "blocking_rejected_or_downgraded"):
        assert isinstance(row[field], int) and not isinstance(row[field], bool) and row[field] >= 0, field
    for entry in row["reuse"]:
        if entry["provenance"].startswith("reused-from:"):
            assert entry["why"] and entry["reason"], entry
    assert row["stale_evidence_reused"] == 0
    assert row["reviewer_dispatches"] >= 1


@pytest.mark.parametrize("case,variant", all_variants())
@pytest.mark.parametrize("rule", RULES)
def test_derived_counts_recomputed_independently(traces, case, variant, rule):
    """repeated_reads, unchanged_content_reviewed, unchanged_lines_reviewed,
    tests_rerun, tests_reused and defects_found are recomputed here from the
    trace's raw material (stored snapshots, presented entries, review texts,
    seeded defects) with code independent of the replay counters."""
    row = _row(traces, case, variant, rule)
    repo = Path(row["repo"])
    commits = {s["n"]: s["commit"] for s in row["snapshots"]}
    repeated = 0
    unchanged = 0
    lines = 0
    prev: set = set()
    for rnd in row["rounds"]:
        presented = set(rnd["presented"])
        repeated += len(presented & prev)
        prev = presented
        if rnd["pre_snapshot"] is None:
            continue
        pre = commits[rnd["pre_snapshot"]]
        post = commits[rnd["post_snapshot"]]
        for path in rnd["scope"]:
            before = _git(repo, "rev-parse", "--verify", "-q", f"{pre}:{path}", check=False).stdout.strip()
            after = _git(repo, "rev-parse", "--verify", "-q", f"{post}:{path}", check=False).stdout.strip()
            if before and after and before == after:
                unchanged += 1
                lines += _git(repo, "cat-file", "-p", after).stdout.count("\n")
    assert row["repeated_reads"] == repeated
    assert row["unchanged_content_reviewed"] == unchanged
    assert row["unchanged_lines_reviewed"] == lines
    assert row["tests_rerun"] == sum(1 for e in row["tests_events"] if e["provenance"] == "fresh")
    assert row["tests_reused"] == sum(1 for e in row["tests_events"] if e["provenance"] != "fresh")
    # defects_found: independent anchor parse over the recorded texts and each round's presented scope
    seeded = load_case(FIXTURES / case)["seeded_defects"]
    found = set()
    for rnd in row["rounds"]:
        for line_block in re.split(r"\n(?=- \[)", rnd["review_text"]):
            m = re.search(r"^- \[(CRITICAL|MINOR)\]", line_block)
            if not m:
                continue
            am = re.search(r"File:\s*`?([^`\s,]+)`?.*?lines?\s+(\d+)(?:\s*-\s*(\d+))?", line_block, re.IGNORECASE | re.DOTALL)
            if not am or am.group(1) not in rnd["scope"]:
                continue
            a, b = int(am.group(2)), int(am.group(3) or am.group(2))
            for idx, d in enumerate(seeded):
                if d["file"] == am.group(1) and a <= d["line_end"] and b >= d["line_start"]:
                    found.add(idx)
    assert row["defects_found"] == len(found)
    assert row["defects_found"] + row["defects_missed"] == len(seeded)


@pytest.mark.parametrize("case,variant", all_variants())
def test_no_invalid_record_reported_as_pass(traces, case, variant):
    for rule in RULES:
        row = _row(traces, case, variant, rule)
        for entry in row["reuse"]:
            if entry["provenance"].startswith("reused-from:"):
                assert entry["result"] == "PASS" and entry["valid"] is not False, entry
        # active records that are invalid never count toward a stage
        for rec in row["ledger_records"]:
            if rec.get("result") == "FAIL" and rec.get("superseded_by") is None:
                assert "exec" not in row["completed_stages"] or rec["check"] not in ("reviewer_approve", "gate")


@pytest.mark.parametrize("case,variant", all_variants())
def test_defect_and_complexity_properties(traces, case, variant):
    old = _row(traces, case, variant, "old")
    new = _row(traces, case, variant, "new")
    assert new["defects_missed"] <= old["defects_missed"]
    assert new["theoretical_complexity_added"] == 0
    if case == "case-8":
        assert new["defects_missed"] == old["defects_missed"]
    if case == "case-6" and variant == "main":
        assert old["theoretical_complexity_added"] >= 1
    if case == "case-7":
        assert old["defects_found"] >= 1 and new["defects_found"] >= 1


@pytest.mark.parametrize("case", ["case-2", "case-4", "case-5"])
def test_routine_cases_reduce_overhead(traces, case):
    old = _row(traces, case, "main", "old")
    new = _row(traces, case, "main", "new")
    fields = ("executor_dispatches", "repeated_reads", "unchanged_content_reviewed", "tests_rerun", "pauses_without_decision")
    for field in fields:
        assert new[field] <= old[field], (field, old[field], new[field])
    assert any(new[field] < old[field] for field in fields), {f: (old[f], new[f]) for f in fields}


def test_case_8_keeps_independent_review_gate_and_fresh_tests(traces):
    for rule in RULES:
        row = _row(traces, "case-8", "main", rule)
        assert row["reviewer_dispatches"] >= 1
        assert row["gate_runs"] == 1
        assert row["tests_rerun"] >= 1


@pytest.mark.parametrize("case", ["case-5", "case-8"])
def test_new_file_under_declared_selector_invalidates_tests_claim(traces, case):
    new = _row(traces, case, "main", "new")
    events = new["tests_events"]
    assert any(e["provenance"] == "fresh" and e["inputs_changed"] and "selector digest changed" in e["helper_reason"]
               for e in events[1:]), events


def test_case_6_dispute_then_concur_without_implementation(traces):
    new = _row(traces, "case-6", "main", "new")
    old = _row(traces, "case-6", "main", "old")
    assert new["blocking_rejected_or_downgraded"] == 1
    assert new["fates"]["reviews/round-1.md#1"] == "concurred"
    # zero Executor dispatches for the finding: the only Executor dispatch is the initial implementation
    assert new["executor_dispatches"] == 1 and old["executor_dispatches"] == 2
    assert not any(r["executor_dispatched"] for r in new["rounds"][1:])
    assert new["implemented_findings"] == []
    assert new["user_pauses"] == []
    assert "exec" in new["completed_stages"]
    # reviewer_approve recorded fresh by the second (concurring) round
    after = new["records_after_dispute"]
    assert after and all(r["id"] > r["watermark"] and r["provenance"] == "fresh" for r in after)
    assert old["theoretical_complexity_added"] == 1 and new["theoretical_complexity_added"] == 0


def test_case_6b_reasserted_stays_blocking_with_risk_pause_only(traces):
    new = _row(traces, "case-6", "6b", "new")
    old = _row(traces, "case-6", "6b", "old")
    assert new["fates"]["reviews/round-1.md#1"] == "re-asserted"
    assert "exec" not in new["completed_stages"]
    assert new["implemented_findings"] == [] and new["executor_dispatches"] == 1
    assert [p["kind"] for p in new["user_pauses"]] == ["risk"]
    assert new["pauses_without_decision"] == 0
    assert old["theoretical_complexity_added"] == 0 and new["theoretical_complexity_added"] == 0
    assert old["pauses_without_decision"] >= 1  # old rule: stuck after implementing the defense


def test_case_6c_incomplete_critical_is_discarded_never_implemented(traces):
    new = _row(traces, "case-6", "6c", "new")
    old = _row(traces, "case-6", "6c", "old")
    assert new["fates"]["reviews/round-1-incomplete.md#1"] == "failed-check"
    assert new["pending_incomplete"] and "missing Reachability, Fix cost" in new["pending_incomplete"][0]["summary"][0]
    assert new["implemented_findings"] == [] and new["executor_dispatches"] == 1
    assert new["reviewer_dispatches"] == 2  # retry table: one re-dispatch
    assert "exec" in new["completed_stages"]
    assert old["theoretical_complexity_added"] == 1 and old["executor_dispatches"] == 2


def test_case_6c_gate_executorless_revalidation_mints_exec(traces):
    new = _row(traces, "case-6", "6c-gate", "new")
    old = _row(traces, "case-6", "6c-gate", "old")
    assert new["gate_runs"] == 1 and old["gate_runs"] == 1
    assert new["revalidation_rounds"] and new["revalidation_rounds"][0]["outcomes"] == ["concurred"]
    reval_round = [r for r in new["rounds"] if r["revalidation_round"]]
    assert len(reval_round) == 1 and not reval_round[0]["executor_dispatched"]
    assert new["executor_dispatches"] == 1  # only the initial implementation
    assert new["fates"]["gate/incomplete.md#1"] == "dropped"
    assert "exec" in new["completed_stages"]
    assert old["theoretical_complexity_added"] == 1 and new["theoretical_complexity_added"] == 0


def test_case_6c_gate_reassert_requires_executor_repair_before_exec(traces):
    new = _row(traces, "case-6", "6c-gate-reassert", "new")
    assert new["gate_runs"] == 1
    assert new["revalidation_rounds"][0]["outcomes"] == ["re-asserted"]
    assert new["executor_dispatches"] == 2  # initial + repair after re-assertion
    assert "exec" in new["completed_stages"]
    assert new["theoretical_complexity_added"] == 0


def test_case_7_dispute_without_concurrence_stays_blocking(traces):
    new = _row(traces, "case-7", "main", "new")
    old = _row(traces, "case-7", "main", "old")
    assert new["fates"]["reviews/round-1.md#1"] == "re-asserted"
    assert new["blocking_rejected_or_downgraded"] == 0
    # the repair Executor round happened after the re-assertion and before reviewer_approve PASS
    approve_ids = [r["id"] for r in new["ledger_records"] if r["check"] == "reviewer_approve" and r.get("result") == "PASS"]
    fail_ids = [r["id"] for r in new["ledger_records"] if r["check"] == "reviewer_approve" and r.get("result") == "FAIL"]
    assert approve_ids and fail_ids and min(approve_ids) > max(fail_ids)
    assert new["executor_dispatches"] == 2
    assert "exec" in new["completed_stages"]
    assert new["defects_found"] == 1 and old["defects_found"] == 1


def test_route_decisions_come_from_the_helper(traces):
    case2 = _row(traces, "case-2", "main", "new")
    assert [d["route"] for d in case2["route_decisions"]] == ["orchestrator-direct", "orchestrator-direct"]
    case7 = _row(traces, "case-7", "main", "new")
    assert all(d["route"] == "executor" for d in case7["route_decisions"])
    assert any("destructive_operation" in r for d in case7["route_decisions"] for r in d["reasons"])


# --------------------------------------------------------------------- report


def _smoke_row(case: str) -> dict:
    case_id = SMOKE_CASES[case]
    artifact_dir = ARTIFACTS / case_id
    row = {"case": case, "variant": "smoke", "rule": "live-opus", "status": "unverified"}
    for field in MEASUREMENT_FIELDS:
        row.setdefault(field, NA)
    meta_path = artifact_dir / "meta.json"
    result_path = artifact_dir / "reviewer-result.json"
    measurement_path = artifact_dir / "reviewer-measurement.json"
    if not meta_path.exists():
        return row
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    row["status"] = "PASS" if meta.get("status") == "pass" else "unverified"
    row["smoke_reason"] = meta.get("reason")
    if meta.get("status") != "pass":
        return row  # a timeout / FAIL / skip keeps every field N/A and the claim unverified
    row["reviewer_dispatches"] = 1
    try:
        started = ledger_mod.parse_iso(meta["started_at"])
        finished = ledger_mod.parse_iso(meta["finished_at"])
        row["elapsed_s"] = int((finished - started).total_seconds())
    except (KeyError, ValueError) as exc:
        # a PASS whose timing cannot be read is not a verified measurement;
        # say why instead of printing N/A next to a PASS
        row["status"] = "unverified"
        row["smoke_reason"] = f"meta.json started_at/finished_at unparseable: {exc!r}"
        return row
    if measurement_path.exists():
        m = json.loads(measurement_path.read_text(encoding="utf-8"))
        for field in ("model", "tokens_in", "tokens_out", "cost_usd"):
            row[field] = m.get(field, NA)
        if m.get("duration_ms") != NA:
            row["duration_ms"] = m["duration_ms"]
    if result_path.exists():
        envelope = json.loads(result_path.read_text(encoding="utf-8"))
        text = envelope.get("result") if isinstance(envelope, dict) else None
        if isinstance(text, str):
            seeded = load_case(FIXTURES / case)["seeded_defects"]
            scope = {d["file"] for d in seeded} | {"pkg/store.py", "tests/test_store.py", "pkg/retention.py", "pkg/cli.py"}
            found = set()
            # a PASS row whose reviewer text does not parse is a harness
            # contradiction: let the UsageError fail the report test
            parsed = triage_mod.parse_review(text)
            for finding in parsed["findings"]:
                anchor = finding_anchor(finding["text"])
                if anchor and anchor[0] in scope:
                    for idx, d in enumerate(seeded):
                        if d["file"] == anchor[0] and anchor[1] <= d["line_end"] and anchor[2] >= d["line_start"]:
                            found.add(idx)
            row["defects_found"] = len(found)
            row["defects_missed"] = len(seeded) - len(found)
    return row


def _fmt(value) -> str:
    if isinstance(value, list):
        return str(len(value)) if value and isinstance(value[0], dict) else (", ".join(map(str, value)) or "0")
    return str(value)


def test_write_evaluation_report(traces):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    lines = ["# Evaluation cases — old rule vs new rule", "",
             "Deterministic replay driven by `scripts/evidence_ledger.py` and `scripts/finding_triage.py`;",
             "fixtures are inputs only. `N/A` = not measurable in the deterministic harness.", ""]
    for (case, variant), pair in sorted(traces.items()):
        title = load_case(FIXTURES / case)["title"] if variant == "main" else load_case(FIXTURES / case)["variants"][variant]["title"]
        lines += [f"## {case} ({variant}) — {title}", "", "| field | old | new |", "|---|---|---|"]
        for field in MEASUREMENT_FIELDS:
            lines.append(f"| `{field}` | {_fmt(pair['old'][field])} | {_fmt(pair['new'][field])} |")
        lines.append("")
    lines += ["## Live Opus Reviewer smoke (cases 6 / 7)", "",
              "| case | status | elapsed_s | model | tokens_in | tokens_out | cost_usd | defects_found | defects_missed | theoretical_complexity_added |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for case in ("case-6", "case-7"):
        row = _smoke_row(case)
        lines.append(f"| {case} ({SMOKE_CASES[case]}) | {row['status']} | {row['elapsed_s']} | {row['model']} | {row['tokens_in']} | "
                     f"{row['tokens_out']} | {row['cost_usd']} | {row['defects_found']} | {row['defects_missed']} | N/A |")
        if row["status"] != "PASS":
            lines.append(f"|  | `unverified` — {row.get('smoke_reason') or 'no live result artifact'} |  |  |  |  |  |  |  |  |")
    lines.append("")
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert REPORT.exists()
