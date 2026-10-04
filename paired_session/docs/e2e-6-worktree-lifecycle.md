# E2E lifecycle design 6: worktree lifecycle W (D12 legacy parity)

Status: decided design (ADR-11), **partly implemented**: W1a accepts `lifecycle_mode=on` on the real path;
W1b runs FINISH; W2a runs the POLISH-Q specialists and their fix leg (the simplifier and test-consolidation
writers are not implemented and not scheduled in W2a); W2b runs DOCS (writer, docs review with an observed
test); W3a runs SECURITY (scans and a fresh security reviewer) and reaches DONE (acceptance pending);
W3b-1 accepts a W DONE without a commit (`auto_commit` false) and reject reopens EXEC; the auto_commit
local commit arrives in W3b-2. Sources: ADR-11, the lane A
legacy-parity map (supervisor-accepted 2026-10-03; kept in the lane A run notes), legacy
`docs/protocol/execution.md` Step 3.4–Step 4, [doc 1](e2e-1-stages-and-roles.md),
[doc 3](e2e-3-docs-security.md), [doc 4](e2e-4-delivery-close.md). D8 (legacy parity of the trust
model): the driving agent is the operator's delegate, accept/reject are plain CLI actions with
provenance, delivery is a local commit only, and external delivery stays behind an off-by-default switch.

## Scope

The real lifecycle will open under exactly the safety bar already accepted for real EXEC: a permission
probe PASS bound to the author, reviewer and gate flags, the sandboxed author in the dedicated live
worktree, fresh read-only reviewers, and the Step 3.4 gate on. FINISH, POLISH-Q, DOCS and SECURITY run
as further turns of the real drive loop **in the same worktree** with the same roles, flags and probe.
They do not use the candidate-tree route of docs 2a–4. That route, with Q, sealed publication and
Compass CLOSE, stays fake-only as post-v2.10.0 hardening (see `e2e-5-real-activation.md`).

## Receipts and transitions

Stage receipts reuse the `lifecycle_spine.begin/complete` bookkeeping, with `candidate_oid` set to the
`git_snapshot(workspace)` digest before the stage and `output_oid` to the digest after it. Stage
transitions are owned by the W router, not by `lifecycle_spine.advance()`: `advance()` restarts EXEC on
any digest change and maps DOCS/SECURITY to the fake `STOP_BEFORE_*` stages, which fits neither the
allowlisted DOCS write nor the W stage order. A code-invalidating write starts a new EXEC convergence
(normal reviewer, `gate_ran=False`, gate again); an allowlisted DOCS write that is disjoint from the
EXEC-reviewed change set advances to SECURITY on its new digest after the docs review and retest. Any
change to `lifecycle_spine.py` itself needs separate authorization, because the fake candidate-tree route
depends on it.

## Doc 1 rules: adopted or replaced

- Adopted: the stage order; coordinator-owned dispatch, evidence and stage advance; the tool-use guard;
  the blocking taxonomy (MEDIUM blocks); one gate per EXEC convergence, gate SKIP only for
  `skipped-by-config`; the finisher and other writers as separate fresh sessions of the author role.
- Adopted (doc 1 §Writes): `docs_file` and the docs allowlist are reserved for DOCS. If the EXEC-reviewed
  change set already touches an allowlisted path, W HOLDs before DOCS instead of replaying, so DOCS can
  never start a replay loop. Implemented (W2b-1): the EXEC author and finisher prompts name the reserved
  paths, and the check sees deletions, staged deletions and renames. One exception: an allowlisted file that DOCS has written (`lifecycle.docs_owned`, accumulated when DOCS
  routes to an EXEC replay or to SECURITY) is DOCS's own entry. The EXEC author is told it may edit it only
  to fix a delivered docs finding, the entry check lets it through, and every later DOCS reviews it again;
  DOCS may rewrite it ("a replayed DOCS stage replaces its own entry"). Any other touched allowlisted path
  leaves only abort, or a rerun with that path outside `--docs-file`/`--docs-allowlist`.
- Adopted (doc 1 §Gate): with lifecycle on, EXEC convergence routes to FINISH and never enters the legacy
  `start_polish_or_done` advisory round. Legacy `--polish-round on` maps to POLISH-Q; open advisory
  findings feed POLISH-Q. Implemented deviation (W2a-2): the fix leg reuses the persistent EXEC author turn
  with the open specialist blockers as its delivered review; the owning specialists then re-review on the
  new tree (only an owner closes its finding), and the write replays EXEC review and gate, then FINISH and
  POLISH-Q in the next epoch. A blocker left after the re-review HOLDs; resume runs another fix round, and
  a fix that leaves the tree unchanged since the last review HOLDs. Specialist counts are per epoch
  (legacy 3.5.2 caps one Step 3.5 run): 4 per specialist with room for two dispatches, so an owner gets
  its review plus up to two re-reviews per epoch, checked before the author writes. POLISH-Q stays capped
  at 32 calls per run, and every fix is an EXEC author round under the EXEC round limit.
- Adopted (W2a-2): blocking findings still open at EXEC convergence start an EXEC repair round (author,
  reviewer, a new gate) up to the EXEC round limit, which HOLDs with the RLO-capable round-limit reason.
- Replaced: candidate materialization and OS separation (docs 2a–4) by the live worktree; the same-vendor
  rejection by ADR-11 D-8; Compass CLOSE by no close (ADR-11 D-3).

## Stages

| Stage | Dispatch (real path) | Writes | Exit / HOLD | Legacy step matched |
|---|---|---|---|---|
| FINISH | fresh author session, same author flags | worktree | unchanged digest → POLISH-Q; changed → EXEC replay | Step 3.4 spent; executor readiness |
| POLISH-Q | fresh reviewer turns per detected language plus code/silent-failure/test reviewers, inlined `agents/*.md` bodies in the frozen role manifest; fixes via the polish author/reviewer turn implementations; one simplifier pass and test consolidation by fresh author sessions | fix, simplify and test writers only | open specialist blocker, zero tool use after one retry, cap → HOLD; any write → EXEC replay; `skip_quality_polish: true` → no-op receipt | Step 3.5 (static-analysis fix 2, code review 3, simplify 1, tests 2 rounds), tool-use guard |
| DOCS | fresh author session as docs writer; fresh docs reviewer over the full diff with an observed test run | frozen `docs_allowlist` (W default `docs_file` = `CHANGELOG.md` since W2b-1, off with an explicit `--docs-file ''` or `"docs_file": ""` in a profile; `paired-session-config.example.json` leaves the key out so the default applies; the fake lifecycle keeps `''`; W docs paths must be exact documentation files outside the HOLD set, reached without symlinks) | HOLD set (below) → HOLD; any other path outside the allowlist (source, tests, comments) → EXEC replay; missing retest → HOLD | Step 3.6; see the `docs_file` entry below |
| SECURITY | coordinator `sensitive_policy` path scan and `scripts/security_preflight.py`; fresh security reviewer | none | any sensitive path, any preflight result other than a clean exit 0, any open security-reviewer finding (any severity) or any open blocking finding → HOLD (no automatic repair, no `.gitignore` edit); a no-op run still scans; tree change during the stage → HOLD; `BUDGET_CAPS['SECURITY']` (3 reviews per run) | Step 3.7 scans; the reviewer follows the parity map W3a |
| DONE | after SECURITY only | — | acceptance pending | Delivery gate |
| DELIVERY | operator `accept --expect <digest>` from W DONE only (existing intent: tree, HEAD, index, state) | `auto_commit: false` (default): none. `auto_commit: true`: one hook-free local commit of the accepted manifest; drift → HOLD | external push/PR/merge refused (D8) | Step 4; differences from W04 below |

**DOCS HOLD set.** A DOCS write to any of these HOLDs even if listed: `AGENTS.md`, `CLAUDE.md`,
`agents/**`, `skills/**`, `.claude/**`, `docs/protocol/**`, `.review-loop/*`, config and manifest files,
`.gitignore`, `.gitattributes`, `.git`, and symlink escapes. Implemented (W2b-1, `docs_denied`): any path
with a `docs_policy.PROTECTED_PARTS` name at any depth (case-insensitive; this adds `.env`, `.npmrc`,
`.netrc`, `.pypirc`, `.gitmodules`, `.mailmap`, `.compass`, `Makefile` and `Dockerfile`),
`.claude-plugin`/`.codex-plugin`, `plugin.json`, `marketplace.json`, `docs/protocol/**`, and every symlink
write. Other config and build files (`package.json`, `pyproject.toml`, lockfiles, `.github/**`) are not in
the set: like code, a DOCS write to them replays EXEC review and gate.

**DOCS review (W2b-2).** When the writer changed only allowlisted paths, or DOCS owns entries from an
earlier replay (`docs_owned`), a fresh docs reviewer (reviewer role, EXEC reviewer protocol) reviews the
full diff and must run the configured test command. A missing observed test, a HOLD or a REVISE without
findings HOLDs before the DOCS receipt, so resume reuses the recorded writer and reviews again. A blocking
finding (the ledger rule: CRITICAL/MAJOR or a security flag) routes to an EXEC replay: the findings (source
`docs-reviewer`, no owner) go to the persistent EXEC reviewer, whose author may fix them in DOCS-owned
entries, then a new gate, FINISH, POLISH-Q and DOCS. A REVISE with only MINOR findings is advisory, and
APPROVE advances to SECURITY, unless any blocking finding is still open. `BUDGET_CAPS['DOCS']` (7) bounds
the DOCS writer and review dispatches per run (a protocol retry may add one). A
no-op DOCS writer without owned entries advances without a docs review (legacy 3.6 reviews only writes).
POLISH-Q specialist turns record the configured test command they ran (`observed_test`) but are not
required to run it: the candidate tree already has the EXEC reviewer's and the gate's observed tests.
Every other write outside the allowlist,
including comment fixes in code (legacy 3.6.2), goes to EXEC replay.

**POLISH-Q differences from legacy (intentional, stricter).** Legacy language agents and the test analyzer
use the cheap model tier; W uses the configured reviewer model. Legacy 3.5.2 reports and continues at its
cap; W HOLDs.
Specialists get the EXEC reviewer protocol (program review views, changed paths, allowed commands,
verified claims); a body command outside the reviewer allowlist is unavailable, not a failure. Each
specialist costs at least one invocation (up to four with protocol and tool-use retries), so a W run needs a
larger `--max-invocations` than the default 25, e.g. 60.

**SECURITY preflight input.** `scripts/security_preflight.py` reads a W01 `delivery-manifest` document
whose `baseline` is captured before the task starts (`scripts/delivery_scope.py`); a `git_snapshot` list
is not accepted. Implemented (W3a-1): creating a W run's state, before any probe or turn, runs
`delivery_scope.py capture --scope .` into `evidence/delivery-baseline-<random>.json` (its sha256 is in the
state); a capture failure refuses the run, and a W run created before W3a has no baseline and HOLDs at
SECURITY. A scope-change successor inherits its parent's baseline (supervisor decision after W3a; W3b-1):
the file is copied into the successor's evidence and bound to the parent's recorded sha256, so the delivery
scope stays relative to the tree before the work item; a parent without a baseline, or one whose file
changed, refuses the successor. Each SECURITY attempt writes a fresh
manifest and runs `security_preflight.py` as a subprocess (`sys.executable`); anything but exit 0 with a
`clean`, coverage-complete report HOLDs, and the HOLD reason names rules and paths, never matched values.
The `sensitive_policy` scan covers tracked plus non-ignored untracked paths. A repository without the legacy
sensitive `.gitignore` patterns gets `review-required` (exit 1) and HOLDs at every SECURITY, as legacy does.
Operator recovery: change the tree outside the run (add the patterns, remove the file) and resume, which
replays EXEC review and gate (then FINISH, POLISH-Q, DOCS, SECURITY) under the round limit; or abort. A tree
change during SECURITY HOLDs, and resume replays the same way.

**`docs_file` entry.** Legacy Step 4 appends the post-delivery summary (status, rounds, polish summary,
findings, cross-vendor line, files) after the gate. W writes the entry during DOCS so that it is reviewed
and scanned; it can only hold facts known before SECURITY (work item, changes, EXEC/POLISH-Q results).
The SECURITY outcome, the commit SHA and token/time totals go only to the Chinese delivery report in the
run directory. DELIVERY does not append to `docs_file` again. A replayed DOCS stage replaces its own entry.

**Commit scope versus legacy W04.** The accepted manifest is the `git_snapshot` the operator accepted:
tracked files plus non-ignored untracked files, so a stray untracked file in the worktree is part of
the reviewed, scanned and accepted tree. Legacy W04 commits only declared task paths. W3b must therefore
also refuse, as W04 does: work staged before the run, content-transforming Git attributes or filters
(`filter`, text/eol/encoding) and `core.autocrlf`.

There is no CLOSE stage on the real path: legacy review-loop never closes a Compass item, and
`compass:close` stays an operator action. Tests are reviewer-observed `test_command` runs, as in real EXEC.

## Refusals (kept, or added in W1a–W3b)

- Kept: `--adversarial-gate off` and `resume --polish` cannot enter the lifecycle; legacy DONE/ACCEPTED
  states cannot either.
- Kept, mechanism replaced in W1a: fake-format lifecycle states stay refused on the real path. Today
  `Coordinator.__init__` refuses every saved `lifecycle_mode=on` state; W1a adds a format marker so only
  W-format state resumes.
- Added in W1a: `lifecycle_mode=on` comes only from the CLI or an operator profile, meaning a `--config`
  outside every author-writable root as decided by `program_binding` (a profile under the workspace,
  run_dir or author-tmp is a workspace profile). Today a workspace `.review-loop/paired-session.json` value
  is stopped only by the blanket refusal; W1a adds an explicit check that keeps the "lifecycle remains
  disabled" message substring.
- Added in W1a: lifecycle runs refuse `--accept-unverified-claude-author` and `--accept-probe-skip`; a
  verified probe-cache reuse is allowed.
- Added in W1a, moved by W1b–W3a: a W run reaches DONE only after a READY SECURITY receipt, and until W3b
  `accept` refuses a W DONE, so a real run never silently skips a stage.
- Added in W3a-2: after the SECURITY clean scan a fresh security reviewer (reviewer role, EXEC reviewer
  protocol, its own earlier findings in an owner ledger) must make tool calls and report no open finding of
  its own; any finding, HOLD or unusable verdict HOLDs. A turn without tool calls is discarded and retried
  once and never touches the ledger; a crash replay reuses only the turn `invoke` returned (evidence contract
  checked), and a HOLD, unusable or malformed review is discarded so resume reviews again. A resume on the tree its
  findings were left open on is refused ("security findings need a fix on a new tree"); the operator's fix
  outside the run changes the tree, EXEC replays, and the next security reviewer disposes its findings (only
  the owner closes them). Until then those findings gate only SECURITY and DONE: the EXEC reviewer cannot see
  or close them, so EXEC, POLISH-Q and DOCS do not count them as blockers. `reject` on a W DONE is refused
  until W3b, like `accept`.
- Added in W1b: FINISH binds `candidate_oid` to the last reviewed snapshot and HOLDs (stale EXEC approval)
  when the tree differs; the docs/skip/polish keys (`docs_file`, `docs_allowlist`, `skip_globs`,
  `skip_quality_polish`, `polish_round`) are operator-only for W run/resume (E-4); the FINISH turn HOLDs when
  it changes HEAD, its branch or the staged index against a baseline persisted per attempt (tags, remotes,
  stash and other branches are shared across worktrees and not checked; HEAD equal to the run parent is a
  W3b delivery check). The FINISH receipt binds the tree the coordinator observes when it writes it.
- Added in W1b–W3b: every writer turn must leave HEAD, refs and the index unchanged, otherwise HOLD; the
  run_dir denyWrite and lease stop writers from calling accept or close (D8) but do not stop a `git commit`
  inside the worktree.
- Added in W3b-1: a W accept needs status DONE and stage DONE and `--expect` over an intent that also binds
  the stage receipts (`receipts_sha256`); `--override-rejection` is refused, so neither a rejected tree nor a
  round-limit HOLD (FINISH, DOCS or SECURITY reasons included) is accepted, and open specialist or security
  blockers are never put into an accepted record; at the rejection limit a W run can only abort. A HEAD
  that moved since the run started HOLDs the accept and sends the run back to SECURITY (restore HEAD and
  resume). `auto_commit` and `external_delivery` are frozen operator-only keys (default false);
  `external_delivery` true refuses the accept (D8). With `auto_commit` false the accept changes no ref and
  no index; the Chinese delivery report is `delivery-report.md` in the run directory. A reject on a W DONE
  reopens EXEC (epoch+1, the persistent author gets the rejection note, then reviewer, gate, FINISH,
  POLISH-Q, DOCS and SECURITY again).
- Not a refusal: author and reviewer may share a vendor exactly as in real EXEC (ADR-11 D-8).

## Fake-only (unchanged)

Candidate roots and scratch Git (`candidate_tree`), `finish_dispatch` with separate filesystems,
`candidate_test_sandbox`, Q proposal/review/bundle, `delivery_seal` (refuses active hooks),
`delivery_publish`/journal/recovery, `delivery_close` with the Compass BACKLOG mutation, SECURITY repair
and ignore consent. Their `fake_dispatch_guard` refusals and assertions do not change.

## Implementation batches and follow-ups

W1a activation and refusal rewiring; W1b FINISH; W2a POLISH-Q; W2b DOCS (including the `docs_file`
lifecycle default); W3a SECURITY; W3b DELIVERY. The README says the lifecycle runs only after W3b. When
W lands, the 1D mapping rows for `skip_quality_polish`, `docs_file` and `auto_commit`
(`1d-entry-mapping.md`) describe the pre-W state and must be updated.
