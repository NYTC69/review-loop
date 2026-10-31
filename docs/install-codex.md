# Install review-loop on Codex CLI

For CODEX_HOME provisioning, run roots and fallback rules, see the [project overview](../README.md).

review-loop is dual-runtime: the same repo ships a Claude Code plugin
(`.claude-plugin/`) and a Codex CLI plugin (`.codex-plugin/` + `.agents/`).
This document covers the Codex install path; for Claude Code see the
top-level `README.md` Quick Start.

## Prerequisites

- **macOS or native Windows** — paired-session runs on macOS and on native Windows; every entry refuses Linux
  (WSL included) and other hosts (`paired-session needs macOS or native Windows; Linux (WSL included) and other hosts are not supported`).
  On Windows only the default efficient mode runs (`--strict`, `permission-probe` and `--detach` are refused), Git
  for Windows (Git Bash) is required, Python is `python` or `py -3`, and the Codex skills run their commands in
  PowerShell as best effort: the Claude Code entry is the tested Windows path. Keep `CODEX_HOME`, run directories
  and workspaces out of OneDrive-synced folders.
- **Codex CLI 0.159.2 or later** — the paired-session Codex default uses
  `gpt-6.1-sol`, which needs 0.159.2+. The plugin install command below was
  verified on 0.155.1; older releases may require the `/plugins` UI path, so
  check `codex plugin --help` before using it.
- **Python ≥ 3.9** — the paired-session coordinator and review-loop's helper
  scripts depend on it; CI runs them on 3.9.
- **git** — used by the coordinator and its roles to scope diffs.
- **Claude CLI** — both `codex` and `claude` must be on PATH, whatever vendors the paired-session
  roles use: the coordinator binds both binaries at run start. Role selection in the paired-session
  operator profile does not remove either CLI requirement.
- **Antigravity CLI (`agy`)** — optional; needed only when the operator profile or the CLI flags
  (`--reviewer-vendor gemini`, `--gate-vendor gemini`) name gemini for the reviewer or the gate. agy has no `login`
  subcommand: sign in on the first interactive `agy` run (see README.md, Configuration).
- **GitHub CLI (`gh`)** — needed only to review a pull request (a PR number or URL).

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

Installing from a local directory marketplace copies the whole directory,
including Git-ignored files, into the host plugin cache. This may copy `.compass/`,
`.claude/`, `HANDOFF.md`, and local caches. Use a clean checkout or the GitHub
marketplace to keep local working-tree state out of the plugin cache.

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
literal slash commands like `/review-loop:paired-session` are Claude-Code-only and
surface as `Unrecognized command` in Codex. Use natural language:

| What you want | Say |
|---|---|
| Full plan → execute → review pipeline | "run review-loop on this branch" |
| Plan a work item only | "use paired-session to plan this task (plan only)" |
| Review-only pass on the working tree | "review the pending changes" |
| Show review-loop's command surface | "show review-loop guide" |
| Paired-session work item (the default review-loop entry from v2.10.0; explicit request) | "use paired-session for this task" |

The legacy workflow was removed in v2.13.0/v2.13.1: a request for "the legacy review-loop workflow" is refused,
and the Codex plan and execute skills were deleted.

Codex exposes four skills under `.agents/skills/`:
`review-loop` (the entry; hands every request to paired-session), `guide`,
`paired-session`, and `review-pr`. `.review-loop/config.md` is shared with the Claude Code path.

Paired-session reads non-program defaults from the optional workspace
`.review-loop/paired-session.json`. Keep role, vendor, program and test-command
settings in an operator-owned profile outside the workspace and run directory,
and pass it with `--config` to both probe and run. The skill uses
`~/.config/review-loop/paired-session.json` when it exists and no other profile
is named. This replaces the workspace profile, so copy any desired non-program
limits into it. Coordinator state defaults to `<repo>/.review-loop/runs/<UUID>/` (gitignored).

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

The third command should list the four review-loop skills `review-loop`, `guide`,
`paired-session` and `review-pr` among the available skills.

## Boundary: Claude Code plugin path vs Codex plugin path

The repo carries two parallel plugin surfaces. They share docs and
session state but install through different package managers.

| Surface | Manifest | Marketplace manifest | Skill tree | Slash commands |
|---|---|---|---|---|
| Claude Code | `.claude-plugin/plugin.json` | `.claude-plugin/marketplace.json` | `skills/` (top-level) | `/review-loop`, `/review-loop:paired-session`, `/review-loop:review-pr`, … |
| Codex CLI | `.codex-plugin/plugin.json` | `.agents/plugins/marketplace.json` | `.agents/skills/` | none — natural-language only |

`code-quality-loop` and `reorganize` are Claude-only skills (no Codex port); on Codex, "review the pending
changes" runs the review-only route (quality writers on by default). The four `.agents/skills/` entries are the
Codex surface. Paired-session is the only review-loop entry in both runtimes; `entry: legacy`
and an explicit legacy request are refused (the legacy workflow was removed in v2.13.0).

If an old wrapper exists at `~/.codex/skills/review-loop/SKILL.md` (the legacy "skills only, no marketplace"
flow), remove it: it duplicates the plugin's `review-loop` skill and may point at the removed legacy workflow.
