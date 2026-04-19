# <Component / Flow / System> Design

**Status**: Draft | Active | Superseded by <design#anchor>
**Last updated**: 2026-04-19
**Owner**: <who to ask>

## Goals

## Non-goals

## Behavior spec

## Edge cases & error handling

## Interfaces / contracts

## Open questions

## References

<!-- 迁移自 README.md:292-317 via compass:adopt 于 2026-04-19 plan=e2439220c6bd -->
## Migrated — README.md:292-317

## Key Design Features

**Live Reports** — After every review round, the Orchestrator shows you what
the Reviewer found: CRITICAL issues, MINOR suggestions, and the verdict.
You see the value of the review loop in real time.

**Plan Conformance** — The Reviewer checks that the Executor's implementation
stays within the approved plan. Unauthorized design decisions are flagged as
CRITICAL even if the code is technically correct.

**Context File** — All loop state is persisted to
`.review-loop/sessions/{uuid}.md`. Both agents read it each round
for instant context. Session files are preserved permanently — the UUID
is printed in the delivery summary. To trace a bug back to a specific
review session, find the UUID in the delivery output and open the
corresponding `.review-loop/sessions/{uuid}.md` file.

**Soft Limits + Stuck Detection** — No hard cap on rounds. When the soft limit
is reached and CRITICALs remain, the Orchestrator asks whether to continue.
Stuck detection stops the loop if the same issue recurs 3 rounds without
progress.

**Quality Polish** — After the adversarial review loop approves, a suite of
specialized agents automatically runs static analysis, simplification, test
coverage, and comment checks. Configurable via `quality_focus` and
`skip_quality_polish`.
