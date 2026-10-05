# Legacy deprecation readiness (2026-10-05)

## 给 owner 的摘要

按 ADR-6，只有四条替换标准全部满足，才能退役 legacy。目前的状态：
- **第 (3) 条已满足：** 一流的用户验收阶段已经实现，accept/reject 都要先出 intent 再按 expect 执行。
- **第 (2) 条部分满足：**
  - 只看 gate run 序列，有 4 次连续的真实 run（ws7–ws10）没有出现 coordinator 缺陷，都经默认入口并 ACCEPTED，其中一次是 review-only。
  - 但如果把同一天消费者现场的缺陷 FIELD-21 也计入，它之后只剩 ws9、ws10 两次；而包含修复的只有 ws10 一次。
  - 这些 run 都是小型 toy 任务，由 supervisor 的 headless driver 驱动，每次 run 后还要手工清理 Codex trust 条目。
  - overseer 成本现已测量（§3.2a）：每个 run 的 overseer 花费 $0.91–1.04（标价），占 Claude 侧花费的 48–52%，占输入 token 的 52–69%（绝大部分是缓存读）。
- **第 (4) 条部分满足：** 已覆盖 poker-tools 以外的仓库（poker-news-bot）；"大任务"还没有定义；真实的订阅限额 HOLD 加 resume 至今没有发生过，只有离线证据，而按现行 ADR-6 离线证据不算满足。
- **第 (1) 条尚未满足：** M7 已有 24 个冻结案例和评分工具，但工具还差一处修复，D-b1 scanner 停在 review 上限，计分 run 还没开始。

所以现在**还不能退役 legacy**；四条全部满足后，还要做一次带证据的最终复核才能退役。最长的那条路（M7）已经从"没有语料"走到了"可以开跑"。legacy 的功能映射：49 项中 covered 27、planned 4、owner 18。Q6（code-quality-loop）和 LG2（review-pr）分别等你决定和复审。下面 §4 把剩下的事按顺序列出，并把你的决定和工程工作分开。

## 1. The rule

**ADR-6** (`DECISIONS.md`, "paired-session primary daily entry and replacement gate", owner 2026-09-24). Criterion (1)
decides WHETHER legacy can be replaced; (2)–(4) decide WHEN. The criteria, shortened:
1. Fresh reviewing roles catch most seeded regressions that keep the suite green, with the same seeded diffs run
   through the old reviewer path as control. **New no worse than old.**
2. **Three consecutive real runs with zero coordinator defects**, and an overseer limited to scoping the work item and
   verifying at the end. Overseer steady-state token cost is measured and included in per-item cost.
3. A **first-class user-acceptance feedback phase** exists in the coordinator.
4. **Coverage:** at least one repo other than poker-tools, one large task, and one real subscription-limit HOLD
   followed by a successful resume.

Further, from ADR-6: "The old implementation may be retired only after all four criteria pass." The primary daily
entry could switch earlier, after an owner go for 1D and a parity decision on polish, docs, security and specialists.

**Amendments that bear on it** (recorded in ADR-11 "Amendments"):
- E-12 (2026-10-04): D12 + D-2 + W2a are the parity decision ADR-6 asks for. 1C closure and M6 no longer gate the
  default entry. They are replaced by E-1 plus the two M6 residuals recorded on 2026-10-01:
  - a clean Claude-author probe PASS. The supervisor's 2026-10-05 amendment classes it as category C (strict only),
    superseded by the revised E-1;
  - one evidence-complete run started from a normal shell. No record found that it was met or superseded.
- E-1 as revised 2026-10-04: GitHub Actions green plus one real run through the default entry, lifecycle on, reaching
  ACCEPTED. This was the gate for switching the *default entry* (v2.10.0), not for retirement.
- D-EFF (2026-10-04, `paired_session/docs/efficient-mode.md`): efficient is the default. Strict-only (category C)
  conditions are not release conditions.

None of the amendments changes the four retirement criteria.

## 2. Status per criterion

| Criterion | Status | Evidence |
|---|---|---|
| Default-entry switch: 1D go and the parity decision | **MET** | ADR-11 + E-12 (D12 + D-2 + W2a are the parity decision); v2.10.0 `c8336e7` (tag) made paired-session the default entry |
| Default-entry switch: revised E-1 evidence and the E-12 residual | **E-1 MET; residual OWNER** | Revised E-1 is met. Gate run `6cd57da7` was ACCEPTED. CI run 37252072913 on the release commit rc4 = `c8336e7` concluded success, macOS jobs 64/64 and 0 non-success jobs (supervisor `gh run view`, 2026-10-05). The normal-shell residual (§1) is open; the gate runs were started by a headless driver. |
| (1) Seeded regressions, new no worse than old | **NOT MET** | M7 (`m7-seeded-defect-comparison.md`, `m7-execution-plan.md`): corpus ready, no scored run (§3.4). One anecdote: gate run `a90a9e54` found and fixed a seeded bug, with no legacy control. |
| (2) Three consecutive zero-defect real runs, limited overseer, overseer cost measured | **PARTIAL** | The gate-run series has 4 consecutive runs (ws7–ws10). Counting the consumer field defect FIELD-21, only 2 follow it (ws9, ws10). Only ws10 ran with the fix. The cost clause is MET: the overseer's tokens and cost are measured and added to per-item cost (§3.2a). The overseer limit is not met (§3.2). |
| (3) First-class user-acceptance feedback phase | **MET** | `accept` / `reject` with `--intent-only` digest then `--expect`; `note` and `--scope-change`; DONE = acceptance pending (`docs/protocol/paired-session-entry.md` "DONE and acceptance"). Accept exercised in the 4 gate runs and in consumer runs. Reject reopens EXEC; it is covered by tests, and no gate run used it. |
| (4a) A repo other than poker-tools | **MET** | poker-news-bot ab_pipeline: many paired-session runs since 2026-09-24, including v2.10.0+ runs `runs210/WI-101`, `WI-102` and `runs211/WI-103` (DONE; direct CLI, lifecycle off) |
| (4b) One large task | **OWNER** | Not defined in ADR-6. A definition is proposed in §3.6. No gate run would qualify; the largest consumer work items are near the proposed size but ran with lifecycle off. |
| (4c) Real subscription-limit HOLD followed by a successful resume | **NOT MET** | Typed rate-limit HOLD in every role and stage, which never spends a budget: `c70591d` (v2.11.0). Offline tests only; no real limit event has happened yet. |

## 3. Evidence available today (2026-10-05)

Verified directly, read-only:
- git tags and commits;
- the run-dir `state.json` / `usage.md` of the gate runs and of the consumer runs;
- the documents in this tree.

Taken from records:
- the supervisor log (attempt history, CI runs, times, FIELD-21);
- lane A's M7 results (§3.4).

### 3.1 Releases
- **v2.10.0** `c8336e7` (tag): default entry, worktree lifecycle, efficient mode.
- **v2.10.1** `5b9b8bb` (tag): long test commands polled to completion (FIELD-20, `2726498`).
- **v2.11.0** `fc5d5f1` (tag): review-only entry (D-LG1), `--detach`/`stop`, typed rate-limit HOLD, F7.
- **v2.11.1** rc1 `99f0577`: includes FIELD-21 `1adb353` and f3 `1121a4b`. Not tagged at the time of writing.

### 3.2 Gate real runs through the default entry
Run dirs: `~/.local/state/review-loop/runs/<id>`; supervisor log `.compass/results/2026-09-24_shadow-supervisor-log.md`
in the main checkout, 2026-10-05.

| Run | Workspace | Release candidate | Task | Result |
|---|---|---|---|---|
| `6cd57da7` | ws7 | v2.10.0 rc4 | fresh (median) | ACCEPTED 10:56; 14 turns; no HOLD |
| `3888ea0e` | ws8 | v2.10.1 rc1 | fresh, 90 s suite | ACCEPTED 13:19; 14 turns; no HOLD; FIELD-20 verified on a real gate turn |
| `a90a9e54` | ws9 | v2.11.0 rc1 | review-only of a dirty seeded bug | reviewer and shadow found the bug, EXEC r2 fixed it; `--detach` used; ACCEPTED 15:53; 14 turns |
| `00d73bc6` | ws10 | v2.11.1 rc1 | fresh (`mode()`) | ACCEPTED 17:14; 14 turns; 0 findings |

All four ran with lifecycle on and in efficient mode: Codex author, Claude reviewer, Codex gate, auto_commit off.
Each state has status ACCEPTED and no HOLD.

**Which runs count as zero-defect.**
- **ws7–ws10 count:** four consecutive runs with no coordinator defect and no HOLD, on four release candidates.
- **Attempts 1–6 earlier that day do not count.** They HOLDed or failed:
  - attempt 1: FIELD-15/16 — run root under `~/.claude`, and a silent drift to legacy;
  - attempt 2: the Codex plugin-cache capability scan blocked dispatch (fixCG);
  - attempt 3: FIELD-17 — a headless `-p` driver kills the background run;
  - attempt 4: FIELD-18 — protocol files were written into the worktree;
  - attempt 5: a designed SECURITY HOLD, because the toy repo's `.gitignore` lacked coverage (FIELD-19);
  - attempt 6: a harness prompt artifact.
  - All are fixed in the releases above.
- **Not counted by this memo; for the owner to judge:** FIELD-21, a coordinator defect in field use.
  - At 13:23 a new Claude session on this host materialized review-loop 2.10.1 and rewrote `installed_plugins.json`.
  - The Claude author turn of a consumer run on v2.10.0 (poker-news-bot `WI-102`) then HOLDed with "changed global
    config: claude_plugins".
  - It was reported to the supervisor at 14:20, between ws8 and ws9. Fixed in `1adb353` (v2.11.1 rc).
  - If consumer runs count, the runs after it are ws9 and ws10. Only ws10 (v2.11.1 rc1) ran a build containing the
    fix, so the series after the fix has one run.

**Why (2) is still PARTIAL.**
- The tasks are tiny toys in a scratch repo.
- The overseer was the supervisor's headless driver. Besides scoping and accepting, it restored `~/.codex/config.toml`
  after every run, removing the Codex trust entry: a known Codex CLI behaviour that is recorded, not reverted.
- The overseer did more than scope and verify: it drove the run, polled it, accepted it and cleaned up after it.
  Its cost is measured in §3.2a.

### 3.2a Overseer cost per item (criterion (2), "measured and included in per-item cost")

**Method.** The script is `.compass/results/lanea/overseer_cost.py`: standard library, read-only. Its output is
`.compass/results/lanea/overseer-cost.json`.

*Overseer:* the driver's `claude -p --output-format stream-json` logs in `~/paired-runs/real210/`, one per run
(`drive7`, `drive8`, `drive9`, `drive10`, each `.jsonl`) plus its accept log (`drive7a` … `drive10a`).
- The accept log resumes the same session.
- Its result event's `modelUsage` / `total_cost_usd` are cumulative for the session.
- The script checks that they equal the main log's `usage` plus the accept log's `usage`, so no other invocation of the
  session is hidden.
- `drive8.jsonl` interleaves a second session; only session `BFB4D924-…` is counted.

*Roles:* tokens per role come from the run's `usage.json` (`by_role`). Codex turns report no cost, so the Codex roles
(author, gate) count in tokens only. Claude cost comes from the result events in `evidence/NNN-*.stdout.jsonl`:
- The persistent reviewer resumes one session across its turns, so each result's `total_cost_usd` is cumulative.
- The script groups the Claude turns by role and session, and counts each session once, at its last result.
- It checks that the session's cumulative tokens equal the sum of its turns' own usage.

*Units:* all USD figures are the CLI's list-price `costUSD`; subscription use is not billed per token. "Input" is all
input tokens, including cache reads.

| Run | Overseer input (cache-read) | Overseer output | Overseer $ | Roles input (cached) | Roles output | Claude roles $ (reviewer + shadow) | Total input | Total output | Total Claude-side $ | Overseer share: input / Claude $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ws7 `6cd57da7` | 1,488,722 (1,430,318) | 9,549 | 0.944 | 679,708 (512,678) | 15,465 | 0.864 | 2,168,430 | 25,014 | 1.809 | 69 % / 52 % |
| ws8 `3888ea0e` | 1,628,634 (1,565,146) | 10,710 | 1.035 | 1,113,118 (928,672) | 15,959 | 0.971 | 2,741,752 | 26,669 | 2.006 | 59 % / 52 % |
| ws9 `a90a9e54` | 1,463,773 (1,400,679) | 10,104 | 0.987 | 1,033,530 (859,022) | 17,400 | 1.077 | 2,497,303 | 27,504 | 2.064 | 59 % / 48 % |
| ws10 `00d73bc6` | 1,314,245 (1,254,656) | 9,332 | 0.914 | 1,187,411 (1,014,064) | 16,635 | 0.975 | 2,501,656 | 25,967 | 1.889 | 52 % / 48 % |

The totals cover the overseer and every role. The Total Claude-side $ excludes the Codex roles, which report no cost.

Per item:
- Claude-side list cost is about $1.8–2.1: the overseer plus the reviewer and shadow.
- The Codex author and gate add 0.3–0.7M input tokens, with no cost reported.
- The overseer runs 26–31 turns over about the role wall time (it polls the run). About 96 % of its input is cache
  reads.

**Status of the clause: MET**; the numbers are complete for all four runs. Caveats:
- These are toy tasks, where a fixed overseer overhead weighs more than it would on real work.
- The overseer was a headless driver that also polled and cleaned up. A human overseer, or `--detach` plus fewer
  polls, would cost differently.
- Codex roles carry no USD figure.
- A single host and a single day.

### 3.3 Consumer field use
- **poker-news-bot:**
  - runs on v2.10.0 (`runs210/WI-101`, `WI-102`) and v2.11 (`runs211/WI-103`), all DONE;
  - all of them are direct CLI with lifecycle off, i.e. its own pipeline, not the default entry;
  - FIELD-20 (long test commands, fixed `2726498`) and FIELD-21 (above) came from this use.
- **poker-tools:**
  - accepted paired runs up to 2026-10-04 (for example `bi/run-01`, `n4/run-02`), on 2.9.x with lifecycle off;
  - no run on 2.10+ found.

### 3.4 M7 (criterion 1)
Evidence: lane A's results file `.compass/results/m7/2026-10-05_lanea-m7-s2-s5.md` (freezes, D-b2 counts, tests, the
m7-s5 rehearsal) and `.compass/results/lanea/m7/build/frozen-s1*/` (m7-s1 freeze results), both local and outside git.
The supervisor log covers m7-s1/m7-s3/m7-s4.
- **Corpus:** 24 cases = 6 archived + 18 synthetic (4 clean); 27 key blockers. D-b2: 0 hits against both pins. In
  `~/paired-runs/m7/{corpus,keys}`.
- **Tools:**
  - grader and freeze committed as m7-s1 `001abec` (lane B branch).
  - Re-freeze with it: 24/24 ok. The grader dry run is refused for 2 cases until `fresh_commit` stages with
    `add -A --force` (fix queued for lane B; a scratch probe with the fix passes).
  - The D-b1 transcript scanner (m7-s3) stopped at the 3-round review cap and is parked for an owner R4.
- **Rehearsals:** the paired first-review stop rehearsed on the fake (m7-s5). Effort fixed at medium for both arms.
- **Not started:** the unscored real pilot (m7-s4, dispatched) and all scored runs (owner authorization and a quota
  window, plan §7 steps 6–8).

### 3.5 Legacy coverage map and pending design work
- **The map:** `docs/paired-session-migration.md` "Legacy → paired-session map (after v2.11.0)", 49 rows:
  - covered 27;
  - planned 4 (D-LG2, M7);
  - owner decision 18 (the legacy-gap §5 items 1, 4–10, 12–15).
- **Q6 memo** `paired_session/docs/q6-code-quality-loop.md` (`913b93a`): recommends retiring code-quality-loop onto
  `run --review-only`, deciding 6 capabilities one by one.
- **LG2 (review-pr port):** the design is parked after 3 review rounds. The draft is outside the tree; the owner
  decides on R4 and on Q-R1..Q-R10.

### 3.6 Proposed definition of "one large task" (criterion (4b); for the owner to decide)

**Data.** Finished real paired-session runs, read-only. The script is `.compass/results/lanea/task_size.py`; the data
is in `task-size.json`. Files and lines come from `context/delta.stat`.
- **poker-news-bot, 64 finished runs:**
  - CLI turns: median 8, max 15.
  - EXEC rounds: median 1, max 4.
  - Size, for the 28 runs with a `delta.stat`: median 2 files / 212 changed lines; p75 4 / 310; max 7 files / 549
    lines.
  - The largest: WI-27 (6 files, 549 lines, 2 EXEC rounds), WI-22 (6, 526), WI-14 (7, 452).
- **poker-tools, 20 runs:** median 11 turns; only 2 have a `delta.stat`, at 12 and 33 files.
- **The four gate runs:** 14 turns each, toy tasks of one module and its tests.
- Role wall time is heavy-tailed (poker-news-bot p75 4,672 s), because it includes waits. It is a poor size measure.

**Proposal: a "large task" is one real work item that meets all of these:**
1. It is not seeded and is not a toy.
2. It runs through the default entry with lifecycle on, so every stage runs.
3. It reaches ACCEPTED.
4. Its accepted change touches at least 6 files and at least 400 changed lines (insertions plus deletions). That is
   about the top of today's consumer work items and well above every gate run.

The size threshold is a floor. Under this proposal the owner chooses which item to use, but the item must still meet
the floor; naming an item is not a waiver. A waiver would be a different definition: alternative (c) below.

Alternatives:
- (b) Define it by turns, rounds or wall time. Turns and wall time track waits and retries rather than size.
- (c) Let the owner name a specific upcoming work item as the large task, whatever its size. This waives the floor.

**Recommendation: the proposal (size floor, default entry, lifecycle on, ACCEPTED).** In practice the owner names the
next poker-news-bot or poker-tools item that meets the floor and has it run through the default entry.

**Owner question:** accept the proposal, choose (b) or (c), or set other thresholds.

**Already-finished lifecycle-off items:** none of the poker-news-bot items above passed through every stage (they ran
with lifecycle off), so under the proposal they do not count. Whether such an item may count is part of the owner's
answer.

## 4. What remains, in order

**Owner decisions** (each unblocks the engineering item named):
1. Authorize the M7 scored runs and a quota window (criterion 1). Decide R4 for the m7-s3 scanner.
2. Read criterion (2):
   - Do supervisor-driven toy runs with the Codex trust cleanup count?
   - Does FIELD-21 in consumer use reset the count?
   - Is the measured driver-session overseer cost (§3.2a) acceptable as the measurement, given its caveats?
3. Define "one large task" for (4b): accept the §3.6 proposal (6+ files, 400+ changed lines, default entry, lifecycle
   on, ACCEPTED), choose an alternative, or name a work item.
4. Criterion (4c): ADR-6 requires a real subscription-limit HOLD followed by a successful resume. The offline
   evidence does not satisfy it. Accepting anything less needs an explicit ADR-6 amendment; otherwise wait for a real
   event.
5. The E-12 normal-shell residual (§1): run it, or record it as superseded.
6. Q6 (code-quality-loop A/B/C plus its 6 capabilities).
7. The 18 owner rows of the legacy map (legacy-gap §5).
8. LG2 R4 and Q-R1..Q-R10.

**Engineering work** (no owner input needed beyond the item above it):
1. Lane B: `m7_corpus.py` `fresh_commit` uses `add -A --force`; re-freeze the 24 cases; dry grade.
2. Close the default-entry evidence: if the owner keeps the normal-shell residual (owner 5), run and record that
   evidence-complete run. The rc4 CI result is recorded (success, 64/64 macOS jobs).
3. Release v2.11.1 (FIELD-21, f3), with its gate run already ACCEPTED (`00d73bc6`) and CI. If the owner counts
   FIELD-21 against criterion (2), add real runs until three consecutive zero-defect runs exist after the fix.
4. M7: the unscored real pilot (m7-s4), then the D-b1 scanner after R4, then the scored runs and grading (after owner 1).
5. Run the large task as defined by the owner through the default entry (after owner 3). Measure its overseer cost
   with `overseer_cost.py`, adapting the run list, so (2)'s cost figure also exists for a real-size item.
6. When a real subscription limit is hit in any paired run, record the HOLD and the successful resume as criterion
   (4c) evidence.
7. Implement the owner answers for the legacy-map rows, Q6 and LG2. The pinned legacy copy must stay runnable until
   M7 is scored.
8. Final readiness review: check all four criteria and the default-entry evidence (normal-shell residual)
   against their evidence, including any ADR-6 amendment. Only then
   remove the legacy entry and its last users (`entry: legacy`, `/review-loop:legacy`), with the owner's go.
