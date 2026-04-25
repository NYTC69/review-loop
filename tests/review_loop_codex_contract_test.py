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

    def test_codex_completed_agents_are_closed_between_rounds(self):
        expectations = {
            ".agents/skills/review-loop/SKILL.md": (
                "Before every new `spawn_agent` call, call `close_agent` on "
                "any completed Codex subagent id from earlier planning, "
                "execution, or local-reviewer rounds unless the orchestrator "
                "explicitly intends to reuse that exact id."
            ),
            "docs/protocol/planning.md": (
                "After the Executor and Reviewer outputs for a planning round "
                "have been validated and persisted to the session file, close "
                "completed Codex subagents for that round before the next "
                "round or phase transition."
            ),
            "docs/protocol/execution.md": (
                "After the Executor and Reviewer outputs for an execution "
                "round have been validated and persisted to the session file, "
                "close completed Codex subagents for that round before the "
                "next round, downstream stage, or delivery step."
            ),
        }
        claude_cli_exception = (
            "The Claude CLI reviewer path is a child process, not a Codex "
            "subagent, so completed-agent cleanup does not apply to it."
        )

        for relative_path, expected in expectations.items():
            with self.subTest(path=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(expected, text)

        text = (ROOT / ".agents/skills/review-loop/SKILL.md").read_text(encoding="utf-8")
        self.assertIn(claude_cli_exception, text)

    def test_codex_umbrella_review_loop_must_not_stop_after_exec_approval(self):
        expected = (
            "For the umbrella `review-loop` entry point, do not deliver, "
            "summarize success, or stop after the execution loop mints only "
            "`exec`; continue through Quality Polish, Documentation "
            "Consistency, Security Preflight, and delivery unless an explicit "
            "`--stop-after` value says otherwise."
        )
        text = (ROOT / ".agents/skills/review-loop/SKILL.md").read_text(encoding="utf-8")
        self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
