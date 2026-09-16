# Reduce review-loop orchestration overhead without weakening review

## Purpose and ownership

This document hands a focused review-loop improvement task to the Codex session
working in `/Users/yuanlei/3Cats/review-loop`. Codex should use it to investigate,
plan, and guide Claude's implementation in that repository.

The task is to make review-loop spend work in proportion to the risk and the
new information in a change. Independent review, meaningful verification, data
protection, and user authorization boundaries remain hard requirements.

This is a problem statement and acceptance frame, not approval for a particular
implementation. Inspect the current Claude and Codex protocols before choosing
the design.

## The problem

review-loop currently applies much of the same orchestration to changes with
very different risk. A small, local correction can cause agents to reread a
large accumulated session, rerun tests whose inputs did not change, repeat
review of already approved material, and traverse Executor, Reviewer, polish,
documentation, security, and adversarial stages again.

Each individual gate is defensible in isolation. The waste comes from treating
every write or round as if it invalidated all prior evidence. The workflow lacks
a first-class way to answer five questions:

1. What changed since the last verified state?
2. Which earlier evidence is still valid for the exact current content?
3. Which review or test claims were actually invalidated by that delta?
4. Does this task need a separate Executor, or only an independent Reviewer?
5. Is a reviewer concern realistic enough to justify implementation cost, or is
   it pushing the product toward over-engineering for a theoretical edge case?

The result is excess latency and token use, more opportunities for reviewers to
re-litigate settled decisions, and user interruptions that do not resolve a real
product or authority question. Adversarial review can also reward maximum
robustness in isolation instead of the smallest design that is robust for the
product's real operating conditions. This can make a disciplined workflow harder
to finish without adding corresponding defect-detection value.

## Evidence from real work

### 1. Approved requirements are repeatedly re-litigated

The poker-tools HU preflop session
`ca4a3d61-4afc-4386-8e79-70dd1b4339b8` reached a fifth planning round. Four
round-2 CRITICAL findings were changes to acceptance criteria that the user later
explicitly approved. Later rounds had to carry and restate the complete authority
record so the reviewer would stop treating those settled choices as unapproved
deviations.

The review was useful when it found real defects. The overhead came from making
the next reviewer reconstruct authority and resolved findings from a growing
historical transcript instead of receiving a compact set of current requirements,
binding decisions, unresolved issues, and the new delta.

### 2. A large session becomes the transport for current state

The poker-tools note-taker session
`53d4bada-4b42-4bf1-9570-ef06cae32d07` grew to more than 1,300 lines while
tracking plan revisions, user rulings, execution rounds, tests, a terminal gate,
polish, and documentation fixes. Much of that history is useful for audit, but
most of it is not needed as the default prompt for every later decision.

Current state and audit history serve different consumers. The workflow should
keep a small authoritative current-state packet and retain append-only history
for on-demand inspection.

### 3. Small follow-up writes can trigger disproportionate replay

`LEARNINGS.md` already records three review-loop releases in which prose-only or
metadata fixes caused the protocol's `any write -> full replay` rule to demand
Executor, Reviewer, polish, docs, and security again. The recorded cost was about
5-10 minutes and tens of thousands of tokens per replay, even when the write
touched no lint-pinned contract and the lint baseline remained unchanged.

The repository has already used a reviewer-only fast replay for that narrow
case. The broader design problem remains: evidence reuse and invalidation are
precedents handled by judgment, rather than an explicit workflow model.

### 4. Small mechanical follow-ups receive little benefit from full orchestration

The GTO Wizard extension 1.0 release and later directory migration motivated
this audit. Once behavior, fixtures, and release checks have been verified for
exact content, a mechanical move or tightly scoped follow-up should invalidate
only claims affected by changed paths, packaging, imports, manifests, or runtime
resolution. Repeating unrelated analysis and tests provides little new evidence.

This does not mean moves are automatically safe. It means the workflow must name
the affected contracts and rerun the checks that prove those contracts.

## Priority changes

Implement and evaluate the first four items before expanding the scope.

### P1. Content-bound evidence reuse

Represent review and test evidence as valid for a specific repository state or
content set. A later delta may reuse an earlier result only when the workflow can
show that the result's relevant inputs and assumptions are unchanged.

At minimum, the model must distinguish:

- evidence still valid for byte-identical content;
- evidence invalidated by changed files or dependencies;
- failed checks, which must never be reused as success;
- unresolved reviewer concerns, which remain open until directly resolved;
- environment-sensitive or manual checks, whose validity may require explicit
  freshness rules.

Rerun a check when its inputs changed, it failed, its environment guarantee
expired, a reviewer identified an unresolved concern, or the dependency mapping
is uncertain. Prefer rerunning a cheap relevant check over inventing a complex
proof of reuse.

The session record should say what was reused, the exact state it came from, and
why the current delta did not invalidate it. "It passed earlier" is insufficient.

### P2. Let the orchestrator implement small scoped changes

Do not require an Executor subagent merely because files will change. For a
small, well-bounded implementation, the main orchestrator may make the change
directly and then send the resulting diff to an independent Reviewer.

Use an Executor when decomposition, isolation, a large implementation surface,
or independent construction adds value. Keep it optional for a one-file fix,
mechanical edit, or reviewer-requested correction that the orchestrator can apply
and verify safely.

The author and Reviewer must remain independent for meaningful runtime or
externally visible changes. Removing a redundant Executor hop must not turn
self-review into the only review.

### P3. Give reviewers a compact current packet

Create a bounded reviewer input that contains the information needed to judge the
current state:

- current user intent and acceptance criteria;
- binding user decisions and authorization relevant to the diff;
- current diff or exact review target;
- contracts and invariants touched by the delta;
- relevant test and manual-check results, including reused-evidence provenance;
- unresolved findings and the executor's response to each;
- declared deviations, risks, and open questions.

Historical rounds and full session logs remain available by path. They should be
loaded when a current claim needs provenance, rather than copied into every
review prompt. A reviewer must be able to request more context without treating
absence of irrelevant history as a defect.

### P4. Prevent adversarial review from driving over-engineering

Adversarial review should find realistic failures, not maximize the number of
hypothetical failures. A concern may block delivery only when the reviewer gives
a concrete argument connecting it to the product:

- the trigger is reachable under supported or plausibly encountered conditions;
- the resulting impact matters to users, data, security, correctness, or
  operations;
- likelihood and impact together justify the implementation and maintenance cost;
- existing constraints, tests, upstream guarantees, or explicit product limits
  do not already exclude the scenario;
- the response is proportionate, with the smallest effective fix considered
  before a general framework or defensive subsystem.

Every CRITICAL finding should state the trigger, reachability evidence, concrete
impact, expected frequency or likelihood, and why a cheaper response is
insufficient. A reviewer should lower the severity, record a non-blocking
follow-up, or omit the finding when the scenario is unreachable, depends on a
chain of unsupported assumptions, has negligible impact, or would require large
complexity for extremely rare benefit.

Low probability alone does not dismiss a risk. A rare but realistically reachable
path to credential exposure, irreversible data loss, authorization bypass, or
similarly severe harm can remain blocking. The point is to combine likelihood,
impact, reachability, and fix cost instead of treating theoretical possibility as
enough.

The workflow should explicitly allow the orchestrator to reject or downgrade an
over-engineering finding with a written rationale. This is normal triage, not a
failure of adversarial review. Escalate to the user only when the remaining choice
is genuinely a product, risk-tolerance, or scope decision.

## Follow-up changes to consider after P1-P4

### Classify plan deviations by impact

A deviation is not automatically CRITICAL. Classify it using user intent,
observable behavior, safety, data integrity, authorization, and verification.
A simpler implementation that satisfies the authorized criteria may be accepted
and documented. A change that silently alters scope, weakens a safety invariant,
or contradicts a binding user decision remains blocking.

### Load protocol by active stage

Agents should read the protocol needed for the current stage and follow direct
references as required. Do not make every agent ingest planning, execution,
polish, documentation, security, and delivery instructions up front when only one
stage is active.

### Generalize safe replay without making it vague

Extend the prose-only fast-replay precedent into explicit invalidation rules.
Avoid a broad "small changes can skip checks" escape hatch. The workflow needs
positive proof of which evidence survives the delta.

### Scope security review to the delivery

Security review should inspect the changed surface, its reachable dependencies,
and realistic interactions. Record unrelated findings for later work when useful;
do not turn every delivery into an open-ended cleanup of the repository.

### Make extra adversarial review risk-based

Independent review remains standard. Additional terminal adversarial review
should depend on blast radius, novelty, sensitive data or authorization paths,
and unresolved uncertainty. The repository's existing single-pass convergence
rule must remain: a terminal gate runs once per convergence, and its findings
return to the normal fix/review loop rather than recursively spawning new gates.

## Required invariants

An optimization is unacceptable if it weakens any of these properties:

- A failed or stale test cannot be represented as a current pass.
- Changed runtime behavior receives independent review.
- Auth, permissions, destructive actions, external writes, secrets, and user data
  retain their existing authorization and safety gates.
- Unrelated dirty work is preserved and excluded from the task.
- Reviewer findings remain traceable to the exact diff they reviewed.
- User decisions are recorded as binding input and are not silently overridden.
- Unknown dependency impact causes a relevant rerun or explicit escalation, not
  optimistic evidence reuse.
- A read-only or plan-only request cannot become implementation.

## Suggested work sequence for Codex and Claude

1. Codex maps the current orchestration paths in the Claude skills, Codex skills,
   and shared protocols. Identify exactly where full context, Executor dispatch,
   test replay, stage replay, and adversarial severity decisions are mandatory
   today.
2. Codex defines a minimal evidence/invalidation model, compact reviewer packet,
   and reachability/impact/cost rubric. Use examples from the sessions above to
   test the model before editing skills.
3. Claude implements P1-P4 in the smallest coherent slice. Keep Claude-side and
   Codex-side behavior aligned where both runtimes expose the same contract.
4. Codex independently reviews the implementation against the invariants in this
   document. Use Opus for Claude Code reviewer invocations unless Yuan requests a
   different model.
5. Only after P1-P4 are measured should the team decide which follow-up changes
   deserve implementation.

Do not combine this work with Codex entry unification, Compass/MemPalace changes,
plugin-cache cleanup, or poker-tools feature work.

## Evaluation

Evaluate the old and new workflow on representative fixtures or dry runs:

1. a read-only review;
2. a one-file bug fix with focused tests;
3. a cross-file runtime change;
4. a documentation-only correction after approval;
5. a mechanical directory move with path/packaging checks;
6. a reviewer-proposed defense against an unreachable or extremely unlikely,
   low-impact scenario whose robust fix would add substantial complexity;
7. a rare but reachable security or irreversible-data-loss scenario;
8. a release-sized change with meaningful independent review.

Record at least:

- user pauses that did not require a product or authority decision;
- repeated file/protocol reads;
- repeated review of unchanged content;
- repeated tests and whether their inputs changed;
- elapsed time and model/token cost where available;
- blocking findings rejected or downgraded for weak reachability, low combined
  risk, or disproportionate fix cost;
- complexity added solely to satisfy theoretical reviewer concerns;
- defects found, defects missed, and stale evidence incorrectly reused.

The target is less duplicated work on routine cases, rejection of over-engineered
defenses in case 6, continued blocking treatment for justified severe risk in
case 7, and no reduction in defect detection or safety for release-sized work.
Do not claim improvement from token counts alone.

## Completion criteria

This task is complete when:

- P1-P4 have explicit protocol semantics shared by the affected runtime paths;
- contract tests or workflow fixtures cover evidence invalidation, direct small
  implementation with independent review, compact reviewer packets, and
  proportional triage of adversarial findings;
- the eight evaluation cases have before/after evidence;
- no required invariant above is weakened;
- documentation explains when the workflow reruns work and when it reuses it;
- remaining follow-ups are separately prioritized rather than folded into the
  initial implementation.

The existing P2 item in `BACKLOG.md`, "Reduce review-loop orchestration overhead
while preserving independent review and safety," is the tracking entry for this
work.
