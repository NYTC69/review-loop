# tasks/todo.md

## Compass adopt — pending manual splits

两条 compass:adopt 推迟的人工细拆任务。Round 2 apply（plan_sha `a8d9343ef0c1`）迁走了 E001-E004（README L1-56），Round 3 apply（plan_sha `e2439220c6bd`，2026-04-19）迁走了 A002 split 成的 E005-E012（README L64-366）。剩下的 A003 (tasks/ideas.md) 仍在 ambiguous[]。

### [x] 1. 细拆 README.md L64-370（A002） — 完成 2026-04-19

Round 3 apply（plan_sha `e2439220c6bd`）把 A002 拆成 E005-E012 共 8 条 entries + 1 条 A005（License no-fit）。落地后：
- E005-E007, E009-E010, E012 → ARCHITECTURE.md（+6 blocks, +256 lines）
- E008 → CLAUDE.md（Configuration schema, +42 lines）
- E011 → DESIGN.md（Key Design Features，absent→scaffold+append, 49 lines）

Phase 3 verify byte-exact pass（3 targets）。Lint 160 PASS/0 FAIL（README 保持 370 行完整，SSOT needles 仍在 README 解析）。Journal: `.compass-adopt/rollback/e2439220c6bd.../` status=succeeded。
旧 journal `a8d9343ef0c1...` 保留作 Round 2 audit。

### [x] 2. 细拆 tasks/ideas.md（A003） — 完成 2026-04-19

手工 Edit 路径（不改 `.compass-adopt/plan.yaml` / 不重跑 adopt:apply）：L1-22 "Ideas to explore" 4 个 bullet 逐条 append 进 `BACKLOG.md` P2（2 条 `[partial]` + 2 条 `[new]`，无 umbrella —— `tasks/ideas.md` 保留作 audit 本体即是 umbrella）；L23-26 "Related bugs caught in the wild" 与 `CLAUDE.md §Plugin agent type sandbox bug` 完全去重后 DROP，仅把第三条 code-simplifier 2026-04-06 incident 单句 inline 进 CLAUDE.md History 句；`tasks/ideas.md` 字节保持不变，`.compass-adopt/plan.yaml` A003 ambiguous 记录原样保留。

---

## Reference

- 当前 plan.yaml plan_sha: `e2439220c6bd`（前 12 位，Round 3）
- 当前 entries[] = 8（E005-E012，覆盖 README L64-366；Round 2 的 E001-E004 已从 entries[] 移出，由旧 journal `a8d9343ef0c1...` 继续作为 audit 记录）
- 当前 ambiguous[] = 4（A001 no-fit L57-63 / A003 split tasks/ideas.md / A004 no-fit review-loop-config.example.md / A005 no-fit L368-370 License）
- 落地 journals：`.compass-adopt/rollback/a8d9343ef0c1.../`（Round 2，succeeded）+ `.compass-adopt/rollback/e2439220c6bd.../`（Round 3，succeeded）

**⚠️ README.md 保留完整形态**：初版 adopt 同时 trim 了 README.md L1-63，但 `run-skill-lint` 的 `guide:readme_marks_*` 和 `shared-schema:*` 断言依赖 README 里的若干 phrase 作 SSOT，trim 后 lint 5 条 FAIL。已 revert README 回到完整 370 行，CLAUDE.md + ARCHITECTURE.md 的 `## Migrated —` block 作为双存储保留。后续要真 trim README 必须同步把 `guide` skill + lint contract 指向新 SSOT（CLAUDE.md migrated block 或其他目标），先别动 README 本体。
