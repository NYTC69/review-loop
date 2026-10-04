# DECISIONS — review-loop

ADR-style. Each entry is immutable once accepted. Supersede with a new
entry; never edit history.

---

### ADR-1: Refine BACKLOG P2 "dry-run Orchestrator mode" to lint+smoke assertions

<!-- synced: 2026-05-04 drawer-id=drawer_3cats_decisions_review-loop_3e0a26f2bcf937e6 sidecar-hash=b69e99880943e5e65fd67064869d11cc target=3cats/decisions_review-loop schema=v1 -->

- **Date**: 2026-05-02
- **Status**: Accepted
- **Context**: BACKLOG P2 (added 2026-04-19) proposed a "dry-run Orchestrator that executes /review-loop against a fixture repo and validates the Agent-call sequence without writing files," motivated by stall-class bugs (`tool_uses: 0` from plugin-defined `subagent_type`, `--output-format json` buffering hang). Recon during plan session b1e5ecca (2026-05-02) showed there is no orchestrator process to instrument: `skills/review-loop/SKILL.md` IS the orchestrator-as-prose, executed by Claude/Codex models. A dry-run process boundary doesn't exist.
- **Options considered**:
  - **(A) Static analysis only** — parse SKILL.md + agents/*.md for `subagent_type:` literals; cheap and CI-friendly but cannot catch runtime drift where the model dynamically constructs a wrong subagent_type.
  - **(B) Stream-json `tool_use` post-processor** — extract `tool_use` events from the smoke runner's stream-json capture and assert sequence/values; catches runtime drift but requires extending smoke contract schema.
  - **(C) Dryrun SKILL flag** — add `--dry-run` to SKILL.md telling the model to skip side-effect tools; requires model self-discipline; unreliable.
  - **(D) Separate orchestrator binary** — rejected as the orchestrator is prose, not a process.
- **Decision**: Implement (A) + (B) as complementary layers. Static lint + runtime smoke assertions cover both shapes (text drift and runtime drift). Reject (C) and (D).
- **Consequences**: Two new lint kinds (A1 `command_flag_co_occurrence`, A2 `agent_subagent_type_whitelist`) plus two new smoke kinds (B3 `tool_use_min_count`, B4 `tool_use_agent_subagent_type_whitelist`). Adds a new `lint_assertions` mapping section in `tests/skills/contracts/assertion-mapping.json` parallel to `smoke_assertions`. Closes BACKLOG P2[1] "dry-run Orchestrator" by replacing it with this concrete scope. Implementation tracked in plan session `.review-loop/sessions/b1e5ecca-6bc1-43cb-b6d3-c8b5174e60ca.md` (4-round codex-reviewed plan; APPROVE 2026-05-02).

---

### ADR-2: B3 wiring strategy — option (b) per-fixture min override

<!-- synced: 2026-05-04 drawer-id=drawer_3cats_decisions_review-loop_eadd66b47858b6df sidecar-hash=ebeb9d92dbd697c57ade2abc7e8dd0d4 target=3cats/decisions_review-loop schema=v1 -->

- **Date**: 2026-05-02
- **Status**: Accepted
- **Context**: B3 (`tool_use_min_count`) is meant to assert each smoke run dispatches at least one Agent/subagent call. Audit (2026-05-02 against `tests/skills/.artifacts/*/tool-use-events.json`) showed 1 of 8 tool-event-capturing smoke fixtures (`plan.fresh.smoke.claude`) actually reaches an Agent dispatch under the existing 120s timeout; the other 7 truncate before Agent is dispatched (0 Agent events each, all 8 with `schema_errors=1`). Wiring `min: 1` to all 8 would red 7 fixtures on first run.
- **Options considered**:
  - **(a) Narrow B3 to plan.fresh only** — assertion ID appears in 1 of 8 fixture files; violates AC #4 ("wired into all 8 smoke fixtures already capturing tool_use_events") textually.
  - **(b) Permissive `min: 0` overrides** — wire B3 to all 8 with the shared mapping defaulting to `min: 1` (real-catch on plan.fresh) and the 7 truncated fixtures using a per-fixture override `{"overrides": {"min": 0, "_comment": "<rationale>"}}` (vacuous-pass with named/traced rationale). Requires a new override-resolver mechanism in `run-skill-smoke`.
  - **(c) Bump per-fixture timeouts** — uniform timeout bump unproven; the 120s cap was set during the Apr 22 timeout fix for sound reasons; each fixture truncates for fixture-specific reasons.
  - **(d) Soft-skip on schema_errors > 0** — contradicts the existing repo-wide pattern at `run-skill-smoke:241-258` and 276-304 ("schema drift only blocks the assertion when no events were captured at all; with non-empty events evaluate content directly"); creates inconsistency with already-shipped smoke kinds.
- **Decision**: (b) — permissive override with new resolver. Test `test_fails_with_min_one_and_zero_agent_events_even_when_truncated` proves the default `min: 1` would catch the regression — only the explicit per-fixture override demotes to `min: 0`. (c) is encoded as one of three approaches in a P3 BACKLOG follow-up to fortify the truncated fixtures.
- **Consequences**: Adds an override-resolver shape `{"id": "<id>", "overrides": {<field>: <value>, …}}` to `scripts/run-skill-smoke` with shallow merge over the mapping entry. Override-key whitelist: `min`, `tool`, `artifact`, `requires_subagent_type`. Unwhitelisted non-`_`-prefixed keys → contract validation error (preserves invariant strength). New BACKLOG P3 entry: "Tighten B3 min from 0 to 1 on 7 truncated smoke fixtures by addressing pre-Agent stream truncation."

---

### ADR-3: `_`-prefix-as-ignored-metadata convention for override resolver

<!-- synced: 2026-05-04 drawer-id=drawer_3cats_decisions_review-loop_3556ec7bdd45bd1d sidecar-hash=0460c07f1362956b3be41ce9835e212f target=3cats/decisions_review-loop schema=v1 -->

- **Date**: 2026-05-02
- **Status**: Accepted
- **Context**: ADR-2 introduced per-fixture override wrappers carrying rationale (e.g. why a specific fixture demotes `min` to 0). The R3 codex reviewer flagged that putting this rationale on the **shared** mapping entry would be misleading because the shared entry's `min` is 1; only per-fixture overrides demote it. The rationale must live next to the override site itself, not on the shared mapping.
- **Options considered**:
  - **(I) Single named `_comment` key** — explicit list of one allowed metadata key in the resolver.
  - **(II) `_`-prefix-as-ignored-metadata convention** — any override key whose name starts with `_` (underscore) is silently dropped during merge and ignored before the override-key whitelist check. Generalizes to `_reason`, `_note`, `_todo`, `_owner`, etc. without further plumbing.
  - **(III) Schema-level `description` field per override-wrapper** — adds a typed field to the contract, requires schema migration.
- **Decision**: (II). Single underscore-prefix rule generalizes naturally and adds zero contract-schema surface.
- **Consequences**: Resolver in both `scripts/run-skill-lint` (lint side) and `scripts/run-skill-smoke` (smoke side) now silently drops any override key starting with `_` during merge, before the whitelist check. New unit test `test_underscore_prefixed_override_key_is_ignored_silently` pins behavior. Convention applies repo-wide for any future override metadata. `_comment` becomes the canonical rationale field but is not blessed in code — only the prefix rule is.

---

### ADR-4: ADR-2 follow-up — lock per-fixture B3 timeout bumps

<!-- synced: 2026-05-08 drawer-id=drawer_3cats_decisions_review-loop_e24abb02eeef3207 sidecar-hash=77ad81331dd7ff55280240212dd00dc7 target=3cats/decisions_review-loop schema=v1 -->

- **Date**: 2026-05-08
- **Status**: Accepted
- **Context**: ADR-2 introduced per-fixture `min: 0` overrides as a vacuous-pass shim for the 7 of 8 `tool_use_events`-capturing smoke fixtures that truncated before Agent dispatch under the 120s wall-clock cap, and recorded a P3 BACKLOG follow-up to "tighten B3 min from 0 to 1 by addressing pre-Agent stream truncation." The spike at `.compass/results/2026-05-08_b3-truncation-spike.json` (committed at 368210a) tabulates per-fixture truncation profiles (NEAR_DISPATCH / IN_DOC_RECON / full-pipeline) and identifies SKILL doc-recon as the bottleneck (NOT user-prompt size — `plan.fresh.smoke.claude` carries the largest user prompt at 1089 chars yet is the only fixture that already passes B3). The spike empirically proposed bumps of 180s / 240s / 480s by tier. Single-fixture re-measurement at v2.6.31 (`execute.session-resume.smoke.claude`, NEAR_DISPATCH tier) showed: 180s → returncode -15 / status skip; 240s → status pass with 1 Agent event (`subagent_type: general-purpose`). Branch B (linear scale-up) selected per the Approved Plan's deterministic Step 2 fork table. The pass/fail criterion under which Branch B was locked is `meta.status == "pass"` AND ≥1 Agent event with `subagent_type: general-purpose`, NOT literal `returncode == 0`: under `execution_policy: best_effort` the runner is by-design permitted to return SIGTERM at the timeout cap (`returncode == -15`) while still stamping `meta.status="pass"` if assertions hold on the captured partial event stream. `meta.status` is the runner's official assertion-completion signal; literal returncode is not. The runner branch this criterion relies on lives in `scripts/run-skill-smoke` lines 967–1002: on `TimeoutExpired`, `evaluate_mapping(...)` runs over the captured partial event stream (line 967), `meta["status"]` is initially stamped `"skip"` (line 976), then upgraded to `"pass"` at line 989 iff `assertion_status == "pass" and not missing_artifacts` — the `timed_out_with_passing_state` gate at line 987 — emitting `final_reason = "assertions passed after timeout cleanup"` (line 990).
- **Options considered**:
  - **(i) Trim user prompt** — rejected up-front per the spike's bottleneck-finding (doc-recon dominates; user-prompt size is not the constraint).
  - **(ii) Bump per-fixture `setup.timeout_seconds`** — chosen. Each fixture's `command` block, `setup.temp_config`, and assertion list preserve their distinct entry-point/stop-point/provenance signals. `min: 0 → 1` flip is atomic with the timeout bump in the same Edit.
  - **(iii) Agent-only stub fallback** — reserved as the uniform Branch C escalation path if Step 2 had double-failed at both 180s and 240s. Not adopted in the active Branch B; would have sacrificed 6 distinct non-B3 signals to recover B3.
- **Decision**: Branch B linear scale-up. Per-fixture lock table:

  | Fixture | Tier | timeout_seconds | B3 override min |
  |---|---|---|---|
  | execute.session-resume.smoke.claude | NEAR_DISPATCH | 240 | 1 |
  | execute.stop-after-before-security.smoke.claude | NEAR_DISPATCH | 240 | 1 |
  | execute.stop-after-polish.smoke.claude | NEAR_DISPATCH | 240 | 1 |
  | execute.from-plan.smoke.claude | IN_DOC_RECON | 360 | 1 |
  | execute.review-only.smoke.claude | IN_DOC_RECON | 360 | 1 |
  | execute.stop-after-before-polish.smoke.claude | IN_DOC_RECON | 360 | 1 |
  | review-loop.regression.smoke.claude | full-pipeline | 600 | 1 |

  `plan.fresh.smoke.claude` is unchanged (control fixture; already passes B3 with implicit shared `min: 1`). Each fixture's `_comment` is refreshed to `min: 1 enforced at <Ns> per ADR-4`.
- **Consequences**: Worst-case CI wall time per the active Branch B is approximately 41 minutes (plan-quoted figure). Branch A figure (29 min) and Branch C figure (10.5 min) are recorded for completeness but do not apply. The B3 shared-mapping default (`min: 1`) is unchanged (AC-5). AC-6 is mechanically pinned via JSON-parse verification: shared `min: 1`, every per-fixture B3 override has `min: 1`, NO fixture retains `min: 0`. Per Round-1 reviewer MINOR #2 explicit framing: 240s and 480s values are extrapolated from the spike's event-rate model, validated empirically only at the NEAR_DISPATCH tier (180s probe at v2.6.31, falsified; 240s probe at v2.6.31, confirmed). The IN_DOC_RECON 360s and full-pipeline 600s values (Branch B linear scale-up) remain best-guess until a future re-measurement falsifies them; this is recorded as an explicit consequence of single-probe scoping per HANDOFF Codex-hang practice. ADR-2 is NOT mutated (append-only).

---

### ADR-5: paired-session 各角色模型固定 — Claude 全用 claude-opus-5-5，Codex 全用 gpt-6-luna
- **Date**: 2026-09-23
- **Status**: Accepted
- **Context**: paired-session 协调程序的各次运行用过不同模型（run #1-#4 Codex 为 `gpt-6-astra`，run #5 为 `gpt-5.6-sol`；Claude 侧写的是 `claude-opus-5` 或别名），跨运行的成本与评审质量对比因此混入模型变量。Codex 侧曾用 `claude-opus-5.5` 调用失败（"model may not exist"），正确的 CLI 值需要固定下来。
- **Options considered**: (A) 每次运行按当时情况选模型 — 灵活，但运行之间不可比；(B) 按角色固定模型（实现方 / 持久 reviewer / 影子 / gate 各不同）— 可调优但组合多；(C) 按厂商固定：Claude 侧所有角色一个模型，Codex 侧所有角色一个模型 — 简单、可比。
- **Decision**: 选 (C)。今后 paired-session 的配置：Claude 侧所有角色（author、持久 reviewer、影子、adversarial gate，无论执行还是评审）都用 `--model claude-opus-5-5`（用连字符；`claude-opus-5.5` 无效；不用会漂移的别名 `opus`）；Codex 侧所有角色都用 `gpt-6-luna`。owner 2026-09-23 决定。
- **Consequences**: 运行之间的模型变量被消除，成本与质量可以直接对比。`claude-opus-5-5` 已于 2026-09-23 在本机 Claude Code 2.1.280 上用 `claude -p --no-session-persistence --model claude-opus-5-5 --effort medium` 实测可用；`gpt-6-luna` 在记录时尚未实测，第一次使用前需用 probe 轮确认。以后换模型要新开 ADR supersede 本条，并在报告中注明换模型前后的运行不可直接比较。run #5 及之前的数据属于旧配置。

---

### ADR-6: paired-session primary daily entry and replacement gate
- **Date**: 2026-09-24
- **Status**: Accepted
- **Context**: The 2026-09-21 owner decision in `BACKLOG.md` at `b61fa29` ("Replacement gate") says "until then the new architecture ships as a mode alongside the old one"; this ADR amends that sentence to allow paired-session to become the primary daily entry after 1A–1C readiness and an explicit go for 1D, while legacy remains the explicit control path.
- **Options considered**: (A) Keep the new architecture alongside the old one without changing the primary daily entry until all four replacement criteria pass; (B) after 1A–1C readiness and explicit authorization for 1D, allow paired-session to become the primary daily entry while retaining legacy as an explicit control until all four criteria pass.
- **Decision**: Adopt (B). Criterion (1) decides WHETHER; criteria (2)–(4) decide WHEN. (1) The fresh reviewing roles must catch most seeded regressions that keep the suite green; run the same seeded diffs through the old reviewer path as control; the new path must be no worse than the old. If both miss, that is a reviewer limit, not an architecture verdict; if only the new one misses, fix its review design first. (2) Three consecutive real runs with zero coordinator defects and an overseer limited to scoping the work item and verifying at the end; measure overseer steady-state token cost and include it in per-item cost. (3) A first-class user-acceptance feedback phase exists in the coordinator. (4) Coverage includes at least one repo other than poker-tools, one large task, and one real subscription-limit HOLD followed by a successful resume. Paired-session may become the primary daily entry only after 1A–1C readiness and an explicit owner go for 1D. 1D may not switch the default route until there is an owner decision or explicit mapping on parity with legacy polish, docs, security stages, and specialist reviewer agents; until then those stages remain reachable through the explicit legacy/control path. The old implementation may be retired only after all four criteria pass. owner 2026-09-24 decision.
- **Consequences**: After 1A–1C readiness and explicit go for 1D, the daily entry may switch to paired-session, subject to the parity decision or explicit mapping for legacy polish, docs, security stages, and specialist reviewer agents; until then those stages remain reachable through the explicit legacy/control path. This does not authorize retirement of the old implementation, which remains the explicit control until all four replacement criteria pass, or authorize starting a real task.

---

### ADR-7: paired-session Codex 角色模型改用 gpt-6-sol
- **Date**: 2026-09-27
- **Status**: Accepted
- **Context**: ADR-5 将 Codex 侧所有 paired-session 角色固定为 `gpt-6-luna`。Yuan 在 2026-09-27 的 D5 明确决定“换成 sol”；这改变产品角色模型，不只是当前执行会话的默认值。
- **Options considered**: (A) 维持 ADR-5 的 `gpt-6-luna` 固定值；(B) 依 D5 将 Codex 侧所有角色统一改为 `gpt-6-sol`，Claude 侧保持原值。
- **Decision**: 采用 (B)。Claude 侧所有 paired-session 角色仍用 `claude-opus-5-5`；Codex 侧所有角色统一用 `gpt-6-sol`。Yuan 2026-09-27 D5 决定。
- **Consequences**: 换模型前后的运行成本与评审质量不能直接比较。已安装 Codex 的 1C 权限探测必须用新模型重跑，且探测结果绑定当时的作者模型与 CLI 配置；这不授权 1D。
- **Supersedes**: ADR-5

---

### ADR-8: paired-session Codex 角色模型切回 gpt-6-luna
- **Date**: 2026-09-27
- **Status**: Accepted
- **Context**: Yuan 在 2026-09-27 的 D9 决定“都切回 luna 吧. 不然我怕做不完, token 就没了.” 当日的成本记录显示 ADR-7 所选模型每 token 约为 gpt-6-luna 的 7 倍。
- **Options considered**: (A) 继续使用 ADR-7 的 Codex 模型 pin；(B) 按 D9 将所有 paired-session Codex 角色恢复为 gpt-6-luna，Claude 角色保持不变。
- **Decision**: 采用 (B)。Codex author、reviewer、shadow、gate 的默认值与 enforcement 均固定为 gpt-6-luna；Claude 角色仍固定为 claude-opus-5-5。Yuan 2026-09-27 D9 决定。
- **Consequences**: ADR-7 (sol) 期间记录的运行与 ADR-8 期间的运行不可直接比较；M6 复查使用 ADR-8 模型。R21-1 的 D1(b) 合成权限证据与模型无关，继续有效。旧 DONE 且未 accept 的 run 通过 reject 重新派发时仍会使用冻结模型；该已知路径记录在 BACKLOG.md，修复前不能声称历史 run 的每条再派发路径均已切到 ADR-8。
- **Supersedes**: ADR-7

---

### ADR-9: paired-session role models are operator-configured
- **Date**: 2026-09-30
- **Status**: Accepted
- **Context**: poker-news-bot 于 2026-09-30 提交 bug 报告（`~/3Cats/poker-news-bot/.compass/results/2026-09-24_ab_pipeline/review_loop_290_bug_report.md`）：v2.9.0 的真实 CLI 路径把各角色模型按厂商写死（ADR-5/ADR-8：codex=gpt-6-luna，claude=claude-opus-5-5），并强制 gate 厂商与 author 相反，导致 `--reviewer-model gpt-6.1-sol --gate-model gpt-6.1-sol` 与“Claude author + Codex reviewer + Codex gate”无法配置；每次新模型发布都要改代码。Yuan 于 2026-09-30 13:15 JST 批准 P0 计划：“三件都同意，按计划推进 P0.”，其中包括 supersede ADR-8。
- **Options considered**: (A) 保持 ADR-8 的模型 pin，每次换模型开新 ADR 并发版；(B) 模型与 gate 厂商由运营者配置，pin 只保留为默认值，用可选 allowlist 和运行时身份检查兜底。
- **Decision**: 采用 (B)。(M1) 默认值不变：未显式指定模型的角色取厂商默认（claude→claude-opus-5-5，codex→gpt-6-luna）；gate 厂商默认取 author 的相反厂商。(M2) 新增 `--gate-vendor {codex,claude}`，可在 paired-session.json 配置；所有 gate 厂商推导点使用解析后的 `args.gate_vendor`；允许与 reviewer 同厂商。(M3) `validate_role_models` 不再 pin 模型，只要求：每个角色的模型 id 形如 `^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$`；若配置了可选键 `allowed_models`（`{"codex": [...], "claude": [...]}`，字符串列表），则每个角色的模型必须在该角色厂商的列表中，格式错误的 allowlist 被拒绝，未设置即无 allowlist。(M4) CLI 上报的模型与该角色配置的模型不一致（`model_identity == MISMATCH`）时，run 进入 HOLD，原因含 “model identity mismatch”；未上报仍只记录为 UNREPORTED，不算错误。(M5) run state 记录每个角色解析后的厂商和模型（新增 `gate_vendor`）；run/resume/accept/reject/note 恢复时以保存值为准，显式给出不同的厂商或模型标志会被拒绝（“role models are fixed for this run”）；没有 `gate_vendor` 的旧 state 按旧规则推导。owner 2026-09-30 批准。
- **Consequences**: ADR-8 的模型 pin 失效，但其默认值保留。benchmark/M7 的可比性改由“每次运行在 state 与 usage ledger 中记录各角色模型，benchmark 运行通过配置固定模型”维持；不同配置下的运行不可直接比较。Claude author 拒绝（1C row 3b）不在本 ADR 范围内，由 P0-3 处理。ADR-5 中“按厂商固定模型”的强制部分同样由本 ADR 取代，其默认模型值保留。
- **Supersedes**: ADR-8

---

### ADR-10: paired-session 的 Step 3.4 gate 默认取 author 的厂商
- **Date**: 2026-10-01
- **Status**: Accepted
- **Context**: ADR-9 (M1) 规定 gate 厂商默认取 author 的相反厂商。owner 于 2026-09-30 决定改为“A 执行 B review，A 家最后终审”，并批准按 P 方案（先扩展 permission-probe，再翻转默认）实施：G-a（v2.9.2）已让 permission-probe 覆盖与 reviewer 不同厂商的 gate 面（`gate_flags_digest` + 第二个 `gate-probe` 回合），G-b（本 ADR，v2.9.3）翻转默认。BACKLOG 中“每个阻塞发现都来自新鲜的对立厂商 gate”这一观察仍然成立：paired-session 中 author ≠ reviewer，所以 author 厂商的 gate 仍与 reviewer 厂商不同，gate 提供的“另一家厂商的新鲜视角”保留下来，只是现在它对立于 reviewer 而不是 author；这个面由 G-a 的 gate probe 证明。
- **Options considered**: (A) 保持 ADR-9 M1（gate 取 author 的相反厂商）；(B) gate 默认取 author 的厂商，可显式覆盖并记录；(C) 只做运营者接受而不扩展 probe（已被 P 方案否决）。
- **Decision**: 采用 (B)。(M1) 未给 `--gate-vendor` 且配置无 `gate_vendor` 键时，`resolve_role_model_defaults` 令 gate 厂商 = author 厂商，gate 模型再按该厂商取默认（codex→gpt-6-luna，claude→claude-opus-5-5）。(M2) run state 的 `config.gate_vendor_source` 记录 `default` 或 `operator`（命令行 `--gate-vendor` 或 `--config` 中的 `gate_vendor` 键即 operator）；没有该键的已保存 run 恢复为 `legacy-derived`；run/resume 输出在 gate 厂商旁打印 `GATE: <vendor> <model> (gate_vendor_source: ...)`。(M3) 恢复绝不静默切换 gate 厂商：没有 `gate_vendor` 的已保存 run 保持旧的“author 的相反厂商”推导（`old_gate_vendor` 只留给这条路径和 M4），其 scope-change successor 的配置注入该推导值，successor 因而保持同一 gate 厂商。(M4) 在创建 state 之前（run、resume、permission-probe）拒绝显式的 `gate_model`：它是另一厂商的已知 id（该厂商 `allowed_models` 中的 id，或 `claude-` / `gpt-` 前缀）而 `gate_vendor` 未显式给出时，提示 `pass --gate-vendor <vendor> to keep it`；显式 `gate_vendor` 搭配另一厂商的模型同样被拒绝。已保存 run 的恢复不受此检查影响。(M5) 示例配置 `paired-session-config.example.json` 加 `gate_vendor: claude`，保持其原有行为（claude gate + claude-opus-5-5）。
- **Consequences**: 部分取代 ADR-9 的 M1（gate 默认取 author 的相反厂商）；ADR-9 其余各条（M2–M5，含“允许与 reviewer 同厂商”和旧 state 按旧规则推导）继续有效，ADR-8 的模型默认值也不变。真实 CLI 上默认配置（codex author + claude reviewer → codex gate）在没有覆盖该 gate 的 PASS gate probe 前被 probe 闸门拒绝，需要先跑一次 `permission-probe`。依赖旧默认的调用方（例如 poker-news-bot 的 Claude author + Codex reviewer + Codex gate）需要显式传 `--gate-vendor codex`。旧 legacy review-loop 协议不变，本 ADR 只适用于 paired-session。
- **Supersedes**: ADR-9 (M1)

---

### ADR-11: paired-session Codex 角色默认模型改为 gpt-6.1-sol
- **Date**: 2026-10-04
- **Status**: Accepted
- **Context**: ADR-9 (M1) 保留了按厂商的默认模型（codex→gpt-6-luna），ADR-10 沿用。Yuan 于 2026-10-04 09:15 JST 选择“改成 gpt-6.1-sol”（推荐项是保留 luna），09:20 补充“默认 claude 这边就是 opus5.5，codex 那边就是 6.1-sol”。gpt-6.1-sol 需要 codex-cli 0.159.2 及以上；更旧的 CLI 在第一次派发时才会失败，不能让默认值在 run 中途失败。监督者 2026-10-04 16:00 决定：测试 harness 的 fake codex 报告今天真实 CLI 上验证过的 0.160.0（v2.9.6 的 owner 真实 probe p296.sh P1/P2 PASS），版本拒绝在所有路径上生效，没有测试专用的绕过。
- **Options considered**: (A) 保持 gpt-6-luna 默认；(B) Codex 默认改为 gpt-6.1-sol，CLI 过旧时在创建 state 前拒绝并给出修法。
- **Decision**: 采用 (B)。(M1) 未显式指定模型的 Codex 角色（author、reviewer、gate）默认取 `gpt-6.1-sol`；Claude 默认不变（claude-opus-5-5）。(M2) 只要有 Codex 角色取了这个默认值，就在创建 run state 之前读取 `codex --version`；低于 0.159.2 或读不出版本时拒绝，消息写明 `upgrade the Codex CLI, or pass --author-model/--reviewer-model/--gate-model gpt-6-luna`（只列取了默认值的角色）。显式指定模型的角色不受此检查。(M3) 已保存的 run 恢复时沿用冻结在 state 里的模型（ADR-9 M5），不迁移，也不做版本检查。(M4) `VERIFIED_CODEX_CLI_VERSIONS` 增加 `codex-cli 0.160.0`，依据是上述真实 probe 的 PASS 记录（`.compass/results/2026-10-04_daily-report.md` T1）。owner 2026-10-04 决定，监督者 2026-10-04 16:00 记录 harness 决定。
- **Consequences**: 取代 ADR-9 (M1) 中 Codex 默认模型的取值，以及 ADR-10 (M1) 中 “codex→gpt-6-luna” 的默认值；ADR-9 的运营者配置、allowlist、身份检查和冻结规则（M2–M5）以及 ADR-10 的 gate 厂商规则不变。用 0.157.0 等旧 CLI 且依赖默认 Codex 模型的调用方，需要升级 CLI 或显式传 `gpt-6-luna`。permission-probe 绑定 author 模型，所以依赖默认值的运营者在升级后需要重跑一次 `permission-probe`（旧 PASS 和 probe 缓存不再命中）。版本检查只对已通过程序绑定（program_binding）的 codex 路径执行，绝不执行 workspace 指定的程序；只对会创建 state 的 run、resume、permission-probe 执行。默认改变前后的 run 成本与评审质量不能直接比较。示例配置、README 和 skills 中写死的旧默认值由 models-b 处理。
- **Supersedes**: ADR-9 (M1) Codex default value; ADR-10 (M1) Codex default value
