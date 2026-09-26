"""Capability regressions for the Claude reviewer launch contract."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from reviewer_permissions import (
    claude_readonly_args, REQUIRED_CLAUDE_FLAGS, unsupported_claude_flags,
)


class ReviewerPermissionsTest(unittest.TestCase):
    def test_readers_are_the_entire_tool_surface_and_customizations_are_disabled(self):
        args = claude_readonly_args()
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(args[args.index("--allowedTools") + 1], "Read,Grep,Glob")
        self.assertEqual(args[args.index("--setting-sources") + 1], "")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(json.loads(args[args.index("--mcp-config") + 1]), {"mcpServers": {}})
        settings = json.loads(args[args.index("--settings") + 1])
        self.assertTrue(settings["disableAllHooks"])
        self.assertEqual(settings["permissions"]["allow"], ["Read", "Grep", "Glob"])
        for tool in ("Bash", "Edit", "Write", "Agent", "Task", "WebFetch", "WebSearch", "mcp__*"):
            self.assertIn(tool, settings["permissions"]["deny"])
        self.assertTrue(set(REQUIRED_CLAUDE_FLAGS).issubset(args))

    def test_probe_requires_every_flag_and_rejects_prefix_matches(self):
        help_text = "\n".join(REQUIRED_CLAUDE_FLAGS)
        self.assertEqual(unsupported_claude_flags(help_text), [])
        for flag in REQUIRED_CLAUDE_FLAGS:
            with self.subTest(flag=flag):
                unsupported = unsupported_claude_flags(help_text.replace(flag, flag + "-unsupported"))
                self.assertIn(flag, unsupported)


if __name__ == "__main__":
    unittest.main()
