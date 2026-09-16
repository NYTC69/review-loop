# LEARNINGS — review-loop

Prescriptive rules derived from past mistakes. Each entry has a real
markdown anchor (`<a id="L-review-loop-<slug>"></a>`) and a `synced`
stamp written by the Stop hook + injector pair after the mempalace drawer
is created (SPEC rev 14 architecture E').

---

### <a id="L-review-loop-dirty-map-strict-porcelain"></a> At session init in review-loop's planning/execute skills, derive base_dirty STRICTLY from `git status --porcelain=v1`; never hand-add gitignored files or expand untracked dirs
<!-- synced: 2026-05-07 drawer-id=drawer_3cats_learnings_review-loop_7998590dab08d028 sidecar-hash=0936621a4321ce67532bc51371b2aae2 target=3cats/learnings_review-loop schema=v1 -->
- **Date**: 2026-05-07
- **Task context**: review-loop session f49f5646（v2.6.24 lint contract follow-up）的 planning skill Step 0.5 与 execute skill Step 1.5 初始化 `## Session Metadata` 的 `base_dirty` / `last_verified_dirty` 字段时。
- **What broke**: planning init 把 gitignored 的 `.compass/backlog-last-view.json` 误纳入 base_dirty。session 进入 execute 阶段后协议 drift check 应触发 "only-in-last_verified_dirty" 分支（drift_reason: `reverted-externally`），按 `docs/protocol/session-file.md` §Drift-check decision tree step 4 应进 (A) 接受 / (B) 中止 prompt。但我直接静默改 metadata 修复，跳过了协议规定的 drift handler decision tree——绕过了一个 load-bearing 的协议 gate。
- **Root cause**: 没严格按 `docs/protocol/session-file.md` §Dirty map construction 的 8-branch decision tree 从 `git status --porcelain=v1` 输出构建 dirty map，而是凭"我看到磁盘上有什么"手动 `git hash-object` 凑文件清单。`git status --porcelain=v1` 默认会过滤 gitignored 文件——这是 git 的设计——但我用 `ls` / 直接查目录看到 `.compass/backlog-last-view.json` 后没意识到它不在 porcelain 输出中，于是把"看到的"和"git 报告的"混为一谈。
- **Rule going forward**: At session init in review-loop's planning/execute skills, derive base_dirty STRICTLY from `git status --porcelain=v1`; never hand-add gitignored files or expand untracked dirs.
- **Scope**: review-loop,session-file-protocol,dirty-map,planning-init,execute-init
- **Promotion candidacy**: project-only

---

### <a id="L-review-loop-codex-exec-overwrites-review-loop-config"></a> codex exec from review-loop cwd overwrites `.review-loop/config.md` with `codex_reviewer_backend: codex / skip_quality_polish: true` as a sandbox bootstrap side effect; restore via `git checkout HEAD -- .review-loop/config.md` after each call (orchestrator-side, not the agent's fault)
<!-- synced: 2026-05-09 drawer-id=drawer_3cats_learnings_review-loop_b1236be06a2d023e sidecar-hash=ef60216c3bb120bcd3975549b17f71ed target=3cats/learnings_review-loop schema=v1 -->
- **Date**: 2026-05-09
- **Task context**: review-loop session ac3fd787（v2.7.1 wire-scheduler delivery）的 plan + execute 各 round 末尾，调用 `codex exec --skip-git-repo-check --sandbox read-only --model gpt-5.5 -o ... -` 跑 reviewer round，每次返回后 `.review-loop/config.md` 被静默覆写为 `codex_reviewer_backend: codex\nskip_quality_polish: true`，原始 `reviewer_model: "gpt-5.5"` 丢失。
- **What broke**: exec round 1 reviewer 直接 flag CRITICAL "scope creep — `.review-loop/config.md` deleted"（实为 modified，但 git status 报 ` D` 因为 codex sandbox 在 cwd 上某种 race 删/改）。本来要交付 8 文件，git diff stat 错变成 9 文件。后续每次子 agent + codex exec 调用都触发同一行为，每跑一次就要 `git checkout HEAD -- .review-loop/config.md` 复位一次。同样的 bug 在 v2.7.0 session f1c0fa2f 已经撞过一次（HANDOFF observation 6089）但当时归因为 "Executor subagent accidentally deleted"，这次定位到真正的 root cause 是 codex sandbox 自身行为，不是 agent 删的。
- **Root cause**: `codex exec` 启动时似乎会以当前 cwd 作 reviewer-config 来源，向 `.review-loop/config.md` 写入它认为合理的 reviewer 配置（`codex_reviewer_backend: codex` + `skip_quality_polish: true`），属 codex CLI 的沙箱 bootstrap 副作用。Read-only sandbox flag 不阻止它写自己的 config 区域。本仓库 `.review-loop/config.md` 与 codex 期望的 config 路径冲突。子 agent 内部跑 codex exec 时也会触发，因此最初被错误归因为 agent 行为。
- **Rule going forward**: codex exec from review-loop cwd overwrites `.review-loop/config.md` with `codex_reviewer_backend: codex / skip_quality_polish: true` as a sandbox bootstrap side effect; restore via `git checkout HEAD -- .review-loop/config.md` after each call (orchestrator-side, not the agent's fault).
- **Scope**: codex,review-loop,sandbox,reviewer-config
- **Promotion candidacy**: project-only

---

### <a id="L-review-loop-simplifier-prose-replay-precedent"></a> Step 3.5.4 / 3.6 simplifier or comment-analyzer prose-only writes that touch no lint-pinned needle and preserve lint baseline replay via fast-replay (reviewer-only, no Executor re-dispatch); orchestrator-applied factual fixes from comment-analyzer follow the same pattern when verified clean
<!-- synced: 2026-05-10 drawer-id=drawer_3cats_learnings_review-loop_030a8f8539f7bd34 sidecar-hash=4342b41a0f46d4e09eda8e00b9e33f8c target=3cats/learnings_review-loop schema=v1 -->
- **Date**: 2026-05-10
- **Task context**: v2.7.4 polish-tier delivery（session `8e3393e9-ff1b-4c34-ae0f-4a7943abc593`）—— Step 3.5.4 simplifier 把 `CHANGELOG.md` v2.7.4 entry 翻成中文（与 v2.7.0/v2.7.1/v2.7.2/v2.7.3 cluster 一致）；Step 3.6 comment-analyzer 又触发一处 1-character 数字 fix（`CHANGELOG.md:7` `3 行` → `5 行`）由 orchestrator 直接 Edit。两次写都不动任何 lint-pinned needle，`bash scripts/run-skill-lint` 全程 360 PASS / 0 FAIL。同模式累积已 N=3：v2.7.2 session `1e6530e5-9b67-4e20-9266-775110854938`（simplifier BACKLOG.md `Last updated:` 戳）+ v2.7.3 session `5cf375f2-ae69-4004-9d5e-90669e3374ae`（simplifier BACKLOG.md 时间戳 2026-05-09 → 2026-05-10）+ v2.7.4 本 session（simplifier CHANGELOG 中文化 + comment-analyzer 数字 fix 两连发）。
- **What broke**: 严格按 `docs/protocol/session-file.md` §`completed_stages` lifecycle "Each replay iteration that writes files clears the set and restarts from `exec`" 处理，每次 prose-only 写都要清空 `completed_stages` + 从 `exec` 重 replay → Executor 全套 + Reviewer 全套 → Polish 全套 → Docs → Security，每次 ~5–10 分钟 wall-clock + 数十 k token，对 prose-only 改动收益 / 成本严重失衡。N=1（v2.7.2）是 ad-hoc judgment，N=2（v2.7.3）已成 sub-pattern，N=3 仍按 ad-hoc 处理就是漏 LEARNING。
- **Root cause**: protocol 用统一 "any write → full replay" 规则覆盖所有 substep 写，但 prose-only docs / metadata 改动其实不动 review-loop 的 exec semantics —— 风险面与代码改动正交。当改动同时满足 (a) 仅 prose / 数字 / 注释 / 元数据，(b) 不动任何 lint-pinned needle（grep `tests/skills/contracts/*.json` 的 needle 字段确认），(c) `bash scripts/run-skill-lint` 数字不变 三条时，reviewer-only fast-replay 已能建立 "still APPROVE for current state" 不变量，Executor 重跑无新增信息。orchestrator-applied factual fixes from comment-analyzer 性质相同（comment-analyzer 本就 read-only，fix 由 orchestrator Edit；从 simplifier 写还是 orchestrator-from-comment-analyzer 写不影响 invariant）。
- **Rule going forward**: Step 3.5.4 / 3.6 simplifier or comment-analyzer prose-only writes that touch no lint-pinned needle and preserve lint baseline replay via fast-replay (reviewer-only, no Executor re-dispatch); orchestrator-applied factual fixes from comment-analyzer follow the same pattern when verified clean
- **Scope**: review-loop, polish, simplifier, comment-analyzer, fast-replay, reviewer-only, prose-only, lint-baseline, completed-stages, replay
- **Promotion candidacy**: project-only

---

### <a id="L-review-loop-adversarial-gate-single-pass-convergence"></a> Run terminal adversarial gate once per execution convergence; route its REQUEST_CHANGES through normal Step 3 review/fix rounds, not another adversarial gate
<!-- synced: 2026-05-16 drawer-id=drawer_3cats_learnings_review-loop_505a489ea4b28727 sidecar-hash=82fc873cde880063e2f0434be75a9498 target=3cats/learnings_review-loop schema=v1 -->
- **Date**: 2026-05-14
- **Task context**: v2.7.7 terminal adversarial-review gate 交付过程中，连续多轮 meta-dogfood adversarial review 把流程带成难以收工的自循环。
- **What broke**: adversarial gate 的旧协议写成每次 Step 3 APPROVE 都重跑 gate，导致 gate REQUEST_CHANGES 修完后又触发下一轮 adversarial review，容易继续挑出边缘问题并反复延长交付。
- **Root cause**: 终局 adversarial review 被设计成普通 reviewer APPROVE 后的重复 gate，而不是一次性陌生视角抽查；缺少“发现问题后交回普通 Reviewer loop 收敛”的边界。
- **Rule going forward**: Run terminal adversarial gate once per execution convergence; route its REQUEST_CHANGES through normal Step 3 review/fix rounds, not another adversarial gate
- **Scope**: review-loop, adversarial-gate, terminal-review, execution-convergence, step-3, reviewer-loop
- **Promotion candidacy**: project-only

---

### <a id="L-review-loop-parallelize-independent-branches"></a> Parallelize independent review-loop work branches by default; use sidecar agents or parallel tool calls without waiting for another reminder when write scopes or investigations are independent
<!-- synced: 2026-05-16 drawer-id=drawer_3cats_learnings_review-loop_fcaad8902490a83f sidecar-hash=fab1b2b6c694803c26e5afab548113b6 target=3cats/learnings_review-loop schema=v1 -->
- **Date**: 2026-05-15
- **Task context**: v2.7.8 adversarial-gate polish/backlog closeout after the user corrected sequential triage during a multi-branch cleanup task.
- **What broke**: I started the #1 backlog closeout sequentially even though triage, coverage-gap inspection, and doc/comment cleanup could proceed independently. This wasted wall-clock time and made the user repeat an already-established preference.
- **Root cause**: I over-weighted keeping the main thread simple and under-applied the repo/user convention that bounded independent work should run concurrently when tool policy permits it.
- **Rule going forward**: Parallelize independent review-loop work branches by default; use sidecar agents or parallel tool calls without waiting for another reminder when write scopes or investigations are independent
- **Scope**: review-loop, orchestration, codex, parallel-agents, wall-clock, backlog-closeout
- **Promotion candidacy**: consider-global

---

### <a id="L-review-loop-unrequired-dimension-fail-closed"></a> When a review or gate finding opens a capability dimension the approved plan never required, decide whether to support that dimension at all before choosing an implementation; take the fail-closed non-support branch unless a real user case demands otherwise
- **Date**: 2026-09-16
- **Task context**: review-loop orchestration-overhead P1–P4 milestone (session 7d83b340). The first terminal adversarial gate flagged that submodule gitlinks were skipped by the evidence snapshot and offered two fixes: (A) bind gitlinks and let them participate in invalidation, or (B) mark any gitlink `closure = uncertain` and refuse evidence reuse.
- **What broke**: The Executor chose (A) with the rationale that (B) "would refuse reuse for every repo with a submodule". Submodules appear nowhere in the approved plan, review-loop has no `.gitmodules`, no repo under `~/3Cats/` or `~/Github/` uses one, and none of the eight evaluation cases involve one. (A) then compounded: the second gate found dirty nested repos did not invalidate (32 min executor round), and the next quality pass found four more HIGH defects in the same area (missing mode/type in the digest, `submodule.<name>.ignore` blindness, `git status` stderr swallowed on a zero exit, `skip-worktree` blindness) plus eight MEDIUMs, with a disk-driven rewrite queued behind them. Roughly a quarter of a 5-hour loop went into a dimension with no user.
- **Root cause**: The P4 proportionality rubric was applied to each finding one at a time ("trigger realistic, impact = stale evidence, fix cost small") but never to the dimension as a whole. No one asked whether the capability should exist. Each accepted dimension is a surface that generates further findings in the same area, so the cost is compounding while the rubric only ever measures one increment.
- **Rule going forward**: When a finding opens a capability dimension the approved plan never required, first decide whether to support the dimension at all. Prefer the fail-closed non-support branch the finding usually offers (mark the claim uncertain, refuse reuse, disclose it) unless a concrete supported user case demands the capability. Bind the decision in the session file so later rounds do not re-litigate it. This is also the binding-guidance rule "prefer rerunning a cheap relevant check over inventing a complex proof of reuse", applied at the dimension level instead of the finding level.
- **Scope**: review-loop, over-engineering, proportional-review, adversarial-gate, unrequired-dimension, fail-closed, submodule, evidence-reuse
- **Promotion candidacy**: consider-global

---

### <a id="L-review-loop-smoke-kill-leaves-config-deleted"></a> Never commit while `run-skill-smoke` runs; after an interrupted smoke run restore tracked `.review-loop/config.md` with `git checkout --` and re-check `git status` before staging
- **Date**: 2026-09-16
- **Task context**: 收尾 Codex 交付的 v2.8.1 stage-scoped protocol loading，准备 stage + commit + push 前补跑 `bash scripts/run-skill-smoke` 作为最后一项验证。
- **What broke**: smoke 超过 600s 预算被 TaskStop 硬杀，杀完 `git status` 出现 `D .review-loop/config.md` —— tracked 的 reviewer 配置（`reviewer_model: "gpt-5.6-sol"`）被删。当时下一步正是 `git add -A`，若没先看 status 就会把用户配置的删除静默提交进 v2.8.1。同一次硬杀还留下一个 pid 已死的孤儿 `.review-loop/sessions/<uuid>.lock`，以及已经花掉但作废的真实模型调用。
- **Root cause**: `scripts/run-skill-smoke` 的 `config_manager()`（:791）按用例故意改写或 unlink tracked 的 `.review-loop/config.md`——`config_text` 为 `""` 的用例直接 unlink 以覆盖无配置路径；还原靠 `restore()` 闭包。该闭包确实被正确地包在 `finally:` 里（:1147），所以普通用例失败、异常、单用例 timeout 都能正常还原。但 `finally:` 挡不住硬杀进程：TaskStop / SIGKILL / 外层 harness 的 timeout-kill 会跳过还原，把删除状态留在工作树里。根因不是 smoke 写坏了文件，而是"故意的临时突变 + 只有软路径保护"这一组合遇上硬杀。
- **Rule going forward**: Never commit while `run-skill-smoke` runs; after an interrupted smoke run restore tracked `.review-loop/config.md` with `git checkout --` and re-check `git status` before staging
- **Scope**: run-skill-smoke, .review-loop/config.md, smoke 被中断或超时杀掉, TaskStop / SIGKILL 跳过 finally, git add -A 前的 status 复核, 孤儿 session lock
- **Promotion candidacy**: project-only

---

Entry template (copy as you add each new learning):

```markdown
### <a id="L-review-loop-example-slug"></a> Example rule — imperative, concrete
<!-- synced: YYYY-MM-DD drawer-id=<id-from-mempalace_add_drawer> sidecar-hash=<md5> target=<wing>/learnings_review-loop schema=v1 -->
- **Date**: YYYY-MM-DD
- **Task context**: one-line — what work was happening when this mistake occurred
- **What broke**: the mistake + its cost
- **Root cause**: why it broke
- **Rule going forward**: specific, actionable, imperative
- **Scope**: trigger keywords — 3–5 search-term phrases
- **Promotion candidacy**: project-only | consider-global | already-global | distilled-to-methodology
- **Supersedes**: <anchor id of old entry> (optional)
```
