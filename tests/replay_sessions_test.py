"""Unit tests for scripts/replay_sessions.py.

29 cases:
  - 4 AC tests (clean / anomaly / mixed / no-tokens)
  - 7 corpus-grounded regex-locking tests (FP1, FP2, WC1, WC2, BP1, KN1, LEG)
  - 1 dedup test (primary + secondary span overlap)
  - 2 CLI-shape tests (--text, --exit-zero)
  - 7 plumbing-edge tests (CLI exit codes, multi-file aggregation, empty
    directory, multi-line counts, anomaly-site fidelity, JSON shape invariants)
  - 5 in-process scan_line unit tests
  - 3 in-process build_report unit tests

Stdlib unittest. Mirrors tests/run_skill_lint_test.py style: subprocess.run
+ tempfile + a small write_fixture helper. The plumbing-edge and unit
classes also exercise replay_sessions module functions directly via
sys.path injection.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
                },
            )
            self.assertEqual(len(report["summary"]), 3)
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


if __name__ == "__main__":
    unittest.main()
