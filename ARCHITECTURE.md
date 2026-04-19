# review-loop — Architecture

**Last updated**: 2026-04-19

## Overview

<one paragraph: what this system does, at the highest level>

## Module layout

<module tree or list: which files live where, one-line purpose each>

## Data flow

<how data moves through the system end-to-end>

## Our schemas

<tables, message formats, internal contracts our code owns>

<!-- 迁移自 README.md:26-42 via compass:adopt 于 2026-04-19 plan=a8d9343ef0c1 -->
## Migrated — README.md:26-42

## Codex Stage 1

Codex uses repo skills under `.agents/skills/`. In Stage 1, the Codex
`review-loop` skill shares `.review-loop/config.md` and `.review-loop/sessions/`
with Claude Code, so both runtimes work against the same project state.
The rest of this README primarily documents the current Claude Code plugin
surface; Codex Stage 1 currently exposes only `review-loop` and `guide`.

The default reviewer path in Codex Stage 1 uses the Claude CLI reviewer
(`claude -p`). If you need to force the Codex fallback reviewer, set
`codex_reviewer_backend: codex` in `.review-loop/config.md`.
The shared `reviewer` and `executor_model` keys do not actively control
Stage 1 Codex reviewer/backend selection.
In Codex Stage 1, `executor_model` is ignored and `codex_executor_model` remains reserved.

Stage 1 does not yet migrate `code-quality-loop`, `review-pr`, or `reorganize`.
