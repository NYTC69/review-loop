# Paired-session coordinator

This is the tracked implementation of the paired-session workflow. Its stable
repository-local entry point is `bin/paired-session`. The current rollout is
still staged: the legacy `/review-loop` entry remains the default until the
paired-session command, recovery path, and migration guide have been reviewed.

The coordinator runs one author and reviewer through PLAN/EXEC, then applies
fresh shadow/adversarial checks and a delivery sequence. It owns isolated run
artifacts, workspace snapshots, invocation limits, and a permission preflight.
Reviewer receipts record the requested model beside the provider-reported
identity and classify it as `MATCH`, `MISMATCH`, or `UNREPORTED`. A match requires
the exact requested ID or that ID with an explicit date suffix; synthetic,
malformed, or missing identity data is unreported, and Claude subagent models
are ignored.
The preflight must pass before either a fresh run or a resumed run can invoke an
author. The Codex author permission probe uses a disposable Git workspace and
checks that workspace and run-owned `$TMPDIR` writes succeed while external
temporary paths and `/tmp` writes fail.
One OS-backed lease serializes commands that mutate a run directory. A second
worktree lease prevents `run`, `resume`, or `permission-probe` commands using
different run directories from coordinating on the same workspace at once. Its
key is the resolved workspace directory's `(st_dev, st_ino)` identity, so
case-variant or symlink paths to the same directory share a lock.

The workspace lock lives outside the product workspace in
`/tmp/paired-session-workspace-leases-<uid>/<sha256-of-device-and-inode>.lock`.
If that location would be inside the workspace, the coordinator tries `/var/tmp`
and then the configured system temporary directory. It uses a private `0700`
per-user directory and fails closed if it cannot create or validate the lock.
A contention HOLD reports the holder PID and `run_dir`. The OS releases the
`flock` when the process exits; the small lock file remains for reuse. Do not
remove a lock file or its directory while a coordinator may hold it. Normal
operation needs no manual cleanup; remove stale files only after confirming no
coordinator can still hold the corresponding lease.

`abort` takes only its own run-directory lease. It marks that run HOLD without
modifying the product workspace, so an operator can abort a stale run while a
different run holds the workspace lease. Fresh-shadow CRITICALs enter the
findings ledger and follow the author repair path even when the persistent
reviewer approves.
Explicit subscription/rate-limit rejections are held with any reported reset
hint and do not consume the invocation budget; their failed attempts remain in
the turn receipt.

## MINOR-only review revisions

A persistent reviewer REVISE containing only MINOR/LOW findings is accepted as
`APPROVE_WITH_ADVISORY` at the final PLAN/EXEC round, or in POLISH. Its raw
REVISE and effective workflow verdict are recorded separately. Those findings
remain open and are labeled advisory in the findings ledger and report until a
later reviewer confirms they are fixed; the next phase receives them as
non-blocking context. Non-final PLAN/EXEC REVISE continues its normal repair
loop. CRITICAL, MAJOR, SECURITY-flagged, and observed test failures are never
downgraded; EXEC and POLISH require a successful observed configured test.
Fresh shadow/gate blockers retain their existing blocking behavior. Review
comparison shows each reviewer decision separately from the final coordinator
status, which may still be HOLD after later checks.

## Local marketplace installs

Installing a plugin from a local directory marketplace copies the whole directory,
including Git-ignored files, into the host plugin cache. That can include
`.compass/`, `.claude/`, `HANDOFF.md`, and local caches. Install from a clean
checkout or the GitHub marketplace to avoid copying working-tree state into the
plugin cache.

## Start a task

Use a dedicated Git worktree for each product task and keep run artifacts in a
sibling directory outside the workspace. Never put `--run-dir` inside the
workspace: the author can write to the workspace and must not be able to alter
coordinator state. The workspace must be a Git worktree and the work item must
be a file. The configured test command is checked before the coordinator starts.

For per-project settings, copy `paired-session-config.example.json` to
`.review-loop/paired-session.json` in the workspace. The CLI loads that profile
for run, probe, and resume; explicit CLI options override profile values. On
resume, the effective settings must still match the saved run configuration.
Models follow ADR-5 vendor pins (Claude: `claude-opus-5-5`; Codex:
`gpt-6-luna`). Changing a role's vendor without updating incompatible model
values is rejected before run state is created.

```sh
bin/paired-session run \
  --workspace /path/to/disposable-worktree \
  --workitem /path/to/WORKITEM.md \
  --run-dir /path/to/worktree-run-id \
  --test-command 'python3 -m unittest'
```

Before `run` or `resume`, perform the author permission probe with the same
workspace, work item, run directory, author vendor/binary, profile, and one-run
overrides:

```sh
bin/paired-session permission-probe \
  --workspace /path/to/disposable-worktree \
  --workitem /path/to/WORKITEM.md \
  --run-dir /path/to/worktree-run-id \
  --test-command 'python3 -m unittest'
```

The probe is bound to the author binary and relevant configuration; changing
them requires a fresh probe. For a Codex author, the coordinator runs external
temporary-path and `/tmp` escape-denial writes directly through `codex sandbox`
with the same `workspace-write`, writable-root, temp-exclusion, and `TMPDIR`
overrides used for the author. The model-driven author probe checks only that
workspace and run-owned `$TMPDIR` writes succeed. It also asks that same author
turn to attempt one write each under an external temp directory, `/tmp`
(`/private/tmp` on macOS), `$HOME`, and the workspace parent. Each target uses
a fresh sentinel filename, never a config file. The coordinator checks each
target on disk before cleaning up its own sentinel: an existing target is
`FAIL` regardless of the model's report; an absent target with an observed
OS-denied attempt is `PASS`; an absent target without a usable attempt report
is `UNKNOWN`. `permission-probe.json` records per-target evidence and cleanup.
For a single clearer retry after `UNKNOWN`, set `PAIRED_SESSION_PROBE_CLARIFY=1`
on the permission-probe command; this changes only the prompt, not the sandbox.

Claude roles use one inline strict sandbox settings object, deny secret-like
environment variables and common credential files, block network access and
local network binding, and require the exact Bash allowlist. The reviewer
permission probe gives exactly one unique run-directory `touch` command a narrow
CLI allowlist entry, then requires its failure to carry OS-sandbox denial
evidence. A CLI permission-layer denial alone does not prove the OS sandbox.
Other probe writes remain outside that allowlist. Claude's session `TMPDIR` is
shared scratch per UID on macOS; paired-session keeps workflow state outside it
and denies writes to the run directory. Bash sandbox results do not establish
direct `Edit` or `Write` safety for a Claude author.
Read-only Claude roles receive the read tools as one rule and each exact
argument-bearing Bash command as its own `--allowedTools` argument.

The probe records hashes for the specified Codex and Claude global files.
Codex's new-workspace trusted-project entry and Claude plugin `lastUpdated`
updates are attributed in the report; any other global-file change fails the
probe. Do not use `--skip-probe` for a real task; it exists for deterministic
tests only.

If a run is held, inspect `state.json`, `open-findings.md`,
`findings-ledger.md`, and the latest receipts under `evidence/` before resuming.
A rate-limit HOLD includes a reset hint when the provider supplies one. Resume
with the same workspace, work item, run directory, author/reviewer settings,
and test command:

`--timeout` (default 2700 seconds, with no upper bound) governs every
non-EXEC-author turn, including PLAN, POLISH, persistent/fresh reviewer,
shadow and gate turns. EXEC author turns use the separate `--exec-turn-timeout`; when omitted,
it defaults to `max(7200, --timeout)` capped at 14400 seconds. On resume, only
an explicit CLI `--exec-turn-timeout` may raise the saved EXEC timeout, never
lower it; project-config defaults do not count as an explicit raise. The value
is saved for later turns and resumes. An already-running turn keeps the timeout
it received when it started. When resuming legacy run state without this setting, the coordinator derives it
from `max(7200, saved --timeout)`, capped at 14400 seconds. The existing
`resume --resume-timeout N` option raises the general per-turn timeout up to
7200 seconds for phases that use `--timeout`.

A run ending in `DONE` is awaiting explicit operator acceptance. Use `accept` to
record acceptance and move it to terminal `ACCEPTED`; repeating `accept` is a
no-op. Use `reject --text` or `reject --file` on a `DONE` run to send in-scope
feedback to one more EXEC author turn. That turn goes through the configured
review again and forces a gate review. Rejections are saved and limited to two
by default; exhausting the limit puts the run on `HOLD`, which can still be
explicitly accepted without another provider run.

```sh
bin/paired-session accept --workspace /path/to/worktree \
  --workitem /path/to/WORKITEM.md --run-dir /path/to/worktree-run-id
bin/paired-session reject --workspace /path/to/worktree \
  --workitem /path/to/WORKITEM.md --run-dir /path/to/worktree-run-id \
  --text 'Please address this in-scope acceptance feedback'
```

An idle `HOLD` run waiting for its next author turn accepts an in-scope
clarification with `note --text '...'` or `note --file /path/to/note`. It reaches
that author turn on `resume`; a newer note replaces a pending one. Notes are
refused while waiting for a reviewer/gate, after the rejection cap, and on a
DONE run (use `reject`). The note cannot authorize new scope. For a scope
change, use `note --scope-change --text/--file` on an idle active or ordinary
HOLD run, or `reject --scope-change` before accepting a DONE run (also allowed
after the rejection limit). The old run ends as `ABORTED(scope-change)` and
prints exact `Probe:` and `Start:` commands for one successor. Run both commands
in order. The successor starts a fresh PLAN, review, and configured gate, with
the old workspace edits still present for the PLAN author to keep or revert.
The old run cannot resume or be accepted; a second chained scope change is
refused. A scope-change note requires the named existing run. Author-produced
plan/code remains visible to reviewers, while the operator note itself is not
forwarded to their prompts.

Per-request provider usage is copied into the active turn receipt as stream
events arrive. If a provider turn fails, times out, or is killed after reporting
usage, the known request totals remain in the usage report; an interrupted
coordinator also retains observed usage when its uncertain turn is archived.

If `state.json` has `uncertain_active`, do not re-probe or replay automatically.
Inspect the recorded PID and receipts. A stopped probe child can be retried with
`permission-probe --retry-uncertain` after operator approval; a product-work
turn requires the same explicit approval with `resume --retry-uncertain` because
the model call may be replayed. After recovering a probe in an existing run
directory, continue with `resume`; do not call `run` again.

```sh
bin/paired-session resume \
  --workspace /path/to/disposable-worktree \
  --workitem /path/to/WORKITEM.md \
  --run-dir /path/to/worktree-run-id \
  --test-command 'python3 -m unittest'
```

`bin/paired-session --help` lists all supported options. `snapshot` is read-only.
Each run directory belongs to one task and must not be shared between tasks.

The workflow still needs the remaining productization and protocol batches
listed in the repository backlog. The legacy implementation remains available
as a comparison path during staged migration.

`test_real_coordinator.py` is a deterministic fake-CLI suite. It verifies
protocol transitions and permissions-command construction; the runtime
permission probe is the effective check against the installed Codex CLI.
