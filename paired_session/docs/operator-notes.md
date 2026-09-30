# HOLD-only operator notes (2C part 1, design step A)

Status: design only. `note --text/--file` supplies an **in-scope clarification** to the next author turn. It neither changes the approved plan nor authorizes new scope. R12-1 R3 findings and the R14-2 plan define the boundaries below; a scope change requires the separate `--scope-change` successor-run path, which is not enabled by this document.

| Run state / next role | `note` result | Saved state and evidence | Resume |
|---|---|---|---|
| HOLD / author in PLAN or EXEC, idle and with remaining author-round/invocation budget | Accept one pending note; no provider call | Operator/time, target phase, SHA-256, evidence path, pending status; no raw text in `state.json` | Next author prompt gets the labelled note once; READY receipt records ID/hash and marks delivered |
| HOLD / author, pending note already exists | Replace it atomically | Keep immutable old and new evidence; record both hashes and `replaced_by`; only new ID remains pending | Next author gets only the new text |
| HOLD / reviewer or gate | Refuse: `run is waiting for <role>; resume first, or use --scope-change` | No state/evidence mutation | Existing role resumes under normal rules |
| HOLD / author with `active` or `uncertain_active` | Refuse until recovery resolves the child | No mutation | Prevents delivery to an overlapping turn |
| HOLD / author in POLISH or with exhausted saved round/invocation budget | Refuse as undeliverable | No mutation | Existing HOLD remains |
| HOLD / `terminal_hold_kind=rejection_limit` | Refuse: `accept, abort or use --scope-change` | Terminal marker unchanged | No author dispatch |
| ACTIVE (PLAN/EXEC/POLISH), DONE, ACCEPTED, ABORTED, or missing run | Refuse; DONE says `use reject` | No run creation or mutation | Unchanged |

Before acquiring leases or creating a run dir, refuse `note` if `state.json` is missing. Read `--file` bytes once into run-owned evidence; state stores only SHA-256/evidence path, and no role receives the source path. Refuse a source under workspace or run dir after realpath/symlink resolution. `--text` uses the same evidence path. Empty text and both/neither source flags refuse.

Inject only into the **next author prompt in its recorded phase**. PLAN says “do not expand the work item”; EXEC says “do not expand the approved plan.” On READY for **either** phase, one `save()` persists receipt, delivered ID/hash, `gate_ran=false` and a forced EXEC gate flag. That flag survives PLAN→EXEC and overrides gate-off; the gate compares diff to approved plan. Note acceptance alone does not set it. Probe never consumes the note. Failed spawn/limit/timeout or author HOLD leaves it pending; HOLD/uncertain retry is at-least-once and labelled repeat. If budget later runs out, report `undelivered` and retain pending through abort.

No coordinator note channel, label, ID, hash, source or evidence path enters fresh reviewer/shadow/gate input; author prompts/receipts are not forwarded. Author-produced plan/diff may independently contain the same words and must stay reviewable, not substring-masked. Fake CLI checks every fresh input for the operator channel. New work beyond the plan is rejected by normal PLAN review or the forced EXEC gate. Since R14-1 CLI scope-change stopped, current fallback is operator abort plus a manually started new run; the dormant method is not invoked. A replacement prompt says `supersedes <old ID>`; ordinary rejection feedback is labelled separately before the note. Verify evidence hash before injection; missing/mismatched bytes HOLD with note pending.
