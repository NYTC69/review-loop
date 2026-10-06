# Paired-session entry (shared contract)

Both paired-session entry skills (`skills/paired-session/SKILL.md` for Claude Code,
`.agents/skills/paired-session/SKILL.md` for Codex) load this file through the
`entry-paired-session` stage. It holds every rule the two hosts share. The host
skill adds its host rules, for example run-directory paths, shell form, how a
long coordinator command runs and is stopped, its legacy pointer, and the exit
codes of its own setup steps; a host rule is more specific and wins. Use the
paired-session coordinator shipped with this plugin. Do not load or invoke the
legacy review-loop workflow for this task. The coordinator owns reviewer
dispatch and limits.

## Entry and failure handling

How this skill was entered decides failure handling (stage A = everything before
the first command that runs `bin/paired-session`):
- Default entry (review-loop handoff with the `entry` key absent): a failed
  stage A check is reported back as `stage A failure: <reason>`; the review-loop
  entry then prints its fallback notice and runs legacy.
- Explicit entry (`entry: paired-session` handoff, or the user asked for
  paired-session): a failed stage A check refuses with the reason, except the
  host skill's long-command execution failure, which is reported as HOLD with
  the reason. On an `entry: paired-session` handoff, print other refusals as
  `review-loop: paired-session entry refused (<reason>); set "entry: legacy" or <host legacy pointer>`.
- Every host setup step before the first coordinator command (loading this
  contract, creating the run directory, writing `WORKITEM.md`, resolving the
  plugin) belongs to stage A. If one fails or is denied, stop and report
  `stage A failure: <reason>`. Do not retry in another location or improvise,
  and never start or continue another workflow from this skill (no legacy
  session file, lock or evidence snapshot): only the review-loop entry falls
  back, and only through its documented notice.
- From the first `bin/paired-session` command on, every refusal or HOLD is
  reported verbatim and never falls back to legacy.

## Stage A checks

When the user invoked this skill directly, first check that every CLI the
resolved roles need is on PATH (`command -v`; the default roles need `codex`
and `claude`); the review-loop entry has already checked this on a handoff.
Identify the intended Git worktree. Use a dedicated task worktree; preserve
unrelated user changes and do not switch away from a dirty checkout. If a
dedicated worktree is unavailable, ask before creating one. Determine the
project's test command from its docs/manifests and ask only if it cannot be
established safely. When a Claude role (reviewer or gate) runs it, the command
must be one exact command without `$(`, backticks, `|`, `;`, `&&`, redirection
or loops; otherwise ask for a `/bin/bash /absolute/path/script.sh` form. A
declined or unanswered question is a failed stage A check. Under `--handsfree`
or `handsfree: true` nobody answers, so any such question is a failed stage A
check, reported as `stage A failure: handsfree cannot answer (<question>)`.
Before `run`, the product worktree must contain no file the entry skills
created: protocol bundles go to the temp root and the run directory lives
outside the worktree. Report any other untracked, non-ignored file in it (`git
status --porcelain --untracked-files=all`) in stage A or with the start line:
it becomes part of the reviewed change and, with `auto_commit`, of the
delivered commit.

## Profile and settings

Use the operator profile the user names; otherwise use
`~/.config/review-loop/paired-session.json` when it exists. Pass its absolute
path with `--config` on every call. `--config` replaces
`<workspace>/.review-loop/paired-session.json`, which may set non-program limits
only; copy any desired non-program limits into the operator profile.
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
from the operator profile, ADR-9). Never pass `--adversarial-gate off`.

## Work item

Write `WORKITEM.md` in the run directory the host skill names (always outside
the product worktree) with goal, acceptance criteria, scope, and verification.
Include only user-approved requirements; mark uncertainties as questions
instead of inventing acceptance criteria. Set the test command from the loaded
profile or the verified project command and pass it as one quoted argument;
never interpolate user text as shell code.

## Review-only entry

A review-only request (the review-loop entry hands off code already implemented,
or the user asks paired-session to review an existing change) runs
`run --review-only`. There is no PLAN phase: the run starts at the EXEC review of
the change, which counts as EXEC round 1, and the coordinator writes the review
scope itself. The change is the whole non-ignored worktree against the review
base, `HEAD` by default (the uncommitted work). Add `--base <ref>` only when the
user names a base (a branch review, for example `--base main`); it must be an
ancestor of `HEAD`. Do not stage, commit or stash anything to shape the change.
- Stage A: list the change (`git status --porcelain --untracked-files=all`, and
  with `--base` also `git diff --name-status <ref>`). Route only a task-related
  change: if a path is unrelated to the request, ask whether to review it too (a
  declined question is a failed stage A check). With `auto_commit`, every
  reviewed path is delivered. `--plan-only` does not apply: refuse it in stage A.
- `WORKITEM.md` states the review goal (one line such as "Review the change for
  correctness" is enough) and carries no review history (ledger ids or earlier
  findings).
- Pass `--review-only` (and `--base`) to `permission-probe` and `run`
  identically, with `--lifecycle-mode on` as for any new run. Later commands read
  the entry and base from the saved state; never pass a different `--base`.
- `run` refuses before creating any state when the change is empty, the index
  has unmerged entries or partially staged paths, the base is not an ancestor of
  `HEAD`, or the work item carries review history; report the refusal verbatim.
  A changed path named like review history (`docs/F001.md`, `APPROVE.txt`) is
  refused too, because the fresh shadow and gate scan the review scope that
  lists it: tell the user to review that change with the legacy workflow.

## Review-PR entry

A review-pr request (the review-pr skill's paired route, `paired_session/docs/review-pr-port.md`) reviews a
change and writes a report; it never fixes, commits, pushes or posts on its own. It runs report mode:
`run --review-only --review-report`, with no author and no writer (REPORTED is its terminal status).
- CLIs: the review-pr skill checks nothing before it hands off, so run the direct-invocation CLI check
  of Stage A checks here, plus `gh` for a PR input; a missing CLI is a failed stage A check.
- Input, resolved in stage A before any coordinator command:
  - none: the local change. Run on the current worktree as the review-only entry above (`--base`
    only when the user names one), with no materialization.
  - a PR number or GitHub PR URL, or a local or remote ref: run
    `python3 <support-root>/scripts/materialize_pr.py <input> --repo <operator repo> --root <run root>`,
    plus `-R <owner/repo>` or `--base <ref>` when the user gives them. It pins the head and base OIDs,
    clones them into `<run root>/pr/<UUID>/` (never the operator's checkout) and prints one JSON object;
    its `workspace` is the clone and `merge_base` the review base. `REFUSED: <reason>` (exit 2) is a
    failed stage A check.
- Size (Q-R10): in the clone, `git diff --shortstat <merge_base> HEAD`. Above about 100 files or 5,000
  changed lines, ask before running; a declined or unanswered question is a failed stage A check.
- Aspects: `code errors comments types tests` select the specialists (`--aspects` with a comma list);
  `all` or none omits the flag. `simplify` is a writer and is not part of the paired review-pr: refuse
  it in stage A on every entry, with no fallback, as
  `review-pr: simplify is not part of the paired review-pr (a writer that edits the checkout); <host legacy review-pr pointer>`,
  never ignore it silently. `parallel` does not apply (the coordinator schedules its roles); say so in
  one line and continue.
- Tests: no test command is the default for every review-pr input (Q-R3): pass `--no-test-command`
  instead of `--test-command`. A test command runs only when the user confirms one for this review; a
  PR's test code then runs on this machine. Never ask about tests under handsfree; a declined or
  unanswered offer leaves the run without tests and never refuses the request.
- `WORKITEM.md` states the review goal and the pins (target repository, PR URL and number, head OID,
  base tip OID, merge base), and no review history. Write the materializer's JSON object, unchanged, to
  `RUN_DIR/pr-pins.json`.
- `run` (and in strict mode `permission-probe`) gets `--review-only --review-report --lifecycle-mode on
  --auto-commit false`, `--workspace` the clone (or the current worktree), `--base <merge_base>` and
  `--review-pr-pins RUN_DIR/pr-pins.json` for a materialized input (the run freezes them for the report
  and the post), `--no-test-command` and `--aspects` as above. Never `--adversarial-gate off`.
- REPORTED: show `RUN_DIR/review-report.md` in the conversation (Strengths only where a role gave
  them). A HOLD leaves the report marked incomplete; report the HOLD as usual. Never `accept`,
  `reject` or `note` a report run.
- Posting (off by default): only on the user's explicit request, never under handsfree. Run
  `python3 <support-root>/paired_session/review_post.py --run-dir RUN_DIR`: it scans the report for
  secrets (refusing names a rule and line, never the value; the report stays local) and prints the
  target, PR, reviewed head, the exact `gh pr review --comment` command and the full body with a
  digest. Show all of it and ask again; only on that second confirmation run it with
  `--confirm <digest>`. It refuses when the PR head moved since the review, and for a run without PR
  pins (the local change, or a ref); report a refusal verbatim. Never `--approve` or `--request-changes`.
- Cleanup: once the report has been shown and the run is terminal, ask before removing the clone, and
  remove it only with `materialize_pr.py --remove <workspace>`; otherwise name the clone. After a stage
  A failure that follows the materialization, name the clone the same way.
- Failures: a failed stage A check for a PR or ref input refuses on every entry and never falls back to
  legacy, since legacy review-pr cannot review a PR. Only the no-argument request may fall back on the
  default entry (Entry and failure handling), and that fallback runs legacy review-pr without `simplify`
  unless the user named it.
- Residual, stated: report mode does not narrow the Codex read root. Codex read-only roles (the gate,
  with the default roles) can read the whole filesystem, credential files included; the efficient
  evidence guard only logs. A prompt injection in an untrusted PR could have a role quote such a file
  into a finding, which is why the post is scanned and confirmed. Narrowing it needs a verified Codex
  permissions profile (follow-up). An operator who wants the credential deny on every role uses Claude
  read-only roles (operator profile).

## Safety mode and the first commands

For a new run, pass `--lifecycle-mode on` to `run` and, in strict mode, to
`permission-probe`, identically; the CLI value overrides any profile value. Never pass
`--skip-probe`, `--accept-unverified-codex-cli`,
`--accept-unverified-claude-author`, `--accept-probe-skip` or
`--override-rejection` on your own initiative.
`--strict` comes only from the user or the operator profile (`safety_mode`),
and goes to `permission-probe` and `run` alike:
both modes keep every sandbox; the default `efficient` mode does not require
the probe PASS and its evidence guard only logs, while `--strict` restores
both (`paired_session/docs/efficient-mode.md`). A strict lifecycle run also
refuses `--accept-unverified-claude-author` and `--accept-probe-skip` (D-7);
an efficient run needs no waiver. The run is strict when the user asked for
`--strict` or the operator profile sets `"safety_mode": "strict"`; read the
profile before choosing the flow.
- Default (efficient): no permission probe; the first coordinator command is
  `run`. If `run` ends before `RUN_DIR/state.json` exists, report its output
  verbatim as a refusal (a host setup step's stage A exit stays a stage A
  failure).
- Strict: `permission-probe` first (same arguments, without
  `--stop-after-plan`), then `run` as a separate command. Exit 0 from the probe
  means PASS or PASS_RESIDUAL_RISK; any other result is a HOLD: report it and
  stop. On exit 0, read `RUN_DIR/permission-probe.json` and tell the user if
  the status is PASS_RESIDUAL_RISK.

Add the same `--config` and mapped one-run options to every call; add
`--stop-after-plan` only to `run` when requested. Every command that can
dispatch model turns (`permission-probe`, `run`, `resume`, `reject --expect`)
uses the host skill's long-command form. Wait for each such command to finish
before starting the next; do not start a second run while one is active.

## Start line and lifecycle-mode backstop

Once `RUN_DIR/state.json` exists (strict: after the probe, before `run`), read
its frozen `config` and print one start line from it: author, reviewer and gate
vendor and model; plan and exec rounds, invocations and timeout; docs file and
skip-quality-polish; and which values came from `.review-loop/config.md`. In
strict mode, if `config.lifecycle_mode` is not `on`, run `abort` with the run's
saved options and report a plugin version mismatch instead of starting the run.

While the run is active, do not call plain `status`: it needs the run lease
and, while the run holds it, prints
`HOLD: another coordinator currently owns this run` although the run is not on
HOLD. Read `RUN_DIR/state.json` directly or use `status --brief`. As a
backstop, if the running state shows `config.lifecycle_mode` other than `on`,
stop the running coordinator command (host skill), wait until the child in
`state.active` has exited, run `abort` with the run's saved options, and report
a plugin version mismatch.

## Existing runs and HOLD

Later commands on an existing run (`resume`, `permission-probe
--retry-uncertain`, `abort`, `reject`, `accept`, `note`,
`attach-verification`) pass the saved `state.json` `config.lifecycle_mode`
value and the run's original workspace, work item, run directory, profile and
options, never the current default. Report DONE/HOLD and the run directory. On
HOLD, inspect its state, findings, and receipts before resuming. In both modes
a reviewer, gate or shadow turn that changes the workspace is void: the
coordinator restores the workspace and re-dispatches it once, and a second
change or a failed restore is a HOLD; an author turn that changes HEAD or the
branch (a commit, reset or checkout) is a HOLD. If `uncertain_active` is
present, do not rerun the probe or resume automatically: check its pid and
receipts; if the child is still alive, wait for it to stop. If its phase is
`PROBE` or `AUTHOR_PERMISSION_PROBE`, ask before rerunning the disposable probe
with `permission-probe --retry-uncertain`. For a product-work turn, ask before
`resume --retry-uncertain` because this may replay a model turn. After
recovering an interrupted probe, use `resume` on the existing run directory;
do not use `run` again or start a new work item. In strict mode, re-run the
permission probe first if it is missing or no longer matches.

A run start that prints `WARNING: the tracked .gitignore does not cover ...`
will HOLD at SECURITY (`security preflight review-required`). Tell the user
then: committing a covering `.gitignore` before the run avoids it. At that
HOLD, the user adds the patterns to `.gitignore` in the worktree, neither
committed nor staged, and resumes (EXEC review and gate replay; the edit ships
with the delivery). A commit moves HEAD and HOLDs: restore HEAD to the run's
parent keeping the edit. A `.gitignore` already modified at the start does not
count as the run's own coverage: restore it to HEAD if HEAD's version covers
the categories, otherwise abort and start a new run after committing it.

## DONE and acceptance

With the saved `config.lifecycle_mode` `on`, DONE means the security stage
passed and acceptance is pending; with `off` (a run started before v2.10.0),
DONE has no finish, quality-polish, docs or security stages, so say so. Report
the stage receipts, open findings, operator verification records still valid
for the tree, and whether the operator profile's `auto_commit` will make one
local commit on acceptance; offer `accept` or `reject`. Never accept or reject
under handsfree, and never on your own judgment:
- Accept only after the user explicitly accepts in this conversation. Run
  `accept --intent-only` with the user's reason as `--reason TEXT` (or no
  `--reason` if they give none), show the digest, then run
  `accept --expect <digest>` with the same `--reason`.
- Reject only on the user's explicit rejection with their note: run
  `reject --intent-only --text NOTE`, show the digest, then run
  `reject --expect <digest> --text NOTE` in the host's long-command form (it
  reopens EXEC and dispatches model turns) and inspect the final status.
- Use `--override-rejection` only when the user asks for it with a reason.

Do not imply user acceptance or delivery authorization from DONE.
