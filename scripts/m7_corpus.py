#!/usr/bin/env python3
"""M7 corpus freeze: build history-free case dirs, verify diff hashes, exclude leaks.

Usage: m7_corpus.py MANIFEST.json   (design: paired_session/docs/m7-seeded-defect-comparison.md D-b2)
Manifest: {"repo": PATH, "out_root": OPTIONAL, "pins": [{"dir": P} | {"repo": P, "commit": SHA}],
  "cases": [{"id": "cNN", "base", "diff_path", "diff_sha256", "key_path"}]}
key_path holds the answer-key fix text, one unit per line (whitespace-normalized, >= 20 chars, D-b2).
Fail closed: any error removes the case dir and exits 1; key files are only read, never copied.
"""
import hashlib, json, re, shutil, subprocess, sys, tempfile
from pathlib import Path

norm = lambda text: " ".join(text.split())
under = lambda path, root: root in (path, *path.parents)
sha = lambda data: hashlib.sha256(data).hexdigest()


def git(*args, cwd=None, data=None):
    return subprocess.run(["git", *args], cwd=cwd, input=data, check=True, capture_output=True).stdout


def pin_texts(pins):
    for pin in pins:  # commit pins are extracted with git archive
        with tempfile.TemporaryDirectory() as tmp:
            if set(pin) == {"repo", "commit"}:
                subprocess.run(["tar", "-x", "-C", tmp], input=git("-C", pin["repo"], "archive", pin["commit"]), check=True)
            elif set(pin) != {"dir"}:
                raise SystemExit(f"bad pin (need exactly dir, or repo+commit): {pin}")
            files = [f for f in Path(pin.get("dir", tmp)).rglob("*") if f.is_file() and not f.is_symlink()]
            if not files:  # a missing, empty or mistyped pin must not scan nothing silently
                raise SystemExit(f"pin holds no files: {pin}")
            yield from (norm(f.read_bytes().decode("utf-8", "replace")) for f in files)


def freeze_case(case, repo_top, out, pin_corpus):
    cid, case_dir, key = case["id"], out / case["id"], Path(case["key_path"]).resolve()
    if not re.fullmatch(r"c\d{2}", cid) or case_dir.exists():
        raise SystemExit(f"{cid!r}: bad case id or case dir already exists")
    if not key.is_file() or under(key, repo_top) or under(key, out):
        raise SystemExit(f"{cid}: key_path missing, in the repo, or in the corpus dir")
    diff = Path(case["diff_path"]).read_bytes()
    units = sorted({u for u in map(norm, key.read_text().splitlines()) if len(u) >= 20})
    if sha(diff) != case["diff_sha256"] or not units:
        raise SystemExit(f"{cid}: diff SHA-256 mismatch, or key has no scannable unit (>= 20 chars)")
    try:
        git("clone", "--quiet", "--no-checkout", str(repo_top), str(case_dir))
        git("checkout", "--quiet", "--detach", case["base"], cwd=case_dir)
        git("apply", "-", cwd=case_dir, data=diff)  # the exact bytes that were hashed
        shutil.rmtree(case_dir / ".git")
        if any(case_dir.rglob(".git")):
            raise SystemExit(f"{cid}: nested .git left in case dir")
    except BaseException:
        shutil.rmtree(case_dir, ignore_errors=True)
        raise
    rec = {"id": cid, "base": case["base"], "diff_sha256": sha(diff), "key_sha256": sha(key.read_bytes()), "status": "ok"}
    hits = sorted({u for u in units for text in pin_corpus if u in text})
    if hits:
        shutil.rmtree(case_dir)  # excluded cases keep no directory
        rec.update(status="excluded", reason=f"answer-key text in pinned sources ({len(hits)} unit(s))", matched_units=hits)
    return rec


def main(argv):
    manifest_path = Path(argv[1])
    manifest = json.loads(manifest_path.read_text())
    repo_top = Path(git("-C", manifest["repo"], "rev-parse", "--show-toplevel").decode().strip()).resolve()
    out = Path(manifest.get("out_root", "/private/tmp/claude-501/m7-corpus")).resolve()
    if under(out, (Path.home() / "3Cats").resolve()) or any((p / ".git").exists() for p in (out, *out.parents)):
        raise SystemExit(f"out_root inside ~/3Cats or a git repo: {out}")
    out.mkdir(parents=True, exist_ok=True)
    pin_corpus = list(pin_texts(manifest["pins"]))
    if not pin_corpus:
        raise SystemExit("pins hold no files; nothing to scan against")
    cases = [freeze_case(c, repo_top, out, pin_corpus) for c in manifest["cases"]]
    (out / "result.json").write_text(json.dumps({"manifest_sha256": sha(manifest_path.read_bytes()), "cases": cases}, indent=2) + "\n")
    print(json.dumps({c["id"]: c["status"] for c in cases}))


if __name__ == "__main__":
    main(sys.argv)
