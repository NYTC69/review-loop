**Last updated**: 2026-11-24

## P0 — blocker / must-do-now

## P1 — high priority

## P2 — normal

- WI-I PLAN round cap: continue automatically like the post-gate-FAIL repair rounds (feature, owner-approved 2026-11-24; request via token-meter). (added 2026-11-24)
  - Owner: "我觉得就是要给 rl report 这个需求. 让他实现. 因为这个逻辑和门审没过很像." / "为什么每次还是需要问我. 我以为这个流程是自动化的."
  - Proposal: at max_plan_rounds with blockers open, run one more PLAN round automatically, then one final round; if still open, split the remaining findings into a follow-up work item (BACKLOG with ids, shown in the delivery report) and proceed to EXEC as if the plan passed.
  - Field evidence: token-meter run 2f6f0112 (v3.15.5): F001 (data-loss class) stayed open r1-r4, r4 added F007-F009, plan grew 324 -> 456 lines; operator used note --scope-change.
  - Owner-approved design (2026-11-24): PLAN: rounds 4 and 5 run automatically; two consecutive rounds that add new blockers trigger the cut early; after round 5 (or the early trigger) the author writes a reduced plan that moves the contested parts into a follow-up WI (finding ids to BACKLOG and the delivery report), the reviewer checks only the reduced plan (at most 1 round), then EXEC on the reduced scope; HOLD only if the blocker is in the core and nothing implementable remains. EXEC: after 5 review rounds the run passes and leftovers split into a new WI. Data-loss/security/money leftovers are listed first and highlighted in the delivery report. The coordinator records an "auto-split by rule" entry (finding ids, target WI) in place of the operator withdraw-findings.
  - Timing: after the Claude reset (11-28); send Yuan the plan summary for confirmation before EXEC (large feature).
- WI-E remaining: E3 (feature, needs owner OK during the trial). (added 2026-11-22)
  - v3.14.0 follow-up: rejections (reject --text) reach only the EXEC author; consider showing delivered rejections to the reviewer and gate like notes. (2026-11-22)
  - v3.15.5 note (Opus, WI-E plan/code review): fresh roles get finding ids masked in notes, but review-history or vendor-attribution phrases in a note (e.g. "Codex approved") still reach them; withdraw-findings refuses those. Low risk under the threat model. (2026-11-24)
- WI-F gemini / agy runtime follow-ups. (added 2026-11-22)
  - Owner 2026-11-24: paid agy plan for gemini roles from December (D-AGY); WI-F unparked: implement with fake CLIs after WI-I, real gemini run once the paid plan is active (or on free quota if available).
  - Status 2026-11-24: plan approved by Opus (F1 retry on explicit agy overload; F2 docs only; F3 dropped with a characterization test), parked by the owner until the paid-agy decision. Plan and review: .compass/results/wi-gemini2/plan.md, plan-review-r1.md (local).
  - v3.11.0 follow-up (WI-TURNFAIL note 7): an agy exit > 0 without a quota line is a terminal ERROR; consider one retry. (2026-11-22)
  - v3.10.0 follow-up (Sol review): README/ADR-21 wording on read-only recovery: a gemini reviewer that changes an existing ignored file HOLDs and leaves the edit on disk; automatic re-dispatch only follows a successful restore. (2026-11-21)
  - v3.11.0 follow-up (WI-TURNFAIL): the unknown-reset backoff slot count is per run, not reset per invoke. (2026-11-22)

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
- Cosmetic polish follow-ups (wording or duplication only; no behavior change). (added 2026-11-24)
  - v3.15.4 note (Opus, WI-G/H R1): when both disposition and evidence errors occur, the correction says "rejected" twice; could merge into one sentence. (2026-11-24)
  - v3.15.4 note (Opus, WI-G/H R1): scenario_matrix re-splits each hint candidate with LABEL_SEPARATOR, close to _label_and_content; minor duplication in a diagnostic. (2026-11-24)
  - v3.15.5 note (Opus, WI-E R1): README "When delivering a new clarification after an earlier note, the author follows…" reads as if the operator delivers; say "When a new clarification is delivered after an earlier note…". (2026-11-24)
  - v3.15.5 note (Opus, WI-E R1): no test combines a withdrawal with two delivered notes (precedence line placement); optional. (2026-11-24)
  - v3.15.6 note (Opus, WI-HISTSCAN R1): paired_session/README "A valid declaration also names its vendors for the existing bare-name exception" holds for the delta patches; say "in the delta patches" (plans exempt bare prose names by another route). (2026-11-24)
- History-scan subject follow-ups (optional; WI-HISTSCAN v3.15.6). (added 2026-11-24)
  - v3.15.6 note (Opus R1): `_history_workitem()` strips every `history-scan-subject:` line, so a malformed line (e.g. `claude unknown`) no longer counts as a mention of `claude` for FIELD-30 / tool-path lookups; consistent with "malformed grants nothing". (2026-11-24)
  - v3.15.6 note (Opus R1): `_history_workitem()` re-reads context/workitem.md per match; cache per scan if it ever matters. (2026-11-24)
  - v3.15.6 note (Opus R1): `HISTORY_SUBJECT_DIR_RE` attribution lookahead omits `reviewed`/`found` (`.codex reviewed it` is masked as a dir); the unmasked prose attribution regexes cover the realistic forms. (2026-11-24)

## Done (recent, trimmed quarterly)


- ~~WI-G cosmetic message and test-helper polish.~~ (added 2026-11-22, closed 2026-11-24, see v3.15.4)
  - G1, G3-G6 fixed in v3.15.4 (matrix near-miss hint takes the label prefix, normalization NOTE wording, retry correction matches the error kind, coordinator_fixture helper, discard docstring).
  - G2 dropped: a bold Files-to-change entry falls back to ordinary PLAN review (safe direction); only glob fallback is pinned by a test, the bold case is not.
- ~~WI-H reviewer retry record follow-ups.~~ (added 2026-11-23, closed 2026-11-24, see v3.15.4)
  - H1 fixed: a later author turn expires the saved reviewer correction. H2 fixed: a late-guard blocked approval is recorded as approval_blocked_reason (receipt and report), not discarded.
- ~~WI-E operator note semantics (E1, E2, E4).~~ (added 2026-11-22, closed 2026-11-24, see v3.15.5)
  - E1 fixed: fresh review roles (shadow, gate, POLISH-Q specialist, DOCS, SECURITY) see delivered notes with finding ids masked as [finding ID]; authors and the persistent reviewer keep the original text.
  - E2 fixed: a later delivered note takes precedence on conflicting points (handoff line on the second delivery, precedence line when two or more notes are delivered).
  - E4 dropped with a characterization test: a withdrawn covering row takes the attached report with it; the other specialist files a new row on a later eligible delta. E3 stays open (feature).
- ~~WI-HISTSCAN history scan blocks a repository whose subject is a tool (token-meter field HOLD on `.claude`).~~ (added 2026-11-24, closed 2026-11-24, see v3.15.6)
  - Fixed: a frozen work-item declaration `history-scan-subject: claude codex gemini` exempts the declared vendors' state dot dirs in the plan and both delta views; ledger ids, verdict words, attributions and history phrases stay caught; the diagnostic reports only the real hit.
- ~~CI: two macOS test groups cancelled at 20 minutes on the v3.15.5 candidate.~~ (added 2026-11-24, closed 2026-11-24, supervisor CI tooling, no plugin change)
  - Root cause: GitHub found no macOS runner (capacity), not a timeout. ci_wait.sh now reruns cancelled jobs once and never counts a cancelled job green; workflow source d14 refreshes the macOS timings (timeout stays 60).
