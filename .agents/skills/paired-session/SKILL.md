---
name: paired-session
description: Use in exactly three cases - the user explicitly asks for paired-session, the review-loop entry hands off because .review-loop/config.md sets `entry: paired-session`, or the review-loop entry hands off because that key is absent (the default entry); this is the plan, implementation, independent-review, finish, polish, docs, security and acceptance workflow. Generic review-loop requests in every other case (key invalid, `legacy`, or explicit legacy) use the legacy entry.
---

# Paired-session workflow

Use the paired-session coordinator shipped with this plugin. Do not load or
invoke the legacy review-loop workflow for this task.

How this skill was entered decides failure handling (stage A = everything before
the first command that runs `bin/paired-session`):
- Default entry (review-loop handoff with the `entry` key absent): a failed
  stage A check is reported back as `stage A failure: <reason>`; the review-loop
  entry then prints its fallback notice and runs legacy.
- Explicit entry (`entry: paired-session` handoff, or the user asked for
  paired-session): a failed stage A check refuses with the reason, except that a
  declined or ungranted outside-sandbox approval is reported as HOLD. On an
  `entry: paired-session` handoff, print other refusals as
  `review-loop: paired-session entry refused (<reason>); set "entry: legacy" or ask for "the legacy review-loop workflow"`.
- From the first `bin/paired-session` command on, every refusal or HOLD is
  reported verbatim and never falls back to legacy.

1. Stage A checks. When the user invoked this skill directly, first check that
   every CLI the resolved roles need is on PATH (`command -v`; the default roles
   need `codex` and `claude`); the review-loop entry has already checked this on
   a handoff. Identify the intended Git worktree. Use a dedicated task worktree;
   preserve unrelated user changes and do not switch away from a dirty checkout.
   If a dedicated worktree is unavailable, ask before creating one. Determine
   the project's test command from its docs/manifests and ask only if it cannot
   be established safely. When a Claude role (reviewer or gate) runs it, the
   command must be one exact command without `$(`, backticks, `|`, `;`, `&&`,
   redirection or loops; otherwise ask for a `/bin/bash /absolute/path/script.sh`
   form. A declined or unanswered question is a failed stage A check. Under
   `--handsfree` or `handsfree: true` nobody answers, so any such question is a
   failed stage A check, reported as
   `stage A failure: handsfree cannot answer (<question>)`.
2. Profile and settings. Use the operator profile the user names; otherwise
   use `~/.config/review-loop/paired-session.json` when it exists. Pass its
   absolute path with `--config` on every call. `--config` replaces
   `<workspace>/.review-loop/paired-session.json`, which may set non-program
   limits only; copy desired non-program limits into the external profile.
   Role/vendor/program/test-command settings belong in the operator profile,
   outside the workspace and run directory. CLI options can override that
   profile. Models are operator-set (ADR-9); a role without one gets its
   vendor's default (`claude-opus-5-5` for Claude; `gpt-6-luna` for Codex), and
   the gate defaults to the author's vendor (ADR-10).
   Legacy keys in `.review-loop/config.md` that are set map to one-run options:
   `docs_file` → `--docs-file`, `skip_quality_polish` → `--skip-quality-polish
   true|false`, `soft_limit_plan` / `soft_limit_exec` → `--max-plan-rounds` /
   `--max-exec-rounds`. Do not apply, but print a warning for, `auto_commit: true`
   (`review-loop: auto_commit in .review-loop/config.md is not applied by
   paired-session; set it in the operator profile`) and `reviewer_model` /
   `executor_model` (models come from the operator profile, ADR-9). Never pass
   `--adversarial-gate off`.
3. For a new run, pass `--lifecycle-mode on` to both `permission-probe` and `run`,
   identically; the CLI value overrides any profile value. Never pass
   `--skip-probe`, `--accept-unverified-codex-cli`,
   `--accept-unverified-claude-author`, `--accept-probe-skip` or
   `--override-rejection` on your own initiative.
   The coordinator spawns provider CLIs and writes to `$CODEX_HOME` outside the
   product workspace; execute each invocation below with the host's full
   filesystem/network permission, outside the current Codex sandbox, and never
   inside the sandbox. If that permission is declined or cannot be granted for
   the first invocation, that is a failed stage A check; for any later
   invocation, report HOLD with the reason (no fallback). Every invocation that can dispatch model turns (`permission-probe`,
   `run`, `resume`, `reject --expect`) starts with a short nonblocking yield
   (about 1 second); then poll its returned command-session id until it
   completes. Do not cancel it or launch a duplicate while it is active, except
   for the lifecycle-mode backstop below.

   First invocation (setup and probe):

   ```sh
   set -eu
   export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
   stage_a() { echo "stage A failure: $1"; exit 3; }
   test -d "$CODEX_HOME" || stage_a "CODEX_HOME $CODEX_HOME is not an existing directory"
   VERSION="$(codex plugin list --json --marketplace review-loop-marketplace | python3 -c 'import json,sys; d=json.load(sys.stdin); rows=[p for p in d.get("installed",[]) if p.get("pluginId")=="review-loop@review-loop-marketplace" and p.get("enabled")]; assert len(rows)==1, "review-loop plugin must be installed and enabled exactly once"; print(rows[0]["version"])')" || stage_a "review-loop plugin is not installed and enabled exactly once"
   python3 -c 'import re,sys; v=sys.argv[1]; m=re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(\.\d+)*", v); msg=None if m and tuple(map(int, m.groups()[:3])) >= (2, 10, 0) else ("installed review-loop %s predates the paired-session lifecycle (2.10.0)" % v if m else "cannot parse installed review-loop version %r" % v); msg and print("stage A failure: " + msg); sys.exit(3 if msg else 0)' "$VERSION" || exit 3
   echo "VERSION=$VERSION"
   PLUGIN_ROOT="$CODEX_HOME/plugins/cache/review-loop-marketplace/review-loop/$VERSION"
   test -x "$PLUGIN_ROOT/bin/paired-session" || stage_a 'paired-session executable missing from installed plugin'
   WORKSPACE='/absolute/path/to/the-selected-dedicated-worktree'
   WORKSPACE="$(cd "$WORKSPACE" && pwd -P)" || stage_a 'selected workspace does not exist'
   test "$(git -C "$WORKSPACE" rev-parse --show-toplevel)" = "$WORKSPACE" || stage_a 'selected workspace is not a Git worktree root'
   TASK_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
   RUN_DIR="$CODEX_HOME/state/paired-session/runs/$TASK_ID"
   WORKITEM="$RUN_DIR/WORKITEM.md"
   mkdir -p "$RUN_DIR" || stage_a 'cannot create the run directory'
   cat > "$WORKITEM" <<'PAIRED_SESSION_WORKITEM' || stage_a 'cannot write the work item'
   [Write the user's approved goal, acceptance criteria, scope, and verification here.]
   PAIRED_SESSION_WORKITEM
   echo "RUN_DIR=$RUN_DIR"
   TEST_COMMAND='[verified project command, or the exact value from the profile]'
   "$PLUGIN_ROOT/bin/paired-session" permission-probe \
     --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
     --test-command "$TEST_COMMAND" --lifecycle-mode on
   ```

   Exit 3 (a line printed `stage A failure: …`) is a failed stage A check,
   including the plugin-version floor (P14); report it per the entry rules
   above. Exit 0 from the probe means PASS or PASS_RESIDUAL_RISK; any other
   result is a HOLD: report it and stop. On exit 0, read
   `RUN_DIR/permission-probe.json` and tell the user if the status is
   PASS_RESIDUAL_RISK, then read the frozen `config` in `RUN_DIR/state.json` and
   print one start line from it: author, reviewer and gate vendor and model;
   plan and exec rounds, invocations and timeout; docs file and
   skip-quality-polish; and which values came from `.review-loop/config.md`. If
   `config.lifecycle_mode` is not `on`, run `abort` with the run's saved options
   and report a plugin version mismatch instead of starting the run.

   Second invocation (run), with the `RUN_DIR` printed by the first and the same
   resolved values:

   ```sh
   set -eu
   export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
   VERSION="$(codex plugin list --json --marketplace review-loop-marketplace | python3 -c 'import json,sys; d=json.load(sys.stdin); rows=[p for p in d.get("installed",[]) if p.get("pluginId")=="review-loop@review-loop-marketplace" and p.get("enabled")]; assert len(rows)==1, "review-loop plugin must be installed and enabled exactly once"; print(rows[0]["version"])')" || { echo "review-loop plugin cannot be resolved after the probe; not starting the run"; exit 4; }
   EXPECTED_VERSION='[the VERSION printed by the first invocation]'
   test "$VERSION" = "$EXPECTED_VERSION" || { echo "plugin version changed ($EXPECTED_VERSION -> $VERSION) after the probe; not starting the run"; exit 4; }
   PLUGIN_ROOT="$CODEX_HOME/plugins/cache/review-loop-marketplace/review-loop/$VERSION"
   WORKSPACE='/the/resolved/worktree/from/the/first/invocation'
   RUN_DIR='/the/RUN_DIR/printed/by/the/first/invocation'
   WORKITEM="$RUN_DIR/WORKITEM.md"
   TEST_COMMAND='[the same test command]'
   "$PLUGIN_ROOT/bin/paired-session" run \
     --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
     --test-command "$TEST_COMMAND" --lifecycle-mode on
   ```

   Replace the placeholders before running. In the work item include only
   user-approved requirements; mark uncertainties as questions instead of
   inventing acceptance criteria. Add the same `--config` and mapped one-run
   options to both calls; add `--stop-after-plan` only to `run` when requested.
   Keep the heredoc delimiter unique and quoted; never interpolate user text as
   shell code. Exit 4 from the second invocation means the installed plugin
   changed or became unresolvable after the probe: report it as a HOLD (no fallback); the run was not
   started. While the run is active, do not call plain `status`: it
   needs the run lease and, while the run holds it, prints
   `HOLD: another coordinator currently owns this run` although the run is not
   on HOLD. Read `RUN_DIR/state.json` directly or use `status --brief`. As a
   backstop, if the running state shows `config.lifecycle_mode` other than `on`,
   end the polled command session, wait until the child in `state.active` has
   exited, run `abort` with the run's saved options, and report a plugin version
   mismatch.
4. Later commands on an existing run (`resume`, `permission-probe
   --retry-uncertain`, `abort`, `reject`, `accept`, `note`,
   `attach-verification`) pass the saved `state.json` `config.lifecycle_mode`
   value and the run's original workspace, work item, run directory, profile
   and options, never the current default; resolve `PLUGIN_ROOT` again inside
   each invocation. Report DONE/HOLD and the run directory. On HOLD, inspect its
   state, findings, and receipts before resuming. If `uncertain_active` is
   present, do not rerun the probe or resume automatically: check its pid and
   receipts; if the child is still alive, wait for it to stop. If its phase is
   `PROBE` or `AUTHOR_PERMISSION_PROBE`, ask before rerunning the disposable
   probe with `permission-probe --retry-uncertain`. For a product-work turn, ask
   before `resume --retry-uncertain` because this may replay a model turn. After
   recovering an interrupted probe, use `resume` on the existing run directory;
   do not run a new work item. Re-run the permission probe first if it is
   missing or no longer matches.
5. DONE. With the saved `config.lifecycle_mode` `on`, DONE means the security
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
     `reject --expect <digest> --text NOTE` as a polled invocation (it reopens
     EXEC and dispatches model turns) and inspect the final status.
   - Use `--override-rejection` only when the user asks for it with a reason.
   Do not imply user acceptance or delivery authorization from DONE.

The coordinator owns reviewer dispatch and limits. This skill is the explicit
paired-session entry and the review-loop handoff target only when the config
key `entry` is `paired-session` or absent and the work is fresh; with `legacy`
or an invalid value, routing stays legacy.
