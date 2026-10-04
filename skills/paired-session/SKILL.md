---
name: paired-session
argument-hint: "<work item> [--plan-only]"
description: >
  Drive an implementation through the paired-session coordinator: independent
  PLAN review, EXEC implementation/review, adversarial gate, then finish,
  quality polish, docs and security up to operator acceptance.
  Trigger in exactly three cases: (1) the user explicitly asks for paired-session
  or names this explicit coordinator entry; (2) the review-loop entry hands off
  because .review-loop/config.md sets `entry: paired-session`; (3) the review-loop
  entry hands off because that key is absent (the default entry). Do not trigger on
  a bare review-loop request in any other case (key invalid, `legacy`, or
  /review-loop:legacy).
---

# Paired-session workflow

Use the shared coordinator shipped with this plugin. Do not load or invoke the
legacy review-loop workflow for this task.

How this skill was entered decides failure handling (stage A = everything before
the first command that runs `bin/paired-session`):
- Default entry (review-loop handoff with the `entry` key absent): a failed
  stage A check is reported back as `stage A failure: <reason>`; the review-loop
  entry then prints its fallback notice and runs legacy.
- Explicit entry (`entry: paired-session` handoff, or the user asked for
  paired-session): a failed stage A check refuses with the reason, except that
  unavailable background execution is reported as HOLD with the reason. On an
  `entry: paired-session` handoff, print `review-loop: paired-session entry refused
  (<reason>); set "entry: legacy" or use /review-loop:legacy`.
- From the first `bin/paired-session` command on, every refusal or HOLD is
  reported verbatim and never falls back to legacy.

1. Stage A checks. When the user invoked this skill directly, first check that
   every CLI the resolved roles need is on PATH (`command -v`; the default roles
   need `codex` and `claude`); the review-loop entry has already checked this
   on a handoff. Identify the intended Git worktree. Use a dedicated task
   worktree; preserve unrelated user changes and do not switch away from a dirty
   checkout. If a dedicated worktree is unavailable, ask before creating one.
   Determine the project's test command from its docs/manifests and ask only if
   it cannot be established safely. When a Claude role (reviewer or gate) runs
   it, the command must be one exact command without `$(`, backticks, `|`, `;`,
   `&&`, redirection or loops; otherwise ask for a
   `/bin/bash /absolute/path/script.sh` form. A declined or unanswered question
   is a failed stage A check. Under `--handsfree` or `handsfree: true` nobody
   answers, so any such question is a failed stage A check, reported as
   `stage A failure: handsfree cannot answer (<question>)`.
2. Profile and settings. Use the operator profile the user names; otherwise
   use `~/.config/review-loop/paired-session.json` when it exists. Pass its
   absolute path with `--config` on every call. It replaces
   `<workspace>/.review-loop/paired-session.json`, which may set non-program
   limits only; copy any desired workspace limits into the operator profile.
   Role/vendor/program/test-command settings belong in the operator profile,
   outside the workspace and run directory. CLI options override the profile.
   Models are operator-set (ADR-9); a role without one gets its vendor's default
   (`claude-opus-5-5` for Claude; `gpt-6.1-sol` for Codex), and the gate defaults
   to the author's vendor (ADR-10).
   Legacy keys in `.review-loop/config.md` that are set map to one-run options:
   `docs_file` → `--docs-file`, `skip_quality_polish` → `--skip-quality-polish
   true|false`, `soft_limit_plan` / `soft_limit_exec` → `--max-plan-rounds` /
   `--max-exec-rounds`. Do not apply, but print a warning for, `auto_commit: true`
   (`review-loop: auto_commit in .review-loop/config.md is not applied by
   paired-session; set it in the operator profile`) and a `reviewer_model` /
   `executor_model` set to anything other than empty or `inherit` (models come
   from the operator profile, ADR-9). Never pass
   `--adversarial-gate off`.
3. Create a UUID. Store the work item and run state under
   `${CLAUDE_PLUGIN_DATA}/runs/<UUID>/`; this location must remain outside the
   product worktree. Create the directory, then write `WORKITEM.md` there with
   goal, acceptance criteria, scope, and verification. Include only
   user-approved requirements; mark uncertainties as questions instead of
   inventing acceptance criteria. Resolve `WORKSPACE` to the intended worktree
   root and `WORKITEM`/`RUN_DIR` to absolute paths. Claude Code substitutes
   `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` in skill content. Define
   path variables anew in each Bash tool call; shell variables do not persist
   across separate calls. Set `TEST_COMMAND` from the loaded profile or
   verified project command, and pass it as one quoted argument. Write any
   launcher log under `${CLAUDE_PLUGIN_DATA}/logs/`, never under `runs/`.
4. For a new run, pass `--lifecycle-mode on` to both `permission-probe` and `run`,
   identically; the CLI value overrides any profile value. Never pass
   `--skip-probe`, `--accept-unverified-codex-cli`,
   `--accept-unverified-claude-author`, `--accept-probe-skip` or
   `--override-rejection` on your own initiative.
   `--strict` comes only from the user or the operator profile (`safety_mode`),
   and goes to `permission-probe` and `run` alike:
   both modes keep every sandbox; the default `efficient` mode does not require
   the probe PASS and its evidence guard only logs, while `--strict` restores
   both (`paired_session/docs/efficient-mode.md`).
   Start the permission probe as a separate Bash call with
   `run_in_background: true`; wait for the background task completion
   notification and inspect its final result. Use a shell-output polling tool
   only if the host exposes one; do not assume a tool named `BashOutput` exists.
   Never run a command that can dispatch model turns (`permission-probe`, `run`,
   `resume`, `reject --expect`) in a foreground Bash call or extend the foreground
   timeout; long model turns exceed tool limits and can orphan a child CLI. If
   background execution is unavailable, that is a failed stage A check.

   First Bash call (`run_in_background: true`):

   ```sh
   WORKSPACE='/absolute/path/to/worktree'
   WORKITEM='${CLAUDE_PLUGIN_DATA}/runs/<UUID>/WORKITEM.md'
   RUN_DIR='${CLAUDE_PLUGIN_DATA}/runs/<UUID>'
   TEST_COMMAND='the verified project command'
   "${CLAUDE_PLUGIN_ROOT}/bin/paired-session" permission-probe \
     --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
     --test-command "$TEST_COMMAND" --lifecycle-mode on
   ```

   Exit 0 means PASS or PASS_RESIDUAL_RISK; any other result is a HOLD: report it
   and stop. On exit 0, read `RUN_DIR/permission-probe.json` and tell the user if
   the status is PASS_RESIDUAL_RISK, then read the frozen `config` in
   `RUN_DIR/state.json` and
   print one start line from it: author, reviewer and gate vendor and model;
   plan and exec rounds, invocations and timeout; docs file and
   skip-quality-polish; and which values came from `.review-loop/config.md`. If
   `config.lifecycle_mode` is not `on`, run `abort` with the run's saved options
   and report a plugin version mismatch instead of starting the run. Otherwise
   make a second Bash call (`run_in_background: true`) with the same resolved
   values:

   ```sh
   WORKSPACE='/absolute/path/to/worktree'
   WORKITEM='${CLAUDE_PLUGIN_DATA}/runs/<UUID>/WORKITEM.md'
   RUN_DIR='${CLAUDE_PLUGIN_DATA}/runs/<UUID>'
   TEST_COMMAND='the verified project command'
   "${CLAUDE_PLUGIN_ROOT}/bin/paired-session" run \
     --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
     --test-command "$TEST_COMMAND" --lifecycle-mode on
   ```

   Add the same `--config` and mapped one-run options to both calls. Add
   `--stop-after-plan` only to `run` when requested. Pass arguments as an
   array; do not interpolate untrusted text into shell source. Wait for each
   background call to finish before starting the next; do not create a second
   run while one is active. While the run is active, do not call plain
   `status`: it needs the run lease and, while the run holds it, prints
   `HOLD: another coordinator currently owns this run` although the run is not
   on HOLD. Read `RUN_DIR/state.json` directly or use `status --brief`. As a
   backstop, if the running state shows `config.lifecycle_mode` other than `on`,
   stop the background command, wait until the child in `state.active` has
   exited, run `abort` with the run's saved options, and report a plugin
   version mismatch.
5. Later commands on an existing run (`resume`, `permission-probe
   --retry-uncertain`, `abort`, `reject`, `accept`, `note`,
   `attach-verification`) pass the saved `state.json` `config.lifecycle_mode`
   value and the run's original workspace, work item, run directory, profile
   and options, never the current default. Report DONE/HOLD and the run
   directory. On HOLD, inspect its state, findings, and receipts before
   resuming. If `uncertain_active` is present, do not rerun the probe or resume
   automatically: check its pid and receipts; if the child is still alive, wait
   for it to stop. If its phase is `PROBE` or `AUTHOR_PERMISSION_PROBE`, ask
   before rerunning the disposable probe with `permission-probe
   --retry-uncertain`. For a product-work turn, ask before `resume
   --retry-uncertain` because this may replay a model turn. After recovering an
   interrupted probe, use `resume` on the existing run directory; do not use
   `run` again. Run resume in a separate Bash call with `run_in_background:
   true`; wait for background completion and inspect its final status.
6. DONE. With the saved `config.lifecycle_mode` `on`, DONE means the security
   stage passed and acceptance is pending; with `off` (a run started before
   v2.10.0), DONE has no finish, quality-polish, docs or security stages, so say
   so. Report the stage receipts, open findings, operator verification records
   still valid for the tree, and whether the operator profile's `auto_commit`
   will make one local commit on acceptance; offer `accept` or `reject`. Never
   accept or reject under handsfree, and never on your own judgment:
   - Accept only after the user explicitly accepts in this conversation. Run
     `accept --intent-only` with the user's reason as `--reason TEXT` (or no
     `--reason` if they give none), show the digest, then run
     `accept --expect <digest>` with the same `--reason`.
   - Reject only on the user's explicit rejection with their note: run
     `reject --intent-only --text NOTE`, show the digest, then run
     `reject --expect <digest> --text NOTE` as its own Bash call with
     `run_in_background: true` (it reopens EXEC and dispatches model turns);
     wait for its completion and inspect the final status.
   - Use `--override-rejection` only when the user asks for it with a reason.
   Do not imply user acceptance or delivery authorization from DONE.

The coordinator owns reviewer dispatch and limits. This skill is the explicit
paired-session entry and the review-loop handoff target only when the config
key `entry` is `paired-session` or absent and the work is fresh; with `legacy`
or an invalid value, routing stays legacy.
