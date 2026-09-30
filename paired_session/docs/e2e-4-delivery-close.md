# E2E lifecycle design 4/4: DELIVERY and CLOSE

Design only; lifecycle disabled. Inputs: [docs 1–3](e2e-1-stages-and-roles.md); legacy `execution.md` Delivery/Step 4, `session-file.md` `completed_stages`, Compass close `Mutate`; R12-4 R1 M7/M8, R2 M7/M11.

## Acceptance is a current-tree operator act

P EXEC/gate→SECURITY pass is `P_PASSED`, not DONE. Reserve Q; Q SECURITY PASS with zero blockers yields DONE/pending acceptance. Accept uses receipt Q OID and checks OID/manifest/config/HEAD/index; drift HOLDs. Lifecycle accept is allowed only from this DONE state, never the legacy rejection-limit HOLD. Reject reserves P'/Q' (R12-4 R1 M7; doc 2a/2b-i).

D8 legacy parity: top-level driver is operator delegate; sandbox roles cannot invoke operator actions. accept/reject/extend/close/delivery are plain CLI with UID/time/action/run/item/digest/HEAD/index/intent/result provenance; mutations require `--expect <digest>` and matching workspace/workitem. Writer allowlist: authorized candidate paths plus author-tmp only. Coordinator-only/denied roots include run/state/ledger/receipts, candidate GIT_DIR/index, live workspace/.git/BACKLOG, user/global config, shell/LaunchAgents and credentials. Candidate and author-tmp (including test/lint/hook scratch, provider HOME/TMP/session/cache) sit on a dedicated filesystem separate from protected roots/credentials; otherwise HOLD. No hardlinks across roots. After each call terminate every process under sandbox identity; verify none remain before ingest/review/cleanup or HOLD. Clean author-tmp fd-relative/no-follow. Lease serializes coordinator commands only. No signatures/service/trust anchor. Accepted D8 residual: any sandbox role with network/credentials may push remotely out-of-band or induce the driver to reject/extend/ignore-consent (R12-4 R2 M11).

## Reviewed commit bundle and Compass closeout

1. Resolve one open item from a fresh Compass view. BACKLOG must be repo-root and outside writer grants; P retains its frozen blob. Freeze title/section/blob/item ID and close-adapter hash. Dirty/stale/ambiguous state HOLDs; never merge unrelated text.
2. Build **P** via doc 2a; freeze hook inventory/config hashes and HOLD on change. Only sandboxed batch-4 runner executes inventoried pre-commit/message hooks; adopt authorized changes. Coordinator Git plumbing disables implicit hooks/fsmonitor/signing; post-commit/unknown ref hooks refuse lifecycle, pre-push is external. Hook-free `commit-tree` creates **C1** from frozen parent/message/time without moving HEAD.
3. Coordinator applies exact-match/Done/date/trim to frozen BACKLOG in isolated index citing C1; no model/live edit. **Q** is BACKLOG-only and opens new EXEC convergence, with no writer grant; reviewers/gate/final/tests/SECURITY run on Q. FINISH/POLISH-Q/DOCS bind no-op receipts to P; Q hooks lack candidate grant. Code findings restart P; other findings HOLD for abort/owner repair. Reserve Q budget before P passes. Create **C2** (parent C1/tree Q), no ref move; CLI accept provenance binds `(parent,ref,P,C1,Q,C2,item,receipts)` and Q SECURITY OID.
4. Verify C1/C2 objects, parents, trees, messages and authors against intent. Hash-verify imported objects (`index-pack --strict` or fsck C2-reachable objects). Under workspace lease/index lock, recheck hook inventory/`.git/config` hashes, ref, parent HEAD, live tree/index, paths and BACKLOG blob/OIDs immediately before each object/ref write; drift HOLDs. Pin hooksPath/fsmonitor off and `--no-gpg-sign`; only explicit runner executes hooks. One CAS publishes both commits atomically. Mismatch/HOLD permits abort before CAS or owner repair/new run; never auto-rebase. After CAS journal/reconcile only manifest paths and BACKLOG in live index/tree per doc 2a; preserve user data (R12-4 R2 M7).
5. CLOSE receipt binds C1/C2 SHAs, P/Q OIDs, exact item/Compass mutation, CLI acceptance provenance and reconciliation. Mark `CLOSED` only when HEAD=C2, receipts/trees verify and BACKLOG Done matches HEAD/live index. Resume reconstructs only from intent and exact commit chain; otherwise HOLD, no second commit/model call. After-CAS crash resumes reconciliation; reject/abort then refuse. If HEAD advanced, HOLD for an explicit operator CLI recovery, never overwrite. No unreviewed BACKLOG edit follows. Run-dir report carries SHAs and Chinese summary with post-SECURITY facts (R12-4 R1 M8; legacy Step 4).

Legacy `auto_commit=false` remains. Lifecycle switch defaults off; `external_delivery` comes only from operator-owned config outside writer roots, is frozen at start, and drift HOLDs. CLI accept authorizes local C1/C2. Merge/push/cleanup require frozen `external_delivery=true`, CLI provenance bound to remote URL/refspec/C2 and remote recheck. Merge is `--ff-only`; push is non-force and only advances to exact C2. Any remote-ahead/diverged state HOLDs. No auto-push or CLOSE rollback (doc 1; legacy Step 4).

## Gate and migration checks before any commit

Gate checks current-OID EXEC/shadow/gate (or controlled SKIP), FINISH/POLISH-Q/DOCS/final/tests, SECURITY, no blockers, CLI acceptance provenance and both intents; Q uses only the narrow metadata no-op receipts above. Missing/stale evidence or budget HOLDs for repair/review, bounded extension or abort. Lifecycle refuses `--adversarial-gate off` and `resume --polish`; legacy DONE/ACCEPTED needs current EXEC/gate proof or reopens EXEC, then all new stages, SECURITY and new accept (legacy §Delivery gate; doc 1).

## M4 fake-CLI acceptance matrix

Fake CLI: `test_plan_to_compass_close_two_commits` trees/index/BACKLOG; `test_post_security_mutation_refuses_accept` drift; `test_hook_sandbox_and_mutation` escape; `test_q_no_writer_liveness`; `test_dirty_backlog_closeout_refuses`; `test_cas_crash_resume_reconciles_once`; `test_sandboxed_roles_cannot_operator_actions` refuses action/root retargeting; `test_accept_reject_extend_provenance` binds UID/digest; `test_reject_reopens_exec_and_gate`; `test_lifecycle_off_gate_legacy_done_polish_refused`; `test_external_delivery_off` switch/provenance/remote. Activation requires live M6 OS-denial checks per provider, loopback/local-IPC/launcher denial, credential/socket-env scrubbing and no surviving writer descendants; fake CLI cannot prove these.

## Implementation batches

Lifecycle stays disabled until all batches pass fake-CLI checks and independent review. Estimates are `coordinator.py` changed lines, each ≤60; split again before review if exceeded.

| # | M4 batch (estimate) | Explicit question |
|---|---|---|
| 1 | Config freeze, old-state refusal (45) | Can legacy DONE/off-gate enter lifecycle? |
| 2 | Item UUID, successor blockers (50) | Can a blocker vanish across a run? |
| 3 | Candidate root/index/OID (60) | Can user work enter the candidate? |
| 4 | Hook inventory and OS sandbox (50) | Can a hook escape or mutate unseen? |
| 5 | Frozen role dispatch/receipts/TMP isolation (55) | Can a writer retarget state or hardlink coordinator data through TMP, forge receipts or read peers? |
| 6 | FINISH and EXEC/gate replay (50) | Can code writes retain stale approval? |
| 7 | POLISH-Q specialist owners (55) | Can an open specialist blocker pass? |
| 8 | Caps and CLI budget extension (45) | Can a role bypass a cap, or can a CLI extension lack provenance? |
| 9 | DOCS grants/final review/tests (55) | Can a docs write bypass retest? |
| 10 | SECURITY manifest preflight (55) | Can a sensitive delivery path pass? |
| 11 | Ignore consent/security repair (50) | Can a repair skip EXEC or operator consent? |
| 12 | CLI accept/reject provenance, OID intent (50) | Can acceptance bypass digest/HEAD/index/intent checks or be run by a sandboxed role? |
| 13 | Q closeout no-op and fresh checks (55) | Can BACKLOG close from an unreviewed Q? |
| 14 | C1/C2 objects, CAS and index (60) | Can crash/ref drift create a wrong commit? |
| 15 | CLOSE receipt/external switch, E2E matrix (50) | Can close/delivery bypass CLI provenance, accepted OIDs or the default-off switch? |
