# FIELD-13: concurrent Claude author probes (design, not implemented)

Status: design for review. It makes no code change. Owner decisions are in §8.
Today's behaviour and the operator workaround are described in
[`concurrent-runs.md`, "Concurrent permission probes"](concurrent-runs.md#concurrent-permission-probes-field-13-open).

## 1. Problem

The Claude author probe (`_claude_author_probe` in `../coordinator.py`) judges
writes outside its own tree by comparing three listings. The listings are made by
`listing` in `../claude_author_probe.py`: `baseline` before the author turn, then
`first` and `second` after it, `SETTLE_SECONDS` apart. They cover:

- the probe's own tree `B = <run_dir.parent>/paired-session-author-probe-XXXX`, fully;
- every other entry in `run_dir.parent`, without descending into it and with directory mtimes ignored;
- every host-wide name `/tmp/paired-session-author-probe-*`, without descending into it.

A key that differs across the three listings counts as an escape, and the verdict
is FAIL. The window from `baseline` to `first` spans the whole author turn, which
takes minutes with a real CLI.

Concurrent work changes these listings when nothing has escaped:

1. **Shared parent.** Another run in the same parent changes it in two ways.
   - Its second Claude author probe Q creates its tree `B_Q` beside P's run dir and later removes it.
   - The other run's run dir is created there. In the default flow `permission-probe` is the first command on a fresh run dir. `run_lease` in `main` creates the run dir with `root.mkdir`, so a run that starts in the same parent during P's window FAILs P (`coordinator.py` around 567 and 6846).
2. **Q's run dir directly in `/tmp`** (`/private/tmp`). `B_Q` is then itself a
   `/tmp/paired-session-author-probe-*` directory. Every concurrent Claude author
   probe on the host sees it, whatever its own parent.
3. **P's parent is `/tmp`, the home directory or the temporary directory.** The
   Codex author probe creates and removes scratch directories in exactly these
   places: `ps-escape-*` in `/tmp`, `.paired-session-escape-*` in `HOME` and
   `paired-session-external-*` in `gettempdir()` (`coordinator.py` around
   4894-4926). Any other activity of this uid there counts too.
4. **Test suites.** These collide through the same `/tmp` names. The v297-ptt
   harness fix hides this in `ClaudeAuthorProbeTests` (§7).

Some names exist **only after an escape**. An escape by any of these probes also
FAILs every concurrent Claude author probe whose listing covers the name. That is
conservative: the escape is real, though attributed to the wrong run.
- A Claude author probe's `/tmp/paired-session-author-probe-<hex>.txt` target.
- The Codex author probe's `slash_tmp_path`, which uses the same prefix and suffix.
- The Claude reviewer and gate targets: `.paired-session-os-probe-*` beside the run
  dir, and `/tmp/paired-session-claude-sandbox-<hex>`, which no listing covers.

The question this design answers:

> How can P tell another **live** probe's owned tree, a sibling dir or a `/tmp`
> name, from a real escape, without opening a hole?

## 2. Threat model

**Adversaries**
- **The probed author model in P.** It is untrusted: it may be prompt-injected or
  adversarial. It runs inside P's sandbox, which may leak; that is why the probe
  exists. In the worst case it can write anywhere this uid can. It can also start
  detached children in a new session. Those children survive the process-group
  kill, stay sandboxed, and can act after the turn.
- **Any other model turn of this uid**, in any lane or role, Claude or Codex. It
  can *read* almost anything without a leak, `/tmp` included. A Codex sandbox keeps
  read access to the whole filesystem. This matters for any lock that can be held
  through a read-only fd.

**Adversary goal:** a PASS from P despite a write outside P's tree, or a hidden write.

**Trusted:** coordinators of this uid that run a version with this design, and the
kernel's file locks.

**Out of scope (unchanged)**
- Other uids, and root.
- Coordinators from before this design (§6).
- Writes *inside* another entry of `run_dir.parent`. The listing never descends there; this is an existing, documented blind spot.

**Requirement.** Every mechanism must fail toward FAIL, UNKNOWN or REFUSED, never
toward PASS. A component the adversary can forge or hold may only *block* or
*delay* a probe. It may never *excuse* a listing difference.

## 3. Candidates

| # | Mechanism | Excuses a difference? | Forgeable or holdable? | Covers | Verdict |
|---|---|---|---|---|---|
| A | Ignore sibling entries named `paired-session-author-probe-*` | yes | Yes. A leaking author runs `mkdir ../paired-session-author-probe-x` and hides a payload in it. | 1 (trees), 2 | rejected: a hole by construction |
| B | Marker file in each tree (`.owner.json` with pid and start time) | yes | Yes. The same leak writes the marker, and a live coordinator's pid is public. | 1 (trees), 2 | rejected |
| C | Protected registry. Q records (path, dev, ino, pid, start time) before it creates `B_Q`; P excuses only entries that match a live record. | yes, bound to one inode | Only if the author can write the registry directory. That directory is protected only by the sandbox under test. An HMAC key the author cannot read only moves the trust to the sandbox's read-deny. | 1 (trees), 2 | deferred: sound only while the sandbox holds, which is what the probe measures |
| D | Parent lock: one exclusive lock per resolved parent, held across the whole `permission-probe` command and around every run-dir creation in that parent | **no** | Holding it needs a lock call on the lock file (§4.2). The worst case is a delayed or refused command. | 1 (trees **and** new run dirs), and 2 together with E | **recommended** |
| E | Rename the Claude probe tree so it no longer matches the `/tmp/paired-session-author-probe-*` glob, e.g. `paired-session-claude-probe-tree-XXXX` | **no** | Nothing to forge | 2 | **recommended**: removes case 2 without a lock |
| F | A private, persistent per-run probe container `<parent>/<run name>.probe/` holding the probe trees. P lists its own container fully, plus the parent without descending. | **no** | Nothing to forge | 1 (trees only) | not recommended alone: a new run dir, and a new container, created in the shared parent during P's window still FAIL P |

**F** hides Q's trees with no lock, because a container's own entry never changes.
It cannot hide the default flow's run-dir creation, so it does not fix the field
case. Combined with D it adds nothing.

**E** removes case 2 outright. The glob still catches every name of its own class:
the probe's `/tmp` target and any escape with that prefix.

**D** then needs only a per-parent key. Probes and run starts in different parents
never wait for each other.

**Case 3** is not fixed by any candidate. A run dir whose parent is `/tmp`, `HOME`
or the temporary directory stays unsupported for concurrent work (§4.5).

## 4. Recommended design: E plus a parent lock (D)

### 4.1 E: a probe-tree name outside the glob

`_claude_author_probe` creates `B` with a prefix that does not start with
`paired-session-author-probe-`, for example `paired-session-claude-probe-tree-`.

These stay unchanged:
- the `/tmp` target name;
- `listing`, `attempts` and `verdict`;
- the rule substitution in `rules_match`, which is name-agnostic.

Existing tests that name the old prefix need an assertion-change ruling at
implementation time. All are in `test_operator_roles.py`:
- lines 1111, 1163, 1168, 1199 and 1712;
- the "foreign leftover" fixture `paired-session-author-probe-stale` at lines 1152
  and 1165. It still passes after E, but it no longer tests its intent, so it must
  use the new prefix.

### 4.2 The lock

**Path**
- The lock lives in a fixed directory, `/tmp/paired-session-probe-locks-<uid>/`.
  - It is created 0700 and checked like the workspace-lease directory: a real directory, owned by this uid, with no group or other bits.
  - It is a fixed path, not chosen through `workspace_lease_path`'s fallbacks, so every run uses the same file.
  - If the path resolves inside the workspace or the context, the command refuses, because an author could write there without a leak.
- The file name is `<sha256 of "st_dev:st_ino" of the resolved run_dir.parent>.lock`.
  Keying by identity means path aliases share one lock. The name matches neither probe glob.

**Kind**
- A POSIX record lock: `fcntl.lockf(fd, LOCK_EX | LOCK_NB)`. The fd is opened
  `O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC` with mode 0600. The file must be a
  regular file owned by this uid, with no group or other bits.
- An exclusive POSIX lock needs a writable fd.
- The premise is that the lock directory lies outside every role's writable roots.
  - Codex: `writable_roots`, and `TMPDIR = run_dir/author-tmp` with `exclude_slash_tmp = true` (`coordinator.py` around 1251 and 1681).
  - Claude: the sandbox's default writable paths.
- **This premise is not fully probed.** The author probe tests `/tmp` only with the
  Write tool (`write_tmp`); its Bash rows target `outside/` and `context/`.
- **Expected on macOS:** `flock` and `fcntl` locks are both advisory locks on the
  same vnode and can block each other. A turn that can only read the file may then
  still block the lock by holding a `flock` on it. The design therefore also adds
  the lock directory to the Claude sandbox read-deny list. A Codex turn keeps full
  read access, so this delay stays a residual (§4.5); it costs availability only.
  §5 test 4 settles the behaviour per OS.
- **POSIX-lock pitfalls.** Closing *any* fd of the file in the coordinator process
  drops the lock. So the payload is read and written only through the locked fd,
  and the file is never opened a second time. The lock also belongs to a process,
  so tests need separate processes, not threads.

**Acquisition point.** The lock is taken in `main`, *before* `run_lease`, so it is
held before the run dir is created and before `Coordinator(args)` saves any state.
`permission_probe` is called only from `main`'s `permission-probe` action
(`coordinator.py` around 6700).
- For `permission-probe` when the author vendor is Claude: hold the lock until the
  command returns. That covers this run's own mkdir, every probe turn and cleanup.
- For any other action whose run dir does not exist yet: take the lock, enter
  `run_lease` (which runs the mkdir), then release the lock. This includes
  `permission-probe` with a non-Claude author, `run`, `status` and `abort`. A new
  run in a shared parent therefore waits while a Claude author probe there is
  running, and never appears inside its window.
- Actions on an existing run dir take no lock.

**Order.** The parent lock comes first, while nothing else is held. Then the run
lease and the workspace lease follow, and both use `LOCK_NB`, which refuses at
once. Only the parent lock ever waits, so there is no deadlock.

**Holder inode check.** The lock directory is outside every role's write scope,
so a change there is escape evidence.
- After its `second` listing, `_claude_author_probe` compares
  `(st_dev, st_ino)` and the regular-file type of `lstat(path)` with `fstat(fd)`.
  A mismatch makes the lock path an escaped target, and the verdict becomes FAIL
  with reason `probe-lock-tampered`.
- The same check runs once right after the lock is acquired. If the file was
  replaced between open and lock, the command retries once, then refuses.
- The lock file is never unlinked.

**Payload.** After acquiring, the holder writes
`{pid, run_dir, action, started_at, wait_bound_s}` through the locked fd.
- `wait_bound_s` is its own whole-command estimate: the number of probe turns in
  the command, times `--timeout`, plus 300 s. The turns are the reviewer probe,
  the author probe, and the gate probe when the gate vendor differs (around 5276,
  5336 and 5361). For a mkdir-only holder it is 60 s.
- Readers use the payload only for the wait message and the bound, never as evidence.

### 4.3 Waiting

**Polling.** `LOCK_NB` every 1 s.

**Wait message.** This is printed to stderr, because no `Coordinator` and no event log exist yet:
`waiting for the probe lock of <parent> (unverified holder pid N, run_dir X, action A; lsof <path> shows the real holder)`.
It is written on first contact, then once a minute. A garbled payload prints `unknown`.

**Bound per holder.**
- It is the holder's `wait_bound_s`, or this command's own estimate if that is
  larger. A forged payload can only lengthen the wait.
- The bound restarts whenever the holder changes, so a queue of lanes keeps moving.

**Total cap.** `--probe-lock-wait-seconds`, which defaults to three times this
command's own estimate.
- `LOCK_NB` polling is not FIFO, so a waiter can starve under constant traffic.
- The cap turns starvation into a refusal.
- A deeper queue needs a larger flag value. This is an availability cost only.

**Cap exceeded.**
- `main` prints `REFUSED: another command holds the probe lock of <parent> (…)` and exits 2.
  - This happens before `run_lease`, so no run dir is created.
  - No `Coordinator` is constructed, so no state is saved and `co.hold` is not called.
  - No old `permission-probe.json` is superseded.
  - No turn runs and no report is written.
  - The probe-pass cache is neither written nor voided.
  - There is nothing to waive.
- This is a new `REFUSED` path. A run- or workspace-lease conflict prints `HOLD:`
  instead (`coordinator.py` around 6854).

### 4.4 Why this opens no hole

- With E, no probe tree matches the `/tmp` glob.
- With the lock, no lock-respecting coordinator creates or removes a probe tree,
  and none creates a run dir, in P's parent during P's command.
- Trees left by a crashed holder are static across P's three listings, and the
  kernel releases a dead holder's lock.

So every difference P sees still comes from one of four sources:
1. P's own author, including its detached children;
2. an escape by another probe;
3. a non-probe actor, such as an operator log;
4. a coordinator from before this design.

None of them is excused: `listing`, `attempts` and `verdict` are unchanged.

What the adversary can do with the lock:

| Action | Effect |
|---|---|
| Hold the lock | Needs a writable fd, or on macOS possibly a `flock` from a reader (§4.2). The result is a delay, then REFUSED for later commands in that parent. Never a PASS. |
| Unlink or replace the lock file during P's window | P's holder check gives `probe-lock-tampered`, which is a FAIL. If this happens after the check, the only effect is a possible overlap with the next command, which yields a false FAIL, never a false PASS. |
| Release P's lock | Not possible. The lock belongs to P's process, and the author gets no fd to it: `O_CLOEXEC`, `Popen`'s default `close_fds`, and no `pass_fds` in the coordinator. |
| A detached child writes after P's window | The next probe in that parent FAILs. This is conservative, because the escape is real. |

The lock is a scheduling device, not evidence. Abusing it costs availability only.

### 4.5 What this does not fix

- **Non-probe changes in a shared parent**, for example an operator log or a
  launcher file. They still FAIL P, as today (FIELD-8 `changed_beside_run_dir`).
- **A run dir whose parent is `/tmp`, `HOME` or the temporary directory** (case 3).
  This stays unsupported for concurrent work.
- **Escapes by other probes into host-wide names.** These still FAIL P, including
  escapes by the Codex `slash_tmp_path`. Owner decision D5 covers that name.
- **Delays from readers.**
  - On macOS, a Codex turn's `flock` can hold the lock, which delays P or ends in REFUSED.
  - Starvation past the cap also ends in REFUSED, never in a wrong verdict.
- **Mixed versions** (§6).

## 5. Tests the implementation needs

All tests use the fake author of `test_operator_roles.ClaudeAuthorProbeTests`, and
none needs a real provider CLI. Overlap is made deterministic: the fake author
waits for a signal file instead of sleeping. Tests that need the `main` path, or
two coordinators, drive `coordinator.py` subprocesses. `ClaudeAuthorProbeTests`
calls `_author_permission_probe` directly and holds no locks or leases.

1. **Shared parent, two `permission-probe` processes, both PASS.**
   - Setup: both run dirs are fresh in one parent. Q's fake author blocks on a signal file.
   - Assert: P's run dir and tree are created only after Q's command has returned.
   - Assert: both PASS.
   - Mutation: removing the lock makes P FAIL, with `B_Q` or Q's run dir among its targets.
2. **A run start in the same parent waits.**
   - Setup: while P's author blocks, start `run`, `status` and `abort` on a fresh run dir in P's parent.
   - Assert: each one waits and creates its run dir only after P returns.
   - Assert: P PASSes.
   - Assert: actions on existing run dirs do not wait.
3. **E: Q's run dir in `/tmp`.**
   - Setup: P has a private parent. Q's run dir is directly in `/tmp`, and Q's tree is held open by a signal.
   - Assert: P does not wait and PASSes. Only P, the observer, is asserted.
   - Mutation: restoring the old prefix makes P FAIL.
4. **Lock kinds.**
   - Assert: a helper with an `O_RDONLY` fd gets `EBADF` from `lockf(LOCK_EX)`.
   - Assert, per OS: whether an `flock` held through that fd blocks `lockf`. Record the result. On macOS, also assert that the Claude sandbox settings carry the read-deny entry.
5. **Busy lock refuses cleanly.**
   - Setup: a helper holds the lock with a writable fd. `--probe-lock-wait-seconds 1`.
   - Assert: `permission-probe` exits 2 and prints `REFUSED:`.
   - Assert: no run dir is created when it was fresh.
   - Assert: on an existing run, `state.json`, `state['permission_probe']` and the old `permission-probe.json` are byte-identical, and nothing is superseded.
   - Assert: no probe tree and no `/tmp` name exist.
   - Assert: an existing probe-pass cache entry is untouched.
6. **Dead holder.**
   - Setup: a holder that left a tree is SIGKILLed.
   - Assert: the next command gets the lock at once and PASSes.
   - Assert: the stale tree is left in place, because it is not P's to remove.
7. **Holder inode check.**
   - Assert: when the fake author unlinks and recreates the lock file during its turn, the result is FAIL with `probe-lock-tampered`.
   - Assert: a replacement between open and lock is retried once, then refused.
8. **Lock-path checks.** Each of these refuses:
   - a symlinked lock file;
   - a file or directory with group or other bits;
   - a directory owned by another uid (simulated);
   - a lock directory inside the workspace.
9. **The window covers the turn, and no fd leaks.**
   - Assert: inside its turn, the fake author's `lockf` on the lock path fails with `EAGAIN` or `EACCES`.
   - Assert: its own `/dev/fd` does not contain the lock file's inode.
10. **Different `HOME`s share the lock.** Two processes with different `HOME` and `CODEX_HOME` and the same parent exclude each other.
11. **Payload and bounds.**
    - Assert: a garbled or forged payload never crashes the wait message.
    - Assert: a larger forged `wait_bound_s` only lengthens the wait, and only up to the cap.
    - Assert: a change of holder restarts the per-holder bound.
12. **Escapes still FAIL.**
    - Assert: the existing escape tests pass with the lock held.
    - Assert: a sibling file written during the window by a process that takes no lock still FAILs P with `changed_beside_run_dir`. This pins that the lock excuses nothing.
13. **Scope.**
    - Assert: the Codex author probe and the reviewer and gate probes take no lock of their own.
    - Assert: `CODEX_PROBE_SHA256` is unchanged.
    - Assert: the lock is released on every exit path of `main`, including exceptions and `HOLD:` / `REFUSED:` returns.
14. **Order.**
    - Setup: run A in workspace W1 waits on the lock that run B in W2 holds.
    - Assert: B completes its probe and cleanup.
    - Assert: while A waits it holds no lease. So a command on A's existing run dir proceeds, and A then meets the run lease as today (`HOLD:`). There is no deadlock.

## 6. Rollout and compatibility

- **Version gating.** A coordinator from before this design takes no lock and uses
  the old tree prefix, so mixed-version lanes still collide. The release note and
  `concurrent-runs.md` must state that all concurrent lanes need this version.
  Otherwise the sequential-probe workaround applies.
- **Probe-pass.** Every release bumps `plugin.json`, and `plugin_version` is part of
  the Claude author flags (`coordinator.py` around 73 and 1645). So old Claude
  author PASS receipts and cache entries are voided as usual, and each run probes
  again once. The macOS read-deny entry (§4.2) also changes
  `claude_bash_sandbox`, and with it the Claude role surfaces and digests, in that
  same release.
- **Probe identifier.** Whether E changes `PROBE` (`claude-author-filesystem-v1`)
  is left to the implementation. The verdict semantics do not change.
- **Docs.** When this lands, update "Concurrent permission probes" in
  `concurrent-runs.md`:
  - probes and run starts in a shared parent wait instead of failing;
  - the `/tmp` run-dir case is gone;
  - §4.5 lists what remains.

## 7. Relation to v297-ptt

v297-ptt (commit d1ce3d6) is test-only. `ClaudeAuthorProbeTests` lists only the
probe's own `/tmp` target and the `/tmp` paths its scenario names. Its test
`test_another_runs_probe_name_in_tmp_changes_no_verdict_in_these_tests` pins that
the real host-wide listing still FAILs on a foreign name.

- **The pin stays valid under this design.** Neither E nor the lock excuses a
  listing difference, and the pin's foreign name is still under the glob. P's own
  process writes it inside P's window, so the lock plays no part. The test's
  comment says the pin "flips by design when FIELD-13 lands", which would hold only
  for candidate C. The implementation batch should reword it.
- **The harness filter stays.** Test processes have different roots, so
  per-parent locks never serialize them. Several tests also create or remove `/tmp`
  names outside any lock window:
  - the silent `/tmp` write test, around lines 1552-1560;
  - the foreign-name pin, around lines 1660-1688;
  - the three host-wide `Path('/tmp').glob('paired-session-author-probe-*.txt')`
    checks at lines 1126, 1190 and 1700.

  Removing the filter, or making those checks per-probe, needs an
  assertion-change ruling.

## 8. Owner decisions

- **D1. Fix shape.**
  - **Recommended: E + D.** This fixes the field case: two lanes whose fresh run
    dirs share one parent.
  - **Lighter alternative: E alone, plus a layout rule.** Each run dir gets its own
    dedicated parent, which is not `/tmp`, `HOME` or the temporary directory, and
    the launcher or run kit creates `<runs>/<id>/run`. This needs no lock and no
    waiting. Shared parents stay unsupported, and the coordinator could warn when a
    run dir's parent holds other entries.
  - **Not recommended: F.** It hides only probe trees, not new run dirs.
- **D2. Lock window and waiting.**
  - **Recommended:** the window covers the whole `permission-probe` command and
    the lock is taken in `main` before `run_lease`. A busy lock is then a clean
    `REFUSED` before any state.
  - Cost: serialization covers every probe turn of the holder's command, and waits
    are bounded as in §4.3, with the default cap of three times the command's
    estimate.
  - **Alternative: lock only around `_claude_author_probe`.** That needs a
    dedicated reason. The reason must stay out of `_author_probe_waived`'s waivable
    set and must not trigger `_probe_void_on_non_pass`. It does not cover run-dir
    creation, so new run dirs in case 1 would still FAIL a probe.
- **D3. Lock kind on macOS.**
  - **Recommended:** `lockf`, plus the read-deny entry for Claude roles. Codex
    readers stay an availability-only residual.
  - Alternative: `flock` everywhere. It is simpler, but any reader can hold it.
- **D4. Candidate C** (a registry that allows truly parallel probes). Only if the
  owner accepts that its soundness rests on the sandbox under test.
- **D5. The Codex `slash_tmp_path` prefix.**
  - **Recommended:** rename it out of the `paired-session-author-probe-` namespace
    in the same release as E, so that a Codex escape is no longer attributed to
    concurrent Claude probes.
  - This needs a `CODEX_PROBE_SHA256` ruling.
