# Reviewer runtime boundary

All report-only reviewers (plan/code Reviewer and quality specialists) use a
fresh, isolated native CLI process. A prose "read-only" request, a
`general-purpose` Agent, and an inherited tool whitelist are not permission
boundaries. Executor, simplifier and test-consolidation authors remain writers.
User authorization and host/admin policy remain authoritative.

## Backend and model selection

Resolve the existing configuration keys before dispatch; this does not add a
model-selection policy:

| Caller / configured path | Launcher | Model |
|---|---|---|
| Claude `reviewer: codex` | `run_codex_reviewer.py` | `reviewer_model`, otherwise Codex default |
| Claude `reviewer: subagent` (compatibility name) | `run_claude_reviewer.py` | `reviewer_model` > `judgment_model` > Claude CLI runtime default |
| Codex default Claude reviewer | `run_claude_reviewer.py` | `reviewer_model` > `judgment_model` > `claude-sonnet-4-6` |
| Codex `codex_reviewer_backend: codex` | `run_codex_reviewer.py` | `codex_reviewer_model`, otherwise Codex default |
| Claude report-only quality specialist | `run_claude_reviewer.py` | existing `judgment` / `cheap` tier resolution; omit `--model` when no override is resolved |
| Codex report-only quality specialist | `run_codex_reviewer.py` | `codex_reviewer_model`, otherwise Codex default |

The `subagent` configuration name is retained and selects an isolated
Claude reviewer rather than a writable in-process general-purpose Agent.
When no Claude model override is resolved, omit `--model` and let Claude CLI
apply its runtime default. Record the native actual model only when the CLI
reports it; never infer it from the requested model.
The local Codex reviewer uses the CLI isolation boundary rather than spawning
the legacy fixed-model `review_loop_reviewer` role. Existing fallback/retry
policy still applies; changing transport does not authorize a new fallback.

## Invocation

Write the self-contained prompt to
`.review-loop/tmp/{invocation_slot}-reviewer-prompt.txt`, where the slot is
unique across concurrent reviewers. Resolve launcher paths against the support
repository and keep cwd in the task workspace:

```
python3 <support-root>/scripts/run_claude_reviewer.py --session-id <slot> --parent-session-id <session-id> --model <resolved-model> --stage <planning|execution|polish> --role <reviewer|specialist-name> --timeout-seconds 570
python3 <support-root>/scripts/run_codex_reviewer.py --session-id <slot> --parent-session-id <session-id> --model <resolved-model> --stage <planning|execution|polish> --role <reviewer|specialist-name> --timeout-seconds 570
```

For the Codex default model, omit `--model`. This means the native CLI built-in
default: the launcher deliberately ignores user config so project and machine
customizations cannot widen its read-only boundary. Custom providers or model
aliases defined only in user config are therefore not inherited; select a model
supported by the clean CLI context or use another configured reviewer backend.
A different positive timeout may be chosen explicitly for a large task; no
unlimited wait. A host command timeout must exceed `--timeout-seconds` by
~15 s of cleanup grace. Never end the turn while a reviewer launcher is running; in
`claude -p`/headless mode a background task is killed when the turn ends. Prefer the
foreground with a host timeout greater than `--timeout-seconds` + 15 s (choose
`--timeout-seconds` so that fits the host cap, e.g. 570 under a 600 s cap). If the
launcher must be backgrounded, poll it within the same turn until it exits. Claude supports
Read/Grep/Glob only, with hooks, plugins, skills and external MCP customizations
disabled. Codex starts in an empty temporary cwd with user config and execpolicy
rules ignored, hooks/plugins/apps disabled, and a read-only shell sandbox. The
prompt explicitly identifies the target repository. Admin-managed machine
policy is a trusted host boundary, not something this plugin can disable. The
launcher also requires host-side `ps` process-table access to verify escaped
descendant cleanup; run it outside a parent tool sandbox that blocks process
inspection. If inspection is unavailable, cleanup fails closed and no review is
accepted.

Do not give report-only agents Bash/Write/Edit/Agent tools. Materialize patches
and run required verification commands in the authorized caller; pass the
artifact paths, command, exit status and relevant output to the reviewer.
Reviewers inspect the evidence and request missing verification. They do not
install tools or run a write-requiring test suite themselves. For quality
agents whose old body asks for Bash, this boundary takes precedence: the caller
runs the named static analysis commands and supplies their results.

The launcher counts root-agent native tool-use events without retaining their
payloads. `tool_uses: 0` is a failed review. The caller retries it once where the active
stage policy allows retries, then reports failure; a missing or `null` count
fails closed without retry. Both launchers reject zero/unverified counts with exit
8 and never publish a result. Missing or `null` counts are evidence gaps and fail closed. For
scheduler jobs the count is also surfaced in each result object.

## Completion, errors and accounting

Poll only bounded heartbeat/status output. On launcher exit 0 AND status `ok`,
require an integer `tool_uses` count, read the returned `result_file`, then apply
the usual output-schema and rubric validation. Never infer approval from a
process result alone. Raw logs and
normalized usage are separate immutable invocation artifacts; a later retry
must not overwrite earlier evidence. Retain `usage_file` in the Timing Log.

| Exit | Meaning | Result usable? |
|---|---|---|
| 0 | clean completion | after schema/rubric validation |
| 1 | command, capture or cleanup failure | no |
| 2 | no valid result / malformed stream | no |
| 3 | missing result | no |
| 4 | total timeout | no |
| 5 | cancelled | no |
| 6 | rate or subscription limit | no |
| 7 | required Claude isolation flags unavailable | no |
| 8 | no verifiable root-agent tool use | no |

Keep raw diagnostic artifacts on failure and report the category; never read a
previous round's compatibility result file as a fresh result. No automatic
permission widening. Resuming after a limit is a new invocation with a new id.
Usage can be partial or unknown on interruption and must be labeled accordingly.
Use `reviewer_usage.py aggregate` for totals; do not add cumulative resume
snapshots or overlapping `modelUsage` and `usage` counters manually. See
[usage-accounting.md](usage-accounting.md) for the accounting contract.

Parallel dispatch uses the same launchers and permission boundary. Arbitrary
extra CLI arguments cannot override isolation, model or result paths.

## Cross-vendor review

Author vendor is the runtime that wrote the change (Claude runtime: claude; Codex runtime: codex). Reviewer vendor is
the launcher actually used, fallbacks included (`run_codex_reviewer.py`: codex; `run_claude_reviewer.py`: claude).
Equal is same-vendor: Claude `reviewer: subagent` and Codex `codex_reviewer_backend: codex`.

Config key `cross_vendor_review`: `auto` (default) or `off`. Any other value fails closed: checked at the run's
first reviewer dispatch, it stops the run with a config error naming the key. No pass runs and nothing is delivered.

Each execution convergence needs one visible `cross-vendor review:` record in the session file and the delivery summary.
One line per pass in `## Review History`, followed by the list of its blocking findings:
`cross-vendor review: <verdict|unavailable|invalid|off> (R<n>, <reviewer vendor/model>, <convergence id or tree fingerprint>)`.
The latest record for the current convergence governs. A convergence ends only when a fix round actually changed files;
revoking `exec` or starting a replay alone never invalidates a blocking record, so abort/resume cannot rerun the pass.
Severities are the shared schema's (reviewer-output.md): a blocking finding is `[CRITICAL]`, `[MINOR]` is advisory.

- Final execution review already cross-vendor: `cross-vendor review: not needed (cross-vendor final review)`.
- Same-vendor and `off`: `cross-vendor review: off (config)`.
- Same-vendor and `auto`: run ONE extra report-only review with the other vendor's launcher, after the last
  execution-round APPROVE and after Step 3.4 if it runs, before Step 3.5 and any `--stop-after before-polish` exit.
  Same launcher contract, isolation, schema and triage gates; `--stage execution --role cross-vendor-reviewer`.
  Record `cross-vendor review: <VERDICT> (...)`.
- Model is the other backend's own key: codex uses `reviewer_model` only when it names a Codex model, else
  `codex_reviewer_model`, else the Codex default; claude uses `reviewer_model` > `judgment_model` > the Claude
  runtime default (`claude-sonnet-4-6` on the Codex runtime, Claude CLI default on the Claude runtime).
- At most once per convergence: the pass never reruns for the same convergence. It reruns only after a reopened
  round that changed files, and that round starts a new convergence.
- A `[CRITICAL]` blocks delivery and reopens a normal execution fix round, which counts toward `soft_limit_exec`.
  It also revokes any already-minted `exec`: treat it as REQUEST_CHANGES at reviewer-only fast-replay
  (session-file.md §Reviewer-only fast-replay, outcome 4: clear `completed_stages`, replay from `exec`).
  On resume, re-derive the block from the latest record and its findings, not from `delivery_blocked_by`.
  `[MINOR]` findings are recorded as advisories.
- `unavailable` only when the CLI is not installed or the launcher failed before producing any output
  (`no-cli`, `launcher-failed`): delivery is not blocked; record `cross-vendor review: unavailable (<reason>)`.
  This is a review result, not an optional integration.
- `invalid`: a result that fails schema, rubric or `tool_uses: 0` gets the existing retry of its launcher
  (runtime-codex.md §Reviewer dispatch: the Claude launcher gets no schema retry; the Codex launcher one correction
  retry; `tool_uses: 0` one retry per Invocation above). If still invalid, delivery is BLOCKED with
  `cross-vendor review: invalid (<reason>)` and the orchestrator reports it to the user. Never `unavailable`.
- Before `delivery_gate.py`, check the latest record for the current convergence. (a) None: load `execution-review`
  and dispatch the first pass. (b) Unresolved `[CRITICAL]`: do NOT rerun the pass; reopen a normal execution fix round.
  (c) `invalid`: stay blocked and report. Then retry the gate.
