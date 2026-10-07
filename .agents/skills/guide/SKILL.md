---
name: guide
description: Codex guide for review-loop - the paired-session entry, review-pr and the shared .review-loop/config.md.
---

# review-loop Guide

## What `review-loop` Does in Codex

A review-loop request goes to the Codex `review-loop` skill, which resolves the `entry` key in
`.review-loop/config.md` and hands the work item to the Codex `paired-session` skill. That skill
runs the coordinator through plan, implementation, review, finish, polish, docs and security up to
DONE; it accepts only on your explicit acceptance.
A request to review existing code hands off the same way as `run --review-only`, and an existing
plan becomes the work item (PLAN drafts and reviews it again). `entry: legacy`, asking for "the legacy
review-loop workflow" and resuming a legacy session are refused, and a failed check before the
coordinator starts refuses instead of falling back. Runs are `efficient` by
default (every sandbox, no permission-probe PASS required); `--strict` or
`"safety_mode": "strict"` in the operator profile adds the probe gate.
The legacy workflow was removed in v2.13.0 (routing) and v2.13.1 (the Codex plan and execute skills, the
Codex Stage 1 workflow and its agents). Legacy session files under `.review-loop/sessions/` are left on
disk and never read.
Details: `docs/paired-session-migration.md`.

A request to review a pull request ("review PR 123", a PR URL), a branch or
ref goes to the Codex `review-pr` skill, which runs the paired-session report mode
(`entry: legacy` and asking for the legacy review-pr are refused): it reviews it in a temporary clone with no tests
unless you confirm one, writes `review-report.md`, and fixes, commits or posts
nothing; posting is a separate request with a secret scan and a second
confirmation of the full body. `simplify` is not available there (Claude Code:
`run /review-loop:code-quality-loop on the change (its POLISH-Q simplifier)`).

## Skills in Codex

- `review-loop`, `paired-session`, `review-pr`
- `guide`

Not on Codex:

- `code-quality-loop` (ask review-loop to review an existing change instead: a review-only run with the
  quality writers on)
- `reorganize`

## Usage Notes

- The entry skills load their instructions via `docs/protocol/loading.md` and
  `scripts/read_protocol.py`, shared with Claude Code.
- Codex repo skills live under `.agents/skills/` in the Codex workspace.
- Keep the shared review-loop config in `.review-loop/config.md`; roles and models come from the
  paired-session operator profile.
