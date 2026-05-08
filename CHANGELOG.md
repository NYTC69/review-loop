# Changelog

## 2026-05-08

### v2.6.33

- Delivery Summary language pinned to 中文 (Simplified Chinese) at the protocol SSOT (`docs/protocol/execution.md` §Step 4 — Delivery #2 / #3). Section headings, prose, and prose-style field values render in 中文; ASCII tokens (file paths, identifiers, SHAs, CLI flags, model names, status enums such as `APPROVE` / `CRITICAL`) stay in their original form. Rule is runtime-agnostic — both Claude Code and Codex Stage 1 inherit via existing "Per `docs/protocol/execution.md` §Step 4 — Delivery" references in `skills/review-loop/SKILL.md`, `skills/execute/SKILL.md`, and `.agents/skills/execute/SKILL.md` (no per-runtime mirror to avoid double-maintenance). The `docs_file` appended copy preserves the same 中文 rendering as the terminal summary. Lint baseline 288 case-level PASS / 0 FAIL preserved.

### v2.6.32

- B3 (`tool_use_min_count`) tightened from `min: 0` (vacuous-pass) to `min: 1` (real-catch) on 7 truncating smoke fixtures via per-fixture `setup.timeout_seconds` bumps. Branch B (linear scale-up) selected after live single-fixture re-measurement at v2.6.31: `execute.session-resume.smoke.claude` failed at 180s (returncode -15 / status skip) and passed at 240s (`status: pass`, 1 Agent event with `subagent_type: general-purpose`). Locked tier values: NEAR_DISPATCH ×3 → 240s (session-resume, stop-after-before-security, stop-after-polish); IN_DOC_RECON ×3 → 360s (from-plan, review-only, stop-after-before-polish); full-pipeline → 600s (review-loop.regression). Each fixture's B3 override flipped `min: 0 → 1` and `_comment` refreshed to `min: 1 enforced at <Ns> per ADR-4` atomically with the timeout bump in the same Edit. `plan.fresh.smoke.claude` is unchanged (control fixture; already passes B3 with implicit shared `min: 1`). `tests/skills/contracts/assertion-mapping.json` shared default unchanged (AC-5). Recorded as ADR-4 (extends ADR-2; ADR-2 not mutated, append-only). The pass/fail criterion under which Branch B was locked is `meta.status == "pass"` AND ≥1 Agent event with `subagent_type: general-purpose`, NOT literal `returncode == 0`: under `execution_policy: best_effort` the runner is by-design permitted to return SIGTERM at the timeout cap (`returncode == -15`) while still stamping `meta.status="pass"` if assertions hold on the captured partial event stream (the `timed_out_with_passing_state` gate at `scripts/run-skill-smoke` line 987). 240s/360s/600s values are extrapolated from the spike's event-rate model and validated empirically only at the NEAR_DISPATCH tier (single-probe scoping per HANDOFF Codex-hang practice); IN_DOC_RECON 360s and full-pipeline 600s remain best-guess until a future re-measurement falsifies them. Worst-case CI wall time approximately 41 minutes per Branch B.
- Closes BACKLOG P3-2 (B3 truncation tightening) — see ADR-4.

### v2.6.31

- `scripts/replay_sessions.py` error-paths hardening — sub-scope (a), genuine plan-locked contract change. `scan_file` now narrowly catches `OSError` (covers `PermissionError`, `FileNotFoundError` race-vs-glob, `IsADirectoryError`, transient FS) at the `read_text` + `stat` call site, writes one stderr line `replay_sessions: unreadable file: <path>: <reason>`, and returns `None`. `build_report` skips `None` records and surfaces the count under a new fourth summary key `summary["unreadable_files"]`. `main` adds a distinct exit code `3` (after `--exit-zero` short-circuit, before anomaly exit-1) so I/O failures aren't conflated with anomaly-detected exit `1`. `render_text` summary footer appends `unreadable=N`. Locked by new `UnreadableFileTest` class with 6 in-process methods using `unittest.mock.patch.object(Path, "read_text", autospec=True, side_effect=...)` plus a `_selective_read_text` helper: exit-3 alone, exit-3 outranks exit-1 (Q2 pin), `--exit-zero` suppresses exit-3 (Q3 pin), unreadable files skipped from `report["files"]` (Q4 pin), stderr line format + glob-sort order (AC-3 pin), `FileNotFoundError` race falls through `OSError` catch (AC-1 enumeration pin). Three pre-existing summary-shape assertions updated explicitly (key list, empty-directory dict + length). Test count 75 → 81. Lint baseline 288 case-level PASS / 0 FAIL preserved. No downstream consumer impact: only `tests/replay_sessions_test.py` references `summary` keys / exit codes; no CI / smoke / docs / contract consumers.
- Closes BACKLOG P3 (`scripts/replay_sessions.py` error-paths sub-scope (a)) — completing the silent-failure-hunter HIGH follow-up filed during v2.6.25 polish (sub-scope (b) shipped in v2.6.30).

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
