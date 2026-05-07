# Changelog

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
