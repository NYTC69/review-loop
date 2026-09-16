# Claude runtime contracts

## Orchestrator rules

- **Plugin agent-type sandbox bug**: every Executor / Reviewer / quality
  agent invocation MUST use `subagent_type: general-purpose` with the
  agent's full `.md` body inlined in the `prompt` parameter. Never use
  `subagent_type: review-loop:<name>`. See `CLAUDE.md` §"Plugin agent
  type sandbox bug" for background.
- Only the Orchestrator writes to the session file. Sub-agents read.
- Live Reports after each round are not optional.

---

## Configuration

Read `.review-loop/config.md` if present, else defaults:

```yaml
reviewer: codex                 # "codex" | "subagent"
reviewer_model: ""              # path-specific reviewer override
judgment_model: ""              # shared tier override for judgment-tier agents
cheap_model: ""                 # shared tier override for cheap-tier agents
executor_model: inherit         # path-specific Claude executor override; "" and inherit fall through to judgment_model
soft_limit_plan: 3              # see docs/protocol/planning.md §Loop control
soft_limit_exec: 3              # see docs/protocol/execution.md §Per-stage max-round caps
auto_commit: false
commit_message_prefix: "feat"
docs_file: CHANGELOG.md
handsfree: false
review_focus: ""                # free text injected into code-review prompts only
quality_focus: ""               # `quality_focus` applies only when Step 3.5 Quality Polish actually runs.
review_style: ""                # free text injected into ALL reviewer prompts
skip_quality_polish: false      # `skip_quality_polish: true` mints `polish` as a no-op completion and still continues through docs and security.
```

`--handsfree` flag at invocation overrides the config value.

Shared tier contract:

- Dispatch precedence is path-specific override -> tier override -> runtime
  backstop.
- Missing `tier` defaults to `judgment`.
- Cheap-tier backstop is `claude-haiku-4-5-20251001`.
- In Codex Stage 1, `cheap_model` is accepted by the shared config but is a
  documented no-op because Stage 1 only ships judgment-tier Codex agents.
- In Codex Stage 1, review stays on the outside-sandbox Claude CLI reviewer
  path unless `codex_reviewer_backend: codex` is explicitly set.
- On that default Codex Stage 1 Claude reviewer path, the model resolves as
  `reviewer_model` -> `judgment_model` -> `claude-sonnet-4-6`.

---
