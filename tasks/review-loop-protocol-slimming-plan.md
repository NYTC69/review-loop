## Solution Plan: Stage-scoped protocol loading and shared-rule deduplication

### Problem Analysis

Baseline is v2.8.0, commit `cadb06c11ba73cef356b6d62f39f4fb96c662a84`.
Five shared protocol documents plus six entry skills currently total 6,573
lines. Each entry requires four/five complete protocols at startup; shared
dispatch, schemas, lifecycle and gate prose also appear in the runtime skills.
The previous milestone's `repeated_reads` metric concerns history transport,
not the volume of workflow instructions. It cannot establish this change's
benefit. The prior delivery's 4,497-line claim uses a different/unknown scope.

The user accepted active-stage loading and shared-rule deduplication. The
purpose is lower instruction cost with the existing behavior intact, for BOTH
Claude Code and Codex. No stage/gate removal or new evidence capability.

### Proposed Approach

Keep shared protocols authoritative. Replace eager full-document imports with
an explicit loading contract: entry/core, planning, execution, gate, polish,
docs, security, delivery, and resume. Cross-cutting guards needed before an
action remain in its prerequisite set. A planning Reviewer must receive the
rubric and its applicable retry policy without loading all of execution.

Use a small local reader (`scripts/read_protocol.py`) and declarative loading
map (`docs/protocol/loading.json`) to emit exact, fence-aware Markdown sections
or whole focused references. The map declares each unit's source and explicit
prerequisites; the reader rejects missing/ambiguous sections or cycles before
emitting a partial bundle. It does not infer dependencies from every Markdown
link, because audit/cross-reference links are not eager imports. It never
edits workflow state or changes decisions. Tools call it at stage entry, or
read the same referenced sections explicitly where shell execution is absent.

Reduce the six SKILL entry bodies to essential role/scope, routing and loading
instructions. Retain unique runtime entry procedures in focused references;
remove duplicated shared mechanics only where a named authoritative unit
covers them. Share Codex reviewer dispatch once (parallel fan-out loaded only
for N>1), and keep Claude dispatch's general-purpose/inlined-agent-body guard.
Use section extraction and focused reference moves instead of rewriting
behavior. Preserve old protocol anchors when practical; update every affected
caller and contract when an anchor/path moves. Do not leave a second normative
copy of a rule merely to satisfy an old string-pin assertion.

A unit may be reused within the same live context only if its content has not
changed and it is still available. New independent agents and resumed/compacted
contexts load their own relevant prerequisites; a saved list of paths is NOT
proof those instructions remain in model context. Required user decisions and
safety facts always travel in the Current Review Packet. No blanket reread per
round and no blanket forwarding of the orchestrator's protocols to a Reviewer.

### Implementation Steps

1. **Freeze control and measure before.** Current work uses the archived
   baseline framework at `.review-loop/tmp/slimming-runtime-cadb06c`; candidate
   loading rules cannot control this session. Record the loading matrix and
   UTF-8 byte/line baselines for each runtime/entry. One main session/lock;
   independent reviewers do not launch nested workflows. Main authoring is an
   explicit user override for this run; it does not change the product's author
   policy. Record the independent plan verdict before implementation.
2. **Extract/reroute.** Add the loading map/reader and reference units. Move
   duplicated runtime dispatch, initialization and stage procedures out of
   eager entry bodies, route to the shared authoritative source, and update
   direct references. Cover optional config branches (skip polish, local/default
   reviewer, N>1), entry modes, stop points, errors, drift and legacy resume.
   Before session init load schema+lock+baseline rules; before each write load
   evidence rules; before dispatch load output/validation/appropriate retry
   contracts; before delivery load all delivery gate rules. Do not trade missing
   prerequisites for a better byte count.
3. **Migrate contracts.** Relocate existing pins to their new SSOT or replace
   obsolete full-import assertions with stage/prerequisite assertions. Keep an
   old-assertion-to-new-coverage migration record. Retain semantic guards; no
   vacuous exemptions or hidden skips to obtain green lint. Update both runtime
   guides and related README explanations only where startup semantics change.
4. **Verify and measure after.** Run targeted reader/loading tests, full skill
   lint, and `python3 -m pytest tests/ -q` (never bare root pytest). Reuse the
   eight P1-P4 cases as behavior regression, without changing their golden
   outcomes. Independently review the diff and coverage migration. Run isolated
   loading exercises against before/after with actual read events and recorded
   role prompts; record missing observations as unverified. No fabricated speed
   or token improvements. Apply bounded fixes through this pinned main loop.

### Files to Modify / Create

- `docs/protocol/loading.json`, `docs/protocol/loading.md` — map and concise
  normative loading/caching/resume contract.
- `scripts/read_protocol.py` — read-only exact-section loader and inventory.
- `docs/protocol/*.md` — move/extract shared rules where useful; preserve their
  operative wording. Focused runtime/stage references may be added here.
- `skills/{review-loop,plan,execute}/SKILL.md` and matching `.agents/skills/`
  files; supporting references inside these skill directories — slim entries
  and runtime-only entry details.
- `skills/guide/SKILL.md`, `.agents/skills/guide/SKILL.md`, `README.md`,
  `CLAUDE.md` — only affected loading/source-location documentation.
- `tests/skills/contracts/{review-loop,shared-schema,assertion-mapping}.json`,
  affected smoke definitions and existing source-location tests — migration.
- `tests/protocol_loading_test.py`, `tests/fixtures/protocol-loading/` and a
  small measurement/report script under `scripts/` — routing/prerequisite
  tests and reproducible before/after byte report. Reuse existing capture
  formats where they support the observation; do not build a telemetry service.
- This plan, the main review-loop session and ignored measurement artifacts.

### Risks & Assumptions

- **Scope:** no evidence_ledger/finding_triage/adversarial runtime changes, no
  security scan narrowing, no handsfree expansion, no gate skip heuristic,
  no submodule work, no external repositories/global entry/cache/marketplace
  changes. Preserve BACKLOG/LEARNINGS and user work. No commits, pushes or
  version changes. User-requested quota monitoring/HANDOFF are session-local.
- **Behavior:** all v2.8.0 author-route, claim validity, Reviewer independence,
  malformed-output handling, dispute/revalidation, convergence, cleanup,
  permission, dirty-state, read-only/plan-only, stop/resume and delivery gates
  retain their semantics. Existing contradictions discovered during extraction
  must be reported, not silently resolved in this loading-only change.
- **Measurement:** fixed baseline SHA, identical entry/config/task/role scope.
  Count transitive required instruction bytes, including wrapper/map/loader
  guidance and embedded agent instructions. Count repeated delivery of the same
  rule in a single context separately from necessary delivery to a fresh agent.
  A file split alone is not a reduction in total delivered instructions.
- **Targets:** startup instruction bytes before first work-agent dispatch down
  >=40% for BOTH plan and execute in BOTH runtimes; actual small-task rule input
  down >=25% where measured; required pre-action rule misses = 0; no unrelated
  future-stage procedure loads; deterministic workflow decisions unchanged.
  Total unique full-lifecycle instructions must not grow through fragmentation.
  Wall time, calls, input/output/cache tokens and billed cost are observations,
  not promises; unavailable metrics stay unavailable.
- **Five loading paths:** plan-only; one-file full delivery; cross-file with
  one repair; stop/resume; release/full lifecycle. Check both runtime branches
  statically and through prerequisite traces. Include negative tests: omitted
  safety prerequisite, missing target, ambiguous heading, fenced heading,
  cyclic reference, fresh/compacted context and changed rule content.
- **Live validation:** obtain actual before/after loading traces for plan-only
  and one-file delivery, plus targeted resume. Claude CLI is not authorized for
  this session. Codex can independently exercise either runtime's instruction
  routing using read-only scenario simulation; label that as a simulation, not
  native Claude execution or a completed end-to-end implementation. Native
  Claude measurements remain unverified until the user supplies a run. Do not
  claim the >=25% actual-workflow target from an inventory or simulation alone.
- **Baselines:** reported last delivery lint 721 PASS / 0 FAIL, pytest 511 PASS;
  verify for current content once. Success is preserved coverage and behavior,
  not keeping an arbitrary old number of phrase needles.
- **Quota:** read-only `account/rateLimits/read` monitor every 60 seconds;
  observed codex weekly 23% at setup. Below 5%: finish the current atomic update,
  stop new model dispatches, persist results and write a concise HANDOFF with
  exact next action. Never rewrite the handoff from the background process.

### Open Questions
- None requiring product scope changes. The native-Claude live measurement
  limitation is disclosed above; execution can proceed and report it honestly.
