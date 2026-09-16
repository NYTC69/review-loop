# Codex runtime contracts

## Runtime Identity

- Codex is the orchestrator.
- Codex Stage 1 assumes a single orchestrator-owned workspace for the session.
- The orchestrator is the only writer of `.review-loop/sessions/{uuid}.md`.
- `review_loop_executor` never writes the session file directly.
- `review_loop_reviewer` never writes the session file directly.
- Do not create or switch to another git worktree or repository checkout.
- When invoking Codex subagents, use a fresh self-contained prompt that embeds
  the required task context directly. Do not rely on inherited or forked parent
  thread context.
- Reject malformed Executor or Reviewer output instead of guessing.
- If the user explicitly resumes an existing Stage 1 session, reopen that file.
  Otherwise create a new session with a new UUID.
- On explicit resume, read the existing session file and continue from its
  `## Current Phase` and existing unresolved state.
- On explicit resume, do not reset the session to a fresh planning run and do
  not overwrite accumulated `## Review History` as if the session were new.

## Completed Agent Cleanup

- Track every Codex subagent id spawned for `review_loop_executor` and
  `review_loop_reviewer` during the session.
- Before every new `spawn_agent` call, call `close_agent` on any completed Codex subagent id from earlier planning, execution, or local-reviewer rounds unless the orchestrator explicitly intends to reuse that exact id.
- Do not close a subagent until its output has been captured, validated or
  rejected, and any information needed for the session-file update, retry
  decision, or user-facing failure report has been copied into
  orchestrator-owned state.
- After each planning round, execution round, or local Codex reviewer retry
  finishes, close the completed Executor and local Reviewer subagents for that
  round before spawning the next agent or moving to the next phase.
- The Claude CLI reviewer path is a child process, not a Codex subagent, so completed-agent cleanup does not apply to it. Continue deleting its temporary
  prompt file immediately after the command returns.
- If cleanup closes one or more obsolete completed agents, log a short live
  update naming the cleanup count. Do not add cleanup details to the session
  file unless they affect the round result.

## Umbrella Completion

- For the umbrella `review-loop` entry point, do not deliver, summarize success, or stop after the execution loop mints only `exec`; continue through Quality Polish, Documentation Consistency, Security Preflight, and delivery unless an explicit `--stop-after` value says otherwise.
- A reviewed no-op execution round is still only the `exec` stage. It is not
  a terminal success state for the umbrella command unless the caller asked
  for `--stop-after exec-round` or `--stop-after before-polish`.
- Before the final user-facing delivery summary, verify that Codex Stage 1 has
  `{exec, polish, docs, security} ⊆ completed_stages`. If any downstream
  stage is missing, continue running the missing stage or set
  `delivery_blocked_by` instead of reporting completion.

## Config Loading

- Read `.review-loop/config.md` if present. If it is absent, use Stage 1
  defaults.
- Consume shared keys conservatively: `reviewer_model`, `judgment_model`,
  `cheap_model`, `soft_limit_plan`, `soft_limit_exec`, `handsfree`,
  `review_focus`, `quality_focus`, `review_style`, and `skip_quality_polish`.
- Do not use the shared `reviewer` key to choose the reviewer backend in Codex.
  In Codex Stage 1, reviewer selection is controlled only by the runtime
  default and optional Codex-only keys.
- Default reviewer behavior in Codex Stage 1:
  - keep review on the outside-sandbox Claude CLI reviewer path
  - do not auto-fall back to the local Codex reviewer
- If `codex_reviewer_backend: codex` is present, skip the Claude path and use
  the local Codex reviewer directly.
- `reviewer_model` applies only to the Claude CLI reviewer path.
- `judgment_model` is the shared-tier fallback for that Claude CLI reviewer
  path before the explicit `claude-sonnet-4-6` backstop.
- `cheap_model` is accepted in shared config but is a documented no-op in
  Codex Stage 1 because Stage 1 currently ships no cheap-tier Codex agents.
- `quality_focus` applies only when Step 3.5 Quality Polish actually runs.
- `skip_quality_polish: true` mints `polish` as a no-op completion and still continues through docs and security.
- `codex_reviewer_model` applies only to the local Codex reviewer path.
- `executor_model` is ignored by the Codex runtime in Stage 1.
- `codex_executor_model` is reserved only and ignored in Stage 1.
- Local Codex Stage 1 agents are all `judgment` tier. If a tier is omitted,
  treat it as `judgment`.
- Do not introduce new required config keys in Stage 1.

## Reviewer dispatch

### Default Reviewer Path

Unless `codex_reviewer_backend: codex` is set, use this default reviewer path:

```bash
claude -p --no-session-persistence --output-format stream-json --include-partial-messages --model {reviewer_model if set; else judgment_model if set; else claude-sonnet-4-6} < .review-loop/tmp/{session_id}-reviewer-prompt.txt
```

Rules:

- Run the Claude call outside the sandbox.
- Do not treat a sandboxed `claude -p` rehearsal as representative of this
  reviewer path. If the command fails inside the sandbox, rerun the same
  command outside before declaring the Claude reviewer path unhealthy or
  switching to fallback.
- Render the full reviewer prompt into
  `.review-loop/tmp/{session_id}-reviewer-prompt.txt`.
- Read stdout line by line. Each line is a JSON event object. Find the line
  where `type == "result"` and use its `result` field as the reviewer output.
  Intermediate events (thinking deltas, assistant blocks, rate limit events)
  are heartbeat signals confirming the process is alive — log them if helpful
  but do not treat them as output. If no `type == "result"` line appears
  before the process exits, treat that as a command execution failure.
- Validate the `result` field against the shared reviewer schema.
- Then run `python3 scripts/finding_triage.py check --input <result file>`
  (mandatory rubric gate per `docs/protocol/execution.md` §Mandatory rubric
  gate: every `[CRITICAL]` carries the six fields `Trigger:`,
  `Reachability:`, `Impact:`, `Likelihood:`, `Fix cost:`, `Cheaper
  response:`). An `incomplete` result is a reviewer schema validation
  failure for this round: discard the output as malformed, record
  `rubric_incomplete: finding #n missing <fields>` in `## Review History`,
  do not retry Claude for that round, and never implement a CRITICAL that
  failed triage.
- If Claude invocation fails or validation fails, do not guess and do not retry
  Claude for that round.
- If Claude invocation fails or validation fails, record a short failure reason
  summary in `## Review History`. Include whether the
  failure was command execution, JSON parsing, missing `result`, or reviewer
  schema validation.
- If `codex_reviewer_backend: codex` is not set, surface that Claude-path
  failure to the user instead of auto-falling back. The default Stage 1
  reviewer separation policy keeps review on the outside-sandbox Claude path
  unless the user explicitly opts into the local Codex reviewer.

### Optional Local Reviewer Path

- Spawn `review_loop_reviewer` only if `codex_reviewer_backend: codex` is set,
  or if the user has otherwise explicitly opted into the local Codex reviewer
  path.
- Invoke `review_loop_reviewer` with a fresh, self-contained prompt that
  embeds the exact review content directly. Do not rely on inherited or forked
  parent thread context.
- Use the same review content and the same reviewer schema rules as the Claude
  path.
- Validate local reviewer output with the same schema rules.
- If the local reviewer output is invalid, retry once with explicit
  correction instructions.
- If the local reviewer retry is still invalid, stop and surface the failure to the
  user.
- The local reviewer output also goes through `python3
  scripts/finding_triage.py check`; `incomplete` counts as invalid output
  for the one correction retry above, and a CRITICAL that failed triage is
  never implemented.

## Codex Hallucination Guard

### Executor Guard

Treat Executor output as invalid and reject it if any of these are true:

- the required section structure is missing
- it claims file changes without concrete repository file paths
- it claims implementation changes that are not reflected in the current-round
  delta attributable to that round
- it reports or implies work performed in a different git worktree or
  repository checkout than the orchestrator-owned current workspace
- it cannot explain deviations from the approved plan when deviations exist

Use this changed file set definition:

- tracked changes: `git diff --name-only HEAD`
- untracked files: `git ls-files --others --exclude-standard`
- actual post-Executor changed file set: the union of those two lists after the
  Executor returns
- deleted tracked files remain part of the tracked-changes source of truth

Execution guard flow:

1. Record the pre-Executor changed file set.
2. Run the Executor.
3. Collect the post-Executor changed file set.
4. Derive the current-round delta from the relevant pre-round and post-round
   state for files touched in that round.
5. Compare the Executor's claimed file list against that current-round delta.
6. Reject outputs that claim file changes not supported by that current-round
   delta, even if the file is still dirty after the round.

The post-Executor set is the source of truth. The pre-Executor set is useful
for file-presence validation and current-round delta derivation, but unchanged
path sets alone do not prove a no-op. Treat a run as no-op only when the
Executor explicitly reports it and there is no meaningful delta attributable to
the current round.

### Reviewer Guard

Treat reviewer output as invalid and reject it if any of these are true:

- `### VERDICT` is missing
- the verdict is not exactly `APPROVE` or `REQUEST_CHANGES`
- `### Strengths` is missing
- any issue uses a severity other than `[CRITICAL]` or `[MINOR]`
- `REQUEST_CHANGES` appears with no `### Issues`
- `APPROVE` appears with any `[CRITICAL]` issue
- `REQUEST_CHANGES` appears with only `[MINOR]` issues
- the output is too malformed to recover issue entries safely
- a code-review response makes claims that should reasonably have concrete file
  or location anchors, but fails to provide them
- it fails to flag workspace divergence when implementation appears to exist
  only in a different git worktree or repository path than the current
  workspace

For plan review, file references are optional, but issues must still point to
concrete plan gaps.

For code review, findings should map to specific files and locations whenever
applicable. Reject code-review findings without concrete anchors only when the
finding should reasonably be able to point to specific files or locations.

## Orchestrator Discipline

- Keep the user informed of each round's status and review findings.
- Never plan yourself; implement directly only under
  `docs/protocol/execution.md` §Author route selection (when
  `evidence_ledger.py route` returns `orchestrator-direct`); otherwise
  delegate to `review_loop_executor`.
- Write the session file yourself; do not delegate session-file writes.
  `## Evidence Ledger` and `completed_stages` are written only through
  `scripts/evidence_ledger.py`.
- Do not invent changed files, reviewer verdicts, plan details, or fixes to
  keep the loop moving. Never reinterpret "passed earlier" prose as a
  reusable PASS; a legacy session without a ledger fails closed (empty
  stages, snap/0 from the current verified worktree, affected checks rerun).
- If Executor or Reviewer output is malformed, reject it and use the retry or
  fallback path defined above.
- Stay within the approved Stage 1 contract and shared `.review-loop` protocol.
