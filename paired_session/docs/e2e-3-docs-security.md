# E2E lifecycle design 3/4: DOCS and SECURITY

Design only; lifecycle disabled. Inputs: [doc 1](e2e-1-stages-and-roles.md), [2a](e2e-2a-tree-binding.md), [2b-i](e2e-2b1-budgets.md), [2b-ii](e2e-2b2-findings-replay.md); legacy `docs/protocol/execution.md` §Quality-agent tool-use guard, §§3.6–3.7; R12-4 R1 M5/S1, R2 M8–M10. Commit/close is doc 4.

## DOCS: bounded writes and a current-tree review

1. Enter with current-OID POLISH-Q and no upstream blocker. A frozen docs-consistency role compares project docs with implementation and **always** checks changed comments/docstrings; record `project_docs=none` if needed (legacy §§3.6.1–3.6.2). Coordinator dispatches docs writer/reviewer and independent full-diff reviewer.
2. DOCS may write **only** the project's frozen explicit exact-path list; default is safe `docs_file` alone (doc 1 §Writes). Other project docs go to finisher, invalidating EXEC. Reserved paths exclude EXEC/FINISH/POLISH writes and EXEC closure; earlier edits HOLD. Deny source, tests, config, manifests, `.gitignore`, `.git`, symlink escapes and instruction paths (`AGENTS.md`, `CLAUDE.md`, `agents/**`, `skills/**`, `.claude/**`, `docs/protocol/**`) even if listed. Coordinator checks OS grants and delta.
3. Stale source comments/docstrings or other nonlisted repairs go to the separate finisher with an explicit path grant, then replay EXEC reviewer/new-convergence gate and downstream stages. DOCS cannot widen its grant (R12-4 R1 C2).
4. Allowed DOCS writes create a new OID and expire full-tree FINISH/POLISH-Q/docs/final/test/SECURITY receipts. Doc 2a's exact-path proof may preserve only an EXEC/gate claim; closure overlap or uncertainty replays EXEC/gate. Otherwise rerun FINISH/POLISH-Q, consistency, fresh docs/full-diff reviews and configured tests on that OID before SECURITY. Reserve slots; exhaustion HOLDs for bounded extension or abort (R12-4 R2 M8).
5. Generate `docs_file`/CHANGELOG from current evidence before SECURITY; later results, tokens and SHA go only in the final report. Replay replaces the prior epoch entry. Docs review checks behavior/comments; final review covers the entire post-DOCS diff. An OPEN finding closes only by its owner's explicit disposition (doc 2b-ii).

## Tool-use and receipt guard (R12-4 R1 M5)

For every quality/DOCS/SECURITY agent, coordinator checks provider-observed nonzero tool use **including a read of candidate diff or manifest**, current OID, frozen model/body hash and tool trace. Self-report is insufficient. Zero/missing evidence discards the result, allows one budgeted fresh retry, then records skip/failure and HOLD; it never passes (legacy tool-use guard). Legal actions: bounded extension, repair/retry or abort.

M4 must provide frozen `agents/*.md` bodies for docs-consistency inspector, docs writer/reviewer, independent final reviewer, security reviewer and fixer before lifecycle activation; a missing body HOLDs before dispatch. Writers use the configured author vendor; read-only reviewers/inspector use the opposite ADR-8 vendor/model, in separate sessions. The final reviewer owns its own findings and must inspect the full candidate diff; role ownership follows doc 2b-ii.

## SECURITY: whole-repo preflight, fresh review and repair

1. After DOCS/final review/tests, scan unconditionally, including no-op/skipped polish (legacy §3.7). Freeze manifest; scan every candidate-OID and delivery-manifest path, including untracked/ignored **delivery** paths. Live files outside manifest receive only doc 2a overlap checks. Resolve candidate paths per doc 2a and scan all legacy §3.7.1 sensitive categories; record path/category/source. A hit is nonwaivable CRITICAL HOLD. For this-run additions, a path-limited fixer removes/untracks and replays EXEC; inherited parent secrets outside grants require abort/new run after owner repair. Never stage a secret to inspect it (R1 S1/R2 M9).
2. Audit every legacy §3.7.2 `.gitignore` category. Missing broad `*credentials*`/`*secret*` patterns always require operator confirmation; other missing patterns require it if they match tracked paths. Record proposed patterns, category, per-pattern candidate matches, OID, digest and operator/time/command-hash provenance. `confirm-ignore --digest D` or `decline-ignore --digest D` binds consent to unchanged patterns/OID. Decline leaves HOLD for abort or a new owner decision; confirmed fixer may append only approved patterns. Nonbroad unmatched patterns are added by default under frozen policy.
3. **Every SECURITY-stage write**, including `.gitignore`, starts a new EXEC convergence, resets `gate_ran`, expires downstream receipts and replays EXEC reviewer/gate→FINISH→POLISH-Q→DOCS→SECURITY (legacy §3.7). For a flagged reserved doc, coordinator records a repair constraint, then only the replayed DOCS writer may edit it; SECURITY fixer gets no grant. Regeneration must preserve the constraint or HOLD. Original security reviewer explicitly disposes its OPEN finding on the new OID. Other repairs use a path-limited fixer; no fixer controls ledger, commit or accept. `security=true` at any severity and MAJOR/MEDIUM block (R13-3 R3).
4. Dispatch a fresh read-only security reviewer over the full candidate/manifest/diff, blind to peers. Its current-OID receipt binds observed inspection tools, role/model/body hash, trace, findings and verdict. **Any** DOCS or SECURITY write restarts the required route above; then rerun tests and sensitive scan on the new OID before SECURITY PASS. Recheck manifest/OID at exit. Missing evidence, blocker, failed check or mismatch HOLDs with named bounded retry/extension, repair replay or abort. PASS is a doc-4 gate, not acceptance.

## Fake-CLI acceptance assigned to doc 3

- `test_docs_source_comment_write_replays_exec_gate`: docs writer is denied; finisher repair restarts EXEC, then all downstream reviews/tests.
- `test_docs_reserved_write_rechecks_current_oid`: scoped claim proof retains only EXEC/gate where valid; FINISH/POLISH-Q, docs/final review, tests and SECURITY rerun.
- `test_docs_new_path_and_lint_input_fail_closed`: undeclared path or EXEC-closure overlap HOLDs; docs reviewer cannot broaden the allowlist.
- `test_docs_changelog_epoch_replace_and_project_consistency`: no duplicate summary; no-doc case still checks comments; post-SECURITY facts appear only in final report.
- `test_zero_tool_docs_and_security_never_pass`: one fresh retry, then named HOLD and budget evidence.
- `test_security_untracked_ignored_secret_blocks`: candidate, live/untracked and sensitive ignored paths are scanned before delivery; no sensitive file staged.
- `test_gitignore_broad_pattern_needs_operator_digest`: absent consent/stale digest/decline cannot write; confirmed write restarts EXEC.
- `test_security_reserved_docs_repair_replays_exec`: only replayed DOCS writer edits, `gate_ran` resets, security owner re-reviews.
- `test_security_noop_and_retest_same_oid`: no-op still scans; post-write test/scan, final-review and security OIDs agree; mismatch HOLDs.

Doc 4 owns acceptance, commit, Compass CLOSE and final gate.
