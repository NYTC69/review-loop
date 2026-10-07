---
name: review-loop
argument-hint: "<work item description> [--handsfree]"
description: >
  Automates a Plan-Execute-Review workflow with built-in iteration loops.
  An Orchestrator coordinates an Executor sub-agent and a configurable Reviewer
  (external AI CLI like Codex, or Claude sub-agent) to drive work items from
  description to delivery — with every review finding visible to the user.
  Trigger: "run review-loop on", "start review loop", "let the agents handle",
  or any task the user wants driven by a plan→review→implement→CR cycle.
---

# review-loop — claude orchestration

Read `docs/protocol/loading.md`. This entry may hand off to paired-session, so its
load must leave no file in the product worktree: first print a fresh bundle
directory outside it with its own Bash call,
`python3 -c 'import os, tempfile, uuid; print(os.path.join(os.path.realpath(tempfile.gettempdir()), f"review-loop-protocol-{os.getuid()}", uuid.uuid4().hex[:12]))'`,
then run, with that printed directory written out literally as `<bundle-dir>`:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime claude --stage entry-review-loop --output <bundle-dir>/protocol-claude-entry-review-loop.md
```

Resolve <support-root> to this plugin/repository, not the task workspace. Cwd,
complete reads of the output file and one Bash command per loader call follow `loading.md`;
a missing, unreadable, or incompletely read file blocks the action.
`docs/protocol/loading.json` is the action/prerequisite map for both runtimes.

Every `/review-loop` request hands off to paired-session per the entry procedures (fresh work; an existing plan as the work item; a code target as `run --review-only`; `entry: legacy` and a legacy session resume are refused): the legacy workflow was removed in v2.13.0. The legacy steps below run only under `/review-loop:legacy` (deleted in v2.13.1).
Detect fresh / plan-exists / code-exists / explicit-resume via the entry procedures. For fresh work run planning → planning-review; after approval continue execution → execution-review → gate → polish → docs → security → delivery. Do not stop after exec alone.

Before initialization load `session-init` AND `review-loop-init`; before a resume
load `resume` AND `review-loop-init`. On planning approval, only the plan-only entry loads `plan-exit`.
The umbrella retains its session/lock and continues directly into execution.
Every other action (work-agent dispatch, review, stage transition, retry, stop or
error exit, and the optional `parallel-review`/`context-persist`) loads its bundle per the `loading.md` table.

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
