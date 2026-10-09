---
name: paired-session
argument-hint: "<work item> [--plan-only | --review-only [--base REF] | --review-pr [INPUT] [ASPECTS] | --code-quality-loop [OPTIONS]]"
description: >
  Drive an implementation through the paired-session coordinator: independent
  PLAN review, EXEC implementation/review, adversarial gate, then finish,
  quality polish, docs and security up to operator acceptance.
  Trigger in exactly five cases: (1) the user explicitly asks for paired-session
  or names this explicit coordinator entry; (2) the review-loop entry hands off
  because .review-loop/config.md sets `entry: paired-session`; (3) the review-loop
  entry hands off because that key is absent or invalid (the default entry); (4)
  /review-loop:review-pr hands off on its paired route (`--review-pr`); (5)
  /review-loop:code-quality-loop hands off on its paired route (`--code-quality-loop`). Do not trigger on
  a bare review-loop request yourself (the review-loop entry routes it), nor on `entry: legacy` (refused
  there since v2.13.0).
---

# Paired-session workflow (Claude Code)

First, before any stage A check or question, read `${CLAUDE_PLUGIN_ROOT}/docs/protocol/loading.md`
and load the shared entry contract (`docs/protocol/paired-session-entry.md`) as
its own Bash command, cwd in the user's workspace. The bundle must stay out of
the product worktree (FIELD-18): first print a fresh bundle directory with its
own Bash call,
`python3 -c 'import os, tempfile, uuid; print(os.path.join(os.path.realpath(tempfile.gettempdir()), f"review-loop-protocol-{os.getuid()}", uuid.uuid4().hex[:12]))'`,
then write that printed directory out literally as `<bundle-dir>`:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/read_protocol.py --runtime claude --stage entry-paired-session --output <bundle-dir>/protocol-claude-entry-paired-session.md
```

After exit 0, read the complete output file in bounded chunks and follow it; a
missing, unreadable or incompletely read file is a failed stage A check,
reported as `stage A failure: cannot load the shared entry contract (<reason>)`.
After compaction, reload it the same way when no coordinator command is
running; while one is running, read
`${CLAUDE_PLUGIN_ROOT}/docs/protocol/paired-session-entry.md` directly (the
stage is that whole file), which writes nothing to the workspace. This file
adds only the Claude Code host rules:

- Legacy pointer: none (the legacy workflow was removed in v2.13.0). Long-command execution failure:
  unavailable background execution: HOLD with the reason, on every entry (nothing falls back).
- Run directory: create a UUID; the run directory is
  `${XDG_STATE_HOME:-$HOME/.local/state}/review-loop/runs/<UUID>/`, outside the
  product worktree and outside `~/.claude` (Claude Code treats paths there as
  sensitive and denies or prompts for every write). Resolve the run root once
  with its own Bash call, `printf '%s\n' "${XDG_STATE_HOME:-$HOME/.local/state}/review-loop"`,
  and use the absolute path it prints (if it is not absolute, use
  `$HOME/.local/state/review-loop`: XDG ignores a relative `XDG_STATE_HOME`).
  Create the run directory (`mkdir -p`), then write `WORKITEM.md` there. The
  root is outside the session's working directory, so a restricted permission
  mode may need the operator to grant it (for example `--add-dir <run root>`).
  If either step fails or is denied, stop: that is a failed stage A check,
  reported as `stage A failure: <reason>`; never try another location. Resolve `WORKSPACE` to the intended worktree root and
  `WORKITEM`/`RUN_DIR` to absolute paths. Claude Code substitutes
  `${CLAUDE_PLUGIN_ROOT}` in skill content. Define path variables anew in each
  Bash tool call; shell variables do not persist across separate calls. Pass
  arguments as an array. Write any launcher log under the run root's `logs/`
  (`.../review-loop/logs/`), never under `runs/`.
- Long-command form: run every command that can dispatch model turns
  (`permission-probe`, `run`, `resume`, `reject --expect`) as its own Bash call
  with `run_in_background: true`; wait for the background task completion
  notification and inspect its final result. Use a shell-output polling tool
  only if the host exposes one; do not assume a tool named `BashOutput` exists.
  Never run such a command in a foreground Bash call or extend the foreground
  timeout; long model turns exceed tool limits and can orphan a child CLI. The
  backstop stops the background command.
- Headless sessions: a `claude -p` session exits when its turn ends and kills
  its background tasks, so ending the turn to wait for the notification kills
  the coordinator mid-turn. Use an interactive session. When you know you run
  headless (the user or the prompt says so), prefer `--detach` on `run`,
  `resume`, `reject --expect` and `permission-probe`: the command returns at
  once (`DETACHED: pid …; log …`) and survives the session's exit; read
  `RUN_DIR/state.json` or `status --brief` for its progress, the log for its
  final line (a probe's result is `RUN_DIR/permission-probe.json`), and end it
  only with `stop` (`paired_session/docs/detach.md`). Without `--detach`, do not
  end the turn while a coordinator command runs: poll in bounded foreground calls, reading only
  `status` and `active.phase` from `RUN_DIR/state.json` (or `status --brief`),
  until the background command itself has exited (the host's task-output tool
  if it has one; otherwise `pgrep -f -- "--run-di[r] <RUN_DIR>"` prints nothing; the
  bracket keeps pgrep from matching its own shell),
  then inspect its final result as above. Recover a run cut off this way by the
  shared contract's uncertain-turn rule: check the turn's pid and phase, and
  ask the user before any `--retry-uncertain`.
- Default (efficient): one Bash call with the `run` block below. Strict: the
  probe block first, then the `run` block as a second Bash call.

Strict only, first Bash call (`run_in_background: true`):

```sh
WORKSPACE='/absolute/path/to/worktree'
WORKITEM='/absolute/run/root/runs/<UUID>/WORKITEM.md'
RUN_DIR='/absolute/run/root/runs/<UUID>'
TEST_COMMAND='the verified project command'
"${CLAUDE_PLUGIN_ROOT}/bin/paired-session" permission-probe \
  --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
  --test-command "$TEST_COMMAND" --lifecycle-mode on
```

`run` (`run_in_background: true`):

```sh
WORKSPACE='/absolute/path/to/worktree'
WORKITEM='/absolute/run/root/runs/<UUID>/WORKITEM.md'
RUN_DIR='/absolute/run/root/runs/<UUID>'
TEST_COMMAND='the verified project command'
"${CLAUDE_PLUGIN_ROOT}/bin/paired-session" run \
  --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
  --test-command "$TEST_COMMAND" --lifecycle-mode on
```

A review-only request (`--review-only`, or the review-loop handoff of code already implemented)
follows the shared contract's review-only entry: add `--review-only` (and `--base "$BASE"`
only when the user named a base) to both blocks.
Add `--auto-commit false` too when the review-loop entry handed it over.

A review-pr handoff (`--review-pr [INPUT] [ASPECTS]` from `/review-loop:review-pr`) follows the
shared contract's Review-PR entry. Host rules: `<support-root>` in the shared contract is
`${CLAUDE_PLUGIN_ROOT}` (for example `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/materialize_pr.py"`); the run
root above is the materializer's `--root`; the materializer runs as its own foreground Bash call with
`timeout: 600000` (a clone and fetch can outlast the default), before the blocks. In both blocks set
`WORKSPACE` to the clone it printed (no input: the current worktree), replace
`--test-command "$TEST_COMMAND"` with `--no-test-command` unless the user confirmed a test command, and
add `--review-only --review-report --auto-commit false`, `--base "$BASE"` (the printed `merge_base`;
no input: only a base the user named), `--review-pr-pins "$RUN_DIR/pr-pins.json"` for a materialized
input (write the printed JSON there with the work item) and `--aspects "$ASPECTS"` when aspects were
given. `simplify` pointer: `run /review-loop:code-quality-loop on the change (its POLISH-Q simplifier)`.

A code-quality-loop handoff (`--code-quality-loop [OPTIONS]` from `/review-loop:code-quality-loop`) follows
the shared contract's Code-quality-loop entry. In both blocks add `--review-only` and the options it names
(`--max-exec-rounds N` as handed over, which wins over `soft_limit_exec`; `--auto-commit false` as handed over;
`--max-invocations 45` unless the profile sets `max_invocations`) and the explicit `--test-command`;
also add `--advisory-fix-round true` (one fix round for the non-blocking findings);
`WORKSPACE` is the current worktree. Its two
notice lines belong to the code-quality-loop skill; only if they are not already in this conversation's visible
output, print them verbatim (the shared contract's Code-quality-loop entry quotes them) as the first output after
loading that contract, before any stage A check. The work item's
first line is `# code-quality-loop: <one-line summary of the uncommitted change>`.

This skill is the explicit paired-session entry and the review-loop handoff target only when the config
key `entry` is `paired-session` or absent and the work is fresh, an existing plan (as the work item) or a review-only
code target (an invalid value counts as absent, with a warning); `entry: legacy` is refused by the entry, since the
legacy workflow was removed in v2.13.0.

Lifecycle-off was removed in this release; saved off runs finish on the pinned v3.0.4 copy at `~/paired-runs/review-loop-v3.0.4`.
