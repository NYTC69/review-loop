**Last updated**: 2026-11-27

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
  - Dogfood MINOR findings F004-F016, F020-F021, F023 (audit wording, test gaps; F022 fixed in v3.17.1, F018 shown unreachable); F023: the docs touch-up does not re-check that an allowlisted docs path is a regular file before writing. (2026-11-26)
  - Review notes: rejections scale the narrow-loop and stage caps differently; touch-up rollback order versus the writer git guard; a protected path written by a failed touch-up writer is not rolled back. (2026-11-26)
  - `backlog_insert` cosmetics: CRLF files, missing blank line before an inserted section, case-sensitive `## P1`/`## P2` heading match. (2026-11-26)
  - Gate note: the AC8 "unchanged state" holds byte for byte only for POLISH-Q; a migrated SECURITY HOLD appends ledger status history (intended). (2026-11-26)
  - v3.17.1 notes (WI-POSTFIX review and dogfood): a merged duplicate backlog entry keeps its first row's priority (a later P1 duplicate can land under ## P2); the run backlog.md line shows only the first ledger id; tests for an all-fixed merged entry and for the F018 preflight re-run are missing. (2026-11-26)
  - v3.17.2 notes (WI-AGYEXIT review and dogfood): an agy run_command output without a "\nCommand output:" marker is searched whole for the status line (agy always prints the marker for a finished step); the fake agy still emits only the old format, so no end-to-end fake-CLI test uses the 1.5.18 header; test name `test_the_exit_status_is_read_from_the_first_line_only` is stale; a kept ignored change blocks the transient retry only for gemini (codex/claude use failed_turn_did_work); `test_igncache` docstring still says an ignored-dir entry voids the verdict. (2026-11-26)
  - v3.17.3 notes (WI-SUBJDIR review and dogfood): the component mask also hides a file extension, so DOTTED_PATH_RE no longer sees that path as a file name; masking a declared vendor inside a prose slash chain can make the chain classifier refuse an undeclared model name that passed before; test gaps (relative-path-start component, the new attribution lookahead, gemini-named and backslash components); a pre-existing V385-PATH interaction on mixed-tool components in delta.patch (Gemini reviewer note). (2026-11-26)
  - v3.18.0 notes (WI-SWITCH review and dogfood, all MINOR): the saved --gemini-bin path is lost after a role switches away from gemini, or for a gemini gate set at run start (`_saved_gemini_bin` reads only frozen gemini author/reviewer slots and switch-to-gemini rows; record it also when the old vendor was gemini); the "pass --gemini-bin to change it" NOTE is wrong while a frozen gemini slot fixes the path; a later explicit --gemini-bin on a switched gemini gate is not saved; a workspace profile naming the switched-away vendor keeps the saved vendor at restore but the next dispatch still HOLDs with an operator-program reason that hides the profile cause (docs and message); the drifted-unswitched-slot fallback refusal does not name the differing field; with lifecycle off an edited reviewer-commands block makes later author/gate switches refuse (skip the unswitched-slot comparison when lifecycle is off); the independence refusal also applies to report runs, which never dispatch the author; test gaps (a successful switch to codex, a switch combined with a model change on an unswitched role, a stopped wait of an unswitched role surviving, model-only independence refusal, the export/generic drift branches, an explicit --gemini-bin winning, one vacuous assertion in the rebuild-and-audit test). (2026-11-27)
## Done (recent, trimmed quarterly)


- ~~WI-POSTGATE: post-gate stages stop holding on non-blocking findings; narrow author loop for SECURITY findings.~~ (added 2026-11-25, closed 2026-11-26, see v3.17.0)
  - POLISH-Q report-only after the gate (review-only keeps its fix leg); DOCS spends its budget and moves on; SECURITY-reviewer findings get up to 2 author fix loops (EXEC reviewer, FINISH, unreviewed docs touch-up, SECURITY); "needs your decision" report section; open rows land under ## P1 / ## P2 in BACKLOG at accept (token-meter field note); in-flight v3.16 runs migrate. Dogfood run found and fixed a touch-up TypeError.
- ~~WI-POSTFIX: accept dropped a still-open duplicate backlog row (F022); SECURITY HOLD resume with scan hits (F018).~~ (added 2026-11-26, closed 2026-11-26, see v3.17.1)
  - F022 fixed (merged entries keep all ledger ids); F018 shown unreachable in normal use, regression test added. Opus review APPROVE, CI success, real run ACCEPTED, Fable gate PASS, dogfood run DONE.
- ~~WI-AGYEXIT: Gemini reviewer evidence and ignored-path field bugs (agy 1.5.18).~~ (added 2026-11-26, closed 2026-11-26, see v3.17.2)
  - The agy 1.5.18 exit-status header is parsed, so a gemini reviewer's configured test counts; ignored paths are free for gemini reviewer-type roles as for codex and claude (token-meter run 9cfa1025). Verified with a real run using a gemini-4-neon reviewer.
- ~~WI-SUBJDIR: a declared history-scan-subject vendor in any path component blocked PLAN approval.~~ (added 2026-11-26, closed 2026-11-26, see v3.17.3)
  - token-meter run 6b89f0b3 (codex-homes/, tests/fixtures/codex/). Reviewed by gemini-4-neon high (first Gemini review of our own code), CI success, real run ACCEPTED, Fable gate PASS, dogfood DONE.
- ~~WI-SWITCH: at a HOLD or stopped quota wait, resume moves a role to another vendor and keeps the run.~~ (added 2026-11-26, closed 2026-11-27, see v3.18.0)
  - token-meter request; owner Q1-Q3 "按建议". Opus exec, Opus review APPROVE, CI success, Fable gate PASS, real runs realrun-switch run2 (to gemini) and run3 (to claude) ACCEPTED, dogfood DONE (fixed 3 consistency issues after a switch).
