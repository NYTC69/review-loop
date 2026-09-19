---
name: execute
description: "Codex Stage 1 execute skill. Run the execution + quality polish + delivery stages of review-loop against one of three entry modes: resume an approved session (`--session`), execute a user-supplied plan (`--plan`), or run a pure-CR pass on the current working tree (`--review-only`). Supports batched runs via `--stop-after STAGE`. Use when you already have a plan, or only want CR on existing code."
---

# execute — codex orchestration

Read `docs/protocol/loading.md`, then run:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime codex --stage entry-execute
```

Resolve <support-root> to this plugin/repository, not the task workspace.
Keep cwd in the user's workspace. Read the complete emitted text before acting.
`docs/protocol/loading.json` is the action/prerequisite map for both runtimes.

Validate the three mutually-exclusive entry modes before locking. Run session-init for a fresh plan/review-only target, or resume for an existing session; then execution → execution-review → gate → polish → docs → security → delivery. Honor every --stop-after boundary.

Before initialization load `session-init` AND `execute-init`; before a resume
load `resume` AND `execute-init`. Before any work-agent dispatch load the
matching `planning` or `execution` bundle; before review load
`planning-review` or `execution-review`. On planning approval, only the plan-only entry loads `plan-exit`.
The umbrella retains its session/lock and continues directly into execution.
Before a stage transition, retry,
stop or error exit, load its applicable bundle per `loading.md`.
Use `parallel-review` only in the Codex orchestrator for N>1 Claude-CLI
reviewer jobs (never in Claude Code or for local Codex Reviewer agents); load
`context-persist` only when that optional substep is applicable.

Single default Claude-CLI reviewer job:
`python3 <support-root>/scripts/run_claude_reviewer.py --session-id {session_id} --model {resolved_reviewer_model}`
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

Reuse a rule unit only while its exact text remains in this live context,
using the fingerprint emitted by the loader. Fresh agents, compaction and
new invocations must load their own prerequisites. Do not use persisted session
state as proof that instructions are still available.
