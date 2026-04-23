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

    def test_codex_delivery_gate_requires_full_downstream_stage_set(self):
        expectations = {
            "docs/protocol/execution.md": "Codex Stage 1: `{exec, polish, docs, security} ⊆ completed_stages`.",
            "docs/protocol/session-file.md": "Codex Stage 1: `{exec, polish, docs, security}`.",
            "skills/execute/SKILL.md": "Codex Stage 1: `{exec, polish, docs, security} ⊆ completed_stages`.",
        }
        legacy = "Codex Stage 1: `{exec} ⊆ completed_stages`."
        for relative_path, expected in expectations.items():
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(expected, text)
                self.assertNotIn(legacy, text)

    def test_codex_downstream_stop_points_are_documented(self):
        expected = (
            "Codex Stage 1 supports `before-polish`, `before-docs`, and "
            "`before-security` as clean stop points."
        )
        for relative_path in (
            "docs/protocol/execution.md",
            "README.md",
            ".agents/skills/guide/SKILL.md",
            "skills/guide/SKILL.md",
        ):
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(expected, text)

    def test_codex_quality_focus_and_skip_quality_polish_are_real_behavior(self):
        quality_focus = (
            "`quality_focus` applies only when Step 3.5 Quality Polish "
            "actually runs."
        )
        skip_quality_polish = (
            "`skip_quality_polish: true` mints `polish` as a no-op completion "
            "and still continues through docs and security."
        )
        for relative_path in (
            "README.md",
            "review-loop-config.example.md",
            ".agents/skills/guide/SKILL.md",
            ".agents/skills/review-loop/SKILL.md",
            "skills/guide/SKILL.md",
            "skills/review-loop/SKILL.md",
        ):
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(quality_focus, text)
                self.assertIn(skip_quality_polish, text)

    def test_codex_stage1_workspace_authority_is_orchestrator_owned(self):
        expected = (
            "Codex Stage 1 assumes a single orchestrator-owned workspace for "
            "the session."
        )
        for relative_path in (
            "README.md",
            ".agents/skills/guide/SKILL.md",
            ".agents/skills/review-loop/SKILL.md",
            "docs/protocol/session-file.md",
        ):
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(expected, text)

    def test_codex_executor_must_not_switch_worktrees(self):
        expected = (
            "Do not create or switch to another git worktree or repository "
            "checkout."
        )
        for relative_path in (
            ".codex/agents/review-loop-executor.toml",
            ".agents/skills/review-loop/SKILL.md",
            "docs/protocol/execution.md",
        ):
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(expected, text)

    def test_codex_reviewer_must_flag_workspace_divergence(self):
        expected = (
            "If implementation appears to exist only in a different git "
            "worktree or repository path than the current workspace, return "
            "REQUEST_CHANGES with a [CRITICAL] workspace divergence issue."
        )
        for relative_path in (
            ".codex/agents/review-loop-reviewer.toml",
            ".agents/skills/review-loop/SKILL.md",
            "docs/protocol/execution.md",
        ):
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
