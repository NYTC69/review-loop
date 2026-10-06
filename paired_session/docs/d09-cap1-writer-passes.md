# D09 capability 1: the simplifier and test-consolidation writer passes (design)

Status: design only. Owner decision D09 (2026-10-05): code-quality-loop retires onto `run --review-only` (Q6 option A)
and **capability 1 is kept**: the simplifier and test-consolidation writers are ported into paired-session. This also
answers owner decision 4 of the migration map for the main pipeline. Capability 3 (comment/type analyzers) goes to
D-LG2; capabilities 2, 4, 5 and 6 are dropped. Defaults: owner decision 2026-10-06 (§4).

Sources: [q6-code-quality-loop.md](q6-code-quality-loop.md) §3–5; [e2e-1](e2e-1-stages-and-roles.md) (POLISH-Q exit
names the simplifier and test writer; writer invalidation); [e2e-2b1](e2e-2b1-budgets.md) (the `simplifier` and
`test-writer` caps and the simplifier item marker); `agents/code-simplifier.md`; legacy `docs/protocol/execution.md`
§3.5.4–3.5.5 and `skills/code-quality-loop/SKILL.md` Steps 2–3. Code: `C` = `coordinator.py`, `WL` =
`worktree_lifecycle.py`, `BP` = `budget_policy.py`, `RG` = `readonly_guard.py`.

## 1. Where the passes run

- **One place: inside POLISH-Q of the worktree lifecycle W**, after the report-only specialists finish with no open
  blocker and before DOCS. W is the route of both the main pipeline and the review-only entry (`run --review-only` goes
  EXEC → FINISH → POLISH-Q → DOCS → SECURITY, as the real LG1 run shows). The defaults differ per entry (§4).
- **No new stage.** Two writer legs of POLISH-Q, like the existing `polish-fix` leg: `next = 'polish-simplify'`, then
  `'polish-tests'`. The non-lifecycle (legacy-format) POLISH is unchanged and gets no writer.
- **Order: simplifier first, then test consolidation**, as legacy (§3.5.4, §3.5.5) and CQL (Steps 2, 3). The test
  writer starts from the tree the simplifier leaves; one replay (§2) reviews both writes together.

## 2. Safety model

- **The writer turn** is the FINISH writer path (`C._lifecycle_writer`), nothing new: a fresh session with the author
  vendor and model (e2e-1); writes only in the workspace; the HEAD, ref and index guard; the docs-reserved note; the F3
  ignored-config record; the author sandbox flags; no ledger, verdict or review history in the prompt. Prompts in `WL`,
  beside `finish_prompt`, each with the changed paths:
  - simplifier: the `agents/code-simplifier.md` body, inlined;
  - test writer: consolidate the changed test files only: keep critical-path tests, remove redundant ones, clear test
    comments. It adds no tests for uncovered logic (pr-test-analyzer reports that, and polish-fix handles it).

  Each writer answers READY or HOLD, as FINISH does.
- **Tree binding, per writer.** All OIDs here are `git_snapshot` digests (C:1339), the kind `candidate_oid` and
  FINISH's `output_oid` already are. Entering the legs, the coordinator copies POLISH-Q's `candidate_oid` (the
  FINISH-approved digest, specialists clean) into the marker as `base_oid`; the replay transition clears
  `candidate_oid`, `base_oid` stays. Simplifier input = `base_oid`; test-writer input = the simplifier's `output_oid`
  (= `base_oid` when it was skipped, no-op, rolled back or exhausted). The live digest must equal the input, else a
  HOLD before dispatch, as a stale POLISH-Q tree does today. After the turn the marker records `input_oid`,
  `output_oid` (the live digest), the receipt id and the state.
- **What decides: the tree, not the answer.** Output = input: `no-op`, whatever the answer. Changed + READY: the local
  check. Changed + HOLD (a partial write): rolled back, `rolled-back:hold`, no local check.
- **Capture, rollback, failed attempts.** Before the baseline run (§3) and before each writer's first attempt the
  coordinator calls `RG.capture(workspace, keep)` (keep = `internal/readonly/<seq>-<role>`; it keeps the index copy,
  permissions and ignored set) and stores the record path in the marker. The capture before the baseline run is kept
  separately as `base_capture`: the restore point for every whole-group rollback (baseline, `rolled-back:review`),
  whichever writers ran. A single writer's rollback uses its own capture. Each rollback is `RG.evidence` (the diff to
  the evidence directory), `RG.restore`, then a check that the live digest = the step's input (`base_oid` or the
  writer's `input_oid`); a restore that does not verify HOLDs through the existing unrestored path. A failed attempt (timeout, malformed answer, an exception out of `_lifecycle_writer`) is rolled back
  and records `exhausted` with `output_oid = input_oid`; only a zero-tool turn is retried, once. The test writer still
  runs after a simplifier rollback or exhaustion (its input is then `base_oid`).
- **Local test executor (new in C; none exists, the reviewer's `_observed_test` is the only test evidence today).**
  - Runs the explicitly configured test command (§3) with `/bin/sh -c`, cwd = the workspace, the run's `--timeout`.
  - stdout and stderr go to `evidence/NNN-polish-q-<writer>-test.log`; only the exit code counts (0 = pass; non-zero
    or timeout = fail). No model call, no invocation.
  - `git_snapshot` before and after: a changed live digest is a failure (the command rewrote tracked or untracked
    non-ignored files; ignored caches are fine).
  - After a changed READY turn: pass → `wrote`; fail → rollback, `rolled-back:tests`.
- **Reviewer after a writer, and the gate.** After both legs, if the live digest ≠ `base_oid`, the run takes the
  polish-fix transition (C ~6547: epoch + 1, `stage = phase = EXEC`, `candidate_oid = None`, `next = reviewer`,
  `gate_ran = False`) **and counts the round like a FINISH write** (`exec_rounds += 1`, C:6133). The persistent EXEC
  reviewer reviews the writer diff, then the shadow and gate, FINISH, and POLISH-Q's specialists on the new tree. No
  writer output reaches DOCS without that chain. If the digest = `base_oid` there is no transition.
- **One chance in review (`rolled-back:review`).** If the replay's first reviewer verdict is not APPROVE, or its gate
  fails, the coordinator does not open a fix round. It rolls the tree back to `base_oid` (`base_capture`, verified), closes the findings that replay raised as `withdrawn` with that evidence (as C ~7080 withdraws malformed
  gate findings), and marks each writer that wrote `rolled-back:review`. It sets the lifecycle to `stage = POLISH-Q`,
  `candidate_oid = base_oid` (the epoch stays) and signs a POLISH-Q READY spine receipt with `output_oid = base_oid`,
  citing the previous epoch's receipts on that byte-identical digest (EXEC approval, gate, FINISH, clean specialists).
  Then `advance` goes to DOCS. A later FINISH write or specialist blocker in the
  replay follows the normal rules (the headroom reserves that round).
- **Write boundary.** As for FINISH: reserved docs paths and `.review-loop/` config are outside the grant; an attempt
  refuses or HOLDs before review; any write invalidates EXEC under e2e-1's rule.
- **SECURITY ordering.** The writers run only in POLISH-Q, so before DOCS and SECURITY; SECURITY reviews the final
  tree. A SECURITY repair replays EXEC and later stages, but the writers stay skipped by their markers.

## 3. Budget and stop rules

- **One pass per item for each writer.** The marker `state['lifecycle']['quality_writers']` holds `base_oid` and, per
  writer, `wrote`, `no-op`, `rolled-back:<tests|hold|review>`, `skipped:<reason>` or `exhausted`. A writer with a state
  is skipped at every later POLISH-Q evaluation (replay epochs, a DONE `reject`, a resume, a scope-change successor);
  the marker is copied into the successor spec like `item_blockers`. One evidence record
  (`NNN-polish-q-<writer>.json`: state, reason, receipt id, digest) is written when the state is written, not again.
- **Caps and attribution.** Each writer call counts against its own `BP` cap (`simplifier` 2/4, `test-writer` 2/4;
  with one zero-tool retry at most 2 are used) **and** against POLISH-Q's run cap (32) as a `polish_calls` row, so
  `_redispatch_budget` and the polish-fix check keep one count.
- **Skip rules**, checked before dispatch, in this order; the reason is recorded:
  - the writer is off in `quality_writers`, or `skip_quality_polish` is true;
  - no explicit test command: `--test-command` on the CLI or `test_command` in a profile (the run records
    `test_command_explicit`; the `npm test` default does not count) → both `skipped:no-test-command`;
  - the simplifier only, "small change": fewer than 20 added plus deleted lines in code files. Counted on the live tree
    (it equals `base_oid` here): `git diff --numstat <review base>` plus the line counts of the files from
    `git ls-files -o --exclude-standard`. Code = not docs, tests, config or binary;
  - the test writer only: no test file among the changed paths;
  - an open blocker after the specialists: polish-fix runs first; the writers wait for a clean POLISH-Q;
  - no headroom (below) → `skipped:budget`;
  - **green baseline:** before the first writer the executor runs once on `base_oid`. A fail, timeout or digest change
    (restored first) → both `skipped:no-green-baseline`, never a rollback.
- **Headroom formula** (the `epochs`/`replays`/`local-checks` constants of `BP` have no runtime counter and are not
  used): before each leg, with W = writers still to run, R = `reviewer + shadow + gate + finisher` = 4 and S =
  `len(specialists(paths))`:
  - `max_invocations − q_reserved − invocations_used ≥ W + R + S` (one replay);
  - `exec_rounds + 2 ≤ exec_round_limit()` (the replay round plus one fix round);
  - `polish_calls + W + S + 1 ≤` the POLISH-Q run cap (the last specialist needs room for two dispatches,
    `_specialist_budget`, C:6607).
- **Cost (gate estimate, LG1 configuration).** A typical change (≥ 20 code lines, tests changed, a command set): 2
  writer calls, 2 local runs, and almost always one replay (reviewer, shadow, gate, finisher, about 4 specialists):
  **about +10 invocations, about +70%** on LG1's 14; wall time not measured. A REVISE in the replay costs no extra
  round (`rolled-back:review`). Under the default `--max-invocations 25` (`q_reserved` 8) an LG1-sized run often has no
  headroom left at POLISH-Q, so the writers are **often `skipped:budget`** unless the operator raises it by about 10.
- **What this changes in e2e-2b1** (its simplifier rules are superseded for both writers; C1-c amends the doc):
  - item states `successful`/`rolled-back`/`exhausted` → the states above, for the test writer too;
  - "restore the old OID but keep the new epoch, rerun EXEC review/gate" → rollback happens before any transition, so
    no epoch opens; after a replay, `rolled-back:review` returns to `base_oid` without a new round;
  - "never reuse old-epoch receipts" → one exception: the `rolled-back:review` receipt cites the previous epoch's
    receipts on the byte-identical `base_oid` digest;
  - "`exhausted` HOLDs and blocks restart/successor before PLAN" → `exhausted` is a skip (the passes are optional);
  - "sign a current-OID policy SKIP at every POLISH-Q evaluation" → one evidence record when the state is written;
  - "reserve slots before a writer; later shortage HOLDs" → the headroom formula, `skipped:budget` before dispatch.

  The e2e-2b1 fake-CLI names were planned, not built (no test defines them); C1-c replaces them with the §5 names.

## 4. What the user sees; opting in or out

- **Key `quality_writers`:** `both`, `simplify`, `tests` or `off`. **Default (owner, 2026-10-06): `both` for
  `run --review-only`, `off` for the main pipeline.** Set by `--quality-writers`, an operator profile or a workspace
  profile (a cost switch, not a safety key, so it is not in `WL.PROFILE_KEYS`); frozen at run start, a different value
  on resume is refused like other lifecycle keys. `skip_quality_polish: true` implies `off`.
- **Progress and status.** Each leg prints a dispatch line (`POLISH-Q simplifier`, `POLISH-Q test-writer`), the
  baseline and local runs, and a `replay epoch N` line when it wrote. `status` shows the marker.
- **Delivery report.** One line per writer: 已修改 N 个文件 / 未改动 / 已跳过（原因，`budget` 时附所需调用数）/
  已回滚（测试失败、HOLD 或审查未过，证据路径）/ 已用尽. With the main-pipeline default `off` it prints one line instead:
  "`--quality-writers both` enables the simplifier and test consolidation (about +10 invocations)".

## 5. Test plan (implementation step)

Fake-CLI tests through the real W path, asserting receipts, stage order, markers and the `exec_rounds` delta:
1. `test_writer_simplify_replay`: the simplifier writes; replay (reviewer, shadow, gate, FINISH, fresh specialists,
   writers skipped by marker), DOCS, SECURITY, DONE; `exec_rounds` + 1.
2. `test_writer_both_one_replay`: both write; the test writer's input is the simplifier's `output_oid`; one epoch.
3. `test_writer_noop`: both no-op, READY or HOLD with an unchanged tree: no epoch, straight to DOCS.
4. `test_writer_rollback_tests`: the local run fails after the simplifier; restored to `base_oid`, a pre-existing
   staged entry still in the index, no epoch; the test writer still runs. A restore that cannot verify HOLDs.
5. `test_writer_hold_changed_tree`: HOLD with a write → `rolled-back:hold`, no local run.
6. `test_writer_failed_attempt`: a write then timeout or malformed → restored, `exhausted`; a zero-tool turn retries once.
7. `test_writer_rollback_review`: the replay reviewer REVISEs (and, separately, the gate fails) → `base_oid` restored,
   findings `withdrawn`, POLISH-Q receipt on `base_oid`, DOCS; no further round. Rounds one short of the limit →
   `skipped:budget`. The simplifier skipped, the test writer writes, the reviewer REVISEs → restored from
   `base_capture`.
8. `test_writer_executor`: red baseline → `skipped:no-green-baseline`, no writer call; a test command that rewrites a
   tracked file fails (baseline and after a write); timeout fails; the log is in evidence; the `npm test` default
   alone → `skipped:no-test-command`.
9. `test_writer_skip_rules`: 10 code lines; a new untracked 30-line code file on the live tree (not small); docs or
   tests only; no test file; each `quality_writers` value; `skip_quality_polish`; a blocker cleared first by polish-fix.
10. `test_writer_budget`: the invocation formula one short → `skipped:budget` with the number in the report; writer
    calls appear in `polish_calls`. At the POLISH-Q edge: `polish_calls + W + S + 1` = cap runs the writers and the
    replay's last specialist passes `_specialist_budget`; one call more used → `skipped:budget`.
11. `test_writer_boundary`: a reserved docs path or `.review-loop/` config write; a HEAD or index change; a stale input.
12. `test_writer_review_only`: the review-only entry with its real starting `exec_rounds` (the existing change, one
    fix round, a FINISH write) runs the same flow, default `both`.
13. `test_writer_markers_survive`: a DONE `reject`, a resume, a scope-change successor; no writer runs twice per item.
14. `test_writer_config`: main pipeline default `off` with the hint line; a workspace profile may set the key; a
    different value on resume is refused.

Plus one real review-only gate run with a simplifiable change and raised `--max-invocations`, as the closing check.

## 6. Implementation batches (about 200 product lines each, tests separate)

- **C1-a, plumbing (~150):** the key and per-entry defaults, the marker and successor copy, the skip rules with the
  live-tree count, the headroom formula, the evidence record, POLISH-Q tail routing with skip and no-op paths, report
  and status lines. Tests 3, 9, 10, 13, 14.
- **C1-b1, writer legs (~150):** the two prompts, the legs through `_lifecycle_writer` with input binding, capture,
  the output digest and receipt, tree-over-answer, failed-attempt handling. Tests 2, 5, 6, 11.
- **C1-b2, replay and review rollback (~150):** the transition with `exec_rounds`, `rolled-back:review` (restore,
  withdraw, receipt), the rollback helper. Tests 1, 7, 12.
- **C1-b3, local executor and baseline (~100):** `test_command_explicit`, the executor, the baseline run, the
  `rolled-back:tests` path. Tests 4, 8.
- **C1-c, docs (no product code):** README and `review-only-entry.md`, the migration map (owner decision 4), Q6 §6, and
  the e2e-2b1 amendment of §3. The code-quality-loop retirement itself is the separate D09 retirement unit.

The line counts are estimates; a batch whose real diff passes about 200 product lines is split at implementation.
