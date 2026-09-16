# Codex parallel reviewer dispatch

### Parallel Reviewer Fan-Out (N>1)

When the orchestrator decides to dispatch N>1 independent reviewer rounds in
the same wall-clock window (for example a polish-stage parallel sweep),
shell out once to the conflict-aware parallel scheduler in
`scripts/review_verification.py` instead of looping the single-shot path
serially. N=1 dispatch keeps the single-shot invocation above
byte-identical — argv, stdin handoff, model resolution, and temp-file
lifecycle are unchanged.

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
- `reviewer_model` — resolved via the same shared model-tier rule used by
  the single-shot path: `reviewer_model if set; else judgment_model if
  set; else claude-sonnet-4-6` (per `docs/protocol/planning.md` §Shared
  model-tier contract)
- `timeout_secs` (optional, default `300.0`)
- `conflict_keys`, `capacity_keys`, `extra_argv`, `worktree` (optional;
  omit unless overriding scheduler defaults)

Inline `prompt_text` directly in the JSON object — do not write per-job
prompt files yourself; the scheduler renders each job's `prompt_text` to
`.review-loop/tmp/{session_id}-reviewer-prompt.{job_id}.txt` internally
and hands the FD to the spawned `claude -p` via stdin redirection (per
`scripts/review_verification.py:457-459` Scheduler docstring and
`:648-651` `_run_one`). For `runtime: "codex"` jobs (the Codex Stage 1
fan-out path documented in this section), per-job stdout is captured by
the scheduler via `subprocess.PIPE` and surfaced through each
`<results.json>` entry's `stdout` field — there is no per-job output
file. For `runtime: "claude_code"` jobs (the Claude-Code orchestrator's
`codex exec -o` fan-out, not used here), per-job stdout is written to
`.review-loop/tmp/{session_id}-reviewer-output.{job_id}.txt`.

Invoke the scheduler outside the sandbox:

`python3 scripts/review_verification.py --jobs .review-loop/tmp/{session_id}-jobs.json --output .review-loop/tmp/{session_id}-results.json`

`<results.json>` is a JSON list of objects, one per job, each carrying
`job_id`, `returncode`, `stdout`, `stderr`, `timed_out`, `parsed_verdict`,
`parsed_issues`, and `error`. For every entry:

- If the entry's `error` field is non-null, or `timed_out` is true, or
  `returncode` is non-zero, classify as a **command-execution failure**
  for the round's failure-mode taxonomy and record `error`, the last
  4 KB of `stderr`, `timed_out`, and `returncode` in `## Review History`.
  Do not attempt to parse `stdout` for that entry — the per-entry
  diagnostic fields take precedence over stream-json parse outcome.
- Treat the per-entry `stdout` field as the same stream-json byte stream
  the single-shot path reads from `claude -p`. Find the line where
  `type == "result"` and use its `result` field as the reviewer output.
- Validate that `result` against the shared reviewer schema in
  `docs/protocol/reviewer-output.md`. The orchestrator remains the single
  authority for verdict extraction and schema validation; the scheduler's
  own `parsed_verdict` / `parsed_issues` are best-effort metadata only
  per `scripts/review_verification.py:12-17` and must not be substituted
  for orchestrator-side validation.
- Then run `python3 scripts/finding_triage.py check --input <result file>`
  on every validated `result` (mandatory rubric gate); an `incomplete`
  result is a reviewer schema validation failure for that entry, discarded
  as malformed and recorded as `rubric_incomplete: finding #n missing
  <fields>`.
- Apply the same per-round failure-mode taxonomy as the single-shot path
  (command execution / JSON parsing / missing `result` / reviewer schema
  validation) when recording `## Review History`.

After the round completes (success or failure), delete every per-job
prompt file `.review-loop/tmp/{session_id}-reviewer-prompt.{job_id}.txt`,
every `runtime: "claude_code"` per-job output file
`.review-loop/tmp/{session_id}-reviewer-output.{job_id}.txt` (absent for
the `runtime: "codex"` path used in this section), and the
`.review-loop/tmp/{session_id}-jobs.json` /
`.review-loop/tmp/{session_id}-results.json` artifacts, matching the
single-shot prompt-cleanup discipline.

Per-job prompt files are scheduler-owned and may already be unlinked
when the orchestrator's cleanup runs (the scheduler unlinks them in its
own `finally:` per `scripts/review_verification.py:646`); treat ENOENT
as success and do not surface it. The `<jobs.json>` / `<results.json>`
artifacts are orchestrator-owned — a non-ENOENT failure to delete them
should be logged as a warning in `## Review History` but must not block
the round verdict.
