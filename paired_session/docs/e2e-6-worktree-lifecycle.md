# E2E lifecycle design 6: worktree lifecycle W (D12 legacy parity)

Status: decided design (ADR-11), **partly implemented**: W1a accepts `lifecycle_mode=on` on the real path;
W1b runs FINISH and HOLDs before POLISH-Q; the remaining stages arrive in W2a–W3b. Sources: ADR-11, the lane A
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
  never start a replay loop.
- Adopted (doc 1 §Gate): with lifecycle on, EXEC convergence routes to FINISH and never enters the legacy
  `start_polish_or_done` advisory round. Legacy `--polish-round on` maps to POLISH-Q; open advisory
  findings feed POLISH-Q, whose fix leg reuses the existing polish author/reviewer turn implementations.
- Replaced: candidate materialization and OS separation (docs 2a–4) by the live worktree; the same-vendor
  rejection by ADR-11 D-8; Compass CLOSE by no close (ADR-11 D-3).

## Stages

| Stage | Dispatch (real path) | Writes | Exit / HOLD | Legacy step matched |
|---|---|---|---|---|
| FINISH | fresh author session, same author flags | worktree | unchanged digest → POLISH-Q; changed → EXEC replay | Step 3.4 spent; executor readiness |
| POLISH-Q | fresh reviewer turns per detected language plus code/silent-failure/test reviewers, inlined `agents/*.md` bodies in the frozen role manifest; fixes via the polish author/reviewer turn implementations; one simplifier pass and test consolidation by fresh author sessions | fix, simplify and test writers only | open specialist blocker, zero tool use after one retry, cap → HOLD; any write → EXEC replay; `skip_quality_polish: true` → no-op receipt | Step 3.5 (static-analysis fix 2, code review 3, simplify 1, tests 2 rounds), tool-use guard |
| DOCS | fresh author session as docs writer; fresh docs reviewer over the full diff with an observed test run | frozen `docs_allowlist` (lifecycle default `docs_file` = `CHANGELOG.md`; the CLI default is still `''` until W2b) | HOLD set (below) → HOLD; any other path outside the allowlist (source, tests, comments) → EXEC replay; missing retest → HOLD | Step 3.6; see the `docs_file` entry below |
| SECURITY | coordinator `sensitive_policy` path scan and `scripts/security_preflight.py`; fresh security reviewer | none | any preflight hit, `security=true` or blocking finding → HOLD (no automatic repair, no `.gitignore` edit); a no-op run still scans; tree change at exit → HOLD | Step 3.7 |
| DONE | after SECURITY only | — | acceptance pending | Delivery gate |
| DELIVERY | operator `accept --expect <digest>` from W DONE only (existing intent: tree, HEAD, index, state) | `auto_commit: false` (default): none. `auto_commit: true`: one hook-free local commit of the accepted manifest; drift → HOLD | external push/PR/merge refused (D8) | Step 4; differences from W04 below |

**DOCS HOLD set.** A DOCS write to any of these HOLDs even if listed: `AGENTS.md`, `CLAUDE.md`,
`agents/**`, `skills/**`, `.claude/**`, `docs/protocol/**`, `.review-loop/*`, config and manifest files,
`.gitignore`, `.gitattributes`, `.git`, and symlink escapes. Every other write outside the allowlist,
including comment fixes in code (legacy 3.6.2), goes to EXEC replay.

**POLISH-Q differences from legacy (intentional, stricter).** Legacy language agents and the test analyzer
use the cheap model tier; W uses the configured reviewer model. Legacy 3.5.2 reports and continues at its
cap; W HOLDs.

**SECURITY preflight input.** `scripts/security_preflight.py` reads a W01 `delivery-manifest` document
whose `baseline` is captured before the task starts (`scripts/delivery_scope.py`); a `git_snapshot` list
is not accepted. W1a or W3a must capture that baseline at run start or add an adapter. A repository without
the legacy sensitive `.gitignore` patterns gets `review-required` (exit 1) and HOLDs at every SECURITY, as
legacy does; W3a defines the operator recovery (adding the patterns outside the run, which changes the tree
and replays from EXEC, or abort).

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
- Added in W1a, moved by W1b: until W2a lands, a W run HOLDs after FINISH with the reason "worktree
  lifecycle stage POLISH-Q not implemented yet (W2a)", so a real run never silently skips a stage.
- Added in W1b: FINISH binds `candidate_oid` to the last reviewed snapshot and HOLDs (stale EXEC approval)
  when the tree differs; the docs/skip/polish keys (`docs_file`, `docs_allowlist`, `skip_globs`,
  `skip_quality_polish`, `polish_round`) are operator-only for W run/resume (E-4); the FINISH turn HOLDs when
  it changes HEAD, its branch or the staged index against a baseline persisted per attempt (tags, remotes,
  stash and other branches are shared across worktrees and not checked; HEAD equal to the run parent is a
  W3b delivery check). The FINISH receipt binds the tree the coordinator observes when it writes it.
- Added in W1b–W3b: every writer turn must leave HEAD, refs and the index unchanged, otherwise HOLD; the
  run_dir denyWrite and lease stop writers from calling accept or close (D8) but do not stop a `git commit`
  inside the worktree.
- Added in W3b: a lifecycle accept is allowed only from W DONE with `--expect`; the rejection-limit HOLD and
  `--override-rejection` paths of `accept()` are refused for lifecycle runs (doc 4 §Acceptance).
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
