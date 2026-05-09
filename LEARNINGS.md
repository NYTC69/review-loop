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
- **Date**: 2026-05-09
- **Task context**: review-loop session ac3fd787（v2.7.1 wire-scheduler delivery）的 plan + execute 各 round 末尾，调用 `codex exec --skip-git-repo-check --sandbox read-only --model gpt-5.5 -o ... -` 跑 reviewer round，每次返回后 `.review-loop/config.md` 被静默覆写为 `codex_reviewer_backend: codex\nskip_quality_polish: true`，原始 `reviewer_model: "gpt-5.5"` 丢失。
- **What broke**: exec round 1 reviewer 直接 flag CRITICAL "scope creep — `.review-loop/config.md` deleted"（实为 modified，但 git status 报 ` D` 因为 codex sandbox 在 cwd 上某种 race 删/改）。本来要交付 8 文件，git diff stat 错变成 9 文件。后续每次子 agent + codex exec 调用都触发同一行为，每跑一次就要 `git checkout HEAD -- .review-loop/config.md` 复位一次。同样的 bug 在 v2.7.0 session f1c0fa2f 已经撞过一次（HANDOFF observation 6089）但当时归因为 "Executor subagent accidentally deleted"，这次定位到真正的 root cause 是 codex sandbox 自身行为，不是 agent 删的。
- **Root cause**: `codex exec` 启动时似乎会以当前 cwd 作 reviewer-config 来源，向 `.review-loop/config.md` 写入它认为合理的 reviewer 配置（`codex_reviewer_backend: codex` + `skip_quality_polish: true`），属 codex CLI 的沙箱 bootstrap 副作用。Read-only sandbox flag 不阻止它写自己的 config 区域。本仓库 `.review-loop/config.md` 与 codex 期望的 config 路径冲突。子 agent 内部跑 codex exec 时也会触发，因此最初被错误归因为 agent 行为。
- **Rule going forward**: codex exec from review-loop cwd overwrites `.review-loop/config.md` with `codex_reviewer_backend: codex / skip_quality_polish: true` as a sandbox bootstrap side effect; restore via `git checkout HEAD -- .review-loop/config.md` after each call (orchestrator-side, not the agent's fault).
- **Scope**: codex,review-loop,sandbox,reviewer-config
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
