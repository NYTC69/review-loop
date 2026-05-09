# Smoke test cases

Each `.json` file in this directory defines one smoke case. The runner is
`scripts/run-skill-smoke`; assertion mapping lives in
`tests/skills/contracts/assertion-mapping.json`.

## `execution_policy`

- **`best_effort`** — the case drives a real `claude -p` (or `codex exec`)
  invocation. Real LLM calls are sensitive to model availability, network
  latency, and per-call timeout budgets, so these cases may legitimately
  FAIL even when the underlying skill is correct.

  The smoke runner reports `best-effort smoke timed out` when the
  invocation exceeds `setup.timeout_seconds` (runner default 300s,
  per-case override common — the regression case below overrides to
  600s) and falls back to seeding the synthetic fixture. Subsequent
  assertions then fail against the seeded fixture rather than the
  (unproduced) real output.

  These FAILs are advisory — they do not block delivery.

- **`strict`** (default if unset) — the case must PASS or the suite is
  considered broken.

## Known-flaky `best_effort` cases

The following cases are documented as intentionally allowed to FAIL on
machines where `claude -p` cannot converge within the case's
`setup.timeout_seconds` budget (typical override 240–600s, per case).
The underlying skill behavior is verified by lint contracts in
`tests/skills/contracts/review-loop.json` and unit tests in
`tests/run_skill_lint_test.py` / `tests/replay_sessions_test.py` /
`tests/review_verification_test.py`; smoke is supplementary.

- `review-loop.regression.smoke.claude` — drives the `review-loop` skill
  end-to-end on a no-op-friendly prompt. Currently FAILs because the
  600s budget is insufficient on this machine for the full
  plan→exec→polish→docs→security lifecycle to produce a real
  `session-final.md`. The runner falls back to the seeded synthetic
  v2.6.0 fixture, which has `entry_point: plan` (the seed's value) rather
  than the expected `entry_point: review-loop` from a real run, so the
  three assertions
  (`execution_or_noop_round_recorded`, `entry_point_review_loop`,
  `completed_stages_ordered_exec_polish_docs_security`) fail against the
  seed. Future paths: either (a) raise the timeout when the runtime
  budget allows, or (b) move the case to a non-real-LLM smoke harness.

- `execute.stop-after-before-polish.smoke.claude` /
  `execute.stop-after-polish.smoke.claude` /
  `execute.from-plan.smoke.claude` /
  `plan.fresh.smoke.claude` /
  `review-loop.noop.claude-default` /
  `review-loop.noop.codex-fallback` /
  `review-loop.review-only.codex-fallback` /
  `review-loop.skip-quality-polish.codex-fallback` — same root cause:
  per-case `setup.timeout_seconds` budget vs real `claude -p` /
  `codex exec` convergence time on this machine.

The strict (non-`best_effort`) cases must pass cleanly. As of v2.7.2:

- `guide.shared-state.codex` — strict (no `execution_policy` field →
  default), PASS.

`best_effort` cases that currently PASS (no policy guarantee, but
empirically green): `execute.session-resume.smoke.claude` (timeout
240s).

## Adding a new smoke case

Set `execution_policy: "strict"` only if the case can run without an
external LLM (e.g. checks against a pre-seeded fixture). For cases that
shell out to `claude -p` or `codex exec`, use `best_effort` and add an
entry to the "known-flaky" list above.
