**Last updated**: 2026-11-22

## P0 — blocker / must-do-now

## P1 — high priority

## P2 — normal

- WI-C regression tests only (no product code). After WI-A/B if there is room (owner 2026-11-22). (added 2026-11-22)
  - v3.14.0 follow-up (Opus, WI-NOTE R4): restore a test that the delivery turn carries the note once (no decisions block), and cover a later EXEC author turn after delivery. (2026-11-22)
  - v3.11.1 follow-up (Opus, WI-DISPFIX): tests for an invalid answer followed by a retry that omits an id, replay through _recorded_reviewer_result, and the discarded marker on the rejected first turn. (2026-11-22)
  - v3.11.0 follow-up (Opus, WI-TESTEVID): add a test that a confirmation review reuses the passing test result of the same tree. (2026-11-22)
  - v3.10.0 follow-up (Sol review): add a regression test combining a gemini author, the default gemini gate and gate-FAIL repair (covered only by ad hoc fake-CLI runs so far). (2026-11-21)
  - v3.11.0 follow-up: the Windows end-to-end helper branches on review_report to pick its assertion; brittle if more report shapes appear. (2026-11-22)
- WI-D reviewer disposition retry edge cases (not seen in the field yet). (added 2026-11-22)
  - v3.11.1 follow-up (Opus, WI-DISPFIX): a still_open row for a closed unexpected id, or an open id the reviewer was not asked about, still HOLDs at once without the protocol retry; make it InvalidDispositionIDs if it shows up in the field. (2026-11-22)
  - v3.11.1 follow-up (Opus, WI-DISPFIX): record ignored dispositions in the receipt (ignored_prior_findings) for audit, like _ignore_operator_dispositions. (2026-11-22)
  - v3.11.0 follow-up (Sol merge review): if a saved quota wait is stopped and resumed during a reviewer disposition retry, the retry instruction is lost and the reviewer gets the ordinary prompt (an extra protocol retry at most). (2026-11-22)
  - v3.11.0 follow-up (Opus, WI-TESTEVID): reverting the change that caused a coordinator test-failure finding leaves that row open until the next reviewer disposition. (2026-11-22)
- WI-E operator note semantics (design; the rejection item is a feature and needs owner OK during the trial). (added 2026-11-22)
  - v3.14.0 follow-up (Opus, WI-NOTE R3): a note's ledger ids reach fresh roles unmasked (withdraw-findings rejects ids in its reason, note does not); replace LEDGER_ID_RE matches with a neutral token in the fresh audience. (2026-11-22)
  - v3.14.0 follow-up (Opus, WI-NOTE R3/R4): a delivered note cannot be superseded; a later contradicting note shows both as standing decisions. (2026-11-22)
  - v3.14.0 follow-up: rejections (reject --text) reach only the EXEC author; consider showing delivered rejections to the reviewer and gate like notes. (2026-11-22)
  - v3.12.0 follow-up (Opus, WI-SPEC plan review): if specialist A later withdraws a row that B's claim was attached to, B's claim goes with it; B raises it again only when a later delta touches that file. (2026-11-22)
- WI-F gemini / agy runtime follow-ups. (added 2026-11-22)
  - v3.11.0 follow-up (WI-TURNFAIL note 7): an agy exit > 0 without a quota line is a terminal ERROR; consider one retry. (2026-11-22)
  - v3.10.0 follow-up (Sol review): README/ADR-21 wording on read-only recovery: a gemini reviewer that changes an existing ignored file HOLDs and leaves the edit on disk; automatic re-dispatch only follows a successful restore. (2026-11-21)
  - v3.11.0 follow-up (WI-TURNFAIL): the unknown-reset backoff slot count is per run, not reset per invoke. (2026-11-22)
- Owner decision pending: whether to use a paid agy plan for gemini roles (the free quota reset was 164 h in the poker-tools trial). (added 2026-11-22)

## P3 — nice to have / someday

- Deferred by the owner until after the v3.2.0 trial (decided 2026-10-17). (added 2026-10-14)
  - `accept --auto-commit false` as a one-way downgrade.
  - A formal known-inherited-failures declaration, so a reviewer approval can count a run whose test command fails on exactly the failures the base branch already has (gg-pipeline field report, 2026-10-19; owner 2026-10-20: not during the trial). Workaround until then: `--deselect <nodeid>` in the test command.
  - gg-pipeline feature requests (2026-10-20; triaged as features, not fixes):
    - An operator route to send a held EXEC run back to the author with a note (`resume --to-author`, or `reject` on a HOLD), and dispatch the author first after `note` + `resume` (gg-pipeline WI-266b, 2026-10-21; deferred). Partly covered in v3.14.0: a reviewer ruling HOLD resumes straight to the author with the note.
    - A requirement change after plan approval that reaches the EXEC reviewer without a full scope-change successor (a superseding operator note shown to the reviewer, or a short re-plan inside the run). Today: `note --scope-change` (gg-pipeline WI-266e, 2026-10-22; deferred). Partly covered in v3.14.0: an in-scope `note` delivered to the author now also reaches the EXEC reviewer, the gate and later writers as an operator decision.
    - When the base moved under a parked run, have the author re-run the configured command and refresh the recorded verification before the gate.
    - Warn at `note` time when the operator text contains a phrase the independence check rejects later (the author may quote the note into the change); and at the last EXEC round, give the author one reword turn instead of the gate HOLD (gg-pipeline WI-263m, 2026-10-21; v3.2.3 only adds the path:line places to the HOLD).
    - A run/resume pre-flight check that each vendor CLI is logged in under the run's CODEX_HOME / Claude config, refusing early with a refresh hint instead of a mid-run HOLD (gg-pipeline WI-282a, 2026-10-24; deferred).
- Deferred Windows items after v3.3.0 (ADR-20): map Git Bash `/c/...` operands through `winplat.windows_abs_path` in `evidence_guard` (log-only on Windows today); decode PowerShell `-EncodedCommand` if a real Codex sample shows it; `--detach` on Windows; Linux/WSL support. (added 2026-10-27)
- v3.12.0 follow-up (audit GR-7): automatic quota sensing to drop specialists first (owner 2026-11-22: deferred, quota is not tight; revisit when it is. quota_status.py is machine-local). (added 2026-11-22)

## Done (recent, trimmed quarterly)

- ~~Gate-survey follow-up GR-4: allow skipping PLAN review for small work items that already list their files and acceptance criteria. (owner via main-rc, 2026-11-19; after the gate-once release)~~ (added 2026-11-19) (closed 2026-11-22, see v3.15.0 WI-FASTPLAN (run --fast-plan; files <= 3 + acceptance criteria))
- ~~Execution plan (owner 2026-11-21, backlog audit .compass/results/backlog-audit-2026-11-21.md): do these merged work items in order after v3.10.0.~~ (added 2026-11-21) (closed 2026-11-22, see v3.10.1-v3.15.0 (all seven work items shipped))
  - 1 patch pack: gate prompt removesuffix + test isolation — done in v3.10.1.
  - 2 WI-TURNFAIL: GR-5 + the long author timeout hint (supervisor note WI-NOPROBE) — done in v3.11.0.
  - 3 WI-TESTEVID: GR-1 + lifecycle writers see operator verification records + accept shows `carried` — done in v3.11.0.
  - 4 WI-SPEC: GR-3 + GR-2 + GR-7 — done in v3.12.0.
  - 5 WI-RESUME: frozen role manifest path-only change (WI-NOPROBE gate note) + GR-6 — done in v3.13.0.
  - 6 WI-NOTE: operator note delivery window (gg-pipeline WI-413) + undelivered note in `status` — done in v3.14.0.
  - 7 WI-FASTPLAN: GR-4 + PLAN/gate history scan parity (v3.8.5 Opus R3) + matrix label detection. "Small" = the work item lists the files to change (at most 3) and has explicit acceptance criteria; otherwise normal PLAN review (owner 2026-11-21). — done in v3.15.0.
- ~~v3.15.0 note: WI-FASTPLAN R4 shipped after three review rounds (R2/R3 Sol REVISE on label edge spellings) with a 76041-variant and 90-real-matrix differential instead of a fourth review (owner approved release 2026-11-22).~~ (added 2026-11-22) (closed 2026-11-22, see backlog cleanup: record only; the release commit and CHANGELOG hold it)
- ~~v3.13.0 accepted residual (gate R2): a hand-edited saved config binding is not re-sealed by a resume role change (the digest guard checks the manifest digest only).~~ (added 2026-11-22) (closed 2026-11-22, see backlog cleanup: accepted residual under the owner threat model)
- ~~v3.13.0 follow-up (Opus, WI-RESUME R1 note 4): a workspace profile with a different vendor is now refused on every restoring action except status (at v3.12.0 it was ignored); hard to reach.~~ (added 2026-11-22) (closed 2026-11-22, see backlog cleanup: accepted: hard to reach, owner threat model)
- ~~WI-A plan scan false HOLDs: shadow plan-path exemption on Codex/gemini + fast-plan polish. Next up (owner 2026-11-22, with WI-B as v3.15.1). (added 2026-11-22)~~ (closed 2026-11-22, see v3.15.1 WI-POLISH)
  - ~~v3.10.1 follow-up (Opus review): on Codex or gemini the shadow inlines the whole plan, so a scenario-matrix tool path in the plan is scanned as prompt text without the context/plan.md exemption and can falsely HOLD the shadow like the gate did before v3.10.1. (2026-11-21)~~
  - ~~v3.15.0 follow-up (Opus, WI-FASTPLAN R1): nested sub-bullets under "## Files to change" count as files (conservative fallback to PLAN review). (2026-11-22)~~
  - ~~v3.15.0 follow-up (Opus, WI-FASTPLAN R1): the matrix "missing rows for X" error could name an unrecognised near-miss label. (2026-11-22)~~
- ~~WI-B operator message wording (text only, no behavior change). Next up (owner 2026-11-22, with WI-A as v3.15.1). (added 2026-11-22)~~ (closed 2026-11-22, see v3.15.1 WI-POLISH)
  - ~~Field (poker-tools gemini trial, 2026-11-21): the allowed_models refusal should name the profile key to edit. (2026-11-22)~~
  - ~~v3.15.0 follow-up (Opus, WI-FASTPLAN R1): the stop-after-plan HOLD says "PLAN approved" after a fast skip; the resume fast_plan check at run creation is a dead branch for resume. (2026-11-22)~~
  - ~~v3.14.0 follow-up (Opus, WI-NOTE R1): an ACCEPTED run still prints "Pending operator note" without saying it was never delivered; no test pins omitting the ruling sentence on a writer replay. (2026-11-22)~~
  - ~~v3.13.0 follow-up (Opus, WI-RESUME R1 note 5): role flags passed again on every operator action are restored from the saved run; consider a NOTE when an explicit flag is ignored. (2026-11-22)~~
  - ~~v3.12.0 follow-up (Opus, WI-SPEC): `resume --skip-quality-polish true` is silently unsaved when resume stops before the save point (round-limit HOLD without --add-rounds, past deadline, DONE, uncertain-turn HOLD); documented, consider a NOTE. (2026-11-22)~~
