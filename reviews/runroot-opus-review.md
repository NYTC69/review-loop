VERDICT: REVISE

Independent adversarial review of `feat/run-root-in-repo` (ad78824, 73e6de2) against `origin/main`, judged against
the owner's rule: realistic use and realistic accidents only, not deliberate bypasses.

Most of the change is sound: the external scratch root, the legacy-state fallback, the gitignore handling and the
lane-to-main-checkout resolution all work. Two realistic problems remain. Both show up only when the workspace is the
main checkout, which is the default when no lane is used, or when run output from lanes collects there.

## BLOCKING

### B1. Full copies of the workspace now sit inside the project, and the project's own test runner picks them up

- Where: `paired_session/coordinator.py:3672` (`_mirror_workspace`), called at `:3711` (`internal/current-review`),
  `:3723` (`internal/last-review`) and `:5895` (`internal/review-start`). Combined with the new default run dir
  `<repo>/.review-loop/runs/<UUID>/` (skills, `scripts/project_paths.py`).
- What happens: every review baseline copies every tracked and non-ignored untracked file (sources, tests,
  `package.json`, snapshots) into `RUN_DIR/internal/last-review/` (and `review-start/` for review-only runs). Nothing
  removes these copies when a run ends. Before this change they were outside the repo. Now they are under
  `<repo>/.review-loop/runs/*/internal/`. Every lane's runs also land in the main checkout, so after N runs the main
  checkout holds N stale copies of the project.
- Being gitignored does not help. Test runners and other tools that walk the filesystem without honouring
  `.gitignore` see these copies. I checked this with the real tools in a scratch repo (`.review-loop/.gitignore` = `*`,
  one copy under `.review-loop/runs/u/internal/last-review/`):
  - `jest@29.7.0` with a default config ran both `./a.test.js` and
    `.review-loop/runs/u/internal/last-review/a.test.js` ("Test Suites: 2 passed"). It also warned
    `jest-haste-map: Haste module naming collision` for the copied `package.json`.
  - `vitest@1.6.0` (`vitest run`) ran both `v.spec.mjs` and the copy ("Test Files 2 passed").
- Realistic failure: a JS/TS project whose test command is `npm test` (jest or vitest), run as paired-session in the
  main checkout or in the main checkout after a few lane runs:
  - The author's and reviewer's test runs, and the reviewer's `self_run_evidence`, include old copies of the suite.
    That multiplies runtime by the number of stored runs.
  - Mirror tests can fail for reasons unrelated to the change, because the mirror has no ignored or generated files
    (`.env`, build output, codegen). The run then gets REVISE or HOLD findings from code nobody changed.
  - The user's own `npm test` outside review-loop shows the same pollution.
  - Other walkers that ignore `.gitignore` do the same: `gofmt -w .`, `find . -name '*.md' | xargs sed -i`,
    `grep -rn`. The author's edits can then land in the mirrors, which corrupts `delta-since-last-review.patch`.
  - pytest, `go test ./...` and tsc skip dot-directories and are not affected.
- Fix options, smallest first:
  - (a) Delete `internal/last-review`, `current-review` and `review-start` when the run reaches a terminal state
    (ACCEPTED, REPORTED, aborted). This bounds the problem to active runs but does not remove it.
  - (b) Keep the mirrors in the per-run external scratch root, which is already stable across resume and stored in
    state. The existing code already tolerates a missing baseline (`baseline.exists()`). Check that a missing
    `review-start` does not change FIELD-27 behaviour.
  - (c) Record the mirrors as Git trees (`git_snapshot` already computes one) and diff the trees instead of keeping
    file copies.
  - At minimum, the README/skills should tell JS users to add `<rootDir>/.review-loop/` to `testPathIgnorePatterns`
    and `modulePathIgnorePatterns` (and vitest `exclude`). On its own this is a weak fix, because the owner wants
    defaults that just work.

### B2. The read-only ignored-entry check still sees `.review-loop/pr` and `.review-loop/logs`, so concurrent review-loop activity voids read-only turns

- Where: `paired_session/readonly_guard.py:136` (`INTERNAL_WORKSPACE_DIRS = ('.review-loop/runs', '.review-loop/codex-home')`).
  The same gap is at `paired_session/evidence_guard.py:214-215` and `paired_session/author_write_guard.py:53-55`
  (the latter is unwired, so it only matters for consistency).
- What happens: both directories are written by review-loop itself, in the main checkout, by default:
  - review-pr clones go to `<repo>/.review-loop/pr/<id>/` (`materialize_pr.py` default root);
  - the skills put launcher logs in `<repo>/.review-loop/logs/` (`skills/paired-session/SKILL.md:66`,
    `.agents/skills/paired-session/SKILL.md:62`, `concurrent-runs.md`).

  `_ignored_entries` records each of these as one collapsed entry, and `ignored_changes` compares its lstat. Mtime
  is part of that comparison, so a new or removed child changes it.
- Reproduced (scratch repo, tracked `config.md`, `.review-loop/.gitignore` = `*`/`!config.md`, one clone in `pr/`):
  `_ignored_entries` returns `['.review-loop/.gitignore', '.review-loop/pr']`. After
  `capture()`, `mkdir .review-loop/pr/y` makes `ignored_changes` return `['.review-loop/pr']`.
- Realistic failure: a paired-session run whose workspace is the main checkout is in a reviewer, gate or shadow turn.
  Meanwhile the user starts a review-pr (a clone is materialized or `--remove`d) or starts another lane's run (a new
  launcher log appears). The turn's receipt gets `voided.ignored_changed` and the read-only turn is thrown away: a
  false HOLD or retry. Concurrent lanes are a documented, supported workflow, so this is not contrived.
- Fix: add `.review-loop/pr` and `.review-loop/logs` to `INTERNAL_WORKSPACE_DIRS`, and to the evidence-guard and
  author-write-guard prefix lists. Better, define the tuple once and import it in all three places. Extend
  `test_readonly_capture_excludes_internal_roots_but_checks_siblings` to create a child under `pr/` and `logs/` after
  `capture()`. Excluding `pr/` from the evidence-guard walk also stops it from walking whole PR clones, including
  their `.git/objects`, on every turn.

## NON-BLOCKING

- N1. The "run dir is not writable by any role" claim is no longer true for a Codex author in the main checkout.
  `paired_session/docs/efficient-mode.md:50` (A(b)) still makes the claim. A Codex author runs `workspace-write` with
  the workspace as its root (`coordinator.py:3169`), so `<ws>/.review-loop/runs/<id>/` (state, `agents/*.md` copies
  hashed into the frozen manifest, `context/`) is writable from that turn. A Claude author is still covered by
  `Edit(//run_dir/**)` and `denyWrite` (`:3351`, `:3357`). Nothing checks the run dir after an author turn
  (`run_dir_inodes` is only armed for read-only scratch roles). A deliberate write is out of scope, but an accidental
  bulk edit (`find . -name '*.md' -exec sed -i …`, `git clean -x`) is plausible. The docs now warn about
  `git clean -x`, which is good. Suggestion: correct the A(b) sentence. Cheaply, also hash
  `RUN_DIR/agents` and `context/*` before and after Codex author turns and HOLD on a change; `run_dir_inodes`
  already exists for this.
- N2. The read-only "ignored set too large" bound is shared with the user's own ignored entries
  (`IGNORED_LIMIT = 2000`). Expanding the collapsed `.review-loop` only adds a handful of entries, so this is fine;
  noted only because `pr/` and `logs/` would count against it until B2 is fixed.
- N3. Scratch roots (`/tmp/review-loop-<uid>/<hash>/`, holding `author-tmp`) are never removed when a run ends. The
  cost is small and `/tmp` is cleaned eventually. Removing it on a terminal state would be tidy. On macOS the first
  candidate is `/tmp` (resolved to `/private/tmp`), not the per-user `$TMPDIR`. That is fine because the root is
  0700 and owner-checked, but it is worth stating in `concurrent-runs.md`.
- N4. `prepare_run_directory` runs twice on a fresh `run`: once in `main()` and once in `Coordinator.__init__`. This
  is harmless (idempotent), just redundant `git check-ignore` calls.
- N5. Submodule checkouts and `--separate-git-dir` repos: `project_repo` returns None because the common dir does
  not end in `.git`, so they fall back to XDG with a notice. This matches the requirement ("cannot be resolved"),
  but the docs only mention "not a Git repo or bare repo". Consider saying "or a submodule".
- N6. Skill wording (`skills/paired-session/SKILL.md`): "Resolve the root once with its own Bash call:
  `python3 … project_paths.py "$WORKSPACE"`". The same block also says variables do not persist across Bash calls,
  so the agent has to define `WORKSPACE` inside that call. A short "(set `WORKSPACE` in that same call)" would remove
  the ambiguity. The Windows line has the same shape.

## What I verified (no problem found)

- Inside-workspace refusal: only paths strictly under `<ws>/.review-loop/runs/` are accepted. `runs` itself,
  `codex-home/<id>` and other paths are refused. A non-ignored `runs/` (an existing `.review-loop/.gitignore` that is
  not overwritten) gives `REFUSED: … is not gitignored` and no state is written. The `.gitignore` is created with
  `open('x')`, so it is never overwritten. `*` also ignores the `.gitignore` itself, so it does not make the worktree
  dirty. Tracked files under `.review-loop/` stay tracked.
- Lane worktree: `project_paths.project_repo` uses `--git-common-dir` to reach the main checkout. On the coordinator
  side, `git_work_tree_root(runs)` picks the main checkout, where the ignore file is written and checked. The run dir
  is outside the lane workspace, so lane scans are unaffected.
- Git-based scans (`candidate_tree`, `_delta_patch`, `materialize_review_context`, `auto_commit` untracked
  listing, delivery scope, the read-only tree compare) all use `--exclude-standard` or `git add -A` semantics, so the
  gitignored run dir is invisible to them. `_mirror_workspace` does not copy `.review-loop/runs`, as the test
  asserts.
- `git diff --no-index` between the mirrors still prints absolute paths when both are inside the repo (git 2.43). The
  history-scan rel-path mapping at `coordinator.py:5418-5431`, which strips `self.internal/<mirror>` from `raw[1:]`,
  therefore still works.
- New runs: `scratch_root` is stored in state and is outside any git work tree, even with a repo-local `TMPDIR`
  (candidates are filtered by `git_work_tree_root`). It is 0700 and owner-checked. Role TMPDIRs, `author-tmp`, author
  probe trees, OS-probe targets and `codex-sandbox-home-*` all go there. The FIELD-28 warning now keys on
  `scratch_base`, so it no longer fires for an in-repo run dir. Codex read-only turns get
  `:tmpdir = write` → `TMPDIR` under the scratch root. Not verifiable on Linux with a real Codex sandbox (fake CLIs
  only).
- Old-layout (v3.3.0) runs: with no `scratch_root` in state, `scratch_base` falls back to `run_dir` and
  `scratch_parent` to `run_dir.parent`. This reproduces the old `author-tmp`, `role-tmp`, probe and policy-digest
  paths and the frozen manifest. The coordinator never adds `scratch_root` to a resumed legacy state
  (`test_legacy_state_keeps_frozen_paths_on_resume_status_abort_stop`). `main()` only checks run dirs outside the
  workspace for legacy runs, so nothing new is required of them.
- A new-layout resume after `/tmp` cleanup: the scratch root is recreated (`mkdir parents=True` plus the private
  check).
- Windows: `user_token()` uses the user name, `temp_root()` is the user's temp directory and `check_private_dir`
  checks for a real directory under the user's roots. `Path` comparisons in `prepare_run_directory` are
  case-insensitive on Windows. `project_repo` handles the `C:/…/.git` output. Not executed on Windows.
- `materialize_pr.py`: a clone is allowed inside the operator repo only at exactly `<repo>/.review-loop/pr`, after
  `ensure_ignore` and `check-ignore`. The nested clone is ignored by the outer repo (`*`), and `git clean` does not
  remove nested repos without `-ff`. Claude reviewer turns in the clone run with `--setting-sources ''`, so the
  outer repo's CLAUDE.md and settings do not leak in.
- Tests are meaningful for what they claim, with two gaps: no test covers `pr/` or `logs/` under `.review-loop`
  (B2), and none covers mirror visibility (B1).

## Tests run (Linux container, git 2.43, Python 3)

- `python3 -m pytest -q tests`: **213 passed, 9 subtests passed** (55.7 s). pytest was installed with pip first; it
  was missing.
- `python3 -m unittest -v paired_session.test_run_root_in_repo paired_session.test_field28 paired_session.test_readonly_scratch paired_session.test_operator_roles`:
  **Ran 170, 1 failure, 2 skipped** (275.8 s).
  - The 2 skips are `root ignores directory modes`: the container runs as root, so the mode checks cannot run. They
    are not macOS-only skips.
  - The failure is
    `test_readonly_scratch.ProbeTrackedFileTests.test_a_glob_like_name_is_checked_out_literally_and_a_sibling_keeps_its_unstaged_bytes`
    (`'unstaged work\n' != 'a base\n'` at line 435). It **also fails on `origin/main`**: I ran the single test in a
    detached `origin/main` worktree and it failed there too. The branch does not touch that test, so this is a
    pre-existing Linux/git-2.43 environment issue, not a regression.
  - All `test_run_root_in_repo` (8), `test_field28` and `test_operator_roles` tests pass.
- `scripts/run-skill-lint`: exit 0 (review-loop 141 passed, shared-schema passed; 0 FAIL).
- Not run: the full `paired_session` discover suite (long). The real Codex/Claude sandboxes cannot be tested here
  (Linux, fake CLIs only), and nothing was run on native Windows.
- Ad-hoc reproductions (scratch dirs, not committed): `readonly_guard._ignored_entries`/`ignored_changes` with a
  `pr/` child added after capture (B2); jest 29.7.0 and vitest 1.6.0 collecting a test copy under
  `.review-loop/runs/u/internal/last-review/` (B1); `git diff --no-index` path form for in-repo mirrors.
