# Paired-session coordinator

This is the tracked implementation of the paired-session workflow. Its stable
repository-local entry point is `bin/paired-session`. The current rollout is
still staged: the legacy `/review-loop` entry remains the default until the
paired-session command, recovery path, and migration guide have been reviewed.

The coordinator runs one author and reviewer through PLAN/EXEC, then applies
fresh shadow/adversarial checks and a delivery sequence. It owns isolated run
artifacts, workspace snapshots, invocation limits, and a permission preflight.
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
them requires a fresh probe. The Codex author probe tests that workspace writes
succeed, run-owned `$TMPDIR` writes succeed, and external temporary paths plus `/tmp` writes fail. Claude roles use one inline strict sandbox settings object, deny secret-like environment variables and common credential files, block network access and local network binding, and require the exact Bash allowlist. The reviewer probe verifies OS-level write denial for both host `/tmp` and the run directory, including its root. Claude's session `TMPDIR` is shared scratch per UID on macOS; paired-session keeps workflow state outside it and denies writes to the run directory. Bash sandbox results do not establish direct `Edit` or `Write` safety for a Claude author. Do not use `--skip-probe` for a
real task; it exists for deterministic tests only.

If a run is held, inspect `state.json`, `open-findings.md`,
`findings-ledger.md`, and the latest receipts under `evidence/` before resuming.
A rate-limit HOLD includes a reset hint when the provider supplies one. Resume
with the same workspace, work item, run directory, author/reviewer settings,
and test command:

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
