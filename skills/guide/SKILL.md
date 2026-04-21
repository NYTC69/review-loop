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

Orchestrator (this session)
│
├── Context file: .review-loop/sessions/{uuid}.md
│   Single source of truth — all agents read it each round
│
├── [Planning phase]
│   → Executor drafts plan → Reviewer critiques → iterate
│
├── [Execution phase]
│   → Executor implements → Reviewer does CR → iterate
│
├── [Quality Polish]  (skip with skip_quality_polish: true)
│   → Language analysis → Code quality → Simplify → Tests → Docs Consistency
│
└── Delivery: findings table + quality summary + time breakdown
```

## Three skills

The review-loop workflow is now split into three composable skills. Pick the
one that matches where your work currently is:

| Skill | When to pick | What it does |
|---|---|---|
| `/review-loop` | You want the full pipeline in one invocation (default, unchanged UX) | Auto-routes based on state — fresh plan, existing plan, or code-already-done — then runs plan → execute → polish → delivery end-to-end |
| `/review-loop:plan` | You only want to iterate on the plan; run the code later (possibly on a different runtime) | Runs the planning loop only. On approval, prints the session UUID and a hint: `Next: review-loop:execute --session <uuid>` |
| `/review-loop:execute` | You already have a plan, or you just want a pure CR sweep over existing code | Runs execution + polish + delivery. Three entry modes: `--session <uuid>`, `--plan <text\|path>`, `--review-only` |

All three skills write the same session-file schema under
`.review-loop/sessions/{uuid}.md`, so you can hand off between them (and
between runtimes — plan on one, execute on the other).

## Usage

```bash
# Basic — starts full plan→review→implement→CR loop
/review-loop add rate limiting to the /api/upload endpoint

# Handsfree — decision questions go to Reviewer, not you
/review-loop refactor auth middleware --handsfree

# If code is already written, it auto-detects and skips to CR
/review-loop review the changes I just made to the parser

# Plan-only, then execute separately (good for big multi-batch work)
/review-loop:plan design an adaptive rate limiter for /api/upload
# → prints "Next: review-loop:execute --session <uuid>"

# Fresh plan → execute multi-batch → delivery
/review-loop:execute --session <uuid> --stop-after before-delivery
# review diff, then:
/review-loop:execute --session <uuid>

# Review-only pipeline (pure CR over already-written code)
/review-loop:execute --review-only --description "parser refactor in src/parse/*"

# Show this guide — slash command or natural language
/review-loop:guide
show me the review-loop guide
```

### Example session — fresh plan → multi-batch execution → delivery

```bash
# 1. Plan-only. Reviewer iterates until APPROVE, then exits.
/review-loop:plan split auth middleware into request-scoped + global layers

# → prints session UUID, e.g. a3c4...

# 2. Execute the plan but stop before Quality Polish so you can
#    review the raw CR diff first.
/review-loop:execute --session a3c4... --stop-after before-polish

# 3. Satisfied — resume the same session for polish + docs + security + delivery.
/review-loop:execute --session a3c4...
```

### Example session — review-only pipeline

```bash
# Workspace is dirty from a previous coding session. You want pure CR
# on the existing diff — no plan, no re-implementation.
/review-loop:execute --review-only --description "hot-reload watcher in src/watcher/*"

# First round is Reviewer-only (no Executor runs). If REQUEST_CHANGES,
# subsequent rounds are the standard Executor → Reviewer CR → fix loop.
```

## `--stop-after <stage>` (execute only)

`--stop-after` is accepted **only by `/review-loop:execute`**. The umbrella
`/review-loop` skill does NOT accept `--stop-after` — its argument surface
remains `<work item description> [--handsfree]` for backward compatibility.
If you need mid-flow stops, invoke `/review-loop:execute` directly.

Stop cleanly at a seam between stages. Claude Code supports the full set:

| Value | Stops |
|---|---|
| `exec-round` | After the current execution round finishes (even on REQUEST_CHANGES) |
| `before-polish` | Before Step 3.5 Quality Polish |
| `before-docs` | Before Step 3.6 Documentation Consistency |
| `before-security` | Before Step 3.7 Security Preflight |
| `before-delivery` | Before Step 4 Delivery |
| `delivery` | Default — no early stop |

Unsupported values are rejected at parse time, before any lock is
acquired or session field is written.

## `--accept-external-state` (unsafe opt-in)

This flag auto-accepts every "external drift detected — (A) accept / (B)
abort" pause-and-confirm prompt the Orchestrator would otherwise surface:

- The drift-check decision tree (external commits / edits between batches).
- The backward-compat fallback for old sessions missing baseline metadata.

**Unsafe**: you are opting out of pausing on external tree drift. Use only
when you *know* the external changes are intentional and you want to
reset baseline silently. Handsfree mode alone does NOT auto-accept — this
flag must be passed explicitly.

> **After updating the plugin**: exit with Ctrl-C twice, then `claude --resume`
> to reload plugins while keeping your conversation. Old sessions keep using
> the version loaded at startup.

## Configuration

Create `.review-loop/config.md` in your project to customize:

| Key | Default | Description |
|-----|---------|-------------|
| `reviewer` | codex | `"codex"` \| `"subagent"` |
| `reviewer_model` | "" | Path-specific reviewer override; in Codex Stage 1 this applies only to the default Claude CLI reviewer path |
| `judgment_model` | "" | Shared tier override for judgment-tier agents |
| `cheap_model` | "" | Shared tier override for cheap-tier agents; default backstop is `claude-haiku-4-5-20251001`; accepted-but-no-op in Codex Stage 1 |
| `executor_model` | inherit | Path-specific Claude executor override; `""` and `inherit` both fall through to `judgment_model` |
| `codex_reviewer_backend` | claude_cli | Codex Stage 1 only; keeps review on the outside-sandbox Claude reviewer unless set to `codex` explicitly |
| `codex_reviewer_model` | "" | Codex Stage 1 only; local Codex reviewer override when `codex_reviewer_backend: codex` |
| `codex_executor_model` | "" | Reserved and ignored in Codex Stage 1 |
| `soft_limit_plan` | 3 | Rounds before asking to continue |
| `soft_limit_exec` | 3 | Same for execution phase |
| `auto_commit` | false | Commit after delivery |
| `commit_message_prefix` | `feat` | Conventional commit type prefix |
| `docs_file` | `CHANGELOG.md` | File to append delivery summary; `""` to skip |
| `handsfree` | false | Default to handsfree mode |
| `review_focus` | "" | Project-specific review priorities (free text) |
| `quality_focus` | "" | What to prioritize in Quality Polish (Step 3.5) |
| `review_style` | "" | Tone/rules for ALL reviews — adversarial + quality agents |
| `skip_quality_polish` | false | Skip Step 3.5 entirely |

Codex Stage 1 keeps review on the outside-sandbox Claude reviewer path by
default. The local Codex reviewer is explicit opt-in only via
`codex_reviewer_backend: codex`. `cheap_model` remains accepted-but-no-op in
Codex Stage 1 because only judgment-tier Codex agents are shipped today. When
neither `reviewer_model` nor `judgment_model` is set, that default Claude
reviewer path backstops to `claude-sonnet-4-6`.

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

- **Live Reports** — see what the Reviewer found after every round
- **Plan Conformance** — flags unauthorized Executor deviations as CRITICAL
- **Context file** — persistent session for traceability and fast agent startup
- **Soft limits + stuck detection** — no hard cap, smart stopping
- **Subagent mode** — no Codex needed; uses a Claude Code sub-agent as Reviewer
- **Project-specific config** — tailor review priorities per project

## More info

GitHub: https://github.com/NYTC69/review-loop
