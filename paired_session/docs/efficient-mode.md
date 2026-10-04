# Efficient safety mode (D-EFF, owner 2026-10-04, amended)

Owner decision D-EFF, amended:
> "A类+B类的…这些要求还是要在以后的review-loop里遵守，但是c类的严格沙箱要求，不需要再加固或者花时间开发了"

Categories A and B are mandatory in every mode. Only category C is dropped from the default.

| Category | What it requires | efficient (default) | strict |
|---|---|---|---|
| A, collaboration integrity | Read-only roles do not change the workspace. Roles cannot alter the coordinator records. Test results never come from a model's claim. | kept | kept |
| B, the operator's machine | The author writes only the workspace and its own tmp. No role writes other repos, the home dir or global `~/.claude` / `~/.codex`. Secret env vars are kept from roles. The git guard. | kept | kept |
| C, strict sandbox assurance | A permission-probe PASS before dispatch; an evidence guard that holds; further sandbox hardening. | dropped | as today, frozen |

**Efficient is not yolo.** Both modes run every role with today's sandbox flags. Efficient differs from strict only in category C.

## 1. Choosing the mode

- `safety_mode` is chosen by the command that creates the run state, usually `permission-probe`, and saved in `state['config']`.
  - Pass `--strict` from `permission-probe` on.
  - A request that differs from the saved mode is refused by `run`, `resume`, `reject` and `permission-probe`, with one exception: `--strict` after only probe turns (none `active` or `uncertain_active` either) upgrades the run. This is a tightening, and the probe report stays valid because the flags are identical.
  - Commands that dispatch no turn (`status`, `abort`, `accept`, `note`, `attach-verification`) print a NOTE and keep the saved mode.
  - A run saved before D-EFF writes `safety_mode: strict` into its successor config.
- **Default:** `efficient` (module constant `DEFAULT_SAFETY_MODE`).
- **Strict is opt-in:** `--strict`, or `"safety_mode": "strict"` in an operator profile given with `--config FILE`.
  - The workspace config `<workspace>/.review-loop/paired-session.json` cannot set it; the key is refused there.
  - Valid values are `efficient` and `strict`.
- **Migration:** a saved state without `safety_mode` (any run made before D-EFF) resumes as `strict`.

## 2. CLI flags: the same in both modes

Every role keeps today's flags in both modes. Nothing in this change touches `_claude_command`, `_codex_command` or `_claude_sandbox_settings`.

| Role | Claude | Codex |
|---|---|---|
| Author | `--permission-mode acceptEdits`; path-scoped `--allowedTools Edit(//<workspace>/**)`; the `--disallowedTools` deny rules for the context, the probe cache and `~/.claude` `~/.codex` `~/.ssh` `~/.aws`; `--settings` with the Bash OS sandbox: `denyWrite` the run dir, the credential env-var and file deny lists, no network | `-c approval_policy="never" -c sandbox_mode="workspace-write"`, writable roots = author-tmp, no network, `exclude_slash_tmp`, `TMPDIR=author-tmp`, `--ignore-rules` |
| Reviewer, gate, shadow, probe | `--restricted --permission-mode dontAsk --tools Read,Grep,Glob,Bash`, exact `Bash(<cmd>)` allow rules, Edit and Write denied, the same OS sandbox | `-c sandbox_mode="read-only"`, `--ignore-rules` |

What these flags deliver:
- **B:** the author writes only the workspace and author-tmp; global and home config are denied; secret env vars are denied to Claude's Bash; Codex's shell excludes secret-named variables by default.
- **A(b):** the run dir is not writable by any role. The author's TMPDIR, `run_dir/author-tmp`, holds no records.

**Owner question (friction).** Friction must be listed with evidence before any flag is removed, and this change removes none. Recorded so far: the read-only roles' exact `Bash(<cmd>)` allowlist makes a reviewer that runs an unlisted command get a denial, not a run. The supervisor may want to collect real-run evidence on this.

## 3. Category C gates

| Gate | strict | efficient |
|---|---|---|
| Permission probe before run, resume and reject (`probe_gate`); `_probe_gate_required` re-checks after an uncertain turn | required | not required. `permission-probe` still runs and its report is still recorded |
| Claude author opt-in or probe PASS (`claude_author_verified`, in `_execute_locked` and `drive`) | required | not required |
| Codex CLI sandbox-contract verification (`codex_contract_verified`) | required | not required |
| `--accept-probe-skip`, `--accept-unverified-claude-author`, `--accept-unverified-codex-cli` | honoured | unnecessary: accepted but not recorded or checked |
| Evidence guard (`sensitive_access`) | holds | log-only: the reason goes to `receipt['evidence_guard']['would_hold']` |
| Codex capability scan (`codex_capabilities`: MCP servers, profiles, permissions in the Codex config) | refuses | refuses. It keeps the sandbox configuration honest (B) |

## 4. Category A and B detection, in both modes

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

**Change from today:** strict mode used to HOLD at once, with no restore. The probe roles keep `allow_mutation_report`.

**Cost:** one `git add -A` into a temporary index per read-only turn. It is stat-cached, so a large repository with few changes stays fast.

**Not covered:** ignored files (as today), submodule contents, refs other than HEAD and its branch, and files outside the workspace.

### b. Coordinator records

- `main` refuses `--run-dir` inside `--workspace`.
- The sandbox flags deny the run dir to every role.
- The context digest is checked after every turn.
- Tests verify all three in efficient mode.

### c. Test results never come from a model's claim

- An EXEC approval needs an observed successful run of the configured test command (`observed_test_succeeded`). The exit code and output come from the vendor CLI's event stream, not from the model's `self_run_evidence`. This holds in both modes.
- **Owner question:** outside the fake lifecycle, the coordinator does not run the test command itself. The owner's wording, "coordinator-run test command", would be a new step: run the configured test after the author turn and gate EXEC on that result. Not built here.

### d. Git guard (B)

- A HEAD or branch change in an author turn is a HOLD ("author changed HEAD or the branch"), in both modes. Read-only roles are covered by **a**. Index staging by the author is allowed.
- The author's sandbox keeps it out of other repos. Its edits inside the workspace, a discard of unrelated work included, appear in the candidate diff that the reviewer and gate judge.

### Global config and secret env (B)

- The global Codex and Claude config check (`global_config_changes`) and the env-var deny list are the same in both modes. Tests verify them in efficient mode.

## 5. Docs

`README.md` (an addition only; the lint-SSOT text is not trimmed) and the paired-session `SKILL.md` must say plainly:
- the sandboxes apply in both modes;
- efficient (the default) does not require a permission-probe PASS, and its evidence guard only logs;
- strict (`--strict` or an operator profile) also requires the probe PASS and lets the evidence guard hold.

## 6. Lane A hook (not edited here)

- D-7 ("lifecycle refuses probe waivers") is W1a's `worktree_lifecycle.refuse_waivers`. Since lane A INT-2c it applies only when `Coordinator.strict` is true; an efficient lifecycle run notes a waiver as unneeded and records none, like any efficient run.
- The fake-lifecycle candidate-test sandbox (`candidate_test_sandbox.run`) is a sandbox requirement, not a probe gate. It stays in both modes.

## 7. Tests

- **Strict pin.** Existing tests keep strict without assertion edits:
  - `RealCoordinatorTests.setUp` patches `DEFAULT_SAFETY_MODE='strict'` in both coordinator module objects (`real_coordinator` and `paired_session.coordinator`);
  - `RealCoordinatorTests.command()` appends `--strict`.
- **New tests:**
  - mode selection: the default, `--strict`, the operator profile, the workspace config refused, an old state resumes strict, the mode fixed on resume;
  - efficient mode: no probe needed; the evidence guard log-only; the sandbox flags identical to strict;
  - in efficient mode: run-dir placement and the context check, the observed-test requirement, the global-config check and the env deny list;
  - the author HEAD guard;
  - eff-b: a reviewer or gate write voids the turn, restores, re-dispatches once and then HOLDs; an unverifiable restore HOLDs.
- **Sub-batches:**
  - **eff-a:** mode plumbing, category C gating and the author HEAD guard;
  - **eff-b:** read-only-turn detection, restore and re-dispatch.
