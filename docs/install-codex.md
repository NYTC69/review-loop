# Install review-loop on Codex CLI

review-loop is dual-runtime: the same repo ships a Claude Code plugin
(`.claude-plugin/`) and a Codex CLI plugin (`.codex-plugin/` + `.agents/`).
This document covers the Codex install path; for Claude Code see the
top-level `README.md` Quick Start.

## Prerequisites

- **Codex CLI 0.155.1 or later (verified)** — older releases may require the
  `/plugins` UI path; check `codex plugin --help` before using the CLI install
  command documented below.
- **Python ≥ 3.11** — `scripts/run_skill_smoke_lib.py` and other helpers
  used by review-loop's smoke / lint suites depend on it.
- **git** — used by the executor / reviewer agents and by the smoke
  harness to scope diffs.
- **Claude CLI** (optional but recommended) — review-loop's Codex Stage 1
  default reviewer path shells out to `claude -p` outside the Codex
  sandbox. Without it, opt into the local Codex reviewer with
  `codex_reviewer_backend: codex` in `.review-loop/config.md`.

## Install path: marketplace + CLI install

review-loop publishes a Codex marketplace manifest at
`.agents/plugins/marketplace.json` and a `plugins/review-loop` symlink
that points back at the repo root. The two together make the repo a
first-class Codex plugin source.

```bash
# Register the marketplace (remote git URL or local path both work)
codex plugin marketplace add NYTC69/review-loop
codex plugin add review-loop@review-loop-marketplace
# or, when developing review-loop locally:
codex plugin marketplace add /path/to/review-loop
codex plugin add review-loop@review-loop-marketplace
```

Start a fresh Codex session after installation. The plugin cache is under
`$CODEX_HOME/plugins/cache/review-loop-marketplace/review-loop/<version>/`
(default `~/.codex/plugins/cache/review-loop-marketplace/review-loop/<version>/`).
The paired-session skill obtains the installed version from
`codex plugin list --json` so it can invoke its bundled coordinator after
plugin updates.

Uninstall the plugin and optionally remove its marketplace:

```bash
codex plugin remove review-loop@review-loop-marketplace
codex plugin marketplace remove review-loop-marketplace
```

## Triggering workflows in a Codex session

Codex matches plugin skills via their `SKILL.md` `description` field;
literal slash commands like `/review-loop:plan` are Claude-Code-only and
surface as `Unrecognized command` in Codex. Use natural language:

| What you want | Say |
|---|---|
| Full plan → execute → review pipeline | "run review-loop on this branch" |
| Plan a work item only | "plan this task with review-loop" |
| Resume an approved plan | "resume review-loop session `<uuid>`" |
| Review-only pass on the working tree | "review the pending changes" |
| Show review-loop's command surface | "show review-loop guide" |
| Paired-session work item (explicit opt-in during migration) | "use paired-session for this task" |

Stage 1 exposes five skills under `.agents/skills/`:
`review-loop` (legacy umbrella), `plan`, `execute`, `guide`, and
`paired-session`. Both `plan` and
`execute` share `.review-loop/config.md` and `.review-loop/sessions/`
with the Claude Code path, so a session started under one runtime can be
resumed under the other.

Paired-session reads the optional project profile
`.review-loop/paired-session.json` and stores coordinator state outside the
workspace under `$CODEX_HOME/state/paired-session/`.

## Verification

After marketplace registration + CLI install, sanity-check:

```bash
# 1. config.toml has both the marketplace and plugin entries
grep -A2 'review-loop' ~/.codex/config.toml

# 2. Cache is populated for the current version
ls ~/.codex/plugins/cache/review-loop-marketplace/review-loop/

# 3. A non-interactive Codex session sees the skills
codex exec --skip-git-repo-check \
  "List enabled plugins and skills. Be terse."
```

The third command should list `review-loop`, its plan/execute/guide skills,
and `paired-session` among the available skills.

## Boundary: Claude Code plugin path vs Codex plugin path

The repo carries two parallel plugin surfaces. They share docs and
session state but install through different package managers.

| Surface | Manifest | Marketplace manifest | Skill tree | Slash commands |
|---|---|---|---|---|
| Claude Code | `.claude-plugin/plugin.json` | `.claude-plugin/marketplace.json` | `skills/` (top-level) | `/review-loop`, `/review-loop:plan`, `/review-loop:paired-session`, … |
| Codex CLI | `.codex-plugin/plugin.json` | `.agents/plugins/marketplace.json` | `.agents/skills/` | none — natural-language only |

The top-level `skills/` tree (with `review-pr`, `code-quality-loop`,
`reorganize`, …) dispatches via Claude's Agent tool and is intentionally
**not** exposed to Codex. The five `.agents/skills/` entries are the
Stage 1 Codex surface. Paired-session is an explicit path in both runtimes;
the familiar legacy `/review-loop` routing remains unchanged in this batch.

There is also a fallback wrapper at `~/.codex/skills/review-loop/SKILL.md`
that some users symlink for the legacy "skills only, no marketplace"
flow. With the marketplace install in place, the wrapper is no longer
needed and can be removed.
