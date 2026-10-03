#!/usr/bin/env python3
"""Read-only final delivery gate over W01 scope, W02 scan and evidence ledger."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

try:
    import delivery_scope as ds
    import evidence_ledger as el
    import security_preflight as sp
    import delivery_actions as da
except ModuleNotFoundError:  # imported as scripts.delivery_gate
    from scripts import delivery_scope as ds
    from scripts import evidence_ledger as el
    from scripts import security_preflight as sp
    from scripts import delivery_actions as da

REQUIRED_STAGES = ("exec", "polish", "docs", "security")
BLOCKED_RE = re.compile(r"^- delivery_blocked_by:\s*(.*?)\s*$", re.MULTILINE)
CANDIDATE_ROW_RE = re.compile(
    r"^\| Delivery candidate \| baseline=`([^`]+)`; manifest=`([^`]+)`; "
    r"baseline_fingerprint=`(sha256:[0-9a-f]{64})`; "
    r"candidate_fingerprint=`(sha256:[0-9a-f]{64})`; scope=(.*?) \|$"
)


def _delivery_blocked_by_text(text: str) -> str:
    span = el.find_canonical_section(text, el.METADATA_HEADING)
    if span is None:
        raise ValueError("session file lacks its canonical final Session Metadata section")
    matches = BLOCKED_RE.findall(text[span[0]:span[1]])
    if len(matches) != 1:
        raise ValueError("delivery_blocked_by must appear exactly once in Session Metadata")
    return matches[0]


def _evidence_report(repo: str, session_text: str) -> dict:
    # Give the checker the same immutable byte snapshot used by packet and
    # metadata parsing; it must not independently reopen a mutable session.
    temp_parent = os.path.realpath(tempfile.gettempdir())
    repository_info = ds.repository(repo)
    common_git_dir = ds.git_line(repo, "rev-parse", "--git-common-dir")
    common_git_dir = os.path.realpath(os.path.join(repo, common_git_dir))
    protected_roots = (repo, repository_info["git_dir"], common_git_dir)
    for protected in protected_roots:
        try:
            inside = os.path.commonpath((protected, temp_parent)) == protected
        except ValueError:
            inside = False
        if inside:
            raise ValueError("system temporary directory resolves inside the repository or Git metadata")
    with tempfile.TemporaryDirectory(prefix="review-loop-delivery-gate-", dir=temp_parent) as temp_dir:
        snapshot = Path(temp_dir) / "session.md"
        snapshot.write_text(session_text, encoding="utf-8")
        argv = [sys.executable, "-I", str(Path(el.__file__).resolve()), "--repo", repo,
                "--session-file", str(snapshot), "check", "--no-write", "--format", "json"]
        result = subprocess.run(argv, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise ValueError("evidence ledger check failed: " + result.stderr.decode("utf-8", "replace").strip())
    try:
        report = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("evidence ledger returned malformed JSON") from exc
    if not isinstance(report, dict) or not isinstance(report.get("completed_stages"), list):
        raise ValueError("evidence ledger report lacks completed_stages")
    return report


def _candidate_binding(session_text: str, repo: str, manifest_path: str, manifest: dict) -> None:
    """Require W05 to consume the exact candidate recorded in the canonical packet."""
    spans = el.canonical_section_candidates(session_text, el.PACKET_HEADING)
    if len(spans) != 1:
        raise ValueError("session must contain exactly one canonical Current Review Packet")
    packet = session_text[spans[0][0]:spans[0][1]]
    rows = [line for line in packet.splitlines() if line.startswith("| Delivery candidate |")]
    if len(rows) != 1:
        raise ValueError("Current Review Packet must bind exactly one Delivery candidate")
    match = CANDIDATE_ROW_RE.fullmatch(rows[0])
    if not match:
        raise ValueError("Current Review Packet Delivery candidate row is malformed")
    recorded_baseline, recorded_manifest, baseline_fingerprint, candidate_fingerprint, _scope = match.groups()
    baseline_artifact_path = os.path.abspath(os.path.join(repo, recorded_baseline))
    baseline_artifact = ds.load_document(baseline_artifact_path, "delivery-baseline")
    ds.match_repository(ds.repository(repo), baseline_artifact)
    canonical_recorded = os.path.normpath(recorded_manifest) == recorded_manifest
    expected_path = os.path.normcase(os.path.abspath(os.path.join(repo, manifest_path)))
    recorded_path = os.path.normcase(os.path.abspath(os.path.join(repo, recorded_manifest)))
    if (not canonical_recorded or recorded_path != expected_path
            or baseline_artifact["fingerprint"] != manifest["baseline_fingerprint"]
            or candidate_fingerprint != manifest["fingerprint"]
            or baseline_fingerprint != manifest["baseline_fingerprint"]):
        raise ValueError("delivery manifest does not match the session's recorded candidate and baseline")


def evaluate_gate(repo_path: str, manifest_path: str, session_file: str) -> dict:
    repo = ds.repository(repo_path)
    manifest = ds.load_document(manifest_path, "delivery-manifest")
    ds.match_repository(repo, manifest["baseline"])
    reasons = []
    session_text = Path(session_file).read_text(encoding="utf-8")
    try:
        _candidate_binding(session_text, repo["worktree"], manifest_path, manifest)
    except (ds.UsageError, OSError, UnicodeError, ValueError) as exc:
        reasons.append("candidate_binding:" + str(exc))
    before = ds.capture_state(repo)
    if before != manifest["current"]:
        reasons.append("stale_delivery_manifest")
    plan = da.compute_plan(repo["worktree"], manifest=manifest)
    reasons.extend("delivery_action:" + item for item in plan["ownership_reasons"])
    try:
        auto_commit = da._auto_commit_enabled(repo["worktree"])
    except (da.ActionError, OSError, UnicodeError) as exc:
        auto_commit = True  # an unreadable/ambiguous setting must not relax the gate
        reasons.append("auto_commit_config:" + str(exc))
    if auto_commit:
        reasons.extend("delivery_action:" + item for item in plan["commit_reasons"])
    security = sp.scan(repo["worktree"], manifest=manifest)
    if (security["status"] != "clean" or security["coverage_complete"] is not True
            or security.get("ignore_coverage_complete") is not True):
        reasons.append("security_preflight_" + security["status"])

    evidence = _evidence_report(repo["worktree"], session_text)
    stages = set(evidence["completed_stages"])
    missing = [stage for stage in REQUIRED_STAGES if stage not in stages]
    if missing:
        reasons.append("missing_completed_stages:" + ",".join(missing))
    security_claim = evidence.get("claims", {}).get("security_scan:session")
    if (not isinstance(security_claim, dict) or security_claim.get("valid") is not True
            or security_claim.get("result") != "PASS"):
        reasons.append("missing_valid_security_scan_evidence")
    try:
        blocked = _delivery_blocked_by_text(session_text)
    except (OSError, UnicodeError, ValueError) as exc:
        blocked = "invalid"
        reasons.append("invalid_delivery_blocked_by:" + str(exc))
    if blocked != "null":
        reasons.append("delivery_blocked_by:" + blocked)

    after = ds.capture_state(repo)
    if after != manifest["current"]:
        reasons.append("repository_changed_during_gate")
    report = ds.seal({
        "schema": 1, "kind": "delivery-gate", "created_at": ds.created_at(),
        "manifest_fingerprint": manifest["fingerprint"],
        "security_ruleset": sp.RULES_VERSION,
        "security_fingerprint": security.get("fingerprint"),
        "completed_stages": sorted(stages), "required_stages": list(REQUIRED_STAGES),
        "delivery_blocked_by": blocked, "auto_commit": auto_commit, "auto_commit_paths": plan["paths"],
        "eligible": not reasons, "reasons": sorted(set(reasons)),
    })
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--session-file", required=True)
    parser.add_argument("--output", help="optional immutable JSON gate report")
    args = parser.parse_args(argv)
    try:
        repo = ds.repository(args.repo)
        ds.validate_output(repo, args.output)
        report = evaluate_gate(repo["worktree"], args.manifest, args.session_file)
        ds.emit(report, args.output)
        return 0 if report["eligible"] else 1
    except ds.UsageError as exc:
        sys.stderr.write(json.dumps({"error": str(exc), "kind": "usage"}) + "\n")
        return 2
    except (ds.CaptureError, sp.ScanError, OSError, ValueError) as exc:
        sys.stderr.write(json.dumps({"error": str(exc), "kind": "delivery-gate"}) + "\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
