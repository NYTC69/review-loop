# Changelog

## 2026-05-08

### v2.6.30

- 8 new tests close 2 P3 backlog items in a bundled delivery (per v2.6.28 controlled-deviation precedent — pure-test, mutually independent, no shared mutation surface): `RunParserHelperContractTest` (1 method, P3 [3b] — `tests/replay_sessions_test.py::run_parser` helper hardening: replaces silent `parsed = None` JSONDecodeError fallback with `AssertionError` carrying subprocess stdout/stderr/returncode, so a parser regression surfaces a clean failure at the assertion site instead of a confusing downstream `TypeError`); `SecondTierCoverageTest` extended with 4 methods (P3 [4] gaps 1, 2 negative + positive, 5 — `bt` quoted regex branch isolated; secondary regex `BARE_REVIEW_LOOP_RE` left-boundary current behavior pinned (negative + positive); `render_text` `>50`-char path-truncation branch); `KindContainsTest` extended with 3 methods (P3 [4] gaps 3, 4 — case-sensitivity invariant pin (uppercase needle matches uppercase, fails on lowercase); empty-needle rejected at contract-load time by `require_fields`). `test_glob_is_single_level_not_recursive` augmented with positive existence assertion (Gap 6). No script-under-test, contract-loader, or lint contract JSON edits. Test count 67 → 75. Lint baseline 288 case-level PASS / 0 FAIL preserved.
- Closes BACKLOG P3 [3b] (`run_parser` helper hardening) and P3 [4] (six third-tier coverage gaps).

### v2.6.28

- 10 new tests close 2 P3 backlog items: `KindContainsTest` (3 methods, isolates `kind: contains` lint mechanic from integration smoke) and `SecondTierCoverageTest` (7 methods covering 6 gaps — gap 1 split into sq + dq quote branches; plus secondary-regex right-boundary, errors=replace UTF-8 decode, *.md non-recursive glob, anomaly_values set-dedup, --text rendering pinning). No script-under-test changes. Test count 57 → 67.
- Closes BACKLOG P3 (KindContainsTest unit class + second-tier replay_sessions coverage).

## 2026-05-07

### v2.6.26

- 15 new tests in `tests/replay_sessions_test.py` close 7 MEDIUM coverage gaps from pr-test-analyzer's v2.6.25 quality-polish pass — `--root` non-directory / non-existent exit-code-2 path, multi-file aggregation, empty-directory contract, same-value-multi-line counts, `anomaly_sites` line-number fidelity, JSON `sort_keys` / per-file `mtime` ISO 8601 invariants, plus in-process `ScanLineUnitTest` and `BuildReportUnitTest` covering `scan_line` / `build_report` directly. No parser change. Test count 14 → 29.
- Closes BACKLOG P3 (replay_sessions test plumbing-edge gaps).

## 2026-05-07

- review-loop v2.6.25: new `scripts/replay_sessions.py` — stdlib-only post-hoc audit channel that walks `.review-loop/sessions/*.md`, enumerates every `subagent_type` value per file, and flags `review-loop:*` occurrences as anomalies. Uses an anchored primary regex (`\bsubagent_type:`) plus a closed-set bare-form secondary regex over the 12 known agent names, with span-overlap dedup so a single occurrence isn't double-counted. Emits JSON by default (`--text` for a human table); exit 1 on anomaly, 0 otherwise (`--exit-zero` overrides). Complements the existing static lint contract for `SKILL.md` / protocol-doc files. Backed by 14 unittest cases (4 acceptance + 7 corpus-grounded with verbatim file:line citations + 1 dedup + 2 CLI).
- Closes BACKLOG P2 (session-replay parser).

## 2026-05-07

- review-loop v2.6.24: per-site `contains` companion assertions for 6 codex `pattern_requires_adjacent` stop-and-surface anchors in `tests/skills/contracts/review-loop.json`. Mirrors the existing `codex_execute_git_diff_failure_present`/`_lock_release` template across plan/SKILL.md and execute/SKILL.md so any future removal of an anchor needle FAILs lint instead of silently passing.
- Lint baseline 272 → 278 case-level PASS / 0 FAIL; unit tests 28/28 OK.
- Closes BACKLOG P3 (per-site contains companion for 6 stop-and-surface anchors). Claude side remains out-of-scope per the v2.6.23 mapping (no Claude analogue; SKILLs delegate to `docs/protocol/planning.md §Reviewer dispatch`).

## 2026-04-23

- review-loop: Codex Stage 1 now follows the same downstream `exec -> polish -> docs -> security -> delivery` lifecycle as Claude Code instead of silently stopping at `exec`.
- Protocol docs, repo skills, plugin mirrors, README, and guide surfaces were aligned on the widened delivery gate, clean stop points, and the real `quality_focus` / `skip_quality_polish` semantics.
- Contract and smoke coverage were expanded for Codex reviewer routes, review-only routing, `skip_quality_polish`, and the missing `before-polish` / `before-security` stop seams.
- Delivery hygiene tightened: plugin metadata bumped to `2.6.18`, stale developer docs were refreshed, and `.gitignore` gained the security-preflight pattern coverage defined in `docs/protocol/execution.md`.
