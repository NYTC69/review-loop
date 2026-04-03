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
