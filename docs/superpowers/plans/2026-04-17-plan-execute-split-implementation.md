# Plan-Execute Split Implementation Plan

> **For agentic workers:** Use the `review-loop:review-loop` skill (Plan → Execute → Review iteration) to implement each phase. Phases are self-contained ship units. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the monolithic `review-loop` skill into three composable skills (`plan`, `execute`, `review-loop`) backed by shared protocol documents, for both Claude Code (plugin) and Codex (Stage 1) runtimes. Shared `.review-loop/sessions/{uuid}.md` remains the cross-runtime bridge.

**Design spec:** `docs/superpowers/specs/2026-04-17-plan-execute-split-design.md` (Approved, Codex review round 10).

**Architecture:** Four new protocol docs under `docs/protocol/` act as the single source of truth for planning/execution/session-file/schema rules. Each SKILL.md (6 total across runtimes) declares a fixed `## Protocol Imports` list; the orchestrator reads those files at start. Runtime-specific SKILL.md wrappers only carry dispatch details, sandbox-bug workarounds, and Stage 1 scope limits.

**Tech stack:** Markdown protocol docs; SKILL.md frontmatter + body; shell-based session-file IO; Bash for git/porcelain dirty-map construction; existing smoke-case framework under `tests/`.

## Phases

Ship order (each phase is an independent ship unit with its own exit criteria):

1. **Phase 1** — Shared protocol docs under `docs/protocol/` (shipped in `e853d53`).
2. **Phase 2** — SKILL split (`skills/plan`, `skills/execute`, umbrella refactor) + behavioral smoke cases, version bump, README + CLAUDE.md updates. No runner changes.
3. **Phase 2.1** — Hallucination-guard smoke runner extension: `scripts/run-skill-smoke` gains `--output-format json` support, streaming `tool_use` event capture, and a new assertion kind that proves each `## Protocol Imports` file was actually Read. Separate ship unit because it is purely runner infrastructure and is not a prerequisite for Phase 2 to land.
4. **Phase 3** — Codex side (`.agents/skills/plan`, `.agents/skills/execute`, umbrella refactor) + Codex smoke cases.
5. **Phase 4** — Cross-runtime integration verification.

---

## File Structure

### New files (Phase 1 — protocol docs)

- `docs/protocol/session-file.md`
  - Canonical section schema + `Approved Plan` three-source content rules + sentinel literal + supplemental `## Review Target`.
  - Init tables for `--session` / `--plan` / `--review-only`.
  - Lock file lifecycle (tied to orchestrator invocation).
  - Moving baseline: `base_head`, `base_dirty`, `last_verified_head`, `last_verified_dirty`, `session_commits`.
  - Dirty-map XY construction rules (8 branches; `Y`-dominant).
  - Drift-check decision tree (5 steps).
  - `completed_stages` invalidation + replay rules.
  - `delivery_blocked_by` lifecycle.
  - `--accept-external-state` semantics.
  - Backward-compat fallback (pause-and-confirm on missing baseline).
- `docs/protocol/planning.md`
  - Current Step 2 content, runtime-agnostic.
  - Executor/Reviewer dispatch rules with `{{claude_code|codex}}` placeholders where runtimes diverge.
- `docs/protocol/execution.md`
  - Current Steps 3 / 3.5 / 3.6 / 3.7 / 4 content.
  - `--stop-after` enum + Codex Stage 1 supported subset + parse-time rejection.
  - Provenance-aware reviewer-prompt rules for the three `Source` values.
  - `--review-only` first-round-skips-Executor rule.
  - Per-stage max-round caps.
- `docs/protocol/executor-output.md`
- `docs/protocol/reviewer-output.md`
  - Extracted verbatim from current SKILL.md bodies.

### New files (Phase 2 — Claude Code side)

- `skills/plan/SKILL.md` — planning phase only; imports session-file + planning + executor-output + reviewer-output.
- `skills/execute/SKILL.md` — three entry modes (`--session` / `--plan` / `--review-only`), `--stop-after`, `--accept-external-state`; imports session-file + execution + executor-output + reviewer-output.
- Phase 2 smoke cases under `tests/skills/smoke/`:
  - `plan.fresh.claude.json`
  - `execute.session-resume.claude.json`
  - `execute.from-plan.claude.json`
  - `execute.review-only.claude.json`
  - `execute.stop-after-polish.claude.json`
  - `review-loop.regression.claude.json`

### New files (Phase 3 — Codex side)

- `.agents/skills/plan/SKILL.md` — mirrors plan skill, Codex dispatch conventions.
- `.agents/skills/execute/SKILL.md` — mirrors execute skill, Stage 1 scope (exec + delivery only).
- Codex smoke cases:
  - `plan.fresh.codex.json`
  - `execute.session-resume.codex.json`
  - `execute.from-plan.codex.json`
  - `execute.review-only.codex.json`

### Modified files

- `skills/review-loop/SKILL.md` — shrink from ~1100 lines to ~250; keep Step 1.5 auto-routing + `--handsfree`; import four protocol docs.
- `.agents/skills/review-loop/SKILL.md` — same refactor; update `## Stage 1 Scope` list to include `plan`, `execute`.
- `skills/guide/SKILL.md` — document three entry modes and usage examples.
- `.agents/skills/guide/SKILL.md` (if exists) — same.
- `.claude-plugin/plugin.json` — bump version to `2.6.0`.
- `.claude-plugin/marketplace.json` — bump version to `2.6.0`.
- `README.md` — document new skills and entry modes.
- `CLAUDE.md` — update Codex Stage 1 Notes section with new scope list.

---

## Phase 1 — Shared protocol docs (precondition; docs-only ship)

- [ ] Write `docs/protocol/session-file.md` with all sections listed under "File Structure / Phase 1".
- [ ] Write `docs/protocol/planning.md` by extracting Step 2 from the current `skills/review-loop/SKILL.md`. Replace runtime-specific Executor/Reviewer call templates with `{{claude_code|codex}}` placeholders plus a runtime-specific resolution note.
- [ ] Write `docs/protocol/execution.md` by extracting Steps 3–4, adding the `--stop-after` enum + Codex subset, provenance-aware reviewer prompt variations, `--review-only` first-round skip rule, and per-stage caps.
- [ ] Write `docs/protocol/executor-output.md` (copy from current Executor Output Schema).
- [ ] Write `docs/protocol/reviewer-output.md` (copy from current Reviewer Output Schema).
- [ ] Cross-check: every rule from the design spec lands in exactly one protocol doc; no duplication across protocol docs.
- [ ] Commit as a docs-only change (no runtime behavior yet).

**Exit criteria:** protocol docs exist, internally consistent, and cover every rule referenced by the design spec. No SKILL.md yet references them (that's Phase 2+).

---

## Phase 2 — Claude Code side (complete ship unit)

- [ ] Create `skills/plan/SKILL.md`:
  - Frontmatter `name: plan`, `description`, `argument-hint: "<work item description> [--handsfree]"`.
  - `## Protocol Imports` block listing the four protocol files.
  - Body: Steps 0 / 0.5 / 1 / 1.5 / 1.6 / 2; Step 1.5 detects pre-existing plan or code and suggests `execute --session` / `--review-only` instead of auto-routing; Step 2 follows `docs/protocol/planning.md`. Exit with session UUID + next-step hint after approval. Write `entry_point: plan`.
- [ ] Create `skills/execute/SKILL.md`:
  - Frontmatter `name: execute`, `description`, `argument-hint`.
  - `## Protocol Imports` block.
  - Body: three entry modes (`--session` / `--plan` / `--review-only`), `--stop-after` with parse-time validation (full set on Claude Code), `--accept-external-state` opt-in.
  - Implement lock acquire + drift check + stage replay + hard-stop semantics per `docs/protocol/session-file.md` and `docs/protocol/execution.md`.
- [ ] Refactor `skills/review-loop/SKILL.md`:
  - Keep frontmatter and external UX.
  - Replace the protocol body with `## Protocol Imports` block + Step 1.5 auto-routing + `--handsfree` dispatch, delegating actual phase execution to the protocol docs.
  - Write `entry_point: review-loop`.
  - Target size: ~250 lines.
- [ ] Update `skills/guide/SKILL.md`:
  - Document the three skills (`plan`, `execute`, `review-loop`) and when to pick which.
  - Show example sessions: fresh plan → execute multi-batch → delivery; review-only pipeline.
- [ ] Add Phase 2 smoke cases under `tests/skills/smoke/`:
  - `plan.fresh.claude.json`: runs `plan`, asserts session file has `plan_source: reviewer-approved` and Approved Plan populated.
  - `execute.session-resume.claude.json`: seeds an approved session, runs `execute --session <uuid>`, asserts delivery and final `completed_stages` covers runtime set.
  - `execute.from-plan.claude.json`: runs `execute --plan <text>`, asserts `plan_source: user-supplied` and reviewer-prompt contains the user-supplied provenance marker.
  - `execute.review-only.claude.json`: seeds dirty workspace, runs `execute --review-only`, asserts first round is Reviewer-only and Approved Plan holds sentinel.
  - `execute.stop-after-polish.claude.json`: asserts `--stop-after before-docs` exits after Step 3.5 with `completed_stages: [exec, polish]` and no delivery.
  - `review-loop.regression.claude.json`: runs end-to-end umbrella skill on a fresh work item; verifies UX identical to pre-refactor (plan loop + execution loop + polish + delivery).
- [ ] Bump `.claude-plugin/plugin.json` version to `2.6.0`.
- [ ] Bump `.claude-plugin/marketplace.json` version to `2.6.0`.
- [ ] Update root `README.md`: new skills, three entry modes, multi-batch example, `--stop-after` enum, `--accept-external-state` flag (documented as unsafe opt-in).
- [ ] Update root `CLAUDE.md` Codex Stage 1 Notes section: new Stage 1 scope list.
- [ ] Run full smoke suite locally; all Phase 2 cases must pass.
- [ ] Manual verification:
  - Fresh plan → execute end-to-end.
  - Multi-batch: `execute --session` twice with `--stop-after` in between; verify `session_commits` / `last_verified_*` update correctly.
  - Drift trigger: manually edit a file mid-batch, rerun `execute --session`; verify pause + decision tree.
  - `--review-only`: modify repo, run `execute --review-only`, verify pure-CR path.

**Explicitly out of Phase 2 scope:** hallucination-guard smoke (orchestrator Read-event assertions for every `## Protocol Imports` file). That assertion class requires runner-infrastructure work on `scripts/run-skill-smoke` and ships as its own unit — see [Phase 2.1](#phase-21--hallucination-guard-smoke-separate-ship-unit) below. Phase 2's behavioral smoke cases ship without it; the Phase 2.1 work ships before Phase 3.

**Exit criteria:** all Phase 2 behavioral smoke cases pass; manual runs work; version bumped; docs updated. The hallucination-guard smoke is tracked under Phase 2.1 and is NOT required for Phase 2 exit. Ship as a single Claude Code release.

### Assertion limitations

Behavioral smoke cases are grey-box assertions against `session-final.md` via the runner's `artifact_contains` substring matcher. A few acceptance criteria are partially covered rather than fully asserted; this section records the known carve-outs so reviewers do not treat them as silent gaps.

- `execute.session-resume.smoke.claude.json` — asserts the single-round resume writes an `### Execution Round N` entry, preserves `entry_point: plan` (from the seeded fixture), and keeps `- Source: reviewer-approved`. It does **not** assert delivery or the full `completed_stages` quartet, because the smoke prompt intentionally stops at the first Reviewer VERDICT. End-to-end delivery parity from a resumed session is covered by the manual-verification steps, not by this smoke.
- `execute.review-only.smoke.claude.json` — does not self-seed a dirty workspace inside the runner, because the smoke executes against the live repo worktree and the session-file assertions do not depend on the dirty set. The round-1 reviewer-only invariant is asserted via the literal `- Executor backend: skipped (review-only first round)` marker that the orchestrator writes to `## Review History` per `skills/execute/SKILL.md` §`--review-only` first-round skip.
- `artifact_contains` is a substring matcher. Compound assertions use `smoke_groups` (mode `all`) to AND several substring probes together. True ordering / structural assertions on YAML would require a richer matcher; that upgrade is out of scope here and can be revisited if the grey-box assertions start drifting.

### Phase 2.1 — Hallucination-guard smoke (separate ship unit)

Separate ship unit. Ships after Phase 2 and before Phase 3. No runtime behavior change; the extension lives entirely in `scripts/run-skill-smoke` and a new assertion kind.

**Goal:** `scripts/run-skill-smoke` captures `tool_use` Read events emitted by the `claude` CLI during a smoke run and exposes an assertion that every path in a SKILL's `## Protocol Imports` block was actually read.

**Concrete tasks:**

- [ ] Detect the runtime of the smoke case. For `runtime: claude`, inject `--output-format json` (and any companion flags required to stream tool events) into the `claude` invocation. For `runtime: codex`, leave the invocation unchanged — Codex is out of scope for this extension.
- [ ] In the wrapper script (`build_claude_wrapper`), tee stdout into a structured parser that walks the streaming JSON, filters `tool_use` events whose tool name is `Read`, and persists the normalized event list as a new artifact (proposed: `meta.tool_use_events.json` with schema `[{"tool": "Read", "target": "<absolute_or_repo_relative_path>"}, ...]`).
- [ ] Add a new `smoke_assertions` kind `tool_use_read_covers_paths` with schema `{"kind": "tool_use_read_covers_paths", "artifact": "meta.tool_use_events.json", "paths": ["<path1>", ...]}`. The matcher passes iff every declared path appears as a `Read` target in the event list.
- [ ] Wire the new assertion into each Phase 2 Claude smoke case:
  - `plan.fresh.smoke.claude.json` — paths: the four `## Protocol Imports` in `skills/plan/SKILL.md`.
  - `execute.*.smoke.claude.json` — paths: the four `## Protocol Imports` in `skills/execute/SKILL.md`.
  - `review-loop.regression.smoke.claude.json` — paths: the four `## Protocol Imports` in `skills/review-loop/SKILL.md`.
- [ ] Pin the JSON parser to the currently-shipping `claude` CLI output format. Add a smoke-level sanity check that flags parser drift (unexpected schema) as a failure rather than silent pass.

**Exit criteria:** the `tool_use_read_covers_paths` assertion passes on every Phase 2 smoke case; parser is pinned; lint + smoke remain at parity or better.

**Risk:** the `claude` CLI's streaming JSON schema may evolve between releases. Mitigation: pin against the current format, fail loudly on schema drift, version the parser alongside the runner.

---

## Phase 3 — Codex side (complete ship unit)

- [ ] Create `.agents/skills/plan/SKILL.md` with the same entry/exit semantics as Phase 2 plan skill but Codex-native dispatch (fresh self-contained Codex subagent prompts; `claude -p --no-session-persistence` reviewer default with Codex reviewer fallback).
- [ ] Create `.agents/skills/execute/SKILL.md`:
  - Stage 1 scope: Step 3 exec + Step 4 delivery only. Steps 3.5 / 3.6 / 3.7 are explicitly out of scope and the skill rejects `--stop-after` values that reference them.
  - Implement the same three entry modes, lock, drift check, `completed_stages` invalidation + replay (trivially terminates since supported set is `{exec}`), hard-stop, `delivery_blocked_by`.
- [ ] Refactor `.agents/skills/review-loop/SKILL.md`:
  - Thin wrapper referencing the same four protocol docs.
  - Update `## Stage 1 Scope` list to `review-loop`, `plan`, `execute`, `guide`.
  - Explicitly disclose that this is a Stage 1 surface expansion (not internal reorg).
- [ ] Update `.agents/skills/guide/SKILL.md` if it exists to match Phase 2 guide updates.
- [ ] Add Codex smoke cases (`plan.fresh.codex.json`, `execute.session-resume.codex.json`, `execute.from-plan.codex.json`, `execute.review-only.codex.json`) aligned to Phase 2 fixture conventions.
- [ ] Run full smoke suite; all Phase 3 cases must pass.

**Exit criteria:** all Phase 3 cases pass; Codex skills work standalone; Stage 1 scope list updated.

---

## Phase 4 — Integration verification

- [ ] Run the combined smoke suite (Phase 2 + Phase 3) in one go.
- [ ] Cross-runtime manual verification:
  - Run `plan` on Codex, copy the UUID, run `execute --session <uuid>` on Claude Code. Assert delivery succeeds and session metadata records `entry_point` history correctly across runtimes.
  - Reverse direction: `plan` on Claude Code → `execute --session` on Codex.
- [ ] Review-loop regression: run the umbrella skill on a non-trivial work item; assert no behavior delta vs pre-refactor.
- [ ] (Optional) Archive design decisions to `docs/decisions/` if that pattern is adopted in this repo.

**Exit criteria:** end-to-end cross-runtime flow works; no regression in existing `review-loop` behavior.

---

## Test plan (per phase)

Each phase's smoke cases assert:

1. **Static contract**: SKILL.md frontmatter valid, argument-hint matches skill body, `## Protocol Imports` block present and enumerates expected files.
2. **Session schema**: session file after skill run contains all canonical sections + the correct `Session Metadata` fields for that entry mode.
3. **Behavior**: entry mode behaves per design spec (e.g., `--review-only` skips first Executor; `--plan` injects user text + marks `plan_source: user-supplied`; `--stop-after` exits at the correct seam without delivery).
4. **Invariants**: `completed_stages` only contains clean-pass entries; drift check triggers on external edits; lock acquired/released correctly across batches.
5. **Hallucination guard**: orchestrator metadata shows Read events for every declared protocol import.
6. **Backward compat**: a seeded pre-v10 session (missing `base_dirty` / `last_verified_*` / `session_commits` / `completed_stages` / `delivery_blocked_by`) triggers the pause-and-confirm prompt on first `execute --session`, not silent backfill.

---

## Risks and mitigations

See design spec §"Open risks" for the full list. Implementation-specific mitigations:

- **Protocol import drift**: smoke-case hallucination guard asserts Read events; reviewers reject SKILL.md changes that skip the Imports block.
- **Version bump skipped**: Phase 2 checklist explicitly lists `plugin.json` + `marketplace.json` bumps; CI (if present) should fail on missing bump when SKILL.md changes.
- **Multi-batch drift noise in real use**: document the `--accept-external-state` opt-in in the guide; make sure error messages are actionable.

---

## Follow-ups (out of scope for this plan)

- Consider factoring `docs/protocol/*.md` into a `protocol/` top-level directory if more skills adopt the pattern.
- Evaluate whether Codex Stage 2 should pick up 3.5 / 3.6 / 3.7 and converge the runtime supported sets.
- Explore whether `--accept-external-state` should log the accepted drift to a dedicated audit file for later review.
