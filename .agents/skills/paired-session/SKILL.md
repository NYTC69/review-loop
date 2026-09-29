---
name: paired-session
description: Use only when the user explicitly asks for paired-session; this is the opt-in plan, implementation, independent-review, and delivery workflow. Generic review-loop requests use the legacy entry.
---

# Paired-session workflow

Use the paired-session coordinator shipped with this plugin. Do not load or
invoke the legacy review-loop workflow for this task.

1. Identify the intended Git worktree. Use a dedicated task worktree; preserve
   unrelated user changes and do not switch away from a dirty checkout. If a
   dedicated worktree is unavailable, ask before creating one.
2. Read `<workspace>/.review-loop/paired-session.json` if present. It may set
   non-program limits only. Put role/vendor/program/test-command settings in an
   operator-owned profile outside the workspace and run directory, and pass
   its absolute path with `--config` for probe and run. `--config` replaces the
   workspace profile; copy desired non-program limits into the external profile.
   CLI options can override
   that profile, but models must match the
   ADR-8 vendor pins (`claude-opus-5-5` for Claude; `gpt-6-luna` for Codex).
   Without a profile, the coordinator selects those pins by role vendor.
   Determine the project's test command from its docs/manifests and ask only if
   it cannot be established safely.
3. Run the setup, work-item write, permission probe, and coordinator run in one
   Bash invocation so its shell variables remain available. Start that shell
   call with a short nonblocking yield (about 1 second), then poll its returned
   command-session id until the full call completes. Do not cancel it or launch
   a duplicate while it is active. The coordinator
   spawns provider CLIs and writes to `$CODEX_HOME` outside the product
   workspace; execute this command with the host's full filesystem/network
   permission, outside the current Codex sandbox. If that permission cannot be
   granted, stop and report HOLD instead of trying inside the sandbox.

   ```sh
   set -eu
   export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
   VERSION="$(codex plugin list --json --marketplace review-loop-marketplace | python3 -c 'import json,sys; d=json.load(sys.stdin); rows=[p for p in d.get("installed",[]) if p.get("pluginId")=="review-loop@review-loop-marketplace" and p.get("enabled")]; assert len(rows)==1, "review-loop plugin must be installed and enabled exactly once"; print(rows[0]["version"])')"
   PLUGIN_ROOT="$CODEX_HOME/plugins/cache/review-loop-marketplace/review-loop/$VERSION"
   test -x "$PLUGIN_ROOT/bin/paired-session" || { echo 'paired-session executable missing from installed plugin'; exit 2; }
   WORKSPACE='/absolute/path/to/the-selected-dedicated-worktree'
   WORKSPACE="$(cd "$WORKSPACE" && pwd -P)"
   test "$(git -C "$WORKSPACE" rev-parse --show-toplevel)" = "$WORKSPACE" || { echo 'selected workspace is not a Git worktree root'; exit 2; }
   TASK_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
   RUN_DIR="$CODEX_HOME/state/paired-session/runs/$TASK_ID"
   WORKITEM="$RUN_DIR/WORKITEM.md"
   mkdir -p "$RUN_DIR"
   cat > "$WORKITEM" <<'PAIRED_SESSION_WORKITEM'
   [Write the user's approved goal, acceptance criteria, scope, and verification here.]
   PAIRED_SESSION_WORKITEM
   TEST_COMMAND='[verified project command, or the exact value from the profile]'
   "$PLUGIN_ROOT/bin/paired-session" permission-probe \
     --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
     --test-command "$TEST_COMMAND"
   "$PLUGIN_ROOT/bin/paired-session" run \
     --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
     --test-command "$TEST_COMMAND"
   ```

   Replace the work-item placeholder and test command before running. Add
   `--config /absolute/path/to/profile.json` to both calls when using the
   external operator profile. Add
   user-requested CLI overrides identically to probe and run; add
   `--stop-after-plan` only to `run` when requested. Keep the heredoc delimiter
   unique and quoted; never interpolate user text as shell code. Never use
   `--skip-probe` for product work.
4. Report DONE/HOLD and the run directory. On HOLD, inspect its state, findings,
   and receipts before resuming. If `uncertain_active` is present, do not rerun
   the probe or resume automatically: check its pid and receipts; if the child
   is still alive, wait for it to stop. If its phase is `PROBE` or
   `AUTHOR_PERMISSION_PROBE`, ask before rerunning the disposable probe with
   `permission-probe --retry-uncertain`. For a product-work turn, ask before
   `resume --retry-uncertain` because this may replay a model turn. After
   recovering an interrupted probe, use `resume` on the existing run directory;
   do not run a new work item. Otherwise,
   resolve `PLUGIN_ROOT` again inside the same shell invocation that runs
   `resume`; use the saved workspace, work item, run directory, profile, and
   options. Start the shell with a short nonblocking yield, then poll its
   command-session id until completion. Re-run the permission probe first if it
   is missing or no longer matches. Do not imply user acceptance or delivery
   authorization from DONE.

The coordinator owns reviewer dispatch and limits. This skill is the explicit
paired-session entry and the review-loop handoff target only when the config
key `entry` is exactly `paired-session` and the work is fresh; otherwise default
routing stays legacy.
