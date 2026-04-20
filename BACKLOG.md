**Last updated**: 2026-04-19

## P0 — blocker / must-do-now

## P1 — high priority

## P1 — high priority

- Orchestrator conformance for `skills/review-loop/SKILL.md` §Protocol Imports MUST directive: currently only 3/5 declared docs (`session-file`, `planning`, `execution`) are Read reliably; `executor-output.md` + `reviewer-output.md` are skipped because their schemas are embedded in `agents/executor.md` + `agents/reviewer.md` (verified via `executor_schema_parity` / `reviewer_schema_parity` consistency_mappings). Either (a) tighten the orchestrator to Read all 5 at start, or (b) relax SKILL.md wording to mark the two output-schema docs as reference-only. Once decided, expand `review_loop_protocol_imports_read` from 3 paths to 5 and wire into all 6 Phase 2 Claude smoke cases. (added 2026-04-20)

## P2 — normal

- 给 subagent 配置不同的模型. 简单的工作用便宜的模型去做，只有复杂的 plan review 和 code review 用复杂的模型做. (added 2026-04-19)
- [partial] Extend scripts/run-skill-smoke to assert no subagent_type: review-loop:* appears in any Agent call during replay — replay harness exists but performs no subagent_type check today. (added 2026-04-19)
- [new] Add a dry-run Orchestrator mode that executes /review-loop against a fixture repo and validates the Agent-call sequence without writing files, so sandbox/agent-type bugs are caught pre-merge instead of via live tool_uses: 0 symptoms. (added 2026-04-19)
- [partial] Expand the claude_plugin_agent_type_forbidden assertion (tests/skills/contracts/review-loop.json:134-140) into context-aware lint coverage: distinguish forbidden Agent-invocation call-sites from legitimate "Never use subagent_type: review-loop:<name>" warnings already present in skills/execute/SKILL.md (L38-41) and skills/review-loop/SKILL.md (L49-52), extend applies_to across all SKILL.md bodies plus Codex-side agent-invocation surfaces, and add a paired assertion that every sandbox-affected agent name must carry an adjacent CRITICAL warning. (added 2026-04-19)
- [new] Build a session-replay parser over .review-loop/sessions/*.md that reconstructs which subagent_type values were used per Agent call and flags any review-loop:* occurrences as anomalies, giving a post-hoc audit channel independent of live observation. (added 2026-04-19)

## P3 — nice to have / someday

## Done (recent, trimmed quarterly)
