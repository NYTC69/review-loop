# review-loop — Development Notes

## Known Pitfalls

### Plugin agent `tools:` frontmatter (root cause of the "sandbox bug")

**Root cause (found 2026-09-19, fixed in v2.8.2):** `tools:` is a list of tool names. The old values `read-only` and `all` are not tool names, so Claude Code resolved every review-loop agent to **zero tools**. Older Claude Code versions spawned them anyway, giving `tool_uses: 0` and hallucinated output; Claude Code 2.1.278 refuses the spawn with `would be spawned with zero tools — unrecognized [read-only]` (or `[all]`). This was never a sandbox restriction on plugin agents.

**Fix:** read-only agents declare `tools: Read, Grep, Glob, Bash` (no Edit/Write; `reviewer` declares `Read, Grep, Glob`); agents that edit files (`executor`, `code-simplifier`) omit `tools` and inherit everything. Never put a policy word in `tools:` — valid values are tool names, `*`, or an omitted field.

**Bash in report-only agents:** the `Bash` in those frontmatters is for direct user invocation only (`git diff`, static analysis). The paired-session coordinator dispatches report-only roles as reviewer-role turns with that role's own permissions; see `docs/protocol/reviewer-runtime.md` (owner decision 2026-10-04: keep Bash, align the docs; the legacy launchers were removed in v2.13.1).

**History:** first seen with Executor (`tools: all`) in commit `8506809`, then with `code-simplifier` (2026-04-06) and `rust-reviewer` (issue #3). Each time the conclusion was "plugin agent types are sandboxed", so the protocol switched to `subagent_type: general-purpose` with the agent body inlined in the prompt.

**Rule**: Every writer-agent invocation uses `subagent_type: general-purpose` with the agent body inlined. Never use `subagent_type: review-loop:<name>`. The agents resolve real tools, so this is a protocol convention rather than a workaround; moving the protocol to native agent types is a separate change — do not mix it into unrelated work. Report-only reviewers instead run as paired-session reviewer-role turns (`docs/protocol/reviewer-runtime.md`); a writable general-purpose agent cannot serve as a permission boundary.

### README.md must stay intact (lint SSOT dependency)

`run-skill-lint`'s `guide:readme_marks_*` and `shared-schema:*` assertions treat several phrases inside `README.md` as the single source of truth (SSOT). Trimming or rewriting README content these assertions reach makes lint FAIL (5 cases observed during compass adopt Round 1). The compass-adopt migration handled this by **double-storing** the migrated blocks: ARCHITECTURE.md / CLAUDE.md / DESIGN.md gained `## Migrated — README.md:<lines>` blocks, but the README body was reverted to its full 370-line form so existing lint needles still resolve.

**Rule**: do not trim or restructure README.md without first updating both the `guide` skill and the lint contract to point their needles at the new SSOT (e.g. the migrated blocks in CLAUDE.md). The `## Migrated —` blocks are intentional duplication, not a cleanup target.

### This repository uses the paired-session entry

`.review-loop/config.md` sets `entry: paired-session` (owner 2026-10-06). From 2026-10-03 until then it pinned `entry: legacy`, under the owner rule that review-loop itself was not developed through paired-session; that rule is lifted.

**Rule**: a paired-session run on this repository runs the coordinator from a released copy (the plugin cache or a pinned clone `~/paired-runs/review-loop-v<version>`), never from this working tree or a lane worktree, because the run may edit the coordinator it is executing. The supervisor's lane workflow (detached Opus executors, Codex review rounds, Opus gate) is separate from this key.

### Plugin cache & version bump

- `plugin.json` and `marketplace.json` version **must** be bumped with **every single push** that changes any file. Without a version bump, `plugin update` thinks cache is current and won't pull new files. This includes "just documentation" or "just guide" changes — ANY change requires a bump.
- After `plugin update`, must open a **new session** — old sessions keep using the version loaded at startup.
- `/reload-plugins` does NOT switch versions.
- **Guide version is auto-bound**: `skills/guide/SKILL.md` reads version from `plugin.json` at runtime via `{VERSION}` placeholder. No manual sync needed.

### Codex marketplace surface (parallel to Claude plugin surface)

review-loop is a **dual-runtime plugin**. The Claude Code plugin path
(`.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` + top-level
`skills/`) and the Codex plugin path
(`.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json` + `.agents/skills/`)
are independent install surfaces that share repo content (config + sessions).

The Codex marketplace surface specifically requires:

1. `.codex-plugin/plugin.json` — Codex plugin manifest.
2. `.agents/plugins/marketplace.json` — Codex marketplace manifest. Lists
   plugins available in this marketplace with `installation: AVAILABLE`
   policy. Without this file, `codex plugin marketplace add` registers a
   marketplace entry but no plugins surface in `/plugins`, so a fresh
   Codex session cannot enable review-loop.
3. `plugins/review-loop` symlink → `..` (the repo root). The marketplace
   manifest points at `./plugins/review-loop` as the plugin source path;
   the symlink is what makes the path resolve to the repo. Tracked in git
   as a real symlink (mode `120000`), mirrors the compass plugin layout.

Codex install flow verified with Codex CLI 0.155.1:
`codex plugin marketplace add NYTC69/review-loop` registers the marketplace;
`codex plugin add review-loop@review-loop-marketplace` installs the plugin.
Start a fresh Codex session to load its skills. Older Codex versions may use
the `/plugins` UI; check `codex plugin --help` rather than assuming the CLI
install command exists.

Slash commands like `/review-loop:paired-session` are Claude-only. Codex matches
plugin skills via their `SKILL.md` `description` field; trigger is
natural-language only. Full step-by-step + verification:
[`docs/install-codex.md`](docs/install-codex.md).

## Codex Notes

- Codex skills live under `.agents/skills/`: `review-loop`, `guide`, `paired-session`, `review-pr`. The Codex
  Stage 1 workflow (its plan/execute skills, `.codex/agents/*.toml`, the reviewer launchers
  `scripts/run_claude_reviewer.py` / `run_codex_reviewer.py` and the parallel-review scheduler) was removed in
  v2.13.1 with the rest of the legacy workflow; the paired-session coordinator dispatches every role itself.
- The Codex paired-session skill runs the coordinator outside the Codex sandbox (full host permission); a
  sandboxed rehearsal is not a valid substitute for that path.
- In `codex exec --ephemeral`, subagent calls should use fresh self-contained
  prompts instead of relying on forked parent-thread context.
- `.review-loop/config.md` is shared by both hosts; legacy `.review-loop/sessions/*.md` files are left on disk
  and never read.

- **Stage-scoped instructions** — the entry skills (`review-loop` and `paired-session` on both hosts) use
  `docs/protocol/loading.md` and `scripts/read_protocol.py` to read exact
  authoritative sections before the relevant action (stages `entry-review-loop` and `entry-paired-session`).
  Shared protocol files remain the SSOT; a link alone is not an eager import. New agents and new or
  compacted contexts reload prerequisites. Runtime entry details live in each
  skill's `references/entry.md`. Loading does not change stage/gate semantics.
  The two paired-session entry skills load their shared contract,
  `docs/protocol/paired-session-entry.md`, the same way (stage
  `entry-paired-session`) and keep only host rules in their `SKILL.md`.

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

Claude/plugin-side writer agents follow this pattern:

```
Agent tool parameters:
  subagent_type: general-purpose
  prompt: |
    {contents of agents/<agent-name>.md body}

    <task-specific instructions here>
```

This applies to writer roles such as executor, code-simplifier and test-consolidation authors. Report-only reviewer/specialist roles are dispatched by the paired-session coordinator (`docs/protocol/reviewer-runtime.md`).

### Agent hallucination guard

Even with `general-purpose`, agents may not use tools and fabricate output. Two defenses:

1. **Agent-side**: All language agents (rust/go/python/frontend-security) open their `.md` body with an instruction to run the analysis commands and read every in-scope file before any analysis, and to base the report only on that output.
2. **Orchestrator-side**: After every agent call, check `tool_uses` in the Agent metadata. If `tool_uses: 0`, discard result and retry once. If retry also fails, skip and report. The paired-session coordinator applies the same guard to its specialist turns (a turn without tool calls is discarded and retried once).

<!-- 迁移自 README.md:1-4 via compass:adopt 于 2026-04-19 plan=a8d9343ef0c1 -->
## Migrated — README.md:1-4

Snapshot of README.md as of 2026-04-19 (compass adopt); README.md is current.

# review-loop

A Claude Code plugin for AI-driven code review, with a Codex Stage 1 repo-skill path alongside the Claude/plugin implementation.

<!-- 迁移自 README.md:5-25 via compass:adopt 于 2026-04-19 plan=a8d9343ef0c1 -->
## Migrated — README.md:5-25

Snapshot of README.md as of 2026-04-19 (compass adopt); README.md is current.

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

Snapshot of README.md as of 2026-04-19 (compass adopt); README.md is current.

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

Snapshot of README.md as of 2026-04-19 (compass adopt); README.md is current.

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
| `adversarial_gate_skip_paths` | `["**/SKILL.md", "docs/protocol/**", "tests/skills/contracts/**"]` | Step 3.4 terminal adversarial gate — skip when every Step 3 changed file matches one of these glob patterns |

For Codex Stage 1, `reviewer_model` controls the default Claude CLI reviewer
path, `codex_reviewer_backend` selects the local Codex fallback reviewer path,
and `codex_reviewer_model` overrides the model used by that Codex fallback
reviewer path. When neither `reviewer_model` nor `judgment_model` is set,
that default Claude reviewer path backstops to `claude-opus-5-5`. The
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
