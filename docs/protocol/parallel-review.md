# Codex parallel reviewer dispatch

### Parallel Reviewer Fan-Out (N>1)

When the orchestrator decides to dispatch N>1 independent reviewer rounds in
the same wall-clock window (for example a polish-stage parallel sweep),
shell out once to the conflict-aware parallel scheduler in
`scripts/review_verification.py` instead of looping the single-shot path
serially. N=1 uses the same native launcher as parallel jobs; permissions,
timeouts and usage semantics are shared through `reviewer-runtime.md`.

Build `<jobs.json>` as a JSON list of objects with one entry per reviewer
round, matching the schema accepted by `_load_jobs` in
`scripts/review_verification.py`:

- `session_id` (required) — current session uuid
- `job_id` (required) — orchestrator-stable identifier unique within the
  round; used as the per-job prompt-file discriminator
- `runtime` (optional, default `"codex"`) — leave at `"codex"` for the
  Codex Stage 1 `claude -p` shell-out path
- `prompt_text` (required for non-empty dispatch) — the full reviewer
  prompt body, identical to what would be rendered into
  `.review-loop/tmp/{session_id}-reviewer-prompt.txt` in the single-shot
  path
- `reviewer_model` — for `runtime: "codex"`, resolved via the same shared
  model-tier rule used by the single-shot path: `reviewer_model if set; else judgment_model if
  set; else claude-sonnet-4-6` (per `docs/protocol/planning.md` §Shared
  model-tier contract). For `runtime: "claude_code"`, use `reviewer_model`
  only: a Codex model, or empty to omit `-m`. Never apply `judgment_model`
  or the Claude-tier fallback to these Codex CLI jobs.
- `timeout_secs` (optional, default `300.0`)
- `stage`, `role` (optional, default `review` and `reviewer`) — record the
  actual stage and specialist name so cost is attributable.
- `conflict_keys`, `capacity_keys`, `worktree` (optional; omit unless
  overriding scheduler defaults)
- `extra_argv` must be absent or empty: arbitrary native flags could defeat the
  read-only permission boundary and are rejected.

Inline `prompt_text` in each job. The scheduler creates a unique job directory
and a wrapper-compatible prompt slot, then invokes `run_claude_reviewer.py`
(`runtime: codex`) or `run_codex_reviewer.py` (`runtime: claude_code`). The
historical runtime names identify the caller, not the child backend.

Invoke the scheduler outside the parent sandbox:

`python3 scripts/review_verification.py --jobs .review-loop/tmp/{session_id}-jobs.json --output .review-loop/tmp/{session_id}-results.json`

Results contain `job_id`, `returncode`, `stdout`, `stderr`, `timed_out`,
`parsed_verdict`, `parsed_issues`, `error`, `status`, `result_file`, `usage_file`,
`tool_uses` and `invocation_id`. `stdout` is a bounded wrapper-status tail, never the raw
model stream. If `error` is non-null, `timed_out` is true, `returncode` is nonzero
or `status` is not `ok`, record the actual failure and do not accept a verdict.
Every parallel reviewer requires a positive integer `tool_uses`; missing, `null`,
or zero is a failed job and cannot pass `--fail-on-any`. The caller may retry a
zero-tool job once where its stage policy allows it; quality, docs and security
roles require exactly one such retry before reporting failure.

A wrapper whose descendant cleanup failed or whose process tree could not be
inspected reports `error: reviewer_cleanup_failed`, distinct from
`reviewer_timeout`. The scheduler's schema check sets `error` only for
`role: reviewer`; a specialist's raw report keeps best-effort parse metadata.

Only after a clean invocation, read `result_file`; validate the shared schema
and run `python3 scripts/finding_triage.py check --input <result file>`.
`parsed_verdict` / `parsed_issues` are convenient metadata, not a replacement
for the orchestrator's schema/rubric gate. Incomplete rubric fields invalidate
that review and never authorize a code change.

The outer scheduler allows bounded cleanup grace beyond the child's deadline,
so timeouts/cancellation can publish failure usage. It forwards cancellation to
wrappers and cleans remaining descendants. A late output cannot change a job's
finished result. Parallel capacity/conflict controls still apply.

Keep immutable invocation raw logs, prompts and usage records for audit. The
scheduler retains them in a unique job directory for offline verification. Keep
them in the ignored artifact area until the session is closed; do not delete
another invocation's files or any unrelated session artifact.
