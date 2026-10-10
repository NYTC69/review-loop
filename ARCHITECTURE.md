# review-loop — Architecture

**Last updated**: 2026-10-08 (v2.13.2)

## Overview

review-loop is a Claude Code and Codex plugin. Its entry skills route a request to one program, the paired-session
coordinator (`bin/paired-session` → `paired_session/coordinator.py`), which runs a work item through PLAN, EXEC, the
adversarial gate, FINISH, POLISH-Q, DOCS and SECURITY to DONE and delivers only on the operator's `accept`. The
author, reviewer, shadow, gate and specialists are separate Codex or Claude CLI processes that the coordinator starts
with their own sandboxes; the skills resolve the entry, run the stage A checks and drive the coordinator commands.
macOS only. The user-facing description is README.md.

## Module layout

- `skills/` (Claude) and `.agents/skills/` (Codex): the entry skills. `review-loop` routes (`references/entry.md`);
  `paired-session` drives the coordinator; `review-pr` and `code-quality-loop` hand off with their own options;
  `guide`; `reorganize` (Claude only, standalone).
- `docs/protocol/`: `paired-session-entry.md`, the shared contract both paired-session skills load; `loading.md` and
  `loading.json` (with `scripts/read_protocol.py`) select what each entry loads; `reviewer-runtime.md`,
  `delivery-scope.md`, `loading-special-cases.md`.
- `bin/paired-session`: the CLI entry point.
- `paired_session/coordinator.py`: the CLI (`run`, `resume`, `accept`, `reject`, `note`, `status`, `abort`, `stop`,
  `permission-probe`, …), the state machine, role dispatch, the finding ledger, budgets and acceptance. Helpers beside
  it include `worktree_lifecycle.py` (stage prompts, specialists, writers, the delivery report), `lifecycle_spine.py`
  (stage receipts), `review_report.py` (report mode and the delivery-report sections), `review_post.py` (the opt-in PR
  post), `docs_policy.py`, `evidence_guard.py`, `leak_scan.py` (the every-route credential scan of the change),
  `security_repair_policy.py`, `sensitive_policy.py` and
  `operator_verification.py`. `paired_session/README.md` is the operator reference for the coordinator CLI.
- `scripts/`: `delivery_scope.py` (delivery baselines and manifests) and `security_preflight.py` (the secret and
  `.gitignore` scan) for SECURITY, `content_rules.py` (the one credential rule table, with a scope per rule: that whole-delivery scan applies six rules, `leak_scan.py` and the review-pr post-body scan apply all), `materialize_pr.py` (review-pr clones), `adversarial_gate_fallback_prompt.txt` (the
  default gate prompt), `read_protocol.py`, `run-skill-lint`.
- `agents/*.md`: the role bodies, all hashed into the frozen role manifest; the specialists' and the simplifier's
  bodies are inlined into their turns.
- Manifests: `.claude-plugin/`, `.codex-plugin/plugin.json`, `.agents/plugins/marketplace.json` and the
  `plugins/review-loop` symlink.

## Data flow

A skill writes the work item (`WORKITEM.md`) into a run directory outside the product workspace and starts `run`. The
coordinator freezes the configuration and the role manifest in `state.json`, then dispatches one role turn at a time.
Each answer is schema-checked, recorded (turns, finding ledger, stage receipts, usage) and printed as a progress line
(`progress.jsonl`). The workspace is snapshotted around every turn: a reviewer turn that changes it is voided and
restored, and an author turn that moves HEAD is a HOLD. At DONE the operator accepts (an intent digest, then
`--expect`): the coordinator writes `delivery-report.md` and, with `auto_commit`, makes one hook-free local commit.

## Our schemas

The run directory holds `state.json` (frozen config, phase, turns, `finding_ledger`, `lifecycle` with the stage
receipts), `progress.jsonl`, `evidence/`, `plan.md`, `usage.json` / `usage.md`, `review-report.md` (report mode) and
`delivery-report.md` (at accept). The role answer schemas are `author_schema`, `review_schema` and `gate_schema` in
`coordinator.py`.
