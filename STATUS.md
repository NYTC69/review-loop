# Status

**Last updated**: 2026-10-08

## Current Branch

- Branch: `audit/paired-session-next-batch` (lane A).
- Released: v2.13.2. The paired-session coordinator is the only workflow; the legacy workflow was removed in
  v2.13.0-v2.13.2. macOS only.
- In progress: the v3.0.0 review (ADR-17). Lane A fixed the live docs (V3-FIX-1, -2, -4, -5, -6); lane B the
  historical docs and leftover code.

## Next Steps

- The 3.0.0 changes decided in ADR-17 (V1-V5, V8, V10, V11) and the release; see BACKLOG.md.

## Latest Verification

- 2026-10-08: `scripts/run-skill-lint` 0 FAIL; `python3 -m pytest -q tests` 211 passed; `git diff --check` clean.
  The full coordinator suite was not run locally in these units (host load); the release CI runs it.
