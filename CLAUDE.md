# review-loop — Development Notes

User documentation: README.md (setup, commands, configuration, tests) and `review-loop-config.example.md` (config keys).

## Known Pitfalls

### Plugin agent `tools:` frontmatter

`tools:` is a list of tool names. A policy word such as `read-only` or `all` is not a tool name: Claude Code then
resolves the agent to zero tools (older versions spawned it anyway and it hallucinated; current versions refuse the
spawn). This was the "plugin agents are sandboxed" bug, fixed in v2.8.2.

- Read-only agents declare `tools: Read, Grep, Glob, Bash` (`reviewer`: `Read, Grep, Glob`); agents that edit files
  (`executor`, `code-simplifier`) omit `tools`. Valid values are tool names, `*`, or an omitted field.
- The `Bash` in report-only agents is for direct user invocation (`git diff`, static analysis); the coordinator
  dispatches those roles with the reviewer role's own permissions (`docs/protocol/reviewer-runtime.md`).

**Rule**: the paired-session coordinator runs every role as a CLI turn of its own (see Agent Invocation Pattern); no skill spawns a role through the Agent tool today. If one ever does, use `subagent_type: general-purpose` with the agent body inlined. Never use `subagent_type: review-loop:<name>`. A writable general-purpose agent cannot serve as a permission boundary for a report-only reviewer.

### Lint needles pin doc sentences

The `scripts/run-skill-lint` contracts (`tests/skills/contracts/*.json`) pin exact sentences in README.md, the skills,
the protocol docs and a few other files (this one included). When you change a pinned sentence, update its assertion
in the same commit and name the change in the commit or report.

### Configured-test evidence

Read-only roles reuse a passing configured test from the latest eligible completed tool observation for the exact
command and workspace snapshot; a later eligible failure supersedes it. A current, intact, passing
`attach-verification` record also supplies proof and settles sandbox-only failures. The persistent EXEC reviewer
still reports commands it ran itself in `self_run_evidence`, also when approval uses a reused result. Missing host
verification is never a finding.

### Paired-session runs on this repository

`.review-loop/config.md` sets `entry: paired-session`. A run on this repository uses the coordinator from a released
copy (the plugin cache or a pinned clone `~/paired-runs/review-loop-v<version>`), never this working tree or a lane
worktree, because the run may edit the coordinator it is executing.

### Plugin cache & version bump

- Bump `plugin.json` and `marketplace.json` with **every push that changes any file**, docs included; otherwise
  `plugin update` treats the cache as current.
- After an update, open a **new session**; `/reload-plugins` does not switch versions.
- `skills/guide/SKILL.md` reads the version from `plugin.json` (`{VERSION}`); no manual sync.

### Codex marketplace surface

review-loop is a dual-runtime plugin. The Claude path (`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`,
`skills/`) and the Codex path (`.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json`, `.agents/skills/`)
are independent install surfaces. The Codex path needs all three of: the manifest, the marketplace file (without it no
plugin appears in `/plugins`), and the tracked symlink `plugins/review-loop` → `..` (mode `120000`). Slash commands
are Claude-only; Codex triggers skills by their `description`. Install steps: [`docs/install-codex.md`](docs/install-codex.md).

## Codex Notes

- Codex skills live under `.agents/skills/`: `review-loop`, `guide`, `paired-session`, `review-pr`.
- The Codex paired-session skill runs the coordinator outside the Codex sandbox (full host permission); a sandboxed
  rehearsal is not a valid substitute.
- `.review-loop/config.md` is shared by both hosts; legacy `.review-loop/sessions/*.md` files are never read.
- **Stage-scoped instructions**: the entry skills (`review-loop` and `paired-session` on both hosts) read exact
  authoritative sections through `docs/protocol/loading.md` and `scripts/read_protocol.py` before the relevant action
  (stages `entry-review-loop`, `entry-paired-session` and its route stages `entry-paired-session-review-only` /
  `-code-quality-loop` / `-review-pr`). Shared protocol files remain the SSOT; a link alone is not an eager import, and
  new or compacted contexts reload prerequisites. The paired-session entry skills load
  `docs/protocol/paired-session-entry.md` this way and keep only host rules in their `SKILL.md`.

## Design Philosophy

### Optional integrations must fail silently

Any external tool integration (MemPalace, Graphify, …; none is wired today) is strictly optional: probe for it first
(MCP tool list or `which <cli>`), skip silently when absent, and catch and silently skip every runtime failure too
(misconfigured, hung, garbage output). Never warn, degrade, or ask the user to install anything; the
Plan-Execute-Review loop is the product.

## Agent Invocation Pattern

The coordinator (`paired_session/coordinator.py`) starts every role as its own Codex or Claude CLI turn with that
role's sandbox and a JSON answer schema:
- author, finisher, docs writer and the POLISH-Q writers write in the workspace; the code-simplifier writer inlines
  `agents/code-simplifier.md`;
- the reviewer (persistent across rounds), the shadow, the gate and the stage reviewers do not change the workspace
  (a Codex one runs workspace-write and the post-turn guard undoes tracked and non-ignored changes); the POLISH-Q
  specialists inline their `agents/<name>.md` body (`_specialist_turn`);
- every `agents/*.md` is hashed into the frozen role manifest: changing one aborts resume of an in-flight run, so
  agent edits ship only in a release that says so.

### Agent hallucination guard

1. **Agent-side**: the language agents (rust/go/python/frontend-security) start by reading every in-scope file and
   base the report only on that.
2. **Coordinator-side**: a specialist or security turn without tool calls is retried once; a second empty turn fails
   closed (HOLD). An EXEC approval also needs the reviewer's own run evidence (`self_run_evidence`), except in a
   review-pr report run without a test command (a static approval).

## Development checks

- `scripts/run-skill-lint` (0 FAIL; contracts in `tests/skills/contracts/`).
- `python3 -m pytest -q tests` (the scripts).
- `python3 -m unittest discover -s paired_session -p 'test_*.py'` (the coordinator; fake CLIs, no provider call;
  the full suite is long, so run single modules while iterating).
- `git diff --check`.

paired-session runs on macOS and native Windows (efficient mode only; ADR-20); Linux is not supported.
