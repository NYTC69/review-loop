<!--
Synthetic evidence-ledger fixture — stable across machines; SHA placeholders
are intentional and never resolved against live git state.

Shape: a session that carries the two Slice 1 canonical sections:
- `## Current Review Packet` directly after `## Approved Plan`
- `## Evidence Ledger` (one fenced JSON block) directly before
  `## Session Metadata`, which stays last
plus the 13-column `## Timing Log` header. `completed_stages` is the value
`scripts/evidence_ledger.py check` derives from the ledger below.
-->

## Problem Description
Add a synthetic session fixture that carries the evidence ledger and the current review packet.

## Context
- The fixture mirrors `docs/protocol/session-file.md` §Canonical sections (12 sections).
- Snapshot and blob identities are placeholders; the ledger is not evaluated against live git state.

## Acceptance Criteria
- The session file contains all twelve canonical sections in the required order.
- `## Current Review Packet` carries an `### Attributable Delta` table.
- `## Evidence Ledger` is a single fenced JSON block placed directly before `## Session Metadata`.

## Current Phase
execution

## Approved Plan

- Source: reviewer-approved

### Solution Plan: Evidence-ledger fixture
1. Land the fixture at `tests/skills/fixtures/sessions/evidence-ledger-session.md`.
2. Reference it from the lint contracts as the ledger + packet shape.

## Current Review Packet

### Intent and acceptance criteria
Land the fixture described in `## Approved Plan`; acceptance criteria as listed in `## Acceptance Criteria`.

### Binding decisions and authorization
- Evidence writes go through `scripts/evidence_ledger.py` only.
- No commit, push, or version bump in this session.

### Exact delta
```
tests/skills/fixtures/sessions/evidence-ledger-session.md | 1 +
```

### Attributable Delta
| snapshot pre | snapshot post | path | pre_blob | post_blob | pre_mode | post_mode |
|---|---|---|---|---|---|---|
| 0 | 1 | `tests/skills/fixtures/sessions/evidence-ledger-session.md` | <absent> | 1111111111111111111111111111111111111111 | <absent> | 100644 |

Materialize: `python3 scripts/evidence_ledger.py delta --session <uuid> --pre 0 --post 1`.

### Touched contracts / invariants
- `tests/skills/contracts/review-loop.json` (fixture needles)

### Evidence
| id | claim | disposition | result | provenance | why | valid | reason |
|---|---|---|---|---|---|---|---|
| 1 | `reviewer_approve:tests/skills/fixtures/sessions/evidence-ledger-session.md` | executed | PASS | fresh | — | yes | byte-identical |
| 2 | `gate:tests/skills/fixtures/sessions/evidence-ledger-session.md` | controlled-skip | — | fresh | — | yes | controlled-skip |

### Unresolved findings and author response
- None.

### Declared deviations
- None.

### Risks / open questions
- None.

### Route Facts
| fact | value | rationale |
|---|---|---|
| small_bounded_scope | true | one new fixture file |
| unambiguous_requirements | true | acceptance criteria name the exact sections |
| known_dependency_impact | uncertain | lint contract closure not yet declared for the new fixture |
| no_useful_decomposition | true | single file |
| safe_verification | true | `bash scripts/run-skill-lint` covers it |
| dirty_work_preserved | true | no overlap with `base_dirty` |
| auth_or_authorization | false | none |
| permissions | false | none |
| destructive_operation | false | none |
| irreversible_data_change | false | none |
| secrets | false | none |
| external_writes | false | none |
| migrations | false | none |
| broad_api_or_architecture | false | none |
| large_surface | false | one file |

### Author route
executor

## Review History

### Execution Round 1
- Executor backend: `general-purpose` (inlined)
- Executor result: fixture created.
- Reviewer backend: `codex`
- Reviewer verdict: `APPROVE`
- Reviewer issues:
  - None.
- Step 3.4: `adversarial-gate: SKIP reason=skipped-by-config`

## Files Changed
- `tests/skills/fixtures/sessions/evidence-ledger-session.md`

## Key Related Files
- `docs/protocol/session-file.md`
- `scripts/evidence_ledger.py`

## Timing Log
| Phase | Round | Role | Duration | Dispatch | Reuse | Reads | Unchanged | Tests | Pause | Model | Tokens | Cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| execution | 1 | executor | 12s | executor:1 reviewer:0 gate:0 | N/A | N/A | N/A | N/A | none | N/A | N/A | N/A |
| execution | 1 | reviewer (codex) | 8s | executor:0 reviewer:1 gate:1 | reused:0 rerun:2 | 0/0 | 0 | rerun (inputs_changed: true) | none | N/A | N/A | N/A |

## Evidence Ledger

```json
{
  "schema": 1,
  "scope": [],
  "snapshots": [
    {
      "n": 0,
      "ref": "refs/review-loop/00000000-0000-0000-0000-000000000000/snap/0",
      "commit": "0000000000000000000000000000000000000000",
      "tree": "0000000000000000000000000000000000000000",
      "head": "0000000000000000000000000000000000000000",
      "recorded_at": "2026-09-15T00:00:00+00:00",
      "label": "baseline",
      "untracked": [],
      "deleted": [],
      "renames": []
    },
    {
      "n": 1,
      "ref": "refs/review-loop/00000000-0000-0000-0000-000000000000/snap/1",
      "commit": "1111111111111111111111111111111111111111",
      "tree": "1111111111111111111111111111111111111111",
      "head": "0000000000000000000000000000000000000000",
      "recorded_at": "2026-09-15T00:01:00+00:00",
      "label": "round 1 post-executor",
      "untracked": ["tests/skills/fixtures/sessions/evidence-ledger-session.md"],
      "deleted": [],
      "renames": []
    }
  ],
  "records": [
    {
      "id": 1,
      "claim_id": "reviewer_approve:tests/skills/fixtures/sessions/evidence-ledger-session.md",
      "check": "reviewer_approve",
      "scope": "tests/skills/fixtures/sessions/evidence-ledger-session.md",
      "stage": "exec",
      "disposition": "executed",
      "result": "PASS",
      "head": "0000000000000000000000000000000000000000",
      "snapshot": 1,
      "inputs": {"tests/skills/fixtures/sessions/evidence-ledger-session.md": "<untracked:100644:1111111111111111111111111111111111111111>"},
      "deps": {},
      "selectors": [],
      "closure": "declared",
      "env": null,
      "freshness": null,
      "requires_freshness": false,
      "assumptions": [],
      "unresolved": [],
      "provenance": "fresh",
      "why": null,
      "author_route": "executor",
      "supersedes": [],
      "recorded_at": "2026-09-15T00:02:00+00:00",
      "superseded_by": null
    },
    {
      "id": 2,
      "claim_id": "gate:tests/skills/fixtures/sessions/evidence-ledger-session.md",
      "check": "gate",
      "scope": "tests/skills/fixtures/sessions/evidence-ledger-session.md",
      "stage": "exec",
      "disposition": "controlled-skip",
      "reason": "adversarial-gate: SKIP reason=skipped-by-config",
      "head": "0000000000000000000000000000000000000000",
      "snapshot": 1,
      "inputs": {"tests/skills/fixtures/sessions/evidence-ledger-session.md": "<untracked:100644:1111111111111111111111111111111111111111>"},
      "deps": {},
      "selectors": [],
      "closure": "declared",
      "env": null,
      "freshness": null,
      "requires_freshness": false,
      "assumptions": [],
      "unresolved": [],
      "provenance": "fresh",
      "why": null,
      "author_route": "executor",
      "supersedes": [],
      "recorded_at": "2026-09-15T00:02:30+00:00",
      "superseded_by": null
    }
  ]
}
```

## Session Metadata
- entry_point: plan
- plan_source: reviewer-approved
- base_head: 0000000000000000000000000000000000000000
- base_dirty: {}
- last_verified_head: 0000000000000000000000000000000000000000
- last_verified_dirty: {}
- session_commits: []
- completed_stages: [exec]
- delivery_blocked_by: null
