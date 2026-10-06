#!/usr/bin/env python3
"""M7 collector: producer receipts, the legacy first-review source, the finding normaliser and grader records.

Producer authentication (design OQ5). The harness holds a per-run secret (`keygen`, mode 0600, outside every case, arm
workspace and corpus) and signs, right after each arm or grader run, a receipt binding the case, arm, window, frozen
manifest hash, base and diff to the SHA-256 of every artifact it collected, the provider ids seen in the streams and the
D-b1 scan status. `verify` re-checks the HMAC and the artifact bytes. A receipt proves that the holder of the secret
registered exactly these bytes for this case; it does not prove that a provider produced them (the recorded provider
session/message ids allow that cross-check against provider-side logs) and it cannot catch a harness that lies when it
signs.

Normaliser (design D-c): both arms become {file, line: null, blocking, security_only, text} with one text template,
"<summary>\\nScenario: <scenario>"; blocking = CRITICAL/MAJOR; paired findings with severity SECURITY, or security below
MAJOR, are security-only. Legacy raw text = the launcher's per-invocation result.txt of the first first-review call that
exited cleanly, found through the launcher summaries in the orchestrator's own stream-json (design erratum m7-s1).

Grader records: one file per grader and case, with blinded arm labels; `adjudication` maps them back, orders the two
initial votes before a third pass and binds the document the way m7_grade.check_adjudication expects.

CLI: m7_collect.py keygen SECRET_PATH | verify SECRET RECEIPT ROOT
"""
import hashlib, hmac, json, os, re, secrets, sys
from pathlib import Path

sha = lambda data: hashlib.sha256(data).hexdigest()
canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
digest = lambda value: sha(canonical(value))


# --- producer receipts ------------------------------------------------------------------------------------------------
def keygen(path):
    path = Path(path).resolve()
    if path.exists() or any((p / ".git").exists() for p in path.parents):
        raise SystemExit(f"secret path exists or lies inside a git repository: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secrets.token_hex(32) + "\n")
    return path


def load_secret(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise SystemExit(f"secret readable by others: {path}")
    key = bytes.fromhex(path.read_text().strip())
    if len(key) != 32:
        raise SystemExit("secret must be 32 bytes")
    return key


def sign(key, payload):
    return {"payload": payload, "hmac_sha256": hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()}


def verify(key, receipt, root=None):
    """The payload of an authentic receipt; with ROOT, every artifact's bytes must still match."""
    if not isinstance(receipt, dict) or not isinstance(receipt.get("payload"), dict):
        raise SystemExit("not a receipt")
    expected = hmac.new(key, canonical(receipt["payload"]), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(receipt.get("hmac_sha256"))):
        raise SystemExit("receipt HMAC mismatch: not produced by this run's harness, or changed after signing")
    if root is not None:
        for rel, want in receipt["payload"].get("artifacts", {}).items():
            path = (Path(root) / rel).resolve()
            if not str(path).startswith(str(Path(root).resolve()) + os.sep) or sha(path.read_bytes()) != want:
                raise SystemExit("receipt artifact changed or outside its root: " + rel)
    return receipt["payload"]


def provider_ids(lines):
    """Session, message and thread ids from Claude stream-json or Codex events, for the provider-side cross-check."""
    sessions, messages, threads = set(), set(), set()
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("session_id"), str): sessions.add(event["session_id"])
        if isinstance(event.get("thread_id"), str): threads.add(event["thread_id"])
        message = event.get("message")
        if event.get("type") == "assistant" and isinstance(message, dict) and isinstance(message.get("id"), str):
            messages.add(message["id"])
    return {"session_ids": sorted(sessions), "message_ids": sorted(messages), "thread_ids": sorted(threads)}


# --- legacy first-review source ---------------------------------------------------------------------------------------
def launcher_summaries(orchestrator_lines):
    """The run_claude_reviewer.py summaries, in the order the orchestrator received them as tool results."""
    out = []
    for line in orchestrator_lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        for item in (event.get("message") or {}).get("content") or []:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            content = item.get("content")
            text = content if isinstance(content, str) else "\n".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict))
            for row in text.splitlines():
                try:
                    summary = json.loads(row)
                except ValueError:
                    continue
                if isinstance(summary, dict) and {"status", "invocation_id", "stream_file"} <= set(summary):
                    out.append(summary)
    return out


def legacy_first_review(orchestrator_path, tmp_dir):
    """The per-invocation result.txt of the first launcher call that exited cleanly; None when there is none."""
    for summary in launcher_summaries(Path(orchestrator_path).read_text(errors="replace").splitlines()):
        if summary["status"] != "ok" or not re.fullmatch(r"[0-9a-f]{32}", str(summary["invocation_id"])):
            continue
        found = sorted(Path(tmp_dir).glob(f"*-reviewer-{summary['invocation_id']}/result.txt"))
        if len(found) != 1:
            raise SystemExit("launcher summary without exactly one per-invocation result.txt: " + summary["invocation_id"])
        return {"invocation_id": summary["invocation_id"], "result": found[0], "stream": found[0].parent / "stream.jsonl"}
    return None


# --- normaliser -------------------------------------------------------------------------------------------------------
def finding(file, blocking, security_only, summary, scenario):
    text = " ".join(str(summary).split()) + "\nScenario: " + " ".join(str(scenario).split())
    return {"file": file.strip() if isinstance(file, str) and file.strip() else "<none>", "line": None,
            "blocking": blocking, "security_only": security_only, "text": text}


def normalise_legacy(text):
    """Reviewer-output schema text -> m7_grade arm record. A text that breaks the schema (docs/protocol/reviewer-output.md:
    verdict, Issues section, verdict/issues consistency) is an arm failure, never an empty success."""
    fail = lambda why: {"status": "failed", "reason": why, "findings": []}
    verdicts = re.findall(r"^### VERDICT: (\S+)\s*$", text, re.M) if isinstance(text, str) else []
    if len(verdicts) != 1 or verdicts[0] not in ("APPROVE", "REQUEST_CHANGES"):
        return fail("no single valid VERDICT line")
    if len(re.findall(r"^### Strengths[ \t]*$", text, re.M)) != 1:   # always required, whatever the verdict
        return fail("no single Strengths section")
    sections = re.findall(r"^### Issues[ \t]*\n(.*?)(?=^### |\Z)", text, re.M | re.S)
    if len(sections) > 1:
        return fail("more than one Issues section")
    body = [line for line in (sections[0].splitlines() if sections else []) if line.strip()]
    if sections and not body:
        return fail("empty Issues section")
    findings, current = [], None
    if body == ["- None."]:
        body = []
    for line in body:
        head = re.match(r"^- \[(CRITICAL|MINOR)\] (\S.*)$", line)
        if head:
            current = {"severity": head.group(1), "summary": re.sub(r"\s+— (must be resolved|recommended).*$", "", head.group(2)),
                       "file": "", "scenario": ""}
            findings.append(current)
        elif current is not None and line.startswith("  "):
            label = re.match(r"^\s+(File|Trigger):\s*(.*)$", line)
            if label and label.group(1) == "File":
                current["file"] = (re.search(r"`([^`]+)`", label.group(2)) or re.match(r"(\S+)", label.group(2) + " x")).group(1)
            elif label:
                current["scenario"] = label.group(2)
        else:   # prose placeholders, other severities, "- None." beside issues
            return fail("Issues line outside the schema: " + line.strip()[:80])
    critical = any(f["severity"] == "CRITICAL" for f in findings)
    if verdicts[0] == "APPROVE" and critical:
        return fail("APPROVE with a CRITICAL issue")
    if verdicts[0] == "REQUEST_CHANGES" and not critical:
        return fail("REQUEST_CHANGES without a CRITICAL issue")
    return {"status": "ok", "findings": [finding(f["file"], f["severity"] == "CRITICAL", False, f["summary"], f["scenario"])
                                         for f in findings]}


def normalise_paired(answer):
    """The first EXEC review receipt's answer -> m7_grade arm record."""
    rows = answer.get("full_review") if isinstance(answer, dict) else None
    if not isinstance(rows, list):
        return {"status": "failed", "reason": "no full_review list", "findings": []}
    out = []
    for row in rows:
        severity, security = str(row.get("severity", "")).upper(), row.get("security") is True
        blocking = severity in ("CRITICAL", "MAJOR")
        out.append(finding(row.get("file"), blocking, not blocking and (severity == "SECURITY" or security),
                           row.get("summary", ""), row.get("failure_scenario", "")))
    return {"status": "ok", "findings": out}


# --- assembling grade inputs ------------------------------------------------------------------------------------------
def findings_document(key, receipts, arms, frozen_manifest_bytes, corpus_root, window, voided=None):
    """m7_grade FINDINGS from authentic arm receipts only (payload.findings_record per case and arm). Each receipt must
    belong to this frozen manifest, this window and its case's frozen base and diff, and every artifact it signed must
    still have its signed bytes under corpus_root/<case>."""
    manifest_sha, frozen = sha(frozen_manifest_bytes), {c["id"]: c for c in json.loads(frozen_manifest_bytes)["cases"]}
    doc = {"arms": {arm: {} for arm in arms}, "voided": dict(voided or {})}
    for receipt in receipts:
        payload = verify(key, receipt)
        if payload.get("kind") != "m7-arm-receipt" or payload.get("arm") not in doc["arms"]:
            raise SystemExit("not an arm receipt of a configured arm")
        case = frozen.get(payload.get("case"))
        if (case is None or payload.get("manifest_sha256") != manifest_sha or payload.get("window") != window
                or payload.get("base") != case["base"] or payload.get("diff_sha256") != case["diff_sha256"]):
            raise SystemExit("receipt bound to another manifest, window, case, base or diff: " + str(payload.get("case")))
        verify(key, receipt, Path(corpus_root) / payload["case"])
        if payload["case"] in doc["arms"][payload["arm"]]:
            raise SystemExit("two receipts for one case and arm: " + payload["case"])
        if payload["scan"]["status"] != "CLEAN" and payload["case"] not in doc["voided"]:
            raise SystemExit("case " + payload["case"] + ": D-b1 scan not CLEAN; void it (both arms) with its evidence")
        doc["arms"][payload["arm"]][payload["case"]] = payload["findings_record"]
    return doc


VOTE_FIELDS = ("verdict", "seed", "category", "mechanism_match", "rationale", "code_evidence", "duplicate_of")


def adjudication(key, grader_receipts, blind, binding, exclusions=None):
    """m7_grade ADJUDICATION from authentic grader records. blind maps the label a grader saw to the real arm;
    binding = {"manifest_sha256", "keys_sha256", "result_sha256", "findings_sha256"} (m7_grade.check_adjudication);
    exclusions = {case: m7_scan.exclusion_evidence(...)} for every voided case."""
    votes, guesses = {}, {}
    for receipt in grader_receipts:
        record = verify(key, receipt)
        if record.get("kind") != "m7-grader-record" or record.get("pass") not in ("initial", "third"):
            raise SystemExit("not a grader record with pass initial or third")
        if record.get("manifest_sha256") != binding.get("manifest_sha256") or record.get("findings_sha256") != binding.get("findings_sha256"):
            raise SystemExit("grader record graded another manifest or findings document")
        arm = blind.get(record.get("arm_label"))
        if arm is None:
            raise SystemExit("grader record for an unknown arm label")
        who = {k: record[k] for k in ("grader", "vendor", "model")}
        for fid, vote in sorted(record.get("votes", {}).items()):
            row = {**who, **{k: vote[k] for k in VOTE_FIELDS if k in vote}}
            votes.setdefault(arm, {}).setdefault(record["case"], {}).setdefault(fid, []).append((record["pass"] == "third", row))
        guesses.setdefault(record["case"], []).append({"grader": record["grader"], "arm_label": record["arm_label"],
                                                       "guess": record.get("arm_guess"), "actual": arm})
    arms = {}
    for arm, cases in votes.items():
        for case, fids in cases.items():
            for fid, rows in fids.items():   # two initial votes, then at most one third-pass vote, in that order
                initial, third = [r for t, r in rows if not t], [r for t, r in rows if t]
                if len(initial) != 2 or len(third) > 1:
                    raise SystemExit(f"{arm}/{case}/{fid[:12]}: needs exactly two initial votes and at most one third pass")
                arms.setdefault(arm, {}).setdefault(case, {})[fid] = initial + third
    return {"binding": dict(binding), "arms": arms, "arm_guesses": guesses, "exclusion_evidence": dict(exclusions or {})}


def arm_receipt(key, frozen_case, manifest_sha256, arm, window, root, artifacts, findings_record, scan_record):
    """Sign what the harness collected for one case, arm and window. artifacts: {name: path under root}; the streams
    among them (*.jsonl) give the provider ids. A case whose D-b1 scan is not CLEAN keeps its receipt but is voided."""
    root = Path(root).resolve()
    scanned = {}
    for art in (scan_record.get("artifacts") or {}).values():   # the scan must cover exactly the bytes signed now
        path = Path(art["path"]).resolve()
        if sha(path.read_bytes()) != art["sha256"]:
            raise SystemExit("artifact changed since the D-b1 scan: " + str(path))
        scanned[path] = art["sha256"]
    rels, ids = {}, {"session_ids": [], "message_ids": [], "thread_ids": []}
    for name, path in sorted(artifacts.items()):
        rel = str(Path(path).resolve().relative_to(root))
        rels[rel] = sha(Path(path).read_bytes())
        if rel.endswith(".jsonl") and scanned.get(Path(path).resolve()) != rels[rel]:
            raise SystemExit("a signed stream was not D-b1 scanned: " + rel)
        if rel.endswith(".jsonl"):
            found = provider_ids(Path(path).read_text(errors="replace").splitlines())
            ids = {k: sorted(set(ids[k]) | set(found[k])) for k in ids}
    if scan_record.get("case") != frozen_case["id"] or scan_record.get("arm") != arm or \
            scan_record.get("base") != frozen_case["base"] or scan_record.get("diff_sha256") != frozen_case["diff_sha256"]:
        raise SystemExit("scan record is not this case and arm")
    return sign(key, {"kind": "m7-arm-receipt", "version": 1, "case": frozen_case["id"], "arm": arm, "window": window,
                      "manifest_sha256": manifest_sha256, "base": frozen_case["base"], "diff_sha256": frozen_case["diff_sha256"],
                      "artifacts": rels, "provider_ids": ids,
                      "scan": {"status": scan_record["status"], "sha256": digest(scan_record)},
                      "findings_record": findings_record})


if __name__ == "__main__":
    if sys.argv[1:2] == ["keygen"]:
        print(keygen(sys.argv[2]))
    elif sys.argv[1:2] == ["verify"]:
        payload = verify(load_secret(sys.argv[2]), json.loads(Path(sys.argv[3]).read_text()), sys.argv[4])
        print(json.dumps({"ok": True, "kind": payload.get("kind"), "case": payload.get("case"), "arm": payload.get("arm")}))
    else:
        raise SystemExit(__doc__)
