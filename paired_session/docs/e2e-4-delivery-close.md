# E2E lifecycle design 4/4: DELIVERY and CLOSE

Design only; lifecycle disabled. Inputs: [1](e2e-1-stages-and-roles.md), [2a](e2e-2a-tree-binding.md), [2b-i](e2e-2b1-budgets.md), [2b-ii](e2e-2b2-findings-replay.md), [3](e2e-3-docs-security.md); legacy `execution.md` §Delivery gate/§Step 4, `session-file.md` §`completed_stages`, Compass `skills/close/SKILL.md` §Mutate; R12-4 R1 M7/M8, R2 M7/M11.

## Acceptance is a current-tree operator act

P's current EXEC/gate→SECURITY success becomes internal `P_PASSED`, **not DONE**. Reserve Q slots, run Q closeout checks, then Q SECURITY PASS with zero blockers becomes `DONE`: pending operator acceptance. Copy accepted Q OID from that receipt, never rehash as authority. Before accept compare OID/manifest, config, HEAD and live index/tree; drift HOLDs for replay or abort. Reject preflight reserves both P' and Q' (R12-4 R1 M7; doc 2a/2b-i).

`accept`, `reject`, ignore consent, budget extension and external delivery require one-use human signatures over frozen action/run/item, parent/ref, P/Q/C1/C2 SHAs, commit author/message/time, receipts and nonce. A separate privileged coordinator service owns verification code and an owner-installed, root-owned or hardware-backed trust anchor; its UI shows the digest and requires human presence. No same-UID project/global config, TTY, stdout or agent tool may register a key or enable external delivery. Missing service/anchor refuses lifecycle activation. Record signature/UID/time; reject replays P/Q, repeat accept is idempotent. Same-UID shells can still use bare Git: this guards lifecycle receipts/CLOSED claims, not repository access (R12-4 R2 M11).

## Reviewed commit bundle and Compass closeout

1. Resolve one open item from a fresh Compass backlog view. Require BACKLOG in this repo root and reserve it from all writer grants; P must retain its frozen blob. Freeze exact title/section/blob and item ID; dirty/stale/ambiguous state HOLDs before dispatch. Freeze the Compass close adapter/skill hash too. Never merge unrelated text.
2. Build **P** via doc 2a. At run start freeze effective hook paths/hashes/interpreters (`core.hooksPath`, `.git/hooks`, wrappers); unknown/change HOLDs. Run pre-commit/message hooks before final review/tests/SECURITY in disposable checkout/GIT_DIR/index with scratch HOME/cache; network, credentials, run dir and live tree denied. Adopt only authorized tracked candidate diff. Failure HOLDs; mutation replays gates and counts as a local check. Active post-commit/unknown ref hooks refuse lifecycle pending owner adapter; pre-push is external only. Hook-free `commit-tree` uses frozen parent/message/time/P to create **C1** without moving HEAD.
3. Coordinator applies deterministic Compass exact-match/Done/date/trim mutation to frozen BACKLOG in isolated index, citing C1; it never calls a model or edits live BACKLOG. **Q** has BACKLOG-only diff and opens a new EXEC convergence outside docs allowlist. Q has **no writer grant**: reviewer/gate and final review/tests/SECURITY run on Q; FINISH/POLISH-Q/DOCS sign current-Q policy no-ops tied to P receipts and BACKLOG-only proof. This refines doc 1 for Q metadata only. Q hook runs without candidate grant. Code findings restart a new P epoch; BACKLOG/message/hook findings HOLD for abort or owner-fixed adapter/new run, not an identical replay. Reserve Q budgets before P_PASSED. After Q checks create **C2** (parent C1/tree Q) without ref update; signed operator accepts `(parent,ref,P,C1,Q,C2,item,receipts)` and Q SECURITY OID.
4. Verify C1/C2 objects, parents, trees, messages, authors and signatures against intent. Under workspace lease/index lock, recheck frozen symbolic ref (refuse detached/switch), parent HEAD, globally clean live tree/index, paths, BACKLOG blob and OIDs. One CAS `update-ref ref C2 parent` publishes both commits atomically. Mismatch/HOLD permits abort before CAS or owner repair/new run; never auto-rebase. After CAS journal/reconcile only manifest paths and BACKLOG in live index/tree per doc 2a; preserve user data (R12-4 R2 M7).
5. CLOSE receipt binds C1/C2 SHAs, P/Q OIDs, exact item/Compass mutation, acceptance and reconciliation. Mark `CLOSED` only when HEAD=C2, receipts/trees verify and BACKLOG Done matches HEAD/live index. Resume reconstructs only from intent and exact commit chain; otherwise HOLD, no second commit/model call. After-CAS crash resumes reconciliation; reject/abort then refuse. If HEAD advanced, HOLD for signed owner recovery, never overwrite. No unreviewed BACKLOG edit follows. Run-dir report carries SHAs and Chinese summary with post-SECURITY facts (R12-4 R1 M8; legacy Step 4).

Legacy `auto_commit=false` remains. Lifecycle switch defaults off and also needs a signed enablement record from the privileged service; agent-writable config alone cannot enable it. Accept authorizes local C1/C2. Merge/push/cleanup require frozen `external_delivery=true`, signed enablement and a separate post-CLOSE human signature bound to exact remote URL/refspec/C2; recheck remote before action. No auto-push or CLOSE rollback (doc 1; legacy Step 4).

## Gate and migration checks before any commit

Gate checks current-OID EXEC/shadow/gate (or controlled SKIP), FINISH/POLISH-Q/DOCS/final/tests, SECURITY, no blockers, signed accept and both intents; Q uses only the narrow metadata no-op receipts above. Missing/stale evidence or budget HOLDs for repair/review, bounded extension or abort. Lifecycle refuses `--adversarial-gate off` and `resume --polish`; legacy DONE/ACCEPTED needs current EXEC/gate proof or reopens EXEC, then all new stages, SECURITY and new accept (legacy §Delivery gate; doc 1).

## M4 fake-CLI acceptance matrix

Fake CLI: `test_plan_to_compass_close_two_commits` checks both trees/commits/index/BACKLOG; `test_post_security_mutation_refuses_accept` checks drift; `test_hook_sandbox_and_mutation` checks escaped/mutating hooks; `test_q_no_writer_liveness` checks no-op/REVISE; `test_dirty_backlog_closeout_refuses` checks user text; `test_cas_crash_resume_reconciles_once` checks ref/crash/duplicate accept; `test_agent_cannot_accept_or_confirm` checks top agents/providers and replaced/self-registered signer key; `test_reject_reopens_exec_and_gate` checks replay; `test_lifecycle_off_gate_legacy_done_polish_refused` checks migration; `test_external_delivery_off` checks switch/signature/remote binding.

## Implementation batches

Lifecycle stays disabled until all batches pass fake-CLI checks and independent review. Estimates are `coordinator.py` changed lines, each ≤60; split again before review if exceeded.

| # | M4 batch (estimate) | Explicit question |
|---|---|---|
| 1 | Config freeze, old-state refusal (45) | Can legacy DONE/off-gate enter lifecycle? |
| 2 | Item UUID, successor blockers (50) | Can a blocker vanish across a run? |
| 3 | Candidate root/index/OID (60) | Can user work enter the candidate? |
| 4 | Hook inventory and OS sandbox (50) | Can a hook escape or mutate unseen? |
| 5 | Frozen role dispatch/receipts (55) | Can writers forge evidence or reviewers read peers? |
| 6 | FINISH and EXEC/gate replay (50) | Can code writes retain stale approval? |
| 7 | POLISH-Q specialist owners (55) | Can an open specialist blocker pass? |
| 8 | Caps and signed budget extension (55) | Can a role exceed a cap silently? |
| 9 | DOCS grants/final review/tests (55) | Can a docs write bypass retest? |
| 10 | SECURITY manifest preflight (55) | Can a sensitive delivery path pass? |
| 11 | Ignore consent/security repair (50) | Can a repair skip EXEC or operator consent? |
| 12 | Signed accept/reject, OID intent (55) | Can an agent accept or bind a new OID? |
| 13 | Q closeout no-op and fresh checks (55) | Can BACKLOG close from an unreviewed Q? |
| 14 | C1/C2 objects, CAS and index (60) | Can crash/ref drift create a wrong commit? |
| 15 | CLOSE receipt/external control, E2E matrix (55) | Can close/push occur without exact consent? |
