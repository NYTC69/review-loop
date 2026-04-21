import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CodexAgentContractTest(unittest.TestCase):
    def test_codex_agent_tomls_do_not_use_unsupported_tier_field(self):
        for relative_path in (
            ".codex/agents/review-loop-executor.toml",
            ".codex/agents/review-loop-reviewer.toml",
        ):
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotIn('tier = "', text)

    def test_codex_default_claude_reviewer_backstop_uses_claude_model(self):
        expected = "claude-sonnet-4-6"
        legacy = "gpt-5.4"
        for relative_path in (
            "README.md",
            "docs/protocol/planning.md",
            ".agents/skills/review-loop/SKILL.md",
        ):
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(expected, text)
                self.assertNotIn(
                    f"`reviewer_model` > `judgment_model` > `{legacy}`",
                    text,
                )


if __name__ == "__main__":
    unittest.main()
