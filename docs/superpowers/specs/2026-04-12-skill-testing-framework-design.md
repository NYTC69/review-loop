# Skill Testing Framework Design

Date: 2026-04-12
Status: Draft for review

## Summary

Add a first-version skill testing and validation framework to this repository
before attempting a single-source workflow refactor.

The framework will target:

- `review-loop`
- `guide`

and will provide:

- static contract/lint checks
- a small number of real smoke runs
- structured result output plus human-readable terminal summaries

The purpose is to reduce skill drift risk across Claude/plugin and Codex
runtime wrappers before extracting a shared workflow core.

## Problem

The repository now contains:

- a Claude/plugin-side `review-loop` workflow
- a Codex-side `review-loop` workflow
- shared state definitions in `.review-loop/*`
- platform-specific dispatch details

This creates a high risk of workflow drift:

- one side gains a new phase or rule and the other side does not
- runtime contracts diverge silently
- smoke-only bugs are discovered only after a real session fails
- refactoring toward a shared workflow core becomes unsafe without regression
  guards

The hardest part of skill development here is not writing prompts. It is
verifying that workflow changes did not silently break the system.

## Goals

- Create a minimal but useful validation framework for `review-loop` and
  `guide`.
- Catch obvious cross-runtime drift with static checks.
- Catch the most important runtime failures with a few real smoke runs.
- Preserve evidence for failures in a way that makes debugging practical.
- Allow CLI-dependent smoke tests to skip cleanly when required tools are
  missing.
- Create a foundation for a later single-source workflow refactor.

## Non-Goals

- Full transcript replay
- Full mocking of Claude or Codex CLIs
- Full CI integration in the first version
- Coverage for every skill in the repository
- Auto-fixing detected drift
- The actual single-source workflow refactor
- A generic framework for arbitrary unrelated skills

## Scope

### Included in v1

- Static checks for `review-loop`
- Static checks for `guide`
- Smoke runs for `guide`
- Smoke runs for `review-loop`
- Shared result reporting and artifact capture

### Excluded in v1

- `code-quality-loop`
- `review-pr`
- `reorganize`
- Non-review-loop plugins or skills

## Proposed Structure

Use a mixed structure:

```text
tests/skills/
├── contracts/
│   ├── review-loop.json
│   ├── guide.json
│   └── shared-schema.json
├── fixtures/
│   ├── sessions/
│   ├── reviewer-prompts/
│   └── expected/
├── smoke/
│   ├── guide.shared-state.codex.json
│   ├── review-loop.noop.codex-fallback.json
│   ├── review-loop.noop.claude-default.json
│   └── guide.plugin-surface.readme.json
├── .artifacts/
│   └── <case-id>/
└── .last-run.json

scripts/
├── run-skill-lint
├── run-skill-smoke
└── run-skill-tests
```

### Why this structure

- `tests/skills/contracts/` keeps static assertions separate from execution
  logic
- `tests/skills/fixtures/` keeps reusable samples centralized
- `tests/skills/smoke/` makes the supported smoke matrix explicit
- `scripts/` provides ergonomic entrypoints without mixing implementation with
  fixtures

### Contract file format

The first version standardizes on JSON for machine-readable contract files and
smoke case definitions. Do not defer this choice to implementation planning.

Use these JSON roles:

- `tests/skills/contracts/review-loop.json`
  - static assertions specific to the `review-loop` wrappers and agents
- `tests/skills/contracts/guide.json`
  - static assertions specific to the `guide` wrappers and docs
- `tests/skills/contracts/shared-schema.json`
  - shared schema assertions reused by both runtimes

These files are machine-readable assertion definitions, not prose reference
documents.

## Output Model

### Terminal output

Every run should emit concise case statuses:

- `PASS`
- `FAIL`
- `SKIP`

### Structured result file

Write an aggregate result file to:

```text
tests/skills/.last-run.json
```

Each case result should include:

- `id`
- `type` (`lint` or `smoke`)
- `target` (`review-loop` or `guide`)
- `runtime` (`claude`, `codex`, or `shared`)
- `status` (`pass`, `fail`, `skip`)
- `reason`
- `artifacts`
- `started_at`
- `finished_at`

### Artifacts directory

Each smoke case writes to:

```text
tests/skills/.artifacts/<case-id>/
```

Per-case artifact collection should be evidence-first:

**Common artifacts**
- `meta.json`
- `stdout.txt`
- `stderr.txt` if relevant

**`review-loop` artifacts**
- `session-path.txt`
- `session-final.md`
- `reviewer-prompt.txt` when available
- `reviewer-result.json` when available
- `git-status-before.txt`
- `git-status-after.txt`

**`guide` artifacts**
- `response.txt`
- `assertions.json`

## Static Checks

### File existence checks

The framework must verify the existence of:

**Claude/plugin-side wrappers**
- `skills/review-loop/SKILL.md`
- `skills/guide/SKILL.md`

**Codex-side wrappers**
- `.agents/skills/review-loop/SKILL.md`
- `.agents/skills/guide/SKILL.md`

**Codex agents**
- `.codex/agents/review-loop-executor.toml`
- `.codex/agents/review-loop-reviewer.toml`

### Shared contract checks

The framework must assert that the required workflow contracts exist in the
expected wrappers:

**Codex `review-loop`**
- session canonical sections
- reviewer output schema
- executor output schema
- Claude reviewer command contract
- soft limit behavior
- reviewer fallback rules
- hallucination guards

**Claude `review-loop`**
- planning loop
- execution loop
- session-file behavior
- reviewer verdict behavior
- quality/delivery flow

### Concrete assertion targets

The first version must define literal assertion targets for the static checks.
Do not leave these as category names only.

Examples of required concrete assertions:

**Codex `review-loop` wrapper**
- must contain `.review-loop/sessions/{uuid}.md`
- must contain `.review-loop/tmp/{uuid}-reviewer-prompt.txt`
- must contain `claude -p --no-session-persistence --output-format json`
- must contain `review_loop_executor`
- must contain `review_loop_reviewer`
- must contain `## Session Metadata`
- must contain `### Reviewer Output Schema`
- must contain `### Executor Output Schema`
- must contain `soft_limit_plan`
- must contain `soft_limit_exec`

**Claude `review-loop` wrapper**
- must contain `soft_limit_plan`
- must contain `soft_limit_exec`
- must contain `Current Phase`
- must contain `Approved Plan`
- must contain `Review History`
- must contain `Quality Polish`
- must contain `Delivery`

**Guide wrappers / docs**
- must contain `.review-loop/config.md`
- must contain `.review-loop/sessions/`
- must contain `codex_reviewer_backend`
- must distinguish Codex Stage 1 surface from Claude/plugin surface

The implementation plan should be able to translate these directly into string
or structure assertions without inventing additional interpretation.

### Anti-pattern checks

**Claude/plugin-side forbidden patterns**
- `subagent_type: review-loop:`

**Codex-side forbidden patterns**
- this v1 check must be represented concretely as a forbidden string check:
  - `inherited or forked parent-thread context`
  - and/or the absence of the required positive phrase:
    `fresh self-contained prompt`

**Reviewer contract checks**
- allowed severities must be explicitly declared
- empty `Issues` behavior must be explicitly declared

### Cross-file consistency checks

The framework must detect contradictions across:

- `review-loop-config.example.md`
- `README.md`
- `.agents/skills/guide/SKILL.md`
- `.agents/skills/review-loop/SKILL.md`
- `.codex/agents/review-loop-reviewer.toml`
- `.codex/agents/review-loop-executor.toml`

Consistency targets include:

- `reviewer_model` semantics
- `codex_reviewer_backend` semantics
- `codex_reviewer_model` semantics
- `executor_model` / `codex_executor_model` semantics
- executor schema parity between orchestrator and executor agent
- reviewer schema parity between orchestrator and fallback reviewer agent

### Consistency mapping definition

The first version must not leave cross-file consistency as a category only.
For each consistency target, define:

- the files to compare
- the normalized expected phrase in each file

Example:

```json
{
  "id": "reviewer_model.claude_default",
  "kind": "consistent_with",
  "files": [
    {
      "path": "README.md",
      "needle": "reviewer_model applies to the Claude CLI reviewer path"
    },
    {
      "path": ".agents/skills/guide/SKILL.md",
      "needle": "reviewer_model still applies to the Claude CLI reviewer path"
    },
    {
      "path": ".agents/skills/review-loop/SKILL.md",
      "needle": "reviewer_model applies only to the Claude CLI reviewer path"
    }
  ]
}
```

The runner's `consistent_with` implementation should compare normalized
presence of these configured phrases only. It must not attempt general semantic
similarity.

### Session fixture checks

Fixture sessions must include:

- `Problem Description`
- `Context`
- `Acceptance Criteria`
- `Current Phase`
- `Approved Plan`
- `Review History`
- `Files Changed`
- `Key Related Files`
- `Timing Log`
- `Session Metadata`

`Session Metadata` must be the final section.

## Smoke Cases

The first version should implement exactly these smoke cases.

### Smoke assertion mapping

The first version must define concrete meanings for smoke assertion ids.
Do not treat them as symbolic labels only.

At minimum:

- `session_created`
  - a session markdown file exists under `.review-loop/sessions/`
- `planning_round_recorded`
  - `Review History` contains a planning round entry
- `execution_or_noop_round_recorded`
  - `Review History` contains either an execution round entry or an explicit
    no-op execution outcome
- `reviewer_backend_codex`
  - the relevant round records `reviewer_backend: codex`
- `reviewer_prompt_exists`
  - a reviewer prompt file artifact exists
- `shared_config_path_mentioned`
  - output contains `.review-loop/config.md`
- `shared_sessions_path_mentioned`
  - output contains `.review-loop/sessions/`
- `reviewer_backend_behavior_mentioned`
  - output mentions default reviewer plus fallback behavior

### 1. `guide.shared-state.codex`

- type: `smoke`
- runtime: `codex`
- target: `guide`

Assertions:
- repo skill can be discovered
- response mentions `.review-loop/config.md`
- response mentions `.review-loop/sessions/`
- response mentions default reviewer and fallback behavior

Skip condition:
- `codex` CLI unavailable

### 2. `review-loop.noop.codex-fallback`

- type: `smoke`
- runtime: `codex`
- target: `review-loop`

Case:
- minimal no-op documentation task
- temporary config sets `codex_reviewer_backend: codex`

Assertions:
- session file created
- planning round recorded
- execution or no-op round recorded
- per-round reviewer backend recorded as `codex`

Skip condition:
- `codex` CLI unavailable

### 3. `review-loop.noop.claude-default`

- type: `smoke`
- runtime: `codex` + `claude`
- target: `review-loop`

Case:
- same minimal no-op documentation task
- no `codex_reviewer_backend` override

Assertions:
- session file created
- default Claude reviewer path is attempted
- if reviewer prompt file exists, preserve it as an artifact
- if Claude reviewer result is accepted, preserve structured result evidence
- if fallback occurs, preserve a reason artifact

Skip condition:
- `codex` CLI unavailable
- `claude` CLI unavailable

### 4. `guide.plugin-surface.readme`

- type: `lint`
- runtime: `shared`
- target: `guide`

Assertions:
- README clearly distinguishes the Claude/plugin surface from Codex Stage 1
- shared state model is documented consistently

This case is a static docs contract check, not a CLI smoke run. It belongs in
`run-skill-lint`, not `run-skill-smoke`.

## CLI Dependency Policy

Smoke tests may depend on locally installed CLIs:

- `codex`
- `claude`

If a required CLI is missing, the case must return `SKIP`, not `FAIL`.

The first version should not attempt to mock these CLIs.

### Execution Policy

Each smoke case may set an optional top-level `execution_policy` field with
one of two values:

- `strict` (default) — any subprocess timeout, non-zero subprocess exit,
  assertion failure, missing required artifact, or malformed case JSON
  yields `status: "fail"`.
- `best_effort` — only subprocess timeouts are downgraded. When a case
  running under `best_effort` times out, the runner emits `status: "skip"`
  with one of two reason strings:
  - `"best-effort smoke timed out"` — default reason.
  - `"best-effort smoke hit environment limitation"` — used when the
    timed-out stderr contains any snippet from the runner's
    `ENVIRONMENT_ERROR_SNIPPETS` list. The list currently contains one
    entry, the Codex session-directory access error string.

Assertion failures, non-timeout non-zero exits, missing artifacts, and
malformed case JSON still FAIL under `best_effort` — the policy only
relaxes the timeout path. Cases currently opting into `best_effort` are
`tests/skills/smoke/review-loop.noop.codex-fallback.json` and
`tests/skills/smoke/review-loop.noop.claude-default.json`.

## Artifact and Cleanup Policy

- Smoke tests may create temporary `.review-loop/config.md` files to force
  specific runtime paths.
- If `.review-loop/config.md` already exists before a smoke test starts, the
  runner must back it up, use the temporary test config, and restore the
  original file after the case completes.
- Tests must clean temporary config after the run unless explicitly preserving
  a failure artifact is necessary.
- Session artifacts may be copied into `tests/skills/.artifacts/<case-id>/`
  even if the original `.review-loop/sessions/` path is gitignored.

## Smoke Case Definition Format

Smoke case definitions under `tests/skills/smoke/` must also use JSON.

Minimum schema:

```json
{
  "id": "review-loop.noop.codex-fallback",
  "type": "smoke",
  "target": "review-loop",
  "runtime": ["codex"],
  "requires": ["codex"],
  "setup": {
    "temp_config": "codex_reviewer_backend: codex\n"
  },
  "assertions": [
    "session_created",
    "planning_round_recorded",
    "reviewer_backend_codex"
  ],
  "execution_policy": "best_effort"
}
```

`execution_policy` is optional and defaults to `"strict"`. Set it to
`"best_effort"` to opt the case into the relaxed timeout behavior defined
in the Execution Policy subsection above; omit the field for any case
that should fail hard on timeout.

This schema is intentionally minimal for v1 and should be implemented as a
lightweight declarative input, not as a full test DSL.

## Why This Must Come Before Shared-Core Refactor

Without these checks, a later single-source workflow extraction would have no
practical regression harness.

This framework creates the minimum viable guardrail needed to safely answer:

- did the Claude wrapper drift?
- did the Codex wrapper drift?
- did the reviewer schema drift?
- did the session contract drift?
- does the runtime still actually work?

## Open Questions

- Whether the first version should store smoke case definitions as JSON, YAML,
  or shell-readable env files
- Whether `guide.plugin-surface.readme` should remain a lightweight smoke or be
  folded into static lint once the framework exists

These questions do not block the first implementation plan.
