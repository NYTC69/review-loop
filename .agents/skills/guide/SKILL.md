---
name: guide
description: Codex Stage 1 guide for review-loop and the shared Claude/Codex state model.
---

# review-loop Guide

## What `review-loop` Does in Codex

In Codex Stage 1, `review-loop` is a repo skill that follows the same broad
review workflow used in Claude Code. It coordinates planning, implementation,
and review while keeping the shared session log current.

Codex reads and writes the same review-loop state as Claude Code:

- `.review-loop/config.md`
- `.review-loop/sessions/`

That means a project can keep one shared config file and one shared session log
history across both runtimes.

## Stage 1 Scope

Stage 1 in Codex includes only:

- `review-loop`
- `guide`

It does not yet migrate:

- `code-quality-loop`
- `review-pr`
- `reorganize`

## Reviewer Behavior

Codex Stage 1 defaults to the Claude CLI reviewer path. In practice, that means
Codex tries `claude -p` first for review work.

If the Claude CLI reviewer path is not available or cannot be used, Codex can
fall back to its local reviewer backend. You can force that fallback path with:

- `codex_reviewer_backend: codex`

This is the override to use when you want Codex to skip the Claude CLI reviewer
and use the Codex reviewer directly. In that case, `codex_reviewer_model` is
the paired model override, while `reviewer_model` still applies to the Claude
CLI reviewer path.

## Usage Notes

- Codex repo skills live under `.agents/skills/` in the Codex workspace.
- Keep the shared review-loop config in `.review-loop/config.md`.
- Keep session logs in `.review-loop/sessions/`.
- Use the Codex fallback reviewer only when you need to bypass the default
  Claude CLI reviewer path.
