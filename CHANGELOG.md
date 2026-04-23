# Changelog

## 2026-04-23

- review-loop: Codex Stage 1 now follows the same downstream `exec -> polish -> docs -> security -> delivery` lifecycle as Claude Code instead of silently stopping at `exec`.
- Protocol docs, repo skills, plugin mirrors, README, and guide surfaces were aligned on the widened delivery gate, clean stop points, and the real `quality_focus` / `skip_quality_polish` semantics.
- Contract and smoke coverage were expanded for Codex reviewer routes, review-only routing, `skip_quality_polish`, and the missing `before-polish` / `before-security` stop seams.
- Delivery hygiene tightened: plugin metadata bumped to `2.6.18`, stale developer docs were refreshed, and `.gitignore` gained the security-preflight pattern coverage defined in `docs/protocol/execution.md`.
