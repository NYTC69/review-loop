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

# review-loop — Umbrella Orchestration Skill

Drive a work item from description to delivery using an Executor and an
independent Reviewer in an iterative loop. The Reviewer catches bugs,
design issues, and gaps a single agent would miss — and the user sees
every finding in real time.

This is the **umbrella** skill. For finer-grained control, see:

- `review-loop:plan` — planning phase only.
- `review-loop:execute` — three entry modes (`--session`, `--plan`,
  `--review-only`), `--stop-after`, multi-batch delivery.

The umbrella preserves the original end-to-end UX: Step 1.5 auto-routes
(plan-exists / code-exists / fresh) and hands off internally into the
planning or execution loops described by the protocol docs below.
`entry_point: review-loop` is written to the session metadata.

## Protocol Imports

The Orchestrator MUST Read each of these files at start. They are the
single source of truth for this skill's planning loop, execution loop,
session schema, and output schemas.

- `docs/protocol/session-file.md`
- `docs/protocol/planning.md`
- `docs/protocol/execution.md`
- `docs/protocol/executor-output.md`
- `docs/protocol/reviewer-output.md`

Do not re-derive any rule that already lives in a protocol doc. When a
step below says "see `docs/protocol/<doc>.md` §Foo", follow that doc
verbatim.

## Orchestrator rules

- **Plugin agent-type sandbox bug**: every Executor / Reviewer / quality
  agent invocation MUST use `subagent_type: general-purpose` with the
  agent's full `.md` body inlined in the `prompt` parameter. Never use
  `subagent_type: review-loop:<name>`. See `CLAUDE.md` §"Plugin agent
  type sandbox bug" for background.
- Only the Orchestrator writes to the session file. Sub-agents read.
- Live Reports after each round are not optional.

---

## Invocation

```
run review-loop on: <work item description> [--handsfree]
```

`--handsfree` (optional): fully autonomous mode. Decision-type
questions go to the Reviewer instead of pausing. External-info
questions (credentials, file paths outside repo, business rules not in
context) always pause. See `docs/protocol/planning.md` §Question
classification.

---

## Configuration

Read `.review-loop/config.md` if present, else defaults:

```yaml
reviewer: codex                 # "codex" | "subagent"
reviewer_model: ""              # path-specific reviewer override
judgment_model: ""              # shared tier override for judgment-tier agents
cheap_model: ""                 # shared tier override for cheap-tier agents
executor_model: inherit         # path-specific Claude executor override; "" and inherit fall through to judgment_model
soft_limit_plan: 3              # see docs/protocol/planning.md §Loop control
soft_limit_exec: 3              # see docs/protocol/execution.md §Per-stage max-round caps
auto_commit: false
commit_message_prefix: "feat"
docs_file: CHANGELOG.md
handsfree: false
review_focus: ""                # free text injected into code-review prompts only
quality_focus: ""               # free text injected into Quality Polish (Step 3.5) prompts
review_style: ""                # free text injected into ALL reviewer prompts
skip_quality_polish: false      # skip Step 3.5 entirely
```

`--handsfree` flag at invocation overrides the config value.

Shared tier contract:

- Dispatch precedence is path-specific override -> tier override -> runtime
  backstop.
- Missing `tier` defaults to `judgment`.
- Cheap-tier backstop is `claude-haiku-4-5-20251001`.
- In Codex Stage 1, `cheap_model` is accepted by the shared config but is a
  documented no-op because Stage 1 only ships judgment-tier Codex agents.
- In Codex Stage 1, review stays on the outside-sandbox Claude CLI reviewer
  path unless `codex_reviewer_backend: codex` is explicitly set.
- On that default Codex Stage 1 Claude reviewer path, the model resolves as
  `reviewer_model` -> `judgment_model` -> `claude-sonnet-4-6`.

---

## Orchestrator Instructions

When triggered, you are the Orchestrator. Coordinate Executor and
Reviewer; enforce the loop; keep the user informed via Live Reports.
Never do the planning or coding yourself.

### Step 0 — Load config and parse flags

Read `.review-loop/config.md` (or defaults). Detect `--handsfree`.
Reviewer backend availability check (`which codex` for
`reviewer: codex`; suggest `reviewer: subagent` fallback if absent).

### Step 0.5 — Initialize session file

Generate a lowercase UUID. Create `.review-loop/sessions/{uuid}.md`
with the canonical section list per `docs/protocol/session-file.md`
§Canonical sections (includes `## Problem Description`, `## Context`,
`## Acceptance Criteria`, `## Current Phase`, `## Approved Plan`,
`## Review History`, `## Files Changed`, `## Key Related Files`,
`## Timing Log`, `## Session Metadata`). `## Current Phase: planning`
(fresh) or `execution` (when Step 1.5 routes to CR).
`entry_point: review-loop`. Fresh baseline quintet from current repo
state. `plan_source` is written only after the planning loop APPROVEs
or Step 1.5 routes directly into Approved Plan / review-only; omitted
during planning draft rounds.

Acquire the single-writer lock per
`docs/protocol/session-file.md` §Lock file lifecycle. Print the
session file path.

### Step 1 — Parse the work item

Extract title, problem description, context, acceptance criteria.
Ask ONE clarifying question if critical information is missing.

### Step 1.5 — Auto-routing (preserved from v2.5.0)

The umbrella skill auto-routes based on detected state. Unlike `plan`,
which only prints a suggestion, the umbrella dispatches internally:

- **Plan already exists** (user says "review this", approved plan in
  context): skip planning; populate `## Approved Plan` with the
  existing plan and `plan_source: reviewer-approved`, set
  `## Current Phase: execution`, jump to the execution round loop.
- **Code already implemented** (user asks for CR only, OR git diff
  shows substantial task-relevant changes): treat as
  `--review-only`-equivalent. Populate `## Approved Plan` with the
  canonical sentinel per `docs/protocol/session-file.md` §Canonical
  sentinel for `review-only`, populate `## Review Target`, set
  `plan_source: review-only`, and jump to the execution loop with the
  first-round Executor skip per `docs/protocol/execution.md`
  §`--review-only` first-round skip.
- **Existing session context file** matching this task: read it and
  resume (equivalent to `execute --session <uuid>`).
- **No prior state**: start from the planning phase as normal.

Also check for `.claude/checkpoint.md`. If present, read it and inject
the content into the session file under `## Previous Session Context`.
Silently load — do not ask.

Display the detected state to the user:

```
Detected: {plan exists / code already implemented / fresh start}
{if checkpoint.md found: + Previous session checkpoint loaded}
→ Starting from: {Planning / Execution / Code Review only}
```

User can override if they disagree.

### Step 1.6 — Historical context retrieval (optional, fail-silently)

**Strictly optional. Skip silently if no external memory tool is
available. Never ask the user to install anything. Never mention the
tool name to users who don't have it.** The fail-silently contract
applies to the entire lifecycle per `CLAUDE.md` §"Optional
integrations must fail silently" — probe failure, runtime failure,
malformed output all fall through silently.

1. **Availability probe**: check if a `mempalace_search` MCP tool is
   listed, OR run `which mempalace` via Bash. If neither, skip.
2. **Resume dedup**: if the session file already has a
   `## Historical Context` section, skip.
3. Extract 1-2 specific search terms from the work item. Prefer MCP,
   else CLI with a **10-second timeout**. On any error / hang /
   timeout / non-zero exit / stderr / malformed output → silently skip
   and continue. Do not log the error; treat as "no context".
4. Append top 3 validated results under `## Historical Context`. If
   none, skip — do not add an empty section.

Display:

```
── review-loop: Starting ──────────────────────────
Work item: {title}
Problem: {problem_description}
Reviewer: {codex | subagent} ({reviewer_model})
Mode: {interactive | handsfree}
Soft limit: {soft_limit_plan} (plan) / {soft_limit_exec} (exec)
{if historical context found: Historical context: {N} relevant memories loaded}
────────────────────────────────────────────────────
```

### Step 2 — Planning phase (fresh only)

When Step 1.5 selected "fresh start", run the planning loop per
`docs/protocol/planning.md` §Round loop. All per-round dispatch,
Executor/Reviewer prompts, loop control, soft limit
(`soft_limit_plan`), stuck detection, and question classification
behavior are defined in that doc and not duplicated here.

On `VERDICT: APPROVE`: promote `## Draft Plan` → `## Approved Plan`
with `- Source: reviewer-approved`, write `plan_source:
reviewer-approved` to `## Session Metadata`, remove `## Draft Plan`
entirely, and proceed to Step 3.

### Step 3 — Execution phase

Run the execution loop per `docs/protocol/execution.md` §Step 3. The
provenance-aware reviewer prompts in that doc select the right
strictness level from `plan_source`:

- `reviewer-approved` (planning-approved or imported) → strict
  plan-conformance.
- `review-only` (code-exists auto-route) → pure CR mode; first
  Executor round is skipped.

Soft limit is `soft_limit_exec` (default 3). On APPROVE, mint `exec`
into `completed_stages` and proceed to Step 3.5.

### Step 3.5 — Quality Polish

Run Step 3.5 per `docs/protocol/execution.md` §Step 3.5. Skip entirely
if `skip_quality_polish: true`. All substeps use
`subagent_type: general-purpose` with the agent body inlined — never
plugin-defined agent types. Hallucination guard on `tool_uses: 0`.

On a no-write clean finish, mint `polish`. Any writing substep clears
`completed_stages` and replays from `exec` per
`docs/protocol/session-file.md` §`completed_stages` lifecycle.

### Step 3.6 — Documentation Consistency

Single pass per `docs/protocol/execution.md` §Step 3.6. Writes → clear
`completed_stages`, replay. No-write → mint `docs`. **After minting
`docs`, proceed to Step 3.7** — a no-op docs stage is not a terminal
state.

### Step 3.7 — Security Preflight

Single scan per `docs/protocol/execution.md` §Step 3.7. Writes to
`.gitignore` or `git rm --cached` → clear `completed_stages`, replay.
No-write → mint `security`. BLOCKED → halt; do not proceed.

Step 3.7 runs **unconditionally** after Step 3.6, regardless of
whether any prior stage wrote files. A no-op session (zero code
changes, zero doc updates) still runs this scan. The only exits before
3.7 are `--stop-after before-security` / `before-docs` /
`before-polish` / `exec-round`.

### Step 4 — Delivery

Gated by the runtime delivery gate: Claude Code requires
`{exec, polish, docs, security} ⊆ completed_stages`. Per
`docs/protocol/execution.md` §Step 4 — Delivery: if `auto_commit:
true`, stage only the Executor-reported files, commit
`{commit_message_prefix}: {title}`, append sha to `session_commits`.
Print the Delivery Summary (status, reviewer backend, plan / exec
rounds, Quality Polish summary, Review Findings table, Files Changed,
Autonomous Decisions, Unresolved Minor Issues, Time Breakdown, Token
Usage, Suggested Next Steps). Append to `docs_file` if set. Cleanup
round temp files; preserve the session file. Clear
`delivery_blocked_by ← null`. Release the lock.

---

## Reviewer Dispatch, Question Classification, Context Management

- **Reviewer Dispatch** — per `docs/protocol/planning.md` §Reviewer
  dispatch, Claude Code block. Two modes (`codex` / `subagent`).
  `subagent` uses `subagent_type: general-purpose` with
  `agents/reviewer.md` inlined plus a "Report only" instruction. Never
  `subagent_type: review-loop:<name>`.
- **Question Classification** — per `docs/protocol/planning.md`
  §Question classification. External-info always pauses.
  Decision-type pauses by default; `--handsfree` forwards to Reviewer
  and logs under `loop_state.autonomous_decisions`.
- **Context Management** — per `docs/protocol/planning.md` §Context
  management discipline. Session file on disk is the single source of
  truth. Orchestrator keeps minimal conversation context: session
  path, latest Reviewer feedback, loop control state. All durable
  state is on disk.

## Important Orchestrator Behaviors

- **Never do the work yourself** — delegate planning to the Executor
  and reviewing to the Reviewer.
- **Keep the session file up to date** — both agents read it each
  round.
- **Preserve the Reviewer's VERDICT** — never override APPROVE,
  never skip REQUEST_CHANGES.
- **Surface blockers immediately** — pause and ask on unrecoverable
  situations.
- **Make findings visible** — Live Report after each round is not
  optional.
