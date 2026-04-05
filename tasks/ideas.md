# Ideas Backlog

## Skill Testing Framework

**Problem**: Skill bugs (agent hallucination, wrong subagent_type, sandbox issues) are extremely
hard to debug and impossible to test without actually running a full workflow. There's no way to
write a unit test for "does the Orchestrator use general-purpose or review-loop:code-simplifier?"

**Motivation**: The code-simplifier sandbox bug took multiple real-world sessions to diagnose.
The only signal was `tool_uses: 0` in live output — no reproducible test case, no CI.

**Ideas to explore**:
- A test harness that replays a canned `/review-loop` run and inspects which `subagent_type`
  was used for each Agent call (assertion: no `review-loop:*` agent types allowed)
- A dry-run mode that executes the Orchestrator against a fixture repo and validates the
  Agent call sequence without actually writing files
- A "skill lint" tool that statically checks SKILL.md for known anti-patterns:
  - `subagent_type: review-loop:*` anywhere in the instruction text
  - Missing CRITICAL warnings near known sandbox-affected agent names
- Session replay: parse existing `.review-loop/sessions/*.md` files to reconstruct
  what agent types were used and flag anomalies

**Related bugs caught in the wild (no test existed)**:
- Executor using `review-loop:executor` → tools blocked (commit 8506809)
- rust-reviewer using plugin type → `tool_uses: 0` (issue #3)
- code-simplifier using `review-loop:code-simplifier` → `tool_uses: 0` (2026-04-06)
