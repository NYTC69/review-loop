# Install review-loop on Codex CLI

review-loop is dual-runtime: the same repo ships a Claude Code plugin
(`.claude-plugin/`) and a Codex CLI plugin (`.codex-plugin/` + `.agents/`).
This document covers the Codex install path; for Claude Code see the
top-level `README.md` Quick Start.

## Prerequisites

- **Codex CLI ≥ 0.130** — earlier releases have not been verified against
  the marketplace flow that review-loop ships.
- **Python ≥ 3.11** — `scripts/run_skill_smoke_lib.py` and other helpers
  used by review-loop's smoke / lint suites depend on it.
- **git** — used by the executor / reviewer agents and by the smoke
  harness to scope diffs.
- **Claude CLI** (optional but recommended) — review-loop's Codex Stage 1
  default reviewer path shells out to `claude -p` outside the Codex
  sandbox. Without it, opt into the local Codex reviewer with
  `codex_reviewer_backend: codex` in `.review-loop/config.md`.

## Install path: marketplace + `/plugins` TUI enable

review-loop publishes a Codex marketplace manifest at
`.agents/plugins/marketplace.json` and a `plugins/review-loop` symlink
that points back at the repo root. The two together make the repo a
first-class Codex plugin source.

```bash
# Register the marketplace (remote git URL or local path both work)
codex plugin marketplace add NYTC69/review-loop
# or, when developing review-loop locally:
codex plugin marketplace add /path/to/review-loop
```

Then, inside a fresh Codex session:

```
/plugins
```

> Note: it is **plural `plugins`**, not `/plugin install …`. Codex CLI
> 0.130 has only `codex plugin marketplace {add, upgrade, remove}` — no
> CLI-side install or enable subcommand. The `/plugins` TUI is the only
> path that writes the enable entry to `~/.codex/config.toml`:
>
> ```toml
> [plugins."review-loop@review-loop-marketplace"]
> enabled = true
> ```
>
> The TUI write requires Codex's approval policy to allow user-level
> config writes (start Codex with `--ask-for-approval on-request` or
> looser; `read-only` / `workspace-write` sandboxes will block the
> write).

Pick `review-loop` from the panel and enable it. Codex caches the
plugin contents at
`~/.codex/plugins/cache/review-loop-marketplace/review-loop/<version>/`.

Uninstall is the reverse: disable in `/plugins`, then on the host shell:

```bash
codex plugin marketplace remove review-loop-marketplace
```

## Triggering review-loop in a Codex session

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

Stage 1 exposes four skills under `.agents/skills/`:
`review-loop` (umbrella), `plan`, `execute`, `guide`. Both `plan` and
`execute` share `.review-loop/config.md` and `.review-loop/sessions/`
with the Claude Code path, so a session started under one runtime can be
resumed under the other.

## Verification

After `marketplace add` + `/plugins` enable, sanity-check:

```bash
# 1. config.toml has both the marketplace and plugin entries
grep -A2 'review-loop' ~/.codex/config.toml

# 2. Cache is populated for the current version
ls ~/.codex/plugins/cache/review-loop-marketplace/review-loop/

# 3. A non-interactive Codex session sees the skills
codex exec --skip-git-repo-check \
  "List enabled plugins and the skills you have. Be terse."
```

The third command should list `review-loop`, `review-loop:plan`,
`review-loop:execute`, `review-loop:guide`, `review-loop:review-loop`
among the available skills.

## Boundary: Claude Code plugin path vs Codex plugin path

The repo carries two parallel plugin surfaces. They share docs and
session state but install through different package managers.

| Surface | Manifest | Marketplace manifest | Skill tree | Slash commands |
|---|---|---|---|---|
| Claude Code | `.claude-plugin/plugin.json` | `.claude-plugin/marketplace.json` | `skills/` (top-level) | `/review-loop`, `/review-loop:plan`, … |
| Codex CLI | `.codex-plugin/plugin.json` | `.agents/plugins/marketplace.json` | `.agents/skills/` | none — natural-language only |

The top-level `skills/` tree (with `review-pr`, `code-quality-loop`,
`reorganize`, …) dispatches via Claude's Agent tool and is intentionally
**not** exposed to Codex. The four `.agents/skills/` entries are the
Stage 1 Codex surface.

There is also a fallback wrapper at `~/.codex/skills/review-loop/SKILL.md`
that some users symlink for the legacy "skills only, no marketplace"
flow. With the marketplace install in place, the wrapper is no longer
needed and can be removed.
