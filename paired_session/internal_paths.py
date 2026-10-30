"""Coordinator-owned workspace subdirectories excluded from role inventories."""
INTERNAL_WORKSPACE_DIRS = tuple(".review-loop/" + name for name in ("runs", "codex-home", "pr", "logs"))
