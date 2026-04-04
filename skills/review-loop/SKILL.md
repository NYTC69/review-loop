---
name: review-loop
argument-hint: "<work item description> [--handsfree]"
description: >
  Automates a Plan-Execute-Review workflow with built-in iteration loops.
  An Orchestrator coordinates an Executor sub-agent and a configurable Reviewer
  (external AI CLI like Codex, or Claude sub-agent) to drive work items from
  description to delivery — with every review finding visible to the user.
  Trigger: "run review-loop on", "start review loop", "let the agents handle",
  or any task the user wants driven by a plan→review→implement→CR cycle.
---

# Review Loop — Dual-Agent Orchestration Skill

Drive a work item from description to delivery using an Executor and an independent
Reviewer in an iterative loop. The Reviewer catches bugs, design issues, and gaps
that a single agent would miss — and you see every finding in real time.

## Architecture

```
You (Orchestrator — this session)
├── Executor sub-agent   — plans, refines, implements (runs in Agent thread)
└── Reviewer             — reviews plans and code, returns structured verdict
    ├── mode: codex      — external CLI: codex exec -s read-only
    └── mode: subagent   — Claude Code sub-agent (read-only tools)
```

Two phases, each with their own iteration loop:

| Phase       | What happens                                                    |
|-------------|-----------------------------------------------------------------|
| **Plan**    | Executor drafts a solution plan → Reviewer critiques → iterate  |
| **Execute** | Executor implements approved plan → Reviewer does CR → iterate  |

---

## Invocation

```
run review-loop on: <work item description> [--handsfree]
```

**`--handsfree`** (optional): fully autonomous mode. Decision-type questions
(architecture choices, approach trade-offs) go to the Reviewer instead of
pausing for the user. All Reviewer-made decisions are logged in the delivery
summary for post-hoc review.

Without `--handsfree` (default): decision-type questions surface to the user.

> **Note**: regardless of mode, questions requiring external information the agents
> cannot know (credentials, file paths outside the repo, business rules not in
> context) **always** pause and ask the user.

---

## Configuration

Read from `.claude/review-loop-config.md` if present, else use defaults:

```yaml
# .claude/review-loop-config.md  ← create this in your project to override defaults
reviewer: codex                 # "codex" | "subagent"
reviewer_model: ""              # codex: -m flag (empty = codex default); subagent: Agent model (empty = inherit Orchestrator)
executor_model: inherit         # Executor sub-agent model ("inherit" | "sonnet" | "opus")
soft_limit_plan: 3              # after N rounds, ask user whether to continue (if CRITICALs remain)
soft_limit_exec: 3              # same for execution phase
auto_commit: false              # commit after execution phase completes
commit_message_prefix: "feat"   # conventional commit prefix
docs_file: CHANGELOG.md         # file to append delivery summary to; "" to skip
handsfree: false                # set true to make --handsfree the default
review_focus: ""                # project-specific review priorities (see below)
quality_focus: ""               # what to prioritize in quality polish (Step 3.5)
review_style: ""                # tone and rules for ALL reviews (adversarial CR + quality agents)
skip_quality_polish: false      # skip Step 3.5 entirely
```

The `--handsfree` flag at invocation always overrides the config value.

**`review_focus`**: free-text field injected into the Reviewer's code review
prompt as additional focus areas. Only applies to code review, not plan review.
Leave empty to use the default review checklist in `reviewer.md`. Example:

```yaml
review_focus: |
  - Security: XSS, CSRF, input sanitization, auth state handling
  - Accessibility: WCAG compliance, keyboard navigation, screen reader
  - UX edge cases: loading states, empty states, error states
```

**`quality_focus`**: free-text field injected into quality agent prompts in
Step 3.5. Tells the quality agents what to prioritize (e.g., "strict clippy
lints, skip comment analysis"). Leave empty for default behavior.

**`review_style`**: free-text field injected into ALL reviewer prompts —
both the adversarial Reviewer (Steps 2-3) and the quality agents (Step 3.5).
Use this to set tone and cross-cutting rules (e.g., "be terse, flag 80-char
violations as CRITICAL"). Leave empty for default behavior.

**`skip_quality_polish`**: set to `true` to skip Step 3.5 entirely. Useful
when you only want the adversarial CR loop without the supplementary polish.

---

## Orchestrator Instructions

When this skill is triggered, you are the **Orchestrator**. Your job is to
coordinate the Executor and Reviewer, enforce the iteration loop, and keep
the user informed of what the review process is finding. Do **not** do the
planning or coding yourself.

### Step 0 — Load config and parse flags

1. Check if `.claude/review-loop-config.md` exists. If so, read and parse it.
   Otherwise use the defaults above.
2. Detect `--handsfree` in the user's invocation message. If present, or if
   `handsfree: true` in config, enable handsfree mode for this session.
3. Confirm the Reviewer backend is available:
   - If `reviewer: codex`: verify `codex` CLI is in PATH by running `which codex`.
     If not found, tell the user and suggest `reviewer: subagent` as fallback.
   - If `reviewer: subagent`: no check needed (uses Agent tool).

### Step 0.5 — Initialize context file

Generate a UUID for this session and create the context file:

```bash
uuid=$(uuidgen | tr '[:upper:]' '[:lower:]')
context_dir=".claude/review-loop-sessions"
mkdir -p "${context_dir}"
context_file="${context_dir}/${uuid}.md"
```

This file is the **single source of truth** for the loop. Both the Executor
and Reviewer read it at the start of each round. The Orchestrator updates
it after every round. This eliminates redundant context passing in prompts
and gives agents instant project understanding without cold-start exploration.

Session files are preserved in the project for traceability — useful for
post-hoc review of which round introduced an issue and what the Reviewer
caught or missed. Add `.claude/review-loop-sessions/` to `.gitignore` if
you don't want them in version control.

The context file structure:

```markdown
# Review Loop Context — {title}

## Problem Description
{problem_description}

## Context
{context}

## Acceptance Criteria
{acceptance_criteria}

## Current Phase
{planning | execution}

## Approved Plan
{empty during planning, populated after plan approval}

## Review History
{accumulated findings with severity and resolution status}

## Files Changed
{updated after each Executor round — files modified/created and why}

## Key Related Files
{files relevant to the task that were NOT changed but important for review context}

## Timing Log
{updated after each step — for post-hoc analysis}
| Phase | Round | Role | Duration |
|-------|-------|------|----------|
| planning | 1 | executor | 45s |
| planning | 1 | reviewer | 82s |
| ... | | | |
```

Tell the user the context file path so they can inspect it if needed:
```
Context file: {context_file}
```

### Step 1 — Parse the work item

Extract from the user's message:
- **Title**: one-line summary
- **Problem Description**: a clear, self-contained description of the problem
  to solve. Write this as if briefing a new engineer who has no prior context.
  Include: what is broken or missing, why it matters, and what the desired
  outcome looks like. This description is passed to every Executor and
  Reviewer call throughout the entire loop — it is the shared understanding
  of "what we are doing and why."
- **Context**: any additional background info, file paths, constraints
- **Acceptance criteria**: what "done" looks like (infer if not stated)

If critical information is missing, ask ONE clarifying question before proceeding.

### Step 1.5 — Detect current work state

Before starting from Step 2 (Planning), assess the conversation context and
project state to determine if this task is already in progress:

- **Plan already exists** (user says "review this", or there's an approved
  plan in context, or code changes are already made): skip the Planning
  phase entirely — jump directly to Step 3 (Execution/CR).
- **Code already implemented**: the user explicitly asks for CR only, OR
  git diff shows substantial changes that are clearly related to this task
  (not just a few trivial or unrelated edits). Assess relevance by checking
  whether the changed files and logic align with the task's problem
  description. If the changes look unrelated or too minor to constitute
  an implementation, fall back to Planning.
- **Existing session context file** found in `.claude/review-loop-sessions/`
  that matches this task: read it and resume from where it left off.
- **No prior state**: start from Step 2 (Planning) as normal.

Display the detected state to the user for confirmation:
```
Detected: {plan exists / code already implemented / fresh start}
→ Starting from: {Planning / Execution / Code Review only}
```

If the user disagrees, they can override. The goal is to pick up where
the work currently is, not force a rigid start-from-scratch sequence.

Display to the user:
```
── review-loop: Starting ──────────────────────────
Work item: {title}
Problem: {problem_description}
Reviewer: {codex | subagent} ({reviewer_model})
Mode: {interactive | handsfree}
Soft limit: {soft_limit_plan} (plan) / {soft_limit_exec} (exec)
────────────────────────────────────────────────────
```

### Step 2 — Planning phase

Initialize loop state:
```
loop_state = {
  phase: "planning",
  round: 0,
  plan_version: null,
  findings: [],        # accumulated across rounds
  pending_issues: [],
  resolved_issues: [],
  timing: {            # wall-clock time tracking per step
    loop_start: null,  # timestamp when loop begins
    steps: []          # [{phase, round, role, start, end, duration_s}]
  },
  token_usage: {       # best-effort tracking
    executor: 0,       # sum from Agent tool metadata
    reviewer: 0        # sum from codex/agent metadata (N/A if unavailable)
  }
}
```

**Timing**: record wall-clock time before and after each Executor and
Reviewer call. Store as:
```
{phase: "planning", round: 1, role: "executor", duration_s: 45}
{phase: "planning", round: 1, role: "reviewer", duration_s: 82}
{phase: "execution", round: 1, role: "executor", duration_s: 120}
...
```
```

**Round loop:**

1. **Update context file** before calling agents:
   - Write/update all sections in `{context_file}`
   - Set `Current Phase: planning`
   - After round 1+: update `Review History` with latest findings

2. **Call the Executor** (via Agent tool, subagent_type: general-purpose,
   model: {executor_model if not "inherit", else omit}):

   > **Why general-purpose**: plugin-defined agent types (review-loop:executor)
   > silently block Write/Edit tools due to sandbox restrictions. Use
   > general-purpose and manually include executor.md body in the prompt.

   Prompt template:
   ```
   You are the Executor in a review-loop workflow.

   {contents of agents/executor.md body — the system prompt}

   Read the context file first: {context_file}
   DO NOT modify the context file — return your output as described in
   the output format above.

   ## Your Task
   Produce a detailed solution plan following the output format in your
   instructions.

   {if round > 1:}
   ## Previous Reviewer Feedback (address each point)
   {reviewer_feedback — this is the one thing passed directly, for immediacy}
   ```

3. **Update context file with Executor's output** — BEFORE calling the
   Reviewer. This is critical: the Reviewer reads the context file for
   orientation. If the Executor produced a new/revised plan, write it
   to the `Approved Plan` (or `Draft Plan`) section now. If the Executor
   reported file changes, update `Files Changed`. The Reviewer must see
   the latest state, not stale data from the previous round.

4. **Call the Reviewer** (see "Reviewer Dispatch" section below) with:

   The template below is the **review content** — what the Reviewer should
   review. How it is delivered depends on the reviewer mode:
   - **codex mode**: prepend `agents/reviewer.md` body to this template
     (codex doesn't load agent definitions).
   - **subagent mode**: send this template as-is — `reviewer.md` is
     auto-loaded as the system prompt by the `review-loop:reviewer` agent.

   Review content template:
   ```
   Read the context file first: {context_file}
   DO NOT modify the context file.

   ## Solution Plan to Review
   {executor_plan}

   {if round > 1:}
   ## Review History
   You are reviewing round {round}. Here is what you found in previous rounds
   and what has changed since. Pay special attention to whether previously
   identified CRITICAL issues have been properly addressed.

   {for each finding in loop_state.findings:}
   - Round {n}: [{severity}] {description} → {Executor claims fixed | Still pending | Accepted}

   ## Your Focus This Round
   1. Verify that previously flagged CRITICAL issues are actually resolved
   2. Check whether the fixes introduced new problems
   3. **Scope Drift**: check whether the Executor quietly changed the plan's
      design decisions while addressing feedback. A fix for a CRITICAL issue
      should not silently introduce new trade-offs, relax constraints, or
      change the agreed approach. If it does, flag as CRITICAL.
   4. Review any new aspects of the plan not covered before

   {else (round == 1):}
   ## Your Task
   This is the first review. Review the plan critically from scratch.

   {endif}

   {if review_style is set:}
   ## Review Style
   {review_style}

   Return your structured verdict following the output format in your
   instructions above.
   ```

3. **Parse the Reviewer's response**:
   - Extract VERDICT (APPROVE / REQUEST_CHANGES)
   - Extract all issues with severity (CRITICAL / MINOR)
   - Update loop_state: add new findings, track resolved vs pending

4. **Display Live Report** to the user:
   ```
   ── review-loop: Round {n} (Planning) ───────────────
   Executor: {duration}s  |  Reviewer: {duration}s
   Reviewer found:
     [CRITICAL] {issue description}
     [MINOR] {issue description}
   Verdict: {APPROVE | REQUEST_CHANGES}
   {if APPROVE: ✓ Plan approved — proceeding to execution}
   {if REQUEST_CHANGES: → sending feedback to Executor...}
   ────────────────────────────────────────────────────
   ```
   If no issues: `Reviewer found: No issues. Clean approval.`

5. **Loop control**:
   - `VERDICT: APPROVE` → exit planning loop, proceed to Step 3.
   - `REQUEST_CHANGES` → feed feedback to next Executor round.
   - **Soft limit reached** (`round >= soft_limit_plan`) AND still has
     CRITICALs → ask the user: "Planning has run {N} rounds and still
     has open CRITICAL issues: {list}. Continue iterating, or proceed
     with current plan?" User decides.
   - **Stuck detection**: if the same CRITICAL issue appears 3 rounds in
     a row without progress, stop and escalate to the user — the Executor
     likely cannot resolve it without human guidance.

6. **If Executor raises a question during planning** — see "Question
   Classification" section below.

### Step 3 — Execution phase

Update loop state:
```
loop_state.phase = "execution"
loop_state.round = 0
```

**Round loop:**

1. **Update context file** before calling agents:
   - Update `Current Phase: execution`
   - Update `Approved Plan` (now populated)
   - Update `Files Changed` and `Key Related Files` after each Executor round
   - Update `Review History` with latest findings

2. **Call the Executor** (via Agent tool, subagent_type: general-purpose,
   model: {executor_model if not "inherit", else omit}):

   Prompt template:
   ```
   You are the Executor in a review-loop workflow.

   {contents of agents/executor.md body — the system prompt}

   Read the context file first: {context_file}
   DO NOT modify the context file — return your output as described in
   the output format above.

   ## Your Task
   Implement the approved plan (see context file). Make all necessary code
   changes. Follow the execution mode output format in your instructions.

   When done, list all files you modified/created so the Orchestrator can
   update the context file for the Reviewer.

   {if round > 1:}
   ## Code Review Feedback (address each point)
   {reviewer_cr_feedback — passed directly for immediacy}
   ```

3. **Update context file with Executor's output** — BEFORE calling the
   Reviewer. Write the Executor's change summary, updated file list, and
   any deviations from the plan into the context file. The Reviewer will
   read this file for orientation — stale data here means a wrong review.

4. **Call the Reviewer** (see "Reviewer Dispatch" section below) with:

   The template below is the **review content**. Delivery depends on mode:
   - **codex mode**: prepend `agents/reviewer.md` body to this template.
   - **subagent mode**: send as-is — `reviewer.md` is auto-loaded.

   Review content template:
   ```
   Read the context file first: {context_file}
   DO NOT modify the context file.
   It contains the problem description, approved plan, review history,
   changed files, and related files.

   ## Changes Made (summary from Executor)
   {executor_change_summary}

   ## Your Task
   Review the code changes against the approved plan in the context file.

   Check both **correctness** (does the code work?) AND **plan conformance**
   (does the code match the plan's design decisions?). If the Executor
   deviated from the plan — introduced new thresholds, relaxed constraints,
   changed the agreed approach — flag it as CRITICAL even if the code is
   technically correct.

   {if review_focus is set:}
   ## Project-Specific Review Priorities
   In addition to the standard review checklist, pay special attention to:
   {review_focus}

   {if round > 1:}
   The context above contains your previous findings. Verify that previously
   flagged CRITICAL issues are actually resolved in code — read the actual
   code, don't just take the Executor's word for it. Also check whether
   fixes introduced regressions or new issues.

   You have read-only access to the project files — use it.

   {if review_style is set:}
   ## Review Style
   {review_style}

   Return your structured verdict following the output format in your
   instructions above.
   ```

   > **Note**: the `review_style` and `review_focus` blocks above are
   > NOT inside the `{if round > 1}` branch — they apply to ALL rounds
   > including round 1. The template indentation above is for readability
   > only; structure the actual prompt so these fields appear unconditionally.

3. **Parse, update loop state, display Live Report** — same as planning phase.

4. **Loop control** — same logic as planning phase (APPROVE exits,
   soft limit uses `soft_limit_exec`, stuck detection applies).

### Step 3.5 — Quality Polish

> **Skip condition**: if `skip_quality_polish: true` in config, skip this
> entire step and go directly to Step 4.

After the execution loop exits with APPROVE, run supplementary quality
checks before delivery. This is NOT a replacement for the adversarial
review — it is automated polish that catches language-specific and
structural issues the adversarial reviewer may not focus on.

#### 3.5.1 — Language detection

Detect languages in changed files (both tracked and untracked):
```bash
{ git diff --name-only --diff-filter=d HEAD; git ls-files --others --exclude-standard; } | grep '\.' | sed 's/.*\.\([^.]*\)$/\1/' | sort -u
```

Map extensions to agents:
- `.go` → `review-loop:go-reviewer`
- `.rs` → `review-loop:rust-reviewer`
- `.py` → `review-loop:python-reviewer`
- `.ts/.tsx/.js/.jsx/.html/.vue/.svelte` → `review-loop:frontend-security-reviewer`
- Multiple languages → run all applicable agents

#### 3.5.2 — Language-specific static analysis

For each detected language, invoke the corresponding agent via `subagent_type: general-purpose` with the agent's full body inlined in the prompt (plugin-defined agent types have their tools silently blocked by Claude Code sandbox):
```
Agent tool:
  subagent_type: general-purpose
  prompt: |
    {contents of agents/<agent-name>.md body}

    IMPORTANT: Use Claude Code's native Bash tool to run shell commands.
    Do NOT use MCP server tools (e.g. run_bash_command).

    ## Changed Files
    {list of changed files for this language, from git diff --name-only --diff-filter=d HEAD}

    Run analysis on the changed files listed above. Context file: {context_file}
    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}
    {if review_style is set:}
    ## Review Style
    {review_style}
```

These agents report findings but should not fix code — include "Report only, do not modify files" in the prompt.

**Hallucination guard**: After each agent returns, check the Agent tool metadata. If `tool_uses: 0`, the agent did not actually read files or run commands — its output is fabricated. Discard the result and retry once. If the retry also has `tool_uses: 0`, skip this agent and report the failure to the user.

Display findings to the user. If any CRITICAL/HIGH issues found, invoke the
Executor (via `subagent_type: general-purpose`) to fix them, then re-run the
language agent to verify. Max **2 fix rounds**. If issues remain after 2
rounds, report them to user and continue to the next sub-step.

#### 3.5.3 — Code quality review-fix loop

Invoke code-reviewer and silent-failure-hunter on the changed code via `subagent_type: general-purpose` with agent body inlined:
```
Agent tool:
  subagent_type: general-purpose
  prompt: |
    {contents of agents/code-reviewer.md body}  (or silent-failure-hunter.md)

    Review the changed files listed in context file: {context_file}
    Report only, do not modify files.
    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}
    {if review_style is set:}
    ## Review Style
    {review_style}
```

- Parse findings, triage by severity
- If CRITICAL/HIGH issues: invoke Executor (`subagent_type: general-purpose`)
  to fix → re-review
- Max **3 rounds** or until clean
- **Stuck detection**: same issue persists 3 rounds = stop

#### 3.5.4 — Simplify

Invoke code-simplifier via `subagent_type: general-purpose` (it needs
Write/Edit tools):
```
Agent tool:
  subagent_type: general-purpose
  prompt: |
    {contents of agents/code-simplifier.md body}
    Simplify the recently changed files: {file_list from context file}
    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}
    {if review_style is set:}
    ## Review Style
    {review_style}
```

If simplify makes changes, run a quick build check to ensure nothing broke.
If build fails, revert the simplify changes and report to user.

#### 3.5.5 — Test consolidation

Invoke pr-test-analyzer via `subagent_type: general-purpose` with agent body inlined:
```
Agent tool:
  subagent_type: general-purpose
  prompt: |
    {contents of agents/pr-test-analyzer.md body}

    Analyze test coverage for the changed files. Context file: {context_file}
    {if quality_focus is set:}
    ## Quality Focus
    {quality_focus}
    {if review_style is set:}
    ## Review Style
    {review_style}
```

If gaps found, invoke Executor (`subagent_type: general-purpose`) to add
missing tests. Then run the build/test command:
- Go: `go test ./...`
- Rust: `cargo test`
- Python: `pytest` or project test command
- Other: language-appropriate command

Max **2 fix rounds** for test failures. If still failing after 2 rounds,
report remaining failures to user and proceed to delivery.

#### 3.5.6 — Display Quality Polish summary

```
── review-loop: Quality Polish ─────────────────────
Static analysis: {go-reviewer: PASS / rust-reviewer: 2 fixed / ...}
Code quality:    {3 rounds, 5 fixed, 0 remaining}
Simplify:        {4 improvements applied}
Tests:           {PASS (12 tests) / 2 added, 1 updated}
────────────────────────────────────────────────────
```

Update the context file with Quality Polish results and timing.

### Step 3.6 — Documentation Consistency Check

After Quality Polish, ensure documentation reflects the latest code changes.

#### 3.6.1 — Update project documentation (if any exists)

Search the project for documentation files:
- Design docs, architecture docs, ADRs (e.g. `docs/`, `design/`, `*.md` outside source dirs)
- Runbooks, operational guides
- Memory files (`.claude/memory/`, `tasks/lessons.md`)
- Learning notes, changelogs, wikis

For each doc found: read it, compare against the code changes in the context file. If the doc describes behavior, APIs, or logic that has changed, update it to reflect the new implementation. Focus on business logic accuracy — do not rewrite style.

If no project documentation is found: note "no project docs found" and proceed to 3.6.2.

#### 3.6.2 — Code comment consistency (always run)

For each changed file, read the current code and verify:
- Function/method docstrings and comments match the actual implementation
- Type/struct comments match actual fields and behavior
- Module-level comments match actual responsibilities
- Inline comments explain current logic (not stale from a previous version)

Fix any stale or incorrect comments directly using the Edit tool.

**Output:**
```
── review-loop: Doc Consistency ─────────────────────
Project docs:   {updated: X files / none found}
Comments fixed: {N} stale comments in {files / "none"}
─────────────────────────────────────────────────────
```

### Step 4 — Delivery

After Quality Polish completes (or is skipped), or user decides to stop:

1. **If `auto_commit: true`**: stage only the files reported as changed by the
   Executor using `git add <file1> <file2> ...` (never `git add -A` or
   `git add .`), then commit with message:
   `{commit_message_prefix}: {title}`

2. **Display Delivery Summary** to the user:

   ```
   ── review-loop: Delivery ───────────────────────────
   ## {title}
   **Status**: {Delivered | Stopped by user — unresolved issues noted below}
   **Reviewer**: {codex | subagent} ({reviewer_model})
   **Mode**: {interactive | handsfree}
   **Plan rounds**: {N}  |  **Exec rounds**: {N}
   **Quality Polish**: {ran / skipped}

   ### Review Findings
   | Round | Phase | Severity | Issue | Resolution |
   |-------|-------|----------|-------|------------|
   {for each finding in loop_state.findings:}
   | {round} | {phase} | {severity} | {description} | {Fixed in round N | Accepted | Unresolved} |

   ### Files Changed
   - {file1} — {what changed}
   - {file2} — {what changed}

   {if handsfree and autonomous_decisions:}
   ### Autonomous Decisions
   - [{question}] → {decision} (Reason: {reason})

   {if unresolved_minor_issues:}
   ### Unresolved Minor Issues
   - {issue} — {why unresolved}

   {if quality polish ran:}
   ### Quality Polish
   Static analysis: {agent: PASS/FIXED/...}
   Code quality:    {rounds, fixed, remaining}
   Simplify:        {improvements applied}
   Tests:           {PASS/FAIL (count)}

   ### Time Breakdown
   | Phase | Round | Executor | Reviewer | Round Total |
   |-------|-------|----------|----------|-------------|
   {for each round in loop_state.timing.steps, grouped by phase+round:}
   | {phase} | {round} | {executor_duration}s | {reviewer_duration}s | {sum}s |

   | | | **Executor Total** | **Reviewer Total** | **Loop Total** |
   | | | {sum_executor}s | {sum_reviewer}s | {total_elapsed}s |

   _Slowest step: {phase} round {N} {role} ({duration}s)_

   ### Token Usage (best-effort)
   | Role     | Tokens | Cost Estimate |
   |----------|--------|---------------|
   | Executor | {sum of Agent tool total_tokens across all rounds, if available} | — |
   | Reviewer | {if available from codex --json or Agent tool} | — |
   | Total    | {sum} | — |
   _Token counts are approximate. Reviewer tokens may show "N/A" in codex mode._

   ### Suggested Next Steps
   - {action items}
   ────────────────────────────────────────────────────
   ```

3. **If `docs_file` is set**: append the delivery summary (without the box
   drawing borders) to that file.

4. **Cleanup temp files**: delete all round output files for this session:
   `rm -f .claude/review-loop-sessions/{session_id}-round-*.txt`
   The context file (`.claude/review-loop-sessions/{session_id}.md`) is
   kept as a permanent record.

---

## Reviewer Dispatch

This section defines how the Orchestrator calls the Reviewer based on the
`reviewer` config value.

### Mode: `codex`

Use Bash to invoke the Codex CLI in non-interactive, read-only mode:

```bash
# If reviewer_model is empty, omit -m flag entirely (codex uses its default model)
# If reviewer_model is set, pass it via -m
codex exec -s read-only {if reviewer_model: -m {reviewer_model}} -o .claude/review-loop-sessions/{session_id}-round-{round}.txt - <<'REVIEW_PROMPT'
{reviewer_prompt}
REVIEW_PROMPT
```

Then read the output file to get the Reviewer's response.

**Important behaviors**:
- The Codex process runs in the same project directory, so it can read all
  project files in its read-only sandbox.
- Use `-o` to capture output to a file, then read it. This is more reliable
  than capturing stdout for long responses.
- If the codex command fails (non-zero exit), report the error to the user
  and ask whether to retry or switch to subagent mode.
- Each review call is stateless (no session persistence between rounds).
  The Orchestrator compensates by including Review History in the prompt.

**Prompt construction for codex mode**: prepend the full `agents/reviewer.md`
body (everything below the frontmatter) to the review content template from
Step 2.4 or Step 3.4. This is necessary because codex does not load Claude
Code agent definitions.

**Project conventions are loaded automatically**:
- Codex loads the project's `codex.md` on every invocation, so the user's
  coding standards and project-specific rules apply during review.
- Similarly, in subagent mode, Claude Code loads `CLAUDE.md` automatically.
- The Reviewer's output format does not need to be pretty — it is consumed
  by the Orchestrator (to extract VERDICT and issues), not by the user
  directly. The user sees the Live Report summary instead.

### Mode: `subagent`

Use the Agent tool to invoke the Reviewer as a Claude Code sub-agent:

```
Agent tool parameters:
  subagent_type: general-purpose
  model: {reviewer_model if set and not empty, else omit — inherits Orchestrator model}
  prompt: |
    {contents of agents/reviewer.md body}

    {review_content — the review content template from Step 2.4 or Step 3.4}

    IMPORTANT: You are a Reviewer. Report only, do not modify any files.
```

Due to the plugin agent type sandbox bug (all plugin-defined agent types have
their tools silently blocked), we use `general-purpose` and inline the
reviewer.md body in the prompt.

**Prompt construction for subagent mode**: prepend the `reviewer.md` body,
then append the review content template (context file content, the plan or
code to review, review history, and task instructions).

**Important behaviors**:
- The sub-agent runs in the same project directory and can read all project
  files via the Read tool (read-only sandbox).
- Claude Code loads `CLAUDE.md` automatically, so project conventions apply.
- The Agent tool returns the sub-agent's full response text. Parse it the
  same way as codex output: extract VERDICT and issues.
- If the Agent tool returns an error, report it to the user and ask whether
  to retry or switch to codex mode.
- Each review call is stateless (fresh sub-agent per call). The Orchestrator
  compensates by including Review History in the prompt.
- Token usage: the Agent tool metadata may include token counts. If
  available, accumulate them in `loop_state.token_usage.reviewer`.

---

## Question Classification

When the Executor raises a question (detected in its output), classify it:

- **External info** (credentials, file paths outside repo, business rules not
  in context) → **always pause and ask the user**, regardless of mode.

- **Decision-type** (architecture choice, approach trade-off, ambiguous
  requirement with multiple valid solutions):
  - **Default mode** → pause and ask the user.
  - **`--handsfree` mode** → forward to Reviewer via the configured mode:

    Decision prompt (review content only):
    ```
    ## Decision Required
    The Executor encountered a decision point and needs guidance:
    {executor_question}

    ## Work Item Context
    {title + context + acceptance_criteria}

    Please make a decision and provide brief reasoning.
    Return: DECISION: <your choice> \n REASON: <why>
    ```

    **If codex mode**: prepend `reviewer.md` body, then pass to `codex exec`.
    **If subagent mode**: prepend `reviewer.md` body, then pass to Agent tool
    with `subagent_type: general-purpose`.

    Log the decision in `loop_state` under autonomous_decisions.

---

## Context Management

The Orchestrator must actively manage its own context to avoid triggering
the AI's automatic compaction, which can lose critical information.

The context file (`.claude/review-loop-sessions/{uuid}.md`) is the single source of
truth for the entire loop. All critical state lives on disk, not in the
Orchestrator's conversation context.

**Sub-agent file access**: sub-agents read the context file from disk
each round. They are explicitly told NOT to modify the context file —
only the Orchestrator writes to it. This prevents sub-agents from
accidentally editing the file (which can cause permission errors in
`.claude/`).

**The Orchestrator's context stays minimal:**
- The context file path
- The latest Reviewer feedback (passed directly to the next Executor call)
- Loop control state (current phase, round number)

**After each round**, update the context file with:
- Latest findings and their resolution status
- Updated file change list (from Executor output)
- Any new related files discovered
- Plan updates (if planning phase)

Since all durable state is on disk, the Orchestrator's conversation context
stays lean and compaction is unlikely to be an issue.

---

## Important Orchestrator Behaviors

- **Never do the work yourself** — delegate planning to the Executor sub-agent
  and reviewing to the Reviewer (codex or subagent).
- **Keep the context file up to date** — both Executor and Reviewer read it
  at the start of each round. Update it after every round with latest
  findings, file changes, and plan updates before calling the next agent.
- **Preserve the Reviewer's VERDICT** — never override an `APPROVE` to keep
  iterating, and never skip a `REQUEST_CHANGES` to save time.
- **Surface blockers immediately** — if the Executor reports it cannot proceed,
  pause and ask the user rather than guessing.
- **Make findings visible** — the Live Report after each round is not optional.
  The whole point of this skill is that the user sees what the review process
  catches. Never silently pass feedback without reporting it.
