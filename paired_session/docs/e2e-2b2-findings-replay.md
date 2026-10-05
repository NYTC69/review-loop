# E2E lifecycle design 2b-ii: finding ownership and replay

Design only; lifecycle disabled. Consume [doc 1](e2e-1-stages-and-roles.md), [doc 2a](e2e-2a-tree-binding.md), [doc 2b-i](e2e-2b1-budgets.md). Sources: archived `r16-2-r1/r2/r3-review.txt`, `r12-4-findings.md` and legacy `docs/protocol/execution.md` §§3.4–3.7. Old findings/receipts never become current-tree approvals by silence.

## Owner identity and ledger

Only coordinator writes ledger, receipt status and severity. Each finding stores immutable ID/text/severity/security flag, source receipt/OID/epoch and owner `(role kind, frozen vendor, exact model, agent-body/rubric hash)`. Role models are operator-configured (ADR-9; the ADR-8 values `gpt-6-luna` for Codex and `claude-opus-5-5` for Claude remain the defaults); dispatch uses frozen `codex exec` or `claude -p`, never the development executor model. Model/body changes need recorded remapping and fresh review.

| Source | Who may dispose | Evidence required |
|---|---|---|
| Persistent EXEC reviewer | Same frozen reviewer identity | Fresh current-OID review of its own finding text + ID; author claim or silence is insufficient |
| Fresh shadow | Same shadow role identity in a new fresh call | Blind initial scan, then own-finding reconciliation; blocking shadow CRITICAL/MAJOR/security cannot be ignored |
| Gate | Normal EXEC reviewer after explicit transfer | Complete six-field rubric; incomplete gate finding is `awaiting-revalidation`, still blocking until normal reviewer re-asserts with rubric or explicitly drops it |
| POLISH-Q specialist, docs or security reviewer | Same role-kind identity at its stage | Fresh current-OID re-review of only its own open texts/IDs; `security=true` remains blocking at any nominal severity |
| Coordinator test/lint/build/preflight check | Coordinator observation, no model owner | Matching current-OID check succeeds; no reviewer prose can declare it fixed |

Only that owner may close/downgrade/drop an AI finding. Gate transfer retains source/severity; normal-reviewer rubric revalidation spends its own round. Operator resolution may reassign owner with provenance and mandatory fresh review, or abort/eligible scope-change; it cannot close/downgrade/hide. Finisher fixes code, never ledger (R12-4 R2 M4).

## Epochs, downstream order and fresh blindness

Open findings live in the item ledger, not source receipts. EXEC replay, reject and resume preserve them. A stale receipt stays historical. Every later call to a role with open findings is re-review even in a new epoch; it sees its own original texts/IDs and cannot be skipped by a selector. Other roles see no such history. Current-OID APPROVE without explicit dispositions leaves findings OPEN.

At FINISH entry, doc 1’s “no open blocker” means no **upstream** EXEC/shadow/transferred-gate blocker; downstream owners cannot run yet. Each stage forces its owner re-review and cannot exit with its own blocker open. DELIVERY requires zero item-wide blockers plus current-tree final review/tests/SECURITY. Downstream blockers survive EXEC replay until their source stage; budget exhaustion gives named HOLD with 2b-i extension or abort, never auto-close (R16-2 R3 M2).

Reserved-DOCS write changes OID, not EXEC convergence. Doc 2a scoped proof retains only EXEC/gate claims; old full-tree FINISH/POLISH-Q/docs/final/test/SECURITY receipts expire. Rerun FINISH/POLISH-Q owners, then docs/final/tests/SECURITY on current OID within 2b-i caps. Simplifier gets its separate current-OID policy SKIP. Missing slots HOLD; path similarity never revives a stale receipt (R16-2 R2 M2).

## Successor and resume contract

Scope-change ABORT supersedes the run, not its open findings. Before successor role dispatch, carry every CRITICAL/MAJOR/SECURITY or `security=true` blocker in the same item ledger with ID/text/severity/source and successor mapping. Even if new scope removes affected code, only mapped owner can confirm on current OID. Same identity first performs a blind pass, then separate own-finding reconciliation. Changed/unmapped identity HOLDs for recorded operator reassignment and fresh review. PLAN/EXEC approval never erases blockers; DELIVERY waits. A run/mapping cap leaves abort legal (R16-2 R2 M1).

This E2E-only handoff strengthens `scope-change.md`'s current “reported/new ledger” rule. Lifecycle activation refuses until successor creation persists the shared item UUID and open blockers before role dispatch; today's 2C command is unchanged.

Pending request/receipt binds item/run, stage, role identity, request ID, epoch/convergence, input/output OID, config/model/body hash, tool trace and output hash. Persist pending ID before spawn. Resume consumes one matching complete receipt once; missing/duplicate/stale/wrong-owner/different-OID evidence HOLDs or starts a fresh authorized owner call within budget. Uncertain child recovery stays fail-closed. Only doc 2a scoped proof can retain an EXEC/gate claim; it never replays a full-tree approval (R16-2 R1 M3).

Fresh shadow/gate/POLISH-Q/docs/security see approved task/plan, current candidate and only their own prior texts when disposing them. Coordinator inlines frozen `agents/*.md`; OS read-only sandbox denies candidate/run artifacts. Snapshots, tool traces and materialized inputs prove no write or peer-evidence read; violation discards verdict and HOLDs. Prompt-only blindness is insufficient (R12-4 R1 M6/S4).

## Review crosswalk and fake-CLI tests

- R16-2 R1 M1: incomplete gate rubric transfers to normal reviewer as blocking awaiting-revalidation. R1 M3: open findings outlive epochs; stale receipts do not. R1 M4: frozen ADR-7 vendor/model/body identity chooses `codex exec` or `claude -p`.
- R16-2 R2 M1: successor carries blockers and requires mapped owner disposition. R2 M2: reserved DOCS invalidates full-tree prefix receipts; only scoped EXEC/gate proof survives. R16-2 R3 M2: FINISH checks upstream blockers; downstream owner re-review closes later, DELIVERY checks all.
- R12-4 R2 M4: only role owner disposes findings. R12-4 R1 M6/S4: OS read-only boundary plus observed input/tool evidence enforces fresh-role blindness.
- Fake CLI: `test_gate_incomplete_rubric_revalidated_by_exec_owner`, `test_open_finding_survives_exec_replay`, `test_successor_security_blocker_needs_owner`, `test_docs_prefix_replays_current_oid_roles`, `test_downstream_blocker_waits_for_source_stage`, `test_wrong_role_or_stale_receipt_cannot_close`, `test_fresh_reviewer_cannot_read_peer_evidence`.

Docs 3/4 own DOCS/SECURITY content and DELIVERY/CLOSE mechanics. This design does not enable lifecycle routing.
