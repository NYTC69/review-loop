# Split `plan` and `execute` into Independent Sub-Skills — Design

Date: 2026-04-17
Status: Approved (Codex design review, 10 rounds, final verdict APPROVE)

## Summary

Split the monolithic `review-loop` skill into three composable skills:

- `plan` — planning phase only; outputs an approved plan to a session file.
- `execute` — execution + quality polish + delivery; accepts three entry
  modes (`--session`, `--plan`, `--review-only`).
- `review-loop` — unchanged external UX; becomes a thin umbrella that reuses
  the same shared protocol docs.

Both Claude Code (plugin) and Codex (Stage 1) runtimes gain the new skills.
The shared `.review-loop/sessions/{uuid}.md` protocol is the cross-runtime
bridge: a user can run `plan` on one runtime and `execute --session` on the
other.

## Problem

`review-loop` today is end-to-end. In real work:

1. **Plan already exists**: the user discussed the plan elsewhere or wrote it
   manually; re-running the planning loop is wasted work.
2. **Large execution**: one plan naturally splits into several independently
   reviewed batches. The user wants control over batch size and review
   granularity instead of one giant CR.
3. **Pure CR on existing code**: code is already written (not via the loop);
   the user only wants iterative independent review + fix.

## Motivation for a shared-protocol-docs architecture

A first-pass design had the umbrella skill call the new sub-skills via a
"thin wrapper + inlined body" pattern. That is copy-paste by another name:
the plugin agent-type sandbox bug (documented in `CLAUDE.md`) prevents real
skill-to-skill invocation, so "inline the body" collapses into duplication.

The approved architecture instead factors the workflow rules out into
`docs/protocol/*.md` files. Each SKILL.md is small and declares a fixed
`## Protocol Imports` block listing the specific protocol files it depends
on. The orchestrator reads those files at start (no wildcard magic; explicit
per-skill enumeration). This is the only place the design deviates from a
classical "sub-command" model, and it exists because the Claude Code runtime
has no skill-to-skill call primitive.

## Architecture

### Protocol documents (single source of truth)

New files under `docs/protocol/`:

- `session-file.md` — session-file schema, canonical sections, init rules for
  `--session` / `--plan` / `--review-only`, lock lifecycle, moving baseline,
  dirty-map XY construction, drift-check decision tree, `completed_stages`
  lifecycle, `delivery_blocked_by` lifecycle, `--accept-external-state`
  semantics.
- `planning.md` — planning-phase protocol; mirrors today's Step 2.
- `execution.md` — execution + 3.5 polish + 3.6 docs + 3.7 security + 4
  delivery; `--stop-after` enum + Codex Stage 1 subset; provenance-aware
  reviewer prompt rules; `--review-only` first-round-skips-Executor rule.
- `executor-output.md`, `reviewer-output.md` — schemas extracted from the
  current SKILL.md files.

### Per-skill imports

| SKILL | Protocol Imports |
|---|---|
| `{skills,.agents/skills}/plan/SKILL.md` | session-file, planning, executor-output, reviewer-output |
| `{skills,.agents/skills}/execute/SKILL.md` | session-file, execution, executor-output, reviewer-output |
| `{skills,.agents/skills}/review-loop/SKILL.md` | all four |

Protocol docs are runtime-agnostic where possible. Runtime-specific bits
(Executor/Reviewer dispatch, sandbox-bug workarounds, Codex Stage 1 scope
limits) live only in each runtime's SKILL.md, not in the protocol docs.

## Session file

All entry points share one canonical section list. The `## Approved Plan`
section always exists; only its `Source` and body change:

| Source | When | Body | Code-reviewer behavior |
|---|---|---|---|
| `reviewer-approved` | `plan` loop approved | reviewer-approved text | strict plan-conformance |
| `user-supplied` | `execute --plan` | user's free-form plan text | plan-conformance flagged as MINOR/advisory |
| `review-only` | `execute --review-only` | canonical sentinel `(none — review-only mode)` | pure CR mode, no plan conformance |

`--review-only` additionally populates a non-canonical `## Review Target`
section describing the scope. Other entries leave it empty.

Canonical sections (unchanged from today except for the `Source` sub-field):
`Problem Description`, `Context`, `Acceptance Criteria`, `Current Phase`,
`Approved Plan`, `Review History`, `Files Changed`, `Key Related Files`,
`Timing Log`, `Session Metadata`.

### Session Metadata schema

```
- entry_point: plan | execute-from-session | execute-from-plan | review-only | review-loop
- plan_source: reviewer-approved | user-supplied | review-only
- base_head: <sha at session creation>
- base_dirty: { "<path>": "<blob_sha>" | "<deleted>", ... }
- last_verified_head: <sha>
- last_verified_dirty: { "<path>": "<blob_sha>" | "<deleted>", ... }
- session_commits: [<sha>, ...]   # commits authored inside this session (auto_commit), append-only
- completed_stages: [<stage>, ...]    # currently-valid validations; see stages section
- delivery_blocked_by: <stage> | "user-abort" | null
```

## Dirty map and drift check

### Dirty map construction

`git status --porcelain=v1 -z` output is normalized into a
`{path: hash_or_tombstone}` map. Rules are evaluated in order; first match
wins. `hash_worktree(p) = git hash-object <p>`;
`hash_index(p) = git ls-files -s <p>` third column.

1. **Unmerged** (`X` or `Y` == `U`, or combinations `AA`/`DD`) → hard error;
   user must resolve conflicts first.
2. **Untracked** (`XY == "??"`) → `{<path>: hash_worktree(<path>)}`.
3. **Rename/copy** (`X in {R,C}` or `Y in {R,C}`): two entries per record —
   `<old>` (rename only; copy's `<src>` gets no entry) and `<new>`.

   | Condition | `<old>` | `<new>` |
   |---|---|---|
   | `Y == 'D'` | `"<deleted>"` | `"<deleted>"` |
   | `Y in {'M','T','A','R','C'}` | `"<deleted>"` | `hash_worktree(<new>)` |
   | `Y == ' '` (staged-only) | `"<deleted>"` | `hash_index(<new>)` |

4. **Y == 'D'** (non-R/C `*D`: ` D/MD/AD/TD`) → `{<path>: "<deleted>"}`.
5. **Y in {'M','T','A'}** → `{<path>: hash_worktree(<path>)}`. Type changes
   are naturally covered by the new blob hash.
6. **Y == ' '** and `X in {'M','A','T'}` → `{<path>: hash_index(<path>)}`.
7. **Y == ' '** and `X == 'D'` → `{<path>: "<deleted>"}`.
8. **Otherwise** → hard error (unknown git state; user verifies environment).

### Drift-check decision tree

Run at the start of every `execute --session` / batch:

1. Compute `current_head` and `current_dirty` per rules above.
2. **HEAD branch**:
   - `current_head == last_verified_head` → goto 3.
   - `current_head` is a descendant of `last_verified_head` AND every commit
     in `last_verified_head..current_head` is in `session_commits` →
     session-owned progress; goto 3.
   - Otherwise → `drift_reason: external-head`; goto 4.
3. **Dirty-map re-verification** (always runs). For each path in
   `current ∪ last_verified_dirty`:
   - Same value on both sides → OK.
   - Different values (hash/tombstone mismatch) → `content-or-deletion-changed`; goto 4.
   - Only in `current` → `newly-dirty`; goto 4 (always external drift; this
     closes the revert/add-back loophole).
   - Only in `last_verified_dirty`, `current_head` unchanged →
     `reverted-externally`; goto 4. (If `current_head` moved forward, the
     file may have been committed — step 2 path b already validated that.)
4. **Drift handling**: print `drift_reason` + diff detail, prompt:

   ```
   (A) Accept drift and reset baseline to current state
       (clears completed_stages entirely, including exec)
   (B) Abort
   ```

   Handsfree mode still blocks (external fact, not a design decision). The
   `--accept-external-state` opt-in flag auto-picks A; flag is documented as
   unsafe.
5. **On clean batch exit**: update `last_verified_head`,
   `last_verified_dirty`, append any `auto_commit` sha to `session_commits`;
   set `delivery_blocked_by` per exit path (see below).

## Single-writer lock

`.review-loop/sessions/{uuid}.lock` is scoped to one orchestrator invocation
(not to delivery). Lifecycle:

- Created on orchestrator start. Content: `pid`, `started_at`, `entry_point`,
  `stop_after`.
- Removed on any clean exit: delivery success, `--stop-after` stop, user
  abort (signal), unrecoverable error (trap).
- Start-time check: no lock → proceed; lock present + PID alive → refuse;
  lock present + PID dead → prompt to recover.
- Lock and moving baseline are orthogonal: lock guards concurrency now;
  baseline distinguishes session-owned progress from external drift over time.

## Skill interfaces

### `plan`

```
run plan on: <work item description> [--handsfree]
```

Executes Steps 0 → 0.5 → 1 → 1.5 → 1.6 → 2 (planning loop). Step 1.5, on
detecting an existing plan or pre-implemented code, suggests using `execute`
and exits rather than auto-routing. On approval it prints the UUID and a
"next: run review-loop:execute --session {uuid}" hint. Does not proceed into
execution.

### `execute`

Mutually exclusive entry modes:

```
run execute: --session <uuid> [--stop-after <stage>] [--handsfree] [--accept-external-state]
run execute: --plan <text|path> --title <...> [--description <...>] [--handsfree]
run execute: --review-only [--description <what was done>] [--handsfree]
```

Mode behavior:

- `--session`: acquire lock → drift check → reviewer strictness follows
  `plan_source` → execution loop (single batch or multi-batch).
- `--plan`: create session, `plan_source: user-supplied`, inject user text
  into `Approved Plan`, record base snapshot, enter execution.
- `--review-only`: create session with the `review-only` sentinel in
  `Approved Plan`, populate `## Review Target`, base snapshot from current
  `HEAD` + dirty set, `session_commits: []`. **Skip the first Executor
  round** and go straight to Reviewer; later rounds follow the standard
  CR→fix loop.

Quality Polish / Docs / Security / Delivery: Claude Code runs Steps 3.5 /
3.6 / 3.7 / 4 in full; Codex Stage 1 runs Step 3 + Step 4 only (Stage 1
scope excludes polish/docs/security).

### `--stop-after <stage>`

Legal values (full set): `exec-round`, `before-polish`, `before-docs`,
`before-security`, `before-delivery`, `delivery` (default).

Runtime-supported subsets:
- Claude Code: full set.
- Codex Stage 1: `exec-round`, `before-delivery`, `delivery`.

Unsupported-on-runtime values are rejected at Step 0 flag parsing, before any
lock is acquired or session field is written.

### `review-loop` (unchanged UX)

Keeps Step 1.5 auto-routing (plan-exists / code-exists / fresh) and
`--handsfree` dispatch, but imports the same four protocol docs. Writes
`entry_point: review-loop`. The SKILL.md shrinks from ~1100 lines to ~250.

## Stages and `completed_stages`

`completed_stages` represents validations that hold **for the current
tree+index state**. It is not a historical log.

### When stages are added

- `exec`: one Executor+Reviewer cycle returned `APPROVE` for the current
  state. In `--review-only` mode a reviewer-only `APPROVE` on the existing
  diff also mints `exec` (no Executor runs by design).
- `polish`: Step 3.5 completed without unresolved issues.
- `docs`: Step 3.6 completed.
- `security`: Step 3.7 completed.

### Invalidation rules (any of these clears ALL entries)

- New Executor round starts.
- Drift accepted (decision tree step 4 → A).
- Old-session baseline backfill accepted.
- Step 3.5 polish substep writes code (simplify / executor-fix /
  test-consolidation).
- Step 3.6 docs write (code or stale-comment fix).
- Step 3.7 security preflight writes `.gitignore` or causes
  `git rm --cached`.

After clearing, the orchestrator **replays from `exec`** in runtime order
until the runtime-supported set is present again. Each replay iteration that
writes files clears the set and restarts from `exec`. Termination is
guaranteed by the per-stage max-round caps below.

### Per-stage caps (explicit for self-contained termination proof)

- Step 3 exec: `soft_limit_exec` (default 3). On cap with CRITICALs remaining
  → ask user (handsfree: auto hard-stop).
- Step 3.5.2 static analysis fix: max 2 rounds.
- Step 3.5.3 code-reviewer / silent-failure-hunter: max 3 rounds.
- Step 3.5.4 simplify: single pass, not looped.
- Step 3.5.5 test consolidation fix: max 2 rounds.
- Step 3.6 docs: single pass.
- Step 3.7 security: single scan.

### Hard-stop and `delivery_blocked_by`

When any stage hits its cap with unresolved findings:
- The stage is NOT added to `completed_stages` (invariant: "only clean passes
  in the set").
- Orchestrator hard-stops: prints stuck summary, updates baseline to current
  state, sets `delivery_blocked_by ← <stage>`. Does not deliver.
- Unrecoverable errors are separate: they preserve `last_verified_*` and
  `delivery_blocked_by` unchanged so the user can audit.

`delivery_blocked_by` lifecycle:
- Set by hard-stop (`<stage>`) or signal abort (`"user-abort"`).
- Cleared by delivery success, by a clean `--stop-after` exit (even if
  resuming from a previously blocked state — the user already acknowledged),
  and by the user's resume-continue choice (see below).

Resume from a non-null `delivery_blocked_by`:
- Prompt the user with the previous block reason: continue or abort.
- On continue: immediately clear `delivery_blocked_by ← null`, then run the
  standard drift check. The user's in-place fix will show up as drift
  (content hashes changed). Accepting the drift clears
  `completed_stages` and replay restarts from `exec`. This is the safe
  default: preserving intermediate stages across a user fix would contradict
  the "validations hold for current state" invariant.
- On abort: clear `delivery_blocked_by`, leave `completed_stages` alone.

## Delivery gate

Step 4 enters only when `runtime_supported_set ⊆ completed_stages`. Claude
Code needs `{exec, polish, docs, security}`; Codex Stage 1 needs `{exec}`.
Because invalidation + replay guarantees entries only exist when valid for
the current state, the gate needs no separate final reviewer pass.

## Cross-runtime handling

Protocol docs are runtime-agnostic. Runtime-specific implementation lives
only in each runtime's SKILL.md:

- Claude Code: `Agent` tool with `subagent_type: general-purpose` + inlined
  agent bodies (workaround for the plugin agent-type sandbox bug).
- Codex: Codex subagents via fresh self-contained prompts;
  `claude -p --no-session-persistence` default reviewer with Codex fallback;
  Stage 1 excludes polish/docs/security.

The shared session file is the only cross-runtime contract. A user can run
`plan` on one runtime and `execute --session {uuid}` on the other.

**Codex Stage 1 scope expansion** is disclosed honestly: the Stage 1 scope
list in `.agents/skills/review-loop/SKILL.md` is updated to
`review-loop`, `plan`, `execute`, `guide`. This is a surface expansion, not
an internal reorg.

## Backward compatibility

Old sessions missing the new metadata fields degrade as follows:

| Missing field | Behavior |
|---|---|
| `plan_source` | Treated as `reviewer-approved` (strict default). |
| baseline quintet (`base_head`, `base_dirty`, `last_verified_head`, `last_verified_dirty`, `session_commits`) | **Pause and prompt** the user: "Accept current repo state as new baseline?" On A: backfill, clear all `completed_stages`. On B: abort. Handsfree blocks; `--accept-external-state` auto-picks A. |
| `completed_stages` | Empty set; start from Step 3. |
| `delivery_blocked_by` | Treated as `null` (normal resume). |

No silent backfill. No silent adoption of external drift.

## Non-goals

- No changes to Executor / Reviewer / quality agent bodies.
- No changes to reviewer output schema.
- No new reviewer backend.
- No change to the umbrella `review-loop` external UX.

## Open risks

- **R1 — protocol doc drift**: if a SKILL.md fails to `Read` its imports,
  behavior silently regresses. Mitigation: fixed `## Protocol Imports` list
  per skill + smoke assertions on orchestrator Read events.
- **R2 — drift-check noise**: manual rebase/commit/stash during multi-batch
  or `--review-only` triggers drift prompts. Mitigation: drift is an "ask",
  not a "block"; user can accept-and-reset; `session_commits` suppresses
  false positives on legitimate auto-commits.
- **R3 — Codex Stage 1 surface expansion**: disclose explicitly in guide,
  README, and tests; do not claim "internal reorg only".
- **R4 — stage-invalidation bugs**: strict "only clean pass" semantics and
  hard-stop on cap prevent stale validations. Smoke cases must cover:
  "polish stabilizes after consecutive writes then delivers", "polish hits
  cap → hard-stop → user fix → resume → deliver", "security writes
  `.gitignore` → replay from `exec`".

## Design review history

10 rounds of independent design review against this spec's predecessor
document (`tasks/todo.md`), using `codex exec -s read-only`:

| Round | Verdict | CRITICAL | MINOR |
|---|---|---|---|
| 1 | REQUEST_CHANGES | 4 | 2 |
| 2 | REQUEST_CHANGES | 2 | 2 |
| 3 | REQUEST_CHANGES | 2 | 1 |
| 4 | REQUEST_CHANGES | 2 | 1 |
| 5 | REQUEST_CHANGES | 2 | 1 |
| 6 | REQUEST_CHANGES | 3 | 1 |
| 7 | REQUEST_CHANGES | 2 | 1 |
| 8 | REQUEST_CHANGES | 2 | 0 |
| 9 | REQUEST_CHANGES | 2 | 0 |
| **10** | **APPROVE** | **0** | **0** |

Key concerns resolved across the rounds: plan-provenance sentinel (v2);
umbrella auto-routing preserved + protocol-docs factoring (v3); moving
baseline with content hashes + `session_commits` ledger (v5-v6); XY dirty
map with tombstones + rename/copy coverage (v7-v9); `completed_stages`
redefined as "current-state validations" with strict invalidation + replay
(v7-v10); hard-stop lifecycle and `delivery_blocked_by` typing (v9-v10);
backward-compat fallback pauses instead of silent backfill (v4).

**Post-approval revision (2026-04-17)**: hallucination-guard smoke
extracted from Phase 2 into a separate Phase 2.1 ship unit due to
runner-infrastructure scope. The SKILL split + behavioral smoke
(Phase 2) ships first. Phase 2.1 follows before Phase 3 Codex
cross-runtime work. No runtime behavior change.
