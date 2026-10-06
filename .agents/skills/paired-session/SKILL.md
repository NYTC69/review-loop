---
name: paired-session
description: Use in exactly four cases - the user explicitly asks for paired-session, the review-loop entry hands off because .review-loop/config.md sets `entry: paired-session`, the review-loop entry hands off because that key is absent (the default entry), or the review-pr skill hands off on its paired route; this is the plan, implementation, independent-review, finish, polish, docs, security and acceptance workflow. Generic review-loop requests in every other case (key invalid, `legacy`, or explicit legacy) use the legacy entry.
---

# Paired-session workflow (Codex)

First, before any stage A check or question, read `docs/protocol/loading.md`
and load the shared entry contract (`docs/protocol/paired-session-entry.md`) as
its own command, cwd in the user's workspace. The bundle must stay out of the
product worktree (FIELD-18): first print a fresh bundle directory as its own
command,
`python3 -c 'import os, tempfile, uuid; print(os.path.join(os.path.realpath(tempfile.gettempdir()), f"review-loop-protocol-{os.getuid()}", uuid.uuid4().hex[:12]))'`,
then write that printed directory out literally as `<bundle-dir>`:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime codex --stage entry-paired-session --output <bundle-dir>/protocol-codex-entry-paired-session.md
```

Resolve `<support-root>` to this plugin, not the task workspace. After exit 0,
read the complete output file in bounded chunks and follow it; a missing,
unreadable or incompletely read file is a failed stage A check, reported as
`stage A failure: cannot load the shared entry contract (<reason>)`. After
compaction, reload it the same way when no coordinator command is running;
while one is running, read `<support-root>/docs/protocol/paired-session-entry.md`
directly (the stage is that whole file), which writes nothing to the workspace.
This file adds only the Codex host rules:

- Legacy pointer: `ask for "the legacy review-loop workflow"`. Long-command
  execution failure: a declined or ungranted outside-sandbox approval (a failed
  stage A check for the first invocation; on the explicit entry, and for any
  later invocation, HOLD with the reason, no fallback).
- Outside-sandbox execution: the coordinator spawns provider CLIs and writes to
  `$CODEX_HOME` outside the product workspace; execute each invocation below
  with the host's full filesystem/network permission, outside the current
  Codex sandbox, and never inside the sandbox.
- Long-command form: every invocation that can dispatch model turns
  (`permission-probe`, `run`, `resume`, `reject --expect`) starts with a short
  nonblocking yield (about 1 second); then poll its returned command-session id
  until it completes. Do not cancel it or launch a duplicate while it is
  active, except for the lifecycle-mode backstop, which ends the polled command
  session.
- Run directory: `$CODEX_HOME/state/paired-session/runs/<task id>`, created by
  the first invocation, which writes the work item there. Resolve `PLUGIN_ROOT`
  again inside each invocation.
- Default (efficient): no permission probe; the first invocation below starts
  the run. Strict: replace `run` in the first invocation with
  `permission-probe` (same arguments, without `--stop-after-plan`), then start
  the run with the second invocation.

First invocation (setup, then `run`; strict: `permission-probe`):

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
"$PLUGIN_ROOT/bin/paired-session" run \
  --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
  --test-command "$TEST_COMMAND" --lifecycle-mode on
```

Exit 3 (a line printed `stage A failure: …`) is a failed stage A check,
including the plugin-version floor (P14); report it per the entry rules, even
though `RUN_DIR/state.json` does not exist. Any
other non-zero exit from the coordinator is a refusal or HOLD: report its
output verbatim (no fallback).

Second invocation (strict only: run), with the `RUN_DIR` printed by the first and the same
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

Replace the placeholders before running. Keep the heredoc delimiter unique and
quoted. Exit 4 from the second invocation means the installed plugin changed
or became unresolvable after the probe: report it as a HOLD (no fallback); the
run was not started.

A review-only request (the user asks to review an existing change, or the review-loop handoff of
a code target) follows the shared contract's review-only entry: add `--review-only` (and
`--base "$BASE"` only when the user named a base) to both invocations.

A review-pr handoff (from the Codex `review-pr` skill) follows the shared contract's Review-PR entry.
Host rules: the run root is `$CODEX_HOME/state/paired-session` (the materializer's `--root`); the
materializer reads through `gh` and the network, so run it as its own invocation outside the sandbox,
with `PLUGIN_ROOT` resolved as in the first invocation, before the first invocation. In both
invocations set `WORKSPACE` to the clone it printed (no input: the current worktree) and skip the
dedicated-worktree question, replace `--test-command "$TEST_COMMAND"` with `--no-test-command` unless
the user confirmed a test command, and add `--review-only --review-report --auto-commit false`,
`--base "$BASE"` (the printed `merge_base`; no input: only a base the user named),
`--review-pr-pins "$RUN_DIR/pr-pins.json"` for a materialized input (the first invocation writes the
printed JSON there with a second quoted heredoc, like the work item) and `--aspects "$ASPECTS"` when
aspects were given. Legacy review-pr pointer: ask for
"the legacy review-pr workflow" (it has no `simplify` on Codex: that writer is
`/review-loop:review-pr --legacy simplify` in Claude Code).

This skill is the explicit
paired-session entry and the review-loop handoff target only when the config
key `entry` is `paired-session` or absent and the work is fresh or a review-only code target; with `legacy`
or an invalid value, routing stays legacy.
