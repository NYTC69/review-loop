# Codex review-loop entry procedures

## Entry
Every review-loop request hands off to the Codex `paired-session` skill; the legacy workflow was removed in v2.13.0.
This entry only resolves `entry`, picks the handoff and runs the stage A checks; it creates no session file or lock.
Detect `--handsfree` in the invocation; when present it overrides the config
value for this invocation. The user task determines fresh work, an existing plan, or a
review-only code target; unrelated dirty work is not a code-exists signal.

Resolve `entry` (exact values `legacy` and `paired-session` only) from
`.review-loop/config.md` once, before any handoff:
- The user explicitly asks for the legacy workflow (for example "use the
  legacy review-loop workflow"): refused. Print `review-loop: the legacy workflow was removed in v2.13.0; ask for review-loop without "legacy" (paired-session is the only entry)` and end.
- Absent: paired-session, the default entry; route it exactly as `paired-session` below,
  but print `review-loop: paired-session entry (the default entry)` instead of the routed notice.
- `legacy`: refused. Print `review-loop: the legacy workflow was removed in v2.13.0; remove "entry: legacy" from .review-loop/config.md (paired-session is the only entry)` and end.
- Any other value (quoted or differently cased included): print `review-loop: entry "<v>" is not valid (paired-session is the only entry); using paired-session` and route it as `paired-session`.
- Duplicate `entry` key, or config present but unreadable: print `review-loop: entry could not be read (<reason>); using paired-session` and route it as `paired-session`.
- `paired-session`: fresh work, an existing plan and a review-only code target hand off (the legacy workflow was removed in v2.13.0). Print `review-loop: paired-session entry (entry set in .review-loop/config.md)`,
  invoke the Codex `paired-session` skill (`.agents/skills/paired-session`) with
  the work item (for a code target, as a review-only request: `--review-only`, plus
  `--base <ref>` only when the user names a base, and `--auto-commit false` when `.review-loop/config.md` sets
  `auto_commit: false` (absent or `true`: the review-only default, one local commit at `accept`); for an existing plan, first print
  `review-loop: an existing plan is used as the work item; paired-session drafts and reviews the plan again` and pass the plan text as the work item), and end this workflow (no session file, lock or stage of its own).
  A paired-session probe/run HOLD is reported and never falls back to legacy.
  An explicit resume of a legacy session is refused: print
  `review-loop: the legacy workflow was removed in v2.13.0; legacy sessions cannot be resumed: start a new run with the session's plan or work item` and end.

Stage A checks (before the first `bin/paired-session` command), read-only, the same whether the key is set or
absent; nothing falls back (there is no legacy route). A failed check refuses: print
`review-loop: paired-session entry refused (<reason>)` and end this workflow (except where a row says otherwise).
- Host: `uname -s` is not `Darwin` → `review-loop: paired-session needs macOS; Linux and other hosts are not supported` (no reason wrapper).
- CLIs: every CLI the resolved roles need is on PATH (`command -v`; the default roles need
  `codex` and `claude`); a missing one is refused with the reason `needs <cli> for the <role> role`.
- Outside-sandbox execution: the coordinator needs full host permission outside the Codex
  sandbox; if it cannot be granted, or the approval for a paired-session shell call is declined → HOLD
  with the reason, as the paired-session skill does.
- Codex home: `${CODEX_HOME:-$HOME/.codex}` is not an existing directory → refused with the
  coordinator's CODEX_HOME reason.
- Questions and host setup (run directory, `WORKITEM.md`, loading the shared contract, the plugin-version floor):
  the paired-session skill asks its own stage A questions (dedicated worktree, test command and its shape; under
  `--handsfree` or `handsfree: true` nobody answers, so any such question fails) and reports a failure as
  `stage A failure: <reason>`, which this entry prints as the refusal above.
From the first `bin/paired-session` command on, every refusal or HOLD is reported verbatim and never falls back to legacy.
