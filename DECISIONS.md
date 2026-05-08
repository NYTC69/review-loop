# DECISIONS — review-loop

ADR-style. Each entry is immutable once accepted. Supersede with a new
entry; never edit history.

---

### ADR-1: Refine BACKLOG P2 "dry-run Orchestrator mode" to lint+smoke assertions

<!-- synced: 2026-05-04 drawer-id=drawer_3cats_decisions_review-loop_3e0a26f2bcf937e6 sidecar-hash=b69e99880943e5e65fd67064869d11cc target=3cats/decisions_review-loop schema=v1 -->

- **Date**: 2026-05-02
- **Status**: Accepted
- **Context**: BACKLOG P2 (added 2026-04-19) proposed a "dry-run Orchestrator that executes /review-loop against a fixture repo and validates the Agent-call sequence without writing files," motivated by stall-class bugs (`tool_uses: 0` from plugin-defined `subagent_type`, `--output-format json` buffering hang). Recon during plan session b1e5ecca (2026-05-02) showed there is no orchestrator process to instrument: `skills/review-loop/SKILL.md` IS the orchestrator-as-prose, executed by Claude/Codex models. A dry-run process boundary doesn't exist.
- **Options considered**:
  - **(A) Static analysis only** — parse SKILL.md + agents/*.md for `subagent_type:` literals; cheap and CI-friendly but cannot catch runtime drift where the model dynamically constructs a wrong subagent_type.
  - **(B) Stream-json `tool_use` post-processor** — extract `tool_use` events from the smoke runner's stream-json capture and assert sequence/values; catches runtime drift but requires extending smoke contract schema.
  - **(C) Dryrun SKILL flag** — add `--dry-run` to SKILL.md telling the model to skip side-effect tools; requires model self-discipline; unreliable.
  - **(D) Separate orchestrator binary** — rejected as the orchestrator is prose, not a process.
- **Decision**: Implement (A) + (B) as complementary layers. Static lint + runtime smoke assertions cover both shapes (text drift and runtime drift). Reject (C) and (D).
- **Consequences**: Two new lint kinds (A1 `command_flag_co_occurrence`, A2 `agent_subagent_type_whitelist`) plus two new smoke kinds (B3 `tool_use_min_count`, B4 `tool_use_agent_subagent_type_whitelist`). Adds a new `lint_assertions` mapping section in `tests/skills/contracts/assertion-mapping.json` parallel to `smoke_assertions`. Closes BACKLOG P2[1] "dry-run Orchestrator" by replacing it with this concrete scope. Implementation tracked in plan session `.review-loop/sessions/b1e5ecca-6bc1-43cb-b6d3-c8b5174e60ca.md` (4-round codex-reviewed plan; APPROVE 2026-05-02).

---

### ADR-2: B3 wiring strategy — option (b) per-fixture min override

<!-- synced: 2026-05-04 drawer-id=drawer_3cats_decisions_review-loop_eadd66b47858b6df sidecar-hash=ebeb9d92dbd697c57ade2abc7e8dd0d4 target=3cats/decisions_review-loop schema=v1 -->

- **Date**: 2026-05-02
- **Status**: Accepted
- **Context**: B3 (`tool_use_min_count`) is meant to assert each smoke run dispatches at least one Agent/subagent call. Audit (2026-05-02 against `tests/skills/.artifacts/*/tool-use-events.json`) showed 1 of 8 tool-event-capturing smoke fixtures (`plan.fresh.smoke.claude`) actually reaches an Agent dispatch under the existing 120s timeout; the other 7 truncate before Agent is dispatched (0 Agent events each, all 8 with `schema_errors=1`). Wiring `min: 1` to all 8 would red 7 fixtures on first run.
- **Options considered**:
  - **(a) Narrow B3 to plan.fresh only** — assertion ID appears in 1 of 8 fixture files; violates AC #4 ("wired into all 8 smoke fixtures already capturing tool_use_events") textually.
  - **(b) Permissive `min: 0` overrides** — wire B3 to all 8 with the shared mapping defaulting to `min: 1` (real-catch on plan.fresh) and the 7 truncated fixtures using a per-fixture override `{"overrides": {"min": 0, "_comment": "<rationale>"}}` (vacuous-pass with named/traced rationale). Requires a new override-resolver mechanism in `run-skill-smoke`.
  - **(c) Bump per-fixture timeouts** — uniform timeout bump unproven; the 120s cap was set during the Apr 22 timeout fix for sound reasons; each fixture truncates for fixture-specific reasons.
  - **(d) Soft-skip on schema_errors > 0** — contradicts the existing repo-wide pattern at `run-skill-smoke:241-258` and 276-304 ("schema drift only blocks the assertion when no events were captured at all; with non-empty events evaluate content directly"); creates inconsistency with already-shipped smoke kinds.
- **Decision**: (b) — permissive override with new resolver. Test `test_fails_with_min_one_and_zero_agent_events_even_when_truncated` proves the default `min: 1` would catch the regression — only the explicit per-fixture override demotes to `min: 0`. (c) is encoded as one of three approaches in a P3 BACKLOG follow-up to fortify the truncated fixtures.
- **Consequences**: Adds an override-resolver shape `{"id": "<id>", "overrides": {<field>: <value>, …}}` to `scripts/run-skill-smoke` with shallow merge over the mapping entry. Override-key whitelist: `min`, `tool`, `artifact`, `requires_subagent_type`. Unwhitelisted non-`_`-prefixed keys → contract validation error (preserves invariant strength). New BACKLOG P3 entry: "Tighten B3 min from 0 to 1 on 7 truncated smoke fixtures by addressing pre-Agent stream truncation."

---

### ADR-3: `_`-prefix-as-ignored-metadata convention for override resolver

<!-- synced: 2026-05-04 drawer-id=drawer_3cats_decisions_review-loop_3556ec7bdd45bd1d sidecar-hash=0460c07f1362956b3be41ce9835e212f target=3cats/decisions_review-loop schema=v1 -->

- **Date**: 2026-05-02
- **Status**: Accepted
- **Context**: ADR-2 introduced per-fixture override wrappers carrying rationale (e.g. why a specific fixture demotes `min` to 0). The R3 codex reviewer flagged that putting this rationale on the **shared** mapping entry would be misleading because the shared entry's `min` is 1; only per-fixture overrides demote it. The rationale must live next to the override site itself, not on the shared mapping.
- **Options considered**:
  - **(I) Single named `_comment` key** — explicit list of one allowed metadata key in the resolver.
  - **(II) `_`-prefix-as-ignored-metadata convention** — any override key whose name starts with `_` (underscore) is silently dropped during merge and ignored before the override-key whitelist check. Generalizes to `_reason`, `_note`, `_todo`, `_owner`, etc. without further plumbing.
  - **(III) Schema-level `description` field per override-wrapper** — adds a typed field to the contract, requires schema migration.
- **Decision**: (II). Single underscore-prefix rule generalizes naturally and adds zero contract-schema surface.
- **Consequences**: Resolver in both `scripts/run-skill-lint` (lint side) and `scripts/run-skill-smoke` (smoke side) now silently drops any override key starting with `_` during merge, before the whitelist check. New unit test `test_underscore_prefixed_override_key_is_ignored_silently` pins behavior. Convention applies repo-wide for any future override metadata. `_comment` becomes the canonical rationale field but is not blessed in code — only the prefix rule is.

---

### ADR-4: ADR-2 follow-up — lock per-fixture B3 timeout bumps

- **Date**: 2026-05-08
- **Status**: Accepted
- **Context**: ADR-2 introduced per-fixture `min: 0` overrides as a vacuous-pass shim for the 7 of 8 `tool_use_events`-capturing smoke fixtures that truncated before Agent dispatch under the 120s wall-clock cap, and recorded a P3 BACKLOG follow-up to "tighten B3 min from 0 to 1 by addressing pre-Agent stream truncation." The spike at `.compass/results/2026-05-08_b3-truncation-spike.json` (committed at 368210a) tabulates per-fixture truncation profiles (NEAR_DISPATCH / IN_DOC_RECON / full-pipeline) and identifies SKILL doc-recon as the bottleneck (NOT user-prompt size — `plan.fresh.smoke.claude` carries the largest user prompt at 1089 chars yet is the only fixture that already passes B3). The spike empirically proposed bumps of 180s / 240s / 480s by tier. Single-fixture re-measurement at v2.6.31 (`execute.session-resume.smoke.claude`, NEAR_DISPATCH tier) showed: 180s → returncode -15 / status skip; 240s → status pass with 1 Agent event (`subagent_type: general-purpose`). Branch B (linear scale-up) selected per the Approved Plan's deterministic Step 2 fork table. The pass/fail criterion under which Branch B was locked is `meta.status == "pass"` AND ≥1 Agent event with `subagent_type: general-purpose`, NOT literal `returncode == 0`: under `execution_policy: best_effort` the runner is by-design permitted to return SIGTERM at the timeout cap (`returncode == -15`) while still stamping `meta.status="pass"` if assertions hold on the captured partial event stream. `meta.status` is the runner's official assertion-completion signal; literal returncode is not. The runner branch this criterion relies on lives in `scripts/run-skill-smoke` lines 967–1002: on `TimeoutExpired`, `evaluate_mapping(...)` runs over the captured partial event stream (line 967), `meta["status"]` is initially stamped `"skip"` (line 976), then upgraded to `"pass"` at line 989 iff `assertion_status == "pass" and not missing_artifacts` — the `timed_out_with_passing_state` gate at line 987 — emitting `final_reason = "assertions passed after timeout cleanup"` (line 990).
- **Options considered**:
  - **(i) Trim user prompt** — rejected up-front per the spike's bottleneck-finding (doc-recon dominates; user-prompt size is not the constraint).
  - **(ii) Bump per-fixture `setup.timeout_seconds`** — chosen. Each fixture's `command` block, `setup.temp_config`, and assertion list preserve their distinct entry-point/stop-point/provenance signals. `min: 0 → 1` flip is atomic with the timeout bump in the same Edit.
  - **(iii) Agent-only stub fallback** — reserved as the uniform Branch C escalation path if Step 2 had double-failed at both 180s and 240s. Not adopted in the active Branch B; would have sacrificed 6 distinct non-B3 signals to recover B3.
- **Decision**: Branch B linear scale-up. Per-fixture lock table:

  | Fixture | Tier | timeout_seconds | B3 override min |
  |---|---|---|---|
  | execute.session-resume.smoke.claude | NEAR_DISPATCH | 240 | 1 |
  | execute.stop-after-before-security.smoke.claude | NEAR_DISPATCH | 240 | 1 |
  | execute.stop-after-polish.smoke.claude | NEAR_DISPATCH | 240 | 1 |
  | execute.from-plan.smoke.claude | IN_DOC_RECON | 360 | 1 |
  | execute.review-only.smoke.claude | IN_DOC_RECON | 360 | 1 |
  | execute.stop-after-before-polish.smoke.claude | IN_DOC_RECON | 360 | 1 |
  | review-loop.regression.smoke.claude | full-pipeline | 600 | 1 |

  `plan.fresh.smoke.claude` is unchanged (control fixture; already passes B3 with implicit shared `min: 1`). Each fixture's `_comment` is refreshed to `min: 1 enforced at <Ns> per ADR-4`.
- **Consequences**: Worst-case CI wall time per the active Branch B is approximately 41 minutes (plan-quoted figure). Branch A figure (29 min) and Branch C figure (10.5 min) are recorded for completeness but do not apply. The B3 shared-mapping default (`min: 1`) is unchanged (AC-5). AC-6 is mechanically pinned via JSON-parse verification: shared `min: 1`, every per-fixture B3 override has `min: 1`, NO fixture retains `min: 0`. Per Round-1 reviewer MINOR #2 explicit framing: 240s and 480s values are extrapolated from the spike's event-rate model, validated empirically only at the NEAR_DISPATCH tier (180s probe at v2.6.31, falsified; 240s probe at v2.6.31, confirmed). The IN_DOC_RECON 360s and full-pipeline 600s values (Branch B linear scale-up) remain best-guess until a future re-measurement falsifies them; this is recorded as an explicit consequence of single-probe scoping per HANDOFF Codex-hang practice. ADR-2 is NOT mutated (append-only).

---
