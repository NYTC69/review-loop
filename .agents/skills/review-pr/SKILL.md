---
name: review-pr
description: Review a GitHub pull request ("review PR 123", a PR URL) or a named branch or ref and write a review report, the review-loop review-pr; also when the user explicitly asks for review-pr. A general request to review local changes ("review my changes") is not this skill (review-loop). It runs the paired-session report mode (no writer, nothing committed or posted); the legacy review-pr was removed in v2.13.0, so `entry: legacy` and a request for "the legacy review-pr workflow" are refused. Not for implementing a work item (review-loop).
---

# review-pr (Codex)

Resolve `entry` in `.review-loop/config.md` (exact values `legacy` and `paired-session`): `legacy` is
refused with `review-pr: the legacy workflow was removed in v2.13.0; remove "entry: legacy" from .review-loop/config.md (paired-session is the only entry)`; an invalid value prints
`review-pr: entry "<v>" is not valid (paired-session is the only entry); using paired-session`, a duplicate key or an
unreadable config prints `review-pr: entry could not be read (<reason>); using paired-session`, and both continue as
absent. `<support-root>` is this plugin, not the task workspace.

- The user asks for "the legacy review-pr workflow": refused with
  `review-pr: the legacy workflow was removed in v2.13.0; ask for a review-pr without the legacy workflow (report mode)`.
- A host that is not macOS (`uname -s` is not `Darwin`): refused with `review-pr: paired-session needs macOS; Linux and other hosts are not supported`.
- Otherwise the paired route. Print
  `review-pr: paired-session report mode (entry set in .review-loop/config.md)` when the key is set, or
  `review-pr: paired-session report mode (the default entry)`
  when it is absent. Hand the request (input and aspects) to the Codex `paired-session` skill as a
  review-pr handoff and end this skill. It loads the shared contract and follows its Review-PR entry: a
  PR or ref is reviewed in a temporary clone, no tests run unless the user confirms a test command, the
  result is `review-report.md`, and nothing is posted unless the user asks.
  - `simplify` is refused there before any coordinator command, with the pointer
    `run /review-loop:code-quality-loop on the change (its POLISH-Q simplifier)` (Claude Code).
  - If the paired-session skill reports `stage A failure: <reason>`, report
    `review-pr: paired-session entry refused (<reason>)` and stop (nothing falls back).
