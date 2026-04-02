# Review Loop — Project Config
# Place this file at: .claude/review-loop-config.md
# All fields are optional. Remove any line to use the default.

reviewer: codex                 # "codex" | "subagent"
reviewer_model: ""              # codex: -m flag (empty = codex default); subagent: Agent model (empty = inherit Orchestrator)
executor_model: inherit         # Executor sub-agent model
soft_limit_plan: 3              # after N rounds, ask user to continue if CRITICALs remain
soft_limit_exec: 3
auto_commit: false
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
# Natural language — injected into quality agent prompts.
# quality_focus: "strict clippy lints, skip comment analysis"

# Tone and rules for ALL reviews (adversarial CR + quality agents).
# Natural language — injected into every reviewer prompt.
# review_style: "be terse, flag 80-char violations as CRITICAL"

# Skip Quality Polish (Step 3.5) entirely.
skip_quality_polish: false
