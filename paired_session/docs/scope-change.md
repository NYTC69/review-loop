# Scope change: terminal run and successor (2C part 3)

> **Historical** (2C design, implemented as `note --scope-change` / `reject --scope-change`). Current behaviour: [paired_session/README.md](../README.md).

Status: design only. This replaces the stopped in-run PLAN reopen design in R12-2/R12-2a/R13-2. No code is enabled here.

## Command and durable transition

- `note --scope-change --text/--file` accepts idle non-DONE/non-terminal runs, including ordinary HOLD. `reject --scope-change` accepts DONE or R13-1 rejection-limit HOLD. Active/uncertain child, held lease, ACCEPTED, ABORTED or invalid note refuses. The exception only ends old work; it cannot accept that result.
- Read `--file` once; copy bytes into old evidence and save operator, time, source type and SHA-256, never its path in role input. Empty notes and note text containing a `reviewer-commands` block refuse. Workspace remains untouched.
- Resolved old `run_dir` is its run ID. Under both leases, write intent **first** with immutable note bytes/hash; replay uses these bytes even if source changes. Then write task/spec in old evidence and `status=ABORTED`, `abort_kind=scope-change`, spec hash/path and provenance. Report completed work, open findings, superseded approval and spec. With intent, only identical replay/read-only inspection is allowed; replay completes ABORTED, never old roles. Other requests refuse. `hold()` and every mutation (run, all resume forms, abort, probe, accept, reject, note) treat ABORTED as absorbing; test abort→resume.
- Spec records old ID/base_commit, original workitem path/hash, same workspace, effective task text/hash (original + labelled note), note hash, depth, unused sibling run dir and exact argv. Effective task file is successor `--workitem`; original path/hash preserve the logical item. Before intent, run the **same** fresh-input scan on the whole effective task using successor path spellings; forbidden tokens refuse with their match. Render shell-quoted `Probe:` and `Start: bin/paired-session run` commands, **both** with `--supersedes <old-run-dir>` and saved role/test/fake-CLI settings; no `--skip-probe` or auto-start. Scope-change reject bypasses the old probe gate because it dispatches no provider. Unsafe argv refuses before abort.
- `--supersedes` verifies parent ABORTED/spec hash, workspace/original item hash, effective task bytes/hash and exact target run dir on **every** probe/run/resume/accept. Under parent lease, the first probe atomically claims this sole child and saves depth=parent+1; Start accepts only that claimed child, never an unrelated pre-existing state. Fixed max=1, so depth-one scope change refuses. New run inherits parent `base_commit` for its complete diff, even across old commits; if base is not HEAD's ancestor, refuse. It still has fresh sessions, PLAN/review, gate, usage and budgets; no old verdict/finding confers approval.
- Successor PLAN author plans to keep/revert old EXEC edits in unchanged workspace. Note is ordinary task input for successor roles; their prompts contain only copied `context/workitem.md`, never the old evidence path. Old roles receive neither. Only successor reaches new DONE/ACCEPTED; its fresh rejection budget is bounded by one-child chain.

## Prior failure crosswalk

| Finding | New boundary |
|---|---|
| (a) PLAN REVISE coerced to APPROVE | Successor starts at ordinary fresh PLAN; no old PLAN verdict or normalization result is reused. |
| (b) stale limit flag enables accept | ABORTED is terminal; successor starts with clean state, and chain depth is verified from its parent. |
| (c) limit HOLD accepts an unapproved change | Over-depth request refuses before any transition; old state stays as it was and no note is approved. |
| (d) raw note reaches fresh roles of same run | No old-role dispatch after intent; only successor roles see the note as task input. |
| (e) EXEC blockers deadlock PLAN | Old blockers stay in its final report; successor PLAN has a new ledger and reviews retained workspace edits from scratch. |
| (f) gate findings deadlock PLAN APPROVE | Old gate/ledger are terminal evidence; the successor uses its own gate after fresh PLAN. |
| (g) leak HOLD recurs from coordinator files | No note is injected into old context/delta; successor's task input is allowed to contain it. |
| (h) R13-1 rejection-limit HOLD | `reject --scope-change` is allowed only through the special terminal transition; `accept` and `resume` of ABORTED refuse. Its rejection budget is not silently raised. |

In-run PLAN reopen, blocker migration and `gate_ran` reset are out of scope. The successor must independently review all retained edits; old findings are reported, not silently closed.
