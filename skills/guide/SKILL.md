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

## How it works (legacy workflow; fresh work goes to paired-session by default, see Entry below)

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
| `/review-loop` | You want the full pipeline in one invocation (default; fresh work goes to paired-session, see Entry below) | Fresh work → paired-session coordinator; existing code → paired-session `run --review-only` (Entry below); an existing plan → paired-session with the plan as the work item (Entry below) |
| `/review-loop:plan` | You only want to iterate on the plan; run the code later (possibly on a different runtime) | Runs the planning loop only. On approval, prints the session UUID and a hint: `Next: review-loop:execute --session <uuid>` |
| `/review-loop:execute` | You already have a plan, or you just want a pure CR sweep over existing code | Runs execution + polish + delivery. Three entry modes: `--session <uuid>`, `--plan <text\|path>`, `--review-only` (legacy workflow only; on the paired-session entry `/review-loop` reviews existing code with `run --review-only`) |

All three skills write the same session-file schema under
`.review-loop/sessions/{uuid}.md`, so you can hand off between them (and
between runtimes — plan on one, execute on the other).
Codex Stage 1 follows the same broad `exec -> polish -> docs -> security -> delivery` lifecycle.
Codex Stage 1 assumes a single orchestrator-owned workspace for the session.

## Entry: paired-session default, legacy on request

From v2.10.0 a fresh `/review-loop <work item>` (Claude) or a fresh review-loop
request (Codex) with no `entry` key in `.review-loop/config.md` hands off to the
paired-session coordinator. The workflow drawn above is the legacy workflow, removed from
routing in v2.13.0: only `/review-loop:legacy`, `/review-loop:plan` and `/review-loop:execute` still
run it, until v2.13.1 deletes them. The entry commands:

| Command | What it does |
|---|---|
| `/review-loop:paired-session <work item> [--plan-only]` | Runs the paired-session coordinator: independent PLAN review (strict mode first needs a permission-probe PASS), EXEC implementation and review, adversarial gate, then finish, quality polish, docs and security. It ends at DONE (or HOLD); the agent accepts or rejects a DONE run only on your explicit decision. `--plan-only` stops after the approved plan |
| `/review-loop:legacy <work item> [--handsfree]` | Runs the legacy workflow and ignores the `entry` key (no entry notice; it prints the deprecation notice) |
| `/review-loop:code-quality-loop [max-rounds]` | A review-only paired-session run on the uncommitted change (one fix round for the non-blocking findings, quality writers on; `accept` commits locally unless `auto_commit: false`); `--legacy`, `--reorganize` and `entry: legacy` are refused (legacy removed in v2.13.0) |

The legacy workflow is deprecated since v2.12.0 and was removed from routing in v2.13.0:
`entry: legacy` is refused, and `/review-loop:legacy`, `/review-loop:plan` and `/review-loop:execute`
(which print a one-line deprecation notice) are deleted in v2.13.1. See
`docs/paired-session-migration.md`.

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
`/review-loop:plan` and `/review-loop:execute` ignore `entry` (legacy; deleted in v2.13.1). On Codex, ask
"use paired-session for this task".
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

Both runtimes load instructions by active action using
`docs/protocol/loading.md` and `scripts/read_protocol.py`. Shared rules are
read before the action they govern; independent agents and resumed/compacted
contexts reload their prerequisites. This changes instruction transport only,
not the required review, evidence, authorization or delivery gates.

```bash
# Basic — starts full plan→review→implement→CR loop (paired-session by default; legacy with `entry: legacy`)
/review-loop add rate limiting to the /api/upload endpoint

# Handsfree, legacy workflow — decision questions go to Reviewer, not you
# (under the paired-session default, handsfree only means stage A questions cannot be asked)
/review-loop:legacy refactor auth middleware --handsfree

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

Stop cleanly at a seam between stages. Claude Code and Codex Stage 1 support
the full set:

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
Codex Stage 1 supports `before-polish`, `before-docs`, and `before-security` as clean stop points.
Executor-created hidden worktrees are forbidden in Codex Stage 1.

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
| `entry` | absent (paired-session) | `paired-session` (`legacy` is refused since v2.13.0); routes every `/review-loop` (Claude) or review-loop skill (Codex) request, review-pr and code-quality-loop (see Entry above) |
| `reviewer` | codex | `"codex"` \| `"subagent"` |
| `reviewer_model` | "" | Path-specific reviewer override; in Codex Stage 1 this applies only to the default Claude CLI reviewer path |
| `judgment_model` | "" | Shared tier override for judgment-tier agents |
| `cheap_model` | "" | Shared tier override for cheap-tier agents; default backstop is `claude-opus-5-5`; accepted-but-no-op in Codex Stage 1 |
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
| `quality_focus` | "" | `quality_focus` applies only when Step 3.5 Quality Polish actually runs. |
| `review_style` | "" | Tone/rules for ALL reviews — adversarial + quality agents |
| `skip_quality_polish` | false | `skip_quality_polish: true` mints `polish` as a no-op completion and still continues through docs and security. |

Under the paired-session default entry only `docs_file`, `skip_quality_polish`
and the soft limits are mapped (the limits become hard caps that HOLD);
`auto_commit` and the model keys only warn, `handsfree` only blocks stage A
questions and acceptance, and the other keys apply to the legacy workflow;
unset paired-session caps are plan 3 / exec 4 rounds. See
`docs/paired-session-migration.md` (Config mapping).

Codex Stage 1 keeps review on the outside-sandbox Claude reviewer path by
default. The local Codex reviewer is explicit opt-in only via
`codex_reviewer_backend: codex`. `cheap_model` remains accepted-but-no-op in
Codex Stage 1 because only judgment-tier Codex agents are shipped today. When
neither `reviewer_model` nor `judgment_model` is set, that default Claude
reviewer path backstops to `claude-opus-5-5`.
`quality_focus` applies only when Step 3.5 Quality Polish actually runs.
`skip_quality_polish: true` mints `polish` as a no-op completion and still continues through docs and security.

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
- **Soft limits + stuck detection** — no hard cap, smart stopping (legacy workflow; paired-session caps are hard)
- **Subagent mode** — no Codex needed; `reviewer: subagent` runs an isolated read-only Claude CLI reviewer
- **Project-specific config** — tailor review priorities per project

## More info

GitHub: https://github.com/NYTC69/review-loop
