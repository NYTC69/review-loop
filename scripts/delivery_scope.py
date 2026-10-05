#!/usr/bin/env python3
"""Read-only delivery baselines and candidates. See docs/protocol/delivery-scope.md.

Only --output writes a file. Git objects, refs, the index and source files are
never written. Worktree blobs are hashed as raw bytes, without executing filters.
"""

from __future__ import annotations

import argparse
import datetime
import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from typing import Optional

SCHEMA = 1
POLICY = "raw-files/nonignored-untracked-except-session-dir/literal-prefix-scope/v2"
# Untracked review-loop session artifacts are never delivery content; the
# evidence ledger applies the same exclusion (SESSION_DIR_PREFIX).
SESSION_DIR_PREFIX = ".review-loop/"
GIT_ENV_OVERRIDES = (
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_PREFIX", "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
)


class UsageError(Exception):
    """Invalid arguments or an invalid/incompatible artifact (exit 2)."""


class CaptureError(Exception):
    """Cannot completely and consistently capture the repository (exit 3)."""


def git_env(command: str) -> dict:
    env = os.environ.copy()
    for key in GIT_ENV_OVERRIDES:
        env.pop(key, None)
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    if command != "check-ignore":  # check-ignore accepts literal filenames but rejects pathspec magic.
        env["GIT_LITERAL_PATHSPECS"] = "1"
    return env


def git(repo: str, *args: str, codes: tuple = (0,)) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args], cwd=repo, env=git_env(args[0]),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode not in codes:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise CaptureError(f"git {args[0]} failed ({result.returncode}): {detail}")
    return result.stdout


def git_line(repo: str, *args: str, codes: tuple = (0,)) -> str:
    data = git(repo, *args, codes=codes)
    return os.fsdecode(data[:-1] if data.endswith(b"\n") else data)


def repository(path: str) -> dict:
    root = os.path.realpath(git_line(path, "rev-parse", "--show-toplevel"))
    return {
        "worktree": root,
        "git_dir": os.path.realpath(git_line(root, "rev-parse", "--absolute-git-dir")),
        "object_format": git_line(root, "rev-parse", "--show-object-format"),
    }


def path_name(value: str, *, selector: bool = False) -> str:
    if not isinstance(value, str) or not value or "\0" in value or os.path.isabs(value):
        raise UsageError(f"expected a repository-relative path: {value!r}")
    parts = value.split("/")
    if ".." in parts or any(part.casefold() == ".git" for part in parts):
        raise UsageError(f"unsafe repository path: {value!r}")
    normalized = "/".join(part for part in parts if part and part != ".")
    if not selector and normalized != value:
        raise UsageError(f"noncanonical repository path: {value!r}")
    return normalized or "."


def scope_paths(repo: str, values: list) -> list:
    if not isinstance(values, list) or not values:
        raise UsageError("at least one literal --scope is required; use . for the entire repository")
    scope = sorted({path_name(value, selector=True) for value in values})
    for value in scope:
        cursor = repo
        for component in value.split("/")[:-1]:
            cursor = os.path.join(cursor, component)
            if os.path.islink(cursor):
                raise UsageError(f"scope traverses a symlink: {value!r}")
        if git(repo, "check-ignore", "--", "./" + value, codes=(0, 1)) and not git(repo, "ls-files", "-z", "--", value):
            raise UsageError(f"scope names ignored, untracked content: {value!r}")
    return scope


def in_scope(path: str, scope: list) -> bool:
    return any(prefix == "." or path == prefix or path.startswith(prefix + "/") for prefix in scope)


def fingerprint(value: object) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def seal(document: dict) -> dict:
    document["fingerprint"] = fingerprint({
        key: value for key, value in document.items() if key not in {"fingerprint", "created_at"}
    })
    return document


def created_at() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def stat_key(st: os.stat_result) -> tuple:
    return st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def disk_entry(repo: str, path: str, algorithm: str) -> Optional[dict]:
    """Hash a leaf through pinned, no-follow directory descriptors.

    A tracked descendant of a directory replaced by a file/symlink is absent;
    the replacement leaf itself is enumerated separately by Git. No symlink
    target is opened, including when a parent changes during capture.
    """
    path_name(path)
    directory = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.split("/")[:-1]:
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                    return None
                raise
            os.close(directory)
            directory = child
        leaf = path.split("/")[-1]
        try:
            before = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISDIR(before.st_mode):
            return None  # Git does not version empty directories.
        if stat.S_ISLNK(before.st_mode):
            payload = os.fsencode(os.readlink(leaf, dir_fd=directory))
            mode = "120000"
            size = len(payload)
            blob = hashlib.new(algorithm, f"blob {size}\0".encode("ascii") + payload)
            raw = hashlib.sha256(payload)
        elif stat.S_ISREG(before.st_mode):
            mode = "100755" if before.st_mode & stat.S_IXUSR else "100644"
            size = before.st_size
            blob = hashlib.new(algorithm, f"blob {size}\0".encode("ascii"))
            raw = hashlib.sha256()
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                if stat_key(os.fstat(source.fileno())) != stat_key(before):
                    raise CaptureError(f"file changed during capture: {path!r}")
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    blob.update(block)
                    raw.update(block)
                if stat_key(os.fstat(source.fileno())) != stat_key(before):
                    raise CaptureError(f"file changed during capture: {path!r}")
        else:
            raise CaptureError(f"unsupported non-file entry: {path!r}")
        after = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
        if stat_key(before) != stat_key(after):
            raise CaptureError(f"file changed during capture: {path!r}")
        return {"mode": mode, "oid": blob.hexdigest(), "sha256": raw.hexdigest(), "size": size}
    finally:
        os.close(directory)


def read_state(repo: dict) -> dict:
    root = repo["worktree"]
    head = git_line(root, "rev-parse", "--verify", "-q", "HEAD", codes=(0, 1)) or None
    head_paths = {}
    if head:
        for raw in git(root, "ls-tree", "-r", "-z", head).split(b"\0"):
            if raw:
                metadata, path = raw.split(b"\t", 1)
                mode, _, oid = metadata.decode("ascii").split()
                head_paths[path_name(os.fsdecode(path))] = {"mode": mode, "oid": oid}
    index = {}
    for raw in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if raw:
            metadata, path = raw.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            index.setdefault(path_name(os.fsdecode(path)), []).append({"mode": mode, "oid": oid, "stage": int(stage)})
    for path in sorted(set(head_paths) | set(index)):
        entries = ([head_paths[path]] if path in head_paths else []) + index.get(path, [])
        if any(entry["mode"] == "160000" for entry in entries):
            raise CaptureError(f"submodules/gitlinks are unsupported by this manifest version: {path!r}")
    untracked = []
    for raw in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if raw:
            path = os.fsdecode(raw)
            if path.startswith(SESSION_DIR_PREFIX):
                continue
            if path.endswith("/"):
                raise CaptureError(f"nested repositories are unsupported by this manifest version: {path!r}")
            untracked.append(path_name(path))
    worktree = {}
    for path in sorted(set(head_paths) | set(index) | set(untracked)):
        entry = disk_entry(root, path, repo["object_format"])
        if entry is not None:
            worktree[path] = entry
    index_path = git_line(root, "rev-parse", "--git-path", "index")
    if not os.path.isabs(index_path):
        index_path = os.path.join(root, index_path)
    try:
        with open(index_path, "rb") as source:
            index_digest = hashlib.sha256(source.read()).hexdigest()
    except FileNotFoundError:
        index_digest = None
    return {"head": head, "head_paths": head_paths, "index": index,
            "index_file_sha256": index_digest, "worktree": worktree, "untracked": sorted(untracked)}


def commit_state(repo: dict, commit: str) -> dict:
    """The state of a clean checkout of `commit` (HEAD, index and worktree all equal to it; nothing untracked): the
    baseline of a review-only run, whose live tree already holds the change under review (review-only-entry.md §4)."""
    root = repo["worktree"]
    head = git_line(root, "rev-parse", "--verify", "-q", commit + "^{commit}", codes=(0, 1)) if not commit.startswith("-") else ""
    if not head:
        raise UsageError(f"--from-commit does not name a commit: {commit!r}")
    head_paths = {}
    for raw in git(root, "ls-tree", "-r", "-z", head).split(b"\0"):
        if raw:
            metadata, path = raw.split(b"\t", 1)
            mode, _, oid = metadata.decode("ascii").split()
            if mode == "160000":
                raise CaptureError(f"submodules/gitlinks are unsupported by this manifest version: {os.fsdecode(path)!r}")
            head_paths[path_name(os.fsdecode(path))] = {"mode": mode, "oid": oid}
    worktree, blobs = {}, {}
    with tempfile.TemporaryFile() as ids:   # one cat-file --batch, read as a stream: bounded memory for any repository
        ids.write("".join(oid + "\n" for oid in sorted({e["oid"] for e in head_paths.values()})).encode("ascii"))
        ids.seek(0)
        process = subprocess.Popen(["git", "-c", "core.fsmonitor=false", "cat-file", "--batch"], cwd=root, stdin=ids,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=git_env("cat-file"))
        try:
            while header := process.stdout.readline():   # "<oid> blob <size>\n<content>\n" per object
                fields = header.decode("ascii").split()
                if len(fields) != 3 or fields[1] != "blob":
                    raise CaptureError(f"unexpected object in {commit!r}: {' '.join(fields)}")
                oid, _kind, size = fields
                digest, left = hashlib.sha256(), int(size)
                while left:
                    chunk = process.stdout.read(min(left, 1024 * 1024))
                    if not chunk:
                        raise CaptureError(f"git cat-file ended inside {oid}")
                    digest.update(chunk)
                    left -= len(chunk)
                if process.stdout.read(1) != b"\n":
                    raise CaptureError(f"git cat-file framing error after {oid}")
                blobs[oid] = (digest.hexdigest(), int(size))
        finally:
            process.stdout.close()
            if process.wait() != 0:
                raise CaptureError(f"git cat-file failed ({process.returncode})")
    for path, entry in head_paths.items():
        worktree[path] = {**entry, "sha256": blobs[entry["oid"]][0], "size": blobs[entry["oid"]][1]}
    return {"head": head, "head_paths": head_paths, "index": {path: [{**entry, "stage": 0}] for path, entry in head_paths.items()},
            "index_file_sha256": None, "worktree": worktree, "untracked": []}


def capture_state(repo: dict) -> dict:
    first = read_state(repo)
    second = read_state(repo)
    if first != second:
        raise CaptureError("repository changed during capture; retry after edits/staging stop")
    return second


def identity(entry: Optional[dict]) -> Optional[str]:
    return f"{entry['mode']}:{entry['oid']}" if entry is not None else None


def changes(before: dict, after: dict) -> list:
    result = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if identity(old) != identity(new):
            result.append({"path": path, "change": "add" if old is None else "delete" if new is None else "modify",
                           "before": identity(old), "after": identity(new)})
    return result


def baseline_changes(state: dict) -> dict:
    conflicts = sorted(path for path, entries in state["index"].items() if any(e["stage"] != 0 for e in entries))
    index = {path: entries[0] for path, entries in state["index"].items() if path not in conflicts}
    head = {path: entry for path, entry in state["head_paths"].items() if path not in conflicts}
    # Untracked files are their own category, even after a staged deletion.
    tracked_worktree = {path: state["worktree"][path] for path in index if path in state["worktree"]}
    return {"staged": changes(head, index), "unstaged": changes(index, tracked_worktree),
            "untracked": state["untracked"], "conflicted": conflicts}


def build_baseline(repo: dict, scope: list, state: dict) -> dict:
    return seal({"schema": SCHEMA, "kind": "delivery-baseline", "policy": POLICY,
                 "created_at": created_at(), "repository": repo, "scope": scope, "state": state})


def build_manifest(baseline: dict, current: dict) -> dict:
    before = baseline["state"]
    preexisting = baseline_changes(before)
    dirty = set(preexisting["untracked"] + preexisting["conflicted"])
    dirty.update(row["path"] for key in ("staged", "unstaged") for row in preexisting[key])
    known = set()
    for state in (before, current):
        for key in ("head_paths", "index", "worktree"):
            known.update(state[key])
    scoped, outside = [], []
    for path in sorted(known):
        old = {key: before[key].get(path) for key in ("head_paths", "index", "worktree")}
        new = {key: current[key].get(path) for key in ("head_paths", "index", "worktree")}
        if old == new:
            continue
        declared = in_scope(path, baseline["scope"])
        row = {"path": path, "before": old, "after": new, "baseline_dirty": path in dirty,
               "ownership": "ambiguous-baseline-overlap" if path in dirty else
                            "declared-post-baseline" if declared else "unclaimed"}
        (scoped if declared else outside).append(row)
    declared_paths = sorted(path for path in known if in_scope(path, baseline["scope"]))
    return seal({
        "schema": SCHEMA, "kind": "delivery-manifest", "policy": POLICY, "created_at": created_at(),
        "baseline": baseline, "baseline_fingerprint": baseline["fingerprint"],
        "current": current, "current_fingerprint": fingerprint(current),
        "task_declared_scope": baseline["scope"], "declared_content_paths": declared_paths,
        "baseline_changes": preexisting, "task_delta": scoped, "outside_scope_delta": outside,
        "ownership_ambiguities": sorted(set(declared_paths) & dirty),
        "head_changed": before["head"] != current["head"],
        "index_file_changed": before["index_file_sha256"] != current["index_file_sha256"],
    })


def unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise UsageError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def validate_state(state: dict, repository_info: dict) -> None:
    if not isinstance(repository_info, dict) or repository_info.get("object_format") not in {"sha1", "sha256"}:
        raise UsageError("invalid repository identity/object format")
    for key in ("worktree", "git_dir"):
        if not isinstance(repository_info.get(key), str) or not os.path.isabs(repository_info[key]):
            raise UsageError("invalid repository identity")
    oid_length = 40 if repository_info["object_format"] == "sha1" else 64

    def digest(value: object, length: int) -> bool:
        return isinstance(value, str) and re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is not None

    if not isinstance(state, dict) or set(state) != {
        "head", "head_paths", "index", "index_file_sha256", "worktree", "untracked"
    }:
        raise UsageError("invalid snapshot fields")
    if state["head"] is not None and not digest(state["head"], oid_length):
        raise UsageError("invalid snapshot HEAD")
    if state["index_file_sha256"] is not None and not digest(state["index_file_sha256"], 64):
        raise UsageError("invalid index file digest")
    for key in ("head_paths", "index", "worktree"):
        if not isinstance(state[key], dict):
            raise UsageError(f"invalid snapshot {key}")
        for path, value in state[key].items():
            path_name(path)
            entries = value if key == "index" else [value]
            if not isinstance(entries, list) or not entries:
                raise UsageError(f"invalid {key} entries: {path!r}")
            stages = []
            for entry in entries:
                if (not isinstance(entry, dict) or entry.get("mode") not in {"100644", "100755", "120000"}
                        or not digest(entry.get("oid"), oid_length)):
                    raise UsageError(f"invalid {key} identity: {path!r}")
                if key == "index":
                    stage = entry.get("stage")
                    if type(stage) is not int or not 0 <= stage <= 3:
                        raise UsageError(f"invalid index stage: {path!r}")
                    stages.append(stage)
                if key == "worktree" and (not digest(entry.get("sha256"), 64)
                        or type(entry.get("size")) is not int or entry["size"] < 0):
                    raise UsageError(f"invalid raw worktree identity: {path!r}")
            if stages and (stages != sorted(set(stages)) or (0 in stages and stages != [0])):
                raise UsageError(f"invalid index stages: {path!r}")
    if not isinstance(state["untracked"], list):
        raise UsageError("invalid untracked list")
    for path in state["untracked"]:
        path_name(path)
    if state["untracked"] != sorted(set(state["untracked"])):
        raise UsageError("untracked paths must be sorted and unique")


def validate_document(document: dict, kind: str) -> None:
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or document.get("kind") != kind:
        raise UsageError(f"expected schema {SCHEMA} {kind}")
    if document.get("policy") != POLICY:
        raise UsageError("incompatible capture policy")
    expected = fingerprint({key: value for key, value in document.items() if key not in {"fingerprint", "created_at"}})
    if document.get("fingerprint") != expected:
        raise UsageError("artifact fingerprint mismatch")
    if kind == "delivery-manifest":
        validate_document(document.get("baseline"), "delivery-baseline")
        validate_state(document["current"], document["baseline"]["repository"])
        expected_manifest = build_manifest(document["baseline"], document["current"])
        if expected_manifest["fingerprint"] != document["fingerprint"]:
            raise UsageError("manifest fields do not match its baseline/current state")
    else:
        validate_state(document["state"], document["repository"])
        if not isinstance(document["scope"], list) or not document["scope"]:
            raise UsageError("invalid baseline scope")
        for path in document["scope"]:
            path_name(path, selector=True)


def load_document(path: str, kind: str) -> dict:
    try:
        with open(path, encoding="utf-8") as source:
            document = json.load(source, object_pairs_hook=unique_object)
        validate_document(document, kind)
        return document
    except (KeyError, TypeError, ValueError) as exc:
        raise UsageError(f"malformed {kind}: {exc}") from exc


def match_repository(repo: dict, baseline: dict) -> list:
    if baseline["repository"] != repo:
        raise UsageError("baseline belongs to a different repository/worktree")
    normalized = scope_paths(repo["worktree"], baseline["scope"])
    if normalized != baseline["scope"]:
        raise UsageError("baseline scope is not canonical")
    return normalized


def validate_output(repo: dict, path: Optional[str]) -> None:
    if path is None:
        return
    full = os.path.realpath(path)
    if os.path.lexists(path):
        raise UsageError(f"output already exists; artifacts are immutable: {path!r}")
    common = git_line(repo["worktree"], "rev-parse", "--git-common-dir")
    common = os.path.realpath(os.path.join(repo["worktree"], common))
    if any(os.path.commonpath((directory, full)) == directory for directory in (repo["git_dir"], common)):
        raise UsageError("output must not be inside Git administrative storage")
    if os.path.commonpath((repo["worktree"], full)) == repo["worktree"]:
        relative = os.path.relpath(full, repo["worktree"])
        # Artifacts must not become part of their own observed candidate:
        # an untracked path under the excluded session directory, or an
        # ignored path, is never inventoried.
        if (relative.replace(os.sep, "/").startswith(SESSION_DIR_PREFIX)
                and not git(repo["worktree"], "ls-files", "--", relative)):
            return
        if not git(repo["worktree"], "check-ignore", "--", "./" + relative, codes=(0, 1)):
            raise UsageError("output inside the worktree must be Git-ignored; use an ignored artifact directory or an external path")


def emit(document: dict, output: Optional[str]) -> None:
    rendered = json.dumps(document, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    if output:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            target.write(rendered)
    else:
        sys.stdout.write(rendered)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", help="capture a pre-task baseline with an explicit literal scope")
    capture.add_argument("--scope", action="append", required=True)
    capture.add_argument("--output")
    capture.add_argument("--from-commit", help="baseline = a clean checkout of this commit (a review-only run's base)")
    manifest = commands.add_parser("manifest", help="bind the current candidate to an immutable baseline")
    manifest.add_argument("--baseline", required=True)
    manifest.add_argument("--output")
    check = commands.add_parser("check", help="check whether a stored candidate still matches the repository")
    check.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        repo = repository(args.repo)
        validate_output(repo, getattr(args, "output", None))
        if args.command == "capture":
            scope = scope_paths(repo["worktree"], args.scope)
            document = build_baseline(repo, scope, commit_state(repo, args.from_commit) if args.from_commit
                                      else capture_state(repo))
        elif args.command == "manifest":
            baseline = load_document(args.baseline, "delivery-baseline")
            match_repository(repo, baseline)
            document = build_manifest(baseline, capture_state(repo))
        else:
            candidate = load_document(args.manifest, "delivery-manifest")
            match_repository(repo, candidate["baseline"])
            current = capture_state(repo)
            fresh = current == candidate["current"]
            document = {"schema": SCHEMA, "kind": "delivery-check", "fresh": fresh,
                        "candidate_fingerprint": candidate["fingerprint"],
                        "current_fingerprint": fingerprint(current),
                        "changed_components": sorted(key for key in current if current[key] != candidate["current"].get(key))}
            emit(document, None)
            return 0 if fresh else 1
        emit(document, args.output)
        return 0
    except UsageError as exc:
        sys.stderr.write(json.dumps({"error": str(exc), "kind": "usage"}, ensure_ascii=True) + "\n")
        return 2
    except (CaptureError, OSError) as exc:
        sys.stderr.write(json.dumps({"error": str(exc), "kind": "capture"}, ensure_ascii=True) + "\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
