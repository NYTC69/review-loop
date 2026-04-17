# Codex Review-Loop Design

Date: 2026-04-11
Status: Draft approved for implementation planning

## Summary

Add a Codex-native `review-loop` skill to this repository without changing the
user-facing entry name. In Claude Code, `review-loop` continues to use the
existing Claude implementation. In Codex, `review-loop` becomes a Codex-native
orchestrator that:

- runs the workflow from a Codex repo skill
- spawns a Codex subagent as the Executor
- uses `claude -p` as the default Reviewer backend
- falls back to a Codex reviewer when Claude CLI is unavailable or invalid
- shares the same `.review-loop/config.md` and `.review-loop/sessions/*.md`
  protocol used by the Claude implementation

Stage 1 includes only `review-loop` and `guide`. It does not migrate
`code-quality-loop`, `review-pr`, or `reorganize`.

## Goals

- Support `review-loop` in Codex with behavior aligned to the existing Claude
  workflow.
- Keep the user-facing command name the same across Claude Code and Codex.
- Preserve backward compatibility for `.review-loop/config.md`.
- Preserve cross-runtime reuse of `.review-loop/sessions/*.md`.
- Default to Claude as the Reviewer in Codex, with Codex as the fallback.
- Keep optional integrations optional and silently skippable.

## Non-Goals

- Packaging as a distributable Codex plugin in Stage 1
- Migrating all standalone tools in Stage 1
- Replacing Claude's current plugin implementation
- Redesigning the session document schema
- Introducing a separate Codex-only state directory

## Current Constraints

### Claude-side constraints

- Claude plugin-defined agent types are not reliable due to the known sandbox
  bug. The current Claude implementation works around this by using
  `subagent_type: general-purpose` with inline agent bodies.
- The current Claude implementation treats the session markdown file as the
  single source of truth.
- Claude config and session files are already in active use and must remain
  readable.

### Codex-side constraints

- Codex skills are repo-scoped and live under `.agents/skills/`.
- Codex subagents are defined separately from Claude plugin agents.
- Codex does not share Claude's plugin runtime, slash-command model, or
  plugin-agent bug profile.
- Codex has hooks and `AGENTS.md`, but Stage 1 should not depend on either for
  correctness.

## Stage 1 Scope

### Included

- Codex repo skill: `review-loop`
- Codex repo skill: `guide`
- Codex Executor subagent
- shared reviewer output schema across all reviewer backends
- Codex reviewer fallback path
- Claude CLI reviewer dispatch from Codex
- Codex-side hallucination guard
- Shared session/config compatibility rules

### Excluded

- Codex versions of `code-quality-loop`
- Codex versions of `review-pr`
- Codex versions of `reorganize`
- plugin packaging/distribution

## User Experience

The user calls `review-loop` in either environment and should not need to know
which backend implementation is running.

- In Claude Code: the existing Claude skill remains the implementation.
- In Codex: a new Codex skill orchestrates the same high-level workflow.

The behavioral contract should remain recognizable:

- plan -> review loop
- execution -> review loop
- live findings surfaced to the user
- persistent session log in `.review-loop/sessions/`

Stage 1 does not need to match every Claude-side implementation detail, but it
must preserve the core role split and state model.

## Architecture

### Claude Code runtime

Unchanged in Stage 1.

### Codex runtime

The Codex implementation becomes:

```text
Codex skill (orchestrator)
├── Codex subagent: executor
└── Reviewer backend
    ├── default: Claude CLI (`claude -p`)
    └── fallback: Codex reviewer
```

The Codex skill owns:

- config loading
- session file creation and updates
- phase control
- reviewer dispatch and fallback
- user-facing progress reports

The Codex executor subagent owns:

- planning output
- implementation output
- file-level change summaries

The reviewer owns:

- critical review of plans
- critical review of code changes
- plan-conformance checks
- test-strategy and test-gap detection

## Shared State Protocol

Stage 1 uses the existing Claude-side protocol as the canonical schema.

### Shared files

- `.review-loop/config.md`
- `.review-loop/sessions/{uuid}.md`

### Config compatibility rule

Do not introduce new required config keys in Stage 1.

Codex should consume the existing keys conservatively:

- `reviewer`
- `reviewer_model`
- `executor_model`
- `soft_limit_plan`
- `soft_limit_exec`
- `handsfree`
- `review_focus`
- `quality_focus`
- `review_style`
- `skip_quality_polish`

The existing `reviewer` key should keep its current Claude-side meaning for
Claude Code. Codex must not reinterpret `reviewer: codex` or
`reviewer: subagent` with inverted or surprising semantics.

In Codex runtime, the shared `reviewer` key is ignored for reviewer selection.
Codex reviewer selection is controlled only by Codex runtime defaults and
optional `codex_*` keys.

If Codex needs runtime-specific reviewer selection, Stage 1 may add optional,
Codex-only keys that Claude will ignore safely. Recommended keys:

- `codex_reviewer_backend`: `claude_cli | codex`
- `codex_reviewer_model`: free-text model override for the Codex fallback
- `codex_executor_model`: reserved for future use; not active in Stage 1

Default Codex behavior when these keys are absent:

- try `claude -p` as the reviewer
- fall back automatically to the Codex reviewer if Claude CLI is unavailable or
  invalid

This avoids semantic drift in the shared `reviewer` key while keeping shared
config backward compatible.

Model key handling in Codex runtime:

- `reviewer_model`: if Codex is using the Claude reviewer path, pass through to
  `claude --model` when non-empty
- `executor_model`: ignored by Codex runtime
- `codex_reviewer_model`: applies only to the Codex fallback reviewer
- `codex_executor_model`: reserved only; ignored in Stage 1

### Session compatibility rule

Keep the existing major sections intact:

- Problem Description
- Context
- Acceptance Criteria
- Current Phase
- Approved Plan
- Review History
- Files Changed
- Key Related Files
- Timing Log

Codex may add a small optional metadata section, but must not rename or remove
the current sections. The metadata section must be the final section in the
file, after all canonical Claude-compatible sections.

Recommended metadata block:

```md
## Session Metadata
- session_origin: codex-skill
- orchestrator_backend: codex
- executor_backend: codex-subagent
- reviewer_backend: claude-cli
- reviewer_fallback_used: false
```

This metadata must be optional and ignorable by Claude.

### Session write semantics

Stage 1 uses one session file per run, identified by a new UUID. Resuming an
existing session reopens that specific file; starting a new run always creates a
new file.

The orchestrator is the only writer of the session file. Executor and reviewer
backends never write the session file directly.

Write rules:

- canonical sections are rewritten in full on each orchestrator update
- `Review History` is logically append-only, but the orchestrator writes the
  full accumulated section content each time
- `Timing Log` is logically append-only, but the orchestrator writes the full
  accumulated section content each time
- `Files Changed` reflects the latest known state after each Executor round
- `Session Metadata` is rewritten in full by the orchestrator and remains last

Stage 1 does not support concurrent writers to the same session file. Mixed
Claude/Codex usage is supported across different sessions, not as simultaneous
writers to one session file.

## Shared Output Contracts

### Reviewer output schema

All reviewer backends in Stage 1 must produce the same schema. This applies to:

- Claude CLI reviewer
- Codex fallback reviewer

The schema matches the current Claude `agents/reviewer.md` contract:

```md
### VERDICT: [APPROVE | REQUEST_CHANGES]

### Issues
- [CRITICAL] <description> — must be resolved before proceeding
  File: `path/file.ext`, around line N
- [MINOR] <description> — recommended improvement

### Strengths
...

### Questions
- ...
```

Rules:

- valid verdicts are exactly `APPROVE` and `REQUEST_CHANGES`
- `### Strengths` is always required
- `### Issues` may be omitted only when there are no issues
- `### Questions` may be omitted only when there are no questions
- reviewer prompts must instruct the backend to follow this schema exactly
- `APPROVE` with no `### Issues` section is valid
- `REQUEST_CHANGES` with no `### Issues` section is invalid

The orchestrator parses this schema for both the Claude and Codex reviewer
paths. A reviewer response is invalid if:

- the verdict line is missing
- the verdict value is not one of the two allowed values
- the response is missing `### Strengths`
- the output is too malformed to recover issue entries safely

Invalid reviewer output triggers fallback or retry according to the reviewer
backend rules.

### Executor output schema

The Executor never writes the session file directly. It always returns
structured text to the orchestrator, and the orchestrator is responsible for
updating the session file.

Planning mode schema:

```md
## Solution Plan: {title}

### Problem Analysis
...

### Proposed Approach
...

### Implementation Steps
1. ...
2. ...

### Files to Modify / Create
- `path/to/file.ext` — reason

### Risks & Assumptions
- ...

### Open Questions
- ...
```

Execution mode schema:

```md
## Implementation Complete: {title}

### Changes Made
...

### Files Modified / Created
- `path/to/file.ext` — what changed

### Deviations from Plan
None

### Notes for Reviewer
...
```

Rules:

- the Codex Executor must follow these exact section names
- the orchestrator writes `Approved Plan`, `Files Changed`, and related session
  sections based on this structured output
- if the Executor output is materially malformed, the orchestrator must reject
  it and request a corrected response instead of guessing

## Reviewer Dispatch Design

### Default path

Codex calls Claude CLI in non-interactive mode using `claude -p`.

The Codex skill passes:

- the shared session file path
- the review content for the current round
- a strict review-only instruction
- the required output format

If `codex_reviewer_backend: codex` is set, skip the Claude CLI path and use the
local Codex reviewer directly.

The reviewer prompt should preserve the existing reviewer semantics:

- independent judgment
- no pressure to approve
- flag missing test strategy in plan review
- flag missing tests in code review
- enforce plan conformance as a critical dimension

### Review content composition

Reviewer prompt construction must differ by round type, but both backends must
receive the same logical review content.

Plan review rounds include:

- shared session file path
- current planning-phase context from the session file
- the latest Executor planning output
- prior review-history context when applicable
- the exact reviewer output schema

Code review rounds include:

- shared session file path
- current execution-phase context from the session file
- the latest Executor execution output
- the actual post-Executor changed-file list
- the actual post-Executor diff against `HEAD`
- prior review-history context when applicable
- the exact reviewer output schema

### Claude CLI invocation pattern

Spike 1 verified the following:

- the Claude reviewer path must run outside the Codex sandbox
- `--bare` is not usable for the default reviewer path because it ignores the
  existing Claude logged-in session and requires API-key style auth
- prompt delivery via stdin redirection from a rendered prompt file works
- `--output-format json` is preferred over text because text mode can be
  contaminated by hook output in some environments

Normative Stage 1 flow:

1. orchestrator renders the full reviewer prompt to a temp file inside
   `.review-loop/tmp/`
2. orchestrator runs Claude outside the sandbox using:

```bash
claude -p --no-session-persistence --output-format json {optional_model_flag} < .review-loop/tmp/{session_id}-reviewer-prompt.txt
```

3. orchestrator captures stdout as the reviewer response
4. orchestrator deletes the temp prompt file immediately after the command
   returns
5. orchestrator parses the first JSON result object from stdout
6. if any trailing non-JSON lines appear, ignore them
7. orchestrator validates the parsed `result` field content against the shared
   reviewer output schema

The rendered prompt must include:

- the shared session file path
- the review content for the current round
- a report-only instruction
- the exact reviewer output schema

### Claude reviewer guardrails

The Claude reviewer path should validate:

- CLI availability
- successful process exit
- parseable JSON result output
- presence of a `result` field containing reviewer text
- presence of a valid verdict inside that reviewer text

If validation fails, the Codex skill falls back automatically to the Codex
reviewer path for that round.

Claude reviewer retry policy in Stage 1:

- invalid output or invocation failure -> no retry
- fall back immediately to the Codex reviewer path

### Fallback path

When Claude CLI is unavailable or returns unusable output, Codex dispatches a
local reviewer implementation using an inline Codex reviewer subagent call.
That reviewer must preserve the same verdict contract expected by the
orchestrator.

Fallback reviewer rules:

- use the same `Review content composition` as the Claude reviewer path
- use the same shared reviewer output schema
- use the identical reviewer-output validation rules as the Claude reviewer path
- only the invocation mechanism changes; prompt semantics stay aligned
- the fallback reviewer is spawned as a Codex subagent, not implemented as
  ad-hoc free text in the orchestrator

### Session recording

Each round should record the reviewer backend used so cross-runtime history is
traceable.

## Codex Hallucination Guard

Stage 1 cannot assume a `tool_uses: 0` signal equivalent to Claude. Therefore
Codex must use output-evidence guards.

### Executor guard

The Executor response is considered suspicious and must be rejected or retried
if any of the following are true:

- it does not name concrete repository file paths when claiming file changes
- it describes implementation changes that are not reflected in the working diff
- it omits the required section structure
- it cannot explain deviations from plan when such deviations exist

Before accepting an execution-mode result, the orchestrator must compare the
Executor's claimed file list with the actual post-Executor changed file set.

Changed file set definition:

- tracked changes: `git diff --name-only --diff-filter=d HEAD`
- untracked files: `git ls-files --others --exclude-standard`
- actual post-Executor changed file set = union of the two lists after the
  Executor returns

Execution guard flow:

1. record the pre-Executor changed file set
2. run the Executor
3. collect the post-Executor changed file set
4. compare the Executor's claimed file list against the post-Executor set
5. reject outputs that claim file changes not present in the post-Executor set

The post-Executor set, not the pre-Executor set, is the source of truth for the
guard. The pre-Executor set is retained only to detect no-op or unchanged runs.

### Reviewer guard

A reviewer response is considered suspicious and must be rejected or retried if:

- it contains no concrete file references when reviewing code changes that
  touched files
- it makes claims about code structure that cannot be mapped to actual files or
  lines
- it omits the shared reviewer schema

For code review rounds, reviewer findings should reference specific files and
locations whenever applicable. For plan review rounds, file references are not
required, but issue descriptions must point to concrete plan gaps.

### Retry policy

- reviewer invalid output from Claude path -> use Codex fallback reviewer
- reviewer invalid output from Codex fallback -> retry once, then surface the
  failure to the user
- executor invalid output -> retry once with explicit correction instructions,
  then stop and surface the failure to the user

## Executor Design

Codex should define a dedicated Executor subagent rather than embedding all
planning and coding logic into the main skill.

Responsibilities:

- produce a plan in planning mode
- implement the approved plan in execution mode
- summarize changed files
- explicitly report deviations from plan

The output contract should align with the current Claude executor as closely as
practical so the session file structure stays comparable.

The Codex Executor definition should live as a dedicated Codex subagent config,
not as an inline free-form prompt embedded entirely in the orchestrator skill.

Stage 1 assumes Codex subagent runtime model override is not supported for the
Executor path. `codex_executor_model` remains reserved and must not be passed to
the Executor or described as active behavior in user-facing guide text.

## Guide Design

Stage 1 includes a Codex-native `guide` skill that explains:

- how `review-loop` behaves in Codex
- that the same `.review-loop/config.md` file is used
- that session logs are shared with Claude runs
- that Claude CLI is the default reviewer backend when available
- that fallback to Codex review can occur automatically

The guide should describe user-facing behavior, not internal implementation
details unless relevant for debugging.

## Optional Integrations

Carry forward the current philosophy:

- optional tools must never become hard dependencies
- absence must be silent
- runtime failure must also be silent when the feature is optional

This applies equally to Codex-side optional context retrieval or hook-based
enhancements.

## Risks

### 1. Claude CLI review-only enforcement may be weaker than expected

Mitigation:

- explicitly constrain the prompt to report-only mode
- prefer read-only invocation patterns where available
- validate output structure and fail closed to Codex fallback

### 2. Shared config semantics may drift between runtimes

Mitigation:

- do not add Stage 1 config keys
- document any Codex-specific interpretation in the guide
- treat shared config as a compatibility layer, not a place for runtime
  internals

### 3. Session file divergence could make cross-runtime history noisy

Mitigation:

- keep the current section names untouched
- add only one optional metadata block
- preserve the existing review-history style

### 4. Attempting full feature parity in Stage 1 would overreach

Mitigation:

- keep Stage 1 to `review-loop` and `guide`
- defer standalone tools to later phases

## Implementation Outline

1. Add Codex repo skill skeletons under `.agents/skills/`.
2. Add Codex subagent definition for the Executor.
3. Add a Codex reviewer fallback path.
4. Implement Claude CLI reviewer dispatch and validation.
5. Reuse `.review-loop` config/session paths exactly.
6. Add the optional session metadata block.
7. Add Codex guide documentation.
8. Validate that a Codex run can read a Claude-created session file.
9. Validate that Claude can still read a Codex-created session file.

## Testing Strategy

### Design-level verification

- Create a Codex-run session file and verify its sections remain compatible
  with the Claude schema.
- Simulate Claude reviewer failure and verify fallback to Codex reviewer.
- Validate that no new required shared config keys are introduced.
- Validate that malformed reviewer output triggers fallback as designed.
- Validate that malformed Executor output is rejected instead of being written
  into the session file.

### Behavioral verification

- Run `review-loop` in Codex on a small fixture change.
- Confirm plan review produces a structured verdict.
- Confirm execution review produces a structured verdict.
- Confirm the session file is updated round by round.
- Confirm `guide` explains the Codex behavior accurately.
- Force Codex local reviewer mode with config and confirm deterministic fallback
  testing is possible. Set `codex_reviewer_backend: codex` in
  `.review-loop/config.md` directly before running the test to bypass the
  Claude CLI path; no mock is required.

## Open Questions

- Whether Claude reviewer invocation can be further constrained with a stable
  read-only tool set across environments

## Stage 1 Spike Requirements

Implementation planning required one blocking spike and one optional
investigation.

### Completed blocking spike: `claude -p` reviewer path

Observed results:

- inside the Codex sandbox, Claude startup hit filesystem permission failures
  and network connection failures, so the reviewer path cannot rely on
  sandboxed execution
- outside the sandbox, non-bare `claude -p --no-session-persistence` works with
  the existing logged-in user session
- stdin redirection from a rendered prompt file works
- JSON output is parseable and suitable for reviewer integration

### Optional follow-up: Codex subagent observability

Stage 1 assumes no documented `tool_uses`-style metadata contract for Codex
subagents and therefore uses output-evidence guards as the normative mechanism.
If stronger subagent observability is discovered during implementation, it may
be added as a non-breaking enhancement.
