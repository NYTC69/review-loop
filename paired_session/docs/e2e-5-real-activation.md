# Real lifecycle activation gates

## Scope and authority

This is the activation plan for the lifecycle in docs 1–4, not permission to activate it.
Round 47 permits offline/fake work only. The real-provider refusal and fake-only double lock remain.
The operator delegate prepares evidence and gate disposition; activation requires recorded owner authorization.
A provider role owns none of these actions.
A fake receipt, simulated separation, passing unit test or renamed status is not real-provider evidence.
Existing M3/M4 acceptance proves coordinator flow, not the OS isolation of real actors.

## Evidence contract

Each gate starts CLOSED; BLOCKED names an unmet dependency, UNKNOWN an inconclusive observation.
Every record has evidence_scope=component or provider-bound; component evidence never verifies a provider gate.
A VERIFIED record names gate/schema version, run/item, implementation SHA,
OS/runtime/interpreter versions, program/config/policy hashes, exact roots and their device/inode identity,
executed positive/negative controls, exits, observations, receipt hashes and the independent review.
The coordinator computes these fields; malformed, absent, stale, simulated or mismatched evidence closes the gate.
Evidence is stored outside every tested actor's writable roots. No role may supply its own VERIFIED record.
Offline verification can establish a component contract only at the tested environment/configuration.
Provider-bound gates additionally require an actual installed provider turn using the intended launch path.
An activation aggregator must consume all current component and provider evidence and fail closed on drift.
That aggregator is a later owner batch; Round 47 never changes the unconditional real-lifecycle refusal.
Any recorded hash, identity or version change invalidates that record and all dependent gates deterministically.
VERIFIED never substitutes for per-run/per-dispatch checks, including teardown and hook VM firewall rechecks.

## A1–A5: threats, evidence and steps

### A1 — A-author-sandbox (doc 2a, Real activation gates)

Threat: any writer (EXEC, FINISH, POLISH-Q, DOCS or SECURITY repair) reads or writes unauthorized roots,
or an old/detached writer changes a rebuilt root or a fresh review checkout after approval.
Evidence: every configured writer launch positively writes its granted candidate paths and private temp,
but OS-denies live workspace reads/writes, run_dir, credentials, peer checkout, authoritative .git, global
configuration, ambient CODEX_HOME/provider HOME and socket/launcher access outside its explicit grants;
old-session and detached-child controls cannot write a new root after stop, including after coordinator death.
A skipped attempt or CLI permission refusal is UNKNOWN. Credential contents never enter evidence.
Classification: needs real provider turns plus offline containment controls; shell-only probes are insufficient.
Small step/test: define one actor-isolation launch contract, then test forbidden roots and detached writers
with an executed local adversary; follow with separately authorized installed-provider denial turns.
Dependencies: A3, A4/A5 root contracts, whole-process containment and fresh-role blindness from doc 2b-ii.
Outbound push by a networked credentialed writer remains the explicit D8 residual risk, not a passed control.

### A2 — A-candidate-rebuild (doc 2a, author ingest and reconstruction)

Threat: crash recovery mixes user workspace changes, stale writer output or a partial reconstruction into the OID.
Evidence: killed coordinator/writer at every ingest/rebuild boundary; restart reconstructs from a verified
scratch commit and coordinator manifest, chains receipt IDs once, and leaves live workspace/index unchanged.
Classification: reconstruction arithmetic is offline; real-process orphan revocation requires A1 evidence.
Small step/test: persist rebuild intent and scratch OID, reconstruct a new root without reusing mutable caches,
then SIGKILL/restart with foreign workspace edits and prove exact manifest and single ingest.
Dependencies: A1 writer revocation, A3 placement, A5 no-follow/separation; doc 2a ingest receipt chain.
Until all dependencies verify, a local reconstruction test is component evidence, not A2 VERIFIED.

### A3 — A-role-tmp (doc 2a, Real activation gates)

Threat: writable temp/schema/context or test caches alias run state, peer evidence or candidate content.
Evidence: actual coordinator dispatch uses per-role private temp outside run_dir, live workspace and candidate;
author schema/context are coordinator-created read-only transport copies outside run_dir, not peer history;
OID/Q tests put TMPDIR, bytecode and tool caches outside candidate, run_dir and evidence, with no links.
Classification: coordinator placement/transport wiring is offline component evidence; installed provider
compliance with TMPDIR/schema/context/HOME/session/cache placement needs real provider turns under A1.
A3 records placement VERIFIED only with evidence_scope=component; the provider-bound A3 gate stays CLOSED
until compliance evidence exists. The activation aggregator requires both records, not the component label.
Small step/test: introduce a coordinator-owned role-root allocator and receipts, then wire author dispatch
and candidate tests; test both provider argv forms, replay/drift, cache writes and invalid path identities.
Allocate under an operator-owned parent, with exclusive creation, mode 0700 and no-follow validation.
Transport roots are read-only to roles; separate writable author temp from schema/context and peer roots.
Resume validates saved root identity rather than accepting a caller-selected replacement.
The placement component becomes VERIFIED only after all stated wiring tests and independent review pass.
A placement record never proves A1 read/write denial or permits real activation.
Dependencies: safe cleanup (TMP below); transport isolation must not modify lane B's protected builders.
If required builder/probe wiring is reserved, record BLOCKED and leave A3 CLOSED; do not verify a helper alone.

### A4 — A-oid-tests (doc 2a §4; R46-QS)

Threat: candidate tests escape, forge a boundary receipt, inherit stdin/capabilities or continue writing afterward.
Evidence: executed test writes allowed root/temp but cannot write live workspace/ref, run state, BACKLOG or
Compass; consuming test/Q proofs validates engine/policy/root/identity against coordinator launch evidence;
stdin EOF and excluded socket/env controls execute; detached descendants cannot write after completion.
Classification: local test subprocess isolation and receipt validation are offline; provider-launch coupling
and reused actor isolation need the A1 real-provider evidence before overall activation.
Small step/test: harden the module-local root guard, close stdin, move temp/caches (A3), validate boundary
receipts, then adversarial detached-child/watchdog tests. Existing sandbox-exec coverage is partial evidence.
Dependencies: TMP, A3, A5; whole-process containment must precede VERIFIED, not merely killpg success.

### A5 — A-filesystem (doc 2a separation; doc 4 authoritative Git state)

Threat: symlink, hardlink, mount alias or check-to-write race lets a role modify authoritative Git/state bytes.
Evidence: realpath/device/inode/no-follow controls prove candidate/scratch Git/index/live workspace/run
separation and deny links to protected data; concurrent swap adversaries cannot redirect coordinator writes.
Classification: local filesystem controls are offline on the actual OS/filesystem; actor denial depends on A1.
Prove cross-root link() returns EXDEV on independent filesystems (docs 2a/4); otherwise HOLD, not simulation.
Small step/test: validate both containment directions and alias identities, then replace path checks followed
by writes with descriptor-relative operations or a proven exclusive boundary; exercise repeated path swaps.
Dependencies: TMP, A3 and publication leases/journals. A last-minute path hash alone cannot satisfy A5.

## Additional carried activation constraints

| Item / source | Threat | Exact evidence / class | Small implementation and test | Dependencies |
|---|---|---|---|---|
| TMP / QS R1 MEDIUM, R45–46 | Python 3.9.6 resetperms follows a planted symlink. | Local outside sentinel bytes/mode/flags survive cleanup of linked files/directories, including error path; offline. | Replace coordinator temp cleanup with no-follow, fd-relative deletion that refuses unknown identity; error controls prove no chmod/chflags of link targets. | None; prerequisite to A3/A5. |
| Detached descendants / QS LOW | setsid child outlives killpg and writes after verification. | Executed detached/double-fork controls after timeout and coordinator death show no surviving writable actor; local containment plus A1 provider evidence. | Introduce enforced actor lifetime/capability revocation; uncertain teardown HOLDs. | A1/A4; hook runner doc 4b remains a separate unresolved prerequisite. |
| write_boundary / QS LOW | Saved engine/profile label substitutes for OS evidence. | Tampered engine, policy, root, tmp identity and receipt replay each refuse; offline. | Consume coordinator-derived boundary evidence at every OID/Q receipt boundary. | A3/A4/A5. |
| stdin/temp / QS LOW | Inherited stdin or shared temp conveys operator input or writable state. | EOF and exclusive transport/cache controls execute in actual dispatch; offline placement, A1 provider isolation later. | DEVNULL stdin and A3 private roots, reject shared or replaced roots. | A3/A4. |
| Module-local root guard / QS LOW | Missing test-root env or reverse containment authorizes a forbidden root. | Missing, slash/home, equal/ancestor/alias roots and real wrappers refuse before spawn; offline. | Validate scoped root and both containment directions in the module itself. | A4/A5. |
| CLOSED generic hold / R46 | A later generic failure changes a completed run to HOLD or repeats actions. | Every mutating command and injected generic failure preserves CLOSED and receipts, zero provider/CAS calls; offline. | Make terminal handling explicit without bypassing close replay verification. | Existing CLOSE proof/journal. |
| Last check-to-write / R46 recovery | Foreign bytes replace a validated path before coordinator recovery writes. | Synchronized swap between validation and write refuses or writes only the held inode; offline on actual filesystem. | FD-based no-follow identity-bound mutation; unknown/foreign bytes HOLD with inspect/recover or abort. | A5 and publication lock ownership. |
| Pending dispatch window / R46 | Restart repeats an uncertain invocation or treats its output as completed. | Crash before/after each persisted dispatch/receipt; exactly once consumption or named abort/new-run HOLD; offline and A1 containment. | Preserve existing conservative abort/new-run policy until owner recovery is implemented. | Budgets, receipts, A1. |
| Fresh reviewer isolation / docs 1/2b-ii | Reviewer reads peers or writes ledger/state. | Installed turns OS-deny peer history/run state reads and all unauthorized writes; audit actual input/tools; provider-bound. | Isolated read-only role launch and positive/negative controls for reviewer, shadow, gate and every specialist reviewer. | A1/A3/A5, no prompt-only proof. |
| Hook isolation / doc 4b | Inventoried hook escapes, survives or changes candidate/message. | Runtime/image/firewall identity and executed OS probes from doc 4b; offline runner controls plus authorized installed activation probes. | Implement reviewed hook runner steps; active unsupported hooks continue HOLDing. | A1/A5, hook receipt/final-gate wiring and batch-8 provenance. |

## Ordered implementation and activation plan

1. TMP safe local cleanup; test link/error controls, no gate promotion.
2. A3 private root allocation, transport/cache wiring and identity validation; verify only full placement evidence.
3. A4 root guard/stdin and write-boundary receipt consumer; keep actor-lifetime requirement CLOSED.
4. CLOSED terminal handling and A5 recovery fd-bound mutations, as separate reviewed batches.
5. A2 crash-safe rebuild; A5 separation/alias controls; hook runner implementation and receipt integration.
6. A1/A4 enforced whole-actor containment, including coordinator death and old writer revocation.
7. Separately authorized installed-provider probes and M6 re-check for every writer/fresh role and A3 compliance.
   Lane B owns the separate probe entry and reserved builders; it cannot advance lifecycle or emit stage receipts.
   Probe and intended production launch share an audited argv/env/policy construction with matching hashes;
   absent equivalence evidence refuses gate verification. Probes do not bypass the real-lifecycle refusal.
8. Separate activation-aggregator review consumes every gate and hook/blindness prerequisite; missing/UNKNOWN
or residual evidence requiring an owner decision refuses activation. No implementation in Round 47 opens it.

Each step plans about 60 product lines; split before review, trace reviewer fixes, and stop above 150 total.
Offline status is never relabeled into provider proof. No step bypasses current tree tests/review/security,
operator intent, publication journal, close receipt, or the default-off external delivery switch.
