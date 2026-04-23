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

## Stage 1 Scope

Stage 1 in Codex includes only:

- `review-loop`
- `guide`

It does not yet migrate:

- `code-quality-loop`
- `review-pr`
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
explicit `claude-sonnet-4-6` backstop.

`cheap_model` is accepted in the shared config so Claude and Codex can share
the same file, but in Codex Stage 1 it is a documented no-op because only
judgment-tier Codex agents are currently shipped.
`quality_focus` applies only when Step 3.5 Quality Polish actually runs.
`skip_quality_polish: true` mints `polish` as a no-op completion and still continues through docs and security.

## Usage Notes

- Executor-created hidden worktrees are forbidden in Codex Stage 1.
- Codex Stage 1 supports `before-polish`, `before-docs`, and `before-security` as clean stop points.
- Codex repo skills live under `.agents/skills/` in the Codex workspace.
- Keep the shared review-loop config in `.review-loop/config.md`.
- Keep session logs in `.review-loop/sessions/`.
- Use the local Codex reviewer only when you need to bypass the default
  Claude CLI reviewer path explicitly.
