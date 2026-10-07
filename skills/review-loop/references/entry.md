# claude review-loop entry procedures

Every `/review-loop` request hands off to the `paired-session` skill; the legacy workflow was removed in v2.13.0.
This entry only resolves `entry`, picks the handoff (Step 1.5) and runs the stage A checks.

### Step 0 — Load config and parse flags

Read `.review-loop/config.md` (or defaults). Detect `--handsfree`.

Resolve `entry` (exact values `legacy` and `paired-session` only; the legacy workflow was removed in v2.13.0):
- Absent: paired-session, the default entry; route it exactly as `paired-session` below.
- `legacy`: refused. Print `review-loop: the legacy workflow was removed in v2.13.0; remove "entry: legacy" from .review-loop/config.md (paired-session is the only entry)` and end.
- Any other value (quoted or differently cased included): print `review-loop: entry "<v>" is not valid (paired-session is the only entry); using paired-session` and route it as `paired-session`.
- Duplicate `entry` key, or config present but unreadable: print `review-loop: entry could not be read (<reason>); using paired-session` and route it as `paired-session`.
- `paired-session`: apply the Step 1.5 entry routing; this entry creates no session file or lock.

### Step 1.5 — Entry routing

**Entry routing (every `/review-loop` request; the legacy workflow was removed in v2.13.0).**
- No prior state: hand off with the work item.
- Code already implemented: hand off as a review-only request (for code already implemented, with
`--review-only`, plus `--base <ref>` only when the user names a base). Code already implemented is detected
from task-relevant changes only: unrelated dirty work is not a code-exists signal (a
review-only run reviews and, with auto_commit, delivers every non-ignored change against its
base). `auto_commit: false` in `.review-loop/config.md` is handed over as `--auto-commit false` (absent or `true`: the review-only default, one local commit at `accept`).
- Plan already exists: print `review-loop: an existing plan is used as the work item; paired-session drafts and reviews the plan again` and hand off with the plan text in the work item.
- Existing session (an explicit resume of a `.review-loop/sessions/` session): refused. Print
  `review-loop: the legacy workflow was removed in v2.13.0; legacy sessions cannot be resumed: start a new run with the session's plan or work item` and end.
To hand off, print `review-loop: paired-session entry (entry set in .review-loop/config.md)` when the key is set, or
`review-loop: paired-session entry (the default entry)` when it is absent, invoke the `paired-session` skill, and end
this workflow (no session file, lock or stage of its own). A paired-session probe/run HOLD is
reported to the user and never falls back to legacy.

**Stage A checks (before the first `bin/paired-session` command).** Read-only, the same whether the key is set or
absent; nothing falls back (there is no legacy route). A failed check refuses: print
`review-loop: paired-session entry refused (<reason>)` and end this workflow (except where a row says otherwise).
- Host: `uname -s` is not `Darwin` → `review-loop: paired-session needs macOS; Linux and other hosts are not supported` (no reason wrapper).
- CLIs: every CLI the resolved roles need is on PATH (`command -v`; the default roles need
  `codex` and `claude`); a missing one is refused with the reason `needs <cli> for the <role> role`.
- Background commands: this host cannot run long background commands (or Codex cannot run
  outside its sandbox) → report HOLD with the reason, as the paired-session skill does.
- Codex home: a role is Codex and `${CODEX_HOME:-$HOME/.codex}` is not an existing directory
  → refused with the coordinator's CODEX_HOME reason.
- Questions and host setup: the paired-session skill asks its own stage A questions (dedicated worktree,
  test command and its shape; under `--handsfree` or `handsfree: true` nobody answers, so any such question
  fails) and sets up the run (run directory, `WORKITEM.md`, the shared contract); it reports a failure as
  `stage A failure: <reason>`, which this entry prints as the refusal above.
From the first `bin/paired-session` command on, every refusal or HOLD is reported verbatim and never falls back to legacy.
