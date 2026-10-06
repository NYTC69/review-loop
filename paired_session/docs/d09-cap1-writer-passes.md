# D09 capability 1: the simplifier and test-consolidation writer passes (design)

Status: design only. Owner decision D09 (2026-10-05): code-quality-loop retires onto `run --review-only` (Q6 option A)
and **capability 1 is kept**: the simplifier and test-consolidation writers are ported into paired-session. This also
answers owner decision 4 of the migration map for the main pipeline. Capability 3 (comment/type analyzers) goes to
D-LG2; capabilities 2, 4, 5 and 6 are dropped.

Sources: [q6-code-quality-loop.md](q6-code-quality-loop.md) §3–5; [e2e-1](e2e-1-stages-and-roles.md) (POLISH-Q exit
names the simplifier and test writer; writer invalidation); [e2e-2b1](e2e-2b1-budgets.md) (the reserved `simplifier`
and `test-writer` caps and the simplifier item marker); `agents/code-simplifier.md`; legacy `docs/protocol/execution.md`
§3.5.4–3.5.5 and `skills/code-quality-loop/SKILL.md` Steps 2–3. Code: `C` = `coordinator.py`, `WL` =
`worktree_lifecycle.py`, `BP` = `budget_policy.py`, `RG` = `readonly_guard.py`.

## 1. Where the passes run

- **One place: inside POLISH-Q of the worktree lifecycle W**, after the report-only specialists finish with no open
  blocker and before DOCS. W is the route of both the main pipeline and the review-only entry (`run --review-only` goes
  EXEC → FINISH → POLISH-Q → DOCS → SECURITY, as the real LG1 run shows), so code-quality-loop's retirement route gets
  the passes with no extra work.
- **No new stage.** The passes are two writer legs of POLISH-Q, like the existing `polish-fix` leg:
  `next = 'polish-simplify'`, then `'polish-tests'`. The non-lifecycle (legacy-format) POLISH is unchanged and gets
  no writer.
- **Order: simplifier first, then test consolidation**, as legacy (§3.5.4 then §3.5.5) and CQL (Step 2 then Step 3).
  Tests follow the final shape of the code. The test writer starts from the tree the simplifier leaves; one replay (§2)
  then reviews both writes together.

## 2. Safety model

- **The writer turn** is the FINISH writer path (`C._lifecycle_writer`), with nothing new: a fresh session with the
  author vendor and model (e2e-1); writes only in the workspace; the HEAD, ref and index guard; the docs-reserved note;
  the F3 ignored-config record; the author sandbox flags; no ledger, verdict or review history in the prompt. The
  prompts live in `WL`, beside `finish_prompt`:
  - simplifier: the `agents/code-simplifier.md` body, inlined, plus the changed paths;
  - test writer: CQL Step 3's four rules (keep critical-path tests, add missing ones for key logic, remove redundant
    ones, clear test comments), limited to tests of the changed code.

  Each writer answers READY or HOLD, as FINISH does.
- **Tree binding, per writer.** Entering the legs, the coordinator copies POLISH-Q's `candidate_oid` (the
  FINISH-approved tree, specialists clean) into the marker as `base_oid`; it stays fixed for the item's legs, since the
  replay transition clears `candidate_oid`. Each writer has its own input:
  - simplifier input = `base_oid`; the live tree must equal it (the check `worktree_polish_turn` already makes);
  - test-writer input = the simplifier's recorded `output_oid` (equal to `base_oid` when the simplifier was skipped,
    no-op, rolled back or exhausted); the live tree must equal it.

  A mismatch HOLDs before dispatch, as a stale POLISH-Q tree does today. After the turn the coordinator computes the
  output tree with the candidate-tree ingest FINISH uses, and records `input_oid`, `output_oid`, the receipt id and the
  state in the marker (`lifecycle_spine` `stage_request` with role `simplifier` or `test-writer`).
- **What decides: the tree, not the answer.**
  - output = input: `no-op`, whatever the answer;
  - output ≠ input and READY: the local check below;
  - output ≠ input and HOLD (a partial write): rolled back as below, state `rolled-back:hold`, no local check.
- **Local check and rollback.** Before each writer's first attempt the coordinator calls `RG.capture(workspace, keep)` (keep =
  `internal/readonly/<seq>-<role>`, the D-EFF category A directory; it keeps the index copy, the permissions and the
  ignored set) and stores the record path in the marker. After a changed READY turn it runs the configured test command
  (one local-check slot).
  - Tests pass: the writer is `wrote`.
  - Tests fail: `RG.evidence` writes the diff to the evidence directory, `RG.restore` puts back the input tree and index,
    and the coordinator verifies that the live tree equals `input_oid`. Then `rolled-back:tests`.
  - A failed attempt (zero-tool, timeout, malformed, or an exception out of `_lifecycle_writer`) may have written
    files. Before any retry the coordinator restores the same way from the writer's capture (taken once, before the
    first attempt) and verifies the live tree = `input_oid`; the retry then binds to `input_oid` again. When the
    attempts run out, the tree is restored and verified the same way and the marker records `exhausted` with
    `output_oid = input_oid`.
  - A restore that does not verify HOLDs through the existing unrestored path.
  - The test writer still runs after a simplifier rollback or exhaustion (its input is then `base_oid`).
- **Reviewer after a writer, and the gate.** After both legs, if the final tree ≠ `base_oid`, the run takes the
  polish-fix transition (C ~6547): epoch + 1, `phase = EXEC`, `candidate_oid = None`, `next = reviewer`,
  `gate_ran = False`. So the persistent EXEC reviewer reviews the writer diff (delta since last review), the shadow and
  the adversarial gate run on the new convergence, FINISH runs, and POLISH-Q's specialists run fresh on the new tree.
  No writer output reaches DOCS without that chain. If the final tree = `base_oid` (all no-op, skipped or rolled back)
  there is no new epoch: that tree is the one FINISH and the specialists already passed. The writers do not run again
  in the replay (§3).
- **Write boundary.** As for FINISH: reserved docs paths and `.review-loop/` config are outside the grant; an attempt
  refuses or HOLDs before review; any write invalidates EXEC under e2e-1's rule (tests and comments included).
- **SECURITY ordering.** The writers run only in POLISH-Q, so always before DOCS and SECURITY, and SECURITY reviews the
  final tree. A SECURITY repair replays EXEC and later stages, but the writers stay skipped by their markers, so
  nothing writes after SECURITY except the existing SECURITY fixer.

## 3. Budget and stop rules

- **One pass per item for each writer.** The per-item marker `state['lifecycle']['quality_writers']` holds `base_oid`
  and, per writer, a state: `wrote`, `no-op`, `rolled-back:<tests|hold>`, `skipped:<reason>` or `exhausted`. Once a
  writer has a state, every later POLISH-Q evaluation skips it: replay epochs, a DONE `reject`, a resume, a scope-change
  successor. The marker is copied into the successor spec the way `item_blockers` already is.
- **Skip evidence.** At each POLISH-Q evaluation that skips a writer, the coordinator writes one evidence record
  (`NNN-polish-q-<writer>-skip.json`: state or reason, source receipt id, current candidate OID). Like e2e-2b1's policy
  SKIP it is a report record, never closure proof, and never replaces a review, the gate or a test.
- **Caps.** The existing `BP` caps as written: `simplifier` 2/4 and `test-writer` 2/4 started attempts per item. An
  attempt covers a zero-tool, timeout or malformed retry. After the attempts run out the writer is `exhausted`, a skip.
- **Skip rules.** Checked before dispatch; the reason is recorded.
  - the writer is off in `quality_writers`, or `skip_quality_polish` is true;
  - the simplifier only, "small change": fewer than 20 added plus deleted lines in code files, counted with
    `git diff --numstat <review base> <base_oid>`. `base_oid` is a candidate tree, so new untracked non-ignored files
    count, as in `_changed_paths()`. Code = not docs, not tests, not config, not binary; a docs-, tests- or config-only
    change is small by this rule;
  - the test writer only: no test file among the changed paths, or no test command (the result cannot be checked);
  - an open blocker after the specialists: the polish-fix leg runs first, and the writers wait for a clean POLISH-Q;
  - no headroom: the writer, its local check and one full replay epoch (reviewer, shadow, gate, finisher, the
    specialists) must fit the invocation, EXEC-round, epoch and replay caps. Otherwise `skipped:budget`, never a HOLD.
- **Cost.** A write costs one replay epoch: about 8 calls in the real LG1 run's configuration (reviewer, shadow, gate,
  finisher and four specialists), about 57% on top of that run's 14 calls. Both writers share that one replay.
- **What this changes in e2e-2b1** (its simplifier rules are superseded for both writers; C1-c amends the doc):
  - item states `successful`/`rolled-back`/`exhausted` → the states above, now for the test writer too;
  - "on build failure, restore the old OID but keep the new epoch and rerun EXEC review/gate" → the restore happens
    before any transition, so the restored tree is the reviewed `base_oid` (or the simplifier's output, which the one
    replay reviews) and no epoch is opened for it;
  - "`exhausted` HOLDs and blocks restart/successor before PLAN until extension" → `exhausted` is a skip; no pre-PLAN
    block. The passes are optional polish (owner rule: no HOLD where nothing is unsafe);
  - "sign a current-OID policy SKIP at every POLISH-Q evaluation" → kept as the skip evidence record above, for every
    skip reason;
  - "reserve writer, review, local-check and replay slots before a writer; later shortage HOLDs" → kept as the headroom
    rule, with `skipped:budget` before dispatch instead of a HOLD.

  The e2e-2b1 fake-CLI names were planned, not built (no test carries them): `test_simplifier_noop_docs_refresh`,
  `…_rollback_new_epoch`, `…_rollback_successor`, `…_success_after_reject`, `…_exhausted_successor`. C1-c replaces them
  in e2e-2b1 with the §5 names.

## 4. What the user sees; opting in or out

- **Key `quality_writers`:** `both` (default), `simplify`, `tests` or `off`. It is set by the CLI flag
  `--quality-writers` or an operator profile and frozen at run start; a different value on resume is refused like other
  lifecycle keys. It joins `WL.PROFILE_KEYS`, so a workspace profile cannot set it (E-4). `skip_quality_polish: true`
  implies `off`. The default is `both` because the owner kept the capability and legacy runs both passes by default;
  the small-change and no-test rules keep the cost to changes where the passes can help.
- **Progress and status.** Each leg prints a dispatch line (`POLISH-Q simplifier`, `POLISH-Q test-writer`), its local
  check, and a `replay epoch N` line when it wrote. `status` shows the marker.
- **Delivery report.** One line per writer: 已修改 N 个文件 / 未改动 / 已跳过（原因）/ 已回滚（测试失败或 HOLD，证据路径）/
  已用尽. The replay epoch appears in the existing stage list.

## 5. Test plan (implementation step)

Fake-CLI tests through the real W path, each asserting the receipts, stage order and markers:
1. `test_writer_simplify_replay`: the simplifier writes; replay (EXEC reviewer, shadow and gate on the writer diff,
   FINISH, fresh specialists, both writers skipped by marker), DOCS, SECURITY, DONE.
2. `test_writer_both_one_replay`: both write; the test writer's input is the simplifier's `output_oid`; one epoch.
3. `test_writer_noop`: both no-op, and READY or HOLD with an unchanged tree: no epoch, straight to DOCS.
4. `test_writer_rollback`: the tests fail after the simplifier; restored, live tree = `base_oid`, a pre-existing staged
   entry still in the index, `rolled-back:tests`, no epoch; the test writer still runs. A restore that cannot verify HOLDs.
5. `test_writer_hold_changed_tree`: HOLD with a changed tree → rolled back (`rolled-back:hold`), no local check.
6. `test_writer_skip_rules`: a 10-line change; a new untracked 30-line code file (not small); docs or tests only; no
   test file; no test command; each `quality_writers` value; `skip_quality_polish`; a specialist blocker cleared first
   by polish-fix. Each skip writes its evidence record.
7. `test_writer_budget`: no headroom → `skipped:budget`; zero-tool retries → `exhausted`, no HOLD, a successor skips it;
   an attempt that writes files and then times out or returns malformed output is restored before the retry, and at
   exhaustion the live tree = `input_oid` and the test writer binds to `base_oid`.
8. `test_writer_boundary`: a reserved docs path or `.review-loop/` config write; a HEAD or index change; a stale input.
9. `test_writer_review_only`: the review-only entry runs the same flow.
10. `test_writer_markers_survive`: a DONE `reject`, a resume, a scope-change successor; no writer runs twice per item.
11. `test_writer_config`: the workspace profile is refused; a different value on resume is refused; CLI = profile.

Plus one real review-only gate run with a simplifiable change, as the closing check (unscored).

## 6. Implementation batches (about 200 product lines each, tests separate)

- **C1-a, plumbing (~160):** the key (CLI, profile, frozen config, refusal), the marker with `base_oid` and its
  successor copy, the skip rules with the numstat count, the headroom rule, the skip evidence record, routing of the
  POLISH-Q tail into the two legs with the skip and no-op path, the report and status lines. Tests 3, 6, 7, 10, 11.
- **C1-b1, the writer legs (~150):** the two prompts in `WL`, each leg through `_lifecycle_writer` with its input
  binding, the capture before the turn, the output tree and receipt, the tree-over-answer decision. Tests 2, 8, 9.
- **C1-b2, check, rollback, replay (~130):** the local check, `evidence` + `restore` + the verify, the HOLD-with-write
  and failed-attempt rollbacks, the transition against `base_oid`. Tests 1, 4, 5, 7 (the failed-attempt part).

The line counts are estimates; a batch whose real diff passes about 200 product lines is split at implementation.
- **C1-c, docs (no product code):** README and `review-only-entry.md` (the passes and the key), the migration map
  (owner decision 4 answered), Q6 §6 (capability 1 kept), and the e2e-2b1 amendment of §3. The code-quality-loop
  retirement itself (the thin entry, lint contracts, §3.6 dependants) is the separate D09 retirement unit.
