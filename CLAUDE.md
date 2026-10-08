# review-loop — Development Notes

## Known Pitfalls

### Plugin agent `tools:` frontmatter (root cause of the "sandbox bug")

**Root cause (found 2026-09-19, fixed in v2.8.2):** `tools:` is a list of tool names. The old values `read-only` and `all` are not tool names, so Claude Code resolved every review-loop agent to **zero tools**. Older Claude Code versions spawned them anyway, giving `tool_uses: 0` and hallucinated output; Claude Code 2.1.278 refuses the spawn with `would be spawned with zero tools — unrecognized [read-only]` (or `[all]`). This was never a sandbox restriction on plugin agents.

**Fix:** read-only agents declare `tools: Read, Grep, Glob, Bash` (no Edit/Write; `reviewer` declares `Read, Grep, Glob`); agents that edit files (`executor`, `code-simplifier`) omit `tools` and inherit everything. Never put a policy word in `tools:` — valid values are tool names, `*`, or an omitted field.

**Bash in report-only agents:** the `Bash` in those frontmatters is for direct user invocation only (`git diff`, static analysis). The paired-session coordinator dispatches report-only roles as reviewer-role turns with that role's own permissions; see `docs/protocol/reviewer-runtime.md` (owner decision 2026-10-04: keep Bash, align the docs; the legacy launchers were removed in v2.13.1).

**History:** first seen with Executor (`tools: all`) in commit `8506809`, then with `code-simplifier` (2026-04-06) and `rust-reviewer` (issue #3). Each time the conclusion was "plugin agent types are sandboxed", so the protocol switched to `subagent_type: general-purpose` with the agent body inlined in the prompt.

**Rule**: the paired-session coordinator runs every role as a CLI turn of its own (see Agent Invocation Pattern); no skill spawns a role through the Agent tool today. If one ever does, use `subagent_type: general-purpose` with the agent body inlined. Never use `subagent_type: review-loop:<name>`. A writable general-purpose agent cannot serve as a permission boundary for a report-only reviewer.

### Lint needles pin doc sentences

The `scripts/run-skill-lint` contracts (`tests/skills/contracts/*.json`) pin exact sentences in README.md, the skills,
the protocol docs and a few other files.

**Rule**: move needle and text together: when you change a pinned sentence, update its assertion or mapping in the
same commit and name the change in the commit or report.

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
are independent install surfaces that share repo content (config).

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
- `.review-loop/config.md` is shared by both hosts; legacy `.review-loop/sessions/*.md` files are left on disk
  and never read.

- **Stage-scoped instructions** — the entry skills (`review-loop` and `paired-session` on both hosts) use
  `docs/protocol/loading.md` and `scripts/read_protocol.py` to read exact
  authoritative sections before the relevant action (stages `entry-review-loop` and `entry-paired-session`).
  Shared protocol files remain the SSOT; a link alone is not an eager import. New agents and new or
  compacted contexts reload prerequisites. The review-loop skills keep their entry
  procedures in `references/entry.md`. Loading does not change stage/gate semantics.
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

This principle applies to any optional integration (MemPalace, Graphify or another tool); none is wired today.

## Agent Invocation Pattern

The coordinator (`paired_session/coordinator.py`) starts every role as its own Codex or Claude CLI turn with that
role's sandbox and a JSON answer schema:
- author, finisher, docs writer and the POLISH-Q writers write in the workspace; the code-simplifier writer inlines
  `agents/code-simplifier.md`;
- the reviewer (persistent across rounds), the shadow, the gate and the stage reviewers are read-only; the POLISH-Q
  specialists inline their `agents/<name>.md` body (`_specialist_turn`);
- `agents/executor.md` and `agents/reviewer.md` are hashed into the frozen role manifest, and every `agents/*.md` is
  hashed: changing one aborts resume of an in-flight run, so agent edits ship only in a release that says so.

### Agent hallucination guard

A role may answer without using its tools. Two defenses:

1. **Agent-side**: All language agents (rust/go/python/frontend-security) open their `.md` body with an instruction to read every in-scope file before any analysis and to base the report only on that.
2. **Coordinator-side**: a specialist or security turn without tool calls is discarded and retried once; a second empty turn fails closed (HOLD). An EXEC approval also needs the reviewer's own run evidence (`self_run_evidence`), except in a review-pr
   report run without a test command (a static approval).

## Development checks

- `scripts/run-skill-lint` (0 FAIL; contracts in `tests/skills/contracts/`).
- `python3 -m pytest -q tests` (the scripts).
- `python3 -m unittest discover -s paired_session -p 'test_*.py'` (the coordinator; fake CLIs, no provider call;
  the full suite is long, so run single modules while iterating).
- `git diff --check`.
paired-session runs on macOS only. Every push that changes a file bumps the versions (Plugin cache & version bump).

## README snapshots

The `## Migrated — README.md` snapshot blocks of 2026-04-19 were dropped (ADR-17 V9). The user documentation is
README.md (setup, commands, configuration, tests) and `review-loop-config.example.md` (the config keys).
