# Detached coordinator commands (`--detach`, `stop`)

FIELD-17 follow-up (lane B, 2026-10-05). A host session that ends (a headless `claude -p` turn, a Codex turn, Ctrl-C of the
host, an SSH drop) must not kill a paired-session run in progress. Default behaviour is unchanged; `--detach` is opt-in.

## How hosts end a running command (macOS, 2026-10-05)
Experiment: a harmless recorder (`python3` sleeping 45 s, logging its ids, every catchable signal and a heartbeat) started by
the host, which then ended its turn. The recorder also forked a `setsid()` + double-forked grandchild that logged the same.
- **Claude Code `claude -p`** (background Bash task, `run_in_background`): the Bash task runs in the shell's session and
  process group (pgid = sid = the shell). At turn end the host sent **SIGTERM** to that group, nothing else within 45 s; a
  process with the default handler dies. The detached grandchild got no signal and ran to its end.
- **Codex `codex exec`** (codex-cli 0.160.0, `exec_command`): the command runs as its own session leader (pgid = sid = pid).
  At turn end it was **SIGKILLed** (no catchable signal, heartbeat stopped at 3 s). The detached grandchild ran to its end.
- An interactive Ctrl-C sends SIGINT to the terminal's foreground group, and an SSH drop sends SIGHUP to the session; a
  detached process has no controlling terminal and is in neither.
Today the coordinator installs no signal handler: SIGTERM ends it at once, and the CLI turn it started (its own session,
`start_new_session=True`) is left running. SIGINT (Ctrl-C) raises KeyboardInterrupt, and the dispatch path keeps the turn
recorded as `active`, kills the turn's process group and waits for it.

## Mechanism
- `bin/paired-session run|resume|reject|permission-probe ... --detach` runs every synchronous check first (argument, model,
  profile and gate-surface refusals still print in the caller's shell), then forks; the child calls `setsid()` and forks
  again; the grandchild re-enters `main()` with the same arguments minus `--detach`, its stdin on `/dev/null` and stdout and
  stderr appended to a log. The caller prints `DETACHED: pid <pid>; log <path>; ...` and exits 0 at once.
- Record, lock and log live in `<system temp>/paired-session-detached-<uid>/` (mode 0700, owner checked; never in the run
  dir or its parent, which the Claude author probe watches), keyed by the resolved run dir: `<key>.json` holds pid, run dir,
  argv, log, status (`running`, then `exited` with the exit code). The caller takes an exclusive `flock` on `<key>.lock`
  before forking and marks the record `starting`; the grandchild inherits the lock, holds it until it exits and is the only
  writer of the record from then on (pid, session id, `running`). So a second `--detach` for the run dir is refused, and an
  older run's record is never read as the current holder's.
- The grandchild redirects its output, installs its handlers and publishes the record before it answers the caller through
  a pipe (`ok <pid>`); a failure there reaches the caller as `REFUSED: the detached command did not start: ...` and nothing
  has run.
- In the grandchild SIGHUP is ignored and SIGTERM kills the CLI turn in flight (the dispatch records its process group; the
  short span from `Popen` to that record only notes a stop and acts on it right after; no signal mask is used, since a child
  would inherit it) and raises `DetachStop` (a BaseException), so the rest is the Ctrl-C
  path above: the turn stays recorded as `active`, its group is reaped, the lease is released; the command exits 130 and the
  log ends with a `STOPPED:` line.
- `bin/paired-session stop --run-dir ...` (same required arguments as other actions) sends SIGTERM to the lock holder's
  recorded pid only if that pid still has the recorded session id (a pid reused after the holder exited is never
  signalled), waits up to 60 s for the lock to be released and reports the exit code; it prints a NOTE when nothing runs and
  a HOLD while a holder is still starting.

## What stays the same
The detached grandchild is an ordinary coordinator process: run lease, workspace lease, FIELD-13 probe lock, `active` and
`uncertain_active`, per-turn process-group cleanup (start, timeout, failure, stop), every Category A/B guard and the exit codes.
After `stop` the next command sees the interrupted turn as uncertain, as after Ctrl-C today: check the process group, then
`resume --retry-uncertain` (or `permission-probe --retry-uncertain` for a probe) or `abort`. Progress: `RUN_DIR/state.json`,
`status --brief`, the log. Without `--detach` nothing changes, including SIGTERM.

## Limits
- POSIX only (fork, setsid), like the rest of the coordinator. A detached run outlives the host session by design; the
  operator ends it with `stop` (or `kill -TERM <pid>`), never with SIGKILL, which would leave the turn's group running.
- The record dir accumulates one small JSON and one log per detached command; nothing prunes it.
