# E2E lifecycle design 2a/4: reviewed-tree binding

Status: design only; lifecycle disabled. Covers R12-4 R2 M5/M6/M12 and R1 M9/S2.
Sources: doc 1, archived `r12-4-draft.md` §EXEC/DELIVERY/Resume and `r12-4-findings.md` R1/R2;
legacy `docs/protocol/session-file.md` §`completed_stages` and `execution.md` §Steps 3.6/3.7/4.
Doc 2b owns budgets, caps and specialist closure.

## One candidate tree (R2 M5)

1. Record `expected_parent_head`, run ID and frozen authorized directory prefixes before any writer. Require the
   user's live index and worktree to be globally clean and at that HEAD; otherwise HOLD without touching them. The
   live checkout is not a test/review input. This strict first-mode admission preserves unrelated user work by
   refusing to proceed with it present.
2. In a separate `candidate_root` outside product workspace and `run_dir`, use external `GIT_DIR` and isolated
   `GIT_INDEX_FILE`; never place `.git` in that root. Start the index at the parent tree. Writers may create paths
   only under frozen authorized prefixes. Each new path is checked and added to the evolving delivery manifest before
   review; unknown prefixes HOLD. Ingest adds, deletes, renames, symlinks and modes, then `git write-tree`: the result
   is `candidate_tree_oid`. No live-index or ignored-file import.
3. After every writer call, compare **all** candidate files (including ignored ones) with the index; an unauthorized
   difference HOLDs. Recompute the actual manifest with `diff-tree -r --no-renames`; EXEC-closure/nonreserved-docs
   writes start a new EXEC epoch; SECURITY writes always invalidate EXEC and **all** downstream stages (doc 1).
   Reserved DOCS writes change OID and invalidate full-tree receipts. A prior scoped EXEC/gate claim survives only via
   the closure proof below.
4. Reviewers get an immutable checkout from the exact OID plus curated context, never live workspace, run evidence or
   other reviewers' reports. Tests/lint/static checks use disposable checkouts from that OID; scratch, caches and
   outputs live outside. Freeze and hash external tool/dependency inputs in an environment manifest; do not borrow
   ignored live files. Cache keys include candidate OID. Test/lint/quick-build run in an OS sandbox no weaker than a
   writer's, denying writes to run dir, external Git dir/index, candidate, live workspace and credentials. Clear
   inherited `GIT_*` and set `GIT_CEILING_DIRECTORIES`; inability to enforce this disables lifecycle. A tracked-path
   content/mode mutation, command failure or checkout/OID mismatch invalidates the test receipt.
5. At DOCS exit, a fresh docs review and final independent full-diff review see the post-DOCS OID. Required tests run
   on it, then unconditional SECURITY preflight/fresh review use the same OID. DELIVERY first re-ingests and hashes
   `candidate_root` again; any post-security write, even one not yet staged, HOLDs and requires new
   review/test/SECURITY evidence. It commits only the tree OID bound in final review, test, SECURITY and operator
   acceptance receipts. Doc 3 details the checks; moving legacy Step 4.3 `docs_file` writing before SECURITY is
   intentional and documented there.

## Git path boundary (R1 S2)

- Resolve each component before and after create/rename. Accept only relative symlinks whose lexical target stays in
  the tree. Reject escapes, `..`, hardlinks (`st_nlink>1` is detection only), run-dir paths and Git metadata aliases.
  Put candidate on a different filesystem from run dir, Git dir/index, live workspace and credentials, so a transient
  cross-root `link()` fails with EXDEV; otherwise keep lifecycle disabled. Validate components with
  Git's `verify_path` semantics (case-fold plus HFS/NTFS normalization), with `core.protectHFS` and `core.protectNTFS`
  enabled. Reject nested `.git` aliases, mode `160000` gitlinks and unauthorized `.gitmodules` changes. A platform
  where these checks cannot be enforced keeps lifecycle disabled. Coordinator Git plumbing pins
  `-c core.fsmonitor=false -c core.hooksPath=/dev/null`; doc 4 runs authorized hooks separately before SECURITY.
- The same checks apply to deletion, rename, index-only removal and live-path reconciliation. Writer grants are
  path-limited; reviewers are read-only. An OS sandbox that cannot enforce this boundary cannot activate lifecycle.
  Fake CLIs prove state logic only, not installed-provider permissions.
- For delivery paths, reject Git attributes that transform checked-out bytes (`filter`, `working-tree-encoding`,
  nonidentity EOL), unless the review uses a verified blob view of exactly what will be committed. This prevents a
  reviewer/test checkout from silently showing different bytes.

## Scoped closure proof and receipt resume (R1 M9)

- Each receipt binds run ID, expected parent, stage, EXEC epoch, request ID, sequence, config/role/model and frozen
  environment-manifest hashes, input OID, output OID for writers, tool evidence, result and output hash. SECURITY/acceptance/DELIVERY bind the accepted
  tree OID. Save the complete receipt before `state.next`.
- Resume selects only the saved pending request ID, never a glob's latest file. Validate all fields against current
  state; stale/missing/duplicate/ malformed or different-tree receipts HOLD. An uncertain active call with no complete
  receipt uses existing uncertain-turn recovery, not auto-replay.
- Exception for an already completed scoped EXEC/gate claim after reserved DOCS writes: coordinator writes proof `(claim
  input OID X, new OID Y, diff-tree -r --no-renames X Y paths, frozen reserved set, reviewed closure set)`. Recompute
  the path set from Git objects on resume. It is valid only if every changed
  path is reserved and none intersects a certain closure; `closure=uncertain` HOLDs. Resume may retain that old-OID
  claim **only** with this proof; it cannot satisfy a current full-tree stage. Final review, tests, SECURITY and
  acceptance always require the current exact OID. This follows legacy `session-file.md` per-record invalidation
  without relabelling legacy DONE/ACCEPTED receipts as lifecycle evidence.

## Local commit and live index (R2 M6)

- Before DELIVERY, verify HEAD is still the frozen parent, live index/worktree still match their globally clean
  preflight snapshot, isolated staged paths equal the delivery manifest, and `write-tree` equals accepted OID. Persist
  commit intent with parent, tree, manifest, run/request ID and live-index hash. Mutating hooks must finish before
  final review/test/SECURITY (doc 4).
- Create the commit object from that tree, persist its SHA in the intent, then compare-and-swap the ref with `git
  update-ref <ref> <new> <parent>`. CAS failure HOLDs with no live-index edit; an externally advanced HEAD needs a new
  run/owner resolution, not an automatic rebase. Verify parent, tree and trailers against intent before publishing
  delivery receipt.
- Hold the workspace lease and Git index lock across CAS and reconciliation. Recheck delivery-path bytes/modes against
  frozen parent; refresh only those paths in the live index/worktree from the committed tree using symlink-safe Git
  plumbing. Journal each completed path and index OID. Unexpected edits or partial failure HOLD; resume finishes only
  verified remaining paths. Never overwrite user changes or CLOSE while a delivery path differs from the commit. The
  strict clean precondition means no unrelated staged path is admitted; unrelated work is preserved by preflight
  refusal.
- If the receipt is missing after commit, resume uses the intent's commit SHA and accepts only the unique ref tip
  matching parent/tree/trailers; otherwise HOLD. It never creates a second commit. Doc 4 owns hooks, commit command,
  Compass closeout and external-action policy.

## Missing test command (R2 M12)

Lifecycle start/resume refuses before a model call if `test_command` is blank, unresolvable or differs from frozen
config. Parse the exact command with the existing executable resolver; unsupported shell/env syntax HOLDs. An in-tree
test script is an EXEC closure input pinned by blob hash. External tools and dependencies belong to the frozen
environment manifest. No agent's claimed pass substitutes for an observed test. The simplifier's quick-build command
must also be configured and executable; otherwise its stage HOLDs.

## Fake-CLI tests owned by 2a

- `test_lifecycle_review_test_security_commit_same_tree_oid`: final receipts and commit match; later write HOLDs.
- `test_lifecycle_excludes_unrelated_dirty_or_holds_before_dispatch`: dirty/index overlap refuses untouched.
- `test_lifecycle_isolated_index_reconciles_delivery_paths_only`: journaled refresh; failed CAS leaves checkout.
- `test_lifecycle_resume_rejects_old_epoch_or_tree_receipt`: pending ID and epoch/OID reject stale reuse.
- `test_lifecycle_skip_receipt_binds_candidate_tree`: SKIP needs closure proof; closure changes invalidate.
- `test_lifecycle_model_tool_receipt_binds_tree`: wrong model, zero tools or OID mismatch cannot pass.
- `test_lifecycle_paths_and_missing_test_command_fail_closed`: Git aliases/gitlinks, hardlinks, symlinks and blank test refuse.

Deferred to **doc 2b**: per-epoch/whole-item budgets, replay/reject caps, simplifier once-per-item receipt, specialist
dispositions and tool-use retry. Its tests include EXEC-invalidating DOCS source comments, simplifier/test-writer
replay/cap HOLD, gate REQUEST_CHANGES→repair→FINISH, malformed gate revalidation, reviewer write/report-read attempts,
writer run-dir/receipt/accept attempts, and stage budget exhaustion. Docs 3/4 own DOCS/SECURITY content and closeout.
