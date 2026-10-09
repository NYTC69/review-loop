# Review-only / code-exists entry for paired-session (design, lg1)

> **Historical** (D-LG1 design, shipped in v2.11.0). Legacy `execute --review-only` was removed in v2.13.1; current behaviour: the [PSE](../../docs/protocol/paired-session-entry.md) "Review-only entry".

Design only; no product code. Owner decision D-LG1 (2026-10-05): build a paired-session entry instead of keeping legacy for
review of existing code. Once it exists, legacy `execute --review-only`, the review-loop code-exists auto-route and later
code-quality-loop can retire. It is also the frozen-diff review start that M7 still needs
(`docs/history/m7-seeded-defect-comparison.md:16-17`).

Gap source: main-checkout `.compass/results/2026-10-05_legacy-gap.md` §1 ("Code-exists auto-route / review-only") and §6.
Anchors are at `18479ba`. `C` = `paired_session/coordinator.py`; `PSE` = `docs/protocol/paired-session-entry.md`.

## 1. Entry contract

**CLI.** `run --review-only [--base <ref>]`, with the other `run` flags unchanged (Q1). `resume`, `reject`, `note`,
`accept`, `abort` and `status` read the entry kind from the frozen state.

**Inputs.**
- The change under review is the workspace tree (tracked plus non-ignored untracked files, `git_snapshot`), measured
  against a **review base** commit.
- `--base` defaults to `HEAD`, which reviews the uncommitted work, as legacy `--review-only` reviewed the open dirty set
  (`skills/execute/references/entry.md:119-145`). A branch review passes `--base main` or a merge-base.
- `WORKITEM.md` is still required, as goal and context. It may be one line ("Review the change for correctness"); when it
  states a goal, the reviewer also checks alignment with it.
- The whole non-ignored tree is reviewed and, with auto_commit, delivered (E6:133-135). Unrelated dirty work would
  therefore be reviewed and committed too; Q7 decides the rule for that.

**Refusals at creation** (before any state, probe or turn):
- `<ref>` does not resolve to a commit, or the base is not an ancestor of `HEAD`.
- The tree equals the base tree.
- The index has unmerged entries.
- The index is **partially staged**: for some changed tracked path, the index differs from both `HEAD` and the worktree.
  After an auto_commit, `read-tree` (C:3951) would silently drop that staged version, which nobody reviewed.
- `--stop-after-plan` is given. The skill refuses its own `--plan-only` argument.
- The work item already carries ledger ids or review-history wording. `_plan_history_issue` (FIELD-11, C:4514-4522) runs
  only at PLAN approval, which this entry skips, so it runs here at creation instead.
  As built (LG1-e), it also scans the review scope. Since FIELD-27 the scope's initial-change path list is exempt,
  both here and when the fresh shadow and gate scan `context/plan.md` (only this run's frozen scope), and
  `delta.stat`/`status.txt` exempt the creation mirror's paths: a changed path that names a vendor or a review
  (`bin/codex-run`, `docs/gate-review-notes.md`) is the user's code. A path a later fix round adds is still caught in
  `delta.stat`/`status.txt`. The rest of the scope (the work item, the test command) is scanned as before. A path
  shaped like a ledger id or a verdict (`docs/F001.md`, `APPROVE.txt`) is still refused here (FIELD-11, kept
  conservative: the ledger-id pattern is an independence guard); such a change is reviewed with the legacy workflow.
- Until LG1-c lands, `--review-only` with `--lifecycle-mode on` (§9). Lifted in LG1-c.

The scope lists one path per line; since LG1-e a name with a tab, newline, other control character, backslash, leading
quote or non-UTF-8 byte is written as an ASCII JSON string (`review_scope_path`), read from raw `-z` output.

**Frozen at creation**, in config and state. A different value at resume is refused like any other config mismatch.
- `entry: review-only`.
- `review_base`, the base OID. It is stored as `base_commit` (C:1820), which already drives `delta.patch` / `delta.stat`
  (C:3188-3192).
- `head_at_start`: the W lifecycle `parent` (C:1831-1833) and the auto_commit CAS parent. The HEAD-moved checks (C:3831,
  C:5576) stay HEAD-relative.
- `candidate_tree_sha256`: the `git_snapshot` digest.
- `index_at_start`: the `ls-files -s` content digest. The accept intent keeps its raw-index `index_sha256` (C:3981).
- `review_scope_sha256` (§2).
- A mirror of the tree at `internal/review-start` (`_mirror_workspace`), so the operator can restore it after a
  tree-changed HOLD.
- `exec_rounds = 1`, with `review_only_round1: {author: 'skipped'}`. The pre-existing change counts as EXEC round 1, as
  legacy counts the skipped first round (`tests/skills/contracts/assertion-mapping.json:58-63`). `exec_rounds` counts
  author turns (C:5332-5333), and REVISE holds at `rounds >= limit` (C:6026-6027, C:6099-6104). With the offset,
  `--max-exec-rounds N` allows exactly N reviews, matching M7's round = one EXEC review (m7:27).

`_changed_paths` (C:5729-5733) diffs against `HEAD`. For review-only it diffs against `review_base`, so committed paths of
`base..HEAD` reach every consumer:
- the security reviewer's "Changed paths" (C:5634);
- the docs reviewer (C:5490);
- POLISH-Q specialist selection and recheck (C:5801-5802, C:5765);
- the DOCS touched check (C:5424).

## 2. Skipping PLAN safely

The run is created in phase `EXEC` with `next = 'reviewer'`. There are no PLAN rounds, no PLAN shadow and no PLAN gate;
`plan_rounds` stays 0 and `plan_skipped: 'review-only'` is recorded.

The coordinator writes `context/plan.md` itself (no model turn) as a **review scope**:
- a fixed header: "Review scope (review-only entry). No plan was drafted or approved; review the change itself.";
- the work-item goal, verbatim;
- the base OID and `head_at_start`;
- the initial change stat, labelled as such because later fixes make it stale;
- the test command.

Its sha256 is frozen. No turn may write it, and a mismatch HOLDs like any frozen context file.

Prompts change in one batch (LG1-b), each with a review-only branch and nothing else:
- `_review_prompt` (C:4402): "correctness, tests and safety of the change; alignment with the stated goal when given"
  instead of plan conformance.
- `_gate_prompt` (C:4476-4478).
- `_review_protocol`'s "Approved plan:" line (C:5903), used by specialists, docs and security, becomes "Review scope:".
- `finish_prompt` (`worktree_lifecycle.py:84-86`).
- The security prompt's "uncommitted change" wording (C:5629), which is wrong when `--base` is not `HEAD`.
- `_author_prompt`. The EXEC author prompt never reads `plan.md` (C:4336-4345), so the review-only branch inlines the
  review scope. Because `exec_rounds` starts at 1, the "Implement the approved plan now" first-turn wording (C:4310,
  C:4331) is never used.

The first dispatch is the EXEC reviewer on the frozen tree; the author's first round is skipped, like legacy's skip
(`docs/protocol/execution.md` §`--review-only`). A live snapshot that differs from `candidate_tree_sha256` before that
review HOLDs ("the tree changed before the first review"). The operator restores the tree from `internal/review-start`
and resumes, or aborts.

Superseded in 3.1.0: off route removed from the user surface; the following description is historical.

## 3. Phases

- **EXEC.** The reviewer reviews first. On BLOCK, the persistent author fixes the findings. Shadow and gate on that first
  review, FIELD-5, round caps (with the offset above), OPV and the finding ledger are unchanged.
- **W lifecycle.** The CLI default is `--lifecycle-mode off` (C:8236); the skill passes `on` (PSE:83). After EXEC
  approval and the gate, the run goes through FINISH → POLISH-Q → DOCS → SECURITY → DONE, unchanged.
  `--skip-quality-polish` and `--docs-file ""` keep their meaning.
- **Quality writers (D09, owner 2026-10-06).** A review-only run defaults to `quality_writers: both` (the main pipeline:
  `off`), the code-quality-loop simplify and test-consolidation passes: after a clean POLISH-Q, with an explicit test
  command and a green baseline, a kept writer change replays EXEC review, shadow, gate, FINISH and the specialists before
  DOCS. Skips, outcomes and cost: `d09-cap1-writer-passes.md` §3-§4 and PSE "Profile and settings".
- **Reduced set: CLI and harness only.** `--lifecycle-mode off` runs only the EXEC loop and the gate. The skill cannot
  reach it: PSE:119-129 aborts any run that is not lifecycle on, PSE:70 forbids gate off, and lifecycle on refuses gate
  off (C:1664).
  - M7 paired arm: `--review-only --lifecycle-mode off --adversarial-gate off --shadow off --polish-round off`.
    - Window 1, `--max-exec-rounds 1`: exactly one EXEC review receipt, then DONE or a round-limit HOLD.
    - Window 2, `--max-exec-rounds 3`: up to three reviews.
  - auto_commit acts only in the W accept (C:3802), so a lifecycle-off run commits nothing.
- **What the author may change.** Fixes for open findings, plus what FINISH, the POLISH-Q fix leg and DOCS allow, as in
  a planned run. The author-HEAD guard still forbids commits, resets and branch switches. Fixes land on top of
  `head_at_start`; the reviewed history is never rewritten.

## 4. Baselines and the pre-existing change

- **Security.** The SECURITY preflight already scans the whole current tree, not a delta (`security_preflight.py:279-318`;
  `_sensitive_paths` C:5680-5690). The delivery baseline only decides which `.gitignore` changes count as owned
  (`security_preflight.py:131-138,332-336`).
  - A live baseline captured over a pre-existing change marks that change `ambiguous-baseline-overlap`
    (`delivery_scope.py:280-282`). Its `.gitignore` edits then do not count, and the run HOLDs `review-required`. That
    is fail-closed, not an exemption.
  - So the base-tree baseline is a usability fix: it lets a reviewed `.gitignore` change count. `delivery_scope.py
    capture` gains `--from-commit <oid>` (additive):
    - `head_paths`, `index` and `worktree` come from `ls-tree -r <oid>`;
    - the worktree entries' `sha256` and `size` are read from the blobs, as `validate_state` requires
      (`delivery_scope.py:343-345`);
    - `index_file_sha256` is None and there are no untracked files.
  - A review-only W run captures this baseline at creation, before any probe or turn.
  - The security reviewer gets `_changed_paths` against the review base (§1) and its owner ledger as today.
- **DOCS.** C:5424-5426 HOLDs when the EXEC-reviewed change already touches docs-allowlist paths. A code-exists change
  often edits `CHANGELOG.md` already. Recommended rule (Q8): paths of the docs allowlist that the reviewed change touches
  at creation are recorded as pre-owned (`docs_owned`), so DOCS may append to them and the docs review covers them.
- **Staged work.** Allowed only fully staged (§1). `_commit_refusals` (C:3863) compares the index with `index_at_start`
  instead of `HEAD`; the commit is built from the accepted manifest. The rel210-fixA journal check is unchanged.

## 5. Safety, modes, resume

- **Categories A and B are unchanged in both modes.** That covers read-only void/restore (modes included), the author
  and writer git guards, the run dir outside the workspace, the observed test, global-config monitoring, the env deny
  list, and CODEX_PLUGINS_OFF with its argv check.
- **Strict vs efficient.** As today: strict needs the permission probe. OPV records stay bound to trees.
- **Resume / HOLD.** As today, plus the frozen-value check and the tree-changed HOLD of §2.
- **Reject after DONE.** Reopens EXEC with the operator text, as today.
- **Scope-change successor.** The successor is started from `successor-config.json`, which is filtered by
  `CONFIGURABLE_DESTS` (C:3689-3690, C:8309-8317), plus the printed `run` command (C:3647-3651). `entry`, `review_base`
  and the scope hash therefore travel in the successor **spec**: the successor reads them from its parent's spec, never
  from a profile, and they are never added to `CONFIGURABLE_DESTS`.
  - The successor's lifecycle parent is `HEAD` at successor creation (existing rule, C:1833 before C:1861), with the
    ancestor check of C:1852.
  - The successor inherits the review base and the baseline (W3b-1 rule).

## 6. Accept and auto_commit

- The accept intent binds the frozen values through `state_sha256` (C:3982), plus `head_ref`, the receipts and the tree.
- auto_commit commits exactly the accepted manifest on `head_at_start`, never on the review base, so branch history stays
  as it is. The commit message names the review base.
- Default (owner 2026-10-06, FIELD-25): a review-only run with lifecycle on defaults to `auto_commit: true`; an explicit
  CLI or operator-profile `false` wins and is kept on resume. `accept` prints the commit (`COMMIT:`); without a commit
  it lists the uncommitted and untracked files (`UNCOMMITTED:`, also in the reports). Lifecycle-off runs commit nothing.
- The delivery report lists the review base, the reviewed `base..head_at_start` commits and the new commit.
- External delivery stays refused (D8).

## 7. Skill routing and lint

- **Claude** (`skills/review-loop/references/entry.md:57-64,80-83`). When the entry resolves to paired-session and the
  code-exists state is detected, the skill hands off with `run --review-only` (plus `--base` when the user names one). It
  keeps legacy's rule that unrelated dirty work is not a code-exists signal. Plan-exists and explicit resume stay as they
  are.
- **Codex.** `.agents/skills/review-loop/references/entry.md:10,25,33-34`, `.agents/skills/review-loop/SKILL.md:22`,
  PSE and both paired-session skills.
- **Text to align.**
  - `skills/guide/SKILL.md:70`;
  - `docs/paired-session-migration.md:8,46`;
  - `paired_session/docs/v2.10-entry-switch.md:126` (P2);
  - README only under the CLAUDE.md lint-SSOT rule.
- **Legacy mapping.** `execute --review-only` stays legacy until retirement (E-8), then maps to `run --review-only`.
  `--stop-after exec-round` maps to `--max-exec-rounds 1 --lifecycle-mode off --adversarial-gate off` (Q4).
- **Lint** (`tests/skills/contracts/review-loop.json`, `assertion-mapping.json`).
  - Existing needles that must change:
    - `review-loop.json:3521-3524`: only "No prior state" hands off;
    - `:3527-3530`: the notice template names `code exists`;
    - `:3654-3657`: the Codex side.
  - New needles:
    - `review_only_routes_to_paired_session`;
    - `review_only_base_flag_documented`;
    - the guide's legacy-only marking for `execute --review-only`.
  - All other needles stay as they are.

## 8. Test plan

1. Creation:
   - each refusal of §1: ref, ancestor, empty, unmerged, partially staged, stop-after-plan, FIELD-11, lifecycle on before
     LG1-c;
   - the frozen values;
   - the scope file and its hash;
   - the review-start mirror;
   - `exec_rounds == 1`.
2. The first dispatch is the EXEC reviewer with review-only wording and no "approved plan". The first author fix turn
   gets the scope and no "Implement the approved plan" text.
3. A tree change before the first review HOLDs; restoring from the mirror and resuming reviews.
4. `--base HEAD~1` with committed and uncommitted parts:
   - `delta.patch` equals `git diff <base>`;
   - `_changed_paths` lists the committed paths;
   - the language specialist for a committed `.py` is selected.
5. Round caps: `--max-exec-rounds 1` gives exactly one review; `3` gives at most three (M7 windows).
6. Fake end-to-end run, lifecycle on: BLOCK, fix, approve, gate, FINISH..SECURITY, DONE, then accept with auto_commit.
   The parent is `head_at_start`, the tree is the accepted manifest, and the base branch is untouched.
7. Baseline:
   - a reviewed change that edits `.gitignore` HOLDs with a live baseline and passes with `--from-commit`;
   - a secret in the pre-existing change is flagged (regression);
   - `delivery_scope.py --from-commit` unit tests (blob sha256/size, `validate_state`).
8. DOCS: a reviewed change that already edited `CHANGELOG.md` reaches DOCS with that path pre-owned and does not HOLD.
9. A fully staged pre-existing change is accepted; a different index at accept HOLDs (fixA).
10. Category A in both modes: a read-only role's edit during the first review is voided and restored.
11. Resume and successor:
    - a frozen-value mismatch is refused;
    - `reject --scope-change` yields a review-only successor that has the base, scope hash and baseline, with
      parent = `HEAD` at successor creation.
12. Lint: the changed needles and the new needles PASS.

LG1-e added the following tests:
- base-tree symlinks and executable files in the from-commit baseline, plus a mode change and a symlink retarget in the
  delivery;
- the delivered tree against the accepted manifest by content and mode;
- scope quoting;
- the history-shaped path refusal;
- DOCS deletion and rename pre-ownership;
- non-UTF-8 names in `commit_state`.

Residuals:
- A non-UTF-8 name is tested at unit level only. macOS APFS refuses such names, so no coordinator run exercises one.
- `_changed_paths` still reads names as replaced text, which affects specialist selection and docs pre-ownership for
  such a name.
- A writer that stages during a review-only run has no review-only test of its own. The mode-independent W test
  `test_a_docs_writer_that_moves_the_index_holds` and test 9 (index changed before accept) cover it.

Tests go in a new `paired_session/test_review_only_entry.py` (strict-pinned harness) and in `tests/delivery_scope_test.py`.

## 9. Batch plan (product lines are estimates)

| Batch | Content | Lines | Tests |
|---|---|---|---|
| LG1-a1 | Flags, refusals, frozen values, review-start mirror, `exec_rounds` offset, start in EXEC/reviewer, tree-changed HOLD, resume mismatch, interim refusal of lifecycle on | ~95 | 1, 3, 5, 11a |
| LG1-a2 | `head_at_start` vs `base_commit`, `_changed_paths` against the review base, successor spec carry | ~60 | 4, 11b |
| LG1-b | Prompt branches: review, gate, `_review_protocol`, `_author_prompt`, finish, security wording | ~80 | 2, 10 |
| LG1-c | `delivery_scope.py --from-commit`, the baseline at creation, docs pre-owned, staged rule, delivery report; lift the lifecycle-on refusal | ~130 | 6, 7, 8, 9 |
| LG1-d | Skills (both hosts), PSE, guide, migration and entry-switch docs, lint needles | ~150 doc lines + lint JSON | 12 |

- Order: a1 → a2 → (b ∥ c) → d.
- M7 is unlocked after a1, a2 and b (lifecycle off). The default-entry route waits for d.
- Each batch: Opus review (max 2 rounds) and targeted tests. One real run through the default entry on a dirty tree
  closes the work.

## 10. Open questions for the owner (with recommendations)

- **Q1. CLI.** `run --review-only` or a `review` subcommand?
  *Recommend `run --review-only`*: one state machine, and every operator action is reused.
- **Q2. Default base.** `HEAD`, or a required `--base`?
  *Recommend `HEAD`* (legacy parity); the skill adds `--base` when the user names a base.
- **Q3. Lifecycle.** Full W by default?
  *Recommend yes* through the skill. Lifecycle off is CLI/harness-only (M7, operator CR); no skill exception.
- **Q4. `--stop-after exec-round`.** Map it, or add a real stop?
  *Recommend the mapping in §7*, given the round-1 offset.
- **Q5. A base that is not an ancestor of `HEAD`.**
  *Recommend refusing in v1.*
- **Q6. code-quality-loop.** Retire it onto this entry, or keep it standalone?
  *Recommend deciding after one real LG1 run.*
- **Q7. Unrelated dirty work** (it would be reviewed and committed).
  *Recommend:* the skill auto-routes only when the dirty set is task-related (legacy rule). The coordinator lists every
  changed path in the review scope, and the operator sees the list in the accept intent. A `--scope <pathspec>` filter is
  deferred until a real need appears.
- **Q8. Docs files the reviewed change already edited.**
  *Recommend pre-owned* (§4), not a refusal.
- **Q9. Does the pre-existing change count as EXEC round 1?**
  *Recommend yes* (legacy parity, M7 window semantics, no first-turn wording problem).
