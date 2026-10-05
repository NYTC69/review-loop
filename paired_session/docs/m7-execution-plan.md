# M7 execution plan: seeded-defect comparison and 2G cost on v2.10.0

Role change (ADR-6 amendment D-READY, 2026-10-05): M7 no longer gates legacy retirement; it is an optional cost and quality study, run only if the owner authorizes it.

Status: plan only (lane B, 2026-10-05); nothing here has been run. Design and pass criteria:
[m7-seeded-defect-comparison.md](m7-seeded-defect-comparison.md) (the "design"); this file says how, in what
order and at what cost. Authority: owner decisions D-POST210 (after v2.10.0, M7 + 2G first) and D-LG1 (a
paired review-only entry will be built; no small legacy CR path is kept), recorded in the supervisor's week
plan (`.compass/results/2026-09-25_week-plan-paired-session.md`, outside this repository; not yet in
`DECISIONS.md`), and ADR-6 criterion (1) "new no worse than old". M7 is evidence for retiring legacy, not a
v2.10.0 gate (amended E-1, D-EFF 2026-10-04: CI green plus one real run reaching ACCEPTED).

## 1. Where things stand
- **Tooling.** `scripts/m7_corpus.py MANIFEST.json` freezes the cases a manifest names; `scripts/m7_grade.py
  MANIFEST RESULT FINDINGS KEYS OUT` grades. No case data is in the repo. The tree has the b14 version:
  findings auto-match on file plus line range (`line=null` rejected); the freeze writes a flat
  history-free tree under `out/<cid>/` (no single staged commit, no `repo/`/`run/` split, no repo-local
  config cleanup), and `key_path` must lie outside the corpus directory.
- **Dot task 20** (offline rehearsal, accepted 2026-10-04): 12 synthetic cases, no model calls; found five
  ways to game the tooling (vague findings score on file/line, duplicate credit, freeze shape, results not
  bound to base/diff, `line=null`).
- **Dot task 21** (grader fix, accepted with notes 2026-10-04): a candidate patch (both scripts, both
  tests, one doc section; 56 tests OK), committed nowhere. Notes: vendor names compared case-sensitively in
  `consensus`; a case's excluded status is not bound by the frozen manifest.
- **Dot task 21b** (fix both notes, normative doc wording, restore the lost `.md`-table and positive-CLI
  test coverage): dispatched, silent since 2026-10-04 10:27.
- **Paired review-start.** Window 1 needs the paired reviewer to start at EXEC review on the frozen diff
  with no author turn before it (design "Two metric windows"). Today's direct CLI (lifecycle off, which
  allows `--adversarial-gate off`) always starts with PLAN; it is no review-start. Scored paired runs
  therefore wait for the D-LG1 entry or an M7-specific review-start harness (whichever lands first; the
  D-LG1 design should say whether it covers §2's M7 mode).

## 2. Arms, pinned copies and symmetry
| | Legacy arm | Paired arm |
|---|---|---|
| Code | v2.10.0 release commit, pinned `~/paired-runs/review-loop-v2.10.0`, `claude --plugin-dir <pinned>` | the commit that ships the review-start, its own pinned copy |
| Window 1 | `/review-loop:execute --review-only --stop-after exec-round`, handsfree | review-start, `--max-exec-rounds 1` |
| Window 2 | `--stop-after before-polish`; harness cap: it watches the session file's exec round records and kills the process group when the 3rd exec verdict is not approve | review-start, `--max-exec-rounds 3` |
| Off | `adversarial_gate_skip_paths: ["**"]`, `skip_quality_polish: true` | `--adversarial-gate off --shadow off --polish-round off`, lifecycle off |
| Reviewer | `reviewer: subagent` = isolated `claude -p` via `scripts/run_claude_reviewer.py`, `reviewer_model: claude-opus-5-5` | `--reviewer-vendor claude --reviewer-model claude-opus-5-5` |
| Author | executor (Claude Agent, `executor_model: inherit`) under `claude --model claude-opus-5-5` | `--author-vendor claude --author-model claude-opus-5-5` |
| Test command | per case, given in `--description` | same command via `--test-command` (the CLI default `npm test` must never apply) |

- **Effort.** The legacy launcher passes no effort flag. Before freezing, find the Claude CLI's default
  effort for that call and set the paired `--reviewer-effort` (default `medium`) to the same value; do the
  same for the legacy executor and the paired `--author-effort` (default `medium`). If a value cannot be
  established, record the asymmetry in the manifest and report it. The author/executor model and both
  effort values go into the manifest.
- **Raw review text.** Legacy: the reviewer child's per-invocation
  `.review-loop/tmp/{session_id}-reviewer-{invocation_id}/result.txt` (the top-level
  `{session_id}-reviewer-result.txt` is overwritten by every call; the design's "Agent-tool return"
  predates the isolated launcher, erratum to be made with step 1); paired: the raw answer of the first EXEC
  review turn.
- **Workspace extras.** The legacy `.review-loop/config.md` (and the launcher's `.review-loop/tmp/`) enter
  the case repo after the tree hash is taken; `.review-loop/` is listed in the case repo's
  `.git/info/exclude`, so it stays out of the reviewed diff; the paired arm uses flags only.
- Both commits, the plugin directory hash, the rate table and the effort values go into the manifest
  before the first scored run. v2.10.0 still ships the legacy skills; the pinned copy must stay runnable
  until M7 is scored even if a later release drops legacy.

## 3. Isolation (per case and arm)
- Case directory `$TMPDIR/m7/<opaque-id>/` with `repo/` (`git archive <base>`, `git init`, one commit, `git
  apply --index`, repo-local config cleanup as in the design) and `run/`. Keys live outside the corpus root
  (as `m7_corpus.py` requires), `chmod 000` until grading.
- Per arm: its own `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, a scratch `HOME` holding only credentials, and
  `XDG_STATE_HOME` inside the case directory (the paired run root, FIELD-15, and its probe parent lock stay
  per case). Verify in the rehearsal that a fresh `CLAUDE_CONFIG_DIR` plus scratch `HOME` can log in on
  macOS (Keychain) and bills the subscription.
- Paired arm: the harness calls the coordinator CLI directly; it never drives the skill through a
  `claude -p` that ends its turn (FIELD-17). The legacy arm runs its skill under `claude -p
  --output-format stream-json` as the design requires.
- D-b1 transcript scan as in the design, including the legacy reviewer child's
  `.review-loop/tmp/*-reviewer-*/stream.jsonl`; D-b2 tool-version grep against both pinned commits before any scored run.
- Voiding (design D-b, D-d): voided plus excluded cases above 4 invalidate the run; more than 2
  infrastructure failures invalidate it; an arm's own failure (hold, crash, cap, no output) scores as
  MISS / `unresolved` and is never excluded.

## 4. Cases, measurements, grading
- **24 cases**: 12 archived (the 7 luna cases, re-checked against the luna bench, plus 5 self-audit-roadmap
  round-1 CRITICAL/MAJOR cases: 10 with blockers, 2 clean) and 12 synthetic (10 seeded, 2 clean); each
  blocker is categorised security-boundary, state-machine or ordinary logic.
- **96 arm-window runs**: 48 window-1 reviews; window 2 has 48–144 reviewer verdicts and 0–96 author fix
  turns. Arms alternate per case.
- **Quality**: recall per blocker category, with and without security-only findings, archived vs
  synthetic; FP blockers on the 4 clean cases and over all 24; rounds-to-approve and `unresolved`.
- **Grading**: grader 1 `claude-opus-5-5`, grader 2 Codex `gpt-6.1-sol` (vendor names normalised, Dot 21b);
  disagreements go to the supervisor, who records the third verdict. Graders write verdict records
  (HIT/PARTIAL/MISS per key blocker; FP or VALID-NOT-IN-KEY per unmatched finding; arm guess). Step 1 fixes
  `m7_grade`'s input contract to consume those records; the b14 file/line auto-match is not used for
  scoring.
- **2G cost**: tokens per role and phase, uncached / cached / output apart. Paired: `usage.json` per role
  and phase. Legacy: orchestrator from `claude -p --output-format stream-json` (`result.usage`, or summed
  per message for a run killed at the cap), reviewer from the launcher's usage records
  (`scripts/reviewer_usage.py aggregate`), executor (Agent subagent) checked for presence; a missing
  source is reported and the cost criterion is not gated (design "Metrics"). Median per-case cost <= 1.5x
  legacy; wall time reported, not gated.

## 5. Wall time and quota estimate (rough)
Basis: real-run `usage.md` tables (`.compass/results/2026-09-20_paired-session-spike/real-run/runs/`),
the legacy token audit (`2026-09-19_orchestrator-context-token-audit.json`), the luna bench; all outside
the repo.
- Paired reviewer turn 0.1–0.4M input (85–95 % cached), 0.3–8k output, 0.5–1.5 min; author fix turn
  0.35–0.8M, 2–9 min. Legacy Opus review 2–5 min; executor median $1.86 per round.
- Raw input: paired W1 24 x 0.1–0.4M = 2–10M; paired W2 24 x ~2 rounds x 0.5–1.2M = 25–60M; legacy W1
  24 x (15–25 orchestrator calls x 80–150k fresh-session context + one review) = 30–100M; legacy W2
  24 x ~2 rounds x 2–4M = 100–200M; grading 96 reads x ~0.2M = ~20M Claude and ~20M Codex.
- Claude total about 180–390M raw. Assuming 90 % cached and output about 1 % of input, the weighted
  load (cache x0.1, output x5, May estimate about 416M per week, ±30 %, older model) is about 10–23 % of
  one Claude week. Codex: grading only, about 5–10 % of its week (the 7-case luna bench moved it 1 %).
- Wall time: 20–50 min per arm-case, 48 arm-cases = 16–40 h serial plus 5–8 h grading; 2–3 cases in
  parallel = 6–20 h. Start only with at least 35 % of the Claude week left (2026-10-05 08:19: 73 % used);
  parallel runs share the 5-hour window, so drop to one at >= 80 % of it (global quota rule).

## 6. Who runs what
- **Dot** (no model calls in the arms): 21b; synthetic cases and keys; freeze to the design's shape and
  manifest hashing; D-b2 grep; the D-b1 scanner and tests; the finding normaliser and grader-record
  format; the legacy-side fake rehearsal. Candidate patches, reviewed and committed by lane B. Every Dot
  task message carries the two cross-repo reminders (global rule §10).
- **GitHub Actions** (a workflow does not exist in this tree yet; adding it is a dependency):
  `paired_session/test_m7_corpus.py` and `test_m7_grade.py` on macOS and Linux, and a freeze check over a
  committed sample manifest (`fetch-depth: 0` for the base SHAs, an explicit `out_root`). No provider
  credentials, so no scored run.
- **Local, owner-authorised**: every scored run and the model graders; lane B drives the harness, the
  supervisor watches quota and settles grader disagreements.

## 7. Order of work
1. Dot 21b lands; lane B reviews and commits Dot 21 + 21b, fixing the grader input contract (§4) and the
   freeze shape (§3); the design erratum on the legacy raw-text source goes in the same commit.
2. Corpus: 5 archived round-1 cases, the two luna facts re-checked, 10 seeded + 2 clean synthetic cases,
   keys, categories, per-case test commands and their result on the frozen tree.
3. D-b1 scanner and normaliser; legacy and collector fake rehearsal; authentication check (§3).
4. Optional unscored legacy pilot on 2–3 cases outside the corpus (for example Dot 20's synthetic
   cases) from the pinned v2.10.0 copy (tooling and cost check only).
5. Review-start lands (D-LG1 or an M7 harness); paired pinned copy; paired fake rehearsal of the
   first-review stop; effort values fixed; manifest with both commits frozen and hashed; D-b2 grep.
6. Owner authorisation for real-provider scored runs and a quota window (§5).
7. Scored runs: both arms, windows 1 and 2, alternating per case.
8. Grading, normalised findings, metrics, 2G cost table; report against the design's pass criteria. The
   retirement decision stays with the owner.

## 8. Dependencies and risks
- Hard: Dot 21b (step 1); the review-start with the §2 M7 mode (step 5); a CI workflow (§6); owner
  authorisation and quota (step 6).
- The two arms run different coordinator versions (legacy at v2.10.0, paired at the review-start commit);
  the manifest records both.
- One run per case and arm is descriptive, not significant (design "Pass criteria").
