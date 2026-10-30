VERDICT: REVISE

Independent adversarial re-review of `feat/run-root-in-repo` (ad78824, 73e6de2, 1bea9e5, 671d6e3) against
`origin/main`. I judged it by the owner's rule: realistic use and realistic accidents only, no deliberate bypasses and
no contrived worst cases. I reviewed the whole branch, not only the fixes.

Summary: the earlier review's B1 and B2 are fixed. I re-ran both reproductions and both now pass. The archive round
trip keeps symlinks, exec bits and empty files. A run created by real v3.3.0 code resumes under the branch with its
old layout unchanged. One new realistic problem comes from the archive fix itself (B1 below), and the fix is a few
lines. Everything else is non-blocking.

## BLOCKING

### B1. A partly cleaned scratch mirror is trusted over the intact archive

- Where: `paired_session/coordinator.py:3694` (`_extract_mirror`: `if target.is_dir(): return target`), reached
  from `_baseline()` (`:3732`) and `_review_start_root()` (`:5619`).
- What happens: for new-layout runs the persistent copy of each baseline is `internal/<name>.tar`. The expanded tree
  in `<scratch_root>/mirrors/<name>` is a cache. `_extract_mirror` re-extracts only when the directory is completely
  gone. If the directory still exists but some files are missing, it is used as if it were complete.
- Why this happens in practice: the scratch root is in `/tmp` (`/private/tmp` on macOS) or `%TEMP%` on Windows. These
  locations are cleaned by age, file by file, not only at reboot:
  - macOS `periodic daily` `110.clean-tmps` (`daily_clean_tmps_days=3`);
  - the `$TMPDIR` cleanup;
  - Windows Storage Sense and Disk Cleanup.

  These cleaners delete old files and keep directories that still have content. The mirrors are also unusually easy
  targets:
  - `tarfile` extraction sets each file's atime and mtime to the source file's mtime. I measured this: after
    extraction, a file whose source is 10 days old shows atime and mtime 10 days in the past.
  - `copy2` keeps the source mtime.

  So most unchanged project files in the mirror look "unused for days" to these cleaners. Only the ctime, set at
  extraction, protects them, and only for cleaners that check ctime.
- Reproduced (Linux, fake CLIs): new-layout run → `capture_review_baseline()` → delete one file from
  `mirrors/last-review/` (what an age-based cleaner does) → new `Coordinator` → `materialize_review_context()`.
  `delta-since-last-review.patch` then reports the unchanged `tracked.txt` as `new file mode 100644`, even though
  `internal/last-review.tar` is intact.
- Realistic failure: a run sits in HOLD over a long weekend on a Mac (HOLD waits for the user), then the user resumes
  it.
  - The reviewer's incremental diff lists unchanged files as added.
  - In a review-only or review-pr run, `_review_start_root()` returns the partial `review-start` / `history-start`
    tree. That shrinks the FIELD-26/27 user-code exemptions (`_review_start_paths`, `_review_start_text`), so the
    history scan can treat the user's own code as review history. This is the false-HOLD class FIELD-27 was written to
    prevent.
  - A coordinator killed while `_capture_baseline` is copying leaves a partial `mirrors/<name>` with the same effect.
    `_mirror_workspace` deletes the target before copying, and the archive is written last.
- Fix (small): stop trusting a pre-existing expanded tree.
  - Simplest: in `_extract_mirror`, always extract into a fresh temporary directory and `os.replace` it over the
    target, once per coordinator process (memoize per process and name). Re-extracting also refreshes ctime.
  - Alternative: after `os.replace`, write a stamp next to the target recording the archive's
    `(size, mtime_ns)` and the member count, and re-extract when the stamp is missing or the file count differs.
    `_capture_baseline` would write the same stamp.
  - Add a test: delete one file from the extracted `mirrors/last-review` and check that a fresh coordinator's delta
    is empty and `_review_start_paths()` is complete.
  - The docs sentence "Reboot cleanup of scratch therefore preserves review comparisons"
    (`paired_session/docs/concurrent-runs.md:13`) is only true once this is fixed.

## NON-BLOCKING

- N1. In a "bare repo in `.git` plus worktrees" layout, every new run is REFUSED.
  - Where: `scripts/project_paths.py:18`.
  - The layout: `git clone --bare <url> project/.git`, then `git worktree add project/main`. From a worktree,
    `--is-bare-repository` prints `false` and the common dir ends in `/.git`, so `project_repo` returns `project/`.
    That directory is not a work tree.
  - `default_root` writes `project/.review-loop/.gitignore` there. The coordinator's `prepare_run_directory` then runs
    `git check-ignore` in `project/`, which fails, and the run is refused:
    `REFUSED: …/runs/x is not gitignored in …/project` (reproduced).
  - The skills say "never try another location", so the user is stuck unless they know to set `RUN_DIR`. The usual
    `.bare` variant of this layout correctly falls back to XDG; only the `.git`-named bare variant breaks.
  - Fix: require `git --git-dir <common> config --bool core.bare` to be false, or require
    `git -C <common.parent> rev-parse --is-inside-work-tree` to succeed, before returning `common.parent`. Otherwise
    fall back.
- N2. PR clones under `<repo>/.review-loop/pr/<id>/` are collected by the main checkout's test runner while they exist.
  - This location is an owner requirement, so it is not blocking. The clone is a full project checkout, and the entry
    contract keeps it unless the user agrees to `--remove`
    (`docs/protocol/paired-session-entry.md:267`).
  - Reproduced: `jest --listTests` in the main checkout lists both `./a.test.js` and
    `.review-loop/pr/c1/a.test.js`. Vitest's default `exclude` does not cover `.review-loop` either.
  - Suggestion: one README line for JS projects (`testPathIgnorePatterns` / `modulePathIgnorePatterns` / vitest
    `exclude`: `<rootDir>/.review-loop/`). Optionally, phrase the cleanup question so that removing the clone is the
    default answer.
- N3. Inherited `RUN_DIR` in the Codex skill.
  - Where: `.agents/skills/paired-session/SKILL.md:90`, `if [ -z "${RUN_DIR:-}" ]`.
  - A `RUN_DIR` exported in the environment Codex was started from silently overrides the project default for every
    new run. `RUN_DIR` is a generic name; before this change the block always assigned it.
  - Suggestion: always assign it, and let an operator choice be stated explicitly. Alternatively, use a namespaced
    variable such as `REVIEW_LOOP_RUN_DIR`. The PowerShell branch uses a script variable and is not affected.
- N4. Wording drift in the docs.
  - `paired_session/docs/efficient-mode.md:142` names only `runs/` and `codex-home/` as excluded. The code also
    excludes `pr/` and `logs/` (`internal_paths.py`).
  - `paired_session/README.md:218` still says "(not a Git repo or bare repo)", while the skills and README say "…or a
    submodule/separate-git-dir checkout".
  - The same CODEX_HOME / run-root paragraph is pasted into README, `paired_session/README.md`, `concurrent-runs.md`
    and `install-codex.md`. In `README.md:3` it sits above the one-line description of the plugin, so the README now
    opens with an internal-path note.
- N5. Versions are not bumped.
  - The commits state "Keep versions and CHANGELOG.md unchanged". CLAUDE.md requires `plugin.json` /
    `marketplace.json` (and the Codex manifest) to be bumped on every push that changes files, and a cached v3.3.0
    install would not pick this branch up.
  - Fine for a review branch. Make sure the merge or release commit does the bump and the changelog entry.
- N6. A new ignored file directly under `.review-loop/` is no longer noticed by the read-only check. Before, the
  collapsed `.review-loop` entry's mtime caught it; now only recorded children are compared (new ignored entries are
  never compared anywhere). A role creating `.review-loop/foo` during a read-only turn is not a realistic accident, so
  this is noted only for completeness.
- N7. On operator `abort`, `_cleanup_scratch` removes `role-tmp` even when the abort message says "a prior CLI child
  may still be running". That child loses its TMPDIR. This is acceptable for an abort, and could be mentioned in the
  abort text.

## What I verified (no problem found)

- Old B1 (workspace mirrors in the repo) is fixed. In a new-layout run in the main checkout
  (`capture_review_baseline`, a `review-start` capture, `materialize_review_context` with a changed test file),
  `.review-loop/` holds only `.gitignore`, `runs/u/{context,evidence,internal/last-review.tar,internal/review-start.tar,rounds,state.json}`.
  - `jest@29.7.0 --listTests` lists only `./a.test.js`. `vitest@1.6.0 run` runs only the two workspace files.
  - The scratch root `/tmp/review-loop-0/<hash>` is outside every work tree (`git_work_tree_root` → None).
- Old B2 is fixed: `_ignored_entries` / `ignored_changes`, with a capture before and these changes during the turn:
  - Changes made: new `runs/r2`, a write to `runs/r1/state.json`, new `pr/c2`, new `logs/b.log`, a rewritten
    `logs/a.log`, and writes under `codex-home/`.
  - Configurations tried: a fresh `.review-loop/`; a tracked `config.md`; a root `.gitignore` with
    `.review-loop/` + `!.review-loop/config.md`; both of the last two.
  - Result: `[]` in every configuration. A changed sibling (`.review-loop/other`) is still reported (branch test).
  - `evidence_guard` prunes the four dirs at `<root>/.review-loop`, and `author_write_guard` filters them, all from
    the one `INTERNAL_WORKSPACE_DIRS` tuple.
- Archive round trip: I diffed the archive-extracted `last-review` against a `cp -a` of the original mirror with
  `find -printf '%p %m %y %s %l'`. Content and metadata are identical, including exec bit 755, a 0-byte file, and a
  relative symlink (`ln -> tracked.txt`). The only difference is the tree root's own mode (0700 from `mkdtemp`, which
  is harmless).
  - The branch test also covers an absolute outside link (`/outside/target`), and the rejection of `/absolute` and
    `../escape` members.
  - Python 3.13.17 takes the `data_filter` branch. The 3.9 fallback was not executed.
- Old-layout runs with real v3.3.0 code: I created a run with `origin/main` code (HOLD at
  `--max-invocations 3`). I then put the branch's files in place at the same plugin path and ran `status` and
  `resume --max-invocations 60 --skip-probe`.
  - The run reached `DONE (acceptance pending)`. `scratch_root` was never added to the state.
  - `author-tmp`, `role-tmp` and `internal/last-review` stayed under the run dir (legacy layout), and the FIELD-28
    warning still fires for the in-repo legacy run dir.
  - Running the branch coordinator from a different path instead gives the usual "operator program changed" HOLD,
    which is expected for any version switch and not caused by this branch.
- Run-dir admission (`prepare_run_directory`): only paths strictly under a gitignored `<ws>/.review-loop/runs/` are
  accepted inside the workspace. An existing `.gitignore` is never overwritten (`open('x')`). A lane workspace with
  runs in the main checkout writes and checks the ignore file in the main checkout. The XDG fallback layout
  (`review-loop/runs`, no dot) is not mistaken for a repo root.
- `materialize_pr.py`: clones are allowed only at exactly `<repo>/.review-loop/pr`, after `ensure_ignore` and
  `check-ignore`. For review-pr, the skills resolve the run root from the operator project before switching
  `WORKSPACE` to the clone, so runs land beside `pr/` and not inside the clone.
- Scratch for new runs: role TMPDIRs, `author-tmp` (now 0700), author probe trees, OS-probe targets and the
  `codex-sandbox-home-*` dirs all derive from `scratch_base`.
  - The Codex policy-digest regex and the `run_dir_inodes` exclusion follow it.
  - It is removed on ACCEPTED, REPORTED or ABORTED and on operator abort. It is not recreated for terminal states, and
    it is recreated with 0700 plus an owner check on a non-terminal resume.
  - The scope-change handover archives the creation tree into the successor's `internal/history-start.tar` before
    cleaning the parent's scratch.
- Windows (reviewed, not executed): `temp_root()` is `%TEMP%`. Mirror and scratch cleanup use `winplat.rmtree`.
  `os.replace` of a fresh directory onto a non-existent target is valid. `project_paths` handles `C:/…/.git`. The Git
  Bash and PowerShell recipes capture only stdout; the fallback notice goes to stderr.

## Tests run (Linux container, root, git 2.43, Python 3.13.17)

- `python3 -m pytest -q tests`: **213 passed, 9 subtests passed**.
- `python3 -m unittest paired_session.test_run_root_in_repo paired_session.test_field28 paired_session.test_readonly_scratch paired_session.test_review_only_entry paired_session.test_review_mirror_cwd paired_session.test_pr1_fixes paired_session.test_operator_roles`:
  **Ran 220, 1 failure, 4 skipped**.
  - The 4 skips are all "root ignores file permissions/directory modes" (`test_pr1_fixes:315,320`,
    `test_operator_roles:2105,2140`). The container runs as root; these are not macOS-only skips.
  - The failure is
    `test_readonly_scratch.ProbeTrackedFileTests.test_a_glob_like_name_is_checked_out_literally_and_a_sibling_keeps_its_unstaged_bytes`.
    It is pre-existing: I re-ran it alone on an `origin/main` worktree and it fails the same way there.
- `scripts/run-skill-lint`: review-loop 141 passed, shared-schema 1 passed, 0 FAIL.
- Not run: the full `paired_session` discover suite, real Codex/Claude sandboxes (fake CLIs only), macOS, and native
  Windows.
- Ad-hoc reproductions (scratch scripts, not committed):
  - jest/vitest collection on a new-layout run, and jest collecting a retained PR clone (N2);
  - the ignored-entry check with pr/logs/runs/codex-home writes in four `.gitignore` setups;
  - the archive-versus-mirror metadata diff;
  - partial-mirror delta corruption (B1);
  - a bare-`.git` worktree layout refused (N1);
  - the v3.3.0 → branch resume.
