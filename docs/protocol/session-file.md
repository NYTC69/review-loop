# Protocol — Session File

The shared `.review-loop/sessions/{uuid}.md` session file is the single
cross-runtime contract for the review-loop workflow. Both Claude Code and
Codex (Stage 1) runtimes read and write the same file format, so a user can
run `plan` on one runtime and `execute --session {uuid}` on the other.

This document is the authoritative schema + lifecycle reference for that
file. Runtime-agnostic: nothing here depends on whether the orchestrator is
Claude Code or Codex.

---

## Canonical sections

Every session file contains the following sections, in this order. Sections
are rewritten in full on each orchestrator update (logical append-only
sections like `## Review History` and `## Timing Log` are still rewritten in
full; the orchestrator is the only writer).

1. `## Problem Description`
2. `## Context`
3. `## Acceptance Criteria`
4. `## Current Phase`
5. `## Approved Plan`
6. `## Review History`
7. `## Files Changed`
8. `## Key Related Files`
9. `## Timing Log`
10. `## Session Metadata` — always last

Two non-canonical supplemental sections may appear alongside the canonical
set:

- `## Review Target` — present **only** when the session was created via
  `execute --review-only`. See
  [§Review Target (review-only only)](#review-target-review-only-only) below.
- `## Draft Plan` — present **only** during the planning phase, before
  `## Approved Plan` has been populated. See
  [§Draft Plan (planning phase only)](#draft-plan-planning-phase-only) below.

---

## Approved Plan — three Sources

`## Approved Plan` is always present. It carries a `Source` sub-field that
identifies the provenance of the plan body. The Source drives reviewer
strictness in the execution phase (see
[execution.md §Provenance-aware reviewer prompts](./execution.md#provenance-aware-reviewer-prompts)).

| Source | When | Body | Code-reviewer behavior |
|---|---|---|---|
| `reviewer-approved` | `plan` loop produced an approved plan | reviewer-approved plan text | strict plan-conformance |
| `user-supplied` | `execute --plan <text\|path>` | user's free-form plan text, injected verbatim | plan-conformance deviations → MINOR/advisory; correctness + intent still enforced |
| `review-only` | `execute --review-only` | canonical sentinel (below) | pure CR mode; reviewer told Approved Plan body is a sentinel |

The `Source` sub-field appears inside the section, e.g.:

```
## Approved Plan

- Source: reviewer-approved

<plan body here>
```

`## Approved Plan` having non-empty body content without a `Source`
sub-field is **invalid**: once the section is populated it must carry one
of the three Source values above. During the planning phase, before a
plan has been approved, the section body is empty and no `Source`
sub-field is written; the current round's draft lives in the
supplemental [§Draft Plan](#draft-plan-planning-phase-only) section
instead.

### Canonical sentinel for `review-only`

When `Source: review-only`, the section body is literally the string:

```
(none — review-only mode)

Scope: see `## Review Target` section below.
```

No other text goes into the body. The sentinel is what the reviewer sees; any
scope/description the user provided goes into `## Review Target` instead.

### Review Target (review-only only)

`--review-only` additionally populates a non-canonical `## Review Target`
section describing the scope of the pure-CR sweep (files, directories, or
free-form description the user passed via `--description`).

- Present only when `plan_source: review-only`.
- Absent for `reviewer-approved` and `user-supplied` entries.
- Rewritten in full by the orchestrator; sub-agents do not touch it.

### Draft Plan (planning phase only)

`## Draft Plan` is a non-canonical supplemental section that holds the
current planning round's draft content. It exists **only during the
planning phase**, while `## Approved Plan` is still empty.

- Present only while `## Current Phase: planning` and `## Approved Plan`
  has an empty body with no `Source` sub-field.
- Each planning round's Executor output **overwrites** this section in
  full; earlier drafts are not retained here (historical context lives
  in `## Review History`).
- On APPROVE, the orchestrator promotes the `## Draft Plan` body into
  `## Approved Plan`, writes `- Source: reviewer-approved`, and
  **removes `## Draft Plan` entirely** from the session file. See
  [planning.md §Loop control](./planning.md#loop-control).
- Absent whenever `## Approved Plan` is already populated — i.e. for
  `--session` resumes into execution, `--plan`, `--review-only`, and
  any point after a planning-phase APPROVE (the section is removed on
  promotion, not left as an empty placeholder).
- Rewritten in full by the orchestrator; sub-agents do not touch it.

---

## Entry-mode initialization table

Each entry point writes a specific initial shape into the canonical sections.
Every cell below is what the orchestrator must produce at session creation
(or at resume for `--session`). Empty cells mean the section starts empty.

| Section | `plan` / fresh `review-loop` (planning) | `--session <uuid>` resume | `--plan <text\|path>` | `--review-only` |
|---|---|---|---|---|
| `## Problem Description` | from `--title` / `--description` flags | preserved from existing file | from `--title` / `--description` flags | from `--description` (if provided) else `"Review-only pass over current working tree."` |
| `## Context` | from `--context` / discovery (or empty placeholder) | preserved | `"User-supplied plan; no planning-phase context captured."` + any `--description` | derived from `## Review Target` scope |
| `## Acceptance Criteria` | from `--acceptance-criteria` / discovery | preserved | `"Implementation matches the user-supplied plan in ## Approved Plan."` | `"Reviewer returns APPROVE with no CRITICAL issues for the target scope."` |
| `## Current Phase` | `planning` | preserved (typically `execution`) | `execution` | `execution` |
| `## Approved Plan` | empty body, no `Source` sub-field | preserved (must already exist) | Source: `user-supplied` + injected user text | Source: `review-only` + canonical sentinel |
| `## Draft Plan` | present; overwritten by each planning round's Executor output | absent | absent | absent |
| `## Review History` | empty | preserved | empty | empty |
| `## Files Changed` | empty | preserved | empty | populated from actual post-open dirty set (read-only snapshot) |
| `## Key Related Files` | empty | preserved | empty | populated from `## Review Target` scope |
| `## Timing Log` | empty table header | preserved | empty table header | empty table header |
| `## Review Target` | absent | preserved if present; absent otherwise | absent | populated from `--description` / scope args |
| `## Session Metadata` | fresh metadata block; `plan_source` omitted during planning draft rounds and written on APPROVE | preserved, then re-baselined on drift check | fresh metadata block (see [§Session Metadata schema](#session-metadata-schema)) | fresh metadata block |

Notes:

- `--session` is a pure resume path. The orchestrator never rewrites existing
  canonical content beyond the single `## Session Metadata` re-baselining
  and the `## Review History` / `## Timing Log` / `## Files Changed` /
  `## Key Related Files` updates that normally accumulate during execution.
- For `--plan`, the orchestrator does **not** run a planning loop; the user
  text is injected verbatim, `plan_source` is set to `user-supplied`, and
  execution starts immediately.
- For `--review-only`, the orchestrator skips the first Executor round (see
  [execution.md §`--review-only` first-round skip](./execution.md#review-only-first-round-skip)).

---

## Session Metadata schema

`## Session Metadata` is the moving-baseline block that the orchestrator
rewrites in full on each update. It lives as the final section in the file.

```
## Session Metadata
- entry_point: plan | execute-from-session | execute-from-plan | review-only | review-loop
- plan_source: reviewer-approved | user-supplied | review-only
- base_head: <sha at session creation>
- base_dirty: { "<path>": "<blob_sha>" | "<deleted>", ... }
- last_verified_head: <sha>
- last_verified_dirty: { "<path>": "<blob_sha>" | "<deleted>", ... }
- session_commits: [<sha>, ...]   # commits authored inside this session (auto_commit), append-only
- completed_stages: [<stage>, ...] # currently-valid validations; see stages section
- delivery_blocked_by: <stage> | "user-abort" | null
```

Fields:

- `entry_point` — identifies how the session was created. `plan` /
  `execute-from-session` / `execute-from-plan` / `review-only` /
  `review-loop`. Set once on creation; **not** rewritten on resume.
  The value `execute-from-session` is reserved for the backward-compat
  edge where a legacy session lacks `entry_point` entirely; in that
  single case the backward-compat backfill path (see
  [§Backward-compat fallback](#backward-compat-fallback)) may write
  `execute-from-session` on first resume. Normal `--session` resumes of
  a well-formed session leave `entry_point` untouched, so the runtime
  values written on fresh session creation are `plan`,
  `execute-from-plan`, `review-only`, or `review-loop`.
- `plan_source` — provenance of `## Approved Plan`. Drives reviewer
  strictness. Omitted during planning draft rounds; written on APPROVE
  (one of the three post-approval values).
- `base_head` — git `HEAD` sha at session creation. Baseline for the
  initial dirty map.
- `base_dirty` — the dirty-map snapshot at session creation. Format: object
  mapping `path → blob_sha` or `path → "<deleted>"` (tombstone).
  See [§Dirty map construction](#dirty-map-construction).
- `last_verified_head` — the most recent `HEAD` sha that passed drift check.
  Updated on every clean batch exit.
- `last_verified_dirty` — the dirty map that passed drift check alongside
  `last_verified_head`.
- `session_commits` — append-only list of shas authored inside this session
  (e.g. by `auto_commit`). Used to distinguish session-owned progress from
  external `HEAD` movement in the drift check.
- `completed_stages` — currently-valid validations. Not a historical log;
  see [§`completed_stages` lifecycle](#completed_stages-lifecycle).
- `delivery_blocked_by` — see
  [§`delivery_blocked_by` lifecycle](#delivery_blocked_by-lifecycle).

Old sessions missing any of these fields are handled by the
[backward-compat fallback rules](#backward-compat-fallback).

---

## Lock file lifecycle

A single-writer lock guards one orchestrator invocation. Path:

```
.review-loop/sessions/{uuid}.lock
```

Scope: one orchestrator invocation (not one delivery). Multi-batch runs that
exit with `--stop-after` still release the lock on clean exit.

Lock body (text or JSON — runtime-specific; schema-equivalent across
runtimes):

- `pid` — orchestrator's OS pid
- `started_at` — ISO-8601 timestamp when the orchestrator acquired the lock
- `entry_point` — same enum as `Session Metadata.entry_point`
- `stop_after` — the `--stop-after` value for this invocation
  (`delivery` if unset)

### Lifecycle events

- **Create on orchestrator start.** After the lock file is written, proceed
  to session init / drift check.
- **Remove on every clean-exit path**:
  - Delivery success.
  - `--stop-after <stage>` clean exit.
  - Signal abort (SIGINT/SIGTERM): trap fires → remove lock → exit.
  - Unrecoverable error trap: remove lock → exit with non-zero.

### Start-time check

Before creating the lock:

1. **No lock present** → proceed, create lock.
2. **Lock present + PID alive** → refuse to start. Print stale-run message
   with the running pid and `started_at`. Do not touch the session file.
3. **Lock present + PID dead** → prompt the user to recover: "A previous
   orchestrator crashed. Recover and proceed?" On yes, delete the stale lock
   and proceed. On no, exit.

The lock and the moving baseline are **orthogonal**: the lock guards
concurrent writers at one moment; the moving baseline distinguishes
session-owned progress from external drift over time.

---

## Dirty map construction

The dirty map is a `{path: hash_or_tombstone}` object built from
`git status --porcelain=v1 -z`. The rules are evaluated **in order**; the
first matching rule wins. The `Y` (worktree) status character is dominant
except in the staged-only sub-cases noted below.

Helper definitions:

- `hash_worktree(p) = git hash-object <p>`
- `hash_index(p) = third column of git ls-files -s <p>`
- Tombstone for a deleted or to-be-deleted file: the literal string
  `"<deleted>"`.

### Eight branches (ordered)

1. **Unmerged** — `X == 'U'` or `Y == 'U'`, or the combinations `AA` and
   `DD`. → **Hard error**: user must resolve conflicts first. The
   orchestrator refuses to proceed.
2. **Untracked** — `XY == "??"`. →
   `{<path>: hash_worktree(<path>)}`.
3. **Rename / copy** — `X in {R,C}` or `Y in {R,C}`. Two entries per record:
   `<old>` (rename only; copy's `<src>` gets no entry) and `<new>`.

   | Condition | `<old>` | `<new>` |
   |---|---|---|
   | `Y == 'D'` | `"<deleted>"` | `"<deleted>"` |
   | `Y in {'M','T','A','R','C'}` | `"<deleted>"` | `hash_worktree(<new>)` |
   | `Y == ' '` (staged-only) | `"<deleted>"` | `hash_index(<new>)` |

4. **`Y == 'D'`** (non-R/C `*D` codes: ` D`, `MD`, `AD`, `TD`) →
   `{<path>: "<deleted>"}`.
5. **`Y in {'M','T','A'}`** →
   `{<path>: hash_worktree(<path>)}`. Type changes are naturally covered by
   the new blob hash.
6. **`Y == ' '` and `X in {'M','A','T'}`** →
   `{<path>: hash_index(<path>)}`.
7. **`Y == ' '` and `X == 'D'`** →
   `{<path>: "<deleted>"}`.
8. **Otherwise** → **hard error** (unknown git state; user verifies
   environment).

### Example outputs

- File `a.txt` modified in worktree, not staged → rule 5 →
  `{"a.txt": "<worktree-blob-sha>"}`.
- File `b.txt` staged as `A`, then modified in worktree → `Y=='M'`, rule 5 →
  `{"b.txt": "<worktree-blob-sha>"}`.
- File `c.txt` deleted in worktree, not staged → rule 4 →
  `{"c.txt": "<deleted>"}`.
- File `d.txt` staged as `D` with clean worktree → rule 7 →
  `{"d.txt": "<deleted>"}`.
- File renamed `e.txt` → `e2.txt`, worktree modified after rename →
  rule 3 row 2 →
  `{"e.txt": "<deleted>", "e2.txt": "<worktree-blob-sha>"}`.

---

## Drift-check decision tree

Runs at the start of every `execute --session` invocation and at the start of
every batch inside a multi-batch run. The orchestrator compares current tree
state against `last_verified_head` + `last_verified_dirty` from
`## Session Metadata`.

### Steps

1. **Compute current state.** Read `current_head = git rev-parse HEAD` and
   `current_dirty = dirty map per rules above`.
2. **HEAD branch.**
   - `current_head == last_verified_head` → **goto 3**.
   - `current_head` is a descendant of `last_verified_head` **AND** every
     commit in `last_verified_head..current_head` is in `session_commits` →
     session-owned progress; **goto 3**.
   - Otherwise → `drift_reason: external-head`; **goto 4**.
3. **Dirty-map re-verification** (always runs). For each path in
   `current_dirty ∪ last_verified_dirty`:
   - Same value on both sides → OK.
   - Different values (hash/tombstone mismatch) →
     `drift_reason: content-or-deletion-changed`; **goto 4**.
   - Only in `current_dirty` → `drift_reason: newly-dirty`; **goto 4**
     (always external drift; this closes the revert-then-add-back loophole).
   - Only in `last_verified_dirty`, `current_head` unchanged →
     `drift_reason: reverted-externally`; **goto 4**. If `current_head`
     moved forward, the file may have been committed — step 2 path b already
     validated that, so this rule does not trigger in that case.
4. **Drift handling.** Print `drift_reason` + a short diff detail block, and
   prompt:

   ```
   (A) Accept drift and reset baseline to current state
       (clears completed_stages entirely, including exec)
   (B) Abort
   ```

   Handsfree mode still blocks (external fact, not a design decision). The
   `--accept-external-state` opt-in flag auto-picks A; flag is documented as
   unsafe (see [§`--accept-external-state`](#--accept-external-state-semantics)).
   - On (A): set `base_head ← current_head`, `base_dirty ← current_dirty`,
     `last_verified_head ← current_head`, `last_verified_dirty ←
     current_dirty`, clear `completed_stages` entirely, continue.
   - On (B): exit without touching `## Session Metadata` other than lock
     release.
5. **Clean batch exit.** On a clean batch exit (APPROVE or `--stop-after`):
   - `last_verified_head ← current_head`
   - `last_verified_dirty ← current_dirty`
   - Append any `auto_commit` sha to `session_commits`
   - Update `delivery_blocked_by` per the exit path (see
     [§`delivery_blocked_by` lifecycle](#delivery_blocked_by-lifecycle)).

---

## `completed_stages` lifecycle

`completed_stages` represents validations that hold **for the current
tree+index state**. It is a set, not a historical log.

### Stage add events

- `exec` — one Executor+Reviewer cycle returned APPROVE for the current
  state. In `--review-only` mode a reviewer-only APPROVE on the existing diff
  also mints `exec` (no Executor runs, by design — see
  [execution.md §`--review-only` first-round skip](./execution.md#review-only-first-round-skip)).
- `polish` — Step 3.5 completed without unresolved issues.
- `docs` — Step 3.6 completed.
- `security` — Step 3.7 completed.

### Invalidation rules (any of these clears the ENTIRE set)

1. A new Executor round starts.
2. Drift accepted in the decision tree step 4 → A.
3. Old-session baseline backfill accepted
   (see [§Backward-compat fallback](#backward-compat-fallback)).
4. Step 3.5 polish substep writes code (simplify / executor-fix /
   test-consolidation).
5. Step 3.6 docs write (code or stale-comment fix).
6. Step 3.7 security preflight writes `.gitignore` or causes
   `git rm --cached`.

### Replay rule

After clearing, the orchestrator **replays from `exec`** in runtime order
until the runtime-supported set is present again. Each replay iteration that
writes files clears the set and restarts from `exec`. Termination is
guaranteed by the per-stage caps documented in
[execution.md §Per-stage max-round caps](./execution.md#per-stage-max-round-caps).

### Runtime-supported sets

- Claude Code: `{exec, polish, docs, security}`.
- Codex Stage 1: `{exec, polish, docs, security}`.

The runtime-supported set is the gate for delivery
(see [execution.md §Delivery gate](./execution.md#delivery-gate)).

---

## `delivery_blocked_by` lifecycle

Type:

```
<stage> | "user-abort" | null
```

where `<stage>` is one of `exec`, `polish`, `docs`, `security`.

### Set-by events

- **Hard-stop**: a stage hits its per-stage max-round cap with unresolved
  findings → `delivery_blocked_by ← <stage>`. The stage is NOT added to
  `completed_stages` (invariant: "only clean passes in the set"). The
  orchestrator prints a stuck summary, updates the baseline to current
  state, and does not deliver.
- **Signal abort**: user hits SIGINT/SIGTERM → the trap sets
  `delivery_blocked_by ← "user-abort"` before releasing the lock.

Unrecoverable errors are a separate path: they preserve `last_verified_*`
and `delivery_blocked_by` unchanged so the user can audit.

### Clear-by events

- **Delivery success** → `delivery_blocked_by ← null`.
- **Clean `--stop-after` exit** (even if resuming from a previously blocked
  state — the user already acknowledged on resume) → `delivery_blocked_by ←
  null`.
- **User's resume-continue choice** → see
  [§Resume from non-null `delivery_blocked_by`](#resume-from-non-null-delivery_blocked_by).

### Resume from non-null `delivery_blocked_by`

On `execute --session <uuid>` when the existing session has a non-null
`delivery_blocked_by`, the orchestrator:

1. Prompts the user with the previous block reason (the `<stage>` or
   `"user-abort"`): **continue** or **abort**.
2. **On continue**: immediately clear `delivery_blocked_by ← null`, then run
   the standard drift check. The user's in-place fix will show up as drift
   (content hashes changed vs `last_verified_dirty`). Accepting the drift
   clears `completed_stages` and replay restarts from `exec`. **This is the
   safe default**: preserving intermediate stages across a user fix would
   contradict the "validations hold for current state" invariant. The user
   explicitly opted into discarding any previously-passed stages by
   accepting drift.
3. **On abort**: clear `delivery_blocked_by`, leave `completed_stages`
   alone, exit.

---

## `--accept-external-state` semantics

The `--accept-external-state` orchestrator flag skips interactive
pause-and-confirm prompts by auto-selecting the "accept" branch wherever
those prompts appear:

- Drift-check step 4 → auto (A): accept drift and reset baseline. Clears
  `completed_stages`.
- Backward-compat fallback (missing baseline quintet) → auto-accept current
  repo state as new baseline. Clears `completed_stages`.

The flag is documented as **unsafe opt-in**. Handsfree mode alone does
**not** auto-pick A; the user must pass `--accept-external-state`
explicitly. The flag has no effect outside these two prompts — it does not
bypass unmerged-conflict errors, unknown-git-state errors, or the per-stage
max-round hard-stops.

---

## Backward-compat fallback

Old session files may predate the v10 metadata schema. The orchestrator
degrades as follows when reading an existing session:

| Missing field(s) | Behavior |
|---|---|
| `plan_source` | Treated as `reviewer-approved` (strict default). |
| Baseline quintet — any of `base_head`, `base_dirty`, `last_verified_head`, `last_verified_dirty`, `session_commits` | **Pause and prompt** the user: "This session predates the moving-baseline schema. Accept current repo state as new baseline?" On (A): backfill all five fields from current state, clear `completed_stages` entirely. On (B): abort. Handsfree alone blocks; `--accept-external-state` auto-picks (A). |
| `completed_stages` | Empty set; replay starts from `exec`. |
| `delivery_blocked_by` | Treated as `null` (normal resume). |

No silent backfill of the baseline quintet. No silent adoption of external
drift. The orchestrator will not "just fill in" the baseline from `HEAD` on
resume without explicit user acknowledgment, because the file's last write
may have been weeks ago and the tree may have moved arbitrarily since.
