#!/usr/bin/env python3
"""Read-only security scan over a fresh W01 delivery manifest.

The scanner inventories exactly the tracked and non-ignored untracked files
bound by the manifest, including staged work. It never follows symlinks, writes
source or ignore files, or prints matched secret values.
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

try:
    import content_rules
    import delivery_scope as ds
except ModuleNotFoundError:  # imported as scripts.security_preflight
    from scripts import content_rules
    from scripts import delivery_scope as ds

RULES_VERSION = "security-preflight/v1"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FINDINGS = 1000

PATH_RULES = (
    ("key-or-certificate", re.compile(r"\.(?:pem|key|crt|cert|cer|p12|pfx|jks|keystore|ppk|asc|gpg|pgp)$", re.I)),
    ("environment-file", re.compile(r"(?:^|/)(?:\.env(?:\..+)?|[^/]+\.env)$", re.I)),
    ("credential-name", re.compile(r"(?:^|/)[^/]*(?:credentials?|secrets?|api[-_.]?key|auth[-_.]?token|passwd|shadow)[^/]*$", re.I)),
    ("ssh-private-key", re.compile(r"(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)(?:$|\.)", re.I)),
    ("service-account", re.compile(r"(?:^|/)service-account[^/]*\.json$", re.I)),
    ("cloud-credential-directory", re.compile(r"(?:^|/)\.(?:aws|gcloud)(?:/|$)", re.I)),
    ("database-dump", re.compile(r"\.(?:sqlite3?|db|dump|sql\.gz)$", re.I)),
    ("terraform-state", re.compile(r"\.(?:tfstate|tfvars)(?:\.|$)", re.I)),
    ("terraform-cache", re.compile(r"(?:^|/)\.terraform(?:/|$)", re.I)),
    ("source-map", re.compile(r"\.map$", re.I)),
    ("log-file", re.compile(r"(?:\.log$|(?:^|/)logs(?:/|$))", re.I)),
)
EXAMPLE_SUFFIX = re.compile(r"\.(?:example|sample)(?:\.[^/]*)?$", re.I)
IGNORE_CASES = {
    "environment-and-config": ((".env", ".env.local", "apps/service.env"), (".env.example",)),
    "keys-and-certificates": (("keys/private.pem", "keys/app.key", "certs/app.crt", "certs/app.cert",
                               "certs/app.cer", "keys/app.p12", "keys/app.pfx", "keys/app.jks",
                               "keys/app.keystore", "keys/app.ppk"), ()),
    "ssh-private-keys": (("id_rsa", "id_dsa", "id_ecdsa", "keys/id_ed25519"), ()),
    "pgp-gpg": (("keys/archive.asc", "keys/archive.gpg", "keys/archive.pgp"), ()),
    "cloud-credentials": ((".aws/credentials", ".gcloud/application_default_credentials.json",
                           "service-account-prod.json"), ()),
    "generic-secrets": (("config/secrets.yaml", "secrets.production.json"), ("secrets.example.json",)),
    "database-and-dumps": (("data.sqlite", "data.db", "backup.dump", "db.sql.gz"), ()),
    "source-maps": (("static/app.js.map",), ()),
    "terraform": (("terraform.tfstate", "terraform.tfstate.backup", "prod.tfvars", ".terraform/plugins/cache"),
                  ("prod.tfvars.example",)),
    "logs": (("logs/app.log",), ()),
}


class ScanError(Exception):
    pass


def _content_findings(content: bytes) -> list:
    """(rule, line) under the shared table scripts/content_rules.py (V312-S), one per rule and line; never the value."""
    found = content_rules.scan_text(content.decode("utf-8", "replace"))
    if len(found) > MAX_FINDINGS:
        raise ScanError("finding-count limit exceeded")
    return found


def _report(**fields) -> dict:
    return ds.seal({"schema": 1, "kind": "security-preflight", "created_at": ds.created_at(),
                    "ruleset": RULES_VERSION, **fields})


def _open_regular(root: str, relative: str) -> int:
    """Open a manifest path without following any directory or leaf symlink."""
    parts = relative.split("/")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    dir_flags = flags | getattr(os, "O_DIRECTORY", 0)
    current = os.open(root, dir_flags)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, dir_flags, dir_fd=current)
            os.close(current)
            current = nxt
        fd = os.open(parts[-1], flags, dir_fd=current)
    finally:
        os.close(current)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ScanError("manifest path is no longer a regular file")
    return fd


def _path_findings(path: str) -> list:
    findings = []
    for rule, pattern in PATH_RULES:
        if pattern.search(path):
            if rule in {"environment-file", "credential-name", "terraform-state"} and EXAMPLE_SUFFIX.search(path):
                continue
            findings.append({"rule": rule, "path": path, "line": None})
    return findings


def _ignored_by_repo_gitignore(repo: str, sample: str, owned_gitignores: frozenset = frozenset()) -> bool:
    output = ds.git(repo, "check-ignore", "--verbose", "--no-index", "--", sample, codes=(0, 1))
    if not output:
        return False
    line = output.decode("utf-8", "surrogateescape").splitlines()[0]
    decision, separator, _matched_path = line.partition("\t")
    fields = decision.rsplit(":", 2)
    if not separator or len(fields) != 3:
        return False
    if fields[2].strip().startswith("!"):
        return False  # a matching negation explicitly re-includes the sample
    source = fields[0]
    if not os.path.isabs(source):
        source = os.path.join(repo, source)
    source = os.path.realpath(source)
    if os.path.basename(source) != ".gitignore":
        return False  # global excludes / info/exclude do not provide portable repo coverage
    try:
        if os.path.commonpath((repo, source)) != repo:
            return False
        relative = os.path.relpath(source, repo)
        if relative.replace(os.sep, "/") in owned_gitignores:
            # A declared task-owned `.gitignore` edit is bound by the fresh
            # W01 candidate and ships with this delivery.
            return True
        tracked = ds.git(repo, "ls-files", "--error-unmatch", "--", relative, codes=(0, 1))
        worktree_matches_head = _git_diff_clean(repo, "diff", "HEAD", "--", relative)
        index_matches_head = _git_diff_clean(repo, "diff", "--cached", "HEAD", "--", relative)
        return bool(tracked) and worktree_matches_head and index_matches_head
    except ValueError:
        return False


def _git_diff_clean(repo: str, *args: str) -> bool:
    env = os.environ.copy()
    for key in ds.GIT_ENV_OVERRIDES:
        env.pop(key, None)
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0", GIT_LITERAL_PATHSPECS="1")
    git_args = [*args]
    if git_args and git_args[0] == "diff":
        git_args.insert(1, "--quiet")
    result = subprocess.run(["git", "--no-replace-objects", "-c", "core.fsmonitor=false", *git_args],
                            cwd=repo, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False)
    if result.returncode not in (0, 1):
        raise ScanError("cannot verify repository .gitignore state")
    return result.returncode == 0


def ignore_coverage(repo: str, owned_gitignores: frozenset = frozenset()) -> list:
    """Probe representative sensitive paths against repository-local ignore rules."""
    rows = []
    for category, (positive, exceptions) in IGNORE_CASES.items():
        uncovered = [sample for sample in positive
                     if not _ignored_by_repo_gitignore(repo, sample, owned_gitignores)]
        incorrectly_ignored = [sample for sample in exceptions
                               if _ignored_by_repo_gitignore(repo, sample, owned_gitignores)]
        rows.append({"category": category, "covered": not uncovered and not incorrectly_ignored,
                     "uncovered_samples": uncovered, "incorrectly_ignored_examples": incorrectly_ignored})
    return rows


def _read_and_scan(root: str, path: str, expected: dict, total_left: int) -> tuple:
    fd = _open_regular(root, path)
    raw_hash, size = hashlib.sha256(), 0
    content = bytearray()
    matched = []
    try:
        before = os.fstat(fd)
        if before.st_size > MAX_FILE_BYTES or before.st_size > total_left:
            raise ScanError("content scan limit exceeded")
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES or size > total_left:
                raise ScanError("content scan limit exceeded")
            raw_hash.update(chunk)
            content.extend(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ) or size != expected.get("size") or raw_hash.hexdigest() != expected.get("sha256"):
            raise ScanError("file changed or no longer matches the manifest")
        # Several markers on one line collapse to one finding per rule and line.
        matched = [{"rule": rule, "path": path, "line": line} for rule, line in _content_findings(bytes(content))]
    finally:
        os.close(fd)
    return size, matched


def _bind_findings_to_git_state(findings: list, state: dict) -> list:
    for finding in findings:
        path = finding["path"]
        finding["in_head"] = path in state["head_paths"]
        finding["in_index"] = path in state["index"]
    return findings


def _scan_git_blob(repo: str, path: str, entry: dict, total_left: int) -> tuple:
    """Scan the exact staged blob named by the manifest's index entry."""
    env = os.environ.copy()
    for key in ds.GIT_ENV_OVERRIDES:
        env.pop(key, None)
    env.update(GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
    limit = min(MAX_FILE_BYTES, total_left)
    process = subprocess.Popen(["git", "--no-replace-objects", "-c", "core.fsmonitor=false",
                                "cat-file", "blob", entry["oid"]], cwd=repo, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        chunks = bytearray()
        while True:
            chunk = process.stdout.read(min(65536, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > limit:
                process.terminate()
                process.wait()
                raise ScanError("content scan limit exceeded")
        content = bytes(chunks)
        if process.wait() != 0:
            raise ScanError("cannot read staged blob for " + path)
    finally:
        if process.stdout:
            process.stdout.close()
    if not content:
        return 0, []
    if len(content) > MAX_FILE_BYTES or len(content) > total_left:
        raise ScanError("content scan limit exceeded")
    if process.returncode:
        raise ScanError("cannot read staged blob for " + path)
    findings = [{"rule": rule, "path": path, "line": line, "source": "index"} for rule, line in _content_findings(content)]
    return len(content), findings


def scan(repo_path: str, manifest_path: str = None, *, manifest: dict = None) -> dict:
    repo = ds.repository(repo_path)
    if manifest is None:
        if manifest_path is None:
            raise ds.UsageError("a delivery manifest path or validated manifest object is required")
        manifest = ds.load_document(manifest_path, "delivery-manifest")
    else:
        ds.validate_document(manifest, "delivery-manifest")
    ds.match_repository(repo, manifest["baseline"])
    before = ds.capture_state(repo)
    if before != manifest["current"]:
        return _report(status="stale", manifest_fingerprint=manifest["fingerprint"],
                       scanned_files=0, scanned_bytes=0, findings=[], coverage_complete=False,
                       ignore_coverage=[], ignore_coverage_complete=False,
                       reason="manifest is stale before scan")

    findings, scanned, total = [], 0, 0
    paths = manifest["current"]["worktree"]
    index = manifest["current"]["index"]
    scanned_blobs = {}

    def append(rows):
        rows = list(rows)
        if len(findings) + len(rows) > MAX_FINDINGS:
            raise ScanError("finding-count limit exceeded")
        findings.extend(rows)

    try:
        for path, expected in sorted(paths.items()):
            mode = expected["mode"]
            if mode == "120000":  # symlink target bytes are deliberately not opened
                append(_path_findings(path))
                continue
            if mode not in {"100644", "100755"}:
                raise ScanError(f"unsupported file mode for {path!r}")
            append(_path_findings(path))
            size, content = _read_and_scan(repo["worktree"], path, expected, MAX_TOTAL_BYTES - total)
            scanned += 1
            total += size
            scanned_blobs.setdefault(expected["oid"], (size, content))
            append(content)
        for path, entries in sorted(index.items()):
            for entry in entries:
                path_findings = [dict(row, source="index") for row in _path_findings(path)]
                append(path_findings)
                if entry["mode"] == "120000":
                    continue
                if entry["mode"] not in {"100644", "100755"}:
                    raise ScanError(f"unsupported staged file entry for {path!r}")
                if entry["oid"] in scanned_blobs:
                    append(dict(row, path=path, source="index") for row in scanned_blobs[entry["oid"]][1])
                    continue
                size, content = _scan_git_blob(repo["worktree"], path, entry, MAX_TOTAL_BYTES - total)
                scanned += 1
                total += size
                scanned_blobs[entry["oid"]] = (size, content)
                append(content)
    except (OSError, ScanError) as exc:
        current = ds.capture_state(repo)
        _bind_findings_to_git_state(findings, manifest["current"])
        return _report(status="incomplete", manifest_fingerprint=manifest["fingerprint"],
                       scanned_files=scanned, scanned_bytes=total, findings=findings,
                       coverage_complete=False, ignore_coverage=[], ignore_coverage_complete=False,
                       reason=type(exc).__name__ + ": " + str(exc),
                       current_fingerprint=ds.fingerprint(current))

    after = ds.capture_state(repo)
    fresh = after == manifest["current"]
    _bind_findings_to_git_state(findings, manifest["current"])
    findings.sort(key=lambda item: (item["path"], item["rule"], item["line"] or 0))
    owned_gitignores = frozenset(
        row["path"] for row in manifest["task_delta"]
        if row["ownership"] == "declared-post-baseline" and row["after"]["worktree"] is not None
        and row["path"].rsplit("/", 1)[-1] == ".gitignore"
    )
    ignores = ignore_coverage(repo["worktree"], owned_gitignores)
    ignore_ok = all(row["covered"] for row in ignores)
    status = "stale" if not fresh else "blocked" if findings else "review-required" if not ignore_ok else "clean"
    return _report(status=status, manifest_fingerprint=manifest["fingerprint"],
                   scanned_files=scanned, scanned_bytes=total, findings=findings,
                   coverage_complete=fresh, ignore_coverage=ignores,
                   ignore_coverage_complete=ignore_ok, current_fingerprint=ds.fingerprint(after))


def scan_file(path: str) -> list:
    """One file under the content rules (the review-pr post body, LG2-b2): rule and line only, never the matched value."""
    content = Path(path).read_bytes()
    if len(content) > MAX_FILE_BYTES:
        raise ScanError("content scan limit exceeded")
    return [{"rule": rule, "line": line}
            for rule, line in sorted(_content_findings(content), key=lambda item: (item[1], item[0]))]


def start_coverage(repo_path: str) -> dict:
    """FIELD-19: the ignore coverage a later scan credits for an unchanged repository, i.e. ignore_coverage() with no
    task-owned `.gitignore` (only a tracked `.gitignore` equal to HEAD counts); for a warning before a run starts."""
    rows = ignore_coverage(ds.repository(repo_path)["worktree"])
    return {"rules_version": RULES_VERSION, "ignore_coverage": rows,
            "uncovered": [row["category"] for row in rows if not row["covered"]]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--manifest")
    mode.add_argument("--ignore-coverage", action="store_true",
                      help="print only the start-time .gitignore coverage (start_coverage) as JSON; exit 0")
    mode.add_argument("--file", help="scan one file with the content rules only; print rule and line as JSON; exit 1 on a match")
    parser.add_argument("--output", help="optional immutable JSON report path")
    args = parser.parse_args(argv)
    if args.file:
        try:
            findings = scan_file(args.file)
        except (OSError, ScanError) as exc:
            sys.stderr.write(json.dumps({"error": str(exc), "kind": "scan"}, ensure_ascii=True) + "\n")
            return 3
        sys.stdout.write(json.dumps({"kind": "security-preflight-file", "ruleset": RULES_VERSION, "findings": findings},
                                    ensure_ascii=True) + "\n")
        return 1 if findings else 0
    if args.ignore_coverage:
        try:
            sys.stdout.write(json.dumps(start_coverage(args.repo), ensure_ascii=True) + "\n")
            return 0
        except (ds.UsageError, ds.CaptureError, OSError, ScanError) as exc:
            sys.stderr.write(json.dumps({"error": str(exc), "kind": "scan"}, ensure_ascii=True) + "\n")
            return 3
    try:
        repo = ds.repository(args.repo)
        ds.validate_output(repo, args.output)
        report = scan(repo["worktree"], args.manifest)
        ds.emit(report, args.output)
        if (report["status"] == "clean" and report["coverage_complete"]
                and report.get("ignore_coverage_complete") is True):
            return 0
        return 3 if report["status"] == "incomplete" else 1
    except ds.UsageError as exc:
        sys.stderr.write(json.dumps({"error": str(exc), "kind": "usage"}, ensure_ascii=True) + "\n")
        return 2
    except (ds.CaptureError, OSError, ScanError) as exc:
        sys.stderr.write(json.dumps({"error": str(exc), "kind": "scan"}, ensure_ascii=True) + "\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
