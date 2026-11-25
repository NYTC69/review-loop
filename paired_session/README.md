# Paired-session coordinator

This is the tracked implementation of the paired-session workflow. Its stable
repository-local entry point is `bin/paired-session`. From v2.10.0, a fresh
`/review-loop` request without an `entry` key hands off to this coordinator through
the paired-session skill (the default entry, `docs/v2.10-entry-switch.md`);
the legacy workflow was removed in v2.13.0 (`entry: legacy` is refused; `/review-loop:legacy` was deleted in v2.13.1).

The coordinator runs one author and reviewer through PLAN/EXEC, then applies
fresh shadow/adversarial checks and a delivery sequence. This README owns the
coordinator CLI rules; host interaction lives in
[paired-session entry](../docs/protocol/paired-session-entry.md). It owns isolated run
artifacts, workspace snapshots, invocation limits, and a permission preflight.
Reviewer receipts record the requested model beside the provider-reported
identity and classify it as `MATCH`, `MISMATCH`, or `UNREPORTED`. A match requires
the exact requested ID or that ID with an explicit date suffix; synthetic,
malformed, or missing identity data is unreported, and Claude subagent models
are ignored.
Every run uses one mode (`docs/efficient-mode.md`): every sandbox applies and no permission probe is needed.
`permission-probe` and strict mode were removed in v3.7.0: `permission-probe` is refused, and `--strict` or
`"safety_mode": "strict"` in a profile only prints a NOTE.
One OS-backed lease serializes commands that mutate a run directory. A second
worktree lease prevents `run` or `resume` commands using
different run directories from coordinating on the same workspace at once. Its
key is the resolved workspace directory's `(st_dev, st_ino)` identity, so
case-variant or symlink paths to the same directory share a lock.

The workspace lock lives outside the product workspace in
`/tmp/paired-session-workspace-leases-<uid>/<sha256-of-device-and-inode>.lock`.
If that location would be inside the workspace, the coordinator tries `/var/tmp`
and then the configured system temporary directory. It uses a private `0700`
per-user directory and fails closed if it cannot create or validate the lock.
A contention HOLD reports the holder PID and `run_dir`. The OS releases the
`flock` when the process exits; the small lock file remains for reuse. Do not
remove a lock file or its directory while a coordinator may hold it. Normal
operation needs no manual cleanup; remove stale files only after confirming no
coordinator can still hold the corresponding lease.

`abort` takes only its own run-directory lease. It marks that run HOLD without
modifying the product workspace, so an operator can abort a stale run while a
different run holds the workspace lease. Fresh-shadow CRITICALs enter the
findings ledger and follow the author repair path even when the persistent
reviewer approves.
Explicit subscription or quota rejections from Codex, Claude or Gemini retry the
same turn within the run-wide wait budget. Every rejection retains its own
receipt and refunds the invocation. Receipts keep the provider's reset hint and
include a numeric reset timestamp when it can be parsed. A terminal Gemini `agy`
result with status `ERROR` and wording such as `quota reached` or `quota exceeded`
is an explicit rejection. Relative reset hints such as `Resets in 164h` determine
the wait. If that wait exceeds the remaining budget, the run immediately enters
a `rate_limited` HOLD with the budget reason.
A turn that fails with a recognized transient provider error ("Selected model is
at capacity", Anthropic's overloaded error) before any tool call or workspace
change is retried once after 60 seconds without consuming the invocation budget.
For a positive agy exit, the terminal JSON `ERROR` can supply that diagnostic.
Explicit quota rejections take precedence. A second qualifying transient failure
holds, and `resume` retries the turn.

## MINOR-only review revisions

An advisory-only persistent reviewer REVISE becomes `APPROVE_WITH_ADVISORY` in
any PLAN or EXEC round, including a quality-writer replay (the writer change is
kept); there is no advisory repair round (WI-GATE1). Advisory-only means findings
below the blocking threshold, and/or only earlier non-blocking rows reported
`still_open`, with no blocking row open. Those rows remain open and labeled advisory in the ledger and report
until a later reviewer confirms they are fixed or withdraws them with evidence.
The raw REVISE and effective verdict are recorded separately. The next phase
receives the rows as non-blocking context. Every non-blocking row still open at
DONE is listed in the run's `backlog.md`; `accept` appends them to the workspace
`BACKLOG.md` (created with a `## P2` heading when missing), inside the accepted
commit when `auto_commit` makes one (the worktree file is written only after the commit is journaled),
otherwise in the working tree. A `BACKLOG.md` that is a symbolic link, not a regular file or ignored by git is
never written: the rows stay in `backlog.md` and accept prints a NOTE.
An EXEC advisory-only REVISE whose only gap is the test evidence HOLDs for `attach-verification` (no round).
Before the gate, the coordinator rechecks any test proof the EXEC approval reused.
EXEC requires proof that the configured test passed on the reviewed tree and non-empty `self_run_evidence`
from the persistent reviewer's own independent checks. The reused result need not appear among those commands.
A submitted EXEC APPROVE with an empty list gets one correction in the same persistent session, after
disposition validation and before applying the answer. The discarded answer supplies no reusable test proof.
When a test is configured, the retry prompt asks the reviewer to rerun it and report it in `self_run_evidence`.
Disposition and evidence errors share this one retry, which counts toward the invocation cap. Resuming the same
reviewer turn keeps the saved correction and its retry count, including after a stopped quota wait or quota HOLD.
Any later author receipt starts the next review with a fresh correction opportunity, including a failed or
quota-denied author turn or one in another phase. If the retry supplies valid dispositions and approves with an
empty list, normal review routing applies its dispositions and records the verdict first. An answer still marked
APPROVE then HOLDs with `EXEC APPROVE rejected: empty self_run_evidence`. Its receipt records
`approval_blocked_reason` and supplies no reusable test proof. The no-test review-report path can approve statically.
Blocking findings retain the normal repair loop or round-limit HOLD. The opt-in advisory fix round (code-quality-loop) keeps its own single
round; its eligible advisory re-review exits immediately.
Blocking means a CRITICAL, MAJOR or SECURITY finding whose category is correctness,
data-loss, security or money. Findings carry a reachability statement, and an empty statement leaves an otherwise
blocking finding blocking. Gate FAIL items are mandatory checks. Category `other` (wording, style,
docs polish, extra robustness) never blocks. Program rows (test failure, secret scan, independence scan, sensitive paths)
set their own blocking category. A MINOR or LOW security-flagged finding is listed but advisory. Eligible blockers at the cap follow the
[automatic delivery round rules](#acceptance-rejection-and-round-limits). Legacy round-limit HOLDs name the
rule that stopped the run in a NOTE line.
Review comparison shows each reviewer decision separately from the final coordinator
status, which may still be HOLD after later checks.

Delivery PLAN drafts include a [scenario matrix](../docs/protocol/scenario-matrix.md).
The coordinator rejects a missing or malformed matrix at approval, and the PLAN reviewer
checks that it covers blocking-class scenarios. The per-round EXEC reviewer and fresh shadow
own robustness and corner-case depth, including checking that each row's named test exists
and passes. The gate makes a single verdict and audits coverage and gaps. An unmet acceptance
criterion or a blocking-class defect can cause FAIL, including a defect outside the written
criteria. Non-blocking corner cases go to BACKLOG. Review-only, code-quality-loop, and review-pr
skip the matrix requirement and audit.

Operator rulings are persistent review input on both lifecycle routes. The
persistent reviewer sees the withdrawn ids and reasons, owes no disposition for
them, and must not raise the same objection as a new finding. If the reviewer
still returns such a disposition, it is ignored and recorded as
`ignored_prior_findings` in the receipt with reason `operator-withdrawn`.
Extra `fixed` and `withdrawn` rows for already closed, unrequested findings are recorded there with
reason `closed-unrequested` when dispositions are applied. The annotations are merged without duplicates
in state and the evidence receipt on replay, and the original answer stays in the receipt.
An applicable disposition for any other unrequested id triggers a correction naming the id and asking for
exactly the requested dispositions. A second invalid or incomplete disposition answer HOLDs.
Fresh shadow and
gate turns receive only `operator decision: <reason>`, without finding ids or
review history. The fresh-prompt scan accepts this operator decision channel
like frozen project guidance; ordinary review history remains subject to the scan.
This is a scope decision for subsequent review, including writer replay, not
semantic matching or automatic suppression of similar findings.

The fresh shadow and gate must not see review history: before launch they refuse
any input that carries reviewer ledger ids (`F` plus three or more digits) or
review narratives (for example "previous review" or "response to reviewer"),
including `context/plan.md` and `context/workitem.md`. FIELD-23 prevents ACCIDENTAL carry-over of review history into a fresh context;
it does not defend against an author who deliberately evades the check.
Deliberate evasion is a known residual. Review independence otherwise rests on
the fresh session and the gate templates, which stay unexempted. The prompt scan
exempts frozen project guidance and the operator decision channel described above.
The inline scenario matrix in the shadow prompt uses the plan-path rules on the
first dispatch and on retries. The coordinator scans the surrounding prompt,
including retry notes, for history and saves the complete dispatched prompt in the audit.

With a run's `base_commit`, both delta patches scan only added (`+`) lines,
excluding `+++` headers; context and removed lines are never scanned. A match is
exempt if the same matched text, case-insensitively, occurs anywhere in that
file at the base commit (post-image path; for renames, also pre-image path).
New files and binaries get no base-text exemption. Bare model names in added text can still qualify for the work-item exception. A base marker reused anywhere in the
same file therefore passes, including copies: this is a documented residual.
The exempt text is the whole match ("Claude approved", not "Claude") and must
occur at the base as a whole word ("APPROVE" is not in "approved"). Another
known residual, on the strict side: a file moved without `git mv` (`mv`, or `rm`
plus a new file) appears as a new untracked file and gets no base-text exemption, so a
base marker in it holds; use `git mv`. In a review-only run (FIELD-26) the change
as created is the user's code, not review history: a match is also exempt when
the same file in the run's creation mirror (`internal/review-start`) contains
it, new files included, so a PR that names a vendor passes. Text a later fix
round adds is not in that mirror and is scanned as before. Its paths count the
same way (FIELD-27): `delta.stat`/`status.txt` exempt the mirror's paths and the
frozen review scope's initial-change list is masked, so `bin/codex-run` passes
while a path a fix round adds is caught; a ledger-id or verdict shaped path
(`docs/F001.md`, `APPROVE.txt`) is still refused at creation (FIELD-11). A
scope-change successor keeps its parent's creation mirror for this.
In `delta.stat` and `status.txt`, matches inside paths that exist at the base
commit are exempt. Repository quotations in `plan.md` and `workitem.md` get the base-text exemption only inside
backticks or fenced code blocks, with the matched text present in a file at the base commit.
Bare Claude, Codex, Opus and Astra names have separate prose exceptions. For added product text, name the model in the frozen work item
as a whole word or a numeric model ID such as `claude-opus-5-5` or `gpt-6-astra`.
Names such as `pre-Opus` and paths such as `/opt/claude-opus-5-5/` do not establish this exception. In `plan.md`, a slash chain of three or more plain words is prose when every part is a model name or a word of at least three letters that the frozen work item uses on its own (`terra/Opus/sol roles`), since v3.8.6; `readers/opus/cache` stays a path. Attribution, vendor paths, finding IDs and verdict words remain subject to the scan, with three exceptions since v3.8.5: a tool-named directory path (`runs_live/codex_0166/`) passes in the delta patches and in `plan.md` when the frozen work item names that tool as a word of its own (a model ID such as `claude-opus-5-5` does not count), unless a tool-named component is a dot directory or the sandbox scratch root (`claude-501`). Tool-state dot directories use the explicit declaration below. A tool name as the last path component (`node_modules/.bin/codex`) counts as a file name for the work-item exception unless a directory before it is the tool's own (`codex/`, `.codex/`, `claude-501/`) or an attribution verb follows it (`GPT/Codex signed off`). In the delta patches, linter rule codes after `# noqa:` are not finding IDs. A refusal lists only the added lines that hold the matched text. Without a
`base_commit`, the old scan applies. The same helper applies to fresh shadow,
gate, PLAN approval and review-only creation inputs.

When the work is about tool-state directories, add `history-scan-subject: claude codex gemini` to the work item
with the applicable vendor IDs. Each plain or Markdown-list declaration takes whitespace-separated,
case-insensitive IDs. Valid declarations combine. An empty or malformed declaration grants no exception.
The frozen declaration allows the corresponding complete components in `plan.md` and in added lines of
`delta.patch` and `delta-since-last-review.patch`. It covers `.claude`, `.codex`, `.codex-paired` (including
suffixed roots and literal `.codex-paired*`), and `.gemini`, including quoted path components such as
`joinpath(".claude", "projects")`. A valid declaration also names its vendors for the existing bare-name
exception in the delta patches. Finding IDs, verdict words, review attributions, and review-history phrases remain subject to the scan.

To recover a held run, `abort` it (the workspace stays untouched), then start a new run with the declaration in
its work item. On a dirty lane, the new run uses HEAD as its base. Its change includes the earlier uncommitted
edits, and the new PLAN author sees the already-implemented tree.
When a shadow or gate is
on, a plan that the reviewer approves while it still carries ledger ids or such
wording is sent back to the author for a restatement without them, so the gate
does not refuse it after EXEC. With no PLAN round left, the author gets one extra
rewrite-only turn that does not count as a PLAN round; the PLAN reviewer then
reviews the rewrite against the plan it approved, and the run holds at PLAN if
the history is still there or the plan changed in substance. A REVISE on the rewrite with no finding of its own that only
leaves non-blocking rows open takes the advisory exit of any PLAN review at the limit instead. Vendor names and other gate-scan triggers in the plan are still found
only at the shadow or gate. The work item is never rewritten: a work item with
`F001`-style identifiers or review narratives holds at PLAN, so keep them out.

Secret scan (V312-S, every route, `--lifecycle-mode off` included): before each
EXEC review verdict is accepted and before the gate, `leak_scan.py` reads the
added lines of the run's change (a patch the coordinator computes for the scan,
the change of `context/delta.patch` with the files' own line endings: tracked
changes against the base plus new untracked files; a review-only or review-pr
run, the change under review). The rules are `scripts/content_rules.py`, one table with a scope
per rule. Added lines are text the run adds, so this scan applies every rule:
the six `whole-delivery` rules (a `-----BEGIN ... PRIVATE KEY-----` marker
followed by key material: 40+ base64 characters after optional line breaks,
`\n` escapes (with one backslash or more, as in a JSON document stored as a
string) and spaces, so a regex or a template that only names the marker is
not a hit, and a marker line with the key body on the next added line is one hit
at the marker's line; `AKIA`/`ASIA` key ids (not an id that ends in `EXAMPLE`,
AWS's documented placeholder), GitHub `gh[pousr]_` and
`github_pat_` tokens, Slack `xox[baprs]-` tokens, Google `AIza` keys, Stripe
live keys) and the `added-text` rules: JWTs (`eyJ` plus three base64url
segments, the third of 43+ characters, the shortest standard signature; not a
token whose header has an `x5c` certificate chain, which is signed data), `sk-`,
`sk-ant-` and `sk-proj-` keys, and a name that contains key, secret, token or
password given a quoted literal of 20+ characters without spaces (not a value
that starts with `sb_publishable_`, `pk_live_` or `pk_test_`, not a name that
contains `public` or `publishable`, and not a JWT-shaped value, which the JWT
rule alone judges). The SECURITY preflight
(`scripts/security_preflight.py`) reads the whole worktree, files the run never
touched included (a hit there is a NOTE, not a HOLD), and holds on hits in added or changed files with no repair,
so it applies only the six
`whole-delivery` rules, the six it had before V312-S: a JWT-shaped sample or a
key-named literal the repository already contained does not hold a run. Its
`--file` scan of a review-pr post body is published text and applies every rule.
The added rules need a value that mixes
letters and digits with one random-looking run of 16+ characters (`_`, `.`, `:`,
`/`, `-`, `|` and `#` separate runs, so a composite id of short words is not
one) and skip
placeholders (`xxx`, `<...>`, `${...}`, changeme, example, dummy, test,
`your_...`, a single repeated character); an environment or config lookup is not
a quoted literal and never matches. Accepted residual: a password-shaped literal
whose segments between those separators are all shorter than 16 characters is
not a hit. A hit is one program finding (`secret-scan`, severity SECURITY) that
the author repairs in a normal EXEC round; the scan closes it once the lines are
gone. It names `path:line (kind)` only, never the value, in the ledger, prompts,
progress, state and reports. With no EXEC round left the run holds with
`secret scan: the change still adds a hardcoded credential at ...`; a review-pr
report lists the finding under Security. A deliberate test fixture is exempted
by a `secret-scan-allow: <path, directory or glob>` line in the work item, one
per line; its hits are recorded as exempted in `state.json` (`secret_scan`).
Declare exemptions before starting the run. If a deliberate fixture or false positive appears later, start a new run with the exemption; resume uses the frozen work item.
The marker exempts hits of this every-route scan only: the SECURITY stage's six
whole-delivery rules are not affected by it. Lines the FINISH, POLISH-Q and DOCS
writers add after the gate are covered by those six SECURITY rules, not by the
added rules, unless the stage sends the run back through EXEC (the review and
the gate, where this scan runs again). The
scan catches the realistic mistake of committing a real credential; obfuscated or
deliberately hidden values are out of scope.

## Local marketplace installs

Installing a plugin from a local directory marketplace copies the whole directory,
including Git-ignored files, into the host plugin cache. That can include
`.compass/`, `.claude/`, `HANDOFF.md`, and local caches. Install from a clean
checkout or the GitHub marketplace to avoid copying working-tree state into the
plugin cache.

## Start a task

For simultaneous lanes, read [Concurrent runs and isolated Codex homes](docs/concurrent-runs.md).

Use a dedicated Git worktree for fresh product work; review-only runs use the
worktree that holds the existing change. Default persistent output is
`<repo>/.review-loop/runs/<UUID>/` (gitignored). Resolve `<repo>` using
`git rev-parse --path-format=absolute --git-common-dir` and its parent when it
ends in `/.git` and the parent is a work tree; lane worktrees therefore use the main checkout. When unresolved
(not a Git repo, a bare repo or a submodule/separate-git-dir checkout), report fallback to
`${XDG_STATE_HOME:-$HOME/.local/state}/review-loop`. An explicit run directory wins;
existing runs resume at their original `--run-dir`, without migration.
Scratch lives in a private per-run root outside any repo (FIELD-28 only warns for
legacy scratch still inside a repo). Do not run `git clean -x` on the workspace
while a run is active: it would delete the run. The workspace must be a Git worktree and the work item must
be a file. The configured test command is checked before the coordinator starts.
A Claude reviewer or gate runs that command (and each `--reviewer-command`) as
one exact allowlisted Bash call in dontAsk mode, so command substitution, pipes,
`;`, `&&`, redirection or loops can be refused before it runs; a new `run`
prints a warning for such a command. Put it in a script and
configure `/bin/bash /absolute/path/to/script.sh`. When a reviewer's run of the
test command hits the CLI's own Bash timeout, configure a faster test command.
FIELD-20 (long test commands): Codex's `exec_command` returns after about 10 s
with a session id and no exit code while the command keeps running. Every
reviewer, gate, docs-reviewer and specialist prompt now tells the role to poll a
still-running command until it has an exit code, never to start another command
or end the turn before that, and to give a long Bash call the turn's command
maximum in milliseconds (at least 3600000 ms). A role's single command may run
for the turn's timeout less an answer margin of 10 % of it (at most 10 minutes),
so above the floor a command that uses its whole maximum still leaves the role
time to answer (a shorter turn keeps the floor and its own deadline still applies);
the floor is 60 minutes of command-tool allowance, so turns of 4000 s or less
keep 3600000 ms. The turn timeout is `--timeout` (default 2700 s) for reviewer
and gate turns, or `--exec-turn-timeout` (default max(7200, `--timeout`), capped
at 14400 s) for EXEC/FINISH/DOCS author turns: the default two-hour author turn
gets 6600000 ms, a four-hour turn 13800000 ms. Those author turns now get the
same long-command rule with their own value; where the coordinator appends the
rule to a prompt that lacks it, the rule comes under its own heading
(`## Coordinator rule for long commands`), so it never reads as part of an
operator rejection or clarification above it.
A command started but never completed (no exit status when the turn ended, or
`-1`) is reported as "not
observed to completion" in an EXEC or DOCS approval HOLD, not as an ordinary
failure. One observed completed exit-0 run of the exact configured command is
still required. The remaining limits are the per-dispatch timeout (`--timeout`)
and the command tool's timeout (reported as a command-tool timeout cause in the EXEC approval HOLD reason and
the DOCS reviewer's HOLD);
a test command longer than either cannot be observed to completion.
Write launcher logs (for example `run ... > run.log`) under
`<repo>/.review-loop/logs/`, outside the run dir's parent. When any role is Codex, `run`, `resume` and `reject`
refuse up front if the selected `CODEX_HOME` (default
`~/.codex`) is not an existing directory.

For operator-selected programs, role/vendor settings and test commands, copy
`paired-session-config.example.json` to an operator-owned path outside the
workspace, run directory and author temp directory, then pass it with
`--config /absolute/path/to/profile.json`. The example leaves `docs_file` out,
so a worktree-lifecycle run keeps its `CHANGELOG.md` default. It sets
`lifecycle_mode: on`, the default full lifecycle; select `off` to stop after gate. A workspace
`.review-loop/paired-session.json` may hold limits and other non-program
settings, but program/role/test-command keys there are refused (`REFUSED`, exit 2)
when that profile is selected, before any run state is created, and an existing run that later finds such keys
holds. `--config` replaces the workspace profile; copy any desired limits
into the external profile because the two files are not layered. The CLI loads the selected profile for run
and resume; explicit CLI options override it. Resume uses saved role models and efforts for omitted flags.
See [resume settings and relocation](#changing-role-settings-or-moving-an-install) for the explicit overrides.
A changed binary or PATH is recorded with a NOTE
(`operator program or PATH changed since run start (changed: path_env); the run continues with the new state`)
and the run continues; a program that is now missing or unsafe still holds. The names of secret-looking environment
variables (for example a `*_TOKEN` set by an agent session) feed the Claude credential deny list, which always
follows the current environment; they also bind the lifecycle role manifest.
Role models are operator-set (ADR-9): a role without `--author-model`,
`--reviewer-model`, `--gate-model` or a profile value gets its vendor's default
(Claude: `claude-opus-5-5`; Codex: `gpt-6.1-sol`); gemini (`agy`, ADR-21) has no
default, so its role needs a model. Before run state is created,
every model id must be well formed and, when `allowed_models` is set, listed for
that role's vendor. An allowlist refusal names the `allowed_models.<vendor>` key
to edit in the paired-session JSON profile for new runs.
The Step 3.4 gate defaults to the author's vendor (ADR-10);
`--gate-vendor` overrides it and is recorded as `gate_vendor_source: operator`,
and a `--gate-model` of the other vendor without `--gate-vendor` is refused.
`lifecycle_mode` defaults to `on` for CLI and Python entry points.
The profile-only key `extra_writable_roots` (default `[]`) lists absolute paths of existing directories outside the run
directory that the Codex author and the Codex reviewer-type roles may also write (a build cache such as `~/.cargo`,
written as an absolute path); a relative, missing or non-directory path, or one inside the run directory, is refused at
start. It is recorded in the run config, and resume uses the recorded list.
`--lifecycle-mode off` is the supported stop-after-gate route: PLAN -> EXEC -> GATE -> DONE (acceptance pending). The operator then handles FINISH, POLISH-Q, docs, security review and merging. Accept never commits on this route: it hands back the uncommitted tree. New runs default to `on` (the full lifecycle); CLI, operator/workspace profiles and Python entry points may select `off`, and saved off runs resume, accept, reject, note and abort normally.
The paired-session skill always passes `--lifecycle-mode on` for review-pr report mode, overriding a profile's off value. Other entries follow the profile, or default to on; a user may select `--lifecycle-mode off` for those entries. The frozen config also records exact
`docs_file`/`docs_allowlist` paths and `skip_quality_polish` (`skip_globs` / `--skip-globs` was removed
in 3.0: a profile that still sets it is refused as an unsupported key);
outside-workspace or wildcard doc paths are refused. The real lifecycle is the
worktree lifecycle of `docs/e2e-6-worktree-lifecycle.md` (D12, ADR-11):
`lifecycle_mode=on` from the command line or an
operator `--config` starts a worktree-lifecycle run that runs PLAN and EXEC and
then runs FINISH (a fresh author turn; a change reopens EXEC review)
and POLISH-Q (fresh report-only specialists, legacy Step 3.5; since v3.17.0 a
delivery run only reports their findings, see [After the gate](#after-the-gate-v3170), while in a
`--review-only` run their blockers go to an author fix that the owning specialist
re-reviews before EXEC review runs again) and DOCS (a fresh docs writer; a
write outside the docs allowlist, a protected path included, is rolled back to the pre-DOCS tree, an allowlisted
write gets a fresh docs review that must run the test, and its REVISE goes back to
the docs writer; a docs finding on a path the writer cannot edit goes to the run's `backlog.md`, not to a repair;
the rollback covers only what the docs writer turns themselves wrote (since v3.16.7 also an errored or discarded
writer turn of the same request, rolled back before the re-dispatch when its evidence snapshots show the write is still
there), so an operator edit made at a DOCS HOLD is never rolled back: an edit of a docs allowlist path stays in DOCS and
is docs-reviewed, any other edit, found before a docs writer dispatch or after a recorded one, sends the run back to the
EXEC reviewer in a new epoch, then FINISH..SECURITY again; since v3.16.6 this includes a protected-path write the docs
writer left at an earlier DOCS HOLD (`CLAUDE.md`, say) together with any operator edit, which before v3.16.6 made the
resume refuse; since v3.17.0 a docs writer is dispatched only while the DOCS stage cap has room for it and its review;
once the cap is spent no docs writer runs, but one docs review of a tree no docs review has seen runs beyond the cap
(once per tree, after an invocation-limit check): an operator-changed tree, or the output of a docs writer that a
v3.16.x run recorded on the last DOCS slot; otherwise DOCS moves on to SECURITY with the docs findings still open, with
no DOCS budget HOLD)
and the SECURITY scans
(`sensitive_policy` paths and `scripts/security_preflight.py`) with a fresh
security reviewer, then reaches DONE (acceptance pending).
SECURITY stops the run for sensitive-looking paths or content hits in files the run added or changed, including
additions left in the index after a rename or deletion, for added or changed files over 8 MiB, for content scans over
256 MiB, for a `.gitignore` coverage gap, and for any blocking security reviewer finding (since v3.17.0 the run repairs
those in the [narrow SECURITY loop](#after-the-gate-v3170)). The same hits in files that were already
committed and left unchanged are printed once as a NOTE and recorded in the SECURITY receipt; a path the run removed
and that is still in the index is also named in a NOTE. A file name with a TAB or a backslash is classified (a backslash
counts as a path separator); a name the path scan still cannot classify, such as one with a line break, stops the run
with its exits: rename or remove the file and resume, or abort. Such a file over 8 MiB is not content-scanned and is named in the NOTE.
A file that passes through a Git filter, for example Git LFS, or an end-of-line conversion counts as changed.
DOCS records supported changes and observed results. Final review, security and commit facts belong to the delivery report.
The DOCS reviewer assigns at least MAJOR to false or unsupported DOCS-stage statements.
Nonexistent test, check or result claims by any author are also at least MAJOR.
Other pre-existing inaccuracies use normal severity judgment. `accept --expect` on a W DONE accepts it without touching refs or
the index, or with an operator `auto_commit: true` makes one hook-free local
commit of exactly the accepted tree (never a push; `auto_commit` defaults to
true for a `--review-only` W run and to false otherwise, and an explicit CLI or
operator-profile value wins), and writes a Chinese
delivery report; `reject` reopens EXEC; a
workspace profile cannot set lifecycle_mode on or the docs/skip/polish keys.
`--adversarial-gate off`, `--override-rejection`, `--accept-unverified-claude-author` and `--accept-probe-skip` are
refused (`--accept-unverified-codex-cli` prints a NOTE and is ignored); use
the enabled gate, and `resume --add-rounds N` or `note --scope-change`. Legacy
DONE/ACCEPTED or fake-format lifecycle states, gate-off and `resume --polish`
cannot enter it.

The advisory POLISH round was removed in 3.0.0. The hidden
`--polish-round on|off` CLI/profile option is deprecated and ignored; it no
longer enables a POLISH round.
Lifecycle POLISH-Q remains available.

`--review-focus TEXT` supplies priorities to the reviewer, shadow and gate;
`--quality-focus TEXT` supplies POLISH-Q priorities; `--review-style TEXT`
supplies tone and rules to all review roles. These values are fixed at run start.
`--author-subagents on|off` (default on) controls a Claude author's Agent tool;
read-only roles cannot spawn subagents. With it on, the author also runs with
`--forward-subagent-text`, so `usage.json` counts every subagent model request,
text-only ones included (once per message id). Forwarded rows carry the usage of
their request's start: the subagent's last request takes its output from the
Agent result's usage, and an earlier subagent request keeps its start-snapshot
output (no other source exists). This needs a Claude Code that supports
`--forward-subagent-text` (verified on 2.1.303); an older CLI fails that author
turn with an unknown-option error.
`run --stop-after-plan` HOLDs once after PLAN approval or a successful fast plan, with `RUN_DIR/plan.md`;
a later `resume` enters EXEC. `--review-only` refuses `--stop-after-plan`.

`run --fast-plan` opts a small delivery into first-draft validation. With one to three
literal top-level file items and at least one acceptance criterion in the work item, a valid
draft with a clean applicable history scan enters EXEC directly. Only nonempty file
list items at the smallest indentation count toward the file limit and glob check.
Other drafts go to ordinary PLAN review. The CLI freezes the flag at creation and
refuses it on review-only routes. `status` shows the decision and reason on stderr.

`run --review-only` reviews the existing workspace change against `--base REF`
(default HEAD, an ancestor of HEAD), skips PLAN and may dispatch author fixes.
Add `--review-report` for a report without writers: it ends at REPORTED, writes
`review-report.md`, and refuses accept, reject and note. Report-only options are
`--review-pr-pins FILE` (frozen PR metadata), `--no-test-command` (static, untested
approval; conflicts with an explicit test command), and `--aspects LIST`
(comma-separated code,errors,comments,types,tests; default all). See
[report-mode details](docs/review-pr-port.md).

Quality writers (D09, `docs/d09-cap1-writer-passes.md`): after a clean POLISH-Q,
`--quality-writers both|simplify|tests|off` (any profile may set it; frozen;
default `both` for `--review-only`, `off` otherwise) runs a fresh author session
that simplifies the changed code, then one that consolidates the changed tests.
The explicit test command runs first on the reviewed tree (a red baseline skips
both) and again after each change. A kept change (`wrote`) replays EXEC review,
shadow, FINISH and the specialists before DOCS without using an EXEC round
(round rule R5).
An eligible advisory-only replay REVISE keeps the change as `APPROVE_WITH_ADVISORY`;
other non-approvals roll it back (`rolled-back:review`), only the named writer
when files are disjoint (the other is reviewed again). After a whole-group
rollback, new open blockers outside every writer's files stay open and go to
EXEC repair with the usual round-limit HOLD, before the normal chain runs again;
writer-file blockers and advisory rows are withdrawn; operator-withdrawn rows stay withdrawn. A test writer that lowers
the number of test cases is rolled back (`rolled-back:tests`). Other outcomes:
`no-op`, `rolled-back:tests`, `rolled-back:boundary`
(reserved docs or `.review-loop/` config), `rolled-back:hold`, `exhausted`, and
the skips `no-test-command`, `small`, `no-test-file`, `budget`,
`no-green-baseline`. Cost: about +10 invocations for a typical change (two
writer calls and one replay); the state is in `lifecycle.quality_writers`, the
outcome in the delivery report.

Advisory fix round (ADVFIX, owner 2026-10-06): `--advisory-fix-round true` (default false, frozen; refused
with `--review-report` and, WI-GATE1, without `--review-only`) gives the author one fix round, at the first clean POLISH-Q exit, for the non-blocking
findings still open, before the quality writers: fix what is reasonable,
dismiss the rest with a reason; then EXEC review, shadow, FINISH and the specialists run again. Once per
run, on its own round outside `--max-exec-rounds`; without invocation headroom it is skipped with the reason.
Findings still open afterwards stay advisory. The delivery report says ran (N of M addressed), skipped or off.

```sh
bin/paired-session run \
  --workspace /path/to/disposable-worktree \
  --workitem /path/to/WORKITEM.md \
  --run-dir /path/to/worktree-run-id \
  --test-command 'python3 -m unittest' --lifecycle-mode on
```

Efficient runs record CLI release updates in
`state.json` under `program_updates` and re-freeze `operator_programs`: Claude must
advance to a newer version under the same `versions` directory; an in-place Codex
replacement must report a parseable release version (its old version is unavailable).
Any other binary or PATH change is recorded under `program_refreezes` with one NOTE, and the run continues
(v3.7.0; earlier releases held and asked for `permission-probe`). A run started by a review-loop version with a
different bundled gate prompt continues with this version's gate prompt after one NOTE (`gate_prompt_refreezes`;
for a lifecycle run only the gate-prompt fields of the frozen role dispatch manifest change). With lifecycle off, a
changed reviewer-commands block of the work item is recorded with a NOTE (`reviewer_command_refreezes`); with
lifecycle on it still holds until the block is restored.
The Codex capability guard scans local user, project, system and file-managed
config, macOS `com.openai.codex` MDM preferences, and MCP/app bundles in the
active CODEX_HOME plugin cache. Every Codex dispatch (author, reviewer, gate)
passes `-c features.plugins=false`, which makes cached bundles inert
(empirical evidence: `.compass/results/2026-10-01_cg-codex-plugin-evidence.md`;
`apps = false` or `remote_plugin = false` alone is not relied on). The argv is
bound into the reviewer and gate flags digests. Since rel210-fixCG the
coordinator's guard therefore records cached MCP/app bundles as inert (path and
sha256 in `plugin_bundles_inert`, and in each Codex turn receipt as
`codex_plugin_bundles_inert`) instead of reporting them, so a ChatGPT default
home (`chatgpt-global`, `openai-curated-remote`) runs. A dedicated `CODEX_HOME`
with `[features]` `remote_plugin = false`, `plugins = false`, `apps = false`
remains a stricter option.
MCP servers declared in config.toml are flagged either way. Managed feature
settings and unreadable MDM preferences fail closed.
The scanner runs before each Codex dispatch. This reflects the
[OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins)
and [managed configuration](https://learn.chatgpt.com/docs/enterprise/managed-configuration)
contracts. The scanner reads configuration only and records no raw payload.
What a Codex author cannot do inside this sandbox: see
"Codex author sandbox: what a work item cannot do" below.

Reviewer sandbox and temp dir (FIELD-1, v2.9.6; workspace-write since v3.6.0): a
Codex reviewer, shadow, gate or stage reviewer turn runs with the Codex
author's sandbox shape: `sandbox_mode="workspace-write"` (the workspace it runs
in is writable, `.git` stays read-only), `$TMPDIR` writable
(`exclude_tmpdir_env_var=false`), `/tmp` excluded, network on, plus the run's
`extra_writable_roots`, with `--ignore-rules`, `features.plugins=false`,
`features.hooks=false` and, on Windows, the elevated sandbox. A configured test
may therefore write build output and caches. The post-turn guard still undoes a
change to HEAD, the branch, the index, a tracked file or a non-ignored untracked
file and re-dispatches once after the restore verifies. Ignored paths stay free (recorded in the receipt
as `readonly_ignored_kept`; since v3.7.2 for Claude reviewer-type roles too, so a turn that only changed a git-ignored
path, such as a test's temp cluster dir, stands). TMPDIR/TMP/TEMP are set to a fresh 0700
`<scratch_root>/role-tmp/<seq>-<role>` outside any repo for that one dispatch
(legacy runs without `scratch_root` retain `<run_dir>/role-tmp/<seq>-<role>`), so a `tmp_path`-style
test has a usable temp dir. The coordinator lists that root in the turn's
receipt and removes it after the turn (also after a failed turn; an uncertain
turn's root when it is archived or at the next dispatch). Removal first stops what is
left of the turn's CLI process group (a descendant that left the group is not
covered; if the group cannot be confirmed gone the scratch is kept, and a
successful turn then HOLDs; a kept leftover is swept at the next dispatch only on an explicit ESRCH for its turn's group (probed, never signalled, since its pid may have been reused) or a recorded never-started turn; no recorded pid, EPERM or any other error keeps it and that dispatch HOLDs naming it), then works through directory handles, resetting
modes and file flags through open file descriptors, and never follows a link
even if a process survives: `role-tmp` must be a real 0700 directory owned by this user, a link or
any other leftover entry there is refused, and inside a scratch a link is
removed as a link while modes and file flags are reset without following one.
A regular file with more than one link, an unreadable directory, or a changed
mode of the scratch root itself, left in the scratch fails the turn. The tests use fake CLIs: they show the
argv, not that `codex exec` honours the sandbox (no permission probe checks that since v3.7.0).

Hard links: the scratch shares a volume with the run dir and usually with the
user's home. If the CLI let a role hard-link a same-user file into its scratch,
it could write through it to any such file it can read; the CLI's sandbox is expected to refuse such a link. As
defence in depth for run-dir files, the coordinator records inode and ctime of
every regular file under the run dir before the turn and fails the turn on any
change until the CLI process exits (making a link, writing through it or
changing mode, flags or times all move the ctime, which no process can set
back), so a link made, written through and removed within one turn is caught.
That check does not cover exactly `state.json` and `progress.jsonl` (no other
names), which the
coordinator writes during the turn (`state.json` is replaced atomically from
the in-memory state after the turn; a write through a link survives only a
coordinator crash before that save; `progress.jsonl` is display only), the
turn's own `evidence/<seq>-<phase>-<role>.*` files (the coordinator reads the
answer and observed commands from its `stdout.jsonl`), Codex session rollouts
under `CODEX_HOME`, or any same-volume file outside the run dir, workspace,
context and the watched global config files; for those the CLI sandbox is the only control. A detached descendant acting after the CLI exits is outside
the window, as for the other per-turn checks. Resume turns: a persistent Codex
reviewer's later turns run `codex exec ... -c sandbox_mode="workspace-write" ... resume
<id>`. A plain `sandbox_mode` override applies
on resume (the author uses the same form); if it did not, those turns would fall back to the CLI's default sandbox and only
the workspace snapshot, context digest and run-dir ctime checks would remain. Claude roles keep Claude
Code's own sandbox TMPDIR (`/tmp/claude`, shared per UID, also with a Claude
author): `denyWrite: run_dir` takes precedence over `allowWrite`, so a run-dir
root cannot be writable for them.

Claude roles use one inline strict sandbox settings object, deny secret-like
environment variables and common credential files, allow network access (since
v3.6.0, owner 2026-11-08) but block local network binding, and require the exact Bash allowlist. The run
directory is denied by both the OS sandbox and the Claude permission rules. Claude's session `TMPDIR` is
shared scratch per UID on macOS; paired-session keeps workflow state outside it
and denies writes to the run directory.
The sandboxed Claude CLI creates an empty `<workspace>/.claude/.cc-writes`
directory by itself; the read-only guard admits exactly `.claude` and an
empty `.claude/.cc-writes` (real directories owned by the current uid). Anything
inside, any other `.claude` entry or a link is still an escape. Git-based
workspace guards never see empty directories, so no other check changed.
Read-only Claude roles receive the read tools as one rule and each exact
argument-bearing Bash command as its own `--allowedTools` argument.

Each turn records hashes for the effective `$CODEX_HOME/config.toml` (or
`~/.codex/config.toml`) and the two Claude global files. Installed Codex may
persist a trust entry for a new workspace. Only an exact insertion of
`[projects."<this run's workspace>"] trust_level = "trusted"` at a TOML table
boundary is attributed; the turn receipt warns `global config mutated by codex CLI
trust persistence`. In a linked worktree Codex trusts the main checkout root of the
workspace's own repository instead; that path is accepted too, and nothing else.
Attribution removes exactly one such block per path (plus at most one blank
line) and the remainder must equal the earlier file byte for byte.
The file is **not** byte-identical in that case. Any other
Codex change fails; Claude plugin `lastUpdated` is attributed separately.
FIELD-22: when two runs share one Codex home (the default `~/.codex` included), the other run's own trust block is
also attributed, as `trusted-concurrent-run-workspace` with that run's id. This applies only when all of these hold:
- that workspace's lease in this user's workspace-lease directory is a private record under its own identity key;
- its run dir's `state.json` names the same workspace and is ACTIVE;
- that run's coordinator lock carries the same pid, and that pid is alive.

Any other addition still fails. This is evidence of another coordinator of the same user, not proof against a
same-UID process, which could edit `config.toml` directly anyway. Sandboxed turns can write neither the lease
directory nor the Codex home. A turn held by such a change before this rule recovers
with a plain `resume`, which re-runs the turn on a fresh baseline. `resume --acknowledge-codex-trust` also accepts
exact trust blocks for a workspace that has such a lease record, including one from a run that has since ended.
The coordinator never edits or restores the user's global config. `--skip-probe`
is a hidden no-op since v3.7.0 (kept so older scripts and tests still parse).
With a Codex home other than the default, the run also hashes the default `~/.codex/config.toml`: a Codex child that
ignored `CODEX_HOME` would write there (R20-0a). Another process may change that file meanwhile: a Codex run on the
default home appends its workspace trust entry, or an operator restores the file from a backup. A change that only adds
or removes whole `[projects."<path>"] trust_level = "trusted"` tables, for paths other than this run's own workspace,
clone and linked-worktree main root, is recorded as `foreign-default-home-trust-entry`, with the paths, and never
voids a completed turn (P0, 2026-10-06). An uncertain turn (cut off mid-turn) records only hashes, so such a change
during it still holds, as before. Any other change to the default config still holds, because the run cannot tell
it from its own Codex escaping `CODEX_HOME`. A trust entry for this run's own path there holds too. The config the
run actually uses, `$CODEX_HOME/config.toml`, keeps its check unchanged: its model, sandbox or hooks changing still
holds. A run on the default home is unchanged.
Use a separate, absolute `CODEX_HOME` for every run, acceptance and supervisor runs included
([`docs/concurrent-runs.md`](docs/concurrent-runs.md)). A Codex run on the default home writes trust entries into the
default config, and other runs then have to attribute them.
Each Codex turn also records a before/after Codex-config comparison. A changed
config during an uncertain turn HOLDs before replay. After inspecting the
change, the operator can run `resume --acknowledge-codex-trust RUN_ID`; this
records UID, time and before/after hashes and accepts only an exact new trusted
entry for that turn's workspace. Other changes stay HOLD, and the coordinator
never edits the config.
Each Codex turn compares only Codex global files; each
Claude turn compares only Claude global files. Cross-vendor changes are recorded
in the turn receipt without HOLDing that turn.

If a run is held, inspect `state.json`, `open-findings.md`,
`findings-ledger.md`, and the latest receipts under `evidence/` before resuming.
A reviewer, gate or shadow turn that changes HEAD, the branch, the index, a tracked
file or a non-ignored untracked file is voided. A verified restore allows one automatic
re-dispatch. A second change or a failed restore holds. For Gemini read-only roles, a
detected edit to an existing ignored file voids the verdict and holds, leaving the
edit on disk for manual inspection.
An author turn that changes HEAD or the branch holds.
`--max-rate-limit-wait SECONDS` (`max_rate_limit_wait` in the JSON profile) is
a nonnegative integer fixed at run start and cannot be raised on resume. The
default is 21600 seconds (6 hours), including for older supported runs. Scheduled
waits share this budget across every role and stage. For a usable reset, the
coordinator waits until the reset or for 30 seconds, whichever is longer, without
using a backoff slot. That full wait must fit the remaining budget, including an
exact fit. Only unknown or stale resets use the four backoff slots of 30, 60,
120 and 240 seconds for the pending dispatch. The saved wait preserves the slot
on resume. A later dispatch starts at the first slot and shares the remaining
run-wide wait budget. If the retry would reach or pass the whole-WI deadline,
the coordinator HOLDs immediately on the deadline.
Set the budget to 0 for immediate quota HOLD. A quota HOLD shows the reset hint
when available and explains why automatic retries stopped. After quota clears,
use plain `resume` with the same workspace, work item, run directory, role
settings and test command.

`--timeout` (default 2700 seconds, at most 86400; a value outside 1..86400 is
refused before the run starts) governs every turn except author turns in EXEC,
FINISH and DOCS, including PLAN, persistent/fresh reviewer, shadow and gate
turns, and the coordinator's own test-command runs. The cap is a one-day
sanity bound; it sits above the 14400-second author cap because a general
timeout above 14400 is a supported setting (the author default then stays at
14400). Author turns in EXEC, FINISH and DOCS use the separate
`--exec-turn-timeout`; when omitted, it defaults to `max(7200, --timeout)`
capped at 14400 seconds. On resume, only an explicit CLI `--exec-turn-timeout`
may raise the saved author timeout, never lower it; project-config defaults do
not count as an explicit raise. The value is saved for later turns and
resumes. An already-running turn keeps the timeout it received when it
started. The existing `resume --resume-timeout N` option raises the general
per-turn timeout up to 86400 seconds for phases that use `--timeout`.
An EXEC, FINISH or DOCS author timeout reports the selected seconds and suggests
`resume --exec-turn-timeout 14400` when the saved timeout is below the cap.
At 14400 seconds, the HOLD identifies it as the maximum author timeout.

The saved invocation base defaults to 25. Automatic delivery transitions record fixed additional
allowances in state for the extra PLAN rounds, the reduced author and review, and the scheduled work
for EXEC rounds four and five. Those allowances survive resume and the raw charged invocation count
still includes the automatic calls.

An explicit CLI `resume --max-invocations N` may raise the saved invocation cap;
a lower value is refused and the same value is accepted. Profile/project defaults
do not count as an explicit raise. The new cap is saved for later turns and resumes;
raises are recorded in state `invocation_cap_raises` and progress events. The host
may raise this cap only when the user asks. Other actions keep the saved cap frozen.
Raising the cap does not lift a report-mode `report budget` HOLD; report runs
have a separate budget.

`--wi-deadline SECONDS` (off by default) bounds the whole work item in wall-clock
time, counted from the run's start; HOLDs, operator waits and coordinator
restarts all count. It is checked before every dispatch: once the deadline has
passed, the run HOLDs instead of starting the next turn. A running turn is never
cut short and keeps its own timeout. The value is fixed at `run`: `resume`
keeps the saved deadline and refuses a different one, and the operator actions
keep the saved deadline. The deadline only blocks new dispatches: once it has
passed, `resume` is refused without dispatching or
touching the HOLD, so every HOLD keeps its reason and its exits
(`note --scope-change`, `abort`). The one exception is an uncertain in-flight
turn: `resume` records it, and `resume --retry-uncertain` still verifies its
process group is gone and archives it, then HOLDs with the deadline reason
instead of dispatching, so `note --scope-change` works afterwards. A wall clock
that moved back (below) is treated the same way. A `DONE` run past
its deadline stays acceptable: `accept` and `reject --scope-change` work, while
`reject` is refused because its next dispatch could only HOLD. If the wall clock moves back by more than 60 seconds
since the last dispatch, the run HOLDs until the clock is past that time again;
elapsed time is never refunded. A scope-change successor starts without a
deadline; pass the time it may use as its own `--wi-deadline`.

## After the gate (v3.17.0)

In a lifecycle-on run the stages after the gate report instead of reopening EXEC (owner decision D-POSTGATE, 2026-11-25).

**Post-gate classes.** Every open POLISH-Q specialist row and DOCS `docs-reviewer` row is classified on each read; the
ledger is never rewritten, so a saved state follows the rule on resume:
- `security`: it blocks by severity and category and is flagged `security`, categorized `security` or of SECURITY
  severity. The SECURITY reviewer owns it (its prompt marks it "forwarded from POLISH-Q …"), may mark it fixed or
  withdrawn, and only SECURITY and DONE block on it.
- `decide`: it blocks and its category is correctness, data-loss or money, or it has no category. It never blocks; it is
  listed for the owner's decision.
- `report`: any other row (a non-blocking severity or category, or a row outside its pass's delta). It goes to the backlog.

In a `--review-only` run (and in report mode) POLISH-Q rows are not classified: a specialist blocker still takes the fix
leg (author fix, owner recheck, EXEC review). DOCS rows are classified in both kinds of run.

**POLISH-Q** in a delivery run completes READY with `report_only` and `to_security` (the row ids) and goes on to DOCS; a
NOTE names the rows. The opt-in quality writers and the advisory fix round are unchanged.

**DOCS** never HOLDs on docs findings or on its budget. A docs writer is dispatched only while the DOCS cap has room for
it and its review. A docs-review REVISE goes back to the docs writer while the budget lasts. Once the budget is spent, the
one docs review past the cap described above still runs; otherwise DOCS completes READY with route SECURITY,
`budget_spent: true` and `leftover` (the open docs rows). The rollback, protected-path and git-guard HOLDs and a docs
writer's HOLD answer stay HOLDs, and an operator code edit at a DOCS stop still goes to the EXEC reviewer.

**Owner decision.** The `decide` rows (risk-first), a spent DOCS budget with its leftover rows, and every docs touch-up no
docs reviewer saw are listed under `## 不阻塞、需要你决定`: at DONE in `open-findings.md` (with a note: `reject --text`
fixes them now, `accept` leaves them to the backlog) and in a DONE NOTE, after accept at the top of `delivery-report.md`,
right after 结论. Their run-backlog entries are P1 (`[P1]` in `backlog.md`). At accept, BACKLOG.md gets only the rows
still open in the ledger: the P1 rows as `[P1 needs owner decision]` under `## P1`, the others under `## P2`; a missing
section is created before `## Done` (at the end when there is none).

**The narrow SECURITY loop.**
- Trigger: a SECURITY HOLD whose only reason is the security reviewer's blocking findings (its own and forwarded rows) on
  an unchanged tree. The SECURITY receipt gets route `SECURITY-FIX`, and the stage becomes EXEC in a new epoch.
- The persistent author fixes the delivered findings (`next: security-fix`) without using an EXEC round.
- The persistent EXEC reviewer, with no shadow, reviews only the fix delta, `context/delta-since-security-hold.patch` (from
  a workspace mirror of the held tree to the current tree; without a mirror its prompt lists the changed paths). Only a
  finding whose reachability starts with `regression:` blocks; the others go to the backlog. The security findings stay
  with the security reviewer. The gate verdict stands; there is no second gate.
- FINISH runs unchanged. A FINISH write goes back to the delta review; the delta still starts at the held tree.
- A docs touch-up: one docs writer updates only the docs that describe behavior the fix changed, and no docs review runs.
  Its writes outside the docs allowlist or to protected paths are rolled back. It is skipped (receipt `skipped`) when no
  docs path is configured, nothing changed since the hold, only docs and tests changed, the DOCS cap has no room, or fewer
  than two invocations are left. Its receipt is stage DOCS, `touchup: true`, route SECURITY.
- SECURITY runs again; READY reaches DONE, and a new findings HOLD starts the next loop.
- Cost: 5 calls with an author fix (4 without a docs path); 4 with an operator fix (3 when the touch-up is skipped).
- Limits: the author fixes at most `SECURITY_FIX_LOOPS` (2) times per run, scaled by counted rejections like the stage
  caps, and only while one more SECURITY review fits `BUDGET_CAPS['SECURITY']` (3 reviews per run) and the invocations
  cover the loop (4, or 5 with a docs path). Otherwise the HOLD names the reason and ends with "fix them outside the run
  and resume (narrow security loop), or abort", or with "the SECURITY budget is spent (…): abort".
- With the default caps the loop cap (2) and the SECURITY budget (3) run out together: after two failed author loops, the
  third SECURITY failure HOLDs with the SECURITY budget spent, and the only exit is `abort`. A cap HOLD with budget left
  happens only when a loop ended without a SECURITY review (for example an unchanged-tree pass).
- The operator variant: fix-and-resume applies to a scan HOLD (sensitive paths, preflight, `.gitignore`), an author HOLD
  answer, an invocation shortage, and a cap HOLD that still has SECURITY budget. Every SECURITY HOLD on an unchanged tree
  records `lifecycle.security_hold` (tree, mirror, manifest). A resume on a changed tree enters the loop `by: operator`
  and goes straight to the EXEC delta review, then FINISH, the touch-up and SECURITY. When no SECURITY review is left for
  the recheck, that resume HOLDs at once ("the operator's change cannot be rechecked: the SECURITY budget is spent (…):
  abort") and spends no calls; so does a migrated v3.16.x operator route. A resume on the held tree starts the author
  loop when the limits allow it, and otherwise refuses ("security findings need a fix on a new tree").
- After an author HOLD where the operator changes nothing, `resume` runs the delta reviewer and FINISH, then starts the
  next author loop on the same tree; it counts toward the cap.
- Records: `lifecycle.security_fix` = `{loop, by, hold_request_id, hold_tree, hold_manifest, mirror, finding_ids}`; the
  next SECURITY moves it to `security_fix_history` with the outcome `passed`, `held`, `unchanged` or `superseded`. The
  delivery report adds a "SECURITY 窄修复环" line.

**Resuming a v3.16.x run.** `resume` migrates the saved state once and records it in `post_gate_migration`; receipts,
turns, ledger rows, verdicts and budgets are not rewritten:
- A delivery run at a POLISH-Q fix HOLD or recheck: on the tree POLISH-Q reviewed it goes on to DOCS with no author turn;
  on a tree a fix changed, the EXEC reviewer reviews the change in a new epoch. A review-only run keeps its fix leg.
- A DOCS HOLD, the budget HOLD included: nothing changes; the next DOCS turn moves on to SECURITY.
- A SECURITY HOLD on findings: on the same tree, author loop 1 (the mirror is taken at resume); on a changed tree, the
  operator loop.
- EXEC reopened by the v3.16.x SECURITY operator route: an operator loop whose held tree is that SECURITY receipt's
  candidate (no mirror; the reviewer gets the changed paths).
- A migration that fails HOLDs and leaves the state unchanged.

## Changing role settings or moving an install

A continuing `resume` can change any of `--author-model`, `--reviewer-model`, `--gate-model`,
`--author-effort`, `--reviewer-effort`, and `--gate-effort`. Both `--flag value` and
`--flag=value` work. Omitted model and effort flags inherit the saved values on every action
that restores the run, even when the current profile supplies different defaults. The saved
vendors and `allowed_models` policy apply. Other actions that restore a run refuse explicit model or
vendor changes; an explicit effort change is ignored by `accept`, `reject`, `note`, `withdraw-findings` and
`attach-verification` and refused by `abort`, a restoring `run` and `resume --polish`.
A NOTE lists the explicit differing effort flags ignored by these operator actions. An ordinary
`resume` that exits before applying requested model or effort changes also lists those flags in
a NOTE. For `--intent-only`, NOTES go to stderr and stdout carries intent JSON. Explicit flags win on
every continuing `resume`: a caller that re-sends its original role flags undoes an earlier override (with a
NOTE), and an author or reviewer model revert opens another session.

A model change starts a new persistent session for the affected author or reviewer. An
effort-only change keeps the session. A replacement author receives the saved approved plan
in EXEC or the current draft during a PLAN revision. Changes take effect when the run continues
and appear in `role_resume_changes` in `state.json`. A saved gate verdict is used once per work item.

After moving a review-loop install, the next verified lifecycle dispatch can update the saved
install paths and retain the sessions if the agent bodies, gate prompt and other dispatch inputs
match. The coordinator records the move in `role_manifest_rebinds` and prints a NOTE. A custom
gate prompt must stay at its saved path with identical contents. The bundled gate prompt keeps
the existing cross-release refreeze behavior.

## Skipping quality polish on resume

Under quota pressure, skip new POLISH-Q specialist passes before dropping the configured fresh shadow.
On a continuing lifecycle-on delivery run, pass `resume --skip-quality-polish true` alongside the usual
workspace, work item, and run directory options. The first change saves the flag and emits a NOTE.
Later resumes inherit it, repeating `true` is idempotent, and explicit `false` is refused.

Completed specialist turns stay in the pass receipt. A saved quota retry for a new specialist pass is cleared,
and its rejected turn and usage remain recorded. POLISH-Q quality writers are skipped if their batch is still
unscheduled. An already scheduled batch follows its saved queue. The flag is saved only by a resume that
continues the run: if that resume stops first (uncertain-turn recovery HOLD, a round-limit HOLD without
`--add-rounds`, a past deadline, or a DONE run), the coordinator prints
`NOTE: --skip-quality-polish true was not saved.` Pass the flag again on the
resume that continues. A profile `skip_quality_polish` value never downgrades a saved run.

A delivery run's specialist findings never block (v3.17.0). In a review-only run, existing blockers still go through
author fix and owner recheck, with one or two calls per owner.
An eligible blocker can also be withdrawn at the existing POLISH-Q fix HOLD.

## Recovering from a POLISH-Q specialist budget HOLD

Since v3.17.0 a delivery run's specialist findings never block, so this recovery is for review-only runs; a delivery
run that v3.16.x held in the POLISH-Q fix leg resumes under the [post-gate rule](#after-the-gate-v3170).

When a blocking finding's owner has exhausted its per-epoch POLISH-Q budget, apply the fix in the
workspace and use plain `resume` on v3.16.2 or later with the original workspace, work item, and run directory.
This also works for runs saved on v3.15.6. You can attach host verification for the edited tree before resuming.
The run-wide POLISH-Q allowance must have room for two calls per owner. The owners recheck the edit
directly. If they clear the blockers, the EXEC reviewer and configured shadow replay with the saved gate
verdict, followed by the rest of the lifecycle. If blockers remain, the run HOLDs on the rechecked tree.
The coordinator detects this post-recheck state as `operator_hold`.
The recheck used one call of the owners' reset budget, so a second operator edit after that HOLD is not
rechecked directly: the author gets a fix turn on the edited tree, then the owners recheck it. The owners
still have to close the blockers.
Resuming on the same tree dispatches nothing at either HOLD, including after attaching verification alone.
Blocking security findings still require their owner's verification and cannot be withdrawn.

The recheck and replay may need a higher `--max-invocations`; a recheck that does not fit HOLDs before
dispatch. If the EXEC replay returns REVISE on a run that is already at or past its EXEC round limit, it
enters a round-limit HOLD. Use
`resume --add-rounds N` to continue from that HOLD.

## Acceptance, rejection and round limits

Automatic continuation and splitting default to on for newly created delivery runs in both lifecycle modes.
Pass `--auto-split-rounds false` or set `"auto_split_rounds": false` in the JSON profile for the legacy rules.
Resume uses the frozen value and refuses an explicit conflicting value. Runs saved without the key and
review-only, code-quality-loop and report routes retain their original round rules.

PLAN uses the configured cap plus granted rounds and up to two automatic repair rounds for transferable
blockers. Two consecutive reviews with new blocking IDs cut at the first eligible boundary. With the default
cap three, new blockers in rounds two and three cut immediately after round three. Otherwise the default
automatic limit is round five. At the cut, the author proposes an independently implementable retained plan
and scope, plus a deferred follow-up. One reduced PLAN review approves the retained scope or leaves a core
HOLD. Advisory-only revisions can approve. A blocked core HOLD gives the finding IDs and reason. Plain resume
keeps the HOLD and an ordinary note is saved without dispatch. `--add-rounds` is available at ordinary
round-limit HOLDs. At the core HOLD, `note --scope-change` starts a successor run using the printed command.
A scope-change successor must `abort` and start a new run.

Initial delivery EXEC uses a fixed round-five boundary while the rule is enabled, regardless of
`--max-exec-rounds`. Earlier convergence advances as usual. Generic R4b/R4c streaks allow continuation
with eligible blockers. The once-only gate can run on round five, or its scoped repair can continue through
that round. Unresolved reviewer blockers and a valid gate FAIL can ship at round five when tree, test,
independence, shadow and lifecycle checks pass. The gate FAIL receipt and exact unmet criteria stay in the
audit and follow-up. Lifecycle reviews, including EXEC re-reviews on writer replay and after rejection, treat
the transferred scope as out of scope and still review regressions within retained scope.
A round-five shadow HOLD stops the run, also after an approval; resume dispatches the reviewer and shadow again.
When the remaining invocations cannot cover both, the HOLD reason names the `resume --max-invocations` value.

Each phase that splits records one follow-up in the run backlog and the delivery report using
`auto-split by rule`, the target and the transferred IDs. The report and the backlog entries list data loss,
security and money findings first. Explicit acceptance carries the follow-up into the repository backlog.

At a current PLAN or EXEC round-limit HOLD, `resume --add-rounds N` (1-10)
continues the same run with N more rounds of that phase. The saved cap stays;
`round_extensions` records the addition. A review/gate HOLD resumes with the
next author turn. For a saved legacy HOLD after a lifecycle write or writer rollback, the pending review is preserved.
The gate runs once per work item (WI-GATE1) and returns PASS or FAIL with per-criterion results and evidence.
If an EXEC approval is stale on resume, the run routes to the persistent reviewer for the current tree in
the same resume. Configured shadow review follows the usual rules, and a completed gate verdict still applies.
A repeat stale approval on the same tree HOLDs with `stale EXEC approval`.
The four required parts of a FAIL item are the quoted unmet acceptance criterion or blocking class, evidence,
the smallest change, and proof it is fixed. PASS requires every listed criterion met and an empty FAIL list.
The EXEC reviewer checks each open FAIL item in full every round and may close it only as fixed.
A saved verdict also counts when an earlier version reset `gate_ran`. Under the legacy round rules, a FAIL gets one automatic fix round and at most one more,
wherever the EXEC cap stood (round rule R2); a FAIL item still open after them HOLDs and recommends splitting the open
gate items into a new work item. From the FAIL until a repair review passes, only the open FAIL items and blocking
regressions introduced by the fix (the reviewer starts their reachability with `regression:`) keep a review from
passing; other new findings go to the BACKLOG. A shadow blocker in that scope waits for the persistent reviewer, who
confirms a blocking regression with `still_open`, marks a valid finding outside the fix as `non_blocking`, or
withdraws an incorrect finding. Use `non_blocking` only for a row marked "shadow finding to confirm" and use
`still_open` for other rows that remain open. The coordinator records an inapplicable `non_blocking` as `still_open`
and prints a NOTE listing the affected IDs. The confirmation uses the same tree and no author round. The automatic hard cap is
5 EXEC rounds whatever `--max-exec-rounds` says. Explicit extensions, counted rejections and the opt-in advisory fix
round add their own allowance. Later FINISH and writer re-reviews use the ordinary threshold. Outside the initial automatic route, R4 asks the owner when
the same non-gate blocker is marked `still_open` in two consecutive reviews, and recommends a split when two consecutive
reviews after an approval each add new blockers. `resume --add-rounds N` resets both streaks. R5 excludes lifecycle
writes, writer replay and rollback, CLI failures and timeouts, and an approval held for host test evidence. A
state saved by v3.8.x is recounted on resume (stage writes and POLISH-Q fixes uncharged, a saved FAIL gets its 1 + 1).
A gate answer that is not a verdict (a reply that is not JSON, a FAIL item without its basis, evidence, smallest change
or proof, an unmet criterion without its own item, or missing evidence claims) is dispatched once more, one retry for
all of these, then HOLDs (`malformed gate verdict`); resume starts a new attempt of the same bound and keeps the earlier
receipts.
At a round-limit HOLD, `run` and `resume` print `NEXT: resume --add-rounds N`; after `reject --expect`,
inspect whether `round_limit_hold.hold_reason` equals the current `hold_reason`.
Other exits are `note --scope-change` or `abort`. Ordinary notes cannot extend a cap.

A run ending in `DONE` is awaiting explicit operator acceptance. Acceptance writes the Chinese `delivery-report.md` only at ACCEPTED and
prints `REPORT: <path>`. A local commit prints `COMMIT: ...; not pushed`; a
review-only acceptance leaving changes uncommitted prints `UNCOMMITTED: ...`. Use `accept` to
record acceptance and move it to terminal `ACCEPTED`; repeating `accept` is a
no-op. Use `reject --text` or `reject --file` on a `DONE` run to send in-scope
feedback to one more EXEC author turn. That turn goes through the EXEC review
again (no second gate); every later EXEC review round receives every rejection note. Rejections are saved and limited to two
by default; exhausting the limit puts the run on `HOLD`. At a rejected-tree
rejection-limit HOLD, use `note` and `resume` for a new author ingest,
`reject --scope-change` for a successor, or `abort`; plain resume cannot accept
the rejected tree.
Acceptance requires DONE; at HOLD, use note and resume when an author turn is
available, a scope-change successor, or abort.

Use `withdraw-findings --finding F003 [--finding F007 ...] --reason TEXT` to
record an operator ruling that approved behavior stays, only on the user's
explicit decision naming the findings and reason, never in handsfree mode or
on the host's own judgment. First issue the same
command with `--intent-only`, then submit it with `--expect <digest>`; ids and
reason are bound to the intent and mutation runs under the run/workspace leases.
Allowed states are DONE (acceptance pending), a PLAN/EXEC round-limit HOLD, a
rejected-tree HOLD, and (v3.8.4) a POLISH-Q fix HOLD (since v3.17.0 specialist rows block only in a review-only run):
"POLISH-Q blockers remain after
the fix", "POLISH-Q fix left the tree unchanged" or "POLISH-Q specialist budget
exhausted". Unknown or closed ids, an empty reason, CRITICAL/MAJOR/SECURITY
security-tagged or security-reviewer rows, specialist-owned rows (except at a POLISH-Q
fix HOLD), gate CRITICAL/HIGH rows and `secret-scan` rows are refused. At a POLISH-Q
fix HOLD, withdrawing the last blocking rows sets the run to replay the EXEC review,
then FINISH and POLISH-Q (only the paths changed since the last completed
specialist pass; none when the tree is the one that pass reviewed); the run stays HOLD
until `resume`. A pass the budget stopped mid-way is dropped when its blockers are withdrawn.
Specialists, DOCS reviewers and SECURITY reviewers receive the decision reason. They continue to review the
implementation and unrelated defects, including defects at the same location.
When another specialist's covered report is attached to the same ledger row, the recheck calls the row's owner.
Withdrawing the row closes the blocker and preserves the attached report in its history.
The other specialist can open a new finding by reporting it again when a later delta touches that file.
Example: a specialist reports that the configured test fails in its sandbox (a
database that cannot start there) while an attached host run passed on that tree. Phrase the reason as approved behavior,
without finding ids, review-history phrases or model/vendor approval attributions;
bare model/vendor names are allowed. A refusal names the matched text. For example:

```sh
bin/paired-session withdraw-findings --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --finding F003 --reason 'Expanded export scope is approved and stays.' --intent-only
bin/paired-session withdraw-findings --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --finding F003 --reason 'Expanded export scope is approved and stays.' --expect <digest>
```

Rows become `withdrawn`; `operator_rulings` saves O001, O002, etc., with ids,
operator attribution, reason and intent. Evidence is in
`evidence/operator-ruling-Oxxx.json`; the ledger, open-findings document and
acceptance delivery report include the ruling. Withdrawal changes no tree,
starts no author turn and counts as no rejection. DONE remains acceptance
pending: accept or send back as before. At a round-limit HOLD caused by the
reviewer verdict, plain `resume` already re-dispatches the reviewer on the same
tree, even when other rows remain open or the reviewer had findings of its own.
Apart from the POLISH-Q fix HOLD above, withdrawal adds no continuation route; the existing exits apply. A rejected tree
remains rejected. Rulings survive resume and writer replay rollback, and every later author-side turn (the EXEC author,
FINISH, the POLISH-Q writers and the DOCS writer) is told about them and asked not to reintroduce the change the
withdrawn findings asked for. This command does not reopen lifecycle stages at DONE.

Before accepting or rejecting, request an operator intent for the exact action.
It prints the digest and bound run/item, worktree, DONE-approved snapshot, HEAD,
index, state and rejection text hashes; accept refuses a changed approved tree,
and the mutation rechecks the intent under both leases. `resume --polish` was
removed in 3.0.0; the option remains parsed only to refuse it. Lifecycle EXEC review replay
after writes remains in place.

```sh
bin/paired-session accept --intent-only --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN"
bin/paired-session accept --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --expect <digest>
bin/paired-session reject --intent-only --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --text 'Recheck this detail.'
bin/paired-session reject --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" --text 'Recheck this detail.' --expect <digest>
```

`accept --reason TEXT` records the operator's acceptance reason; the intent
digest covers it, so give the same `--reason` to `accept --intent-only` and to
`accept`. `accept` refuses `--text` and `--file` (they belong to `reject` and
`note`). `accept` also refuses while a CLI turn is active or uncertain: once its process group is gone,
settle it with `resume --retry-uncertain` (a probe turn saved by an earlier release too; a DONE run stays DONE),
or abort.

A run saved by an older build (review-loop 2.13.x or earlier: state `version` 1)
refuses state-changing run commands, `abort` included, and is refused as a `--supersedes` parent, before loading its configuration:
`run was created by an older paired-session build; start a new run, or finish or abort this one with review-loop 2.13.x (for example a pinned copy at ~/paired-runs/review-loop-v2.13.<n>)`.
Such a run is never migrated. `stop` and `snapshot` bypass this version gate.
`status` and `status --brief` read it without modifying its state. Snapshots keep
counting tracked and untracked non-ignored files; stale refusals report both path
counts and tell the operator to restore the approved tree or start a new run.

Operator rejection, including the tree held at the rejection limit, permanently
records that tree's digest. An unchanged author answer enters `HOLD rejected-tree`
before review. Status includes the author's rationale, truncated to 2,000 characters,
and a pointer to that author receipt. The operator may add `note` guidance, change
the workspace and resume a new author ingest, or start a scope-change successor.
After the rejection limit, such an author ingest is reviewed again before DONE, on every route (the gate ran once).
Ordinary accept/reject require `--expect`; acceptance requires DONE.
Retry-uncertain with no receipt follows plain resume; a fresh author ingest is required for rejected trees.

Repeated same-class blocks (FIELD-5, v2.9.7): the EXEC reviewer, shadow and gate
prompts ask for every blocking finding to start with an explicit defect-class
label `[class: kebab-case-name]`; a gate label is kept at the start of the
finding's ledger summary, so the persistent reviewer can reuse it. The
coordinator reads only that label; it never infers a class from the text. A
BLOCK is an EXEC reviewer or gate verdict with new blocking findings. Old
blockers merged into a refused approval, a REVISE without new blockers, a
refused approval and an approval that only routes to the gate neither count nor
break the run; an approval that ends the review (a gate without blockers, or a
reviewer approval with no gate to follow) breaks it. When one class appears in
each of three consecutive BLOCKs, the run HOLDs `structural fix / re-scope
needed: finding class ...` with the finding ids per verdict, after routing the
next turn to the author; the round-limit HOLD keeps precedence (its exits are
`resume --add-rounds N`, `note --scope-change` or `abort`).
The HOLD closes no finding and never accepts. Use `note` with a structural plan
and `resume`, `note --scope-change`, or `abort`; after the HOLD the count starts
afresh, so the same class HOLDs again only after three more BLOCKs. The history
is in `block_class_events` and `structural_holds` (written only when the HOLD
happens) in state.json.
Blocking findings without a label never HOLD; they are counted in
`unlabeled_blocking_findings` (run total, and per reviewer/gate turn receipt) so a
reviewer that never labels is visible.

When a blocking repair needs an in-scope operator ruling, the persistent EXEC
reviewer can return `REVISE_NEEDS_OPERATOR`. The run HOLDs with the question and
the repair ready for the next author turn. Queue your answer with `note --text`
or `note --file`, then `resume`. If a round cap, stop rule or structural HOLD
takes precedence, the terminal shows the question in a separate NOTE and the
existing continuation instructions apply. With sufficient test evidence,
advisory-only findings still take the advisory approval path and the question
goes to the BACKLOG with the advisory.
If the ruling request adds no blocker, the HOLD shows the first open blocker
or directs you to the review receipt.

An idle `HOLD` run accepts an in-scope clarification with `note --text '...'`
or `note --file /path/to/note` when an author turn is available in PLAN or EXEC.
An EXEC HOLD waiting for its reviewer also accepts a note for the next author
turn. A newer note replaces a pending one. Once delivered, the hash-verified
note appears as an operator decision in subsequent reviewer prompts, with its
ID shown only to the persistent reviewer.
Subsequent EXEC, FINISH, DOCS and POLISH-Q writers also receive the decision and implement and document it as decided.
Shadow, gate, POLISH-Q specialists, DOCS review and SECURITY review receive the note with finding IDs such as
`F123` replaced by `[finding ID]`. Describe the approved behavior in words so those reviewers can apply it.
When a new clarification is delivered after an earlier note, the author follows the new clarification on any
conflicting point. Later prompts list delivered notes oldest first. Within approved scope, the later note takes
precedence on each conflicting point, and unrelated points in earlier notes still apply.
Since v3.7.2 a note is also queued at a round-limit HOLD (it reaches the author turn after `resume --add-rounds N`)
and, with lifecycle on, while the run waits for the gate, FINISH, DOCS or a POLISH-Q step: it reaches the next newly
dispatched author-role turn (EXEC author, FINISH writer, DOCS writer or POLISH-Q writer). A recorded writer turn that
resume reuses is not dispatched again and takes no note. `note` prints which turn will receive it. Notes are
refused while a turn is active or uncertain, when the invocation budget is
exhausted, at a PLAN-reviewer wait, at a SECURITY wait, at a gate wait with lifecycle off, and at DONE
(use `reject`). A note file must be outside the workspace and run directory.
The note cannot authorize new scope. For a scope
change, use `note --scope-change --text/--file` on an idle active or ordinary
HOLD run, or `reject --scope-change` before accepting a DONE run (also allowed
after the rejection limit). The old run ends as `ABORTED(scope-change)` and
prints the exact `Start:` command for one successor. Run it. The successor starts a fresh PLAN, review, and configured gate, with
the old workspace edits still present for the PLAN author to keep or revert.
The old run cannot resume or be accepted; a second chained scope change is
refused. A scope-change note requires the named existing run. Author-produced
plan/code remains visible to reviewers, while the operator note itself is not
forwarded to their prompts.

If a note is still queued at DONE, `accept` and plain `resume` leave it undelivered.
An in-scope rejection reopens EXEC, and the next EXEC author receives both the
queued note and the rejection feedback. Preview with
`reject --intent-only --text "<rejection feedback>"`, then use
`reject --expect <digest> --text "<rejection feedback>"` with the same feedback.

When the author's sandbox cannot run a check (for example xcodegen, XCTest or
CoreSimulator under a Claude author), the operator can run it on the host and
attach the result to an idle ACTIVE, HOLD or DONE run (OPV, v2.9.5; at DONE
before `accept`):

```sh
bin/paired-session attach-verification --workspace "$WS" --workitem "$ITEM" --run-dir "$RUN" \
  --command 'xcodebuild test -scheme App' --exit-code 0 --log /tmp/xcodebuild.log \
  --log-sha256 "$(shasum -a 256 /tmp/xcodebuild.log | cut -d' ' -f1)" --note 'ran on the host simulator'
```

The record (command, cwd inside the workspace, exit code, log SHA-256, time, operator UID and a non-empty note)
binds to the current workspace snapshot digest. The log must lie outside the workspace and run directory and
match `--log-sha256`. The coordinator copies it to `evidence/operator-verification-V<n>.log`.

The EXEC reviewer, shadow, gate, POLISH-Q specialists, DOCS reviewer and SECURITY reviewer use the latest
completed tool observation from a validated read-only turn on the same tree for the exact configured test command.
An eligible failure supersedes an earlier observed pass. An intact host log with exit code 0 for that command
and tree can waive the duplicate test and settle a sandbox-only failure. Missing host verification is never
a finding. Each role still reviews independently, and the persistent reviewer lists the commands they ran
in `self_run_evidence`. Receipts cite the source turn sequence in `reused_test_evidence` or the host record ID
in `operator_test_evidence`. Changes to code or tests require proof for the resulting tree. POLISH-Q's coordinator-run
baseline and post-write checks still run as before.

The read-only roles, FINISH, the EXEC author, the DOCS writer, the POLISH-Q simplifier and the POLISH-Q test
consolidator see current operator evidence in their prompts. The block shows the command, cwd, exit code, log hash
and last 2,000 log characters. The operator note stays in the record. The writers see evidence for their live input
tree. FINISH and the EXEC author can use a passing host record to proceed when the configured test fails solely
because of their sandbox. The next review reconciles an open test-failure finding with a newer eligible pass
or a valid host record.

A changed tree or log copy voids a record. The snapshot covers tracked and untracked non-ignored files.
Both the start and end snapshots of an author turn count, including failed turns. An interrupted author turn
without an end snapshot leaves the tree unknown. The persistent reviewer is told when a record they saw was
withdrawn, with its ID and reason. Attaching requires an idle ACTIVE, HOLD or DONE run and a log that passes
the fresh-role input scan.

Allowlisted DOCS writer changes and eligible EXEC author docs-only changes carry a current record to the new tree.
The record includes the history in `carried`, and a DOCS writer receipt cites the carried IDs in `opv_carried`.
`accept` keeps the carry-over history in `acceptance.json`. Its stdout identifies DOCS writer and EXEC author
docs-only carry-over and includes the record ID, command, exit code and log hash. A direct record is shown as
current for the accepted tree. A stale or damaged record is omitted.

Each run records an item UUID. A scope-change successor inherits it and copies
the parent's OPEN blocking findings into its protected successor spec and state,
with their original run/ID provenance. These records do not enter fresh-role prompts.

Per-request provider usage is copied into the active turn receipt as stream
events arrive. If a provider turn fails, times out, or is killed after reporting
usage, the known request totals remain in the usage report; an interrupted
coordinator also retains observed usage when its uncertain turn is archived.

If `state.json` has `uncertain_active`, do not replay automatically.
Inspect the recorded PID and receipts. A stopped turn needs explicit operator
approval for `resume --retry-uncertain` because the model call may be replayed
(an uncertain probe turn saved by an earlier release is archived and replays nothing).
Continue in the existing run directory; do not call `run` again.

```sh
bin/paired-session resume \
  --workspace /path/to/disposable-worktree \
  --workitem /path/to/WORKITEM.md \
  --run-dir /path/to/worktree-run-id \
  --test-command 'python3 -m unittest'
```

`run`, `resume` and `reject` support `--detach`: the caller
returns after launch, and the coordinator continues with a separate log. `stop`
sends SIGTERM to that detached coordinator and stops its active CLI child.
Inspect any interrupted CLI turn before retrying. If stopped during a quota
wait, plain `resume` honors the saved UTC retry time and keeps the original
budget charge. If the quota rejection occurred on a persistent reviewer correction, resume keeps the
original correction and the spent answer attempt through a stopped wait or a rate-limited HOLD.
See [detached commands](docs/detach.md).
Plain `status` reads state under the run lease; while a coordinator owns it,
it prints `HOLD: another coordinator currently owns this run` and exits 2.
Use lease-free `status --brief [N]` (default 20 events) during a run. It shows the
pending quota retry time along with progress events for waits, retries and exhausted budgets.
Both modes show a pending operator note's ID, recipient and creation time. At DONE
and ACCEPTED, the summary says the note remains undelivered. At DONE, it also
explains how to send the note with an in-scope rejection. Plain `status` prints
the summary to stderr and keeps the saved JSON on stdout. Brief
prints the saved summary to stdout even when there are no progress events.
Progress is saved to `RUN_DIR/progress.jsonl`; `--quiet-progress` suppresses
live event lines while keeping that file and the final status line.

Exit codes: `run`, `resume` and `reject` return 0 at DONE, ACCEPTED or REPORTED,
and 2 otherwise. `permission-probe` (removed in v3.7.0) is refused with exit 2.
Completed `accept` calls return 0 even if delivery returns HOLD; inspect the
printed status. Successful operator actions and reads return 0; refusals and
lease/OS errors return 2. `abort` returns 2 after putting a run on HOLD, or 0 for an already
ACCEPTED, CLOSED or REPORTED run. Detached launch success returns 0 before the
run finishes; a stopped detached command exits 130.

`bin/paired-session --help` lists all supported options. `snapshot` is read-only.
Each run directory belongs to one task and must not be shared between tasks.

The workflow still needs the remaining productization and protocol batches
listed in the repository backlog. The legacy implementation was removed in v2.13.0 (routing) and
v2.13.1 (files).

`test_real_coordinator.py` is a deterministic fake-CLI suite. It verifies
protocol transitions and permissions-command construction, not the installed CLIs' sandbox enforcement.

### Codex author sandbox: what a work item cannot do

A Codex author runs under the `workspace-write` sandbox (writes confined to the
workspace and the run-owned temp dir; `network_access` is on since v3.6.0, owner 2026-11-08).

The Codex author's sandbox cannot write `.git` (for example `.git/ORIG_HEAD.lock`),
so the author cannot run `git merge`, `git rebase`, `git commit` or similar commands.
For a merge-resolution work item, the operator starts `git merge --no-commit <branch>`
before the run, leaving conflicts in the worktree; the author only edits files.
Ordinary runs with lifecycle on or off do not refuse `MERGE_HEAD` and can accept
the resulting tree with `--auto-commit false`; acceptance leaves the index and
merge state for the operator to stage and complete the merge afterwards.
In an ordinary lifecycle-on run (lifecycle off never commits), an explicit `--auto-commit true` refuses an index that differs
from HEAD; every auto-commit is a single-parent commit and would drop the merge
parent, so keep auto-commit off for this route. `--review-only` refuses unmerged
index entries: resolve and stage them outside the run first and pass
`--auto-commit false`, because a review-only run defaults to `--auto-commit true`
and would commit the staged merge with a single parent.

Real runs on this machine (poker-tools, 2026-10-01) showed three things a work item
cannot do there:

- Postgres `initdb` fails (`shmget` returns EPERM), so a test database cannot be
  created inside the sandbox.
- Unix-socket connections to a server running outside the sandbox fail; they
  need network access enabled (on since v3.6.0; untested whether they now work under
  the nested sandbox).
- iOS builds and the simulator are unavailable: the Swift macro plugin server
  answers "malformed response" under the nested sandbox, and the connection to
  CoreSimulatorService is refused.

A work item that needs one of these will stall or fail in the author turn. Options for the operator:

1. Pick a Claude author (`--author-vendor claude`) for that item.
2. Split out the step that needs the capability and keep the rest in the work item.
3. Run that step outside paired-session, by hand, and feed the result back as
   ordinary workspace content.

Do not loosen the sandbox to make such an item pass; the safety rows in `docs/1c-safety-controls.md`
describe the sandbox as configured.

### Troubleshooting: a test that starts a database in a Claude role's Bash sandbox (macOS)

On macOS, PostgreSQL `initdb` inside a Claude role's Bash sandbox can create SysV shared memory but cannot remove it, so
each failed start leaks one segment. Once the 32-slot table (`kern.sysv.shmmni`) is full, `initdb` fails host-wide,
also outside the run. Routes:

1. Run the DB-backed test on the host and attach its log with `attach-verification` (see above); a passing record of
   the configured test settles the EXEC, FINISH and DOCS test checks for that exact tree. After an EXEC author HOLD for
   that test, attach the record and `resume`: the author sees it (v3.8.3) and hands the tree back for review.
2. Use a Codex reviewer (`--reviewer-vendor codex`) for that item.

Cleanup: first check that no postgres is running (`pgrep -l postgres`), then list the segments with `ipcs -m` and remove
each one whose NATTCH is 0 with `ipcrm -m <id>`.

### Removed: the fake lifecycle

The offline fake lifecycle (the candidate-tree Q, delivery publication and closeout harness, with its modules `delivery_*`, `closeout_*`, `q_proposal`, `q_evidence`, `candidate_test_sandbox`, `finish_dispatch`, `security_repair_policy`, `hook_inventory_*` and `safe_temp`) was removed in V3-B4 (ADR-17 V4); the worktree lifecycle never used it.
