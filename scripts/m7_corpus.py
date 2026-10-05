#!/usr/bin/env python3
"""M7 corpus freeze: single clean commit plus staged candidate under case/repo/.

Usage: m7_corpus.py MANIFEST.json   (design: paired_session/docs/m7-seeded-defect-comparison.md D-b2)
Manifest: {"repo": PATH, "out_root": OPTIONAL, "pins": [{"dir": P, "expected_sha256"} | {"repo": P, "commit": 40-hex}],
  "cases": [{"id": "cNN", "base", "diff_path", "diff_sha256", "key_path"}]}
key_path holds the answer-key fix text, one unit per line (whitespace-normalized, >= 20 chars, D-b2).
Fail closed: any error removes the case dir and exits 1; key files are only read, never copied.
"""
import fnmatch, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

norm = lambda text: " ".join(text.split())
under = lambda path, root: root in (path, *path.parents)
sha = lambda data: hashlib.sha256(data).hexdigest()
HEX40 = re.compile(r"[0-9a-f]{40}")
in_results = lambda path: any(a == ".compass" and b == "results" for a, b in zip(path.parts, path.parts[1:]))


def git(*args, cwd=None, data=None):
    return subprocess.run(["git", *args], cwd=cwd, input=data, check=True, capture_output=True).stdout


def tree_texts(root, strict):  # strict (dir pins): symlinks abort; else skipped
    def boom(err):
        raise SystemExit(f"unreadable path under {root}: {err}")
    texts, lines = [], []
    for top, dirs, names in os.walk(root, onerror=boom):
        for path in (Path(top) / n for n in dirs + names):
            if path.is_symlink() and strict:
                raise SystemExit(f"symlink in pinned dir: {path}")
            if path.is_symlink() or path.is_dir():
                continue
            try:
                data = path.read_bytes()
            except OSError as err:
                raise SystemExit(f"unreadable file {path}: {err}")
            texts.append(norm(data.decode("utf-8", "replace")))
            lines.append(f"{path.relative_to(root)} {sha(data)}")
    return texts, sorted(lines)


def pin_texts(pins):
    corpus, recs = [], []
    for pin in pins:  # commit pins are extracted with git archive
        with tempfile.TemporaryDirectory() as tmp:
            if set(pin) == {"repo", "commit"}:
                if not HEX40.fullmatch(str(pin["commit"])):
                    raise SystemExit(f"repo pin commit must be a full 40-hex SHA: {pin}")
                tree = git("-C", pin["repo"], "rev-parse", pin["commit"] + "^{tree}").decode().strip()
                archive = git("-C", pin["repo"], "archive", pin["commit"])
                subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
                rec, strict = {**pin, "tree": tree}, False
            elif set(pin) == {"dir", "expected_sha256"}:
                rec, strict = dict(pin), True
            else:
                raise SystemExit(f"bad pin (need exactly dir+expected_sha256, or repo+commit): {pin}")
            texts, lines = tree_texts(pin.get("dir", tmp), strict)
            if not texts:  # a missing, empty or mistyped pin must not scan nothing silently
                raise SystemExit(f"pin holds no files: {pin}")
            if strict and sha("\n".join(lines).encode()) != pin["expected_sha256"]:
                raise SystemExit(f"dir pin expected_sha256 mismatch: {pin['dir']}")
            corpus += texts
            recs.append(rec)
    return corpus, recs


CLEAN_DIRS = (".agents", ".codex", ".claude")
CLEAN_FILES = ("CLAUDE.md", "CHANGELOG.md")


def cleaned_path(path):
    return (path in CLEAN_FILES or any(path == d or path.startswith(d + "/") for d in CLEAN_DIRS)
            or fnmatch.fnmatchcase(path, "tasks/*-plan*"))


def clean_context(root):
    removed = []
    for path in sorted(root.rglob("*"), key=lambda p: (len(p.parts), str(p))):
        rel = path.relative_to(root).as_posix()
        if cleaned_path(rel) and (path.exists() or path.is_symlink()):
            removed.append(rel)
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    return removed


def case_git(root, *args, data=None, **env):
    """Every git call on a case repo: no inherited GIT_* variables, no system or global config (init.templateDir,
    apply.whitespace, diff.noprefix, filters or hooks there could plant files or change the frozen bytes), no hooks."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, *args], cwd=root, input=data, check=True,
                          capture_output=True, env={**clean, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, **env}).stdout


def worktree_sha256(root, index_listing):
    """m7-s1: the raw bytes and on-disk mode of every indexed path, no git filters (an eol or clean attribute cannot hide
    a change). m7_grade.worktree_sha256 must stay identical."""
    rows = []
    for row in sorted(r for r in index_listing.decode("utf-8", "surrogateescape").split("\0") if r):
        meta, path = row.split("\t", 1)
        disk = root / path
        if meta.split()[0] == "160000":
            rows.append([path, "gitlink", None])
        elif disk.is_symlink():
            rows.append([path, "120000", sha(os.fsencode(os.readlink(disk)))])
        elif disk.is_file():
            rows.append([path, "100755" if disk.stat().st_mode & 0o100 else "100644", sha(disk.read_bytes())])
        else:
            rows.append([path, "missing", None])
    return sha(json.dumps(rows, ensure_ascii=False).encode("utf-8", "surrogateescape"))


def fresh_commit(root, timestamp):
    # Local disposable repository only; no templates, global config or inherited hooks.
    case_git(root, "init", "-q", "--template=", ".")
    # m7-s1b: --force stages every archived file left after the cleanup, so .gitignore cannot filter a tracked file a
    # second time (the archive itself still follows the base's export-ignore / export-subst attributes).
    case_git(root, "add", "-A", "--force")
    case_git(root, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "Candidate baseline",
             GIT_AUTHOR_NAME="M7", GIT_COMMITTER_NAME="M7", GIT_AUTHOR_EMAIL="m7@example.invalid",
             GIT_COMMITTER_EMAIL="m7@example.invalid", GIT_AUTHOR_DATE=timestamp, GIT_COMMITTER_DATE=timestamp)


def freeze_case(case, repo_top, out, pin_corpus):
    cid, case_dir, key = case["id"], out / case["id"], Path(case["key_path"]).resolve()
    if not re.fullmatch(r"c\d{2}", cid) or case_dir.exists():
        raise SystemExit(f"{cid!r}: bad case id or case dir already exists")
    if not key.is_file() or under(key, repo_top) or under(key, out):
        raise SystemExit(f"{cid}: key_path missing, in the repo, or in the corpus dir")
    if in_results(key):
        raise SystemExit(f"{cid}: key_path under a .compass/results directory, which the D-b1 scan denies: {key}")
    if not HEX40.fullmatch(str(case["base"])):
        raise SystemExit(f"{cid}: base must be a full 40-hex SHA")
    diff = Path(case["diff_path"]).read_bytes()
    units = sorted({u for u in map(norm, key.read_text().splitlines()) if len(u) >= 20})
    if sha(diff) != case["diff_sha256"] or not units:
        raise SystemExit(f"{cid}: diff SHA-256 mismatch, or key has no scannable unit (>= 20 chars)")
    root = case_dir / "repo"
    try:
        root.mkdir(parents=True)
        source_tree = git("rev-parse", case["base"] + "^{tree}", cwd=repo_top).decode().strip()
        timestamp = git("show", "-s", "--format=%aI", case["base"], cwd=repo_top).decode().strip()
        archive = git("archive", case["base"], cwd=repo_top)
        subprocess.run(["tar", "-x", "-C", str(root)], input=archive, check=True)
        if any(root.rglob(".git")):
            raise SystemExit(f"{cid}: nested .git left in case dir")
        removed = clean_context(root)
        fresh_commit(root, timestamp)
        base_tree = case_git(root, "rev-parse", "HEAD^{tree}").decode().strip()
        case_git(root, "apply", "--index", "-", data=diff)
        paths = case_git(root, "diff", "--cached", "--name-only", "--no-renames", "-z").decode().split("\0")
        if any(cleaned_path(path) for path in paths if path):
            raise SystemExit(f"{cid}: diff touches cleaned path; replace before freeze")
        staged = case_git(root, "diff", "--cached", "HEAD", "--binary")
        if sha(staged) != sha(diff):
            raise SystemExit(f"{cid}: staged diff hash differs; canonicalize and re-freeze before running")
        tree = case_git(root, "write-tree").decode().strip()
        disk = worktree_sha256(root, case_git(root, "ls-files", "-s", "-z"))
        # Scan the actual candidate files, excluding only fresh Git metadata.
        case_texts = []
        for path in sorted(root.rglob("*")):
            if ".git" not in path.relative_to(root).parts and path.is_file() and not path.is_symlink():
                case_texts.append(norm(path.read_text(errors="replace")))
        (case_dir / "run").mkdir()
    except BaseException:
        shutil.rmtree(case_dir, ignore_errors=True)
        raise
    rec = {"id": cid, "base": case["base"], "source_base_tree": source_tree,
           "base_tree": base_tree, "tree": tree, "worktree_sha256": disk, "diff_sha256": sha(diff),
           "key_sha256": sha(key.read_bytes()), "status": "ok", "scan_sha256": None, "removed_context": removed}
    for where, texts, why in (("pinned source", pin_corpus, "answer-key text in pinned sources"),
                              ("frozen case", case_texts, "key text in frozen case")):
        hits = sum(any(u in t for t in texts) for u in units)
        if hits:
            shutil.rmtree(case_dir)
            # An exclusion is bound to the exact scan inputs. Matching unit hashes
            # are safe to expose; answer-key text itself remains sealed.
            scan = {"kind": "D-b2", "case": cid, "base": case["base"], "diff_sha256": sha(diff),
                    "location": where, "matched_count": hits,
                    "matched_unit_sha256": [sha(u.encode()) for u in units if any(u in t for t in texts)]}
            return {**rec, "status": "excluded", "reason": why, "matched_count": hits,
                    "location": where, "scan": scan, "scan_sha256": sha(json.dumps(scan, sort_keys=True).encode())}
    return rec


def main(argv):
    manifest_path = Path(argv[1])
    manifest = json.loads(manifest_path.read_text())
    repo_top = Path(git("-C", manifest["repo"], "rev-parse", "--show-toplevel").decode().strip()).resolve()
    out = Path(manifest.get("out_root", "/private/tmp/claude-501/m7-corpus")).resolve()
    if under(out, (Path.home() / "3Cats").resolve()) or any((p / ".git").exists() for p in (out, *out.parents)):
        raise SystemExit(f"out_root inside ~/3Cats or a git repo: {out}")
    if in_results(out):   # m7-s1b: every read of such a case would trip the D-b1 deny pattern (m7-s4 pilot)
        raise SystemExit(f"out_root under a .compass/results directory, which the D-b1 scan denies: {out}")
    out.mkdir(parents=True, exist_ok=True)
    pin_corpus, pin_recs = pin_texts(manifest["pins"])
    if not pin_corpus:
        raise SystemExit("pins hold no files; nothing to scan against")
    cases = [freeze_case(c, repo_top, out, pin_corpus) for c in manifest["cases"]]
    # Complete the immutable manifest before any reviewer run. The input manifest
    # remains separately hashed; no post-run modification is permitted.
    bindings = {c["id"]: c for c in cases}
    frozen = {**manifest, "cases": [{**c, **{k: bindings[c["id"]][k] for k in
              ("source_base_tree", "base_tree", "tree", "worktree_sha256", "key_sha256", "status", "scan_sha256")}} for c in manifest["cases"]]}
    frozen_bytes = (json.dumps(frozen, indent=2, sort_keys=True) + "\n").encode()
    (out / "frozen-manifest.json").write_bytes(frozen_bytes)
    result = {"manifest_sha256": sha(frozen_bytes), "input_manifest_sha256": sha(manifest_path.read_bytes()),
              "pins": pin_recs, "cases": cases}
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({c["id"]: c["status"] for c in cases}))


if __name__ == "__main__":
    main(sys.argv)
