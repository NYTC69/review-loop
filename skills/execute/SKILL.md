---
name: execute
argument-hint: "<--session <uuid> | --plan <text|path> --title <title> | --review-only> [--stop-after <stage>] [--handsfree] [--accept-external-state]"
description: >
  Run the execution + quality polish + delivery stages of review-loop
  against one of three entry modes: resume an approved session
  (`--session`), execute a user-supplied plan (`--plan`), or run a
  pure-CR pass on the current working tree (`--review-only`). Supports
  batched runs via `--stop-after STAGE`. Use when you already have a
  plan, or only want CR on existing code.
---

# execute — claude orchestration

Read `docs/protocol/loading.md`, then run:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime claude --stage entry-execute --output .review-loop/tmp/protocol-entry-execute.md
```

Resolve <support-root> to this plugin/repository, not the task workspace. Cwd,
complete reads of the output file and one Bash command per loader call follow `loading.md`;
a missing, unreadable, or incompletely read file blocks the action.
`docs/protocol/loading.json` is the action/prerequisite map for both runtimes.

Validate the three mutually-exclusive entry modes before locking. Run session-init for a fresh plan/review-only target, or resume for an existing session; then execution → execution-review → gate → polish → docs → security → delivery. Honor every --stop-after boundary.

Before initialization load `session-init` AND `execute-init`; before a resume
load `resume` AND `execute-init`. Every other action (work-agent dispatch, review,
plan approval, stage transition, retry, stop or error exit, and the optional
`parallel-review`/`context-persist`) loads its bundle per the `loading.md` table.

The caller owns the session file and lock. Preserve unrelated dirty work;
read-only/plan-only scope and user authorization override implementation steps.
Independent Reviewer approval, output validation, rubric/triage and evidence
guards remain mandatory. Use existing configured backend/model resolution.
Never infer a PASS, a skip, or permission from not loading a future stage.

Full session schema: `docs/protocol/session-file.md`; active loops:
`docs/protocol/planning.md` and `docs/protocol/execution.md`; output contracts:
`docs/protocol/executor-output.md` and `docs/protocol/reviewer-output.md`.
These are scoped references, not an eager import list. Detailed native entry
steps live in `references/entry.md` and are selected by the loading map.
Unit reuse within one live context follows `loading.md`.
