# Codex Review-Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Codex-native Stage 1 implementation of `review-loop` and `guide` that shares `.review-loop` state with the existing Claude implementation.

**Architecture:** Keep Claude Code's current plugin implementation untouched and add a parallel Codex-native path using repo skills plus project-scoped Codex subagents. The Codex `review-loop` skill will own orchestration, session-file writes, and reviewer dispatch; the Executor and fallback Reviewer will run as Codex subagents, while the default reviewer path invokes `claude -p` outside the Codex sandbox using JSON output.

**Tech Stack:** Markdown skill prompts, TOML Codex agent definitions, Claude CLI, Codex CLI, shared `.review-loop` Markdown state files.

---

## File Structure

### New files

- `.agents/skills/review-loop/SKILL.md`
  Codex-native orchestrator prompt for Stage 1 review-loop behavior.
- `.agents/skills/guide/SKILL.md`
  Codex-native guide prompt describing Stage 1 behavior and shared state.
- `.codex/agents/review-loop-executor.toml`
  Project-scoped Codex Executor subagent.
- `.codex/agents/review-loop-reviewer.toml`
  Project-scoped Codex fallback Reviewer subagent.

### Modified files

- `.gitignore`
  Ignore `.review-loop/tmp/` used for Claude reviewer prompt files.
- `review-loop-config.example.md`
  Document optional Codex-specific config keys and clarify shared-key behavior.
- `README.md`
  Add Codex usage notes, Stage 1 scope, and shared-state expectations.
- `CLAUDE.md`
  Add development notes for the Codex path, especially the shared protocol and
  the verified Claude CLI reviewer contract.

### Verification targets

- `.agents/skills/review-loop/SKILL.md`
- `.agents/skills/guide/SKILL.md`
- `.codex/agents/review-loop-executor.toml`
- `.codex/agents/review-loop-reviewer.toml`
- `review-loop-config.example.md`
- `README.md`
- `CLAUDE.md`
- `.gitignore`

## Task 1: Scaffold Codex Runtime Files

**Files:**
- Create: `.agents/skills/review-loop/SKILL.md`
- Create: `.agents/skills/guide/SKILL.md`
- Create: `.codex/agents/review-loop-executor.toml`
- Create: `.codex/agents/review-loop-reviewer.toml`

- [ ] **Step 1: Verify the Codex runtime files do not exist yet**

Run:

```bash
test -f .agents/skills/review-loop/SKILL.md
```

Expected: exit code 1

Run:

```bash
test -f .codex/agents/review-loop-executor.toml
```

Expected: exit code 1

- [ ] **Step 2: Create the Codex Executor subagent**

Write `.codex/agents/review-loop-executor.toml` with this structure:

```toml
name = "review_loop_executor"
description = "Executor for the Codex review-loop workflow. Produces structured planning and implementation output and never writes the session file directly."
model = "gpt-5.4"
model_reasoning_effort = "high"
sandbox_mode = "workspace-write"
developer_instructions = """
You are the Executor in a review-loop workflow.

You have two modes:
- Planning mode: return the exact planning schema required by the orchestrator.
- Execution mode: implement the approved plan and return the exact execution schema required by the orchestrator.

Rules:
- Never write the session file directly.
- Read relevant repository files before planning or editing.
- Use exact section headers required by the shared Executor output schema.
- In execution mode, list every modified or created file explicitly.
- If blocked by missing information, say so instead of guessing.
"""
```

- [ ] **Step 3: Create the Codex fallback Reviewer subagent**

Write `.codex/agents/review-loop-reviewer.toml` with this structure:

```toml
name = "review_loop_reviewer"
description = "Fallback reviewer for the Codex review-loop workflow. Returns the shared reviewer schema and never edits files."
model = "gpt-5.4"
model_reasoning_effort = "high"
sandbox_mode = "read-only"
developer_instructions = """
You are the Reviewer in a review-loop workflow.

Return the exact shared reviewer output schema:
- ### VERDICT: [APPROVE | REQUEST_CHANGES]
- ### Issues
- ### Strengths
- ### Questions

Rules:
- Never modify files.
- Read the session file and any referenced code before reviewing.
- In plan review, flag missing test strategy and unvalidated assumptions.
- In code review, enforce correctness, tests, and plan conformance.
"""
```

- [ ] **Step 4: Create the Codex `review-loop` skill skeleton**

Write `.agents/skills/review-loop/SKILL.md` with frontmatter and these core sections:

```md
---
name: review-loop
description: Codex-native Stage 1 review-loop skill. Orchestrates planning and execution with a Codex Executor and Claude/Codex reviewer backends while sharing .review-loop state with Claude Code.
---

# review-loop

## Stage 1 Scope
- review-loop
- shared .review-loop config and session files
- Claude CLI default reviewer
- Codex fallback reviewer

## Runtime Rules
- Codex is the orchestrator.
- Spawn `review_loop_executor` for plan and execution work.
- Use Claude CLI reviewer outside the sandbox with JSON output.
- Fall back to `review_loop_reviewer` if Claude reviewer invocation or validation fails.
- The orchestrator is the only writer of the session file.
```

- [ ] **Step 5: Create the Codex `guide` skill skeleton**

Write `.agents/skills/guide/SKILL.md` with this structure:

```md
---
name: guide
description: Show the Codex Stage 1 guide for review-loop.
---

# review-loop Guide

- `review-loop` uses the same `.review-loop/config.md` file as Claude Code.
- Session logs remain in `.review-loop/sessions/`.
- Codex acts as orchestrator.
- Claude CLI is the default reviewer backend when available.
- Codex reviewer is the automatic fallback backend.
```

- [ ] **Step 6: Verify the scaffold files exist and are readable**

Run:

```bash
for f in \
  .agents/skills/review-loop/SKILL.md \
  .agents/skills/guide/SKILL.md \
  .codex/agents/review-loop-executor.toml \
  .codex/agents/review-loop-reviewer.toml
do
  test -f "$f" || exit 1
done
```

Expected: exit code 0

- [ ] **Step 7: Commit the scaffold**

```bash
git add .agents/skills/review-loop/SKILL.md .agents/skills/guide/SKILL.md .codex/agents/review-loop-executor.toml .codex/agents/review-loop-reviewer.toml
git commit -m "feat: scaffold codex review-loop runtime"
```

## Task 2: Wire Shared Config and Repo Hygiene

**Files:**
- Modify: `.gitignore`
- Modify: `review-loop-config.example.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write the failing checks for missing Codex config documentation**

Run:

```bash
rg -n "codex_reviewer_backend|codex_reviewer_model|codex_executor_model" review-loop-config.example.md
```

Expected: no matches

Run:

```bash
rg -n "\.review-loop/tmp/" .gitignore
```

Expected: no matches

- [ ] **Step 2: Update `.gitignore` for temporary Claude reviewer prompt files**

Append this line to `.gitignore`:

```gitignore
.review-loop/tmp/
```

- [ ] **Step 3: Extend `review-loop-config.example.md` with Codex-specific optional keys**

Add commented examples and compatibility notes like:

```md
# Codex-only optional overrides. Claude Code should ignore these safely.
# codex_reviewer_backend: claude_cli   # "claude_cli" | "codex"
# codex_reviewer_model: ""             # model override for Codex fallback reviewer
# codex_executor_model: ""             # reserved in Stage 1; ignored
```

Also add a short note that:

```md
# In Codex runtime, the shared `reviewer` key is not used to select the reviewer backend.
# Codex defaults to Claude CLI, then falls back to the local Codex reviewer.
```

- [ ] **Step 4: Update `CLAUDE.md` with Codex-path development notes**

Add a short section that captures:

```md
## Codex Stage 1 Notes

- Codex skills live under `.agents/skills/`.
- Codex subagents live under `.codex/agents/*.toml`.
- The Claude reviewer contract for Codex uses:
  `claude -p --no-session-persistence --output-format json < prompt-file`
- This reviewer call must run outside the Codex sandbox.
- `.review-loop/config.md` and `.review-loop/sessions/*.md` remain the shared protocol.
```

- [ ] **Step 5: Verify the shared-config updates landed**

Run:

```bash
rg -n "codex_reviewer_backend|codex_reviewer_model|codex_executor_model" review-loop-config.example.md
```

Expected: 3 matches

Run:

```bash
rg -n "\.review-loop/tmp/" .gitignore
```

Expected: 1 match

- [ ] **Step 6: Commit the shared-config and hygiene changes**

```bash
git add .gitignore review-loop-config.example.md CLAUDE.md
git commit -m "docs: add codex shared-config guidance"
```

## Task 3: Implement the Codex `review-loop` Skill

**Files:**
- Modify: `.agents/skills/review-loop/SKILL.md`
- Test: `.codex/agents/review-loop-executor.toml`
- Test: `.codex/agents/review-loop-reviewer.toml`

- [ ] **Step 1: Write the failing static checks for missing Stage 1 contracts**

Run:

```bash
rg -n "claude -p --no-session-persistence --output-format json|review_loop_executor|review_loop_reviewer|Session Metadata|### VERDICT" .agents/skills/review-loop/SKILL.md
```

Expected: incomplete matches or missing lines

- [ ] **Step 2: Implement config loading and session-file rules**

Expand `.agents/skills/review-loop/SKILL.md` so it explicitly instructs Codex to:

```md
- read `.review-loop/config.md` if present
- create `.review-loop/sessions/{uuid}.md`
- create `.review-loop/tmp/{uuid}-reviewer-prompt.txt`
- keep Session Metadata as the final section
- rewrite canonical sections on each orchestrator update
- remain the only writer of the session file
```

- [ ] **Step 3: Implement Executor orchestration rules**

Add explicit instructions that:

```md
- spawn `review_loop_executor` for planning rounds
- spawn `review_loop_executor` for execution rounds
- reject malformed Executor output instead of guessing
- compare the Executor's claimed files against:
  `git diff --name-only --diff-filter=d HEAD`
  plus `git ls-files --others --exclude-standard`
```

- [ ] **Step 4: Implement reviewer dispatch and validation rules**

Add exact Stage 1 reviewer logic:

```md
- default reviewer path:
  `claude -p --no-session-persistence --output-format json {optional_model_flag} < .review-loop/tmp/{session_id}-reviewer-prompt.txt`
- run the Claude call outside the sandbox
- parse the first JSON result object from stdout
- validate the `result` field against the shared reviewer schema
- if validation or invocation fails, spawn `review_loop_reviewer`
- validate fallback reviewer output with the same schema rules
```

- [ ] **Step 5: Implement plan-round and code-round review content composition**

Add two distinct sections in `.agents/skills/review-loop/SKILL.md`:

```md
Plan review content:
- session file path
- current planning context
- latest Executor planning output
- prior review history when present

Code review content:
- session file path
- current execution context
- latest Executor execution output
- changed file list
- post-Executor diff against HEAD
- prior review history when present
```

- [ ] **Step 6: Implement Codex hallucination guard instructions**

Add a dedicated section that says:

```md
- reject Executor output that claims changed files absent from the actual post-Executor changed set
- reject reviewer output missing `### VERDICT`
- reject `REQUEST_CHANGES` output with no `### Issues`
- reject code-review findings with no concrete file references when files changed
```

- [ ] **Step 7: Verify the orchestrator prompt contains every required contract**

Run:

```bash
rg -n "review_loop_executor|review_loop_reviewer|claude -p --no-session-persistence --output-format json|git diff --name-only --diff-filter=d HEAD|git ls-files --others --exclude-standard|Session Metadata|REQUEST_CHANGES" .agents/skills/review-loop/SKILL.md
```

Expected: all patterns matched

- [ ] **Step 8: Commit the Codex `review-loop` skill**

```bash
git add .agents/skills/review-loop/SKILL.md
git commit -m "feat: add codex review-loop skill"
```

## Task 4: Implement the Codex `guide` Skill and README

**Files:**
- Modify: `.agents/skills/guide/SKILL.md`
- Modify: `README.md`

- [ ] **Step 1: Write the failing doc checks**

Run:

```bash
rg -n "\.agents/skills|Codex|claude -p|shared \.review-loop/config\.md|shared \.review-loop/sessions" README.md .agents/skills/guide/SKILL.md
```

Expected: missing or incomplete coverage

- [ ] **Step 2: Expand the Codex `guide` skill into a real Stage 1 guide**

Add sections covering:

```md
- what `review-loop` does in Codex
- that Claude Code and Codex share `.review-loop/config.md`
- that session logs are shared in `.review-loop/sessions/`
- that Claude CLI is the default reviewer path
- that Codex fallback review can be forced with `codex_reviewer_backend: codex`
- that Stage 1 only includes `review-loop` and `guide`
```

- [ ] **Step 3: Update `README.md` with Codex usage**

Add a short Codex section that documents:

```md
## Codex Stage 1

- Codex uses repo skills under `.agents/skills/`
- `review-loop` in Codex shares `.review-loop/config.md` and `.review-loop/sessions/` with Claude Code
- the default reviewer path in Codex uses Claude CLI
- fallback reviewer mode can be forced via `codex_reviewer_backend: codex`
- Stage 1 does not yet migrate `code-quality-loop`, `review-pr`, or `reorganize`
```

- [ ] **Step 4: Verify the user-facing docs are aligned**

Run:

```bash
rg -n "Codex Stage 1|codex_reviewer_backend|claude -p|\.review-loop/config\.md|\.review-loop/sessions/" README.md .agents/skills/guide/SKILL.md
```

Expected: all concepts found in both docs

- [ ] **Step 5: Commit the guide and README changes**

```bash
git add .agents/skills/guide/SKILL.md README.md
git commit -m "docs: add codex guide and usage notes"
```

## Task 5: Verify the Stage 1 Contract End-to-End

**Files:**
- Test: `.agents/skills/review-loop/SKILL.md`
- Test: `.agents/skills/guide/SKILL.md`
- Test: `.codex/agents/review-loop-executor.toml`
- Test: `.codex/agents/review-loop-reviewer.toml`
- Test: `review-loop-config.example.md`
- Test: `README.md`
- Test: `CLAUDE.md`
- Test: `.gitignore`

- [ ] **Step 1: Run the static contract checks**

Run:

```bash
rg -n "claude -p --no-session-persistence --output-format json|review_loop_executor|review_loop_reviewer|codex_reviewer_backend|codex_reviewer_model|codex_executor_model|\.review-loop/tmp/" \
  .agents/skills/review-loop/SKILL.md \
  .agents/skills/guide/SKILL.md \
  .codex/agents/review-loop-executor.toml \
  .codex/agents/review-loop-reviewer.toml \
  review-loop-config.example.md \
  README.md \
  CLAUDE.md \
  .gitignore
```

Expected: all required strings are present in the correct files

- [ ] **Step 2: Force deterministic local-reviewer mode in config**

Temporarily create or edit `.review-loop/config.md` with:

```md
codex_reviewer_backend: codex
```

Expected: the Codex skill can bypass the Claude CLI path for testing.

- [ ] **Step 3: Run a Codex smoke test against the repo**

Run:

```bash
codex exec -C . -s workspace-write -a on-request "Use the repo skill review-loop guide to explain the current Stage 1 behavior in this repository."
```

Expected:

- Codex discovers the repo skill
- The response mentions shared `.review-loop/config.md`
- The response mentions shared `.review-loop/sessions/`
- The response mentions Claude CLI default reviewer and Codex fallback

- [ ] **Step 4: Remove the temporary local-reviewer override if it was only for smoke testing**

Run:

```bash
rm -f .review-loop/config.md
```

Expected: temporary local-only test config removed

- [ ] **Step 5: Review the final diff and create the integration commit**

Run:

```bash
git status --short
git diff -- .agents .codex .gitignore README.md CLAUDE.md review-loop-config.example.md
```

Expected: only the planned Stage 1 files changed

- [ ] **Step 6: Commit the verified Stage 1 implementation**

```bash
git add .agents .codex .gitignore README.md CLAUDE.md review-loop-config.example.md
git commit -m "feat: add codex stage 1 review-loop support"
```
