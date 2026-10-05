# Q6: retire code-quality-loop onto the review-only entry? (memo for the owner)

Docs only. D-LG1 Q6 (`review-only-entry.md` §10) said: "code-quality-loop: retire it onto this entry, or keep it
standalone? Recommend deciding after one real LG1 run." That run exists. This memo maps what code-quality-loop does
against what a review-only paired-session run already gives, and asks for the decision.

Abbreviations: `CQL` = `skills/code-quality-loop/SKILL.md`; `C` = `paired_session/coordinator.py`;
`WL` = `paired_session/worktree_lifecycle.py`; `BP` = `paired_session/budget_policy.py`. Lines are at `57cb6cf`/`1121a4b`.

## 1. The real LG1 run (evidence)

v2.11.0 gate run `a90a9e54`, review-only on the default entry, workspace `~/paired-runs/real210/ws9`.
- **Configuration:** lifecycle on, efficient mode, author Codex gpt-6.1-sol, reviewer Claude claude-opus-5-5, gate
  Codex. auto_commit off, max EXEC rounds 4.
- **Turns:**
  1. EXEC: reviewer, shadow; author fix; reviewer, shadow; gate.
  2. FINISH.
  3. POLISH-Q: four specialists (python-reviewer, code-reviewer, silent-failure-hunter, pr-test-analyzer).
  4. DOCS writer and docs review.
  5. SECURITY.
  6. Accepted.
- **Findings:**
  - The seeded defect (`median()` never sorts) was found in round 1 by both the reviewer (F001) and the shadow
    (F004), and fixed in EXEC round 2. The weak tests (F005) were fixed with it.
  - The specialists added only MINOR findings, which stayed open and non-blocking: NaN handling (silent-failure-hunter)
    and missing single-element and iterator tests (pr-test-analyzer).
  - The docs review flagged the DOCS writer's CHANGELOG entry for carrying run details (F010, open MINOR).
- **Cost:** 14 invocations of 25; 1.03M input tokens (83 % cached), 17k output; about 26 minutes of turn time.

So one real run shows the review-only entry doing code-quality-loop's core job: review the existing change, fix the
blockers, re-review until approved, run the quality specialists and keep the tests green.

## 2. Capability map

| code-quality-loop (CQL) | Review-only paired-session run (lifecycle on) | Status |
|---|---|---|
| **Scope.** The unstaged `git diff`. Language detection uses `git diff HEAD` plus untracked files (CQL:37-41). | The whole non-ignored change against `--base` (default `HEAD`): staged, unstaged and untracked; with `--base REF`, committed work too. Refuses partially staged paths and unmerged entries. | covered (wider) |
| **Rounds.** Default 5; STUCK when the OPEN set is unchanged for 3 rounds; then always finalizes (CQL:26-33, 334-365). | `--max-exec-rounds` (default 4; round 1 is the existing change). FIELD-5 structural HOLD after the same defect class in 3 BLOCKs. The cap and STUCK end in a HOLD, not a finalize. | covered (stricter) |
| **Review roles.** Round 1: code-reviewer, silent-failure-hunter, comment-analyzer, type-design-analyzer; round 2+: code-reviewer, silent-failure-hunter (CQL:190-191). | The EXEC reviewer, plus a fresh shadow and the adversarial gate. POLISH-Q then runs code-reviewer, silent-failure-hunter, pr-test-analyzer and the language reviewers (WL:13-16). | partly: no comment-analyzer or type-design-analyzer |
| **Who fixes.** The orchestrator applies fixes with Edit and runs no tests (CQL:373-393). Every fix round returns to a fresh review round; clean only on a clean review (CQL:358, 405). An early exit happens only when no fix was applied (CQL:407). | A persistent author turn, re-reviewed by the persistent reviewer, a fresh shadow and the adversarial gate. The FINISH writer runs the test command. A FINISH or SECURITY write replays EXEC review and the gate. A DOCS write does so only when it leaves the docs allowlist or the docs review asks for changes; allowlisted docs with a passing docs review go on to SECURITY (C:5662-5684). | covered; adds a separate author, the shadow and the gate |
| **Pre-loop static analysis.** The caller runs each language's static-analysis commands into artifacts, the language reviewers report, the orchestrator fixes (CQL:110-179). | Language reviewers run as POLISH-Q specialists after FINISH and run the test command. No linter or static-analysis command runs and no artifact is kept. | partly (static analysis lost) |
| **Design-doc boundary.** CQL finds and loads the newest project design document automatically, constrains every fix to it and remembers the skipped issues (CQL:51, 378-381). | The review scope and the work item; a design document reaches the roles only if the work item names it | lost (automatic loading) |
| **Reorganize** (auto when > 3 files or > 100 lines, CQL:413-421) | none | lost |
| **code-simplifier writer and build check.** The build command is picked from the detected languages (go build, ...), with up to 3 fix attempts (CQL:429-474). | No simplifier: `BP:10` reserves a `simplifier` budget, but nothing dispatches it. No build step: only the configured test command runs (FINISH, reviewers). | lost (simplifier; automatic build selection) |
| **Test consolidation writer** (CQL:476-494) | none. `BP:10` reserves `test-writer`, never dispatched. pr-test-analyzer reports gaps, and a blocking one goes to the author through the polish-fix leg. | lost (writer); report kept |
| **pr-test-analyzer** with orchestrator fixes (CQL:496-546) | A POLISH-Q specialist. MEDIUM and up block; the owner re-checks the fix. | covered |
| **Docs and comment consistency.** CQL searches every project doc (design, ADR, runbooks, memory files, changelogs) and updates the stale ones (5.1). It always checks and fixes changed code comments (5.2) (CQL:564-592). | The DOCS writer edits allowlisted docs only. The docs reviewer checks the docs and that "the changed code comments must match the code" (C:5692-5695). That review runs only when DOCS wrote or owns a doc (C:5662); a blocking finding returns to EXEC. | partly (conditional comment check; no project-wide doc sweep) |
| **Security** (none in CQL) | SECURITY stage: sensitive paths, preflight and a fresh security reviewer | added |
| **Delivery.** No commit; a final report (CQL:594-618). | Operator `accept`. Optional one local commit (auto_commit), never a push. A Chinese delivery report. | covered |
| **Config.** `quality_focus`, `review_style`, `judgment_model`, `cheap_model` (CQL:53-58) | not read; models come from the operator profile | lost (owner decision 8 in the migration map) |
| **Hosts.** Claude only; no Codex counterpart (`.agents/skills` has none) | Both hosts | added |

## 3. What retiring it would lose
1. **The three writer passes:** reorganize, simplify and test consolidation. These are the only places in the plugin
   that restructure or simplify code, or rewrite tests, without a defect to justify it. Paired-session's writers fix
   delivered findings only. The equivalent decision for the main pipeline is still open: owner decision 4 in the
   migration map ("port or formally drop the simplifier and test-consolidation writers").
2. **comment-analyzer and type-design-analyzer.** Neither runs anywhere in paired-session. The D-LG2 review-pr design
   proposes them as conditional specialists.
3. **Automated helpers outside the review loop:**
   - the caller's static analysis with artifacts;
   - automatic loading of the newest design document, which constrains every fix;
   - the project-wide documentation sweep (5.1);
   - unconditional code-comment fixing (5.2);
   - build-command selection per detected language;
   - test-command selection per detected language, running every applicable suite when several languages changed
     (CQL:486-492). Paired-session runs the one configured test command.

   With A these become manual: a static-analysis command can be added to the test command, a design document can be
   named in the work item, docs edits stay with DOCS. Some cannot be replaced by configuration: the doc sweep, and
   comment fixing outside the conditional docs review.
4. **A different loop shape, not a cheaper one.**
   - CQL re-reviews after every fix round, up to 5 rounds, and always reaches its finalize, even after STUCK or at the
     cap.
   - A paired run HOLDs at its cap. It adds a separate author, a fresh shadow, the gate and SECURITY.
   - This memo has only the paired run's cost: 14 turns and about 26 minutes. It has no CQL measurement on the same
     task, so it makes no cost comparison.
5. **Its knobs:** `[max-rounds]`, `--skip-reorganize`, `--reorganize`, the tier keys and the focus/style keys.
6. **Things that depend on it:**
   - the lint contracts `tests/skills/contracts/review-loop.json:606-655` (ten dispatch-target entries);
   - `assertion-mapping.json:1546-1604`, and the cheap-model backstop mapping at `:1190-1202`;
   - `tests/reviewer_dispatch_contract_test.py:12, 30, 51`, and `:80-85` (the finalize order);
   - `agents/go-reviewer.md:13`;
   - README, ARCHITECTURE and guide mentions.

   A retirement has to move or remove these in the same change.

## 4. Options
- **A. Retire onto `run --review-only` plus POLISH-Q.**
  - `/review-loop:code-quality-loop` becomes a thin entry to the review-only route, with a notice listing what no
    longer runs.
  - Everything in §3.1-3.3 and §3.5 is dropped or becomes manual; the loop shape changes as in §3.4.
  - Small change (skill text, lint, docs, the contracts in §3.6).
- **B. Port as a review-only mode flag, for example `run --review-only --quality-loop`.** Capability by capability; A
  and B can be combined, porting some items and dropping others.
  - Keeps the paired review/fix loop. After POLISH-Q it adds the missing writers as gated stages: simplify and test
    consolidation as writer turns whose writes replay EXEC review and the gate, so no unreviewed write ships; and
    reorganize behind a size threshold.
  - comment-analyzer and type-design-analyzer join POLISH-Q.
  - Largest change: coordinator stages, budgets (the reserved `simplifier`/`test-writer` caps), tests and docs.
  - It overlaps owner decision 4 for two of its items, the simplifier and test consolidation.
- **C. Keep it standalone, as legacy.**
  - It stays a Claude-only legacy skill, outside the paired-session safety model (orchestrator Edit fixes, no shadow,
    gate or security).
  - No work now. It must stay runnable after legacy retirement, and it blocks deleting the legacy reviewer plumbing.

## 5. Recommendation
**A now, B only if the owner wants the writer passes back.**
- The real run shows the review-only route already delivers code-quality-loop's core value with stronger checks.
- Most of what A drops is optional polish or can be done by hand (§3). CQL itself skips reorganize for small changes.
- The simplifier and test consolidation are the same writers owner decision 4 asks about for the main pipeline, so
  decide them once.
- C keeps a second, weaker review path alive and works against retiring legacy.

## 6. Owner question
**Q6.**
- A: retire code-quality-loop onto `run --review-only`;
- B: port it as a review-only mode with gated stages;
- C: keep it as a standalone legacy skill.

With A or B, also choose for each capability (keep and port, defer, or drop):
1. simplifier and test consolidation (this is also owner decision 4, main pipeline);
2. reorganize;
3. comment-analyzer and type-design-analyzer (already planned as D-LG2 specialists);
4. static analysis with artifacts;
5. automatic design-document loading;
6. the project-wide doc sweep.

*Recommend:* A, with items 1-6 dropped or manual for now. Revisit item 1 when owner decision 4 is answered, and
item 3 with D-LG2.
