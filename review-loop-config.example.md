# Review Loop — Project Config
# Place this file at: .review-loop/config.md
# All fields are optional. Remove any line to use the default.

# Shared config keys retain their Claude-side meaning in the shared protocol.
reviewer: codex                 # shared Claude/plugin key; Codex Stage 1 does not use this to pick the reviewer backend
reviewer_model: ""              # shared path-specific reviewer override; in Codex Stage 1 this applies only to the default Claude CLI reviewer path
judgment_model: ""              # shared tier override for judgment-tier agents; Codex Stage 1 uses this as the fallback for the default Claude reviewer path
cheap_model: ""                 # shared tier override for cheap-tier agents; defaults to claude-opus-5-5 and is accepted-but-no-op in Codex Stage 1
executor_model: inherit         # shared Claude/plugin executor override; "" and inherit fall through to judgment_model; ignored by Codex Stage 1
# Codex runtime does not use `reviewer` to choose the reviewer backend.
# Codex runtime-specific backend/model behavior comes from the optional `codex_*` keys below.
# Codex defaults to the outside-sandbox Claude CLI reviewer and does not auto-fall back to the local Codex reviewer.
# If neither `reviewer_model` nor `judgment_model` is set, that Claude path uses `--model claude-opus-5-5`.
# codex_reviewer_backend: claude_cli  # "claude_cli" | "codex" ; set "codex" only for explicit local-Codex review opt-in
# codex_reviewer_model: ""            # model override for the local Codex reviewer when codex_reviewer_backend: codex
# codex_executor_model: ""            # shared key remains `executor_model`; this is reserved/ignored in Stage 1
# Entry for fresh `/review-loop <work item>` (Claude) or the review-loop skill (Codex); default paired-session from v2.10.0.
# "paired-session" (exact values only: since v2.13.0 "legacy" is refused, and anything else is warned about and treated as absent).
# Fresh work, an existing plan (as the work item) and review-only requests (run --review-only) are routed; a legacy
# session resume is refused. Absent key = paired-session (the default entry). Codex honors this key the same way.
# The legacy workflow is deprecated since v2.12.0 (removal after the open legacy-map rows are decided;
# review-pr and code-quality-loop are ported);
# since v2.13.0 nothing routes to it (/review-loop:legacy, :plan and :execute remain until v2.13.1).
soft_limit_plan: 3              # after N rounds, ask user to continue if CRITICALs remain
soft_limit_exec: 3
auto_commit: false              # legacy only; paired-session reads auto_commit from the operator profile (E-4)
commit_message_prefix: "feat"
docs_file: CHANGELOG.md
handsfree: false

# Project-specific review priorities for code review phase.
# These are injected into the Reviewer's prompt as additional focus areas.
# Plan review is intentionally generic — it focuses on problem understanding.
# review_focus: |
#   - Security: XSS, CSRF, input sanitization, auth state handling
#   - Accessibility: WCAG compliance, keyboard navigation, screen reader
#   - UX edge cases: loading states, empty states, error states

# What to prioritize in quality polish (Step 3.5).
# `quality_focus` applies only when Step 3.5 Quality Polish actually runs.
# quality_focus: "strict clippy lints, skip comment analysis"

# Tone and rules for ALL reviews (adversarial CR + quality agents).
# Natural language — injected into every reviewer prompt.
# review_style: "be terse, flag 80-char violations as CRITICAL"

# `skip_quality_polish: true` mints `polish` as a no-op completion and still continues through docs and security.
skip_quality_polish: false

# Step 3.4 terminal adversarial gate — skip when every Step 3 changed file matches one of these glob patterns.
# adversarial_gate_skip_paths:
#   - "**/SKILL.md"
#   - "docs/protocol/**"
#   - "tests/skills/contracts/**"

# Cross-vendor review: when the final execution review is same-vendor as the author, run one extra review with the
# other vendor's CLI before delivery. "auto" (default) | "off" (records `cross-vendor review: off (config)`).
# cross_vendor_review: auto

# context_persist_threshold: 25   # trigger planning.md §3.5 persist when context_pct >= N; default 70
