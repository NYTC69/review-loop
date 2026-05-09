# review-loop — Development Notes

## Known Pitfalls

### Plugin agent type sandbox bug (CRITICAL)

**ALL plugin-defined agent types have their tools silently blocked by Claude Code sandbox.** This applies to both `tools: all` AND `tools: read-only`. Agents invoked via `subagent_type: review-loop:<name>` get zero tool access — they cannot Read, Grep, or Bash. The result is `tool_uses: 0` and completely hallucinated output.

**Fix**: Always use `subagent_type: general-purpose` and inline the agent's `.md` body in the prompt. For read-only agents, add "Report only, do not modify files" to the prompt.

**History**: First discovered with Executor (`tools: all`) in commit `8506809`. We incorrectly concluded `tools: read-only` was safe. This wrong assumption was recorded in memory and carried forward through 4+ commits (including a code-simplifier recurrence on 2026-04-06) until `rust-reviewer` was caught hallucinating (issue #3). The root cause is Claude Code's sandbox, not the `tools:` declaration.

**Rule**: When adding ANY new agent invocation, always use `subagent_type: general-purpose` with inlined body. Never use `subagent_type: review-loop:<name>`.

### README.md must stay intact (lint SSOT dependency)

`run-skill-lint`'s `guide:readme_marks_*` and `shared-schema:*` assertions treat several phrases inside `README.md` as the single source of truth (SSOT). Trimming or rewriting README content these assertions reach makes lint FAIL (5 cases observed during compass adopt Round 1). The compass-adopt migration handled this by **double-storing** the migrated blocks: ARCHITECTURE.md / CLAUDE.md / DESIGN.md gained `## Migrated — README.md:<lines>` blocks, but the README body was reverted to its full 370-line form so existing lint needles still resolve.

**Rule**: do not trim or restructure README.md without first updating both the `guide` skill and the lint contract to point their needles at the new SSOT (e.g. the migrated blocks in CLAUDE.md). The `## Migrated —` blocks are intentional duplication, not a cleanup target.

### Plugin cache & version bump

- `plugin.json` and `marketplace.json` version **must** be bumped with **every single push** that changes any file. Without a version bump, `plugin update` thinks cache is current and won't pull new files. This includes "just documentation" or "just guide" changes — ANY change requires a bump.
- After `plugin update`, must open a **new session** — old sessions keep using the version loaded at startup.
- `/reload-plugins` does NOT switch versions.
- **Guide version is auto-bound**: `skills/guide/SKILL.md` reads version from `plugin.json` at runtime via `{VERSION}` placeholder. No manual sync needed.

## Codex Stage 1 Notes

- Codex skills live under `.agents/skills/`.
- Codex subagents live under `.codex/agents/*.toml`.
- The Claude reviewer contract for Codex uses `claude -p --no-session-persistence --output-format stream-json --include-partial-messages < prompt-file`. Read stdout line-by-line; find the event where `type == "result"` and use its `result` field.
- This reviewer call must run outside the Codex sandbox.
- Sandbox diagnostic caveat: a sandboxed `claude -p` rehearsal is not a valid
  substitute for the real Codex reviewer path. If the sandboxed call fails,
  rerun the same command outside the Codex sandbox before changing protocol
  assumptions or falling back to Codex reviewer.
- In `codex exec --ephemeral`, subagent calls should use fresh self-contained
  prompts instead of relying on forked parent-thread context.
- `.review-loop/config.md` and `.review-loop/sessions/*.md` remain the shared protocol.
- **Stage 1 scope (current)** — Codex continues to expose only the
  repo-local `review-loop` + `guide` skills under `.agents/skills/`,
  but the shared Stage 1 runtime contract now carries the same broad
  `exec -> polish -> docs -> security -> delivery` lifecycle as Claude
  Code. Phase 3 split-skill work remains future Codex surface work, not
  a limitation of the current downstream lifecycle.
- **Parallel-CR library entry point** — `scripts/review_verification.py`
  is the conflict-aware parallel reviewer-fan-out scheduler (Codex
  Stage 1 scope only). Orchestrator wiring lives at the
  `Parallel Reviewer Fan-Out (N>1)` subsection in each of the three
  Codex Stage 1 SKILL files (`.agents/skills/{review-loop,plan,
  execute}/SKILL.md`); single-shot N=1 dispatch keeps the existing
  `claude -p` shell-out byte-identical and only N>1 fans out via
  `python3 scripts/review_verification.py --jobs <path> --output <path>`.
  Claude/plugin-side reviewer dispatch is in-process Agent-tool
  dispatch and is not externally wrappable.

## Design Philosophy

### Optional integrations must fail silently

review-loop is designed for a broad audience — not every user will have the same tools installed. Any integration with external tools (MemPalace, Graphify, etc.) **must be strictly optional**: if the tool is unavailable, the skill proceeds normally without degradation, without warnings, and without asking the user to install anything.

**Rule**: Before using any optional external tool, probe for its availability first (check MCP tool list or `which <cli>`). If unavailable, skip the step entirely and continue. Never make the skill depend on an optional integration.

**Rule 2**: Even after a successful availability probe, the tool may fail at runtime (misconfigured, hung, garbage output). **All runtime failures must also be caught and silently skipped.** The "fail silently" contract applies to the entire lifecycle — not just the initial probe.

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

All Claude/plugin-side agents must follow this pattern:

```
Agent tool parameters:
  subagent_type: general-purpose
  prompt: |
    {contents of agents/<agent-name>.md body}

    <task-specific instructions here>
```

This applies to the Claude/plugin-side agents: executor, reviewer, code-reviewer, silent-failure-hunter, comment-analyzer, type-design-analyzer, pr-test-analyzer, code-simplifier, go-reviewer, rust-reviewer, python-reviewer, frontend-security-reviewer.

Codex Stage 1 runtime agents are defined separately under `.codex/agents/*.toml`
and do not use this Claude-specific invocation pattern.

### Agent hallucination guard

Even with `general-purpose`, agents may not use tools and fabricate output. Two defenses:

1. **Agent-side**: All language agents (rust/go/python/frontend-security) have a `**MANDATORY**` tool-use instruction at the top of their `.md` body.
2. **Orchestrator-side**: After every agent call, check `tool_uses` in metadata. If `tool_uses: 0`, discard result and retry once. If retry also fails, skip and report.

<!-- 迁移自 README.md:1-4 via compass:adopt 于 2026-04-19 plan=a8d9343ef0c1 -->
## Migrated — README.md:1-4

# review-loop

A Claude Code plugin for AI-driven code review, with a Codex Stage 1 repo-skill path alongside the Claude/plugin implementation.

<!-- 迁移自 README.md:5-25 via compass:adopt 于 2026-04-19 plan=a8d9343ef0c1 -->
## Migrated — README.md:5-25

## Quick Start

```
/plugin marketplace add NYTC69/review-loop
/plugin install review-loop@review-loop-marketplace
```

Start a new session. The `/review-loop` command is now available in all your projects.

**Optional** — copy the config template to customize per-project defaults:

```bash
mkdir -p .review-loop
cp ~/.claude/plugins/cache/review-loop/review-loop-config.example.md .review-loop/config.md
```

> **After updating the plugin** — Claude Code caches plugins at session
> start. After `/plugin update`, exit with Ctrl-C twice and `claude --resume`
> to reload plugins while keeping your conversation context. This is a
> Claude Code caching behavior, not a review-loop limitation.

<!-- 迁移自 README.md:43-56 via compass:adopt 于 2026-04-19 plan=a8d9343ef0c1 -->
## Migrated — README.md:43-56

## Skill Tests

The repository includes a first-version skill testing framework for
`review-loop` and `guide`.

- `scripts/run-skill-lint` runs static contract checks
- `scripts/run-skill-smoke` runs the small real smoke suite
- `scripts/run-skill-tests` runs both in order

Test output uses `PASS`, `FAIL`, and `SKIP`.

- Aggregate results: `tests/skills/.last-run.json`
- Per-case artifacts: `tests/skills/.artifacts/`

<!-- 迁移自 README.md:224-261 via compass:adopt 于 2026-04-19 plan=e2439220c6bd -->
## Migrated — README.md:224-261

## Configuration

All options live in `.review-loop/config.md`. Every field is optional.

| Key | Default | Description |
|-----|---------|-------------|
| `reviewer` | `codex` | Shared Claude/plugin reviewer mode; Codex Stage 1 does not use this key to choose the reviewer backend |
| `reviewer_model` | `""` | codex: `--model` flag; subagent: Agent `model` param (empty = inherit) |
| `executor_model` | `inherit` | Shared Claude/plugin executor-model key; ignored by Codex Stage 1 |
| `soft_limit_plan` | `3` | After N rounds, ask user to continue if CRITICALs remain |
| `soft_limit_exec` | `3` | Same for execution phase |
| `auto_commit` | `false` | Stage changed files and commit after delivery |
| `commit_message_prefix` | `feat` | Conventional commit type prefix |
| `docs_file` | `CHANGELOG.md` | File to append delivery summary; `""` to skip |
| `handsfree` | `false` | Default to hands-free mode (decisions go to Reviewer) |
| `review_focus` | `""` | Project-specific review priorities (free text) |
| `quality_focus` | `""` | `quality_focus` applies only when Step 3.5 Quality Polish actually runs |
| `review_style` | `""` | Tone and rules for all reviews (free text) |
| `skip_quality_polish` | `false` | `skip_quality_polish: true` mints `polish` as a no-op completion and still continues through docs and security |

For Codex Stage 1, `reviewer_model` controls the default Claude CLI reviewer
path, `codex_reviewer_backend` selects the local Codex fallback reviewer path,
and `codex_reviewer_model` overrides the model used by that Codex fallback
reviewer path. When neither `reviewer_model` nor `judgment_model` is set,
that default Claude reviewer path backstops to `claude-sonnet-4-6`. The
`reviewer` and `executor_model` entries above still
describe shared Claude/plugin-side behavior and do not actively control
Stage 1 Codex behavior.

### Natural language config examples

```yaml
review_focus: |
  - Security: auth checks, input validation, SQL injection
  - Performance: N+1 queries, missing indexes

quality_focus: "strict clippy lints, skip comment analysis"

review_style: "be terse, flag any unwrap() as CRITICAL"
```
