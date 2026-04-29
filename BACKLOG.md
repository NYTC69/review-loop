**Last updated**: 2026-04-29

## P0 — blocker / must-do-now

## P1 — high priority

~~Debug Claude review stall in manual / review-loop review flows.~~ (closed 2026-04-26 — root cause: `--output-format json` buffers all output until generation ends; sonnet-4-6 with extended thinking on 70k cached tokens takes 2-3 min, producing no visible output. With `--include-partial-messages` the first thinking_delta arrives at ~3.7s confirming the process is alive. Fix: switched Codex Stage 1 reviewer command to `--output-format stream-json --include-partial-messages`; orchestrator now scans line-by-line for `type == "result"` event. Updated SKILL.md, docs/protocol/planning.md, CLAUDE.md, and contract test needle.)

## P2 — normal

- [new] Add a dry-run Orchestrator mode that executes /review-loop against a fixture repo and validates the Agent-call sequence without writing files, so sandbox/agent-type bugs are caught pre-merge instead of via live tool_uses: 0 symptoms. (added 2026-04-19)
- [new] Build a session-replay parser over .review-loop/sessions/*.md that reconstructs which subagent_type values were used per Agent call and flags any review-loop:* occurrences as anomalies, giving a post-hoc audit channel independent of live observation. (added 2026-04-19)

## P3 — nice to have / someday

## Done (recent, trimmed quarterly)

- ~~Manual split `tasks/ideas.md` (compass adopt A003).~~ (closed 2026-04-19 — moved the four "Ideas to explore" bullets into `BACKLOG.md`, dropped already-duplicated "Related bugs caught in the wild" notes except for the surviving code-simplifier incident copied into `CLAUDE.md`, and kept `tasks/ideas.md` unchanged as the audit umbrella.)
- ~~给 subagent 配置不同的模型. 简单的工作用便宜的模型去做，只有复杂的 plan review 和 code review 用复杂的模型做.~~ (closed 2026-04-23 — cross-runtime judgment/cheap tiering landed across Codex Stage 1 + Claude paths; follow-up fixes included reviewer backstop alignment, unsupported `tier` field removal in `.codex/agents/*.toml`, noop smoke hardening, stale runtime cleanup, hook-injection guardrails, and timeout-budget tuning.)
- ~~[new] Make scripts/run-skill-smoke grader fall back to captured-events content when stream is missing `type=result`.~~ (closed 2026-04-27, see 29b483a — schema_errors only block when events list is also empty; otherwise content assertion runs directly with a partial-stream drift_note appended to PASS/FAIL messages. Two new fake-claude integration tests cover the partial-stream PASS and partial-stream FAIL paths.)
- ~~[new] Fix detached-descendant FD-seek corruption in scripts/run_skill_smoke_lib.finalize_stream_capture_artifact.~~ (closed 2026-04-27, see 29b483a — `_atomic_write_text` writes a fresh sibling and `os.replace`'s it over the destination, so any FD a surviving descendant still holds writes to the orphaned inode instead of corrupting the new payload. Unit test reproduces the corruption mode end-to-end and confirms a new inode plus zero-gap-free output.)
- ~~[partial] Expand the claude_plugin_agent_type_forbidden assertion into context-aware lint coverage.~~ (closed 2026-04-29, see 7616313 — new `forbidden_line_pattern` and `pattern_requires_adjacent` lint kinds plus `all-skill-bodies` scope; review-loop contract switches the forbidden check to a column-anchored regex and adds adjacent-CRITICAL guards for executor and code-simplifier name mentions; skills/plan/SKILL.md gains a CRITICAL marker so the new adjacency assertion holds; 8 new integration tests in tests/run_skill_lint_test.py cover the line-pattern and adjacency behaviors.)
