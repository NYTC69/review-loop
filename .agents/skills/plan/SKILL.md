---
name: plan
description: Codex Stage 1 planning-only skill. Drives a work item from raw description to a reviewer-approved plan in `.review-loop/sessions/{uuid}.md`, then exits with a hint to resume via `review-loop:execute --session UUID`. Use when you want plan-only iteration without immediately entering execution.
---

# plan — codex orchestration

Read `docs/protocol/loading.md`, then run:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime codex --stage entry-plan
```

Resolve <support-root> to this plugin/repository, not the task workspace.
Keep cwd in the user's workspace. Read the complete emitted text before acting.
`docs/protocol/loading.json` is the action/prerequisite map for both runtimes.

Run session-init, then planning → planning-review until a valid APPROVE. Promote Draft Plan and exit with the existing session hand-off hint. Never enter implementation from this skill.

Before initialization load `session-init` AND `plan-init`; before a resume
load `resume` AND `plan-init`. Before any work-agent dispatch load the
matching `planning` or `execution` bundle; before review load
`planning-review` or `execution-review`. On planning approval, only the plan-only entry loads `plan-exit`.
The umbrella retains its session/lock and continues directly into execution.
Before a stage transition, retry,
stop or error exit, load its applicable bundle per `loading.md`.
Use `parallel-review` only in the Codex orchestrator for N>1 Claude-CLI
reviewer jobs (never in Claude Code or for local Codex Reviewer agents); load
`context-persist` only when that optional substep is applicable.

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

Reuse a rule unit only while its exact text remains in this live context,
using the fingerprint emitted by the loader. Fresh agents, compaction and
new invocations must load their own prerequisites. Do not use persisted session
state as proof that instructions are still available.
