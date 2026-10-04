# Codex umbrella entry procedures

## Entry
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
  print none of its notices.
- Absent: paired-session, the default entry; route it exactly as `paired-session` below,
  but print `review-loop: paired-session is the default entry; set "entry: legacy" in .review-loop/config.md or ask for "the legacy review-loop workflow"` instead of the routed notice.
- `legacy`: legacy, with no entry notice.
- Any other value (quoted or differently cased included): legacy. Print `review-loop: entry "<v>" is not valid (legacy|paired-session); using legacy workflow`
- Duplicate `entry` key, or config present but unreadable: legacy. Print `review-loop: entry could not be read (<reason>); using legacy workflow`
- `paired-session`: only fresh work (no existing plan, code target or session) hands off. Print `review-loop: paired-session entry (entry set in .review-loop/config.md)`,
  invoke the Codex `paired-session` skill (`.agents/skills/paired-session`) with
  the work item, and end this workflow (no legacy session file, lock or stage),
  unless the skill reports a failed stage A check before its first
  `bin/paired-session` command (below). Print the notice and hand off only after
  this entry's own stage A checks (host, CLIs, outside-sandbox execution, Codex home)
  pass; the question checks run in the paired-session skill after the handoff.
  A paired-session probe/run HOLD is reported and never falls back to legacy.
  Plan-exists, code-exists and explicit resume always stay legacy; print
  `review-loop: entry is paired-session but <plan exists|code exists|existing session> detected; using legacy workflow`.
  Decided once, before session creation; once a legacy session file or lock
  exists, a re-detection or user override never hands off.

Stage A checks (before the first `bin/paired-session` command), read-only. The host, CLI,
outside-sandbox and Codex-home rows run here before the handoff; the Questions row runs in
the paired-session skill. With the key absent, a failed check falls back to legacy: print
its notice and continue with the legacy entry above (allocate the UUID, acquire the lock,
then `## Initialize / route`); no UUID or lock exists yet. With
`entry: paired-session`, a failed check refuses: print
`review-loop: paired-session entry refused (<reason>); set "entry: legacy" or ask for "the legacy review-loop workflow"`
and end this workflow (except where a row says otherwise).
- Host, key absent only: `uname -s` is not `Darwin` → `review-loop: paired-session default entry needs a verified host (macOS); using legacy workflow`.
  With the key set there is no host check; in strict mode the permission probe decides, in efficient mode nothing does.
- CLIs: every CLI the resolved roles need is on PATH (`command -v`; the default roles need
  `codex` and `claude`). Missing → `review-loop: paired-session default entry needs <cli> for the <role> role; using legacy workflow`.
- Outside-sandbox execution: the coordinator needs full host permission outside the Codex
  sandbox; if it cannot be granted → `review-loop: paired-session default entry unavailable (<reason>); using legacy workflow`;
  with the key set, report HOLD with the reason, as the paired-session skill does today.
  A declined outside-sandbox approval for the paired-session skill's first shell call is
  also a failed stage A check (key absent: this unavailable notice and legacy; key set: HOLD).
- Codex home: `${CODEX_HOME:-$HOME/.codex}` is not an existing directory → the same
  unavailable notice with the coordinator's CODEX_HOME reason.
- Questions: the paired-session skill asks its own stage A questions (dedicated worktree,
  test command and its shape). A declined or unanswered question is a failed check and the
  skill reports it before its first `bin/paired-session` command; with the key absent this
  workflow then prints the unavailable notice with that reason and continues with the legacy
  entry above. Under `--handsfree` or `handsfree: true` nobody answers, so such a question is a
  failed check: `review-loop: paired-session default entry needs an answer (<question>) that handsfree cannot give; using legacy workflow`.
- Host setup (run directory, `WORKITEM.md`, loading the shared contract, the plugin-version floor): a failure the
  paired-session skill reports as `stage A failure: <reason>` is a failed check; with the key absent print
  `review-loop: paired-session default entry unavailable (<reason>); using legacy workflow` and continue with the legacy entry above.
After a handoff, legacy continues only through one of these notices: never allocate the legacy UUID, lock or session file
for a handed-off work item without first printing the fallback notice, and never fall back silently.
From the first `bin/paired-session` command on, every refusal or HOLD is reported verbatim and never falls back to legacy.

## Initialize / route
For fresh work use the plan initialization, work-item parsing and Step 1.6
historical-context retrieval from .agents/skills/plan/references/entry.md.
For an existing plan/code target or explicit resume use the corresponding
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
