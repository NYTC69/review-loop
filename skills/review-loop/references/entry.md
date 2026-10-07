# claude review-loop entry procedures

Read selected sections through `scripts/read_protocol.py`; shared protocol rules take precedence.

### Step 0 — Load config and parse flags


Read `.review-loop/config.md` (or defaults). Detect `--handsfree`.
Reviewer backend availability check (`which codex` for
`reviewer: codex`; suggest `reviewer: subagent` fallback if absent).

Resolve `entry` (exact values `legacy` and `paired-session` only; the legacy workflow was removed in v2.13.0):
- Absent: paired-session, the default entry; route it exactly as `paired-session` below.
- `legacy`: refused. Print `review-loop: the legacy workflow was removed in v2.13.0; remove "entry: legacy" from .review-loop/config.md (paired-session is the only entry)` and end.
- Any other value (quoted or differently cased included): print `review-loop: entry "<v>" is not valid (paired-session is the only entry); using paired-session` and route it as `paired-session`.
- Duplicate `entry` key, or config present but unreadable: print `review-loop: entry could not be read (<reason>); using paired-session` and route it as `paired-session`.
- `paired-session`: apply the Step 1.5 entry routing before Step 0.5 creates any session file or lock.

Steps 0.5, 1 and 1.6 and the auto-routing bullets of Step 1.5 are the legacy workflow: only `/review-loop:legacy` (which
ignores `entry`; deleted in v2.13.1) runs them. Every `/review-loop` request goes from Step 0 straight to the Step 1.5
"Entry routing" and "Stage A checks" paragraphs and hands off from there.

### Step 0.5 — Initialize session file

Legacy only (`/review-loop:legacy`).

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
worktree with `python3 scripts/evidence_ledger.py --session {uuid}
snapshot` — the mandatory entry point for every `## Evidence Ledger`
write (`snapshot` / `record` / `check` / `classify` / `delta`); never
hash content or mint stages in prose.

Acquire the single-writer lock per
`docs/protocol/session-file.md` §Lock file lifecycle. Print the
session file path.

### Step 1 — Parse the work item

Legacy only (`/review-loop:legacy`); `/review-loop` passes the work item to paired-session as written.

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

**Entry routing (every `/review-loop` request; the legacy workflow was removed in v2.13.0).** Decided before Step 0.5 creates any
session file or lock; the auto-routing bullets above describe the legacy workflow and apply only under `/review-loop:legacy`.
- No prior state: hand off with the work item.
- Code already implemented: hand off as a review-only request (for code already implemented, with
`--review-only`, plus `--base <ref>` only when the user names a base). Code already implemented is detected
from task-relevant changes only: unrelated dirty work is not a code-exists signal (a
review-only run reviews and, with auto_commit, delivers every non-ignored change against its
base).
- Plan already exists: print `review-loop: an existing plan is used as the work item; paired-session drafts and reviews the plan again` and hand off with the plan text in the work item.
- Existing session (an explicit resume of a `.review-loop/sessions/` session): refused. Print
  `review-loop: the legacy workflow was removed in v2.13.0; legacy sessions cannot be resumed: start a new run with the session's plan or work item` and end.
To hand off, print `review-loop: paired-session entry (entry set in .review-loop/config.md)` when the key is set, or
`review-loop: paired-session entry (the default entry)` when it is absent, invoke the `paired-session` skill, and end
this workflow (no legacy session file, lock or stage). A paired-session probe/run HOLD is
reported to the user and never falls back to legacy.

**Stage A checks (before the first `bin/paired-session` command).** Read-only, the same whether the key is set or
absent; nothing falls back (there is no legacy route). A failed check refuses: print
`review-loop: paired-session entry refused (<reason>)` and end this workflow (except where a row says otherwise).
- Host: `uname -s` is not `Darwin` → `review-loop: paired-session needs macOS; Linux and other hosts are not supported` (no reason wrapper).
- CLIs: every CLI the resolved roles need is on PATH (`command -v`; the default roles need
  `codex` and `claude`); a missing one is refused with the reason `needs <cli> for the <role> role`.
- Background commands: this host cannot run long background commands (or Codex cannot run
  outside its sandbox) → report HOLD with the reason, as the paired-session skill does.
- Codex home: a role is Codex and `${CODEX_HOME:-$HOME/.codex}` is not an existing directory
  → refused with the coordinator's CODEX_HOME reason.
- Questions and host setup: the paired-session skill asks its own stage A questions (dedicated worktree,
  test command and its shape; under `--handsfree` or `handsfree: true` nobody answers, so any such question
  fails) and sets up the run (run directory, `WORKITEM.md`, the shared contract); it reports a failure as
  `stage A failure: <reason>`, which this entry prints as the refusal above.
From the first `bin/paired-session` command on, every refusal or HOLD is reported verbatim and never falls back to legacy.

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

Legacy only (`/review-loop:legacy`).

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
