"""Manifest-bound read-only security coverage tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import delivery_scope as ds
from scripts import security_preflight as sp


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT)


def candidate(repo, tmp_path):
    info = ds.repository(str(repo))
    baseline = ds.build_baseline(info, ["."], ds.capture_state(info))
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(ds.build_manifest(baseline, ds.capture_state(info))))
    return path


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitignore").write_text(""".review-loop/
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
!untracked.env
!credentials.json
""")
    (root / "app.py").write_text("print('ok')\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root


def test_scans_manifest_bound_tracked_and_untracked_files_without_secret_values(repo, tmp_path):
    (repo / "untracked.env").write_text("API_TOKEN=ghp_" + "A" * 36 + "\n")
    (repo / "credentials.json").write_text("{}\n")
    manifest = candidate(repo, tmp_path)
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "blocked"
    assert result["coverage_complete"] is True
    assert result["ignore_coverage_complete"] is True
    assert {row["path"] for row in result["findings"]} >= {"untracked.env", "credentials.json"}
    assert {row["rule"] for row in result["findings"]} >= {"github-token", "credential-name", "environment-file"}
    assert "A" * 20 not in json.dumps(result)


def test_scans_staged_content_and_detects_private_key_block(repo, tmp_path):
    marker = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
    (repo / "src.py").write_text("safe = True\n# " + marker + "\n")
    git(repo, "add", "src.py")
    manifest = candidate(repo, tmp_path)
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "blocked"
    assert any(row["rule"] == "private-key-block" and row["path"] == "src.py" and row["line"] == 2
               for row in result["findings"])


def test_scans_staged_blob_even_when_worktree_is_safe(repo, tmp_path):
    (repo / "app.py").write_text("# -----BEGIN " + "ENCRYPTED PRIVATE KEY-----\n")
    git(repo, "add", "app.py")
    (repo / "app.py").write_text("safe current worktree\n")
    manifest = candidate(repo, tmp_path)
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "blocked"
    assert any(row["rule"] == "private-key-block" and row.get("source") == "index"
               for row in result["findings"])


def test_scans_index_only_sensitive_path_after_worktree_deletion(repo, tmp_path):
    (repo / ".env").write_text("ordinary placeholder\n")
    git(repo, "add", "-f", ".env")
    (repo / ".env").unlink()
    manifest = candidate(repo, tmp_path)
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "blocked"
    assert any(row["path"] == ".env" and row["rule"] == "environment-file"
               and row.get("source") == "index" for row in result["findings"])


def test_staged_blob_scan_ignores_git_replace_refs(repo, tmp_path):
    (repo / "app.py").write_text("# -----BEGIN " + "PRIVATE KEY-----\n")
    git(repo, "add", "app.py")
    (repo / "app.py").write_text("safe worktree\n")
    oid = git(repo, "rev-parse", ":app.py").decode().strip()
    safe_oid = subprocess.check_output(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                                      input=b"safe replacement\n").decode().strip()
    git(repo, "replace", oid, safe_oid)
    manifest = candidate(repo, tmp_path)
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "blocked"
    assert any(row["source"] == "index" and row["rule"] == "private-key-block"
               for row in result["findings"])


def test_stale_manifest_fails_before_scan(repo, tmp_path):
    manifest = candidate(repo, tmp_path)
    (repo / "app.py").write_text("changed after manifest\n")
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "stale"
    assert result["coverage_complete"] is False


def test_symlink_target_is_not_followed(repo, tmp_path):
    secret = tmp_path / "outside.pem"
    secret.write_text("-----BEGIN " + "PRIVATE KEY-----\n")
    (repo / "link.txt").symlink_to(secret)
    manifest = candidate(repo, tmp_path)
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "clean"
    assert not any(row["path"] == "link.txt" for row in result["findings"])


def test_content_scan_limit_fails_closed(repo, tmp_path, monkeypatch):
    (repo / "large.txt").write_bytes(b"x" * 32)
    monkeypatch.setattr(sp, "MAX_FILE_BYTES", 16)
    manifest = candidate(repo, tmp_path)
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "incomplete"
    assert result["coverage_complete"] is False
    assert "limit" in result["reason"]


def test_staged_blob_limit_is_checked_before_reading_full_object(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "MAX_FILE_BYTES", 16)
    (repo / "app.py").write_bytes(b"x" * 100_000)
    git(repo, "add", "app.py")
    (repo / "app.py").write_text("safe\n")
    manifest = candidate(repo, tmp_path)
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "incomplete"
    assert "limit" in result["reason"]


def test_incomplete_repo_gitignore_coverage_requires_review(repo, tmp_path):
    (repo / ".gitignore").write_text(".review-loop/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "remove sensitive ignore coverage")
    manifest = candidate(repo, tmp_path)
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "review-required"
    assert result["coverage_complete"] is True
    assert result["ignore_coverage_complete"] is False
    assert any(not row["covered"] for row in result["ignore_coverage"])


def test_staged_gitignore_change_does_not_count_as_head_coverage(repo, tmp_path):
    original = (repo / ".gitignore").read_text()
    (repo / ".gitignore").write_text(".review-loop/\n")
    git(repo, "add", ".gitignore")
    (repo / ".gitignore").write_text(original)
    manifest = candidate(repo, tmp_path)
    result = sp.scan(str(repo), str(manifest))
    assert result["status"] == "review-required"
    assert result["ignore_coverage_complete"] is False


def test_declared_task_gitignore_edit_counts_as_coverage(repo, tmp_path):
    original = (repo / ".gitignore").read_text()
    (repo / ".gitignore").write_text(".review-loop/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "remove sensitive ignore coverage")
    info = ds.repository(str(repo))
    baseline = ds.build_baseline(info, [".gitignore"], ds.capture_state(info))
    (repo / ".gitignore").write_text(original)
    manifest = tmp_path / "owned-gitignore.json"
    manifest.write_text(json.dumps(ds.build_manifest(baseline, ds.capture_state(info))))
    result = sp.scan(str(repo), str(manifest))
    assert result["ignore_coverage_complete"] is True, result["ignore_coverage"]
    assert result["status"] == "clean"


def test_undeclared_gitignore_edit_does_not_count_as_coverage(repo, tmp_path):
    original = (repo / ".gitignore").read_text()
    (repo / ".gitignore").write_text(".review-loop/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "remove sensitive ignore coverage")
    (repo / "src.py").write_text("print('x')\n")
    info = ds.repository(str(repo))
    baseline = ds.build_baseline(info, ["src.py"], ds.capture_state(info))
    (repo / ".gitignore").write_text(original)
    manifest = tmp_path / "unowned-gitignore.json"
    manifest.write_text(json.dumps(ds.build_manifest(baseline, ds.capture_state(info))))
    result = sp.scan(str(repo), str(manifest))
    assert result["ignore_coverage_complete"] is False


def test_start_coverage_cli_reports_what_a_scan_of_the_unchanged_repo_credits(repo, tmp_path):
    """FIELD-19: --ignore-coverage is the same ignore_coverage() a scan uses, with no task-owned .gitignore."""
    script = Path(sp.__file__)
    full = json.loads(subprocess.check_output([sys.executable, str(script), "--repo", str(repo), "--ignore-coverage"]))
    assert full["uncovered"] == [] and full["ignore_coverage"] == sp.ignore_coverage(str(repo))
    (repo / ".gitignore").write_text(".review-loop/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "remove sensitive ignore coverage")
    narrow = json.loads(subprocess.check_output([sys.executable, str(script), "--repo", str(repo), "--ignore-coverage"]))
    result = sp.scan(str(repo), str(candidate(repo, tmp_path)))
    assert narrow["uncovered"] == [row["category"] for row in result["ignore_coverage"] if not row["covered"]]
    assert "environment-and-config" in narrow["uncovered"]
    (repo / ".gitignore").write_text(".review-loop/\n.env\n")   # dirty at start: not credited, as a scan would not
    assert sp.start_coverage(str(repo))["uncovered"] == narrow["uncovered"]
