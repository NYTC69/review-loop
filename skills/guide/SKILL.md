---
name: guide
description: >
  Show review-loop usage guide: how it works, commands, configuration,
  and key features. Use when the user asks for help with review-loop.
---

First, read `~/.claude/plugins/marketplaces/review-loop-marketplace/.claude-plugin/plugin.json` to get the current version number.

Then display the following guide to the user, replacing `{VERSION}` with the version from plugin.json:

---

# review-loop {VERSION} — Quick Reference

## How it works

```
/review-loop <work item description> [--handsfree]

review-loop entry (resolves `entry`, runs the stage A checks)
│
└── paired-session coordinator (run directory outside the product worktree)
    ├── PLAN   author drafts the plan → independent reviewer → iterate
    ├── EXEC   author implements → reviewer + shadow review → iterate
    ├── GATE   adversarial gate
    ├── FINISH → POLISH-Q specialists → docs → security
    └── DONE (or HOLD) → you accept or reject; nothing ships without your decision
```

## Commands

| Command | What it does |
|---|---|
| `/review-loop <work item>` | The default entry: fresh work → paired-session; existing code → paired-session `run --review-only`; an existing plan → paired-session with the plan as the work item (Entry below) |
| `/review-loop:paired-session <work item> [--plan-only]` | Runs the paired-session coordinator directly: independent PLAN review (strict mode first needs a permission-probe PASS), EXEC implementation and review, adversarial gate, then finish, quality polish, docs and security. It ends at DONE (or HOLD); the agent accepts or rejects a DONE run only on your explicit decision. `--plan-only` stops after the approved plan |
| `/review-loop:review-pr [PR number\|PR URL\|ref] [aspects]` | Paired-session report mode (below) |
| `/review-loop:code-quality-loop [max-rounds]` | A review-only paired-session run on the uncommitted change (one fix round for the non-blocking findings, quality writers on; `accept` commits locally unless `auto_commit: false`); `--legacy`, `--reorganize` and `entry: legacy` are refused (legacy removed in v2.13.0) |
| `/review-loop:reorganize` | Reorganizes code file structure without changing behaviour (standalone) |
| `/review-loop:guide` | This guide |

The legacy workflow was removed in v2.13.0 (routing) and v2.13.1 (`/review-loop:legacy`,
`/review-loop:plan`, `/review-loop:execute` and its protocol files). Their replacements:
plan-only work → `/review-loop:paired-session <work item> --plan-only`; an existing plan or a review of
existing code → `/review-loop`. Legacy session files under `.review-loop/sessions/` are left on disk and
never read; resuming one is refused. See `docs/paired-session-migration.md`.

## Entry

The `entry` key in `.review-loop/config.md` takes `legacy` or `paired-session`, written unquoted (exact values only; since v2.13.0 `legacy` is refused, and anything else is warned about and treated as absent).
With the key absent or `paired-session`, fresh work, an existing plan (used as the
work item; PLAN drafts and reviews it again) and a review-only request on
existing code (`run --review-only`, plus `--base <ref>` when you name a base) are
handed to paired-session; resuming a legacy session is refused. Unrelated dirty
work is not existing code: a review-only run reviews and, with `auto_commit`,
delivers every change against its base. A failed check before the
coordinator starts (a host other than macOS, required CLIs, background or outside-sandbox
execution, Codex home, on Codex an installed plugin older than v2.10.0, or a
declined or unanswered worktree or test-command question; under handsfree any
such question counts as unanswered) refuses with the reason, whether the key is set or not
(unavailable background or outside-sandbox execution is reported as HOLD); nothing falls back.
Once the coordinator has started, a refusal or HOLD is reported. Roles come from the
operator profile you name or `~/.config/review-loop/paired-session.json`;
without one, Codex is the author and gate and Claude the reviewer.
Runs are `efficient` by default: every sandbox applies, but no permission-probe
PASS is required; `--strict` or `"safety_mode": "strict"` in the operator
profile adds the probe gate. In both modes a reviewer turn that changes the
workspace is voided and restored, and an author commit is a HOLD.
On Codex, ask "use paired-session for this task".
Details: `docs/paired-session-migration.md`.

`/review-loop:review-pr [PR number|PR URL|ref] [aspects]` runs the paired-session report mode
(`entry: legacy` and `--legacy` are refused since v2.13.0).
A PR or ref is reviewed in a temporary clone (the operator's checkout is never
touched); no tests run unless you confirm a test command; the EXEC reviewer,
shadow, gate, the selected specialists (`code errors comments types tests`) and
the security stage report into `review-report.md` in the run directory, and
nothing is fixed, committed or posted. Posting is a separate request with a
secret scan and a second confirmation of the full body (`gh pr review
--comment` only). `simplify` is a writer and is refused on this route
(`run /review-loop:code-quality-loop on the change (its POLISH-Q simplifier)`).

## Usage

```bash
# Basic — starts the paired-session plan→review→implement→CR run
/review-loop add rate limiting to the /api/upload endpoint

# If code is already written, it hands off as a review-only run
/review-loop review the changes I just made to the parser

# I already have a plan: /review-loop (the plan becomes the work item)
/review-loop implement this plan: <plan text>

# Plan only
/review-loop:paired-session design an adaptive rate limiter for /api/upload --plan-only

# Show this guide — slash command or natural language
/review-loop:guide
show me the review-loop guide
```

> **After updating the plugin**: exit with Ctrl-C twice, then `claude --resume`
> to reload plugins while keeping your conversation. Old sessions keep using
> the version loaded at startup.

## Configuration

Create `.review-loop/config.md` in your project to customize:

| Key | Default | Description |
|-----|---------|-------------|
| `entry` | absent (paired-session) | `paired-session` (`legacy` is refused since v2.13.0); routes every `/review-loop` (Claude) or review-loop skill (Codex) request, review-pr and code-quality-loop (see Entry above) |
| `soft_limit_plan` | 3 | Becomes `--max-plan-rounds` (a hard cap that HOLDs) |
| `soft_limit_exec` | 3 | Becomes `--max-exec-rounds` (a hard cap that HOLDs) |
| `docs_file` | `CHANGELOG.md` | Becomes `--docs-file`; `""` to skip |
| `skip_quality_polish` | false | Becomes `--skip-quality-polish` |
| `review_focus` | "" | Project-specific review priorities (free text), given to the reviewer, shadow and gate |
| `review_style` | "" | Tone and rules for the review roles and the POLISH-Q specialists |
| `quality_focus` | "" | Quality priorities for the POLISH-Q specialists |
| `auto_commit` | false | Not applied on the main pipeline (a warning; set it in the operator profile); `false` keeps a code-quality-loop run uncommitted |
| `handsfree` | false | Blocks stage A questions (any such question fails) and acceptance |

Unset paired-session caps are plan 3 / exec 4 rounds. `reviewer_model` and `executor_model` only warn
(models come from the operator profile); the other legacy keys are ignored. See
`docs/paired-session-migration.md` (Config mapping).

### review_focus examples

```yaml
# Backend
review_focus: |
  - Concurrency: race conditions, deadlocks, mutex usage
  - Test coverage: error paths, not just happy paths

# Frontend
review_focus: |
  - Security: XSS, CSRF, input sanitization
  - Accessibility: WCAG, keyboard nav, screen reader

# Web API
review_focus: |
  - Security: auth checks, rate limiting, input validation
  - API contract: backward compatibility, proper status codes
```

## Key features

- **Independent review** — the reviewer, shadow and gate never share the author's session
- **Hard caps** — plan and exec round limits end in a HOLD, never a silent stop
- **Acceptance stays yours** — a DONE run is accepted or rejected only on your explicit decision
- **Project-specific config** — tailor review priorities per project

## More info

GitHub: https://github.com/NYTC69/review-loop
