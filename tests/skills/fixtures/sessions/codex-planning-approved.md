## Problem Description
Add stable fixtures for the review-loop skill testing framework.

## Context
- This fixture is synthetic and intentionally compact.
- It mirrors the session shape used by the review-loop lint and smoke runners.
- This fixture models a fallback-approved session artifact.
- Stability matters more than reproducing noisy runtime logs.

## Acceptance Criteria
- The session includes the required sections in the required order.
- The approved plan and reviewer outcome are present.
- `## Session Metadata` is the final section.

## Current Phase
planning

## Approved Plan
### Solution Plan: Add stable fixtures
1. Create a compact session fixture with the required section order.
2. Create a stable planning-review prompt fixture with the shared session path and reviewer schema.
3. Create a machine-readable reviewer schema expectation file.

## Review History
### Planning Round 1
- Executor backend: `codex-subagent`
- Executor result: proposed a minimal stable fixture set.
- Reviewer backend: `codex` (fallback reviewer)
- Reviewer fallback used: `true`
- Reviewer verdict: `APPROVE`
- Reviewer issues:
  - None.
- Outcome: Approved for fixture creation.

## Files Changed
None.

## Key Related Files
- `tests/skills/contracts/shared-schema.json`
- `tests/skills/contracts/review-loop.json`
- `.agents/skills/review-loop/SKILL.md`

## Timing Log
- t0 - Session fixture initialized.
- t1 - Planning round approved for the stable fixture set.

## Session Metadata
- session_origin: synthetic-fixture
- orchestrator_backend: codex
- executor_backend: codex-subagent
- reviewer_backend: codex
- reviewer_fallback_used: true
