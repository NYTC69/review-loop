# Legacy deprecation readiness (2026-10-05)

> **Historical** (2026-10-05 readiness memo). The legacy workflow was removed in v2.13.0 (routing) and v2.13.1 (files), ADR-16; current entry: [migration guide](../../docs/paired-session-migration.md).

## 给 owner 的摘要

**owner 已裁定 ready（D-READY，2026-10-05）。** 原话："我觉得已经跑了很多了，bob和tools两个repo这两天一直在跑，也在ship 工作，而且一周前还遇到过额度用完 hold，reset了继续的情况，所以我觉得已经算ready了"。

supervisor 的解读（作为解读记录）：
- ADR-6 的替换标准按现场证据视为**满足**：poker-news-bot 和 poker-tools 这几天一直在用 paired-session 跑真实工作并交付；owner 记得的那次额度用完事后查到了（`~/3Cats/poker-news-bot/.compass/results/2026-09-24_ab_pipeline/workitems/ADR_EVIDENCE_quota_resume_20261005.md`）：是 Codex 额度，不是 Claude；新流程从中断中恢复并跑完的证据是 WI-86（第一次因 Claude OAuth 过期 HOLD，第二次因 14400 秒超时及 claude_plugins 相关中断 HOLD；两次恢复，最终 DONE）；Codex 额度那次 run 在 reset 后续上了，但失败重试耗光了调用上限，这两个教训 v2.11.0 都已修。owner 看过证据后维持裁定（“我的结论不变, ready to ship”）。
- M7 不再卡退役，改为可选的成本/质量研究。
- legacy 从 v2.12.0 起宣布 deprecated：显式选择 legacy 时打印一行提示，路由和行为都不变。
- 代码删除要等三件事，确保不丢失只有 legacy 能做的事：
  1. review-pr 移植（D-LG2；lg2-design 停放，等你的第 4 轮复审）；
  2. code-quality-loop（Q6 memo，等你决定）；
  3. legacy 映射表里 18 个 owner 行。
- 记录在 `DECISIONS.md` ADR-6 的 Amendments（D-READY）；§2 的状态列已据此更新，§4 只列删除前提。

**owner 的后续决定（D-OWNER-1005，2026-10-05）：** owner 回答了决策表的全部 59 题，记录在 `DECISIONS.md` ADR-13。
- legacy map 的 18 行：13 行 keep、5 行 retire，**全部是暂定**，每行在对应工作项开始时再和 owner 确认。
- Q6 选 A：code-quality-loop 退役到 `run --review-only` + POLISH-Q。
- review-pr 设计的第 4 轮 review 已授权。
- D10 的回答已照录；D-READY 之后它们不再影响退役。
- 删除前提的最新表格在 `docs/paired-session-migration.md`（"Deprecation status"）。

**裁定之前的证据状态**（保留作记录；按当时的 ADR-6 逐条评估）：
- **第 (3) 条已满足：** 一流的用户验收阶段已经实现，accept/reject 都要先出 intent 再按 expect 执行。
- **第 (2) 条部分满足，连续计数已归零：**
  - ws7–ws10 曾是 4 次连续零 coordinator 缺陷的真实 run（都经默认入口并 ACCEPTED，其中一次是 review-only）。
  - 但在已发布的 v2.11.1 上，supervisor 并行启动了 ws11 和 ws12。ws11 因 coordinator 缺陷 FIELD-22 HOLD：并发的 ws12 的 Codex turn 往共享的 `~/.codex/config.toml` 追加了自己的 trust 条目，而且 `--acknowledge-codex-trust` 无法恢复。所以 FIELD-22 修复之后的连续计数是 **0**。
  - ws12（review-only，多文件）本身没有 coordinator 缺陷：
    - reviewer 和 shadow 首轮就发现了植入的 bug，第 2 轮修好。
    - 之后 POLISH 阶段的 specialist 提出了浮点边界问题，EXEC 因此重开。作者的修复先引入了溢出（r3），修溢出时又引入了一个真正的浮点边界回归（r4）。
    - 这个回归被 r4 的 shadow 抓住。run 在 EXEC 上限（4 轮）HOLD，没有把已知的 MAJOR 交付出去。
    - 因此有一个问题请你决定：review-only 的 EXEC 上限 4 轮是否太低。
  - 这些 run 都是小型 toy 任务，由 supervisor 的 headless driver 驱动，每次 run 后还要手工清理 Codex trust 条目。
  - overseer 成本现已测量（§3.2a）：每个 run 的 overseer 花费 $0.91–1.04（标价），占 Claude 侧花费的 48–52%，占输入 token 的 52–69%（绝大部分是缓存读）。
- **第 (4) 条部分满足：** 已覆盖 poker-tools 以外的仓库（poker-news-bot）；"大任务"还没有定义；本文档没有找到真实订阅限额 HOLD 加 resume 的记录，只有离线证据（owner 后来证实约一周前发生过一次，见上）。
- **第 (1) 条尚未满足：** M7 已有 24 个冻结案例和评分工具，但工具还差一处修复，D-b1 scanner 停在 review 上限，计分 run 还没开始。

裁定之前的结论是"还不能退役"；D-READY 之后，退役不再等这些标准，只等 §4 的删除前提。legacy 的功能映射：49 项中 covered 27、planned 4、owner 18。

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

None of the amendments above changes the four retirement criteria. The owner ruling D-READY (2026-10-05, ADR-6
"Amendments" in `DECISIONS.md`) does: it treats the gate as met on field evidence (summary above), makes M7 optional,
and leaves code removal to the preconditions in §4.

## 2. Status per criterion

"MET by owner ruling" means met under D-READY on field evidence. The status this memo had assessed before the ruling
is kept in the Evidence column.

| Criterion | Status | Evidence |
|---|---|---|
| Default-entry switch: 1D go and the parity decision | **MET** | ADR-11 + E-12 (D12 + D-2 + W2a are the parity decision); v2.10.0 `c8336e7` (tag) made paired-session the default entry |
| Default-entry switch: revised E-1 evidence and the E-12 residual | **E-1 MET; residual OWNER** | Revised E-1 is met. Gate run `6cd57da7` was ACCEPTED. CI run 37252072913 on the release commit rc4 = `c8336e7` concluded success, macOS jobs 64/64 and 0 non-success jobs (supervisor `gh run view`, 2026-10-05). The normal-shell residual (§1) is open; the gate runs were started by a headless driver. |
| (1) Seeded regressions, new no worse than old | **No longer a gate (D-READY)** | Before the ruling: NOT MET. M7 is now an optional cost and quality study. M7 (`docs/history/m7-seeded-defect-comparison.md`, `docs/history/m7-execution-plan.md`): corpus ready, no scored run (§3.4). One anecdote: gate run `a90a9e54` found and fixed a seeded bug, with no legacy control. |
| (2) Three consecutive zero-defect real runs, limited overseer, overseer cost measured | **MET by owner ruling** | Field evidence: poker-news-bot and poker-tools ran and shipped real work on paired-session over the preceding days. Before the ruling: PARTIAL. The series was 4 (ws7–ws10), but ws11 on the released v2.11.1 hit a coordinator defect (FIELD-22, §3.2b). The count after the FIELD-22 fix is 0. (Counting FIELD-21 in consumer use had already cut the series to 2 runs, with only ws10 on the fix.) The cost clause is MET: the overseer's tokens and cost are measured and added to per-item cost (§3.2a). The overseer limit is not met (§3.2). |
| (3) First-class user-acceptance feedback phase | **MET** | `accept` / `reject` with `--intent-only` digest then `--expect`; `note` and `--scope-change`; DONE = acceptance pending (`docs/protocol/paired-session-entry.md` "DONE and acceptance"). Accept exercised in the 4 gate runs and in consumer runs. Reject reopens EXEC; it is covered by tests, and no gate run used it. |
| (4a) A repo other than poker-tools | **MET** | poker-news-bot ab_pipeline: many paired-session runs since 2026-09-24, including v2.10.0+ runs `runs210/WI-101`, `WI-102` and `runs211/WI-103` (DONE; direct CLI, lifecycle off) |
| (4b) One large task | **MET by owner ruling** | The ruling counts the field work in both repositories. Before the ruling: OWNER. Not defined in ADR-6. A definition is proposed in §3.6. No gate run would qualify; the largest consumer work items are near the proposed size but ran with lifecycle off. |
| (4c) Real subscription-limit HOLD followed by a successful resume | **MET by owner ruling** | Located after the ruling (`~/3Cats/poker-news-bot/.compass/results/2026-09-24_ab_pipeline/workitems/ADR_EVIDENCE_quota_resume_20261005.md`): no Claude limit stop exists 2026-09-20..10-05; the recalled event was a Codex limit. Resume to DONE after a real interruption: WI-86 (v2.9.4; Claude OAuth expiry, not a limit). Real Codex limit stop on the pre-release spike coordinator (09-22): it resumed after the banked reset, but failed retries had spent the call cap and the run was halted. Both lessons are fixed in v2.11.0 (`c70591d`): it immediately HOLDs a provider rate-limit rejection with `hold_kind: rate_limited` in every role and stage, unless another guard wraps the error; rate-limited calls consume neither the invocation cap nor stage budgets (offline tests only; no paired-session limit HOLD on v2.11.0+ yet). The owner kept the ruling on this evidence. |

## 3. Evidence available today (2026-10-05)

Verified directly, read-only:
- git tags and commits;
- the run-dir `state.json` / `usage.md` of the gate runs and of the consumer runs;
- the `state.json`, receipts and turn logs of ws11 and ws12 (§3.2b);
- the documents in this tree.

Taken from records:
- the supervisor log (attempt history, CI runs, times, FIELD-21);
- the supervisor's FIELD-22 diagnosis (which run appended the trust entry; §3.2b);
- lane A's M7 results (§3.4).

### 3.1 Releases
- **v2.10.0** `c8336e7` (tag): default entry, worktree lifecycle, efficient mode.
- **v2.10.1** `5b9b8bb` (tag): long test commands polled to completion (FIELD-20, `2726498`).
- **v2.11.0** `fc5d5f1` (tag): review-only entry (D-LG1), `--detach`/`stop`, typed rate-limit HOLD, F7.
- **v2.11.1** `99f0577` (tag; pinned `~/paired-runs/review-loop-v2.11.1`): FIELD-21 `1adb353`, f3 `1121a4b`.

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
  ws11 then broke the series (§3.2b).
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
| ws7 `6cd57da7` | 1,488,722 (1,430,318) | 9,549 | 0.944 | 679,708 (512,678) | 15,465 | 0.864 | 2,168,430 | 25,014 | 1.808 | 69 % / 52 % |
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

### 3.2b Two parallel runs on the released v2.11.1 (ws11, ws12)

Both runs started at 17:49 on 2026-10-05, in parallel. Each was launched by a supervisor headless driver through the
default entry, on the pinned v2.11.1. Both ran with lifecycle on in efficient mode: Codex author, Claude reviewer,
Codex gate, max EXEC rounds 4. Run dirs (read-only): `~/.local/state/review-loop/runs/<id>`.

**ws11 `2110ada2`** (fresh multi-file task: `csv_stats.load_column` + `stats.describe`): **HOLD, coordinator defect.**
- PLAN author and reviewer finished. The EXEC author turn then ended with "codex turn changed global config:
  codex_config" (unexpected-content-change); 3 invocations.
- Cause, FIELD-22, assigned to lane B: during ws11's Codex turn, the concurrent ws12 run's Codex turn appended its own
  workspace trust entry to the shared default `~/.codex/config.toml`.
- The guard accepts only the turn's own workspace trust append, and `--acknowledge-codex-trust` cannot recover this
  case.
- This resets criterion (2): the count after the FIELD-22 fix is 0.
- It also shows that two runs sharing one `CODEX_HOME` are not safe today (see `concurrent-runs.md`: one absolute
  `CODEX_HOME` per concurrent run).

**ws12 `70804fc3`** (review-only, multi-file dirty change, seeded bug: `bins()` drops the maximum value): **HOLD at the
EXEC round limit, no coordinator defect.** 18 turns.
Turn sequence numbers are in brackets; the ledger is in `state.json` `finding_ledger`.
1. EXEC r1 [1–2]: reviewer and shadow both found the seeded bug (F001, F003 MAJOR).
2. EXEC r2 [3–6]: the author fixed it, and the reviewer approved.
   - The gate returned `needs-attention` with two MEDIUM findings: F007, range overflow and width underflow; F008,
     decimal-edge rounding.
   - MEDIUM does not block, so the run went on to FINISH [7].
3. POLISH-Q [8–11]: four specialists. The pr-test-analyzer raised the interior-edge float case as F015 MAJOR (the same
   area as F008), which reopened EXEC.
4. EXEC r3 [12–15]: the author changed the index formula to `(v - low) * n / span`. That fixed F015 and introduced an
   overflow on large finite inputs (F017 MAJOR, raised by the r3 reviewer).
5. EXEC r4 [16–18]: the author fixed F017 with `(offset / span) * n`, the form the r3 reviewer suggested. That
   reintroduced a float-boundary error: F025 MAJOR, `bins([0, 1, 49], 49)` puts 1 in bin 0.
   - The r4 reviewer approved, but the r4 shadow caught F025, so the effective verdict was REVISE.
   - The run was at the cap of 4 EXEC rounds and HOLDed with F025 open.

Interpretation:
- The review roles did their job: the seeded bug was found in round 1, and a regression from an author fix was caught
  before delivery.
- The HOLD kept a known MAJOR from shipping. An operator acceptance despite the round limit (RLO) would have
  delivered it.
- The run did not converge in 4 EXEC rounds on a numeric task:
  - In review-only, round 1 reviews the existing change, so the author had 3 fix turns (r2–r4).
  - The POLISH-Q reopen used rounds from the same cap.
  - Each fix of a float edge opened the next one: overflow, then rounding. The gate's MEDIUM F008 had flagged the
    area already in r2.
- The run was not continued (quota).
- Owner question (§4, owner 9): is `max_exec_rounds` 4 too low for review-only runs, given round 1 is spent on the
  existing change and polish fixes share the cap? `max_exec_rounds` is part of the run's frozen configuration. Options:
  - keep 4: non-convergence is information, and the HOLD hands the decision to the operator;
  - default review-only to 5;
  - count POLISH-Q fix rounds separately.

  The memo makes no recommendation from a single run.

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

Answered: D10 (D-OWNER-1005): the proposed floor, with the owner naming the next qualifying item.

**Already-finished lifecycle-off items:** none of the poker-news-bot items above passed through every stage (they ran
with lifecycle off), so under the proposal they do not count. Whether such an item may count is part of the owner's
answer.

## 4. What remains: removal preconditions (after D-READY)

Legacy is deprecated from v2.12.0. Choosing it prints a one-line notice; routing and behavior do not change. Its code
is removed only after all three preconditions hold, so that nothing only legacy can do is lost. The table with the
legacy-map rows is in `docs/paired-session-migration.md` ("Deprecation status").

1. **review-pr ported** (D-LG2). The owner authorized design round 4 and answered Q-R1..Q-R10 (D-OWNER-1005 D03); the
   design review and the port are still to be done.
2. **code-quality-loop retired** onto `run --review-only` + POLISH-Q. Decided by D09 = A (D-OWNER-1005); the
   retirement and the writer port (capability 1, provisional with L117) are still to be done.
3. **The 18 owner rows of the legacy map**. They were answered on 2026-10-05: D-OWNER-1005, `DECISIONS.md` ADR-13; 13
   keep and 5 retire, all provisional. Each "keep" row needs a paired-session equivalent, or the owner re-confirms
   it as retire; each "retire" row needs a re-confirmation, and L133 (Linux) also a real Linux paired-session run before
   the host check opens. L89 (the stage A fallback) conflicts with removal itself and is resolved at re-confirmation.
   Every row is confirmed with the owner again before its work item is implemented. The work items are listed in `docs/paired-session-migration.md` ("Deprecation status").

Then a final check: each legacy capability is covered or explicitly dropped. Only then are the legacy entry and its
last users (`entry: legacy`, `/review-loop:legacy`, `plan`, `execute`) removed, with the owner's go.

**Open, but no longer gating removal:**
- FIELD-22 (lane B): a concurrent run's trust append to a shared Codex config HOLDs another run (§3.2b). It is a
  coordinator defect to fix in its own right; the three-run streak is no longer required.
- The review-only EXEC cap (§3.2b): keep `max_exec_rounds` 4, raise the review-only default, or count POLISH-Q fixes
  separately.
- M7 (optional): if the owner authorizes it, it measures cost and quality against a pinned legacy copy. Lane B's
  `fresh_commit` fix applies; the D-b1 scanner is redesigned (D04), then gets 3 review rounds; scored runs follow D04
  and D05 (D06).
- The large-task definition (§3.6) and the E-12 normal-shell residual (§1): answered (D-OWNER-1005 D10: the size floor
  with an item the owner names; run E-12 once); not gating.
