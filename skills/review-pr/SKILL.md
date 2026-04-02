---
name: review-pr
description: "Comprehensive code review using specialized agents. Each agent focuses on a different quality aspect (code, errors, comments, types, tests, simplify). Runs sequentially by default or in parallel on request."
argument-hint: "[aspects: code|errors|comments|types|tests|simplify|all] [parallel]"
---

# Comprehensive Code Review

Run a comprehensive code review using multiple specialized agents, each focusing
on a different aspect of code quality. Agents are invoked via the Agent tool and
run with read-only access to the project (except code-simplifier which needs
write access).

**Review Aspects (optional):** "$ARGUMENTS"

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

---

## Step 2 — Available Review Aspects

Each aspect maps to a specialized agent in the `agents/` directory:

| Aspect       | Agent                  | When applicable                     |
|--------------|------------------------|-------------------------------------|
| **code**     | code-reviewer          | Always (general code quality)       |
| **errors**   | silent-failure-hunter  | Always (error handling analysis)    |
| **comments** | comment-analyzer       | If comments/docs added or changed   |
| **types**    | type-design-analyzer   | If types added or modified          |
| **tests**    | pr-test-analyzer       | If test files changed               |
| **simplify** | code-simplifier        | After other reviews pass (polish)   |

When `all` is selected, determine applicable aspects based on the changed files:
- **Always run**: `code`, `errors`
- **If test files changed** (files matching `*test*`, `*spec*`, `__tests__/*`): `tests`
- **If comments or docs changed**: `comments`
- **If type definitions added/modified** (interfaces, type aliases, classes): `types`
- **`simplify` runs last** when selected — it applies changes, so it goes after
  all read-only reviews complete.

---

## Step 3 — Launch Review Agents

For each applicable aspect, invoke the corresponding agent via the **Agent tool**.

### Read-only agents (code, errors, comments, types, tests)

These agents have `tools: read-only` and cannot modify files.

```
Agent tool parameters:
  subagent_type: review-loop:<agent-name>
  prompt: |
    Review the following code changes. Focus on your area of expertise.

    ## Changed Files
    {list of changed file paths}

    ## Diff
    {git diff output}

    Provide your findings as a structured report with:
    - **Critical Issues** (must fix)
    - **Important Issues** (should fix)
    - **Suggestions** (nice to have)
    - **Positive Observations** (what's done well)

    Reference specific files and line numbers where possible.
```

Agent name mapping:
- `code` → `subagent_type: review-loop:code-reviewer`
- `errors` → `subagent_type: review-loop:silent-failure-hunter`
- `comments` → `subagent_type: review-loop:comment-analyzer`
- `types` → `subagent_type: review-loop:type-design-analyzer`
- `tests` → `subagent_type: review-loop:pr-test-analyzer`

### The `simplify` aspect (special handling)

The `code-simplifier` agent has `tools: all` because it modifies files to apply
simplifications. Due to a known plugin agent type sandbox bug, plugin-defined
agent types silently block Write/Edit tools. Therefore, invoke code-simplifier
via `subagent_type: general-purpose` with the agent's full body inlined in the
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

Launch all read-only agents simultaneously (multiple Agent tool calls in one
response). Wait for all to complete, then:
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

**Full review (all applicable aspects, sequential):**
```
/review-loop:review-pr
```

**Specific aspects:**
```
/review-loop:review-pr tests errors
# Reviews only test coverage and error handling

/review-loop:review-pr comments
# Reviews only code comments

/review-loop:review-pr simplify
# Simplifies changed code
```

**Parallel review:**
```
/review-loop:review-pr all parallel
# Launches all applicable agents in parallel
```

**Combine:**
```
/review-loop:review-pr code errors parallel
# Reviews code quality and error handling in parallel
```

---

## Agent Descriptions

**code-reviewer** (`review-loop:code-reviewer`):
- Checks CLAUDE.md / project guideline compliance
- Detects bugs, logic errors, and anti-patterns
- Reviews general code quality and style

**silent-failure-hunter** (`review-loop:silent-failure-hunter`):
- Finds silent failures and swallowed errors
- Reviews catch blocks and error propagation
- Checks error logging adequacy

**comment-analyzer** (`review-loop:comment-analyzer`):
- Verifies comment accuracy vs actual code
- Identifies comment rot and stale docs
- Checks documentation completeness

**type-design-analyzer** (`review-loop:type-design-analyzer`):
- Analyzes type encapsulation and invariants
- Reviews type design quality
- Rates invariant expression strength

**pr-test-analyzer** (`review-loop:pr-test-analyzer`):
- Reviews behavioral test coverage
- Identifies critical test gaps
- Evaluates test quality and assertions

**code-simplifier** (via `general-purpose` — has write access):
- Simplifies complex or verbose code
- Improves clarity and readability
- Applies project standards
- Preserves all existing functionality

---

## Tips

- **Run early**: before creating a PR, not after
- **Focus on changes**: agents analyze `git diff` by default
- **Address critical first**: fix high-priority issues before lower priority
- **Re-run after fixes**: verify issues are resolved
- **Use specific aspects**: target what you care about to save time
- **Parallel for speed**: use `parallel` when you want all results at once
