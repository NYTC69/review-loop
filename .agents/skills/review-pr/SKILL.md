---
name: review-pr
description: Review a GitHub pull request ("review PR 123", a PR URL), a branch or ref, or the local change and write a review report, the review-loop review-pr. With `entry` absent or `paired-session` in .review-loop/config.md it runs the paired-session report mode (no writer, nothing committed or posted); with `entry: legacy`, or when the user asks for "the legacy review-pr workflow", it runs the legacy review-pr. Not for implementing a work item (review-loop).
---

# review-pr (Codex)

Resolve `entry` in `.review-loop/config.md` as the review-loop entry does (exact values `legacy` and
`paired-session`; an invalid value, a duplicate key or an unreadable config is legacy, with that entry's
warning line). `<support-root>` is this plugin, not the task workspace.

- The user asks for "the legacy review-pr workflow", or `entry` is `legacy` (or invalid): the legacy
  route. Read `<support-root>/skills/review-pr/SKILL.md` and follow its legacy review (Step 1 on); its
  report-only reviewers run through this host's reviewer launcher under
  `<support-root>/docs/protocol/reviewer-runtime.md`. `simplify` (a writer through the Claude Agent tool)
  is not available on Codex: say so and point to `/review-loop:review-pr --legacy simplify` in Claude
  Code. A PR number, PR URL or ref on the legacy route is refused:
  `review-pr: the legacy review reads only the local diff; review a PR or ref on the paired route`.
- `entry` absent or `paired-session`: the paired route. Print
  `review-pr: paired-session report mode (entry set in .review-loop/config.md)` when the key is set, or
  `review-pr: paired-session report mode is the default entry; set "entry: legacy" in .review-loop/config.md or ask for "the legacy review-pr workflow"`
  when it is absent. Hand the request (input and aspects) to the Codex `paired-session` skill as a
  review-pr handoff and end this skill. It loads the shared contract and follows its Review-PR entry: a
  PR or ref is reviewed in a temporary clone, no tests run unless the user confirms a test command, the
  result is `review-report.md`, and nothing is posted unless the user asks.
  - `simplify` is refused there before any coordinator command, with the pointer to
    `/review-loop:review-pr --legacy simplify` (Claude Code).
  - The no-argument request with the key absent: if the paired-session skill reports
    `stage A failure: <reason>`, print
    `review-pr: paired-session default entry unavailable (<reason>); using the legacy review` and take
    the legacy route, without `simplify`. With the key set, or for a PR or ref input, report the failure
    and stop (never fall back).
