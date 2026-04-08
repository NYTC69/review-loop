# review-loop — Development Notes

## Known Pitfalls

### Plugin agent type sandbox bug (CRITICAL)

**ALL plugin-defined agent types have their tools silently blocked by Claude Code sandbox.** This applies to both `tools: all` AND `tools: read-only`. Agents invoked via `subagent_type: review-loop:<name>` get zero tool access — they cannot Read, Grep, or Bash. The result is `tool_uses: 0` and completely hallucinated output.

**Fix**: Always use `subagent_type: general-purpose` and inline the agent's `.md` body in the prompt. For read-only agents, add "Report only, do not modify files" to the prompt.

**History**: First discovered with Executor (`tools: all`) in commit `8506809`. We incorrectly concluded `tools: read-only` was safe. This wrong assumption was recorded in memory and carried forward through 4+ commits until `rust-reviewer` was caught hallucinating (issue #3). The root cause is Claude Code's sandbox, not the `tools:` declaration.

**Rule**: When adding ANY new agent invocation, always use `subagent_type: general-purpose` with inlined body. Never use `subagent_type: review-loop:<name>`.

### Plugin cache & version bump

- `plugin.json` and `marketplace.json` version **must** be bumped with **every single push** that changes any file. Without a version bump, `plugin update` thinks cache is current and won't pull new files. This includes "just documentation" or "just guide" changes — ANY change requires a bump.
- After `plugin update`, must open a **new session** — old sessions keep using the version loaded at startup.
- `/reload-plugins` does NOT switch versions.
- **Guide version is auto-bound**: `skills/guide/SKILL.md` reads version from `plugin.json` at runtime via `{VERSION}` placeholder. No manual sync needed.

## Design Philosophy

### Optional integrations must fail silently

review-loop is designed for a broad audience — not every user will have the same tools installed. Any integration with external tools (MemPalace, Graphify, etc.) **must be strictly optional**: if the tool is unavailable, the skill proceeds normally without degradation, without warnings, and without asking the user to install anything.

**Rule**: Before using any optional external tool, probe for its availability first (check MCP tool list or `which <cli>`). If unavailable, skip the step entirely and continue. Never make the skill depend on an optional integration.

**Why this matters**: review-loop's value is the Plan-Execute-Review loop itself. Optional integrations add convenience for users who have them, but must never become a barrier for users who don't. A skill that fails because MemPalace isn't installed has failed its core audience.

**How to implement**: wrap optional steps in an availability check:
```
if mempalace MCP tool is available OR `which mempalace` succeeds:
    → run optional step
else:
    → skip silently, continue
```

This principle applies to: MemPalace context retrieval (Step 1.6), any future Graphify integration, or any other optional tool.

## Agent Invocation Pattern

All agents must follow this pattern:

```
Agent tool parameters:
  subagent_type: general-purpose
  prompt: |
    {contents of agents/<agent-name>.md body}

    <task-specific instructions here>
```

This applies to: executor, reviewer, code-reviewer, silent-failure-hunter, comment-analyzer, type-design-analyzer, pr-test-analyzer, code-simplifier, go-reviewer, rust-reviewer, python-reviewer, frontend-security-reviewer.

### Agent hallucination guard

Even with `general-purpose`, agents may not use tools and fabricate output. Two defenses:

1. **Agent-side**: All language agents (rust/go/python/frontend-security) have a `**MANDATORY**` tool-use instruction at the top of their `.md` body.
2. **Orchestrator-side**: After every agent call, check `tool_uses` in metadata. If `tool_uses: 0`, discard result and retry once. If retry also fails, skip and report.
