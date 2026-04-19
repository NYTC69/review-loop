# tasks/todo.md

## Compass adopt — pending manual splits

两条 compass:adopt 推迟的人工细拆任务。apply 当前 plan.yaml 只迁走了 E001-E004（README L1-56 里的 title / Quick Start / Codex Stage 1 overview / Skill Tests），其它段落都还在 ambiguous[] 里。

### [ ] 1. 细拆 README.md L64-370（A002）

当前一整块 307 行进 ambiguous[] recommendation=split。细拆计划参考 plan.yaml A002.split_hint：

- L64-137 Three Skills + Multi-batch + `--stop-after` + `--accept-external-state` → ARCHITECTURE（或其中 Three Skills 概念部分归 DESIGN）
- L139-181 Workflow Overview + Example → ARCHITECTURE
- L183-222 Standalone Tools → ARCHITECTURE
- L224-261 Configuration 表 + examples → CLAUDE（配置手册）或 DECISIONS（每个 key 的选定理由）
- L263-273 Reviewer Modes → ARCHITECTURE
- L275-290 Included Agents → ARCHITECTURE
- L292-317 Key Design Features → DESIGN（前瞻性行为说明）
- L319-366 File Structure → ARCHITECTURE
- L368-370 License → no-fit（留在 README）

做法：改 `.compass-adopt/plan.yaml`，把 A002 从 ambiguous[] 挪到 entries[]（拆成 ~8 条细 entries），重算 plan_sha，再跑 `/compass:adopt apply .compass-adopt/plan.yaml`。注意：当前 plan 已经 apply 过的话需要先 `rm -rf .compass-adopt/rollback/<旧-plan-sha>/`。

### [ ] 2. 细拆 tasks/ideas.md（A003）

当前整文件进 ambiguous[] recommendation=split。细拆计划：

- L1-22 Problem + Motivation + "Ideas to explore" 4 个 bullet → 4 条独立 BACKLOG P2 条目（每 bullet 一条 + 一条 umbrella）
- L23-26 "Related bugs caught in the wild" → LEARNINGS 条目（需先与 `CLAUDE.md` §Plugin agent type sandbox bug 去重，可能直接删）

做法：改 plan.yaml，把 A003 挪到 entries[]（拆成 ~4 条 BACKLOG + 可能 1 条 LEARNINGS），重算 plan_sha，再跑 apply。

---

## Reference

- 当前 plan.yaml plan_sha: `a8d9343ef0c1`（前 12 位）
- 当前已分类 entries[] = 4（E001-E004，全在 README.md L1-56）
- 当前 ambiguous[] = 4（A001 no-fit / A002 split / A003 split / A004 no-fit）
