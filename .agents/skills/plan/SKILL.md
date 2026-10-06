---
name: plan
description: Codex Stage 1 planning-only skill. Drives a work item from raw description to a reviewer-approved plan in `.review-loop/sessions/{uuid}.md`, then exits with a hint to resume via `review-loop:execute --session UUID`. Use when you want plan-only iteration without immediately entering execution.
---

# plan — codex orchestration

Read `docs/protocol/loading.md`, then run:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime codex --stage entry-plan --output .review-loop/tmp/protocol-entry-plan.md
```

Resolve <support-root> to this plugin/repository, not the task workspace. Cwd,
complete reads of the output file and one Bash command per loader call follow `loading.md`;
a missing, unreadable, or incompletely read file blocks the action.
`docs/protocol/loading.json` is the action/prerequisite map for both runtimes.

Run session-init, then planning → planning-review until a valid APPROVE. Promote Draft Plan and exit with the existing session hand-off hint. Never enter implementation from this skill.

Before initialization load `session-init` AND `plan-init`; before a resume
load `resume` AND `plan-init`. On planning approval, this plan-only entry loads `plan-exit`.
Every other action (work-agent dispatch, review, stage transition, retry, stop or
error exit, and the optional `parallel-review`/`context-persist`) loads its bundle per the `loading.md` table.

Single default Claude-CLI reviewer job:
`python3 <support-root>/scripts/run_claude_reviewer.py --session-id {session_id} --parent-session-id {session_id} --model {resolved_reviewer_model} --stage planning --role reviewer --timeout-seconds 570`
Rules: `docs/protocol/runtime-codex.md` §Reviewer dispatch.

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
