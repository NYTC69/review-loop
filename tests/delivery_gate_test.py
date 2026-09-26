"""W05 final gate tests against disposable repository/session fixtures."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts import delivery_gate as gate
from scripts import delivery_scope as ds


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT)


def fixture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / ".gitignore").write_text(""".review-loop/
.env
.env.*
*.env
!.env.example
!.env.sample
*.pem
*.key
*.crt
*.cert
*.cer
*.p12
*.pfx
*.jks
*.keystore
*.ppk
id_rsa*
id_dsa*
id_ecdsa*
id_ed25519*
*.asc
*.gpg
*.pgp
*credentials*
!*credentials.example*
!*credentials.sample*
service-account*.json
.aws/
.gcloud/
*secret*
!*secret.example*
!*secret.sample*
secrets.*
!secrets.example*
!secrets.sample*
*.sqlite
*.sqlite3
*.db
*.dump
*.sql.gz
*.map
*.tfstate
*.tfstate.*
*.tfvars
!*.tfvars.example
.terraform/
*.log
logs/
""")
    (repo / "src.py").write_text("print('safe')\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    info = ds.repository(str(repo))
    baseline = ds.build_baseline(info, ["src.py"], ds.capture_state(info))
    manifest = tmp_path / "manifest.json"
    candidate = ds.build_manifest(baseline, ds.capture_state(info))
    manifest.write_text(json.dumps(candidate))
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline))
    session = tmp_path / "session.md"
    session.write_text(
        "## Current Review Packet\n"
        f"| Delivery candidate | baseline=`{baseline_path}`; manifest=`{manifest}`; "
        f"baseline_fingerprint=`{baseline['fingerprint']}`; "
        f"candidate_fingerprint=`{candidate['fingerprint']}`; scope=`src.py` |\n\n"
        "## Review History\n\n"
        "## Evidence Ledger\n```json\n{\"schema\":1,\"scope\":[],\"snapshots\":[],\"records\":[]}\n```\n\n"
        "## Session Metadata\n- delivery_blocked_by: null\n"
    )
    return repo, manifest, session


def evidence_report():
    return {"completed_stages": ["exec", "polish", "docs", "security"],
            "claims": {"security_scan:session": {"valid": True, "result": "PASS"}}}


def test_gate_requires_fresh_candidate_clean_scan_all_stages_and_unblocked_session(tmp_path, monkeypatch):
    repo, manifest, session = fixture(tmp_path)
    monkeypatch.setattr(gate, "_evidence_report", lambda *_: evidence_report())
    result = gate.evaluate_gate(str(repo), str(manifest), str(session))
    assert result["eligible"] is True, result["reasons"]
    assert result["reasons"] == []
    assert result["auto_commit_paths"] == []


def test_gate_blocks_missing_stage_invalid_security_or_blocked_session(tmp_path, monkeypatch):
    repo, manifest, session = fixture(tmp_path)
    session.write_text(session.read_text().replace("null", "security"))
    evidence = evidence_report()
    evidence["completed_stages"].remove("security")
    evidence["claims"]["security_scan:session"]["valid"] = False
    monkeypatch.setattr(gate, "_evidence_report", lambda *_: evidence)
    result = gate.evaluate_gate(str(repo), str(manifest), str(session))
    assert result["eligible"] is False
    assert any("missing_completed_stages" in reason for reason in result["reasons"])
    assert "missing_valid_security_scan_evidence" in result["reasons"]
    assert "delivery_blocked_by:security" in result["reasons"]


def test_gate_blocks_stale_manifest_and_security_findings(tmp_path, monkeypatch):
    repo, manifest, session = fixture(tmp_path)
    evidence = evidence_report()
    monkeypatch.setattr(gate, "_evidence_report", lambda *_: evidence)
    (repo / "src.py").write_text("not in the candidate\n")
    result = gate.evaluate_gate(str(repo), str(manifest), str(session))
    assert result["eligible"] is False
    assert any("stale" in reason for reason in result["reasons"])


def test_evidence_check_is_read_only_and_missing_stages_are_not_invented(tmp_path):
    repo, _manifest, session = fixture(tmp_path)
    before = session.read_bytes()
    report = gate._evidence_report(str(repo), session.read_text())
    assert report["completed_stages"] == []
    assert session.read_bytes() == before


def test_evidence_snapshot_refuses_temp_directory_inside_repository(tmp_path, monkeypatch):
    repo, _manifest, session = fixture(tmp_path)
    in_repo_tmp = repo / "temporary"
    in_repo_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(in_repo_tmp))
    monkeypatch.setattr(gate.tempfile, "tempdir", None)
    with pytest.raises(ValueError, match="inside the repository"):
        gate._evidence_report(str(repo), session.read_text())
    assert list(in_repo_tmp.iterdir()) == []


def test_gate_rejects_manifest_not_bound_to_packet(tmp_path, monkeypatch):
    repo, manifest, session = fixture(tmp_path)
    monkeypatch.setattr(gate, "_evidence_report", lambda *_: evidence_report())
    session.write_text(session.read_text().replace(f"`{manifest}`", f"`{tmp_path / 'other.json'}`"))
    result = gate.evaluate_gate(str(repo), str(manifest), str(session))
    assert result["eligible"] is False
    assert any(reason.startswith("candidate_binding:") for reason in result["reasons"])


def test_gate_requires_exact_candidate_path_token(tmp_path, monkeypatch):
    repo, manifest, session = fixture(tmp_path)
    monkeypatch.setattr(gate, "_evidence_report", lambda *_: evidence_report())
    session.write_text(session.read_text().replace(f"manifest=`{manifest}`", f"manifest=`{manifest}-suffix`"))
    result = gate.evaluate_gate(str(repo), str(manifest), str(session))
    assert result["eligible"] is False
    assert any(reason.startswith("candidate_binding:") for reason in result["reasons"])


def test_gate_rejects_noncanonical_candidate_path_alias(tmp_path, monkeypatch):
    repo, manifest, session = fixture(tmp_path)
    monkeypatch.setattr(gate, "_evidence_report", lambda *_: evidence_report())
    alias = manifest.parent / "unused" / ".." / manifest.name
    session.write_text(session.read_text().replace(f"manifest=`{manifest}`", f"manifest=`{alias}`"))
    result = gate.evaluate_gate(str(repo), str(manifest), str(session))
    assert result["eligible"] is False
    assert any(reason.startswith("candidate_binding:") for reason in result["reasons"])


def test_gate_rejects_candidate_not_using_the_recorded_immutable_baseline(tmp_path, monkeypatch):
    repo, manifest, session = fixture(tmp_path)
    monkeypatch.setattr(gate, "_evidence_report", lambda *_: evidence_report())
    baseline_path = tmp_path / "baseline.json"
    baseline = json.loads(baseline_path.read_text())
    baseline["scope"] = ["."]
    baseline_path.write_text(json.dumps(baseline))
    result = gate.evaluate_gate(str(repo), str(manifest), str(session))
    assert result["eligible"] is False
    assert any(reason.startswith("candidate_binding:") for reason in result["reasons"])


def test_gate_consumers_share_one_manifest_if_artifact_path_changes_mid_gate(tmp_path, monkeypatch):
    repo, manifest_path, session = fixture(tmp_path)
    candidate_path = manifest_path
    original = json.loads(manifest_path.read_text())
    info = ds.repository(str(repo))
    replacement_baseline = ds.build_baseline(info, ["."], ds.capture_state(info))
    replacement = ds.build_manifest(replacement_baseline, ds.capture_state(info))
    assert replacement["fingerprint"] != original["fingerprint"]
    real_plan = gate.da.compute_plan
    real_scan = gate.sp.scan

    def swap_then_plan(repo_path, manifest_path_arg=None, *, manifest=None):
        assert manifest["fingerprint"] == original["fingerprint"]
        candidate_path.write_text(json.dumps(replacement))
        return real_plan(repo_path, manifest=manifest)

    def same_snapshot_scan(repo_path, manifest_path_arg=None, *, manifest=None):
        assert manifest["fingerprint"] == original["fingerprint"]
        return real_scan(repo_path, manifest=manifest)

    monkeypatch.setattr(gate.da, "compute_plan", swap_then_plan)
    monkeypatch.setattr(gate.sp, "scan", same_snapshot_scan)
    monkeypatch.setattr(gate, "_evidence_report", lambda *_: evidence_report())
    result = gate.evaluate_gate(str(repo), str(manifest_path), str(session))
    assert result["manifest_fingerprint"] == original["fingerprint"]
    assert result["eligible"] is True
    assert json.loads(manifest_path.read_text())["fingerprint"] == replacement["fingerprint"]


def test_evidence_checker_nonzero_exit_is_rejected(tmp_path, monkeypatch):
    repo, _manifest, session = fixture(tmp_path)
    class Result:
        returncode = 1
        stderr = b"invalid evidence"
        stdout = b'{"completed_stages":[]}'
    real_run = gate.subprocess.run
    def fake_run(argv, *args, **kwargs):
        if "-I" in argv:
            return Result()
        return real_run(argv, *args, **kwargs)
    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="evidence ledger check failed"):
        gate._evidence_report(str(repo), session.read_text())


def _session_for(tmp_path, repo, scope, mutate):
    info = ds.repository(str(repo))
    baseline = ds.build_baseline(info, ds.scope_paths(info["worktree"], scope), ds.capture_state(info))
    mutate()
    candidate = ds.build_manifest(baseline, ds.capture_state(info))
    manifest = tmp_path / "manifest-2.json"
    manifest.write_text(json.dumps(candidate))
    baseline_path = tmp_path / "baseline-2.json"
    baseline_path.write_text(json.dumps(baseline))
    session = tmp_path / "session-2.md"
    session.write_text(
        "## Current Review Packet\n"
        f"| Delivery candidate | baseline=`{baseline_path}`; manifest=`{manifest}`; "
        f"baseline_fingerprint=`{baseline['fingerprint']}`; "
        f"candidate_fingerprint=`{candidate['fingerprint']}`; scope=`src.py` |\n\n"
        "## Review History\n\n"
        "## Evidence Ledger\n```json\n{\"schema\":1,\"scope\":[],\"snapshots\":[],\"records\":[]}\n```\n\n"
        "## Session Metadata\n- delivery_blocked_by: null\n"
    )
    return manifest, session


def test_commit_only_refusals_apply_to_the_gate_only_when_auto_commit_is_enabled(tmp_path, monkeypatch):
    repo, _manifest, _session = fixture(tmp_path)
    monkeypatch.setattr(gate, "_evidence_report", lambda *_: evidence_report())
    git(repo, "config", "core.autocrlf", "true")
    manifest, session = _session_for(
        tmp_path, repo, ["src.py"], lambda: (repo / "src.py").write_text("print('task')\n"))
    result = gate.evaluate_gate(str(repo), str(manifest), str(session))
    assert result["eligible"] is True, result["reasons"]
    assert result["auto_commit"] is False
    assert result["auto_commit_paths"] == ["src.py"]

    (repo / ".review-loop").mkdir()
    (repo / ".review-loop/config.md").write_text("auto_commit: true\n")
    enabled = gate.evaluate_gate(str(repo), str(manifest), str(session))
    assert enabled["eligible"] is False
    assert enabled["auto_commit"] is True
    assert any("attributes" in reason for reason in enabled["reasons"])
