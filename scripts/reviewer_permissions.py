"""Claude reviewer capabilities, independent of prompt-level instructions.

The boundary excludes project/user customizations and model-selected actions.
Administrator-managed policy remains a trusted machine prerequisite: Claude's
safe mode intentionally preserves managed hooks and commands. This is not an
OS sandbox for a hostile administrator or an untrusted Claude executable.
"""

from __future__ import annotations

import json
import re


READ_ONLY_TOOLS = ("Read", "Grep", "Glob")
DENIED_TOOLS = (
    "Bash", "PowerShell", "Edit", "Write", "Agent", "Task", "WebFetch",
    "WebSearch", "NotebookEdit", "mcp__*",
)
REQUIRED_CLAUDE_FLAGS = (
    "--safe-mode", "--restricted", "--setting-sources", "--settings",
    "--tools", "--allowedTools", "--permission-mode",
    "--disable-slash-commands", "--strict-mcp-config", "--mcp-config",
)


def claude_readonly_args():
    """Return fresh CLI arguments that expose only built-in file readers.

    --allowedTools alone is insufficient: inherited grants can approve other
    tools. --tools removes those capabilities. Safe/restricted modes omit user
    and project customizations; explicit hook, skill and MCP flags add defense
    in depth. Unlike --bare, safe mode retains the caller's normal OAuth login.
    """
    settings = {
        "disableAllHooks": True,
        "permissions": {
            "allow": list(READ_ONLY_TOOLS),
            "deny": list(DENIED_TOOLS),
            "defaultMode": "dontAsk",
        },
    }
    tools = ",".join(READ_ONLY_TOOLS)
    return [
        "--safe-mode", "--restricted", "--setting-sources", "",
        "--settings", json.dumps(settings, separators=(",", ":")),
        "--tools", tools, "--allowedTools", tools,
        "--permission-mode", "dontAsk", "--disable-slash-commands",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
    ]


def unsupported_claude_flags(help_text):
    """Fail closed on CLI versions lacking a required isolation capability."""
    available = set(re.findall(r"(?<![\w-])--[A-Za-z][A-Za-z0-9-]*", help_text))
    return [flag for flag in REQUIRED_CLAUDE_FLAGS if flag not in available]
