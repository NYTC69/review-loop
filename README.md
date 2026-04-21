# review-loop

A Claude Code plugin for AI-driven code review, with a Codex Stage 1 repo-skill path alongside the Claude/plugin implementation.

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

## Codex Stage 1

Codex uses repo skills under `.agents/skills/`. In Stage 1, the Codex
`review-loop` skill shares `.review-loop/config.md` and `.review-loop/sessions/`
with Claude Code, so both runtimes work against the same project state.
The rest of this README primarily documents the current Claude Code plugin
surface; Codex Stage 1 currently exposes only `review-loop` and `guide`.

The default reviewer path in Codex Stage 1 uses the Claude CLI reviewer
(`claude -p`) and stays on that outside-sandbox Claude path unless you
explicitly opt into the local Codex reviewer with
`codex_reviewer_backend: codex` in `.review-loop/config.md`.
In Codex Stage 1, `reviewer_model` overrides that Claude reviewer path,
`judgment_model` is its shared-tier fallback, and the empty backstop is an
explicit `--model claude-sonnet-4-6`.
The shared `cheap_model` key is accepted for cross-runtime config
compatibility, but Stage 1 currently has no cheap-tier Codex agents, so it is
a documented no-op there.
The shared `reviewer` and `executor_model` keys do not actively control
Stage 1 Codex reviewer/backend selection. In Codex Stage 1,
`executor_model` is ignored and `codex_executor_model` remains reserved.

Stage 1 does not yet migrate `code-quality-loop`, `review-pr`, or `reorganize`.

## Skill Tests

The repository includes a first-version skill testing framework for
`review-loop` and `guide`.

- `scripts/run-skill-lint` runs static contract checks
- `scripts/run-skill-smoke` runs the small real smoke suite
- `scripts/run-skill-tests` runs both in order

Test output uses `PASS`, `FAIL`, and `SKIP`.

- Aggregate results: `tests/skills/.last-run.json`
- Per-case artifacts: `tests/skills/.artifacts/`

## Claude Plugin Surface

The commands, configuration tables, reviewer modes, and included agent list
below describe the current Claude Code plugin surface. They are not yet part of
the Codex Stage 1 surface beyond the shared `review-loop` and `guide` entries
described above.

## Three Skills: `plan`, `execute`, `review-loop`

Starting in v2.6.0 the workflow is split into three composable skills. Pick
the one that matches where your work currently is:

- **`/review-loop`** — the umbrella. Full plan → execute → polish →
  delivery in one invocation. Step 1.5 auto-routes based on detected state
  (fresh / existing plan / code already implemented). Unchanged external UX
  from earlier versions.
- **`/review-loop:plan`** — planning phase only. Drives a work item to a
  reviewer-approved plan in `.review-loop/sessions/{uuid}.md`, then exits
  with a hand-off hint (`Next: review-loop:execute --session <uuid>`). Use
  this when you want plan-only iteration, or want to plan on one runtime
  and execute on another.
- **`/review-loop:execute`** — execution + quality polish + delivery.
  Three mutually-exclusive entry modes:
  - `--session <uuid>` — resume an approved session. Reviewer strictness
    follows the session's `plan_source` (strict for `reviewer-approved`,
    advisory-for-plan-conformance for `user-supplied`, pure CR for
    `review-only`).
  - `--plan <text|path> --title <title>` — execute a user-supplied plan
    verbatim. `plan_source: user-supplied`; plan-conformance deviations
    become advisory MINOR findings.
  - `--review-only [--description <what was done>]` — pure CR sweep over
    the current working tree. Skips the first Executor round; goes
    straight to the Reviewer.

All three skills share the same session-file schema and can hand off
between invocations (and between runtimes).

### Multi-batch example

Stop cleanly between stages with `--stop-after <stage>`, then resume:

```bash
# 1. Plan-only.
/review-loop:plan split auth middleware into request-scoped + global layers
# → prints session UUID, e.g. a3c4...

# 2. Execute but stop before Quality Polish.
/review-loop:execute --session a3c4... --stop-after before-polish

# 3. Review the diff, then resume — runs polish + docs + security + delivery.
/review-loop:execute --session a3c4...
```

### `--stop-after <stage>` enum (Claude Code)

Claude Code supports the full set of stages:

| Value | Stops |
|---|---|
| `exec-round` | After the current execution round finishes (even on REQUEST_CHANGES) |
| `before-polish` | Before Step 3.5 Quality Polish |
| `before-docs` | Before Step 3.6 Documentation Consistency |
| `before-security` | Before Step 3.7 Security Preflight |
| `before-delivery` | Before Step 4 Delivery |
| `delivery` | Default — no early stop |

Unsupported values are rejected at parse time, before any lock is acquired
or session field is written. (Codex Stage 1 supports only
`exec-round`, `before-delivery`, `delivery` — Steps 3.5 / 3.6 / 3.7 are out
of Stage 1 scope.)

### `--accept-external-state` (unsafe opt-in)

Auto-accepts every "external drift detected — (A) accept / (B) abort"
pause-and-confirm prompt the Orchestrator would otherwise surface
(drift-check decision tree; backward-compat missing-baseline fallback).

**Unsafe**. Use only when you *know* external tree changes between
batches were intentional and you want to reset baseline silently. The
`--handsfree` flag alone does NOT auto-accept drift — this flag must be
passed explicitly.

## Workflow Overview

```
/review-loop <task>
│
├── 1. Planning
│   Executor drafts plan → Adversarial Reviewer critiques → iterate until APPROVE
│
├── 2. Execution
│   Executor implements → Adversarial Reviewer code-reviews → iterate until APPROVE
│
├── 3. Quality Polish (automatic)
│   Language-specific static analysis → code quality review →
│   code simplification → test coverage check → docs consistency
│
└── 4. Delivery
    Findings table + quality summary + time breakdown
```

Both the Executor and Reviewer operate independently — the Reviewer is a
different AI (or an isolated sub-agent) that catches blind spots, design
deviations, and unauthorized compromises the Executor would silently ship.

## Example: Rust Repo

```
/review-loop add rate limiting to the upload endpoint using tower middleware
```

**Planning** — The Executor drafts a plan using `tower::limit::RateLimitLayer`.
The Reviewer flags a missing per-IP bucket strategy and rates it CRITICAL.
The Executor revises. The Reviewer approves on round 2.

**Execution** — The Executor implements the plan. The Reviewer catches that the
`RateLimitLayer` was applied globally instead of per-route and flags plan
conformance violation. Fixed and approved on round 2.

**Quality Polish** — `rust-reviewer` runs `cargo clippy`, `code-simplifier`
removes a redundant `.clone()`, `pr-test-analyzer` notes missing test for
the 429 response path.

**Delivery** — Full findings table, quality summary, and time breakdown are
shown. Optionally auto-commits the result.

## Standalone Tools

### `/review-loop:code-quality-loop`

Run quality polish independently on existing code. Same agents as Step 3.5
but triggered on demand — useful for cleaning up code that was written
outside the review-loop workflow.

### `/review-loop:reorganize <file/dir or 'diff'>`

Restructure code files: rearrange module layout, extract shared logic, remove
redundancy, add section comments. Splits coupled files into focused modules.
Preserves all functionality — this is restructuring, not rewriting.

```
/review-loop:reorganize src/engine.go    # single file
/review-loop:reorganize src/core/        # directory
/review-loop:reorganize diff             # all uncommitted changes
```

### `/review-loop:review-pr [aspects]`

Spot-check specific aspects of recent changes. Available aspects:

| Aspect | Agent | What it checks |
|--------|-------|---------------|
| `code` | code-reviewer | Style, patterns, best practices |
| `errors` | silent-failure-hunter | Swallowed errors, silent fallbacks |
| `comments` | comment-analyzer | Comment accuracy, staleness |
| `types` | type-design-analyzer | Type design, encapsulation |
| `tests` | pr-test-analyzer | Test coverage, edge cases |
| `simplify` | code-simplifier | Unnecessary complexity |

```
/review-loop:review-pr code errors tests
```

### `/review-loop:guide`

Show the usage guide — how it works, commands, configuration, and key features.

## Configuration

All options live in `.review-loop/config.md`. Every field is optional.

| Key | Default | Description |
|-----|---------|-------------|
| `reviewer` | `codex` | Shared Claude/plugin reviewer mode; Codex Stage 1 does not use this key to choose the reviewer backend |
| `reviewer_model` | `""` | Path-specific reviewer override; in Codex Stage 1 this applies only to the default Claude CLI reviewer path |
| `judgment_model` | `""` | Shared tier override for judgment-tier agents; Codex Stage 1 also uses it as the fallback model for the default Claude reviewer path |
| `cheap_model` | `""` | Shared tier override for cheap-tier agents; default backstop is `claude-haiku-4-5-20251001`; accepted-but-no-op in Codex Stage 1 |
| `executor_model` | `inherit` | Path-specific Claude executor override; `""` and `inherit` both fall through to `judgment_model`; ignored by Codex Stage 1 |
| `codex_reviewer_backend` | `claude_cli` | Codex Stage 1 only; keeps review on the outside-sandbox Claude reviewer unless set to `codex` explicitly |
| `codex_reviewer_model` | `""` | Codex Stage 1 only; local Codex reviewer override when `codex_reviewer_backend: codex` |
| `codex_executor_model` | `""` | Reserved and ignored in Codex Stage 1 |
| `soft_limit_plan` | `3` | After N rounds, ask user to continue if CRITICALs remain |
| `soft_limit_exec` | `3` | Same for execution phase |
| `auto_commit` | `false` | Stage changed files and commit after delivery |
| `commit_message_prefix` | `feat` | Conventional commit type prefix |
| `docs_file` | `CHANGELOG.md` | File to append delivery summary; `""` to skip |
| `handsfree` | `false` | Default to hands-free mode (decisions go to Reviewer) |
| `review_focus` | `""` | Project-specific review priorities (free text) |
| `quality_focus` | `""` | What to prioritize in quality polish (free text) |
| `review_style` | `""` | Tone and rules for all reviews (free text) |
| `skip_quality_polish` | `false` | Skip Quality Polish (Step 3.5) entirely |

For Codex Stage 1, the reviewer separation policy is explicit: unless
`codex_reviewer_backend: codex` is set, review stays on the
outside-sandbox Claude CLI reviewer path. That default path resolves its model
as `reviewer_model` > `judgment_model` > `claude-sonnet-4-6` and passes it via
`--model`. The local Codex reviewer is opt-in only. The `cheap_model` entry is
accepted in the shared config but remains a no-op in Stage 1 because only
judgment-tier Codex agents are currently shipped.

### Natural language config examples

```yaml
review_focus: |
  - Security: auth checks, input validation, SQL injection
  - Performance: N+1 queries, missing indexes

quality_focus: "strict clippy lints, skip comment analysis"

review_style: "be terse, flag any unwrap() as CRITICAL"
```

## Reviewer Modes

| Mode | Config | How it works |
|------|--------|-------------|
| **codex** (default) | `reviewer: codex` | Calls `codex exec -s read-only` — cross-AI review from a different model |
| **subagent** | `reviewer: subagent` | Claude Code sub-agent with read-only tools — no external CLI required |

The codex mode gives you genuinely independent review from a different AI.
The subagent mode uses a Claude Code sub-agent — convenient when you don't
have Codex installed. Set `reviewer_model` to control which model the
Reviewer uses.

## Included Agents

| Agent | Role |
|-------|------|
| `executor` | Implements plans and code changes as a sub-agent |
| `reviewer` | Independent adversarial reviewer (plan + code review) |
| `code-reviewer` | Style, patterns, and best-practice checks |
| `code-simplifier` | Removes unnecessary complexity while preserving behavior |
| `silent-failure-hunter` | Finds swallowed errors, silent fallbacks, inadequate error handling |
| `pr-test-analyzer` | Reviews test coverage quality and completeness |
| `comment-analyzer` | Checks comment accuracy, staleness, and maintainability |
| `type-design-analyzer` | Analyzes type design — encapsulation, invariants, usefulness |
| `go-reviewer` | Go static analysis (`go vet`, `staticcheck`, etc.) |
| `rust-reviewer` | Rust static analysis (`cargo clippy`, etc.) |
| `python-reviewer` | Python static analysis (`ruff`, `mypy`, etc.) |
| `frontend-security-reviewer` | Frontend security: XSS, CSRF, auth state, dependency risks |

## Key Design Features

**Live Reports** — After every review round, the Orchestrator shows you what
the Reviewer found: CRITICAL issues, MINOR suggestions, and the verdict.
You see the value of the review loop in real time.

**Plan Conformance** — The Reviewer checks that the Executor's implementation
stays within the approved plan. Unauthorized design decisions are flagged as
CRITICAL even if the code is technically correct.

**Context File** — All loop state is persisted to
`.review-loop/sessions/{uuid}.md`. Both agents read it each round
for instant context. Session files are preserved permanently — the UUID
is printed in the delivery summary. To trace a bug back to a specific
review session, find the UUID in the delivery output and open the
corresponding `.review-loop/sessions/{uuid}.md` file.

**Soft Limits + Stuck Detection** — No hard cap on rounds. When the soft limit
is reached and CRITICALs remain, the Orchestrator asks whether to continue.
Stuck detection stops the loop if the same issue recurs 3 rounds without
progress.

**Quality Polish** — After the adversarial review loop approves, a suite of
specialized agents automatically runs static analysis, simplification, test
coverage, and comment checks. Configurable via `quality_focus` and
`skip_quality_polish`.

## File Structure

The tree below shows the Claude/plugin-side structure. Codex Stage 1 also uses
the runtime paths `.agents/skills/` and `.codex/agents/` for its repo skills
and subagents. Only `review-loop` and `guide` are wired for Codex in Stage 1.

```
review-loop/
├── docs/
│   └── protocol/                 ← Shared protocol docs (single source of truth)
│       ├── session-file.md       ← Canonical session schema + moving baseline
│       ├── planning.md           ← Planning phase round loop
│       ├── execution.md          ← Execution / polish / docs / security / delivery
│       ├── executor-output.md    ← Executor output schema
│       └── reviewer-output.md    ← Reviewer output schema
├── skills/
│   ├── review-loop/
│   │   └── SKILL.md              ← Umbrella orchestrator (auto-routing)
│   ├── plan/
│   │   └── SKILL.md              ← Planning-only sub-skill
│   ├── execute/
│   │   └── SKILL.md              ← Execution + polish + delivery (3 entry modes)
│   ├── code-quality-loop/
│   │   └── SKILL.md              ← Standalone quality polish
│   ├── reorganize/
│   │   └── SKILL.md              ← Code file restructuring
│   ├── review-pr/
│   │   └── SKILL.md              ← Spot-check specific aspects
│   └── guide/
│       └── SKILL.md              ← Usage guide
├── agents/
│   ├── executor.md                     ← Executor sub-agent
│   ├── reviewer.md                     ← Adversarial Reviewer
│   ├── code-reviewer.md                ← Code style + patterns
│   ├── code-simplifier.md              ← Complexity reduction
│   ├── silent-failure-hunter.md        ← Error handling review
│   ├── pr-test-analyzer.md             ← Test coverage review
│   ├── comment-analyzer.md             ← Comment quality review
│   ├── type-design-analyzer.md         ← Type design review
│   ├── go-reviewer.md                  ← Go static analysis
│   ├── rust-reviewer.md                ← Rust static analysis
│   ├── python-reviewer.md              ← Python static analysis
│   └── frontend-security-reviewer.md  ← Frontend security
├── review-loop-config.example.md ← Copy to .review-loop/config.md and customize
├── .gitignore
├── LICENSE                       ← Apache 2.0
└── README.md
```

## License

Apache 2.0
