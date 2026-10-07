# review-loop

Workflow instructions load by active action on both Claude Code and Codex.
The [loading contract](docs/protocol/loading.md) and its declarative map select
exact shared-protocol sections before each action. Within a live context,
unchanged instructions need not be delivered repeatedly; new agents and
resumed/compacted contexts reload prerequisites. Review and safety gates are
unchanged. Runtime entry procedures live under each skill's `references/`.

A Claude Code plugin for AI-driven code review, with a Codex Stage 1 repo-skill path alongside the Claude/plugin implementation.

## Quick Start

```
/plugin marketplace add NYTC69/review-loop
/plugin install review-loop@review-loop-marketplace
```

Start a new session. The `/review-loop` command is now available in all your projects.

**Requirements (default entry).** macOS only (Linux is not supported for now; ADR-15 amendment); `claude` and
`codex` on PATH (codex-cli 0.159.2 or later); a dedicated git worktree for the task and a test command; an
interactive session (a headless session needs `--detach`); one absolute `CODEX_HOME` per run.

From v2.10.0 a fresh `/review-loop <work item>` without an `entry` key in
`.review-loop/config.md` hands off to the paired-session coordinator (the default
entry; since v2.13.0 `entry: legacy` is refused and nothing falls back to legacy), and
`/review-loop:paired-session <work item>` is the explicit entry. See
[`docs/paired-session-migration.md`](docs/paired-session-migration.md).
The legacy workflow is deprecated since v2.12.0: it still runs unchanged and prints a
one-line notice when you choose it; review-pr and code-quality-loop follow `entry` too, and
removal waits for the open legacy-map rows (the removal preconditions are in the migration guide).
Workspace `.review-loop/paired-session.json` may contain non-program limits only.
Keep role, vendor, program and test-command settings in an operator-owned profile
outside the product workspace and run directory, then pass its absolute path with
`--config` to every command (probe and run in strict mode); the paired-session skill uses
`~/.config/review-loop/paired-session.json` when it exists and you name no other
profile, and that profile replaces the workspace file. The plugin
ships `paired_session/paired-session-config.example.json` for that profile.

**Safety modes (D-EFF).** Every role runs with the same OS sandboxes in both
modes. `efficient`, the default, does not require a permission-probe PASS before
dispatch, and its evidence guard only records what it would have held. `strict`
(`--strict`, or `"safety_mode": "strict"` in the operator profile; the
workspace file cannot set it) also requires the probe PASS and lets the evidence
guard hold. The first command that creates the run (`run` by default,
`permission-probe` in strict mode) fixes its mode, so pass `--strict` from that
first command on; a `--strict` that
arrives after only the probe has run still upgrades the run, later it is
refused. Runs made before this change resume strict. In both modes a reviewer
turn that changes the workspace is voided, restored and re-dispatched once (a
second change or a failed restore is a HOLD), and an author turn that changes
HEAD or the branch is a HOLD. See
[`paired_session/docs/efficient-mode.md`](paired_session/docs/efficient-mode.md).

**Optional** — copy the config template to customize per-project defaults:

```bash
mkdir -p .review-loop
cp ~/.claude/plugins/cache/review-loop-marketplace/review-loop/<version>/review-loop-config.example.md .review-loop/config.md
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
surface; Codex Stage 1 also exposes the paired-session skill, which is the
default review-loop entry from v2.10.0.
The rest of this subsection, up to "Install in Codex CLI", describes the legacy workflow (deprecated since v2.12.0).
Codex Stage 1 follows the same broad `exec -> polish -> docs -> security -> delivery` lifecycle.
Codex Stage 1 assumes a single orchestrator-owned workspace for the session.
Codex Stage 1 supports `before-polish`, `before-docs`, and `before-security` as clean stop points.
Executor-created hidden worktrees are forbidden in Codex Stage 1.

The default reviewer path in Codex Stage 1 uses the Claude CLI reviewer
(`claude -p`) and stays on that outside-sandbox Claude path unless you
explicitly opt into the local Codex reviewer with
`codex_reviewer_backend: codex` in `.review-loop/config.md`.
When a Claude reviewer is selected, the permission probe checks unique writes
to host `/tmp`, the run directory, and the `context` directory passed through
`--add-dir`. The Claude reviewer sandbox requires a supported Claude host/backend;
the probe (required only in strict mode) fails closed when the OS sandbox is unavailable.
In Codex Stage 1, `reviewer_model` overrides that Claude reviewer path,
`judgment_model` is its shared-tier fallback, and the empty backstop is an
explicit `--model claude-opus-5-5`.
The shared `cheap_model` key is accepted for cross-runtime config
compatibility, but Stage 1 currently has no cheap-tier Codex agents, so it is
a documented no-op there.
The shared `reviewer` and `executor_model` keys do not actively control
Stage 1 Codex reviewer/backend selection. In Codex Stage 1,
`executor_model` is ignored and `codex_executor_model` remains reserved.

Codex has no code-quality-loop or reorganize skill; ask review-loop to review an existing change (a review-only
run, quality writers on).

### Install in Codex CLI

review-loop ships a Codex marketplace manifest at
`.agents/plugins/marketplace.json` plus a `plugins/review-loop` symlink so
the repo installs as a first-class Codex plugin (visible in `/plugins`,
parallel to the Claude Code plugin install at the top of this README).

```bash
codex plugin marketplace add NYTC69/review-loop
codex plugin add review-loop@review-loop-marketplace
```

Then, inside a fresh Codex session:

```
/plugins
```

The CLI install command above is sufficient; `/plugins` is an optional UI for
inspecting the installed plugin and its skills.

The plugin is cached under `$CODEX_HOME/plugins/cache/` (default
`~/.codex/plugins/cache/`). The paired-session skill reads the installed plugin
version from `codex plugin list --json` rather than assuming a fixed versioned
path.

Once enabled, the six Stage 1 skills under `.agents/skills/` (`review-loop`,
`plan`, `execute`, `guide`, `paired-session`, `review-pr`) are exposed to the Codex agent and respond to
natural-language triggers like "run review-loop on this branch" or
"plan this task with review-loop". Codex matches plugin skills by their
`SKILL.md` `description`, not by literal slash commands —
`/review-loop:plan` etc. are Claude-only and surface as `Unrecognized` in
Codex.

A fresh review-loop request hands off to the coordinator by default from
v2.10.0; ask Codex to "use paired-session for this task" to name it explicitly
(the request for "the legacy review-loop workflow" still runs legacy until v2.13.1 removes it).
It reads non-program workspace defaults from `.review-loop/paired-session.json`
when no `--config` is given; the skill passes `~/.config/review-loop/paired-session.json`
as `--config` when that file exists and you name no other profile. Program and role settings require an external operator
profile passed with `--config`, which replaces rather than layers onto the workspace profile.
Run artifacts stay outside the product workspace under
the user-level Codex state folder.

Full step-by-step + verification: [`docs/install-codex.md`](docs/install-codex.md).

## Reviewer isolation and invocation evidence

The following describes the legacy workflow (deprecated since v2.12.0).

Report-only reviewers use bounded native CLI launchers. Claude reviewers expose
only Read/Grep/Glob; Codex reviewers use a clean configuration context and a
read-only sandbox. The `reviewer: subagent` config name remains accepted but
selects the isolated Claude launcher. Verification commands run in the caller
and their evidence is supplied to the reviewer. See the
[reviewer runtime contract](docs/protocol/reviewer-runtime.md).

Each native reviewer call retains immutable diagnostics and normalized usage,
including partial/unknown usage on failure. The
[usage contract](docs/protocol/usage-accounting.md) explains cache and resume
accounting. The [delivery manifest](docs/protocol/delivery-scope.md) identifies
HEAD/index/worktree and pre-existing user changes without staging or committing.
The W02/W04/W05 consumers are documented in
[delivery-controls.md](docs/protocol/delivery-controls.md): manifest-bound
security scanning, ownership-limited auto-commit, and a machine-checked final
delivery gate.

## Skill Tests

The repository includes a first-version skill testing framework for
`review-loop` and `guide`.

- `scripts/run-skill-lint` runs static contract checks
- `scripts/run-skill-smoke` runs the small real smoke suite
- `scripts/run-skill-tests` runs both in order

Test output uses `PASS`, `FAIL`, and `SKIP`.

- Aggregate results: `tests/skills/.last-run.json`
- Per-case artifacts: `tests/skills/.artifacts/`

Native lifecycle regressions run only with an explicit opt-in, in disposable
fixture repositories with frozen support copies and independent file/index
assertions. They never use the candidate workflow to approve its own changes:

```bash
python3 scripts/run_runtime_regression.py --live --runtime codex --output .compass/results/native-regression-run
python3 scripts/run_runtime_regression.py --live --runtime claude --output .compass/results/native-regression-run
```

Choose a fresh output directory for a rerun. Cases cover plan-only, execution,
review-only, stop/resume and invalid-flag rejection. Missing CLIs, authentication
failures and timeouts are reported as unavailable/failure, not passing tests.
Unit tests run with `python3 -m pytest tests`; explicitly naming `tests` avoids
recursively collecting the plugin's repository symlink.

## Claude Plugin Surface

The following describes the legacy workflow (deprecated since v2.12.0) unless a line names paired-session.

The commands, configuration tables, reviewer modes, and included agent list
below describe the current Claude Code plugin surface. They are not yet part of
the Codex Stage 1 surface beyond the six Codex skills described above (`review-loop`, `plan`, `execute`,
`guide`, `paired-session`, `review-pr`).

## Three Skills: `plan`, `execute`, `review-loop`

Starting in v2.6.0 the workflow is split into three composable skills. Pick
the one that matches where your work currently is:

- **`/review-loop`** — the umbrella. With the default entry, fresh work goes to the paired-session
  coordinator and code already implemented to a paired-session review-only run (`run --review-only`);
  an existing plan becomes the work item (PLAN drafts and reviews it again); resuming a legacy session is
  refused (the legacy workflow described here was removed from routing in v2.13.0).
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
| `before-polish` | After Step 3.4 gate APPROVE/SKIP, before Step 3.5 Quality Polish |
| `before-docs` | Before Step 3.6 Documentation Consistency |
| `before-security` | Before Step 3.7 Security Preflight |
| `before-delivery` | Before Step 4 Delivery |
| `delivery` | Default — no early stop |

Unsupported values are rejected at parse time, before any lock is acquired
or session field is written. Codex Stage 1 supports the same full set here;
Codex Stage 1 supports `before-polish`, `before-docs`, and `before-security` as clean stop points.

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

Follows `entry`: by default a paired-session review-only run on the uncommitted change (review, fix,
one fix round for the non-blocking findings, simplify, consolidate tests, docs, security; `accept` makes one local commit unless `auto_commit: false`).
`--legacy` or `entry: legacy` runs the legacy loop (deprecated). Arguments: `[max-rounds]`,
`--skip-reorganize`, `--legacy`; `--reorganize` is legacy only.

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
| `entry` | absent = `paired-session` | `paired-session` (exact value); `legacy` is refused since v2.13.0; anything else is warned about and treated as absent |
| `reviewer` | `codex` | Shared Claude/plugin reviewer mode; Codex Stage 1 does not use this key to choose the reviewer backend |
| `reviewer_model` | `""` | Path-specific reviewer override; in Codex Stage 1 this applies only to the default Claude CLI reviewer path |
| `judgment_model` | `""` | Shared tier override for judgment-tier agents; Codex Stage 1 also uses it as the fallback model for the default Claude reviewer path |
| `cheap_model` | `""` | Shared tier override for cheap-tier agents; default backstop is `claude-opus-5-5`; accepted-but-no-op in Codex Stage 1 |
| `executor_model` | `inherit` | Path-specific Claude executor override; `""` and `inherit` both fall through to `judgment_model`; ignored by Codex Stage 1 |
| `codex_reviewer_backend` | `claude_cli` | Codex Stage 1 only; keeps review on the outside-sandbox Claude reviewer unless set to `codex` explicitly |
| `codex_reviewer_model` | `""` | Codex Stage 1 only; local Codex reviewer override when `codex_reviewer_backend: codex` |
| `codex_executor_model` | `""` | Reserved and ignored in Codex Stage 1 |
| `soft_limit_plan` | `3` | After N rounds, ask user to continue if CRITICALs remain |
| `soft_limit_exec` | `3` | Same for execution phase |
| `auto_commit` | `false` | Stage changed files and commit after delivery. Legacy workflow only: paired-session reads `auto_commit` only from the operator profile and prints a warning when `auto_commit: true` is set here. Review-only runs (`/review-loop` on existing code, code-quality-loop) default to `true`: one local commit at `accept`, never a push; code-quality-loop honours an explicit `auto_commit: false` here |
| `commit_message_prefix` | `feat` | Conventional commit type prefix |
| `docs_file` | `CHANGELOG.md` | File to append delivery summary; `""` to skip |
| `handsfree` | `false` | Default to hands-free mode (decisions go to Reviewer) |
| `review_focus` | `""` | Project-specific review priorities (free text); paired-session: `--review-focus` (L105), frozen at run start, for the reviewer, shadow and gate |
| `quality_focus` | `""` | `quality_focus` applies only when Step 3.5 Quality Polish actually runs. Paired-session: `--quality-focus` (L105), frozen at run start, for the POLISH-Q specialists |
| `review_style` | `""` | Tone and rules for all reviews (free text); paired-session: `--review-style` (L105), frozen at run start, for every review role |
| `skip_quality_polish` | `false` | `skip_quality_polish: true` mints `polish` as a no-op completion and still continues through docs and security. |
| `cross_vendor_review` | `auto` | `auto` or `off`; a same-vendor final review gets one extra other-vendor review |
| `adversarial_gate_skip_paths` | `["**/SKILL.md", "docs/protocol/**", "tests/skills/contracts/**"]` | Step 3.4 terminal adversarial gate — skip when every Step 3 changed file matches one of these glob patterns. |

For Codex Stage 1, the reviewer separation policy is explicit: unless
`codex_reviewer_backend: codex` is set, review stays on the
outside-sandbox Claude CLI reviewer path. That default path resolves its model
as `reviewer_model` > `judgment_model` > `claude-opus-5-5` and passes it via
`--model`. The local Codex reviewer is opt-in only. The `cheap_model` entry is
accepted in the shared config but remains a no-op in Stage 1 because only
judgment-tier Codex agents are currently shipped.
`quality_focus` applies only when Step 3.5 Quality Polish actually runs.
`skip_quality_polish: true` mints `polish` as a no-op completion and still continues through docs and security.

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
progress. The default entry (paired-session) has hard round caps instead (plan 3 / exec 4 unless set) that
end in a HOLD; at that HOLD `resume --add-rounds N` continues the same run (L100). A legacy-format run
(`--lifecycle-mode off`) may instead `accept --override-rejection --reason TEXT`; the worktree lifecycle
refuses that override.

**Quality Polish** — After the adversarial review loop approves, a suite of
specialized agents automatically runs static analysis, simplification, test
coverage, and comment checks. Configurable via `quality_focus` and
`skip_quality_polish`.

## File Structure

The tree below shows the Claude/plugin-side structure. Codex Stage 1 also uses
the runtime paths `.agents/skills/` and `.codex/agents/` for its repo skills
and subagents. Six skills are wired for Codex: `review-loop`, `plan`, `execute`, `guide`,
`paired-session` and `review-pr`.

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
