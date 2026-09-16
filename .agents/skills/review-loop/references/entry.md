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
