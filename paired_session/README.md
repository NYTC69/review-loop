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

For simultaneous lanes, read [Concurrent runs and isolated Codex homes](docs/concurrent-runs.md).

Use a dedicated Git worktree for each product task and keep run artifacts in a
sibling directory outside the workspace. Never put `--run-dir` inside the
workspace: the author can write to the workspace and must not be able to alter
coordinator state. The workspace must be a Git worktree and the work item must
be a file. The configured test command is checked before the coordinator starts.

For operator-selected programs, role/vendor settings and test commands, copy
`paired-session-config.example.json` to an operator-owned path outside the
workspace, run directory and author temp directory, then pass it with
`--config /absolute/path/to/profile.json`. A workspace
`.review-loop/paired-session.json` may hold limits and other non-program
settings, but program/role/test-command keys there cause HOLD when that profile
is selected. `--config` replaces the workspace profile; copy any desired limits
into the external profile because the two files are not layered. The CLI loads the selected profile for run, probe,
and resume; explicit CLI options override it. On resume, effective settings
must still match the saved run configuration. A changed binary or PATH requires
a fresh permission probe before the run can continue.
Role models are operator-set (ADR-9): a role without `--author-model`,
`--reviewer-model`, `--gate-model` or a profile value gets its vendor's default
(Claude: `claude-opus-5-5`; Codex: `gpt-6-luna`). Before run state is created,
every model id must be well formed and, when `allowed_models` is set, listed for
that role's vendor. The Step 3.4 gate defaults to the author's vendor (ADR-10);
`--gate-vendor` overrides it and is recorded as `gate_vendor_source: operator`,
and a `--gate-model` of the other vendor without `--gate-vendor` is refused.
`lifecycle_mode` defaults to `off`. The frozen config also records exact
`docs_file`/`docs_allowlist` paths, `skip_globs` and `skip_quality_polish`;
outside-workspace or wildcard doc paths are refused. `lifecycle_mode=on` is
currently refused before any model dispatch, including resume of a saved
lifecycle state: FINISH through CLOSE and their isolation checks are not yet
implemented. Legacy DONE/ACCEPTED, gate-off and `resume --polish` cannot enter
the incomplete lifecycle. No real lifecycle run is enabled by these fields.

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
of metadata writes before any real lifecycle activation.
It does not dispatch writers or prove installed OS sandboxing. Before any live
caller may use ingest, it must positively stop the writer process group and
deny that writer OS access to the scratch Git directory and index. Same-device
candidates remain ineligible for activation.

The disabled `security_repair_policy` helper checks a proposed SECURITY
repair against the frozen legacy ignore-pattern table and a caller-supplied
candidate-file inventory. Its `SecurityRepair` result lists exact fixer paths,
approved ignore patterns, any operator-consent digest, and an EXEC replay
requirement. It does not authorize a writer by itself. A future dispatcher must
derive the file inventory from the current candidate OID, prove each grant is
an exact no-follow file leaf, and enforce the observed write boundary: every
SECURITY OID change replays EXEC and the gate even if no repair was planned.
It must verify appended `.gitignore` lines against `approved_patterns`, route
`ReservedDocsRepair` to the replayed DOCS writer, and require both a fresh
SECURITY review and disposition by the original finding owner on the new OID.

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
bound into the reviewer, gate and author flags digests. The guard still reports
cached bundles unless the effective `$CODEX_HOME/config.toml` itself sets
`[features] plugins = false`, so a ChatGPT default home (`chatgpt-global`,
`openai-curated-remote`) HOLDs. Recipe for a dedicated `CODEX_HOME`:
`[features]` with `remote_plugin = false`, `plugins = false`, `apps = false`.
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
The coordinator never edits or restores the user's global config. `--skip-probe`
is accepted only when `FAKE_CODEX_TEST_ROOT` contains this run and both provider
binaries are fake CLI wrappers. This is a misuse guard for tests, not a security boundary.
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

Before accepting or rejecting, request an operator intent for the exact action.
It prints the digest and bound run/item, worktree, DONE-approved snapshot, HEAD,
index, state and rejection text hashes; accept refuses a changed approved tree,
and the mutation rechecks the intent under both leases. `resume --polish` also
refuses when the workspace no longer matches the snapshot approved at DONE.

```sh
bin/paired-session accept --intent-only --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN"
bin/paired-session accept --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --expect <digest>
bin/paired-session reject --intent-only --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --text 'Recheck this detail.'
bin/paired-session reject --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --text 'Recheck this detail.' --expect <digest>
```

Runs created without the acceptance snapshot and rejected-digest fields refuse
mutating commands: `run was created by an older paired-session build; start a new run`.
`status` reads such a run without migrating or modifying its state. Snapshots keep
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
in state, events and acceptance evidence. Both leases and role run-dir write denials
apply. This explicit ruling needs no separate intent preview; ordinary accept/reject
still require `--expect`. `ACCEPTED` returns before stale checks. Retry-uncertain with
no receipt follows plain resume; a fresh author ingest is required for rejected trees.

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
Each run now records an item UUID. A successor inherits it and copies the
parent's OPEN blocking findings into its protected successor spec and state,
with their original run/ID provenance. These records do not enter fresh-role
prompts or change the legacy reviewer gate. A legacy successor spec lacking
the item fields is marked `item_blockers_complete=false`; it cannot later be
treated as verified lifecycle handoff evidence. Lifecycle dispatch remains off.

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

1. Pick a Claude author (`--author-vendor claude`) for that item. It needs a
   passing Claude-author permission-probe, or the documented
   `--accept-unverified-claude-author --reason` opt-in, which is the operator's
   own decision (see the probe section above). The opt-in waives only the
   Claude author's probe part: a report that is UNKNOWN solely because the author
   probe could not prove the sandbox (author-model-escape-unknown or
   author-model-refused) then passes the run/resume/reject gate, while a reviewer
   or gate probe failure, a config change or any escape still blocks and
   `--accept-probe-skip` is still refused for them.
2. Split out the step that needs the capability and keep the rest in the work item.
3. Run that step outside paired-session, by hand, and feed the result back as
   ordinary workspace content.

Do not loosen the sandbox to make such an item pass; the probe PASS and the
safety rows in `docs/1c-safety-controls.md` are bound to the sandbox as probed.

### Fake closeout item admission (offline only)

`fake_lifecycle_drive(backlog_item=N)` may freeze an item from a Compass view generated
within ten minutes. The view must name this repo-root tracked `BACKLOG.md`, contain
one open item with that ID, and match its unique normalized title and section.
Workspace/index drift, ambiguous items and symlink paths refuse before PLAN.
The run records the exact HEAD, BACKLOG blob and hashes of the view and adapter.
This admission does not close an item: Q construction, fresh Q checks and DELIVERY
remain required. Real lifecycle entry remains disabled.

`closeout_adapter.close_blob` produces an **unreviewed Q proposal** from the exact
frozen BACKLOG bytes, a C1 object ID and closing date. It moves only the selected
item and its children, restores an empty source sentinel, updates Last updated
and retains the newest five Done blocks. It writes no file and supplies no
approval. Q still needs isolated materialization and fresh review/test/SECURITY
receipts before accept or delivery. Closeout intake is write-once at a fresh
lifecycle parent, and BACKLOG is excluded from the declared writer grants.

`fake_materialize_q(c1, day)` is an offline object proposal after the P SECURITY
pass. It checks the P tree, C1 parent/tree and current adapter hash, rejects
non-child body text or a missing final newline, and uses a temporary index to
write a BACKLOG-only Q tree in scratch Git. Its status is always UNREVIEWED.
It changes neither live HEAD/index/BACKLOG, the P root/index, nor lifecycle state.
Fresh Q tests/reviews/gate/SECURITY and attributed acceptance remain mandatory;
this method cannot commit, publish or close. C1 message/author/intent verification
belongs to the later bundle-verification step. Real lifecycle still refuses.

The fake Q source reviewer preserves its raw verdict in the turn and records an
independent effective verdict/advisory proof. Nonempty REVISE with only
non-security MINOR/LOW uses the existing advisory rule; empty REVISE, major and
security findings refuse. This source remains UNREVIEWED until fresh Q bundle
checks; proof advisories are copied, not shared with the mutable answer list.

### Fake-only Q receipt bundle

The offline lifecycle harness retains Q's proposal/test/reviewer source as
`UNREVIEWED`. `fake_q_complete` dispatches separate gate, final and SECURITY
reviews at the same Q OID, requires observed configured-test success and binds
phase, workspace and increasing turn sequences. P FINISH/POLISH-Q/DOCS receipts
must be current no-ops. A distinct protected `REVIEWED` bundle is evidence for
future delivery; it does not itself publish commits or CLOSE. A failed or
uncertain Q attempt requires abort and a new run. Real lifecycle activation
remains disabled; sandbox/cache/process-isolation gates are still required.

Non-security reviewer MINOR/LOW and gate low findings are retained in Q proof
advisories. Their raw verdict stays in the provider receipt; only the coordinator
classifies them as nonblocking. Major/security and gate medium-or-higher findings
refuse the bundle. This follows P SECURITY's blocking set; no advisory authorizes
real activation or bypasses tests/tree binding.

Q final/SECURITY and source APPROVE or nonempty REVISE with only non-security
MINOR/LOW findings use APPROVE_WITH_ADVISORY. Empty REVISE remains blocked.
Q source and bundle advisories stay OPEN in the finding ledger before acceptance.

The fake DELIVERY intake re-reads protected Q bundle and provider turn receipts,
current program hashes, source tests, Q tree and P no-op receipts before any
Git write. A pending, relabeled, later-turn or failed-test proof refuses intake.

Fake delivery prepares a deterministic unpublished C2 (parent C1, tree Q),
with signing/hooks disabled, and an intent digest binding objects, Q proofs,
HEAD/index/live snapshot and operator provenance. `accept --expect` must match
that digest; this acceptance publishes no ref. CAS/reconciliation is a later step.

Preparation permits only the normal fake-router HOLD (or existing DONE/ACCEPTED);
other HOLDs/terminal states refuse. Delivery accept requires DONE/PENDING before
rechecking intent. Fake delivery rejection is explicitly refused until P'/Q'
recovery is wired; use abort/new run or an explicit scope-change instead.

Fake delivery seals Git hook/config inventory before PLAN and binds it into the operator intent. Active hooks refuse until the hook runner exists. C1 has a fixed coordinator author, committer, start time and item message; delivery refuses metadata or inventory drift. Real lifecycle remains disabled.

Fake publication uses a protected acceptance journal. Its PREPARED/PUBLISHED phases are incomplete delivery states, not CLOSE receipts. Post-CAS verification checks frozen proof files, current programs, exact candidate bytes and the C1/C2 chain without assuming the old HEAD; final live-index reconciliation is a separate required check.

Fake publication imports C1/C2 through `index-pack --strict`, records the journal before the single old-value CAS, and keeps the live index lock while checking out through an alternate index and replacing the live index. Final verification compares read-only index entries to Q (it cannot run `write-tree` while holding that same lock). Publication errors record a digest-bound HOLD; post-CAS replay is a separate required recovery step.

A publication journal or structured publication HOLD quarantines ordinary operator commands (including scope-change, probe, note, accept and resume). Read-only status remains available. Use the locked publication recovery path; pending journal phases are not acceptance or CLOSE.

Fake-only Python drive helpers now prepare reviewed delivery, require explicit operator acceptance, reconcile sealed publication, and produce an idempotent CLOSE receipt with C1/C2 and exact Q facts. The public CLI still refuses real lifecycle activation; external actions are unavailable, including explicit true requests. Recovery releases ordinary-command quarantine only after exact reconciliation and lock removal. Mid-stage resume and full PLAN-to-close fault coverage remain acceptance gates.

Fake OID and Q tests require the macOS OS write sandbox; unavailable isolation refuses dispatch. Writes are limited to the candidate root and a fresh test temporary directory, excluding candidate Git/Compass/BACKLOG metadata. `/dev/null` permits data writes for the system Python launcher. Network and hardlink creation are denied; receipts record the sandbox engine, profile and roots. Real activation remains refused. An initial Q reviewer test failure is not erased by a later pass.
