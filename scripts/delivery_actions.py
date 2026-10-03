#!/usr/bin/env python3
"""Plan or perform a delivery commit limited to W01-attributed task paths.

`plan` is read-only. `commit` is an explicit side effect and requires both
`--auto-commit` (the resolved user setting) and a fresh W05 delivery gate.
Existing staged work, ambiguous ownership, out-of-scope changes, content
filters, and repository hooks fail closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

try:
    import delivery_scope as ds
except ModuleNotFoundError:  # imported as scripts.delivery_actions
    from scripts import delivery_scope as ds


class ActionError(Exception):
    pass


AUTO_COMMIT_RE = re.compile(r"^\s*auto_commit\s*:\s*(true|false)\s*$", re.I)


def _auto_commit_enabled(repo: str) -> bool:
    path = Path(repo) / ".review-loop/config.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    declarations = []
    fence = None
    for line in lines:
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        match = AUTO_COMMIT_RE.match(line)
        if match:
            declarations.append(match.group(1).lower() == "true")
    if len(declarations) > 1:
        raise ActionError("config.md has duplicate auto_commit declarations; refusing to commit")
    return declarations[0] if declarations else False


def _git(repo: str, *args: str, index_file: str = None, configs: tuple = (), codes=(0,)) -> bytes:
    env = os.environ.copy()
    for key in ds.GIT_ENV_OVERRIDES:
        env.pop(key, None)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1", GIT_LITERAL_PATHSPECS="1")
    if index_file:
        env["GIT_INDEX_FILE"] = index_file
    argv = ["git", "-c", "core.fsmonitor=false"]
    for key, value in configs:
        argv.extend(["-c", key + "=" + value])
    argv.extend(args)
    result = subprocess.run(argv, cwd=repo,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode not in codes:
        raise ActionError("git " + args[0] + " failed: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _git_input(repo: str, data: bytes, *args: str) -> bytes:
    env = os.environ.copy()
    for key in ds.GIT_ENV_OVERRIDES:
        env.pop(key, None)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=repo,
                            env=env, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise ActionError("git " + args[0] + " failed: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _read_manifest_blob(repo: str, path: str, expected: dict) -> bytes:
    """Read one path without following symlinks, then bind bytes to W01 identity."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    parent = os.open(repo, directory_flags)
    try:
        parts = path.split("/")
        for component in parts[:-1]:
            child = os.open(component, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        leaf = parts[-1]
        before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if expected["mode"] == "120000":
            if not stat.S_ISLNK(before.st_mode):
                raise ActionError("manifest symlink changed type: " + path)
            data = os.fsencode(os.readlink(leaf, dir_fd=parent))
            after = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        else:
            if not stat.S_ISREG(before.st_mode) or before.st_size > 8 * 1024 * 1024:
                raise ActionError("manifest file is not regular or exceeds the safe commit size: " + path)
            fd = os.open(leaf, flags, dir_fd=parent)
            try:
                chunks = []
                size = 0
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 8 * 1024 * 1024:
                        raise ActionError("manifest file exceeds the safe commit size: " + path)
                    chunks.append(chunk)
                after_fd = os.fstat(fd)
            finally:
                os.close(fd)
            after = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after_fd.st_dev, after_fd.st_ino, after_fd.st_size, after_fd.st_mtime_ns
            ):
                raise ActionError("manifest file changed while reading: " + path)
            data = b"".join(chunks)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ActionError("manifest path changed while reading: " + path)
    finally:
        os.close(parent)
    if len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
        raise ActionError("worktree bytes no longer match the W01 manifest: " + path)
    return data


def _filtered_paths(repo: str, paths: list) -> list:
    filtered = []
    autocrlf = ds.git(repo, "config", "--get", "core.autocrlf", codes=(0, 1)).decode("ascii", "ignore").strip().lower()
    if autocrlf in {"true", "input"}:
        filtered.extend(paths)
    for path in paths:
        output = _git(repo, "check-attr", "--all", "--", path).decode("utf-8", "replace")
        for line in output.splitlines():
            fields = line.rsplit(":", 2)
            if len(fields) == 3 and fields[1].strip() in {
                "filter", "text", "eol", "working-tree-encoding", "ident",
            } and fields[2].strip() not in ("unspecified", "unset"):
                filtered.append(path)
                break
    return sorted(set(filtered))


def compute_plan(repo_path: str, manifest_path: str = None, *, manifest: dict = None) -> dict:
    repo = ds.repository(repo_path)
    if manifest is None:
        if manifest_path is None:
            raise ds.UsageError("a delivery manifest path or validated manifest object is required")
        manifest = ds.load_document(manifest_path, "delivery-manifest")
    else:
        ds.validate_document(manifest, "delivery-manifest")
    ds.match_repository(repo, manifest["baseline"])
    current = ds.capture_state(repo)
    # Ownership reasons apply to every delivery (W05); commit reasons only
    # matter when the guarded commit itself runs (W04, opt-in auto_commit).
    ownership = []
    commit_only = []
    if current != manifest["current"]:
        ownership.append("delivery manifest is stale")
    if manifest["head_changed"]:
        ownership.append("HEAD moved after the delivery baseline")
    if manifest["outside_scope_delta"]:
        ownership.append("manifest has unclaimed outside-scope changes")
    baseline = manifest["baseline"]["state"]
    if manifest["baseline_changes"]["staged"] or manifest["baseline_changes"]["conflicted"]:
        commit_only.append("pre-task index changes or conflicts must be reconciled before auto-commit")
    # Compare per-path index entries, not the raw index file: a stat-cache
    # refresh (for example `git status`) rewrites the file without staging.
    if current["index"] != baseline["index"]:
        commit_only.append("the index changed after baseline capture; refusing to commit mixed staged work")

    paths = []
    for row in manifest["task_delta"]:
        if row["ownership"] != "declared-post-baseline":
            ownership.append("task delta contains a path without declared ownership: " + row["path"])
        elif row["before"]["worktree"] != row["after"]["worktree"]:
            paths.append(row["path"])
    paths = sorted(set(paths))
    filtered = _filtered_paths(repo["worktree"], paths)
    if filtered:
        commit_only.append("Git content-transform attributes prevent raw-manifest commit: " + ", ".join(filtered))
    reasons = ownership + commit_only
    return ds.seal({
        "schema": 1, "kind": "delivery-action-plan", "created_at": ds.created_at(),
        "manifest_fingerprint": manifest["fingerprint"], "repository": repo,
        "eligible": not reasons, "reasons": sorted(set(reasons)),
        "ownership_reasons": sorted(set(ownership)), "commit_reasons": sorted(set(commit_only)),
        "paths": paths, "head": current["head"],
        "baseline_index_sha256": baseline["index_file_sha256"],
    })


def _index_path(repo: str) -> str:
    value = ds.git_line(repo, "rev-parse", "--git-path", "index")
    return value if os.path.isabs(value) else os.path.join(repo, value)


def _sha256_file(path: str):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return None


def _evaluate_delivery_gate(repo_path: str, manifest_path: str, session_file: str) -> dict:
    try:
        from delivery_gate import evaluate_gate
    except ModuleNotFoundError:  # imported as scripts.delivery_actions
        from scripts.delivery_gate import evaluate_gate
    return evaluate_gate(repo_path, manifest_path, session_file)


def commit(repo_path: str, manifest_path: str, session_file: str, message: str) -> dict:
    if not message.strip() or "\0" in message:
        raise ActionError("commit message must be non-empty")
    repo = ds.repository(repo_path)["worktree"]
    if not _auto_commit_enabled(repo):
        raise ActionError("commit refused: .review-loop/config.md does not enable auto_commit")
    # Recompute the final W05 gate immediately before touching the index.
    gate = _evaluate_delivery_gate(repo_path, manifest_path, session_file)
    if not gate["eligible"]:
        raise ActionError("delivery gate blocked: " + "; ".join(gate["reasons"]))
    plan = compute_plan(repo_path, manifest_path)
    if not plan["eligible"]:
        raise ActionError("delivery action blocked: " + "; ".join(plan["reasons"]))
    if plan["manifest_fingerprint"] != gate["manifest_fingerprint"]:
        raise ActionError("delivery manifest changed after W05; rerun the final gate")
    if not plan["paths"]:
        return {"status": "no-changes", "manifest_fingerprint": plan["manifest_fingerprint"], "paths": []}

    repo = plan["repository"]["worktree"]
    manifest = ds.load_document(manifest_path, "delivery-manifest")
    if manifest["fingerprint"] != plan["manifest_fingerprint"]:
        raise ActionError("delivery manifest changed after W04 planning; rerun W01/W05")
    if not plan["head"]:
        raise ActionError("auto-commit requires an existing HEAD commit")
    gpgsign = _git(repo, "config", "--bool", "--get", "commit.gpgsign", codes=(0, 1)).decode("ascii", "ignore").strip()
    if gpgsign == "true":
        raise ActionError("configured commit signing is unsupported by the guarded commit-tree path")
    current = ds.capture_state(ds.repository(repo))
    if current != manifest["current"]:
        raise ActionError("delivery candidate changed before commit; rerun W02/W05")

    index_path = _index_path(repo)
    index_dir = os.path.dirname(index_path)
    lock_path = index_path + ".lock"
    # The fresh candidate (checked above) pins the index file bytes observed
    # at W01 manifest time; per-path equality with the baseline is in the plan.
    expected_index_sha = manifest["current"]["index_file_sha256"]
    if _sha256_file(index_path) != expected_index_sha:
        raise ActionError("the user index changed after W05; commit refused")
    lock_fd = None
    private_index = None
    lock_owned = False
    committed = False
    empty_hooks = tempfile.TemporaryDirectory(prefix="review-loop-no-hooks-")
    try:
        try:
            lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            lock_owned = True
        except FileExistsError as exc:
            raise ActionError("Git index is locked by another process") from exc
        if _sha256_file(index_path) != expected_index_sha:
            raise ActionError("the user index changed while acquiring the delivery lock")

        fd, private_index = tempfile.mkstemp(prefix=".review-loop-delivery-", suffix=".index", dir=index_dir)
        os.close(fd)
        os.unlink(private_index)
        hook_config = (("core.hooksPath", empty_hooks.name),)
        _git(repo, "read-tree", plan["head"], index_file=private_index, configs=hook_config)
        for path in plan["paths"]:
            entry = manifest["current"]["worktree"].get(path)
            if entry is None:
                _git(repo, "update-index", "--force-remove", "--", path,
                     index_file=private_index, configs=hook_config)
                continue
            data = _read_manifest_blob(repo, path, entry)
            oid = _git_input(repo, data, "hash-object", "-w", "--stdin").decode("ascii").strip()
            if oid != entry["oid"]:
                raise ActionError("stored blob id differs from W01 manifest: " + path)
            _git(repo, "update-index", "--add", "--cacheinfo", f"{entry['mode']},{oid},{path}",
                 index_file=private_index, configs=hook_config)

        tree = _git(repo, "write-tree", index_file=private_index, configs=hook_config).decode("ascii").strip()
        changed = sorted(set(os.fsdecode(path) for path in
                             _git(repo, "diff", "--name-only", "--no-renames", "-z",
                                  plan["head"], tree).split(b"\0") if path))
        if changed != plan["paths"]:
            raise ActionError("private commit tree contains a path outside the W01 task delta")
        if ds.capture_state(ds.repository(repo)) != manifest["current"]:
            raise ActionError("repository changed during private-index staging; commit refused")

        commit_oid = _git(repo, "commit-tree", tree, "-p", plan["head"], "-m", message,
                          configs=hook_config).decode("ascii").strip()
        _git(repo, "read-tree", commit_oid, index_file=private_index, configs=hook_config)
        with open(private_index, "rb") as source:
            final_index = source.read()
        os.write(lock_fd, final_index)
        os.fsync(lock_fd)

        # Compare-and-swap HEAD: if another actor commits meanwhile, nothing is
        # published and the private commit object remains unreachable.
        _git(repo, "update-ref", "HEAD", commit_oid, plan["head"], configs=hook_config)
        committed = True
        os.close(lock_fd)
        lock_fd = None
        try:
            os.replace(lock_path, index_path)
            lock_owned = False
            dir_fd = os.open(index_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            index_sync = "updated"
        except OSError as exc:
            index_sync = "failed:" + type(exc).__name__
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if lock_owned:
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
        if private_index is not None:
            try:
                os.unlink(private_index)
            except FileNotFoundError:
                pass
        empty_hooks.cleanup()
    post_state = ds.capture_state(ds.repository(repo)) if committed else None
    return {"status": "committed", "manifest_fingerprint": plan["manifest_fingerprint"],
            "commit": commit_oid, "paths": plan["paths"], "hooks": "disabled",
            "index_sync": index_sync if committed else "not-published",
            "post_commit_worktree_drift": post_state is not None and
            post_state["worktree"] != manifest["current"]["worktree"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--manifest", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("plan", help="read-only ownership and staging preview")
    do_commit = sub.add_parser("commit", help="commit only exact delivery-owned paths")
    do_commit.add_argument("--session-file", required=True)
    do_commit.add_argument("--message", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            report = compute_plan(args.repo, args.manifest)
            print(json.dumps(report, sort_keys=True, indent=2))
            return 0 if report["eligible"] else 1
        report = commit(args.repo, args.manifest, args.session_file, args.message)
        print(json.dumps(report, sort_keys=True, indent=2))
        return 0
    except ds.UsageError as exc:
        sys.stderr.write(json.dumps({"error": str(exc), "kind": "usage"}) + "\n")
        return 2
    except (ds.CaptureError, ActionError, OSError, ValueError) as exc:
        sys.stderr.write(json.dumps({"error": str(exc), "kind": "delivery-action"}) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
