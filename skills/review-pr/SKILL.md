---
name: review-pr
description: "Comprehensive code review using specialized agents. Each agent focuses on a different quality aspect (code, errors, comments, types, tests). It runs the paired-session report mode (a PR, a ref or the local change; no writer, nothing posted); the legacy review was removed in v2.13.0, so `entry: legacy` and `--legacy` are refused."
argument-hint: "[PR number|PR URL|ref] [aspects: code|errors|comments|types|tests|all]"
---

# Comprehensive Code Review

Run a comprehensive code review using multiple specialized agents, each focusing
on a different aspect of code quality, as a paired-session report mode run (Step 0).

**Review Aspects (optional):** "$ARGUMENTS"

---

## Step 0 — Entry: paired route (the legacy workflow was removed in v2.13.0)

Resolve `entry` in `.review-loop/config.md` (exact values `legacy` and `paired-session`): `legacy` is refused with
`review-pr: the legacy workflow was removed in v2.13.0; remove "entry: legacy" from .review-loop/config.md (paired-session is the only entry)`; an invalid value prints `review-pr: entry "<v>" is not valid (paired-session is the only entry); using paired-session`,
a duplicate key or an unreadable config prints `review-pr: entry could not be read (<reason>); using paired-session`, and
both continue as absent. Then:
- `--legacy` in `$ARGUMENTS`: refused with `review-pr: the legacy workflow was removed in v2.13.0; run /review-loop:review-pr without --legacy (report mode)`.
- A host that is not macOS (`uname -s` is not `Darwin`): refused with `review-pr: paired-session needs macOS; Linux and other hosts are not supported`.
- Otherwise the paired route. Print
  `review-pr: paired-session report mode (entry set in .review-loop/config.md)` when the key is set, or
  `review-pr: paired-session report mode (the default entry)`
  when it is absent. Invoke the `paired-session` skill with `--review-pr` and the remaining arguments
  (input and aspects), and end this skill. It follows the shared contract's Review-PR entry: a PR or ref
  is reviewed in a temporary clone, no tests run unless you confirm a test command, the result is
  `review-report.md`, and nothing is posted unless you ask.
  - `simplify` is refused there before any coordinator command, with the pointer
    `run /review-loop:code-quality-loop on the change (its POLISH-Q simplifier)`.
  - If the paired-session skill reports `stage A failure: <reason>`, report
    `review-pr: paired-session entry refused (<reason>)` and stop (nothing falls back).

---

## Usage Examples

**Default route (paired-session report mode; `entry` absent or `paired-session`, see Step 0):**
```
/review-loop:review-pr 123
# Reviews PR #123 of this repository in a temporary clone; writes review-report.md, posts nothing

/review-loop:review-pr https://github.com/owner/repo/pull/123 code tests
# Only the code and tests specialists (the reviewer, shadow, gate and security stage always run)
```

---

## Agent Descriptions

**code-reviewer**:
- Checks CLAUDE.md / project guideline compliance
- Detects bugs, logic errors, and anti-patterns
- Reviews general code quality and style

**silent-failure-hunter**:
- Finds silent failures and swallowed errors
- Reviews catch blocks and error propagation
- Checks error logging adequacy

**comment-analyzer**:
- Verifies comment accuracy vs actual code
- Identifies comment rot and stale docs
- Checks documentation completeness

**type-design-analyzer**:
- Analyzes type encapsulation and invariants
- Reviews type design quality
- Rates invariant expression strength

**pr-test-analyzer**:
- Reviews behavioral test coverage
- Identifies critical test gaps
- Evaluates test quality and assertions
