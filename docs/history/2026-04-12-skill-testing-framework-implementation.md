# Skill Testing Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-version skill testing and validation framework for `review-loop` and `guide` that combines static contract checks with a small number of real smoke runs.

**Architecture:** The framework will live alongside the repository as `tests/skills/` plus `scripts/` entrypoints. Static checks will be driven by JSON contract definitions, while smoke cases will run through lightweight JSON case definitions and capture artifacts into a stable directory for debugging and future regression use.

**Tech Stack:** Shell entrypoints, JSON contract files, JSON smoke definitions, repository fixtures, local `codex` / `claude` CLIs for smoke runs when available.

---

## File Structure

### New files

- `tests/skills/contracts/review-loop.json`
  Static assertions for the `review-loop` wrappers and related agents.
- `tests/skills/contracts/guide.json`
  Static assertions for the `guide` wrappers and docs.
- `tests/skills/contracts/shared-schema.json`
  Shared schema assertions reused by multiple checks.
- `tests/skills/fixtures/sessions/codex-planning-approved.md`
  Stable session fixture for schema validation.
- `tests/skills/fixtures/reviewer-prompts/claude-planning-review.txt`
  Stable reviewer prompt fixture for schema-oriented checks.
- `tests/skills/fixtures/expected/reviewer-schema.json`
  Expected reviewer contract sample.
- `tests/skills/smoke/guide.shared-state.codex.json`
  Codex guide smoke-case definition.
- `tests/skills/smoke/review-loop.noop.codex-fallback.json`
  Codex fallback reviewer smoke-case definition.
- `tests/skills/smoke/review-loop.noop.claude-default.json`
  Default Claude reviewer smoke-case definition.
- `tests/skills/smoke/guide.plugin-surface.readme.json`
  Static docs-boundary smoke/lint case definition.
- `scripts/run-skill-lint`
  Static lint driver.
- `scripts/run-skill-smoke`
  Smoke driver.
- `scripts/run-skill-tests`
  Unified runner for lint + smoke.

### Runtime-generated paths

- `tests/skills/.artifacts/<case-id>/`
  Per-case evidence directory.
- `tests/skills/.last-run.json`
  Aggregate result file.

### Modified files

- `.gitignore`
  Ignore `tests/skills/.artifacts/` and `tests/skills/.last-run.json` if not already ignored elsewhere.
- `README.md`
  Add a brief note about the new testing framework entrypoints if needed.

## Task 1: Scaffold the Test Framework Layout

**Files:**
- Create: `tests/skills/contracts/review-loop.json`
- Create: `tests/skills/contracts/guide.json`
- Create: `tests/skills/contracts/shared-schema.json`
- Create: `tests/skills/smoke/guide.shared-state.codex.json`
- Create: `tests/skills/smoke/review-loop.noop.codex-fallback.json`
- Create: `tests/skills/smoke/review-loop.noop.claude-default.json`
- Create: `tests/skills/smoke/guide.plugin-surface.readme.json`
- Modify: `.gitignore`

- [ ] **Step 1: Verify the target layout does not already exist**

Run:

```bash
test -d tests/skills
```

Expected: exit code 1

- [ ] **Step 2: Create the contracts directory and JSON contract stubs**

Create:

```text
tests/skills/contracts/review-loop.json
tests/skills/contracts/guide.json
tests/skills/contracts/shared-schema.json
```

Each file should be valid JSON with at least:

```json
{
  "id": "review-loop",
  "description": "Static contract assertions for the review-loop workflow",
  "assertions": []
}
```

Use target-appropriate ids/descriptions per file.

- [ ] **Step 3: Create the smoke-case JSON stubs**

Create the four smoke/lint case files under `tests/skills/smoke/` with this
minimum schema:

```json
{
  "id": "guide.shared-state.codex",
  "type": "smoke",
  "target": "guide",
  "runtime": "codex",
  "requires": ["codex"],
  "setup": {},
  "command": ["command", "goes", "here"],
  "artifacts": {
    "capture": {
      "session_path": "latest_session",
      "git_status_before": "git_status_before",
      "git_status_after": "git_status_after"
    },
    "required": [
      "session_path",
      "git_status_before",
      "git_status_after",
      "assertions",
      "meta"
    ]
  },
  "assertions": ["assertion_id"]
}
```

Set the correct ids, types, targets, and runtimes for each case from the spec.

- [ ] **Step 4: Update `.gitignore`**

Add:

```gitignore
tests/skills/.artifacts/
tests/skills/.last-run.json
```

- [ ] **Step 5: Verify the scaffold exists**

Run:

```bash
for f in \
  tests/skills/contracts/review-loop.json \
  tests/skills/contracts/guide.json \
  tests/skills/contracts/shared-schema.json \
  tests/skills/smoke/guide.shared-state.codex.json \
  tests/skills/smoke/review-loop.noop.codex-fallback.json \
  tests/skills/smoke/review-loop.noop.claude-default.json \
  tests/skills/smoke/guide.plugin-surface.readme.json
do
  test -f "$f" || exit 1
done
```

Expected: exit code 0

- [ ] **Step 6: Commit the scaffold**

```bash
git add tests/skills .gitignore
git commit -m "feat: scaffold skill test framework"
```

## Task 2: Encode Static Contract Assertions

**Files:**
- Modify: `tests/skills/contracts/review-loop.json`
- Modify: `tests/skills/contracts/guide.json`
- Modify: `tests/skills/contracts/shared-schema.json`
- Create: `tests/skills/contracts/assertion-mapping.json`

- [ ] **Step 1: Write the failing assertion inventory**

Run:

```bash
rg -n "\"assertions\": \\[\\]" tests/skills/contracts
```

Expected: three matches

- [ ] **Step 2: Encode `review-loop` contract assertions**

Populate `tests/skills/contracts/review-loop.json` with explicit assertions for:

- Claude wrapper existence and key strings, including explicit assertions on:
  - `skills/review-loop/SKILL.md`
  - `soft_limit_plan`
  - `soft_limit_exec`
  - `Current Phase`
  - `Approved Plan`
  - `Review History`
- `Quality Polish`
- `Delivery`
- Codex wrapper existence and key strings, including explicit assertions on:
  - `.agents/skills/review-loop/SKILL.md`
  - `.review-loop/sessions/{uuid}.md`
  - `.review-loop/tmp/{uuid}-reviewer-prompt.txt`
  - `review_loop_executor`
  - `review_loop_reviewer`
  - `## Session Metadata`
  - `### Reviewer Output Schema`
  - `### Executor Output Schema`
- Codex executor/reviewer references
- Claude reviewer command contract
- soft limit behavior
- hallucination guard presence
- forbidden-pattern assertions:
  - Claude/plugin side must not contain `subagent_type: review-loop:`
  - Codex side must not rely on inherited or forked parent-thread context
- cross-file consistency assertions for:
  - executor schema parity between `.agents/skills/review-loop/SKILL.md` and `.codex/agents/review-loop-executor.toml`
  - reviewer schema parity between `.agents/skills/review-loop/SKILL.md` and `.codex/agents/review-loop-reviewer.toml`

Represent each assertion with explicit fields like:

```json
{
  "kind": "contains",
  "path": ".agents/skills/review-loop/SKILL.md",
  "needle": "claude -p --no-session-persistence --output-format json"
}
```

- [ ] **Step 3: Encode `guide` contract assertions**

Populate `tests/skills/contracts/guide.json` with assertions for:

- shared config path mention
- shared sessions path mention
- `codex_reviewer_backend`
- Codex/Claude surface distinction
- route marker for the `guide.plugin-surface.readme` lint case

- [ ] **Step 4: Encode `shared-schema` assertions**

Populate `tests/skills/contracts/shared-schema.json` with assertions for:

- canonical session section names
- reviewer schema headings
- allowed reviewer severities
- empty-issues handling
- cross-file config semantics for:
  - `reviewer_model`
  - `codex_reviewer_model`
  - `executor_model`
  - `codex_executor_model`

- [ ] **Step 5: Encode explicit cross-file consistency checks**

Add machine-readable assertions that compare required semantics across:

- `review-loop-config.example.md`
- `README.md`
- `.agents/skills/guide/SKILL.md`
- `.agents/skills/review-loop/SKILL.md`
- `.codex/agents/review-loop-executor.toml`
- `.codex/agents/review-loop-reviewer.toml`

This step must encode:

- `codex_reviewer_backend` semantics consistency
- reviewer model semantics consistency
- executor model semantics consistency
- fallback reviewer model semantics consistency
- executor schema parity
- reviewer schema parity

- [ ] **Step 5.5: Encode assertion mappings explicitly**

Create `tests/skills/contracts/assertion-mapping.json` with two sections:

- `smoke_assertions`
- `consistency_mappings`

`smoke_assertions` must define concrete checks for:

- `session_created`
- `planning_round_recorded`
- `execution_or_noop_round_recorded`
- `reviewer_backend_codex`
- `reviewer_prompt_exists`
- `shared_config_path_mentioned`
- `shared_sessions_path_mentioned`
- `reviewer_backend_behavior_mentioned`

`consistency_mappings` must define explicit file/needle pairs for:

- `reviewer_model`
- `codex_reviewer_backend`
- `codex_reviewer_model`
- `executor_model`
- `codex_executor_model`
- executor schema parity
- reviewer schema parity

- [ ] **Step 6: Verify assertions are explicit and non-empty**

Run:

```bash
rg -n "\"assertions\": \\[" tests/skills/contracts
rg -n "\"needle\"" tests/skills/contracts
```

Expected: assertions present and non-empty

- [ ] **Step 7: Commit the contract definitions**

```bash
git add tests/skills/contracts
git commit -m "feat: add skill contract definitions"
```

## Task 3: Add Stable Fixtures

**Files:**
- Create: `tests/skills/fixtures/sessions/codex-planning-approved.md`
- Create: `tests/skills/fixtures/reviewer-prompts/claude-planning-review.txt`
- Create: `tests/skills/fixtures/expected/reviewer-schema.json`

- [ ] **Step 1: Choose stable fixture content**

Source the content from already-observed repository-safe examples:

- a planning-approved Codex session sourced from one of:
  - `.review-loop/sessions/00C2D4D1-B699-4FC3-B6E7-D0AC3C8B304C.md`
  - `.review-loop/sessions/a84abfa3-f29e-4cb4-88b2-730a10612d5a.md`
- a reviewer prompt shape for planning review sourced from the captured
  planning-review prompt used during the Stage 1 Claude reviewer validation
- an expected reviewer schema sample

Keep fixtures small and stable; remove irrelevant noise.

If the listed session-source paths do not exist in the current worktree,
author a minimal synthetic fixture that satisfies the schema in Step 2 instead
of blocking the task.

- [ ] **Step 2: Create the session fixture**

Write a compact but schema-complete sample session markdown file that includes:

- `Problem Description`
- `Context`
- `Acceptance Criteria`
- `Current Phase`
- `Approved Plan`
- `Review History`
- `Files Changed`
- `Key Related Files`
- `Timing Log`
- `Session Metadata`

Ensure `Session Metadata` is last.

- [ ] **Step 3: Create the reviewer prompt fixture**

Write a stable planning-review prompt sample showing:

- review-only instruction
- shared session file path
- current planning-phase context
- latest Executor planning output
- explicit reviewer schema

- [ ] **Step 4: Create the expected reviewer schema sample**

Write `tests/skills/fixtures/expected/reviewer-schema.json` with an expected
machine-readable representation such as:

```json
{
  "verdicts": ["APPROVE", "REQUEST_CHANGES"],
  "allowed_severities": ["CRITICAL", "MINOR"],
  "required_sections": ["VERDICT", "Strengths"],
  "optional_sections": ["Issues", "Questions"]
}
```

- [ ] **Step 5: Verify fixture completeness**

Run:

```bash
rg -n "## Session Metadata" tests/skills/fixtures/sessions/codex-planning-approved.md
rg -n "### VERDICT: \\[APPROVE \\| REQUEST_CHANGES\\]" tests/skills/fixtures/reviewer-prompts/claude-planning-review.txt
```

Expected: both patterns found

- [ ] **Step 6: Commit the fixtures**

```bash
git add tests/skills/fixtures
git commit -m "feat: add skill test fixtures"
```

## Task 4: Implement `run-skill-lint`

**Files:**
- Create: `scripts/run-skill-lint`
- Test: `tests/skills/contracts/*.json`
- Test: `tests/skills/fixtures/*`

- [ ] **Step 1: Write the failing runner invocation**

Run:

```bash
test -x scripts/run-skill-lint
```

Expected: exit code 1

- [ ] **Step 2: Implement the lint runner**

Write `scripts/run-skill-lint` as an executable shell script that:

1. walks the JSON contract files
2. evaluates simple assertion kinds such as:
   - `exists`
   - `contains`
   - `not_contains`
   - `section_last`
   - `consistent_with`
3. prints `PASS / FAIL / SKIP`
4. writes `tests/skills/.last-run.json` for lint cases

The first version does not need a complex engine; keep it simple and explicit.

For v1, define `consistent_with` narrowly:

- compare literal normalized phrases across a fixed set of files
- normalization means: lowercase, collapse repeated whitespace, strip Markdown
  backticks
- do not attempt fuzzy semantic matching
- use it only for the explicit config/runtime semantics listed in Task 2
- load the actual comparison pairs from
  `tests/skills/contracts/assertion-mapping.json`

This narrow definition is intentional for v1. It reduces scope from broader
semantic equivalence to deterministic normalized-text consistency.

`tests/skills/.last-run.json` must use merge/update semantics across runners:

- lint creates the file if absent
- smoke loads the existing file and appends or updates smoke case results
- `scripts/run-skill-tests` must preserve both lint and smoke results in the
  final aggregate file

- [ ] **Step 2.5: Route `guide.plugin-surface.readme` into lint**

Implement explicit discovery/routing for
`tests/skills/smoke/guide.plugin-surface.readme.json` inside
`scripts/run-skill-lint`.

This case must be executed by lint, not by smoke.

- [ ] **Step 3: Make the runner executable**

Run:

```bash
chmod +x scripts/run-skill-lint
```

- [ ] **Step 4: Run lint and make it pass**

Run:

```bash
scripts/run-skill-lint
```

Expected:

- terminal shows case statuses
- `tests/skills/.last-run.json` created
- no failing contract checks on the current repo state

- [ ] **Step 5: Commit the lint runner**

```bash
git add scripts/run-skill-lint
git commit -m "feat: add skill lint runner"
```

## Task 5: Implement `run-skill-smoke`

**Files:**
- Create: `scripts/run-skill-smoke`
- Test: `tests/skills/smoke/*.json`
- Test: `tests/skills/.artifacts/`

- [ ] **Step 1: Write the failing runner invocation**

Run:

```bash
test -x scripts/run-skill-smoke
```

Expected: exit code 1

- [ ] **Step 2: Implement smoke-case loading**

Write `scripts/run-skill-smoke` as an executable shell script that:

1. loads smoke case JSON definitions
2. checks required CLIs (`codex`, `claude`)
3. returns `SKIP` when a required CLI is unavailable
4. creates `tests/skills/.artifacts/<case-id>/`
5. honors `execution_policy: strict|best_effort` per case (default
   `strict`); under `best_effort`, subprocess timeouts downgrade to
   `status: "skip"` with reason `"best-effort smoke timed out"` or
   `"best-effort smoke hit environment limitation"` when the timed-out
   stderr matches an entry in the runner's environment-signal list, while
   assertion failures, non-timeout non-zero exits, missing artifacts, and
   malformed case JSON continue to FAIL

Use these exact command patterns for v1:

Set:

```bash
WORKTREE=$(git rev-parse --show-toplevel)
```

at the top of the smoke runner and use that value in all `codex exec -C
"$WORKTREE"` commands.

**Guide smoke**

```bash
codex exec -C "$WORKTREE" -s workspace-write --ephemeral \
  "Use the repo skill guide to explain the current Stage 1 behavior in this repository. Include the shared config path, shared sessions path, and reviewer backend behavior."
```

**Review-loop fallback smoke**

```bash
codex exec -C "$WORKTREE" -s workspace-write --ephemeral \
  "Use the repo skill review-loop to handle this task end-to-end: add one concise sentence to the Codex Stage 1 section of README.md clarifying that executor_model is ignored in Codex Stage 1 and codex_executor_model remains reserved. If the task is already satisfied, complete the loop as a no-op round rather than making a duplicate edit."
```

**Review-loop Claude-default smoke**

```bash
codex exec -C "$WORKTREE" --dangerously-bypass-approvals-and-sandbox --ephemeral \
  "Use the repo skill review-loop to handle this task end-to-end: add one concise sentence to the Codex Stage 1 section of README.md clarifying that executor_model is ignored in Codex Stage 1 and codex_executor_model remains reserved. If the task is already satisfied, complete the loop as a no-op round rather than making a duplicate edit."
```

Rationale: this case must allow the default Claude reviewer path to execute
outside the Codex sandbox, while the forced-Codex-fallback smoke does not need
that escape hatch.

The smoke runner must evaluate assertion ids by consulting
`tests/skills/contracts/assertion-mapping.json` rather than hardcoding
ad-hoc shell logic per id.

- [ ] **Step 3: Implement the `guide.shared-state.codex` smoke**

Run a minimal Codex repo-skill guide invocation and assert:

- `.review-loop/config.md` appears in output
- `.review-loop/sessions/` appears in output
- reviewer backend behavior is described

Store:

- `response.txt`
- `assertions.json`
- `meta.json`
- `stderr.txt` when non-empty

- [ ] **Step 4: Implement the `review-loop.noop.codex-fallback` smoke**

Use a temporary `.review-loop/config.md` containing:

```text
codex_reviewer_backend: codex
```

Assertions:

- session file created
- planning round recorded
- execution or no-op round recorded
- reviewer backend recorded as `codex`
- `git-status-before.txt` captured
- `git-status-after.txt` captured

Store at minimum:

- `session-path.txt`
- `session-final.md`
- `meta.json`
- `stdout.txt`
- `stderr.txt` when non-empty
- `git-status-before.txt`
- `git-status-after.txt`

If `.review-loop/config.md` already exists, backup it, restore it afterward.

- [ ] **Step 5: Implement the `review-loop.noop.claude-default` smoke**

Assertions:

- session file created
- reviewer prompt file exists or existed as an artifact
- reviewer prompt file existence is a required success-path assertion
- if Claude reviewer succeeds, preserve structured result evidence
- if fallback occurs, preserve fallback reason evidence

Store at minimum:

- `session-path.txt`
- `session-final.md`
- `reviewer-prompt.txt`
- `reviewer-result.json` when available
- `meta.json`
- `stdout.txt`
- `stderr.txt` when non-empty

This case must require both `codex` and `claude`, and must `SKIP` if either is
missing.

- [ ] **Step 6: Treat `guide.plugin-surface.readme` as lint, not smoke**

Do not implement this case in `run-skill-smoke`.
Document in the smoke runner that this case belongs to lint.

- [ ] **Step 7: Run smoke and inspect artifacts**

Run:

```bash
scripts/run-skill-smoke
```

Expected:

- per-case `PASS / FAIL / SKIP`
- artifacts created under `tests/skills/.artifacts/`
- `tests/skills/.last-run.json` updated with smoke results

- [ ] **Step 8: Commit the smoke runner**

```bash
git add scripts/run-skill-smoke tests/skills/smoke
git commit -m "feat: add skill smoke runner"
```

## Task 6: Implement Unified Entry Point and Docs

**Files:**
- Create: `scripts/run-skill-tests`
- Modify: `README.md`

- [ ] **Step 1: Write the failing unified runner invocation**

Run:

```bash
test -x scripts/run-skill-tests
```

Expected: exit code 1

- [ ] **Step 2: Implement unified runner**

Write `scripts/run-skill-tests` as an executable shell wrapper that:

1. runs `scripts/run-skill-lint`
2. runs `scripts/run-skill-smoke`
3. preserves the final aggregate result file
4. exits non-zero if any case fails

- [ ] **Step 3: Document the new test entrypoints**

Add a concise section to `README.md` covering:

- `scripts/run-skill-lint`
- `scripts/run-skill-smoke`
- `scripts/run-skill-tests`
- `PASS / FAIL / SKIP`
- `tests/skills/.last-run.json`
- `tests/skills/.artifacts/`

- [ ] **Step 4: Verify the unified flow**

Run:

```bash
scripts/run-skill-tests
```

Expected:

- lint runs
- smoke runs
- final aggregate JSON exists

- [ ] **Step 5: Commit the unified runner and docs**

```bash
git add scripts/run-skill-tests README.md
git commit -m "feat: add unified skill test runner"
```
