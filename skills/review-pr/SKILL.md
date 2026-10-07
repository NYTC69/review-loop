---
name: review-pr
description: "Comprehensive code review using specialized agents. Each agent focuses on a different quality aspect (code, errors, comments, types, tests, simplify). It runs the paired-session report mode (a PR, a ref or the local change; no writer, nothing posted); the legacy review was removed in v2.13.0, so `entry: legacy` and `--legacy` are refused."
argument-hint: "[PR number|PR URL|ref] [aspects: code|errors|comments|types|tests|all]"
---

# Comprehensive Code Review

Run a comprehensive code review using multiple specialized agents, each focusing
on a different aspect of code quality. Report-only reviewers use fresh isolated
native CLI processes; `code-simplifier` remains a writer via the Agent tool.

At entry, read [the reviewer runtime contract](../../docs/protocol/reviewer-runtime.md).
Its launcher, permission, completion, and accounting requirements apply to every
report-only dispatch below. A general-purpose Agent is not a read-only boundary.

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

The sections below are the legacy review, unreachable since v2.13.0 (deleted in v2.13.1).

---

## Step 1 — Determine Review Scope

1. Run `git diff --name-only` to identify unstaged changed files (this is the
   default scope). If there are no unstaged changes, fall back to
   `git diff --cached --name-only` (staged changes).
2. If no changes are found at all, tell the user and stop.
3. Parse `$ARGUMENTS` to detect:
   - **Aspect selection**: one or more of `code`, `errors`, `comments`, `types`,
     `tests`, `simplify`, `all`. Default: `all`.
   - **Execution mode**: `parallel` keyword triggers parallel mode. Default:
     sequential.
4. Run `git diff` (or `git diff --cached`) to capture the full diff content —
   this is what agents will review.

Display to the user:
```
── review-pr ──────────────────────────────────────
Scope: git diff (unstaged changes)
Files: {N} changed
Aspects: {selected aspects or "all"}
Mode: {sequential | parallel}
────────────────────────────────────────────────────
```

## Step 1.5 — Load shared tier config

Read `.review-loop/config.md` if it exists. Extract:

- `judgment_model`: shared tier override for judgment-tier review agents
- `cheap_model`: shared tier override for cheap-tier review agents; if absent,
  cheap-tier dispatches backstop to `claude-opus-5-5`
- `review_style`: optional review tone / cross-cutting rules

Missing agent `tier` defaults to `judgment`.

---

## Step 2 — Available Review Aspects

Each aspect maps to a specialized agent in the `agents/` directory:

| Aspect       | Agent                  | When applicable                     |
|--------------|------------------------|-------------------------------------|
| **code**     | code-reviewer          | Always (general code quality)       |
| **errors**   | silent-failure-hunter  | Always (error handling analysis)    |
| **comments** | comment-analyzer       | If comments/docs added or changed   |
| **types**    | type-design-analyzer   | If types added or modified          |
| **tests**    | pr-test-analyzer       | Always (checks if changes lack tests too) |
| **simplify** | code-simplifier        | After other reviews pass (polish)   |

When `all` is selected, determine applicable aspects based on the changed files:
- **Always run**: `code`, `errors`, `tests` (catches missing tests for new code too)
- **If comments or docs changed**: `comments`
- **If type definitions added/modified** (interfaces, type aliases, classes): `types`
- **`simplify` runs last** when selected — it applies changes, so it goes after
  all read-only reviews complete.

---

## Step 3 — Launch Review Agents

For each applicable report-only aspect, use the isolated native launcher below.
The `simplify` writer has its own Agent invocation after these reviews.

### Report-only agents (code, errors, comments, types, tests)

Inline the selected role body into the raw report prompt below and save that
same prompt to `.review-loop/tmp/{invocation_slot}-reviewer-prompt.txt`. Include
the absolute target repository path. Give each aspect/attempt a unique slot.
Keep cwd in the task workspace and resolve the launcher against the support
repository:

```sh
python3 <support-root>/scripts/run_claude_reviewer.py --session-id <invocation_slot> --parent-session-id <session_id> --model <resolved-model> --stage polish --role <agent-name> --timeout-seconds 570
```

Keep the existing judgment/cheap tier rules in the dispatch inventory. Its
`else omit` means no judgment-tier override: omit `--model` and use the Claude
CLI runtime default. Record the actual model only when the CLI reports it. Do
not silently select a different tier or launch a writable Agent.

Reviewers only read/search. The caller materializes the diff and runs any
required static analysis or tests, then supplies artifact paths, commands,
exit statuses, and relevant output. The runtime boundary takes precedence over
any role-body instruction to use Bash, install tools, or modify files. Preserve
the aspect's raw report format; do not wrap it in a planning-review schema.

```
Native reviewer prompt:
  prompt: |
    {contents of agents/<agent-name>.md body}

    Review the following code changes. Focus on your area of expertise.
    Report only; do not modify files.
    Use read/search tools only; inspect the caller-provided verification
    evidence and request missing checks instead of executing commands yourself.

    ## Target Repository
    {absolute task repository path}

    ## Changed Files
    {list of changed file paths}

    ## Diff
    {git diff output}

    ## Caller Verification Evidence
    {artifact paths, commands, exit statuses, relevant output, or no checks run}

    Provide your findings as a structured report with:
    - **Critical Issues** (must fix)
    - **Important Issues** (should fix)
    - **Suggestions** (nice to have)
    - **Positive Observations** (what's done well)

    Reference specific files and line numbers where possible.
```

Role mapping (inline each role body into the native launcher prompt):
- `code` → inline `agents/code-reviewer.md` body
- `errors` → inline `agents/silent-failure-hunter.md` body
- `comments` → inline `agents/comment-analyzer.md` body
- `types` → inline `agents/type-design-analyzer.md` body
- `tests` → inline `agents/pr-test-analyzer.md` body

Concrete dispatch inventory:
- `review_pr_code_reviewer_dispatch` -> `code-reviewer`; tier: `judgment`; `model: {judgment_model if set; else omit}`
- `review_pr_silent_failure_hunter_dispatch` -> `silent-failure-hunter`; tier: `judgment`; `model: {judgment_model if set; else omit}`
- `review_pr_comment_analyzer_dispatch` -> `comment-analyzer`; tier: `cheap`; `model: {cheap_model if set; else claude-opus-5-5}`
- `review_pr_type_design_analyzer_dispatch` -> `type-design-analyzer`; tier: `judgment`; `model: {judgment_model if set; else omit}`
- `review_pr_pr_test_analyzer_dispatch` -> `pr-test-analyzer`; tier: `cheap`; `model: {cheap_model if set; else claude-opus-5-5}`

**Completion and inspection evidence**: Accept a report only when launcher exit
is 0 and returned `status` is `ok`; read its returned `result_file` and validate
the aspect's report. Retain `invocation_id`, `tool_uses`, `stream_file`, `stderr_file`, and `usage_file` from the
native return metadata. Use the launcher’s `tool_uses` count; do not read the
raw stream into context. A missing or null count is unverified and fails closed.
If zero tool uses are confirmed, discard the report and retry once with a new
slot; if still zero, skip this aspect and report the failure. Never reuse a previous result as fresh.

### The `simplify` aspect

The `code-simplifier` agent modifies files to apply simplifications.

**CRITICAL — single spawning path**: Do NOT use `subagent_type: review-loop:code-simplifier`.
This writer uses `general-purpose`; report-only reviewers use the native
launcher above. Use `subagent_type: general-purpose` with the agent's full body inlined in the
prompt:

```
Agent tool parameters:
  subagent_type: general-purpose
  prompt: |
    {full body of agents/code-simplifier.md — everything below the frontmatter}

    ## Changed Files to Simplify
    {list of changed file paths}

    ## Diff
    {git diff output}

    Review the changed code and apply simplifications directly.
    After making changes, summarize what you simplified and why.
```

Concrete dispatch anchor: `review_pr_code_simplifier_dispatch`.
`code-simplifier` is a `cheap`-tier dispatch and resolves `model` as
`cheap_model` if set, else `claude-opus-5-5`.

**Important**: `simplify` always runs last (after all read-only reviews), because
it modifies files. **Skip `simplify` if any prior review returned CRITICAL
issues** — fix those first, then re-run with `simplify`.

### Sequential mode (default)

Run agents one at a time in this order:
1. `code` (general quality first — sets the baseline)
2. `errors` (error handling)
3. `comments` (if applicable)
4. `types` (if applicable)
5. `tests` (if applicable)
6. `simplify` (if selected — always last)

After each agent completes, display its findings to the user before proceeding
to the next.

### Parallel mode

Launch all report-only reviewers simultaneously through the same isolated
native launcher, using distinct prompt/artifact slots. Wait for all to complete, then:
1. Display all findings together
2. Run `simplify` last if selected (never in parallel — it modifies files)

---

## Step 4 — Aggregate Results

After all agents complete, compile a unified summary:

```markdown
# Code Review Summary

## Critical Issues ({count} found)
- [{agent-name}] Issue description [file:line]
- ...

## Important Issues ({count} found)
- [{agent-name}] Issue description [file:line]
- ...

## Suggestions ({count} found)
- [{agent-name}] Suggestion [file:line]
- ...

## Strengths
- What's done well (from agent observations)

## Recommended Actions
1. Fix critical issues first
2. Address important issues
3. Consider suggestions
4. Re-run `/review-loop:review-pr` after fixes to verify
```

If `simplify` was run, also note:
```
## Simplifications Applied
- {file}: {what was simplified}
```

---

## Usage Examples

**Default route (paired-session report mode; `entry` absent or `paired-session`, see Step 0):**
```
/review-loop:review-pr 123
# Reviews PR #123 of this repository in a temporary clone; writes review-report.md, posts nothing

/review-loop:review-pr https://github.com/owner/repo/pull/123 code tests
# Only the code and tests specialists (the reviewer, shadow, gate and security stage always run)
```

The examples below are the legacy route, removed in v2.13.0 (`--legacy` and `entry: legacy` are refused; deleted in v2.13.1).

**Full review (all applicable aspects, sequential):**
```
/review-loop:review-pr --legacy
```

**Specific aspects:**
```
/review-loop:review-pr --legacy tests errors
# Reviews only test coverage and error handling

/review-loop:review-pr --legacy comments
# Reviews only code comments

/review-loop:review-pr --legacy simplify
# Simplifies changed code
```

**Parallel review:**
```
/review-loop:review-pr --legacy all parallel
# Runs report-only aspects in parallel, then simplify last
```

**Combine:**
```
/review-loop:review-pr --legacy code errors parallel
# Reviews code quality and error handling in parallel
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

**code-simplifier** (has write access):
- Simplifies complex or verbose code
- Improves clarity and readability
- Applies project standards
- Preserves all existing functionality
