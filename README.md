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
The legacy workflow was removed in v2.13.0 (routing) and v2.13.1 (`/review-loop:legacy`, `/review-loop:plan`,
`/review-loop:execute`, the Codex plan and execute skills and the legacy protocol files): plan-only work is
`/review-loop:paired-session <work item> --plan-only`, and an existing plan or a review of existing code is
`/review-loop` (see the migration guide).
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

**Optional** — copy the config template and uncomment only the keys you change (every key is commented out at its default):

```bash
mkdir -p .review-loop
cp ~/.claude/plugins/cache/review-loop-marketplace/review-loop/<version>/review-loop-config.example.md .review-loop/config.md
```

> **After updating the plugin** — Claude Code caches plugins at session
> start. After `/plugin update`, exit with Ctrl-C twice and `claude --resume`
> to reload plugins while keeping your conversation context. This is a
> Claude Code caching behavior, not a review-loop limitation.

## Codex Stage 1

Codex uses repo skills under `.agents/skills/`. The Codex `review-loop` skill
shares `.review-loop/config.md` with Claude Code, so both runtimes read the same
project settings.
The rest of this README primarily documents the current Claude Code plugin
surface; Codex Stage 1 also exposes the paired-session skill, which is the
default review-loop entry from v2.10.0.
The legacy Codex Stage 1 workflow (its plan and execute skills, the Claude CLI reviewer launcher and the
`codex_reviewer_backend` / `codex_reviewer_model` / `codex_executor_model` keys) was removed in v2.13.0-v2.13.1;
the paired-session coordinator dispatches every role.

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

Once enabled, the four skills under `.agents/skills/` (`review-loop`,
`guide`, `paired-session`, `review-pr`) are exposed to the Codex agent and respond to
natural-language triggers like "run review-loop on this branch" or
"use paired-session for this task". Codex matches plugin skills by their
`SKILL.md` `description`, not by literal slash commands —
`/review-loop:paired-session` etc. are Claude-only and surface as `Unrecognized` in
Codex.

A fresh review-loop request hands off to the coordinator by default from
v2.10.0; ask Codex to "use paired-session for this task" to name it explicitly
(a request for "the legacy review-loop workflow" is refused: the legacy workflow was removed in v2.13.0).
It reads non-program workspace defaults from `.review-loop/paired-session.json`
when no `--config` is given; the skill passes `~/.config/review-loop/paired-session.json`
as `--config` when that file exists and you name no other profile. Program and role settings require an external operator
profile passed with `--config`, which replaces rather than layers onto the workspace profile.
Run artifacts stay outside the product workspace under
the user-level Codex state folder.

Full step-by-step + verification: [`docs/install-codex.md`](docs/install-codex.md).

## Reviewer isolation and invocation evidence

Report-only reviewers and specialists are dispatched by the paired-session coordinator as fresh
reviewer-role turns (the legacy launchers were removed in v2.13.1); see the
[reviewer runtime contract](docs/protocol/reviewer-runtime.md). The
[delivery manifest](docs/protocol/delivery-scope.md) identifies HEAD/index/worktree and pre-existing user
changes without staging or committing.

## Skill Tests

The repository includes a first-version skill testing framework for
`review-loop` and `guide`.

- `scripts/run-skill-lint` runs static contract checks (the legacy smoke suite was removed in v2.13.1)

Test output uses `PASS`, `FAIL`, and `SKIP`.

- Aggregate results: `tests/skills/.last-run.json`
- Per-case artifacts: `tests/skills/.artifacts/`

Unit tests run with `python3 -m pytest tests`; explicitly naming `tests` avoids
recursively collecting the plugin's repository symlink.

## Claude Plugin Surface

The following describes the legacy workflow (removed in v2.13.0) unless a line names paired-session.

The commands, configuration tables, reviewer modes, and included agent list
below describe the current Claude Code plugin surface. They are not yet part of
the Codex surface beyond the four Codex skills described above (`review-loop`,
`guide`, `paired-session`, `review-pr`).

## The `/review-loop` entry

- **`/review-loop`** — the entry. Fresh work goes to the paired-session
  coordinator and code already implemented to a paired-session review-only run (`run --review-only`);
  an existing plan becomes the work item (PLAN drafts and reviews it again); resuming a legacy session is
  refused. `/review-loop:plan` and `/review-loop:execute` (with `--stop-after` and
  `--accept-external-state`) were deleted in v2.13.1: plan-only work is
  `/review-loop:paired-session <work item> --plan-only`.

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

A paired-session review-only run on the uncommitted change (review, fix,
one fix round for the non-blocking findings, simplify, consolidate tests, docs, security; `accept` makes one local commit unless `auto_commit: false`).
Arguments: `[max-rounds] [--skip-reorganize]`. Since v2.13.0 `--legacy` and `entry: legacy` are refused, and
`--reorganize` is refused with a pointer to `/review-loop:reorganize` (run it after the run).

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
| `soft_limit_plan` | `3` | `--max-plan-rounds`: the PLAN round cap; at the cap the run HOLDs and `resume --add-rounds N` continues it |
| `soft_limit_exec` | `4` | `--max-exec-rounds`: the same for EXEC |
| `auto_commit` | `false` | Not applied on the main pipeline: paired-session reads `auto_commit` from the operator profile and prints a warning when `auto_commit: true` is set here. Review-only runs (`/review-loop` on existing code, code-quality-loop) default to `true`: one local commit at `accept`, never a push; both honour an explicit `auto_commit: false` here |
| `docs_file` | `CHANGELOG.md` | File to append delivery summary; `""` to skip |
| `handsfree` | `false` | Nobody answers questions: a stage A question fails the entry, and `accept` / `reject` are never run |
| `review_focus` | `""` | Project-specific review priorities (free text); paired-session: `--review-focus` (L105), frozen at run start, for the reviewer, shadow and gate |
| `quality_focus` | `""` | Paired-session: `--quality-focus` (L105), frozen at run start, for the POLISH-Q specialists |
| `review_style` | `""` | Tone and rules for all reviews (free text); paired-session: `--review-style` (L105), frozen at run start, for every review role |
| `skip_quality_polish` | `false` | `true` skips the POLISH-Q specialists and the quality writers; docs and security still run |

`reviewer_model` and `executor_model` are not applied: set to anything other than empty or `inherit`, they
print a warning; models come from the operator profile. The legacy-only keys `reviewer`, `judgment_model`,
`cheap_model`, `codex_reviewer_backend`, `codex_reviewer_model`, `codex_executor_model`,
`commit_message_prefix`, `cross_vendor_review`, `adversarial_gate_skip_paths` and `context_persist_threshold`
were removed with the legacy workflow and have no effect.

### Natural language config examples

```yaml
review_focus: |
  - Security: auth checks, input validation, SQL injection
  - Performance: N+1 queries, missing indexes

quality_focus: "strict clippy lints, skip comment analysis"

review_style: "be terse, flag any unwrap() as CRITICAL"
```

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

**Run Directory** — Each run's state, evidence, findings ledger, usage and reports live in its run
directory outside the workspace; legacy `.review-loop/sessions/` files are left on disk and never read.

**Round Caps** — The default entry (paired-session) has hard round caps (plan 3 / exec 4 unless set) that
end in a HOLD; at that HOLD `resume --add-rounds N` continues the same run (L100). A legacy-format run
(`--lifecycle-mode off`) may instead `accept --override-rejection --reason TEXT`; the worktree lifecycle
refuses that override.

**Quality Polish** — After the adversarial review loop approves, a suite of
specialized agents automatically runs static analysis, simplification, test
coverage, and comment checks. Configurable via `quality_focus` and
`skip_quality_polish`.

## File Structure

The tree below shows the Claude/plugin-side structure. Codex also uses
the runtime path `.agents/skills/` for its repo skills. Four skills are wired for Codex:
`review-loop`, `guide`, `paired-session` and `review-pr`.

```
review-loop/
├── docs/
│   └── protocol/                 ← Shared protocol docs (single source of truth)
│       ├── loading.md / loading.json   ← Protocol loading contract and map
│       └── paired-session-entry.md     ← Shared paired-session entry contract
├── skills/
│   ├── review-loop/
│   │   └── SKILL.md              ← Entry: routes to paired-session
│   ├── paired-session/
│   │   └── SKILL.md              ← Paired-session coordinator entry
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
