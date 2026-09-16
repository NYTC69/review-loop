---
name: execute
argument-hint: "<--session <uuid> | --plan <text|path> --title <title> | --review-only> [--stop-after <stage>] [--handsfree] [--accept-external-state]"
description: >
  Run the execution + quality polish + delivery stages of review-loop
  against one of three entry modes: resume an approved session
  (`--session`), execute a user-supplied plan (`--plan`), or run a
  pure-CR pass on the current working tree (`--review-only`). Supports
  batched runs via `--stop-after <stage>`. Use when you already have a
  plan, or only want CR on existing code.
---

# execute — Execution + Polish + Delivery Sub-Skill

Drive an approved / user-supplied / review-only plan through the
execution loop, quality polish (Step 3.5), docs consistency (3.6),
security preflight (3.7), and delivery (Step 4). Supports multi-batch
runs via `--stop-after <stage>` and the unsafe `--accept-external-state`
opt-in.

## Protocol Imports

The Orchestrator MUST Read each of these files at start. They are the
single source of truth for this skill's execution loop and output
schemas.

- `docs/protocol/session-file.md`
- `docs/protocol/execution.md`
- `docs/protocol/executor-output.md`
- `docs/protocol/reviewer-output.md`

Do not re-derive any rule that already lives in a protocol doc. When a
step below says "see `docs/protocol/<doc>.md` §Foo", follow that doc
verbatim.
The startup read set is complete only after all 4 docs above have been read
explicitly; the embedded executor/reviewer prompt bodies are not a substitute
for reading `executor-output.md` and `reviewer-output.md`.

## Orchestrator rules

- **Plugin agent-type sandbox bug**: every Executor / Reviewer / quality
  agent invocation MUST use `subagent_type: general-purpose` with the
  agent's full `.md` body inlined in the `prompt` parameter. Never use
  `subagent_type: review-loop:<name>`. See `CLAUDE.md` §"Plugin agent
  type sandbox bug".
- **Unsupported `--stop-after` values are rejected at parse time**,
  before any lock is acquired or session field is written. The
  Orchestrator must not modify the session file or create the lock
  until flag parsing has succeeded.
- Only the Orchestrator writes to the session file. Sub-agents read.
- Live Reports after each round are not optional.

## Invocation — three mutually-exclusive entry modes

Exactly one of the three entry flags must be supplied. Supplying more
than one is a parse error; the Orchestrator exits without touching the
session file or the lock.

```
# Entry mode 1 — resume an approved session
run execute: --session <uuid> [--stop-after <stage>] [--handsfree] [--accept-external-state]

# Entry mode 2 — execute a user-supplied plan verbatim
run execute: --plan <text|path> --title <...> [--description <...>] [--stop-after <stage>] [--handsfree] [--accept-external-state]

# Entry mode 3 — pure code-review over the current working tree
run execute: --review-only [--description <what was done>] [--stop-after <stage>] [--handsfree] [--accept-external-state]
```

---

## Step 0 — Parse and validate flags

Before parsing flags or touching session state, Read the 4 Protocol Imports
docs listed above.

Execute before any lock or session write.

1. **Entry-mode mutual exclusion**: count how many of `--session`,
   `--plan`, `--review-only` are present. If ≠ 1 → print usage and
   exit with non-zero.
2. **`--stop-after <stage>`**: validate against the Claude Code full
   supported set (per `docs/protocol/execution.md`
   §Runtime-supported subsets):

   - `exec-round`
   - `before-polish`
   - `before-docs`
   - `before-security`
   - `before-delivery`
   - `delivery` (default when flag is absent)

   Any other value → reject at parse time. Error message must list the
   supported subset. Do **not** create the lock, do **not** touch the
   session file.

3. **`--handsfree`**: enable handsfree mode for this invocation.
   Handsfree alone does NOT auto-accept drift — see `--accept-external-state`.
4. **`--accept-external-state`**: **unsafe opt-in**. Auto-selects "(A)
   accept" wherever `docs/protocol/session-file.md` instructs the
   Orchestrator to pause-and-confirm (drift check step 4; backward-compat
   missing-baseline fallback). Documented as unsafe: the user is opting
   out of pausing on external tree drift. The flag has no effect outside
   those two prompts — it does not bypass unmerged-conflict errors,
   unknown-git-state errors, or per-stage hard-stops.
5. **Config load**: read `.review-loop/config.md` if present; otherwise
   use defaults from `skills/review-loop/SKILL.md` §Configuration.
6. **Reviewer backend availability**: same probe as `plan` skill
   (`which codex` for `reviewer: codex`).

## Step 0.5 — Resolve target UUID (no writes yet)

Compute the session UUID so the lock path is known; **do not read or
write the session file yet** — the single-writer lock must come first
per `docs/protocol/session-file.md` §Lock file lifecycle.

- `--session <uuid>`: adopt the UUID the user supplied. Confirm the
  path is well-formed (`.review-loop/sessions/{uuid}.md`). Do not Read
  the file content yet.
- `--plan <text|path>`: generate a fresh lowercase UUID.
- `--review-only`: generate a fresh lowercase UUID.

Flag parsing (Step 0) and `--stop-after` validation have already
completed; those steps are intentionally pre-lock per the `--stop-after`
design-spec requirement and `docs/protocol/execution.md`
§Parse-time validation.

## Step 1 — Acquire the single-writer lock

Per `docs/protocol/session-file.md` §Lock file lifecycle. **Every
subsequent read and write of the session file (creation, resume-time
re-baselining, round updates) must happen under this lock.** Summary:

- `.review-loop/sessions/{uuid}.lock` — PID, ISO-8601 `started_at`,
  `entry_point`, `stop_after`.
- No lock → proceed. Lock present + PID alive → refuse. Lock present +
  PID dead → prompt-to-recover.
- Release on every clean exit path (delivery, `--stop-after` stop,
  signal abort trap, unrecoverable error trap).

## Step 1.5 — Initialize or resume the session (under the lock)

Per the entry-mode initialization table in
`docs/protocol/session-file.md` §Entry-mode initialization table. All
reads and writes below happen **after** Step 1 acquired the lock.

`entry_point` is set once on session creation per
`docs/protocol/session-file.md` §Session Metadata schema; `--session`
resumes preserve the original value.

### Mode: `--session <uuid>`

1. Read `.review-loop/sessions/{uuid}.md`. If the file is missing →
   release the lock and exit with an error.
2. Preserve all existing canonical content.
3. Respect backward-compat fallback: if any baseline quintet field is
   missing, pause-and-prompt per
   `docs/protocol/session-file.md` §Backward-compat fallback.
   `--accept-external-state` auto-picks (A). Handsfree alone blocks.
   The `entry_point` backfill rule for legacy sessions also lives in
   `docs/protocol/session-file.md` §Session Metadata schema; this
   skill defers to the protocol doc rather than re-stating it here.

### Mode: `--plan <text|path>`

1. Create `.review-loop/sessions/{uuid}.md` with the `--plan` column of
   the init table:
   - `## Approved Plan` → `- Source: user-supplied` followed by the
     user's free-form plan text injected verbatim. If `--plan` was a
     path, read the file and inject its contents; if it was inline
     text, inject directly.
   - `## Current Phase: execution`.
   - `## Context` = `"User-supplied plan; no planning-phase context
     captured."` + any `--description`.
   - `## Acceptance Criteria` = `"Implementation matches the
     user-supplied plan in ## Approved Plan."`.
2. Write `## Session Metadata`:
   - `entry_point: execute-from-plan`
   - `plan_source: user-supplied`
   - Fresh baseline quintet (`base_head`, `base_dirty`, etc.) from
     current repo state.
3. This mode drives **provenance-aware reviewer behavior**: during
   execution rounds the reviewer's plan-conformance deviations are
   advisory / MINOR per `docs/protocol/execution.md` §Provenance-aware
   reviewer prompts, `plan_source: user-supplied` block. Correctness +
   intent-alignment are still enforced strictly.

### Mode: `--review-only`

1. Create `.review-loop/sessions/{uuid}.md` with the `--review-only`
   column of the init table:
   - `## Approved Plan` → `- Source: review-only` followed by the
     **two-line canonical sentinel** exactly as documented in
     `docs/protocol/session-file.md` §Canonical sentinel for
     `review-only`:

     ```
     (none — review-only mode)

     Scope: see `## Review Target` section below.
     ```

     No other text goes into the body.
   - `## Review Target` (non-canonical supplemental section) is
     populated from the user's `--description` / scope arguments.
   - `## Current Phase: execution`.
   - `## Files Changed` — populated from the actual post-open dirty set
     (read-only snapshot).
2. Write `## Session Metadata`:
   - `entry_point: review-only`
   - `plan_source: review-only`
   - Fresh baseline quintet from current repo state.
3. The execution loop **skips the first Executor round** per
   `docs/protocol/execution.md` §`--review-only` first-round skip.

## Step 2 — Drift check

Per `docs/protocol/session-file.md` §Drift-check decision tree (5
steps). For `--plan` and `--review-only` fresh sessions, the freshly
written baseline equals current state so step 2 + 3 pass cleanly. For
`--session` resumes, run the full decision tree.

On detected drift:

```
(A) Accept drift and reset baseline to current state
    (clears completed_stages entirely, including exec)
(B) Abort
```

- `--accept-external-state` auto-picks (A). Unsafe.
- Handsfree still blocks on drift (external fact, not a design
  decision).
- On (A): set `base_head ← current_head`, `base_dirty ←
  current_dirty`, `last_verified_head ← current_head`,
  `last_verified_dirty ← current_dirty`, clear `completed_stages`
  entirely, continue.
- On (B): release the lock, exit.

### Resume from non-null `delivery_blocked_by`

Per `docs/protocol/session-file.md` §Resume from non-null
`delivery_blocked_by`. When the existing session has a non-null
`delivery_blocked_by`: prompt continue-or-abort with the previous block
reason; on continue, clear `delivery_blocked_by ← null` and then run
the standard drift check.

## Step 3 — Execution round loop

Per `docs/protocol/execution.md` §Step 3 — Execution round loop. Round
sequence: update context (snap/0 via `evidence_ledger.py snapshot` if
none exists) → `### Route Facts` in the packet + `evidence_ledger.py
route --path <touched paths>` per `docs/protocol/execution.md` §Author
route selection (`orchestrator-direct` = every eligibility fact `true`,
every sensitive flag `false`, ledger cross-check passed, snapshot
stored, execution phase → implement directly and produce a Direct
Implementation Record per `docs/protocol/executor-output.md`; anything
else → Executor; without `--facts` the helper reads `### Route Facts`
only from `## Current Review Packet`, never from history or an old DIR,
and a packet without the block routes to `executor`; the canonical packet
is the unique `## Current Review Packet` heading whose next `## ` heading is
`## Review History`, otherwise fail closed to `executor`) → Executor or
direct round (skipped on `--review-only`
round 1) → update
context → `evidence_ledger.py snapshot` → `check` → rewrite
`## Current Review Packet` in full with the `### Attributable Delta`
from `evidence_ledger.py delta` (pre = the snapshot the previous packet
was reviewed at, snap/0 for a first round) → optional context-persist
sub-step → Reviewer (prompt opens with "Read `## Current Review Packet`
first. Load a `## Review History` entry only when the packet references
it or a claim needs provenance. Absence of irrelevant history is not a
defect.") → parse → `finding_triage.py check` (mandatory rubric gate per
`docs/protocol/execution.md` §Mandatory rubric gate: every `[CRITICAL]`
carries the six fields `Trigger:`, `Reachability:`, `Impact:`,
`Likelihood:`, `Fix cost:`, `Cheaper response:`; `incomplete` → discard
the output as malformed, record `rubric_incomplete: finding #n missing
<fields>`, re-dispatch the Reviewer once with the missing-field list,
second `incomplete` → reviewer failure for the round; never implement a
CRITICAL that failed triage) →
`evidence_ledger.py record --check reviewer_approve`
(PASS / FAIL, `--author-route`, plus `--supersedes <earlier id>` when
the review scope grew — every active record of a required check must be
valid; a FAIL is only superseded by a covering executed PASS) →
`check` → Live Report + 13-column Timing Log row → loop
control. `## Evidence Ledger` and `completed_stages` are written only
through `scripts/evidence_ledger.py`; a legacy session without a ledger
fails closed (empty stages, snap/0 from the current verified worktree,
affected checks rerun; "passed earlier" prose is never a PASS).

Dispute flow per `docs/protocol/execution.md` §Dispute flow: the
Reviewer's verdict is never overridden; to dispute a disproportionate
complete CRITICAL run `finding_triage.py dispute --finding n --rationale
<file> --review <output>`, copy its `packet_line` (`Triage: disputed
CRITICAL #n → MINOR/follow-up`) into the packet, implement nothing, and
let the next independent Reviewer `concur` (`concurred` proceeds without
implementing; `re-asserted` keeps blocking; `incomplete` discards the
output); an `orchestrator-direct` dispute needs `--reviewer-record` from a
later round.

### Provenance-aware reviewer prompts

The Orchestrator picks the reviewer-prompt block that matches the
active `## Session Metadata.plan_source`, per
`docs/protocol/execution.md` §Provenance-aware reviewer prompts:

- `plan_source: reviewer-approved` → strict plan-conformance block:
  undisclosed material deviations block; a disclosed equivalent
  simplification satisfying intent and acceptance criteria is not
  automatically CRITICAL; a missing disclosure of a harmless change is a
  MINOR record correction.
- `plan_source: user-supplied` → plan-conformance deviations become
  `[MINOR]`/advisory unless they change user-visible behavior or
  violate acceptance criteria; correctness + intent still enforced.
  The orchestrator MUST emit the literal sentinel
  `(plan_source: user-supplied — plan conformance is advisory/MINOR)`
  verbatim inside the reviewer prompt so tests and audits can confirm
  this block was selected.
- `plan_source: review-only` → pure CR mode. No plan-conformance
  language. Reviewer is explicitly told the Approved Plan body is the
  canonical sentinel from
  `docs/protocol/session-file.md` §Canonical sentinel for `review-only`
  and `## Review Target` carries the scope.

### `--review-only` first-round skip

Per `docs/protocol/execution.md` §`--review-only` first-round skip:

1. Round 1 — jump straight to the Reviewer. No Executor output. The
   review content targets the existing diff + `## Review Target` scope.
   The Orchestrator writes the round-1 Review History entry with the
   literal marker `- Executor backend: skipped (review-only first round)`
   so tests and audits can assert the skip unambiguously.
2. APPROVE → enter the normal post-APPROVE transition: run Step 3.4
   before Step 3.5, and mint `exec` only after Step 3.4 APPROVE/controlled
   SKIP, or after Step 3.4 REQUEST_CHANGES is repaired and a later normal
   Step 3 Reviewer returns APPROVE. This remains the only path where `exec`
   can be added with no author round (Executor or orchestrator-direct)
   before the first review-only gate.
3. REQUEST_CHANGES → round 2+ follows the standard CR → fix loop
   (Executor runs, then Reviewer, alternating).

### Executor / Reviewer dispatch (Claude Code)

Executor: see `docs/protocol/planning.md` §Executor dispatch, Claude
Code block — same template, with the execution-mode task body per
`docs/protocol/execution.md` §Round steps step 2.

Dispatch anchor: `execute_executor_dispatch_skill`. The execution-phase
Executor remains a `judgment`-tier dispatch and resolves `model` as
`executor_model` if set and not `inherit`, else `judgment_model` if set,
else omit.

Reviewer: see `docs/protocol/planning.md` §Reviewer dispatch, Claude
Code block, with the execution-mode review content template per
`docs/protocol/execution.md` §Round steps step 5. `review_style` and
`review_focus` apply to all rounds, not only round 2+.

**Never use `subagent_type: review-loop:<name>`** — plugin agent-type
sandbox bug. Always `general-purpose` with body inlined.

### Loop control

- `APPROVE` → exit the execution loop. If Step 3.4 has not yet run in this
  execution convergence, run Step 3.4 before Step 3.5 and do not mint `exec`
  yet. Step 3.4 is single-pass per execution convergence. Step 3.4 APPROVE or
  controlled SKIP mints `exec` into `completed_stages` for the current
  tree+index state, then proceeds to Step 3.5 unless `--stop-after
  before-polish`. Step 3.4 REQUEST_CHANGES withholds `exec` and feeds the gate
  findings to ordinary Step 3 Executor/Reviewer repair rounds; do not run Step
  3.4 again while repairing those findings. A later normal Step 3 reviewer
  APPROVE after those repairs mints `exec` and proceeds to Step 3.5 unless
  `--stop-after before-polish`. "Mint `exec`" = record `gate` through
  `evidence_ledger.py record` (PASS, FAIL, or `--disposition
  controlled-skip --reason "<verbatim SKIP banner>" --closure declared`)
  and let `check` derive `completed_stages`.
- `REQUEST_CHANGES` → feed feedback to the next Executor round.
- Soft limit + stuck detection per `docs/protocol/execution.md`
  §Per-stage max-round caps (`soft_limit_exec`, default 3).
- `--stop-after exec-round` → clean exit after the current round
  finishes (even on `REQUEST_CHANGES`). Perform step 5 of the drift
  tree (update `last_verified_*`, append to `session_commits`).

### No-op round validation

Per `docs/protocol/execution.md` §No-op execution round validation. The
Orchestrator compares the Executor's claimed file list against the
current-round delta (pre-round vs post-round). Same path sets alone do
not prove a no-op.

- If git diff --name-only HEAD itself fails (non-zero exit, missing repo, etc.) when computing the pre-Executor or post-Executor changed set, stop and surface the failure to the user.
  Then release the single-writer lock per docs/protocol/session-file.md §Lock file lifecycle before exiting. Do not proceed with a partial or invented changed-set.

## Step 3.4 — Terminal Adversarial Gate

Per `docs/protocol/execution.md` §Step 3.4 — Terminal Adversarial Gate.
Single-entry-point Python invoker; single-pass per execution convergence
between Step 3 APPROVE and Step 3.5 polish entry. Gate APPROVE/SKIP mints
`exec`; gate REQUEST_CHANGES returns to ordinary Step 3 repair rounds without
minting `exec`, and the gate does not run again while repairing those findings.
All concerns (plugin-path preference,
snapshot/restore, drain threads, signal cleanup) live inside the invoker.

```bash
# Terminal Adversarial Gate — single-entry-point Python invoker.
python3 scripts/adversarial_gate_invoke.py --focus-file "$focus_text_file"
adversarial_exit=$?
# 0 → APPROVE; 1 → REQUEST_CHANGES; SKIP reasons land on stderr.
```

SKIP banner format: `adversarial-gate: SKIP reason=<reason>[ detail=<...>]`
(5 reasons per the protocol-doc SKIP-reason table). Verdict table:
adapter exit 0 → APPROVE; exit 1 → REQUEST_CHANGES; exit 2 → REQUEST_CHANGES
because produced-but-malformed adversarial output is blocking, not SKIP.
APPROVE mints `exec`; REQUEST_CHANGES does not mint `exec` and feeds findings
to ordinary Step 3 repair rounds without another Step 3.4 pass in that repair
path.

Fallback cleanup failure is a blocking REQUEST_CHANGES, not SKIP: if
`.review-loop/config.md` cannot be proven restored/deleted after fallback
execution, or an unexpected existing or create-from-empty config change must be
preserved for inspection, do not mint `exec`; feed the synthetic `[CRITICAL]`
cleanup issue to ordinary Step 3 repair rounds without another Step 3.4 pass
in that repair path.

No Agent dispatch involved — invoker is a Bash shell-out to Python, so
the plugin-agent-sandbox bug does not apply.

Gate rubric revalidation per `docs/protocol/execution.md` §Gate rubric
revalidation: partition the rendered gate text with `finding_triage.py
check --session {uuid} --input <gate text> --record-pending`; only the
complete findings (`complete_findings_text`) feed the next Executor round;
when none is complete skip the Executor (`revalidation_round: true`,
Timing Log `executor:0`); the next normal Step 3 Reviewer prompt carries a
`## Rubric revalidation` block listing every pending entry with its
missing fields ("re-assert it as a `[CRITICAL]` with the full six-field
rubric, or drop it; nothing listed has been implemented"); per entry
`finding_triage.py revalidate --pending <id> --review <output>` →
`re-asserted` (ordinary repair loop) / `concurred` (dropped) /
`incomplete` (output discarded; normal-Reviewer retry row). A synthetic
invoker REQUEST_CHANGES (cleanup / capture / adapter / banner failure — a
synthetic invoker text anchored at the invoker / adapter, `confidence=1.0`,
carrying no rubric fields) is
an infrastructure failure, not a finding: it is not partitioned, not
revalidated and not disputable (`check` lists it under `infrastructure`,
exit 1); a gate `[CRITICAL]` at those paths that carries any rubric field
is an ordinary finding; `exec` stays withheld until the runtime failure is
resolved and Step 3 re-runs. The gate is
never re-run; `exec` is minted only on a triage-complete APPROVE with no
complete gate finding open and every pending entry dropped or repaired
(`finding_triage.py status` exit 0); revalidation rounds count toward
`soft_limit_exec`. An incomplete finding never appears in an Executor
prompt.

The `adversarial_gate_skip_paths` config key (default
`["**/SKILL.md", "docs/protocol/**", "tests/skills/contracts/**"]`)
lets the orchestrator skip the gate entirely when every Step 3 changed
file matches one of the patterns.

## Step 3.5 — Quality Polish

Per `docs/protocol/execution.md` §Step 3.5. Runs language-specific
static analysis, code-quality review-fix loop, simplify, test
consolidation. `quality_focus` applies only when Step 3.5 Quality Polish
actually runs. If `skip_quality_polish: true` in config, mint `polish`
as a no-op completion and continue to Step 3.6.

- Any substep that writes code runs the write-boundary sequence
  `evidence_ledger.py snapshot` → `classify` → `check`: records whose
  declared closure the write touched are invalidated per record, and an
  exec-invalidating delta replays from `exec` per
  `docs/protocol/session-file.md` §`completed_stages` lifecycle.
  Termination is guaranteed by the per-stage caps.
- Narrow `reviewer-only fast-replay` exception: a Step 3.5.4 write that
  `classify` reports as non-invalidating for `exec` (no changed path inside
  a declared `exec` closure; no `uncertain` closure) keeps the surviving
  records per `docs/protocol/session-file.md` §`completed_stages`
  lifecycle; no prose-likeness or path-category test is applied.
- Record every substep outcome through `evidence_ledger.py record`
  (`static_analysis` PASS or `--disposition not-applicable --reason`,
  `agent_review:<name>`, `simplify`, `tests` with `--env-command` and
  `--selector` for its discovery roots). Step 3.5.4 reviewer-only
  fast-replay `APPROVE` does not itself make `polish` present; `polish` is
  derived by `check` once every required record is valid after the full
  Step 3.5 invocation finished cleanly.
- Hallucination guard: for every quality agent returning `tool_uses:
  0`, discard and retry once; if retry is also 0, skip and report.
- `--stop-after before-polish` → exit after Step 3.4 APPROVE/SKIP and
  before Step 3.5 starts.
  `--stop-after before-docs` → exit after Step 3.5 and before Step 3.6.

All Step 3.5 invocations use `subagent_type: general-purpose` with the
agent body inlined, per `docs/protocol/execution.md` §3.5.2 /
§3.5.3 / §3.5.4 / §3.5.5.

## Step 3.6 — Documentation Consistency

Per `docs/protocol/execution.md` §Step 3.6. Single pass. Update project
docs + fix stale code comments. Writes → `snapshot` → `classify` →
`check`; exec-invalidating → replay from `exec`, otherwise the narrow
`reviewer-only fast-replay` of the touched non-exec claims. Record
`docs_consistency` (PASS on APPROVE, FAIL on REQUEST_CHANGES); `docs` is
derived. **After `docs` is present, proceed to Step 3.7** — a no-op docs
stage is not a terminal state.

- Hallucination guard: for every documentation-stage agent returning `tool_uses: 0`, discard and retry once; if retry is also 0, skip and report.

`--stop-after before-security` → exit after Step 3.6 and before Step 3.7.

## Step 3.7 — Security Preflight

Per `docs/protocol/execution.md` §Step 3.7. Single scan. Check for
tracked/staged sensitive files; audit `.gitignore` for missing
coverage. Writes to `.gitignore` or `git rm --cached` → `snapshot` →
`check`, always exec-invalidating → replay from `exec`. No-write →
record `security_scan` (`--env-command`); `security` is derived.

Step 3.7 runs **unconditionally** after Step 3.6, regardless of
whether any prior stage wrote files. A no-op session (zero code
changes, zero doc updates) still runs this scan — it is a security
gate, not a content-dependent step. The only exits before 3.7 are
`--stop-after before-security` / `before-docs` / `before-polish` /
`exec-round`.

- Hallucination guard: for every security-stage agent returning `tool_uses: 0`, discard and retry once; if retry is also 0, skip and report.

`--stop-after before-delivery` → exit after Step 3.7 and before Step 4.

## Step 4 — Delivery

Per `docs/protocol/execution.md` §Step 4 — Delivery, gated by the
delivery gate in §Delivery gate: `runtime_supported_set ⊆
completed_stages` (re-derived by `evidence_ledger.py check` immediately
before the gate). For Claude Code the runtime set is
`{exec, polish, docs, security}`; for Codex Stage 1 it is
`{exec, polish, docs, security}`. Codex Stage 1: `{exec, polish, docs, security} ⊆ completed_stages`.

On gate failure, hard-stop per §Delivery gate: set
`delivery_blocked_by ← <first missing stage>`, release the single-writer lock per docs/protocol/session-file.md §Lock file lifecycle, and exit without delivering.
The stuck summary is printed from `docs/protocol/execution.md`
§Per-stage max-round caps.

On gate pass:

1. If `auto_commit: true`: stage only the files the Executor reported
   (never `git add -A`/`git add .`), commit with
   `{commit_message_prefix}: {title}`, append sha to `session_commits`.
2. Print the Delivery Summary (format in
   `docs/protocol/execution.md` §Step 4 — Delivery — this skill reuses
   the same format). Render the Delivery Summary in 中文 (Simplified
   Chinese) per `docs/protocol/execution.md` §Step 4: section
   headings, prose, and prose-style field values use 中文; ASCII
   tokens stay in original form.
3. Append to `docs_file` if set.
4. Cleanup round temp files; preserve the session file.
5. Clear `delivery_blocked_by ← null`. Release the lock.

## `--stop-after` replay / invalidation interaction

- Clean `--stop-after` exit at any stage clears `delivery_blocked_by`
  per `docs/protocol/session-file.md` §`delivery_blocked_by`
  lifecycle, even if the session was previously blocked (the user
  already acknowledged on resume).
- Stage invalidation rules (see
  `docs/protocol/session-file.md` §`completed_stages` lifecycle) apply
  during replay. Each writing substep clears the set; replay restarts
  from `exec`. Per-stage caps bound iteration.

## Hard-stop and `delivery_blocked_by` lifecycle

Per `docs/protocol/session-file.md` §`delivery_blocked_by` lifecycle.

- Set by: per-stage hard-stop (`<stage>`) or signal abort
  (`"user-abort"`).
- Cleared by: delivery success, clean `--stop-after` exit, user's
  resume-continue choice.
- Unrecoverable errors preserve `last_verified_*` and
  `delivery_blocked_by` unchanged so the user can audit.

---

## Context Management

The Orchestrator keeps minimal state between rounds (session path,
latest Reviewer feedback, round number, current stage). All durable
state is on disk. See `docs/protocol/planning.md` §Context management
discipline (applies equally to execution). The Reviewer's default input
is `## Current Review Packet`; `## Review History` is loaded on demand by
entry id.
