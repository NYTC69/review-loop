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
- Absent: legacy. Print once: `review-loop: legacy workflow via implicit entry; set "entry: paired-session" in .review-loop/config.md to opt in, or ask for "the legacy review-loop workflow" explicitly`
- Any other value (quoted or differently cased included): legacy. Print `review-loop: entry "<v>" is not valid (legacy|paired-session); using legacy workflow`
- Duplicate `entry` key, or config present but unreadable: legacy. Print `review-loop: entry could not be read (<reason>); using legacy workflow`
- `paired-session`: only fresh work (no existing plan, code target or session) hands off. Print `review-loop: paired-session entry is experimental (entry set in .review-loop/config.md)`,
  invoke the Codex `paired-session` skill (`.agents/skills/paired-session`) with
  the work item, and end this workflow (no legacy session file, lock or stage).
  A paired-session probe/run HOLD is reported and never falls back to legacy.
  Plan-exists, code-exists and explicit resume always stay legacy; print
  `review-loop: entry is paired-session but <plan exists|code exists|existing session> detected; using legacy workflow`.
  Decided once, before session creation; once a legacy session file or lock
  exists, a re-detection or user override never hands off.

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
Reviewer backend: {claude-cli ({reviewer_model | judgment_model | claude-sonnet-4-6}) | codex (review_loop_reviewer / {codex_reviewer_model})}
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
through the `reviewer_model | judgment_model | claude-sonnet-4-6` chain;
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
