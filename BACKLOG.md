**Last updated**: 2026-04-25

## P0 — blocker / must-do-now

## P1 — high priority

- [new] Debug Claude review stall in manual / review-loop review flows. Repro is now cross-session rather than isolated: trivial prompts return quickly on the host, but `claude -p` review runs can sit for minutes with no incremental output; `--bare` fails with `Not logged in`; `--setting-sources project` strips user-level plugins/hooks but does not eliminate the stall. Need a systematic repro matrix across TTY / non-TTY, `--setting-sources`, `--add-dir`, manual review prompts, and review-loop reviewer prompts; then pin down whether the stall is caused by plugins/hooks/statusline, auth, streaming, network, session persistence, or something in the review-loop default Claude reviewer path. (added 2026-04-25)

## P2 — normal

- [new] Add a dry-run Orchestrator mode that executes /review-loop against a fixture repo and validates the Agent-call sequence without writing files, so sandbox/agent-type bugs are caught pre-merge instead of via live tool_uses: 0 symptoms. (added 2026-04-19)
- [partial] Expand the claude_plugin_agent_type_forbidden assertion (tests/skills/contracts/review-loop.json:134-140) into context-aware lint coverage: distinguish forbidden Agent-invocation call-sites from legitimate "Never use subagent_type: review-loop:<name>" warnings already present in skills/execute/SKILL.md (L38-41) and skills/review-loop/SKILL.md (L49-52), extend applies_to across all SKILL.md bodies plus Codex-side agent-invocation surfaces, and add a paired assertion that every sandbox-affected agent name must carry an adjacent CRITICAL warning. (added 2026-04-19)
- [new] Build a session-replay parser over .review-loop/sessions/*.md that reconstructs which subagent_type values were used per Agent call and flags any review-loop:* occurrences as anomalies, giving a post-hoc audit channel independent of live observation. (added 2026-04-19)

## P3 — nice to have / someday

## Done (recent, trimmed quarterly)

- ~~Fix completed-agent cleanup in the Codex Stage 1 review-loop orchestrator so normal planning/execution loops do not hit `agent thread limit reached (max 6)`.~~ (closed 2026-04-25 — Codex Stage 1 now documents completed subagent cleanup before new spawns and after planning/execution/local-reviewer rounds; protocol docs carry the shared rule; contract/unit coverage added; live smoke follow-up stabilized guide semantic assertions and bounded best-effort live smoke TTL to 120s.)
- ~~Orchestrator conformance for `skills/review-loop/SKILL.md` / `skills/{plan,execute}/SKILL.md` §Protocol Imports MUST directive.~~ (closed 2026-04-25 — live Claude smoke validated the strengthened imports-read assertions on the target `tool_use_events` cases; current prompt tweak in `plan.fresh.smoke.claude` makes the single-round plan case reviewer-approvable again under the stricter Codex reviewer.)
- ~~Run a fresh Claude smoke pass for the `tool_use_events` cases to validate `no_forbidden_review_loop_subagent_types_in_agent_calls` on live stream output.~~ (closed 2026-04-25 — main-based live Claude smoke passed on `plan.fresh`, `execute.{from-plan,review-only,session-resume,stop-after-before-polish,stop-after-polish,stop-after-before-security}`, and `review-loop.regression`.)
- ~~Extend `scripts/run-skill-smoke` to assert no `subagent_type: review-loop:*` appears in any Agent call during replay.~~ (closed 2026-04-25 — stream capture, assertion wiring, and live Claude validation are all complete.)
- ~~Manual split README.md L64-370 (compass adopt A002).~~ (closed 2026-04-19 — Round 3 apply `plan_sha e2439220c6bd` split A002 into E005-E012 plus A005 no-fit; landed the migrated blocks in `ARCHITECTURE.md`, `CLAUDE.md`, and `DESIGN.md`; verify byte-exact passed; README itself stayed intact because lint still treats README phrases as SSOT.)
- ~~Manual split `tasks/ideas.md` (compass adopt A003).~~ (closed 2026-04-19 — moved the four "Ideas to explore" bullets into `BACKLOG.md`, dropped already-duplicated "Related bugs caught in the wild" notes except for the surviving code-simplifier incident copied into `CLAUDE.md`, and kept `tasks/ideas.md` unchanged as the audit umbrella.)
- ~~给 subagent 配置不同的模型. 简单的工作用便宜的模型去做，只有复杂的 plan review 和 code review 用复杂的模型做.~~ (closed 2026-04-23 — cross-runtime judgment/cheap tiering landed across Codex Stage 1 + Claude paths; follow-up fixes included reviewer backstop alignment, unsupported `tier` field removal in `.codex/agents/*.toml`, noop smoke hardening, stale runtime cleanup, hook-injection guardrails, and timeout-budget tuning.)
