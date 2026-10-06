---
name: guide
description: Codex Stage 1 guide for review-loop and the shared Claude/Codex state model.
---

# review-loop Guide

## What `review-loop` Does in Codex

In Codex Stage 1, `review-loop` is a repo skill that follows the same broad
review workflow used in Claude Code. It coordinates planning, implementation,
and review while keeping the shared session log current.
Codex Stage 1 follows the same broad `exec -> polish -> docs -> security -> delivery` lifecycle.

Codex reads and writes the same review-loop state as Claude Code:

- `.review-loop/config.md`
- `.review-loop/sessions/`

That means a project can keep one shared config file and one shared session log
history across both runtimes.
Codex Stage 1 assumes a single orchestrator-owned workspace for the session.

## Entry: paired-session default, legacy on request

From v2.10.0 a fresh review-loop request with no `entry` key in
`.review-loop/config.md` hands off to the Codex `paired-session` skill, which
runs the coordinator through plan, implementation, review, finish, polish,
docs and security up to DONE; it accepts only on your explicit acceptance.
A request to review existing code hands off the same way as `run --review-only`.
`entry: legacy`, an existing plan or session, or asking for "the legacy
review-loop workflow" keeps the legacy workflow described above. Runs are `efficient` by
default (every sandbox, no permission-probe PASS required); `--strict` or
`"safety_mode": "strict"` in the operator profile adds the probe gate.
The legacy workflow is deprecated since v2.12.0. It still runs unchanged; `entry: legacy`,
asking for "the legacy review-loop workflow", and the plan and execute skills invoked on
their own print a one-line deprecation notice. review-pr and code-quality-loop follow `entry`; removal
waits for the open owner rows of the legacy map.
Details: `docs/paired-session-migration.md`.

A request to review a pull request ("review PR 123", a PR URL), a branch or
ref goes to the Codex `review-pr` skill, which follows `entry` the same way:
the paired-session report mode reviews it in a temporary clone with no tests
unless you confirm one, writes `review-report.md`, and fixes, commits or posts
nothing; posting is a separate request with a secret scan and a second
confirmation of the full body. `simplify` is not available there (Claude Code:
`/review-loop:review-pr --legacy simplify`). For the legacy review, ask for
"the legacy review-pr workflow".

## Stage 1 Scope

Stage 1 in Codex includes:

- `review-loop`, `plan`, `execute`, `paired-session`, `review-pr`
- `guide`

Not on Codex:

- `code-quality-loop` (ask review-loop to review an existing change instead: a review-only run with the
  quality writers on)
- `reorganize`

## Reviewer Behavior

Codex Stage 1 defaults to the outside-sandbox Claude CLI reviewer path. In
practice, that means review stays on `claude -p --model ...` unless the user
explicitly opts into the local Codex reviewer.

You can force the local Codex reviewer with:

- `codex_reviewer_backend: codex`

This is the override to use when you want Codex to skip the Claude CLI reviewer
and use the Codex reviewer directly. In that case, `codex_reviewer_model` is
the paired model override, while `reviewer_model` still applies to the Claude
CLI reviewer path and `judgment_model` is its shared-tier fallback before the
explicit `claude-opus-5-5` backstop.

`cheap_model` is accepted in the shared config so Claude and Codex can share
the same file, but in Codex Stage 1 it is a documented no-op because only
judgment-tier Codex agents are currently shipped.
`quality_focus` applies only when Step 3.5 Quality Polish actually runs.
`skip_quality_polish: true` mints `polish` as a no-op completion and still continues through docs and security.

## Usage Notes

- Instructions load by active action via `docs/protocol/loading.md` and
  `scripts/read_protocol.py`, shared with Claude Code. Only still-available,
  unchanged units can be reused in one live context; resume/compaction and
  independent agents reload their prerequisites. All lifecycle gates remain.

- Executor-created hidden worktrees are forbidden in Codex Stage 1.
- Codex Stage 1 supports `before-polish`, `before-docs`, and `before-security` as clean stop points.
- Codex repo skills live under `.agents/skills/` in the Codex workspace.
- Keep the shared review-loop config in `.review-loop/config.md`.
- Keep session logs in `.review-loop/sessions/`.
- Use the local Codex reviewer only when you need to bypass the default
  Claude CLI reviewer path explicitly.
