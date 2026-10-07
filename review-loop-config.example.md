# Review Loop — Project Config
# Place this file at: .review-loop/config.md
# All fields are optional and shown commented out with their default; uncomment only the keys you change.

# Roles, vendors and models come from the operator profile: the one you name, else
# ~/.config/review-loop/paired-session.json (example: paired_session/paired-session-config.example.json); role and
# vendor keys in the workspace .review-loop/paired-session.json are refused.
# reviewer_model / executor_model here are not applied (a warning when set to anything other than "" or inherit);
# models come from the operator profile.
# Entry for fresh `/review-loop <work item>` (Claude) or the review-loop skill (Codex); default paired-session from v2.10.0.
# "paired-session" (exact values only: since v2.13.0 "legacy" is refused, and anything else is warned about and treated as absent).
# Fresh work, an existing plan (as the work item) and review-only requests (run --review-only) are routed; a legacy
# session resume is refused. Absent key = paired-session (the default entry). Codex honors this key the same way.
# The legacy workflow was removed in v2.13.0 (routing) and v2.13.1 (/review-loop:legacy, :plan, :execute
# and its files deleted).
# entry: paired-session

# soft_limit_plan: 3            # --max-plan-rounds; at the cap the run HOLDs and `resume --add-rounds N` continues it
# soft_limit_exec: 4            # --max-exec-rounds
# auto_commit: false            # review-only runs (/review-loop on existing code, code-quality-loop): false keeps the
                                # accepted change uncommitted (default: one local commit at accept, never a push);
                                # main pipeline: not applied (true prints a warning; the operator profile decides)
# docs_file: CHANGELOG.md       # --docs-file; "" skips the docs entry
# handsfree: false              # true: a stage A question fails the entry; accept/reject are never run

# Project-specific review priorities (--review-focus), frozen at run start, for the reviewer, the shadow and the gate.
# review_focus: |
#   - Security: XSS, CSRF, input sanitization, auth state handling
#   - Accessibility: WCAG compliance, keyboard navigation, screen reader
#   - UX edge cases: loading states, empty states, error states

# What the POLISH-Q specialists prioritize (--quality-focus).
# quality_focus: "strict clippy lints, skip comment analysis"

# Tone and rules for every review role (--review-style).
# review_style: "be terse, flag 80-char violations as CRITICAL"

# true skips the POLISH-Q specialists and the quality writers; docs and security still run.
# skip_quality_polish: false

# Removed with the legacy workflow (no effect if set): reviewer, judgment_model, cheap_model, codex_reviewer_backend,
# codex_reviewer_model, codex_executor_model, commit_message_prefix, cross_vendor_review,
# adversarial_gate_skip_paths, context_persist_threshold.
