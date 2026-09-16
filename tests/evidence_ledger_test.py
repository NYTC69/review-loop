"""Tests for scripts/evidence_ledger.py (Slice 1: P1 content-bound evidence +
P3 attributable delta).

Every test builds its own throwaway git repository under `tmp_path`; the real
repository's `.git` is never touched.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER = REPO_ROOT / "scripts" / "evidence_ledger.py"
UUID = "11111111-2222-3333-4444-555555555555"

SESSION_TEMPLATE = """## Problem Description
Throwaway session for evidence-ledger tests.

## Context
- synthetic

## Acceptance Criteria
- n/a

## Current Phase
execution

## Approved Plan

- Source: reviewer-approved

Plan body.

## Review History

## Files Changed
None.

## Key Related Files
None.

## Timing Log
| Phase | Round | Role | Duration |
|---|---|---|---|

## Session Metadata
- entry_point: plan
- plan_source: reviewer-approved
- base_head: 0000000000000000000000000000000000000000
- base_dirty: {}
- last_verified_head: 0000000000000000000000000000000000000000
- last_verified_dirty: {}
- session_commits: []
- completed_stages: []
- delivery_blocked_by: null
"""

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


# ---------- helpers ----------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(GIT_ENV)
    return subprocess.run(["git", *args], cwd=str(repo), env=env, capture_output=True, text=True, check=check)


def _write(repo: Path, rel: str, content: str) -> Path:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _commit_all(repo: Path, message: str = "commit") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "src/a.py", "a = 1\n")
    _write(repo, "src/b.py", "b = 2\n")
    _write(repo, "tests/test_a.py", "def test_a():\n    assert True\n")
    _write(repo, "docs/notes.md", "notes\n")
    _commit_all(repo, "base")
    (repo / ".review-loop" / "sessions").mkdir(parents=True)
    (repo / ".review-loop" / "sessions" / f"{UUID}.md").write_text(SESSION_TEMPLATE, encoding="utf-8")
    _write(repo, ".gitignore", ".review-loop/\n")
    _commit_all(repo, "ignore session dir")
    return repo


def run(repo: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(GIT_ENV)
    completed = subprocess.run(
        [sys.executable, str(LEDGER), "--repo", str(repo), "--session", UUID, *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == expect, (
        f"expected exit {expect}, got {completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )
    return completed


def run_json(repo: Path, *args: str, expect: int = 0) -> dict:
    return json.loads(run(repo, *args, expect=expect).stdout)


def snapshot(repo: Path, **kwargs) -> dict:
    return run_json(repo, "snapshot", *kwargs.get("extra", []))


def session_text(repo: Path) -> str:
    return (repo / ".review-loop" / "sessions" / f"{UUID}.md").read_text(encoding="utf-8")


def ledger(repo: Path) -> dict:
    text = session_text(repo)
    start = text.index("## Evidence Ledger")
    fence = text.index("```json\n", start) + len("```json\n")
    end = text.index("\n```", fence)
    return json.loads(text[fence:end])


def completed_stages_line(repo: Path) -> str:
    for line in session_text(repo).splitlines():
        if line.startswith("- completed_stages:"):
            return line
    raise AssertionError("completed_stages line missing")


def record_approve(repo: Path, *paths: str, closure: str = "declared", result: str = "PASS", extra=()) -> dict:
    args = ["record", "--check", "reviewer_approve", "--result", result, "--closure", closure]
    for p in paths:
        args += ["--scope-path", p, "--input", p]
    return run_json(repo, *args, *extra)


def record_gate_skip(repo: Path, *paths: str) -> dict:
    args = ["record", "--check", "gate", "--disposition", "controlled-skip",
            "--reason", "adversarial-gate: SKIP reason=skipped-by-config", "--closure", "declared"]
    for p in paths:
        args += ["--scope-path", p, "--input", p]
    return run_json(repo, *args)


def record_tests(repo: Path, *, result: str = "PASS", inputs=("src/a.py",), selectors=("glob:tests/**",),
                 closure: str = "declared", extra=()) -> dict:
    args = ["record", "--check", "tests", "--scope", "pytest tests/", "--env-command", "pytest tests/",
            "--result", result, "--closure", closure]
    for p in inputs:
        args += ["--input", p]
    for s in selectors:
        args += ["--selector", s]
    return run_json(repo, *args, *extra)


def check(repo: Path, *extra: str, expect: int = 0) -> dict:
    return run_json(repo, "check", *extra, expect=expect)


def claim(report: dict, prefix: str) -> dict:
    matches = [c for cid, c in report["claims"].items() if cid.startswith(prefix)]
    assert len(matches) == 1, f"expected one claim starting with {prefix!r}: {list(report['claims'])}"
    return matches[0]


# ---------- section placement + snapshot mechanics ----------


def test_snapshot_inserts_ledger_before_session_metadata_and_stores_objects(tmp_path):
    repo = make_repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD").stdout
    index_before = _git(repo, "ls-files", "-s").stdout
    _write(repo, "src/a.py", "a = 1\n# dirty before session\n")
    _write(repo, "src/untracked.py", "u = 1\n")
    status_before = _git(repo, "status", "--porcelain").stdout

    out = snapshot(repo)
    assert out["snapshot"] == 0
    assert out["ref"] == f"refs/review-loop/{UUID}/snap/0"
    assert "src/untracked.py" in out["untracked"]

    text = session_text(repo)
    assert text.index("## Evidence Ledger") < text.index("## Session Metadata")
    assert text.rstrip().endswith("- delivery_blocked_by: null")
    led = ledger(repo)
    assert led["snapshots"][0]["commit"] == out["commit"]

    # every blob in the snapshot tree is retrievable
    tree = _git(repo, "ls-tree", "-r", out["tree"]).stdout.splitlines()
    assert tree, "snapshot tree is empty"
    for line in tree:
        blob = line.split()[2]
        _git(repo, "cat-file", "-e", blob)
    paths = {line.split("\t")[1] for line in tree}
    assert "src/untracked.py" in paths and "src/a.py" in paths
    # the dirty content (not HEAD content) is what was stored
    stored = _git(repo, "show", f"{out['commit']}:src/a.py").stdout
    assert "# dirty before session" in stored

    # HEAD, branches, the user's index, and the worktree are untouched
    assert _git(repo, "rev-parse", "HEAD").stdout == head_before
    assert _git(repo, "ls-files", "-s").stdout == index_before
    assert _git(repo, "status", "--porcelain").stdout == status_before
    assert _git(repo, "for-each-ref", "refs/heads").stdout.count("\n") == 1
    refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/review-loop").stdout.split()
    assert refs == [f"refs/review-loop/{UUID}/snap/0"]
    # the private index file is not left behind
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    assert not (git_dir / "review-loop" / f"{UUID}.index").exists()


def test_snapshot_objects_survive_gc_and_second_snapshot_increments(tmp_path):
    repo = make_repo(tmp_path)
    first = snapshot(repo)
    _write(repo, "src/a.py", "a = 2\n")
    second = snapshot(repo)
    assert second["snapshot"] == 1
    _git(repo, "gc", "-q", "--prune=now")
    for out in (first, second):
        _git(repo, "cat-file", "-e", out["commit"])
        _git(repo, "cat-file", "-e", out["tree"])


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_snapshot_fails_closed_on_read_only_git_dir(tmp_path):
    repo = make_repo(tmp_path)
    _write(repo, "src/a.py", "a = 99\n")
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    objects = git_dir / "objects"
    saved = {}
    for d in [git_dir, objects, *[p for p in objects.iterdir() if p.is_dir()]]:
        saved[d] = d.stat().st_mode
        d.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        completed = run(repo, "snapshot", expect=3)
        assert "STORAGE-ERROR" in completed.stderr
        assert "## Evidence Ledger" not in session_text(repo)
    finally:
        for d, mode in saved.items():
            d.chmod(mode)


def test_linked_worktree_uses_resolved_git_dir(tmp_path):
    repo = make_repo(tmp_path)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    (wt / ".review-loop" / "sessions").mkdir(parents=True)
    (wt / ".review-loop" / "sessions" / f"{UUID}.md").write_text(SESSION_TEMPLATE, encoding="utf-8")
    _write(wt, "src/a.py", "a = 3\n")
    out = run_json(wt, "snapshot")
    assert (wt / ".git").is_file()  # linked worktree marker
    _git(wt, "cat-file", "-e", out["commit"])
    refs = _git(wt, "for-each-ref", "--format=%(refname)", "refs/review-loop").stdout.split()
    assert refs == [f"refs/review-loop/{UUID}/snap/0"]


# ---------- record + active-record rule ----------


def test_byte_identical_reuse_is_valid_and_reuse_requires_why(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    rec = record_approve(repo, "src/a.py")
    assert rec["id"] == 1
    report = check(repo)
    approve = claim(report, "reviewer_approve:")
    assert approve["valid"] is True and approve["reasons"] == ["byte-identical"]

    # reuse without --why is rejected; with --why it is a fresh record with provenance
    run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
        "--result", "PASS", "--closure", "declared", "--provenance", "reused-from:1", expect=2)
    reused = run_json(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
                      "--result", "PASS", "--closure", "declared", "--provenance", "reused-from:1",
                      "--why", "inputs byte-identical since round 1")
    assert reused["id"] == 2
    led = ledger(repo)
    assert led["records"][0]["superseded_by"] == 2
    assert led["records"][1]["superseded_by"] is None
    assert led["records"][1]["provenance"] == "reused-from:1"


def test_changed_input_invalidates(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_approve(repo, "src/a.py")
    _write(repo, "src/a.py", "a = 42\n")
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["changed input: src/a.py"]
    assert report["completed_stages"] == []


def test_changed_dep_invalidates(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    run_json(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
             "--dep", "src/b.py", "--result", "PASS", "--closure", "declared")
    _write(repo, "src/b.py", "b = 3\n")
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["changed dep: src/b.py"]


def test_fail_is_never_reused_and_dominates_until_fresh_pass(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_gate_skip(repo, "src/a.py")
    record_approve(repo, "src/a.py")            # id 2 PASS
    assert check(repo)["completed_stages"] == ["exec"]
    record_approve(repo, "src/a.py", result="FAIL")  # id 3 FAIL supersedes the PASS
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["FAIL"]
    assert report["completed_stages"] == []
    assert report["stages"]["exec"]["active"]["reviewer_approve"] == [3]
    assert "governing" not in report["stages"]["exec"]
    # a FAIL cannot be the source of reuse
    completed = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
                    "--result", "PASS", "--closure", "declared", "--provenance", "reused-from:3",
                    "--why", "x", expect=2)
    assert "not currently valid: FAIL" in completed.stderr
    # the superseded PASS (id 2) can never satisfy the claim again
    completed = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
                    "--result", "PASS", "--closure", "declared", "--provenance", "reused-from:2",
                    "--why", "x", expect=2)
    assert "superseded" in completed.stderr
    # FAIL then fresh PASS satisfies
    record_approve(repo, "src/a.py")            # id 4 PASS
    report = check(repo)
    assert report["completed_stages"] == ["exec"]
    assert ledger(repo)["records"][2]["superseded_by"] == 4
    # FAIL then FAIL on the same claim: the newer FAIL is active and still blocks
    record_approve(repo, "src/a.py", result="FAIL")  # id 5
    record_approve(repo, "src/a.py", result="FAIL")  # id 6
    report = check(repo)
    assert report["completed_stages"] == []
    assert report["stages"]["exec"]["active"]["reviewer_approve"] == [6]


# ---------- cross-claim_id supersession (execution round 1 CRITICAL #1 / #2) ----------


def test_active_fail_in_other_scope_blocks_exec_until_explicitly_superseded(tmp_path):
    """(a) an active `reviewer_approve:a.py` FAIL + a later `reviewer_approve:b.py`
    PASS → `exec` absent (every active record of a required check must be valid;
    no governing-record selection across claim_ids). (b) the later record with
    `--supersedes` of the FAIL and scope `a.py|b.py` PASS → `exec` present."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_gate_skip(repo, "src/a.py", "src/b.py")          # id 1
    record_approve(repo, "src/a.py", result="FAIL")         # id 2 FAIL, scope a.py
    record_approve(repo, "src/b.py")                        # id 3 PASS, scope b.py
    report = check(repo)
    assert report["completed_stages"] == []
    assert report["stages"]["exec"]["active"]["reviewer_approve"] == [2, 3]
    assert any(m.startswith("reviewer_approve (record 2): FAIL") for m in report["stages"]["exec"]["missing"])
    # classify also consults every active exec-required record
    _write(repo, "src/a.py", "a = 9\n")
    out = run_json(repo, "classify", expect=1)
    assert any("(record 2): changed closure paths ['src/a.py']" in r for r in out["reasons"])
    _write(repo, "src/a.py", "a = 1\n")
    # (b) a covering PASS that explicitly supersedes the FAIL satisfies exec
    out = record_approve(repo, "src/a.py", "src/b.py", extra=["--supersedes", "2"])  # id 4
    assert out["supersedes"] == [2]
    led = ledger(repo)
    assert led["records"][1]["superseded_by"] == 4
    assert led["records"][3]["supersedes"] == [2]
    assert led["records"][2]["superseded_by"] is None       # b.py PASS stays active and valid
    report = check(repo)
    assert report["completed_stages"] == ["exec"]
    assert report["stages"]["exec"]["active"]["reviewer_approve"] == [3, 4]
    # an explicit mark survives later implicit re-marking
    record_approve(repo, "src/a.py")                        # id 5, same claim_id as the FAIL
    assert ledger(repo)["records"][1]["superseded_by"] == 4


def test_supersedes_fail_requires_covering_executed_pass(tmp_path):
    """(c) `--supersedes` of a FAIL by a PASS whose scope does not cover it →
    exit 2, ledger unchanged; a non-PASS or a FAIL also cannot supersede it."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_approve(repo, "src/a.py", "src/b.py", result="FAIL")   # id 1 FAIL, scope a.py|b.py
    before = session_text(repo)
    c = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/b.py", "--input", "src/b.py",
            "--result", "PASS", "--closure", "declared", "--supersedes", "1", expect=2)
    assert "does not cover" in c.stderr and session_text(repo) == before
    c = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--scope-path", "src/b.py",
            "--input", "src/a.py", "--input", "src/b.py", "--result", "FAIL", "--closure", "declared",
            "--supersedes", "1", expect=2)
    assert "only an executed PASS" in c.stderr and session_text(repo) == before
    # a superset scope PASS covers it
    out = record_approve(repo, "src/a.py", "src/b.py", "docs/notes.md", extra=["--supersedes", "1"])
    assert out["id"] == 2 and ledger(repo)["records"][0]["superseded_by"] == 2
    # command-scoped FAIL: the scope key must be identical
    record_tests(repo, result="FAIL")                                 # id 3 scope "pytest tests/"
    before = session_text(repo)
    c = run(repo, "record", "--check", "tests", "--scope", "pytest tests/ -k x", "--env-command", "pytest",
            "--result", "PASS", "--closure", "declared", "--input", "src/a.py", "--supersedes", "3", expect=2)
    assert "does not cover" in c.stderr and session_text(repo) == before
    record_tests(repo, extra=["--supersedes", "3"])                   # identical scope → allowed
    assert ledger(repo)["records"][2]["superseded_by"] == 4
    # a gate FAIL cannot be buried by a controlled-skip
    run_json(repo, "record", "--check", "gate", "--scope-path", "src/a.py", "--input", "src/a.py",
             "--result", "FAIL", "--closure", "declared")             # id 5
    before = session_text(repo)
    c = run(repo, "record", "--check", "gate", "--scope-path", "src/a.py", "--input", "src/a.py",
            "--disposition", "controlled-skip", "--reason", "skip", "--closure", "declared",
            "--supersedes", "5", expect=2)
    assert "only an executed PASS" in c.stderr and session_text(repo) == before


def test_supersedes_rejects_other_check_unknown_and_already_superseded(tmp_path):
    """(d) `--supersedes` targeting a different check / unknown id / already-superseded id → exit 2."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_gate_skip(repo, "src/a.py")                  # id 1 (gate)
    record_approve(repo, "src/a.py")                    # id 2 PASS
    record_approve(repo, "src/a.py")                    # id 3 PASS → 2 implicitly superseded
    before = session_text(repo)
    for target, needle in (("1", "not 'reviewer_approve'"), ("42", "does not exist"),
                           ("2", "already superseded"), ("x", "integer ids"), ("3,3", "twice")):
        c = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/b.py", "--input", "src/b.py",
                "--result", "PASS", "--closure", "declared", "--supersedes", target, expect=2)
        assert needle in c.stderr, c.stderr
        assert session_text(repo) == before
    # a valid PASS target of the same check may be superseded freely
    out = record_approve(repo, "src/b.py", extra=["--supersedes", "3"])
    assert ledger(repo)["records"][2]["superseded_by"] == out["id"]


def test_stale_earlier_scope_pass_needs_explicit_supersession_when_scope_grows(tmp_path):
    """(f) a stale earlier-scope PASS (inputs changed) plus a valid superset-scope
    PASS: without `--supersedes` → `exec` absent; with it → `exec` present. The
    orchestrator must supersede when the review scope grows."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_approve(repo, "src/a.py")                    # id 1 PASS, scope a.py
    _write(repo, "src/a.py", "a = 2\n")                 # round 2 edits a.py → record 1 stale
    snapshot(repo)
    record_gate_skip(repo, "src/a.py", "src/b.py")      # id 2 (new convergence's gate)
    record_approve(repo, "src/a.py", "src/b.py")        # id 3 PASS, superset scope, no --supersedes
    report = check(repo)
    assert report["completed_stages"] == []
    assert report["stages"]["exec"]["missing"] == ["reviewer_approve (record 1): changed input: src/a.py"]
    record_approve(repo, "src/a.py", "src/b.py", extra=["--supersedes", "1"])   # id 4
    report = check(repo)
    assert report["completed_stages"] == ["exec"]
    assert ledger(repo)["records"][0]["superseded_by"] == 4
    assert report["stages"]["exec"]["active"]["reviewer_approve"] == [4]


def test_uncertain_closure_is_never_valid_nor_reusable(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_gate_skip(repo, "src/a.py")
    record_approve(repo, "src/a.py", closure="uncertain")
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["uncertain closure"]
    assert report["completed_stages"] == []
    completed = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
                    "--result", "PASS", "--closure", "declared", "--provenance", "reused-from:2",
                    "--why", "x", expect=2)
    assert "uncertain closure" in completed.stderr
    # deps = "uncertain" alone is also invalid
    run_json(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
             "--deps-uncertain", "--result", "PASS", "--closure", "declared")
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["deps uncertain"]


def test_not_applicable_and_controlled_skip_only_for_permitted_checks(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    # illegal for tests / reviewer_approve
    c = run(repo, "record", "--check", "tests", "--scope", "pytest", "--disposition", "not-applicable",
            "--reason", "none", "--closure", "declared", expect=2)
    assert "not-applicable is only legal" in c.stderr
    c = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--disposition",
            "controlled-skip", "--reason", "skip", "--closure", "declared", expect=2)
    assert "controlled-skip is only legal" in c.stderr
    # --result must be absent for non-executed dispositions; --reason required
    run(repo, "record", "--check", "gate", "--scope-path", "src/a.py", "--disposition", "controlled-skip",
        "--result", "PASS", "--reason", "x", "--closure", "declared", expect=2)
    run(repo, "record", "--check", "static_analysis", "--scope", "none", "--disposition", "not-applicable",
        "--closure", "declared", expect=2)
    # permitted: static_analysis not-applicable, gate controlled-skip; neither renders as PASS
    run_json(repo, "record", "--check", "static_analysis", "--scope", "none", "--disposition", "not-applicable",
             "--reason", "no static analyzer configured", "--closure", "declared")
    record_gate_skip(repo, "src/a.py")
    report = check(repo)
    sa = claim(report, "static_analysis:")
    assert sa["valid"] is True and sa["result"] is None and sa["reasons"] == ["not-applicable"]
    gate = claim(report, "gate:")
    assert gate["valid"] is True and gate["result"] is None and gate["reasons"] == ["controlled-skip"]
    led = ledger(repo)
    assert all("result" not in r for r in led["records"])
    assert led["records"][0]["reason"] == "no static analyzer configured"
    markdown = run(repo, "check", "--format", "markdown").stdout
    assert "PASS" not in markdown


def test_derived_stages_per_required_set_and_metadata_line(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    assert check(repo)["completed_stages"] == []
    assert completed_stages_line(repo) == "- completed_stages: []"
    record_approve(repo, "src/a.py")
    assert check(repo)["stages"]["exec"]["missing"] == ["gate: no record"]
    record_gate_skip(repo, "src/a.py")
    assert check(repo)["completed_stages"] == ["exec"]
    assert completed_stages_line(repo) == "- completed_stages: [exec]"
    run_json(repo, "record", "--check", "static_analysis", "--scope", "none", "--disposition", "not-applicable",
             "--reason", "none configured", "--closure", "declared")
    for agent in ("code-reviewer", "silent-failure-hunter"):
        run_json(repo, "record", "--check", f"agent_review:{agent}", "--scope-path", "src/a.py",
                 "--input", "src/a.py", "--result", "PASS", "--closure", "declared")
    run_json(repo, "record", "--check", "simplify", "--scope-path", "src/a.py", "--input", "src/a.py",
             "--result", "PASS", "--closure", "declared")
    record_tests(repo)
    assert check(repo)["completed_stages"] == ["exec", "polish"]
    run_json(repo, "record", "--check", "docs_consistency", "--input", "docs/notes.md", "--result", "PASS",
             "--closure", "declared")
    run_json(repo, "record", "--check", "security_scan", "--env-command", "scan", "--input", ".gitignore",
             "--result", "PASS", "--closure", "declared")
    report = check(repo)
    assert report["completed_stages"] == ["exec", "polish", "docs", "security"]
    assert completed_stages_line(repo) == "- completed_stages: [exec, polish, docs, security]"
    assert session_text(repo).rstrip().endswith("- delivery_blocked_by: null")
    # a write to a polish input drops polish (and exec) but keeps docs / security
    _write(repo, "src/a.py", "a = 7\n")
    report = check(repo)
    assert report["completed_stages"] == ["docs", "security"]
    # --require exits 1 for an absent stage
    run(repo, "check", "--require", "exec", expect=1)
    run(repo, "check", "--require", "docs", expect=0)


# ---------- freshness ----------


def test_manual_without_freshness_is_non_reusable(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    run_json(repo, "record", "--check", "manual:smoke on device", "--env-command", "manual", "--input", "src/a.py",
             "--result", "PASS", "--closure", "declared")
    report = check(repo)
    m = claim(report, "manual:")
    assert m["valid"] is False and m["reasons"] == ["missing freshness"]
    assert ledger(repo)["records"][0]["requires_freshness"] is True


def test_ttl_expiry_uses_recorded_at(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    run_json(repo, "record", "--check", "manual:smoke", "--env-command", "manual", "--input", "src/a.py",
             "--result", "PASS", "--closure", "declared", "--freshness", "ttl:3600")
    assert claim(check(repo), "manual:")["valid"] is True
    report = check(repo, "--now", "2999-01-01T00:00:00+00:00")
    assert claim(report, "manual:")["reasons"] == ["expired ttl"]
    # env-sensitive non-manual record with ttl behaves the same
    record_tests(repo, extra=["--env-sensitive", "--freshness", "ttl:0"])
    report = check(repo, "--now", "2999-01-01T00:00:00+00:00")
    assert claim(report, "tests:")["reasons"] == ["expired ttl"]


def test_bound_to_head_invalidates_after_commit(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    run_json(repo, "record", "--check", "manual:smoke", "--env-command", "manual", "--input", "src/a.py",
             "--result", "PASS", "--closure", "declared", "--freshness", "bound-to-head")
    assert claim(check(repo), "manual:")["valid"] is True
    _write(repo, "docs/notes.md", "changed\n")
    _commit_all(repo, "move head")
    report = check(repo)
    assert claim(report, "manual:")["reasons"] == ["head moved"]


# ---------- tombstones, untracked, renames ----------


def test_deletion_tombstone(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "src" / "b.py").unlink()
    out = snapshot(repo)
    assert out["deleted"] == ["src/b.py"]
    rec = run_json(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/b.py", "--input", "src/b.py",
                   "--result", "PASS", "--closure", "declared")
    assert ledger(repo)["records"][0]["inputs"] == {"src/b.py": "<deleted>"}
    assert claim(check(repo), "reviewer_approve:")["valid"] is True
    _write(repo, "src/b.py", "b = 2\n")
    assert claim(check(repo), "reviewer_approve:")["reasons"] == ["tombstone reappeared: src/b.py"]
    # staged deletion is also a tombstone
    (repo / "src" / "b.py").unlink()
    _git(repo, "rm", "-q", "--cached", "src/b.py")
    assert snapshot(repo)["deleted"] == ["src/b.py"]
    assert rec["id"] == 1


def test_untracked_blob_is_bound_and_invalidates_on_change(tmp_path):
    repo = make_repo(tmp_path)
    _write(repo, "src/new.py", "n = 1\n")
    snapshot(repo)
    run_json(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/new.py", "--input", "src/new.py",
             "--result", "PASS", "--closure", "declared")
    stored = ledger(repo)["records"][0]["inputs"]["src/new.py"]
    assert stored.startswith("<untracked:") and stored.endswith(">")
    assert claim(check(repo), "reviewer_approve:")["valid"] is True
    _write(repo, "src/new.py", "n = 2\n")
    assert claim(check(repo), "reviewer_approve:")["reasons"] == ["changed input: src/new.py"]
    # an input path missing from the snapshot is rejected at record time
    run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/nope.py", "--input", "src/nope.py",
        "--result", "PASS", "--closure", "declared", expect=2)


def test_rename_recorded_as_tombstone_plus_new_path(tmp_path):
    repo = make_repo(tmp_path)
    _git(repo, "mv", "src/b.py", "src/c.py")
    out = snapshot(repo)
    assert out["deleted"] == ["src/b.py"]
    snap = ledger(repo)["snapshots"][0]
    assert snap["renames"] and snap["renames"][0]["from"] == "src/b.py" and snap["renames"][0]["to"] == "src/c.py"
    run_json(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/b.py", "--scope-path", "src/c.py",
             "--input", "src/b.py", "--input", "src/c.py", "--result", "PASS", "--closure", "declared")
    inputs = ledger(repo)["records"][0]["inputs"]
    assert inputs["src/b.py"] == "<deleted>"
    # identity = "<git mode>:<blob sha>" (mode-bound since the round-5 gate fix)
    assert inputs["src/c.py"] == "100644:" + _git(repo, "hash-object", "src/c.py").stdout.strip()
    assert ledger(repo)["records"][0]["claim_id"] == "reviewer_approve:src/b.py|src/c.py"
    assert claim(check(repo), "reviewer_approve:")["valid"] is True


# ---------- selectors ----------


def test_selector_new_file_under_tests_invalidates_tests_claim(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_tests(repo)
    assert claim(check(repo), "tests:")["valid"] is True
    _write(repo, "tests/test_new.py", "def test_new():\n    assert True\n")
    report = check(repo)
    assert claim(report, "tests:")["reasons"] == ["selector digest changed: glob:tests/**"]


def test_selector_renamed_member_invalidates(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_tests(repo)
    _git(repo, "mv", "tests/test_a.py", "tests/test_renamed.py")
    report = check(repo)
    assert claim(report, "tests:")["reasons"] == ["selector digest changed: glob:tests/**"]


def test_lint_closure_selector_catches_new_skill_file_and_contract(tmp_path):
    repo = make_repo(tmp_path)
    _write(repo, "scripts/run-skill-lint", "#!/bin/sh\nexit 0\n")
    _write(repo, "skills/plan/SKILL.md", "# plan\n")
    _write(repo, "docs/protocol/session-file.md", "# proto\n")
    _write(repo, "tests/skills/contracts/demo.json", json.dumps({
        "id": "demo",
        "assertions": [{"id": "x", "kind": "contains", "path": "skills/plan/SKILL.md", "needle": "plan"}],
    }))
    _commit_all(repo, "lint surface")
    closure = run_json(repo, "lint-closure")
    assert closure["inputs"] == ["scripts/run-skill-lint"]
    patterns = {(s["kind"], s["pattern"]) for s in closure["selectors"]}
    assert ("discovery", "skills/**/SKILL.md") in patterns
    assert ("discovery", "docs/protocol/*.md") in patterns
    assert ("discovery", "tests/skills/contracts/**") in patterns
    assert ("glob", "skills/plan/SKILL.md") in patterns
    closure_file = tmp_path / "closure.json"
    closure_file.write_text(json.dumps(closure), encoding="utf-8")

    snapshot(repo)
    run_json(repo, "record", "--check", "lint", "--scope", "bash scripts/run-skill-lint",
             "--env-command", "bash scripts/run-skill-lint", "--result", "PASS", "--closure", "declared",
             "--closure-file", str(closure_file))
    assert claim(check(repo), "lint:")["valid"] is True
    _write(repo, "skills/execute/SKILL.md", "# execute\n")
    reasons = claim(check(repo), "lint:")["reasons"]
    assert "selector digest changed: discovery:skills/**/SKILL.md" in reasons
    (repo / "skills" / "execute" / "SKILL.md").unlink()
    assert claim(check(repo), "lint:")["valid"] is True
    _write(repo, "tests/skills/contracts/other.json", "{}")
    reasons = claim(check(repo), "lint:")["reasons"]
    assert "selector digest changed: discovery:tests/skills/contracts/**" in reasons


# ---------- classify ----------


def test_classify_declared_closure_paths(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_approve(repo, "src/a.py")
    record_gate_skip(repo, "src/a.py")
    # change outside the declared closure → non-invalidating
    _write(repo, "docs/notes.md", "prose only\n")
    out = run_json(repo, "classify", expect=0)
    assert out["exec"] == "non-invalidating" and out["changed_paths"] == ["docs/notes.md"]
    # change inside the closure → invalidated (exit 1)
    _write(repo, "src/a.py", "a = 5\n")
    out = run_json(repo, "classify", expect=1)
    assert out["exec"] == "invalidated"
    assert any("changed closure paths ['src/a.py']" in r for r in out["reasons"])


def test_classify_uncertain_closure_invalidates_regardless_of_path(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_approve(repo, "src/a.py", closure="uncertain")
    record_gate_skip(repo, "src/a.py")
    _write(repo, "docs/notes.md", "prose only\n")
    out = run_json(repo, "classify", expect=1)
    assert out["exec"] == "invalidated"
    assert any("uncertain closure" in r for r in out["reasons"])


def test_classify_with_no_exec_records_fails_closed(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    out = run_json(repo, "classify", expect=1)
    assert out["exec"] == "invalidated"
    assert "reviewer_approve: no active record" in out["reasons"]


# ---------- delta ----------


def test_delta_attributes_only_round_change_for_already_dirty_file(tmp_path):
    repo = make_repo(tmp_path)
    _write(repo, "src/a.py", "a = 1\npre_existing_dirty = True\n")   # dirty before the session
    _write(repo, "unrelated.txt", "external\n")
    base = snapshot(repo)                                            # snap/0
    _write(repo, "src/a.py", "a = 1\npre_existing_dirty = True\nround_one = True\n")
    _write(repo, "unrelated.txt", "external moved on\n")             # unrelated dirty path drifts
    post = snapshot(repo)                                            # snap/1
    out = run_json(repo, "delta", "--format", "json", "--exclude", "unrelated.txt")
    assert out["pre"] == 0 and out["post"] == 1
    assert [row["path"] for row in out["rows"]] == ["src/a.py"]
    row = out["rows"][0]
    assert row["pre_blob"] != row["post_blob"]
    patch = out["patch"]
    assert "+round_one = True" in patch
    assert "+pre_existing_dirty" not in patch
    assert "unrelated.txt" not in patch
    # the patch is exactly git diff between the two stored snapshot commits
    expected = _git(repo, "diff", "--no-renames", base["commit"], post["commit"], "--", "src/a.py").stdout
    assert patch == expected
    # blob identities in the table match the stored trees
    assert row["pre_blob"] == _git(repo, "rev-parse", f"{base['commit']}:src/a.py").stdout.strip()
    assert row["post_blob"] == _git(repo, "rev-parse", f"{post['commit']}:src/a.py").stdout.strip()
    markdown = run(repo, "delta", "--exclude", "unrelated.txt").stdout
    assert "### Attributable Delta" in markdown
    assert "| 0 | 1 | `src/a.py` |" in markdown
    assert "unrelated.txt" not in markdown
    assert f"refs/review-loop/{UUID}/snap/0" in markdown


def test_delta_requires_two_snapshots_and_supports_explicit_range(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    run(repo, "delta", expect=2)
    _write(repo, "src/a.py", "a = 2\n")
    snapshot(repo)
    _write(repo, "src/b.py", "b = 9\n")
    snapshot(repo)
    out = run_json(repo, "delta", "--format", "json", "--pre", "0", "--post", "2")
    assert [row["path"] for row in out["rows"]] == ["src/a.py", "src/b.py"]
    out = run_json(repo, "delta", "--format", "json", "--pre", "1", "--post", "2")
    assert [row["path"] for row in out["rows"]] == ["src/b.py"]


# ---------- prune, route, misc ----------


def test_prune_deletes_refs_only_for_this_session(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    _git(repo, "update-ref", "refs/review-loop/other-session/snap/0", "HEAD")
    out = run_json(repo, "prune")
    assert out["pruned_refs"] == [f"refs/review-loop/{UUID}/snap/0"]
    refs = _git(repo, "for-each-ref", "--format=%(refname)", "refs/review-loop").stdout.split()
    assert refs == ["refs/review-loop/other-session/snap/0"]
    assert "pruned_at" in ledger(repo)


# ---------- route (Slice 2: orchestrator-direct author route) ----------


ELIGIBILITY_FACTS = ("small_bounded_scope", "unambiguous_requirements", "known_dependency_impact",
                     "no_useful_decomposition", "safe_verification", "dirty_work_preserved")
SENSITIVE_FLAGS = ("auth_or_authorization", "permissions", "destructive_operation", "irreversible_data_change",
                   "secrets", "external_writes", "migrations", "broad_api_or_architecture", "large_surface")


def route_facts(overrides: dict | None = None, omit: tuple = ()) -> str:
    values = {name: "true" for name in ELIGIBILITY_FACTS}
    values.update({name: "false" for name in SENSITIVE_FLAGS})
    values.update(overrides or {})
    rows = ["### Route Facts", "", "| fact | value | rationale |", "|---|---|---|"]
    for name, value in values.items():
        if name in omit:
            continue
        rows.append(f"| {name} | {value} | synthetic rationale for {name} |")
    return "\n".join(rows) + "\n"


def write_facts(tmp_path: Path, text: str) -> str:
    target = tmp_path / "route-facts.md"
    target.write_text(text, encoding="utf-8")
    return str(target)


def route(repo: Path, tmp_path: Path, text: str, *extra: str, expect: int) -> dict:
    return run_json(repo, "route", "--facts", write_facts(tmp_path, text), *extra, expect=expect)


def test_route_all_true_all_false_is_orchestrator_direct(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    out = route(repo, tmp_path, route_facts(), expect=0)
    assert out["route"] == "orchestrator-direct" and out["phase"] == "execution" and out["snapshot"] == 0
    assert out["facts"] == {name: "true" for name in ELIGIBILITY_FACTS}
    assert out["flags"] == {name: "false" for name in SENSITIVE_FLAGS}
    assert run(repo, "route", "--facts", write_facts(tmp_path, route_facts()), "--format", "route").stdout.strip() \
        == "orchestrator-direct"
    # stdin works too
    env = os.environ.copy()
    env.update(GIT_ENV)
    completed = subprocess.run([sys.executable, str(LEDGER), "--repo", str(repo), "--session", UUID,
                                "route", "--facts", "-", "--format", "route"],
                               input=route_facts(), capture_output=True, text=True, env=env, check=False)
    assert completed.returncode == 0 and completed.stdout.strip() == "orchestrator-direct"


def test_route_each_sensitive_flag_alone_forces_executor(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    for flag in SENSITIVE_FLAGS:
        for value in ("true", "uncertain"):
            out = route(repo, tmp_path, route_facts({flag: value}), expect=1)
            assert out["route"] == "executor"
            assert out["reasons"] == [f"sensitive flag {flag} is {value}, not false"]


def test_route_each_eligibility_fact_false_uncertain_or_omitted_forces_executor(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    for fact in ELIGIBILITY_FACTS:
        for value in ("false", "uncertain"):
            out = route(repo, tmp_path, route_facts({fact: value}), expect=1)
            assert out["route"] == "executor"
            assert out["reasons"] == [f"eligibility fact {fact} is {value}, not true"]
        out = route(repo, tmp_path, route_facts(omit=(fact,)), expect=1)
        assert out["route"] == "executor"
        assert out["reasons"] == [f"eligibility fact {fact} is missing, not true"]
    out = route(repo, tmp_path, route_facts(omit=("secrets",)), expect=1)
    assert out["reasons"] == ["sensitive flag secrets is missing, not false"]


def test_route_known_dependency_impact_cross_checks_ledger_closure(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_approve(repo, "src/a.py", closure="uncertain")   # id 1 uncertain closure
    out = route(repo, tmp_path, route_facts(), "--path", "src/a.py", expect=1)
    assert out["route"] == "executor"
    assert out["cross_check"] == {"required": True, "checked_records": [1],
                                  "failed": ["record 1 (reviewer_approve:src/a.py): closure uncertain"]}
    assert out["reasons"][0].startswith("ledger cross-check failed: known_dependency_impact is true")
    # without --path every active claim is cross-checked (fail closed)
    out = route(repo, tmp_path, route_facts(), expect=1)
    assert out["cross_check"]["checked_records"] == [1]
    # a path outside the record's closure does not touch it → passes
    out = route(repo, tmp_path, route_facts(), "--path", "docs/notes.md", expect=0)
    assert out["route"] == "orchestrator-direct" and out["cross_check"]["checked_records"] == []
    # a declared closure passes; deps uncertain fails; selectors count as touching
    record_approve(repo, "src/a.py")                        # id 2 declared → supersedes 1
    assert route(repo, tmp_path, route_facts(), "--path", "src/a.py", expect=0)["route"] == "orchestrator-direct"
    record_tests(repo, inputs=(), selectors=("glob:src/**",), extra=["--deps-uncertain"])   # id 3
    out = route(repo, tmp_path, route_facts(), "--path", "src/b.py", expect=1)
    assert out["cross_check"]["failed"] == ["record 3 (tests:pytest tests/): deps uncertain"]
    # known_dependency_impact false never consults the ledger, but still routes to executor
    out = route(repo, tmp_path, route_facts({"known_dependency_impact": "false"}), expect=1)
    assert out["cross_check"] == {"required": False, "checked_records": [], "failed": []}


def test_route_planning_phase_and_missing_snapshot_force_executor(tmp_path):
    repo = make_repo(tmp_path)
    out = route(repo, tmp_path, route_facts(), expect=1)          # no snapshot yet
    assert out["route"] == "executor" and out["snapshot"] is None
    assert out["reasons"] == ["no stored snapshot (evidence cannot be bound; direct route refused)"]
    snapshot(repo)
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    session.write_text(session_text(repo).replace("## Current Phase\nexecution", "## Current Phase\nplanning"),
                       encoding="utf-8")
    out = route(repo, tmp_path, route_facts(), expect=1)
    assert out["route"] == "executor" and out["phase"] == "planning"
    assert out["reasons"] == ["phase is planning, not execution (planning is always Executor-authored)"]


def test_route_reads_packet_block_by_default_and_rejects_malformed_blocks(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    # no packet / no block anywhere → fail closed to executor (exit 1), never exit 2
    report = run_json(repo, "route", expect=1)
    assert report["route"] == "executor"
    assert "route facts block absent from Current Review Packet" in report["reasons"]
    # block inside the session packet is the default source
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    text = session_text(repo).replace(
        "## Review History\n",
        "## Current Review Packet\n\n### Author route\nexecutor\n\n" + route_facts() + "\n## Review History\n",
    )
    session.write_text(text, encoding="utf-8")
    assert run_json(repo, "route", expect=0)["route"] == "orchestrator-direct"
    # malformed: unknown fact / illegal value / duplicate / missing rationale
    for text, needle in (
        (route_facts({"looks_easy": "true"}), "unknown route fact"),
        (route_facts({"secrets": "no"}), "illegal value"),
        (route_facts() + "| secrets | false | again |\n", "listed twice"),
        (route_facts().replace("| secrets | false | synthetic rationale for secrets |", "| secrets | false | |"),
         "no rationale"),
    ):
        c = run(repo, "route", "--facts", write_facts(tmp_path, text), expect=2)
        assert needle in c.stderr, c.stderr


STALE_HISTORY_ENTRY = (
    "## Review History\n\n### Execution Round 1 — Direct Implementation Record\n\n"
    "### Author Route: orchestrator-direct\n\n" + route_facts() + "\n"
)


def test_route_ignores_stale_history_block_when_packet_has_none(tmp_path):
    """Round-2 finding: an all-eligible `### Route Facts` block left in
    `## Review History` (an old DIR) must never authorize the direct route
    when the current packet carries no block → `executor`, exit 1."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    text = session_text(repo).replace("## Review History\n", STALE_HISTORY_ENTRY)
    # (1) packet section present but without the block
    session.write_text(text.replace(
        "## Review History\n", "## Current Review Packet\n\n### Author route\nexecutor\n\n## Review History\n"),
        encoding="utf-8")
    report = run_json(repo, "route", expect=1)
    assert report["route"] == "executor"
    assert report["reasons"][0] == "route facts block absent from Current Review Packet"
    assert report["facts"] == {name: "missing" for name in ELIGIBILITY_FACTS}
    # (2) no packet section at all — the stale history block is still ignored
    session.write_text(text, encoding="utf-8")
    report = run_json(repo, "route", expect=1)
    assert report["route"] == "executor"
    assert "route facts block absent from Current Review Packet" in report["reasons"]
    # the stale block is genuinely all-eligible: read explicitly it would route direct
    assert route(repo, tmp_path, route_facts(), expect=0)["route"] == "orchestrator-direct"


def test_route_uses_packet_block_not_stale_history_block(tmp_path):
    """History carries an all-eligible block, the packet carries a block with
    `secrets: true`: the packet's block decides (executor), proving the
    history block was not read. Swapping them proves the direction."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    base = session_text(repo)
    packet = "## Current Review Packet\n\n### Author route\nexecutor\n\n"
    # history: all-eligible; packet: secrets true → executor
    session.write_text(base.replace(
        "## Review History\n",
        packet + route_facts({"secrets": "true"}) + "\n" + STALE_HISTORY_ENTRY), encoding="utf-8")
    report = run_json(repo, "route", expect=1)
    assert report["route"] == "executor"
    assert report["flags"]["secrets"] == "true"
    assert report["reasons"] == ["sensitive flag secrets is true, not false"]
    # history: secrets true; packet: all-eligible → orchestrator-direct
    session.write_text(base.replace(
        "## Review History\n",
        packet + route_facts() + "\n" + STALE_HISTORY_ENTRY.replace(route_facts(), route_facts({"secrets": "true"}))),
        encoding="utf-8")
    report = run_json(repo, "route", expect=0)
    assert report["route"] == "orchestrator-direct"
    assert report["flags"]["secrets"] == "false"
    # a malformed block inside the packet stays exit 2 even with a valid history block
    session.write_text(base.replace(
        "## Review History\n",
        packet + route_facts({"looks_easy": "true"}) + "\n" + STALE_HISTORY_ENTRY), encoding="utf-8")
    c = run(repo, "route", expect=2)
    assert "unknown route fact" in c.stderr


def test_route_ignores_block_before_current_review_packet(tmp_path):
    """A `### Route Facts` heading that precedes `## Current Review Packet`
    (e.g. pasted into `## Approved Plan`) is outside the packet section and
    is ignored."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    base = session_text(repo)
    assert base.index("## Approved Plan") < base.index("## Review History")
    text = base.replace("Plan body.\n", "Plan body.\n\n" + route_facts() + "\n").replace(
        "## Review History\n", "## Current Review Packet\n\n### Author route\nexecutor\n\n## Review History\n")
    session.write_text(text, encoding="utf-8")
    report = run_json(repo, "route", expect=1)
    assert report["route"] == "executor"
    assert report["reasons"][0] == "route facts block absent from Current Review Packet"
    assert report["facts"] == {name: "missing" for name in ELIGIBILITY_FACTS}


PACKET_WITHOUT_BLOCK = "## Current Review Packet\n\n### Author route\nexecutor\n\n"
NOT_UNIQUE_REASON = "canonical packet section not uniquely identifiable"


def test_route_selects_canonical_packet_by_structure_not_first_heading(tmp_path):
    """Round-3 finding (a): `## Approved Plan` quotes a `## Current Review
    Packet` heading with an all-eligible `### Route Facts` block before the
    real packet, which carries no block. The canonical packet is the unique
    heading followed by `## Review History`, so the real one is chosen →
    `executor` with the absent-block reason, never `orchestrator-direct`."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    base = session_text(repo)
    fake_packet = "## Current Review Packet\n\n(quoted protocol text)\n\n" + route_facts() + "\n"
    text = base.replace("Plan body.\n", "Plan body.\n\n" + fake_packet).replace(
        "## Review History\n", PACKET_WITHOUT_BLOCK + "## Review History\n")
    assert text.index("## Current Review Packet") < text.index(PACKET_WITHOUT_BLOCK)  # fake comes first
    session.write_text(text, encoding="utf-8")
    report = run_json(repo, "route", expect=1)
    assert report["route"] == "executor"
    assert report["packet_candidates"] == 1
    assert report["reasons"][0] == "route facts block absent from Current Review Packet"
    assert report["facts"] == {name: "missing" for name in ELIGIBILITY_FACTS}
    # the mirror image: fake packet carries `secrets: true`, real packet is all-eligible → direct
    text = base.replace("Plan body.\n", "Plan body.\n\n" + fake_packet.replace(
        route_facts(), route_facts({"secrets": "true"}))).replace(
        "## Review History\n", PACKET_WITHOUT_BLOCK + route_facts() + "\n## Review History\n")
    session.write_text(text, encoding="utf-8")
    report = run_json(repo, "route", expect=0)
    assert report["route"] == "orchestrator-direct"
    assert report["flags"]["secrets"] == "false"


def test_route_fails_closed_when_packet_heading_is_ambiguous(tmp_path):
    """Round-3 finding (b): the plan quotes both `## Current Review Packet`
    and a following `## Review History` heading, so two headings satisfy the
    structural rule → not uniquely identifiable → `executor`, exit 1, first
    reason `canonical packet section not uniquely identifiable`, and no fact
    from either block is read."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    base = session_text(repo)
    quoted = ("## Current Review Packet\n\n" + route_facts() + "\n## Review History\n\n(quoted)\n\n")
    real = PACKET_WITHOUT_BLOCK + route_facts() + "\n## Review History\n"
    text = base.replace("Plan body.\n", "Plan body.\n\n" + quoted)
    idx = text.rindex("## Review History\n")  # the real section heading is the last one
    text = text[:idx] + real + text[idx + len("## Review History\n"):]
    assert text.count("## Current Review Packet") == 2 and text.count("## Review History") == 2
    session.write_text(text, encoding="utf-8")
    report = run_json(repo, "route", expect=1)
    assert report["route"] == "executor"
    assert report["packet_candidates"] == 2
    assert report["reasons"][0] == NOT_UNIQUE_REASON
    assert report["facts"] == {name: "missing" for name in ELIGIBILITY_FACTS}
    assert report["rationale"] == {}
    # zero candidates (no packet at all) also reports the structural reason first
    session.write_text(base, encoding="utf-8")
    report = run_json(repo, "route", expect=1)
    assert report["packet_candidates"] == 0
    assert report["reasons"][0] == NOT_UNIQUE_REASON
    assert report["reasons"][1] == "route facts block absent from Current Review Packet"


def test_route_ignores_quoted_packet_heading_inside_review_history_details(tmp_path):
    """Round-3 finding (c): a raw reviewer output quoted inside a
    `<details>` block of `## Review History` repeats the packet heading; it
    is followed by `## Files Changed`, not `## Review History`, so it is
    never a candidate. The real packet (blockless) decides → `executor`."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    base = session_text(repo)
    history = ("## Review History\n\n### Round 1 — Reviewer\n\n<details><summary>Raw</summary>\n\n"
               "## Current Review Packet\n\n" + route_facts() + "\n</details>\n\n")
    text = base.replace("## Review History\n", PACKET_WITHOUT_BLOCK + history)
    session.write_text(text, encoding="utf-8")
    report = run_json(repo, "route", expect=1)
    assert report["route"] == "executor"
    assert report["packet_candidates"] == 1
    assert report["reasons"][0] == "route facts block absent from Current Review Packet"
    # with a real block in the real packet the quoted one still plays no part
    session.write_text(base.replace("## Review History\n", PACKET_WITHOUT_BLOCK + route_facts({"secrets": "true"})
                                    + "\n" + history), encoding="utf-8")
    report = run_json(repo, "route", expect=1)
    assert report["reasons"] == ["sensitive flag secrets is true, not false"]


def test_ledger_and_metadata_writers_refuse_ambiguous_sections(tmp_path):
    """Round-3 finding (d): `## Evidence Ledger` / `## Session Metadata` are
    selected by the same structural rule (ledger → followed by
    `## Session Metadata`; metadata → last heading). A quoted
    ledger+metadata pair inside `## Approved Plan` makes the ledger
    ambiguous → `record`, `check`, `snapshot` exit 2 and write nothing; a
    `## Session Metadata` that is not the last heading is not canonical →
    `check` (its writer) exits 2 and writes nothing."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_approve(repo, "src/a.py")
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    good = session_text(repo)
    quoted = "## Evidence Ledger\n\n```json\n{\"schema\": 1}\n```\n\n## Session Metadata\n- quoted: yes\n\n"
    ambiguous = good.replace("Plan body.\n", "Plan body.\n\n" + quoted)
    session.write_text(ambiguous, encoding="utf-8")
    for args in (("record", "--check", "gate", "--scope-path", "src/a.py", "--disposition", "controlled-skip",
                  "--reason", "adversarial-gate: SKIP reason=x"),
                 ("check",),
                 ("snapshot",)):
        c = run(repo, *args, expect=2)
        assert "not uniquely identifiable" in c.stderr, c.stderr
        assert session.read_text(encoding="utf-8") == ambiguous  # nothing written
    refs = _git(repo, "for-each-ref", f"refs/review-loop/{UUID}/").stdout
    assert refs.count("snap/") == 1  # the ambiguous snapshot stored nothing
    # metadata not last → not canonical → the completed_stages writer refuses
    stray = good.rstrip("\n") + "\n\n## Notes\nstray trailing section\n"
    session.write_text(stray, encoding="utf-8")
    c = run(repo, "check", expect=2)
    assert "canonical `## Session Metadata` section" in c.stderr
    assert session.read_text(encoding="utf-8") == stray
    # and a read-only check with --no-write still evaluates (the metadata writer is not reached)
    run(repo, "check", "--no-write", expect=0)


def test_direct_implementation_record_content_state_round_trip(tmp_path):
    """An orchestrator-direct round: the DIR's `### Content State` (pre/post
    path → blob) is exactly the packet's Attributable Delta rows, and every
    blob resolves in the stored snapshot trees; `reviewer_approve` carries
    `author_route: orchestrator-direct`."""
    repo = make_repo(tmp_path)
    pre = snapshot(repo)                                                   # snap/0
    assert route(repo, tmp_path, route_facts(), "--path", "src/a.py", expect=0)["route"] == "orchestrator-direct"
    _write(repo, "src/a.py", "a = 1\ndirect = True\n")                     # the direct write
    post = snapshot(repo)                                                  # snap/1
    rows = run_json(repo, "delta", "--format", "json", "--path", "src/a.py")["rows"]
    content_state = {row["path"]: (row["pre_blob"], row["post_blob"]) for row in rows}
    assert list(content_state) == ["src/a.py"]
    pre_blob, post_blob = content_state["src/a.py"]
    assert pre_blob == _git(repo, "rev-parse", f"{pre['commit']}:src/a.py").stdout.strip()
    assert post_blob == _git(repo, "rev-parse", f"{post['commit']}:src/a.py").stdout.strip()
    assert post_blob == _git(repo, "hash-object", "src/a.py").stdout.strip()   # matches the worktree
    dir_text = "### Content State\n| path | pre_blob | post_blob |\n|---|---|---|\n" + "\n".join(
        f"| `{p}` | {b[0]} | {b[1]} |" for p, b in content_state.items()) + "\n"
    # rendering the delta as markdown yields the same identities the DIR carries
    markdown = run(repo, "delta", "--path", "src/a.py").stdout
    assert f"| 0 | 1 | `src/a.py` | {pre_blob} | {post_blob} |" in markdown
    assert f"| `src/a.py` | {pre_blob} | {post_blob} |" in dir_text
    rec = run_json(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
                   "--result", "PASS", "--closure", "declared", "--author-route", "orchestrator-direct")
    record_gate_skip(repo, "src/a.py")
    report = check(repo)
    assert report["claims"]["reviewer_approve:src/a.py"]["author_route"] == "orchestrator-direct"
    assert report["completed_stages"] == ["exec"]
    assert ledger(repo)["records"][rec["id"] - 1]["inputs"]["src/a.py"] == f"100644:{post_blob}"


def test_record_without_snapshot_and_missing_session_are_usage_errors(tmp_path):
    repo = make_repo(tmp_path)
    completed = run(repo, "record", "--check", "tests", "--scope", "x", "--env-command", "x", "--result", "PASS",
                    expect=2)
    assert "no snapshot stored" in completed.stderr
    env = os.environ.copy()
    env.update(GIT_ENV)
    completed = subprocess.run(
        [sys.executable, str(LEDGER), "--repo", str(repo), "--session", "missing", "snapshot"],
        capture_output=True, text=True, env=env, check=False,
    )
    assert completed.returncode == 2 and "cannot read session file" in completed.stderr


def test_check_no_write_leaves_session_untouched(tmp_path):
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_approve(repo, "src/a.py")
    record_gate_skip(repo, "src/a.py")
    before = session_text(repo)
    report = check(repo, "--no-write")
    assert report["completed_stages"] == ["exec"]
    assert session_text(repo) == before
    check(repo)
    assert completed_stages_line(repo) == "- completed_stages: [exec]"


def test_ledger_fixture_session_is_well_formed():
    fixture = REPO_ROOT / "tests" / "skills" / "fixtures" / "sessions" / "evidence-ledger-session.md"
    text = fixture.read_text(encoding="utf-8")
    order = [line for line in text.splitlines() if line.startswith("## ")]
    assert order == [
        "## Problem Description", "## Context", "## Acceptance Criteria", "## Current Phase",
        "## Approved Plan", "## Current Review Packet", "## Review History", "## Files Changed",
        "## Key Related Files", "## Timing Log", "## Evidence Ledger", "## Session Metadata",
    ]
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import evidence_ledger  # noqa: WPS433
    finally:
        sys.path.pop(0)
    led = evidence_ledger.load_ledger(text)
    assert led["schema"] == 1 and led["snapshots"] and led["records"]
    assert "### Attributable Delta" in text
    assert "| Phase | Round | Role | Duration | Dispatch | Reuse | Reads | Unchanged | Tests | Pause | Model | Tokens | Cost |" in text


# ---------- git mode + submodule gitlinks (execution round 5: Step 3.4 gate CRITICALs) ----------


def _chmod_exec(path: Path, executable: bool) -> None:
    mode = path.stat().st_mode
    bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    path.chmod((mode | bits) if executable else (mode & ~bits))


def _hash(repo: Path, rel: str) -> str:
    return _git(repo, "hash-object", rel).stdout.strip()


def test_executable_bit_only_change_invalidates_records_selectors_classify_and_delta(tmp_path):
    """Gate finding #8: `chmod +x` leaves the blob sha unchanged. It must (a)
    invalidate `tests` / `reviewer_approve` records with a mode reason, (b)
    change the selector digest, (c) make `classify` invalidate exec and (d)
    produce a delta row with equal blobs and different modes."""
    repo = make_repo(tmp_path)
    snapshot(repo)                                                    # snap/0
    record_approve(repo, "src/a.py")                                  # id 1
    record_gate_skip(repo, "src/a.py")                                # id 2
    record_tests(repo)                                                # id 3: input src/a.py, selector glob:tests/**
    a_blob = _hash(repo, "src/a.py")
    led = ledger(repo)
    assert led["records"][0]["inputs"]["src/a.py"] == f"100644:{a_blob}"
    old_digest = led["records"][2]["selectors"][0]["members_digest"]
    report = check(repo)
    assert claim(report, "reviewer_approve:")["valid"] and claim(report, "tests:")["valid"]
    assert report["completed_stages"] == ["exec"]

    _chmod_exec(repo / "src" / "a.py", True)                          # content untouched
    assert _hash(repo, "src/a.py") == a_blob
    report = check(repo)                                              # (a)
    assert claim(report, "reviewer_approve:")["reasons"] == ["input mode changed 100644→100755: src/a.py"]
    assert claim(report, "tests:")["reasons"] == ["input mode changed 100644→100755: src/a.py"]
    assert report["completed_stages"] == []
    c = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
            "--result", "PASS", "--closure", "declared", "--provenance", "reused-from:1", "--why", "x", expect=2)
    assert "mode changed 100644→100755" in c.stderr
    out = run_json(repo, "classify", expect=1)                        # (c)
    assert out["exec"] == "invalidated" and out["changed_paths"] == ["src/a.py"]
    assert any("(record 1): changed closure paths ['src/a.py']" in r for r in out["reasons"])

    _chmod_exec(repo / "tests" / "test_a.py", True)                   # selector member: mode only
    assert "selector digest changed: glob:tests/**" in claim(check(repo), "tests:")["reasons"]
    post = snapshot(repo)                                             # snap/1
    record_tests(repo)                                                # id 4, bound to snap/1
    assert ledger(repo)["records"][3]["selectors"][0]["members_digest"] != old_digest   # (b)
    assert ledger(repo)["records"][3]["inputs"]["src/a.py"] == f"100755:{a_blob}"

    delta = run_json(repo, "delta", "--format", "json")               # (d)
    assert [r["path"] for r in delta["rows"]] == ["src/a.py", "tests/test_a.py"]
    row = delta["rows"][0]
    assert row["pre_blob"] == row["post_blob"] == a_blob
    assert (row["pre_mode"], row["post_mode"]) == ("100644", "100755")
    assert "old mode 100644" in delta["patch"] and "new mode 100755" in delta["patch"]
    markdown = run(repo, "delta").stdout
    assert f"| 0 | 1 | `src/a.py` | {a_blob} | {a_blob} | 100644 | 100755 |" in markdown
    assert _git(repo, "rev-parse", f"{post['commit']}:src/a.py").stdout.strip() == a_blob
    assert _git(repo, "ls-tree", post["tree"], "src/a.py").stdout.startswith("100755 blob")

    # restoring the original mode makes the snap/0-bound records byte- and mode-identical again
    _chmod_exec(repo / "src" / "a.py", False)
    _chmod_exec(repo / "tests" / "test_a.py", False)
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["byte-identical"]
    assert report["completed_stages"] == ["exec"]


def test_legacy_identity_without_mode_is_never_reusable(tmp_path):
    """A record whose inputs carry a bare blob sha (ledger written before git
    modes were bound) is treated as uncertain: invalid, never matching, and
    refused as a reuse source."""
    repo = make_repo(tmp_path)
    _write(repo, "src/new.py", "n = 1\n")
    snapshot(repo)
    record_approve(repo, "src/a.py")                                  # id 1 tracked
    record_approve(repo, "src/new.py")                                # id 2 untracked
    led = ledger(repo)
    tracked = led["records"][0]["inputs"]["src/a.py"]                 # "100644:<sha>"
    untracked = led["records"][1]["inputs"]["src/new.py"]             # "<untracked:100644:<sha>>"
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    legacy = session_text(repo).replace(tracked, tracked.split(":", 1)[1]).replace(
        untracked, "<untracked:" + untracked[len("<untracked:100644:"):])
    session.write_text(legacy, encoding="utf-8")
    assert ledger(repo)["records"][0]["inputs"]["src/a.py"] == tracked.split(":", 1)[1]
    report = check(repo)
    assert report["claims"]["reviewer_approve:src/a.py"]["reasons"] == \
        ["input identity lacks git mode (legacy record, not reusable): src/a.py"]
    assert report["claims"]["reviewer_approve:src/new.py"]["reasons"] == \
        ["input identity lacks git mode (legacy record, not reusable): src/new.py"]
    assert report["completed_stages"] == []
    c = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
            "--result", "PASS", "--closure", "declared", "--provenance", "reused-from:1", "--why", "x", expect=2)
    assert "lacks git mode" in c.stderr
    # a fresh record re-binds with the mode and is valid again
    record_approve(repo, "src/a.py")                                  # id 3 supersedes 1
    assert check(repo)["claims"]["reviewer_approve:src/a.py"]["reasons"] == ["byte-identical"]


def add_submodule(tmp_path: Path, repo: Path, name: str = "sub") -> tuple:
    """Real submodule (local clone; no network): returns (first, second)
    commits of the subrepo; the superproject's gitlink points at `second`."""
    sub = tmp_path / "subrepo"
    sub.mkdir()
    _git(sub, "init", "-q")
    _write(sub, "s.txt", "s1\n")
    first = _commit_all(sub, "s1")
    _write(sub, "s.txt", "s2\n")
    second = _commit_all(sub, "s2")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), name)
    _commit_all(repo, "add submodule")
    return first, second


def test_submodule_gitlink_is_snapshotted_and_pointer_move_invalidates(tmp_path):
    """Gate finding #9 (option A): the gitlink is stored in the snapshot tree
    with mode 160000 (a); moving the submodule pointer changes the selector
    digest, invalidates a covering record, makes `classify` invalidate exec
    and yields a delta row (b); the submodule path is a legal input (c)."""
    repo = make_repo(tmp_path)
    first, second = add_submodule(tmp_path, repo)
    head_before = _git(repo, "rev-parse", "HEAD").stdout
    index_before = _git(repo, "ls-files", "-s").stdout
    out = snapshot(repo)                                              # snap/0
    tree = _git(repo, "ls-tree", "-r", out["tree"]).stdout.splitlines()
    assert f"160000 commit {second}\tsub" in tree                     # (a)
    record_approve(repo, "sub")                                       # (c) id 1: explicit gitlink input
    assert ledger(repo)["records"][0]["inputs"]["sub"] == f"160000:{second}"
    record_gate_skip(repo, "sub")                                     # id 2
    record_tests(repo, inputs=(), selectors=("dir:sub", "glob:**"))   # id 3: dependency via selectors only
    report = check(repo)
    assert claim(report, "reviewer_approve:")["valid"] and claim(report, "tests:")["valid"]
    assert report["completed_stages"] == ["exec"]

    _git(repo / "sub", "checkout", "-q", first)                       # (b) worktree pointer moves; index untouched
    assert _git(repo, "ls-files", "-s", "sub").stdout.split()[1] == second
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["changed input: sub"]
    assert claim(report, "tests:")["reasons"] == ["selector digest changed: dir:sub",
                                                  "selector digest changed: glob:**"]
    assert report["completed_stages"] == []
    out = run_json(repo, "classify", expect=1)
    assert out["exec"] == "invalidated" and out["changed_paths"] == ["sub"]
    assert any("(record 1): changed closure paths ['sub']" in r for r in out["reasons"])
    snapshot(repo)                                                    # snap/1
    delta = run_json(repo, "delta", "--format", "json")
    assert delta["rows"] == [{"pre": 0, "post": 1, "path": "sub", "pre_blob": second, "post_blob": first,
                              "pre_mode": "160000", "post_mode": "160000"}]
    assert f"-Subproject commit {second}" in delta["patch"] and f"+Subproject commit {first}" in delta["patch"]
    assert f"| 0 | 1 | `sub` | {second} | {first} | 160000 | 160000 |" in run(repo, "delta").stdout
    # the superproject's HEAD and index were never touched
    assert _git(repo, "rev-parse", "HEAD").stdout == head_before
    assert _git(repo, "ls-files", "-s").stdout == index_before


def test_gitlink_without_checkout_binds_index_pointer_and_tombstones_removal(tmp_path):
    """A gitlink fabricated via `update-index --cacheinfo 160000` (submodule
    never cloned) binds the index pointer; a pointer change in the index
    invalidates a `dir:` selector covering the parent directory; removing the
    directory is a tombstone."""
    repo = make_repo(tmp_path)
    oid1 = _git(repo, "rev-parse", "HEAD").stdout.strip()             # any commit id serves as a pointer
    oid2 = _git(repo, "rev-parse", "HEAD~1").stdout.strip()
    (repo / "vendor" / "lib").mkdir(parents=True)
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{oid1},vendor/lib")
    out = snapshot(repo)
    assert f"160000 commit {oid1}\tvendor/lib" in _git(repo, "ls-tree", "-r", out["tree"]).stdout.splitlines()
    record_tests(repo, inputs=(), selectors=("dir:vendor",))
    assert claim(check(repo), "tests:")["valid"] is True
    _git(repo, "update-index", "--cacheinfo", f"160000,{oid2},vendor/lib")
    assert claim(check(repo), "tests:")["reasons"] == ["selector digest changed: dir:vendor"]
    assert run_json(repo, "classify", expect=1)["changed_paths"] == ["vendor/lib"]
    (repo / "vendor" / "lib").rmdir()
    assert snapshot(repo)["deleted"] == ["vendor/lib"]
    assert claim(check(repo), "tests:")["reasons"] == ["selector digest changed: dir:vendor"]


def test_untracked_nested_repository_is_bound_as_gitlink_and_commit_moves_it(tmp_path):
    """Quality-polish fix 2: `git ls-files --others` lists an untracked nested
    repository as `dir/`; it is bound like a gitlink to the nested HEAD (never
    lstat'ed as a file), a commit inside it changes the identity / selector
    digest, and a nested repository without a commit is listed as skipped."""
    repo = make_repo(tmp_path)
    nested = repo / "vendor" / "nested"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    _write(nested, "lib.py", "x = 1\n")
    first = _commit_all(nested, "nested one")
    empty = repo / "vendor" / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")                                          # no commit: cannot be bound
    assert "vendor/nested/" in _git(repo, "ls-files", "--others", "--exclude-standard").stdout.split()
    out = snapshot(repo)
    assert out["untracked"] == ["vendor/nested"]
    assert out["nested_repos_skipped"] == [{"path": "vendor/empty",
                                            "reason": "nested repository has no commit (unborn HEAD)"}]
    assert f"160000 commit {first}\tvendor/nested" in _git(repo, "ls-tree", "-r", out["tree"]).stdout.splitlines()
    assert ledger(repo)["snapshots"][0]["nested_repos_skipped"][0]["path"] == "vendor/empty"
    record_tests(repo, inputs=("vendor/nested",), selectors=("dir:vendor",))
    rec = ledger(repo)["records"][0]
    assert rec["inputs"]["vendor/nested"] == f"<untracked:160000:{first}>"
    assert claim(check(repo), "tests:")["valid"] is True
    _write(nested, "lib.py", "x = 2\n")
    second = _commit_all(nested, "nested two")
    assert second != first
    report = check(repo)
    assert claim(report, "tests:")["reasons"] == ["changed input: vendor/nested", "selector digest changed: dir:vendor"]
    assert run_json(repo, "classify", expect=1)["changed_paths"] == ["vendor/nested"]
    out = snapshot(repo)
    assert f"160000 commit {second}\tvendor/nested" in _git(repo, "ls-tree", "-r", out["tree"]).stdout.splitlines()


def _dirty_digest(*triples: tuple) -> str:
    """The helper's dirty-content digest: sha256 over the sorted
    `(XY, path, oid)` triples joined by tabs / newlines."""
    import hashlib
    return hashlib.sha256("\n".join(f"{xy}\t{p}\t{oid}" for xy, p, oid in sorted(triples)).encode()).hexdigest()


def _blob(repo: Path, rel: str) -> str:
    return _git(repo, "hash-object", rel).stdout.strip()


def _bound_dirty_submodule(tmp_path: Path) -> tuple:
    """Superproject with a clean submodule at `sub`, snapshotted (snap/0) and
    covered by an approve record (id 1), a gate skip (id 2) and a `tests`
    record with `dir:sub` / `glob:**` selectors (id 3): every claim valid."""
    repo = make_repo(tmp_path)
    _, second = add_submodule(tmp_path, repo)
    snapshot(repo)                                                    # snap/0: clean
    record_approve(repo, "sub")                                       # id 1
    record_gate_skip(repo, "sub")                                     # id 2
    record_tests(repo, inputs=(), selectors=("dir:sub", "glob:**"))   # id 3
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["byte-identical"]
    assert claim(report, "tests:")["reasons"] == ["byte-identical"]
    assert report["completed_stages"] == ["exec"]
    assert ledger(repo)["records"][0]["inputs"]["sub"] == f"160000:{second}"
    return repo, second


def _assert_dirty_invalidates(repo: Path, second: str, expected_identity: str, approve_id: int = 1) -> None:
    """The submodule HEAD is still `second`; the current identity is
    `expected_identity`; the record is invalid (`changed input`), the
    selector digests moved, `classify` invalidates and names the path.
    `approve_id` is the id of the active `reviewer_approve` record that
    `classify` is expected to name (the caller may have re-recorded it)."""
    assert _git(repo / "sub", "rev-parse", "HEAD").stdout.strip() == second
    assert _git(repo, "ls-files", "-s", "sub").stdout.split()[1] == second
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["changed input: sub"]
    assert claim(report, "tests:")["reasons"] == ["selector digest changed: dir:sub",
                                                  "selector digest changed: glob:**"]
    assert report["completed_stages"] == []
    out = run_json(repo, "classify", expect=1)
    assert out["exec"] == "invalidated" and out["changed_paths"] == ["sub"]
    assert any(f"(record {approve_id}): changed closure paths ['sub']" in r for r in out["reasons"])
    c = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "sub", "--input", "sub", "--result",
            "PASS", "--closure", "declared", "--provenance", f"reused-from:{approve_id}", "--why", "x", expect=2)
    assert "not currently valid: changed input: sub" in c.stderr
    out = snapshot(repo)                                              # a snapshot binds the dirty identity
    assert out["gitlink_dirty"] == {"sub": expected_identity.split("+dirty:", 1)[1]}
    assert f"160000 commit {second}\tsub" in _git(repo, "ls-tree", "-r", out["tree"]).stdout.splitlines()
    record_approve(repo, "sub")
    assert ledger(repo)["records"][-1]["inputs"]["sub"] == expected_identity


def test_submodule_modified_tracked_file_with_unchanged_head_invalidates(tmp_path):
    """Gate finding #11 (1): a modified tracked file inside a checked-out
    submodule, HEAD unchanged, changes the identity to
    `160000:<commit>+dirty:<digest>`: the record is invalid, selector digests
    move, `classify` invalidates and the delta row shows the identity change
    (the patch is empty: both trees hold the same commit)."""
    repo, second = _bound_dirty_submodule(tmp_path)
    _write(repo, "sub/s.txt", "dirty\n")
    digest = _dirty_digest((" M", "s.txt", _blob(repo, "sub/s.txt")))
    identity = f"160000:{second}+dirty:{digest}"
    _assert_dirty_invalidates(repo, second, identity, approve_id=1)   # the record from `_bound_dirty_submodule`
    assert ledger(repo)["snapshots"][1]["gitlink_dirty"] == {"sub": digest}
    delta = run_json(repo, "delta", "--format", "json")
    assert delta["rows"] == [{"pre": 0, "post": 1, "path": "sub", "pre_blob": second,
                              "post_blob": f"{second}+dirty:{digest}", "pre_mode": "160000", "post_mode": "160000"}]
    assert delta["patch"] == ""
    assert f"| 0 | 1 | `sub` | {second} | {second}+dirty:{digest} | 160000 | 160000 |" in run(repo, "delta").stdout
    # the new record bound to the dirty identity is valid while that exact dirty state persists
    assert claim(check(repo), "reviewer_approve:")["reasons"] == ["byte-identical"]
    _write(repo, "sub/s.txt", "dirtier\n")                             # different dirty content: different digest
    assert claim(check(repo), "reviewer_approve:")["reasons"] == ["changed input: sub"]


def test_submodule_untracked_file_with_unchanged_head_invalidates(tmp_path):
    """Gate finding #11 (2): a new untracked (non-ignored) file inside the
    submodule, HEAD unchanged, invalidates the same way; an ignored file does
    not participate (as git does)."""
    repo, second = _bound_dirty_submodule(tmp_path)
    _write(repo, "sub/.gitignore", "*.log\n")
    _commit_all(repo / "sub", "ignore logs")
    second = _git(repo / "sub", "rev-parse", "HEAD").stdout.strip()
    _git(repo, "add", "sub")
    _commit_all(repo, "bump sub")
    snapshot(repo)                                                    # rebase the ledger on the new pointer
    record_approve(repo, "sub")
    assert claim(check(repo), "reviewer_approve:")["reasons"] == ["byte-identical"]
    _write(repo, "sub/ignored.log", "noise\n")                          # ignored: still clean
    assert claim(check(repo), "reviewer_approve:")["reasons"] == ["byte-identical"]
    _write(repo, "sub/new.txt", "new\n")
    digest = _dirty_digest(("??", "new.txt", _blob(repo, "sub/new.txt")))
    # the rebased `record_approve` above is id 4: that is the active record `classify` names
    _assert_dirty_invalidates(repo, second, f"160000:{second}+dirty:{digest}", approve_id=4)


def test_submodule_restored_to_clean_is_byte_identical_again(tmp_path):
    """Gate finding #11 (3): removing the dirty content restores the plain
    `160000:<commit>` identity, so the original record is byte-identical
    again and a record bound to the dirty state is now the stale one."""
    repo, second = _bound_dirty_submodule(tmp_path)
    _write(repo, "sub/s.txt", "dirty\n")
    _write(repo, "sub/new.txt", "new\n")
    digest = _dirty_digest((" M", "s.txt", _blob(repo, "sub/s.txt")), ("??", "new.txt", _blob(repo, "sub/new.txt")))
    # the active approve record is still id 1; the helper's own re-record is id 4, bound dirty
    _assert_dirty_invalidates(repo, second, f"160000:{second}+dirty:{digest}", approve_id=1)
    _git(repo / "sub", "checkout", "-q", "--", "s.txt")
    (repo / "sub" / "new.txt").unlink()
    assert _git(repo / "sub", "status", "--porcelain").stdout == ""
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["changed input: sub"]   # active record 4 is the dirty one
    assert claim(report, "tests:")["reasons"] == ["byte-identical"]                  # selectors bound at snap/0
    assert snapshot(repo)["gitlink_dirty"] == {}
    record_approve(repo, "sub")                                       # id 5 rebinds the clean identity
    assert ledger(repo)["records"][-1]["inputs"]["sub"] == f"160000:{second}"
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["byte-identical"]
    assert report["completed_stages"] == ["exec"]


def test_untracked_nested_repository_dirty_file_invalidates_and_recurses(tmp_path):
    """Gate finding #11 (4): an untracked nested repository (not a submodule)
    with a dirty file behaves the same; a nested-nested repository inside it
    is bound recursively (its own commit and dirty digest fold in)."""
    repo = make_repo(tmp_path)
    nested = repo / "vendor" / "nested"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    _write(nested, "lib.py", "x = 1\n")
    first = _commit_all(nested, "nested one")
    snapshot(repo)
    record_tests(repo, inputs=("vendor/nested",), selectors=("dir:vendor",))
    assert ledger(repo)["records"][0]["inputs"]["vendor/nested"] == f"<untracked:160000:{first}>"
    assert claim(check(repo), "tests:")["reasons"] == ["byte-identical"]
    _write(nested, "lib.py", "x = 2\n")                                # dirty tracked file, HEAD unchanged
    assert _git(nested, "rev-parse", "HEAD").stdout.strip() == first
    report = check(repo)
    assert claim(report, "tests:")["reasons"] == ["changed input: vendor/nested", "selector digest changed: dir:vendor"]
    assert run_json(repo, "classify", expect=1)["changed_paths"] == ["vendor/nested"]
    digest = _dirty_digest((" M", "lib.py", _blob(nested, "lib.py")))
    out = snapshot(repo)
    assert out["gitlink_dirty"] == {"vendor/nested": digest}
    assert f"160000 commit {first}\tvendor/nested" in _git(repo, "ls-tree", "-r", out["tree"]).stdout.splitlines()
    record_tests(repo, inputs=("vendor/nested",), selectors=("dir:vendor",))
    assert ledger(repo)["records"][-1]["inputs"]["vendor/nested"] == f"<untracked:160000:{first}+dirty:{digest}>"
    assert claim(check(repo), "tests:")["reasons"] == ["byte-identical"]
    # nested-nested repository: its commit is part of the digest, and so is its own dirty content
    inner = nested / "inner"
    inner.mkdir()
    _git(inner, "init", "-q")
    _write(inner, "i.txt", "i1\n")
    inner_commit = _commit_all(inner, "inner one")
    digest2 = _dirty_digest((" M", "lib.py", _blob(nested, "lib.py")), ("??", "inner", inner_commit))
    assert snapshot(repo)["gitlink_dirty"] == {"vendor/nested": digest2}
    assert claim(check(repo), "tests:")["reasons"] == ["changed input: vendor/nested", "selector digest changed: dir:vendor"]
    _write(inner, "i.txt", "i2\n")
    inner_digest = _dirty_digest((" M", "i.txt", _blob(inner, "i.txt")))
    digest3 = _dirty_digest((" M", "lib.py", _blob(nested, "lib.py")), ("??", "inner", f"{inner_commit}+dirty:{inner_digest}"))
    assert snapshot(repo)["gitlink_dirty"] == {"vendor/nested": digest3}
    assert digest != digest2 != digest3


def test_nested_repository_deeper_than_four_levels_fails_closed(tmp_path):
    """Gate finding #11: nested-nested repositories are recursed up to a depth
    of 4; a fifth level is a storage failure naming the path (exit 3, nothing
    written), and removing it lets the snapshot bind again."""
    repo = make_repo(tmp_path)
    current = repo / "vendor"
    for level in range(1, 6):
        current = current / f"r{level}"
        current.mkdir(parents=True)
        _git(current, "init", "-q")
        _write(current, "f.txt", f"level {level}\n")
        _commit_all(current, f"level {level}")
    completed = run(repo, "snapshot", expect=3)
    assert "STORAGE-ERROR" in completed.stderr
    assert "nested repository vendor/r1/r2/r3/r4/r5: nested more than 4 levels deep" in completed.stderr
    assert "## Evidence Ledger" not in session_text(repo)
    import shutil
    shutil.rmtree(repo / "vendor" / "r1" / "r2" / "r3" / "r4" / "r5")
    out = snapshot(repo)
    assert list(out["gitlink_dirty"]) == ["vendor/r1"]


def test_legacy_plain_gitlink_identity_of_dirty_nested_repo_is_invalid_and_never_reused(tmp_path):
    """Gate finding #11 (5): a record carrying the plain `160000:<commit>`
    identity (written before dirty content was bound) for a submodule that is
    currently dirty reads `changed input` and is refused as a reuse source."""
    repo, second = _bound_dirty_submodule(tmp_path)
    _write(repo, "sub/s.txt", "dirty\n")
    snapshot(repo)                                                    # snap/1 binds the dirty identity
    record_approve(repo, "sub")                                       # id 4: `160000:<commit>+dirty:<digest>`
    bound = ledger(repo)["records"][-1]["inputs"]["sub"]
    assert bound.startswith(f"160000:{second}+dirty:")
    assert claim(check(repo), "reviewer_approve:")["reasons"] == ["byte-identical"]

    def legacy(data: dict) -> None:
        data["records"][-1]["inputs"]["sub"] = f"160000:{second}"
    _rewrite_ledger(repo, legacy)
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["changed input: sub"]
    assert report["completed_stages"] == []
    c = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "sub", "--input", "sub", "--result",
            "PASS", "--closure", "declared", "--provenance", "reused-from:4", "--why", "x", expect=2)
    assert "reused-from source 4 is not currently valid: changed input: sub" in c.stderr
    assert ledger(repo)["records"][-1]["id"] == 4                     # nothing written


def test_submodule_with_broken_git_file_is_a_storage_failure_naming_the_path(tmp_path):
    """Quality-polish fix 4: only an unborn HEAD (exit 1) may fall back to the
    index pointer; `git rev-parse` exit 128 propagates as exit 3."""
    repo = make_repo(tmp_path)
    oid = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "sub").mkdir()
    _write(repo, "sub/.git", "gitdir: /nonexistent/review-loop-broken-gitdir\n")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},sub")
    completed = run(repo, "snapshot", expect=3)
    assert "STORAGE-ERROR" in completed.stderr and "submodule sub" in completed.stderr
    assert "## Evidence Ledger" not in session_text(repo)


def test_unborn_head_repository_snapshots_with_null_head(tmp_path):
    """Quality-polish fix 6: `current_head` treats only exit 1 as unborn."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "src/a.py", "a = 1\n")
    (repo / ".review-loop" / "sessions").mkdir(parents=True)
    (repo / ".review-loop" / "sessions" / f"{UUID}.md").write_text(SESSION_TEMPLATE, encoding="utf-8")
    out = snapshot(repo)
    assert out["untracked"] == ["src/a.py"]
    assert ledger(repo)["snapshots"][0]["head"] is None
    assert check(repo)["head"] is None


def _replace_metadata_prefix(repo: Path, block: str) -> None:
    text = session_text(repo)
    assert text.count("## Session Metadata") == 1
    (repo / ".review-loop" / "sessions" / f"{UUID}.md").write_text(
        text.replace("## Session Metadata", block + "## Session Metadata"), encoding="utf-8")


def test_malformed_now_and_ledger_shape_are_usage_errors(tmp_path):
    """Quality-polish fix 3: user input and ledger shape problems are exit 2
    naming the offending value / record, never a traceback."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    completed = run(repo, "check", "--now", "garbage", expect=2)
    assert "--now 'garbage' is not an ISO-8601 timestamp" in completed.stderr
    record_approve(repo, "src/a.py")                                  # id 1: a reusable source
    completed = run(repo, "record", "--check", "reviewer_approve", "--result", "PASS", "--scope-path", "src/a.py",
                    "--input", "src/a.py", "--provenance", "reused-from:1", "--why", "w", "--now", "nope", expect=2)
    assert "--now 'nope' is not an ISO-8601 timestamp" in completed.stderr
    before = session_text(repo)
    broken = json.dumps({"schema": 1, "scope": [], "snapshots": [], "records": [
        {"id": 7, "claim_id": "tests:pytest", "check": "tests", "recorded_at": "2026-09-15T00:00:00+00:00"},
    ]}, indent=2)
    _replace_metadata_prefix(repo, f"## Evidence Ledger\n\n```json\n{broken}\n```\n\n")
    completed = run(repo, "check", expect=2)
    assert "ledger record 7 lacks required key 'superseded_by'" in completed.stderr
    assert "Traceback" not in completed.stderr
    (repo / ".review-loop" / "sessions" / f"{UUID}.md").write_text(before, encoding="utf-8")


def test_fenceless_ledger_section_is_refused_not_overwritten(tmp_path):
    """Quality-polish fix 7."""
    repo = make_repo(tmp_path)
    _replace_metadata_prefix(repo, "## Evidence Ledger\n\n(hand-written placeholder, no json block)\n\n")
    before = session_text(repo)
    completed = run(repo, "snapshot", expect=2)
    assert "`## Evidence Ledger` section has no ```json block; refusing to overwrite it" in completed.stderr
    assert session_text(repo) == before


def _rewrite_ledger(repo: Path, mutate) -> str:
    """Apply `mutate` to the canonical ledger object and write it back; returns
    the resulting session text."""
    data = ledger(repo)
    mutate(data)
    text = session_text(repo)
    start = text.index("## Evidence Ledger")
    fence = text.index("```json\n", start) + len("```json\n")
    end = text.index("\n```", fence)
    text = text[:fence] + json.dumps(data, indent=2) + text[end:]
    (repo / ".review-loop" / "sessions" / f"{UUID}.md").write_text(text, encoding="utf-8")
    return text


def test_internal_error_exits_3_fail_closed_with_session_unchanged(tmp_path):
    """Quality-polish fix 3: a bug inside the helper is exit 3 + INTERNAL-ERROR,
    never exit 1 (which is a determination), and nothing is written. The
    trigger is a record whose `inputs` is a string — a shape the load-time
    validator does not cover (round 2 M-6 covers timestamps, ttl seconds,
    snapshot keys and triage lists), so evaluation genuinely crashes."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_tests(repo)
    assert claim(check(repo), "tests:")["valid"] is True
    path = repo / ".review-loop" / "sessions" / f"{UUID}.md"

    def corrupt(data):
        assert isinstance(data["records"][0]["inputs"], dict)
        data["records"][0]["inputs"] = "src/a.py"
    text = _rewrite_ledger(repo, corrupt)
    completed = run(repo, "check", expect=3)
    assert "evidence-ledger: INTERNAL-ERROR AttributeError:" in completed.stderr
    assert "(fail closed; nothing written)" in completed.stderr and "Traceback" in completed.stderr
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["records"].__getitem__(0).__setitem__("recorded_at", "yesterday"),
     "ledger record 1 key 'recorded_at' is not an ISO-8601 timestamp"),
    (lambda d: d["records"].__getitem__(0).__setitem__("recorded_at", 5),
     "ledger record 1 key 'recorded_at' must be an ISO-8601 string, got 5"),
    (lambda d: d["records"].__getitem__(0).__setitem__("freshness", {"kind": "ttl", "seconds": "x"}),
     "ledger record 1 key 'freshness.seconds' must be an integer for kind 'ttl', got 'x'"),
    (lambda d: d["snapshots"].__getitem__(0).__setitem__("n", "0"),
     "ledger snapshot '0' key 'n' must be an integer, got '0'"),
    (lambda d: d["snapshots"].__getitem__(0).__setitem__("tree", None),
     "ledger snapshot 0 key 'tree' must be a string, got None"),
    (lambda d: d["snapshots"].__getitem__(0).__setitem__("commit", 7),
     "ledger snapshot 0 key 'commit' must be a string, got 7"),
    (lambda d: d.__setitem__("triage", {"next_id": 1, "disputes": {}, "pending_rubric_incomplete": []}),
     "`triage.disputes` must be a list, got {}"),
    (lambda d: d.__setitem__("triage", {"next_id": 1, "disputes": [], "pending_rubric_incomplete": "x"}),
     "`triage.pending_rubric_incomplete` must be a list, got 'x'"),
])
def test_ledger_shape_validation_names_record_snapshot_and_key(tmp_path, mutate, message):
    """Round 2 M-6: a hand-corrupted timestamp, ttl seconds, snapshot key or
    triage list is exit 2 naming the record / snapshot / key — never an
    INTERNAL-ERROR — and nothing is written."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_tests(repo, extra=["--env-sensitive", "--freshness", "ttl:10"])
    text = _rewrite_ledger(repo, mutate)
    completed = run(repo, "check", expect=2)
    assert message in completed.stderr, completed.stderr
    assert "Traceback" not in completed.stderr and "INTERNAL-ERROR" not in completed.stderr
    assert session_text(repo) == text


def test_prune_refuses_stale_snapshot_lookups_until_a_new_baseline(tmp_path):
    """Quality-polish fix 11: after `prune` every snapshot lookup is a clear
    usage error until `snapshot` stores a new baseline; a failed private-index
    cleanup is reported, not hidden."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    record_approve(repo, "src/a.py")
    out = run_json(repo, "prune")
    assert out["pruned_refs"] == [f"refs/review-loop/{UUID}/snap/0"] and out["index_cleanup"] == "absent"
    for args in (("record", "--check", "reviewer_approve", "--result", "PASS", "--scope-path", "src/a.py",
                  "--input", "src/a.py"), ("classify",), ("delta", "--pre", "0", "--post", "1")):
        completed = run(repo, *args, expect=2)
        assert "snapshots were pruned at" in completed.stderr and "run `snapshot` to store a new baseline" in completed.stderr
    assert run(repo, "route", expect=2).stderr.count("snapshots were pruned at") == 1
    out = snapshot(repo)                                              # new baseline: snap/1
    assert out["snapshot"] == 1 and "pruned_at" not in ledger(repo)
    assert ledger(repo)["snapshots"][0]["pruned_at"]
    assert record_approve(repo, "src/a.py")["snapshot"] == 1
    completed = run(repo, "delta", "--pre", "0", "--post", "1", expect=2)  # snap/0's tree is gone
    assert "snapshots were pruned at" in completed.stderr
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    index_file = git_dir / "review-loop" / f"{UUID}.index"
    index_file.mkdir(parents=True)                                     # unlink() on a directory fails
    (index_file / "keep").write_text("x", encoding="utf-8")
    try:
        out = run_json(repo, "prune")
    finally:
        (index_file / "keep").unlink()
        index_file.rmdir()
    assert out["pruned_refs"] == [f"refs/review-loop/{UUID}/snap/1"] and out["index_cleanup"] == "failed"
    assert str(index_file) in out["index_cleanup_error"]                 # M-5: the OSError text is reported
    assert out["already_pruned"] is False and out["refs_missing"] is False and out["stamped"] == [1]


def test_closure_file_closure_key_is_honoured_unless_overridden(tmp_path):
    """Quality-polish fix 12."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    closure_file = tmp_path / "closure.json"
    closure_file.write_text(json.dumps({"inputs": ["src/a.py"], "closure": "declared"}), encoding="utf-8")
    base = ["record", "--check", "reviewer_approve", "--result", "PASS", "--scope-path", "src/a.py",
            "--closure-file", str(closure_file)]
    assert run_json(repo, *base)["closure"] == "declared"
    assert run_json(repo, *base, "--closure", "uncertain")["closure"] == "uncertain"
    records = ledger(repo)["records"]
    assert [r["closure"] for r in records] == ["declared", "uncertain"]
    assert records[0]["inputs"] == {"src/a.py": records[1]["inputs"]["src/a.py"]}
    closure_file.write_text(json.dumps({"inputs": ["src/a.py"], "closure": "bogus"}), encoding="utf-8")
    assert "closure file `closure` must be one of" in run(repo, *base, expect=2).stderr
    closure_file.write_text(json.dumps({"inputs": ["src/a.py"]}), encoding="utf-8")
    assert run_json(repo, *base)["closure"] == "uncertain"           # no key: the default still applies


def test_session_directory_is_never_in_snapshot_scope(tmp_path):
    """Quality-polish fix 13: untracked `.review-loop/**` (the session file
    the orchestrator rewrites at every step) never enters the snapshot, so
    session writes never appear in classify / delta."""
    repo = tmp_path / "unignored"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "src/a.py", "a = 1\n")
    _commit_all(repo, "base")
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    session.parent.mkdir(parents=True)
    session.write_text(SESSION_TEMPLATE, encoding="utf-8")
    assert ".review-loop/" in _git(repo, "ls-files", "--others", "--exclude-standard", "--directory").stdout
    out = snapshot(repo)
    assert out["untracked"] == [] and out["paths"] == 1
    record_approve(repo, "src/a.py")
    record_gate_skip(repo, "src/a.py")
    assert check(repo)["completed_stages"] == ["exec"]                 # the ledger write itself changed the file
    assert run_json(repo, "classify")["changed_paths"] == []
    _write(repo, ".review-loop/scratch.txt", "orchestrator note\n")
    assert run_json(repo, "classify")["changed_paths"] == []
    assert run_json(repo, "snapshot", "--scope", ".review-loop")["untracked"] == []


# ---------- quality-polish round 2 ----------


def test_prune_stamps_from_the_ledger_even_when_refs_are_already_gone(tmp_path):
    """H-2: a prune whose session write failed leaves the refs deleted and the
    ledger unstamped; the re-run stamps every unstamped snapshot (reporting
    `refs_missing`), so nothing binds to an unreferenced tree afterwards, and
    a further prune is a no-op reporting `already_pruned`."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    _git(repo, "update-ref", "-d", f"refs/review-loop/{UUID}/snap/0")     # the failed-write prune's footprint
    assert "pruned_at" not in ledger(repo)
    out = run_json(repo, "prune")
    assert out == {"pruned_refs": [], "index_cleanup": "absent", "already_pruned": False,
                   "refs_missing": True, "stamped": [0]}
    data = ledger(repo)
    assert data["pruned_at"] and data["snapshots"][0]["pruned_at"] == data["pruned_at"]
    completed = run(repo, "record", "--check", "reviewer_approve", "--result", "PASS", "--scope-path", "src/a.py",
                    "--input", "src/a.py", expect=2)
    assert "snapshots were pruned at" in completed.stderr and "run `snapshot` to store a new baseline" in completed.stderr
    before = session_text(repo)
    out = run_json(repo, "prune")
    assert out == {"pruned_refs": [], "index_cleanup": "absent", "already_pruned": True, "refs_missing": False}
    assert session_text(repo) == before


def test_closure_file_check_key_selectors_and_path_lists_are_validated(tmp_path):
    """M-1: a closure file for another check, a dict selector without `kind`
    / `pattern`, or non-list `inputs` / `deps` is exit 2 naming the problem;
    nothing is recorded."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    closure_file = tmp_path / "closure.json"
    base = ["record", "--check", "reviewer_approve", "--result", "PASS", "--scope-path", "src/a.py",
            "--closure-file", str(closure_file)]
    cases = [
        ({"check": "lint", "inputs": ["src/a.py"]}, "closure file is for check 'lint', not --check 'reviewer_approve'"),
        ({"inputs": ["src/a.py"], "selectors": [{"kind": "glob"}]},
         "closure file `selectors[0]` must carry non-empty `kind` and `pattern`"),
        ({"inputs": ["src/a.py"], "selectors": ["glob:tests/**", {"kind": "", "pattern": "x"}]},
         "closure file `selectors[1]` must carry non-empty `kind` and `pattern`"),
        ({"inputs": ["src/a.py"], "selectors": [7]}, "closure file `selectors[0]` must be an object or a"),
        ({"inputs": "src/a.py"}, "closure file `inputs` must be a list, got 'src/a.py'"),
        ({"inputs": ["src/a.py"], "deps": {"src/b.py": 1}}, "closure file `deps` must be a list"),
        ({"inputs": ["src/a.py", 3]}, "closure file `inputs[1]` must be a non-empty path string, got 3"),
    ]
    for payload, message in cases:
        closure_file.write_text(json.dumps(payload), encoding="utf-8")
        completed = run(repo, *base, expect=2)
        assert message in completed.stderr, (payload, completed.stderr)
    assert ledger(repo)["records"] == []
    closure_file.write_text(json.dumps({"check": "reviewer_approve", "inputs": ["src/a.py"], "deps": [],
                                        "selectors": [{"kind": "glob", "pattern": "src/**"}, "dir:tests"]}),
                            encoding="utf-8")
    out = run_json(repo, *base)
    rec = ledger(repo)["records"][0]
    assert out["id"] == 1 and [(s["kind"], s["pattern"]) for s in rec["selectors"]] == [("glob", "src/**"), ("dir", "tests")]


def test_closure_file_rejects_unknown_top_level_keys(tmp_path):
    """Gate finding #12: a top-level key outside {check, closure, inputs,
    deps, selectors} (e.g. `dependencies` for `deps`) is exit 2 naming the
    key; nothing is recorded, so a typo can never produce a complete-looking
    closure that silently misses its deps."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    closure_file = tmp_path / "closure.json"
    base = ["record", "--check", "reviewer_approve", "--result", "PASS", "--scope-path", "src/a.py",
            "--closure-file", str(closure_file)]
    before = session_text(repo)
    closure_file.write_text(json.dumps({"inputs": ["src/a.py"], "dependencies": ["src/b.py"], "closure": "declared"}),
                            encoding="utf-8")
    completed = run(repo, *base, expect=2)
    assert "closure file: unknown key 'dependencies' (allowed: check, closure, inputs, deps, selectors)" in completed.stderr
    closure_file.write_text(json.dumps({"inputs": ["src/a.py"], "dependencies": [], "Selectors": []}), encoding="utf-8")
    completed = run(repo, *base, expect=2)
    assert "closure file: unknown keys 'Selectors', 'dependencies' (allowed:" in completed.stderr
    assert ledger(repo)["records"] == []
    assert session_text(repo) == before                                # nothing written at all
    # all five allowed keys together still record normally
    closure_file.write_text(json.dumps({"check": "reviewer_approve", "closure": "declared", "inputs": ["src/a.py"],
                                        "deps": ["src/b.py"], "selectors": ["dir:src"]}), encoding="utf-8")
    run_json(repo, *base)
    rec = ledger(repo)["records"][0]
    assert sorted(rec["deps"]) == ["src/b.py"] and sorted(rec["inputs"]) == ["src/a.py"]
    assert rec["closure"] == "declared" and [s["pattern"] for s in rec["selectors"]] == ["src"]


def test_nested_repos_skipped_is_carried_into_classify_and_check(tmp_path):
    """M-3: the unbound region of the last baseline is visible in every
    `classify` / `check` report, not only in the `snapshot` output."""
    repo = make_repo(tmp_path)
    empty = repo / "vendor" / "empty"
    empty.mkdir(parents=True)
    _git(empty, "init", "-q")                                          # unborn HEAD: cannot be bound
    skipped = [{"path": "vendor/empty", "reason": "nested repository has no commit (unborn HEAD)"}]
    assert snapshot(repo)["nested_repos_skipped"] == skipped
    assert run_json(repo, "classify", expect=1)["nested_repos_skipped"] == skipped
    assert check(repo)["nested_repos_skipped"] == skipped
    _write(empty, "lib.py", "x = 1\n")
    _commit_all(empty, "nested one")                                   # now bindable: the next baseline clears it
    assert snapshot(repo)["nested_repos_skipped"] == []
    assert run_json(repo, "classify", expect=1)["nested_repos_skipped"] == []
    assert check(repo)["nested_repos_skipped"] == []


def test_record_now_is_validated_for_fresh_provenance_too(tmp_path):
    """M-4: `--now garbage` is exit 2 before anything else, whatever the
    provenance."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    completed = run(repo, "record", "--check", "reviewer_approve", "--result", "PASS", "--scope-path", "src/a.py",
                    "--input", "src/a.py", "--now", "garbage", expect=2)
    assert "--now 'garbage' is not an ISO-8601 timestamp" in completed.stderr
    assert ledger(repo)["records"] == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_snapshot_withdraws_its_ref_when_the_session_write_fails(tmp_path):
    """M-7: when the session file cannot be written after `update-ref`, the
    just-created ref is deleted before exit 3, so "nothing written" is true,
    the ledger is unchanged and a retry stores the same snap/{n}."""
    repo = make_repo(tmp_path)
    sessions = repo / ".review-loop" / "sessions"
    before = session_text(repo)
    sessions.chmod(0o500)                                              # the atomic-write temp file cannot be created
    try:
        completed = run(repo, "snapshot", expect=3)
    finally:
        sessions.chmod(0o700)
    assert "evidence-ledger: STORAGE-ERROR cannot write session file" in completed.stderr
    assert "(fail closed; nothing written)" in completed.stderr
    assert _git(repo, "for-each-ref", "--format=%(refname)", "refs/review-loop").stdout.split() == []
    assert session_text(repo) == before
    assert snapshot(repo)["snapshot"] == 0                             # retry-safe


# ---------- Step 3.5.5 coverage: nested storage failure, type swaps, lint-closure errors, fences ----------


def _install_ref_deletion_veto(repo: Path) -> Path:
    """A `reference-transaction` hook that refuses every deletion under
    `refs/review-loop/` while creations still succeed: a deterministic,
    monkeypatch-free way to make `git update-ref -d` fail on demand."""
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    hook = git_dir / "hooks" / "reference-transaction"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        '[ "$1" = prepared ] || exit 0\n'
        "while read old new ref; do\n"
        '  case "$new" in 0000000000000000000000000000000000000000)\n'
        '    case "$ref" in refs/review-loop/*) exit 1;; esac;;\n'
        "  esac\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    return hook


def test_snapshot_reports_both_failures_when_the_ref_withdrawal_also_fails(tmp_path):
    """(a) The session write fails AND the compensating `update-ref -d`
    fails: one StorageError carrying both messages, exit 3, ledger unchanged,
    and the ref that could not be withdrawn is named (and still present)."""
    repo = make_repo(tmp_path)
    sessions = repo / ".review-loop" / "sessions"
    before = session_text(repo)
    hook = _install_ref_deletion_veto(repo)
    ref = f"refs/review-loop/{UUID}/snap/0"
    sessions.chmod(0o500)                                              # the atomic-write temp file cannot be created
    try:
        completed = run(repo, "snapshot", expect=3)
    finally:
        sessions.chmod(0o700)
    assert "evidence-ledger: STORAGE-ERROR cannot write session file" in completed.stderr
    assert (
        f"; additionally could not withdraw {ref}: git update-ref -d {ref} failed (exit 128): "
        "fatal: ref updates aborted by hook (fail closed; nothing written)"
    ) in completed.stderr
    assert "INTERNAL-ERROR" not in completed.stderr and "Traceback" not in completed.stderr
    assert session_text(repo) == before
    assert "## Evidence Ledger" not in session_text(repo)
    # exactly what the message says: the ref survived the failed withdrawal
    assert _git(repo, "for-each-ref", "--format=%(refname)", "refs/review-loop").stdout.split() == [ref]
    hook.unlink()
    assert snapshot(repo)["snapshot"] == 0                             # a retry overwrites snap/0 and records it
    assert ledger(repo)["snapshots"][0]["ref"] == ref


def _symlink(repo: Path, rel: str, target: str) -> None:
    full = repo / rel
    if full.is_symlink() or full.exists():
        full.unlink()
    os.symlink(target, full)


def test_symlink_and_regular_file_swaps_with_identical_bytes_invalidate_by_mode(tmp_path):
    """(b) A symlink replaced by a regular file holding the same bytes keeps
    the blob sha and changes only the git mode (120000 → 100644), and back
    again (100644 → 120000): bound evidence is invalidated with a mode
    reason, `classify` reports the closure path and every delta row carries
    equal blobs with differing modes."""
    repo = make_repo(tmp_path)
    _symlink(repo, "src/link", "a.py")                                 # blob bytes: b"a.py"
    _commit_all(repo, "symlink")
    blob = _git(repo, "rev-parse", "HEAD:src/link").stdout.strip()
    snapshot(repo)                                                     # snap/0
    record_approve(repo, "src/link")                                   # id 1, bound as a symlink
    assert ledger(repo)["records"][0]["inputs"]["src/link"] == f"120000:{blob}"
    assert claim(check(repo), "reviewer_approve:")["reasons"] == ["byte-identical"]

    (repo / "src" / "link").unlink()
    (repo / "src" / "link").write_bytes(b"a.py")                       # same bytes, now a regular file
    assert _hash(repo, "src/link") == blob
    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["input mode changed 120000→100644: src/link"]
    assert report["completed_stages"] == []
    out = run_json(repo, "classify", expect=1)
    assert out["changed_paths"] == ["src/link"]
    assert any("(record 1): changed closure paths ['src/link']" in r for r in out["reasons"])
    snapshot(repo)                                                     # snap/1
    record_approve(repo, "src/link")                                   # id 2, bound as a regular file
    assert ledger(repo)["records"][1]["inputs"]["src/link"] == f"100644:{blob}"
    assert claim(check(repo), "reviewer_approve:")["reasons"] == ["byte-identical"]
    delta = run_json(repo, "delta", "--format", "json")
    assert [(r["path"], r["pre_blob"], r["post_blob"], r["pre_mode"], r["post_mode"]) for r in delta["rows"]] == [
        ("src/link", blob, blob, "120000", "100644")
    ]
    assert "deleted file mode 120000" in delta["patch"] and "new file mode 100644" in delta["patch"]

    _symlink(repo, "src/link", "a.py")                                 # regular file → symlink, same bytes
    report = check(repo)
    assert claim(report, "reviewer_approve:")["id"] == 2
    assert claim(report, "reviewer_approve:")["reasons"] == ["input mode changed 100644→120000: src/link"]
    assert run_json(repo, "classify", expect=1)["changed_paths"] == ["src/link"]
    snapshot(repo)                                                     # snap/2
    delta = run_json(repo, "delta", "--pre", "1", "--post", "2", "--format", "json")
    assert [(r["path"], r["pre_blob"], r["post_blob"], r["pre_mode"], r["post_mode"]) for r in delta["rows"]] == [
        ("src/link", blob, blob, "100644", "120000")
    ]
    markdown = run(repo, "delta", "--pre", "1", "--post", "2").stdout
    assert f"| 1 | 2 | `src/link` | {blob} | {blob} | 100644 | 120000 |" in markdown
    assert run_json(repo, "delta", "--pre", "0", "--post", "2", "--format", "json")["rows"] == []


def test_tracked_file_replaced_by_directory_unbinds_the_path_and_invalidates(tmp_path):
    """(b) A tracked regular file replaced by a directory is not content the
    ledger binds (`worktree_state` skips it): the path is absent from the
    snapshot identity map while its new children are bound as untracked,
    evidence bound to the file is invalidated, `classify` reports the closure
    path, no new evidence can be bound to the path, and `delta` shows the
    removal next to the added child."""
    repo = make_repo(tmp_path)
    snapshot(repo)                                                     # snap/0
    record_approve(repo, "src/a.py")                                   # id 1
    record_tests(repo)                                                 # id 2: input src/a.py
    a_blob = _hash(repo, "src/a.py")
    (repo / "src" / "a.py").unlink()
    (repo / "src" / "a.py").mkdir()
    _write(repo, "src/a.py/inner.py", "inner = 1\n")

    report = check(repo)
    assert claim(report, "reviewer_approve:")["reasons"] == ["input deleted: src/a.py"]
    assert claim(report, "tests:")["reasons"] == ["input deleted: src/a.py"]
    assert report["completed_stages"] == []
    out = run_json(repo, "classify", expect=1)
    assert out["changed_paths"] == ["src/a.py", "src/a.py/inner.py"]
    assert any("(record 1): changed closure paths ['src/a.py']" in r for r in out["reasons"])

    post = snapshot(repo)                                              # snap/1
    assert post["deleted"] == []                                       # still on disk (as a directory): no tombstone
    assert post["untracked"] == ["src/a.py/inner.py"]
    tree = _git(repo, "ls-tree", "-r", "--name-only", post["tree"]).stdout.split()
    assert "src/a.py" not in tree and "src/a.py/inner.py" in tree
    c = run(repo, "record", "--check", "reviewer_approve", "--scope-path", "src/a.py", "--input", "src/a.py",
            "--result", "PASS", "--closure", "declared", expect=2)
    assert "input path 'src/a.py' is neither in snapshot 1 nor a recorded tombstone" in c.stderr

    delta = run_json(repo, "delta", "--format", "json")
    rows = {r["path"]: (r["pre_blob"], r["post_blob"], r["pre_mode"], r["post_mode"]) for r in delta["rows"]}
    assert rows["src/a.py"] == (a_blob, "<absent>", "100644", "<absent>")
    assert rows["src/a.py/inner.py"] == ("<absent>", _hash(repo, "src/a.py/inner.py"), "<absent>", "100644")
    assert set(rows) == {"src/a.py", "src/a.py/inner.py"}
    assert "deleted file mode 100644" in delta["patch"]
    assert f"| 0 | 1 | `src/a.py` | {a_blob} | <absent> | 100644 | <absent> |" in run(repo, "delta").stdout


def test_lint_closure_malformed_contract_json_is_a_usage_error_naming_the_file(tmp_path):
    """(c) A contract file that is not JSON: exit 2, message names the file,
    no traceback, nothing printed or written."""
    repo = make_repo(tmp_path)
    _write(repo, "tests/skills/contracts/broken.json", "{not json")
    before = session_text(repo)
    c = run(repo, "lint-closure", expect=2)
    assert c.stderr.startswith("evidence-ledger: ERROR cannot read contract broken.json: ")
    assert "Traceback" not in c.stderr and c.stdout == ""
    assert session_text(repo) == before


def test_lint_closure_unreadable_contracts_dir_or_file_is_a_usage_error(tmp_path):
    """(c) A contracts directory (or a single contract) that cannot be read
    is a named UsageError (exit 2), never an INTERNAL-ERROR traceback."""
    if os.geteuid() == 0:
        pytest.skip("permission bits do not apply to root")
    repo = make_repo(tmp_path)
    contracts = repo / "tests" / "skills" / "contracts"
    _write(repo, "tests/skills/contracts/demo.json", "{}")
    contracts.chmod(0)
    try:
        c = run(repo, "lint-closure", expect=2)
    finally:
        contracts.chmod(0o755)
    assert c.stderr.startswith(f"evidence-ledger: ERROR cannot read contracts directory {contracts.resolve()}: ")
    assert "Permission denied" in c.stderr
    assert "INTERNAL-ERROR" not in c.stderr and "Traceback" not in c.stderr and c.stdout == ""
    (contracts / "demo.json").chmod(0)
    try:
        c = run(repo, "lint-closure", expect=2)
    finally:
        (contracts / "demo.json").chmod(0o644)
    assert c.stderr.startswith("evidence-ledger: ERROR cannot read contract demo.json: ")
    assert "Permission denied" in c.stderr and "Traceback" not in c.stderr
    assert run_json(repo, "lint-closure")["check"] == "lint"           # readable again → normal output


def _fenced(block: str) -> str:
    return "```markdown\n" + block + "```\n\n"


def test_route_ignores_fenced_route_facts_inside_the_packet(tmp_path):
    """(d) Fenced code inside `## Current Review Packet` is opaque: a
    `### Route Facts` block that exists only inside a fence is "absent"
    (executor, exit 1); with a fenced fake block and a real block the real
    one decides (they differ on `secrets`); fenced rows after the real block
    are not part of it. The same applies to an explicit `--facts` file."""
    repo = make_repo(tmp_path)
    snapshot(repo)
    session = repo / ".review-loop" / "sessions" / f"{UUID}.md"
    base = session_text(repo)
    packet = "## Current Review Packet\n\n### Author route\nexecutor\n\n"

    def write_packet(body: str) -> None:
        session.write_text(base.replace("## Review History\n", packet + body + "## Review History\n"), encoding="utf-8")

    # only a fenced block → absent
    write_packet(_fenced(route_facts()))
    report = run_json(repo, "route", expect=1)
    assert report["route"] == "executor"
    assert report["reasons"][0] == "route facts block absent from Current Review Packet"
    assert report["facts"] == {name: "missing" for name in ELIGIBILITY_FACTS}
    # fenced all-eligible block first, real block with secrets: true after it → the real one was read
    write_packet(_fenced(route_facts()) + route_facts({"secrets": "true"}) + "\n")
    report = run_json(repo, "route", expect=1)
    assert report["flags"]["secrets"] == "true"
    assert report["reasons"] == ["sensitive flag secrets is true, not false"]
    # real all-eligible block, then fenced rows inside the same body (a duplicate `secrets` row and a
    # quoted heading) → neither "listed twice" nor a truncated body: orchestrator-direct
    write_packet(route_facts() + "\n" + _fenced("### Route Facts\n| secrets | true | quoted example |\n"))
    report = run_json(repo, "route", expect=0)
    assert report["route"] == "orchestrator-direct" and report["flags"]["secrets"] == "false"
    # an explicit --facts file follows the same rule
    assert route(repo, tmp_path, _fenced(route_facts()) + route_facts({"secrets": "true"}), expect=1)["flags"]["secrets"] == "true"
    c = run(repo, "route", "--facts", write_facts(tmp_path, _fenced(route_facts())), expect=2)
    assert "no `### Route Facts` block found" in c.stderr
