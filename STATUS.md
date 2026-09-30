# Status

**Last updated**: 2026-09-23

## Current Branch

- Branch: `audit/paired-session-next-batch`
- Task: integrate run-derived paired-session fixes as an experimental candidate while preserving the legacy review-loop
- Status: in progress; no live paired-session run has been started from this candidate

## Next Steps

- **Priority 1 — productize paired-session and deprecate legacy invocation modes:** finish stable CLI and operational contract (1A), package/install in both runtimes (1B), pass readiness and installed-Codex permission probe (1C), route the familiar entry to paired-session (1D), then deprecate old implicit calls while retaining an explicit legacy/control path (1E).
- **Priority 2 — protocol optimization after Workstream 1:** independently verify delivery-safety gates and effective author sandbox; then validate quota/preflight in a controlled run; implement operator feedback/acceptance (scope changes -> PLAN + independent review; in-scope clarification -> author); then idempotent resume, policy decisions, and token/cost measurements.
- No live paired-session run is authorized by the roadmap change. The protected `real-run/` directory remains untouched.

## Latest Verification

- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s paired_session -p 'test_real_coordinator.py' -q` — 81 passed
- `git diff --check` — pass
- The paired-session suite passes 81 tests; skill lint and Claude manifest validation pass. Opus 5.5 found actionable lifecycle, model-default, and sandbox-temp findings; fixes are in, awaiting final review. No review-loop orchestration is used.

## Notes

- This worktree contains a versioning candidate under `paired_session/`; the ignored spike source remains unchanged.
- The current tests use a fake CLI. The effective author permission probe has not been run against the installed Codex CLI.
- The existing W01-W05 delivery-controls changes remain isolated and uncommitted in `.worktrees/self-audit-batch-1`.
- The legacy Orchestrator/Executor/Reviewer workflow is still the current default. The plan moves the daily entry to paired-session after 1A–1C; the old implementation remains an explicit control path until all four replacement-gate criteria in `BACKLOG.md` pass.
