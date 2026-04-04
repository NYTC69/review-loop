---
name: code-quality-loop
description: "Iterative review-fix loop: reviews code, auto-fixes issues, repeats until clean or stuck. Finishes with reorganize (for large changes), simplify + test consolidation."
argument-hint: "[max-rounds]"
---

# Code Quality Loop

Automated code review cycle: review -> fix -> re-review until clean. Focuses on code-level quality (correctness, style, error handling, tests).

## Overview

```
Pre-loop:  language-specific agents -> fix (once per detected language)
Loop R1:   code-reviewer + silent-failure-hunter + comment-analyzer + type-design-analyzer -> triage -> fix
Loop R2+:  code-reviewer + silent-failure-hunter -> triage -> fix -> repeat
Finalize:  reorganize (if applicable) -> code-simplifier agent -> build -> test consolidation -> pr-test-analyzer
```

## Arguments

`$ARGUMENTS`: Optional flags and max round count.

- **Max rounds**: `/code-quality-loop 3` — limit to 3 rounds (default: 5)
- **Skip reorganize**: `/code-quality-loop --skip-reorganize` — skip the Reorganize step in Finalize
- **Force reorganize**: `/code-quality-loop --reorganize` — always run Reorganize regardless of change size
- **Combined**: `/code-quality-loop 3 --skip-reorganize`

If neither `--skip-reorganize` nor `--reorganize` is specified, the Orchestrator decides automatically based on change scope (see Finalize Step 1).

## Initialization

1. **Scope**: Always reviews `git diff` (unstaged changes), consistent with all internal review tools. Run `git diff --name-only --diff-filter=d` to identify changed files. If no changes found, abort with message "No changes to review."

2. **Language detection**: Detect languages from changed files:
   ```bash
   { git diff --name-only --diff-filter=d HEAD; git ls-files --others --exclude-standard; } | grep '\.' | sed 's/.*\.\([^.]*\)$/\1/' | sort -u
   ```
   Map file extensions to language-specific agents:
   - `.go` -> `go-reviewer`
   - `.rs` -> `rust-reviewer`
   - `.py` -> `python-reviewer`
   - `.ts`, `.tsx`, `.js`, `.jsx`, `.html`, `.vue`, `.svelte` -> `frontend-security-reviewer`

   Record the set of applicable language agents as `language_agents`.

3. **Load design context (optional)**: Look for a design document in the project (e.g., `openspec/changes/*/design.md`, `docs/design.md`, or similar). If multiple exist, use the most recently modified one. If found, read it. This is the **fix boundary constraint** -- applies to all fix actions throughout this command (loop fixes, simplify, etc.). Do not deviate from design intent unless a design-level defect is found (security vulnerability, race condition, obvious logic error). If no design document is found, skip this constraint -- all fixes are allowed.

4. **Load config (optional)**: Read `.review-loop/config.md` if it exists. Extract:
   - `quality_focus`: free-text field injected into all agent prompts during this loop. Tells agents what to prioritize.
   - `review_style`: free-text field injected into ALL agent prompts. Sets tone and cross-cutting rules.
   If the config file does not exist or the fields are empty/absent, skip injection.

5. **State init**: `round = 0`, `issue_ledger = []`, `skipped_issues = []`, `consecutive_stuck = 0`, `max_rounds = $ARGUMENTS or 5`.

**Output after init:**

```
REVIEW LOOP
  Scope:       {file count} files ({list or summary})
  Languages:   {detected languages, e.g. "Go, Python" or "none (general review only)"}
  Design:      {loaded from path/to/design.md / none}
  Config:      {loaded / none}
  Max rounds:  {max_rounds}
```

## Pre-loop: Language-Specific Static Analysis

For each agent in `language_agents`, launch a sub-agent via the Agent tool. These are deterministic static analysis tools -- fix all issues now so the loop starts with clean, compilable code.

Run each applicable language agent:

```
Agent tool parameters:
  subagent_type: general-purpose
  prompt: |
    {contents of agents/<agent-name>.md body}

    IMPORTANT: Use Claude Code's native Bash tool to run shell commands.
    Do NOT use MCP server tools (e.g. run_bash_command).

    ## Changed Files
    {list of changed files for this language, from git diff --name-only --diff-filter=d HEAD}

    Run static analysis on the changed files listed above.

    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}

    {if review_style is set:}
    ## Review Style
    {review_style}
```

Agent name mapping (all use `subagent_type: general-purpose` with agent body inlined in prompt):
- `go-reviewer` -> inline `agents/go-reviewer.md` body
- `rust-reviewer` -> inline `agents/rust-reviewer.md` body
- `python-reviewer` -> inline `agents/python-reviewer.md` body
- `frontend-security-reviewer` -> inline `agents/frontend-security-reviewer.md` body

If multiple language agents apply, launch them sequentially (each may find issues that require fixes before the next can run cleanly).

**Hallucination guard**: After each agent returns, check the Agent tool metadata. If `tool_uses: 0`, the agent did not actually read files or run commands — its output is fabricated. Discard the result and retry once. If the retry also has `tool_uses: 0`, skip this agent and report: `STATIC ANALYSIS: {AGENT-NAME} — SKIPPED (agent failed to use tools)`.

**Output per language agent:**

```
STATIC ANALYSIS: {AGENT-NAME}
  Result:        {PASS / X issues (C critical, H high, M medium)}
  Auto-fixed:    {X issues}
```

Fix all issues (CRITICAL, HIGH, and MEDIUM) reported by each language agent before proceeding to the next. Use the Edit tool to apply fixes directly.

If no language agents are applicable, skip this phase entirely.

## Loop Execution

Increment `round` each iteration.

### Phase 1: REVIEW

Launch review agents via the Agent tool. Each agent runs with read-only access.

- **Round 1**: Run all four agents -- `code-reviewer`, `silent-failure-hunter`, `comment-analyzer`, `type-design-analyzer` -- full scan across all dimensions.
- **Round 2+**: Run only `code-reviewer` and `silent-failure-hunter` -- fixes are code-only changes, no need to re-check comments/types.

`pr-test-analyzer` and `code-simplifier` are always skipped in the loop -- both have dedicated steps in Finalize.

**Hallucination guard**: After each agent returns, check the Agent tool metadata. If `tool_uses: 0`, discard the result and retry once. If the retry also has `tool_uses: 0`, skip that agent for this round and note it in the output.

Agent invocations:

```
Agent tool parameters (code-reviewer):
  subagent_type: general-purpose
  prompt: |
    {contents of agents/code-reviewer.md body}

    Review the following code changes. Focus on code quality, bugs, logic errors, and anti-patterns.

    ## Changed Files
    {list of changed file paths from git diff --name-only --diff-filter=d}

    ## Diff
    {git diff output}

    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}

    {if review_style is set:}
    ## Review Style
    {review_style}

    Provide your findings as a structured report with severity levels:
    - **CRITICAL** (must fix -- security, correctness, data loss)
    - **HIGH** (should fix -- error handling, edge cases)
    - **MEDIUM** (improve -- style, clarity, efficiency)
    Reference specific files and line numbers.
```

```
Agent tool parameters (silent-failure-hunter):
  subagent_type: general-purpose
  prompt: |
    {contents of agents/silent-failure-hunter.md body}

    Review the following code changes. Focus on error handling analysis.

    ## Changed Files
    {list of changed file paths from git diff --name-only --diff-filter=d}

    ## Diff
    {git diff output}

    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}

    {if review_style is set:}
    ## Review Style
    {review_style}

    Provide your findings as a structured report with severity levels:
    - **CRITICAL** (must fix)
    - **HIGH** (should fix)
    - **MEDIUM** (improve)
    Reference specific files and line numbers.
```

```
Agent tool parameters (comment-analyzer -- Round 1 only):
  subagent_type: general-purpose
  prompt: |
    {contents of agents/comment-analyzer.md body}

    Review the following code changes. Focus on comment accuracy and documentation.

    ## Changed Files
    {list of changed file paths from git diff --name-only --diff-filter=d}

    ## Diff
    {git diff output}

    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}

    {if review_style is set:}
    ## Review Style
    {review_style}

    Provide your findings as a structured report with severity levels:
    - **CRITICAL** (must fix)
    - **HIGH** (should fix)
    - **MEDIUM** (improve)
    Reference specific files and line numbers.
```

```
Agent tool parameters (type-design-analyzer -- Round 1 only):
  subagent_type: general-purpose
  prompt: |
    {contents of agents/type-design-analyzer.md body}

    Review the following code changes. Focus on type design quality.

    ## Changed Files
    {list of changed file paths from git diff --name-only --diff-filter=d}

    ## Diff
    {git diff output}

    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}

    {if review_style is set:}
    ## Review Style
    {review_style}

    Provide your findings as a structured report with severity levels:
    - **CRITICAL** (must fix)
    - **HIGH** (should fix)
    - **MEDIUM** (improve)
    Reference specific files and line numbers.
```

**Output:**

```
-- ROUND {round} ----------------------------------------
[REVIEW]
  code-reviewer:          {X issues (C critical, H high, M medium)}
  silent-failure-hunter:  {X issues (C critical, H high, M medium)}
  comment-analyzer:       {X issues (Round 1 only)}
  type-design-analyzer:   {X issues (Round 1 only)}
  Total:                  {X issues (C critical, H high, M medium)}
```

### Phase 2: TRIAGE

1. Extract issues from all agent outputs. Record each: `{source, severity, file, line, description}`
2. Filter out issues already in `skipped_issues` (previously skipped due to design constraint) -- do not re-process them.
3. Merge all issues (CRITICAL/HIGH/MEDIUM) with previous round's ledger:
   - New issue (not in previous round) -> status OPEN
   - Previous round present, this round absent -> status FIXED
   - Previous round present, this round still present -> status OPEN (fix failed or not attempted)
4. Count: `new_count`, `fixed_count`, `open_count`

**Stuck detection**: Compare this round's OPEN issue set with previous round. If identical -> `consecutive_stuck++`. If any change -> reset to 0.

**Output (continues the round block):**

```
[TRIAGE]
  New:           {new_count}
  Fixed:         {fixed_count} (from last round)
  Open:          {open_count} ({critical_count} critical, {high_count} high, {medium_count} medium)
  Skipped:       {skipped_count} (design constraint)
```

### Phase 3: DECIDE

**IMPORTANT**: DECIDE evaluates the TRIAGE results -- issues found by the **reviewers this round**. "No OPEN issues" means the reviewers' fresh review found no problems at all (no new issues AND no previously found issues still present). Fixing issues does not count as verification; only a clean review round does.

- **No OPEN issues from this round's REVIEW** -> proceed to Finalize with result CLEAN
- **`consecutive_stuck >= 3`** (same issues unresolved for 3 rounds) -> proceed to Finalize with result STUCK
- **`round >= max_rounds`** -> proceed to Finalize with result MAX_ROUNDS
- **Otherwise** -> proceed to Phase 4

Always proceed to Finalize regardless of result -- code has been partially modified and must at least be buildable and testable.

**Output (continues the round block):**

```
[DECIDE]       {CLEAN -> finalizing / FIXING {N} issues / STUCK -> finalizing / MAX_ROUNDS -> finalizing}
```

### Phase 4: FIX

For each OPEN issue sorted by severity (CRITICAL -> HIGH -> MEDIUM):

1. Read the relevant code at the reported file and line
2. If design.md is loaded, check whether the fix deviates from design:
   - **No deviation** -> apply fix directly
   - **Deviation, but reviewer found a design defect** (security, race condition, etc.) -> apply fix, mark `design_override = true`
   - **Deviation, not a design defect** -> skip this issue, add to `skipped_issues` list so it is ignored in future rounds
3. Apply fix using Edit tool

**Output per fix:**

```
[FIX {n}/{total}] {file}:{line}
  Issue:         {short description}
  Source:        {agent name that reported it}
  Action:        {fixed / skipped (design constraint) / design override applied}
```

Do NOT run tests after each fix. Testing happens once in the Finalize phase.

**Round summary output (after all fixes applied):**

```
[SUMMARY]
  Applied:       {applied_count}/{total} fixes
  Skipped:       {skipped_count} (design constraint)
  Next:          -> re-reviewing in round {round + 1}
-------------------------------------------------
```

**After FIX, ALWAYS return to Phase 1** for re-review. Fixes must be verified by a fresh review round -- fixing all issues does not mean the code is clean. The fixes themselves may introduce new problems. Do NOT re-evaluate DECIDE after FIX -- go straight to Phase 1.

**Early exit (only exception)**: If zero fixes were actually applied this round (all issues skipped by design constraint), the code is unchanged -- running another review would produce identical results. Proceed directly to Finalize instead of returning to Phase 1.

## Finalize

Execute the following steps regardless of how the loop ended (CLEAN, STUCK, or MAX_ROUNDS). Code has been partially modified and must be left in a buildable, testable state.

### Step 1: Reorganize (conditional)

**Skip if** `--skip-reorganize` was passed. **Always run if** `--reorganize` was passed.

**If neither flag was passed**, decide automatically: examine the scope of changes from Initialization (file count, total lines changed via `git diff --stat`). Apply this heuristic:
- **Skip**: <= 3 files changed AND <= 100 lines changed — likely a bug fix or small patch
- **Run**: > 3 files changed OR > 100 lines changed — likely a feature or significant refactor

Run `/review-loop:reorganize diff` to restructure changed files. The reorganize skill includes its own build verification.

**Output:**

```
[REORGANIZE]   {skipped (small change) / skipped (--skip-reorganize) / ran (see reorganize output)}
```

### Step 2: Simplify + Build

Launch the `code-simplifier` agent for final code polish (auto-fix). Due to the known plugin agent type sandbox bug, invoke code-simplifier via `subagent_type: general-purpose` with the agent's full body inlined in the prompt:

```
Agent tool parameters:
  subagent_type: general-purpose
  prompt: |
    {full body of agents/code-simplifier.md -- everything below the frontmatter}

    ## Changed Files to Simplify
    {list of changed file paths from git diff --name-only --diff-filter=d}

    ## Diff
    {git diff output}

    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}

    {if review_style is set:}
    ## Review Style
    {review_style}

    Review the changed code and apply simplifications directly.
    After making changes, summarize what you simplified and why.
```

Then verify compilation. Detect build command from the languages found during initialization:
- Go files present: `go build ./...`
- Rust files present: `cargo check`
- TypeScript files present: `tsc --noEmit` (if tsconfig.json exists)
- Python files present: `python -m py_compile` on changed `.py` files
- Multiple languages: run all applicable build checks
- No specific build tool: skip build check

If build fails -> fix the compilation error and rebuild. Max 3 attempts. If still failing after 3 attempts, report to user and continue to Step 3.

### Step 3: Test consolidation

Consolidate tests for the changed code:
- Keep only tests for critical logic paths
- Add missing tests for key logic that lacks coverage
- Remove redundant or trivial tests
- Ensure test comments clearly describe what each test verifies

Then run the full test suite and fix any failures. Only fix tests related to the changed code -- pre-existing failures in unrelated tests are not in scope.

Detect test command from project languages:
- Go: `go test ./...`
- Rust: `cargo test`
- Python: `pytest` (or `python -m pytest`)
- TypeScript/JavaScript: `npm test` (or project-specific test command from package.json)
- Multiple languages: run all applicable test commands
- No test runner found: skip test execution, warn user

Max 3 fix cycles. If still failing after 3 attempts, report remaining failures to user and continue.

### Step 4: Test quality gate

Launch the `pr-test-analyzer` agent to verify test quality (coverage gaps, missing edge cases):

```
Agent tool parameters:
  subagent_type: general-purpose
  prompt: |
    {contents of agents/pr-test-analyzer.md body}

    Analyze test coverage for the changed files.

    ## Changed Files
    {list of changed file paths from git diff --name-only --diff-filter=d}

    ## Diff
    {git diff output}

    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}

    {if review_style is set:}
    ## Review Style
    {review_style}

    Provide your findings as a structured report with severity levels:
    - **CRITICAL** (must fix -- missing tests for critical paths)
    - **HIGH** (should fix -- coverage gaps)
    - **MEDIUM** (improve -- edge cases)
    Reference specific files and line numbers.
```

If issues found -> fix and re-run tests.

Max 2 fix cycles. If still failing, report to user and continue.

**Finalize output (all 4 steps):**

```
-- FINALIZE ------------------------------------------
[REORGANIZE]   {skipped (small change) / skipped (--skip-reorganize) / ran (see output above)}
[SIMPLIFY]     {X improvements applied / 0 (already clean)}
[BUILD]        {PASS / FAIL -> fixed (attempt {n}/3) / FAIL (unresolved)}
[TESTS]
  Consolidated:  added {X}, removed {X}, updated {X}
  Test run:      {PASS ({N} tests) / FAIL -> fixed (attempt {n}/3)}
[TEST REVIEW]
  Quality:       {PASS / {X} issues -> fixed (attempt {n}/2)}
  Final run:     {PASS ({N} tests) / FAIL (unresolved)}
-------------------------------------------------
```

### Step 5: Documentation Consistency Check

#### 5.1 — Update project documentation (if any exists)

Search the project for documentation files:
- Design docs, architecture docs, ADRs (e.g. `docs/`, `design/`, `*.md` outside source dirs)
- Runbooks, operational guides
- Memory files (`.claude/memory/`, `tasks/lessons.md`)
- Learning notes, changelogs, wikis

For each doc found: compare against the code changes (`git diff HEAD`). If the doc describes behavior, APIs, or logic that has changed, update it to reflect the new implementation.

If no project documentation is found: note "no project docs found" and proceed to 5.2.

#### 5.2 — Code comment consistency (always run)

For each changed file, read the current code and verify:
- Function/method comments match the actual implementation
- Type/struct comments match actual fields and behavior
- Inline comments explain current logic (not stale)

Fix any stale or incorrect comments directly.

**Output:**
```
[DOC CONSISTENCY]
  Project docs:   {updated: X files / none found}
  Comments fixed: {N} stale comments in {files / "none"}
```

## Final Report

```
== REVIEW LOOP COMPLETE ============================

Rounds:  {N}
Result:  {CLEAN / STUCK / MAX_ROUNDS}
Fixed:   {X}  Remaining: {X}  Overrides: {X}  Skipped: {X}

FIXED ISSUES
  [R{n}] {source}: {description} @ {file}:{line}
  [R{n}] {source}: {description} @ {file}:{line}

REMAINING ISSUES (if any)
  [{severity}] {source}: {description} @ {file}:{line}
    Reason: {RECURRING / SKIPPED_BY_DESIGN}

DESIGN OVERRIDES (if any)
  [R{n}] {file}:{line} -- {what changed and why}

TEST CONSOLIDATION
  Added: {X}  Removed: {X}  Updated: {X}

====================================================
```
