# Protocol — Reviewer Output Schema

All reviewer backends (Claude sub-agent, Claude CLI, Codex reviewer CLI,
Codex `review_loop_reviewer` subagent) return the shared Stage 1 reviewer
schema below. The orchestrator parses this schema verbatim and rejects
outputs that violate any rule.

This document is runtime-agnostic; the same schema and validation rules
apply to every reviewer backend on every runtime.

---

## Schema

```
### VERDICT: [APPROVE | REQUEST_CHANGES]

### Issues
- [CRITICAL] <description> — must be resolved before proceeding
  Trigger: ...
  Reachability: ...
  Impact: ...
  Likelihood: ...
  Fix cost: ...
  Cheaper response: ...
  File: `path/file.ext`, around line N
- [MINOR] <description> — recommended improvement
- None.

### Strengths
...

### Questions
- ...
```

---

## Rules

### Verdict

- Valid verdicts are **exactly** `APPROVE` and `REQUEST_CHANGES`. Any
  other literal (e.g. `APPROVED`, `APPROVE_WITH_CHANGES`,
  `REQUEST_CHANGE`, localized text) is invalid.
- `### VERDICT:` is required; missing verdict = invalid.

### Issues section

- Allowed issue severities are **exactly** `[CRITICAL]` and `[MINOR]`. Any
  other severity label (e.g. `[MAJOR]`, `[HIGH]`, `[WARNING]`, `[INFO]`,
  `[NITPICK]`) is invalid.
- `### Issues` may be **omitted** only when there are no issues at all.
- If `### Issues` is present with no issues, its body must contain exactly
  `- None.` — not `No issues found`, not localized free text, not an empty
  list.
- Prose placeholders ("no issues found", "N/A", etc.) inside `### Issues`
  are invalid.
- For code review, issues should point to specific files and locations
  whenever applicable. Reject code-review findings without concrete anchors
  only when the finding should reasonably be able to point to specific
  files or locations. For plan review, file references are optional, but
  issues must still point to concrete plan gaps.

### Blocking rubric (every `[CRITICAL]`)

- Every `[CRITICAL]` carries six rubric fields, each non-empty: `Trigger:`,
  `Reachability:`, `Impact:`, `Likelihood:`, `Fix cost:`, `Cheaper
  response:`. They are written as indented continuation lines under the
  bullet (normal Reviewer path) or as inline `Label:` segments inside the
  finding body (the Step 3.4 gate adapter renders each finding on one
  line); `scripts/finding_triage.py` accepts both forms equivalently.
- The syntactic parser (`_validate_reviewer_output_schema` on the Codex
  path, the prose rules above on the Claude path, the gate adapter on the
  Step 3.4 path) tolerates the continuation lines exactly like the `File:`
  annotation and is unchanged; rubric completeness is checked afterwards by
  `finding_triage.py check` (see [§Rubric gate](#rubric-gate)).
- `[MINOR]` findings and `APPROVE` outputs carry no rubric and are never
  inspected.
- Unreachable scenarios, unsupported assumption chains, negligible combined
  risk, or clearly disproportionate complexity for extremely rare low-impact
  benefit cannot block; rare but realistically reachable credential
  exposure, authorization bypass, irreversible data loss, destructive
  action, or comparable harm remains blocking.
- Plan deviations: all are disclosed by the author; a disclosed equivalent
  simplification satisfying intent and acceptance criteria is not
  automatically `[CRITICAL]`; material changes to user intent, observable
  behavior, safety, authorization, data integrity, or explicit constraints
  block; a missing disclosure alone is a `[MINOR]` record correction when
  the deviation is harmless.

### Strengths section

- `### Strengths` is **always required**. It must be present in every
  reviewer response, regardless of verdict.

### Questions section

- `### Questions` may be omitted only when there are no questions.

### Verdict / issues consistency

- `APPROVE` with no `### Issues` section → valid.
- `APPROVE` with `### Issues` containing exactly `- None.` → valid.
- `APPROVE` with any `[CRITICAL]` issue → **invalid** (semantic
  inconsistency).
- `REQUEST_CHANGES` with no `### Issues` section → **invalid**.
- `REQUEST_CHANGES` with `### Issues` containing exactly `- None.` →
  **invalid**.
- `REQUEST_CHANGES` with only `[MINOR]` issues → **invalid** (a reviewer
  cannot request changes over MINOR-only findings).

### Code-review anchor rule

A code-review response that makes claims that should reasonably have
concrete file or location anchors, but fails to provide them, is invalid.
This is a deliberate hallucination guard: vague "error handling could be
better" findings without a file:line anchor are rejected.

---

## Rejection behavior

If reviewer output violates any of the rules above, the orchestrator:

1. Records a short failure-reason summary in `## Review History` (whether
   the failure was missing verdict, missing `### Strengths`, bad severity
   label, inconsistent verdict/issues, missing anchors, or malformed
   output).
2. Does **not** guess what the reviewer meant. Do not "best-effort parse"
   a response that has no verdict or has a verdict outside the allowed set.
3. Falls back or retries per the active runtime's dispatch path. For
   dispatch and fallback behavior, see
   [planning.md §Reviewer dispatch](./planning.md#reviewer-dispatch-claude_codecodex)
   and [execution.md](./execution.md).

### Rubric gate

`scripts/finding_triage.py check --input <parsed review text>` is a
**required post-parse step** on every blocking review output, on both
runtimes and on all three parser paths — never advisory. Order: (1) the
syntactic parse above, unchanged; (2) `finding_triage.py check`, which
partitions every `[CRITICAL]` into complete / incomplete (exit 0 complete,
1 incomplete, 2 malformed input) and names the missing fields per finding.

On `incomplete` the review output is **discarded as malformed**: the
orchestrator records `rubric_incomplete: finding #n missing <fields>` in
`## Review History`, the output never becomes a valid verdict, and the
per-backend retry / terminal behavior of
[execution.md §Mandatory rubric gate](./execution.md#mandatory-rubric-gate)
applies (Claude plugin subagent: one re-dispatch with the missing-field
list, then reviewer failure; Codex Stage 1 Claude CLI: no retry; Step 3.4
gate: no producer re-run, `exec` withheld, incomplete findings follow
[execution.md §Gate rubric revalidation](./execution.md#gate-rubric-revalidation)).
**The orchestrator never implements a `[CRITICAL]` that failed triage.**
A missing rubric field requests corrected review output, never a code
change; verdicts are never overridden.

---

## Cross-references

- Reviewer dispatch templates (runtime-specific):
  [planning.md §Reviewer dispatch](./planning.md#reviewer-dispatch-claude_codecodex).
- Provenance-aware reviewer prompts (by `plan_source`):
  [execution.md §Provenance-aware reviewer prompts](./execution.md#provenance-aware-reviewer-prompts).
- Rubric gate retry table and dispute flow:
  [execution.md §Mandatory rubric gate](./execution.md#mandatory-rubric-gate),
  [execution.md §Dispute flow](./execution.md#dispute-flow).
- Executor output schema (mirrored rejection / retry discipline):
  [executor-output.md](./executor-output.md).
