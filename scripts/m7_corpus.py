#!/usr/bin/env python3
"""M7 corpus freeze: build history-free case dirs, verify diff hashes, exclude leaks.

Usage: m7_corpus.py MANIFEST.json   (design: paired_session/docs/m7-seeded-defect-comparison.md D-b2)
Manifest: {"repo": PATH, "out_root": OPTIONAL, "pins": [{"dir": P, "expected_sha256"} | {"repo": P, "commit": 40-hex}],
  "cases": [{"id": "cNN", "base", "diff_path", "diff_sha256", "key_path"}]}
key_path holds the answer-key fix text, one unit per line (whitespace-normalized, >= 20 chars, D-b2).
Fail closed: any error removes the case dir and exits 1; key files are only read, never copied.
"""
import hashlib, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

norm = lambda text: " ".join(text.split())
under = lambda path, root: root in (path, *path.parents)
sha = lambda data: hashlib.sha256(data).hexdigest()
HEX40 = re.compile(r"[0-9a-f]{40}")


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


def freeze_case(case, repo_top, out, pin_corpus):
    cid, case_dir, key = case["id"], out / case["id"], Path(case["key_path"]).resolve()
    if not re.fullmatch(r"c\d{2}", cid) or case_dir.exists():
        raise SystemExit(f"{cid!r}: bad case id or case dir already exists")
    if not key.is_file() or under(key, repo_top) or under(key, out):
        raise SystemExit(f"{cid}: key_path missing, in the repo, or in the corpus dir")
    if not HEX40.fullmatch(str(case["base"])):
        raise SystemExit(f"{cid}: base must be a full 40-hex SHA")
    diff = Path(case["diff_path"]).read_bytes()
    units = sorted({u for u in map(norm, key.read_text().splitlines()) if len(u) >= 20})
    if sha(diff) != case["diff_sha256"] or not units:
        raise SystemExit(f"{cid}: diff SHA-256 mismatch, or key has no scannable unit (>= 20 chars)")
    try:
        git("clone", "--quiet", "--no-checkout", str(repo_top), str(case_dir))
        git("checkout", "--quiet", "--detach", case["base"], cwd=case_dir)
        git("apply", "-", cwd=case_dir, data=diff)  # the exact bytes that were hashed
        base_tree = git("rev-parse", "HEAD^{tree}", cwd=case_dir).decode().strip()
        shutil.rmtree(case_dir / ".git")
        if any(case_dir.rglob(".git")):
            raise SystemExit(f"{cid}: nested .git left in case dir")
        case_texts = tree_texts(case_dir, False)[0]
    except BaseException:
        shutil.rmtree(case_dir, ignore_errors=True)
        raise
    rec = {"id": cid, "base": case["base"], "base_tree": base_tree, "diff_sha256": sha(diff),
           "key_sha256": sha(key.read_bytes()), "status": "ok"}
    for where, texts, why in (("pinned source", pin_corpus, "answer-key text in pinned sources"),
                              ("frozen case", case_texts, "key text in frozen case")):
        hits = sum(any(u in t for t in texts) for u in units)  # only the count is recorded, never the text
        if hits:
            shutil.rmtree(case_dir)  # excluded cases keep no directory
            return {**rec, "status": "excluded", "reason": why, "matched_count": hits, "location": where}
    return rec


def main(argv):
    manifest_path = Path(argv[1])
    manifest = json.loads(manifest_path.read_text())
    repo_top = Path(git("-C", manifest["repo"], "rev-parse", "--show-toplevel").decode().strip()).resolve()
    out = Path(manifest.get("out_root", "/private/tmp/claude-501/m7-corpus")).resolve()
    if under(out, (Path.home() / "3Cats").resolve()) or any((p / ".git").exists() for p in (out, *out.parents)):
        raise SystemExit(f"out_root inside ~/3Cats or a git repo: {out}")
    out.mkdir(parents=True, exist_ok=True)
    pin_corpus, pin_recs = pin_texts(manifest["pins"])
    if not pin_corpus:
        raise SystemExit("pins hold no files; nothing to scan against")
    cases = [freeze_case(c, repo_top, out, pin_corpus) for c in manifest["cases"]]
    result = {"manifest_sha256": sha(manifest_path.read_bytes()), "pins": pin_recs, "cases": cases}
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({c["id"]: c["status"] for c in cases}))


if __name__ == "__main__":
    main(sys.argv)
