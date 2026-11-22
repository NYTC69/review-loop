# Scenario matrix

For a delivery work item, put a `## Scenario matrix` section in `plan.md`.
Use a subsection or a label for each dimension. One dimension can have several
rows. Write each row as `scenario | expected behavior | test id`, in a Markdown
table or a bullet. Use `N/A: <one-line reason>` for an inapplicable dimension.

Use the full dimension name or a complete alias for a plain or bulleted label
(for example, `Dependency failure:` or `Dependencies:`). A heading, bold label,
or numbered label can also use a longer phrase containing the name or alias.
When a row starts on the label line, use the full name or a complete alias
regardless of markup. Prose such as `Dependency note: none` leaves the current
dimension in effect.

1. **Input boundaries** — limits, empty values, and invalid inputs.
2. **Dependency failure** — errors, timeouts, and unavailable dependencies.
3. **Repetition and concurrency** — retries, duplicates, and simultaneous operations.
4. **Ordering and state transitions** — sequencing, invalid transitions, and stale state.
5. **Partial failure and recovery** — interrupted work, cleanup, and recovery.
6. **Permissions and identity** — access, identity, and ownership.
7. **Compatibility** — existing data, callers, and supported environments.

Include a row with a test only when its failure would block delivery because of
correctness, data loss, security, or money. Other scenarios may be BACKLOG
notes. Most dimensions may be N/A for a small change. For a trivial change
(docs-only, rename, or pure config/text), use only `Trivial: <reason>` in the
section. Domain-specific extensions are future BACKLOG work.

PLAN review checks that all dimensions are addressed and all blocking-class
scenarios are covered. On a successful `run --fast-plan` handoff, the coordinator
validates the first draft's matrix structure before entering EXEC. Each EXEC
review and the fresh shadow check that the implementation handles every row and
that its named test exists and passes.

The gate makes one verdict and audits coverage and gaps. An unmet acceptance
criterion or a blocking-class defect is grounds for FAIL, including defects
outside the written criteria. The gate checks passing evidence for each row,
whether a trivial exemption is justified, and any blocking-class gap it spots.
The per-round reviewer and fresh shadow own robustness and corner-case depth.
Non-blocking corner cases go to BACKLOG.

When the gate finds a missing row, add the supplemental row alongside its test
(for example, in a test description or adjacent test documentation). The
EXEC reviewer verifies the row, implementation, and passing test through the
existing gate FAIL repair flow. The gate runs once.

Review-only, code-quality-loop, and review-pr review an existing change and
have no approved plan. The matrix requirement and audit are skipped on those
routes.
