#!/usr/bin/env python3
"""M7 grader: pair first-review findings to answer-key blockers, score per arm (design D-a/D-d, Metrics).

Usage: m7_grade.py MANIFEST.json RESULT.json FINDINGS.json KEYS.json OUT_PREFIX  (writes OUT_PREFIX.json/.md)
MANIFEST.json: {"line_tolerance": N (int >= 0, design leaves N open), "arms": [names]}; its SHA-256 must equal
  RESULT.json["manifest_sha256"] (m7_corpus.py result), so N and the arm list cannot change after the freeze.
FINDINGS.json: {"voided": {cid: reason}, "arms": {arm: {cid: {"status": "ok"|"failed", "findings": [
  {"file", "line", "blocking": bool, "text", "security_only"?: bool}]}}}}; a counted case missing from an arm is
  an arm failure. KEYS.json (outside the repo): {cid: {"split": "archived"|"synthetic", "blockers": [{"file",
  "start", "end"?, "category"}]}}; [] blockers marks a clean case.
Pairing (fail closed): same file, integer line within [start - N, end + N]; maximum one-to-one matching, so the
score does not depend on finding order. A scored finding (blocking or security_only) with a non-integer line, a
non-bool flag, or both flags aborts the run: design line 22 makes line null, so such input needs a decision, not a
guess. security_only findings are non-blocking: credited only in "with security-only" recall, never FP.
FP = blocking finding inside no key range; extra findings inside a range but unmatched are not FP. A clean case has
nothing to miss, so its arm failures are reported in clean_case_arm_failures (a crashed arm must not pass "zero FP").
More than 4 voided+excluded cases aborts (design line 13). Every voided reason must start "D-b1:" or "infra:"; more than 2 "infra:" aborts (D-d). Excluded/voided cases are listed with reasons and never counted; arm failures score as misses.
"""
import hashlib, json, sys
from pathlib import Path

rate = lambda a, b: round(a / b, 4) if b else None
hit = lambda f, b, tol: f["file"] == b["file"] and b["start"] - tol <= f["line"] <= b.get("end", b["start"]) + tol


def match(blockers, findings, tol):  # augmenting-path matching; returns one hit flag per blocker
    owner = {}
    def aug(i, seen):
        for j, f in enumerate(findings):
            if j not in seen and hit(f, blockers[i], tol):
                seen.add(j)
                if j not in owner or aug(owner[j], seen):
                    owner[j] = i
                    return True
        return False
    return [aug(i, set()) for i in range(len(blockers))]


def kind(f):
    blk, sec = f.get("blocking"), f.get("security_only", False)
    if type(blk) is not bool or type(sec) is not bool or (blk and sec) or ((blk or sec) and type(f.get("line")) is not int):
        raise SystemExit(f"bad flags, or scored finding without integer line: {f}")
    return "blk" if blk else "sec" if sec else None


def score_case(blockers, rec, tol):
    if not isinstance(rec, dict) or rec.get("status") != "ok":
        return dict(failed=True, hits=[False] * len(blockers), hits_sec=[False] * len(blockers), fp=0)
    ks = [(f, kind(f)) for f in rec["findings"]]
    blk, sec = [f for f, k in ks if k == "blk"], [f for f, k in ks if k == "sec"]
    return dict(failed=False, hits=match(blockers, blk, tol), hits_sec=match(blockers, blk + sec, tol),
                fp=sum(not any(hit(f, b, tol) for b in blockers) for f in blk))


def stats(items):
    n, h, s = len(items), sum(i["hit"] for i in items), sum(i["hit_sec"] for i in items)
    return dict(key_blockers=n, hits=h, recall=rate(h, n), hits_with_security_only=s, recall_with_security_only=rate(s, n))


def grade(manifest_bytes, result, findings, keys):
    manifest, voided, tol = json.loads(manifest_bytes), findings.get("voided", {}), json.loads(manifest_bytes).get("line_tolerance")
    status = {c["id"]: c["status"] for c in result["cases"]}
    if hashlib.sha256(manifest_bytes).hexdigest() != result.get("manifest_sha256") or type(tol) is not int or tol < 0 or len(status) != len(result["cases"]):
        raise SystemExit("manifest hash mismatch, bad line_tolerance, or duplicate case id")
    if set(status.values()) - {"ok", "excluded"} or not manifest.get("arms") or len(set(voided) | {c for c, v in status.items() if v == "excluded"}) > 4 or set(voided) - set(status) or set(findings["arms"]) != set(manifest.get("arms", [])) \
            or any(set(r) - set(status) for r in findings["arms"].values()) or not all(str(r).startswith(("D-b1:", "infra:")) for r in voided.values()) or sum(str(r).startswith("infra:") for r in voided.values()) > 2:
        raise SystemExit("unknown status, voided id, arm set or case id in an arm record, voided reason not D-b1:/infra:, more than 2 infra, or more than 4 voided+excluded cases (design lines 13, 29, 39)")
    counted = sorted(c for c, s in status.items() if s == "ok" and c not in voided)
    if any(keys.get(c, {}).get("split") not in ("archived", "synthetic") or "blockers" not in keys[c] for c in counted):
        raise SystemExit("answer key (split, blockers) missing for a counted case")
    arms = {}
    for arm, recs in findings["arms"].items():
        rows = {c: score_case(keys[c]["blockers"], recs.get(c), tol) for c in counted}
        items = [dict(case=c, split=keys[c]["split"], category=b["category"], file=b["file"], start=b["start"], hit=r["hits"][i],
                      hit_sec=r["hits_sec"][i]) for c, r in rows.items() for i, b in enumerate(keys[c]["blockers"])]
        group = lambda field: {v: stats([i for i in items if i[field] == v]) for v in sorted({i[field] for i in items})}
        arms[arm] = dict(cases=len(rows), **stats(items), by_split=group("split"), by_category=group("category"),
                         fp_blockers=sum(r["fp"] for r in rows.values()), clean_case_fp_blockers=sum(r["fp"] for c, r in rows.items() if not keys[c]["blockers"]),
                         arm_failures_counted_as_miss=sum(r["failed"] for r in rows.values()), per_blocker=items,
                         clean_case_arm_failures=sum(r["failed"] for c, r in rows.items() if not keys[c]["blockers"]))
    return dict(manifest_sha256=result["manifest_sha256"], line_tolerance=tol, counted_cases=counted,
                excluded_not_counted={c["id"]: c.get("reason", "") for c in result["cases"] if c["status"] == "excluded"}, voided_not_counted=voided, arms=arms)


def table(rep):
    cols = ["cases", "key_blockers", "hits", "recall", "recall_with_security_only", "fp_blockers", "clean_case_fp_blockers", "arm_failures_counted_as_miss", "clean_case_arm_failures"]
    return "\n".join(["| arm | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 1)] + [f"| {a} | " + " | ".join(str(m[c]) for c in cols) + " |" for a, m in rep["arms"].items()] + ["", f"Excluded (not counted): {rep['excluded_not_counted']}", f"Voided (not counted): {rep['voided_not_counted']}", ""])


if __name__ == "__main__":
    rep = grade(Path(sys.argv[1]).read_bytes(), *(json.loads(Path(p).read_text()) for p in sys.argv[2:5]))
    Path(sys.argv[5] + ".json").write_text(json.dumps(rep, indent=2) + "\n")
    Path(sys.argv[5] + ".md").write_text(table(rep))
