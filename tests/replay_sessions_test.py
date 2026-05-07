"""Unit tests for scripts/replay_sessions.py.

14 cases:
  - 4 AC tests (clean / anomaly / mixed / no-tokens)
  - 7 corpus-grounded regex-locking tests (FP1, FP2, WC1, WC2, BP1, KN1, LEG)
  - 1 dedup test (primary + secondary span overlap)
  - 2 CLI-shape tests (--text, --exit-zero)

Stdlib unittest. Mirrors tests/run_skill_lint_test.py style: subprocess.run
+ tempfile + a small write_fixture helper.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "replay_sessions.py"


def write_fixture(dirpath: Path, name: str, content: str) -> Path:
    target = dirpath / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def run_parser(root_dir: Path, *flags: str):
    """Shell out to the parser; return (exit_code, parsed_stdout_or_text, raw_stdout)."""
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root_dir), *flags],
        capture_output=True,
        text=True,
    )
    raw = completed.stdout
    if "--text" in flags:
        return completed.returncode, raw, raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    return completed.returncode, parsed, raw


def _file_record(report: dict, basename: str) -> dict:
    """Find the file record whose path ends in basename."""
    for entry in report["files"]:
        if entry["path"].endswith(basename):
            return entry
    raise AssertionError(f"no file record for {basename} in {report}")


class AcceptanceCriteriaTest(unittest.TestCase):
    """AC §2 a-d — the four mandatory acceptance scenarios."""

    def test_clean_session_only_general_purpose(self):
        # AC §2(a) — fixture verbatim from b3c76110:126.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "clean.md",
                "- Executor backend: Agent (subagent_type: general-purpose), tool_uses: 70 (hallucination guard passed)\n",
            )
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 0)
            rec = _file_record(report, "clean.md")
            self.assertEqual(rec["counts"], {"general-purpose": 1})
            self.assertFalse(rec["anomaly"])
            self.assertEqual(report["summary"]["files_with_anomaly"], 0)

    def test_anomaly_review_loop_executor(self):
        # AC §2(b) — `subagent_type: review-loop:executor` triggers anomaly.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "bad.md",
                "Agent dispatch:\n  subagent_type: review-loop:executor\n  prompt: ...\n",
            )
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 1)
            rec = _file_record(report, "bad.md")
            self.assertTrue(rec["anomaly"])
            self.assertEqual(rec["anomaly_values"], ["review-loop:executor"])
            self.assertEqual(rec["counts"], {"review-loop:executor": 1})

    def test_mixed_general_purpose_and_review_loop(self):
        # AC §2(c) — mixed values, anomaly_values lists only review-loop:*.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "mixed.md",
                "  subagent_type: general-purpose\n"
                "  subagent_type: review-loop:executor\n",
            )
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 1)
            rec = _file_record(report, "mixed.md")
            self.assertTrue(rec["anomaly"])
            self.assertEqual(rec["counts"], {"general-purpose": 1, "review-loop:executor": 1})
            self.assertEqual(rec["anomaly_values"], ["review-loop:executor"])

    def test_no_subagent_type_tokens_at_all(self):
        # AC §2(d) — empty counts, no anomaly, exit 0.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "empty.md",
                "## Some prose\n\nNo dispatch references at all here.\n",
            )
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 0)
            rec = _file_record(report, "empty.md")
            self.assertEqual(rec["counts"], {})
            self.assertFalse(rec["anomaly"])


class CorpusGroundedRegexLockingTest(unittest.TestCase):
    """Verbatim corpus lines that lock the regex semantics."""

    def test_FP1_requires_subagent_type_must_not_match(self):
        # Source: .review-loop/sessions/b1e5ecca-6bc1-43cb-b6d3-c8b5174e60ca.md:103
        # Locks `\b` left anchor — `requires_subagent_type` is a longer key.
        line = (
            "   - Field shape: `{kind, artifact?, min?, tool?, requires_subagent_type?}`."
            " Defaults: `artifact = \"tool-use-events.json\"`, `min = 1`, `tool = \"Agent\"`,"
            " `requires_subagent_type = true`.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "fp1.md", line)
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 0)
            rec = _file_record(report, "fp1.md")
            self.assertEqual(rec["counts"], {})
            self.assertFalse(rec["anomaly"])

    def test_FP2_prose_truncation_must_not_match(self):
        # Source: .review-loop/sessions/db03d9d3-d183-4c7f-a76b-039078e50f6c.md:13
        # Locks wrapper-or-allowlist value rule — `per` is unwrapped & not on allowlist.
        line = (
            "- Why anomaly = `review-loop:*` subagent_type: per CLAUDE.md "
            "§\"Plugin agent type sandbox bug (CRITICAL)\", any plugin-defined agent type "
            "silently runs with `tool_uses: 0` and hallucinated output. "
            "A `review-loop:*` subagent_type appearing in a session artifact means either "
            "(a) the orchestrator regressed to the broken pattern in that round, or "
            "(b) the artifact records a historical broken call worth flagging for retroactive audit. "
            "Either way it is an anomaly worth surfacing.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "fp2.md", line)
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 0)
            rec = _file_record(report, "fp2.md")
            self.assertEqual(rec["counts"], {})
            self.assertFalse(rec["anomaly"])

    def test_WC1_wildcard_placeholder_preserves_full_value(self):
        # Source: .review-loop/sessions/b3c76110-be33-4f78-b9c9-2c636a10942a.md:122
        # Locks `*` preservation in extended char class.
        line = (
            "- Sandbox-bug scan: 13 `subagent_type: review-loop:*` hits, "
            "all in guard/warning context (zero actual invocations).\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "wc1.md", line)
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 1)
            rec = _file_record(report, "wc1.md")
            self.assertEqual(rec["counts"], {"review-loop:*": 1})
            self.assertTrue(rec["anomaly"])

    def test_WC2_dual_value_verbatim_from_claude_md_line_13(self):
        # Source: CLAUDE.md:13 (project's repo CLAUDE.md, also referenced
        # by 78edcbdd:139). Locks `<>` preservation AND verifies dual-value
        # counts on a single line.
        line = (
            "**Rule**: When adding ANY new agent invocation, always use "
            "`subagent_type: general-purpose` with inlined body. "
            "Never use `subagent_type: review-loop:<name>`.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "wc2.md", line)
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 1)
            rec = _file_record(report, "wc2.md")
            self.assertEqual(
                rec["counts"], {"general-purpose": 1, "review-loop:<name>": 1}
            )
            self.assertTrue(rec["anomaly"])
            self.assertEqual(rec["anomaly_values"], ["review-loop:<name>"])

    def test_BP1_bare_prefix_no_agent_name_must_not_match(self):
        # Source: .review-loop/sessions/b1e5ecca-6bc1-43cb-b6d3-c8b5174e60ca.md:13
        # Locks closed-set requirement on secondary regex — `review-loop:`
        # followed by space (no closed-set name) must not match.
        line = (
            "- `tool_use_agent_subagent_type_forbidden` — blacklists "
            "`review-loop:` subagent_type prefix in actual `tool_use` events.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "bp1.md", line)
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 0)
            rec = _file_record(report, "bp1.md")
            self.assertEqual(rec["counts"], {})
            self.assertFalse(rec["anomaly"])

    def test_KN1_real_bare_known_agent_name_via_secondary_regex(self):
        # Source: .review-loop/sessions/b1e5ecca-6bc1-43cb-b6d3-c8b5174e60ca.md:154
        # REAL bare-form (no `subagent_type:` precedent on this line).
        # Primary's `bare` allowlist requires the `\bsubagent_type\s*:\s*`
        # left anchor, so neither `general-purpose` nor `review-loop:reviewer`
        # is matched by primary; secondary catches `review-loop:reviewer`.
        line = (
            "     - `ToolUseAgentSubagentTypeWhitelistTest` (4): all general-purpose pass;"
            " review-loop:reviewer fail (regression); mixed pass+fail rejection;"
            " no `subagent_type` events vacuous-pass.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "kn1.md", line)
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 1)
            rec = _file_record(report, "kn1.md")
            self.assertEqual(rec["counts"], {"review-loop:reviewer": 1})
            self.assertTrue(rec["anomaly"])

    def test_LEG_paren_wrapped_general_purpose(self):
        # Source: .review-loop/sessions/b3c76110-be33-4f78-b9c9-2c636a10942a.md:126
        # Locks unwrapped `general-purpose` allowlist branch when preceded
        # by `subagent_type:` (paren-wrapped accounting form).
        line = (
            "- Executor backend: Agent (subagent_type: general-purpose), "
            "tool_uses: 70 (hallucination guard passed)\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "leg.md", line)
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 0)
            rec = _file_record(report, "leg.md")
            self.assertEqual(rec["counts"], {"general-purpose": 1})
            self.assertFalse(rec["anomaly"])


class DedupTest(unittest.TestCase):
    """Locks span-overlap dedup rule from scan_line pseudocode."""

    def test_double_count_primary_and_secondary_dedup(self):
        # Constructed (no real corpus line both wraps the whole block in
        # backticks AND has a closed-set agent name). Primary `bare` branch
        # captures `review-loop:executor`; secondary's match for the same
        # token is contained within the primary span and is SKIPPED.
        line = "Test on line: `subagent_type: review-loop:executor` here.\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "dc1.md", line)
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 1)
            rec = _file_record(report, "dc1.md")
            self.assertEqual(rec["counts"], {"review-loop:executor": 1})
            self.assertTrue(rec["anomaly"])
            # And exactly one anomaly site (not two).
            self.assertEqual(len(rec["anomaly_sites"]), 1)


class CliShapeTest(unittest.TestCase):
    """Locks CLI flags --text and --exit-zero."""

    def test_text_flag_renders_table_and_keeps_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "anom.md",
                "  subagent_type: review-loop:executor\n",
            )
            code, _, raw = run_parser(tmpdir, "--text")
            self.assertEqual(code, 1)
            # Output must NOT be JSON.
            stripped = raw.lstrip()
            self.assertFalse(stripped.startswith("{"))
            self.assertIn("review-loop:executor", raw)

    def test_exit_zero_flag_suppresses_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "anom.md",
                "  subagent_type: review-loop:executor\n",
            )
            code, report, _ = run_parser(tmpdir, "--exit-zero")
            self.assertEqual(code, 0)
            # Anomaly is still reported in the JSON.
            self.assertEqual(report["summary"]["files_with_anomaly"], 1)


if __name__ == "__main__":
    unittest.main()
