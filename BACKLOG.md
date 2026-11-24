**Last updated**: 2026-11-25

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
- Optional small follow-ups from review notes (regrouped 2026-11-25; v3.15.5-v3.16.2). (added 2026-11-25)
  - v3.15.5 note (Opus, WI-E R1): no test combines a withdrawal with two delivered notes (precedence line placement); optional. (2026-11-24)
  - v3.16.0 note (Opus R1): data-loss/security/money first ordering exists only in the delivery report; run and repository backlog entries list transferred findings in ledger order. (2026-11-24)
  - v3.16.0 note (Opus R2): a round-5 shadow HOLD also stops an APPROVE; the resume re-dispatches reviewer and shadow and the fixed round-5 allowance (4) may not cover a second pass, so the operator may need resume --max-invocations. (2026-11-24)
  - v3.16.2 note (Opus R1): after an operator-recheck still_open HOLD the owner count is 1, so a second operator edit falls through to an author turn instead of a direct recheck (safe; owner closure still required); document it or grant a direct recheck whenever operator_hold sees a changed tree. (2026-11-24)
  - v3.16.2 note (Opus R1): the operator grant does not pre-check the invocation limit like the author path; the recheck HOLDs at the limit and resume with a higher --max-invocations continues (clearer message possible). (2026-11-24)
  - v3.16.4 note (Opus R1, Fable gate): no test for a second stale replay on the same tree at the reviewer-result replay site (coordinator.py ~10485); only the FINISH site is tested. (2026-11-25)
  - v3.16.4 note (Fable gate): a stale approval that re-routes no longer passes hold(), so its exec_comparisons effective_verdict stays APPROVE and comparison.md shows it as effective (display only). (2026-11-25)
  - v3.16.4 note (Opus R1): the non-lifecycle stop-after-gate re-route (coordinator.py ~5379) has no per-tree bound and a different NOTE wording than _rereview_stale_exec_approval; README "can HOLD" on a repeat could say "HOLDs"; the test_opv_replay fixture pause is not named in the CHANGELOG. (2026-11-25)
## Done (recent, trimmed quarterly)


- ~~WI-F gemini / agy runtime follow-ups.~~ (added 2026-11-22, closed 2026-11-24, see v3.16.1)
  - F1 fixed: an agy positive exit whose terminal JSON ERROR carries capacity/overload wording gets one transient retry (no-work attempts only; quota classification first).
  - F2 docs: READMEs and ADR-21 match the code (verified restore allows one re-dispatch; an ignored-file edit voids the verdict and HOLDs with the edit on disk).
  - F3 dropped: backoff slots are already per dispatch (cleared on success) with a run-wide wait budget; pinned by test_unknown_reset_backoff_restarts_for_next_dispatch.
- ~~WI-POLBUDGET POLISH-Q specialist-budget HOLD has no exit after an operator fix (token-meter field blocker, run 1862edcf).~~ (added 2026-11-24, closed 2026-11-24, see v3.16.2)
  - Fixed: at that HOLD a plain resume on a changed tree (operator edit) resets the exhausted owners' epoch counts and dispatches their recheck (no author turn); clear -> new epoch with EXEC replay; still open -> HOLD. Security rows stay owner-only; held runs resume in place.
- ~~WI-OPVVOID resume voids operator test records bound to the live tree; shadow EXEC HOLD loops (token-meter field blocker, run 1862edcf).~~ (added 2026-11-24, closed 2026-11-25, see v3.16.3)
  - Fixed: operator-verification lookups are read-only; records are voided only against the live tree (resume at EXEC reviewer/gate, accept, writer turns, attach). Held runs: attach-verification again on the live tree, then resume.
- ~~WI-STALEFIX stale EXEC approval routes to re-review; docs/comment cleanups.~~ (added 2026-11-25, closed 2026-11-25, see v3.16.4)
  - Fixed: a reviewer approval of an older tree re-routes to the persistent reviewer in the same resume (once per live tree; a repeat on the same tree HOLDs as before; the gate stays spent).
  - Docs/comments: combined correction sentence, README clarification and delta-patch wording, operator_hold named, prompt_block live-tree docstring, WI-I CHANGELOG mention; scenario_matrix split kept (_label_and_content validates labels, so it cannot give the raw typo-hint prefix).
- ~~Review notes accepted as-is (no action; owner 2026-11-25 cleanup).~~ (added 2026-11-24, closed 2026-11-25, see v3.16.4)
  - History-scan: malformed declaration strips the line (consistent with "malformed grants nothing"); per-match workitem re-read (no cost issue); `.codex reviewed it` lookahead (prose regexes cover realistic forms).
  - Auto-split: transferred-scope line from finding summaries (descriptive); generous round 4-5 allowances (harmless).
  - OPV: old-tree records stay current until the next prompt_block/writer turn (lookups match the exact tree); "run every OPV-status module" moved to memory as a process lesson.
