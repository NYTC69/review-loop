# Paired-session coordinator

This is the tracked implementation of the paired-session workflow. Its stable
repository-local entry point is `bin/paired-session`. From v2.10.0, a fresh
`/review-loop` request without an `entry` key hands off to this coordinator through
the paired-session skill (the default entry, `docs/v2.10-entry-switch.md`);
the legacy workflow was removed in v2.13.0 (`entry: legacy` is refused; `/review-loop:legacy` was deleted in v2.13.1).

The coordinator runs one author and reviewer through PLAN/EXEC, then applies
fresh shadow/adversarial checks and a delivery sequence. It owns isolated run
artifacts, workspace snapshots, invocation limits, and a permission preflight.
Reviewer receipts record the requested model beside the provider-reported
identity and classify it as `MATCH`, `MISMATCH`, or `UNREPORTED`. A match requires
the exact requested ID or that ID with an explicit date suffix; synthetic,
malformed, or missing identity data is unreported, and Claude subagent models
are ignored.
In strict mode (`--strict` or an operator profile's `"safety_mode": "strict"`)
the preflight must pass before either a fresh run or a resumed run can invoke an
author; the default `efficient` mode keeps every sandbox but needs no probe PASS
(`docs/efficient-mode.md`). The Codex author permission probe uses a disposable Git workspace and
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
`APPROVE_WITH_ADVISORY` at the final PLAN/EXEC round. Its raw
REVISE and effective workflow verdict are recorded separately. Those findings
remain open and are labeled advisory in the findings ledger and report until a
later reviewer confirms they are fixed; the next phase receives them as
non-blocking context. Non-final PLAN/EXEC REVISE continues its normal repair
loop. CRITICAL, MAJOR, SECURITY-flagged, and observed test failures are never
downgraded; EXEC requires a successful observed configured test.
Fresh shadow/gate blockers retain their existing blocking behavior. Review
comparison shows each reviewer decision separately from the final coordinator
status, which may still be HOLD after later checks.

The fresh shadow and gate must not see review history: before launch they refuse
any input that carries reviewer ledger ids (`F` plus three or more digits) or
review narratives (for example "previous review" or "response to reviewer"),
including `context/plan.md` and `context/workitem.md`. FIELD-23 prevents ACCIDENTAL carry-over of review history into a fresh context;
it does not defend against an author who deliberately evades the check.
Deliberate evasion is a known residual. Review independence otherwise rests on
the fresh session and the gate templates, which stay unexempted, as do prompts.
With a run's `base_commit`, both delta patches scan only added (`+`) lines,
excluding `+++` headers; context and removed lines are never scanned. A match is
exempt if the same matched text, case-insensitively, occurs anywhere in that
file at the base commit (post-image path; for renames, also pre-image path).
New files and binaries get no exemption. A base marker reused anywhere in the
same file therefore passes, including copies: this is a documented residual.
The exempt text is the whole match ("Claude approved", not "Claude") and must
occur at the base as a whole word ("APPROVE" is not in "approved"). Another
known residual, on the strict side: a file moved without `git mv` (`mv`, or `rm`
plus a new file) appears as a new untracked file and gets no exemption, so a
base marker in it holds; use `git mv`. In a review-only run (FIELD-26) the change
as created is the user's code, not review history: a match is also exempt when
the same file in the run's creation mirror (`internal/review-start`) contains
it, new files included, so a PR that names a vendor passes. Text a later fix
round adds is not in that mirror and is scanned as before. Its paths count the
same way (FIELD-27): `delta.stat`/`status.txt` exempt the mirror's paths and the
frozen review scope's initial-change list is masked, so `bin/codex-run` passes
while a path a fix round adds is caught; a ledger-id or verdict shaped path
(`docs/F001.md`, `APPROVE.txt`) is still refused at creation (FIELD-11). A
scope-change successor keeps its parent's creation mirror for this.
In `delta.stat` and `status.txt`, matches inside paths that exist at the base
commit are exempt. In `plan.md` and `workitem.md`, a match is exempt only inside
backticks or a fenced code block and only if its matched text occurs in a file
at the base commit. Unquoted narrative is scanned as before. Without a
`base_commit`, the old scan applies. The same helper applies to fresh shadow,
gate, PLAN approval and review-only creation inputs.
When a shadow or gate is
on, a plan that the reviewer approves while it still carries ledger ids or such
wording is sent back to the author for a restatement without them, so the gate
does not refuse it after EXEC. With no PLAN round left, the author gets one extra
rewrite-only turn that does not count as a PLAN round; the PLAN reviewer then
reviews the rewrite against the plan it approved, and the run holds at PLAN if
the history is still there or the plan changed in substance. Vendor names and other gate-scan triggers in the plan are still found
only at the shadow or gate. The work item is never rewritten: a work item with
`F001`-style identifiers or review narratives holds at PLAN, so keep them out.

## Local marketplace installs

Installing a plugin from a local directory marketplace copies the whole directory,
including Git-ignored files, into the host plugin cache. That can include
`.compass/`, `.claude/`, `HANDOFF.md`, and local caches. Install from a clean
checkout or the GitHub marketplace to avoid copying working-tree state into the
plugin cache.

## Start a task

For simultaneous lanes, read [Concurrent runs and isolated Codex homes](docs/concurrent-runs.md).

Use a dedicated Git worktree for each product task and keep run artifacts in a
sibling directory outside the workspace. Never put `--run-dir` inside the
workspace: the author can write to the workspace and must not be able to alter
coordinator state. Keep the run root outside any git repository too: the role
TMPDIRs live under the run directory, and a project that refuses scratch space
inside a repository then falls back to /tmp, which the Codex read-only sandbox
denies (FIELD-28; `run`, `resume` and `permission-probe` warn on stderr). The workspace must be a Git worktree and the work item must
be a file. The configured test command is checked before the coordinator starts.
A Claude reviewer or gate runs that command (and each `--reviewer-command`) as
one exact allowlisted Bash call in dontAsk mode, so command substitution, pipes,
`;`, `&&`, redirection or loops can be refused before it runs; `run` and a new
`permission-probe` print a warning for such a command. Put it in a script and
configure `/bin/bash /absolute/path/to/script.sh`. When the probe's run of the
test command hits the Claude CLI's own Bash timeout (set per call, at most 10
minutes by default), the probe fails with `allowed-command-timeout (<N> s)`
instead of `allowed-command-failed`;
re-run the probe on a less loaded host, or configure a faster test command (the
same one for probe and run).
FIELD-20 (long test commands): Codex's `exec_command` returns after about 10 s
with a session id and no exit code while the command keeps running. Every probe,
reviewer, gate, docs-reviewer and specialist prompt now tells the role to poll a
still-running command until it has an exit code, never to start another command
or end the turn before that, and to give a long Bash call the 600000 ms timeout.
A command started but never completed (no exit status when the turn ended, or
`-1`) is reported as `allowed-command-not-completed` by the probe and as "not
observed to completion" in an EXEC or DOCS approval HOLD, not as an ordinary
failure. One observed completed exit-0 run of the exact configured command is
still required. The remaining limits are the per-dispatch timeout (`--timeout`)
and, for a Claude role, the CLI's 10-minute cap on one Bash call (reported as
`allowed-command-timeout`, above); a test command longer than either cannot be
observed to completion.
Write launcher logs (for example `permission-probe ... > probe.log`) outside the
run dir's parent: the Claude author probe watches the entries beside the run dir,
and a log that grows there during the probe fails it as a file changed outside
the run dir. When any role is Codex, `run`, `resume`, `reject` and
`permission-probe` refuse up front if the selected `CODEX_HOME` (default
`~/.codex`) is not an existing directory.

For operator-selected programs, role/vendor settings and test commands, copy
`paired-session-config.example.json` to an operator-owned path outside the
workspace, run directory and author temp directory, then pass it with
`--config /absolute/path/to/profile.json`. The example leaves `docs_file` out,
so a worktree-lifecycle run keeps its `CHANGELOG.md` default. It sets
`lifecycle_mode: on`, which is also the CLI default for a new run; to create a run
with lifecycle off, pass `--lifecycle-mode off`. A workspace
`.review-loop/paired-session.json` may hold limits and other non-program
settings, but program/role/test-command keys there are refused (`REFUSED`, exit 2)
when that profile is selected, before any run state is created; `permission-probe`
reports them in its result instead, and an existing run that later finds such keys
holds. `--config` replaces the workspace profile; copy any desired limits
into the external profile because the two files are not layered. The CLI loads the selected profile for run, probe,
and resume; explicit CLI options override it. On resume, effective settings
must still match the saved run configuration. A changed binary or PATH requires
a fresh permission probe before the run can continue; the refusal names what
changed (for example `changed: path_env`), so run `reject`, `resume` and `accept`
from the same shell setup as the probe. The names of secret-looking environment
variables (for example a `*_TOKEN` set by an agent session) do not void a recorded
permission-probe PASS: the Claude credential deny list always follows the current
environment. They still bind a strict run's operator opt-in and accepted probe skip, the
probe-pass cache key and a lifecycle-on role manifest. A Claude-author refusal
also names the failing probe check.
Role models are operator-set (ADR-9): a role without `--author-model`,
`--reviewer-model`, `--gate-model` or a profile value gets its vendor's default
(Claude: `claude-opus-5-5`; Codex: `gpt-6.1-sol`). Before run state is created,
every model id must be well formed and, when `allowed_models` is set, listed for
that role's vendor. The Step 3.4 gate defaults to the author's vendor (ADR-10);
`--gate-vendor` overrides it and is recorded as `gate_vendor_source: operator`,
and a `--gate-model` of the other vendor without `--gate-vendor` is refused.
`lifecycle_mode` defaults to `on` for a new run started from the CLI (earlier
versions defaulted to `off`); a saved run keeps the mode it was created with, so
`resume`, `accept` and the other run commands need no flag. The paired-session skill
passes `--lifecycle-mode on` for every new run (D-4). The frozen config also records exact
`docs_file`/`docs_allowlist` paths and `skip_quality_polish` (`skip_globs` / `--skip-globs` was removed
in 3.0: a profile that still sets it is refused as an unsupported key);
outside-workspace or wildcard doc paths are refused. The real lifecycle is the
worktree lifecycle of `docs/e2e-6-worktree-lifecycle.md` (D12, ADR-11):
`lifecycle_mode=on` from the command line or an
operator `--config` starts a worktree-lifecycle run that runs PLAN and EXEC and
then runs FINISH (a fresh author turn; a change reopens EXEC review and gate)
and POLISH-Q (fresh report-only specialists, legacy Step 3.5; their blockers
go to an author fix that the owning specialist re-reviews before EXEC review
and gate run again) and DOCS (a fresh docs writer; a protected path HOLDs, a
write outside the docs allowlist reopens EXEC review and gate, an allowlisted
write gets a fresh docs review that must run the test) and the SECURITY scans
(`sensitive_policy` paths and `scripts/security_preflight.py`) with a fresh
security reviewer (any hit or finding HOLDs), then reaches DONE (acceptance
pending). `accept --expect` on a W DONE accepts it without touching refs or
the index, or with an operator `auto_commit: true` makes one hook-free local
commit of exactly the accepted tree (never a push; `auto_commit` defaults to
true for a `--review-only` W run and to false otherwise, and an explicit CLI or
operator-profile value wins), and writes a Chinese
delivery report; `reject` reopens EXEC; a
workspace profile can neither enable it nor set its docs/skip/polish keys, and
a strict run refuses
`--accept-unverified-claude-author` and `--accept-probe-skip` (D-7; an
efficient run needs neither and records neither). Legacy
DONE/ACCEPTED or fake-format lifecycle states, gate-off and `resume --polish`
cannot enter it.

In 3.0.0, the lifecycle-off advisory POLISH round is removed. The hidden
`--polish-round off` CLI/profile option is accepted as a no-op; other values are refused.
`--lifecycle-mode off` converges directly to DONE; lifecycle-on POLISH-Q remains available.

Quality writers (D09, `docs/d09-cap1-writer-passes.md`): after a clean POLISH-Q,
`--quality-writers both|simplify|tests|off` (any profile may set it; frozen;
default `both` for `--review-only`, `off` otherwise) runs a fresh author session
that simplifies the changed code, then one that consolidates the changed tests.
The explicit test command runs first on the reviewed tree (a red baseline skips
both) and again after each change. A kept change (`wrote`) replays EXEC review,
shadow, gate, FINISH and the specialists before DOCS, on its own budget of one
replay round and one fix round (outside `--max-exec-rounds`; `writer_replay_rounds`);
the replay's first non-approval rolls it back (`rolled-back:review`), only the
writer the findings name when the two changed different files (the other is
reviewed again). A test writer that lowers the number of test cases is rolled
back (`rolled-back:tests`). Other outcomes: `no-op`, `rolled-back:tests`, `rolled-back:boundary`
(reserved docs or `.review-loop/` config), `rolled-back:hold`, `exhausted`, and
the skips `no-test-command`, `small`, `no-test-file`, `budget`,
`no-green-baseline`. Cost: about +10 invocations for a typical change (two
writer calls and one replay); the state is in `lifecycle.quality_writers`, the
outcome in the delivery report.

Advisory fix round (ADVFIX, owner 2026-10-06): `--advisory-fix-round true` (default false, frozen; refused
with `--review-report`) gives the author one fix round, at the first clean POLISH-Q exit, for the non-blocking
findings still open (MINOR, LOW, a gate's MEDIUM/LOW), before the quality writers: fix what is reasonable,
dismiss the rest with a reason; then EXEC review, shadow, gate, FINISH and the specialists run again. Once per
run, on its own round outside `--max-exec-rounds`; without invocation headroom it is skipped with the reason.
Findings still open afterwards stay advisory. The delivery report says ran (N of M addressed), skipped or off.

The disabled E2E candidate-tree module can materialize a clean HEAD into an
external scratch checkout with a separate scratch Git directory and index. It
returns the baseline tree OID, frozen parent/ref, live-index hash and whether
the candidate is on a different filesystem from the other roots. Same-device
materialization is useful for offline tests but cannot activate lifecycle.
Immutable review/test checkouts and OS enforcement are separate steps; this
module is not called by the live route.
The baseline now rejects hidden/sparse live index state, linked worktree or
common-Git scratch paths, ambiguous prefixes and transforming Git attributes.
Each checkout file is compared byte-for-byte to its indexed blob before ingest.
The offline baseline now batches blob and attribute checks, including legacy
`crlf`, and checks NFC/casefold aliases at each directory component. Scratch
Git uses fixed case/symlink settings and pins the source commit under a private
scratch ref. Offline ingest stages only authorized adds/deletes/modes/symlinks,
including ignored files, as no-filter blobs in a temporary scratch index. It
adopts a verified index, returns a manifest/tree OID, and rejects later byte,
path, ref or live-index drift. Every cumulative manifest path is rechecked
against the frozen grant, even if the scratch index was changed before ingest.
The adopted tree is rebuilt from an empty scratch index using independently
hashed candidate bytes; it never trusts a copied cache-tree or Git replace
ref. Verification repeats that fresh-index proof before a review may rely on
the OID. These helpers still require a stopped writer and installed OS denial
of metadata writes before any real candidate-tree activation.
It does not dispatch writers or prove installed OS sandboxing. Before any live
caller may use ingest, it must positively stop the writer process group and
deny that writer OS access to the scratch Git directory and index. Same-device
candidates remain ineligible for activation.

```sh
bin/paired-session run \
  --workspace /path/to/disposable-worktree \
  --workitem /path/to/WORKITEM.md \
  --run-dir /path/to/worktree-run-id \
  --test-command 'python3 -m unittest' --lifecycle-mode on
```

In strict mode, before `run` or `resume`, perform the author permission probe with the same
workspace, work item, run directory, author vendor/binary, profile, and one-run
overrides:

```sh
bin/paired-session permission-probe \
  --workspace /path/to/disposable-worktree \
  --workitem /path/to/WORKITEM.md \
  --run-dir /path/to/worktree-run-id \
  --test-command 'python3 -m unittest' --lifecycle-mode on
```

The probe is bound to the author binary and relevant configuration; changing
them requires a fresh probe. For a Codex author, the coordinator runs external
temporary-path and `/tmp` escape-denial writes directly through `codex sandbox`
as advisory controls. Their argv, return codes and observations appear under
`advisory_direct_controls`; this separate CLI's policy is not proven equivalent
to the real `codex exec` author policy. Under Yuan D1(b), its full-parameter
positive controls and OS-denied checks of every escape target may qualify an
otherwise UNKNOWN model result as `PASS_RESIDUAL_RISK` only when every dedicated
directory is unchanged in mtime/ctime/link count/listing, no sentinel exists
and model positive writes succeeded. A refused synthetic argv stays UNKNOWN;
a written target or failed positive check FAILs. The report states
"equivalence to real codex exec UNVERIFIED; re-check at M6". Reviewer OS-only
UNKNOWN still blocks the overall probe.
The Codex capability guard scans local user, project, system and file-managed
config, macOS `com.openai.codex` MDM preferences, and MCP/app bundles in the
active CODEX_HOME plugin cache. Every Codex dispatch (author, reviewer, gate,
probe) passes `-c features.plugins=false`, which makes cached bundles inert
(empirical evidence: `.compass/results/2026-10-01_cg-codex-plugin-evidence.md`;
`apps = false` or `remote_plugin = false` alone is not relied on). The argv is
bound into the reviewer, gate and author flags digests. Since rel210-fixCG the
coordinator's guard therefore records cached MCP/app bundles as inert (path and
sha256 in `plugin_bundles_inert`, and in each Codex turn receipt as
`codex_plugin_bundles_inert`) instead of reporting them, so a ChatGPT default
home (`chatgpt-global`, `openai-curated-remote`) runs. A dedicated `CODEX_HOME`
with `[features]` `remote_plugin = false`, `plugins = false`, `apps = false`
remains a stricter option.
MCP servers declared in config.toml are flagged either way. Managed feature
settings and unreadable MDM preferences fail closed. After its Codex turns the
permission probe runs the guard again; a new finding FAILs the probe.
A missing `config.toml` (a fresh `codex login` home) is an empty config for the
probe controls and the config digest.
The scanner runs at the author probe and before each Codex dispatch; the author
config digest remains part of the probe binding. This reflects the
[OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins)
and [managed configuration](https://learn.chatgpt.com/docs/enterprise/managed-configuration)
contracts. The scanner reads configuration only and records no raw payload.
For installed Codex 0.157, the direct control requires a named `-P` profile
derived from the author's sandbox overrides. It byte-copies only the active
`config.toml` into a disposable `CODEX_HOME` under the run directory, verifies
the copy and deletes it after each check; no auth or credential files are
copied. Any secret embedded in `config.toml` remains inside the run-owned copy.
The probe binds the installed `codex --version` to the author flags. The real
model-driven author probe checks that workspace and run-owned `$TMPDIR` writes
succeed. It also asks that same author
turn to attempt one write each under an external temp directory, `/tmp`
(`/private/tmp` on macOS), `$HOME`, and the workspace parent. Each target uses
a fresh sentinel filename, never a config file. The coordinator checks each
target on disk before cleaning up its own sentinel: an existing target is
`FAIL` regardless of the model's report; an absent target with an observed
OS-denied attempt is `PASS`; an absent target without a usable attempt report
is `UNKNOWN`. A missing or malformed attempt stays `UNKNOWN` for that target;
only the D1(b) filesystem and synthetic conditions above can qualify residual
risk. `permission-probe.json`
records per-target evidence and cleanup.
What a Codex author cannot do inside this sandbox: see
"Codex author sandbox: what a work item cannot do" below.
Escape sentinels live in coordinator-created dedicated directories. The probe
records each directory's before/after mtime, ctime, link count and sorted
listing. A new entry FAILs; a changed directory with no entry stays UNKNOWN.
Dedicated-directory cleanup removes only known sentinels and its own empty
directories; foreign entries there are retained and reported.
For a single clearer retry after `UNKNOWN`, set `PAIRED_SESSION_PROBE_CLARIFY=1`
on the permission-probe command; this changes only the prompt, not the sandbox.

Read-only temp dir (FIELD-1, v2.9.6): a Codex reviewer, shadow, gate or probe
turn runs under the permission profile `paired_session_readonly` (root and
workspace read-only, only `$TMPDIR` writable, network off), selected with
`--config default_permissions="paired_session_readonly"` because `codex exec`
has no `-P` (verified on codex-cli 0.160.0; an explicit `--config` outranks
user, system and project `config.toml`; managed config, requirements and MDM
may outrank it, and the capability guard refuses a dispatch when they set
permission keys), instead of
`sandbox_mode="read-only"`, with TMPDIR/TMP/TEMP set to a fresh 0700
`<run_dir>/role-tmp/<seq>-<role>` for that one dispatch, so a `tmp_path`-style
test has a usable temp dir. The coordinator lists that root in the turn's
receipt and removes it after the turn (also after a failed turn; an uncertain
turn's root when it is archived or at the next dispatch). Removal first stops what is
left of the turn's CLI process group (a descendant that left the group is not
covered; if the group cannot be confirmed gone the scratch is kept, and a
successful turn then HOLDs; a kept leftover is swept at the next dispatch only on an explicit ESRCH for its turn's group (probed, never signalled, since its pid may have been reused) or a recorded never-started turn; no recorded pid, EPERM or any other error keeps it and that dispatch HOLDs naming it), then works through directory handles, resetting
modes and file flags through open file descriptors, and never follows a link
even if a process survives: `role-tmp` must be a real 0700 directory owned by this user, a link or
any other leftover entry there is refused, and inside a scratch a link is
removed as a link while modes and file flags are reset without following one.
A regular file with more than one link, an unreadable directory, or a changed
mode of the scratch root itself, left in the scratch fails the turn. The reviewer and gate flags digests
change, so an older probe PASS or probe-pass cache entry no longer matches:
re-run `permission-probe`. A Codex probe or gate-probe turn must now write its
own `$TMPDIR` and must be refused each of: a write to `/tmp`, to the user temp
dir, to the run dir, to `role-tmp` beside its own scratch (`$TMPDIR/..`), to the
context, every workspace write, and `ln <run-dir file> "$TMPDIR/..."` (a hard
link into its scratch); every target must be absent and the link source keep a
single link afterwards. A leg counts as refused only on an explicit sandbox or
OS denial (Operation not permitted, Permission denied, Read-only file system,
a sandbox deny line) with a non-zero exit; `command not found` (exit 127), exit
126 or any other error makes the probe UNKNOWN, never PASS. The reviewer and
gate probes' `git --literal-pathspecs checkout --` and `rm` legs target an
existing tracked file of the workspace (a regular, non-symlink `git ls-files`
entry, preferring one without unstaged changes, then one without `:*?[]\`;
named in the report as `probe_tracked_file`), so the workspace needs at least
one; without one the probe is refused before any turn (this refusal leaves the
probe-pass cache alone: no surface was tested). `--literal-pathspecs` keeps a
name such as `*.py` from reaching other files. A broken surface deletes that
file: the probe FAILs and reports it, and the file is not restored. Only that
real probe shows that `codex exec` honours the profile; the tests use fake CLIs, and the probe runs fresh turns only (see
resume turns below).

Hard links: the scratch shares a volume with the run dir and usually with the
user's home. If the CLI let a role hard-link a same-user file into its scratch,
it could write through it to any such file it can read; the probe's refused
`ln` is the evidence that it cannot (a probe that sees the link made FAILs). As
defence in depth for run-dir files, the coordinator records inode and ctime of
every regular file under the run dir before the turn and fails the turn on any
change until the CLI process exits (making a link, writing through it or
changing mode, flags or times all move the ctime, which no process can set
back), so a link made, written through and removed within one turn is caught.
That check does not cover exactly `state.json` and `progress.jsonl` (no other
names), which the
coordinator writes during the turn (`state.json` is replaced atomically from
the in-memory state after the turn; a write through a link survives only a
coordinator crash before that save; `progress.jsonl` is display only), the
turn's own `evidence/<seq>-<phase>-<role>.*` files (the coordinator reads the
answer and observed commands from its `stdout.jsonl`), Codex session rollouts
under `CODEX_HOME`, or any same-volume file outside the run dir, workspace,
context and the watched global config files; for those the refused `ln` in the
probe is the only control. That control assumes the probe model does not forge
its own command events: the probe reads them from the turn's `stdout.jsonl` and
the Codex rollout, which are outside the ctime check, so if the CLI did allow a
hard link a hostile probe model could link one of them, append a fake refused
`ln` and skip the real one (reading stdout from a pipe into memory would close
this; not done). The refused writes are judged by their targets on disk and
are not affected. A detached descendant acting after the CLI exits is outside
the window, as for the other per-turn checks. Resume turns: a persistent Codex
reviewer's later turns run `codex exec ... --config default_permissions="paired_session_readonly" ... resume
<id>`; the probe runs fresh turns only, so whether the real CLI applies the
profile on resume (before, a plain `sandbox_mode` override did) is unproven; if
it did not, those turns would fall back to the CLI's default sandbox and only
the workspace snapshot, context digest and run-dir ctime checks would remain. Claude roles keep Claude
Code's own sandbox TMPDIR (`/tmp/claude`, shared per UID, also with a Claude
author): `denyWrite: run_dir` takes precedence over `allowWrite`, so a run-dir
root cannot be writable for them.

Claude roles use one inline strict sandbox settings object, deny secret-like
environment variables and common credential files, block network access and
local network binding, and require the exact Bash allowlist. The reviewer
permission probe gives exactly one unique run-directory `touch` command a narrow
CLI allowlist entry. That command may be denied by either the CLI permission
layer or the OS sandbox; its result alone cannot establish the OS boundary.
The reviewer probe also uses a dedicated target beside the run directory:
`sandbox.filesystem.denyWrite` covers that target, while `permissions.deny`
does not. Its exact `/usr/bin/touch` command is allowlisted; the prior `printf`
redirection was refused by Claude's CLI in dontAsk mode before OS dispatch.
Only an observed OS denial
with the target absent gives this check PASS; a CLI-layer refusal is UNKNOWN,
and a file written there is FAIL. The run directory remains denied by both
the OS sandbox and the Claude permission rules.
Other probe writes remain outside that allowlist. Claude's session `TMPDIR` is
shared scratch per UID on macOS; paired-session keeps workflow state outside it
and denies writes to the run directory. Bash sandbox results do not establish
direct `Edit` or `Write` safety for a Claude author.
The sandboxed Claude CLI creates an empty `<workspace>/.claude/.cc-writes`
directory by itself; the Claude author probe admits exactly `.claude` and an
empty `.claude/.cc-writes` (real directories owned by the current uid). Anything
inside, any other `.claude` entry or a link is still an escape. Git-based
workspace guards never see empty directories, so no other check changed.
The Claude author probe prompt states that it is an operator-authorized self-test in a
disposable, probe-owned tree, that a denial is the expected success and must not be
worked around, and asks for each tool result verbatim. The model's status and findings
never decide PASS or FAIL; the filesystem, the exact tool_use accounting and the sentinel
do. If the model makes no prescribed tool call and answers HOLD, the probe is `UNKNOWN`
with reason `author-model-refused` and a message that the model declined to run it.
Read-only Claude roles receive the read tools as one rule and each exact
argument-bearing Bash command as its own `--allowedTools` argument.

The probe records hashes for the effective `$CODEX_HOME/config.toml` (or
`~/.codex/config.toml`) and the two Claude global files. Installed Codex may
persist a trust entry for a new workspace. Only an exact insertion of
`[projects."<this run's workspace>"] trust_level = "trusted"` at a TOML table
boundary is attributed; the report warns `global config mutated by codex CLI
trust persistence`. In a linked worktree Codex trusts the main checkout root of the
workspace's own repository instead; that path is accepted too, and nothing else.
Attribution removes exactly one such block per path (plus at most one blank
line) and the remainder must equal the earlier file byte for byte.
The file is **not** byte-identical in that case. Any other
Codex change fails; Claude plugin `lastUpdated` is attributed separately.
FIELD-22: when two runs share one Codex home (the default `~/.codex` included), the other run's own trust block is
also attributed, as `trusted-concurrent-run-workspace` with that run's id. This applies only when all of these hold:
- that workspace's lease in this user's workspace-lease directory is a private record under its own identity key;
- its run dir's `state.json` names the same workspace and is ACTIVE;
- that run's coordinator lock carries the same pid, and that pid is alive.

Any other addition still fails. This is evidence of another coordinator of the same user, not proof against a
same-UID process, which could edit `config.toml` directly anyway. Sandboxed turns can write neither the lease
directory nor the Codex home. A turn held by such a change before this rule recovers
with a plain `resume`, which re-runs the turn on a fresh baseline. `resume --acknowledge-codex-trust` also accepts
exact trust blocks for a workspace that has such a lease record, including one from a run that has since ended.
The coordinator never edits or restores the user's global config. `--skip-probe`
is accepted only when `FAKE_CODEX_TEST_ROOT` contains this run and both provider
binaries are fake CLI wrappers. This is a misuse guard for tests, not a security boundary.
With a Codex home other than the default, the run also hashes the default `~/.codex/config.toml`: a Codex child that
ignored `CODEX_HOME` would write there (R20-0a). Another process may change that file meanwhile: a Codex run on the
default home appends its workspace trust entry, or an operator restores the file from a backup. A change that only adds
or removes whole `[projects."<path>"] trust_level = "trusted"` tables, for paths other than this run's own workspace,
clone and linked-worktree main root, is recorded as `foreign-default-home-trust-entry`, with the paths, and never
voids a completed turn (P0, 2026-10-06). An uncertain turn (cut off mid-turn) records only hashes, so such a change
during it still holds, as before. Any other change to the default config still holds, because the run cannot tell
it from its own Codex escaping `CODEX_HOME`. A trust entry for this run's own path there holds too. The config the
run actually uses, `$CODEX_HOME/config.toml`, keeps its check unchanged: its model, sandbox or hooks changing still
holds. A run on the default home is unchanged.
Use a separate, absolute `CODEX_HOME` for every run, acceptance and supervisor runs included
([`docs/concurrent-runs.md`](docs/concurrent-runs.md)). A Codex run on the default home writes trust entries into the
default config, and other runs then have to attribute them.
Each Codex turn also records a before/after Codex-config comparison. A changed
config during an uncertain turn HOLDs before replay. After inspecting the
change, the operator can run `resume --acknowledge-codex-trust RUN_ID`; this
records UID, time and before/after hashes and accepts only an exact new trusted
entry for that turn's workspace. Other changes stay HOLD, and the coordinator
never edits the config. `resume --retry-uncertain` still checks the current
permission-probe binding before replay; trust acknowledgement does not bypass it.
Each Codex turn compares only Codex global files; each
Claude turn compares only Claude global files. Cross-vendor changes are recorded
in the turn receipt without HOLDing that turn.

If a run is held, inspect `state.json`, `open-findings.md`,
`findings-ledger.md`, and the latest receipts under `evidence/` before resuming.
Two HOLDs apply in both safety modes: a reviewer, gate or shadow turn that
changes the workspace is void (the workspace is restored and the turn
re-dispatched once; a second change or a failed restore holds), and an author
turn that changes HEAD or the branch holds.
A rate-limit HOLD includes a reset hint when the provider supplies one. Resume
with the same workspace, work item, run directory, author/reviewer settings,
and test command:

`--timeout` (default 2700 seconds, at most 86400; a value outside 1..86400 is
refused before the run starts) governs every non-EXEC-author turn, including
PLAN, persistent/fresh reviewer, shadow and gate turns, and the
coordinator's own test-command runs. The cap is a one-day sanity bound; it sits
above the 14400-second EXEC cap because a general timeout above 14400 is a
supported setting (the EXEC default then stays at 14400). EXEC author turns use the separate `--exec-turn-timeout`; when omitted,
it defaults to `max(7200, --timeout)` capped at 14400 seconds. On resume, only
an explicit CLI `--exec-turn-timeout` may raise the saved EXEC timeout, never
lower it; project-config defaults do not count as an explicit raise. The value
is saved for later turns and resumes. An already-running turn keeps the timeout
it received when it started. When resuming legacy run state without this setting, the coordinator derives it
from `max(7200, saved --timeout)`, capped at 14400 seconds. The existing
`resume --resume-timeout N` option raises the general per-turn timeout up to
7200 seconds for phases that use `--timeout`.

`--wi-deadline SECONDS` (off by default) bounds the whole work item in wall-clock
time, counted from the run's start; HOLDs, operator waits and coordinator
restarts all count. It is checked before every dispatch: once the deadline has
passed, the run HOLDs instead of starting the next turn. A running turn is never
cut short and keeps its own timeout. The value is fixed at `run`: `resume`
keeps the saved deadline and refuses a different one, and the operator actions
keep the saved deadline. The deadline only blocks new dispatches: once it has
passed, `resume` and `permission-probe` are refused without dispatching or
touching the HOLD, so every HOLD keeps its reason and its exits
(`accept --override-rejection` at a round-limit or rejected-tree HOLD,
`note --scope-change`, `abort`). The one exception is an uncertain in-flight
turn: `resume` records it, and `resume --retry-uncertain` (or
`permission-probe --retry-uncertain` for a probe turn) still verifies its
process group is gone and archives it, then HOLDs with the deadline reason
instead of dispatching, so `note --scope-change` works afterwards. A wall clock
that moved back (below) is treated the same way. A `DONE` run past
its deadline stays acceptable: `accept` and `reject --scope-change` work, while
`reject` is refused because its next dispatch could only HOLD. If the wall clock moves back by more than 60 seconds
since the last dispatch, the run HOLDs until the clock is past that time again;
elapsed time is never refunded. A scope-change successor starts without a
deadline; pass the time it may use as its own `--wi-deadline`.

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

Before accepting or rejecting, request an operator intent for the exact action.
It prints the digest and bound run/item, worktree, DONE-approved snapshot, HEAD,
index, state and rejection text hashes; accept refuses a changed approved tree,
and the mutation rechecks the intent under both leases. `resume --polish` was
removed in 3.0.0; the option remains parsed only to refuse it. Lifecycle-off
DONE no longer runs the FIELD-32 final gate recheck; lifecycle-on gate replay
after writes remains in place.

```sh
bin/paired-session accept --intent-only --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN"
bin/paired-session accept --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --expect <digest>
bin/paired-session reject --intent-only --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --text 'Recheck this detail.'
bin/paired-session reject --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --text 'Recheck this detail.' --expect <digest>
```

`accept --reason TEXT` records the operator's acceptance reason; the intent
digest covers it, so give the same `--reason` to `accept --intent-only` and to
`accept`. `accept` refuses `--text` and `--file` (they belong to `reject` and
`note`). `accept` also refuses while a CLI turn is active or uncertain (in the
legacy and the worktree lifecycle alike): once its process group is gone,
settle a probe turn with `permission-probe --retry-uncertain` (a DONE run stays
DONE) and any other turn with `resume --retry-uncertain`, or abort.

A run saved by an older build (review-loop 2.13.x or earlier: state `version` 1)
refuses every run command, `abort` included, and is refused as a `--supersedes` parent, before any of its state is read:
`run was created by an older paired-session build; start a new run, or finish or abort this one with review-loop 2.13.x (for example a pinned copy at ~/paired-runs/review-loop-v2.13.<n>)`.
Such a run is never migrated. `status` and `status --brief` read it without modifying its state. Snapshots keep
counting tracked and untracked non-ignored files; stale refusals report both path
counts and tell the operator to restore the approved tree or start a new run.

Operator rejection, including the tree held at the rejection limit, permanently
records that tree's digest. An unchanged author answer enters `HOLD rejected-tree`
before review. Status includes the author's rationale, truncated to 2,000 characters,
and a pointer to that author receipt. The operator may add `note` guidance, change
the workspace and resume a new author ingest, or explicitly rule on the exact held tree:

```sh
bin/paired-session accept --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" \
  --override-rejection --reason 'I inspected the author rationale and accept this tree.'
```

The override requires `HOLD rejected-tree`, a non-empty reason and an unchanged
held snapshot. It records operator UID/time, reason, digest and rationale pointer
in state, events and acceptance evidence. The same command is the owner's ruling
at a PLAN or EXEC round-limit HOLD (`PLAN round limit reached`, `EXEC round limit
reached`, `EXEC round limit reached after adversarial gate`; RLO, v2.9.5): the
HOLD records its tree, the override needs that HOLD to be the current one (any
later HOLD cause, an operator-rejected tree, a changed tree, an active or
uncertain turn or an empty reason is refused), and `acceptance.json` adds the
recorded `round_limit_hold` and the findings still open (id, severity, source,
security flag, one-line summary). Both leases and role run-dir write denials
apply. This explicit ruling needs no separate intent preview; ordinary accept/reject
still require `--expect`. `ACCEPTED` returns before stale checks. Retry-uncertain with
no receipt follows plain resume; a fresh author ingest is required for rejected trees.

Repeated same-class blocks (FIELD-5, v2.9.7): the EXEC reviewer, shadow and gate
prompts ask for every blocking finding to start with an explicit defect-class
label `[class: kebab-case-name]`; a gate label is kept at the start of the
finding's ledger summary, so the persistent reviewer can reuse it. The
coordinator reads only that label; it never infers a class from the text. A
BLOCK is an EXEC reviewer or gate verdict with new blocking findings. Old
blockers merged into a refused approval, a REVISE without new blockers, a
refused approval and an approval that only routes to the gate neither count nor
break the run; an approval that ends the review (a gate without blockers, or a
reviewer approval with no gate to follow) breaks it. When one class appears in
each of three consecutive BLOCKs, the run HOLDs `structural fix / re-scope
needed: finding class ...` with the finding ids per verdict, after routing the
next turn to the author; the round-limit HOLD keeps precedence and its override.
The HOLD closes no finding and never accepts. Use `note` with a structural plan
and `resume`, `note --scope-change`, or `abort`; after the HOLD the count starts
afresh, so the same class HOLDs again only after three more BLOCKs. The history
is in `block_class_events` and `structural_holds` (written only when the HOLD
happens) in state.json.
Blocking findings without a label never HOLD; they are counted in
`unlabeled_blocking_findings` (run total, and per reviewer/gate turn receipt) so a
reviewer that never labels is visible.

An idle `HOLD` run waiting for its next author turn accepts an in-scope
clarification with `note --text '...'` or `note --file /path/to/note`. It reaches
that author turn on `resume`; a newer note replaces a pending one. Notes are
refused while waiting for a reviewer/gate and on a
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

When the author's sandbox cannot run a check (for example xcodegen, XCTest or
CoreSimulator under a Claude author), the operator can run it on the host and
attach the result to an idle ACTIVE, HOLD or DONE run (OPV, v2.9.5; at DONE
before `accept`):

```sh
bin/paired-session attach-verification --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" \
  --command 'xcodebuild test -scheme App' --exit-code 0 --log /tmp/xcodebuild.log \
  --log-sha256 "$(shasum -a 256 /tmp/xcodebuild.log | cut -d' ' -f1)" --note 'ran on the host simulator'
```

The record (command, cwd inside the workspace, exit code, log sha256, time,
operator UID and a non-empty note) is bound to the current workspace snapshot
digest; the log, which must lie outside the workspace and run dir and match
`--log-sha256`, is copied into `evidence/operator-verification-V<n>.log`. The
EXEC reviewer, shadow and gate prompts show the command, cwd, exit
code, log hash and the last 2,000 log characters (not the note) as
operator-verified evidence for this exact tree. It is voided for good once the
snapshot differs (an operator edit or an author turn in the workspace) or the
log copy changes. Every tree the coordinator observes in an author turn counts,
also when the turn fails: its start and end snapshots, and an unknown tree when
the CLI ran but no end snapshot exists (also after the coordinator itself was
killed during an author turn: the next command that finds the interrupted turn
voids it). The snapshot covers tracked
and untracked non-ignored files, so a change to an ignored file does not void it. The persistent reviewer, whose
thread saw a record, is told in its next prompt that it was withdrawn (id and
reason only). `accept` lists the records still current for the accepted tree in
`acceptance.json` and on stdout. Attaching is refused while a turn is active, on
ACCEPTED, or when the shown text would fail the fresh-role history scan. It is
evidence only: no verdict is derived from it.

Each run now records an item UUID. A successor inherits it and copies the
parent's OPEN blocking findings into its protected successor spec and state,
with their original run/ID provenance. These records do not enter fresh-role
prompts or change the legacy reviewer gate. A legacy successor spec lacking
the item fields is marked `item_blockers_complete=false`; it cannot later be
treated as verified lifecycle handoff evidence.

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
listed in the repository backlog. The legacy implementation was removed in v2.13.0 (routing) and
v2.13.1 (files).

`test_real_coordinator.py` is a deterministic fake-CLI suite. It verifies
protocol transitions and permissions-command construction; the runtime
permission probe is the effective check against the installed Codex CLI.

### Codex author sandbox: what a work item cannot do

A Codex author runs under the `workspace-write` sandbox (writes confined to the
workspace and the run-owned temp dir; `network_access` is off). Real runs on
this machine (poker-tools, 2026-10-01) showed three things a work item cannot do
there:

- Postgres `initdb` fails (`shmget` returns EPERM), so a test database cannot be
  created inside the sandbox.
- Unix-socket connections to a server running outside the sandbox fail; they
  work only with network access enabled, which the author policy keeps off.
- iOS builds and the simulator are unavailable: the Swift macro plugin server
  answers "malformed response" under the nested sandbox, and the connection to
  CoreSimulatorService is refused.

A work item that needs one of these will stall or fail in the author turn, not
in the permission probe. Options for the operator:

1. Pick a Claude author (`--author-vendor claude`) for that item. The default
   efficient mode needs no probe for it. In strict mode it needs a
   passing Claude-author permission-probe, or (without lifecycle, D-7) the documented
   `--accept-unverified-claude-author --reason` opt-in, which is the operator's
   own decision (see the probe section above). The opt-in waives only the
   Claude author's probe part: a report that is UNKNOWN solely because the author
   probe could not prove the sandbox (author-model-escape-unknown or
   author-model-refused) then passes the run/resume/reject gate, while a reviewer
   or gate probe failure, a config change or any escape still blocks and
   `--accept-probe-skip` is still refused for them. Claude Code's auto mode
   blocks a `run --accept-unverified-claude-author` command as "Create Unsafe
   Agents", so the owner launches such a run by hand in a terminal; with a
   passing Claude author probe (poker-tools N4 run-02) the opt-in is not needed.
2. Split out the step that needs the capability and keep the rest in the work item.
3. Run that step outside paired-session, by hand, and feed the result back as
   ordinary workspace content.

Do not loosen the sandbox to make such an item pass; the probe PASS and the
safety rows in `docs/1c-safety-controls.md` are bound to the sandbox as probed.

### Removed: the fake lifecycle

The offline fake lifecycle (the candidate-tree Q, delivery publication and closeout harness, with its modules `delivery_*`, `closeout_*`, `q_proposal`, `q_evidence`, `candidate_test_sandbox`, `finish_dispatch`, `security_repair_policy`, `hook_inventory_*` and `safe_temp`) was removed in V3-B4 (ADR-17 V4); the worktree lifecycle never used it.
