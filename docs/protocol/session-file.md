# Protocol — Session File

The shared `.review-loop/sessions/{uuid}.md` session file is the single
cross-runtime contract for the review-loop workflow. Both Claude Code and
Codex (Stage 1) runtimes read and write the same file format, so a user can
run `plan` on one runtime and `execute --session {uuid}` on the other.

This document is the authoritative schema + lifecycle reference for that
file. Runtime-agnostic: nothing here depends on whether the orchestrator is
Claude Code or Codex.

Codex Stage 1 assumes a single orchestrator-owned workspace for the session.
Until a future protocol revision adds explicit orchestrator-managed worktree
binding, there is no executor-side workspace override in Stage 1.

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
6. `## Current Review Packet` — rewritten in full each round; see
   [§Current Review Packet](#current-review-packet)
7. `## Review History`
8. `## Files Changed`
9. `## Key Related Files`
10. `## Timing Log`
11. `## Evidence Ledger` — one fenced JSON block, written only by
    `scripts/evidence_ledger.py`; see [§Evidence Ledger](#evidence-ledger)
12. `## Session Metadata` — always last

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
| `## Current Review Packet` | empty until the first round's packet is written | preserved; rewritten in full by the next round (legacy session without one: fresh packet whose first delta is snap/0 → snap/1) | empty until the first round's packet is written | empty until the first round's packet is written |
| `## Review History` | empty | preserved | empty | empty |
| `## Files Changed` | empty | preserved | empty | populated from actual post-open dirty set (read-only snapshot) |
| `## Key Related Files` | empty | preserved | empty | populated from `## Review Target` scope |
| `## Timing Log` | empty 13-column table header (see [§Timing Log columns](#timing-log-columns)) | preserved; legacy 4-column rows rewritten with `N/A` on first write | empty 13-column table header | empty 13-column table header |
| `## Evidence Ledger` | `evidence_ledger.py snapshot` stores snap/0 of the current verified worktree; no records | preserved; legacy session without a ledger: snap/0 taken from the current verified worktree, `completed_stages` empty (see [§Backward-compat fallback](#backward-compat-fallback)) | snap/0 stored; no records | snap/0 stored; no records |
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
  see [§`completed_stages` lifecycle](#completed_stages-lifecycle). Derived
  from `## Evidence Ledger` by `scripts/evidence_ledger.py check` at every
  write boundary; the orchestrator never hand-edits it (see
  [§Derived `completed_stages`](#derived-completed_stages)).
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

- `exec` — Step 3 reviewer APPROVE plus either Step 3.4 APPROVE/controlled
  SKIP, or Step 3.4 REQUEST_CHANGES repaired by a later normal Step 3 reviewer
  APPROVE, for the current state. In `--review-only` mode a reviewer-only
  APPROVE on the existing diff enters Step 3.4; that gate's APPROVE/controlled
  SKIP mints `exec`, while gate REQUEST_CHANGES returns to ordinary Step 3
  Executor/Reviewer repair rounds and a later normal reviewer APPROVE mints
  `exec` (no Executor runs before the first review-only gate, by design — see
  [execution.md §`--review-only` first-round skip](./execution.md#review-only-first-round-skip)).
- `polish` — Step 3.5 completed without unresolved issues. A Step 3.5
  `reviewer-only fast-replay` APPROVE preserves the current set but does not
  itself mint `polish`; Step 3.5.6 mints `polish` only after the full Step
  3.5 invocation finishes cleanly with either no writes, or only eligible
  writes already approved via reviewer-only fast-replay.
- `docs` — Step 3.6 completed. Eligible Step 3.6 prose/comment/metadata-only
  writes may use reviewer-only fast-replay, but `docs` is still minted only
  after the Step 3.6 reviewer APPROVE.
- `security` — Step 3.7 completed.

### Invalidation rules

Two events clear the ENTIRE set (external provenance = uncertain):

1. Drift accepted in the decision tree step 4 → A.
2. Old-session baseline backfill accepted
   (see [§Backward-compat fallback](#backward-compat-fallback)).

Every other write — a new Executor or orchestrator-direct round, a Step 3.5
/ 3.6 write, a Step 3.7 `.gitignore` write or `git rm --cached` — invalidates
**per record** through the [§Active-record rule](#active-record-rule): each
active evidence record whose `inputs`, `deps`, or selector membership no
longer matches the worktree becomes invalid, and `evidence_ledger.py check`
recomputes `completed_stages` from what remains valid. A stage that lost a
required claim disappears from the set; a stage whose closure was untouched
stays. There is no all-clear for ordinary writes and no line-count or
path-category rule anywhere.

### Reviewer-only fast-replay

This is the narrow path on which already-earned `completed_stages` survive a
Step 3.5.4 or Step 3.6 write. It is no longer decided by looking at the
diff's prose-likeness: the orchestrator runs `evidence_ledger.py classify`,
which is a dependency-closure proof (see
[§Convergence rule](#convergence-rule)). A write is eligible for
`reviewer-only fast-replay` only when all of the following are true:

- The write happened in Step 3.5.4 or Step 3.6.
- `classify` reports the delta as non-invalidating for `exec`: no changed
  path lies inside the declared closure of any `exec`-required record, and
  every `exec`-required record has `closure = declared`.
- The touched non-exec claims (for example `docs_consistency`) are replayed
  by a Reviewer-only round and recorded fresh.

The exception is fail-closed. The following always invalidate `exec` and
start a new execution convergence:

- Any changed path inside a declared `exec` closure, or any `exec` record
  with `closure = uncertain` (regardless of path).
- Drift acceptance, old-session baseline backfill, and Step 3.7 writes.

Fast-replay outcomes:

1. **Step 3.5.4 APPROVE**: preserve the current `completed_stages`, continue
   through the remaining Step 3.5 substeps, and do **not** mint `polish`
   yet.
2. **Step 3.5.6 clean finish**: mint `polish` only if the Step 3.5
   invocation finished cleanly and had either no writes, or only eligible
   writes that already passed reviewer-only fast-replay.
3. **Step 3.6 APPROVE**: preserve the current `completed_stages` and mint
   `docs` for the current tree+index state.
4. **REQUEST_CHANGES at fast-replay review**: clear `completed_stages`
   entirely and replay from `exec`. Do not preserve earlier stage entries.

### Replay rule

After `exec` is invalidated, the orchestrator **replays from `exec`** in
runtime order until the runtime-supported set is present again. Each replay
iteration that writes files invalidates per record and, when `exec` is
touched, restarts from `exec`. Termination is guaranteed by the per-stage
caps documented in
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
| `## Evidence Ledger` / `## Current Review Packet` (legacy session without bound evidence) | **Fail closed.** `completed_stages` is treated as empty regardless of what the file says; `evidence_ledger.py snapshot` stores snap/0 from the current verified worktree (tracked dirty and in-scope untracked content included, so nothing pre-existing is ever attributed to the task); a fresh packet is written whose first delta is snap/0 → snap/1; every affected check is rerun and recorded. "Passed earlier" prose in `## Review History` is never reinterpreted as a reusable PASS. |

No silent backfill of the baseline quintet. No silent adoption of external
drift. The orchestrator will not "just fill in" the baseline from `HEAD` on
resume without explicit user acknowledgment, because the file's last write
may have been weeks ago and the tree may have moved arbitrarily since.

---

## Evidence Ledger

`## Evidence Ledger` holds one fenced ` ```json ` block:
`{"schema": 1, "scope": [...], "snapshots": [...], "records": [...]}`. The
orchestrator is the only writer and **every** write goes through
`scripts/evidence_ledger.py` (`snapshot`, `record`, `check`, `classify`,
`delta`, `route`, `prune`); there
is no in-prose hashing and no in-prose stage minting. The same block
carries an optional `triage` object (`{"next_id", "disputes",
"pending_rubric_incomplete"}`) written only by `scripts/finding_triage.py`
(`dispute`, `concur`, `revalidate`, `check --record-pending`) and preserved
untouched by `evidence_ledger.py`; see
[execution.md §Dispute flow](./execution.md#dispute-flow). Both runtimes shell
out to the same helper. Codex Stage 1 runs `snapshot` / `record` outside
the Codex sandbox (the workspace-write sandbox denies `.git` writes), under
the same rule the Codex mirrors apply to the `claude -p` reviewer command.

### Snapshots

`evidence_ledger.py snapshot` stores **content**, not bare hashes, at every
write boundary: all tracked paths plus untracked, non-ignored paths inside
the declared `scope` are hashed with `git hash-object -w`, assembled into a
tree through a private index (`GIT_INDEX_FILE=$(git rev-parse
--absolute-git-dir)/review-loop/{uuid}.index` — resolved, never a hard-coded
`.git/`, because linked worktrees have a `.git` file), committed with
`git commit-tree` under an explicit helper identity, and pinned as
`refs/review-loop/{uuid}/snap/{n}`. The worktree, the user's index, `HEAD`,
and branches are untouched; the objects stay reachable while the ref exists.
Each snapshot entry records `n`, `ref`, `commit`, `tree`, `head`,
`recorded_at`, `untracked[]`, `deleted[]` (tombstones, incl. the old path of
a rename from `git status --porcelain=v2 -z`) and `renames[]`. Submodule
gitlinks are stored in the snapshot tree with mode `160000` and the commit
the submodule currently points to (its checked-out `HEAD`, else the index
pointer), so a pointer move participates in selectors, records, `classify`
and `delta` exactly like a blob change; a `dir:` selector also covers a
gitlink at exactly its directory path. Untracked content under
`.review-loop/` (the session directory) is never in scope, so session-file
writes never appear in `classify.changed_paths` or a delta; an untracked
nested repository (listed as `dir/` by `git ls-files --others`) is bound
like a gitlink to its checked-out `HEAD`, and one that cannot be bound is
listed under `nested_repos_skipped` in the snapshot output and persisted on
the snapshot entry; `classify` and `check` carry the latest entry's
`nested_repos_skipped` in their JSON output, so an unbound region is never
invisible. A checked-out submodule or nested repository with uncommitted or
untracked content (`git status --porcelain=v1 -z --untracked-files=all`
non-empty; ignored files excluded) is bound as
`160000:<commit>+dirty:<digest>` — sha256 over its sorted `(XY, path,
content oid)` entries, nested-nested repositories recursed with the same
rule up to 4 levels (deeper fails closed, exit 3). The digest is therefore
bounded by how it is built: `git status` inside the nested repository
selects the entries, and the digest binds their sorted `(XY, path, current
content oid)` triples with each oid read from disk. It therefore moves
whenever a selected path's `XY` or content changes, including a further edit
to a path porcelain already reports; mode and type reach it only insofar as
they change a path's `XY` or content oid; and a path status never selects
(`submodule.<name>.ignore` on a nested-nested submodule, `skip-worktree` /
`assume-unchanged`, a directory git could not open) is absent from it
entirely. Superproject paths are bound from disk with their own mode and are
unaffected.
Because a git tree
can only hold the plain commit, the digest is persisted per snapshot as
`gitlink_dirty: {path: digest}` on the snapshot entry and re-applied
whenever the tree is read, so records, selectors, `classify` and `delta`
all see the suffixed identity and a record bound to the plain
`160000:<commit>` of a now-dirty repository reads `changed input`. snap/0 is the
current verified worktree at initialization; the first round's delta is
snap/0 → snap/1. If a snapshot cannot be stored (read-only `.git`, sandbox),
the helper exits 3 and writes nothing: no record may be appended, every
claim stays `uncertain`, the direct author route is refused, and the
reviewer prompt states `delta: unattributable — reviewing worktree diff
against the last stored snapshot`. Refs are kept for audit;
`evidence_ledger.py prune --session <uuid>` deletes them only when the user
asks. `prune` stamps `pruned_at` on the ledger and on every snapshot entry
it has not stamped yet — driven by the ledger, not by the refs it found, so
a re-run after a failed session write still stamps (`refs_missing: true`)
and a prune with nothing left to stamp reports `already_pruned: true`.
While the ledger carries `pruned_at`, `record`, `classify`, `delta` and
`route` refuse every snapshot lookup with `snapshots were pruned at <when>;
run `snapshot` to store a new baseline` (exit 2); a new `snapshot` lifts the
ledger-level stamp, and an individual stamped entry stays unusable (a
`delta` naming it is refused the same way).

### Evidence record

```
id            monotonic integer, append order
claim_id      "<check>:<scope key>" — two records with the same claim_id are
              checks of the same claim
check         reviewer_approve | gate | tests | lint | static_analysis |
              simplify | docs_consistency | security_scan |
              agent_review:<name> | manual:<desc>
scope         sorted "|"-joined review-target paths (reviewer_approve, gate,
              agent_review:*, simplify); the command line (tests, lint,
              static_analysis); the literal `session` (docs_consistency,
              security_scan); the description (manual:<desc>)
stage         exec | polish | docs | security | n/a
disposition   executed | not-applicable | controlled-skip
result        PASS | FAIL — present iff disposition = executed
reason        verbatim reason — present iff disposition ≠ executed
head          HEAD sha at record time;  snapshot: n
inputs        {path: "<mode>:<blob_sha>" | "<deleted>" |
                     "<untracked:<mode>:<blob_sha>>"}
deps          {path: "<mode>:<blob_sha>" | ...} | "uncertain"
selectors[]   {kind: glob | dir | discovery, pattern, members_digest}
closure       declared | uncertain   (default uncertain)
env           {command, recorded_at} — required for tests, lint,
              static_analysis, security_scan, manual:*
freshness     {"kind": "ttl", "seconds": N} | {"kind": "bound-to-head"} | null
requires_freshness   true for manual:* and any record marked env-sensitive
assumptions[] unresolved[]
provenance    "fresh" | "reused-from:<id>"  (+ mandatory `why` for reuse)
author_route  executor | orchestrator-direct | n/a
supersedes[]  ids of earlier same-check records this record explicitly
              retired via `record --supersedes` (empty when none)
recorded_at   superseded_by: <id> | null  (implicit: a higher-id record of
              the same claim_id; explicit: the record whose --supersedes
              named this one)
```

- `not-applicable` is legal only for `static_analysis`; `controlled-skip`
  (the verbatim `adversarial-gate: SKIP reason=… detail=…` banner from
  `scripts/adversarial_gate_invoke.py`, or the orchestrator's
  `skipped-by-config`) only for `gate`. Neither is a PASS and neither ever
  renders as one; each satisfies its stage requirement only where the
  protocol already allows N/A or controlled SKIP. The helper rejects any
  other use at record time.
- Blob identities are the stored blobs of the bound snapshot, so staged,
  unstaged and untracked states hash the same bytes. Every identity carries
  the git mode (`100644`, `100755`, `120000`, `160000`), so a mode-only
  change — an executable-bit flip, a file ↔ symlink type change, a submodule
  pointer move — invalidates the record exactly like a content change; a
  legacy identity without a mode never matches and is non-reusable. A rename
  is the new path plus a `<deleted>` tombstone for the old path.
- `members_digest` = sha256 over the sorted `(path, mode, blob_sha)` triples
  matching `pattern` in the snapshot tree (tracked and in-scope untracked
  alike).
  Discovery-based checks must declare selectors, not just files: `tests`
  (`tests/**`, config files), `lint` (emitted by `evidence_ledger.py
  lint-closure`), `static_analysis` / `security_scan` (their roots), and
  `reviewer_approve` / `gate` / `agent_review:*` whenever the review target
  is a directory. A closure that cannot be stated as files + selectors is
  `uncertain`.
- A record that requires `freshness` and lacks it is **non-reusable**: it
  fails validity (never a schema error) and still counts as executed for the
  round that produced it.

### Active-record rule

Implemented in `evidence_ledger.py check`; deterministic.

1. For each `claim_id` the **active** record is the one with the highest
   `id`; every lower-`id` record for that claim is marked
   `superseded_by = <active id>` on the next ledger write and is never
   consulted for stage derivation. A record may also be **explicitly
   superseded across `claim_id`s**: `evidence_ledger.py record --supersedes
   <id>[,<id>...]` marks those earlier records of the **same `check`** as
   `superseded_by = <new id>` when the new record is written — the
   orchestrator does this when the review scope grows, so a stale
   earlier-scope claim cannot block a stage forever. A superseded record,
   implicit or explicit, is never consulted by `check` or `classify` and
   never becomes active again.
2. An active record is **valid** ⇔ (`disposition = executed ∧ result =
   PASS` ∨ `disposition ∈ {not-applicable, controlled-skip}` where
   permitted) ∧ `closure = declared` ∧ `deps ≠ "uncertain"` ∧ every
   `inputs` / `deps` entry is byte-identical to the current worktree
   (tombstones must still be absent; untracked blobs must still match) ∧
   every selector's recomputed `members_digest` matches (a member that newly
   matches, stops matching, is removed, or is renamed invalidates; no fixed
   path exceptions) ∧ `unresolved = []` ∧ freshness holds (`ttl` not expired
   against `recorded_at`; `bound-to-head` requires `head` = current `HEAD`).
3. **Failure dominance**: an active `FAIL` keeps the claim unsatisfied until
   a *new* record with a higher `id` and `result = PASS` is appended. A
   prior PASS can never satisfy a claim after a later FAIL, because it is
   superseded by construction. Dominance holds across scopes: any active
   `FAIL` of a required check blocks that check's stage whatever its scope
   key (a FAIL followed by another FAIL of the same `claim_id` leaves the
   newer FAIL active and the claim unsatisfied). **FAIL supersession
   guard**: a record whose `result` is `FAIL` may only be explicitly
   superseded by an **executed PASS** of the same `check` whose scope key
   **covers** the FAIL's scope key — for path-scoped checks
   (`reviewer_approve`, `gate`, `agent_review:*`, `simplify`) the new sorted
   path set must be a superset of the old one; for command / literal scopes
   it must be identical. Any other `--supersedes` target of a FAIL record,
   or a `--supersedes` naming a different `check`, a non-existent id, or an
   already-superseded id → `record` exits 2 and writes nothing.
4. A `FAIL`, an `uncertain` closure, an unresolved finding, or an expired
   freshness is never reused; `provenance = reused-from:<id>` is legal only
   when the source is the active record of the same claim and is currently
   valid under rule 2 (the helper rejects anything else).
5. Anything not covered by rules 1–4 → the claim is unsatisfied → relevant
   rerun. Prefer rerun over argument.

### Required claim sets and derived `completed_stages`

| Stage | Required checks (**every** active record of each check must be valid) |
|---|---|
| `exec` | `reviewer_approve` (executed PASS) + `gate` (executed PASS or `controlled-skip`) |
| `polish` | `static_analysis` (executed PASS or `not-applicable`), `agent_review:code-reviewer`, `agent_review:silent-failure-hunter`, `simplify`, `tests` |
| `docs` | `docs_consistency` |
| `security` | `security_scan` |

A stage is present iff **every active (non-superseded) record of each
required check is valid** under the active-record rule and in a permitted
satisfying state. There is no newest-record-per-check selection across
`claim_id`s: an active `FAIL` of a required check blocks the stage whatever
its scope, until it is superseded — implicitly by a higher-id record of the
same `claim_id`, or explicitly via `record --supersedes` under the FAIL
supersession guard. `completed_stages` is **derived** from the
ledger by `evidence_ledger.py check` at every write boundary — after any
Executor / orchestrator-direct / simplifier / doc / `.gitignore` write and
at drift check — and written into `## Session Metadata` by the helper; the
orchestrator never hand-edits it. The stage-add events in
[§`completed_stages` lifecycle](#completed_stages-lifecycle) describe *when*
the required records get recorded; presence is always the derived value.

### Convergence rule

After a write, `evidence_ledger.py classify` computes the changed-path set
(current worktree vs the last snapshot) and, for **every** active
`exec`-required record (not one per check), checks whether any changed path is in `inputs ∪ deps`,
whether a selector digest changed, or whether the record has `closure =
uncertain`. If **no** `exec`-required record is touched and all have
`closure = declared`, the delta is **non-invalidating for exec**; otherwise
`exec` is invalidated (helper exit 1). Positive proof only: there is no
prose-looking, extension, directory, or path-category rule. A later write
outside the declared `reviewer_approve` closure is non-invalidating **only
because** the orchestrator declared that closure complete at approval time;
if it cannot enumerate the closure it records `closure = uncertain`, and
every later write invalidates `exec`.

**When we rerun vs reuse.** A check is reused (recorded with
`provenance: reused-from:<id>` and a `why` sentence) only when its previous
record is currently valid: same bytes for every input, dependency and
selector member, closure declared, freshness intact, nothing unresolved.
Any other situation — a FAIL, a changed or deleted input, a new or renamed
member under a declared selector, an expired ttl, a moved `HEAD` for a
`bound-to-head` record, an `uncertain` closure — is a rerun; the rerun's
record supersedes the old one. An `exec`-invalidating delta starts a new
execution convergence (Step 3 round → independent Reviewer → Step 3.4 runs
once, unconditionally); a non-invalidating delta is a reviewer-only
incremental replay of the touched non-exec claims with no new convergence
and no gate. No line-count skip anywhere.

---

## Current Review Packet

`## Current Review Packet` sits directly after `## Approved Plan` and is
rewritten in full each round (planning rounds included). It is the
Reviewer's default input: reviewer prompts say "Read `## Current Review
Packet` first. Load a `## Review History` entry only when the packet
references it or a claim needs provenance. Absence of irrelevant history is
not a defect." Helpers locate the canonical packet by session structure,
never by the first matching heading: it is
the unique `## Current Review Packet` heading whose next `## ` heading is
`## Review History` (likewise `## Evidence Ledger` is the heading followed by
`## Session Metadata`, and `## Session Metadata` is the last heading); a
quoted heading inside
`## Approved Plan` or a history entry is never selected, and zero or more
than one candidate fails closed (`route` → `executor`; ledger / metadata
readers and writers → exit 2, nothing written).

Required fields — never truncated and never moved to history:

| Field | Content |
|---|---|
| Intent and acceptance criteria | full text |
| Binding decisions and authorization | full text of every user ruling still in force |
| Exact delta | `git diff --stat` (navigation only) **plus** `### Attributable Delta` |
| `### Attributable Delta` | table `snapshot pre \| snapshot post \| path \| pre_blob \| post_blob`; `pre` is the snapshot the previous packet was reviewed at (snap/0 for a first round), **never** `HEAD`; unrelated dirty paths never appear |
| Touched contracts / invariants | list |
| Evidence | table from `evidence_ledger.py check --format markdown`: ledger id, claim, disposition, result, fresh/reused, `why`, valid, reason |
| Unresolved findings + author response | list |
| Declared deviations | list |
| Risks / open questions | list |
| `### Route Facts` | execution rounds only: table `fact \| value \| rationale` — the six eligibility facts (`true \| false \| uncertain`) and nine sensitive flags (`true \| false`) per [execution.md §Author route selection](./execution.md#author-route-selection); `evidence_ledger.py route` reads this block |
| Author route | `executor` \| `orchestrator-direct`, exactly as returned by `evidence_ledger.py route` (always `executor` in a planning round) |

Boundedness is semantic, not a line cap: only *supporting* detail
(prior-round transcripts, superseded findings, resolved discussions) is
referenced into `## Review History` by entry id. The Attributable Delta is
materialized deterministically by `scripts/evidence_ledger.py delta
--session <uuid> --pre <n> --post <m>`, which runs `git diff` between the
two stored snapshot commits restricted to the scope paths and verifies each
hunk's pre/post blob against the table; hashes verify the patch, they are
not the patch. Findings are anchored to that materialized patch. For a
planning round the delta is the plan-text diff between rounds.

---

## Timing Log columns

```
| Phase | Round | Role | Duration | Dispatch | Reuse | Reads | Unchanged | Tests | Pause | Model | Tokens | Cost |
```

| Column | Value |
|---|---|
| `Duration` | wall-clock from `loop_state.timing.steps`; `N/A` when not recorded |
| `Dispatch` | `executor:n reviewer:n gate:n` for the row's step |
| `Reuse` | `reused:n rerun:n` plus each reused claim's ledger id and its `why` (from `evidence_ledger.py check`) |
| `Reads` | `## Review History` entries presented to the Reviewer this round / total entries (packet-referenced only) |
| `Unchanged` | review-scope paths whose pre and post snapshot blobs are identical (presented but absent from the Attributable Delta) |
| `Tests` | `rerun` or `reused` with `inputs_changed: true\|false` |
| `Pause` | `none` or the pause kind (`product \| risk \| scope \| authorization \| other`) |
| `Model`, `Tokens` | from Agent / reviewer metadata (`loop_state.token_usage`) when present, else `N/A` |
| `Cost` | USD from the same metadata (`total_cost_usd` of a `claude -p --output-format json` envelope, or an Agent-tool cost field) when present, else `N/A` |

On the first write under this header every existing four-column row is
rewritten with `N/A` in each new column; the table is always rectangular.
Never invent a value: any unmeasured cell is `N/A`. These columns are the
per-step overhead subset of the evaluation field set; per-case fields that
need seeded ground truth (`defects_found`, `defects_missed`,
`theoretical_complexity_added`, `blocking_rejected_or_downgraded`,
`stale_evidence_reused`) have no production column.
