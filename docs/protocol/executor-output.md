# Protocol — Executor Output Schema

The Executor produces one of two outputs depending on phase: a solution
plan in planning rounds, or an implementation summary in execution rounds.
The section headers below are **mandatory**; the orchestrator parses them
verbatim and rejects outputs that omit or rename them.

This document is runtime-agnostic. Both Claude Code and Codex runtimes
enforce the same schemas.

---

## Planning mode output

```
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
- ...

### Risks & Assumptions
- ...

### Open Questions
- ...
```

### Rules

- The outer `## Solution Plan: {title}` heading is required.
- All six `###` subsections are required in the order shown.
- `### Implementation Steps` must be a numbered list.
- `### Files to Modify / Create` entries should use backticked paths and a
  short `— reason` tail.
- `### Open Questions` is empty-list-acceptable (e.g. `- None.`) but the
  header itself must be present.

---

## Execution mode output

```
## Implementation Complete: {title}

### Changes Made
...

### Files Modified / Created / Deleted
- `path/to/file.ext` — what changed
- ...

### Deviations from Plan
None  /  [explain if any]

### Notes for Reviewer
...
```

### Rules

- The outer `## Implementation Complete: {title}` heading is required.
- All four `###` subsections are required.
- `### Files Modified / Created / Deleted` (or its variant `### Files
  Modified / Created` on the Claude Code side) must list every touched
  path. Deleted tracked files must appear. For a no-op round, the body is
  the literal word `None`.
- `### Deviations from Plan` is the literal word `None` if there were no
  deviations; otherwise an explicit explanation.
- `### Notes for Reviewer` is always present; it summarizes what the
  reviewer should focus on. For a no-op round it explicitly identifies the
  round as a no-op (e.g. "No-op round. The approved plan and current code
  already satisfy this step.").

### No-op execution round

A valid no-op execution round encodes that status explicitly in the
schema:

- `### Changes Made` states that no code changes were required.
- `### Files Modified / Created / Deleted` body is `None`.
- `### Notes for Reviewer` identifies the round as a no-op.

Same path sets alone do not prove a no-op; the orchestrator validates the
claim against the pre-round / post-round current-round delta (see
[execution.md §No-op execution round validation](./execution.md#no-op-execution-round-validation)).

---

## Rejection rules

The orchestrator treats Executor output as **invalid** and rejects it if
any of the following are true:

1. A mandatory `##` or `###` section header is missing.
2. The required section structure is present but the body is unparseable
   (for example, `### Implementation Steps` is not a list in planning mode).
3. The output claims file changes without concrete repository file paths.
4. The output claims implementation changes that are not reflected in the
   current-round delta attributable to that round (execution mode).
5. The output cannot explain a plan deviation when a deviation exists
   (execution mode, reviewer-approved / user-supplied only).

### Retry and escalation

- **Retry once.** On invalid output, the orchestrator re-dispatches the
  Executor with explicit correction instructions pointing at the specific
  rule that failed.
- **Second failure → stop and surface.** If the retry is still invalid,
  the orchestrator stops and reports the failure to the user. It does not
  silently "best-effort parse" partial output and does not invent missing
  sections.

These rules apply identically to Claude Code and Codex Stage 1.
