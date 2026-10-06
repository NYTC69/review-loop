# CQL-RETIRE: code-quality-loop onto the review-only entry (design)

Status: design only, no code. Owner decision D09 / Q6 option A (2026-10-05): "retire code-quality-loop onto
`run --review-only` + POLISH-Q". Capability 1 (the simplifier and test-consolidation writers) shipped in v2.12.5
([d09-cap1-writer-passes.md](d09-cap1-writer-passes.md)); capability 3 (comment/type analyzers) went to D-LG2;
capabilities 2, 4, 5 and 6 are dropped. This document is the retirement change itself, in the shape review-pr took
in LG2-c: the skill follows `entry`; the paired route is the default; `entry: legacy` or an explicit legacy request
keeps the old skill, with the deprecation notice, until legacy is deleted. No coordinator code changes; lane B owns
the POLISH-Q writer code.

Sources: [q6-code-quality-loop.md](q6-code-quality-loop.md) (the capability map and §3 losses);
[review-pr-port.md](review-pr-port.md) and `skills/review-pr/SKILL.md` Step 0 (the LG2-c routing shape);
`docs/protocol/paired-session-entry.md` (shared contract; "Review-only entry", "Quality writers");
`docs/paired-session-migration.md` ("Deprecation status", the legacy map). Abbreviations: `CQL` =
`skills/code-quality-loop/SKILL.md`; `PSE` = `docs/protocol/paired-session-entry.md`; `PS` =
`skills/paired-session/SKILL.md`; `RLJ` = `tests/skills/contracts/review-loop.json`; `AM` =
`tests/skills/contracts/assertion-mapping.json`. Lines are at `4c4ccac`.

## 1. Routing

### 1.1 Claude: `/review-loop:code-quality-loop`

A new **Step 0 — Entry** at the top of CQL (after the frontmatter and title; the current body becomes the legacy
route, unchanged). The `argument-hint` becomes `"[max-rounds] [--skip-reorganize | --reorganize] [--legacy]"`.

Resolve `entry` in `.review-loop/config.md` as the review-loop entry does (exact values `legacy` and
`paired-session`; an invalid value, a duplicate key or an unreadable config is legacy, with that entry's warning
line). Then:
- `--legacy` in `$ARGUMENTS`: drop it and run the legacy loop (Initialization on), whatever `entry` says. Print the
  deprecation notice (§3.2) once, before Initialization.
- `entry: legacy` (or an invalid entry): the legacy loop, unchanged, with the deprecation notice.
- `entry` absent or `paired-session`: the paired route.
  1. Print `code-quality-loop: paired-session review-only run (entry set in .review-loop/config.md)` when the key is
     set, or `code-quality-loop: the paired-session review-only run is the default entry; set "entry: legacy" in .review-loop/config.md or pass --legacy for the legacy loop`
     when it is absent.
  2. Map the arguments (§1.3). A refused argument stops here with its one line, before any handoff.
  3. Print the one-line loss notice:
     `code-quality-loop: the paired route reviews, fixes, simplifies and consolidates tests, and accept makes one local commit (never a push); it does not reorganize, run static-analysis artifacts, load a design document or sweep project docs (use /review-loop:code-quality-loop --legacy for those)`.
  4. Invoke the `paired-session` skill with `--code-quality-loop` and the mapped options, and end this skill.
  5. The no-argument-equivalent fallback, as review-pr: with the key **absent**, if the paired-session skill reports
     `stage A failure: <reason>`, print
     `code-quality-loop: paired-session default entry unavailable (<reason>); using the legacy loop` and continue
     with the legacy loop on the original arguments (no deprecation notice: legacy was not chosen). With the key set,
     report the failure and stop (never fall back). From the first `bin/paired-session` command on, nothing falls
     back (PSE "Entry and failure handling").

### 1.2 The paired-session side of the handoff

PS (host rules) gets one paragraph beside the review-pr paragraph (PS:116-127):

> A code-quality-loop handoff (`--code-quality-loop [OPTIONS]` from `/review-loop:code-quality-loop`) follows the
> shared contract's Code-quality-loop entry. In both blocks add `--review-only` and the mapped
> options; `WORKSPACE` is the current worktree. Legacy pointer: `use /review-loop:code-quality-loop --legacy`.

PSE gets a **Code-quality-loop entry** subsection after "Review-only entry" (about 12 lines):
- It is a review-only run (everything in "Review-only entry" applies: the change is the whole non-ignored worktree
  against `HEAD`; the same stage A listing and the same refusals). CQL never took a base, so no `--base` is passed.
- **CLIs:** the code-quality-loop skill checks nothing before it hands off, so run the direct-invocation CLI check
  of "Stage A checks" here (every CLI the resolved roles need, `command -v`), as the Review-PR entry does (PSE:145-146).
  A missing CLI is a failed stage A check, so with the key absent the default-entry fallback (§1.1 step 5) applies
  before any coordinator command.
- **Test command:** as for any review-only run (profile `test_command`, else the verified project command, asked
  only when it cannot be established). Pass it as `--test-command` on the CLI: that makes it explicit, which the
  quality writers need (`skipped:no-test-command` otherwise).
- **Quality writers:** the review-only default `both` applies; do not pass `--quality-writers` (an operator profile
  that sets it wins, as for any review-only run).
- **Budget:** pass `--max-invocations 35` unless the operator profile sets `max_invocations` (D09 §3: under the
  default 25 an LG1-sized run is usually `skipped:budget` at the writers, and the writers are this entry's point).
- **Commit:** an explicit `auto_commit: false` in `.review-loop/config.md` is honored: CQL hands over
  `--auto-commit false` and the run passes it. With the key absent (or `true`) the review-only default applies
  (owner FIELD-25, 2026-10-06: auto_commit true, with the untracked-file notice): `accept` makes one local commit and
  never pushes; an operator-profile `auto_commit: false` also wins, as for any review-only run.
- **WORKITEM.md:** the goal "Review and improve the quality of the uncommitted change: correctness, error handling,
  tests and simplicity; fix what the reviews find." plus a "Review priorities" section carrying `quality_focus` and
  `review_style` verbatim when they are set (§1.3). No review history.
- **Result:** the run ends DONE (or HOLD) like any review-only run; show the delivery report (it has one line per
  quality writer, D09 §4) and accept or reject only on the user's explicit decision.
- PSE "Entry and failure handling": "only the review-loop or review-pr entry falls back" becomes "only the
  review-loop, review-pr or code-quality-loop entry falls back".

The exact command the paired-session skill runs (Claude, efficient mode; strict adds the same flags to the probe):

```sh
"${CLAUDE_PLUGIN_ROOT}/bin/paired-session" run \
  --workspace "$WORKSPACE" --workitem "$WORKITEM" --run-dir "$RUN_DIR" \
  --test-command "$TEST_COMMAND" --lifecycle-mode on \
  --review-only --advisory-fix-round true [--max-exec-rounds N] [--max-invocations 35] [--config "$PROFILE"]
```

CQL-R3 (after the ws28 closing run, where a headless model printed neither notice and the work item began with `# Work item`, which became the commit title):
- **Notices:** CQL Step 0 owns the entry line and the loss notice and prints them verbatim as visible text, first after resolving `entry` and before any other tool call or skill invocation, also headless. The paired-session skill (PS handoff paragraph, PSE Code-quality-loop entry, which quotes both lines) prints them, as its first output after loading the shared contract and before any stage A check, only when they are missing from the conversation's visible output. Lint pins the three lines in CQL and PSE.
- **Work item title:** the first line of WORKITEM.md becomes the commit title and the delivery report's work-item name (`coordinator.py` accept, `worktree_lifecycle.py` report). PSE "Work item" now requires a title `# <one-line summary of the task>` for every handoff (never `# Work item`); the review-only entry starts with a title naming the change (its old one-line goal "Review the change for correctness" would also have become the commit title); the CQL entry uses `# code-quality-loop: <one-line summary of the uncommitted change>`.
- **Non-blocking fix round (wired, CQL-WIRE):** owner decision "加一轮修非阻塞". The coordinator option `--advisory-fix-round` (ADVFIX, lane B, 1ac3c1d) is passed as `--advisory-fix-round true` in both blocks (PSE Code-quality-loop entry, the PS handoff paragraph): after the first clean POLISH-Q the author gets one round for the open non-blocking findings, then the full re-review chain, before the quality writers; once, on its own round, skipped with the reason without budget room. Later commands repeat it with the run's other options (PSE "Existing runs and HOLD"). This is what the legacy loop's "fixed what it found" maps to (ws28 changed no code without it).

What the user sees, in order: the entry line (§1.1 step 1), the loss notice, the paired-session start line and
progress (EXEC review of the change as round 1, author fixes, shadow and gate, FINISH, POLISH-Q specialists, the
`POLISH-Q simplifier` / `POLISH-Q test-writer` legs and a `replay epoch N` line when one wrote, DOCS, SECURITY), then
DONE with the Chinese delivery report, then the accept question. Differences from the legacy loop that the user
meets: untracked and staged files are part of the change; a partially staged path or unmerged entry refuses before
any state; the round cap ends in a HOLD, not a finalize.

### 1.3 Argument and config mapping

One line each; "refused" stops before the handoff with that line.

| CQL input | Paired route |
|---|---|
| `[max-rounds]` N (default 5) | `--max-exec-rounds N` when N ≥ 2 (round 1 is the review of the existing change; the cap is a HOLD). It wins over `soft_limit_exec`. Absent: the usual mapping (`soft_limit_exec`, else the coordinator default 4); one value goes to probe and run. N < 2 or not an integer: refused, `code-quality-loop: max-rounds must be an integer of at least 2 on the paired route (round 1 reviews the existing change)`. |
| `--skip-reorganize` | accepted as a no-op: `code-quality-loop: --skip-reorganize has no effect; the paired route never reorganizes` |
| `--reorganize` | refused: `code-quality-loop: reorganize is not part of the paired route; run /review-loop:reorganize after the run, or /review-loop:code-quality-loop --legacy --reorganize` |
| `--legacy` | the legacy route (§1.1) |
| any other argument | refused: `code-quality-loop: unknown argument <arg>; usage: /review-loop:code-quality-loop [max-rounds] [--skip-reorganize] [--legacy]` |
| `judgment_model`, `cheap_model` (config.md) | not applied; when set, one warning line: `code-quality-loop: <key> in .review-loop/config.md is not applied by paired-session; models come from the operator profile` (the PSE warning for `reviewer_model`, same shape) |
| `quality_focus`, `review_style` (config.md) | mapped: copied verbatim into WORKITEM.md under "Review priorities" (operator text, which every role reads; FIELD-30 keeps tool names there from holding the scan) |
| `auto_commit` (config.md) | `false`: handed over as `--auto-commit false`; absent or `true`: the review-only default (one local commit at `accept`), no warning on this route |
| `skip_quality_polish`, `docs_file`, `soft_limit_exec` (config.md) | as for every paired run (PSE "Profile and settings"); `skip_quality_polish: true` also turns the writers off (D09 §4) |

### 1.4 Codex

There is no Codex code-quality-loop skill (`.agents/skills/` has none; Q6 §2 "Hosts"). No skill is added. On Codex the
same capability is the existing review-loop handoff of "review my existing change" to `run --review-only`, whose
writers default to `both`. The Codex guide gets one sentence saying so (§3.5). A Codex user who asks for "the code
quality loop" by name is matched to the review-loop skill by description; no new routing.

## 2. Q6 memo §3 losses under option A

| Q6 §3 item | Under D09 / option A |
|---|---|
| 3.1 simplifier and test consolidation writers | covered: D09 capability 1, shipped v2.12.5 (POLISH-Q writer legs, default `both` for review-only) |
| 3.1 reorganize | accepted loss (capability 2 dropped); `/review-loop:reorganize` stays as a standalone tool (migration map "covered") |
| 3.2 comment-analyzer, type-design-analyzer | covered by the decision: capability 3 went to D-LG2, where they are review-pr report-mode specialists (`/review-loop:review-pr comments types`); they do not run in the fixing route's POLISH-Q, which the decision did not ask for |
| 3.3 caller static analysis with artifacts | accepted loss (capability 4 dropped); manual: put the analyzer in the test command |
| 3.3 automatic design-document loading | accepted loss (capability 5 dropped); manual: name the document in the work item |
| 3.3 project-wide doc sweep (CQL 5.1) | accepted loss (capability 6 dropped); DOCS edits allowlisted docs only |
| 3.3 unconditional code-comment fixing (CQL 5.2) | accepted loss under A ("everything in §3.1-3.3 is dropped or becomes manual"); the docs review checks changed comments when DOCS runs |
| 3.3 build-command selection per language | accepted loss under A; manual: the build goes into the test command |
| 3.3 per-language test-command selection | accepted loss under A; one configured test command |
| 3.4 loop shape (always finalize vs HOLD at the cap) | accepted under A (Q6 §4 A: "the loop shape changes as in §3.4"); the HOLD is stated in the start notice |
| 3.5 knobs | mapped or refused in §1.3 |
| 3.6 dependants | moved in this change (§3) |

No row needs the owner.

## 3. What moves or changes in the same change

Principle: the legacy body of CQL stays byte-for-byte until legacy is deleted, so every contract and test that
pins the legacy loop stays green and is **kept, not moved**. The retirement adds the entry and its contracts,
updates the deprecation sentence and the user docs, and touches no coordinator code.

### 3.1 The skills

| File:line | Planned edit |
|---|---|
| CQL:3 | description: prefix "With `entry` absent or `paired-session` it runs a paired-session review-only run (review, fix, simplify, consolidate tests; `run --review-only`); `entry: legacy` or `--legacy` runs the legacy loop." |
| CQL:4 | `argument-hint: "[max-rounds] [--skip-reorganize \| --reorganize] [--legacy]"` |
| CQL:13→14 | insert `## Step 0 — Entry: paired route or legacy` (§1.1, §1.3; about 30 lines) after the reviewer-runtime paragraph (CQL:11-13) and before `## Overview`. Constraints from `tests/reviewer_dispatch_contract_test.py`: no fenced block with `subagent_type:` (test :19-27 counts exactly one), no "Native reviewer prompt" text (test :30-47 counts exactly 6), stays before `## Initialization` (test :30-47), and no `### Phase`, `## Finalize` or `### Step N:` heading (test :80-85 orders those). The deprecation notice (§3.2) is quoted in Step 0. |
| CQL:14-618 | unchanged (the legacy route) |
| PS:3 | argument-hint adds `\| --code-quality-loop [OPTIONS]` |
| PS:8-14 | description: "Trigger in exactly four cases" → five; add "(5) /review-loop:code-quality-loop hands off on its paired route (`--code-quality-loop`)". The two needles there stay intact: "the review-loop entry hands off because .review-loop/config.md sets `entry: paired-session`" (AM:2108-2117) and "(key invalid, `legacy`, or" (AM:2118-2127). |
| PS:127→128 | the code-quality-loop handoff paragraph (§1.2, about 5 lines) |
| PSE:30-31 | "only the review-loop or review-pr entry falls back" → "only the review-loop, review-pr or code-quality-loop entry falls back" |
| PSE:138→139 | `## Code-quality-loop entry` (§1.2, about 14 lines). PSE is loaded whole (`docs/protocol/loading.json:92-94`), so no loading change. |
| `.agents/skills/paired-session/SKILL.md` | unchanged (no Codex code-quality-loop) |

### 3.2 The deprecation sentence (9 copies, one mapping)

New text (it no longer says code-quality-loop uses legacy):
`review-loop: legacy is deprecated since v2.12.0; the default paired-session entry covers fresh work, review of existing changes, review-pr and code-quality-loop; removal is planned after the open legacy-map rows are settled`

| File:line | Edit |
|---|---|
| `skills/review-loop/references/entry.md:15`, `skills/legacy/SKILL.md:31`, `skills/plan/references/entry.md:10`, `skills/execute/references/entry.md:12`, `.agents/skills/review-loop/references/entry.md:40`, `.agents/skills/plan/references/entry.md:10`, `.agents/skills/execute/references/entry.md:12`, `docs/paired-session-migration.md:16` | the sentence replaced |
| CQL Step 0 (new) | the sentence quoted for the legacy route |
| AM:2128-2161 (`legacy_deprecation_notice`) | the 8 needles replaced; a 9th `{path: skills/code-quality-loop/SKILL.md}` added. Its consumer RLJ:3549-3553 is unchanged. The lint compares case- and whitespace-insensitively (`scripts/run-skill-lint:28-36`), so all 9 copies change in one commit. |

### 3.3 Lint contracts

Kept unchanged (they pin the legacy body, which stays): RLJ:606-655 (ten `code_quality_*_dispatch_target`), AM:1546-1604
(the eleven CQL needles), AM:1190-1202 (the CQL pair of `cheap_model_backstop_haiku_4_5`, used by
`shared-schema.json:166-170`), and the four all-skill scans that see CQL today (RLJ:517-522, RLJ:533-542 satisfied by
CQL:433, AM:586-593 via RLJ:1243 and `guide.json:156-157`, AM:576-585 via RLJ:1240). They are removed with the legacy
body at legacy deletion, not here.

Added, mirroring LG2-c's review-pr entries (RLJ:3858-3881, 3959-3971; AM:2270-2330):

| New id | Kind | Path(s) | Needle |
|---|---|---|---|
| `cql_routes_on_entry` | contains | CQL | `## Step 0 — Entry: paired route or legacy` |
| `cql_legacy_argument` | contains | CQL | "- `--legacy` in `$ARGUMENTS`: drop it and run the legacy loop" |
| `cql_paired_route_hands_off` | contains | CQL | "Invoke the `paired-session` skill with `--code-quality-loop`" |
| `cql_reorganize_refused` | contains | CQL | "code-quality-loop: reorganize is not part of the paired route" |
| `cql_default_entry_notice_consistent` | consistent_with → AM `cql_default_entry_notice` | CQL, `docs/paired-session-migration.md` | "code-quality-loop: the paired-session review-only run is the default entry; set \"entry: legacy\"" |
| `cql_legacy_pointer_consistent` | consistent_with → AM `cql_legacy_pointer` | CQL, PS, PSE | "/review-loop:code-quality-loop --legacy" |
| `pse_cql_entry_section` | contains | PSE | `## Code-quality-loop entry` |
| `pse_cql_budget` | contains | PSE | "pass `--max-invocations 35` unless the operator profile sets `max_invocations`" |
| `pse_cql_cli_check` | contains | PSE | "the code-quality-loop skill checks nothing before it hands off, so run the direct-invocation CLI" |
| `paired_session_skill_cql_handoff` | contains | PS | "/review-loop:code-quality-loop hands off on its paired route (`--code-quality-loop`)" |

Added in CQL-R2 (the CQL-R1 gate asked to pin the handoff command, not only headings): `paired_session_skill_cql_review_only_flags`
and `paired_session_skill_cql_explicit_test_command` (PS: `--review-only`, the options, `--max-invocations 35` and the
explicit `--test-command`), `pse_cql_explicit_test_command`, `pse_cql_auto_commit_false_honored`,
`cql_auto_commit_false_handed_over`, and `pse_default_entry_covers_cql` (PSE "Entry and failure handling" names the
review-pr and code-quality-loop handoffs as default entries, and a refusal carries the handing-off entry's own name).

Changed in CQL-WIRE: `pse_cql_advisory_fix_round_reserved` (CQL-R3, it pinned the "does not exist yet" slot) is
replaced by `pse_cql_advisory_fix_round` (PSE: "- Non-blocking fix round: pass `--advisory-fix-round true` in both
blocks"), `pse_cql_advisory_fix_round_not_reserved` (not_contains "it does not exist yet") and
`paired_session_skill_cql_advisory_fix_round` (PS: "also add `--advisory-fix-round true` (one fix round for the
non-blocking findings);").

`scripts/run-skill-lint` builds its case list from the contract files only (`:1346-1350`), so these ids are the
whole case delta. Its all-skill scans (`:269-306`) iterate over the skill files and already see CQL; Step 0 must keep
them green (no `subagent_type: review-loop:` line, no new `subagent_type` value).

### 3.4 Python tests

`tests/reviewer_dispatch_contract_test.py:12, 30-47, 51, 60-77, 80-85` stay unchanged and green: they read the legacy
body, which does not change (the Step 0 constraints in §3.1 keep them so). No paired_session test changes: the
coordinator is untouched; the handoff is skill text (tested by lint), as for review-pr in LG2-c.
`agents/go-reviewer.md:13` (the CQL pre-loop pointer) and `paired_session/worktree_lifecycle.py:99` (a docstring
citing CQL Step 3; lane B's file) stay.

### 3.5 README, guides, config example (README stays intact: only the lines below change)

| File:line | Planned edit |
|---|---|
| README.md:26-28 | keep line 26's needle "The legacy workflow is deprecated since v2.12.0" (AM:2162-2165, `guide.json:151-155`); "it will be removed only after review-pr and code-quality-loop are ported" → "review-pr and code-quality-loop follow `entry` too; removal waits for the open legacy-map rows" |
| README.md:95 | "Stage 1 does not yet migrate `code-quality-loop` or `reorganize`." → "Codex has no code-quality-loop or reorganize skill; ask review-loop to review an existing change (a review-only run, quality writers on)." |
| README.md:319-323 | the section body: "Follows `entry`: by default a paired-session review-only run (review, fix, simplify, consolidate tests, docs, security; accept makes one local commit). `--legacy` or `entry: legacy` runs the legacy loop (deprecated). Arguments: `[max-rounds]`, `--skip-reorganize`, `--legacy`; `--reorganize` is legacy only." |
| README.md:485-486 | unchanged (the directory stays) |
| `skills/guide/SKILL.md:68-71` | keep line 68's needle; "review-pr is ported (it follows `entry`); removal waits for the code-quality-loop port and the open owner rows" → "review-pr and code-quality-loop follow `entry`; removal waits for the open owner rows" |
| `skills/guide/SKILL.md:66→67` | a **new** third row in the entry-command table, after the existing paired-session (:65) and legacy (:66) rows, which stay: `` | `/review-loop:code-quality-loop [max-rounds] [--legacy]` | A review-only paired-session run on the uncommitted change (quality writers on; accept commits locally); `--legacy` or `entry: legacy` runs the legacy loop (deprecation notice) | `` |
| `.agents/skills/guide/SKILL.md:35-38` | keep line 35's needle; same wording change as the Claude guide |
| `.agents/skills/guide/SKILL.md:57-60` | "It does not yet migrate: code-quality-loop, reorganize" → "Not on Codex: code-quality-loop (ask review-loop to review an existing change instead) and reorganize" |
| `review-loop-config.example.md:23-24` | keep "legacy workflow is deprecated since v2.12.0" (AM:2180-2181); "(removal after code-quality-loop is ported and the open legacy-map rows are decided; review-pr is ported)" → "(removal after the open legacy-map rows are decided; review-pr and code-quality-loop are ported)" |
| `CLAUDE.md` | no change (no hit) |

### 3.6 Other docs

| File:line | Planned edit |
|---|---|
| `docs/paired-session-migration.md:93→94` | (done in CQL-R1: the `cql_default_entry_notice` mapping needs it) two new lines in "Notices and warnings you will see", after the review-pr lines (:92-93): `` - code-quality-loop, no `entry` key: `code-quality-loop: the paired-session review-only run is the default entry; set "entry: legacy" in .review-loop/config.md or pass --legacy for the legacy loop` `` and `` - code-quality-loop, `entry: paired-session`: `code-quality-loop: paired-session review-only run (entry set in .review-loop/config.md)` `` (the first carries the `cql_default_entry_notice` needle, §3.3) |
| `docs/paired-session-migration.md:23` | status → "retired onto the review-only entry (CQL-RETIRE, vX.Y.Z); capability 1 shipped v2.12.5, 3 with D-LG2, 2/4/5/6 dropped" |
| `docs/paired-session-migration.md:143` | "planned (D09)" → "covered (CQL-RETIRE); `--legacy` keeps the legacy loop until legacy is deleted" |
| `docs/paired-session-migration.md:175` | stale since v2.12.5: "none / keep (provisional)" → "POLISH-Q quality writers (D09 capability 1, v2.12.5) / covered" |
| `docs/paired-session-migration.md:176` | "code-quality-loop follows D09" → "code-quality-loop: dropped from the fixing route under D09 (report mode keeps them)" |
| `ARCHITECTURE.md:42` | as README:95 (also drops the stale `review-pr` from "does not yet migrate") |
| `ARCHITECTURE.md:180-184` | as README:319-323 |
| `docs/install-codex.md:120-122` | unchanged (still true: not exposed to Codex) |
| `paired_session/docs/q6-code-quality-loop.md:1` | one status line under the title: "Decided D09 = A; retired by [cql-retirement.md](cql-retirement.md)." |
| `CHANGELOG.md` | a new release entry (Chinese, like the others): the paired route of `/review-loop:code-quality-loop`, the argument mapping, what no longer runs, `--legacy`, the new deprecation sentence |
| Plugin manifests | version bump only, by the release. Surfaced, not in scope: `.claude-plugin/plugin.json:4` and `.claude-plugin/marketplace.json:14` say "5 skills" while `skills/` has 9. |

Historical design docs (`d09-cap1-writer-passes.md`, `review-only-entry.md`, `review-pr-port.md`,
`legacy-deprecation-readiness.md`, `1d-entry-mapping.md`, `docs/superpowers/**`), `DECISIONS.md` and `BACKLOG.md`
cite CQL as history and stay.

## 4. Size and implementation units

No product code: every line is skill text, protocol text, contracts or docs. About 230 changed lines in total.

| Unit | Content | Lines |
|---|---|---|
| **CQL-R1, routing** | CQL frontmatter and Step 0 (§1.1, §1.3); PS description, hint and handoff paragraph; PSE fallback line and `## Code-quality-loop entry`; the §3.3 contract additions (RLJ and AM) | ~60 skill/protocol + ~70 contract |
| **CQL-R2, notice and docs** | the deprecation sentence in 9 places and its AM mapping (§3.2); README, guides, config example (§3.5); migration guide, ARCHITECTURE, Q6 status line, CHANGELOG (§3.6) | ~45 text + ~20 contract + CHANGELOG |

Both units fit the ~150-line cap and ship in one release: R1 without R2 would leave the deprecation notice saying
code-quality-loop still uses legacy. R1 first (R2's docs describe R1's behavior). Verification for each: skill lint
PASS, `tests/reviewer_dispatch_contract_test.py` green (it must not change). Closing check after both (as LG2's PR #6
run): one real `/review-loop:code-quality-loop` on a small uncommitted change with a test command, default entry,
reaching DONE with the writer lines in the delivery report, plus `--legacy` printing the notice; the supervisor runs
it, not a lane.

## 5. Decisions taken here (for the supervisor)

Supervisor rulings (2026-10-06) on the trade-offs this design proposed:
- **Commit:** the CQL route follows the review-only default (owner FIELD-25: auto_commit true, untracked-file notice).
  The Step 0 loss notice says "accept makes one local commit (never a push ...)". CQL-R1 gate ruling: an explicit
  `auto_commit: false` in `.review-loop/config.md` is honored (handed over as `--auto-commit false`).
- **`--max-invocations 35`** unless the profile sets it: approved (D09-F on lane B removes the EXEC-round budget
  skip; the invocation headroom still applies).
- **`quality_focus` / `review_style` into the work item** ("Review priorities"): approved as the interim; the D11 L105
  port may replace it.
- **`--reorganize` refused** with a pointer to the standalone tool and `--legacy`; **no Codex skill**: approved.
