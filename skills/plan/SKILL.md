---
name: plan
argument-hint: "<work item description> [--handsfree]"
description: >
  Run the planning phase only: drive a work item from raw description to a
  reviewer-approved plan in `.review-loop/sessions/{uuid}.md`, then exit with
  a hint to resume via `review-loop:execute --session <uuid>`. Use when you
  want plan-only iteration without immediately entering execution.
---

# plan — Planning-Only Sub-Skill

Drive a work item through the Plan loop (Executor drafts → Reviewer
critiques → iterate) until the Reviewer returns APPROVE, then stop. This
skill does **not** enter the execution phase; it hands off to
`review-loop:execute --session <uuid>` (or to a different runtime's
`execute` skill) via the shared session file.

## Protocol Imports

The Orchestrator MUST Read each of these files at start. They are the
single source of truth for this skill's planning loop and output schemas.

- `docs/protocol/session-file.md`
- `docs/protocol/planning.md`
- `docs/protocol/executor-output.md`
- `docs/protocol/reviewer-output.md`

Do not re-derive any rule that already lives in a protocol doc. When a
step below says "see `docs/protocol/<doc>.md` §Foo", follow that doc
verbatim.

## Orchestrator rules

- **Plugin agent-type sandbox bug**: every Executor / Reviewer invocation
  MUST use `subagent_type: general-purpose` with the agent's full `.md`
  body inlined in the `prompt` parameter. Never use
  `subagent_type: review-loop:<name>` — plugin-defined agent types have
  their tools silently blocked by the Claude Code sandbox. See
  `CLAUDE.md` §"Plugin agent type sandbox bug" for background.
- The session file on disk is the single source of truth. Only the
  Orchestrator writes to it. Sub-agents read it.
- The Live Report after each round is not optional — users must see
  every review finding.

## Invocation

```
run plan on: <work item description> [--handsfree]
```

`--handsfree` forwards decision-type Executor questions to the Reviewer
instead of pausing for the user. External-info questions still pause
regardless of mode. See `docs/protocol/planning.md` §Question
classification.

---

## Step 0 — Load config and parse flags

1. Read `.review-loop/config.md` if present; otherwise fall back to the
   defaults documented in `skills/review-loop/SKILL.md` §Configuration.
2. Detect `--handsfree` in the invocation message. If present (or
   `handsfree: true` in config), enable handsfree for this session.
3. Confirm the Reviewer backend is available per `reviewer:` config
   (see `docs/protocol/planning.md` §Reviewer dispatch). For
   `reviewer: codex`, run `which codex`; if missing, suggest
   `reviewer: subagent` and exit.

## Step 0.5 — Initialize session file

1. Generate a lowercase UUID.
2. Create `.review-loop/sessions/{uuid}.md` with the canonical section
   list per `docs/protocol/session-file.md` §Canonical sections, using
   the `plan` entry-mode column of §Entry-mode initialization table.
   - `## Approved Plan` body is empty; no `Source` sub-field is written
     during planning draft rounds.
   - `## Draft Plan` is present; it will be overwritten by each planning
     round's Executor output (see `docs/protocol/session-file.md`
     §Draft Plan and `docs/protocol/planning.md` §3).
   - `## Current Phase: planning`.
3. Write the initial `## Session Metadata` block. `entry_point: plan`.
   `plan_source` is **omitted** during planning draft rounds — it is
   written on APPROVE only (per Phase 1 decision; see
   `docs/protocol/session-file.md` §Session Metadata schema).
4. Acquire the single-writer lock per
   `docs/protocol/session-file.md` §Lock file lifecycle.
5. Tell the user the session path so they can inspect it.

## Step 1 — Parse the work item

Extract from the user's message: title, problem description, context,
acceptance criteria. If critical information is missing, ask ONE
clarifying question before proceeding.

## Step 1.5 — Detect pre-existing state (no auto-dispatch)

**`plan` does not auto-route into execution.** If the work item looks
like it should skip the planning loop, print a suggestion and exit
instead of dispatching — the user is the one who chose the `plan`
entry point, and the hand-off is their call.

Check:

- **Plan already exists**: the user's message says "review this",
  points at an already-written plan doc, or the conversation shows an
  approved plan. → Suggest:
  ```
  Detected: existing plan in the conversation/context.
  Next:  review-loop:execute --plan "<text|path>" --title "<title>"
         (or review-loop:execute --session <uuid> if you already have a session)
  ```
- **Code already implemented**: `git status` shows substantial,
  task-relevant changes. → Suggest:
  ```
  Detected: implementation appears to already be in the working tree.
  Next:  review-loop:execute --review-only --description "<what was done>"
  ```
- **Neither** → proceed to Step 1.6.

Print the suggestion and exit. Do **not** dispatch Executor / Reviewer.
The user can rerun with the suggested command if they agree.

## Step 1.6 — Historical context retrieval (optional, fail-silently)

This step is strictly optional. Skip it entirely and silently if no
external memory tool is available. Never ask the user to install
anything. Never mention the tool name to users who don't have it. The
fail-silently contract applies to the entire lifecycle — probe failure,
runtime failure, malformed output — per `CLAUDE.md` §"Optional
integrations must fail silently".

1. **Availability probe**: check if a `mempalace_search` MCP tool is
   listed, OR run `which mempalace` via Bash. If neither, skip.
2. **Resume dedup**: if the session file already carries a
   `## Historical Context` section (should not happen for `plan` fresh
   init — guard anyway), skip.
3. Extract 1-2 specific search terms from the work item.
4. Call the memory tool with a **10-second timeout**. If the call
   errors, hangs, times out, returns non-zero, produces stderr, or
   returns malformed output, **silently skip this step and continue.**
5. If the top results parse cleanly, append up to 3 bullets under a
   `## Historical Context` section. Otherwise skip — no empty section.

## Step 2 — Planning round loop

Run the planning loop per `docs/protocol/planning.md` §Round loop. For
this skill specifically:

- Each round: update context file → Executor → update context file
  (write the round's draft into `## Draft Plan`) → optional
  context-persist sub-step (§3.5 in the protocol doc) → Reviewer →
  parse → Live Report.
- Loop control: `APPROVE` → promote `## Draft Plan` into
  `## Approved Plan` with `- Source: reviewer-approved`, write
  `plan_source: reviewer-approved` to `## Session Metadata`, **remove
  `## Draft Plan` entirely** from the session file, and exit the
  planning loop (do NOT continue into execution — that is the `execute`
  skill's job).
- `REQUEST_CHANGES` → feed feedback into the next Executor round.
- Soft-limit prompt + stuck detection per
  `docs/protocol/planning.md` §Loop control.

### Executor dispatch (Claude Code)

Follow `docs/protocol/planning.md` §Executor dispatch, Claude Code
block. Reminder:

```
Agent tool parameters:
  subagent_type: general-purpose
  model: {executor_model if not "inherit", else omit}
  prompt: |
    You are the Executor in a review-loop workflow.

    {contents of agents/executor.md body}

    Read the context file first: {session_file_path}
    DO NOT modify the context file.

    ## Your Task
    Produce a detailed solution plan following the output format in your
    instructions.

    {if round > 1:}
    ## Previous Reviewer Feedback (address each point)
    {reviewer_feedback}
```

Never use `subagent_type: review-loop:executor`.

### Reviewer dispatch (Claude Code)

Follow `docs/protocol/planning.md` §Reviewer dispatch, Claude Code
block. Two modes (`codex` and `subagent`) controlled by `reviewer:` in
`.review-loop/config.md`. For subagent mode, `subagent_type` is
`general-purpose` with the `agents/reviewer.md` body inlined plus an
explicit "Report only, do not modify any files" instruction.

## Step 3 — Exit with hand-off hint

After the Reviewer returns `APPROVE`:

1. The session file now has `## Approved Plan` populated with
   `- Source: reviewer-approved` and `## Session Metadata.plan_source:
   reviewer-approved`. `## Draft Plan` has been removed.
2. Release the lock per `docs/protocol/session-file.md` §Lock file
   lifecycle.
3. Print the delivery hand-off:

   ```
   ── review-loop:plan — approved ──────────────────
   Session: {uuid}
   Session file: .review-loop/sessions/{uuid}.md
   Plan rounds: {N}
   Status: Approved (plan_source: reviewer-approved)

   Next: review-loop:execute --session {uuid}
   ────────────────────────────────────────────────
   ```

The `plan` skill does not deliver code and does not enter any execution
stage. `completed_stages` is not minted here — that is strictly the
`execute` skill's responsibility. No auto-dispatch.

---

## Context Management

The Orchestrator keeps a minimal state between rounds (session path,
latest Reviewer feedback, round number). All durable state lives on
disk in the session file. See `docs/protocol/planning.md` §Context
management discipline.
