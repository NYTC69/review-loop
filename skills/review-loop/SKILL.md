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
    └── mode: subagent   — Claude Code sub-agent (read-only tools) [TODO]
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
reviewer_model: ""              # codex: passed to -m flag (empty = codex default model); subagent: Agent tool model
executor_model: inherit         # Executor sub-agent model ("inherit" | "sonnet" | "opus")
soft_limit_plan: 3              # after N rounds, ask user whether to continue (if CRITICALs remain)
soft_limit_exec: 3              # same for execution phase
auto_commit: false              # commit after execution phase completes
commit_message_prefix: "feat"   # conventional commit prefix
docs_file: CHANGELOG.md         # file to append delivery summary to; "" to skip
handsfree: false                # set true to make --handsfree the default
```

The `--handsfree` flag at invocation always overrides the config value.

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
   - If `reviewer: subagent`: no check needed (uses Agent tool). [TODO]

### Step 1 — Parse the work item

Extract from the user's message:
- **Title**: one-line summary
- **Context**: any background info, file paths, constraints mentioned
- **Acceptance criteria**: what "done" looks like (infer if not stated)

If critical information is missing, ask ONE clarifying question before proceeding.

Display to the user:
```
── review-loop: Starting ──────────────────────────
Work item: {title}
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
  token_usage: {       # best-effort tracking
    executor: 0,       # sum from Agent tool metadata
    reviewer: 0        # sum from codex/agent metadata (N/A if unavailable)
  }
}
```

**Round loop:**

1. **Call the Executor** (via Agent tool, subagent_type: general-purpose,
   model: {executor_model}):

   Prompt template:
   ```
   You are the Executor in a review-loop workflow.

   {contents of agents/executor.md body — the system prompt}

   ## Work Item
   {title}

   ## Context
   {context}

   ## Acceptance Criteria
   {acceptance_criteria}

   ## Your Task
   Produce a detailed solution plan following the output format in your instructions.

   {if round > 1:}
   ## Previous Reviewer Feedback (address each point)
   {reviewer_feedback}

   ## Previously Identified Issues (for reference)
   {loop_state.pending_issues formatted as list}
   ```

2. **Call the Reviewer** (see "Reviewer Dispatch" section below) with:

   Prompt template:
   ```
   {contents of agents/reviewer.md body — the system prompt}

   ## Work Item
   {title}

   ## Solution Plan to Review
   {executor_plan}

   ## Acceptance Criteria
   {acceptance_criteria}

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

   Return your structured verdict following the output format in your
   instructions above.
   ```

3. **Parse the Reviewer's response**:
   - Extract VERDICT (APPROVE / REQUEST_CHANGES)
   - Extract all issues with severity (CRITICAL / MINOR)
   - Update loop_state: add new findings, track resolved vs pending

4. **Display Live Report** to the user:
   ```
   ── review-loop: Round {n}/{max} (Planning) ─────────
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

1. **Call the Executor** (via Agent tool):

   Prompt template:
   ```
   You are the Executor in a review-loop workflow.

   {contents of agents/executor.md body}

   ## Approved Plan
   {final_plan}

   ## Work Item
   {title}

   ## Your Task
   Implement the approved plan. Make all necessary code changes. Follow the
   execution mode output format in your instructions.

   {if round > 1:}
   ## Code Review Feedback (address each point)
   {reviewer_cr_feedback}

   ## Previously Identified Issues (for reference)
   {loop_state.pending_issues formatted as list}
   ```

2. **Call the Reviewer** with:

   Prompt template:
   ```
   {contents of agents/reviewer.md body}

   ## Work Item
   {title}

   ## Approved Plan
   {final_plan}

   ## Changes Made (summary from Executor)
   {executor_change_summary}

   {if round > 1:}
   ## Review History
   You are reviewing round {round} of the execution phase. Here is what you
   found in previous rounds. Verify that fixes are correct and complete —
   don't just take the Executor's word for it, read the actual code.

   {for each finding in loop_state.findings where phase == "execution":}
   - Round {n}: [{severity}] {description} → {Executor claims fixed | Still pending | Accepted}

   {also include unresolved findings from planning phase if any:}
   Carried over from planning:
   - [{severity}] {description} → {status}

   ## Your Focus This Round
   1. Verify previously flagged CRITICAL issues are actually resolved in code
   2. Check whether the fixes introduced regressions or new issues
   3. **Plan Conformance**: verify that the Executor's fix stays within the
      scope of the approved plan. If the fix introduces new design decisions,
      thresholds, trade-offs, or relaxations NOT authorized by the plan,
      flag it as CRITICAL — the Executor should escalate design changes back
      to the planning phase rather than making autonomous compromises during
      execution. A fix that is "logically correct" but deviates from the
      agreed approach is still a CRITICAL issue.
   4. Review any new code not covered in previous rounds

   {else (round == 1):}
   ## Your Task
   This is the first code review. Review the implementation against the
   approved plan above.

   Check both correctness (does the code work?) AND conformance (does the
   code match the plan's design decisions?). If the Executor deviated from
   the plan — even if the code is technically correct — flag it as CRITICAL.

   You have read-only access to the project files — use it to verify the
   changes, don't rely solely on the Executor's summary.

   Return your structured verdict following the output format in your
   instructions above.
   ```

3. **Parse, update loop state, display Live Report** — same as planning phase.

4. **Loop control** — same logic as planning phase (APPROVE exits,
   soft limit uses `soft_limit_exec`, stuck detection applies).

### Step 4 — Delivery

After execution loop exits with `APPROVE` (or user decides to stop):

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

   ### Token Usage (best-effort)
   | Role     | Tokens | Cost Estimate |
   |----------|--------|---------------|
   | Executor | {sum of Agent tool total_tokens across all rounds, if available} | — |
   | Reviewer | {if available from codex --json or Agent tool} | — |
   | Total    | {sum} | — |
   _Note: token counts are approximate. Executor tokens come from Agent tool
   metadata. Reviewer tokens may be unavailable in codex mode (shows "N/A")._

   ### Suggested Next Steps
   - {action items}
   ────────────────────────────────────────────────────
   ```

3. **If `docs_file` is set**: append the delivery summary (without the box
   drawing borders) to that file.

---

## Reviewer Dispatch

This section defines how the Orchestrator calls the Reviewer based on the
`reviewer` config value.

### Mode: `codex`

Use Bash to invoke the Codex CLI in non-interactive, read-only mode:

```bash
# If reviewer_model is empty, omit -m flag entirely (codex uses its default model)
# If reviewer_model is set, pass it via -m
codex exec -s read-only {if reviewer_model: -m {reviewer_model}} -o /tmp/review-loop-{session_id}-{round}.txt - <<'REVIEW_PROMPT'
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

**Project conventions are loaded automatically**:
- Codex loads the project's `codex.md` on every invocation, so the user's
  coding standards and project-specific rules apply during review.
- Similarly, in subagent mode, Claude Code loads `CLAUDE.md` automatically.
- The Reviewer's output format does not need to be pretty — it is consumed
  by the Orchestrator (to extract VERDICT and issues), not by the user
  directly. The user sees the Live Report summary instead.

### Mode: `subagent` [TODO — Phase B]

Use the Agent tool with subagent_type set to the reviewer agent name, with
read-only tools. Prompt includes the full reviewer.md instructions + review
content. To be implemented after codex mode is tested.

---

## Question Classification

When the Executor raises a question (detected in its output), classify it:

- **External info** (credentials, file paths outside repo, business rules not
  in context) → **always pause and ask the user**, regardless of mode.

- **Decision-type** (architecture choice, approach trade-off, ambiguous
  requirement with multiple valid solutions):
  - **Default mode** → pause and ask the user.
  - **`--handsfree` mode** → forward to Reviewer:
    ```
    {reviewer.md instructions}

    ## Decision Required
    The Executor encountered a decision point and needs guidance:
    {executor_question}

    ## Work Item Context
    {title + context + acceptance_criteria}

    Please make a decision and provide brief reasoning.
    Return: DECISION: <your choice> \n REASON: <why>
    ```
    Log the decision in `loop_state` under autonomous_decisions.

---

## Context Management

The Orchestrator must actively manage its own context to avoid triggering
the AI's automatic compaction, which can lose critical information.

### Pinned Context (NEVER compact or summarize these)

The following must be retained verbatim in the Orchestrator's context at
all times, from the moment they are created until the loop ends:

1. **Approved Plan** (`final_plan`) — the Reviewer needs this every round
   to check Plan Conformance. If this is lost, the Reviewer cannot detect
   Executor deviations. This is the single most important piece of context.
2. **Accumulated Findings** (`loop_state.findings`) — the full list of
   every issue found, its severity, and resolution status. This is what
   makes the review loop valuable.
3. **Work Item** (title, context, acceptance criteria) — the "why" behind
   everything.

### Compactable Context (actively compress after each round)

4. Raw sub-agent outputs — after extracting the plan/summary/findings,
   compress to a one-paragraph summary. Do NOT accumulate full responses.
5. Raw Reviewer outputs — after parsing VERDICT and issues, discard the
   full text. The extracted findings in `loop_state` are sufficient.
6. Live Reports already displayed to the user — no need to retain.

### Proactive Compaction Strategy

Before each round, estimate whether the next Executor + Reviewer cycle
will fit in context. If it's getting tight:

1. First: compact all raw outputs from completed rounds (rule 4-6 above).
2. If still tight: summarize the Executor's change descriptions from
   earlier rounds, keeping only the latest round's details.
3. **NEVER** summarize or drop the Approved Plan or Findings — if context
   is so limited that these don't fit, stop the loop and deliver what you
   have, noting "stopped due to context limits" in the delivery summary.

### Note for users on context limits

If you see context running low during a loop, you can manually compact
the conversation. When you do, **prioritize preserving**:
- The approved plan (this is what the Reviewer checks conformance against)
- The accumulated findings and learnings

---

## Important Orchestrator Behaviors

- **Never do the work yourself** — delegate planning to the Executor sub-agent
  and reviewing to the Reviewer (codex or subagent).
- **Pass full context to each call** — both Executor and Reviewer start fresh
  each round. Include everything they need in the prompt; don't assume they
  remember prior turns.
- **Preserve the Reviewer's VERDICT** — never override an `APPROVE` to keep
  iterating, and never skip a `REQUEST_CHANGES` to save time.
- **Surface blockers immediately** — if the Executor reports it cannot proceed,
  pause and ask the user rather than guessing.
- **Make findings visible** — the Live Report after each round is not optional.
  The whole point of this skill is that the user sees what the review process
  catches. Never silently pass feedback without reporting it.
