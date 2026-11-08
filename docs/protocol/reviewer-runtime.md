# Reviewer runtime boundary

A report-only reviewer or quality specialist (the report-only `agents/*.md` bodies) reads and searches only: it
installs nothing and writes nothing, and it bases its report on the files in scope and on the evidence it is given.
It reports missing evidence or a failed inspection instead of inventing results.

Since v2.13.1 the only dispatcher is the paired-session coordinator: it inlines the agent body into a fresh
reviewer-role turn (the POLISH-Q specialists and the review-pr report mode), and that role's own permissions apply. A Codex
reviewer-type turn runs workspace-write since v3.6.0 so a configured test can write build output and caches; the coordinator
undoes any change to HEAD, the branch, the index, a tracked file or a non-ignored untracked file and dispatches the turn once
more, while ignored paths stay free.
The legacy launcher scripts (`run_claude_reviewer.py`, `run_codex_reviewer.py`) were removed with the legacy workflow.
