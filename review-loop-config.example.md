# Review Loop — Project Config
# Place this file at: .review-loop/config.md
# All fields are optional. Remove any line to use the default.

# Shared config keys retain their Claude-side meaning in the shared protocol.
reviewer: codex                 # shared Claude/plugin key; Codex Stage 1 does not use this to pick the reviewer backend
reviewer_model: ""              # shared path-specific reviewer override; in Codex Stage 1 this applies only to the default Claude CLI reviewer path
judgment_model: ""              # shared tier override for judgment-tier agents; Codex Stage 1 uses this as the fallback for the default Claude reviewer path
cheap_model: ""                 # shared tier override for cheap-tier agents; defaults to claude-haiku-4-5-20251001 and is accepted-but-no-op in Codex Stage 1
executor_model: inherit         # shared Claude/plugin executor override; "" and inherit fall through to judgment_model; ignored by Codex Stage 1
# Codex runtime does not use `reviewer` to choose the reviewer backend.
# Codex runtime-specific backend/model behavior comes from the optional `codex_*` keys below.
# Codex defaults to the outside-sandbox Claude CLI reviewer and does not auto-fall back to the local Codex reviewer.
# codex_reviewer_backend: claude_cli  # "claude_cli" | "codex" ; set "codex" only for explicit local-Codex review opt-in
# codex_reviewer_model: ""            # model override for the local Codex reviewer when codex_reviewer_backend: codex
# codex_executor_model: ""            # shared key remains `executor_model`; this is reserved/ignored in Stage 1
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
