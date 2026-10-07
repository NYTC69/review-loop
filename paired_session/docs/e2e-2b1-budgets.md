# E2E lifecycle design 2b-i: caps and operator liveness

> **Historical** (fake-only candidate-tree lifecycle design, M3/M4). The real lifecycle is W, [doc 6](e2e-6-worktree-lifecycle.md), the default since v2.10.0.

Status: design only; lifecycle disabled. Consume [doc 1](e2e-1-stages-and-roles.md) and [doc 2a](e2e-2a-tree-binding.md) as accepted. Sources: `docs/protocol/execution.md` §Step 3.4, §Step 3.5.4; archived `r12-4-findings.md` R1 M4/R2 M3 and `r17-3-r1/r2/r3-review.txt` in `.compass/results/2026-09-23_self-audit-roadmap/`. Finding ownership/blind replay belong to doc 2b-ii.

## Counting and fixed caps

Before dispatch, reserve both one provider and one launch slot; after outcome release the unused slot. Started/uncertain calls spend role/run/item slots even on failure. Proven zero-model-start refusal spends launch only; unproven spends provider. Exhaustion HOLDs **before** dispatch, never after count exceeds cap. Known 429/auth windows wait for reset or abort. Local checks use local slots; stale OID/epoch receipts cannot refund/advance (doc 2a).

Lifecycle-only defaults/hard ceilings leave legacy defaults intact. An epoch is EXEC convergence plus downstream stages; invalidating writes/rejects start another. Per-epoch counters reset only then; run/item counters never reset on resume. Global caps do not guarantee completion.

| Counter | Default / hard | Scope and unit |
|---|---:|---|
| PLAN | 3 / 5 | author+reviewer rounds/run; 2 calls/round |
| EXEC; shadow | 6 / 8 | rounds/convergence; author+reviewer+shadow (if on) each round; reserve one for post-gate repair |
| Step 3.4 gate | 2 / 4 | started attempts/convergence for one valid rubric verdict |
| FINISH | 4 / 6 | finisher calls/epoch, including docs-prefix refresh |
| POLISH-Q | 32 / 50 | provider calls/epoch; each selected language/code/silent-failure specialist 4 / 6, analyzer 4 / 6, test writer 2 / 4 |
| Simplifier | 2 / 4 | started attempts/item, including zero-tool retry; at most one successful pass/item |
| DOCS; final review | 7 / 10; 2 / 4 | calls/epoch, including fix, consistency recheck and zero-tool retry |
| SECURITY | 3 / 5 | review/fixer/retry calls/epoch; repair replays EXEC |
| Local checks | 16 / 24 | authoritative test/lint/build executions/epoch; same-OID reuse only per doc 2a |
| Run; item | 192 / 224; 384 / 448 | started provider calls; at most two runs/item |
| Epoch; replay; reject | 15 / 17; 12 / 14; 2 / 2 | epochs, downstream replays and rejects/run; 1 + 12 + 2 = 15 |
| Pre-model launches | 4 / 8 | proven zero-model-start refusals/run; item hard cap 16 |

Owner-registered immutable item UUID has a coordinator-owned ledger outside workspace/run roots. Abort/restart and one 2C successor share it; copy/rename/worktree change cannot mint another. Missing or mismatched ledger HOLDs for owner reconciliation. Successor consumes run 2, not run 3; approvals do not transfer. Only coordinator changes counters/receipts.

**Any exhausted cap gives a named HOLD before dispatch/write, never SKIP or APPROVE.** Pre-gate EXEC uses at most 5 default/7 hard rounds, reserving one round for gate repair; shadow follows it. Gate attempts without payload spend attempts; adapter-accepted payloads, including incomplete rubrics sent to the normal reviewer for revalidation, spend the single verdict. Any HOLD permits abort; tests/blockers resume after repair, ledger mismatch needs owner reconciliation, and cap HOLD permits extension. Liveness means a legal operator action, not automatic DONE.

From budget HOLD: `resume --extend-budget <stage> --by <n>`, abort, or eligible 2C successor. A leased extension records operator/time/item/stage/old-new caps/command hash. `n` is 1–2 for stage/round/epoch/replay/launches, 1–16 for `run-calls`; that command atomically raises run+item by `n`. Hard ceilings never move; per item allow ≤8 commands, ≤64 extra calls and ≤4 extra epochs. Raising a stage does not raise global caps. In DONE, extension is control-only, preserves DONE and dispatches no provider. At hard cap, run 2 or scope limit, abort remains legal; no automatic new item or in-flight timeout change.

Before a writer, require writer, fresh owning-role review, local-check and—on EXEC invalidation—replay/epoch plus next-epoch EXEC/shadow/gate slots from run/item budgets. Later shortage HOLDs. Before DONE `reject`, reserve a configured clean route (EXEC/shadow/gate repair, FINISH, specialists/analyzer, simplifier or policy SKIP, DOCS writer/consistency/review/final, SECURITY, reserved-DOCS refresh, tests), plus reject/epoch/run/item headroom. Shortage, including reject 3/2, refuses reject without changing DONE; accept/control-only extension/abort remain. At lifecycle/successor start check remaining item/epoch slots and simplifier marker **before PLAN**. No cap waives a gate, final review or test.

Simplifier item state is `successful`, `rolled-back` or `exhausted`. After success **or rollback**, coordinator signs current-OID policy `SKIP(state, source-receipt-id)` at every POLISH-Q evaluation, including same-epoch DOCS prefix refresh and later epochs/runs/successors; it is not doc 2a EXEC/gate closure proof and never replaces fresh review/gate/tests. On build failure, restore old OID but keep the new epoch: rerun EXEC review/gate on that OID, never reuse old-epoch receipts, then report rollback and sign SKIP. No usable disposition (zero-tool, timeout, malformed) retries only within POLISH-Q while attempts remain; after all attempts, `exhausted` HOLDs. That marker blocks restart/successor before PLAN until bounded extension; scope change is ineligible and refused without consuming run 2. At hard exhaustion abort remains legal. Legacy §3.5.4 revert/report stays visible.

## Review finding resolution and fake-CLI acceptance

- R17-3 R1 M1: success/rollback sign current-OID policy SKIP. M2: specialist 4 calls cover initial, two refreshes and zero-tool retry. M3: writer reserves immediate closure. M4: HOLD has extension/abort. M5: EXEC reserves one post-gate repair round.
- R17-3 R2 M1: shadow follows EXEC. M2: zero-tool spends attempt; exhaustion HOLDs. M3: reject shortage preserves DONE. M4: external item UUID spans copies/successors. M5: startup route includes shadow, gate repair, docs refresh, checks.
- R17-3 R3 M1: exhausted marker blocks run 2 before PLAN. R12-4 R1 M4: replay/item counters are monotone with HOLD. R12-4 R2 M3: epoch + item caps count reject/replay; stale receipts need doc 2a proof.
- Fake CLI: `test_cap_hold`, `test_bounded_extension`, `test_gate_repair_shadow`, `test_reject_keeps_done`, `test_simplifier_noop_docs_refresh`, `test_simplifier_rollback_new_epoch`, `test_simplifier_rollback_successor`, `test_simplifier_success_after_reject`, `test_simplifier_exhausted_successor`, `test_pre_model_refusal_cap`.

Doc 2b-ii owns finding disposition and blind replay; docs 3/4 own content, DELIVERY and CLOSE. None is enabled here.
