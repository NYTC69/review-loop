# Review Loop — Project Config
# Place this file at: .claude/review-loop-config.md
# All fields are optional. Remove any line to use the default.

reviewer: codex                 # "codex" | "subagent"
reviewer_model: ""              # codex: -m flag, empty = codex default; subagent: Agent tool model
executor_model: inherit         # Executor sub-agent model
soft_limit_plan: 3              # after N rounds, ask user to continue if CRITICALs remain
soft_limit_exec: 3
auto_commit: false
commit_message_prefix: "feat"
docs_file: CHANGELOG.md
handsfree: false
