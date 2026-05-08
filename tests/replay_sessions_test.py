"""Unit tests for scripts/replay_sessions.py.

47 cases:
  - 4 AC tests (clean / anomaly / mixed / no-tokens)
  - 7 corpus-grounded regex-locking tests (FP1, FP2, WC1, WC2, BP1, KN1, LEG)
  - 1 dedup test (primary + secondary span overlap)
  - 2 CLI-shape tests (--text, --exit-zero)
  - 7 plumbing-edge tests (CLI exit codes, multi-file aggregation, empty
    directory, multi-line counts, anomaly-site fidelity, JSON shape invariants)
  - 5 in-process scan_line unit tests
  - 3 in-process build_report unit tests
  - 11 second-tier and third-tier coverage tests (sq/dq/bt quoted regex
    branches with white-box named-group assertions, secondary-regex
    left-boundary NOT-enforced + matches-after-whitespace pins,
    secondary-regex right-boundary extended-token block, render_text
    path-truncation branch, errors=replace UTF-8 decode, *.md
    non-recursive glob, anomaly_values set-dedup, --text full layout
    rendering pinning)
  - 1 run_parser helper contract test (AssertionError on non-JSON stdout)
  - 6 unreadable-file tests (OSError parent-class catch, exit-3 vs
    exit-1 priority, --exit-zero suppression, files-list omission,
    stderr line format + glob-sort order, FileNotFoundError race
    falls through OSError catch)

Stdlib unittest. Mirrors tests/run_skill_lint_test.py style: subprocess.run
+ tempfile + a small write_fixture helper. The plumbing-edge, unit,
second-tier, run-parser-helper-contract, and unreadable-file classes
also exercise replay_sessions module functions directly via sys.path
injection.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "replay_sessions.py"

sys.path.insert(0, str(ROOT / "scripts"))
import replay_sessions  # noqa: E402


def write_fixture(dirpath: Path, name: str, content: str) -> Path:
    target = dirpath / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def run_parser(root_dir: Path, *flags: str):
    """Shell out to the parser; return (exit_code, parsed_stdout_or_text, raw_stdout).

    Raises AssertionError on JSON parse failure with stdout/stderr embedded
    in the message, so a parser-side regression that breaks JSON output
    surfaces a clean failure at the assertion site rather than a confusing
    downstream `TypeError: 'NoneType' object is not subscriptable` from a
    caller indexing `parsed["..."]`.
    """
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
        raise AssertionError(
            f"replay_sessions parser produced non-JSON stdout (returncode={completed.returncode!r}):\n"
            f"--- stdout ---\n{raw}\n"
            f"--- stderr ---\n{completed.stderr}"
        )
    return completed.returncode, parsed, raw


def _file_record(report: dict, basename: str) -> dict:
    """Find the file record whose path ends in basename."""
    for entry in report["files"]:
        if entry["path"].endswith(basename):
            return entry
    raise AssertionError(f"no file record for {basename} in {report}")


class RunParserHelperContractTest(unittest.TestCase):
    """AC-3b.3 — locks the helper contract that `run_parser` raises
    AssertionError carrying subprocess stdout/stderr when the parser
    produces non-JSON stdout. Replaces the prior silent `parsed = None`
    fallback that surfaced as a confusing downstream `TypeError` at the
    caller's `parsed["summary"][...]` site."""

    def test_run_parser_raises_assertionerror_on_non_json_stdout(self):
        global SCRIPT
        original_script = SCRIPT
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fake_script = tmpdir / "fake_parser.py"
            fake_script.write_text(
                "import sys\n"
                'print("not json")\n'
                'print("oops", file=sys.stderr)\n'
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            SCRIPT = fake_script
            try:
                with self.assertRaises(AssertionError) as cm:
                    run_parser(tmpdir)
                msg = str(cm.exception)
                self.assertIn("not json", msg)
                self.assertIn("oops", msg)
            finally:
                SCRIPT = original_script


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


class PlumbingEdgeTest(unittest.TestCase):
    """7 MEDIUM coverage gaps surfaced by pr-test-analyzer's v2.6.25 quality-polish pass."""

    def test_root_nonexistent_path_exit_code_2(self):
        # Gap (i) part A: --root pointing at a path that does not exist.
        # Use TemporaryDirectory and exit the with-block to guarantee deletion.
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = tmp
        # bad_path is now a deleted directory.
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", bad_path],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn(
            "replay_sessions: root not found or not a directory:", completed.stderr
        )
        self.assertIn(bad_path, completed.stderr)

    def test_root_non_directory_exit_code_2(self):
        # Gap (i) part B: --root pointing at a regular file (not a directory).
        tmp_file = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
        tmp_file.close()
        bad_path = tmp_file.name
        try:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--root", bad_path],
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(completed.stdout, "")
            self.assertIn(
                "replay_sessions: root not found or not a directory:",
                completed.stderr,
            )
            self.assertIn(bad_path, completed.stderr)
        finally:
            os.unlink(bad_path)

    def test_multi_file_aggregation_counts(self):
        # Gap (ii): three .md files — one clean, two with anomalies.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "clean.md",
                "  subagent_type: general-purpose\n",
            )
            write_fixture(
                tmpdir,
                "bad1.md",
                "  subagent_type: review-loop:executor\n",
            )
            write_fixture(
                tmpdir,
                "bad2.md",
                "line 1: subagent_type: review-loop:reviewer\n"
                "line 2: subagent_type: review-loop:reviewer\n",
            )
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 1)
            self.assertEqual(report["summary"]["files_scanned"], 3)
            self.assertEqual(report["summary"]["files_with_anomaly"], 2)
            self.assertEqual(report["summary"]["total_anomaly_occurrences"], 3)
            # Locked by sorted(root.glob("*.md")) in build_report.
            basenames = [Path(r["path"]).name for r in report["files"]]
            self.assertEqual(basenames, ["bad1.md", "bad2.md", "clean.md"])

    def test_empty_directory_contract(self):
        # Gap (iii): empty directory exits 0, summary keys exactly 3.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 0)
            self.assertEqual(report["files"], [])
            self.assertEqual(
                report["summary"],
                {
                    "files_scanned": 0,
                    "files_with_anomaly": 0,
                    "total_anomaly_occurrences": 0,
                    "unreadable_files": 0,
                },
            )
            self.assertEqual(len(report["summary"]), 4)
            # Anti-assertion: mtime is per-file, never in summary.
            self.assertNotIn("mtime", report["summary"])

    def test_same_value_multiple_lines_counts(self):
        # Gap (iv): three identical lines accumulate to count 3.
        # Cross-coverage: anomaly multi-line accumulation is structurally
        # covered by the bad2.md sub-case in test_multi_file_aggregation_counts.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "repeat.md",
                "  subagent_type: general-purpose\n"
                "  subagent_type: general-purpose\n"
                "  subagent_type: general-purpose\n",
            )
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 0)
            rec = _file_record(report, "repeat.md")
            self.assertEqual(rec["counts"], {"general-purpose": 3})
            self.assertFalse(rec["anomaly"])

    def test_anomaly_sites_line_number_fidelity(self):
        # Gap (v): anomaly_sites are insertion-ordered (ascending line numbers).
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "sites.md",
                "intro line 1\n"
                "  subagent_type: review-loop:executor\n"
                "intro line 3\n"
                "  subagent_type: review-loop:reviewer\n"
                "intro line 5\n"
                "  subagent_type: review-loop:executor\n",
            )
            code, report, _ = run_parser(tmpdir)
            self.assertEqual(code, 1)
            rec = _file_record(report, "sites.md")
            self.assertEqual(
                rec["anomaly_sites"],
                [
                    {"value": "review-loop:executor", "line": 2},
                    {"value": "review-loop:reviewer", "line": 4},
                    {"value": "review-loop:executor", "line": 6},
                ],
            )
            self.assertEqual(
                rec["counts"],
                {"review-loop:executor": 2, "review-loop:reviewer": 1},
            )
            self.assertEqual(
                rec["anomaly_values"],
                ["review-loop:executor", "review-loop:reviewer"],
            )

    def test_json_shape_sort_keys_invariant(self):
        # Gap (vi): pin sort_keys=True ordering at JSON-emit time.
        # Per-file `rec['mtime']` is the regex-checked field; `summary`
        # has no `mtime` key.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "one.md",
                "  subagent_type: general-purpose\n",
            )
            code, report, raw = run_parser(tmpdir)
            self.assertEqual(code, 0)

            # Raw-stdout pinning (locks sort_keys=True at JSON-emit time).
            self.assertTrue(raw.startswith("{\n"))
            # First key after `{\n  ` is `"files":` (alphabetical: files < summary).
            self.assertTrue(
                raw.startswith('{\n  "files":'),
                f"unexpected stdout prefix: {raw[:40]!r}",
            )
            # Per-file dict's first key is `"anomaly":` (alphabetical first of six).
            self.assertIn('"files": [\n    {\n      "anomaly":', raw)

            # Parsed-dict full-key-list pinning at all three levels.
            self.assertEqual(list(report.keys()), ["files", "summary"])
            self.assertEqual(
                list(report["files"][0].keys()),
                [
                    "anomaly",
                    "anomaly_sites",
                    "anomaly_values",
                    "counts",
                    "mtime",
                    "path",
                ],
            )
            self.assertEqual(
                list(report["summary"].keys()),
                [
                    "files_scanned",
                    "files_with_anomaly",
                    "total_anomaly_occurrences",
                    "unreadable_files",
                ],
            )

            # Per-file mtime regex (ISO 8601 UTC with +00:00 suffix).
            self.assertIsNotNone(
                re.match(
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00$",
                    report["files"][0]["mtime"],
                )
            )
            # Anti-assertion: mtime is per-file (set in scan_file), not in summary.
            self.assertNotIn("mtime", report["summary"])


class ScanLineUnitTest(unittest.TestCase):
    """Gap (vii) part A: in-process unit tests for scan_line."""

    def test_scan_line_general_purpose_increments_counts_only(self):
        hits: dict = {}
        sites: list = []
        replay_sessions.scan_line(
            "  subagent_type: general-purpose", 42, hits, sites
        )
        self.assertEqual(hits, {"general-purpose": 1})
        self.assertEqual(sites, [])

    def test_scan_line_review_loop_appends_site_with_line_no(self):
        hits: dict = {}
        sites: list = []
        replay_sessions.scan_line(
            "  subagent_type: review-loop:executor", 7, hits, sites
        )
        self.assertEqual(hits, {"review-loop:executor": 1})
        self.assertEqual(sites, [{"value": "review-loop:executor", "line": 7}])

    def test_scan_line_no_match_leaves_hits_and_sites_unchanged(self):
        hits: dict = {}
        sites: list = []
        replay_sessions.scan_line("random prose with no token", 1, hits, sites)
        self.assertEqual(hits, {})
        self.assertEqual(sites, [])

    def test_scan_line_dedup_within_one_line(self):
        # Locks the span-overlap dedup invariant in-process (DC1 fixture).
        hits: dict = {}
        sites: list = []
        replay_sessions.scan_line(
            "Test on line: `subagent_type: review-loop:executor` here.",
            99,
            hits,
            sites,
        )
        self.assertEqual(hits, {"review-loop:executor": 1})
        self.assertEqual(len(sites), 1)

    def test_scan_line_accumulates_across_calls(self):
        # Locks the (value, line) accumulation contract from gap iv probe.
        hits: dict = {}
        sites: list = []
        for line_no in (10, 20, 30):
            replay_sessions.scan_line(
                "  subagent_type: general-purpose", line_no, hits, sites
            )
        self.assertEqual(hits, {"general-purpose": 3})


class BuildReportUnitTest(unittest.TestCase):
    """Gap (vii) part B: in-process unit tests for build_report."""

    def test_build_report_empty_directory_returns_empty_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = replay_sessions.build_report(Path(tmp))
            self.assertEqual(
                report,
                {
                    "files": [],
                    "summary": {
                        "files_scanned": 0,
                        "files_with_anomaly": 0,
                        "total_anomaly_occurrences": 0,
                        "unreadable_files": 0,
                    },
                },
            )

    def test_build_report_skips_non_md_files(self):
        # Locks the *.md-only single-level glob contract.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "notes.txt", "irrelevant text\n")
            write_fixture(tmpdir, "data.json", '{"k": "v"}\n')
            write_fixture(
                tmpdir,
                "target.md",
                "  subagent_type: review-loop:executor\n",
            )
            report = replay_sessions.build_report(tmpdir)
            self.assertEqual(len(report["files"]), 1)
            self.assertTrue(report["files"][0]["path"].endswith("target.md"))

    def test_build_report_aggregation_summary_matches_per_file_records(self):
        # Locks the aggregation invariant in-process: summary fields equal
        # the recomputation from the per-file records.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "a.md",
                "  subagent_type: general-purpose\n",
            )
            write_fixture(
                tmpdir,
                "b.md",
                "  subagent_type: review-loop:executor\n",
            )
            write_fixture(
                tmpdir,
                "c.md",
                "  subagent_type: review-loop:reviewer\n"
                "  subagent_type: review-loop:reviewer\n",
            )
            report = replay_sessions.build_report(tmpdir)
            summary = report["summary"]
            self.assertEqual(summary["files_scanned"], len(report["files"]))
            self.assertEqual(
                summary["files_with_anomaly"],
                sum(1 for f in report["files"] if f["anomaly"]),
            )
            self.assertEqual(
                summary["total_anomaly_occurrences"],
                sum(len(f["anomaly_sites"]) for f in report["files"]),
            )


class SecondTierCoverageTest(unittest.TestCase):
    """Second-tier coverage tests (v2.6.26 + v2.6.30 third-tier additions).

    v2.6.26 Step 3.5.5 surfaced six MEDIUM gaps closed in v2.6.28; v2.6.30
    extends with `bt` quoted branch, secondary-regex left-boundary
    behavior pin (negative + positive), and render_text path-truncation
    branch.
    """

    def test_sq_quoted_value_general_purpose(self):
        # Gap (1A): pin SUBAGENT_TYPE_RE `sq` (single-quoted) branch for the
        # `general-purpose` allowlist value. In-process scan_line.
        hits: dict = {}
        sites: list = []
        replay_sessions.scan_line(
            "  subagent_type: 'general-purpose'", 1, hits, sites
        )
        self.assertEqual(hits, {"general-purpose": 1})
        self.assertEqual(sites, [])

    def test_dq_quoted_value_review_loop_executor(self):
        # Gap (1B): pin SUBAGENT_TYPE_RE `dq` (double-quoted) branch for the
        # `review-loop:executor` value. In-process scan_line.
        hits: dict = {}
        sites: list = []
        replay_sessions.scan_line(
            '  subagent_type: "review-loop:executor"', 7, hits, sites
        )
        self.assertEqual(hits, {"review-loop:executor": 1})
        self.assertEqual(sites, [{"value": "review-loop:executor", "line": 7}])

    def test_bt_quoted_value_review_loop_executor(self):
        # AC-4.1 (Gap 1, v2.6.30) — pin SUBAGENT_TYPE_RE `bt` (backtick)
        # branch for the `review-loop:executor` value. Symmetric with the
        # `sq` and `dq` branches above. In-process scan_line.
        #
        # NOTE (test-theater fix, R1 CRITICAL): the `hits`/`sites`
        # assertions below remain valid integration coverage, but they
        # are NOT sufficient to pin the `bt` branch in isolation —
        # `BARE_REVIEW_LOOP_RE` (the secondary regex) ALSO matches
        # `review-loop:executor` inside the backticks, so removing the
        # `bt` arm from `SUBAGENT_TYPE_RE` would still leave hits/sites
        # populated. The white-box `SUBAGENT_TYPE_RE` assertions below
        # close that hole by directly checking the named-capture groups.
        line = "  subagent_type: `review-loop:executor`"

        # White-box: assert the primary regex `bt` arm fires standalone.
        m = replay_sessions.SUBAGENT_TYPE_RE.search(line)
        self.assertIsNotNone(
            m,
            "SUBAGENT_TYPE_RE must match the backtick-wrapped value",
        )
        self.assertEqual(
            m.group("bt"),
            "review-loop:executor",
            "the `bt` named-capture group must fire on backtick-quoted value",
        )
        self.assertIsNone(
            m.group("sq"),
            "the `sq` group must NOT fire on backtick-quoted value",
        )
        self.assertIsNone(
            m.group("dq"),
            "the `dq` group must NOT fire on backtick-quoted value",
        )

        # Integration: hits/sites still populated end-to-end (kept as
        # additional coverage, but NOT the primary assertion for this AC).
        hits: dict = {}
        sites: list = []
        replay_sessions.scan_line(line, 11, hits, sites)
        self.assertEqual(hits, {"review-loop:executor": 1})
        self.assertEqual(sites, [{"value": "review-loop:executor", "line": 11}])

    def test_secondary_regex_left_boundary_NOT_enforced_pins_current_behavior(self):
        # AC-4.2 (Gap 2 negative, v2.6.30) — pins current behavior that the
        # secondary regex `BARE_REVIEW_LOOP_RE` (replay_sessions.py:49-52)
        # lacks a left-boundary lookbehind. A future tightening to add
        # `(?<![A-Za-z0-9_\-])` would intentionally fail this test, prompting
        # the right conversation. Out of bundle scope per AC-3b.4.
        hits: dict = {}
        sites: list = []
        replay_sessions.scan_line(
            "the xreview-loop:executor briefly", 1, hits, sites
        )
        self.assertEqual(hits, {"review-loop:executor": 1})
        self.assertEqual(
            sites, [{"value": "review-loop:executor", "line": 1}]
        )

    def test_secondary_regex_left_boundary_matches_after_whitespace(self):
        # AC-4.2 (Gap 2 positive, v2.6.30) — pin the legitimate
        # whitespace-prefixed left-boundary occurrence: a real bare-form
        # `review-loop:executor` mention preceded by a space matches.
        hits: dict = {}
        sites: list = []
        replay_sessions.scan_line(
            "talking about review-loop:executor briefly", 3, hits, sites
        )
        self.assertEqual(hits, {"review-loop:executor": 1})
        self.assertEqual(
            sites, [{"value": "review-loop:executor", "line": 3}]
        )

    def test_render_text_truncates_path_when_over_fifty_chars(self):
        # AC-4.5 (Gap 5, v2.6.30) — pin the render_text path-truncation
        # branch at scripts/replay_sessions.py:153-154
        # (`if len(path_short) > 50: path_short = "..." + path_short[-47:]`).
        # `build_report` uses single-level `root.glob("*.md")`, so the long
        # path is a long *filename* directly under tmpdir, not a long subdir.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fixture = write_fixture(
                tmpdir,
                "a" * 60 + ".md",
                "  subagent_type: review-loop:executor\n",
            )
            self.assertGreater(
                len(str(fixture)),
                50,
                "fixture path must exceed 50 chars to exercise the truncation branch",
            )
            _, _, raw = run_parser(tmpdir, "--text")
            truncated = "..." + str(fixture)[-47:]
            self.assertIn(truncated, raw)
            self.assertNotIn(str(fixture), raw)

    def test_secondary_regex_right_boundary_blocks_extended_token(self):
        # Gap (2): pin BARE_REVIEW_LOOP_RE right-boundary lookahead
        # `(?![A-Za-z0-9_\-])` at scripts/replay_sessions.py:51. Appending
        # an alpha char to a closed-set agent name must block the match.
        hits: dict = {}
        sites: list = []
        replay_sessions.scan_line(
            "talking about review-loop:executorx briefly", 1, hits, sites
        )
        self.assertEqual(hits, {})
        self.assertEqual(sites, [])

    def test_decode_errors_replace_does_not_crash_on_invalid_utf8(self):
        # Gap (3): pin `errors="replace"` decode at scan_file line 105.
        # Surrounding lines parse normally even with invalid UTF-8 bytes
        # in between.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            bad = tmpdir / "bad.md"
            bad.write_bytes(
                b"  subagent_type: review-loop:executor\n"
                b"\xff\xfeoops\n"
                b"  subagent_type: general-purpose\n"
            )
            rec = replay_sessions.scan_file(bad)
            self.assertEqual(
                rec["counts"],
                {"review-loop:executor": 1, "general-purpose": 1},
            )
            self.assertTrue(rec["anomaly"])
            self.assertEqual(rec["anomaly_values"], ["review-loop:executor"])

    def test_glob_is_single_level_not_recursive(self):
        # Gap (4): pin single-level `sorted(root.glob("*.md"))` at
        # scripts/replay_sessions.py:127. Files inside subdirectories
        # must be ignored.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "top.md",
                "  subagent_type: review-loop:executor\n",
            )
            write_fixture(
                tmpdir,
                "sub/nested.md",
                "  subagent_type: review-loop:reviewer\n",
            )
            self.assertTrue(
                (tmpdir / "sub" / "nested.md").exists(),
                "fixture file must exist on disk to validate the glob skip is real",
            )
            report = replay_sessions.build_report(tmpdir)
            self.assertEqual(len(report["files"]), 1)
            self.assertTrue(report["files"][0]["path"].endswith("top.md"))
            self.assertEqual(report["summary"]["files_scanned"], 1)

    def test_anomaly_values_dedups_and_sorts(self):
        # Gap (5): pin `sorted({s["value"] for s in sites})` at scan_file
        # line 110 — set-dedup + alphabetical sort. Drives scan_file
        # end-to-end with a 5-line fixture (3 executor, 2 reviewer
        # interleaved). If line 110 ever changes from `sorted({...})` to
        # `list({...})` (no sort) or `sorted(s["value"] for s in sites)`
        # (no dedup), only this test would fail.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            fixture = write_fixture(
                tmpdir,
                "fix.md",
                "  subagent_type: review-loop:executor\n"
                "  subagent_type: review-loop:reviewer\n"
                "  subagent_type: review-loop:executor\n"
                "  subagent_type: review-loop:reviewer\n"
                "  subagent_type: review-loop:executor\n",
            )
            rec = replay_sessions.scan_file(fixture)
            self.assertEqual(
                rec["anomaly_values"],
                ["review-loop:executor", "review-loop:reviewer"],
            )
            self.assertEqual(len(rec["anomaly_sites"]), 5)
            self.assertEqual(
                rec["counts"],
                {"review-loop:executor": 3, "review-loop:reviewer": 2},
            )

    def test_render_text_full_layout_pinning(self):
        # Gap (6): pin render_text full layout via subprocess --text path.
        # Title + separator, header f-string, per-site arrow lines, footer
        # summary string, plus two horizontal rules.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "a.md",
                "  subagent_type: review-loop:executor\n"
                "  subagent_type: review-loop:reviewer\n",
            )
            write_fixture(
                tmpdir,
                "b.md",
                "  subagent_type: general-purpose\n",
            )
            code, _, raw = run_parser(tmpdir, "--text")
            self.assertEqual(code, 1)
            # Title + separator pinning.
            self.assertTrue(
                raw.startswith("Session-replay parser report\n" + "=" * 60 + "\n"),
                f"unexpected stdout prefix: {raw[:80]!r}",
            )
            # Header f-string pinning.
            self.assertIn(f"{'PATH':<50}  {'ANOM':<5}  COUNTS", raw)
            # Per-site arrow lines pinning.
            self.assertIn("  -> line 1: review-loop:executor", raw)
            self.assertIn("  -> line 2: review-loop:reviewer", raw)
            # Footer summary string pinning.
            self.assertIn(
                "Summary: scanned=2  with_anomaly=1  total_anomaly_occurrences=2",
                raw,
            )
            # Horizontal rule pinning (rendered twice in the layout).
            self.assertIn("-" * 60, raw)


class UnreadableFileTest(unittest.TestCase):
    """P3-3a coverage: unreadable-file handling, exit-3 contract, stderr format.

    All tests run in-process via replay_sessions.scan_file / build_report /
    main with unittest.mock.patch.object(Path, "read_text", side_effect=...).
    Subprocess can't see Python-side patches, so the in-process style mirrors
    BuildReportUnitTest above (lines 620-684). The side_effect callable
    inspects `self.name` and raises selectively for targeted basenames,
    falling through to the original Path.read_text for other files.
    """

    @staticmethod
    def _selective_read_text(targets, exc_factory):
        """Return a side_effect callable that raises `exc_factory(self)` when
        `self.name` is in `targets`, otherwise delegates to the real
        Path.read_text. `targets` is an iterable of basename strings.
        """
        original = Path.read_text
        target_set = set(targets)

        def side_effect(self, *args, **kwargs):
            if self.name in target_set:
                raise exc_factory(self)
            return original(self, *args, **kwargs)

        return side_effect

    def test_unreadable_file_alone_exits_3(self):
        # Single unreadable file -> exit 3, summary["unreadable_files"]==1.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "locked.md", "  subagent_type: general-purpose\n")
            side_effect = self._selective_read_text(
                {"locked.md"},
                lambda p: PermissionError(13, "Permission denied", str(p)),
            )
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with patch.object(Path, "read_text", autospec=True, side_effect=side_effect), \
                 contextlib.redirect_stdout(buf_out), \
                 contextlib.redirect_stderr(buf_err):
                code = replay_sessions.main([
                    "--root", str(tmpdir),
                ])
            self.assertEqual(code, 3)
            report = json.loads(buf_out.getvalue())
            self.assertEqual(report["summary"]["unreadable_files"], 1)
            self.assertEqual(report["summary"]["files_scanned"], 0)

    def test_unreadable_with_anomaly_still_exits_3(self):
        # Q2 pin: exit-3 outranks exit-1 when both anomaly and unreadable
        # are present.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(
                tmpdir,
                "anom.md",
                "  subagent_type: review-loop:executor\n",
            )
            write_fixture(
                tmpdir,
                "clean.md",
                "  subagent_type: general-purpose\n",
            )
            write_fixture(tmpdir, "locked.md", "irrelevant body\n")
            side_effect = self._selective_read_text(
                {"locked.md"},
                lambda p: PermissionError(13, "Permission denied", str(p)),
            )
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with patch.object(Path, "read_text", autospec=True, side_effect=side_effect), \
                 contextlib.redirect_stdout(buf_out), \
                 contextlib.redirect_stderr(buf_err):
                code = replay_sessions.main(["--root", str(tmpdir)])
            self.assertEqual(code, 3)
            report = json.loads(buf_out.getvalue())
            self.assertGreaterEqual(report["summary"]["files_with_anomaly"], 1)
            self.assertEqual(report["summary"]["unreadable_files"], 1)

    def test_exit_zero_suppresses_unreadable_exit_3(self):
        # Q3 pin: --exit-zero forces 0 even when unreadable_files > 0.
        # JSON still reports the count truthfully.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "locked.md", "  subagent_type: general-purpose\n")
            side_effect = self._selective_read_text(
                {"locked.md"},
                lambda p: PermissionError(13, "Permission denied", str(p)),
            )
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with patch.object(Path, "read_text", autospec=True, side_effect=side_effect), \
                 contextlib.redirect_stdout(buf_out), \
                 contextlib.redirect_stderr(buf_err):
                code = replay_sessions.main([
                    "--root", str(tmpdir), "--exit-zero",
                ])
            self.assertEqual(code, 0)
            report = json.loads(buf_out.getvalue())
            self.assertEqual(report["summary"]["unreadable_files"], 1)

    def test_unreadable_files_skipped_from_report_files_list(self):
        # Q4 pin: unreadable files do NOT appear in report["files"].
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "good.md", "  subagent_type: general-purpose\n")
            write_fixture(tmpdir, "bad.md", "irrelevant body\n")
            side_effect = self._selective_read_text(
                {"bad.md"},
                lambda p: PermissionError(13, "Permission denied", str(p)),
            )
            buf_err = io.StringIO()
            with patch.object(Path, "read_text", autospec=True, side_effect=side_effect), \
                 contextlib.redirect_stderr(buf_err):
                report = replay_sessions.build_report(tmpdir)
            self.assertEqual(len(report["files"]), 1)
            self.assertTrue(report["files"][0]["path"].endswith("good.md"))
            self.assertEqual(report["summary"]["unreadable_files"], 1)
            self.assertEqual(report["summary"]["files_scanned"], 1)

    def test_stderr_line_format_and_glob_sort_order(self):
        # AC-3 pin: one stderr line per unreadable file, stable prefix
        # `replay_sessions: unreadable file: `, in glob-sort order.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "a.md", "irrelevant\n")
            write_fixture(tmpdir, "b.md", "  subagent_type: general-purpose\n")
            write_fixture(tmpdir, "c.md", "irrelevant\n")
            side_effect = self._selective_read_text(
                {"a.md", "c.md"},
                lambda p: PermissionError(13, "Permission denied", str(p)),
            )
            buf_err = io.StringIO()
            with patch.object(Path, "read_text", autospec=True, side_effect=side_effect), \
                 contextlib.redirect_stderr(buf_err):
                replay_sessions.build_report(tmpdir)
            err = buf_err.getvalue()
            err_lines = [ln for ln in err.splitlines() if ln]
            self.assertEqual(len(err_lines), 2)
            for line in err_lines:
                self.assertTrue(
                    line.startswith("replay_sessions: unreadable file: "),
                    f"unexpected stderr line prefix: {line!r}",
                )
            # Glob-sort order: a.md before c.md.
            a_idx = next(i for i, ln in enumerate(err_lines) if "a.md" in ln)
            c_idx = next(i for i, ln in enumerate(err_lines) if "c.md" in ln)
            self.assertLess(a_idx, c_idx)

    def test_filenotfound_race_falls_through_oserror(self):
        # AC-1 enumeration pin: catch is `OSError`, not `PermissionError`-only.
        # FileNotFoundError (a glob/delete race) must also be handled.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            write_fixture(tmpdir, "racer.md", "  subagent_type: general-purpose\n")
            side_effect = self._selective_read_text(
                {"racer.md"},
                lambda p: FileNotFoundError(2, "No such file or directory", str(p)),
            )
            buf_err = io.StringIO()
            with patch.object(Path, "read_text", autospec=True, side_effect=side_effect), \
                 contextlib.redirect_stderr(buf_err):
                report = replay_sessions.build_report(tmpdir)
            self.assertEqual(report["summary"]["unreadable_files"], 1)
            self.assertEqual(len(report["files"]), 0)
            self.assertIn("racer.md", buf_err.getvalue())


if __name__ == "__main__":
    unittest.main()
