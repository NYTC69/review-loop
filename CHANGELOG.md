# Changelog

## 2026-05-14

### v2.7.7 — 终局 adversarial-review gate (Step 3.4)

- 在 Step 3 reviewer APPROVE 之后、Step 3.5 quality polish 之前插入一次终局
  "陌生眼睛" review pass (Step 3.4 — Terminal Adversarial Gate)。双 runtime
  (Claude `skills/execute/SKILL.md` + Codex `.agents/skills/execute/SKILL.md`)
  对称记录, 共享 5 行 Bash dispatch:
  ```bash
  python3 scripts/adversarial_gate_invoke.py --focus-file "$focus_text_file"
  adversarial_exit=$?
  ```
- 新增 `scripts/adversarial_gate_invoke.py` — stdlib-only Python invoker,
  drain-thread + timeout pattern 是 `Scheduler._run_one in
  scripts/review_verification.py` 核心 pattern 的 faithful port; sentinel
  bytes 与 `wait_after_kill_timed_out` 诊断 flag 故意 NOT ported。
- 新增 `scripts/adversarial_gate_adapter.py` — 双 input-mode 翻译层 (raw +
  plugin-json), 同时支持 stdin 与 `--input <path>` 双 handoff;
  输出 review-loop verdict + bulleted issues。
- 新增 `scripts/adversarial_gate_fallback_prompt.txt` — fallback-path
  prompt 模板, `string.Template.safe_substitute` 渲染。
- 配置: 新增 `adversarial_gate_skip_paths` (默认 `["**/SKILL.md",
  "docs/protocol/**", "tests/skills/contracts/**"]`); 当 Step 3 changed-set
  全部命中跳过 Glob 时整轮 skip。
- Plugin-path preference + snapshot/restore: 优先走 `node
  $CODEX_PLUGIN_ROOT/scripts/codex-companion.mjs adversarial-review --scope
  working-tree --json` (JSON-RPC `runAppServerTurn` 不触发
  `.review-loop/config.md` 覆写 side-effect); 退到 `codex exec
  --output-schema` fallback path 时, 先 snapshot 再 restore 配置文件。
- 6 个 SKIP reason 带可选 `detail=`: `plugin-root-unresolved`,
  `cache-schema-unresolved`, `codex-unauthenticated`,
  `adapter-exit-2-malformed`, `runtime-error` (carries detail),
  `runtime-timeout`。
- 共享 `_kill_process_group` 同时驱动 timeout 与 signal 两条 cleanup 路径
  (kill child → restore config → exit 顺序), 避免 orphan child 在 restore
  后再次 mutate config。
- 加宽 auth-regex (`(?i)(?:unauthenticated|not signed in|login
  required|authentication|oauth|unauthorized)`) 捕获 `AuthenticationError`
  / `OAuth2` 等 concatenated 形式; FP risk 视为可接受 (两条分支都 SKIP,
  只 banner reason 不同)。
- `--scope working-tree`-only: 不带 `--base`, 否则 `git.mjs:resolveReviewTarget`
  会 short-circuit 到 branch diff 漏掉 working tree。
- Re-run 语义: 每次 Step 3 APPROVE 触发一次 gate; gate REQUEST_CHANGES 把
  findings 喂回下一轮 Step 3, 直到 pass / skip / `soft_limit_exec` cap。
- Codex Stage 1 outside-sandbox 要求 (mirrors reviewer / scheduler 调用
  注解): Python invoker 写 tempfile, read-only sandbox 会拦截。
- R6 反馈内联修复: signal-race during Popen assignment
  (`pthread_sigmask` block window), cleanup unlink-only-after-restore-OK,
  adapter-spawn OSError 测试, auth regex parametrized 4 sub-cases。
- Lint contracts 新增 12 个 `kind: contains` 断言, version-pin needles
  bump 4 处。Plugin v2.7.6 → v2.7.7。

## 2026-05-10

### v2.7.6 — protocol↔LEARNING fast-replay alignment

- Aligns `docs/protocol/session-file.md` and `docs/protocol/execution.md` with
  `L-review-loop-simplifier-prose-replay-precedent`: eligible Step 3.5.4 /
  Step 3.6 prose/comment/metadata-only writes can use reviewer-only
  fast-replay instead of forcing Executor re-dispatch, while code writes,
  lint-pinned changes, lint-baseline changes, security writes, accepted drift,
  and baseline backfills still clear `completed_stages` and replay from `exec`.
- Defines the conservative state machine explicitly: Step 3.5.4 fast-replay
  APPROVE preserves existing stages but does not mint `polish`; Step 3.5.6
  mints `polish` only after the full Step 3.5 invocation finishes cleanly with
  either no writes or only eligible writes already approved by reviewer-only
  fast-replay; Step 3.6 fast-replay APPROVE mints `docs`; fast-replay
  REQUEST_CHANGES fails closed to normal replay from `exec`.
- Mirrors the execution wording into both runtime skill surfaces
  (`skills/execute/SKILL.md` and `.agents/skills/execute/SKILL.md`) and adds
  `reviewer_only_fast_replay_consistent` lint coverage via
  `tests/skills/contracts/assertion-mapping.json`.
- Plugin v2.7.5 → v2.7.6. Lint baseline 369 → **370 PASS / 0 FAIL** (+1);
  `python3 -m unittest tests.run_skill_lint_test` 34/34 PASS.

### v2.7.5 — Codex marketplace plugin surface (visible in `/plugins`, parallel to Claude install)

- Goal: bring the Codex install/enable experience to compass parity. Before v2.7.5,
  `codex plugin marketplace add NYTC69/review-loop` registered a marketplace entry but no
  plugin surfaced in `/plugins` — fresh Codex sessions could not see review-loop and fell
  back to the legacy `~/.codex/skills/review-loop/SKILL.md` wrapper. Compass works because
  it ships `.agents/plugins/marketplace.json` plus a `plugins/compass` symlink; review-loop
  shipped neither.
- **`.agents/plugins/marketplace.json`** — new Codex marketplace manifest mirroring
  compass's pattern. Lists review-loop with `installation: AVAILABLE` /
  `authentication: ON_INSTALL` policy and source `{ source: "local", path: "./plugins/review-loop" }`.
  Marketplace name `review-loop-marketplace` matches the existing `.claude-plugin/marketplace.json`
  name so users see one consistent marketplace identifier across both runtimes.
- **`plugins/review-loop` symlink → `..`** — tracked in git as a real symlink (mode `120000`).
  Resolves the marketplace manifest's `./plugins/review-loop` path back to the repo root,
  letting Codex cache the plugin contents at
  `~/.codex/plugins/cache/review-loop-marketplace/review-loop/2.7.5/`.
- **Verified end-to-end**: with the new manifest in place,
  `codex exec -c 'plugins."review-loop@review-loop-marketplace".enabled=true' "list skills"`
  surfaces all four Stage 1 skills (`review-loop`, `review-loop:plan`, `review-loop:execute`,
  `review-loop:guide`) — confirming a fresh Codex session that enables the plugin via
  `/plugins` will get the same surface.
- **Docs**: new `docs/install-codex.md` covering prerequisites, `marketplace add` →
  `/plugins` enable flow, natural-language triggers (Codex 0.130 has no
  `plugin install`/`enable` CLI subcommand and does not recognise `/review-loop:*` slash
  commands), verification, and the Claude-vs-Codex plugin-surface boundary table.
  README's `## Codex Stage 1` section gains an `### Install in Codex CLI` subsection
  pointing at the new doc; `CLAUDE.md` gains a `### Codex marketplace surface` block
  documenting the three required files for future maintenance.
- **Lint contract +9** (`tests/skills/contracts/review-loop.json`):
  `plugin_version_pinned_codex_plugin_json`,
  `codex_marketplace_manifest_name`, `codex_marketplace_manifest_plugin_entry`,
  `codex_marketplace_manifest_source_local`, `codex_marketplace_manifest_source_path`,
  `codex_marketplace_manifest_installation_available`,
  `codex_install_doc_present`, `codex_install_doc_referenced_from_readme`,
  `codex_marketplace_manifest_referenced_in_claude_md`. Three existing `plugin_version_pinned_*`
  needles bumped `"2.7.4"` → `"2.7.5"` in lockstep with `.claude-plugin/plugin.json` and
  `.claude-plugin/marketplace.json` (`metadata.version` + `plugins[0].version`).
- Plugin v2.7.4 → v2.7.5. Lint baseline 360 → **369 PASS / 0 FAIL** (+9).
  `python3 -m unittest discover -s tests -p '*_test.py'` 187/187 PASS.

### v2.7.4 — Banner-parity polish-tier lint-mirror bundle

- v2.7.3 banner parity 的 polish-tier follow-up（drift audit gap-closure session `8e3393e9-ff1b-4c34-ae0f-4a7943abc593`，源 `.compass/results/2026-05-10_v273-banner-parity-followup-gaps.json`）：新增 **8 条** 每行 `kind: contains` lint records 静态守护 umbrella startup banner 在两个 runtime 的字段，并把 3 条已有 `plugin_version_pinned_*` needles 与版本号 lockstep 升至 `"2.7.4"`。封堵 v2.7.3 留下的不对称——Claude 侧 0 条 per-line records，Codex 侧仍有 5 行（top border / work-item / problem / mode / historical-context template）silent 未守护。
- **Claude umbrella +3**（target `skills/review-loop/SKILL.md:217-225`）：`claude_umbrella_startup_banner_section_header_declared`（`── review-loop: Starting ──` ASCII border）/ `claude_umbrella_startup_banner_reviewer_label_declared`（`Reviewer: {codex | subagent} ({reviewer_model})`，沿用 Claude 侧 `reviewer` 配置 key）/ `claude_umbrella_startup_banner_soft_limit_label_declared`（`Soft limit: {soft_limit_plan} (plan) / {soft_limit_exec} (exec)`）。
- **Codex umbrella +5**（target `.agents/skills/review-loop/SKILL.md:73-110`）：`codex_umbrella_startup_banner_top_border_declared`（同 `── review-loop: Starting ──` ASCII border，跨 runtime 共享 needle 但锚定到不同 path）/ `codex_umbrella_startup_banner_work_item_label_declared`（`Work item: {title}`）/ `codex_umbrella_startup_banner_problem_label_declared`（`Problem: {problem_description}`）/ `codex_umbrella_startup_banner_mode_label_declared`（`Mode: {interactive | handsfree}`）/ `codex_umbrella_startup_banner_historical_context_row_template_declared`（`Historical context: {N} relevant memories loaded`，可选行模板）。
- **版本号 lockstep +0/3**（target `.claude-plugin/`）：`plugin_version_pinned_plugin_json` / `plugin_version_pinned_marketplace_metadata` / `plugin_version_pinned_marketplace_plugins_first` needles 由 `"2.7.3"` 升至 `"2.7.4"`，与 `plugin.json:.version` + `marketplace.json:.metadata.version` / `.plugins[0].version` 同步。
- Plugin v2.7.3 → v2.7.4（parity-only patch tier；无 reviewer 派发或 protocol 行为变更）。Lint baseline 352 → **360 PASS / 0 FAIL**（+8）。
- BACKLOG P3 "v2.7.3 banner-parity follow-up gaps" 关闭。

### v2.7.3 — Codex umbrella startup-banner parity (post-v2.7.2 drift audit)

- 镜像 Claude umbrella `skills/review-loop/SKILL.md:218-225` 的 `── review-loop: Starting ──` 启动 banner 到 Codex umbrella `.agents/skills/review-loop/SKILL.md`：在 `## Runtime Identity` 和 `## Completed Agent Cleanup` 之间新增 `## Startup Banner` 节，5 行固定字段（work item / problem / reviewer backend / mode / soft-limit）+ 1 行条件 `Historical context`，跨运行时 UX parity 修复（drift audit `.compass/results/2026-05-10_cross-runtime-skill-drift-audit.json` `drift-finding-2-startup-banner`）。`Reviewer backend` 行使用 backend-appropriate label（`claude-cli ({reviewer_model | judgment_model | claude-sonnet-4-6})` vs `codex (review_loop_reviewer / {codex_reviewer_model})`），不复用 Claude 的 `reviewer` 配置 key 因为 Codex Stage 1 不用它选 backend。
- 同 banner 节内文字化历史上下文委托关系（companion #1，drift audit `drift-finding-1-historical-context`）：明确 Codex umbrella 不内联跑 Step 1.6，由 `.agents/skills/plan/SKILL.md` Step 1.6 负责 historical-context 拉取，resume-dedup 保证 end-to-end 1 fetch/session — 防止未来审计再次误判为 drift。
- 在 `skills/execute/SKILL.md` 与 `.agents/skills/execute/SKILL.md` 的 "On gate pass" item 2 后追加 Delivery Summary 中文 rendering 规则（companion #2，drift audit `drift-finding-3-delivery-summary-zhcn-shared-gap`）：把 `docs/protocol/execution.md §Step 4` 已有的 SSOT 规则显式写入两个 SKILL body，使其可被 lint 静态守护。文本两边 byte-identical，单条 needle 跨两个 record 复用。
- 新增 7 条 `kind: contains` lint records 到 `tests/skills/contracts/review-loop.json`：4 条 banner（section / reviewer-backend label / soft-limit label / print-once rule）+ 1 条 companion #1 委托说明 + 2 条 companion #2 中文 rule（每个 SKILL body 各 1）。3 条现有 `plugin_version_pinned_*` records 更新为 `"2.7.3"`。
- Plugin v2.7.2 → v2.7.3（parity-only patch tier；无 reviewer 派发或 protocol 行为变更）。Lint baseline 345 → 352 PASS / 0 FAIL，`tests/review_verification_test.py` 57/57 不变。
- BACKLOG P3 "Codex umbrella startup-banner parity" 关闭。

## 2026-05-09

### v2.7.2

- v2.7.1 wire-scheduler 交付的 polish-tier 跟进 bundle（BACKLOG P3，session `1e6530e5-9b67-4e20-9266-775110854938`）：新增 **48 条** lint 静态守护 + 显式文档化 1 个 best-effort smoke 已知 flake（`review-loop.regression.smoke.claude`），全部为 strict-expansion，零 review-loop 行为变更。
- **Item (a)** plugin 版本一致性 lint（+3）：`plugin_version_pinned_plugin_json` / `plugin_version_pinned_marketplace_metadata` / `plugin_version_pinned_marketplace_plugins_first` — 锚定 `.claude-plugin/plugin.json:.version` 与 `marketplace.json:.metadata.version` / `.plugins[0].version` 同步为 `"2.7.2"`，未来任意一处单独漂移会立即 FAIL，避免再发生 v2.7.1 cache miss 类故障。
- **Item (b)** runtime 双路标签 + reviewer-output 文件名模板（+9 = 3 SKILL × 3 needle）：在 `.agents/skills/{review-loop,plan,execute}/SKILL.md` 三处 `Parallel Reviewer Fan-Out (N>1)` 子节绑定 `runtime: "codex"` 与 `runtime: "claude_code"` 文案，以及两行 concat needle `per-job stdout is written to\n\`.review-loop/tmp/{session_id}-reviewer-output.{job_id}.txt\`.` 唯一锚定 runtime-split 出现位置（同模板在 cleanup 段重复出现，单行 contains 无法区分）。
- **Item (c)** Edit C precedence 规则（+12 = 3 SKILL × 4 needle）：trigger（多行 `\`error\` field is non-null, or \`timed_out\` is true, or\n  \`returncode\` is non-zero` — 文本在 `or` 后换行 + 2 空格缩进，单行 needle count=0）/ classification（`classify as a **command-execution failure**`）/ precedence-tail（`diagnostic fields take precedence over stream-json parse outcome.`）/ skip-parse（`Do not attempt to parse \`stdout\` for that entry`）。
- **Item (d)** Edit D ENOENT 清理纪律 + policy 子句（+9 = 3 SKILL × 3 needle）：scheduler-half（`Per-job prompt files are scheduler-owned and may already be unlinked`）/ orchestrator failure-type（`non-ENOENT failure to delete them`）/ 多行 policy clauses（`should be logged as a warning in \`## Review History\` but must not block\nthe round verdict.` — 单行 `must not block the round verdict` 因换行 count=0，多行是唯一稳定形式）。
- **Item (e)** `parsed_verdict` / `parsed_issues` "best-effort metadata only" caveat（+4 = 1 source + 3 mirrors）：源端 `scripts/review_verification.py:12-17` 模块 docstring 锚 `Stream-json parsing here is intentionally a *best-effort* duplicate of`；3 个 SKILL 镜像锚 `parsed_verdict\` / \`parsed_issues\` are best-effort metadata only`。
- **Item (f)** `_load_jobs` 10 字段 schema list（+10）：在 `scripts/review_verification.py` 中分别守护 `entry["session_id"]` / `entry["job_id"]` / `entry.get("runtime", "codex")` / `entry.get("prompt_text", "")` / `entry.get("reviewer_model", "")` / `entry.get("timeout_secs", 300.0)` / `entry.get("conflict_keys")` / `entry.get("capacity_keys")` / `entry.get("extra_argv")` / `entry.get("worktree")` 字面量；任意字段重命名 / 默认值漂移会立即 FAIL。
- **Item (g)** protocol forward-pointer anchor（+1）：`docs/protocol/planning.md` § Reviewer dispatch forward-pointer 句 `Parallel Reviewer Fan-Out (N>1)\` subsection in each` 与三个 SKILL 已守护字符串绑定，protocol → SKILL 链接保活。
- **Item (h)** smoke `review-loop.regression.smoke.claude` 文档化（plan Option B + 保留 Option A 的扩展）：plan 原 Option A 假设 root cause 是 renderer heading drift；执行阶段经实测 fixture artifact 与 `meta.json` 后定位到真实 root cause 是 `claude -p` 在 `setup.timeout_seconds: 600` 预算内无法 converge 完整 review-loop end-to-end，runner 退到 synthetic v2.6.0 fixture 作 best-effort fallback，导致 3 项 mode-any/mode-all assertion 同时失败（不止 plan 假设的 heading drift 一项）。**Option B**：新增 `tests/skills/smoke/README.md`，明确将本 case 列入 known-flaky `best_effort` 名单，并解释 root cause + 后续路径（提高 timeout 或迁移到非真 LLM smoke harness）；本变更满足 plan acceptance criterion `either fix or document`。**Option A 保留为无害扩展**：在 `tests/skills/contracts/assertion-mapping.json` 新增 smoke assertion `execution_round_recorded_parenthetical_review_only_form` 锚 `### Round 1 (Execution / review-only)` 并加入 `execution_round_recorded` mode-any group 第 5 个成员；当未来 timeout 预算允许真 run 产出该 heading 时，该 member 会让此条 mode-any 自动 PASS。`reviewer_round_1_recorded` group 经核对未受同种 heading drift 影响，保持原状。
- **Lint baseline**：297 PASS / 0 FAIL → **345 PASS / 0 FAIL**（+48）。`tests/review_verification_test.py` 57 case 全 PASS 不变。
- **JSON 完整性**：`plugin.json` / `marketplace.json` / `tests/skills/contracts/review-loop.json` / `tests/skills/contracts/assertion-mapping.json` 全部 `json.load` 通过。
- BACKLOG P3 v2.7.1 polish-tier follow-ups bundle (8 items) 关闭。

### v2.7.1

- 将 v2.7.0 引入的 conflict-aware parallel CR scheduler (`scripts/review_verification.py`) 接入 Codex Stage 1 三个 reviewer-dispatch site：`.agents/skills/{review-loop,plan,execute}/SKILL.md` 各新增一节 `Parallel Reviewer Fan-Out (N>1)`，明确 N=1 走原 `claude -p` 单次 shell-out 路径不变（argv/stdin/模型解析/临时文件生命周期 byte-identical），N>1 走 `python3 scripts/review_verification.py --jobs <path> --output <path>` 单次外部 fan-out。
- 文档化 jobs.json schema（`session_id`/`job_id`/`runtime`/`prompt_text`/`reviewer_model`/`timeout_secs`/`conflict_keys`/`capacity_keys`/`extra_argv`/`worktree`，由 `scripts/review_verification.py` `_load_jobs` 决定）；明确 `prompt_text` 由 orchestrator 内联到 jobs.json，scheduler 自己渲染 `.review-loop/tmp/{session_id}-reviewer-prompt.{job_id}.txt` 并以 file-FD handoff 喂 stdin（避免 large-prompt deadlock）。
- 明确 orchestrator 是 verdict 提取与 schema 校验的唯一权威：每个 `<results.json>` 条目的 `stdout` 字段按 `docs/protocol/reviewer-output.md` shared schema 解析；scheduler 自己的 `parsed_verdict` / `parsed_issues` 仅作 metadata，per `scripts/review_verification.py:12-17`。
- `docs/protocol/planning.md` §Reviewer dispatch 的 forward pointer 由 "wiring lands in a follow-up" 改为指向上述三个具体 anchor，protocol→skill 链接保留，debt marker 解除。
- 新增 9 条 lint 断言（`codex_parallel_reviewer_fanout_anchor_*` / `codex_parallel_reviewer_scheduler_invocation_*` / `codex_parallel_reviewer_perjob_prompt_path_*`，3 needle × 3 file，inline `kind: contains`）静态守护新 prose；lint baseline 由 288 PASS / 0 FAIL 扩展到 297 PASS / 0 FAIL（无 FAIL）。
- AC narrowing 复述：本轮 wiring 仅作用于 Codex Stage 1。Claude/plugin-side `skills/{review-loop,plan,execute}/SKILL.md` 的 reviewer dispatch 是 in-process Agent-tool 调用，不走 subprocess，无法外部包装；不在本次改动范围。
- BACKLOG P2[1] follow-up "wire scheduler into Codex Stage 1 reviewer dispatch (3 sites)" 关闭。

## 2026-05-08

### v2.7.0

- 引入 `scripts/review_verification.py` — stdlib-only **conflict-aware parallel CR scheduler**（716 行），将单次 reviewer dispatch 扩展为 N 路并行 fan-out 能力。两套正交机制：(1) **二元锁** `conflict_keys`（仅 codebase invariants — `prompt_file:{session_id}:{job_id}` 与可选 `worktree:{path}`，**不含 `cli_rate:*`**）确保两个共享真实资源的 job 互斥；(2) **容量计数器** `capacity_keys`（`cli_rate:claude` / `cli_rate:codex`）opt-in 节流。默认 `Scheduler(max_parallel=4, capacity_limits=None)` 下 5 个同 runtime 的 Codex Stage 1 job 并发跑满 worker pool（peak concurrency = 4），不会被 rate-limit 串行化（解决 R1 默认 `cli_rate:*` 误锁问题）。
- `_run_one` 走 **file-FD handoff**（Approach A）：先把 prompt 写入 `.review-loop/tmp/{session_id}-reviewer-prompt.{job_id}.txt`，再以 `open(prompt_path, "rb")` 打开传给 `Popen(stdin=prompt_fp)` — 而非 `stdin=PIPE` + parent-side write loop（避免 large-prompt deadlock）。`os.killpg(os.getpgid(proc.pid), …)` POSIX session-isolation；SIGKILL 后 `proc.wait(timeout=5.0)` 防 D-state 挂起；`finally:` 始终 close FD + `os.unlink` 临时文件。
- **快照一次（snapshot-once）finalization** 保证 late-drain immutability：reader thread 写入 worker-frame `collections.deque`，`JobResult.stdout_bytes` 在 finalization 时取一次快照后即与 deque 解耦。`ImmutabilityOfFinishedJobsLateDrainTest` 用 `threading.Event` gate 真实驱动 `_run_one` 端到端验证。
- **Reviewer-output schema 严格解析**（`_parse_stream_json_result` + `_validate_reviewer_output_schema`）：verdict 仅接受 `APPROVE`/`REQUEST_CHANGES`；强制 `### Strengths` 出现；Issue 列表 strip 前导 `- ` 后才匹配 `[CRITICAL]`/`[MINOR]`；未支持的 severity（`[MAJOR]`/`[HIGH]`/...）直接 `schema_violation:invalid_severity` 拒绝；六类 verdict/issues 一致性违反全部命名 discriminator（`approve_with_critical` / `request_changes_without_issues` / `request_changes_minor_only` / `issues_prose_placeholder` / `issues_empty_body` / `invalid_severity`）。
- 调度可靠性 hardening：`Scheduler.submit` 在 `ex.submit` 抛异常时回滚 `_claim`（防 binary-lock 永久泄露）；reader thread 异常以 `[reader_error: ...]` 哨兵附加到 buffer 而非 silent swallow；`prompt_fp.close()` 异常处理拓宽到 `Exception`。
- **CLI front door**：`--jobs` / `--max-parallel` (默认 2) / `--default-timeout` (默认 300) / `--output` / `--text` / `--fail-on-any` / `--capacity key=N`（可重复）/ `--tmp-dir`。Exit codes: 0 clean / 1 fail-on-any-nonzero / 2 argv error / 3 scheduler-invariant violation。
- **AC #2 narrowing**：本轮 fan-out 是 **Codex Stage 1 only** 能力（包装 3 个 `claude -p` shell-out site：`.agents/skills/{review-loop:378, plan:224, execute:342}`）。Claude-side 是 in-process Agent tool，不走 subprocess 不可外部包装；spawn follow-up `BACKLOG.md` P2 跟踪 orchestrator wiring。
- 测试：57 cases / 16 test classes（mocked subprocess via `unittest.mock.patch.object`，全部 offline）— 涵盖 timeout drain / returncode propagation / immutability double-record + late-drain / 默认 conflict_keys 不含 `cli_rate:*` / parallel fan-out peak `==4` / capacity throttle `==1` / binary-lock conflict / build-argv 模型解析 (`reviewer_model > judgment_model > claude-sonnet-4-6`) / `StdinDeliveryTest`（4 子测固化 C4：file-like、basename、内容字节、非 PIPE、cleanup） / 11 个 schema 一致性 case / `output_file_missing` + `output_file_unreadable` claude_code 路径 / `worker_exception` 传播 + 锁释放。Lint baseline **288 PASS / 0 FAIL** 保持；ruff 清；mypy 清；bandit 仅 LOW 已 noqa；smoke `exit 0`（2 pre-existing FAIL 与本次代码无关）。
- 文档：`docs/protocol/planning.md` §Reviewer dispatch 加 forward-pointer note，`CLAUDE.md` Codex Stage 1 Notes 加 1 bullet 指向 scheduler，`BACKLOG.md` P2[1] 标 in-progress 并 spawn follow-up "wire scheduler into Codex Stage 1 reviewer dispatch (3 sites)"。
- 计划 4 轮 + 执行 3 轮 codex/gpt-5.5 reviewer 迭代（session `f1c0fa2f-2371-46d8-92cd-d02302b174b2`）：plan R1 → R4 APPROVE（fix `cli_rate:*` default + wrapper anchors + late-drain test gap + R3 risk reframing + C3 contract citation 修正 + AC narrowing + C4 stdin gap + session_id/tmp_dir 显式化）；execute R1 → R3 APPROVE（fix LateDrain test theater + ParallelFanOut bypass + parser schema laxity + unknown severity 拒绝 + issues_empty_body direct case）；polish 综合修复 4 critical 测试 + 4 高优 hardening + 4 simplifier 抽取。Final-boss BACKLOG P2 [1] 完成。
- Plugin v2.6.33 → **v2.7.0** milestone bump（不是 patch — 引入新公共 `scripts/` library 接口，是显著能力扩展）。

### v2.6.33

- Delivery Summary language pinned to 中文 (Simplified Chinese) at the protocol SSOT (`docs/protocol/execution.md` §Step 4 — Delivery #2 / #3). Section headings, prose, and prose-style field values render in 中文; ASCII tokens (file paths, identifiers, SHAs, CLI flags, model names, status enums such as `APPROVE` / `CRITICAL`) stay in their original form. Rule is runtime-agnostic — both Claude Code and Codex Stage 1 inherit via existing "Per `docs/protocol/execution.md` §Step 4 — Delivery" references in `skills/review-loop/SKILL.md`, `skills/execute/SKILL.md`, and `.agents/skills/execute/SKILL.md` (no per-runtime mirror to avoid double-maintenance). The `docs_file` appended copy preserves the same 中文 rendering as the terminal summary. Lint baseline 288 case-level PASS / 0 FAIL preserved.

### v2.6.32

- B3 (`tool_use_min_count`) tightened from `min: 0` (vacuous-pass) to `min: 1` (real-catch) on 7 truncating smoke fixtures via per-fixture `setup.timeout_seconds` bumps. Branch B (linear scale-up) selected after live single-fixture re-measurement at v2.6.31: `execute.session-resume.smoke.claude` failed at 180s (returncode -15 / status skip) and passed at 240s (`status: pass`, 1 Agent event with `subagent_type: general-purpose`). Locked tier values: NEAR_DISPATCH ×3 → 240s (session-resume, stop-after-before-security, stop-after-polish); IN_DOC_RECON ×3 → 360s (from-plan, review-only, stop-after-before-polish); full-pipeline → 600s (review-loop.regression). Each fixture's B3 override flipped `min: 0 → 1` and `_comment` refreshed to `min: 1 enforced at <Ns> per ADR-4` atomically with the timeout bump in the same Edit. `plan.fresh.smoke.claude` is unchanged (control fixture; already passes B3 with implicit shared `min: 1`). `tests/skills/contracts/assertion-mapping.json` shared default unchanged (AC-5). Recorded as ADR-4 (extends ADR-2; ADR-2 not mutated, append-only). The pass/fail criterion under which Branch B was locked is `meta.status == "pass"` AND ≥1 Agent event with `subagent_type: general-purpose`, NOT literal `returncode == 0`: under `execution_policy: best_effort` the runner is by-design permitted to return SIGTERM at the timeout cap (`returncode == -15`) while still stamping `meta.status="pass"` if assertions hold on the captured partial event stream (the `timed_out_with_passing_state` gate at `scripts/run-skill-smoke` line 987). 240s/360s/600s values are extrapolated from the spike's event-rate model and validated empirically only at the NEAR_DISPATCH tier (single-probe scoping per HANDOFF Codex-hang practice); IN_DOC_RECON 360s and full-pipeline 600s remain best-guess until a future re-measurement falsifies them. Worst-case CI wall time approximately 41 minutes per Branch B.
- Closes BACKLOG P3-2 (B3 truncation tightening) — see ADR-4.

### v2.6.31

- `scripts/replay_sessions.py` error-paths hardening — sub-scope (a), genuine plan-locked contract change. `scan_file` now narrowly catches `OSError` (covers `PermissionError`, `FileNotFoundError` race-vs-glob, `IsADirectoryError`, transient FS) at the `read_text` + `stat` call site, writes one stderr line `replay_sessions: unreadable file: <path>: <reason>`, and returns `None`. `build_report` skips `None` records and surfaces the count under a new fourth summary key `summary["unreadable_files"]`. `main` adds a distinct exit code `3` (after `--exit-zero` short-circuit, before anomaly exit-1) so I/O failures aren't conflated with anomaly-detected exit `1`. `render_text` summary footer appends `unreadable=N`. Locked by new `UnreadableFileTest` class with 6 in-process methods using `unittest.mock.patch.object(Path, "read_text", autospec=True, side_effect=...)` plus a `_selective_read_text` helper: exit-3 alone, exit-3 outranks exit-1 (Q2 pin), `--exit-zero` suppresses exit-3 (Q3 pin), unreadable files skipped from `report["files"]` (Q4 pin), stderr line format + glob-sort order (AC-3 pin), `FileNotFoundError` race falls through `OSError` catch (AC-1 enumeration pin). Three pre-existing summary-shape assertions updated explicitly (key list, empty-directory dict + length). Test count 75 → 81. Lint baseline 288 case-level PASS / 0 FAIL preserved. No downstream consumer impact: only `tests/replay_sessions_test.py` references `summary` keys / exit codes; no CI / smoke / docs / contract consumers.
- Closes BACKLOG P3 (`scripts/replay_sessions.py` error-paths sub-scope (a)) — completing the silent-failure-hunter HIGH follow-up filed during v2.6.25 polish (sub-scope (b) shipped in v2.6.30).

### v2.6.30

- 8 new tests close 2 P3 backlog items in a bundled delivery (per v2.6.28 controlled-deviation precedent — pure-test, mutually independent, no shared mutation surface): `RunParserHelperContractTest` (1 method, P3 [3b] — `tests/replay_sessions_test.py::run_parser` helper hardening: replaces silent `parsed = None` JSONDecodeError fallback with `AssertionError` carrying subprocess stdout/stderr/returncode, so a parser regression surfaces a clean failure at the assertion site instead of a confusing downstream `TypeError`); `SecondTierCoverageTest` extended with 4 methods (P3 [4] gaps 1, 2 negative + positive, 5 — `bt` quoted regex branch isolated; secondary regex `BARE_REVIEW_LOOP_RE` left-boundary current behavior pinned (negative + positive); `render_text` `>50`-char path-truncation branch); `KindContainsTest` extended with 3 methods (P3 [4] gaps 3, 4 — case-sensitivity invariant pin (uppercase needle matches uppercase, fails on lowercase); empty-needle rejected at contract-load time by `require_fields`). `test_glob_is_single_level_not_recursive` augmented with positive existence assertion (Gap 6). No script-under-test, contract-loader, or lint contract JSON edits. Test count 67 → 75. Lint baseline 288 case-level PASS / 0 FAIL preserved.
- Closes BACKLOG P3 [3b] (`run_parser` helper hardening) and P3 [4] (six third-tier coverage gaps).

### v2.6.28

- 10 new tests close 2 P3 backlog items: `KindContainsTest` (3 methods, isolates `kind: contains` lint mechanic from integration smoke) and `SecondTierCoverageTest` (7 methods covering 6 gaps — gap 1 split into sq + dq quote branches; plus secondary-regex right-boundary, errors=replace UTF-8 decode, *.md non-recursive glob, anomaly_values set-dedup, --text rendering pinning). No script-under-test changes. Test count 57 → 67.
- Closes BACKLOG P3 (KindContainsTest unit class + second-tier replay_sessions coverage).

## 2026-05-07

### v2.6.26

- 15 new tests in `tests/replay_sessions_test.py` close 7 MEDIUM coverage gaps from pr-test-analyzer's v2.6.25 quality-polish pass — `--root` non-directory / non-existent exit-code-2 path, multi-file aggregation, empty-directory contract, same-value-multi-line counts, `anomaly_sites` line-number fidelity, JSON `sort_keys` / per-file `mtime` ISO 8601 invariants, plus in-process `ScanLineUnitTest` and `BuildReportUnitTest` covering `scan_line` / `build_report` directly. No parser change. Test count 14 → 29.
- Closes BACKLOG P3 (replay_sessions test plumbing-edge gaps).

## 2026-05-07

- review-loop v2.6.25: new `scripts/replay_sessions.py` — stdlib-only post-hoc audit channel that walks `.review-loop/sessions/*.md`, enumerates every `subagent_type` value per file, and flags `review-loop:*` occurrences as anomalies. Uses an anchored primary regex (`\bsubagent_type:`) plus a closed-set bare-form secondary regex over the 12 known agent names, with span-overlap dedup so a single occurrence isn't double-counted. Emits JSON by default (`--text` for a human table); exit 1 on anomaly, 0 otherwise (`--exit-zero` overrides). Complements the existing static lint contract for `SKILL.md` / protocol-doc files. Backed by 14 unittest cases (4 acceptance + 7 corpus-grounded with verbatim file:line citations + 1 dedup + 2 CLI).
- Closes BACKLOG P2 (session-replay parser).

## 2026-05-07

- review-loop v2.6.24: per-site `contains` companion assertions for 6 codex `pattern_requires_adjacent` stop-and-surface anchors in `tests/skills/contracts/review-loop.json`. Mirrors the existing `codex_execute_git_diff_failure_present`/`_lock_release` template across plan/SKILL.md and execute/SKILL.md so any future removal of an anchor needle FAILs lint instead of silently passing.
- Lint baseline 272 → 278 case-level PASS / 0 FAIL; unit tests 28/28 OK.
- Closes BACKLOG P3 (per-site contains companion for 6 stop-and-surface anchors). Claude side remains out-of-scope per the v2.6.23 mapping (no Claude analogue; SKILLs delegate to `docs/protocol/planning.md §Reviewer dispatch`).

## 2026-04-23

- review-loop: Codex Stage 1 now follows the same downstream `exec -> polish -> docs -> security -> delivery` lifecycle as Claude Code instead of silently stopping at `exec`.
- Protocol docs, repo skills, plugin mirrors, README, and guide surfaces were aligned on the widened delivery gate, clean stop points, and the real `quality_focus` / `skip_quality_polish` semantics.
- Contract and smoke coverage were expanded for Codex reviewer routes, review-only routing, `skip_quality_polish`, and the missing `before-polish` / `before-security` stop seams.
- Delivery hygiene tightened: plugin metadata bumped to `2.6.18`, stale developer docs were refreshed, and `.gitignore` gained the security-preflight pattern coverage defined in `docs/protocol/execution.md`.
