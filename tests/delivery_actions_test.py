"""W04 ownership-safe delivery action tests in disposable repositories."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts import delivery_actions as actions
from scripts import delivery_scope as ds


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.STDOUT)


def fixture_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / ".gitignore").write_text(".review-loop/\n")
    (root / "src.py").write_text("base\n")
    (root / "other.txt").write_text("other\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root


def make_baseline(repo, scope=("src.py",)):
    info = ds.repository(str(repo))
    normalized_scope = ds.scope_paths(info["worktree"], list(scope))
    return ds.build_baseline(info, normalized_scope, ds.capture_state(info))


def make_manifest(repo, tmp_path, baseline):
    info = ds.repository(str(repo))
    path = tmp_path / ("candidate-" + str(len(list(tmp_path.glob("candidate-*.json")))) + ".json")
    path.write_text(json.dumps(ds.build_manifest(baseline, ds.capture_state(info))))
    return path


def test_plan_lists_only_declared_task_delta_and_does_not_change_index(tmp_path):
    root = fixture_repo(tmp_path)
    baseline = make_baseline(root, ("src.py", "new.py"))
    (root / "src.py").write_text("task change\n")
    (root / "new.py").write_text("task addition\n")
    manifest = make_manifest(root, tmp_path, baseline)
    before = ds.capture_state(ds.repository(str(root)))
    plan = actions.compute_plan(str(root), str(manifest))
    after = ds.capture_state(ds.repository(str(root)))
    assert plan["eligible"] is True
    assert plan["paths"] == ["new.py", "src.py"]
    assert before["index"] == after["index"]


def test_plan_blocks_outside_scope_and_baseline_overlap(tmp_path):
    root = fixture_repo(tmp_path)
    baseline = make_baseline(root, ("src.py",))
    (root / "src.py").write_text("task change\n")
    (root / "other.txt").write_text("outside edit\n")
    manifest = make_manifest(root, tmp_path, baseline)
    outside = actions.compute_plan(str(root), str(manifest))
    assert outside["eligible"] is False
    assert any("outside-scope" in reason for reason in outside["reasons"])

    second = tmp_path / "second"
    second.mkdir()
    root2 = fixture_repo(second)
    (root2 / "src.py").write_text("preexisting staged\n")
    git(root2, "add", "src.py")
    baseline2 = make_baseline(root2, ("src.py",))
    (root2 / "src.py").write_text("preexisting staged plus task\n")
    manifest2 = tmp_path / "second-manifest.json"
    manifest2.write_text(json.dumps(ds.build_manifest(baseline2, ds.capture_state(ds.repository(str(root2))))))
    plan2 = actions.compute_plan(str(root2), str(manifest2))
    assert plan2["eligible"] is False
    assert any("ownership" in reason or "staged" in reason for reason in plan2["reasons"])


def test_plan_blocks_content_transform_attributes(tmp_path):
    root = fixture_repo(tmp_path)
    (root / ".gitattributes").write_text("*.py filter=custom\n")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-qm", "attributes")
    baseline = make_baseline(root, ("src.py",))
    (root / "src.py").write_text("task change\n")
    manifest = make_manifest(root, tmp_path, baseline)
    plan = actions.compute_plan(str(root), str(manifest))
    assert plan["eligible"] is False
    assert any("attributes" in reason for reason in plan["reasons"])


def test_plan_blocks_core_autocrlf_content_transformation(tmp_path):
    root = fixture_repo(tmp_path)
    git(root, "config", "core.autocrlf", "true")
    baseline = make_baseline(root, ("src.py",))
    (root / "src.py").write_text("task change\n")
    manifest = make_manifest(root, tmp_path, baseline)
    plan = actions.compute_plan(str(root), str(manifest))
    assert plan["eligible"] is False
    assert any("attributes" in reason for reason in plan["reasons"])


def test_commit_rechecks_gate_and_only_commits_owned_paths(tmp_path, monkeypatch):
    root = fixture_repo(tmp_path)
    (root / ".review-loop").mkdir()
    (root / ".review-loop/config.md").write_text("auto_commit: true\n")
    (root / "user-draft.txt").write_text("pre-task user work\n")
    baseline = make_baseline(root, ("src.py", "new.py", "other.txt"))
    (root / "src.py").write_text("task change\n")
    (root / "new.py").write_text("task addition\n")
    (root / "other.txt").unlink()
    manifest = make_manifest(root, tmp_path, baseline)
    monkeypatch.setattr(actions, "_evaluate_delivery_gate",
                        lambda *args: {"eligible": True, "reasons": [],
                                       "manifest_fingerprint": json.loads(manifest.read_text())["fingerprint"]})
    result = actions.commit(str(root), str(manifest), str(tmp_path / "session.md"), "task: scoped")
    assert result["status"] == "committed"
    assert result["paths"] == ["new.py", "other.txt", "src.py"]
    committed = set(git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").decode().splitlines())
    assert committed == {"new.py", "other.txt", "src.py"}
    assert (root / "new.py").read_text() == "task addition\n"
    assert not (root / "other.txt").exists()
    assert (root / "user-draft.txt").read_text() == "pre-task user work\n"
    assert "user-draft.txt" in git(root, "status", "--porcelain").decode()
    assert git(root, "diff", "--cached", "--name-only").decode().strip() == ""
    assert result["post_commit_worktree_drift"] is False


def test_commit_uses_manifest_blob_if_worktree_changes_after_final_hash_check(tmp_path, monkeypatch):
    root = fixture_repo(tmp_path)
    (root / ".review-loop").mkdir()
    (root / ".review-loop/config.md").write_text("auto_commit: true\n")
    baseline = make_baseline(root, ("src.py",))
    (root / "src.py").write_text("reviewed task content\n")
    manifest = make_manifest(root, tmp_path, baseline)
    monkeypatch.setattr(actions, "_evaluate_delivery_gate",
                        lambda *args: {"eligible": True, "reasons": [],
                                       "manifest_fingerprint": json.loads(manifest.read_text())["fingerprint"]})
    real_git = actions._git
    def mutate_after_final_check(repo_arg, *args, **kwargs):
        if args and args[0] == "commit-tree":
            (root / "src.py").write_text("concurrent user edit\n")
        return real_git(repo_arg, *args, **kwargs)
    monkeypatch.setattr(actions, "_git", mutate_after_final_check)
    result = actions.commit(str(root), str(manifest), str(tmp_path / "session.md"), "task: verified blob")
    committed = git(root, "show", "HEAD:src.py").decode()
    assert committed == "reviewed task content\n"
    assert (root / "src.py").read_text() == "concurrent user edit\n"
    assert result["post_commit_worktree_drift"] is True
    assert " M src.py" in git(root, "status", "--porcelain").decode()


def test_concurrent_index_change_after_gate_is_preserved_and_blocks_commit(tmp_path, monkeypatch):
    root = fixture_repo(tmp_path)
    (root / ".review-loop").mkdir()
    (root / ".review-loop/config.md").write_text("auto_commit: true\n")
    baseline = make_baseline(root, ("src.py",))
    (root / "src.py").write_text("task change\n")
    manifest = make_manifest(root, tmp_path, baseline)
    def stage_unrelated(*args):
        (root / "other.txt").write_text("concurrent user staged edit\n")
        git(root, "add", "other.txt")
        return {"eligible": True, "reasons": []}
    monkeypatch.setattr(actions, "_evaluate_delivery_gate", stage_unrelated)
    with pytest.raises(actions.ActionError, match="index changed"):
        actions.commit(str(root), str(manifest), str(tmp_path / "session.md"), "task: scoped")
    assert git(root, "diff", "--cached", "--name-only").decode().strip() == "other.txt"


def test_commit_rejects_manifest_replaced_after_gate(tmp_path, monkeypatch):
    root = fixture_repo(tmp_path)
    (root / ".review-loop").mkdir()
    (root / ".review-loop/config.md").write_text("auto_commit: true\n")
    baseline = make_baseline(root, ("src.py",))
    (root / "src.py").write_text("task change\n")
    manifest = make_manifest(root, tmp_path, baseline)
    gate_fingerprint = json.loads(manifest.read_text())["fingerprint"]
    expanded_baseline = ds.build_baseline(baseline["repository"], ["."], baseline["state"])
    replacement = make_manifest(root, tmp_path, expanded_baseline)

    def replace_after_gate(*args):
        manifest.write_text(replacement.read_text())
        return {"eligible": True, "reasons": [], "manifest_fingerprint": gate_fingerprint}

    monkeypatch.setattr(actions, "_evaluate_delivery_gate", replace_after_gate)
    with pytest.raises(actions.ActionError, match="manifest changed after W05"):
        actions.commit(str(root), str(manifest), str(tmp_path / "session.md"), "task: guarded")
    assert git(root, "rev-parse", "HEAD").decode().strip() == baseline["state"]["head"]


def test_commit_requires_explicit_auto_commit_flag(tmp_path, capsys):
    root = fixture_repo(tmp_path)
    baseline = make_baseline(root, ("src.py",))
    manifest = make_manifest(root, tmp_path, baseline)
    rc = actions.main(["--repo", str(root), "--manifest", str(manifest), "commit",
                       "--session-file", str(tmp_path / "session.md"), "--message", "no"])
    assert rc == 1
    assert "auto_commit" in capsys.readouterr().err


def test_auto_commit_requires_one_explicit_config_value_outside_examples(tmp_path):
    root = fixture_repo(tmp_path)
    config = root / ".review-loop/config.md"
    config.parent.mkdir()
    config.write_text("Example:\n```yaml\nauto_commit: true\n```\n")
    assert actions._auto_commit_enabled(str(root)) is False
    config.write_text("auto_commit: true\n")
    assert actions._auto_commit_enabled(str(root)) is True
    config.write_text("auto_commit: true\nauto_commit: false\n")
    with pytest.raises(actions.ActionError, match="duplicate"):
        actions._auto_commit_enabled(str(root))


def test_index_stat_refresh_after_baseline_does_not_block_plan(tmp_path):
    root = fixture_repo(tmp_path)
    baseline = make_baseline(root, ("src.py",))
    before_sha = baseline["state"]["index_file_sha256"]
    # A stat-only change plus an index refresh rewrites the index file
    # without staging anything (what `git status` does opportunistically).
    os.utime(root / "other.txt", (1_000_000_000, 1_000_000_000))
    git(root, "update-index", "--refresh")
    (root / "src.py").write_text("task change\n")
    manifest = make_manifest(root, tmp_path, baseline)
    current = json.loads(manifest.read_text())["current"]
    assert current["index_file_sha256"] != before_sha
    plan = actions.compute_plan(str(root), str(manifest))
    assert plan["eligible"] is True, plan["reasons"]
    assert plan["paths"] == ["src.py"]


def test_plan_separates_ownership_from_commit_only_reasons(tmp_path):
    root = fixture_repo(tmp_path)
    git(root, "config", "core.autocrlf", "true")
    baseline = make_baseline(root, ("src.py",))
    (root / "src.py").write_text("task change\n")
    manifest = make_manifest(root, tmp_path, baseline)
    plan = actions.compute_plan(str(root), str(manifest))
    assert plan["eligible"] is False
    assert plan["ownership_reasons"] == []
    assert any("attributes" in reason for reason in plan["commit_reasons"])


def test_untouched_preexisting_untracked_file_in_scope_does_not_block_plan(tmp_path):
    root = fixture_repo(tmp_path)
    (root / "notes.txt").write_text("user scratch\n")
    baseline = make_baseline(root, (".",))
    (root / "src.py").write_text("task change\n")
    manifest = make_manifest(root, tmp_path, baseline)
    plan = actions.compute_plan(str(root), str(manifest))
    assert plan["eligible"] is True, plan["reasons"]
    assert plan["paths"] == ["src.py"]
