# claude review-loop entry procedures

Read selected sections through `scripts/read_protocol.py`; shared protocol rules take precedence.

### Step 0 — Load config and parse flags


Read `.review-loop/config.md` (or defaults). Detect `--handsfree`.
Reviewer backend availability check (`which codex` for
`reviewer: codex`; suggest `reviewer: subagent` fallback if absent).

Resolve `entry` (exact values `legacy` and `paired-session` only):
- Absent: paired-session, the default entry; route it exactly as `paired-session` below.
- `legacy`: legacy, with no entry notice. Print only the one-line deprecation notice, once, before Step 0.5:
  `review-loop: legacy is deprecated since v2.12.0; the default paired-session entry covers fresh work and review of existing changes; review-pr and code-quality-loop still use legacy until they are ported; removal is planned after that`
- Any other value (quoted or differently cased included): legacy. Print `review-loop: entry "<v>" is not valid (legacy|paired-session); using legacy workflow`
- Duplicate `entry` key, or config present but unreadable: legacy. Print `review-loop: entry could not be read (<reason>); using legacy workflow`
- `paired-session`: apply the Step 1.5 entry routing before Step 0.5 creates any session file or lock.

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
worktree with `python3 scripts/evidence_ledger.py --session {uuid}
snapshot` — the mandatory entry point for every `## Evidence Ledger`
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

**Entry routing (only when `entry` resolved to `paired-session`, set or by default).** Two
states hand off, "No prior state" and "Code already implemented" (a review-only request). On
either, print `review-loop: paired-session entry (entry set in .review-loop/config.md)` when the key is set,
or `review-loop: paired-session is the default entry; set "entry: legacy" in .review-loop/config.md or use /review-loop:legacy for the legacy workflow` when it is absent,
invoke the `paired-session` skill with the work item (for code already implemented, with
`--review-only`, plus `--base <ref>` only when the user names a base), and end this workflow
(no legacy session file, lock or stage), unless the paired-session skill reports a failed
stage A check before its first `bin/paired-session` command (see below). Print the notice
and hand off only after this entry's own stage A checks (host, CLIs, background commands,
Codex home) pass; the question checks run in the paired-session skill after the handoff
(Questions row below). This routing is decided once, before
Step 0.5; once a legacy session file or lock exists, a re-detection or user
override never hands off. A paired-session probe/run HOLD is
reported to the user and never falls back to legacy. Code already implemented is detected
from task-relevant changes only: unrelated dirty work is not a code-exists signal (a
review-only run reviews and, with auto_commit, delivers every non-ignored change against its
base). Plan-exists and an existing session (explicit resume) always stay legacy; print
`review-loop: entry is paired-session but <plan exists|existing session> detected; using legacy workflow`.

**Stage A checks (before the first `bin/paired-session` command).** Read-only. The host,
CLI, background-command and Codex-home rows run here before the handoff; the Questions row
runs in the paired-session skill. With the key absent, a failed check falls back to legacy: print its notice and
continue with Step 0.5. With `entry: paired-session`, a failed check refuses: print
`review-loop: paired-session entry refused (<reason>); set "entry: legacy" or use /review-loop:legacy`
and end this workflow (except where a row says otherwise).
- Host, key absent only: `uname -s` is not `Darwin` → `review-loop: paired-session default entry needs a verified host (macOS); using legacy workflow`.
  With the key set there is no host check; in strict mode the permission probe decides, in efficient mode nothing does.
- CLIs: every CLI the resolved roles need is on PATH (`command -v`; the default roles need
  `codex` and `claude`). Missing → `review-loop: paired-session default entry needs <cli> for the <role> role; using legacy workflow`.
- Background commands: this host cannot run long background commands (or Codex cannot run
  outside its sandbox) → `review-loop: paired-session default entry unavailable (<reason>); using legacy workflow`;
  with the key set, report HOLD with the reason, as the paired-session skill does today.
- Codex home: a role is Codex and `${CODEX_HOME:-$HOME/.codex}` is not an existing directory
  → the same unavailable notice with the coordinator's CODEX_HOME reason.
- Questions: the paired-session skill asks its own stage A questions (dedicated worktree,
  test command and its shape). A declined or unanswered question is a failed check and the
  skill reports it before its first `bin/paired-session` command; with the key absent this
  workflow then prints the unavailable notice with that reason and continues with Step 0.5.
  Under `--handsfree` or `handsfree: true` nobody answers, so such a question is a failed
  check: `review-loop: paired-session default entry needs an answer (<question>) that handsfree cannot give; using legacy workflow`.
- Host setup (run directory, `WORKITEM.md`, loading the shared contract): a failure the paired-session skill reports as
  `stage A failure: <reason>` is a failed check; with the key absent print
  `review-loop: paired-session default entry unavailable (<reason>); using legacy workflow` and continue with Step 0.5.
After a handoff, legacy continues only through one of these notices: never start Step 0.5 (session file, lock,
snapshot) for a handed-off work item without first printing the fallback notice, and never fall back silently.
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
