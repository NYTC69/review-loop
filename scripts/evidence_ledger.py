#!/usr/bin/env python3
"""Evidence ledger helper for review-loop (P1 content-bound evidence + P3 packet delta).

Mandatory production entry point for every write to the `## Evidence Ledger`
section of `.review-loop/sessions/{uuid}.md` on both runtimes (Claude Code and
Codex Stage 1). The orchestrator shells out; there is no in-prose hashing and
no in-prose stage minting. `completed_stages` in `## Session Metadata` is
derived by `check` from the ledger and never hand-edited.

Subcommands
-----------
  snapshot      store the current worktree content (tracked + in-scope untracked)
                as a parentless commit under refs/review-loop/{uuid}/snap/{n}
  record        append one evidence record bound to a stored snapshot;
                `--supersedes <id>[,<id>...]` explicitly retires earlier
                records of the same check (FAIL supersession guard applies)
  check         evaluate active records against the current worktree, derive
                `completed_stages`, mark supersession
  classify      decide whether the delta since the last snapshot invalidates
                `exec` (declared-closure proof only; no path categories)
  delta         materialize the Attributable Delta between two stored snapshots
  lint-closure  emit the `lint` claim's closure (selectors + inputs) as JSON
  prune         delete this session's snapshot refs (only when the user asks)
  route         author route decision from the `### Route Facts` block:
                `orchestrator-direct` only when every eligibility fact is
                `true`, every sensitive flag is `false`, the ledger cross-check
                passes, a snapshot exists and the phase is execution;
                otherwise `executor`. Never classifies from paths. Without
                `--facts` the block is read only from `## Current Review
                Packet` (never from history or an old DIR); a packet without
                the block routes to `executor`.

Canonical section selection
---------------------------
A canonical section is identified by session structure, never by the first
matching heading: its heading AND its canonical successor heading. The
canonical packet is the unique `## Current Review Packet` heading whose next
`## ` heading is `## Review History`; zero or more than one candidate fails
closed (`route` → `executor`, first reason `canonical packet section not
uniquely identifiable`). `## Evidence Ledger` is the heading whose next `## `
heading is `## Session Metadata`, and `## Session Metadata` is the last
heading in the file; every reader and writer of those sections uses the same
rule, and an ambiguous section is exit 2 with nothing written. A quoted
heading inside `## Approved Plan` or a `## Review History` entry is therefore
never mistaken for the real section.

Active records and supersession
-------------------------------
For each `claim_id` the highest-id record is the active one; lower-id records
of the same claim are implicitly superseded. A record may also be explicitly
superseded across claim_ids by `record --supersedes` (same `check` only). A
superseded record is never consulted. A stage is present iff **every** active
record of each required check is valid — there is no newest-record-per-check
selection across claim_ids, so any active FAIL of a required check blocks the
stage. A FAIL may only be explicitly superseded by an executed PASS of the same
check whose scope key covers the FAIL's scope key (superset of review-target
paths for path-scoped checks; identical key otherwise).

Exit codes
----------
  0  success, or an affirmative determination (`route` → orchestrator-direct)
  1  negative determination: `classify` when `exec` is invalidated;
     `check --require <stage>` when that stage is absent; `route` → executor
  2  usage / validation error (bad arguments, illegal record, illegal reuse,
     illegal `--supersedes`, malformed route facts, missing session or
     snapshot); nothing is written
  3  storage failure (git plumbing failed, snapshot could not be stored) or
     an internal error (`INTERNAL-ERROR <Type>: <msg>` + traceback on
     stderr); fail closed — nothing is written to the ledger. Exit 1 is
     reserved for determinations and is never produced by a crash.

Snapshot mechanics
------------------
`git rev-parse --absolute-git-dir` (linked worktrees have a `.git` file) →
private `GIT_INDEX_FILE={git-dir}/review-loop/{uuid}.index` → `git hash-object
-w --stdin-paths` → `git update-index -z --index-info` → `git write-tree` →
`git commit-tree` (explicit GIT_AUTHOR_* / GIT_COMMITTER_* env) →
`git update-ref refs/review-loop/{uuid}/snap/{n}`. HEAD, branches, the user's
index, and the worktree are never touched. Stdlib only.

Every content identity is `<git mode>:<object id>` (`100644` / `100755`
blob, `120000` symlink, `160000` submodule gitlink), so a mode-only change
or a submodule pointer move invalidates evidence exactly like a content
change. Gitlinks are read from the index (`git ls-files -s`), bound to the
submodule's checked-out HEAD (else the index pointer) and stored in the
snapshot tree with mode 160000. An untracked nested repository (`dir/` from
`ls-files --others`) is bound the same way to its HEAD. A checked-out
submodule / nested repository with uncommitted or untracked content is bound
as `160000:<commit>+dirty:<digest>` — sha256 over the sorted `(XY, path,
content oid)` triples of its `git status --porcelain=v1 -z
--untracked-files=all` output, nested-nested repositories recursed with the
same rule up to MAX_NESTED_REPO_DEPTH levels (deeper fails closed, exit 3).
Known boundary (accepted, not a defect to report): because the digest is
derived from `git status` rather than from disk the way superproject paths
are, it does not carry mode or type, and it inherits what `git status` hides
— a mode-only change inside a nested repository, `submodule.<name>.ignore`
on a nested-nested submodule, `skip-worktree` / `assume-unchanged`, and an
unreadable directory (git exits 0 and only warns on stderr) can leave the
digest unchanged. Superproject paths are bound from disk and are unaffected.
A git tree can only hold the plain commit, so the
digest is persisted per snapshot as `gitlink_dirty: {path: digest}` on the
snapshot entry and re-applied by `snapshot_tree_paths()`; ledger bindings,
`state["paths"]` and selector digests always carry the suffixed identity.
Untracked content under `.review-loop/` (the session directory) is never in
scope.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import traceback
from typing import Dict, List, Optional, Tuple

LEDGER_SCHEMA = 1
LEDGER_HEADING = "## Evidence Ledger"
METADATA_HEADING = "## Session Metadata"
PACKET_HEADING = "## Current Review Packet"
STAGE_ORDER = ["exec", "polish", "docs", "security"]

# Required claim sets per stage: check name -> permitted satisfying states.
# A state is "executed:PASS", "not-applicable" or "controlled-skip".
REQUIRED_CLAIMS: Dict[str, Dict[str, set]] = {
    "exec": {
        "reviewer_approve": {"executed:PASS"},
        "gate": {"executed:PASS", "controlled-skip"},
    },
    "polish": {
        "static_analysis": {"executed:PASS", "not-applicable"},
        "agent_review:code-reviewer": {"executed:PASS"},
        "agent_review:silent-failure-hunter": {"executed:PASS"},
        "simplify": {"executed:PASS"},
        "tests": {"executed:PASS"},
    },
    "docs": {"docs_consistency": {"executed:PASS"}},
    "security": {"security_scan": {"executed:PASS"}},
}

# Checks whose disposition may legally be something other than `executed`.
NOT_APPLICABLE_CHECKS = {"static_analysis"}
CONTROLLED_SKIP_CHECKS = {"gate"}

# Checks that must carry `env {command, recorded_at}` when executed.
ENV_REQUIRED_CHECKS = {"tests", "lint", "static_analysis", "security_scan"}

# Checks whose scope key is the sorted, "|"-joined review-target path list.
PATH_SCOPED_CHECKS = {"reviewer_approve", "gate", "simplify"}
COMMAND_SCOPED_CHECKS = {"tests", "lint", "static_analysis"}
SESSION_SCOPED_CHECKS = {"docs_consistency", "security_scan"}

DEFAULT_STAGE_BY_CHECK = {
    "reviewer_approve": "exec",
    "gate": "exec",
    "static_analysis": "polish",
    "simplify": "polish",
    "tests": "polish",
    "docs_consistency": "docs",
    "security_scan": "security",
}

DISPOSITIONS = ("executed", "not-applicable", "controlled-skip")
CLOSURES = ("declared", "uncertain")
AUTHOR_ROUTES = ("executor", "orchestrator-direct", "n/a")
SELECTOR_KINDS = ("glob", "dir", "discovery")

# `### Route Facts` block (Slice 2). Every eligibility fact must be `true`
# and every sensitive flag `false` for `orchestrator-direct`; anything else
# (including a missing entry) routes to `executor`.
ROUTE_FACTS_HEADING = "### Route Facts"
ELIGIBILITY_FACTS = (
    "small_bounded_scope",
    "unambiguous_requirements",
    "known_dependency_impact",
    "no_useful_decomposition",
    "safe_verification",
    "dirty_work_preserved",
)
SENSITIVE_FLAGS = (
    "auth_or_authorization",
    "permissions",
    "destructive_operation",
    "irreversible_data_change",
    "secrets",
    "external_writes",
    "migrations",
    "broad_api_or_architecture",
    "large_surface",
)
ROUTE_VALUES = ("true", "false", "uncertain")

SNAPSHOT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "review-loop evidence ledger",
    "GIT_AUTHOR_EMAIL": "review-loop@localhost",
    "GIT_COMMITTER_NAME": "review-loop evidence ledger",
    "GIT_COMMITTER_EMAIL": "review-loop@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}


class UsageError(Exception):
    """Bad arguments or an illegal record/reuse. Exit 2, nothing written."""


class StorageError(Exception):
    """Git plumbing failed or the snapshot could not be stored. Exit 3."""


# ---------------------------------------------------------------- utilities


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> _dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = _dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def git(repo: str, *args: str, env: Optional[dict] = None,
        input_bytes: Optional[bytes] = None, ok_codes: Tuple[int, ...] = (0,)) -> bytes:
    final_env = os.environ.copy()
    if env:
        final_env.update(env)
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            env=final_env,
            input=input_bytes,
            capture_output=True,
            check=False,
        )
    except OSError as exc:  # git missing, cwd gone, ...
        raise StorageError(f"git {' '.join(args)}: {exc}") from exc
    if completed.returncode not in ok_codes:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise StorageError(f"git {' '.join(args)} failed (exit {completed.returncode}): {detail}")
    return completed.stdout


def git_text(repo: str, *args: str, **kwargs) -> str:
    return git(repo, *args, **kwargs).decode("utf-8", errors="replace").strip()


def resolve_repo(path: str) -> str:
    try:
        return git_text(path, "rev-parse", "--show-toplevel")
    except StorageError as exc:
        raise UsageError(f"not a git repository: {path} ({exc})") from exc


def current_head(repo: str) -> Optional[str]:
    # Only exit 1 with empty output (unborn HEAD) means "no head"; any other
    # git failure propagates as StorageError (exit 3).
    out = git_text(repo, "rev-parse", "--verify", "-q", "HEAD", ok_codes=(0, 1))
    return out or None


def parse_now(value: Optional[str]) -> _dt.datetime:
    """`--now` override (tests) or the current UTC time; a malformed
    timestamp is a usage error (exit 2), never an internal error."""
    if value is None:
        return _dt.datetime.now(_dt.timezone.utc)
    try:
        return parse_iso(value)
    except ValueError as exc:
        raise UsageError(f"--now {value!r} is not an ISO-8601 timestamp: {exc}") from exc


def glob_to_regex(pattern: str) -> "re.Pattern[str]":
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def selector_matches(selector: dict, path: str) -> bool:
    kind = selector.get("kind")
    pattern = selector.get("pattern", "")
    if kind == "dir":
        normalized = pattern.rstrip("/")
        # `dir:X` covers everything under X/ and a gitlink (submodule) stored
        # at exactly X — the submodule *is* the content under that directory.
        return path == normalized or path.startswith(normalized + "/")
    return glob_to_regex(pattern).match(path) is not None


# Content identity = "<git mode>:<object id>". Every path in a worktree state,
# a snapshot tree, an `inputs` / `deps` binding and a selector digest carries
# its git mode, so an executable-bit flip (100644 ↔ 100755), a file ↔ symlink
# type change, or a submodule pointer move (160000:<commit>) changes the
# identity even when the object id does not. A bare object id (a record
# written before modes were bound) never matches: it is non-reusable.
GITLINK_MODE = "160000"
# The oid part of a gitlink identity is `<commit>` or, for a checked-out
# submodule / nested repository with uncommitted or untracked content,
# `<commit>+dirty:<sha256>` (see `nested_repo_dirty_digest`). The mode stays
# 160000 and `split_identity` hands the suffix back intact, so a dirty and a
# clean state of the same commit never compare equal.
DIRTY_SUFFIX = "+dirty:"
_IDENTITY_RE = re.compile(r"^(\d{6}):([0-9a-f]{40,64}(?:\+dirty:[0-9a-f]{64})?)$")


def plain_oid(oid: str) -> str:
    """The git object id of an identity's oid part without any `+dirty:`
    suffix — the only part a git tree or `git diff --raw` can carry."""
    return oid.split(DIRTY_SUFFIX, 1)[0]


def make_identity(mode: str, oid: str) -> str:
    return f"{mode}:{oid}"


def split_identity(identity: str) -> Optional[Tuple[str, str]]:
    """(mode, oid) for a `<mode>:<oid>` identity; None for a legacy bare oid
    or anything else that is not a mode-bound identity."""
    match = _IDENTITY_RE.match(identity)
    return (match.group(1), match.group(2)) if match else None


def members_digest(selector: dict, paths: Dict[str, str]) -> str:
    """sha256 over the sorted (path, mode, oid) triples currently matching the
    selector. `paths` maps path → `<mode>:<oid>` identity."""
    triples = []
    for p, identity in paths.items():
        if not selector_matches(selector, p):
            continue
        parsed = split_identity(identity)
        mode, oid = parsed if parsed else ("", identity)
        triples.append((p, mode, oid))
    payload = "\n".join(f"{p}\t{mode}\t{oid}" for p, mode, oid in sorted(triples)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def in_scope(path: str, scope: List[str]) -> bool:
    if not scope:
        return True
    for prefix in scope:
        normalized = prefix.rstrip("/")
        if path == normalized or path.startswith(normalized + "/"):
            return True
    return False


# ------------------------------------------------------------ worktree state


def _split_nul(data: bytes) -> List[str]:
    return [item.decode("utf-8", errors="surrogateescape") for item in data.split(b"\0") if item]


def head_paths(repo: str) -> set:
    if current_head(repo) is None:
        return set()
    return set(_split_nul(git(repo, "ls-tree", "-r", "-z", "--name-only", "HEAD")))


def porcelain_renames(repo: str) -> List[dict]:
    """Rename/copy records from `git status --porcelain=v2 -z`."""
    entries = git(repo, "status", "--porcelain=v2", "-z").split(b"\0")
    renames: List[dict] = []
    i = 0
    while i < len(entries):
        entry = entries[i].decode("utf-8", errors="surrogateescape")
        if entry.startswith("2 "):
            fields = entry.split(" ", 9)
            score = fields[8] if len(fields) > 8 else ""
            new_path = fields[9] if len(fields) > 9 else ""
            orig = entries[i + 1].decode("utf-8", errors="surrogateescape") if i + 1 < len(entries) else ""
            renames.append({"from": orig, "to": new_path, "kind": "copy" if score.startswith("C") else "rename"})
            i += 2
            continue
        i += 1
    return renames


def index_entries(repo: str) -> Dict[str, Tuple[str, str]]:
    """path → (mode, oid) from `git ls-files -s` (first stage entry wins, so
    an unmerged path is still listed once). Gitlinks appear with mode 160000."""
    entries: Dict[str, Tuple[str, str]] = {}
    for item in _split_nul(git(repo, "ls-files", "-z", "-s")):
        meta, _, path = item.partition("\t")
        if path not in entries:
            parts = meta.split(" ")
            if len(parts) >= 3:
                entries[path] = (parts[0], parts[1])
    return entries


def gitlink_worktree_oid(repo: str, path: str, index_oid: str) -> str:
    """Commit the submodule at `path` currently points to: its checked-out
    HEAD when a repository lives there (`.git` file or directory present —
    without that guard `git rev-parse` would walk up and answer with the
    superproject's HEAD), else the index's recorded pointer."""
    full = os.path.join(repo, path)
    if not os.path.lexists(os.path.join(full, ".git")):
        return index_oid
    # Only an unborn HEAD (exit 1, empty output) falls back to the index
    # pointer; a broken `.git` file or any other git failure (exit 128, ...)
    # is a storage failure naming the submodule path.
    try:
        out = git_text(full, "rev-parse", "--verify", "-q", "HEAD", ok_codes=(0, 1))
    except StorageError as exc:
        raise StorageError(f"submodule {path}: {exc}") from exc
    return out or index_oid


# A checked-out submodule or nested repository is bound to its HEAD commit
# plus, when `git status` reports anything inside it, a deterministic digest
# of that dirty content (`<commit>+dirty:<digest>`), so uncommitted tracked
# changes and untracked files under the declared path invalidate evidence
# like a commit does, within the `git status` boundary documented in the
# module docstring. Nested-nested repositories are bound
# recursively with the same rule up to this depth; deeper nesting fails
# closed (StorageError naming the path, exit 3).
MAX_NESTED_REPO_DEPTH = 4


def _nested_status_entries(full: str, label: str) -> List[Tuple[str, str]]:
    """(XY, path) for every entry of `git status --porcelain=v1 -z
    --untracked-files=all` in the repository at `full` (ignored files are
    excluded, as git does). A rename / copy contributes both its new path and
    its original path. `--no-optional-locks` keeps git from refreshing the
    nested index: nothing of the user's is touched."""
    try:
        raw = git(full, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all")
    except StorageError as exc:
        raise StorageError(f"nested repository {label}: {exc}") from exc
    items = raw.split(b"\0")
    out: List[Tuple[str, str]] = []
    i = 0
    while i < len(items):
        text = items[i].decode("utf-8", errors="surrogateescape")
        i += 1
        if len(text) < 4:
            continue
        xy, path = text[:2], text[3:].rstrip("/")
        out.append((xy, path))
        if ("R" in xy or "C" in xy) and i < len(items):
            out.append((xy, items[i].decode("utf-8", errors="surrogateescape").rstrip("/")))
            i += 1
    return out


def nested_repo_identity_oid(full: str, label: str, index_oid: str, depth: int) -> str:
    """`<commit>` or `<commit>+dirty:<digest>` for the repository at `full`
    (`label` names it in errors; `index_oid` is the recorded pointer used when
    it has no commit). `depth` is 1 for a repository directly under the
    superproject; beyond MAX_NESTED_REPO_DEPTH the binding fails closed."""
    if depth > MAX_NESTED_REPO_DEPTH:
        raise StorageError(
            f"nested repository {label}: nested more than {MAX_NESTED_REPO_DEPTH} levels deep, cannot be bound"
        )
    try:
        head = git_text(full, "rev-parse", "--verify", "-q", "HEAD", ok_codes=(0, 1))
    except StorageError as exc:
        raise StorageError(f"nested repository {label}: {exc}") from exc
    commit = head or index_oid
    digest = nested_repo_dirty_digest(full, label, depth)
    return commit + (DIRTY_SUFFIX + digest if digest else "")


def nested_repo_dirty_digest(full: str, label: str, depth: int) -> Optional[str]:
    """sha256 over the sorted `(XY, path, oid)` triples of everything `git
    status` reports inside the repository at `full`; None when it is clean.
    `oid` is the worktree content of `path`: `git hash-object` of a regular
    file or of a symlink's target, `<deleted>` for a missing path, and a
    nested-nested repository's own `<commit>[+dirty:<digest>]` (recursive,
    depth-limited). A directory that is not a repository cannot be bound.

    `git status` selects the entries; the digest then binds their sorted
    `(XY, path, current content oid)` triples, reading each selected path's
    oid from disk. So the digest moves whenever a selected path's `XY` or its
    content changes, including a further edit to a path porcelain already
    reports. Two things bound it. Mode and type are not bound directly: they
    reach the digest only insofar as they change a path's `XY` or content
    oid, so a mode or type change leaving both untouched is invisible. And a
    path status never selects — content under `submodule.<name>.ignore` on a
    nested-nested submodule or under `skip-worktree` / `assume-unchanged`,
    and a directory git could not open (git exits 0 and only warns on
    stderr) — is absent from the digest entirely. This is a documented
    boundary of the nested case, not of superproject paths, which are bound
    from disk with their own mode."""
    entries = _nested_status_entries(full, label)
    if not entries:
        return None
    triples: List[Tuple[str, str, str]] = []
    regular: List[Tuple[str, str]] = []
    for xy, path in entries:
        target = os.path.join(full, path)
        if not os.path.lexists(target):
            triples.append((xy, path, "<deleted>"))
        elif os.path.islink(target):
            link = os.readlink(target).encode("utf-8", errors="surrogateescape")
            triples.append((xy, path, git_text(full, "hash-object", "--stdin", input_bytes=link)))
        elif os.path.isdir(target):
            if not os.path.lexists(os.path.join(target, ".git")):
                raise StorageError(
                    f"nested repository {label}: {path} is a directory but not a repository, cannot be bound"
                )
            pointer = git_text(full, "ls-files", "-s", "--", path)  # `<mode> <oid> <stage>\t<path>` or empty
            index_oid = pointer.split(" ")[1] if pointer.startswith(GITLINK_MODE + " ") else "<unborn>"
            triples.append((xy, path, nested_repo_identity_oid(target, f"{label}/{path}", index_oid, depth + 1)))
        else:
            regular.append((xy, path))
    if regular:
        payload = "".join(p + "\n" for _, p in regular).encode("utf-8", errors="surrogateescape")
        try:
            out = git_text(full, "hash-object", "--stdin-paths", input_bytes=payload)
        except StorageError as exc:
            raise StorageError(f"nested repository {label}: {exc}") from exc
        shas = out.split("\n") if out else []
        if len(shas) != len(regular):
            raise StorageError(f"nested repository {label}: hash-object returned an unexpected number of identities")
        triples.extend((xy, p, sha) for (xy, p), sha in zip(regular, shas))
    digest_payload = "\n".join(f"{xy}\t{p}\t{oid}" for xy, p, oid in sorted(triples))
    return hashlib.sha256(digest_payload.encode("utf-8", errors="surrogateescape")).hexdigest()


# Untracked content under the session directory is never in scope: the
# orchestrator writes the session file at every step, and those writes must
# never surface in `classify.changed_paths` or a delta.
SESSION_DIR_PREFIX = ".review-loop/"


def _partition_untracked(repo: str, scope: List[str]) -> Tuple[List[str], Dict[str, str], List[dict]]:
    """(untracked paths, nested repositories → HEAD, skipped nested entries).

    `git ls-files --others` lists an untracked nested repository as `dir/`
    (trailing slash). Such an entry is bound like a gitlink to the nested
    repository's checked-out HEAD instead of being hashed as a file; a
    directory listed that way without a `.git` inside, or a nested
    repository without any commit, cannot be bound and is skipped (listed
    under `nested_repos_skipped`)."""
    untracked: List[str] = []
    nested: Dict[str, str] = {}
    skipped: List[dict] = []
    for raw in _split_nul(git(repo, "ls-files", "-z", "--others", "--exclude-standard")):
        path = raw.rstrip("/")
        if not path or raw.startswith(SESSION_DIR_PREFIX) or not in_scope(path, scope):
            continue
        if not raw.endswith("/"):
            untracked.append(path)
            continue
        if not os.path.lexists(os.path.join(repo, path, ".git")):
            skipped.append({"path": path, "reason": "listed as a directory but no .git inside"})
            continue
        oid = gitlink_worktree_oid(repo, path, "")
        if not oid:
            skipped.append({"path": path, "reason": "nested repository has no commit (unborn HEAD)"})
            continue
        nested[path] = oid
        untracked.append(path)
    return untracked, nested, skipped


def worktree_state(repo: str, scope: List[str], write_objects: bool) -> dict:
    """Current content identities (`path → "<mode>:<oid>"`): tracked paths
    present on disk plus in-scope untracked, non-ignored paths. Submodule
    gitlinks are bound as `160000:<commit>` (the checked-out HEAD when the
    submodule is present, else the index pointer), with `+dirty:<digest>`
    appended when `git status` inside a present submodule / nested
    repository reports anything (`gitlink_dirty` maps those paths to their
    digest); see `nested_repo_dirty_digest` for what status does not report.
    Deleted tracked paths
    (index or HEAD) are returned as tombstones. When `write_objects` is true
    the blobs are stored (`hash-object -w`) so a snapshot can reference them."""
    index = index_entries(repo)
    untracked, nested_repos, nested_skipped = _partition_untracked(repo, scope)
    deleted = set()
    candidates: List[str] = []
    gitlinks: Dict[str, str] = {}
    for path, (mode, oid) in index.items():
        full = os.path.join(repo, path)
        if not os.path.lexists(full):
            deleted.add(path)
            continue
        if mode == GITLINK_MODE:
            gitlinks[path] = gitlink_worktree_oid(repo, path, oid)
            continue
        if os.path.isdir(full) and not os.path.islink(full):
            continue  # a tracked file replaced by a directory: not content this helper binds
        candidates.append(path)
    for path in head_paths(repo):
        if path not in index and not os.path.lexists(os.path.join(repo, path)):
            deleted.add(path)
    candidates.extend(p for p in untracked if p not in nested_repos)
    gitlinks.update(nested_repos)

    regular: List[str] = []
    symlinks: List[str] = []
    modes: Dict[str, str] = {}
    for path in candidates:
        st = os.lstat(os.path.join(repo, path))
        is_symlink = stat.S_ISLNK(st.st_mode)
        if is_symlink:
            symlinks.append(path)
            mode = "120000"
        else:
            regular.append(path)
            mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
        modes[path] = mode

    identities: Dict[str, str] = {}
    write_flag = ["-w"] if write_objects else []
    if regular:
        payload = "".join(p + "\n" for p in regular).encode("utf-8", errors="surrogateescape")
        out = git_text(repo, "hash-object", *write_flag, "--stdin-paths", input_bytes=payload)
        shas = out.split("\n") if out else []
        if len(shas) != len(regular):
            raise StorageError("hash-object returned an unexpected number of identities")
        for path, sha in zip(regular, shas):
            identities[path] = make_identity(modes[path], sha)
    for path in symlinks:
        target = os.readlink(os.path.join(repo, path)).encode("utf-8", errors="surrogateescape")
        identities[path] = make_identity(
            modes[path], git_text(repo, "hash-object", *write_flag, "--stdin", input_bytes=target)
        )
    gitlink_dirty: Dict[str, str] = {}
    for path, oid in gitlinks.items():
        if os.path.lexists(os.path.join(repo, path, ".git")):
            digest = nested_repo_dirty_digest(os.path.join(repo, path), path, 1)
            if digest:
                gitlink_dirty[path] = digest
                oid = oid + DIRTY_SUFFIX + digest
        identities[path] = make_identity(GITLINK_MODE, oid)

    return {
        "paths": identities,
        "untracked": sorted(set(untracked)),
        "deleted": sorted(deleted),
        "renames": porcelain_renames(repo),
        "nested_repos_skipped": nested_skipped,
        "gitlink_dirty": gitlink_dirty,
    }


def snapshot_tree_paths(repo: str, snap: dict) -> Dict[str, str]:
    """path → `<mode>:<oid>` for every entry of a stored snapshot's tree
    (blobs, symlinks and 160000 gitlinks alike). A git tree can only hold a
    gitlink's commit, so the `+dirty:<digest>` suffix of a dirty nested
    repository is re-applied from the snapshot entry's `gitlink_dirty` map."""
    out = git(repo, "ls-tree", "-r", "-z", snap["tree"])
    dirty = snap.get("gitlink_dirty") or {}
    paths: Dict[str, str] = {}
    for entry in _split_nul(out):
        meta, _, path = entry.partition("\t")
        parts = meta.split(" ")
        if len(parts) >= 3:
            oid = parts[2]
            if parts[0] == GITLINK_MODE and path in dirty:
                oid += DIRTY_SUFFIX + dirty[path]
            paths[path] = make_identity(parts[0], oid)
    return paths


# ------------------------------------------------------------- session file


def session_path(args) -> str:
    if getattr(args, "session_file", None):
        return os.path.abspath(args.session_file)
    if not args.session:
        raise UsageError("--session <uuid> is required")
    return os.path.join(args.repo, args.sessions_dir, f"{args.session}.md")


def read_session(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise UsageError(f"cannot read session file {path}: {exc}") from exc


def write_session(path: str, text: str) -> None:
    tmp = path + ".evidence-ledger.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise StorageError(f"cannot write session file {path}: {exc}") from exc


_SECTION_RE = re.compile(r"^## .*$", re.MULTILINE)


def _section_headings(text: str) -> List[Tuple[int, str]]:
    """(offset, stripped heading line) for every top-level `## ` heading."""
    out: List[Tuple[int, str]] = []
    for match in _SECTION_RE.finditer(text):
        start = match.start()
        line_end = text.find("\n", start)
        out.append((start, text[start:line_end if line_end != -1 else len(text)].strip()))
    return out


# Canonical sections are identified by their heading AND their canonical
# successor heading (session-file.md §Canonical sections), never by the first
# matching heading: a plan or a history entry may quote the same heading.
# `None` means "the last heading in the file".
CANONICAL_SUCCESSOR: Dict[str, Optional[str]] = {
    PACKET_HEADING: "## Review History",
    LEDGER_HEADING: METADATA_HEADING,
    METADATA_HEADING: None,
}


def canonical_section_candidates(text: str, heading: str) -> List[Tuple[int, int]]:
    """Every (start, end) span whose heading is `heading` and whose next
    top-level heading is the canonical successor (or which is the last
    heading when the successor is None)."""
    successor = CANONICAL_SUCCESSOR[heading]
    headings = _section_headings(text)
    spans: List[Tuple[int, int]] = []
    for idx, (start, line) in enumerate(headings):
        if line != heading:
            continue
        following = headings[idx + 1][1] if idx + 1 < len(headings) else None
        if following != successor:
            continue
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(text)
        spans.append((start, end))
    return spans


def find_canonical_section(text: str, heading: str) -> Optional[Tuple[int, int]]:
    """Span of the canonical `heading` section (heading line included), None
    when absent. More than one structural candidate is ambiguous → UsageError
    (exit 2; a writer writes nothing)."""
    spans = canonical_section_candidates(text, heading)
    if len(spans) > 1:
        raise UsageError(
            f"canonical `{heading}` section not uniquely identifiable ({len(spans)} candidates: a heading "
            f"followed by `{CANONICAL_SUCCESSOR[heading] or 'end of file'}` appears more than once)"
        )
    return spans[0] if spans else None


def empty_ledger() -> dict:
    return {"schema": LEDGER_SCHEMA, "scope": [], "snapshots": [], "records": []}


_FENCE_RE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)

# Every ledger record must carry these keys (a record written by `record`
# always does); a record without them is a malformed ledger, not a crash.
REQUIRED_RECORD_KEYS = ("id", "claim_id", "check", "superseded_by", "recorded_at")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_ledger_shape(ledger: dict) -> None:
    """Shape check run once at load time: records / snapshots are lists;
    every record carries `REQUIRED_RECORD_KEYS` with an integer `id`, an
    ISO-8601 `recorded_at` and an integer `freshness.seconds` when
    `freshness.kind` is `ttl`; every snapshot has an integer `n` and string
    `tree` / `commit` and, when present, a `gitlink_dirty` path → digest
    string map; `triage.next_id` is an integer and `triage.disputes`
    / `triage.pending_rubric_incomplete` are lists when present. Raises
    UsageError (exit 2) naming the offending record / snapshot / key."""
    for key in ("records", "snapshots"):
        if not isinstance(ledger.get(key), list):
            raise UsageError(f"`{LEDGER_HEADING}` key {key!r} must be a list")
    for idx, rec in enumerate(ledger["records"]):
        if not isinstance(rec, dict):
            raise UsageError(f"ledger record #{idx} is not an object")
        label = f"ledger record {rec['id']!r}" if "id" in rec else f"ledger record #{idx}"
        for key in REQUIRED_RECORD_KEYS:
            if key not in rec:
                raise UsageError(f"{label} lacks required key {key!r}")
        if not _is_int(rec["id"]):
            raise UsageError(f"{label} has a non-integer id")
        if not isinstance(rec["recorded_at"], str):
            raise UsageError(f"{label} key 'recorded_at' must be an ISO-8601 string, got {rec['recorded_at']!r}")
        try:
            parse_iso(rec["recorded_at"])
        except ValueError as exc:
            raise UsageError(f"{label} key 'recorded_at' is not an ISO-8601 timestamp: {exc}") from exc
        freshness = rec.get("freshness")
        if isinstance(freshness, dict) and freshness.get("kind") == "ttl" and not _is_int(freshness.get("seconds")):
            raise UsageError(f"{label} key 'freshness.seconds' must be an integer for kind 'ttl', "
                             f"got {freshness.get('seconds')!r}")
    for idx, snap in enumerate(ledger["snapshots"]):
        if not isinstance(snap, dict):
            raise UsageError(f"ledger snapshot #{idx} is not an object")
        label = f"ledger snapshot {snap['n']!r}" if "n" in snap else f"ledger snapshot #{idx}"
        if not _is_int(snap.get("n")):
            raise UsageError(f"{label} key 'n' must be an integer, got {snap.get('n')!r}")
        for key in ("tree", "commit"):
            if not isinstance(snap.get(key), str):
                raise UsageError(f"{label} key {key!r} must be a string, got {snap.get(key)!r}")
        dirty = snap.get("gitlink_dirty")
        if dirty is not None and (not isinstance(dirty, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in dirty.items())):
            raise UsageError(f"{label} key 'gitlink_dirty' must map path strings to digest strings, got {dirty!r}")
    triage = ledger.get("triage")
    if triage is not None:
        if not isinstance(triage, dict):
            raise UsageError("`triage` object in the `## Evidence Ledger` block is malformed")
        if "next_id" in triage and not _is_int(triage["next_id"]):
            raise UsageError(f"`triage.next_id` must be an integer, got {triage['next_id']!r}")
        for key in ("disputes", "pending_rubric_incomplete"):
            if key in triage and not isinstance(triage[key], list):
                raise UsageError(f"`triage.{key}` must be a list, got {triage[key]!r}")


def load_ledger(text: str) -> dict:
    span = find_canonical_section(text, LEDGER_HEADING)
    if span is None:
        return empty_ledger()
    body = text[span[0]:span[1]]
    match = _FENCE_RE.search(body)
    if not match:
        raise UsageError(f"`{LEDGER_HEADING}` section has no ```json block; refusing to overwrite it")
    try:
        ledger = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise UsageError(f"`{LEDGER_HEADING}` JSON block is malformed: {exc}") from exc
    if not isinstance(ledger, dict):
        raise UsageError(f"`{LEDGER_HEADING}` JSON block must be an object")
    for key, default in empty_ledger().items():
        ledger.setdefault(key, default)
    validate_ledger_shape(ledger)
    return ledger


def render_ledger(ledger: dict) -> str:
    return (
        f"{LEDGER_HEADING}\n\n```json\n"
        + json.dumps(ledger, indent=2, sort_keys=False)
        + "\n```\n\n"
    )


def store_ledger(text: str, ledger: dict) -> str:
    block = render_ledger(ledger)
    span = find_canonical_section(text, LEDGER_HEADING)
    if span is not None:
        return text[:span[0]] + block + text[span[1]:]
    meta = find_canonical_section(text, METADATA_HEADING)
    if meta is None and any(line == METADATA_HEADING for _, line in _section_headings(text)):
        raise UsageError(f"`{METADATA_HEADING}` is present but is not the last section; refusing to place the ledger")
    if meta is not None:
        prefix = text[:meta[0]]
        if not prefix.endswith("\n\n"):
            prefix = prefix.rstrip("\n") + "\n\n"
        return prefix + block + text[meta[0]:]
    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + block


_STAGES_LINE_RE = re.compile(r"^- completed_stages:.*$", re.MULTILINE)


def render_stages(stages: List[str]) -> str:
    return "[" + ", ".join(stages) + "]"


def store_completed_stages(text: str, stages: List[str]) -> str:
    line = f"- completed_stages: {render_stages(stages)}"
    meta = find_canonical_section(text, METADATA_HEADING)
    if meta is None:
        raise UsageError(f"session file has no canonical `{METADATA_HEADING}` section (it must be the last section)")
    body = text[meta[0]:meta[1]]
    if _STAGES_LINE_RE.search(body):
        body = _STAGES_LINE_RE.sub(line, body, count=1)
    else:
        body = body.rstrip("\n") + "\n" + line + "\n"
    return text[:meta[0]] + body + text[meta[1]:]


# ------------------------------------------------------------------- records


def canonical_check(name: str) -> str:
    if name in DEFAULT_STAGE_BY_CHECK or name in {"lint"}:
        return name
    if name.startswith("agent_review:") and len(name) > len("agent_review:"):
        return name
    if name.startswith("manual:") and len(name) > len("manual:"):
        return name
    raise UsageError(
        f"unknown check {name!r}; expected one of "
        "reviewer_approve, gate, tests, lint, static_analysis, simplify, "
        "docs_consistency, security_scan, agent_review:<name>, manual:<desc>"
    )


def scope_key(check: str, args) -> str:
    if check in PATH_SCOPED_CHECKS or check.startswith("agent_review:"):
        if args.scope_path:
            return "|".join(sorted(set(args.scope_path)))
        if args.scope:
            return args.scope
        raise UsageError(f"{check} requires --scope-path (review-target paths) or --scope")
    if check in COMMAND_SCOPED_CHECKS or check == "lint":
        if args.scope:
            return args.scope
        if args.env_command:
            return args.env_command
        raise UsageError(f"{check} requires --scope (the command line) or --env-command")
    if check in SESSION_SCOPED_CHECKS:
        return "session"
    if check.startswith("manual:"):
        return check[len("manual:"):]
    raise UsageError(f"cannot derive a scope key for {check}")


def parse_freshness(value: Optional[str]) -> Optional[dict]:
    if value is None:
        return None
    if value == "bound-to-head":
        return {"kind": "bound-to-head"}
    if value.startswith("ttl:"):
        try:
            seconds = int(value[len("ttl:"):])
        except ValueError as exc:
            raise UsageError(f"invalid freshness {value!r}; expected ttl:<seconds>") from exc
        if seconds < 0:
            raise UsageError("ttl seconds must be non-negative")
        return {"kind": "ttl", "seconds": seconds}
    raise UsageError(f"invalid freshness {value!r}; expected ttl:<seconds> or bound-to-head")


def parse_selector(value: str) -> dict:
    kind, sep, pattern = value.partition(":")
    if not sep or kind not in SELECTOR_KINDS or not pattern:
        raise UsageError(f"invalid selector {value!r}; expected <glob|dir|discovery>:<pattern>")
    return {"kind": kind, "pattern": pattern}


def bind_path(path: str, snap: dict, tree: Dict[str, str], role: str) -> str:
    """Identity of `path` in the bound snapshot: `<mode>:<oid>`,
    `<untracked:<mode>:<oid>>` or the `<deleted>` tombstone."""
    if path in tree:
        identity = tree[path]
        if path in set(snap.get("untracked", [])):
            return f"<untracked:{identity}>"
        return identity
    if path in set(snap.get("deleted", [])):
        return "<deleted>"
    raise UsageError(
        f"{role} path {path!r} is neither in snapshot {snap['n']} nor a recorded tombstone"
    )


def mark_supersession(ledger: dict) -> None:
    """Implicit supersession: for each claim_id the highest-id record is
    active. An explicit `superseded_by` mark (from `record --supersedes`) is
    never cleared — a superseded record can never become active again."""
    latest: Dict[str, int] = {}
    for rec in ledger["records"]:
        latest[rec["claim_id"]] = max(latest.get(rec["claim_id"], -1), rec["id"])
    for rec in ledger["records"]:
        active_id = latest[rec["claim_id"]]
        if rec["id"] != active_id and rec.get("superseded_by") is None:
            rec["superseded_by"] = active_id


def active_records(ledger: dict) -> Dict[str, dict]:
    mark_supersession(ledger)
    return {rec["claim_id"]: rec for rec in ledger["records"] if rec["superseded_by"] is None}


def is_path_scoped(check: str) -> bool:
    return check in PATH_SCOPED_CHECKS or check.startswith("agent_review:")


def scope_covers(check: str, new_scope: str, old_scope: str) -> bool:
    """Does the new record's scope key cover the old one? Path-scoped checks:
    the new sorted path set must be a superset of the old one. Command /
    literal scopes: the keys must be identical."""
    if is_path_scoped(check):
        return set(old_scope.split("|")) <= set(new_scope.split("|"))
    return new_scope == old_scope


def validate_supersedes(ledger: dict, targets: List[int], check: str, scope: str,
                        disposition: str, result: Optional[str]) -> List[dict]:
    """Guard for `record --supersedes`. Returns the target records or raises
    UsageError (exit 2, nothing written)."""
    mark_supersession(ledger)
    by_id = {r["id"]: r for r in ledger["records"]}
    out: List[dict] = []
    for target_id in targets:
        target = by_id.get(target_id)
        if target is None:
            raise UsageError(f"--supersedes target {target_id} does not exist")
        if target["check"] != check:
            raise UsageError(
                f"--supersedes target {target_id} is check {target['check']!r}, not {check!r}; "
                "only records of the same check may be superseded"
            )
        if target.get("superseded_by") is not None:
            raise UsageError(
                f"--supersedes target {target_id} is already superseded by {target['superseded_by']}"
            )
        if target.get("disposition") == "executed" and target.get("result") == "FAIL":
            if disposition != "executed" or result != "PASS":
                raise UsageError(
                    f"--supersedes target {target_id} is a FAIL; only an executed PASS of the same "
                    "check may supersede it (failure dominance)"
                )
            if not scope_covers(check, scope, target["scope"]):
                raise UsageError(
                    f"--supersedes target {target_id} is a FAIL with scope {target['scope']!r}; "
                    f"the new scope {scope!r} does not cover it (failure dominance)"
                )
        out.append(target)
    return out


def _pruned_error(when: str) -> UsageError:
    return UsageError(f"snapshots were pruned at {when}; run `snapshot` to store a new baseline")


def latest_snapshot(ledger: dict) -> Optional[dict]:
    if ledger.get("pruned_at"):
        raise _pruned_error(ledger["pruned_at"])
    return ledger["snapshots"][-1] if ledger["snapshots"] else None


def latest_nested_repos_skipped(ledger: dict) -> List[dict]:
    """`nested_repos_skipped` of the last stored snapshot entry (pruned or
    not): the region the baseline could not bind, carried into `check` /
    `classify` output so it is never invisible."""
    if not ledger["snapshots"]:
        return []
    return list(ledger["snapshots"][-1].get("nested_repos_skipped", []))


def snapshot_by_n(ledger: dict, n: int) -> dict:
    if ledger.get("pruned_at"):
        raise _pruned_error(ledger["pruned_at"])
    for snap in ledger["snapshots"]:
        if snap["n"] == n:
            if snap.get("pruned_at"):
                raise _pruned_error(snap["pruned_at"])
            return snap
    raise UsageError(f"snapshot {n} is not recorded in the ledger")


# ---------------------------------------------------------------- evaluation


def evaluate_record(rec: dict, repo: str, state: dict, head: Optional[str], now: _dt.datetime) -> Tuple[bool, List[str], str]:
    """Return (valid, reasons, satisfied_state)."""
    reasons: List[str] = []
    check = rec["check"]
    disposition = rec.get("disposition")
    satisfied_state = ""

    if disposition == "executed":
        if rec.get("result") == "PASS":
            satisfied_state = "executed:PASS"
        else:
            reasons.append("FAIL")
    elif disposition == "not-applicable":
        if check in NOT_APPLICABLE_CHECKS:
            satisfied_state = "not-applicable"
        else:
            reasons.append(f"not-applicable is not permitted for {check}")
    elif disposition == "controlled-skip":
        if check in CONTROLLED_SKIP_CHECKS:
            satisfied_state = "controlled-skip"
        else:
            reasons.append(f"controlled-skip is not permitted for {check}")
    else:
        reasons.append(f"unknown disposition {disposition!r}")

    if rec.get("closure") != "declared":
        reasons.append("uncertain closure")
    deps = rec.get("deps")
    if deps == "uncertain":
        reasons.append("deps uncertain")

    current = state["paths"]
    for role, mapping in (("input", rec.get("inputs") or {}), ("dep", deps if isinstance(deps, dict) else {})):
        for path, expected in sorted(mapping.items()):
            actual = current.get(path)
            if expected == "<deleted>":
                if actual is not None:
                    reasons.append(f"tombstone reappeared: {path}")
                continue
            expected_identity = expected
            if expected.startswith("<untracked:") and expected.endswith(">"):
                expected_identity = expected[len("<untracked:"):-1]
            expected_parsed = split_identity(expected_identity)
            if expected_parsed is None:
                # A bare object id from a ledger written before git modes were
                # bound: it can never be shown byte- and mode-identical.
                reasons.append(f"{role} identity lacks git mode (legacy record, not reusable): {path}")
                continue
            if actual is None:
                reasons.append(f"{role} deleted: {path}")
                continue
            actual_parsed = split_identity(actual)
            if actual_parsed is None or actual_parsed[1] != expected_parsed[1]:
                reasons.append(f"changed {role}: {path}")
            elif actual_parsed[0] != expected_parsed[0]:
                reasons.append(f"{role} mode changed {expected_parsed[0]}→{actual_parsed[0]}: {path}")

    for selector in rec.get("selectors") or []:
        if members_digest(selector, current) != selector.get("members_digest"):
            reasons.append(f"selector digest changed: {selector.get('kind')}:{selector.get('pattern')}")

    if rec.get("unresolved"):
        reasons.append("unresolved findings")

    if rec.get("requires_freshness"):
        freshness = rec.get("freshness")
        if not freshness:
            reasons.append("missing freshness")
        elif freshness.get("kind") == "ttl":
            recorded = parse_iso(rec["recorded_at"])
            if recorded + _dt.timedelta(seconds=int(freshness.get("seconds", 0))) < now:
                reasons.append("expired ttl")
        elif freshness.get("kind") == "bound-to-head":
            if rec.get("head") != head:
                reasons.append("head moved")
        else:
            reasons.append(f"unknown freshness kind {freshness.get('kind')!r}")

    valid = not reasons
    if valid:
        reasons = ["byte-identical" if satisfied_state == "executed:PASS" else satisfied_state]
    return valid, reasons, satisfied_state


def evaluate_ledger(ledger: dict, repo: str, state: dict, now: _dt.datetime) -> dict:
    head = current_head(repo)
    active = active_records(ledger)
    claims: Dict[str, dict] = {}
    for claim_id, rec in sorted(active.items(), key=lambda kv: kv[1]["id"]):
        valid, reasons, satisfied_state = evaluate_record(rec, repo, state, head, now)
        claims[claim_id] = {
            "id": rec["id"],
            "check": rec["check"],
            "stage": rec.get("stage"),
            "disposition": rec.get("disposition"),
            "result": rec.get("result"),
            "provenance": rec.get("provenance"),
            "why": rec.get("why"),
            "author_route": rec.get("author_route"),
            "snapshot": rec.get("snapshot"),
            "valid": valid,
            "satisfied_state": satisfied_state if valid else "",
            "reasons": reasons,
        }

    # A stage is present iff EVERY active (non-superseded) record of each
    # required check is valid and in a permitted satisfying state. There is
    # no newest-record-per-check selection across claim_ids: an active FAIL
    # of a required check blocks the stage whatever its scope, until it is
    # superseded (implicitly by a higher-id record of the same claim_id, or
    # explicitly via `record --supersedes` under the FAIL supersession guard).
    stages: Dict[str, dict] = {}
    completed: List[str] = []
    for stage in STAGE_ORDER:
        missing: List[str] = []
        active_ids: Dict[str, List[int]] = {}
        for check, permitted in REQUIRED_CLAIMS[stage].items():
            candidates = sorted((c for c in claims.values() if c["check"] == check), key=lambda c: c["id"])
            active_ids[check] = [c["id"] for c in candidates]
            if not candidates:
                missing.append(f"{check}: no record")
                continue
            for cand in candidates:
                if not cand["valid"]:
                    missing.append(f"{check} (record {cand['id']}): {'; '.join(cand['reasons'])}")
                elif cand["satisfied_state"] not in permitted:
                    missing.append(
                        f"{check} (record {cand['id']}): {cand['satisfied_state']} does not satisfy {stage}"
                    )
        present = not missing
        stages[stage] = {"present": present, "active": active_ids, "missing": missing}
        if present:
            completed.append(stage)
    return {"claims": claims, "stages": stages, "completed_stages": completed, "head": head}


def render_check_markdown(report: dict) -> str:
    lines = [
        "| id | claim | disposition | result | provenance | why | valid | reason |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for claim_id, c in report["claims"].items():
        lines.append(
            "| {id} | `{claim}` | {disp} | {res} | {prov} | {why} | {valid} | {reason} |".format(
                id=c["id"], claim=claim_id, disp=c["disposition"], res=c["result"] or "—",
                prov=c["provenance"], why=c["why"] or "—", valid="yes" if c["valid"] else "no",
                reason="; ".join(c["reasons"]),
            )
        )
    lines.append("")
    lines.append(f"completed_stages: {render_stages(report['completed_stages'])}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- subcommands


def cmd_snapshot(args) -> int:
    path = session_path(args)
    text = read_session(path)
    ledger = load_ledger(text)
    if args.scope is not None:
        ledger["scope"] = sorted(set(args.scope))
    scope = ledger["scope"]
    repo = args.repo
    uuid = args.session or os.path.splitext(os.path.basename(path))[0]

    state = worktree_state(repo, scope, write_objects=True)
    git_dir = git_text(repo, "rev-parse", "--absolute-git-dir")
    index_dir = os.path.join(git_dir, "review-loop")
    index_file = os.path.join(index_dir, f"{uuid}.index")
    try:
        os.makedirs(index_dir, exist_ok=True)
        if os.path.exists(index_file):
            os.unlink(index_file)
    except OSError as exc:
        raise StorageError(f"cannot prepare private index {index_file}: {exc}") from exc

    env = {"GIT_INDEX_FILE": index_file}
    env.update(SNAPSHOT_IDENTITY_ENV)
    # `<mode> <oid>\t<path>` per entry; gitlinks go in with mode 160000 so the
    # stored tree carries the submodule pointer like any other content. The
    # tree can only hold the plain commit: a `+dirty:` digest is persisted on
    # the snapshot entry (`gitlink_dirty`) instead.
    def _index_line(p: str, identity: str) -> bytes:
        mode, oid = identity.split(":", 1)
        return f"{mode} {plain_oid(oid)}\t{p}".encode("utf-8", errors="surrogateescape") + b"\0"

    payload = b"".join(_index_line(p, identity) for p, identity in sorted(state["paths"].items()))
    n = len(ledger["snapshots"])
    ref = f"refs/review-loop/{uuid}/snap/{n}"
    try:
        git(repo, "update-index", "-z", "--index-info", env=env, input_bytes=payload)
        tree = git_text(repo, "write-tree", env=env)
        commit = git_text(
            repo, "commit-tree", tree, "-m",
            f"review-loop snapshot {n} for session {uuid}", env=env,
        )
        git(repo, "update-ref", ref, commit, env=env)
    finally:
        try:
            if os.path.exists(index_file):
                os.unlink(index_file)
        except OSError:
            pass

    snap = {
        "n": n,
        "ref": ref,
        "commit": commit,
        "tree": tree,
        "head": current_head(repo),
        "recorded_at": now_iso(),
        "label": args.label or "",
        "untracked": state["untracked"],
        "deleted": state["deleted"],
        "renames": state["renames"],
    }
    if state["nested_repos_skipped"]:
        snap["nested_repos_skipped"] = state["nested_repos_skipped"]
    if state["gitlink_dirty"]:
        snap["gitlink_dirty"] = state["gitlink_dirty"]
    ledger["snapshots"].append(snap)
    ledger.pop("pruned_at", None)  # a new baseline lifts the post-prune refusal
    try:
        write_session(path, store_ledger(text, ledger))
    except (StorageError, UsageError) as exc:
        # the ledger never learned of this snapshot: withdraw the ref this
        # run created so "nothing written" holds and a retry reuses snap/{n}
        try:
            git(repo, "update-ref", "-d", ref)
        except StorageError as cleanup_exc:
            raise StorageError(f"{exc}; additionally could not withdraw {ref}: {cleanup_exc}") from exc
        raise
    print(json.dumps({"snapshot": n, "ref": ref, "commit": commit, "tree": tree,
                      "paths": len(state["paths"]), "deleted": state["deleted"],
                      "untracked": state["untracked"],
                      "nested_repos_skipped": state["nested_repos_skipped"],
                      "gitlink_dirty": state["gitlink_dirty"]}))
    return 0


# The only top-level keys a closure file may carry. Anything else (a typo
# such as `dependencies` for `deps`) is refused rather than ignored, because
# an ignored key would silently record an incomplete closure as complete.
CLOSURE_FILE_KEYS = ("check", "closure", "inputs", "deps", "selectors")


def _load_closure_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"cannot read closure file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise UsageError("closure file must be a JSON object")
    unknown = sorted(key for key in data if key not in CLOSURE_FILE_KEYS)
    if unknown:
        noun = "key" if len(unknown) == 1 else "keys"
        raise UsageError(f"closure file: unknown {noun} {', '.join(repr(k) for k in unknown)} "
                         f"(allowed: {', '.join(CLOSURE_FILE_KEYS)})")
    return data


def _closure_file_paths(closure_data: dict, key: str) -> List[str]:
    """`inputs` / `deps` of a closure file: absent means none; present must
    be a list of strings."""
    value = closure_data.get(key, [])
    if not isinstance(value, list):
        raise UsageError(f"closure file `{key}` must be a list, got {value!r}")
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise UsageError(f"closure file `{key}[{idx}]` must be a non-empty path string, got {item!r}")
    return value


def _closure_file_selectors(closure_data: dict) -> List[str]:
    """Selectors of a closure file as `<kind>:<pattern>` strings: a dict entry
    must carry non-empty `kind` and `pattern`; a string entry is taken as is
    (and parsed by `parse_selector`); anything else is a usage error."""
    value = closure_data.get("selectors", [])
    if not isinstance(value, list):
        raise UsageError(f"closure file `selectors` must be a list, got {value!r}")
    out: List[str] = []
    for idx, sel in enumerate(value):
        if isinstance(sel, dict):
            kind, pattern = sel.get("kind"), sel.get("pattern")
            if not isinstance(kind, str) or not kind or not isinstance(pattern, str) or not pattern:
                raise UsageError(f"closure file `selectors[{idx}]` must carry non-empty `kind` and `pattern`, got {sel!r}")
            out.append(f"{kind}:{pattern}")
        elif isinstance(sel, str):
            out.append(sel)
        else:
            raise UsageError(f"closure file `selectors[{idx}]` must be an object or a `<kind>:<pattern>` string, "
                             f"got {sel!r}")
    return out


def cmd_record(args) -> int:
    now = parse_now(args.now)  # validated up front: `--now garbage` is exit 2 whatever the provenance
    path = session_path(args)
    text = read_session(path)
    ledger = load_ledger(text)
    snap = snapshot_by_n(ledger, args.snapshot) if args.snapshot is not None else latest_snapshot(ledger)
    if snap is None:
        raise UsageError("no snapshot stored; run `snapshot` before `record`")
    repo = args.repo
    tree = snapshot_tree_paths(repo, snap)

    check = canonical_check(args.check)
    scope = scope_key(check, args)
    claim_id = f"{check}:{scope}"

    disposition = args.disposition
    if disposition == "executed":
        if args.result not in ("PASS", "FAIL"):
            raise UsageError("--result PASS|FAIL is required when --disposition executed")
        if args.reason:
            raise UsageError("--reason is only legal for not-applicable / controlled-skip")
    else:
        if args.result:
            raise UsageError(f"--result must be absent when --disposition {disposition}")
        if not args.reason:
            raise UsageError(f"--disposition {disposition} requires --reason")
        if disposition == "not-applicable" and check not in NOT_APPLICABLE_CHECKS:
            raise UsageError(f"not-applicable is only legal for {sorted(NOT_APPLICABLE_CHECKS)}; got {check}")
        if disposition == "controlled-skip" and check not in CONTROLLED_SKIP_CHECKS:
            raise UsageError(f"controlled-skip is only legal for {sorted(CONTROLLED_SKIP_CHECKS)}; got {check}")

    inputs_list = list(args.input or [])
    deps_list = list(args.dep or [])
    selectors_raw = list(args.selector or [])
    closure = args.closure  # explicit --closure always wins
    if args.closure_file:
        closure_data = _load_closure_file(args.closure_file)
        file_check = closure_data.get("check")
        if file_check is not None and file_check != check:
            raise UsageError(f"closure file is for check {file_check!r}, not --check {check!r}")
        inputs_list.extend(_closure_file_paths(closure_data, "inputs"))
        deps_list.extend(_closure_file_paths(closure_data, "deps"))
        selectors_raw.extend(_closure_file_selectors(closure_data))
        file_closure = closure_data.get("closure")
        if file_closure is not None:
            if file_closure not in CLOSURES:
                raise UsageError(f"closure file `closure` must be one of {list(CLOSURES)}; got {file_closure!r}")
            if closure is None:
                closure = file_closure
    if closure is None:
        closure = "uncertain"

    inputs = {p: bind_path(p, snap, tree, "input") for p in sorted(set(inputs_list))}
    if args.deps_uncertain:
        deps: object = "uncertain"
    else:
        deps = {p: bind_path(p, snap, tree, "dep") for p in sorted(set(deps_list))}
    selectors = []
    for raw in selectors_raw:
        sel = parse_selector(raw)
        sel["members_digest"] = members_digest(sel, tree)
        selectors.append(sel)

    env = None
    if args.env_command:
        env = {"command": args.env_command, "recorded_at": now_iso()}
    elif disposition == "executed" and (check in ENV_REQUIRED_CHECKS or check.startswith("manual:")):
        raise UsageError(f"{check} requires --env-command (env {{command, recorded_at}})")

    requires_freshness = bool(args.env_sensitive) or check.startswith("manual:")
    freshness = parse_freshness(args.freshness)

    provenance = args.provenance
    why = args.why
    if provenance != "fresh":
        if not provenance.startswith("reused-from:"):
            raise UsageError("--provenance must be `fresh` or `reused-from:<id>`")
        if not why:
            raise UsageError("--why is mandatory for reused-from provenance")
        try:
            source_id = int(provenance[len("reused-from:"):])
        except ValueError as exc:
            raise UsageError("reused-from:<id> requires an integer id") from exc
        source = next((r for r in ledger["records"] if r["id"] == source_id), None)
        if source is None:
            raise UsageError(f"reused-from source record {source_id} does not exist")
        if source["claim_id"] != claim_id:
            raise UsageError(
                f"reused-from source {source_id} is claim {source['claim_id']!r}, not {claim_id!r}"
            )
        active = active_records(ledger)
        if active.get(claim_id, {}).get("id") != source_id:
            raise UsageError(f"reused-from source {source_id} is superseded; it cannot be reused")
        state = worktree_state(repo, ledger["scope"], write_objects=False)
        valid, reasons, _ = evaluate_record(source, repo, state, current_head(repo), now)
        if not valid:
            raise UsageError(
                f"reused-from source {source_id} is not currently valid: {'; '.join(reasons)}"
            )
        if disposition != "executed" or args.result != "PASS":
            raise UsageError("a reused record must be an executed PASS")

    supersedes_ids: List[int] = []
    if args.supersedes:
        for token in args.supersedes.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                supersedes_ids.append(int(token))
            except ValueError as exc:
                raise UsageError(f"--supersedes expects integer ids, got {token!r}") from exc
        if not supersedes_ids:
            raise UsageError("--supersedes requires at least one record id")
        if len(set(supersedes_ids)) != len(supersedes_ids):
            raise UsageError("--supersedes lists the same id twice")
    supersedes_targets = validate_supersedes(
        ledger, supersedes_ids, check, scope, disposition, args.result if disposition == "executed" else None
    )

    stage = args.stage or DEFAULT_STAGE_BY_CHECK.get(check) or ("polish" if check.startswith("agent_review:") else "n/a")
    next_id = max((r["id"] for r in ledger["records"]), default=0) + 1
    record = {
        "id": next_id,
        "claim_id": claim_id,
        "check": check,
        "scope": scope,
        "stage": stage,
        "disposition": disposition,
        "head": current_head(repo),
        "snapshot": snap["n"],
        "inputs": inputs,
        "deps": deps,
        "selectors": selectors,
        "closure": closure,
        "env": env,
        "freshness": freshness,
        "requires_freshness": requires_freshness,
        "assumptions": list(args.assumption or []),
        "unresolved": list(args.unresolved or []),
        "provenance": provenance,
        "why": why,
        "author_route": args.author_route,
        "recorded_at": now_iso(),
        "supersedes": [t["id"] for t in supersedes_targets],
        "superseded_by": None,
    }
    if disposition == "executed":
        record["result"] = args.result
    else:
        record["reason"] = args.reason
    ledger["records"].append(record)
    for target in supersedes_targets:
        target["superseded_by"] = next_id
    mark_supersession(ledger)
    write_session(path, store_ledger(text, ledger))
    print(json.dumps({"id": next_id, "claim_id": claim_id, "snapshot": snap["n"], "closure": closure,
                      "supersedes": record["supersedes"]}))
    return 0


def cmd_check(args) -> int:
    path = session_path(args)
    text = read_session(path)
    ledger = load_ledger(text)
    repo = args.repo
    now = parse_now(args.now)
    state = worktree_state(repo, ledger["scope"], write_objects=False)
    report = evaluate_ledger(ledger, repo, state, now)
    # an unbound region of the last stored baseline is always visible
    report["nested_repos_skipped"] = latest_nested_repos_skipped(ledger)
    if not args.no_write:
        updated = store_ledger(text, ledger)  # persists superseded_by marks
        updated = store_completed_stages(updated, report["completed_stages"])
        if updated != text:
            write_session(path, updated)
    if args.format == "markdown":
        sys.stdout.write(render_check_markdown(report))
    else:
        print(json.dumps(report, indent=2))
    if args.require and args.require not in report["completed_stages"]:
        return 1
    return 0


def cmd_classify(args) -> int:
    path = session_path(args)
    text = read_session(path)
    ledger = load_ledger(text)
    repo = args.repo
    snap = latest_snapshot(ledger)
    if snap is None:
        raise UsageError("no snapshot stored; nothing to classify against")
    state = worktree_state(repo, ledger["scope"], write_objects=False)
    before = snapshot_tree_paths(repo, snap)
    after = state["paths"]
    changed = sorted(
        p for p in set(before) | set(after) if before.get(p) != after.get(p)
    )
    reasons: List[str] = []
    active = active_records(ledger)
    # Every active exec-required record is consulted (never just the newest per check).
    for check in REQUIRED_CLAIMS["exec"]:
        candidates = sorted((r for r in active.values() if r["check"] == check), key=lambda r: r["id"])
        if not candidates:
            reasons.append(f"{check}: no active record")
            continue
        for rec in candidates:
            if rec.get("closure") != "declared":
                reasons.append(f"{check} (record {rec['id']}): uncertain closure")
                continue
            deps = rec.get("deps")
            if deps == "uncertain":
                reasons.append(f"{check} (record {rec['id']}): deps uncertain")
                continue
            closure_paths = set(rec.get("inputs") or {}) | set(deps)
            touched = sorted(closure_paths & set(changed))
            if touched:
                reasons.append(f"{check} (record {rec['id']}): changed closure paths {touched}")
            for selector in rec.get("selectors") or []:
                if members_digest(selector, after) != selector.get("members_digest"):
                    reasons.append(
                        f"{check} (record {rec['id']}): selector digest changed {selector['kind']}:{selector['pattern']}"
                    )
    invalidated = bool(reasons)
    print(json.dumps({
        "exec": "invalidated" if invalidated else "non-invalidating",
        "since_snapshot": snap["n"],
        "changed_paths": changed,
        "reasons": reasons,
        "nested_repos_skipped": snap.get("nested_repos_skipped", []),
    }, indent=2))
    return 1 if invalidated else 0


def cmd_delta(args) -> int:
    path = session_path(args)
    text = read_session(path)
    ledger = load_ledger(text)
    repo = args.repo
    if not ledger["snapshots"]:
        raise UsageError("no snapshot stored")
    post_n = args.post if args.post is not None else ledger["snapshots"][-1]["n"]
    pre_n = args.pre if args.pre is not None else post_n - 1
    if pre_n < 0 or pre_n >= post_n:
        raise UsageError(f"need pre < post; got pre={pre_n} post={post_n}")
    pre = snapshot_by_n(ledger, pre_n)
    post = snapshot_by_n(ledger, post_n)
    pre_tree = snapshot_tree_paths(repo, pre)
    post_tree = snapshot_tree_paths(repo, post)
    excludes = set(args.exclude or [])
    if args.path:
        paths = sorted(set(args.path) - excludes)
    else:
        paths = sorted(
            p for p in set(pre_tree) | set(post_tree)
            if pre_tree.get(p) != post_tree.get(p) and p not in excludes
        )
    def _side(tree: Dict[str, str], p: str) -> Tuple[str, str]:
        if p not in tree:
            return ("<absent>", "<absent>")
        parsed = split_identity(tree[p])
        return parsed if parsed else ("<absent>", "<absent>")

    rows = []
    for p in paths:
        pre_mode, pre_blob = _side(pre_tree, p)
        post_mode, post_blob = _side(post_tree, p)
        rows.append({"pre": pre_n, "post": post_n, "path": p,
                     "pre_blob": pre_blob, "post_blob": post_blob,
                     "pre_mode": pre_mode, "post_mode": post_mode})
    patch = ""
    stat_text = ""
    if paths:
        raw = git_text(repo, "diff", "--raw", "--no-abbrev", "--no-renames", pre["commit"], post["commit"], "--", *paths)
        # `:<pre mode> <post mode> <pre oid> <post oid> <status>\t<path>`;
        # a mode-only change (same oid, `old mode`/`new mode` in the patch)
        # and a submodule pointer move (160000 → 160000) are both listed. A
        # nested repository whose only change is its `+dirty:` digest is not:
        # both trees hold the same commit, so the row carries the identity
        # change while the patch is empty for that path.
        seen: Dict[str, Tuple[str, str, str, str]] = {}
        for line in raw.splitlines():
            meta, _, rpath = line.partition("\t")
            fields = meta.split(" ")
            if len(fields) >= 4:
                seen[rpath] = (fields[0].lstrip(":"), fields[1], fields[2], fields[3])
        zero = "0" * 40
        for row in rows:
            got = seen.get(row["path"])
            pre_plain, post_plain = plain_oid(row["pre_blob"]), plain_oid(row["post_blob"])
            if got is None:
                if (pre_plain, row["pre_mode"]) != (post_plain, row["post_mode"]):
                    raise StorageError(f"snapshot diff lists no change for {row['path']} but identities differ")
                continue
            exp_pre = pre_plain if pre_plain != "<absent>" else zero
            exp_post = post_plain if post_plain != "<absent>" else zero
            exp_pre_mode = row["pre_mode"] if row["pre_mode"] != "<absent>" else "000000"
            exp_post_mode = row["post_mode"] if row["post_mode"] != "<absent>" else "000000"
            if got != (exp_pre_mode, exp_post_mode, exp_pre, exp_post):
                raise StorageError(
                    f"attributable delta mismatch for {row['path']}: table {exp_pre_mode}:{exp_pre}..{exp_post_mode}:{exp_post}, "
                    f"patch {got[0]}:{got[2]}..{got[1]}:{got[3]}"
                )
        patch = git(repo, "diff", "--no-renames", pre["commit"], post["commit"], "--", *paths).decode("utf-8", errors="replace")
        stat_text = git_text(repo, "diff", "--stat", "--no-renames", pre["commit"], post["commit"], "--", *paths)

    if args.format == "json":
        print(json.dumps({"pre": pre_n, "post": post_n, "pre_ref": pre["ref"], "post_ref": post["ref"],
                          "rows": rows, "stat": stat_text, "patch": patch}, indent=2))
        return 0
    if args.format == "patch":
        sys.stdout.write(patch)
        return 0
    lines = ["### Attributable Delta", "",
             "| snapshot pre | snapshot post | path | pre_blob | post_blob | pre_mode | post_mode |",
             "|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['pre']} | {row['post']} | `{row['path']}` | {row['pre_blob']} | {row['post_blob']} "
                     f"| {row['pre_mode']} | {row['post_mode']} |")
    if not rows:
        lines.append("| " + str(pre_n) + " | " + str(post_n) + " | (no attributable change) | — | — | — | — |")
    lines.append("")
    lines.append(f"Materialize: `python3 scripts/evidence_ledger.py delta --session <uuid> --pre {pre_n} --post {post_n}` "
                 f"(`git diff {pre['ref']} {post['ref']} -- <paths>`).")
    lines.append("")
    if stat_text:
        lines.extend(["```", stat_text, "```", ""])
    if patch:
        lines.extend(["```diff", patch.rstrip("\n"), "```", ""])
    sys.stdout.write("\n".join(lines))
    return 0


def _collect_paths(node, out: List[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "path" and isinstance(value, str):
                out.append(value)
            else:
                _collect_paths(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_paths(item, out)


def cmd_lint_closure(args) -> int:
    repo = args.repo
    selectors = [
        {"kind": "discovery", "pattern": "tests/skills/contracts/**"},
        {"kind": "discovery", "pattern": "docs/protocol/*.md"},
        {"kind": "discovery", "pattern": "skills/**/SKILL.md"},
        {"kind": "discovery", "pattern": ".agents/skills/**/SKILL.md"},
        {"kind": "discovery", "pattern": "tests/skills/smoke/*.json"},
    ]
    contracts_dir = os.path.join(repo, "tests", "skills", "contracts")
    referenced: List[str] = []
    if os.path.isdir(contracts_dir):
        try:
            names = sorted(os.listdir(contracts_dir))
        except OSError as exc:
            raise UsageError(f"cannot read contracts directory {contracts_dir}: {exc}") from exc
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(contracts_dir, name), "r", encoding="utf-8") as fh:
                    _collect_paths(json.load(fh), referenced)
            except (OSError, json.JSONDecodeError) as exc:
                raise UsageError(f"cannot read contract {name}: {exc}") from exc
    for ref in sorted(set(referenced)):
        selectors.append({"kind": "glob", "pattern": ref})
    closure = {
        "check": "lint",
        "inputs": ["scripts/run-skill-lint"],
        "deps": [],
        "selectors": selectors,
        "closure": "declared",
    }
    print(json.dumps(closure, indent=2))
    return 0


def cmd_prune(args) -> int:
    path = session_path(args)
    text = read_session(path)
    ledger = load_ledger(text)
    repo = args.repo
    uuid = args.session or os.path.splitext(os.path.basename(path))[0]
    prefix = f"refs/review-loop/{uuid}/"
    refs = git_text(repo, "for-each-ref", "--format=%(refname)", prefix).split("\n")
    refs = [r for r in refs if r]
    for ref in refs:
        git(repo, "update-ref", "-d", ref)
    git_dir = git_text(repo, "rev-parse", "--absolute-git-dir")
    index_file = os.path.join(git_dir, "review-loop", f"{uuid}.index")
    report: dict = {"pruned_refs": refs, "index_cleanup": "absent"}
    try:
        if os.path.exists(index_file):
            os.unlink(index_file)
            report["index_cleanup"] = "removed"
    except OSError as exc:
        report["index_cleanup"] = "failed"  # reported, never hidden; the refs are still gone
        report["index_cleanup_error"] = str(exc)
    # Stamping is driven by the ledger, not by the refs found: after a prune
    # whose session write failed (refs already gone), the re-run must still
    # stamp every unstamped snapshot so nothing binds to an unreferenced tree.
    unstamped = [snap for snap in ledger["snapshots"] if not snap.get("pruned_at")]
    needs_stamp = bool(unstamped) or (bool(ledger["snapshots"]) and not ledger.get("pruned_at"))
    report["already_pruned"] = not needs_stamp
    report["refs_missing"] = needs_stamp and not refs
    if needs_stamp:
        when = now_iso()
        ledger["pruned_at"] = when
        for snap in unstamped:
            snap["pruned_at"] = when  # its tree is gone: never bind to it again
        write_session(path, store_ledger(text, ledger))
        report["stamped"] = [snap["n"] for snap in unstamped]
    print(json.dumps(report))
    return 0


# ------------------------------------------------------------------- route


_HEADING_LINE_RE = re.compile(r"^#{1,6} ", re.MULTILINE)


# A fenced code block (``` … ```; an unterminated fence runs to the end of the
# text). Its contents are opaque to the route-facts extractor: a heading or a
# table row quoted inside a fence is neither the block nor part of it.
_FENCED_CODE_RE = re.compile(r"^[ \t]*(`{3,})[^\n]*\n.*?(?:^[ \t]*\1`*[ \t]*$|\Z)", re.MULTILINE | re.DOTALL)


def _blank_fenced_code(text: str) -> str:
    """Replace every fenced code block with blank lines of the same count."""
    return _FENCED_CODE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def extract_route_facts_block(text: str) -> str:
    """Body of the `### Route Facts` block (until the next heading of any
    level). Fenced code inside `text` is ignored."""
    text = _blank_fenced_code(text)
    idx = None
    for match in _HEADING_LINE_RE.finditer(text):
        line_end = text.find("\n", match.start())
        line = text[match.start():line_end if line_end != -1 else len(text)]
        if line.strip() == ROUTE_FACTS_HEADING:
            idx = match.start()
            break
    if idx is None:
        raise UsageError(f"no `{ROUTE_FACTS_HEADING}` block found")
    body_start = text.find("\n", idx)
    if body_start == -1:
        return ""
    nxt = _HEADING_LINE_RE.search(text, body_start + 1)
    return text[body_start + 1:nxt.start() if nxt else len(text)]


def parse_route_facts(block: str) -> Dict[str, Tuple[str, str]]:
    """Parse `| name | value | rationale |` rows into {name: (value, rationale)}.
    Unknown names, duplicate names, illegal values, or a missing rationale
    are malformed (UsageError → exit 2)."""
    known = set(ELIGIBILITY_FACTS) | set(SENSITIVE_FLAGS)
    entries: Dict[str, Tuple[str, str]] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue  # separator row
        name = cells[0].strip("`").lower()
        if name in ("fact", "flag", "entry", "name"):
            continue  # header row
        if name not in known:
            raise UsageError(f"unknown route fact {cells[0]!r}")
        if name in entries:
            raise UsageError(f"route fact {name!r} listed twice")
        if len(cells) < 3 or not cells[2]:
            raise UsageError(f"route fact {name!r} has no rationale (every entry needs a one-line rationale)")
        value = cells[1].strip("`").lower()
        if value not in ROUTE_VALUES:
            raise UsageError(f"route fact {name!r} has illegal value {cells[1]!r}; expected true | false | uncertain")
        entries[name] = (value, cells[2])
    return entries


def session_phase(text: str) -> Optional[str]:
    for match in _SECTION_RE.finditer(text):
        line_end = text.find("\n", match.start())
        line = text[match.start():line_end if line_end != -1 else len(text)].strip()
        if not line.startswith("## Current Phase"):
            continue
        inline = line[len("## Current Phase"):].strip()
        if inline.startswith(":"):
            return inline[1:].strip().lower() or None
        body_end = text.find("\n## ", match.start() + 1)
        body = text[line_end + 1:body_end if body_end != -1 else len(text)] if line_end != -1 else ""
        for candidate in body.splitlines():
            candidate = candidate.strip()
            if candidate:
                return candidate.strip("`").lower()
        return None
    return None


def record_touches(rec: dict, paths: List[str]) -> bool:
    """Does this record's declared closure name any of the change's paths?
    Paths only select which ledger claims to cross-check; they never
    classify the change itself."""
    if not paths:
        return True  # no paths declared: every active claim is cross-checked
    deps = rec.get("deps")
    closure_paths = set(rec.get("inputs") or {}) | (set(deps) if isinstance(deps, dict) else set())
    if closure_paths & set(paths):
        return True
    return any(selector_matches(sel, p) for sel in rec.get("selectors") or [] for p in paths)


ROUTE_FACTS_ABSENT_REASON = "route facts block absent from Current Review Packet"
PACKET_NOT_UNIQUE_REASON = "canonical packet section not uniquely identifiable"


def packet_route_facts_block(text: str) -> Tuple[Optional[str], int]:
    """(`### Route Facts` block of the canonical packet or None, number of
    packet candidates).

    The canonical packet is selected by session structure — the unique
    `## Current Review Packet` heading whose next `## ` heading is
    `## Review History` — never by the first matching heading, so a heading
    quoted inside `## Approved Plan` or a `## Review History` entry is never
    read. Only that section (heading up to the next `## ` heading) is
    consulted: a stale block anywhere else — an earlier history entry, an
    old Direct Implementation Record — is never read. `(None, 1)` means the
    block is absent from the identified packet; `(None, 0)` / `(None, n>1)`
    mean the packet is not uniquely identifiable; all route to `executor`.
    A block that exists but is malformed still raises UsageError (exit 2)
    from `parse_route_facts`."""
    spans = canonical_section_candidates(text, PACKET_HEADING)
    if len(spans) != 1:
        return None, len(spans)
    try:
        return extract_route_facts_block(text[spans[0][0]:spans[0][1]]), 1
    except UsageError:
        return None, 1


def decide_route(entries: Optional[Dict[str, Tuple[str, str]]], ledger: dict, phase: Optional[str],
                 paths: List[str], packet_candidates: int = 1) -> dict:
    reasons: List[str] = []
    if packet_candidates != 1:
        # zero or several structural candidates: fail closed before any fact is read
        reasons.append(PACKET_NOT_UNIQUE_REASON)
        if packet_candidates == 0:
            reasons.append(ROUTE_FACTS_ABSENT_REASON)
        entries = {}
    elif entries is None:
        entries = {}
        reasons.append(ROUTE_FACTS_ABSENT_REASON)
    facts = {name: entries.get(name, ("missing", ""))[0] for name in ELIGIBILITY_FACTS}
    flags = {name: entries.get(name, ("missing", ""))[0] for name in SENSITIVE_FLAGS}

    if phase != "execution":
        reasons.append(f"phase is {phase or 'unknown'}, not execution (planning is always Executor-authored)")
    for name in ELIGIBILITY_FACTS:
        if facts[name] != "true":
            reasons.append(f"eligibility fact {name} is {facts[name]}, not true")
    for name in SENSITIVE_FLAGS:
        if flags[name] != "false":
            reasons.append(f"sensitive flag {name} is {flags[name]}, not false")

    cross_check = {"required": facts["known_dependency_impact"] == "true", "checked_records": [], "failed": []}
    if cross_check["required"]:
        for rec in sorted(active_records(ledger).values(), key=lambda r: r["id"]):
            if not record_touches(rec, paths):
                continue
            cross_check["checked_records"].append(rec["id"])
            if rec.get("closure") != "declared":
                cross_check["failed"].append(f"record {rec['id']} ({rec['claim_id']}): closure uncertain")
            elif rec.get("deps") == "uncertain":
                cross_check["failed"].append(f"record {rec['id']} ({rec['claim_id']}): deps uncertain")
        if cross_check["failed"]:
            reasons.append("ledger cross-check failed: known_dependency_impact is true but " +
                           "; ".join(cross_check["failed"]))

    snap = latest_snapshot(ledger)
    if snap is None:
        reasons.append("no stored snapshot (evidence cannot be bound; direct route refused)")

    route = "executor" if reasons else "orchestrator-direct"
    return {
        "route": route,
        "phase": phase,
        "packet_candidates": packet_candidates,
        "snapshot": snap["n"] if snap else None,
        "facts": facts,
        "flags": flags,
        "rationale": {name: entries[name][1] for name in entries},
        "paths": sorted(set(paths)),
        "cross_check": cross_check,
        "reasons": reasons if reasons else ["every eligibility fact true; every sensitive flag false; "
                                            "ledger cross-check passed; snapshot stored; phase execution"],
    }


def cmd_route(args) -> int:
    path = session_path(args)
    text = read_session(path)
    ledger = load_ledger(text)
    packet_candidates = 1
    if args.facts is None:
        # Default source: the `### Route Facts` block of the canonical
        # `## Current Review Packet` only (structural selection). A packet
        # that is absent or not uniquely identifiable, or a packet without
        # the block, fails closed to `executor`; a malformed block inside
        # the identified packet is still exit 2.
        block, packet_candidates = packet_route_facts_block(text)
        entries = parse_route_facts(block) if block is not None else None
    else:
        if args.facts == "-":
            facts_text = sys.stdin.read()
        else:
            try:
                with open(args.facts, "r", encoding="utf-8") as fh:
                    facts_text = fh.read()
            except OSError as exc:
                raise UsageError(f"cannot read route facts {args.facts}: {exc}") from exc
        entries = parse_route_facts(extract_route_facts_block(facts_text))
    decision = decide_route(entries, ledger, session_phase(text), list(args.path or []),
                            packet_candidates=packet_candidates)
    if args.format == "route":
        print(decision["route"])
    else:
        print(json.dumps(decision, indent=2))
    return 0 if decision["route"] == "orchestrator-direct" else 1


# --------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evidence_ledger.py",
        description="review-loop evidence ledger helper (content-bound evidence + attributable delta).",
    )
    parser.add_argument("--repo", default=".", help="path inside the git repository (default: cwd)")
    parser.add_argument("--sessions-dir", default=".review-loop/sessions",
                        help="session directory relative to the repo root")
    parser.add_argument("--session", help="session uuid (file: <sessions-dir>/<uuid>.md)")
    parser.add_argument("--session-file", help="explicit session file path (overrides --session lookup)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("snapshot", help="store the current worktree content as a snapshot")
    p.add_argument("--scope", nargs="*", default=None,
                   help="path prefixes whose untracked files are in scope (default: all non-ignored)")
    p.add_argument("--label", default="", help="free-text label (e.g. 'round 2 post-executor')")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("record", help="append an evidence record bound to a snapshot")
    p.add_argument("--check", required=True)
    p.add_argument("--scope", help="explicit scope key (command line for tests/lint/static_analysis)")
    p.add_argument("--scope-path", action="append", help="review-target path (repeatable)")
    p.add_argument("--stage", choices=STAGE_ORDER + ["n/a"])
    p.add_argument("--disposition", choices=DISPOSITIONS, default="executed")
    p.add_argument("--result", choices=("PASS", "FAIL"))
    p.add_argument("--reason", help="verbatim reason for not-applicable / controlled-skip")
    p.add_argument("--snapshot", type=int, help="snapshot n to bind to (default: latest)")
    p.add_argument("--input", action="append", help="direct input path (repeatable)")
    p.add_argument("--dep", action="append", help="dependency path (repeatable)")
    p.add_argument("--deps-uncertain", action="store_true", help="record deps = \"uncertain\"")
    p.add_argument("--selector", action="append", help="<glob|dir|discovery>:<pattern> (repeatable)")
    p.add_argument("--closure-file", help="JSON {inputs, deps, selectors[, closure]} (e.g. lint-closure output); "
                                          "its `closure` key applies unless --closure is given")
    p.add_argument("--closure", choices=CLOSURES, default=None,
                   help="declared | uncertain (default: the closure file's `closure` key, else uncertain)")
    p.add_argument("--env-command", help="command line for env {command, recorded_at}")
    p.add_argument("--env-sensitive", action="store_true", help="mark the record freshness-required")
    p.add_argument("--freshness", help="ttl:<seconds> | bound-to-head")
    p.add_argument("--assumption", action="append")
    p.add_argument("--unresolved", action="append")
    p.add_argument("--provenance", default="fresh", help="fresh | reused-from:<id>")
    p.add_argument("--why", help="mandatory sentence for reused-from provenance")
    p.add_argument("--author-route", choices=AUTHOR_ROUTES, default="n/a")
    p.add_argument("--supersedes", help="comma-separated ids of earlier records of the same check that this "
                                        "record explicitly supersedes (FAIL supersession guard applies)")
    p.add_argument("--now", help="ISO timestamp override used for reuse-validity evaluation (tests)")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("check", help="evaluate active records; derive completed_stages")
    p.add_argument("--format", choices=("json", "markdown"), default="json")
    p.add_argument("--no-write", action="store_true", help="do not persist supersession / completed_stages")
    p.add_argument("--require", choices=STAGE_ORDER, help="exit 1 unless this stage is present")
    p.add_argument("--now", help="ISO timestamp override for freshness evaluation (tests)")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("classify", help="exec-invalidating vs non-invalidating delta since the last snapshot")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("delta", help="materialize the Attributable Delta between two snapshots")
    p.add_argument("--pre", type=int)
    p.add_argument("--post", type=int)
    p.add_argument("--path", action="append", help="restrict to these scope paths (repeatable)")
    p.add_argument("--exclude", action="append", help="unrelated dirty paths to keep out of the table")
    p.add_argument("--format", choices=("markdown", "json", "patch"), default="markdown")
    p.set_defaults(func=cmd_delta)

    p = sub.add_parser("lint-closure", help="emit the lint claim's closure as JSON")
    p.set_defaults(func=cmd_lint_closure)

    p = sub.add_parser("prune", help="delete this session's snapshot refs (user-requested only)")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("route", help="author route decision from the `### Route Facts` block "
                                     "(exit 0 orchestrator-direct, 1 executor, 2 malformed)")
    p.add_argument("--facts", help="file holding the `### Route Facts` block, or `-` for stdin "
                                   "(default: the block inside the canonical `## Current Review Packet` "
                                   "only — the unique heading followed by `## Review History`; absent, "
                                   "ambiguous, or blockless → executor)")
    p.add_argument("--path", action="append",
                   help="path the change will touch (repeatable); selects which ledger claims the "
                        "known_dependency_impact cross-check inspects — never classifies the change")
    p.add_argument("--format", choices=("json", "route"), default="json")
    p.set_defaults(func=cmd_route)
    return parser


def main(argv: List[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.repo = resolve_repo(os.path.abspath(args.repo))
        return args.func(args)
    except UsageError as exc:
        sys.stderr.write(f"evidence-ledger: ERROR {exc}\n")
        return 2
    except StorageError as exc:
        sys.stderr.write(f"evidence-ledger: STORAGE-ERROR {exc} (fail closed; nothing written)\n")
        return 3
    except Exception as exc:  # noqa: BLE001 — any bug fails closed as exit 3, never exit 1 (a determination)
        traceback.print_exc(file=sys.stderr)
        sys.stderr.write(f"evidence-ledger: INTERNAL-ERROR {type(exc).__name__}: {exc} (fail closed; nothing written)\n")
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
