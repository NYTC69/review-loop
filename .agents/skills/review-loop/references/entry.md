# Codex umbrella entry procedures

## Entry
Every review-loop request resolves `entry` and routes as below (the `entry` list and the stage A
checks); the rest of this paragraph, `## Initialize / route` and `## Startup Banner` describe the legacy
workflow and apply only to the explicit legacy request (removed in v2.13.1).
Detect `--handsfree` in the invocation; when present it overrides the config
value for this invocation. Handsfree alone never accepts external drift.
Resolve config through runtime-codex.md. If explicitly resuming, use the
execute entry's UUID/lock/resume sequence and retain original entry_point,
phase, history and unresolved state. Otherwise allocate a new UUID.
The user task determines fresh planning, existing-plan execution, or a
review-only code target; unrelated dirty work is not a code-exists signal.
Use the corresponding plan/execute entry procedures under the same session,
with entry_point: review-loop. Do not launch nested skill sessions.
Before any creation or resume read, acquire the shared single-writer lock.

Resolve `entry` (exact values `legacy` and `paired-session` only) from
`.review-loop/config.md` once, before allocating a UUID or acquiring the lock:
- The user explicitly asks for the legacy workflow (for example "use the
  legacy review-loop workflow"): ignore `entry`, do not read or validate it, and
  print none of its notices; print only the deprecation notice below. (This request
  is the Codex counterpart of `/review-loop:legacy`; it goes with the legacy skills in v2.13.1.)
- Absent: paired-session, the default entry; route it exactly as `paired-session` below,
  but print `review-loop: paired-session entry (the default entry)` instead of the routed notice.
- `legacy`: refused. Print `review-loop: the legacy workflow was removed in v2.13.0; remove "entry: legacy" from .review-loop/config.md (paired-session is the only entry)` and end.
- Any other value (quoted or differently cased included): print `review-loop: entry "<v>" is not valid (paired-session is the only entry); using paired-session` and route it as `paired-session`.
- Duplicate `entry` key, or config present but unreadable: print `review-loop: entry could not be read (<reason>); using paired-session` and route it as `paired-session`.
- `paired-session`: fresh work, an existing plan and a review-only code target hand off (the legacy workflow was removed in v2.13.0). Print `review-loop: paired-session entry (entry set in .review-loop/config.md)`,
  invoke the Codex `paired-session` skill (`.agents/skills/paired-session`) with
  the work item (for a code target, as a review-only request: `--review-only`, plus
  `--base <ref>` only when the user names a base; for an existing plan, first print
  `review-loop: an existing plan is used as the work item; paired-session drafts and reviews the plan again` and pass the plan text as the work item), and end this workflow (no legacy session file, lock or stage).
  A paired-session probe/run HOLD is reported and never falls back to legacy.
  An explicit resume of a legacy session is refused: print
  `review-loop: the legacy workflow was removed in v2.13.0; legacy sessions cannot be resumed: start a new run with the session's plan or work item` and end.

Deprecation notice (the explicit legacy request only; once, before the UUID and lock; it changes
no routing): `review-loop: legacy is deprecated since v2.12.0; the default paired-session entry covers fresh work, review of existing changes, review-pr and code-quality-loop; removal is planned after the open legacy-map rows are settled`

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

## Initialize / route
For fresh work use the plan initialization, work-item parsing and Step 1.6
historical-context retrieval from .agents/skills/plan/references/entry.md.
On the legacy route (the explicit legacy request only), for an existing plan/code target or explicit resume use the corresponding
mode in .agents/skills/execute/references/entry.md and the shared init table.
Load plan-init or execute-init accordingly. On planning APPROVE continue
execution in this same session. Preserve the one-fetch historical-context
dedup and print the startup banner only once.

## Startup Banner

At entry-point detection, immediately after Runtime Identity is resolved
and Config Loading has populated the runtime fields, print the following
banner once. This block is pure UX — it does not change reviewer
dispatch, schema validation, or `completed_stages` minting.

```
── review-loop: Starting ──────────────────────────
Work item: {title}
Problem: {problem_description}
Reviewer backend: {claude-cli ({reviewer_model | judgment_model | claude-opus-5-5}) | codex (review_loop_reviewer / {codex_reviewer_model})}
Mode: {interactive | handsfree}
Soft limit: {soft_limit_plan} (plan) / {soft_limit_exec} (exec)
{if `## Historical Context` was populated by the plan sub-skill: Historical context: {N} relevant memories loaded}
────────────────────────────────────────────────────
```

Print this banner once per session, immediately after entry-point
detection and before the first sub-skill dispatch. Do not reprint on
sub-skill resume or per-round dispatch.

The `Reviewer backend` row uses backend-appropriate labels resolved per
§Config Loading: the `claude-cli` branch shows the model resolved
through the `reviewer_model | judgment_model | claude-opus-5-5` chain;
the `codex` branch shows the local Codex reviewer agent name plus
`codex_reviewer_model`. Codex Stage 1 ignores the shared `reviewer`
config key for backend selection (per §Config Loading), so the banner
does not surface that key.

Note on the `Historical context` row: the umbrella does not run Step 1.6
inline. The plan sub-skill at `.agents/skills/plan/SKILL.md` Step 1.6
owns historical-context retrieval, and resume-dedup keeps end-to-end
behavior at exactly 1 fetch per session. The umbrella surfaces the count
in the banner only when `## Historical Context` has already been
populated by that sub-skill before the banner is rendered (e.g. on
resume); otherwise the row is omitted.
