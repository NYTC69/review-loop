import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LintAssertionFixtureCase:
    """Helper: write a temp contract file + fixtures, run lint, return the
    matching record from tests/skills/.last-run.json, then clean up."""

    def __init__(self, contract_id: str, assertions: list, fixture_dir: Path):
        self.contract_id = contract_id
        self.assertions = assertions
        self.fixture_dir = fixture_dir
        self.contract_path = ROOT / "tests/skills/contracts" / f"{contract_id}.json"

    def write_contract(self):
        self.contract_path.write_text(
            json.dumps({"id": self.contract_id, "type": "lint", "assertions": self.assertions}, indent=2) + "\n",
            encoding="utf-8",
        )

    def cleanup(self):
        if self.contract_path.exists():
            self.contract_path.unlink()

    def run_lint(self):
        completed = subprocess.run(
            ["bash", "scripts/run-skill-lint"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        last_run = json.loads((ROOT / "tests/skills/.last-run.json").read_text(encoding="utf-8"))
        record = next(
            (entry for entry in last_run["results"] if entry.get("id") == self.contract_id),
            None,
        )
        return completed, record


def write_fixture(dirpath: Path, name: str, content: str) -> Path:
    target = dirpath / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


class ForbiddenLinePatternTest(unittest.TestCase):
    PATTERN = r"^\s*subagent_type:\s*(?P<quote>['\"]?)review-loop:[^\s'\"`#]+(?P=quote)\s*(?:#.*)?$"

    def _run(self, fixture_content: str, contract_id: str):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmpdir:
            fixture = write_fixture(Path(tmpdir), "SKILL.md", fixture_content)
            relative = fixture.relative_to(ROOT).as_posix()
            case = LintAssertionFixtureCase(
                contract_id=contract_id,
                assertions=[
                    {
                        "id": "test_forbidden_line_pattern",
                        "kind": "forbidden_line_pattern",
                        "path": relative,
                        "pattern": self.PATTERN,
                    }
                ],
                fixture_dir=Path(tmpdir),
            )
            try:
                case.write_contract()
                return case.run_lint()
            finally:
                case.cleanup()

    def test_passes_when_only_doc_text_mentions_subagent_type(self):
        # Backtick-wrapped prose, never-use sentences, and warning headings
        # all keep the line from starting with `subagent_type:` at column 0.
        content = "\n".join(
            [
                "Never use `subagent_type: review-loop:executor`.",
                "**CRITICAL — plugin sandbox bug**: Do NOT use `subagent_type: review-loop:code-simplifier`.",
                "  - `subagent_type: review-loop:<name>` (placeholder used inside backticks)",
            ]
        )
        completed, record = self._run(content, "zz.lint.line-pattern.docs-only")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_fails_when_invocation_line_appears_at_column_zero(self):
        content = "\n".join(
            [
                "Agent tool parameters:",
                "  subagent_type: review-loop:executor",
                "  prompt: |",
                "    body...",
            ]
        )
        completed, record = self._run(content, "zz.lint.line-pattern.invocation")
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("review-loop:executor", completed.stdout)

    def test_fails_when_invocation_line_uses_quoted_value(self):
        # A YAML-style quoted form is still a real invocation line and must
        # be flagged. `[^\s'\"`#]+` handles the inside-quotes case via the
        # named backreference closing the quote.
        content = '  subagent_type: "review-loop:reviewer"\n'
        completed, record = self._run(content, "zz.lint.line-pattern.quoted")
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")


class PatternRequiresAdjacentTest(unittest.TestCase):
    def _run(self, fixture_content: str, contract_id: str, *, window_lines: int = 1):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmpdir:
            fixture = write_fixture(Path(tmpdir), "SKILL.md", fixture_content)
            relative = fixture.relative_to(ROOT).as_posix()
            case = LintAssertionFixtureCase(
                contract_id=contract_id,
                assertions=[
                    {
                        "id": "test_pattern_requires_adjacent",
                        "kind": "pattern_requires_adjacent",
                        "path": relative,
                        "needle": "subagent_type: review-loop:executor",
                        "required_nearby": ["CRITICAL"],
                        "window_lines": window_lines,
                    }
                ],
                fixture_dir=Path(tmpdir),
            )
            try:
                case.write_contract()
                return case.run_lint()
            finally:
                case.cleanup()

    def test_passes_when_required_nearby_appears_on_same_line(self):
        content = (
            "**CRITICAL — plugin sandbox bug**: Never use "
            "`subagent_type: review-loop:executor`.\n"
        )
        completed, record = self._run(content, "zz.lint.adjacent.same-line")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_passes_when_required_nearby_appears_within_window(self):
        content = "\n".join(
            [
                "**CRITICAL — sandbox bug:**",
                "Never use `subagent_type: review-loop:executor`.",
                "Always use general-purpose instead.",
            ]
        )
        completed, record = self._run(content, "zz.lint.adjacent.window")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_fails_when_required_nearby_is_too_far_above(self):
        content = "\n".join(
            [
                "**CRITICAL** sandbox warnings live near the top of this section.",
                "",
                "",
                "Never use `subagent_type: review-loop:executor`.",
            ]
        )
        completed, record = self._run(content, "zz.lint.adjacent.far", window_lines=1)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("missing nearby", completed.stdout)

    def test_passes_when_needle_does_not_appear(self):
        content = "Some unrelated docs without any sandbox-affected agent name.\n"
        completed, record = self._run(content, "zz.lint.adjacent.no-mention")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")


class AllSkillBodiesScopeTest(unittest.TestCase):
    def test_real_repo_contract_assertions_pass_under_new_lint_kinds(self):
        # Integration smoke: the four review-loop contract assertions added
        # for [2] all pass against the current repo. Locks the contract
        # against future regressions of the line-pattern + adjacent guards.
        completed = subprocess.run(
            ["bash", "scripts/run-skill-lint"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        last_run = json.loads((ROOT / "tests/skills/.last-run.json").read_text(encoding="utf-8"))
        review_loop_record = next(
            entry for entry in last_run["results"] if entry.get("id") == "review-loop"
        )
        self.assertEqual(review_loop_record["status"], "pass", review_loop_record["reason"])


if __name__ == "__main__":
    unittest.main()
