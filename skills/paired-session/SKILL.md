---
name: paired-session
argument-hint: "<work item> [--plan-only]"
description: >
  Drive an implementation through the paired-session coordinator: independent
  PLAN review, EXEC implementation/review, adversarial gate, and delivery.
  Trigger only when the user explicitly asks for paired-session or names this
  explicit coordinator entry. Do not trigger on a bare review-loop request.
---

# Paired-session workflow

Use the shared coordinator shipped with this plugin. Do not load or invoke the
legacy review-loop workflow for this task.

1. Identify the intended Git worktree. Use a dedicated task worktree; preserve
   unrelated user changes and do not switch away from a dirty checkout. If a
   dedicated worktree is unavailable, ask before creating one.
2. Read `<workspace>/.review-loop/paired-session.json` if present. It may set
   non-program limits only. Put role/vendor/program/test-command settings in an
   operator-owned profile outside the workspace and run directory, and pass
   its absolute path with `--config` for probe and run. CLI options can override
   that profile, but models must match the
   ADR-8 vendor pins (`claude-opus-5-5` for Claude; `gpt-6-luna` for Codex).
   Without a profile, the coordinator selects those pins by role vendor.
   Determine the project's test command from its docs/manifests and ask only if
   it cannot be established safely.
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
   verified project command, and pass it as one quoted argument.
4. Start the permission probe as a separate Bash call with
   `run_in_background: true`; wait for the background task completion
   notification and inspect its final result. Use a shell-output polling tool
   only if the host exposes one; do not assume a tool named `BashOutput` exists.
   Only after PASS, start the coordinator run as another Bash call with
   `run_in_background: true`. Never run either command in a foreground Bash
   call or extend the foreground timeout; long model turns exceed tool limits
   and can orphan a child CLI. If background execution is unavailable, stop and
   report HOLD with the reason.

   First Bash call (`run_in_background: true`):

   ```sh
   WORKSPACE='/absolute/path/to/worktree'
   WORKITEM='${CLAUDE_PLUGIN_DATA}/runs/<UUID>/WORKITEM.md'
   RUN_DIR='${CLAUDE_PLUGIN_DATA}/runs/<UUID>'
   TEST_COMMAND='the verified project command'
   "${CLAUDE_PLUGIN_ROOT}/bin/paired-session" permission-probe \
     --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
     --test-command "$TEST_COMMAND"
   ```

   If using the external operator profile, add the same `--config
   /absolute/path/to/profile.json` argument to this probe and the run below.
   Wait for the host's background-task completion event and inspect its final
   result. Only after the probe reports PASS, make a second Bash call
   (`run_in_background: true`) with the same resolved values:

   ```sh
   WORKSPACE='/absolute/path/to/worktree'
   WORKITEM='${CLAUDE_PLUGIN_DATA}/runs/<UUID>/WORKITEM.md'
   RUN_DIR='${CLAUDE_PLUGIN_DATA}/runs/<UUID>'
   TEST_COMMAND='the verified project command'
   "${CLAUDE_PLUGIN_ROOT}/bin/paired-session" run \
     --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
     --test-command "$TEST_COMMAND"
   ```

   Wait for each background call to finish before starting the next; do not
   create a second run while one is active. If the host offers an output-polling
   tool, use it to inspect the final status; otherwise wait for its background
   completion event. Do not assume a tool named `BashOutput` exists. Include
   non-program workspace defaults automatically only without `--config`.
   An external operator profile replaces the workspace profile: copy desired
   limits into it and pass `--config` on both calls. Add explicit
   one-run override arguments identically to both invocations. Add
   `--stop-after-plan` only to `run` when requested. Pass arguments as an array;
   do not interpolate untrusted text into shell source. Never use
   `--skip-probe` for product work.
5. Report DONE/HOLD and the run directory. On HOLD, inspect its state, findings,
   and receipts before resuming. If `uncertain_active` is present, do not rerun
   the probe or resume automatically: check its pid and receipts; if the child
   is still alive, wait for it to stop. If its phase is `PROBE` or
   `AUTHOR_PERMISSION_PROBE`, ask before rerunning the disposable probe with
   `permission-probe --retry-uncertain`. For a product-work turn, ask before
   `resume --retry-uncertain` because this may replay a model turn. After
   recovering an interrupted probe, use `resume` on the existing run directory;
   do not use `run` again. Run resume in a separate Bash call with
   `run_in_background: true`, the same run directory, worktree, work item,
   profile, and options; wait for background completion and inspect its final
   status. Do not imply user acceptance or delivery authorization from DONE.

The coordinator owns reviewer dispatch and limits. The current user-facing
default is still staged; this skill is the explicit paired-session entry until
the migration batch changes routing.
