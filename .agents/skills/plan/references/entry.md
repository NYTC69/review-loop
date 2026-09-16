# codex plan entry procedures

Read selected sections through `scripts/read_protocol.py`; shared protocol rules take precedence.

## Step 0 — Load config and parse flags


1. Read `.review-loop/config.md` if present; otherwise fall back to
   Stage 1 defaults documented in `docs/protocol/runtime-codex.md`
   §Config Loading.
2. Detect `--handsfree` in the invocation message. If present (or
   `handsfree: true` in config), enable handsfree for this session.
3. Default reviewer behavior in Codex Stage 1 keeps review on the
   outside-sandbox Claude CLI reviewer path. If
   `codex_reviewer_backend: codex` is set, use the local Codex reviewer
   directly. Do not auto-fall back from the Claude path to the local
   Codex reviewer.

## Step 0.5 — Initialize session file

1. Generate a lowercase UUID.
2. Acquire the single-writer lock per
   `docs/protocol/session-file.md` §Lock file lifecycle. The lock must
   exist before the session file is created; all subsequent reads and
   writes of the session file (creation, metadata write, planning round
   updates, and Step 1.5 suggest-and-exit cleanup) happen under this
   lock.
3. Under the lock, create `.review-loop/sessions/{uuid}.md` with the
   canonical section list per
   `docs/protocol/session-file.md` §Canonical sections, using the
   `plan` entry-mode column of §Entry-mode initialization table.
   - `## Approved Plan` body is empty; no `Source` sub-field is written
     during planning draft rounds.
   - `## Draft Plan` is present; it will be overwritten by each planning
     round's Executor output.
   - `## Current Phase: planning`.
   - `## Current Review Packet` (empty until round 1) and
     `## Evidence Ledger` are part of the canonical list; the Timing Log
     header has the 13 columns of `docs/protocol/session-file.md`
     §Timing Log columns.
4. Under the lock, write the initial `## Session Metadata` block.
   `entry_point: plan`. `plan_source` is omitted during planning draft
   rounds — it is written on APPROVE only.
5. Run `python3 scripts/evidence_ledger.py snapshot --session {uuid}`
   outside the Codex sandbox to store snap/0 (the only way
   `## Evidence Ledger` is ever written).
6. Tell the user the session path so they can inspect it.

## Step 1 — Parse the work item

Extract from the user's message: title, problem description, context,
acceptance criteria. If critical information is missing, ask ONE
clarifying question before proceeding.

## Step 1.5 — Detect pre-existing state (no auto-dispatch)

`plan` does not auto-route into execution. If the work item looks like
it should skip the planning loop, print a suggestion and exit instead
of dispatching — the user is the one who chose the `plan` entry point,
and the hand-off is their call.

Check:

- **Plan already exists** (the user's message says "review this", points
  at an already-written plan doc, or the conversation shows an approved
  plan): suggest
  ```
  Detected: existing plan in the conversation/context.
  Next:  review-loop:execute --plan "<text|path>" --title "<title>"
         (or review-loop:execute --session <uuid> if you already have a session)
  ```
- **Code already implemented** (`git status` shows substantial,
  task-relevant changes): suggest
  ```
  Detected: implementation appears to already be in the working tree.
  Next:  review-loop:execute --review-only --description "<what was done>"
  ```
- Neither → proceed to Step 1.6.

Print the suggestion and exit. Do not dispatch Executor / Reviewer.
Before exiting on either suggest-and-exit branch, release the
single-writer lock acquired in Step 0.5 per
`docs/protocol/session-file.md` §Lock file lifecycle.

## Step 1.6 — Historical context retrieval (optional, fail-silently)

This step is strictly optional. Skip it entirely and silently if no
external memory tool is available. Never ask the user to install
anything. Never mention the tool name to users who don't have it. The
fail-silently contract applies to the entire lifecycle — probe failure,
runtime failure, malformed output — per `CLAUDE.md` §"Optional
integrations must fail silently".

1. Availability probe: check if a `mempalace_search` MCP tool is listed,
   OR run `which mempalace`. If neither, skip.
2. Resume dedup: if the session file already carries a
   `## Historical Context` section, skip.
3. Extract 1-2 specific search terms from the work item.
4. Call the memory tool with a 10-second timeout; kill the probe at the
   deadline rather than awaiting it. Any error / hang / timeout /
   non-zero exit / stderr / malformed output → silently skip.
5. If the top results parse cleanly, append up to 3 bullets under a
   `## Historical Context` section. Otherwise skip — no empty section.

## Step 3 — Exit with hand-off hint

After the Reviewer returns `APPROVE`:

1. The session file now has `## Approved Plan` populated with
   `- Source: reviewer-approved` and `## Session Metadata.plan_source:
   reviewer-approved`. `## Draft Plan` has been removed.
2. Release the lock per `docs/protocol/session-file.md` §Lock file
   lifecycle.
3. Print the delivery hand-off:

   ```
   ── review-loop:plan — approved ──────────────────
   Session: {uuid}
   Session file: .review-loop/sessions/{uuid}.md
   Plan rounds: {N}
   Status: Approved (plan_source: reviewer-approved)

   Next: review-loop:execute --session {uuid}
   ────────────────────────────────────────────────
   ```

The `plan` skill does not deliver code and does not enter any execution
stage. `completed_stages` is not minted here — that is strictly the
`execute` skill's responsibility. No auto-dispatch.

---
