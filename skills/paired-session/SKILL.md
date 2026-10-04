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

# Paired-session workflow (Claude Code)

First, before any stage A check or question, read `docs/protocol/loading.md`
and load the shared entry contract (`docs/protocol/paired-session-entry.md`) as
its own Bash command, cwd in the user's workspace:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/read_protocol.py --runtime claude --stage entry-paired-session --output .review-loop/tmp/protocol-claude-entry-paired-session.md
```

After exit 0, read the complete output file in bounded chunks and follow it; a
missing, unreadable or incompletely read file is a failed stage A check,
reported as `stage A failure: cannot load the shared entry contract (<reason>)`.
After compaction, reload it the same way when no coordinator command is
running; while one is running, read
`${CLAUDE_PLUGIN_ROOT}/docs/protocol/paired-session-entry.md` directly (the
stage is that whole file), which writes nothing to the workspace. This file
adds only the Claude Code host rules:

- Legacy pointer: `use /review-loop:legacy`. Long-command execution failure:
  unavailable background execution (a failed stage A check; on the explicit
  entry, HOLD with the reason).
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

This skill is the explicit paired-session entry and the review-loop handoff target only when the config
key `entry` is `paired-session` or absent and the work is fresh; with `legacy`
or an invalid value, routing stays legacy.
