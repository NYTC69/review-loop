#!/usr/bin/env python3
"""M7 offline scoring with explicit independent mechanism adjudication.

CLI: m7_grade.py FROZEN_MANIFEST RESULT FINDINGS KEYS OUT_PREFIX [ADJUDICATION]
The CLI verifies case/repo Git trees and staged diff bytes beside RESULT.
Null lines are valid. Location alone never earns a HIT. Without bound, independent
code-reading votes, findings are ADJUDICATION_REQUIRED and score_ready is false.
Exact repeated prose is deduplicated across nearby lines; matching is one-to-one.
Adjudication and exclusion evidence are data inputs, NOT authenticated producer,
model-execution, native-isolation or release-approval evidence. See the M7 doc's
tooling contract and real-run prerequisites before using this format in a real comparison.
"""
import hashlib, json, os, sys
from pathlib import Path

rate = lambda a, b: round(a / b, 4) if b else None
canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
digest = lambda value: hashlib.sha256(canonical(value)).hexdigest()


def seed_id(b):
    return digest(b)


def finding_id(f):
    # Repeating identical prose at different nearby lines cannot buy another HIT.
    return digest({"file": f.get("file"), "text": " ".join(str(f.get("text", "")).split())})


def hit(f, b, tol):
    return (f["file"] == b["file"] and (f["line"] is None or
            b["start"] - tol <= f["line"] <= b.get("end", b["start"]) + tol))


def match(blockers, findings, tol, approved=None):
    """Stable maximum one-to-one matching of independently adjudicated edges."""
    approved = approved or set()
    owner = {}
    def aug(i, seen):
        for j in sorted(range(len(findings)), key=lambda j: finding_id(findings[j])):
            f = findings[j]
            if j not in seen and hit(f, blockers[i], tol) and (finding_id(f), seed_id(blockers[i])) in approved:
                seen.add(j)
                if j not in owner or aug(owner[j], seen):
                    owner[j] = i
                    return True
        return False
    for i in sorted(range(len(blockers)), key=lambda i: seed_id(blockers[i])):
        aug(i, set())
    return [i in owner.values() for i in range(len(blockers))]


def kind(f):
    if not isinstance(f, dict):
        raise SystemExit("finding must be an object")
    blk, sec, line = f.get("blocking"), f.get("security_only", False), f.get("line")
    if (type(blk) is not bool or type(sec) is not bool or (blk and sec)
            or (line is not None and (type(line) is not int or line < 1))
            or not isinstance(f.get("file"), str) or not f["file"]):
        raise SystemExit("bad finding flags, file or line (positive integer or null required)")
    return "blk" if blk else "sec" if sec else None


def consensus(votes):
    """Validate explicit code-reading decisions; never infer them from prose."""
    if not isinstance(votes, list) or len(votes) not in (2, 3):
        return None
    seen = set()
    for v in votes:
        if not isinstance(v, dict) or not all(isinstance(v.get(k), str) and v[k].strip()
                for k in ("grader", "vendor", "model", "verdict", "rationale", "code_evidence")):
            raise SystemExit("incomplete adjudication vote")
        if v["grader"] in seen:
            raise SystemExit("duplicate adjudicator")
        seen.add(v["grader"])
        if v["verdict"] not in ("HIT", "PARTIAL", "MISS", "FP", "VALID-NOT-IN-KEY", "DUPLICATE"):
            raise SystemExit("unknown adjudication verdict")
        if v["verdict"] == "HIT" and (v.get("mechanism_match") is not True or
                not isinstance(v.get("category"), str)):
            raise SystemExit("HIT needs explicit mechanism and category agreement")
    if (votes[0]["vendor"].strip().casefold() == votes[1]["vendor"].strip().casefold() or
            not any(v["vendor"].strip().casefold() == "anthropic" and "opus" in v["model"].lower() for v in votes[:2])):
        raise SystemExit("first two graders need Opus and a different vendor")
    decision = lambda v: (v["verdict"], v.get("seed"), v.get("category"), v.get("mechanism_match"), v.get("duplicate_of"))
    if decision(votes[0]) == decision(votes[1]):
        if len(votes) != 2:
            raise SystemExit("third pass only for disagreement")
        return votes[0]
    # Disagreement is unresolved until a separate third pass. Conservatively
    # require it to agree with one prior decision, rather than invent a fourth.
    if len(votes) == 3 and decision(votes[2]) in [decision(v) for v in votes[:2]]:
        return votes[2]
    return None


def score_case(blockers, rec, tol, decisions=None):
    empty = dict(failed=True, hits=[False] * len(blockers), hits_sec=[False] * len(blockers),
                 fp=0, duplicates=0, valid_not_in_key=[], adjudication_required=[], outcomes=[])
    if not isinstance(rec, dict) or rec.get("status") != "ok":
        return empty
    if not isinstance(rec.get("findings"), list):
        raise SystemExit("findings must be a list")
    ks = [(f, kind(f)) for f in rec["findings"]]
    decisions = decisions or {}
    unique, duplicates = {}, 0
    for f, k in sorted(ks, key=lambda x: (x[1] != "blk", canonical(x[0]))):
        if not k:
            continue
        fid = finding_id(f)
        if fid in unique:
            duplicates += 1
        else:
            unique[fid] = (f, k)
    if set(decisions) - set(unique):
        raise SystemExit("adjudication for unknown finding")
    approved, pending, valid, outcomes = set(), [], [], []
    fp = 0
    by_seed = {seed_id(b): b for b in blockers}
    valid_text = lambda f: isinstance(f.get("text"), str) and f["text"].strip()
    votes_of = {fid: consensus(decisions.get(fid, [])) for fid, (f, k) in unique.items() if valid_text(f)}
    for fid, (f, k) in sorted(unique.items()):
        if not valid_text(f):
            if k == "blk": fp += 1
            outcomes.append({"finding": fid, "verdict": "INVALID-TEXT"})
            continue
        vote = votes_of[fid]
        if vote is not None and vote["verdict"] == "DUPLICATE":   # m7-s1: only of a finding with its own verdict
            target = vote.get("duplicate_of")
            if target not in unique or target == fid or not valid_text(unique[target][0]):
                raise SystemExit("duplicate needs another finding")
            if votes_of[target] is not None and votes_of[target]["verdict"] == "DUPLICATE":
                raise SystemExit("duplicate chains and cycles are refused: point at the scored finding")
            if votes_of[target] is None:   # that finding is still pending, so this one is too
                vote = None
        if vote is None:
            pending.append({"finding": fid, "reason": "independent mechanism/code adjudication required",
                            "candidate_seeds": sorted(seed_id(b) for b in blockers if hit(f, b, tol))})
            continue
        verdict, sid = vote["verdict"], vote.get("seed")
        if verdict in ("HIT", "PARTIAL", "MISS") and sid is not None and sid not in by_seed:
            raise SystemExit("adjudication references unknown seed")
        if verdict == "HIT":
            b = by_seed.get(sid)
            if b is None or vote["category"] != b["category"]:
                raise SystemExit("HIT contradicts seed category")
            if not hit(f, b, tol):
                pending.append({"finding": fid, "reason": "HIT vote conflicts with frozen file/location filter",
                                "candidate_seeds": sorted(seed_id(x) for x in blockers if hit(f, x, tol))})
                outcomes.append({"finding": fid, "verdict": "ADJUDICATION_REQUIRED", "seed": sid})
                continue
            approved.add((fid, sid))
        elif verdict == "VALID-NOT-IN-KEY":
            if sid is not None:
                raise SystemExit("VALID-NOT-IN-KEY must not target a seed")
            valid.append({"finding": fid, "evidence": vote["code_evidence"], "rationale": vote["rationale"]})
        elif verdict == "FP" and k == "blk":
            fp += 1
        elif verdict == "DUPLICATE":   # target checked above
            duplicates += 1
        outcomes.append({"finding": fid, "verdict": verdict, "seed": sid})
    blk = [f for f, k in unique.values() if k == "blk"]
    sec = [f for f, k in unique.values() if k == "sec"]
    # Multiple independently phrased reports of one seed are explicitly redundant.
    hits = match(blockers, blk, tol, approved)
    hits_sec = match(blockers, blk + sec, tol, approved)
    duplicates += max(0, len({fid for fid, sid in approved}) - sum(hits_sec))
    return dict(failed=False, hits=hits, hits_sec=hits_sec, fp=fp, duplicates=duplicates,
                valid_not_in_key=valid, adjudication_required=pending, outcomes=outcomes)


def stats(items):
    n, h, s = len(items), sum(i["hit"] for i in items), sum(i["hit_sec"] for i in items)
    return dict(key_blockers=n, hits=h, recall=rate(h, n), hits_with_security_only=s, recall_with_security_only=rate(s, n))


def check_keys(manifest, status, keys, keys_bytes):
    ids = manifest.get("cases")
    if isinstance(ids, list):  # m7_corpus.py manifests list case objects {"id", ...}; bare id strings also work
        ids = [c.get("id") if isinstance(c, dict) else c for c in ids]
    frozen = keys_bytes is not None and hashlib.sha256(keys_bytes).hexdigest() == manifest.get("keys_sha256")
    if not frozen or json.loads(keys_bytes) != keys:
        raise SystemExit("KEYS.json bytes do not match manifest keys_sha256")
    if not isinstance(ids, list) or not all(type(i) is str for i in ids) or len(set(ids)) != len(ids) \
            or set(ids) != set(status) or not set(ids) <= set(keys):
        raise SystemExit("manifest cases: duplicate, not equal to the RESULT case ids, or a case has no key entry")
    for c in ids:
        k = keys[c]
        bl = k.get("blockers") if isinstance(k, dict) else None
        if not isinstance(bl, list) or (k.get("clean") is True) != (not bl):
            raise SystemExit(f"case {c}: empty blockers need clean=true, and blockers exclude clean=true")
        if not all(isinstance(b, dict) and type(b.get("start")) is int and type(b.get("end", b["start"])) is int
                   and b.get("end", b["start"]) >= b["start"] for b in bl):
            raise SystemExit(f"case {c}: blocker start/end must be int with end >= start")
        if any(not isinstance(b.get("file"), str) or not b["file"] or not isinstance(b.get("category"), str)
               or not b["category"] for b in bl) or len({seed_id(b) for b in bl}) != len(bl):
            raise SystemExit("bad or duplicate blocker identity")


def check_adjudication(manifest_bytes, result, findings, keys_bytes, adjudication):
    if adjudication is None:
        return {}
    expected = {"manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "result_sha256": digest(result), "findings_sha256": digest(findings),
                "keys_sha256": hashlib.sha256(keys_bytes).hexdigest()}
    if not isinstance(adjudication, dict) or adjudication.get("binding") != expected:
        raise SystemExit("adjudication binding mismatch")
    arms = adjudication.get("arms", {})
    if not isinstance(arms, dict) or set(arms) - set(findings["arms"]):
        raise SystemExit("unknown adjudication arm")
    for arm, cases in arms.items():
        if not isinstance(cases, dict) or set(cases) - set(findings["arms"][arm]):
            raise SystemExit("unknown adjudication case")
    return arms


def check_bindings(manifest, result, voided, findings, adjudication=None):
    cases = manifest.get("cases", [])
    if not all(isinstance(c, dict) for c in cases):
        raise SystemExit("frozen case objects with base/diff/tree binding required")
    expected = {c["id"]: c for c in cases}
    fields = {"base": 40, "source_base_tree": 40, "base_tree": 40, "tree": 40,
              "diff_sha256": 64, "key_sha256": 64}
    for rec in result["cases"]:
        target = expected[rec["id"]]
        for field, length in fields.items():
            value = target.get(field)
            if (not isinstance(value, str) or len(value) != length or any(c not in "0123456789abcdef" for c in value)
                    or rec.get(field) != value):
                raise SystemExit("case base/diff/tree/key binding mismatch: " + rec["id"] + ":" + field)
        if target.get("status") not in ("ok", "excluded") or rec["status"] != target["status"]:
            raise SystemExit("frozen status binding mismatch: " + rec["id"])
        if "scan_sha256" not in target or rec.get("scan_sha256") != target["scan_sha256"]:
            raise SystemExit("frozen scan binding mismatch: " + rec["id"])
        if target["status"] == "ok" and (target["scan_sha256"] is not None or rec.get("scan") is not None):
            raise SystemExit("ok case must have no frozen scan evidence")
        if rec["status"] == "excluded":
            scan = rec.get("scan", {})
            if (not isinstance(scan, dict) or scan.get("kind") != "D-b2" or scan.get("case") != rec["id"]
                    or scan.get("base") != rec["base"] or scan.get("diff_sha256") != rec["diff_sha256"]
                    or type(scan.get("matched_count")) is not int or scan["matched_count"] <= 0
                    or scan["matched_count"] != len(scan.get("matched_unit_sha256", []))
                    or scan.get("location") not in ("pinned source", "frozen case")
                    or rec.get("scan_sha256") != hashlib.sha256(json.dumps(scan, sort_keys=True).encode()).hexdigest()):
                raise SystemExit("excluded case lacks bound D-b2 scan evidence")
    scans = (adjudication or {}).get("exclusion_evidence", {})
    for cid, reason in voided.items():
        scan = scans.get(cid)
        if not isinstance(scan, dict) or scan.get("case") != cid or scan.get("reason") != reason:
            raise SystemExit("voided case requires independent bound scan/log evidence")
        if scan.get("base") != expected[cid]["base"] or scan.get("diff_sha256") != expected[cid]["diff_sha256"]:
            raise SystemExit("void evidence base/diff binding mismatch")
        if scan.get("arm") not in findings["arms"]:
            raise SystemExit("void evidence missing cause arm")
        artifacts = scan.get("artifacts", {})
        required = ("transcript", "tool_log") if reason.startswith("D-b1:") else ("infrastructure_log",)
        for name in required:
            artifact = artifacts.get(name)
            if not isinstance(artifact, dict) or not isinstance(artifact.get("content"), str) or not artifact["content"]:
                raise SystemExit("void evidence missing artifact bytes")
            if hashlib.sha256(artifact["content"].encode()).hexdigest() != artifact.get("sha256"):
                raise SystemExit("void evidence artifact digest mismatch")
        if reason.startswith("D-b1:"):
            violations = scan.get("violations")
            if not isinstance(violations, list) or not violations:
                raise SystemExit("D-b1 evidence needs resolved scan violations")
            for v in violations:
                if not isinstance(v, dict) or not all(isinstance(v.get(k), str) and v[k]
                       for k in ("cause", "raw_input", "resolution", "policy_rule")):
                    raise SystemExit("incomplete D-b1 scan violation")
                artifact = artifacts.get(v.get("artifact"))
                line = v.get("line")
                if not artifact or type(line) is not int or not 1 <= line <= len(artifact["content"].splitlines()):
                    raise SystemExit("D-b1 scan reference outside supplied artifact")
                if v["raw_input"] not in artifact["content"].splitlines()[line - 1]:
                    raise SystemExit("D-b1 scan input not in referenced log line")
        elif not isinstance(scan.get("cause"), str) or not scan["cause"].strip():
            raise SystemExit("infrastructure exclusion requires attributed cause")


RUN_ARTIFACTS = (".review-loop/",)   # the legacy arm's config and launcher tmp (plan §2), kept out of the diff by info/exclude


def worktree_sha256(root, index_listing):
    """m7-s1: the raw bytes and on-disk mode of every indexed path, no git filters (an eol or clean attribute cannot hide
    a change, and skip-worktree or assume-unchanged flags hide nothing). Identical to m7_corpus.worktree_sha256."""
    sha = lambda data: hashlib.sha256(data).hexdigest()
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


def verify_workspace(manifest, result, root):
    """CLI checks the frozen bytes; this is not producer authentication or OS isolation."""
    import subprocess
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}   # as m7_corpus.case_git: no inherited GIT_*,
    env = {**clean, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}  # system/global config or hooks
    def git(repo, *args):
        try:
            return subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, *args], cwd=repo, check=True,
                                  capture_output=True, env=env).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SystemExit("frozen workspace unavailable: " + str(repo)) from exc
    for c in result["cases"]:
        if c["status"] != "ok":
            continue
        # Opaque IDs only; never allow result metadata to address an arbitrary path.
        import re
        if not re.fullmatch(r"c\d{2}", c["id"]):
            raise SystemExit("invalid frozen case id")
        repo = root / c["id"] / "repo"
        if repo.is_symlink() or repo.parent.is_symlink():
            raise SystemExit("symlinked frozen workspace")
        if git(repo, "rev-parse", "--show-toplevel").decode().strip() != str(repo.resolve()):
            raise SystemExit("wrong frozen repository")
        if git(repo, "rev-list", "--count", "HEAD").strip() != b"1":
            raise SystemExit("frozen repo must have exactly one commit")
        if git(repo, "rev-parse", "HEAD^{tree}").decode().strip() != c.get("base_tree"):
            raise SystemExit("frozen init tree mismatch")
        if git(repo, "write-tree").decode().strip() != c.get("tree"):
            raise SystemExit("frozen candidate tree mismatch")
        others = [p for p in git(repo, "ls-files", "--others", "-z").decode().split("\0") if p]
        if git(repo, "diff", "--name-only") or any(not p.startswith(RUN_ARTIFACTS) for p in others):
            raise SystemExit("frozen workspace has unstaged or untracked changes")
        frozen = {f["id"]: f for f in manifest.get("cases", []) if isinstance(f, dict)}.get(c["id"], {})
        if not isinstance(frozen.get("worktree_sha256"), str) or len(frozen["worktree_sha256"]) != 64:
            raise SystemExit("frozen manifest lacks worktree_sha256 (frozen before m7-s1): re-freeze")
        if worktree_sha256(repo, git(repo, "ls-files", "-s", "-z")) != frozen["worktree_sha256"]:
            raise SystemExit("frozen workspace file bytes or modes differ from the freeze")
        if hashlib.sha256(git(repo, "diff", "--cached", "HEAD", "--binary")).hexdigest() != c.get("diff_sha256"):
            raise SystemExit("frozen staged diff mismatch")


def grade(manifest_bytes, result, findings, keys, keys_bytes=None, adjudication=None):
    manifest, voided, tol = json.loads(manifest_bytes), findings.get("voided", {}), json.loads(manifest_bytes).get("line_tolerance")
    status = {c["id"]: c["status"] for c in result["cases"]}
    if hashlib.sha256(manifest_bytes).hexdigest() != result.get("manifest_sha256") or type(tol) is not int or tol < 0 or len(status) != len(result["cases"]):
        raise SystemExit("manifest hash mismatch, bad line_tolerance, or duplicate case id")
    if set(status.values()) - {"ok", "excluded"} or not manifest.get("arms") or len(set(voided) | {c for c, v in status.items() if v == "excluded"}) > 4 or set(voided) - set(status) or set(findings["arms"]) != set(manifest.get("arms", [])) \
            or any(set(r) - set(status) for r in findings["arms"].values()) or not all(str(r).startswith(("D-b1:", "infra:")) for r in voided.values()) or sum(str(r).startswith("infra:") for r in voided.values()) > 2:
        raise SystemExit("unknown status, voided id, arm set or case id in an arm record, voided reason not D-b1:/infra:, more than 2 infra, or more than 4 voided+excluded cases (design lines 13, 29, 39)")
    check_keys(manifest, status, keys, keys_bytes)
    check_bindings(manifest, result, voided, findings, adjudication)
    decisions = check_adjudication(manifest_bytes, result, findings, keys_bytes, adjudication)
    counted = sorted(c for c, s in status.items() if s == "ok" and c not in voided)
    if any(keys.get(c, {}).get("split") not in ("archived", "synthetic") or "blockers" not in keys[c] for c in counted):
        raise SystemExit("answer key (split, blockers) missing for a counted case")
    arms = {}
    for arm, recs in findings["arms"].items():
        rows = {c: score_case(keys[c]["blockers"], recs.get(c), tol, decisions.get(arm, {}).get(c)) for c in counted}
        items = [dict(case=c, split=keys[c]["split"], category=b["category"], file=b["file"], start=b["start"], seed=seed_id(b), hit=r["hits"][i],
                      hit_sec=r["hits_sec"][i]) for c, r in rows.items() for i, b in enumerate(keys[c]["blockers"])]
        group = lambda field: {v: stats([i for i in items if i[field] == v]) for v in sorted({i[field] for i in items})}
        arms[arm] = dict(cases=len(rows), **stats(items), by_split=group("split"), by_category=group("category"),
                         fp_blockers=sum(r["fp"] for r in rows.values()), clean_case_fp_blockers=sum(r["fp"] for c, r in rows.items() if not keys[c]["blockers"]),
                         arm_failures_counted_as_miss=sum(r["failed"] for r in rows.values()), per_blocker=sorted(items, key=lambda x: (x["case"], x["category"], x["file"], x["start"], x["seed"])),
                         details=rows, duplicates=sum(r["duplicates"] for r in rows.values()),
                         adjudication_required=sum(len(r["adjudication_required"]) for r in rows.values()),
                         valid_not_in_key=sum(len(r["valid_not_in_key"]) for r in rows.values()),
                         clean_case_arm_failures=sum(r["failed"] for c, r in rows.items() if not keys[c]["blockers"]))
    return dict(manifest_sha256=result["manifest_sha256"], line_tolerance=tol, counted_cases=counted,
                score_ready=not any(a["adjudication_required"] for a in arms.values()),
                evidence_authority="UNAUTHENTICATED_INPUTS_NO_NATIVE_PROOF",
                excluded_not_counted={c["id"]: c.get("reason", "") for c in result["cases"] if c["status"] == "excluded"}, voided_not_counted=voided, arms=arms)


def table(rep):
    cols = ["cases", "key_blockers", "hits", "recall", "recall_with_security_only", "fp_blockers", "clean_case_fp_blockers", "arm_failures_counted_as_miss", "clean_case_arm_failures", "adjudication_required", "duplicates", "valid_not_in_key"]
    return "\n".join(["| arm | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 1)] + [f"| {a} | " + " | ".join(str(m[c]) for c in cols) + " |" for a, m in rep["arms"].items()] + ["", f"Excluded (not counted): {rep['excluded_not_counted']}", f"Voided (not counted): {rep['voided_not_counted']}", ""])


if __name__ == "__main__":
    kb = Path(sys.argv[4]).read_bytes()
    docs = [json.loads(Path(p).read_text()) for p in sys.argv[2:4]]
    adj = json.loads(Path(sys.argv[6]).read_text()) if len(sys.argv) > 6 else None
    verify_workspace(json.loads(Path(sys.argv[1]).read_text()), docs[0], Path(sys.argv[2]).resolve().parent)
    rep = grade(Path(sys.argv[1]).read_bytes(), *docs, json.loads(kb), kb, adj)
    Path(sys.argv[5] + ".json").write_text(json.dumps(rep, indent=2) + "\n")
    Path(sys.argv[5] + ".md").write_text(table(rep))
