# Efficient safety mode (D-EFF, owner 2026-10-04, amended; the only mode since v3.7.0)

For CODEX_HOME provisioning, run roots and fallback rules, see the [project overview](../../README.md).

Owner decision D-EFF, amended:
> "A类+B类的…这些要求还是要在以后的review-loop里遵守，但是c类的严格沙箱要求，不需要再加固或者花时间开发了"

Categories A and B are mandatory. Category C was dropped: first from the default (D-EFF), then entirely in v3.7.0
(owner decision 2026-11-08), which removed strict mode and the permission probe. Efficient is the only mode.

| Category | What it requires | Since v3.7.0 |
|---|---|---|
| A, collaboration integrity | Read-only roles do not change the workspace. Roles cannot alter the coordinator records. Test results never come from a model's claim. | kept |
| B, the operator's machine | The author writes only the workspace and its own tmp. No role writes other repos, the home dir or global `~/.claude` / `~/.codex`. Secret env vars are kept from roles. The git guard. | kept |
| C, strict sandbox assurance | A permission-probe PASS before dispatch; an evidence guard that holds; further sandbox hardening. | dropped (strict mode removed) |

**Efficient is not yolo.** Every role runs with the sandbox flags below. Only category C is gone.

## 1. The mode

- `efficient` is the only mode (module constant `DEFAULT_SAFETY_MODE`).
- `--strict` on the command line, or `"safety_mode": "strict"` in any profile (operator or workspace), prints one NOTE
  (`NOTE: strict mode was removed in v3.7.0; the run uses the default mode`) and the run continues in the default mode.
- A run saved strict resumes in the default mode with the same NOTE; its saved config is left as saved. A run saved
  without `safety_mode` (made before D-EFF) resumes in the default mode.
- `"safety_mode": "efficient"` stays accepted; any other value is refused.
- `permission-probe` is refused before any state, lease or run dir: `REFUSED: permission-probe was removed in v3.7.0; runs
  do not need it` (exit 2).

## 2. CLI flags

Every role keeps the flags it had before v3.7.0. Removing strict mode did not change `_claude_command`, `_codex_command` or `_claude_sandbox_settings`.

| Role | Claude | Codex |
|---|---|---|
| Author | `--permission-mode acceptEdits`; path-scoped `--allowedTools Edit(//<workspace>/**)`; the `--disallowedTools` deny rules for the context, the former probe-cache path (kept so a v3.6.0 frozen role dispatch manifest stays byte-identical) and `~/.claude` `~/.codex` `~/.ssh` `~/.aws`; `--settings` with the Bash OS sandbox: `denyWrite` the run dir, the credential env-var and file deny lists, network on (v3.6.0) | `-c approval_policy="never" -c sandbox_mode="workspace-write"`, writable roots = author-tmp and `extra_writable_roots`, network on (v3.6.0), `exclude_slash_tmp`, `TMPDIR=author-tmp`, `--ignore-rules` |
| Reviewer, gate, shadow | `--restricted --permission-mode dontAsk --tools Read,Grep,Glob,Bash`, exact `Bash(<cmd>)` allow rules, Edit and Write denied, the same OS sandbox | v3.6.0: the author's shape, `-c sandbox_mode="workspace-write"`, writable roots = the workspace, the dispatch's own `$TMPDIR` scratch root and `extra_writable_roots`, network on, `exclude_slash_tmp`; `--ignore-rules` (the guard in §4a still undoes tracked and non-ignored changes) |

What these flags deliver:
- **B:** the author writes only the workspace and author-tmp; global and home config are denied; secret env vars are denied to Claude's Bash; Codex's shell excludes secret-named variables by default.
- **A(b):** a Codex author's workspace-write covers `.review-loop/runs/` when the workspace is the main checkout. A Claude author is admitted with the same in-repo run dir (v3.4.1): the settings deny `Edit(//<run dir>/**)` and the Bash `denyWrite` on the run dir override its workspace allow, so its own run stays protected; other runs' dirs there do not. Under the owner threat model (users and models do not act maliciously), accidental damage is the residual risk; do not run bulk rewrites or `git clean -x` over the workspace during a run. The author's TMPDIR, `<scratch_root>/author-tmp` outside any repo, holds no records
  (legacy saved runs retain `run_dir/author-tmp`).

**Native Windows exception (v3.3.0, ADR-20 D1).** Claude Code has no Bash sandbox on native Windows, so there
`_claude_sandbox_settings` sets `sandbox.enabled` and `sandbox.failIfUnavailable` to false; the rest of the settings
keeps its shape and the macOS settings are byte-identical. `--detach` and gemini roles are refused before any lease or
state write; `--strict` and a strict run get the NOTE of §1, as on macOS.
- Claude read-only roles keep `--restricted --permission-mode dontAsk`, the exact `Bash(<cmd>)` allow rules and the
  Edit/Write/NotebookEdit/Agent denies, with no OS layer under them; the post-turn read-only workspace check (§4a)
  still voids, restores and holds. On Windows it compares content and existence, not permission bits.
- The Claude author's Bash runs unsandboxed: its Edit/Write stay limited by the path rules (written in Claude Code's
  Windows form `//c/Users/...`), but its Bash can write outside the workspace, the run dir included. The context
  digest, the global-config check and the other post-turn checks still run; a Bash write to the run dir's records
  (for example `state.json`) is neither prevented nor detected.
- Secret-named env vars are not hidden from Claude's Bash. Instead `permissions.deny` gets Read rules for the
  credential paths the sandbox hid (`~/.aws`, `~/.ssh`, `~/.config/gcloud`, `~/.config/gh`, `~/.azure`,
  `~/.docker/config.json`, `~/.git-credentials`, `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.kube/config`) for every
  Claude role; Bash can still read those files.
- Codex roles keep their flags and add `-c windows.sandbox="elevated"` (W10: without it every shell command is
  rejected by policy). Their scratch and the author's temp dir are given as `TMPDIR`, `TMP` and `TEMP`.
These are accepted residuals under the owner rule: reasonable protection for normal use, assuming users and models do
not act maliciously.

**Owner question (friction).** Friction must be listed with evidence before any flag is removed, and this change removes none. Recorded so far: the read-only roles' exact `Bash(<cmd>)` allowlist makes a reviewer that runs an unlisted command get a denial, not a run. The supervisor may want to collect real-run evidence on this.

## 3. Category C gates (removed in v3.7.0)

| Former gate | Since v3.7.0 |
|---|---|
| Permission probe before run, resume and reject | removed; `permission-probe` is refused. No probe report, probe cache, probe lock or gate probe exists |
| Claude author probe PASS | removed; a real Claude author is never refused |
| Codex CLI sandbox-contract verification (the verified codex-cli version list) | removed |
| `--accept-unverified-codex-cli`, `--accept-probe-skip`, `--accept-unverified-claude-author` | refused as removed options ("removed in v3.7.0; runs need no probe or waiver") |
| `--skip-probe` | a hidden no-op |
| Evidence guard (`sensitive_access`) | log-only: the reason goes to `receipt['evidence_guard']['would_hold']` |
| Codex capability scan (`codex_capabilities`: MCP servers, profiles, permissions in the Codex config) | still refuses. It keeps the sandbox configuration honest (B) |

**Program and gate-prompt changes without a probe.** When the operator programs or PATH changed since run start, the
next dispatch records the new state (state key `program_refreezes`) and prints one NOTE ("operator program or PATH
changed since run start (changed: X); the run continues with the new state"); a missing or unsafe program still HOLDs. A
run started by another release, whose bundled gate prompt differs, continues with this release's gate prompt after one
NOTE (`gate_prompt_refreezes`); for a lifecycle run only the gate-prompt fields of the frozen role dispatch manifest are
updated. With lifecycle off, a changed reviewer-commands block of the work item is recorded with a NOTE
(`reviewer_command_refreezes`); with lifecycle on it still HOLDs ("restore the reviewer-commands block of the work item
and resume, or abort and start a new run").

**Saved runs from earlier releases.** The state keys `permission_probe`, `permission_probe_superseded`,
`probe_skip_override`, `claude_author_override` and `residual_risk` are tolerated and ignored. An uncertain `PROBE` or
`AUTHOR_PERMISSION_PROBE` turn saved by an earlier release is settled with `resume --retry-uncertain`.

## 4. Category A and B detection

### a. Read-only roles (reviewer, gate, shadow) do not change the workspace (sub-batch eff-b)

**Before each such turn** the coordinator records:
- HEAD and the symbolic ref;
- the index as a `git ls-files -s` listing (content-level, so a stat refresh by `git status` does not count);
- the worktree as today's `git_snapshot`: tracked and non-ignored untracked files by content hash, with symlinks as `link:<target>`.

For a restore it also keeps a byte copy of `.git/index` and a tree of the worktree (a temporary index seeded from the real one, then `git add -A` and `git write-tree`).

**After the turn**, any difference voids the turn, and its verdict is never used:
- the receipt records `voided`;
- `evidence/NNN-<role>-workspace-change.diff` holds the diff from the pre-turn tree to the post-turn tree, plus the HEAD, ref and index changes.

**Restore** happens only from these records:
- set the ref or HEAD back;
- write the saved tree with `checkout-index`;
- delete the non-ignored files the turn added;
- write the saved index bytes back;
- then **verify** that all four values equal the pre-turn values.

If the restore verifies, the turn is re-dispatched once. If it does not, or the capture failed, the run HOLDs, naming the evidence file. A second violation is restored and HOLDs.

Before D-EFF, strict mode held at once, with no restore.

**Cost:** one `git add -A` into a temporary index per read-only turn. It is stat-cached, so a large repository with few changes stays fast.

**Not covered:** the content of ignored files (it is never kept or restored), submodule contents, refs other than HEAD and its
branch, and files outside the workspace. A change of the executable bit alone is detected since lane A rel210-fixA (the snapshot
marks executable files). Every permission bit (for example `0644` to `0600`, or the executable bit) is detected and undone since
lane A roperm: `capture()` also records `st_mode & 0o7777` of every tracked and non-ignored untracked regular file (one lstat each,
no content read); a change voids the turn like any other change, the undo chmods the recorded bits back after the tree restore
(so it verifies with `core.fileMode` false too, where git records no mode), and the evidence file lists `mode:` lines. A failed
undo records the bits still wrong (a path now missing or no longer a regular file included), and the run stays refused until they
are back.
Delivery is unchanged: git does not record those bits, so they are never reviewed or delivered (auto_commit takes modes from the
accepted manifest). Residuals: the bits of ignored files (their type, size, mtime and mode are only checked by the ignored-entry
lstat below), of symlinks and directories, and ACLs, extended attributes and file flags.
A workspace below the repository top is not a case: the coordinator requires `.git` in the workspace.

**Ignored entries (eff-e).** For Claude and Gemini read-only roles the read-only sandbox is the first barrier and this is the second
line. Codex reviewer-type roles (workspace-write since v3.6.0) may write ignored paths: a changed
entry is recorded in the receipt as `readonly_ignored_kept` and the turn stands.
- `capture()` records the lstat metadata (type, size, mtime_ns, mode, symlink target) of each ignored entry exactly as
  `git ls-files -o -i --exclude-standard --directory` lists it, so a collapsed directory is one entry.
- For a Claude or Gemini role, an entry that changed or is gone after the turn voids the verdict and HOLDs with the changed paths.
  Nothing is restored, and there is no re-dispatch.
- **Residuals:** a change inside a collapsed ignored directory that leaves the directory entry itself unchanged, and a new ignored
  file outside the recorded entries, are not detected.
- The capture is bounded (`IGNORED_LIMIT` entries, `IGNORED_SECONDS`). Past either bound the receipt says `ignored set too large:
  not checked`.
- For a Claude or Gemini role, a CLI home or cache the CLI writes to inside an ignored workspace path (for example `__pycache__`
  or a test runner cache such as `.pytest_cache`, `target/` or `node_modules/.cache` when the vendor sandbox allows the write)
  HOLDs every read-only turn. Keep such caches outside the workspace. `.review-loop/runs/`, `.review-loop/codex-home/`, `.review-loop/pr/` and `.review-loop/logs/`
  are excluded from workspace scans, so the recommended isolated homes may live there.

**An unreadable workspace after the turn (eff-e)**, for example a broken `.git/index`, is an unverifiable violation. The
unrestored record keeps the pre-turn baseline, and dispatch and accept refuse until the workspace matches it again.

**A failed undo (eff-c)** leads the HOLD reason ("read-only turn changed the workspace and it could not be restored; manual restore
needed"), whatever else the turn tripped, and is recorded as `state['unrestored_readonly_turn']`. `run`, `resume`, `reject`,
`accept`, a scope change and their `--intent-only` refuse while it stands; once the workspace is back to the pre-turn snapshot, HEAD,
branch and index, the next of them clears it. A failed tree capture still records HEAD, branch and index before the turn, so a commit
or branch switch is still seen.

**The turn's process group is stopped before the undo (lane A INT-2c).** If it cannot be verified stopped, nothing is restored (a
process left in it could change the tree again) and the turn is recorded as unrestored in the same way; the operator stops that
turn's processes, then restores the workspace by hand.

### b. Coordinator records

- `main` refuses `--run-dir` inside `--workspace`.
- The sandbox flags deny the run dir to every role.
- The context digest is checked after every turn.
- Tests verify all three in efficient mode.

### c. Test results never come from a model's claim

- An EXEC approval needs an observed successful run of the configured test command (`observed_test_succeeded`). The exit code and output come from the vendor CLI's event stream, not from the model's `self_run_evidence`.
- **Owner question:** EXEC approval is not gated on a test run of the coordinator's own. The coordinator runs the configured test command itself only for the quality writers' baseline and checks (`_test_run`, D09). The owner's wording, "coordinator-run test command", would be a new step: run the configured test after the author turn and gate EXEC on that result. Not built here.

### d. Git guard (B)

- A HEAD or branch change in an author turn is a HOLD ("author changed HEAD or the branch"). Read-only roles are covered by **a**. Index staging by the author is allowed.
- The author's sandbox keeps it out of other repos. Its edits inside the workspace, a discard of unrelated work included, appear in the candidate diff that the reviewer and gate judge.

### Global config and secret env (B)

- The global Codex and Claude config check (`global_config_changes`) and the env-var deny list apply. Tests verify them.
- A normal plugin update is kept (FIELD-21, poker-news-bot WI-102). A new Claude Code session outside the run can rewrite a plugin's entry in `installed_plugins.json` when it materializes a new plugin version. That happened in WI-102, where review-loop went from 2.10.0 to 2.10.1. `normal_plugin_update` recognizes such a normal update precisely:
  - the document outside `plugins` is unchanged;
  - the plugin keys, the entries and each entry's keys are unchanged;
  - a changed entry differs only in the scalar values of `version`, `installPath`, `gitCommitSha` and `lastUpdated`;
  - a changed `installPath` is the canonical cache directory `<plugins>/cache/<marketplace>/<plugin>/<new version>`, present on disk as a real directory.

  The update is recorded as `global_config_changes.plugin_update`.
  - FIELD-24 (supervisor 2026-10-06; it replaces FIELD-21's void and re-dispatch): the turn is kept. The running CLI loaded its plugins at start, and the old versioned cache directory stays on disk, so the turn's work is unaffected. The finding is cleared, and the receipt adds `next_turn_registry` ("the next turn starts on the updated plugin registry: <plugin> <old> -> <new>"), so a reader sees the version change between turns. Nothing is voided or re-dispatched, and a later update in another turn is recorded the same way; there is no second-update HOLD. FIELD-24 came from poker-news-bot WI-109, where a release relaunch voided a 78-minute author turn.
  - Any other change, or a turn that also tripped another check (a workspace void, a run-dir touch, or a later check of the same turn), is a hard finding as before. RF-4's version/lastUpdated bump is one case of a normal update.
- V325-CFG (gg-pipeline WI-266h): a read-only Claude turn (reviewer, shadow or gate) whose only finding is a changed `settings.json` in its CLI's global config home is kept. Such a turn cannot write that file, so another process (the owner's interactive session, another run) changed it. The receipt records `concurrent_settings_change` (before/after sha256), a NOTE is printed, and the next turn takes its own baseline. Author and writer turns, a change together with another finding and a voided turn still hold. A global-config HOLD message now ends with `GLOBAL_CONFIG_HINT`: the change may come from another process on the machine, and `resume` re-runs the turn (a normal plugin update keeps `PLUGIN_UPDATE_HINT`).

## 5. Docs

`README.md` and the paired-session `SKILL.md` must say plainly:
- every sandbox applies;
- there is no permission probe and no strict mode, and the evidence guard only logs.

## 6. Lifecycle hook (D-7)

- D-7 ("lifecycle refuses probe waivers") was W1a's strict-only internal `worktree_lifecycle.refuse_waivers` check. Since
  v3.7.0 every waiver flag is refused at the public surface as a removed option, and no waiver is needed.

## 7. Tests

- Before v3.7.0 the strict pin (`RealCoordinatorTests` patched `DEFAULT_SAFETY_MODE='strict'` and appended `--strict`)
  kept the older tests on strict. v3.7.0 removed the pin and the probe tests.
- `test_efficient_mode.py` covers: `--strict`, a strict profile and a run saved strict print the NOTE and run in the
  default mode; an unknown `safety_mode` is refused; no probe or opt-in is needed and the evidence guard only logs;
  waiver flags are refused as removed options; every role keeps its sandbox flags; run-dir placement, the observed-test
  requirement and the global-config check; the author HEAD guard.
- `test_readonly_guard.py` covers eff-b: a reviewer or gate write voids the turn, restores, re-dispatches once and then
  HOLDs; an unverifiable restore HOLDs.
- **Sub-batches (history):**
  - **eff-a:** mode plumbing, category C gating and the author HEAD guard;
  - **eff-b:** read-only-turn detection, restore and re-dispatch.
