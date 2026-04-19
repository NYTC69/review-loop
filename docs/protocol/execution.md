# Protocol — Execution Phase

The execution phase drives an approved plan (or a user-supplied plan, or a
review-only target) through an iterative Executor + Reviewer CR loop, then
through quality polish, documentation consistency, and security preflight,
and finally to delivery.

This document is runtime-agnostic. Runtime-specific dispatch is marked with
`{{claude_code|codex}}` placeholder blocks. Codex Stage 1 has a reduced
scope (no polish / docs / security / non-delivery `--stop-after` stages);
all reductions are called out inline.

For session-file schema, lock, moving baseline, dirty map, drift check,
`completed_stages` lifecycle, `delivery_blocked_by` lifecycle, and
backward-compat fallback, see [session-file.md](./session-file.md). For
output schemas, see [executor-output.md](./executor-output.md) and
[reviewer-output.md](./reviewer-output.md).

---

## Phase entry conditions

The execution loop runs when:

- `execute --session <uuid>` resumes a session whose `## Current Phase` is
  `execution` (or whose `## Approved Plan` is populated).
- `execute --plan <text|path>` — fresh session with the user text injected,
  `plan_source: user-supplied`.
- `execute --review-only` — fresh session with the sentinel Approved Plan,
  `plan_source: review-only`.
- The umbrella `review-loop` skill has just exited a successful planning
  loop.

At phase entry the orchestrator sets `## Current Phase: execution` and
initializes loop state:

```
loop_state.phase = "execution"
loop_state.round = 0
```

Wall-clock timing, `loop_state.findings`, and the context-persist sub-step
follow the same pattern documented in
[planning.md §Round loop](./planning.md#round-loop). This document focuses
on what is different in execution.

---

## `--stop-after` enum

`--stop-after <stage>` controls where the orchestrator performs a clean
shutdown instead of proceeding to the next stage. Lock release and
baseline update on a clean `--stop-after` exit follow the normal rules in
[session-file.md §Drift-check decision tree](./session-file.md#drift-check-decision-tree)
(step 5) and [§Lock file lifecycle](./session-file.md#lock-file-lifecycle).

### Legal values (full set)

| Value | Semantics |
|---|---|
| `exec-round` | Exit after the current execution round finishes (even if reviewer returned REQUEST_CHANGES). Useful for batch-sized iteration. |
| `before-polish` | Exit after the execution loop APPROVEs and before Step 3.5 starts. |
| `before-docs` | Exit after Step 3.5 and before Step 3.6. |
| `before-security` | Exit after Step 3.6 and before Step 3.7. |
| `before-delivery` | Exit after Step 3.7 and before Step 4. |
| `delivery` | Default. No early stop; run through delivery. |

### Runtime-supported subsets

- **Claude Code**: full set (all six values).
- **Codex Stage 1**: `exec-round`, `before-delivery`, `delivery` only.
  Steps 3.5 / 3.6 / 3.7 are out of scope, so `before-polish`,
  `before-docs`, and `before-security` are invalid on Codex.

### Parse-time validation

Unsupported-on-runtime values are **rejected at Step 0 flag parsing**,
before any lock is acquired or any session field is written. The
orchestrator must not modify the session file (or create the lock) before
validating `--stop-after`. Error message must name the runtime and list the
supported subset.

---

## Step 3 — Execution round loop

The execution round loop mirrors the planning round loop (see
[planning.md §Round loop](./planning.md#round-loop)) with three differences:

1. The Executor is invoked in **execution mode** — it produces code changes,
   not plans.
2. The Reviewer prompt is provenance-aware. See
   [§Provenance-aware reviewer prompts](#provenance-aware-reviewer-prompts).
3. `--review-only` skips the **first** Executor call. See
   [§`--review-only` first-round skip](#review-only-first-round-skip).

### Round steps

1. **Update context file** before calling agents:
   - `## Current Phase: execution`
   - `## Approved Plan` — populated (see
     [session-file.md §`## Approved Plan` — three Sources](./session-file.md#approved-plan--three-sources)).
   - `## Files Changed`, `## Key Related Files` — refreshed after each
     Executor round.
   - `## Review History` — append-only accumulation.
2. **Call the Executor** with the execution-mode prompt:

   ```
   You are the Executor in a review-loop workflow.

   {contents of agents/executor.md body — the system prompt}

   Read the context file first: {session_file_path}
   DO NOT modify the context file — return your output as described in
   the output format above.

   ## Your Task
   Implement the approved plan (see context file). Make all necessary code
   changes. Follow the execution mode output format in your instructions.

   When done, list all files you modified/created so the Orchestrator can
   update the context file for the Reviewer.

   {if round > 1:}
   ## Code Review Feedback (address each point)
   {reviewer_cr_feedback — passed directly for immediacy}
   ```

   Executor dispatch — see
   [planning.md §Executor dispatch](./planning.md#executor-dispatch-claude_codecodex).

3. **Update context file with the Executor's output** before calling the
   Reviewer. Write the Executor's change summary, updated file list, and
   any deviations from the plan. Update `## Files Changed` from the actual
   post-Executor state (see the no-op validation rules below).
4. **Optional context-persist sub-step** — same as
   [planning.md §3.5](./planning.md#35-optional-context-persist-sub-step).
5. **Call the Reviewer** with the execution-mode review content template:

   ```
   Read the context file first: {session_file_path}
   DO NOT modify the context file.
   It contains the problem description, approved plan, review history,
   changed files, and related files.

   ## Changes Made (summary from Executor)
   {executor_change_summary}

   ## Your Task
   {see §Provenance-aware reviewer prompts below — pick the block for
    the active plan_source}

   {if review_focus is set:}
   ## Project-Specific Review Priorities
   In addition to the standard review checklist, pay special attention to:
   {review_focus}

   {if round > 1:}
   The context above contains your previous findings. Verify that previously
   flagged CRITICAL issues are actually resolved in code — read the actual
   code, don't just take the Executor's word for it. Also check whether
   fixes introduced regressions or new issues.

   You have read-only access to the project files — use it.

   {if review_style is set:}
   ## Review Style
   {review_style}

   Return your structured verdict following the output format in your
   instructions above.
   ```

   > `review_style` and `review_focus` apply to **all** rounds including
   > round 1, and are NOT inside the `{if round > 1}` branch.

   Reviewer dispatch — see
   [planning.md §Reviewer dispatch](./planning.md#reviewer-dispatch-claude_codecodex).

6. **Parse, update loop state, display Live Report** — same as planning.
7. **Loop control** — APPROVE exits Step 3 and enters the downstream stages
   (Step 3.5 on Claude Code, Step 4 on Codex Stage 1). `REQUEST_CHANGES`
   feeds feedback to the next round. Soft limit + stuck detection per the
   caps below.

### No-op execution round validation

When the Executor reports a no-op round:

- `### Changes Made` states that no code changes were required.
- `### Files Modified / Created / Deleted` is `None`.
- `### Notes for Reviewer` identifies the round as a no-op.

The orchestrator compares the Executor's claimed file list against the
**current-round delta** attributable to that round (pre-round vs post-round
actual state). Same path sets alone do not prove a no-op. Reject outputs
that claim file changes not supported by the current-round delta, even if
the file was already dirty before the round started.

Changed file set definition:

- tracked changes: `git diff --name-only HEAD`
- untracked: `git ls-files --others --exclude-standard`
- post-Executor set: union of those two lists
- deleted tracked files remain part of the tracked-changes source of truth

### Provenance-aware reviewer prompts

The reviewer prompt varies by `## Session Metadata.plan_source`. The
orchestrator picks the block that matches the active value.

#### `plan_source: reviewer-approved`

```
## Your Task
Review the code changes against the approved plan in the context file.

Check both **correctness** (does the code work?) AND **plan conformance**
(does the code match the plan's design decisions?). If the Executor
deviated from the plan — introduced new thresholds, relaxed constraints,
changed the agreed approach — flag it as CRITICAL even if the code is
technically correct.
```

Strict plan-conformance applies.

#### `plan_source: user-supplied`

```
## Your Task
Review the code changes against the user-supplied plan in the context
file.

Enforce correctness and intent alignment strictly. The plan body was
provided by the user verbatim and was NOT produced by a planning loop,
so plan-conformance deviations are advisory: treat them as [MINOR]
unless the deviation changes user-visible behavior or violates stated
acceptance criteria.

(plan_source: user-supplied — plan conformance is advisory/MINOR)
```

Plan-conformance deviations become MINOR/advisory; correctness + intent
alignment still enforced. The final parenthetical line is a **stable
sentinel** emitted verbatim in the reviewer prompt so tests and audits
can assert that this provenance-aware block (and only this block) was
selected. The exact literal is `(plan_source: user-supplied — plan
conformance is advisory/MINOR)`; orchestrators must emit it
character-for-character when `plan_source: user-supplied`.

#### `plan_source: review-only`

```
## Your Task
This is a pure code-review pass. The `## Approved Plan` section in the
context file holds the canonical review-only sentinel (see
[session-file.md §Canonical sentinel for `review-only`](./session-file.md#canonical-sentinel-for-review-only)
for the exact literal) — do not treat its contents as a plan and do not
check plan conformance.

Review the diff in `## Files Changed` (and the scope described in
`## Review Target`) for correctness, quality, edge cases, security, and
test coverage.
```

Pure CR mode. No plan-conformance language. The reviewer is explicitly
told the Approved Plan body is the canonical sentinel defined in
[session-file.md §Canonical sentinel for `review-only`](./session-file.md#canonical-sentinel-for-review-only).

### review-only first-round skip

When `plan_source: review-only`, the orchestrator **skips the first
Executor call**:

1. Round 1: jump straight to the Reviewer. The review content points at the
   existing diff + `## Review Target` scope. No Executor output is produced.
2. If the Reviewer returns APPROVE, mint `exec` into `completed_stages`
   (per [session-file.md §`completed_stages` lifecycle](./session-file.md#completed_stages-lifecycle))
   and proceed downstream. This is the only code path where `exec` is added
   without the Executor running.
3. If the Reviewer returns REQUEST_CHANGES, round 2+ follows the standard
   CR → fix loop: Executor runs with the reviewer feedback to fix the code;
   subsequent rounds alternate Executor + Reviewer normally.

---

## Step 3.5 — Quality Polish (Claude Code only)

> **Codex Stage 1 excludes Step 3.5 entirely.** On Codex, after Step 3
> APPROVEs, the orchestrator jumps straight to Step 4 delivery gate check.

> **Skip condition** (Claude Code): if `skip_quality_polish: true` in
> `.review-loop/config.md`, skip all of Step 3.5 and proceed to Step 3.6.

Step 3.5 is supplementary polish, not a replacement for the adversarial CR
loop. It runs language-specific static analysis, generic code review,
simplification, and test consolidation, re-running the Executor on any
CRITICALs it surfaces.

When any substep writes code (executor-fix, simplify, test consolidation),
`completed_stages` is cleared entirely and the orchestrator replays from
`exec` per [session-file.md §`completed_stages` lifecycle](./session-file.md#completed_stages-lifecycle).

### 3.5.1 — Language detection

Detect languages in changed files (tracked and untracked):

```bash
{ git diff --name-only --diff-filter=d HEAD; git ls-files --others --exclude-standard; } \
  | grep '\.' | sed 's/.*\.\([^.]*\)$/\1/' | sort -u
```

Map extensions to agents:

- `.go` → `go-reviewer`
- `.rs` → `rust-reviewer`
- `.py` → `python-reviewer`
- `.ts` / `.tsx` / `.js` / `.jsx` / `.html` / `.vue` / `.svelte` →
  `frontend-security-reviewer`
- Multiple languages → run all applicable agents.

### 3.5.2 — Language-specific static analysis

For each detected language, invoke the corresponding agent.

#### Dispatch {{claude_code|codex}}

{{claude_code}}

Use the Agent tool with `subagent_type: general-purpose`. Plugin-defined
agent types (e.g. `review-loop:<name>`) have their tools silently blocked
by the Claude Code sandbox — always inline the full body of
`agents/<agent-name>.md` in the `prompt` parameter.

```
Agent prompt:
  {contents of agents/<agent-name>.md body}

  IMPORTANT: Use Claude Code's native Bash tool to run shell commands.
  Do NOT use MCP server tools (e.g. run_bash_command).

  ## Changed Files
  {list from git diff --name-only --diff-filter=d HEAD}

  Run analysis on the changed files listed above. Context file:
  {session_file_path}

  {if quality_focus is set:}
  ## Quality Focus
  {quality_focus}

  {if review_style is set:}
  ## Review Style
  {review_style}

  Report only, do not modify files.
```

{{codex}}

N/A — Stage 1 scope excludes Quality Polish.

**Hallucination guard**: after each agent returns, check
`tool_uses` in metadata. If `tool_uses: 0`, the agent did not actually read
files or run commands — its output is fabricated. Discard and retry once.
If the retry is also `tool_uses: 0`, skip this agent and report the
failure.

Display findings. If CRITICAL / HIGH issues are found, invoke the Executor
via the same Executor dispatch used by Step 3 (see
[planning.md §Executor dispatch](./planning.md#executor-dispatch-claude_codecodex))
to fix, then re-run the language agent.

**Max 2 fix rounds.** If issues remain after 2 rounds, report them and
continue to 3.5.3.

### 3.5.3 — Code quality review-fix loop

Invoke `code-reviewer` and `silent-failure-hunter` on the changed code.
Report-only; they do not modify files.

```
Agent prompt:
  {contents of agents/code-reviewer.md body}  # or silent-failure-hunter.md

  Review the changed files listed in context file: {session_file_path}
  Report only, do not modify files.

  {if quality_focus is set:}
  ## Quality Focus
  {quality_focus}

  {if review_style is set:}
  ## Review Style
  {review_style}
```

- Parse findings, triage by severity.
- If CRITICAL / HIGH issues are found, **do not stop the loop**. Triage each
  issue:
  - **Can fix autonomously** (clear implementation fix — input sanitization,
    missing auth check, obvious logic error): invoke Executor immediately to
    fix, then re-run the review agent to verify. Do not ask the user first.
  - **Requires design decision** (architecture change, security trade-off,
    ambiguous requirement): surface to the user and wait. Apply the
    decision and continue.
  - Never batch both categories and stop — fix what can be fixed while
    asking about what cannot. Both paths must complete before moving on.
- **Max 3 rounds** or until clean.
- **Stuck detection**: the same issue persisting 3 rounds = stop.

### 3.5.4 — Simplify

Invoke `code-simplifier` (it needs Write/Edit tools).

#### Dispatch {{claude_code|codex}}

{{claude_code}}

Use the Agent tool with `subagent_type: general-purpose`. Plugin agent
type `review-loop:code-simplifier` has tools silently blocked — do not
use it.

```
Agent prompt:
  {contents of agents/code-simplifier.md body}
  Simplify the recently changed files: {file_list from context file}

  {if quality_focus is set:}
  ## Quality Focus
  {quality_focus}

  {if review_style is set:}
  ## Review Style
  {review_style}
```

{{codex}}

N/A — Stage 1 scope excludes Quality Polish.

**Single pass** — not looped. If simplify makes changes, run a quick build
check to ensure nothing broke. If build fails, revert the simplify changes
and report to the user.

### 3.5.5 — Test consolidation

Invoke `pr-test-analyzer`.

#### Dispatch {{claude_code|codex}}

{{claude_code}}

Use the Agent tool with `subagent_type: general-purpose`. Plugin agent
types are off-limits (sandbox bug) — always inline the full body of
`agents/pr-test-analyzer.md` in the `prompt` parameter.

```
Agent prompt:
  {contents of agents/pr-test-analyzer.md body}

  Analyze test coverage for the changed files. Context file:
  {session_file_path}

  {if quality_focus is set:}
  ## Quality Focus
  {quality_focus}

  {if review_style is set:}
  ## Review Style
  {review_style}
```

{{codex}}

N/A — Stage 1 scope excludes Quality Polish.

If gaps found, invoke the Executor via the same Executor dispatch used by
Step 3 (see
[planning.md §Executor dispatch](./planning.md#executor-dispatch-claude_codecodex))
to add missing tests. Then run the language-appropriate build/test
command (e.g. `go test ./...`, `cargo test`, `pytest`).

**Max 2 fix rounds** for test failures. If still failing after 2 rounds,
report remaining failures to the user and proceed to Step 3.6.

### 3.5.6 — Quality Polish summary

```
── review-loop: Quality Polish ─────────────────────
Static analysis: {go-reviewer: PASS / rust-reviewer: 2 fixed / ...}
Code quality:    {3 rounds, 5 fixed, 0 remaining}
Simplify:        {4 improvements applied}
Tests:           {PASS (12 tests) / 2 added, 1 updated}
────────────────────────────────────────────────────
```

Update the session file with Quality Polish results and timing. On a clean
finish with no writes at all in this Step 3.5 invocation, mint `polish` into
`completed_stages`.

---

## Step 3.6 — Documentation Consistency (Claude Code only)

> Codex Stage 1 excludes Step 3.6.

**Single pass** — not looped.

### 3.6.1 — Update project documentation (if any exists)

Search the project for design docs, architecture docs, ADRs, runbooks,
memory files (`.claude/memory/`, `tasks/lessons.md`), changelogs, wikis.
For each doc found: read it, compare against the code changes in the
session file. If the doc describes behavior, APIs, or logic that has
changed, update it to reflect the new implementation. Focus on business
logic accuracy; do not rewrite style.

If no docs are found, note "no project docs found" and proceed to 3.6.2.

### 3.6.2 — Code comment consistency (always run)

For each changed file, verify:

- Function / method docstrings and comments match the actual implementation.
- Type / struct comments match actual fields and behavior.
- Module-level comments match actual responsibilities.
- Inline comments explain current logic (not stale from a previous version).

Fix stale / incorrect comments directly using Edit.

### Output

```
── review-loop: Doc Consistency ─────────────────────
Project docs:   {updated: X files / none found}
Comments fixed: {N} stale comments in {files / "none"}
─────────────────────────────────────────────────────
```

If Step 3.6 performs any writes (doc or comment fixes), `completed_stages`
is cleared and replay restarts from `exec`. On a no-write completion, mint
`docs` — **then proceed to Step 3.7**. A no-op docs stage is not a
terminal state; Step 3.7 still has to run.

---

## Step 3.7 — Security Preflight (Claude Code only)

> Codex Stage 1 excludes Step 3.7.

**Single scan** — runs on every delivery regardless of `auto_commit`.
Step 3.7 runs **unconditionally** after Step 3.6 on every Claude Code
invocation that reaches this point, regardless of whether any prior
stage wrote files. A no-op session (zero code changes, zero doc
updates) still runs this scan — it is a security gate, not a
content-dependent step. The only exits before 3.7 are
`--stop-after before-security` / `before-docs` / `before-polish` /
`exec-round`.

### 3.7.1 — Check for tracked or staged sensitive files

Run each of these via Bash and collect every match into a flagged-files
list:

```bash
# Keys & certificates
git ls-files | grep -iE '\.(pem|key|crt|cert|cer|p12|pfx|jks|keystore|ppk|asc|gpg|pgp)$'

# Environment & config secrets (exclude safe .example/.sample templates)
git ls-files | grep -iE '(^|/)(\.env|\.env\..+)$' | grep -v -iE '\.(example|sample)(\.[^/]*)?$'
git ls-files | grep -iE '\.(env)$'

# Credential / secret basenames (exclude .example/.sample)
git ls-files | grep -iE '(^|/)[^/]*(credentials?|secrets?|api[-_.]?key|auth[-_.]?token|passwd|shadow)[^/]*$' \
  | grep -v -iE '\.(example|sample)(\.[^/]*)?$'

# SSH private keys
git ls-files | grep -iE '(^|/)id_(rsa|dsa|ecdsa|ed25519)'

# Cloud service account credentials
git ls-files | grep -iE '(^|/)service-account[^/]*\.json$'

# Cloud credential directories
git ls-files | grep -iE '(^|/)\.(aws|gcloud)/'

# Database dumps / files
git ls-files | grep -iE '\.(sqlite3?|db|dump|sql\.gz)$'

# Terraform state (exclude .example templates)
git ls-files | grep -iE '(\.tfstate|\.tfvars)($|\.)' | grep -v -iE '\.example$'

# Terraform plugin/module cache directory
git ls-files | grep -E '(^|/)\.terraform/'

# Source maps (all variants)
git ls-files | grep -iE '\.map$'

# Log files
git ls-files | grep -iE '\.log$'
git ls-files | grep -E '(^|/)logs/'
```

If flagged files are found, report each as **CRITICAL** and halt. Tell the
user to untrack each file with `git rm --cached <file>` and add the
appropriate pattern to `.gitignore`. Do not proceed.

### 3.7.2 — Audit `.gitignore` for missing sensitive pattern coverage

Read `.gitignore` (create if missing). For each category below, check
whether adequate glob coverage already exists. If not, add its patterns.

| Category | Patterns to add if missing |
|----------|---------------------------|
| Environment & config | `.env`, `.env.*`, `*.env`, `!.env.example`, `!.env.sample` |
| Keys & certificates | `*.pem`, `*.key`, `*.crt`, `*.cert`, `*.cer`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore` |
| SSH key files | `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*`, `*.ppk` |
| PGP / GPG | `*.asc`, `*.gpg`, `*.pgp` |
| Cloud credentials ⚠️ always confirm | `*credentials*`, `!*credentials.example*`, `!*credentials.sample*`, `service-account*.json`, `.aws/`, `.gcloud/` |
| Generic secret files ⚠️ always confirm | `*secret*`, `!*secret.example*`, `!*secret.sample*`, `secrets.*`, `!secrets.example*`, `!secrets.sample*` |
| Database & dumps | `*.sqlite`, `*.sqlite3`, `*.db`, `*.dump`, `*.sql.gz` |
| Compiled source maps | `*.map` (intentionally broad — JS, CSS, all variants) |
| Terraform | `*.tfstate`, `*.tfstate.*`, `*.tfvars`, `!*.tfvars.example`, `.terraform/` |
| Logs | `*.log`, `logs/` |

Before writing any pattern:

- **Wildcard patterns** (`*.pem`, `id_rsa*`, `*.tfstate`): probe with
  `git ls-files -- '<glob>'` (git's native pathspec matches at any depth).
- **Literal / directory patterns** (`.env`, `.aws/`, `.gcloud/`,
  `.terraform/`, `logs/`): probe with anchored grep
  (e.g. `git ls-files | grep -E '(^|/)\.env$'`).
- Categories marked ⚠️ always confirm: **always** ask the user before
  adding, regardless of tracked-files match. Globs are broad enough to hit
  source code.
- All other patterns: if tracked files are found → warn and confirm; if
  none → add silently.

Use Edit to append missing patterns, grouped by category with a comment
header (e.g. `# Keys & certificates`).

### Output

```
── review-loop: Security Preflight ─────────────────
Tracked sensitive files: {NONE | CRITICAL: <file1>, <file2>, ...}
.gitignore additions:    {N patterns added across M categories | already covered}
Status: {✓ CLEAN — ready to commit | ✗ BLOCKED — N sensitive files must be removed from tracking}
─────────────────────────────────────────────────────
```

If BLOCKED: halt. Do not proceed to Step 4 until resolved.

If Step 3.7 writes `.gitignore` or causes `git rm --cached`,
`completed_stages` is cleared and replay restarts from `exec`. On a
no-write completion, mint `security`.

---

## Delivery gate

Step 4 is gated by:

```
runtime_supported_set ⊆ completed_stages
```

- Claude Code: `{exec, polish, docs, security} ⊆ completed_stages`.
- Codex Stage 1: `{exec} ⊆ completed_stages`.

Because invalidation + replay guarantee that set entries only exist when
they are valid for the current state, no separate final reviewer pass is
required — the gate is structural.

On gate failure, the orchestrator **hard-stops**, sets
`delivery_blocked_by ← <stage>` where `<stage>` is the first missing
member of the runtime set, and exits without delivering. See
[session-file.md §`delivery_blocked_by` lifecycle](./session-file.md#delivery_blocked_by-lifecycle)
for the full lifecycle (including resume-from-non-null behavior).

---

## Step 4 — Delivery

After the gate passes:

1. **If `auto_commit: true`**: stage only the files reported as changed by
   the Executor (never `git add -A` / `git add .`), then commit with:
   `{commit_message_prefix}: {title}`. Append the resulting sha to
   `session_commits` in `## Session Metadata`.
2. **Display the Delivery Summary** to the user (see summary template in
   the runtime's SKILL.md — runtime-specific formatting, but always
   includes: status, reviewer backend, rounds, quality-polish summary,
   review findings table, files changed, autonomous decisions if any,
   unresolved minor issues if any, time breakdown, token usage, suggested
   next steps).
3. **If `docs_file` is set**: append the delivery summary (without the box
   borders) to that file.
4. **Cleanup temp files**: delete round output files for this session, e.g.
   `rm -f .review-loop/sessions/{session_id}-round-*.txt` (Claude Code),
   `rm -f .review-loop/tmp/{session_id}-reviewer-prompt.txt` (Codex —
   already deleted per-round). The session file itself is preserved as a
   permanent audit record.
5. **Clear `delivery_blocked_by`** → `null`. **Release the lock.**

---

## Per-stage max-round caps

Caps are explicit to guarantee termination of the replay cycle documented
in [session-file.md §`completed_stages` lifecycle](./session-file.md#completed_stages-lifecycle).

| Stage | Cap |
|---|---|
| Step 3 exec loop | `soft_limit_exec` (default 3). On cap with CRITICALs remaining → ask the user; handsfree → auto hard-stop. |
| Step 3.5.2 static analysis fix | 2 rounds. |
| Step 3.5.3 code-reviewer / silent-failure-hunter | 3 rounds. |
| Step 3.5.4 simplify | single pass (not looped). |
| Step 3.5.5 test consolidation fix | 2 rounds. |
| Step 3.6 docs | single pass. |
| Step 3.7 security | single scan. |

When any stage hits its cap with unresolved findings, the stage is NOT
added to `completed_stages` (invariant: "only clean passes in the set").
The orchestrator prints a stuck summary, updates the baseline to current
state, sets `delivery_blocked_by ← <stage>`, and exits without delivering.

---

## Cross-references

- Session file schema + canonical sections, lock, moving baseline, dirty
  map, drift check, `completed_stages` + `delivery_blocked_by` lifecycles,
  `--accept-external-state`, backward-compat fallback:
  [session-file.md](./session-file.md).
- Planning phase round loop, dispatch templates, question classification,
  context-persist sub-step: [planning.md](./planning.md).
- Executor output schema + rejection rules:
  [executor-output.md](./executor-output.md).
- Reviewer output schema + validation rules:
  [reviewer-output.md](./reviewer-output.md).
