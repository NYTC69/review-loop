**Last updated**: 2026-11-26

## P0 — blocker / must-do-now

## P1 — high priority

## P2 — normal

- WI-E remaining: E3 (feature, needs owner OK during the trial). (added 2026-11-22)
  - v3.14.0 follow-up: rejections (reject --text) reach only the EXEC author; consider showing delivered rejections to the reviewer and gate like notes. (2026-11-22)
  - v3.15.5 note (Opus, WI-E plan/code review): fresh roles get finding ids masked in notes, but review-history or vendor-attribution phrases in a note (e.g. "Codex approved") still reach them; withdraw-findings refuses those. Low risk under the threat model. (2026-11-24)

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
- Gemini / agy follow-ups (WI-F v3.16.1; do when the paid agy plan is active in December). (added 2026-11-24)
  - Real gemini run on the v3.16.1+ coordinator once the paid plan is active (WI-F was verified with fake CLIs and a standard-role real run only). (2026-11-24)
  - v3.16.1 note (Opus R1): TRANSIENT_PROVIDER_RE lacks Gemini-native overload wording (503 UNAVAILABLE / "The model is overloaded"); widen it from a real agy capture. (2026-11-24)
  - v3.16.1 note (Opus R1): a Gemini capacity failure made ineligible by an ignored-file edit HOLDs with the transient resume hint, not the "changed ignored files, check them by hand" note (path still in voided.ignored_changed); add that note when touched. (2026-11-24)
  - v3.16.1 note (Opus R1): no test for the Gemini context-digest condition or the Gemini writer Git guard on the transient path (the no-work guard is mutation-tested). (2026-11-24)
- Optional follow-ups from the v3.16.7 review and dogfood run (WI-DOCSFIX2). (added 2026-11-25)
  - Audit labels: the past-cap review of a last-slot writer output is recorded as `operator_recheck` and printed as "recheck of the changed tree"; the failed-writer rollback diff path is not kept in the DOCS receipt; two NOTEs can contradict each other when the per-path rule splits a failed writer's writes (dogfood F005, F006, F013, F014). (2026-11-25)
  - Test gaps: git-guard-discarded writer turn, chaining across several failed turns, missing-snapshot fallback, retry inside one invoke() (dogfood F004, F009-F011). (2026-11-25)
  - Own-repo dogfood friction: attach-verification refuses a host log that contains vendor names (review-loop's own test output mentions claude/codex), so the operator trims those lines; the reviewer sandbox times out on the paired_session test modules, so each tree needs a host attach. (2026-11-25)
- Optional follow-ups from the v3.17.0 reviews and dogfood run (WI-POSTGATE). (added 2026-11-26)
  - Dogfood MINOR findings F004-F016, F020-F023 (audit wording, test gaps); F023: the docs touch-up does not re-check that an allowlisted docs path is a regular file before writing. (2026-11-26)
  - Review notes: rejections scale the narrow-loop and stage caps differently; touch-up rollback order versus the writer git guard; a protected path written by a failed touch-up writer is not rolled back. (2026-11-26)
  - `backlog_insert` cosmetics: CRLF files, missing blank line before an inserted section, case-sensitive `## P1`/`## P2` heading match. (2026-11-26)
  - Gate note: the AC8 "unchanged state" holds byte for byte only for POLISH-Q; a migrated SECURITY HOLD appends ledger status history (intended). (2026-11-26)
## Done (recent, trimmed quarterly)


- ~~Review notes accepted as-is (no action; owner 2026-11-25 cleanup).~~ (added 2026-11-24, closed 2026-11-25, see v3.16.4)
  - History-scan: malformed declaration strips the line (consistent with "malformed grants nothing"); per-match workitem re-read (no cost issue); `.codex reviewed it` lookahead (prose regexes cover realistic forms).
  - Auto-split: transferred-scope line from finding summaries (descriptive); generous round 4-5 allowances (harmless).
  - OPV: old-tree records stay current until the next prompt_block/writer turn (lookups match the exact tree); "run every OPV-status module" moved to memory as a process lesson.
- ~~WI-SMALL optional review-note follow-ups (8 items).~~ (added 2026-11-25, closed 2026-11-25, see v3.16.5)
  - Fixed: risk-first order in auto-split backlog entries; POLISH-Q operator recheck pre-checks the invocation limit; a re-routed stale approval records STALE; the stop-after-gate re-route is bounded once per tree; the round-5 shadow HOLD names the --max-invocations value.
  - Tests/docs: withdrawal plus two notes, second stale replay at the replay site, README on a second operator edit and "a repeat HOLDs", CHANGELOG names the test_opv_replay fixture pause.
- ~~WI-DOCSFIX: DOCS rollback reverted operator edits; DOCS budget HOLD offered only abort (token-meter 1862edcf).~~ (added 2026-11-25, closed 2026-11-25, see v3.16.6)
  - Rollback covers only the writer turn's own writes; operator changes at a DOCS HOLD route to the docs review or the EXEC reviewer; one docs recheck per tree past the DOCS cap; the invocation-limit HOLD says attach-verification keeps the saved cap.
- ~~WI-DOCSFIX2: DOCS last-slot writer dead end; errored-writer outside writes attributed to the operator.~~ (added 2026-11-25, closed 2026-11-25, see v3.16.7)
  - One past-cap docs review per unreviewed tree; errored/discarded writer writes rolled back per path from evidence snapshots; v3.16.6 protected-path route documented; first release checked by a dogfood review-only lifecycle run. The two v3.16.5 review notes were closed as accepted (owner 2026-11-25).
- ~~WI-POSTGATE: post-gate stages stop holding on non-blocking findings; narrow author loop for SECURITY findings.~~ (added 2026-11-25, closed 2026-11-26, see v3.17.0)
  - POLISH-Q report-only after the gate (review-only keeps its fix leg); DOCS spends its budget and moves on; SECURITY-reviewer findings get up to 2 author fix loops (EXEC reviewer, FINISH, unreviewed docs touch-up, SECURITY); "needs your decision" report section; open rows land under ## P1 / ## P2 in BACKLOG at accept (token-meter field note); in-flight v3.16 runs migrate. Dogfood run found and fixed a touch-up TypeError.
