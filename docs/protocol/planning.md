# Protocol — Planning Phase

The planning phase drives a work item from a raw description to a
reviewer-approved plan. The output of a successful planning loop is a
populated `## Approved Plan` in the session file with
`plan_source: reviewer-approved`.

This document is runtime-agnostic. Places where the Claude Code and Codex
runtimes dispatch agents differently are marked with
`{{claude_code|codex}}` placeholder blocks; each runtime's SKILL.md resolves
those placeholders with its own dispatch mechanism.

The session file schema, lifecycle of `## Approved Plan`, and related
metadata rules live in [session-file.md](./session-file.md). The executor and
reviewer output schemas live in [executor-output.md](./executor-output.md)
and [reviewer-output.md](./reviewer-output.md).

---

## Phase entry conditions

The planning loop runs when:

- A `plan` skill invocation starts fresh (`entry_point: plan`).
- The umbrella `review-loop` skill enters fresh with no pre-existing plan or
  code (`entry_point: review-loop`, Step 1.5 auto-routing picked "fresh").

The planning loop does **not** run when:

- `execute --session <uuid>` is resuming a session whose `## Current Phase`
  is `execution` (orchestrator jumps straight to the execution loop).
- `execute --plan <text|path>` — user text is injected verbatim into
  `## Approved Plan`, `plan_source ← user-supplied`, loop skips planning.
- `execute --review-only` — Approved Plan is set to the review-only
  sentinel, loop skips planning.

See [session-file.md §Entry-mode initialization table](./session-file.md#entry-mode-initialization-table)
for the exact section content per entry mode.

---

## Loop state

Initialize at phase start:

```
loop_state = {
  phase: "planning",
  round: 0,
  plan_version: null,
  findings: [],        # accumulated across rounds
  pending_issues: [],
  resolved_issues: [],
  timing: {            # wall-clock time tracking per step
    loop_start: <now>,
    steps: []          # [{phase, round, role, start, end, duration_s}]
  },
  token_usage: {       # best-effort tracking
    executor: 0,       # sum from agent metadata
    reviewer: 0        # sum from reviewer metadata; may be N/A
  }
}
```

Record wall-clock time before and after each Executor / Reviewer call and
append to `loop_state.timing.steps`.

---

## Round loop

Each planning round follows the same six-step sequence.

### 1. Update context file before calling agents

- Write / update every canonical section in the session file per
  [session-file.md §Canonical sections](./session-file.md#canonical-sections).
- Set `## Current Phase: planning`.
- Round 2+: update `## Review History` with the findings from the previous
  round (resolution status included).

### 2. Call the Executor

Dispatch the Executor with the planning task. The prompt is constructed as
follows:

```
You are the Executor in a review-loop workflow.

{contents of agents/executor.md body — the system prompt}

Read the context file first: {session_file_path}
DO NOT modify the context file — return your output as described in
the output format above.

## Your Task
Produce a detailed solution plan following the output format in your
instructions.

{if round > 1:}
## Previous Reviewer Feedback (address each point)
{reviewer_feedback — the one artifact passed directly, for immediacy}
```

#### Executor dispatch {{claude_code|codex}}

{{claude_code}}

Use the Agent tool with `subagent_type: general-purpose`. Do **not** use
`subagent_type: review-loop:executor` — plugin-defined agent types have their
Write/Edit tools silently blocked by the Claude Code sandbox; the Executor
will be unable to create or modify files, `tool_uses` will be 0, and the
output will be hallucinated. Always inline the full body of
`agents/executor.md` in the `prompt` parameter.

```
Agent tool parameters:
  subagent_type: general-purpose
  model: {executor_model if not "inherit"; else omit}
  prompt: |
    {the prompt template above}
```

{{codex}}

Spawn the `review_loop_executor` Codex subagent with a **fresh,
self-contained prompt** that embeds the work item, relevant session content,
and the required planning schema directly. Do not rely on inherited or
forked parent-thread context. Codex runs within its own sandbox; no extra
tool-type workaround is required.

---

### 3. Update context file with the Executor's output

**Before** calling the Reviewer. This is load-bearing: the Reviewer reads
the session file for orientation; stale data here means an incorrect review.

- Write the current round's draft into `## Draft Plan` (the non-canonical
  supplemental section that exists only during the planning phase; see
  [session-file.md §Draft Plan](./session-file.md#draft-plan-planning-phase-only)).
  Overwrite the section in full each round; earlier drafts are not
  retained there. `## Approved Plan` stays empty (no `Source` sub-field)
  until the Reviewer returns APPROVE.
- If the Executor reported file changes (planning rounds rarely do, but
  spike validations may), update `## Files Changed` and
  `## Key Related Files`.

### 3.5. Optional context-persist sub-step

Best-effort context management. Skip entirely if session-state telemetry is
unavailable (no `~/.claude/session-state.json` or runtime-equivalent).

1. Read `~/.claude/session-state.json` (or runtime-equivalent). If absent or
   malformed, skip.
2. Parse `context_pct`. If missing, skip.
3. Resolve `threshold` from `.review-loop/config.md` field
   `context_persist_threshold`: parse as an integer in the range
   `[0, 100]`; on absent / unreadable / non-integer / out-of-range,
   fall back to `70` silently.
4. If `context_pct >= threshold`:
   a. Derive `task_slug` from the earliest clear task description
      (lowercase kebab-case, max 5 words).
   b. Scan conversation for expensive intermediate results (coordinates,
      calibration values, benchmark numbers, discovered API structures).
   c. If results found, write to
      `{cwd}/.claude/results/{YYYY-MM-DD}_{task_slug}.json` using
      idempotent merge (replace by label, append new, never delete). Update
      `~/.claude/persist-state.json` atomically with `last_persisted_at`,
      `context_pct_at_persist`, `task_slug`.
   d. Log either a persist summary or a "no intermediate results" line.
   e. Continue to the Reviewer regardless.
5. If `context_pct < threshold`, skip silently.

**Config field** — `context_persist_threshold` (integer, default `70`)
in `.review-loop/config.md`, written as a flat `key: value` line
alongside the other keys (`reviewer:`, `skip_quality_polish:`, etc. —
see `review-loop-config.example.md`). Repos that want a more
aggressive persist cadence (triggering sooner) set a smaller value;
repos that want less frequent persistence set a larger one.
Out-of-range (outside `[0, 100]`) or unparseable values silently fall
back to `70`.

### 4. Call the Reviewer

Build the review content template:

```
Read the context file first: {session_file_path}
DO NOT modify the context file.

## Solution Plan to Review
{executor_plan}

{if round > 1:}
## Review History
You are reviewing round {round}. Here is what you found in previous rounds
and what has changed since. Pay special attention to whether previously
identified CRITICAL issues have been properly addressed.

{for each finding in loop_state.findings:}
- Round {n}: [{severity}] {description} → {Executor claims fixed | Still pending | Accepted}

## Your Focus This Round
1. Verify that previously flagged CRITICAL issues are actually resolved.
2. Check whether the fixes introduced new problems.
3. **Scope Drift**: check whether the Executor quietly changed the plan's
   design decisions while addressing feedback. A fix for a CRITICAL issue
   should not silently introduce new trade-offs, relax constraints, or
   change the agreed approach. If it does, flag as CRITICAL.
4. Review any new aspects of the plan not covered before.

{else (round == 1):}
## Your Task
This is the first review. Review the plan critically from scratch.
{endif}

{if review_style is set:}
## Review Style
{review_style}

Return your structured verdict following the output format in your
instructions above.
```

#### Reviewer dispatch {{claude_code|codex}}

{{claude_code}}

Two modes, controlled by `reviewer:` in `.review-loop/config.md`.

- **Mode `codex`** — invoke the Codex CLI in non-interactive, read-only
  mode. Prepend the full `agents/reviewer.md` body (everything below the
  frontmatter) to the review content template, because Codex does not load
  Claude Code agent definitions. Use single-quoted heredoc
  (`<<'REVIEW_PROMPT'`) so zsh does not expand `$variables` inside the
  prompt. Run **synchronously** (never with `run_in_background: true`).
  Capture output with `-o` to a round-scoped temp file, then read the file.
  If `codex exec` fails non-zero, fall back to subagent mode **for this
  round only**; do not ask the user and do not stop the loop. Never fall
  back to `subagent_type: review-loop:reviewer` — plugin agent types have
  tools silently blocked.
- **Mode `subagent`** — use the Agent tool with
  `subagent_type: general-purpose`. Inline the `agents/reviewer.md` body at
  the top of the `prompt`, then append the review content template. Plugin
  agent types are off-limits (sandbox bug). Include an explicit
  "Report only, do not modify any files" instruction at the end of the
  prompt.

Both modes are stateless per round; the Orchestrator compensates by
including Review History in the prompt.

{{codex}}

Default reviewer path: `claude -p --no-session-persistence --output-format
json {optional_model_flag}` with stdin fed from
`.review-loop/tmp/{session_id}-reviewer-prompt.txt`. Run **outside** the
Codex sandbox. Parse the first JSON result object from stdout; validate its
`result` field against the shared reviewer schema.

If Claude invocation fails or validation fails, **do not retry Claude** for
that round. Record a short failure-reason summary in `## Review History`
(execution, JSON parsing, missing `result`, or schema validation) and spawn
`review_loop_reviewer` as the fallback. The fallback uses the same review
content and the same reviewer schema rules. If the fallback output is
invalid, retry once with explicit correction instructions; if the retry is
still invalid, stop and surface the failure to the user.

If `codex_reviewer_backend: codex` is set in config, skip the Claude path
entirely and use `review_loop_reviewer` directly.

Delete `.review-loop/tmp/{session_id}-reviewer-prompt.txt` immediately after
the Claude command returns (success or failure).

---

### 5. Parse the Reviewer's response

- Extract `### VERDICT:` (`APPROVE` | `REQUEST_CHANGES`). See
  [reviewer-output.md](./reviewer-output.md) for the full schema + rejection
  rules.
- Extract all issues with severity (`[CRITICAL]` / `[MINOR]`).
- Update `loop_state`: add new findings, mark previously-pending findings as
  resolved or still-pending based on the new report.
- If the reviewer output is invalid under the shared schema, reject and use
  the retry / fallback path documented in
  [reviewer-output.md](./reviewer-output.md).

---

### 6. Display Live Report

Render a per-round summary to the user:

```
── review-loop: Round {n} (Planning) ───────────────
Executor: {duration}s  |  Reviewer: {duration}s
Reviewer found:
  [CRITICAL] {issue description}
  [MINOR] {issue description}
Verdict: {APPROVE | REQUEST_CHANGES}
{if APPROVE: ✓ Plan approved — proceeding to execution}
{if REQUEST_CHANGES: → sending feedback to Executor...}
────────────────────────────────────────────────────
```

If no issues: `Reviewer found: No issues. Clean approval.`

The Live Report is **not optional**. Users must see what the review catches
every round, regardless of mode.

---

## Loop control

- `VERDICT: APPROVE` → promote the current `## Draft Plan` body into
  `## Approved Plan` with `- Source: reviewer-approved` as the first
  sub-field, set `## Session Metadata.plan_source ← reviewer-approved`,
  **remove `## Draft Plan` entirely** from the session file, and exit the
  planning loop. See
  [session-file.md §Draft Plan](./session-file.md#draft-plan-planning-phase-only).
  Subsequent behavior depends on the enclosing skill:
  - `plan` skill → print the UUID and a "next: run execute --session
    {uuid}" hint and exit.
  - `review-loop` umbrella → proceed directly into the execution loop
    (see [execution.md](./execution.md)).
- `REQUEST_CHANGES` → feed the reviewer's feedback to the next Executor
  round (step 2 of the next iteration).

### Soft-limit prompt

When `round >= soft_limit_plan` AND the latest verdict is still
`REQUEST_CHANGES` with CRITICALs, pause and ask:

> "Planning has run {N} rounds and still has open CRITICAL issues:
>  {list}. Continue iterating, or proceed with the current plan?"

The user decides. Handsfree mode forwards this as a decision-type question
(see [§Question classification](#question-classification)).

### Stuck detection

If the same CRITICAL issue (same description, same file/line anchor where
applicable) appears 3 rounds in a row **without progress**, stop and
escalate to the user. The Executor likely cannot resolve it without human
guidance. Surface the full history of the issue so the user can unblock.

---

## Question classification

If the Executor raises a question (detected in its output — e.g. the
`### Open Questions` section in the planning schema), classify it:

- **External info** — credentials, file paths outside the repo, business
  rules not in context, environment details. → **Always** pause and ask the
  user, regardless of mode.
- **Decision-type** — architecture choice, approach trade-off, ambiguous
  requirement with multiple valid solutions.
  - Default mode → pause and ask the user.
  - `--handsfree` mode → forward to the Reviewer as a decision query. The
    Reviewer returns a `DECISION: <choice>` + `REASON: <why>` pair. Log the
    decision under `loop_state.autonomous_decisions` for the delivery
    summary.

The decision query uses the same Reviewer dispatch as a normal review round,
but the prompt body is:

```
## Decision Required
The Executor encountered a decision point and needs guidance:
{executor_question}

## Work Item Context
{title + context + acceptance_criteria}

Please make a decision and provide brief reasoning.
Return: DECISION: <your choice>
        REASON: <why>
```

---

## Context management discipline

- The session file on disk is the single source of truth. Do not duplicate
  state in the Orchestrator's conversation context.
- Sub-agents read the session file every round; they are told **not** to
  modify it. The Orchestrator is the only writer.
- Between rounds, the Orchestrator keeps only: the session file path, the
  latest Reviewer feedback (passed directly to the next Executor call), and
  the loop control state (phase, round number).

This keeps the Orchestrator context lean so compaction rarely fires and all
durable state is recoverable from disk.
