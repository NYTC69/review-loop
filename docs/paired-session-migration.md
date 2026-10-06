# Migrating from `/review-loop` to paired-session (v2.10.0 default entry)

## What paired-session is
paired-session is a coordinator (`bin/paired-session`) that runs one author and one reviewer through PLAN and EXEC, applies fresh shadow/adversarial checks, and then, with `lifecycle_mode=on`, runs finish, quality polish, docs and security turns before DONE (acceptance pending). It owns isolated run artifacts outside the workspace, invocation limits and a permission probe. See [`paired_session/README.md`](../paired_session/README.md); the entry-switch design is [`paired_session/docs/v2.10-entry-switch.md`](../paired_session/docs/v2.10-entry-switch.md).

## Status in v2.10.0
- **paired-session is the default entry.** A fresh `/review-loop <work item>` (Claude) or a fresh review-loop request (Codex) with no `entry` key in `.review-loop/config.md` hands off to the `paired-session` skill, which runs the coordinator with `--lifecycle-mode on`.
- A request to review code that already exists (code-exists) hands off as `run --review-only` (D-LG1): no PLAN phase; the EXEC review of the change against `HEAD`, or `--base <ref>` when you name a base, is round 1. Unrelated dirty work is not a code-exists signal.
- Plan-exists and existing-session states stay legacy, as do `/review-loop:plan` and `execute`. `/review-loop:review-pr` follows `entry` (see Review-pr below). Legacy `/review-loop:execute --review-only` stays legacy until its retirement; its paired-session equivalent is `run --review-only`, and its `--stop-after exec-round` maps to the operator CLI options `--max-exec-rounds 1 --lifecycle-mode off --adversarial-gate off` (not a skill route).
- Nothing is removed: the legacy workflow stays available through `entry: legacy`, `/review-loop:legacy`, or (Codex) "use the legacy review-loop workflow".
- New runs are `efficient` by default and need no permission probe (see Safety modes). In strict mode the probe PASS is bound to the plugin version: after upgrading, a strict run directory needs a new `permission-probe`. Finish a run with the version that started it. If a v2.9.x run is nevertheless continued under v2.10.0, it resumes strict, needs a new probe, keeps `lifecycle_mode=off` and ends at DONE.

## Deprecation status (v2.12.0)
The legacy workflow is deprecated since v2.12.0: the owner ruled the ADR-6 replacement gate met on field evidence (ADR-6 amendment D-READY, 2026-10-05, in `DECISIONS.md`). Nothing is removed and no routing changes. When you explicitly choose legacy (`entry: legacy`, `/review-loop:legacy`, the Codex "legacy review-loop workflow" request, or `/review-loop:plan` / `execute` invoked on their own), the entry prints one line:

`review-loop: legacy is deprecated since v2.12.0; the default paired-session entry covers fresh work, review of existing changes, review-pr and code-quality-loop; removal is planned after the open legacy-map rows are settled`

The legacy code is removed only after every precondition below holds, so that nothing only legacy can do is lost:

| Removal precondition | Status (2026-10-06) | Rows in the [legacy map](#legacy--paired-session-map-after-v2110) |
|---|---|---|
| `review-pr` ported to paired-session (D-LG2) | ported (LG2-a to LG2-d, entry routing LG2-c); closing check passed 2026-10-06 (PR NYTC69/review-loop#6: one real PR review through the default entry, REPORTED complete, no post) | `/review-loop:review-pr`; comment-analyzer, type-design-analyzer |
| `code-quality-loop` retired onto `run --review-only` + POLISH-Q (Q6) | retired onto the review-only entry (CQL-RETIRE: `/review-loop:code-quality-loop` follows `entry`; `--legacy` keeps the legacy loop until legacy is deleted); capability 1 (the writers) shipped in v2.12.5, capability 3 (the analyzers) went with D-LG2, capabilities 2, 4, 5 and 6 are dropped | `/review-loop:code-quality-loop`; code-simplifier and test-consolidation writers |
| Every "keep (provisional)" row has a paired-session equivalent, or the owner re-confirms it as retire | 13 rows; removal work items below | the "keep (provisional)" rows |
| Every "retire (provisional)" row is re-confirmed | 5 rows | the "retire (provisional)" rows |
| L133 Linux: a real Linux paired-session run, then the host check is opened | not run; the owner's retire answer names it as the condition | `Linux (legacy works today)` |

The owner answered the 18 legacy-map rows on 2026-10-05 (`DECISIONS.md` ADR-13, D-OWNER-1005). Every keep/retire
answer is **provisional**: the owner asked that each be confirmed again when its work item starts. No row is
implemented before that confirmation.

Removal work items (the "keep (provisional)" rows; each needs its paired-session equivalent, or a re-confirmed retire,
before legacy is removed):
1. L75 plan-exists auto-route and L78 `execute --plan`: a paired-session path for an existing plan (E-8).
2. L80 `--stop-after before-polish` / `before-docs` / `before-security`: intermediate stops.
3. L89 the stage A fallback when the key is absent. The owner's keep conflicts with removal itself: the sheet called
   a refusal with new wording mandatory once legacy is gone, and the owner chose "keep legacy". This is resolved at
   re-confirmation, not here.
4. L90 handsfree reviewer decisions (`DECISION:`): a decision path that does not HOLD every author question.
5. L99 `judgment_model`, `cheap_model`: a mapping.
6. L100 `soft_limit_plan`, `soft_limit_exec`: a continue path at the cap.
7. L102 `commit_message_prefix`: a mapping.
8. L108 `cross_vendor_review`: a same-vendor check.
9. L117 the code-simplifier and test-consolidation writers: a port (D09 capability 1).
10. L119 finding dispute / triage: a dispute flow.
11. L120 the Chinese delivery report: brought up to the legacy content.
12. L135 CI and off-macOS tests (see also D08: `tests/` joins CI).

M7, the seeded-defect comparison, no longer gates removal; it stays an optional cost and quality study.

## Status in v2.9.x (for reference)
In v2.9.x the legacy workflow was the default and `entry: paired-session` was an experimental opt-in; without the key `/review-loop` printed a one-line implicit-entry notice.

## Default and opt-out
To keep the legacy workflow, add to `.review-loop/config.md` (the key is documented in `review-loop-config.example.md`):

```
entry: legacy
```

`entry: legacy` is also valid on v2.9.x, so you can set it before upgrading. The key is workspace-committed, so anyone who clones the repository is routed the same way. Other ways to the legacy workflow:
- Claude: `/review-loop:legacy <work item>` runs the legacy workflow and ignores `entry` (it does not read or validate it and prints none of its notices).
- Codex has no slash commands. Ask in natural language: "use the legacy review-loop workflow".
- `/review-loop:plan` and `execute` stay legacy and are not affected by `entry`.
- `/review-loop:review-pr` follows `entry`; `/review-loop:review-pr --legacy` (Claude) or "use the legacy review-pr workflow" (Codex) runs legacy review-pr once.

`entry: paired-session` routes the same way as the missing key but prints the explicit-entry notice; a failed check before the coordinator starts then refuses instead of falling back (unavailable background or outside-sandbox execution is reported as HOLD). The explicit entry `/review-loop:paired-session <work item>` (Claude) remains available and, like the default entry, runs with `--lifecycle-mode on`. `--plan-only` on it maps to `run --stop-after-plan`.

## What is and is not routed
| Situation | Result |
|---|---|
| Fresh work item, `entry` absent or `paired-session` | paired-session |
| `entry: legacy`, an invalid value, or an unreadable config | legacy (the invalid and unreadable cases print a warning; `entry: legacy` prints the deprecation notice) |
| Plan already exists | legacy |
| Code already implemented (task-relevant changes; unrelated dirty work does not count) | paired-session `run --review-only` |
| Existing legacy session / explicit resume | legacy |
| `/review-loop:review-pr` (Claude) or a PR review request (Codex), `entry` absent or `paired-session` | paired-session report mode (Review-pr below) |
| review-pr with `entry: legacy`, `--legacy` or "the legacy review-pr workflow" | legacy review-pr (local diff only) |

Before the handoff the entry checks the host (macOS, key absent only), the CLIs the roles need, background or outside-sandbox execution and the Codex home; the paired-session skill then establishes a dedicated worktree and a test command, asking only when it cannot. With the key absent, a failed check before the first `bin/paired-session` command falls back to legacy with a notice; with `entry: paired-session` it refuses. From the first `bin/paired-session` command on, a refusal or HOLD is reported and never falls back to legacy. Legacy sessions and plans cannot be imported into paired-session. A paired run is resumed only with `paired-session resume` and its original options; the two workflows never cross.

## Notices and warnings you will see
Printed by the `/review-loop` skill text (Claude wording; Codex names "the legacy review-loop workflow" instead of the slash command):
- No `entry` key: `review-loop: paired-session is the default entry; set "entry: legacy" in .review-loop/config.md or use /review-loop:legacy for the legacy workflow`
- `entry: paired-session`: `review-loop: paired-session entry (entry set in .review-loop/config.md)`
- Value other than exactly `legacy` or `paired-session` (quoted or differently cased included): `review-loop: entry "<v>" is not valid (legacy|paired-session); using legacy workflow`
- Duplicate `entry` key or unreadable config: `review-loop: entry could not be read (<reason>); using legacy workflow`
- A plan or a session already exists: `review-loop: entry is paired-session but <plan exists|existing session> detected; using legacy workflow`
- A failed check before the coordinator starts, key absent: `review-loop: paired-session default entry ...; using legacy workflow` (the reason names the host, the missing CLI, the unanswered question or the unavailable execution)
- A failed check before the coordinator starts, `entry: paired-session`: `review-loop: paired-session entry refused (<reason>); set "entry: legacy" or use /review-loop:legacy`
- An explicit legacy choice (v2.12.0): the deprecation notice above (Deprecation status)
- review-pr, no `entry` key: `review-pr: paired-session report mode is the default entry; set "entry: legacy" in .review-loop/config.md or pass --legacy for the legacy review`
- review-pr, `entry: paired-session`: `review-pr: paired-session report mode (entry set in .review-loop/config.md)`
- code-quality-loop, no `entry` key: `code-quality-loop: the paired-session review-only run is the default entry; set "entry: legacy" in .review-loop/config.md or pass --legacy for the legacy loop`
- code-quality-loop, `entry: paired-session`: `code-quality-loop: paired-session review-only run (entry set in .review-loop/config.md)`

## Review-pr
`/review-loop:review-pr` (Claude) and the Codex `review-pr` skill follow `entry` (owner answer Q-R8, which supersedes E-8 for
review-pr only). The paired route is report mode (`run --review-only --review-report`): a PR, a ref or the local change is
reviewed by the EXEC reviewer, the shadow, the gate, the selected specialists and the security stage, and the result is
`review-report.md` in the run directory. No role writes; nothing is fixed, committed or pushed. A PR or ref is reviewed in a
temporary clone under the run root, removed only when you agree. No tests run unless you confirm a test command for the
review. Posting the report as one `gh pr review --comment` is a separate request, after a secret scan and a second
confirmation of the full body. Differences from legacy review-pr: `simplify` (a writer) is refused with a pointer to
`--legacy`; `parallel` does not apply; findings arrive in the report at the end, not after each agent. The contract is the
Review-PR entry in `docs/protocol/paired-session-entry.md`; the design is `paired_session/docs/review-pr-port.md`.

## Author and reviewer roles are inverted
Legacy: Claude executes, Codex reviews. paired-session defaults to the reverse: Codex is the author and Claude is the reviewer. Roles come from an operator-owned profile outside the workspace: the one you name, otherwise `~/.config/review-loop/paired-session.json` when it exists (it then replaces `.review-loop/paired-session.json`); not from `reviewer` or `executor_model`; without one the defaults above apply. `.review-loop/paired-session.json` may hold non-program limits only (role/vendor/program keys there are refused), and `--config` replaces it rather than layering. Models are operator-set (ADR-9); a role without one gets its vendor's default (Claude `claude-opus-5-5`, Codex `gpt-6.1-sol`, ADR-12), and the Step 3.4 gate defaults to the author's vendor (ADR-10), so the default gate is Codex.

## Safety modes
New runs are `efficient` by default ([`paired_session/docs/efficient-mode.md`](../paired_session/docs/efficient-mode.md)): every sandbox and the secret and global-config checks apply, but `run` needs no permission-probe PASS, the `--accept-*` waivers are unnecessary (a NOTE says so) and the evidence guard only logs. `--strict`, or `"safety_mode": "strict"` in the operator profile, restores the probe PASS gate, the Claude-author opt-in and the holding evidence guard, and a strict lifecycle run refuses `--accept-unverified-claude-author` and `--accept-probe-skip` (D-7). The mode is fixed when the run is created; a run started on v2.9.x resumes strict. Two HOLDs apply in both modes: a reviewer, gate or shadow turn that changes the workspace is void (the workspace is restored and the turn re-dispatched once; a second change or a failed restore is a HOLD), and an author turn that changes HEAD or the branch (a commit, reset or checkout) is a HOLD.

## Config mapping
Set legacy keys map to one-run paired-session options: `docs_file` → `--docs-file`, `skip_quality_polish` → `--skip-quality-polish`, `soft_limit_plan` / `soft_limit_exec` → `--max-plan-rounds` / `--max-exec-rounds`; the start line shows the effective values and which came from `.review-loop/config.md`. `auto_commit: true` in `.review-loop/config.md` is not applied (set `auto_commit` in the operator profile; with it, `accept` makes one local commit of the accepted tree) and prints a warning; `reviewer_model` / `executor_model` print a warning when set to anything other than empty or `inherit` (models come from the profile). Other legacy keys (`reviewer`, `review_focus`, `review_style`, `quality_focus`, ...) are not mapped; `handsfree` only means that the paired-session skill cannot ask its stage A questions, and it never runs `accept` or `reject`. paired-session caps are hard (a HOLD), not a prompt; unset defaults are plan 3 / exec 4 rounds (legacy 3/3).

## Legacy → paired-session map (after v2.11.0)
How close legacy is to retirement: every legacy entry, flag, skill, config key and step, with its paired-session equivalent. Source: the legacy-gap inventory of 2026-10-05, updated for D-LG1 (review-only entry, v2.11.0) and the D-LG2 design.

Status values:
- **covered**: an equivalent exists, or none is needed once legacy is gone (marked "none needed").
- **planned**: designed or scheduled, under the named decision. M7 is the seeded-defect comparison.
- **keep (provisional)** / **retire (provisional)**: the owner's answer of 2026-10-05 (D-OWNER-1005), to be confirmed
  again when the row's work item starts. Keep: paired-session needs an equivalent before legacy is removed. Retire:
  the capability is not carried over as a legacy path; any follow-up named in the row still applies (L105 warn or drop,
  L107 delete `skip_globs`, L133 open paired-session to Linux after a real Linux run). `Lnn` is the row's line at `57cb6cf`, from which the decision sheet was built.

**Entries and skills**

| Legacy | paired-session equivalent | Status |
|---|---|---|
| `/review-loop` with fresh work (Claude), a fresh review-loop request (Codex) | the paired-session skill runs `run --lifecycle-mode on` | covered |
| Code-exists auto-route (review an existing change) | `run --review-only [--base <ref>]` (D-LG1) | covered |
| `/review-loop:execute --review-only` | `run --review-only`; the legacy command stays until E-8 is revisited, and M7 needs it from a pinned copy | covered |
| Plan-exists auto-route | none: the PLAN author drafts again, and legacy plans are not imported | keep (provisional): keep legacy (L75) |
| `/review-loop:plan` (both hosts) | `run --stop-after-plan`: `/review-loop:paired-session <work item> --plan-only` (Claude), or a request to stop after the plan (Codex, PSE) | covered |
| `/review-loop:execute --session <uuid>` | `resume` of a paired run (after `--stop-after-plan`) | covered |
| `/review-loop:execute --plan <text\|path>` | plan text in `WORKITEM.md`, then drafted and reviewed again (no `plan_source` import) | keep (provisional): keep legacy (L78) |
| `--stop-after exec-round` | operator CLI `--max-exec-rounds 1 --lifecycle-mode off --adversarial-gate off` (not a skill route) | planned (M7) |
| `--stop-after before-polish` / `before-docs` / `before-security` | none; a HOLD plus `resume` partly substitutes | keep (provisional): keep legacy (L80) |
| `--stop-after before-delivery` | DONE = acceptance pending | covered |
| `--accept-external-state` | none needed: external drift is a HOLD by design | covered |
| Resume of a legacy session | none needed: paired runs resume with `resume`; legacy sessions are not imported | covered |
| `/review-loop:review-pr` | report mode on a materialized PR copy (D-LG2; nothing is posted unless the operator opts in); `simplify` only through `--legacy` | covered (LG2); closing check passed 2026-10-06 (PR NYTC69/review-loop#6) |
| `/review-loop:code-quality-loop` | `run --review-only` + POLISH-Q (D09 = A; `paired_session/docs/cql-retirement.md`) | covered (CQL-RETIRE); `--legacy` keeps the legacy loop until legacy is deleted |
| `/review-loop:reorganize` | none needed: a standalone tool without review-loop state | covered |
| `/review-loop:guide` (both hosts) | already describes paired-session; final rewrite at retirement | covered |
| `/review-loop:legacy`, `entry: legacy`, the Codex "legacy review-loop workflow" request | none needed: they go away with legacy | covered |
| Stage A fallback to legacy (key absent) | none after retirement: the default entry must refuse, with new wording | keep (provisional): keep legacy (L89); conflicts with removal itself, resolved at re-confirmation |
| Handsfree reviewer decisions (`DECISION:`) | every author question is a HOLD for a human | keep (provisional): keep legacy (L90) |

**Config keys** (`.review-loop/config.md`)

| Key | paired-session equivalent | Status |
|---|---|---|
| `reviewer`, `codex_reviewer_backend` | role vendors from the operator profile or `--reviewer-vendor` | covered |
| `reviewer_model`, `executor_model` | `--reviewer-model` / `--author-model` or the operator profile; warned when set to anything other than empty or `inherit`, not applied | covered |
| `codex_reviewer_model`, `codex_executor_model` | `--reviewer-model` / `--author-model` or the operator profile; silently ignored | covered |
| `judgment_model`, `cheap_model` | per-role models only; no tiering of specialists | keep (provisional): keep legacy; give paired-session a mapping (L99) |
| `soft_limit_plan`, `soft_limit_exec` | `--max-plan-rounds` / `--max-exec-rounds`: a hard HOLD, not a prompt; exec default 4 (legacy 3) | keep (provisional): keep legacy; give paired-session a continue path (L100) |
| `auto_commit` | operator-profile `auto_commit`: one hook-free local commit on accept | covered |
| `commit_message_prefix` | none: the commit message is fixed | keep (provisional): keep legacy; give paired-session a mapping (L102) |
| `docs_file` | `--docs-file` | covered |
| `handsfree` | stage A questions fail; `accept` / `reject` are never run | retire (provisional) (L104) |
| `review_focus`, `review_style`, `quality_focus` | none (ignored) | retire (provisional): warn or drop (L105) |
| `skip_quality_polish` | `--skip-quality-polish` | covered |
| `adversarial_gate_skip_paths` | none: `skip_globs` is frozen but not read, and the lifecycle refuses `--adversarial-gate off` | retire (provisional): delete `skip_globs` (L107) |
| `cross_vendor_review` | the default roles are cross-vendor; no same-vendor detection | keep (provisional): keep legacy; give paired-session a same-vendor check (L108) |
| `context_persist_threshold` | none needed: state lives in the run directory | covered |
| `entry` | none needed: goes away with legacy | covered |

**Workflow steps and integrations**

| Legacy step | paired-session equivalent | Status |
|---|---|---|
| Plan drafting and review; implementation and execution review; stuck detection; terminal adversarial gate (3.4); quality-polish specialists (3.5: language reviewers, code-reviewer, silent-failure-hunter, pr-test-analyzer); docs (3.6); security (3.7); delivery gate and `auto_commit`; evidence and usage | PLAN / EXEC with shadow, FIELD-5 structural HOLD, gate, POLISH-Q, DOCS, SECURITY, accept, receipts and `usage.json` | covered |
| code-simplifier writer (3.5.4), test-consolidation writer (3.5.5) | POLISH-Q quality writers (D09 capability 1, v2.12.5) | covered |
| comment-analyzer, type-design-analyzer (review-pr, code-quality-loop only) | specialists of the review-pr report mode (D-LG2) | covered for review-pr (LG2); code-quality-loop: dropped from the fixing route under D09 (report mode keeps them) |
| Dispute / triage of a finding | owner-only disposition; operator `note` | keep (provisional): keep legacy; give paired-session a dispute flow (L119) |
| Chinese delivery report with findings, rounds and token totals | a shorter report, only at ACCEPTED | keep (provisional): keep legacy; bring paired-session up to it (L120) |
| Push / PR | none in either workflow; `accept` refuses external delivery | covered |
| Compass BACKLOG close | none needed: legacy never closed items either; no close stage (D-3) | covered |
| Compass checkpoint injection, MemPalace historical context | none | retire (provisional): drop; MemPalace is no longer used (L123) |

**Runtimes and platforms**

| Legacy | paired-session equivalent | Status |
|---|---|---|
| Claude Code plugin; Codex Stage 1 (natural-language triggers) | `skills/paired-session` and `.agents/skills/paired-session` with the shared entry contract | covered |
| `reviewer: codex \| subagent`, `.codex/agents/*.toml`, `scripts/run_claude_reviewer.py` | the coordinator launches its own role CLIs | covered |
| Parallel reviewer fan-out | none needed: roles run in sequence (wall time only) | covered |
| macOS | the full path | covered |
| Linux (legacy works today) | with the `entry` key absent, a non-macOS host falls back to legacy; with `entry: paired-session` there is no host check; no real Linux run recorded | retire (provisional): open paired-session to Linux after a real Linux run (L133) |
| Windows | unsupported in both | covered |
| CI and off-macOS tests | the macOS-sandbox tests skip elsewhere (`DARWIN_SANDBOX`); the workflow location is to be confirmed | keep (provisional): keep legacy (L135) |

**Owner answers:** the 18 rows above were answered on 2026-10-05 (D-OWNER-1005): 13 keep, 5 retire, all
provisional. The owner's note on L123: "mem palace早就被踢出去了, 我们现在完全不用他. 不用对齐这个."

Push and PR delivery stays an owner decision (D8).

**Must stay runnable for M7:** M7 is optional since the ADR-6 amendment D-READY (2026-10-05); if it is run, its legacy arm needs these from a pinned copy, even if the live plugin drops them:
- `execute --review-only` with `--stop-after exec-round` or `before-polish`;
- `reviewer: subagent`;
- `adversarial_gate_skip_paths`;
- `skip_quality_polish`;
- handsfree.

## Known gaps
Details and evidence: [`paired_session/docs/1c-safety-controls.md`](../paired_session/docs/1c-safety-controls.md) and ADR-11 in `DECISIONS.md` (added with the lifecycle in the same release).
- **1C remains an inventory, not a closure.** Owner decision E-12 (2026-10-04) means it no longer gates the default entry; it does not mean these gaps are fixed. The lifecycle runs with a sandboxed author, fresh reviewers and the adversarial gate on; a probe PASS or PASS_RESIDUAL_RISK is required only in strict mode.
- Several controls (for example the Codex author sandbox and the hook/credential flags) are verified only by offline tests of generated flags; their equivalence to a real `codex exec` is unverified.
- A headless `claude -p` session cannot drive a run the usual way: the skill starts `run` in the background and ends its turn to wait, and `-p` then exits and kills the background run mid-turn. Use an interactive session. If you must run headless, say so in the prompt, so the agent keeps its turn open and polls the run until its command has exited. A run cut off this way is recovered with `--retry-uncertain` (`resume`, or `permission-probe` for a cut-off probe); the agent checks the cut-off turn and asks you first.
- The per-round change detection cannot see gitignored files or `.git`, ignored files are not inventoried on real runs, and Git hooks are neither run nor blocked.
- `accept` and `reject` have no operator authentication. DONE is not acceptance: the agent runs `accept` or `reject` only on your explicit decision in the conversation.
- The candidate-tree isolation track (separate candidate checkouts and coordinator-run test sandboxes) remains later hardening, not part of v2.10.0. There is no Compass BACKLOG close stage (owner decision D-3).
- External delivery (push, PR, merge) is not available; with `auto_commit` on, acceptance makes one local commit only.
- Installed Codex writes a trust entry into `~/.codex/config.toml` (outside the worktree); the coordinator attributes and reports it but does not revert it.
- Concurrent runs need one absolute `CODEX_HOME` each, and concurrent permission probes can fail each other; see [`paired_session/docs/concurrent-runs.md`](../paired_session/docs/concurrent-runs.md).
