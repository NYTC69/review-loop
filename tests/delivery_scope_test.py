"""Delivery manifests use disposable repositories; source-repo Git is untouched."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "delivery_scope.py"
ROOT = SCRIPT.parents[1]
SPEC = importlib.util.spec_from_file_location("delivery_scope", SCRIPT)
SCOPE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCOPE)
GIT_ENV = {
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}


def git(repo, *args, input=None, check=True):
    return subprocess.run(["git", *args], cwd=repo, env={**os.environ, **GIT_ENV},
                          input=input, capture_output=True, check=check)


def write(repo, path, content):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode() if isinstance(content, str) else content)
    return target


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    write(root, "task.txt", "base\n")
    write(root, "unrelated.txt", "unrelated base\n")
    write(root, "delete.txt", "remove me\n")
    write(root, ".gitignore", "artifacts/\nignored.txt\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    return root


def run(repo, *args, expect=0, env=None):
    result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), *map(str, args)],
                            env={**os.environ, **GIT_ENV, **(env or {})}, capture_output=True, text=True)
    assert result.returncode == expect, result.stderr
    return json.loads(result.stdout) if result.stdout else json.loads(result.stderr) if expect else None


def baseline(repo, *scope):
    output = repo.parent / "baseline.json"
    run(repo, "capture", *[item for path in scope for item in ("--scope", path)], "--output", output)
    return output, json.loads(output.read_text())


def manifest(repo, source):
    output = repo.parent / "candidate.json"
    run(repo, "manifest", "--baseline", source, "--output", output)
    return output, json.loads(output.read_text())


def paths(rows):
    return {row["path"] for row in rows}


def admin_contents(repo):
    return {str(path.relative_to(repo / ".git")): path.read_bytes()
            for path in (repo / ".git").rglob("*") if path.is_file()}


def test_dirty_same_file_and_prestaged_unrelated_are_separate_and_read_only(repo):
    write(repo, "task.txt", "base\nuser draft\n")
    write(repo, "unrelated.txt", "user staged\n")
    git(repo, "add", "unrelated.txt")
    write(repo, "unrelated.txt", "user staged\nuser unstaged\n")
    write(repo, "user note.txt", "existing untracked\n")
    metadata = admin_contents(repo)
    source, base = baseline(repo, "task.txt", "new.txt", "delete.txt")
    write(repo, "task.txt", "base\nuser draft\ntask addition\n")
    write(repo, "new.txt", "new task file\n")
    (repo / "delete.txt").unlink()
    target, result = manifest(repo, source)
    assert paths(result["baseline_changes"]["staged"]) == {"unrelated.txt"}
    assert paths(result["baseline_changes"]["unstaged"]) == {"task.txt", "unrelated.txt"}
    assert result["baseline_changes"]["untracked"] == ["user note.txt"]
    assert paths(result["task_delta"]) == {"task.txt", "new.txt", "delete.txt"}
    row = next(row for row in result["task_delta"] if row["path"] == "task.txt")
    assert row["before"]["worktree"] == base["state"]["worktree"]["task.txt"]
    assert row["before"]["worktree"]["oid"] != row["before"]["head_paths"]["oid"]
    assert row["ownership"] == "ambiguous-baseline-overlap"
    assert result["ownership_ambiguities"] == ["task.txt"]
    assert result["outside_scope_delta"] == []
    assert result["current"]["index"] == base["state"]["index"]
    assert run(repo, "check", "--manifest", target)["fresh"] is True
    assert admin_contents(repo) == metadata


def test_scopes_are_literal_prefixes_and_include_missing_deleted_and_untracked(repo):
    write(repo, "src/old.txt", "old untracked\n")
    source, _ = baseline(repo, "src/", "delete.txt", "literal*.txt")
    (repo / "src/old.txt").unlink()
    (repo / "delete.txt").unlink()
    write(repo, "src/new.txt", "new\n")
    write(repo, "src-other/new.txt", "outside\n")
    write(repo, "literal*.txt", "literal star\n")
    write(repo, "literal-other.txt", "not a glob\n")
    _, result = manifest(repo, source)
    assert paths(result["task_delta"]) == {"src/old.txt", "src/new.txt", "delete.txt", "literal*.txt"}
    assert paths(result["outside_scope_delta"]) == {"src-other/new.txt", "literal-other.txt"}
    assert result["task_declared_scope"] == ["delete.txt", "literal*.txt", "src"]
    assert result["ownership_ambiguities"] == ["src/old.txt"]
    assert next(row for row in result["task_delta"] if row["path"] == "delete.txt")["after"]["worktree"] is None


def test_index_only_delta_and_index_only_staleness(repo):
    source, base = baseline(repo, "task.txt")
    write(repo, "task.txt", "task staged\n")
    git(repo, "add", "task.txt")
    write(repo, "task.txt", "base\n")
    target, result = manifest(repo, source)
    row = result["task_delta"][0]
    assert row["path"] == "task.txt" and row["before"]["worktree"] == row["after"]["worktree"]
    assert row["before"]["index"] != row["after"]["index"]
    assert result["index_file_changed"] is True
    git(repo, "add", "task.txt")
    stale = run(repo, "check", "--manifest", target, expect=1)
    assert stale["fresh"] is False and "index" in stale["changed_components"]
    assert "worktree" not in stale["changed_components"]
    assert base["state"]["index"] == result["baseline"]["state"]["index"]


@pytest.mark.parametrize("mutation", ["outside", "untracked", "mode", "flags", "head"])
def test_candidate_staleness_binds_all_observed_state(repo, mutation):
    source, _ = baseline(repo, "task.txt")
    write(repo, "task.txt", "task\n")
    target, result = manifest(repo, source)
    assert run(repo, "manifest", "--baseline", source)["fingerprint"] == result["fingerprint"]
    if mutation == "outside":
        write(repo, "unrelated.txt", "later edit outside scope\n")
    elif mutation == "untracked":
        write(repo, "later.txt", "later untracked\n")
    elif mutation == "mode":
        (repo / "task.txt").chmod(0o755)
    elif mutation == "flags":
        git(repo, "update-index", "--assume-unchanged", "unrelated.txt")
    else:
        git(repo, "commit", "--allow-empty", "-qm", "new head")
    assert run(repo, "check", "--manifest", target, expect=1)["fresh"] is False


def test_raw_binary_unusual_names_and_symlink_targets(repo):
    source, _ = baseline(repo, ".")
    content = b"\x00\xff\r\nbinary\x80"
    names = ["space name.bin", "tab\tname.bin", "line\nname.bin", "-option.bin", "日本語.bin"]
    for name in names:
        write(repo, name, content)
    outside = write(repo.parent, "outside.txt", "contents must not be hashed as the link\n")
    (repo / "link").symlink_to(outside)
    target, result = manifest(repo, source)
    for name in names:
        entry = result["current"]["worktree"][name]
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
        assert entry["oid"] == git(repo, "hash-object", "--stdin", input=content).stdout.decode().strip()
    link = result["current"]["worktree"]["link"]
    assert link["mode"] == "120000"
    assert link["sha256"] == hashlib.sha256(os.fsencode(str(outside))).hexdigest()
    outside.write_text("changing the target does not change the link\n")
    assert run(repo, "check", "--manifest", target)["fresh"] is True


def test_tracked_directory_replaced_by_external_symlink_never_reads_target(repo):
    write(repo, "folder/tracked.txt", "tracked\n")
    git(repo, "add", "folder/tracked.txt")
    git(repo, "commit", "-qm", "folder")
    source, _ = baseline(repo, "folder")
    (repo / "folder/tracked.txt").unlink()
    (repo / "folder").rmdir()
    outside = repo.parent / "outside"
    outside.mkdir()
    os.mkfifo(outside / "tracked.txt")  # Following this target would hang or fail.
    (repo / "folder").symlink_to(outside, target_is_directory=True)
    _, result = manifest(repo, source)
    assert result["current"]["worktree"]["folder"]["mode"] == "120000"
    assert "folder/tracked.txt" not in result["current"]["worktree"]


@pytest.mark.parametrize("value", ["../outside", "src/../../outside", "/tmp/outside", ".git/config", "src/.git/HEAD"])
def test_rejects_unsafe_scope(repo, value):
    assert run(repo, "capture", "--scope", value, expect=2)["kind"] == "usage"


def test_rejects_scope_through_symlink_but_allows_leaf_symlink(repo):
    outside = repo.parent / "outside"
    outside.mkdir()
    (repo / "link").symlink_to(outside, target_is_directory=True)
    assert "symlink" in run(repo, "capture", "--scope", "link/file", expect=2)["error"]
    assert "link" in run(repo, "capture", "--scope", "link")["state"]["worktree"]


def test_ignored_outputs_and_ignored_scope(repo):
    (repo / "artifacts").mkdir()
    output = repo / "artifacts/baseline.json"
    run(repo, "capture", "--scope", "task.txt", "--output", output)
    assert "artifacts/baseline.json" not in json.loads(output.read_text())["state"]["worktree"]
    assert "immutable" in run(repo, "capture", "--scope", "task.txt", "--output", output, expect=2)["error"]
    assert "Git-ignored" in run(repo, "capture", "--scope", ".", "--output", repo / "candidate.json", expect=2)["error"]
    assert "administrative" in run(repo, "capture", "--scope", ".", "--output", repo / ".git/refs/new", expect=2)["error"]
    assert "ignored" in run(repo, "capture", "--scope", "ignored.txt", expect=2)["error"]


def test_unborn_index_and_staged_deletion_still_on_disk(repo, tmp_path):
    git(repo, "rm", "--cached", "delete.txt")
    source, _ = baseline(repo, "delete.txt")
    _, result = manifest(repo, source)
    assert "delete.txt" in paths(result["baseline_changes"]["staged"])
    assert "delete.txt" in result["baseline_changes"]["untracked"]
    assert result["task_delta"] == []
    empty = tmp_path / "unborn"
    empty.mkdir()
    git(empty, "init", "-q")
    write(empty, "new.txt", "new\n")
    git(empty, "add", "new.txt")
    base = run(empty, "capture", "--scope", ".")
    assert base["state"]["head"] is None and base["state"]["head_paths"] == {}
    assert base["state"]["index"]["new.txt"][0]["stage"] == 0


def test_conflicted_index_retains_all_stages(repo):
    oid = git(repo, "rev-parse", "HEAD:task.txt").stdout.strip()
    payload = b"0 " + b"0" * 40 + b"\ttask.txt\0"
    payload += b"".join(b"100644 " + oid + b" " + str(stage).encode() + b"\ttask.txt\0" for stage in (1, 2, 3))
    git(repo, "update-index", "-z", "--index-info", input=payload)
    source, base = baseline(repo, "task.txt")
    _, result = manifest(repo, source)
    assert [entry["stage"] for entry in base["state"]["index"]["task.txt"]] == [1, 2, 3]
    assert result["baseline_changes"]["conflicted"] == ["task.txt"]
    assert "task.txt" not in paths(result["baseline_changes"]["staged"])
    assert result["ownership_ambiguities"] == ["task.txt"]


def test_rejects_nested_repository_and_gitlink(repo):
    nested = repo / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    assert "nested repositories" in run(repo, "capture", "--scope", ".", expect=3)["error"]
    # Keep the nested repo, but make it a tracked gitlink to exercise that branch.
    oid = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{oid},nested")
    assert "gitlinks" in run(repo, "capture", "--scope", ".", expect=3)["error"]


def test_artifact_tampering_wrong_repo_and_inconsistent_capture(repo, monkeypatch):
    source, base = baseline(repo, "task.txt")
    base["scope"] = ["../outside"]
    source.write_text(json.dumps(base))
    assert "fingerprint" in run(repo, "manifest", "--baseline", source, expect=2)["error"]
    source.write_text(json.dumps(SCOPE.seal(base)))
    assert "unsafe" in run(repo, "manifest", "--baseline", source, expect=2)["error"]
    base["scope"] = ["task.txt"]
    base["repository"]["worktree"] = "/different/repository"
    source.write_text(json.dumps(SCOPE.seal(base)))
    assert "different repository" in run(repo, "manifest", "--baseline", source, expect=2)["error"]
    states = iter([{"head": "first"}, {"head": "second"}])
    monkeypatch.setattr(SCOPE, "read_state", lambda _: next(states))
    with pytest.raises(SCOPE.CaptureError, match="changed during capture"):
        SCOPE.capture_state({})


def test_environment_cannot_redirect_capture_to_another_index(repo):
    alternative = repo.parent / "alternative-index"
    alternative.write_bytes(b"invalid index data")
    result = run(repo, "capture", "--scope", ".", env={"GIT_INDEX_FILE": str(alternative)})
    assert "task.txt" in result["state"]["index"]
    assert alternative.read_bytes() == b"invalid index data"


def test_review_packet_contract_binds_the_same_candidate_identity():
    session_contract = (ROOT / "docs/protocol/session-file.md").read_text()
    assert "| Delivery candidate |" in session_contract
    for field in ("baseline path", "manifest path", "baseline fingerprint",
                  "candidate fingerprint", "declared scope", "ownership ambiguity"):
        assert field in session_contract


def test_untracked_session_directory_is_excluded_but_tracked_config_is_not(repo):
    write(repo, ".review-loop/config.md", "auto_commit: false\n")
    git(repo, "add", ".review-loop/config.md")
    git(repo, "commit", "-qm", "config")
    sessions = repo / ".review-loop" / "sessions"
    sessions.mkdir(parents=True)
    write(repo, ".review-loop/sessions/s.md", "round 1\n")
    out = sessions / "baseline.json"
    run(repo, "capture", "--scope", "task.txt", "--output", out)
    base = json.loads(out.read_text())
    assert not any(path.startswith(".review-loop/sessions/") for path in base["state"]["untracked"])
    write(repo, ".review-loop/sessions/s.md", "round 2\n")
    write(repo, "task.txt", "task\n")
    candidate_path = sessions / "candidate.json"
    run(repo, "manifest", "--baseline", out, "--output", candidate_path)
    candidate = json.loads(candidate_path.read_text())
    assert candidate["outside_scope_delta"] == []
    assert paths(candidate["task_delta"]) == {"task.txt"}
    write(repo, ".review-loop/config.md", "auto_commit: true\n")
    second = sessions / "candidate-2.json"
    run(repo, "manifest", "--baseline", out, "--output", second)
    assert paths(json.loads(second.read_text())["outside_scope_delta"]) == {".review-loop/config.md"}



def test_from_commit_baseline_is_a_clean_checkout_of_the_base(repo):
    """LG1-c (review-only-entry.md §4): the base tree, not the live tree that already holds the change under review."""
    base = git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    write(repo, "task.txt", "committed change\n")
    os.symlink("task.txt", repo / "link")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "committed part of the change")
    write(repo, "unrelated.txt", "uncommitted change\n")
    write(repo, "new.txt", "untracked part\n")
    info = SCOPE.repository(str(repo))
    state = SCOPE.commit_state(info, base)
    SCOPE.validate_state(state, info)
    assert (state["head"], state["untracked"], state["index_file_sha256"]) == (base, [], None)
    assert set(state["worktree"]) == {"task.txt", "unrelated.txt", "delete.txt", ".gitignore"}
    assert state["worktree"]["task.txt"]["sha256"] == hashlib.sha256(b"base\n").hexdigest()
    assert state["worktree"]["task.txt"]["size"] == len(b"base\n")
    assert state["index"]["task.txt"] == [dict(state["head_paths"]["task.txt"], stage=0)]
    document = run(repo, "capture", "--scope", ".", "--from-commit", base)
    path = repo.parent / "from-commit.json"
    path.write_text(json.dumps(document))
    manifest = SCOPE.build_manifest(SCOPE.load_document(str(path), "delivery-baseline"), SCOPE.capture_state(info))
    owned = dict((row["path"], row["ownership"]) for row in manifest["task_delta"])
    assert owned == dict.fromkeys(("task.txt", "link", "unrelated.txt", "new.txt"), "declared-post-baseline")
    assert run(repo, "capture", "--scope", ".", "--from-commit", "no-such-ref", expect=2)["kind"] == "usage"
