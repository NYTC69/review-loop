# Delivery controls

W01 (`delivery-scope.md`) provides the immutable baseline and candidate
manifest. W02, W04 and W05 consume that same manifest; none may recapture a new
baseline to make an ownership or freshness failure disappear.

## W02 — Security Preflight

Run `scripts/security_preflight.py` against the current W01 manifest after Docs
Consistency and on every delivery, including no-op deliveries. It scans all
tracked and non-ignored untracked paths represented in `manifest.current`;
both worktree bytes and each staged index blob are checked, so a staged secret
cannot escape detection when the worktree copy is already safe. It checks
sensitive filenames and high-confidence private-key / provider-token markers
in regular-file contents.
Matched values are never printed. Symlinks are not followed; file and total-byte
limits, stale manifests, unreadable paths, and unsupported content fail closed.

The report also probes representative sensitive filenames against repository
`.gitignore` rules. Global excludes and `.git/info/exclude` do not count as
portable repository coverage. Coverage counts `.gitignore` files tracked and
unchanged from HEAD, plus a `.gitignore` edit that is a declared task-owned path
in the fresh W01 candidate (it ships with this delivery). Missing coverage
yields `review-required`; the scanner never creates or edits `.gitignore`.

Exit 0 means the scan is complete, clean, and ignore coverage is complete. Exit
1 means findings, a stale candidate, or ignore coverage requiring remediation.
Exit 3 means incomplete scanning or I/O failure. Only exit 0 can be recorded as
`security_scan` executed PASS. Bind that record to `scripts/security_preflight.py`
and its helpers, use `--selector dir:.` with declared closure, and retain the
manifest fingerprint in the recorded environment command. W05 reruns the scan
at the final gate rather than trusting a mutable report file.

## W04 — Staging and commit ownership

`scripts/delivery_actions.py plan` is read-only. It prints only W01
`task_delta` paths whose ownership is `declared-post-baseline`. Its
`ownership_reasons` cover a stale candidate, moved HEAD, out-of-scope changes and
same-file baseline overlap in the task delta. Its `commit_reasons` cover
unresolved conflicts or pre-task staged changes, per-path index changes since
the baseline, and content-transform Git attributes that make the raw manifest
identity differ from commit content. A stat-cache-only index rewrite (for
example by `git status`) is not an index change. `eligible` requires both lists
to be empty.

`delivery_actions.py commit` requires the resolved `auto_commit: true` setting,
the session file, and an explicit commit message. It reads exactly one
`auto_commit: true` declaration outside fenced examples in `.review-loop/config.md`;
missing, false, or duplicate declarations refuse the action. It reruns W05 immediately
before acting, builds a private index from current HEAD, inserts only W01-bound
task blobs, verifies that the resulting tree changes exactly the approved
paths, and advances HEAD with an expected-old compare-and-swap. It then
atomically refreshes those owned entries in the user's index while preserving
unrelated user files. It never rereads working-tree content to build the commit.
Existing unrelated staged content blocks the action. Hooks are disabled because
a hook can mutate files after the gate; tests and security checks are the
pre-commit validation. On any race or mismatch the helper fails closed and
preserves the observed index for reconciliation; it never resets or cleans it.

Before committing, W04 compares the manifest fingerprints returned by W05,
its own ownership plan, and its final manifest read. A path replacement between
these reads blocks the action.

The helper returns the commit SHA and index-sync status; the caller appends the
SHA to `session_commits` and surfaces any index-sync warning.
No action is taken when `auto_commit` is false or absent. A no-op task returns
`no-changes` without staging or committing.

The commit command writes Git administrative state; in Codex Stage 1 run it
outside the parent sandbox, under the same host-side boundary required for
evidence-ledger snapshots. A sandbox that denies `.git` writes must fail with
no commit rather than widen its own permissions.

## W05 — Final delivery gate

`scripts/delivery_gate.py` is read-only. It requires a fresh W01 candidate, no
W04 `ownership_reasons`, no W04 `commit_reasons` when `auto_commit` is enabled
(an unreadable or duplicate setting counts as enabled and is itself a reason),
a fresh clean W02 scan with ignore coverage, a
valid executed `security_scan` PASS, every runtime-required
`completed_stages` value from `evidence_ledger.py check --no-write`, and exactly
`delivery_blocked_by: null` in the canonical Session Metadata section. The W01
manifest and baseline fingerprints must also match the Delivery candidate row
in the unique canonical Current Review Packet; W05 loads and validates the
recorded immutable baseline artifact and requires its fingerprint to match the
candidate. The row records baseline and manifest paths in fixed fields.
Packet, metadata, and evidence checks
all use one session-text snapshot so a concurrent session rewrite cannot mix
their decisions. The candidate row uses the exact `baseline=...; manifest=...;
baseline_fingerprint=...; candidate_fingerprint=...; scope=...` field order;
the manifest path must be canonical, with no `.` or `..` segments.
The checker receives an ephemeral copy under a verified external temporary
directory, and starts Python in isolated mode to ignore inherited Python path
customization. W05 passes its one validated in-memory manifest to both the
ownership planner and scanner, so they cannot reopen a swapped artifact path.
It rechecks the repository after the scan and returns a sealed JSON decision
with the manifest fingerprint and reasons. Only `eligible: true` authorizes
Step 4.

W04 commit calls W05 again. A stale report or altered gate artifact cannot
authorize a commit because W04 recomputes the gate from the session, manifest,
ledger and current files.
