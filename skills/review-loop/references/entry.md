# claude review-loop entry procedures

Read selected sections through `scripts/read_protocol.py`; shared protocol rules take precedence.

### Step 0 — Load config and parse flags


Read `.review-loop/config.md` (or defaults). Detect `--handsfree`.
Reviewer backend availability check (`which codex` for
`reviewer: codex`; suggest `reviewer: subagent` fallback if absent).

### Step 0.5 — Initialize session file

Generate a lowercase UUID. Create `.review-loop/sessions/{uuid}.md`
with the canonical section list per `docs/protocol/session-file.md`
§Canonical sections (includes `## Problem Description`, `## Context`,
`## Acceptance Criteria`, `## Current Phase`, `## Approved Plan`,
`## Current Review Packet`, `## Review History`, `## Files Changed`,
`## Key Related Files`, `## Timing Log` (13-column header per
`docs/protocol/session-file.md` §Timing Log columns), `## Evidence
Ledger`, `## Session Metadata`). `## Current Phase: planning`
(fresh) or `execution` (when Step 1.5 routes to CR).
`entry_point: review-loop`. Fresh baseline quintet from current repo
state. `plan_source` is written only after the planning loop APPROVEs
or Step 1.5 routes directly into Approved Plan / review-only; omitted
during planning draft rounds. Store snap/0 of the current verified
worktree with `python3 scripts/evidence_ledger.py snapshot --session
{uuid}` — the mandatory entry point for every `## Evidence Ledger`
write (`snapshot` / `record` / `check` / `classify` / `delta`); never
hash content or mint stages in prose.

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

Current Codex Stage 1 uses the orchestrator's current workspace only.
Executor-created hidden worktrees are forbidden in Codex Stage 1.

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
