---
name: code-quality-loop
description: "Iterative review-fix loop: reviews code, auto-fixes issues, repeats until clean or stuck. Finishes with simplify + test consolidation."
argument-hint: "[max-rounds]"
---

# Review Loop

Automated code review cycle: review → fix → re-review until clean. Focuses on code-level quality (correctness, style, error handling, tests).

## Overview

```
Pre-loop:  go-reviewer agent → fix (Go only, once)
Loop R1:   /review-loop:review-pr code errors comments types → triage → fix
Loop R2+:  /review-loop:review-pr code errors → triage → fix → repeat
Finalize:  code-simplifier agent → build → test consolidation → /review-loop:review-pr tests
```

## Arguments

`$ARGUMENTS`: Optional max round count. Example: `/code-quality-loop 3`. Default: 5.

## Initialization

1. **Scope**: Always reviews `git diff` (unstaged changes), consistent with all internal review tools. Run `git diff --name-only --diff-filter=d` to identify changed files. If no changes found, abort with message "No changes to review."

2. **Language detection**: Check for `.go` files in scope. Set `IS_GO` flag.

3. **Load design context (optional)**: Look for a design document in the project (e.g., `openspec/changes/*/design.md`, `docs/design.md`, or similar). If multiple exist, use the most recently modified one. If found, read it. This is the **fix boundary constraint** — applies to all fix actions throughout this command (loop fixes, simplify, etc.). Do not deviate from design intent unless a design-level defect is found (security vulnerability, race condition, obvious logic error). If no design document is found, skip this constraint — all fixes are allowed.

4. **State init**: `round = 0`, `issue_ledger = []`, `skipped_issues = []`, `consecutive_stuck = 0`, `max_rounds = $ARGUMENTS or 5`.

**Output after init:**

```
REVIEW LOOP
  Scope:       {file count} files ({list or summary})
  Language:    {Go / other}
  Design:      {loaded from path/to/design.md / none}
  Max rounds:  {max_rounds}
```

## Pre-loop: Go Static Analysis (Go projects only)

Launch the `go-reviewer` agent once before entering the loop. Static analysis tools (vet/lint/race/vuln) are deterministic — fix all issues now so the loop starts with clean, compilable code.

```
GO STATIC ANALYSIS
  go-review:     {PASS / X issues (C critical, H high, M medium)}
  Auto-fixed:    {X issues}
```

Fix all issues (CRITICAL, HIGH, and MEDIUM) before proceeding. `go build` in Finalize catches any remaining compilation issues.

## Loop Execution

Increment `round` each iteration.

### Phase 1: REVIEW

- **Round 1**: Run `/review-loop:review-pr code errors comments types` — full scan across all dimensions.
- **Round 2+**: Run `/review-loop:review-pr code errors` — fixes are code-only changes, no need to re-check comments/types.

`tests` and `simplify` agents are always skipped — both have dedicated steps in Finalize.

**Output:**

```
── ROUND {round} ─────────────────────────────
[REVIEW]
  review-pr:     {X issues (C critical, H high, M medium)}
```

### Phase 2: TRIAGE

1. Extract issues from review output. Record each: `{source, severity, file, line, description}`
2. Filter out issues already in `skipped_issues` (previously skipped due to design constraint) — do not re-process them.
3. Merge all issues (CRITICAL/HIGH/MEDIUM) with previous round's ledger:
   - New issue (not in previous round) → status OPEN
   - Previous round present, this round absent → status FIXED
   - Previous round present, this round still present → status OPEN (fix failed or not attempted)
4. Count: `new_count`, `fixed_count`, `open_count`

**Stuck detection**: Compare this round's OPEN issue set with previous round. If identical → `consecutive_stuck++`. If any change → reset to 0.

**Output (continues the round block):**

```
[TRIAGE]
  New:           {new_count}
  Fixed:         {fixed_count} (from last round)
  Open:          {open_count} ({critical_count} critical, {high_count} high, {medium_count} medium)
  Skipped:       {skipped_count} (design constraint)
```

### Phase 3: DECIDE

**IMPORTANT**: DECIDE evaluates the TRIAGE results — issues found by the **reviewer this round**. "No OPEN issues" means the reviewer's fresh review found no problems at all (no new issues AND no previously found issues still present). Fixing issues does not count as verification; only a clean review round does.

- **No OPEN issues from this round's REVIEW** → proceed to Finalize with result CLEAN
- **`consecutive_stuck >= 3`** (same issues unresolved for 3 rounds) → proceed to Finalize with result STUCK
- **`round >= max_rounds`** → proceed to Finalize with result MAX_ROUNDS
- **Otherwise** → proceed to Phase 4

Always proceed to Finalize regardless of result — code has been partially modified and must at least be buildable and testable.

**Output (continues the round block):**

```
[DECIDE]       {CLEAN → finalizing / FIXING {N} issues / STUCK → finalizing / MAX_ROUNDS → finalizing}
```

### Phase 4: FIX

For each OPEN issue sorted by severity (CRITICAL → HIGH → MEDIUM):

1. Read the relevant code at the reported file and line
2. If design.md is loaded, check whether the fix deviates from design:
   - **No deviation** → apply fix directly
   - **Deviation, but reviewer found a design defect** (security, race condition, etc.) → apply fix, mark `design_override = true`
   - **Deviation, not a design defect** → skip this issue, add to `skipped_issues` list so it is ignored in future rounds
3. Apply fix using Edit tool

**Output per fix:**

```
[FIX {n}/{total}] {file}:{line}
  Issue:         {short description}
  Action:        {fixed / skipped (design constraint) / design override applied}
```

Do NOT run tests after each fix. Testing happens once in the Finalize phase.

**Round summary output (after all fixes applied):**

```
[SUMMARY]
  Applied:       {applied_count}/{total} fixes
  Skipped:       {skipped_count} (design constraint)
  Next:          → re-reviewing in round {round + 1}
──────────────────────────────────────────────────
```

**After FIX, ALWAYS return to Phase 1** for re-review. Fixes must be verified by a fresh review round — fixing all issues does not mean the code is clean. The fixes themselves may introduce new problems. Do NOT re-evaluate DECIDE after FIX — go straight to Phase 1.

**Early exit (only exception)**: If zero fixes were actually applied this round (all issues skipped by design constraint), the code is unchanged — running another review would produce identical results. Proceed directly to Finalize instead of returning to Phase 1.

## Finalize

Execute the following steps regardless of how the loop ended (CLEAN, STUCK, or MAX_ROUNDS). Code has been partially modified and must be left in a buildable, testable state.

### Step 1: Simplify + Build

Launch the `code-simplifier` agent for final code polish (auto-fix). Then verify compilation:
- Go: `go build ./...`
- Other: language-appropriate build/compile check (e.g., `tsc --noEmit`)

If build fails → fix the compilation error and rebuild. Max 3 attempts. If still failing after 3 attempts, report to user and continue to Step 2.

### Step 2: Test consolidation

Consolidate tests for the changed code:
- Keep only tests for critical logic paths
- Add missing tests for key logic that lacks coverage
- Remove redundant or trivial tests
- Ensure test comments clearly describe what each test verifies

Then run the full test suite and fix any failures. Only fix tests related to the changed code — pre-existing failures in unrelated tests are not in scope.
- Go: `go test ./...`
- Other: project test command

Max 3 fix cycles. If still failing after 3 attempts, report remaining failures to user and continue.

### Step 3: Test quality gate

Run `/review-loop:review-pr tests` to verify test quality (coverage gaps, missing edge cases). If issues found → fix and re-run tests.

Max 2 fix cycles. If still failing, report to user and continue.

**Finalize output (all 3 steps):**

```
── FINALIZE ──────────────────────────────────
[SIMPLIFY]     {X improvements applied / 0 (already clean)}
[BUILD]        {PASS / FAIL → fixed (attempt {n}/3) / FAIL (unresolved)}
[TESTS]
  Consolidated:  added {X}, removed {X}, updated {X}
  Test run:      {PASS ({N} tests) / FAIL → fixed (attempt {n}/3)}
[TEST REVIEW]
  Quality:       {PASS / {X} issues → fixed (attempt {n}/2)}
  Final run:     {PASS ({N} tests) / FAIL (unresolved)}
──────────────────────────────────────────────
```

## Final Report

```
══ REVIEW LOOP COMPLETE ══════════════════════

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
  [R{n}] {file}:{line} — {what changed and why}

TEST CONSOLIDATION
  Added: {X}  Removed: {X}  Updated: {X}

══════════════════════════════════════════════
```
