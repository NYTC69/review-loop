# codex execute entry procedures

Read selected sections through `scripts/read_protocol.py`; shared protocol rules take precedence.

## Step 0 — Parse and validate flags


Execute before any lock or session write.

1. **Entry-mode mutual exclusion**: count how many of `--session`,
   `--plan`, `--review-only` are present. If ≠ 1 → print usage and
   exit with non-zero.
2. **`--stop-after <stage>`**: validate against the Codex Stage 1
   supported set per `docs/protocol/execution.md`
   §Runtime-supported subsets:

   - `exec-round`
   - `before-polish`
   - `before-docs`
   - `before-security`
   - `before-delivery`
   - `delivery` (default when flag is absent)

   Codex Stage 1 accepts every value listed above for `--stop-after`;
   `before-polish`, `before-docs`, and `before-security` are the most
   common request points and are highlighted here for that reason.
   `exec-round` and `before-delivery` are also accepted. `delivery` is
   the default no-early-stop value (run through delivery), not a stop
   point.

   Any other value → reject at parse time. Error message must list the
   supported subset. Do not create the lock, do not touch the session
   file.

3. **`--handsfree`**: enable handsfree mode for this invocation.
   Handsfree alone does NOT auto-accept drift — see
   `--accept-external-state`.
4. **`--accept-external-state`**: unsafe opt-in. Auto-selects "(A)
   accept" wherever `docs/protocol/session-file.md` instructs the
   Orchestrator to pause-and-confirm (drift check step 4; backward-compat
   missing-baseline fallback). The flag has no effect outside those two
   prompts — it does not bypass unmerged-conflict errors, unknown
   git-state errors, or per-stage hard-stops.
5. **Config load**: read `.review-loop/config.md` if present; otherwise
   use Stage 1 defaults documented in
   `docs/protocol/runtime-codex.md` §Config Loading.
6. **Reviewer backend resolution**: default Stage 1 keeps review on the
   outside-sandbox Claude CLI reviewer path. If
   `codex_reviewer_backend: codex` is set, use the local Codex reviewer
   directly. Do not auto-fall back from the Claude path to the local
   Codex reviewer.

## Step 0.5 — Resolve target UUID (no writes yet)

Compute the session UUID so the lock path is known; do not read or
write the session file yet — the single-writer lock must come first per
`docs/protocol/session-file.md` §Lock file lifecycle.

- `--session <uuid>`: adopt the UUID the user supplied. Confirm the
  path is well-formed (`.review-loop/sessions/{uuid}.md`). Do not Read
  the file content yet.
- `--plan <text|path>`: generate a fresh lowercase UUID.
- `--review-only`: generate a fresh lowercase UUID.

Flag parsing (Step 0) and `--stop-after` validation have already
completed; those steps are intentionally pre-lock.

## Step 1 — Acquire the single-writer lock

Per `docs/protocol/session-file.md` §Lock file lifecycle. Every
subsequent read and write of the session file (creation, resume-time
re-baselining, round updates) must happen under this lock.

- `.review-loop/sessions/{uuid}.lock` — PID, ISO-8601 `started_at`,
  `entry_point`, `stop_after`.
- No lock → proceed. Lock present + PID alive → refuse. Lock present +
  PID dead → prompt-to-recover.
- Release on every clean exit path (delivery, `--stop-after` stop,
  signal abort trap, unrecoverable error trap).

## Step 1.5 — Initialize or resume the session (under the lock)

Per the entry-mode initialization table in
`docs/protocol/session-file.md` §Entry-mode initialization table. All
reads and writes below happen after Step 1 acquired the lock.

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
   - `## Context` = "User-supplied plan; no planning-phase context
     captured." plus any `--description`.
   - `## Acceptance Criteria` = "Implementation matches the
     user-supplied plan in ## Approved Plan."
2. Write `## Session Metadata`:
   - `entry_point: execute-from-plan`
   - `plan_source: user-supplied`
   - Fresh baseline quintet (`base_head`, `base_dirty`, etc.) from
     current repo state.
3. This mode drives provenance-aware reviewer behavior: during
   execution rounds the reviewer's plan-conformance deviations are
   advisory / MINOR per `docs/protocol/execution.md` §Provenance-aware
   reviewer prompts, `plan_source: user-supplied` block. Correctness +
   intent-alignment are still enforced strictly.

### Mode: `--review-only`

1. Create `.review-loop/sessions/{uuid}.md` with the `--review-only`
   column of the init table:
   - `## Approved Plan` → `- Source: review-only` followed by the
     two-line canonical sentinel exactly as documented in
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
3. The execution loop skips the first Executor round per
   `docs/protocol/execution.md` §`--review-only` first-round skip.

## Step 2 — Drift check

Per `docs/protocol/session-file.md` §Drift-check decision tree (5
steps). For `--plan` and `--review-only` fresh sessions, the freshly
written baseline equals current state so steps 2-3 pass cleanly. For
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
`delivery_blocked_by`: prompt continue-or-abort with the previous
block reason; on continue, clear `delivery_blocked_by ← null` and then
run the standard drift check.
