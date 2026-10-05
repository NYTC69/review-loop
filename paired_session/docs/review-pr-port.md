# review-pr on paired-session (design, lg2)

Design only; no product code. Owner decision D-LG2 (2026-10-05): keep `review-pr` and `code-quality-loop` as capabilities
and migrate them onto paired-session; this document designs `review-pr`.

Owner answers of 2026-10-05 (`DECISIONS.md` ADR-13, D-OWNER-1005):
- D03 authorized design round 4 and answered Q-R1..Q-R10 as recommended (§6).
- D09 = A settles Q6 and Q-R9: code-quality-loop retires onto `run --review-only` + POLISH-Q.
  - Its capability 3 (comment-analyzer and type-design-analyzer) is deferred to this port, as review-pr specialists
    (§2.4).
  - Its capability 1 (the simplifier and test-consolidation writers) is ported separately; that is provisional, with
    the D11 row L117 (decision-sheet numbering at `57cb6cf`; today the "code-simplifier writer" row of
    `docs/paired-session-migration.md`).

Anchors are at `6af06ef` (moved from `03acfb0` for round 4; the cited lines moved, their text did not):
- `C` = `paired_session/coordinator.py`; `WL` = `paired_session/worktree_lifecycle.py`; `RP` = `skills/review-pr/SKILL.md`;
  `PSE` = `docs/protocol/paired-session-entry.md`; `RO` = `paired_session/docs/review-only-entry.md` (the LG1 entry this
  builds on).
- There is no Codex counterpart: `.agents/skills/` has no `review-pr`, so Codex users have no review-pr today.

## 1. What legacy review-pr does (RP)

| # | Behavior | Where |
|---|---|---|
| L1 | Scope: the unstaged `git diff`; if empty, the staged diff; if both are empty, stop. **There is no PR number, branch or base input**: "PR" is only the name. | RP:21-33 |
| L2 | Arguments: aspects `code errors comments types tests simplify all` (default `all`) and the `parallel` keyword. | RP:4, 27-31 |
| L3 | Tier config from `.review-loop/config.md`: `judgment_model`, `cheap_model` (backstop `claude-opus-5-5`) and `review_style`. | RP:45-54, 146-151 |
| L4 | Aspect to agent: code → code-reviewer, errors → silent-failure-hunter, comments → comment-analyzer, types → type-design-analyzer, tests → pr-test-analyzer, simplify → code-simplifier. | RP:58-69 |
| L5 | `all` applicability: code, errors and tests always; comments if comments or docs changed; types if type definitions changed; simplify last. | RP:71-76 |
| L6 | Report-only aspects run as fresh native launchers (`run_claude_reviewer.py`, read-only, `tool_uses` check, one retry). The caller runs any checks and passes the evidence. | RP:80-159 |
| L7 | simplify is a writer (`general-purpose` Agent) that edits the operator's checkout. It runs last and is skipped after any CRITICAL. | RP:161-192 |
| L8 | Sequential (default, findings shown after each agent) or parallel (report-only reviewers at once, simplify after). | RP:194-212 |
| L9 | Output: a chat summary with Critical / Important / Suggestions / Strengths / Recommended Actions (+ Simplifications Applied). No file, no GitHub post, no rounds, no fix loop. | RP:216-249 |

## 2. Target: what the port does

A review-pr request runs the coordinator on a **materialized copy of the PR head**, as a review-only run against the
merge base, in a **report mode**. The report mode never dispatches a writer. The coordinator's reviewer, shadow, gate,
specialists and security reviewer each review the change once. Their findings are collected into one report. Nothing
is committed, pushed or posted.

### 2.1 Inputs

| Input | Meaning | Resolution (by the skill, stage A, before any coordinator command) |
|---|---|---|
| none | Legacy parity (L1): the local change | Not a PR. It routes to LG1: `run --review-only` in report mode on the current worktree (`--base HEAD`), with no materialization. |
| `<number>` / PR URL | A GitHub PR | Read-only `gh pr view <n> [-R owner/repo] --json number,url,headRefOid,baseRefName,baseRefOid,isCrossRepository`, plus `gh repo view <owner/repo> --json url` for the **target (base) repository**. If the installed `gh` lacks `baseRefOid`, use `gh api repos/<owner>/<repo>/pulls/<n>` (`.base.sha`, `.head.sha`). A URL gives `-R` and the number. The run pins the target repository URL, the PR number, `headRefOid` and `baseRefOid`. Other forges are out of scope (Q-R2). |
| `<branch or ref>` | A local or remote branch | A local ref: `git rev-parse --verify <ref>^{commit}` in the operator repository. A remote branch `<remote>/<branch>`: the remote's URL (`git remote get-url <remote>`) and the branch name, fetched into the clone (§2.2). The run pins the head OID. |
| `--base <ref>` | The branch it merges into | Default: the PR's base (`baseRefOid`); for a ref input, the remote default branch. A local ref in a repository with no remote needs `--base`, since no default exists. The base is resolved, pinned and fetched independently of the head. A remote base is fetched from its remote, never read from a possibly stale local branch. |
| aspects | L2 | Mapped to specialists (§2.4); `parallel` is dropped (§5). |

**Pinned, then verified.** The skill resolves every input to OIDs once.
- After the fetch it verifies, in the clone, that the head and base OIDs are the pinned ones. A PR updated in between is
  re-resolved once, then refused.
- The review base is `git merge-base --all <base OID> <head OID>`, computed in the clone, and must give exactly one
  result. It is always an ancestor of the head, as RO §1 requires. More than one merge base (criss-cross history) is
  refused in v1.
- A merge-commit head needs nothing special: `base..head` contains the merged commits, and the diff is against the
  merge base.
- The pinned values go into `WORKITEM.md` and the report (§2.5).

**PR and ref inputs fail closed.** Any stage A failure refuses the request on every entry and never falls back to legacy:
- `gh` missing or not logged in;
- an unresolvable PR or ref;
- an OID that cannot be fetched or does not verify;
- no single merge base;
- a declined or unanswered Q-R10 size question.

No tests is the default (§2.4), not a question: a declined or unanswered offer to confirm a test command, under
handsfree too, leaves the run without tests and never refuses the request.

Legacy review-pr cannot review a PR (L1), so a fallback would silently review, and with `simplify` edit, the operator's
local change instead. Only the no-argument request may fall back on the default entry (as PSE "Entry and failure
handling"). Such a fallback never runs the `simplify` writer unless the user named the `simplify` aspect.

### 2.2 Materializing the PR head

The coordinator reviews a workspace tree (RO §1) and needs a git worktree (C:1810). It has no checkout helper. The
operator's checkout must not change: no branch switch, no new refs, no stash. It may hold unrelated dirty work.

**Decided (Q-R7, owner 2026-10-05): a temporary, self-contained clone, not a worktree.**

Every git command below runs with `GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 -c core.hooksPath=/dev/null`.
That also drops the operator's credential helpers (`gh auth setup-git` in `~/.gitconfig`, osxkeychain in the system
config), so a private repository would fail to clone. The network steps (the clone and the fetches from a remote URL)
therefore add `-c credential.helper= -c 'credential.helper=!gh auth git-credential'` and run with
`GIT_TERMINAL_PROMPT=0`: credentials come from `gh` only, and nothing prompts.
No hook and no filter driver from the operator's configuration runs (for example an LFS smudge with network access).
Attributes in the PR cannot define drivers. LFS pointers stay pointers; their content is not reviewed and the report
says so.
1. Create `<run root>/pr/<UUID>/` outside every product worktree, next to the run directories (PSE "Work item").
2. Run `git clone --no-checkout --reference-if-able <operator repo> --dissociate <target repository URL> <dir>`.
   - The operator's objects only speed up the clone; `--dissociate` copies what the clone needs, so it does not depend
     on alternates.
   - A garbage collection in the operator repository cannot remove objects the review needs.
   - The clone's `origin` is the target repository, not the operator's checkout.
   - For a local-ref input, the target is the base's remote (or, with no remote, the operator repository itself,
     cloned with `--no-local`: a real object copy, no alternates).
3. Fetch the pinned head and base into clone-local refs, each from its own source:
   - the head of a PR: `refs/pull/<n>/head` from the target repository, which includes fork PRs because GitHub serves
     fork heads there;
   - the head of a remote branch: from its remote URL;
   - the head of a **local ref** (which may hold unpushed commits): always from the operator repository, read-only, by
     its full ref name (`git -C <dir> fetch <operator repo> <refs/heads/...>:refs/review/head`, which only reads that
     repository), then checked against the pinned OID. A fetch by bare OID can be refused by the uploading side's
     `uploadpack` settings;
   - the base: from its own source, the same way.

   Store them as `refs/review/head` and `refs/review/base`, then verify both OIDs (§2.1).
4. Run `git -C <dir> checkout --detach refs/review/head`. The tree is then clean: the change is `base..HEAD`, all
   committed. Detached HEAD is supported (C:3179, C:4053).
5. Run with `--workspace <dir>`.

The operator repository is only read: no ref, worktree entry, config or object changes, and no network access through
its remotes.

Why not `git worktree add`: it writes into the operator repository's `.git/worktrees` and counts against the owner's
two-worktrees-per-project rule. A fetch into the operator repository would also create refs there.

**Cleanup and evidence.**
- The run directory holds what the report cites, independent of the clone: `context/delta.patch`, the receipts, the
  ledgers and `review-report.md`.
- `status` and the report of a `REPORTED` run must not need the workspace (an LG2-a test).
- The skill removes the clone only after the report has been shown, the run is terminal and the user agrees, and only
  through the materializer's `--remove`. `--remove` deletes nothing but the directory it created, which it recognizes
  by a marker file holding the run's UUID. If anything is uncertain, it keeps the clone and names it.

### 2.3 The coordinator run

```
run --review-only --base <merge-base OID> --review-report    # new flag, LG2-a
    --lifecycle-mode on --auto-commit false --workspace <clone> ...
```

**Lifecycle choice (the point the dispatch asked for).** A PR review must not commit, push or rewrite the PR. There are
three options:

| Option | Writers | Specialists | Commits | Verdict |
|---|---|---|---|---|
| A. lifecycle off, `--max-exec-rounds 1` | none (REVISE holds at the round limit, RO §1) | none: POLISH-Q exists only with lifecycle on (C:1852-1853, WL:79) | none | loses legacy's specialists (L4) |
| B. lifecycle on, `--auto-commit false` | author fix rounds, FINISH, POLISH-Q fix, DOCS writer in the clone | yes | none: accept changes no ref or index (C:3896-3897; `test_accept_on_a_worktree_done_changes_no_ref_or_index_and_writes_the_delivery_report`) | it fixes the PR instead of reviewing it, and spends author turns on someone else's code |
| **C. report mode (decided, Q-R1)** | none | yes, report-only | none | matches L6/L9; needs new coordinator code |

**Report mode, the behavior** (frozen config key `review_report: true`, saved only for such runs, in the F2
pattern of `review_only`):
- It needs `--review-only`. It refuses `--auto-commit true` and `--stop-after-plan`. `--lifecycle-mode on` stays the
  skill route (PSE), and the adversarial gate stays on.
- **Sequence:**
  1. The EXEC reviewer and the shadow review once (round 1 = the existing change, RO Q9).
  2. The gate runs **regardless of the EXEC verdict**: in report mode a REVISE is a finding, not a fix request.
  3. The POLISH-Q specialists run once in report-only form; their blockers do not trigger a `polish-fix` (C:6052-6055).
  4. The SECURITY preflight and the security reviewer run.
  5. The run reaches a new terminal status, `REPORTED`.
- **Never dispatched:** FINISH (a writer, WL:88), DOCS (a writer), the author and the POLISH round. No state of a
  report run can reach a writer:
  - `reject` and `note` are refused at every point of the run, not only after `REPORTED`;
  - `resume` continues only the report sequence;
  - there is no scope-change successor. Successors exist only through `note --scope-change` and
    `reject --scope-change` (C:1779), which stay refused like every `note`/`reject`. A successor carries an accepted
    or rejected work item into a new scope; a report run has neither acceptance nor a writer, so a changed scope is a
    new review-pr request with fresh pinning. Keeping the refusal total also keeps "no state reaches a writer" a
    single rule with no exception.
- **Findings vs failures.**
  - Into the report:
    - every verdict and finding of a role that completed validly (REVISE, BLOCK, specialist blockers, security
      findings);
    - a SECURITY preflight result (a secret or a sensitive path in the PR is reported as a CRITICAL security
      finding). The security reviewer still runs, because report mode delivers nothing.
  - A HOLD, as today:
    - a Category A violation that cannot be restored, or a second violation;
    - a launcher or CLI failure after its one retry, a zero-tool-use turn after its retry, or an uncertain turn;
    - the invocation budget running out;
    - a preflight capture failure;
    - an independence-check failure (FIELD-11).
  - A HOLD is never turned into `REPORTED`. A report written at a HOLD is marked **incomplete** and lists the roles that
    did not run.
  - Today's gates that stop progression on open blockers (POLISH-Q, SECURITY) do not apply in report mode; a blocker is
    a finding.
- **Turn limits:** `--max-exec-rounds` is irrelevant (no second round). The invocation budget is set from the role
  count: reviewer, shadow, gate, the selected specialists and the security reviewer, plus one retry each. A large
  PR is reviewed through the files and `delta.patch` the roles read, not an inlined diff; Q-R10 sets a size threshold
  that asks the operator first.
- **Output:** `REPORTED` writes `review-report.md` (§2.5), then `findings-ledger.*` and `open-findings.md` as `hold()`
  does today (C:3651-3655).
- **Operator actions:** `accept` and `reject` are refused on a `REPORTED` run (nothing to deliver). `abort` and `status`
  work.
- **Category A:** every role is read-only. The existing void-and-restore of a workspace change applies unchanged
  (efficient-mode §4a).
- **Category B:** no writer exists, so the author sandbox is unused. Roles keep their flags, and those differ by vendor:
  - Claude roles get the credential-path deny (`~/.config/gh`, `~/.ssh`, `~/.npmrc`, `~/.netrc` and others;
    C:2392-2419, C:2404).
  - Codex read-only roles run with `":root"="read"` (C:540): they can read the whole filesystem, credential files and
    other repositories included. With the default roles that is the gate (ADR-10). In efficient mode the evidence
    guard only logs (PSE).
  - A PR is untrusted content. A prompt injection in it can ask a role to quote a file it read into a finding, and the
    report is generated from the findings. A local report is not an outbound channel; posting to GitHub is (§2.5), so
    the post is guarded there.
  - **Residual, stated explicitly:** report mode does not narrow the Codex read root in v1. Whether a Codex
    permissions profile can confine reads to the clone and the run context, and still run its tools, is unverified.
    LG2-c records this residual in the PSE section; narrowing it is a follow-up that needs a verified profile. An
    operator who wants the credential deny on every role can run report mode with Claude read-only roles (operator
    profile).
- **Category C:** unchanged (efficient default, strict opt-in).

### 2.4 Specialists (L4, L5)

| Legacy aspect | Paired-session today | Port |
|---|---|---|
| code | `code-reviewer`, always (WL:13-16 `QUALITY_AGENTS`) | same |
| errors | `silent-failure-hunter`, always | same |
| tests | `pr-test-analyzer`, always | same |
| comments | not wired | add `comment-analyzer`, selected when a changed path is docs-like (`*.md`, `docs/**`) or the diff touches comment lines (Q-R5) |
| types | not wired | add `type-design-analyzer`, selected by type-bearing suffixes (`.ts .tsx .py .rs .go .java .kt .swift`) (Q-R5). This is broader than legacy's "type definitions changed", and on purpose: suffix selection is deterministic and needs no model judgment. |
| simplify | (writer) | dropped from review-pr (§5) |
| (none) | language reviewers by suffix: python, go, rust, frontend-security | kept; an addition over legacy |
| (none) | the EXEC reviewer, shadow, gate and SECURITY reviewer | kept; an addition over legacy |

- Aspect arguments select a subset of the specialists, with the same names as legacy (`code errors comments types
  tests`; `all` is the default). The EXEC reviewer, gate and security reviewer always run.
- `simplify` on the paired route is refused at stage A, with a pointer to `--legacy` (legacy review-pr still has it);
  it is never silently ignored. A LG2-c test covers the refusal.
- **Tests run inside the PR copy.** The coordinator's specialist prompt has the specialist run the test command
  (WL:190) as an exact `Bash(<cmd>)` (efficient-mode §2), so a PR's own test code executes on the operator's machine.
  The sandbox has no network and denies writes outside the clone. Code under review is untrusted, though. This is a
  behavior change: legacy reviewers never ran anything, and the caller supplied the evidence (L6).
  - Decided (Q-R3): a review-pr run executes no test command unless the operator confirms one for this review. That
    holds for any input, not only for `isCrossRepository`, since a ref can name a fork branch too.
  - "No test command" (LG2-b) means:
    - no `Bash(<cmd>)` allow rule for any role (reviewer, shadow, gate, specialists). `reviewer_commands()` merges
      the test command, `--reviewer-command` and the work item's `reviewer-commands` (C:2131-2149). A report run with
      no test command therefore refuses at creation a `--reviewer-command` or a work item that declares any, and keeps
      the frozen list empty on resume;
    - the EXEC approval evidence gate changes for this case only. Today an EXEC reviewer, shadow or gate approval without
      an observed successful run of the configured test command is refused (C:5451-5463), so a clean PR reviewed with
      no tests would HOLD. In report mode with no test command, an approval is recorded as **static, untested**
      instead. Every other mode, and report mode with a confirmed test command, keeps the gate. This does not weaken
      Category A ("test results never come from a model's claim"): no test result is claimed, and the report says none
      was run;
    - the specialist prompt (WL:190) and the reviewer prompts say that no tests were run and ask the roles to mark
      claims that would need them;
    - the report states "tests not run".
  - `test_command` is already frozen (C:2038, C:8551) and defaults to `npm test` (C:8495). What is missing is a value
    for "none": LG2-b adds `--no-test-command` (report mode only), frozen as `test_command: null`. Every consumer must
    accept null:
    - `reviewer_commands()` (C:2131-2149) and `_saved_configured_commands` (C:1294);
    - the CLI checks in `main`: `resolve_test_executable` (C:8942), `dontask_command_hint` (C:8916) and
      `configured_command_issue` (C:8949);
    - the evidence guard's configured-command list (C:5430) and the EXEC approval evidence gate (C:5451-5463);
    - the specialist and reviewer prompts (WL:190);
    - the strict permission probe. It takes the test command as its allowed command (C:7176, C:7258-7263) and records
      `not-attempted` when it was not run (C:7087-7088). With null, the allowed-command part of the probe prompt and
      that check are skipped; the sandbox-denial attempts stay. So a strict no-test report run can pass the probe,
      while an ordinary strict probe keeps requiring the attempt.
- Tier config (L3, `judgment_model` / `cheap_model`) does not map: models come from the operator profile (ADR-9).
  `review_style` is not mapped (as the migration doc already says for paired-session).

### 2.5 Output

- **`review-report.md`** in the run directory, generated from the findings ledger, not written by a model:
  - one section per ledger severity. The ledger keeps CRITICAL, MAJOR, MINOR and SECURITY, with HIGH/MEDIUM normalized
    to MAJOR and LOW to MINOR (WL:17-18):
    - CRITICAL → Critical;
    - SECURITY → Security, right after Critical;
    - MAJOR → Important;
    - MINOR → Suggestions;
    - nothing is dropped;
  - each finding with its role, `file:line` and verdict context (EXEC REVISE/BLOCK, gate, specialist, security);
  - the pinned OIDs (target repository, head, base tip, merge base) and the PR URL;
  - the roles that ran (with their tool-use counts) and the roles skipped or failed;
  - whether tests ran (approvals recorded as static and untested are marked), LFS pointers left unreviewed,
    "incomplete" if the run ended at a HOLD, and a coverage note for a PR above the Q-R10 threshold. Role tool-use counts
    show what was read; they do not prove that every changed line was reviewed;
  - a fixed "Recommended Actions" list (fix Critical, then Important, then re-run review-pr), as RP:238-242.
- **Legacy L9 parity:** the skill shows the report in the conversation. "Strengths" is kept only where a role reports
  it; it is not invented.
- **GitHub (off by default).** Posting is an explicit operator action. The skill never posts on its own initiative, and
  never under handsfree:
  - on the user's explicit request, it shows the exact command bound to the pinned target,
    `gh pr review <pinned PR URL> --comment --body-file <report>`, together with the target repository, the PR number
    and the head OID the report reviewed;
  - before it asks, it scans the exact body file with the SECURITY preflight's content rules (`CONTENT_RULES` in
    `scripts/security_preflight.py`, through a single-file entry that LG2-b adds; matched values are never printed).
    Any hit refuses the post, and the report stays local with the rule and line named. The rules are patterns: they
    do not catch every secret (for example a password quoted from `~/.netrc`), so the full body below is the human
    check;
  - the second confirmation shows the **full body** that would be posted, not only the command;
  - it runs the command only after that confirmation, and refuses if the PR's head moved since the review (the
    report would describe another commit);
  - never `--approve` or `--request-changes`;
  - no inline comments in v1 (Q-R4).

## 3. Entry and skills

- **`/review-loop:review-pr` (Claude).**
  - With `entry` absent or `paired-session`: runs the port.
  - With `entry: legacy`: runs today's RP unchanged. `/review-loop:legacy` runs the legacy review-loop workflow, not
    review-pr, so a one-off legacy review-pr needs its own control: the argument `--legacy` (Q-R8).
  - This supersedes E-8 for review-pr only (Q-R8). `plan` and `execute` stay as decided.
- **Codex.** A new `.agents/skills/review-pr` (natural-language trigger "review PR 123"), loading the same shared contract.
- **Shared contract.** A new PSE section "Review-PR entry" covers:
  - the input table (§2.1);
  - the clone recipe (§2.2);
  - the flags (§2.3);
  - the posting rule (§2.5);
  - the test-command rule for every review-pr input (§2.4, Q-R3);
  - handsfree: as in PSE, a question nobody can answer (the size threshold) is a failed stage A check. The test
    command is never asked under handsfree: the run has no tests, which is the default. Posting never happens under
    handsfree.
- **Stage A failures for PR and ref inputs refuse** on every entry (§2.1). Only the no-argument request, which is the
  LG1 route, may fall back to legacy on the default entry, and then without `simplify` unless the user named it.

## 4. Batch plan (product lines are estimates)

| Batch | Content | Lines | Tests |
|---|---|---|---|
| LG2-a | Report mode in the coordinator: flag and frozen key, refusals, and every changed transition: the EXEC verdict to the gate, the gate to POLISH-Q whatever the verdict, POLISH-Q with no fix leg and no blocker gate, SECURITY with preflight results as findings, the `REPORTED` terminal; `reject`/`note` refused throughout, `--scope-change` forms included (no successor); resume stays in report mode; `status` without the workspace | ~250 (may split a1/a2) | fake end-to-end REVISE → gate → specialists → security → REPORTED with no author turn; BLOCK; a preflight secret reported; every HOLD class of §2.3 stays a HOLD and marks the report incomplete; refusals, including `note --scope-change` and `reject --scope-change`; resume mismatch; Category A void in report mode; Claude report-mode roles keep the credential deny |
| LG2-b | `review-report.md` from the ledger; specialists `comment-analyzer` and `type-design-analyzer` and the aspect subset; "no test command" for report mode (no allow rule from any of the three sources, the static-untested approval in report mode only, and the null sentinel through every consumer listed in §2.4: the CLI precheck `resolve_test_executable` (C:690, called in `main` at C:8942; the fake harness paths C:7590 and C:7860 are not CLI prechecks), `dontask_command_hint`, `configured_command_issue`, `_saved_configured_commands`, the evidence gate and the strict probe; the pre-post secret scan and the full-body confirmation (§2.5) as a testable `post` step) | ~200 (may split b1/b2) | report content and attribution; aspect selection; with no tests and no findings the run reaches REPORTED with static-untested approvals; no allow rule from any source, still empty after resume; a work-item `reviewer-commands` is refused; ordinary runs keep the evidence gate; through the CLI, a repository with no npm or test script still starts a no-test report run and reaches REPORTED; a strict no-test report run passes a fake probe while an ordinary strict probe still records `not-attempted`; a report with a planted secret refuses the post and stays local; the confirmation text holds the full body; a moved head refuses the post |
| LG2-c | Skills: Claude `review-pr` routing on `entry`, the new Codex skill, the PSE section (with the Codex read-root residual), guide, migration doc, lint needles | ~180 doc lines + lint | lint PASS; `simplify` on the paired route is refused with the `--legacy` pointer |
| LG2-d | The materializer (`scripts/materialize_pr.py`): resolve and pin, clone `--reference-if-able --dissociate` (or `--no-local`), fetch head and base into clone-local refs, verify OIDs, single merge base, detached checkout with no global config, hooks or filters, marker file, `--remove` | ~150 (may split) | local bare "target" and "fork" remotes with `refs/pull/N/head` (no network): PR, fork PR, remote branch, local ref, a local branch with unpushed commits in a repository that has a remote, a local ref with no remote and no `--base` refused, moved head refused, criss-cross refused, the operator repo's refs, config, worktree list and objects unchanged, the clone survives a gc of the operator repo, `--remove` refuses a directory without its marker; a private-remote stand-in (a local HTTP server running `git http-backend` behind Basic auth, with a fake `gh` as the credential helper) clones and fetches through the helper, and fails closed with no prompt when the helper has no credential; a hook and a smudge filter configured in a fake HOME's gitconfig do not run |

- Order: a → b → d → c (skills last, once the CLI exists).
- **Closing check:** one real review of a real PR through the default entry, with no post.

## 5. Legacy features dropped

- **`simplify` (L7):** a writer that edits the operator's checkout during a "review". It is not part of a PR review.
  The writers are ported separately under D09 capability 1 (provisional, with the D11 row L117, sheet numbering).
- **`parallel` (L8):** the coordinator schedules its roles. Specialists run one after another as today (sequential
  per-specialist receipts). A parallel specialist fan-out is a separate performance item.
- **Per-agent tier config (L3)** and **`review_style`**: models come from the operator profile.
- **"Findings shown after each agent" (L8):** the report arrives at REPORTED. Progress lines (`status --brief`) show
  which role is running.
- **The staged-diff fallback (L1):** LG1 reviews the whole non-ignored change against `HEAD`, staged or not, with its
  refusals (partially staged, unmerged). That is stricter, but well defined.
- **Caller-run verification (L6)** becomes role-run tests under Q-R3 (none by default). The coordinator's
  zero-tool-use retry and fail-closed HOLD replace the launcher's `tool_uses` rule (C:6079-6128).

## 6. Owner answers (D-OWNER-1005 D03 and D09, 2026-10-05)

Every question was answered as recommended; D09 = A then superseded Q-R9's "decide later".

| Question | Answer |
|---|---|
| Q-R1 Report mode vs fix mode | Report mode (option C) is the only v1 mode. A fix mode (option B, the fixed tree exported as a patch) may follow later. |
| Q-R2 PR sources | GitHub PRs through read-only `gh` calls, plus any local or remote ref; no other forge in v1. |
| Q-R3 Untrusted test execution | No test command for any review-pr run unless the operator confirms one for this review (a ref can name a fork branch). |
| Q-R4 Posting | Off by default. On explicit request and a second confirmation, one `gh pr review --comment` with the report. Never approve or request changes; no inline comments in v1. |
| Q-R5 comment-analyzer, type-design-analyzer | review-pr only in v1 (report mode), so ordinary runs keep their invocation budget. |
| Q-R6 Report severities | The ledger's CRITICAL/SECURITY/MAJOR/MINOR as Critical/Security/Important/Suggestions; no model-written summary. Keeping HIGH vs MEDIUM in the ledger is a separate item. |
| Q-R7 Materialization | A temporary self-contained clone (`--reference-if-able --dissociate`) under the run root (§2.2). Not `git worktree add`, and not `--shared`. |
| Q-R8 Entry | `/review-loop:review-pr` follows `entry` once LG2-c lands; this supersedes E-8 for review-pr only. Legacy stays reachable through `entry: legacy`, a `--legacy` argument, or the Codex request "use the legacy review-pr workflow". |
| Q-R10 Base snapshot and size | Pin the base tip at resolution and report it; no automatic re-review when the base moves. Above about 100 files or 5,000 changed lines, ask the operator before the run. |
| Q-R9 code-quality-loop | The owner answered "after the first real LG1 run"; D09 = A then resolved it. code-quality-loop retires onto `run --review-only` + POLISH-Q. Capability 3 (the two analyzers) comes into this port as review-pr specialists (Q-R5), and capability 1 (the writers) is ported separately (provisional, L117). Capabilities 2, 4, 5 and 6 are dropped. |
