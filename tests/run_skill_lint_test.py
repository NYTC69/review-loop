import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Optional


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

    def test_real_repo_passes_under_new_kinds(self):
        # Integration smoke: A1 + A2 wired into review-loop.json + guide.json
        # all green against the real repo. Locks against regressions of
        # the new `command_flag_co_occurrence` and
        # `agent_subagent_type_whitelist` heuristics.
        completed = subprocess.run(
            ["bash", "scripts/run-skill-lint"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        last_run = json.loads((ROOT / "tests/skills/.last-run.json").read_text(encoding="utf-8"))
        for contract_id in ("review-loop", "guide"):
            record = next(entry for entry in last_run["results"] if entry.get("id") == contract_id)
            self.assertEqual(record["status"], "pass", f"{contract_id}: {record['reason']}")


class LintAssertionOverrideWhitelistTest(unittest.TestCase):
    """Regression: the lint-side id-resolver must reject any
    non-`_`-prefixed override key. Earlier the whitelist was an empty
    `frozenset()` paired with a truthy guard (`if WHITELIST and key not in
    WHITELIST: fail`), which silently accepted every override key. The
    fixed semantic is strict: empty whitelist = no overrides allowed."""

    def test_unwhitelisted_lint_override_key_is_rejected(self):
        contract_id = "zz.lint-override-whitelist.unwhitelisted"
        case = LintAssertionFixtureCase(
            contract_id=contract_id,
            assertions=[
                {
                    "id": "claude_reviewer_command_flags_present",
                    "overrides": {"definitely_not_an_allowed_key": "x"},
                }
            ],
            fixture_dir=Path(ROOT),
        )
        try:
            case.write_contract()
            completed, record = case.run_lint()
        finally:
            case.cleanup()
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "fail", record)
        # Per-assertion detail lives on stdout; record["reason"] is summary.
        self.assertIn("definitely_not_an_allowed_key", completed.stdout)
        self.assertIn("not in the allowed list", completed.stdout)

    def test_underscore_prefixed_override_key_is_silently_dropped(self):
        # `_comment` and other `_`-prefixed keys are metadata and must NOT
        # trip the whitelist check. The assertion resolves and runs against
        # the real repo with no overrides applied.
        contract_id = "zz.lint-override-whitelist.underscore-ignored"
        case = LintAssertionFixtureCase(
            contract_id=contract_id,
            assertions=[
                {
                    "id": "claude_reviewer_command_flags_present",
                    "overrides": {"_comment": "rationale"},
                }
            ],
            fixture_dir=Path(ROOT),
        )
        try:
            case.write_contract()
            completed, record = case.run_lint()
        finally:
            case.cleanup()
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "pass", record)


class CommandFlagCoOccurrenceTest(unittest.TestCase):
    REQUIRED_FLAGS = [
        "--no-session-persistence",
        "--output-format stream-json",
        "--include-partial-messages",
    ]

    def _run(self, fixture_content: str, contract_id: str, *, anchor=None, required_flags=None, applies_to=None):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmpdir:
            fixture = write_fixture(Path(tmpdir), "SKILL.md", fixture_content)
            relative = fixture.relative_to(ROOT).as_posix()
            assertion = {
                "id": "test_command_flag_co_occurrence",
                "kind": "command_flag_co_occurrence",
                "required_flags": required_flags if required_flags is not None else self.REQUIRED_FLAGS,
            }
            if applies_to is not None:
                assertion["applies_to"] = applies_to
            else:
                assertion["path"] = relative
            if anchor is not None:
                assertion["anchor"] = anchor
            case = LintAssertionFixtureCase(
                contract_id=contract_id,
                assertions=[assertion],
                fixture_dir=Path(tmpdir),
            )
            try:
                case.write_contract()
                return case.run_lint()
            finally:
                case.cleanup()

    def test_passes_when_all_flags_on_single_line(self):
        content = (
            "```bash\n"
            "claude -p --no-session-persistence --output-format stream-json "
            "--include-partial-messages --model X < prompt\n"
            "```\n"
        )
        completed, record = self._run(content, "zz.lint.cmdflags.single-line")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_passes_when_all_flags_with_backslash_continuation(self):
        content = (
            "```bash\n"
            "claude -p --no-session-persistence \\\n"
            "  --output-format stream-json \\\n"
            "  --include-partial-messages --model X < prompt\n"
            "```\n"
        )
        completed, record = self._run(content, "zz.lint.cmdflags.backslash")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_passes_when_prose_followed_by_fence_block(self):
        content = (
            "Default reviewer path:\n"
            "```bash\n"
            "claude -p --no-session-persistence --output-format stream-json "
            "--include-partial-messages\n"
            "```\n"
        )
        completed, record = self._run(content, "zz.lint.cmdflags.fence-adjacent")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_passes_when_flag_straddles_plain_newline_no_backslash(self):
        # Synthetic R3-CRITICAL fixture: required flag spans a plain
        # newline with no backslash continuation. Pass 3 whitespace
        # normalization must collapse `\n` into ` ` so the flag matches.
        content = (
            "Default reviewer path: `claude -p --no-session-persistence --output-format\n"
            "stream-json --include-partial-messages --model X` with stdin fed from foo.\n"
        )
        completed, record = self._run(content, "zz.lint.cmdflags.plain-newline")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_passes_for_planning_md_lines_294_to_295_real_repo_shape(self):
        # Real-repo ground truth for `docs/protocol/planning.md:294-295`:
        # line 294 opens a SINGLE backtick `claude -p --no-session-persistence
        # --output-format` and that backtick stays open; line 295 begins
        # `stream-json …` and closes the single backtick. This is multi-line
        # single-backtick inline code — A1 must treat the wrapped span as a
        # real command body (NOT prose paraphrase) and verify all three
        # required flags resolve as substrings after Pass 3 normalization.
        content = (
            "Default reviewer path: `claude -p --no-session-persistence --output-format\n"
            "stream-json --include-partial-messages --model {x}`\n"
            "with stdin fed from foo.\n"
        )
        completed, record = self._run(content, "zz.lint.cmdflags.planning-md-shape")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_fails_when_flag_missing_regression_for_bug_b(self):
        content = (
            "```bash\n"
            "claude -p --no-session-persistence --output-format json --include-partial-messages\n"
            "```\n"
        )
        completed, record = self._run(content, "zz.lint.cmdflags.missing-flag")
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("--output-format stream-json", completed.stdout)

    def test_fails_for_single_backtick_inline_command_missing_flag(self):
        # Pin the narrowed backtick-prose guard: multi-line single-backtick
        # inline code IS a real command body. A regression that drops
        # `--include-partial-messages` from such an inline-code block must
        # be caught (not silently treated as prose paraphrase). This proves
        # the guard only suppresses *same-line* `…` wrappers and still
        # detects regressions in multi-line single-backtick inline form.
        content = (
            "Default reviewer path: `claude -p --no-session-persistence --output-format\n"
            "stream-json --model {x}`\n"
            "with stdin fed from foo.\n"
        )
        completed, record = self._run(content, "zz.lint.cmdflags.inline-missing-flag")
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("--include-partial-messages", completed.stdout)

    def test_passes_when_anchor_absent_vacuous(self):
        content = "Some unrelated docs without any reviewer command.\n"
        completed, record = self._run(content, "zz.lint.cmdflags.no-anchor")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_default_anchor_when_anchor_field_omitted(self):
        # Field-validation order: anchor is optional; default `claude -p`
        # is substituted before validation. With no anchor field passed,
        # the default still finds and validates the command block.
        content = (
            "```bash\n"
            "claude -p --no-session-persistence --output-format stream-json "
            "--include-partial-messages\n"
            "```\n"
        )
        completed, record = self._run(content, "zz.lint.cmdflags.default-anchor")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_read_error_accumulation_multiple_files(self):
        # When 2+ files in scope are unreadable (invalid UTF-8 bytes),
        # the final lint failure message must concatenate ALL file read-error
        # messages. This proves the read-error accumulation in
        # scripts/run-skill-lint:702-708 works. We test by creating a contract
        # with command_flag_co_occurrence kind and two unreadable fixture files.
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmpdir:
            tmppath = Path(tmpdir)
            # Create two files with invalid UTF-8 leading bytes
            file1 = tmppath / "bad1.md"
            file2 = tmppath / "bad2.md"
            file1.write_bytes(b'\xff\xfe\x00\x00')
            file2.write_bytes(b'\xff\xfe\x00\x00')

            relative1 = file1.relative_to(ROOT).as_posix()
            relative2 = file2.relative_to(ROOT).as_posix()

            case = LintAssertionFixtureCase(
                contract_id="zz.lint.cmdflags.read-error-accumulation",
                assertions=[
                    {
                        "id": "test_command_flag_co_occurrence_1",
                        "kind": "command_flag_co_occurrence",
                        "path": relative1,
                        "required_flags": ["--test-flag"],
                    },
                    {
                        "id": "test_command_flag_co_occurrence_2",
                        "kind": "command_flag_co_occurrence",
                        "path": relative2,
                        "required_flags": ["--test-flag"],
                    },
                ],
                fixture_dir=tmppath,
            )
            try:
                case.write_contract()
                completed, record = case.run_lint()
            finally:
                case.cleanup()

            # Expect failure because files are unreadable
            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            self.assertEqual(record["status"], "fail")
            # Both file read errors should appear in stdout (per-assertion output)
            # The output will mention "unable to read" for both file paths
            output = completed.stdout + completed.stderr
            self.assertIn("unable to read", output)
            # Verify both files are mentioned in the output
            self.assertIn("bad1.md", output)
            self.assertIn("bad2.md", output)


class AgentSubagentTypeWhitelistTest(unittest.TestCase):
    def _run(self, fixture_content: str, contract_id: str, *, ignore_negated_examples=True, whitelist=None):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmpdir:
            fixture = write_fixture(Path(tmpdir), "SKILL.md", fixture_content)
            relative = fixture.relative_to(ROOT).as_posix()
            case = LintAssertionFixtureCase(
                contract_id=contract_id,
                assertions=[
                    {
                        "id": "test_agent_subagent_type_whitelist",
                        "kind": "agent_subagent_type_whitelist",
                        "path": relative,
                        "whitelist": whitelist if whitelist is not None else ["general-purpose"],
                        "ignore_negated_examples": ignore_negated_examples,
                    }
                ],
                fixture_dir=Path(tmpdir),
            )
            try:
                case.write_contract()
                return case.run_lint()
            finally:
                case.cleanup()

    def test_passes_when_only_general_purpose_used(self):
        content = "Agent dispatch:\n  subagent_type: general-purpose\n  prompt: ...\n"
        completed, record = self._run(content, "zz.lint.subagent-whitelist.allowed")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_fails_when_review_loop_executor_at_column_zero(self):
        # Real invocation line, no negation token nearby, no anti-block
        # heading, value not backticked. Should fail.
        content = (
            "Agent dispatch follows.\n"
            "subagent_type: review-loop:executor\n"
            "prompt: ...\n"
        )
        completed, record = self._run(content, "zz.lint.subagent-whitelist.bad")
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("review-loop:executor", completed.stdout)

    def test_passes_when_same_line_never_use_negation(self):
        content = "Never use `subagent_type: review-loop:executor` — sandbox bug.\n"
        completed, record = self._run(content, "zz.lint.subagent-whitelist.never-use")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_passes_in_blockquote(self):
        content = "> subagent_type: review-loop:executor (anti-pattern)\n"
        completed, record = self._run(content, "zz.lint.subagent-whitelist.blockquote")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_ignore_negated_examples_false_overrides_suppression(self):
        # With suppression off, even a "Never use" prose mention fails.
        content = "Never use `subagent_type: review-loop:executor`.\n"
        completed, record = self._run(
            content, "zz.lint.subagent-whitelist.no-suppress", ignore_negated_examples=False
        )
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")

    def test_passes_for_skill_review_loop_lines_314_to_315(self):
        # Real-repo prose shape: `Never \`subagent_type: review-loop:<name>\`.`
        content = (
            "  `subagent` uses `subagent_type: general-purpose` with\n"
            "  `agents/reviewer.md` inlined plus a \"Report only\" instruction. Never\n"
            "  `subagent_type: review-loop:<name>`. For Claude reviewer prompts,\n"
        )
        completed, record = self._run(content, "zz.lint.subagent-whitelist.review-loop-prose")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_passes_for_planning_md_lines_279_to_280(self):
        # Real-repo prose shape with "Never fall back to" mention.
        content = (
            "  back to subagent mode **for this round only**; do not ask the user and do\n"
            "  not stop the loop. Never fall back to `subagent_type: review-loop:reviewer`\n"
            "  — plugin agent types have tools silently blocked.\n"
        )
        completed, record = self._run(content, "zz.lint.subagent-whitelist.planning-prose")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")


class KindContainsTest(unittest.TestCase):
    """Isolates the `kind: contains` lint mechanic from the integration smoke
    in AllSkillBodiesScopeTest. Three branches: needle-present (PASS),
    needle-absent (FAIL with "does not contain"), missing path (FAIL with
    "is missing"). Mirrors ForbiddenLinePatternTest._run convention."""

    def _run(
        self,
        *,
        fixture_content: str,
        contract_id: str,
        needle: str,
        write_fixture_file: bool = True,
        fixture_name: str = "DOC.md",
        path_override: Optional[str] = None,
    ):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as tmpdir:
            if write_fixture_file:
                fixture = write_fixture(Path(tmpdir), fixture_name, fixture_content)
                relative = fixture.relative_to(ROOT).as_posix()
            else:
                relative = None
            assertion_path = path_override if path_override is not None else relative
            case = LintAssertionFixtureCase(
                contract_id=contract_id,
                assertions=[
                    {
                        "id": "test_contains",
                        "kind": "contains",
                        "path": assertion_path,
                        "needle": needle,
                    }
                ],
                fixture_dir=Path(tmpdir),
            )
            try:
                case.write_contract()
                return case.run_lint()
            finally:
                case.cleanup()

    def test_passes_when_needle_present_in_path(self):
        # Locks the PASS branch of `kind: contains` (run-skill-lint:471-472).
        # Per-assertion message "contains expected text" is emitted by
        # print_case() to stdout; record["reason"] is the case-level
        # aggregate ("1 passed") so we assert against completed.stdout.
        completed, record = self._run(
            fixture_content="hello world\nthe needle is HERE\n",
            contract_id="zz.lint.kind-contains.present",
            needle="the needle is HERE",
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")
        self.assertIn("contains expected text", completed.stdout)

    def test_fails_when_needle_absent_from_path(self):
        # Locks the FAIL-when-absent branch (run-skill-lint:473).
        needle = "the needle is HERE"
        completed, record = self._run(
            fixture_content="hello world without the magic phrase\n",
            contract_id="zz.lint.kind-contains.absent",
            needle=needle,
        )
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("does not contain", completed.stdout)
        self.assertIn(needle, completed.stdout)

    def test_fails_when_path_is_missing(self):
        # Locks the FAIL-when-missing-path branch (run-skill-lint:466).
        # No fixture file is written; path_override points at a path that
        # does not exist on disk.
        bogus_path = "tests/skills/contracts/zz-kind-contains-nonexistent-fixture.md"
        completed, record = self._run(
            fixture_content="",
            contract_id="zz.lint.kind-contains.missing-path",
            needle="anything",
            write_fixture_file=False,
            path_override=bogus_path,
        )
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("is missing", completed.stdout)
        self.assertIn("zz-kind-contains-nonexistent-fixture.md", completed.stdout)

    def test_passes_when_case_matches_uppercase_needle(self):
        # AC-4.3 (v2.6.30) — case-sensitivity invariant of Python's `in`
        # at run-skill-lint:471. Uppercase needle "HERE" matches uppercase
        # text "the needle is HERE" → PASS.
        completed, record = self._run(
            fixture_content="the needle is HERE\n",
            contract_id="zz.lint.kind-contains.case-match",
            needle="HERE",
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_fails_when_case_differs_from_uppercase_needle(self):
        # AC-4.3 (v2.6.30) — case-sensitivity invariant. Uppercase needle
        # "HERE" does NOT match lowercase text "the needle is here" → FAIL.
        # Locks the case-sensitive invariant of Python's `in` operator at
        # run-skill-lint:471 so a future loader change cannot silently flip
        # to case-insensitive matching.
        completed, record = self._run(
            fixture_content="the needle is here\n",
            contract_id="zz.lint.kind-contains.case-mismatch",
            needle="HERE",
        )
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("does not contain", completed.stdout)

    def test_empty_needle_is_rejected_with_missing_required_field(self):
        # AC-4.4 (v2.6.30) — `require_fields` (run-skill-lint:67-71) rejects
        # `assertion.get(fn) in (None, "")`, so an empty needle is caught at
        # contract-load time and never reaches the `kind: contains` runtime
        # handler. Pin this loader-side defensive guarantee so a future
        # loader change that silently accepts `""` cannot regress without
        # updating this test.
        completed, record = self._run(
            fixture_content="any content\n",
            contract_id="zz.lint.kind-contains.empty-needle",
            needle="",
        )
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("missing required field(s): needle", completed.stdout)


if __name__ == "__main__":
    unittest.main()
