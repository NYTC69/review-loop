# Paired-session entry (shared contract)

Both paired-session entry skills (`skills/paired-session/SKILL.md` for Claude Code,
`.agents/skills/paired-session/SKILL.md` for Codex) load this file through the
`entry-paired-session` stage. It holds every rule the two hosts share. The host
skill adds its host rules, for example run-directory paths, shell form, how a
long coordinator command runs and is stopped, and the exit
codes of its own setup steps; a host rule is more specific and wins. Use the
paired-session coordinator shipped with this plugin. The coordinator owns reviewer
dispatch and limits.

## Entry and failure handling

How this skill was entered decides failure handling (stage A = everything before
the first command that runs `bin/paired-session`):
- Default entry (a review-loop, review-pr or code-quality-loop handoff with the
  `entry` key absent) and explicit entry (an `entry: paired-session` handoff from
  any of those entries, or the user asked for paired-session) fail alike: the
  legacy workflow was removed in v2.13.0, so nothing falls back. A failed stage A
  check is reported back as `stage A failure: <reason>` and refuses, except the
  host skill's long-command execution failure, which is reported as HOLD with the
  reason. Print refusals with the handing-off entry's own name (`review-loop`,
  `review-pr` or `code-quality-loop`) as
  `<entry>: paired-session entry refused (<reason>)`.
- Every host setup step before the first coordinator command (loading this
  contract, creating the run directory, writing `WORKITEM.md`, resolving the
  plugin) belongs to stage A. If one fails or is denied, stop and report
  `stage A failure: <reason>`. Do not retry in another location or improvise,
  and never start or continue another workflow from this skill (no legacy
  session file, lock or evidence snapshot).
- From the first `bin/paired-session` command on, every refusal or HOLD is
  reported verbatim and never falls back to legacy.

## Stage A checks

On every entry, first check the host and the CLIs: a host that is not macOS
(`uname -s` is not `Darwin`) is refused with
`<entry>: paired-session needs macOS; Linux and other hosts are not supported` (`<entry>` is the
handing-off entry's name, or `paired-session` when this skill was invoked directly),
and every CLI the resolved roles need must be on PATH (`command -v`; the default
roles need `codex` and `claude`; the review-loop entry has already checked both on
a handoff).
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
The keys in `.review-loop/config.md` that are set map to one-run options:
`docs_file` → `--docs-file`, `skip_quality_polish` → `--skip-quality-polish
true|false`, `soft_limit_plan` / `soft_limit_exec` → `--max-plan-rounds` /
`--max-exec-rounds`, and `review_focus` / `review_style` / `quality_focus` → `--review-focus` /
`--review-style` / `--quality-focus` (each value as one quoted argument in the `--review-focus=<value>`
form, so a value that starts with "-" is not read as an option; the run freezes them and gives
review focus and style to the reviewer, the shadow and the gate, quality focus and style to the POLISH-Q
specialists, never to the author). `auto_commit` applies to a review-only run only (whatever entry started
it): `auto_commit: false` becomes `--auto-commit false` (the review-loop and code-quality-loop entries hand it
over), and absent or `true` keeps the review-only default (one local commit at `accept`), with no warning. On
the main pipeline do not apply, but print a warning for, `auto_commit: true`
(`review-loop: auto_commit in .review-loop/config.md is not applied by
paired-session; set it in the operator profile`), and for a `reviewer_model` /
`executor_model` set to anything other than empty or `inherit` (models come
from the operator profile, ADR-9). Never pass `--adversarial-gate off`.

Quality writers (`quality_writers`: `both`, `simplify`, `tests` or `off`; set by
`--quality-writers` or any profile, frozen at run start): after a clean POLISH-Q
a fresh author session may simplify the changed code, then consolidate the
changed tests. The default is `both` for a review-only run and `off` for the main
pipeline, whose delivery report then names `--quality-writers both` (about +10
invocations). A writer is skipped with a recorded reason: `no-test-command` (no
explicit `--test-command` or profile `test_command`), `small` (under 20 changed
code lines), `no-test-file`, `budget` (no invocation room for one replay) or
`no-green-baseline` (the test command fails before any writer). A kept change
(`wrote`) passed the test command and is reviewed again (EXEC reviewer, shadow,
gate, FINISH, specialists) before DOCS, on its own round budget (one replay round
and one fix round, outside `--max-exec-rounds`); otherwise the change is rolled
back: `rolled-back:tests` (the command failed, or the test writer lowered the
number of test cases), `rolled-back:boundary` (a reserved docs path or
`.review-loop/` config), `rolled-back:review` (the replay's first review or gate
did not approve; when the two writers changed different files and the findings
name one of them, only that one is rolled back and the other is reviewed again),
`rolled-back:hold`, or `exhausted` (a failed attempt). Report each writer's
outcome with the stage receipts.

## Work item

Write `WORKITEM.md` in the run directory the host skill names (always outside
the product worktree, and best outside any git repository: tools that refuse
scratch space inside a repository then fall back to /tmp, which the Codex
read-only sandbox denies; `run` warns about it) with goal, acceptance criteria, scope, and verification.
The first line is the title, `# <one-line summary of the task>`: `accept` uses it as the commit title and the delivery report names the work item with it, so never a generic heading such as `# Work item`.
Add a `secret-scan-allow: <path or glob>` line (one per path) only when the user says a secret-like literal under that path is a deliberate test fixture; the coordinator's secret scan otherwise blocks it.
Include only user-approved requirements; mark uncertainties as questions
instead of inventing acceptance criteria. Set the test command from the loaded
profile or the verified project command and pass it as one quoted argument;
never interpolate user text as shell code. Write launcher logs under the run
root's `logs/`, not next to the run dir (a strict probe counts a write beside
the run dir as an author escape). Read a launcher log only when a coordinator
command ends without its final line (DONE, HOLD, REFUSED, ACCEPTED, REPORTED or
a probe PASS/FAIL), for example after a crash or a traceback: then read its last
lines for the error. Otherwise read `RUN_DIR/state.json` or `status --brief`, and
never stream a whole log into the conversation.

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
  reviewed path is delivered. A review-only run defaults to `auto_commit: true`
  (owner 2026-10-06); `auto_commit: false` in `.review-loop/config.md` (as `--auto-commit false`, see Profile
  and settings), an explicit `--auto-commit false` or operator-profile
  `auto_commit: false` wins, and `accept` then lists the uncommitted files. `--plan-only` does not apply: refuse it in stage A.
- `WORKITEM.md` starts with a title that names the change (`# <one-line summary
  of the change>`, see Work item), then states the review goal (one line such as
  "Review the change for correctness" is enough) and carries no review history
  (ledger ids or earlier findings).
- Pass `--review-only` (and `--base`) to `permission-probe` and `run`
  identically, with the lifecycle flag of any new run (Safety mode and the first commands). Later commands read
  the entry and base from the saved state; never pass a different `--base`.
- `run` refuses before creating any state when the change is empty, the index
  has unmerged entries or partially staged paths, the base is not an ancestor of
  `HEAD`, or the work item carries review history; report the refusal verbatim.
  The change as created is the user's code (FIELD-26/27): its content and its
  paths pass the fresh shadow and gate scan even when they name a vendor or a
  review (`bin/codex-run`, `docs/gate-review-notes.md`); text or paths a later
  fix round adds are scanned as before. A changed path shaped like a ledger id
  or a verdict (`docs/F001.md`, `APPROVE.txt`) is still refused: tell the user
  to rename the path or review that change by hand.

## Code-quality-loop entry

A code-quality-loop handoff (`/review-loop:code-quality-loop` on its paired route,
`paired_session/docs/cql-retirement.md`) is a review-only run: everything in the review-only entry
above applies (the change is the whole non-ignored worktree against `HEAD`, the same stage A listing
and refusals). code-quality-loop takes no base, so no `--base` is passed.
- Notices: the code-quality-loop skill owns its entry line and loss notice (its Step 0). If they are not already in this conversation's visible output, print them verbatim as the first output after this contract is loaded, before any stage A check or coordinator command:
  `code-quality-loop: paired-session review-only run (entry set in .review-loop/config.md)` (key set) or
  `code-quality-loop: paired-session review-only run (the default entry)` (key absent), then
  `code-quality-loop: the paired route reviews, fixes, simplifies and consolidates tests, and accept makes one local commit (never a push; `auto_commit: false` in .review-loop/config.md keeps it uncommitted); it does not reorganize, run static-analysis artifacts, load a design document or sweep project docs`.
- CLIs: the code-quality-loop skill checks nothing before it hands off, so run the direct-invocation CLI
  check of Stage A checks here; a missing CLI is a failed stage A check.
- Test command: as for any review-only run (profile `test_command`, else the verified project command).
  Pass it as `--test-command`: the quality writers need an explicit one (`skipped:no-test-command`).
- Quality writers: the review-only default `both`; do not pass `--quality-writers` (a profile value wins).
- Budget: pass `--max-invocations 45` unless the operator profile sets `max_invocations` (under the
  default 25 the writers are usually `skipped:budget`; 45 is a cap that leaves room for the non-blocking fix round
  and the writers on a typical run). Rounds: a handed-over N wins over `soft_limit_exec`;
  pass one `--max-exec-rounds` value, the same to `permission-probe` and `run`.
- Commit: `auto_commit: false` in `.review-loop/config.md` is handed over as `--auto-commit false`; pass
  it. Otherwise the review-only default applies (`auto_commit: true` unless the profile says false; no
  warning for `auto_commit` on this handoff): `accept` makes one local commit and never pushes.
- `WORKITEM.md`: the first line is `# code-quality-loop: <one-line summary of the uncommitted change>` (the commit title), then
  the goal "Review and improve the quality of the uncommitted change: correctness, error
  handling, tests and simplicity; fix what the reviews find." No review history. `quality_focus` and
  `review_style` reach the review roles through the run flags of Profile and settings, not the work item.
- Non-blocking fix round: pass `--advisory-fix-round true` in both blocks (owner decision "加一轮修非阻塞"). After the first clean
  POLISH-Q the author gets one round for the open non-blocking findings (MINOR, LOW, a gate's MEDIUM/LOW): fix what is
  reasonable, dismiss the rest with a reason; then EXEC review, shadow, gate, FINISH and the specialists run again, before
  the quality writers. It runs once, on its own round outside `--max-exec-rounds`, and is skipped with the reason when the
  invocation budget has no room; findings still open stay advisory.
- Result: DONE (or HOLD) as for any review-only run; report each quality writer's outcome and the fix
  round (state.json `lifecycle.quality_writers` and `advisory_fix`) with the stage receipts, and accept or
  reject only on the user's explicit decision. `accept` writes the delivery report (DONE and acceptance).

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
  `review-pr: simplify is not part of the paired review-pr (a writer that edits the checkout); run /review-loop:code-quality-loop on the change (its POLISH-Q simplifier)`,
  never ignore it silently. `parallel` does not apply (the coordinator schedules its roles); say so in
  one line and continue.
- Tests: no test command is the default for every review-pr input (Q-R3): pass `--no-test-command`
  instead of `--test-command`. A test command runs only when the user confirms one for this review; a
  PR's test code then runs on this machine. Never ask about tests under handsfree; a declined or
  unanswered offer leaves the run without tests and never refuses the request.
- `WORKITEM.md` states the review goal and the pins (target repository, PR URL and number, head OID,
  pinned base OID, merge base), and no review history. Write the materializer's JSON object, unchanged, to
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
- Failures: a failed stage A check refuses on every entry and for every input, and never falls back to
  legacy (removed in v2.13.0; Entry and failure handling).
- Residual, stated: report mode does not narrow the Codex read root. Codex read-only roles (the gate,
  with the default roles) can read the whole filesystem, credential files included; the efficient
  evidence guard only logs. A prompt injection in an untrusted PR could have a role quote such a file
  into a finding, which is why the post is scanned and confirmed. Narrowing it needs a verified Codex
  permissions profile (follow-up). An operator who wants the credential deny on every role uses Claude
  read-only roles (operator profile).

## Safety mode and the first commands

Pass `--lifecycle-mode on` to `run` and, in strict mode, to `permission-probe`, identically, only when no operator/workspace profile sets `lifecycle_mode`.
The CLI value overrides any profile value, so when a profile sets `lifecycle_mode`, omit the flag (or pass the
profile's value) in both commands. Never pass
`--skip-probe` or `--accept-unverified-codex-cli` on your own initiative.
When the user selects the stop-after-gate route, pass `--lifecycle-mode off` in both commands.
The removed `--adversarial-gate off`, `--override-rejection`,
`--accept-unverified-claude-author` and `--accept-probe-skip` options are refused.
Use the enabled gate, `resume --add-rounds N` or `note --scope-change` at HOLD, and permission-probe for strict runs.
`--lifecycle-mode off` is the supported stop-after-gate route: PLAN -> EXEC -> GATE -> DONE (acceptance pending). The operator then handles FINISH, POLISH-Q, docs, security review and merging. Accept never commits on this route: it hands back the uncommitted tree. New runs default to `on` (the full lifecycle); CLI, operator/workspace profiles and Python entry points may select `off`, and saved off runs resume, accept, reject, note and abort normally.
`--strict` comes only from the user or the operator profile (`safety_mode`),
and goes to `permission-probe` and `run` alike:
both modes keep every sandbox; the default `efficient` mode does not require
the probe PASS and its evidence guard only logs, while `--strict` restores
both (`paired_session/docs/efficient-mode.md`). An efficient run needs no probe waiver. The run is strict when the user asked for
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

Add the same `--config` and mapped one-run options to every call. Plan only (the Claude skill's
`--plan-only`, or a request on either host to stop after the plan): add `--stop-after-plan` only to `run`,
never to `permission-probe`; the run HOLDs after PLAN approval (`PLAN approved; stopped by
--stop-after-plan; resume enters EXEC`) with the approved plan in `RUN_DIR/plan.md`: show it, and `resume`
only on the user's request. Every command that can
dispatch model turns (`permission-probe`, `run`, `resume`, `reject --expect`)
uses the host skill's long-command form. Wait for each such command to finish
before starting the next; do not start a second run while one is active.

## Start line and lifecycle-mode backstop

Once `RUN_DIR/state.json` exists (strict: after the probe, before `run`), read
its frozen `config` and print one start line from it: author, reviewer and gate
vendor and model; plan and exec rounds, invocations and timeout; docs file and
skip-quality-polish; and which values came from `.review-loop/config.md`. In
strict mode, if `config.lifecycle_mode` differs from the selected mode (`on` by default,
`off` for the stop-after-gate route, the profile's `lifecycle_mode` when it sets one), run `abort` with the run's
saved options and report a plugin version mismatch instead of starting the run.

While the run is active, do not call plain `status`: it needs the run lease
and, while the run holds it, prints
`HOLD: another coordinator currently owns this run` although the run is not on
HOLD. Read `RUN_DIR/state.json` directly or use `status --brief`. As a
backstop, if the running state shows `config.lifecycle_mode` different from the selected mode,
stop the running coordinator command (host skill), wait until the child in
`state.active` has exited, run `abort` with the run's saved options, and report
a plugin version mismatch.

## Existing runs and HOLD

Later commands on an existing run (`resume`, `permission-probe
--retry-uncertain`, `abort`, `reject`, `accept`, `note`,
`attach-verification`) pass the saved `state.json` `config.lifecycle_mode`
value and the run's original workspace, work item, run directory, profile and
options, never the current default. Saved off runs use this release normally.
Report DONE/HOLD and the run directory. On
HOLD, inspect its state, findings, and receipts before resuming. If `uncertain_active` is
present, do not rerun the probe or resume automatically: check its pid and
receipts; if the child is still alive, wait for it to stop. If its phase is
`PROBE` or `AUTHOR_PERMISSION_PROBE`, ask before rerunning the disposable probe
with `permission-probe --retry-uncertain`. For a product-work turn, ask before
`resume --retry-uncertain` because this may replay a model turn. After
recovering an interrupted probe, use `resume` on the existing run directory;
do not use `run` again or start a new work item. In strict mode, re-run the
permission probe first if it is missing or no longer matches.

For coordinator HOLD, acceptance, rejection and round-limit rules, use
[the operator reference](../../paired_session/README.md#acceptance-rejection-and-round-limits).
At a round-limit HOLD, report the open findings and offer the documented exits.
Add rounds only on the user's request, with the number they choose; never on
your own initiative.

A run start that prints `WARNING: the tracked .gitignore does not cover ...`
will HOLD at SECURITY (`security preflight review-required`). Tell the user
then: committing a covering `.gitignore` before the run avoids it. At that
HOLD, the user adds the patterns to `.gitignore` in the worktree, neither
committed nor staged, and resumes (EXEC review and gate replay; the edit ships
with the delivery). A commit moves HEAD and HOLDs: restore HEAD to the run's
parent keeping the edit. A `.gitignore` already modified at the start does not
count as the run's own coverage: restore it to HEAD if HEAD's version covers
the categories, otherwise abort and start a new run after committing it. A
review-only run is different: the reviewed change is the run's own (its
baseline is the review base), so a covering `.gitignore` edit in that change
counts at SECURITY; the start warning then needs no restore or abort.

## DONE and acceptance

With the saved `config.lifecycle_mode` `on`, DONE means the security stage
passed and acceptance is pending; with `off`, convergence goes straight to
DONE without finish, quality-polish, docs or security stages, so say so.
The old off-route POLISH round is removed in 3.0.0; the hidden CLI/profile
option `--polish-round on|off` remains an accepted deprecated no-op (removed in 3.0.0; ignored). Report
the stage receipts, open findings, operator verification records still valid
for the tree, and whether the run's frozen `config.auto_commit` (the operator
profile, the CLI or the review-only default) will make one local commit on
acceptance with lifecycle on. Off acceptance returns the uncommitted tree; offer `accept` or `reject`. Never accept or reject
under handsfree, and never on your own judgment:
- Accept only after the user explicitly accepts in this conversation. Run
  `accept --intent-only` with the user's reason as `--reason TEXT` (or no
  `--reason` if they give none), show the digest, then run
  `accept --expect <digest>` with the same `--reason`. Relay its `COMMIT:`
  (the local commit, never pushed) or `UNCOMMITTED:` (the files to commit
  yourself) line verbatim, and show the delivery report named by its
  `REPORT:` line (`RUN_DIR/delivery-report.md`, written only at ACCEPTED).
- Reject only on the user's explicit rejection with their note: run
  `reject --intent-only --text NOTE`, show the digest, then run
  `reject --expect <digest> --text NOTE` in the host's long-command form (it
  reopens EXEC and dispatches model turns) and inspect the final status.
- At a round-limit HOLD, use `resume --add-rounds N` on the user's instruction; acceptance requires DONE.

Do not imply user acceptance or delivery authorization from DONE.
Do not raise the invocation cap on your own: use `resume --max-invocations N`
only when the user asks. After a raise, later commands pass the raised value.
