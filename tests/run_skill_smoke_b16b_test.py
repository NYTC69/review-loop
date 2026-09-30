import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_skill_smoke_lib import (
    finalize_stream_capture_artifact,
    last_round_direct_noop,
    parse_stream_json_capture,
    load_gate,
)

EMPTY = "| 0 | 0 | (no attributable change) | — | — |\n"
CHANGED = "| 0 | 1 | `README.md` | aaa | bbb |\n"
HEADER = "| snapshot pre | snapshot post | path | pre_blob | post_blob |\n|---|---|---|---|---|\n"
META = "\n## Session Metadata\n- entry_point: execute\n"


def packet(rows):
    return "## Current Review Packet\n\n### Attributable Delta\n" + HEADER + rows + "\n"


def round_entry(n, route):
    return f"### Execution Round {n}\n- Author route: {route}\n- Reviewer verdict: APPROVE\n\n"


def session(rounds, rows, inline_delta=None):
    history = "## Review History\n\n"
    for n, route in rounds:
        history += round_entry(n, route)
    if inline_delta is not None:
        history = history.rstrip("\n") + "\n### Attributable Delta\n" + HEADER + inline_delta + "\n"
    return packet(rows) + history + META


class LastRoundDirectNoopTest(unittest.TestCase):
    def test_single_direct_noop_round_passes(self):
        ok, _ = last_round_direct_noop(session([(1, "orchestrator-direct")], EMPTY))
        self.assertTrue(ok)

    def test_earlier_direct_noop_round_does_not_cover_a_later_changing_round(self):
        text = session([(1, "orchestrator-direct"), (2, "executor")], CHANGED)
        self.assertFalse(last_round_direct_noop(text)[0])
        # Round 2 claims direct but the packet (round 2's delta) shows a change: still FAIL.
        text = session([(1, "orchestrator-direct"), (2, "orchestrator-direct")], CHANGED)
        self.assertFalse(last_round_direct_noop(text)[0])
        # Round 2 is an executor round even though the packet is empty: round 1's route must not count.
        text = session([(1, "orchestrator-direct"), (2, "executor")], EMPTY)
        self.assertFalse(last_round_direct_noop(text)[0])

    def test_round_numbers_decide_which_round_is_last(self):
        text = session([(2, "executor"), (1, "orchestrator-direct")], EMPTY)
        self.assertFalse(last_round_direct_noop(text)[0])

    def test_empty_marker_next_to_a_change_row_fails(self):
        text = session([(1, "orchestrator-direct")], EMPTY + CHANGED)
        self.assertFalse(last_round_direct_noop(text)[0])

    def test_inline_round_delta_takes_precedence_over_the_packet(self):
        ok = session([(1, "orchestrator-direct")], CHANGED, inline_delta=EMPTY)
        bad = session([(1, "orchestrator-direct")], EMPTY, inline_delta=CHANGED)
        self.assertTrue(last_round_direct_noop(ok)[0])
        self.assertFalse(last_round_direct_noop(bad)[0])

    def test_quoted_or_duplicate_packet_headings_cannot_supply_an_empty_delta(self):
        quoted = "## Approved Plan\n```\n" + packet(EMPTY) + "```\n\n"
        text = quoted + session([(1, "orchestrator-direct")], CHANGED)
        self.assertFalse(last_round_direct_noop(text)[0])
        duplicate = packet(EMPTY) + session([(1, "orchestrator-direct")], CHANGED)
        self.assertFalse(last_round_direct_noop(duplicate)[0])
        self.assertTrue(last_round_direct_noop(quoted + session([(1, "orchestrator-direct")], EMPTY))[0])

    def test_marker_text_in_a_non_empty_row_fails(self):
        for row in ("| 0 | 1 | (no attributable change) | aaa | bbb |\n", "| 0 | 1 | `(no attributable change)` | — | — |\n",
                    "| 0 | 0 | (no attributable change) | — |\n"):
            with self.subTest(row=row):
                self.assertFalse(last_round_direct_noop(session([(1, "orchestrator-direct")], row))[0])
        seven = "| 0 | 0 | (no attributable change) | — | — | — | — |\n"  # evidence_ledger.py's real form
        self.assertTrue(last_round_direct_noop(session([(1, "orchestrator-direct")], seven))[0])

    def test_entry_point_must_sit_in_session_metadata(self):
        stray = session([(1, "orchestrator-direct")], EMPTY).replace(META, "\n- entry_point: execute\n")
        self.assertFalse(last_round_direct_noop(stray)[0])

    def test_entry_point_under_a_later_section_does_not_satisfy_the_metadata_check(self):
        base = session([(1, "orchestrator-direct")], EMPTY)
        for tail in ("\n## Notes\n- entry_point: execute\n",  # empty Session Metadata, stray line in the next section
                     "\n- other: x\n\n## Notes\n- entry_point: execute\n"):
            with self.subTest(tail=tail):
                self.assertFalse(last_round_direct_noop(base.replace(META, "\n## Session Metadata" + tail))[0])
        self.assertTrue(last_round_direct_noop(base + "\n## Notes\n- x\n")[0])

    def test_missing_delta_table_or_marker_only_text_fails(self):
        text = "## Review History\n\n" + round_entry(1, "orchestrator-direct") + "(no attributable change)\n" + META
        self.assertFalse(last_round_direct_noop(text)[0])

    def test_foreign_or_empty_session_fails(self):
        self.assertFalse(last_round_direct_noop("")[0])
        self.assertFalse(last_round_direct_noop("# some other document\n(no attributable change)\n")[0])
        no_meta = session([(1, "orchestrator-direct")], EMPTY).replace("- entry_point: execute", "- other: x")
        self.assertFalse(last_round_direct_noop(no_meta)[0])


class PluginLoadCheckTest(unittest.TestCase):
    def check(self, root, plugins):
        """(plugins recorded in meta, violation text) for a synthetic tool-use-events file, from a passing case."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tool-use-events.json"
            path.write_text(json.dumps({"plugins_loaded": plugins}), encoding="utf-8")
            meta = {}
            status, reason = load_gate(path, root, meta, "pass", "assertions passed")
            return meta.get("plugins_loaded", []), reason if status == "fail" else ""

    def test_init_event_plugins_are_captured(self):
        init = {"type": "system", "subtype": "init", "plugins": [{"name": "review-loop", "path": "/wt"}, "junk"]}
        payload, _ = parse_stream_json_capture(json.dumps(init) + "\n")
        self.assertEqual(payload["plugins_loaded"], [{"name": "review-loop", "path": "/wt"}])

    def test_worktree_plugin_passes_and_is_recorded(self):
        plugins = [{"name": "review-loop@x", "path": ROOT.as_posix()}, {"name": "other", "path": "/elsewhere"}]
        self.assertEqual(self.check(ROOT, plugins), (plugins, ""))

    def test_review_loop_from_another_path_is_a_violation(self):
        plugins = [{"name": "review-loop", "path": "/home/u/.claude/plugins/cache/review-loop"}]
        loaded, violation = self.check(ROOT, plugins)
        self.assertEqual(loaded, plugins)
        self.assertIn("not the worktree", violation)

    def test_no_init_plugins_or_missing_file_is_not_a_violation(self):
        self.assertEqual(self.check(ROOT, []), ([], ""))
        self.assertEqual(load_gate(ROOT / "does-not-exist.json", ROOT, {}, "pass", "ok"), ("pass", "ok"))

    def test_violation_keeps_a_prior_failure_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tool-use-events.json"
            path.write_text(json.dumps({"plugins_loaded": [{"name": "review-loop", "path": "/x"}]}), encoding="utf-8")
            status, reason = load_gate(path, ROOT, {}, "fail", "earlier failure")
        self.assertEqual(status, "fail")
        self.assertIn("earlier failure", reason)


class DiagnosabilityTest(unittest.TestCase):
    STREAM = "\n".join([
        "not json",
        json.dumps({"type": "surprise"}),
        json.dumps({"type": "assistant", "message": "bad"}),
        json.dumps({"type": "result", "subtype": "success", "result": "ok"}),
    ]) + "\n"

    def test_parse_error_details_name_each_failure_and_are_capped(self):
        payload, _ = parse_stream_json_capture(self.STREAM)
        self.assertEqual(payload["parse_errors"], 3)
        self.assertEqual(payload["parse_error_details"], ["invalid json line", "surprise/None", "assistant/None"])
        payload, _ = parse_stream_json_capture("bad\n" * 30)
        self.assertEqual(payload["parse_errors"], 30)
        self.assertEqual(len(payload["parse_error_details"]), 20)

    def test_raw_stream_is_kept_next_to_the_events_and_capped_to_its_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact, text = Path(tmp) / "tool-use-events.json", Path(tmp) / "stdout.txt"
            artifact.write_text(self.STREAM, encoding="utf-8")
            self.assertTrue(finalize_stream_capture_artifact(artifact, text))
            self.assertEqual((Path(tmp) / "raw-stream.jsonl").read_text(encoding="utf-8"), self.STREAM)
            big = "x" * (6 * 1024 * 1024) + "TAIL\n"
            artifact.write_text(big, encoding="utf-8")
            finalize_stream_capture_artifact(artifact, text)
            raw = (Path(tmp) / "raw-stream.jsonl").read_text(encoding="utf-8")
            self.assertEqual(len(raw), 5 * 1024 * 1024)
            self.assertTrue(raw.endswith("TAIL\n"))


def run_timeout_case(case_id, session_id, session_text, stream_lines):
    """Run a best-effort case whose command hangs after the fake claude finishes (the timeout branch)."""
    from tests.run_skill_smoke_plugin_dir_test import GROUP_ID, SMOKE_DIR

    case_path = SMOKE_DIR / f"{case_id}.json"
    artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
    session_path = ROOT / ".review-loop/sessions" / f"{session_id}.md"
    marker = ROOT / ".review-loop/tmp/smoke-session-uuid"
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(session_text, encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmpdir:
        fake = Path(tmpdir) / "claude"
        payload = "\n".join(json.dumps(line) for line in stream_lines)
        fake.write_text(f"#!/usr/bin/env bash\ncat >/dev/null\nprintf '%s\\n' '{payload}'\n", encoding="utf-8")
        fake.chmod(0o755)
        env = dict(os.environ, PATH=f"{tmpdir}{os.pathsep}{os.environ.get('PATH', '')}")
        case = {
            "id": case_id, "type": "smoke", "target": "execute", "runtime": "claude", "requires": ["claude"],
            "execution_policy": "best_effort", "setup": {"timeout_seconds": 5},
            "artifacts": {
                "capture": {"session_path": "latest_session", "session_final": "latest_session",
                            "tool_use_events": "stream_json_read_events"},
                "required": ["session_path", "session_final", "tool_use_events", "assertions", "meta"],
            },
            "command": ["python3", "-c", (
                "import pathlib, subprocess, sys, time\n"
                "tmp = pathlib.Path(sys.argv[1]) / '.review-loop' / 'tmp'\n"
                "tmp.mkdir(parents=True, exist_ok=True)\n"
                f"(tmp / 'smoke-session-uuid').write_text({session_id!r}, encoding='utf-8')\n"
                f"print('.review-loop/sessions/{session_id}.md', flush=True)\n"
                "subprocess.run(['claude', '-p', 'x'], stdin=subprocess.DEVNULL, check=True)\n"
                "time.sleep(60)\n"), "__WORKTREE__"],
            "assertions": [GROUP_ID],
        }
        try:
            case_path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
            completed = subprocess.run(["bash", "scripts/run-skill-smoke", "--case", case_id], cwd=ROOT,
                                       capture_output=True, text=True, env=env, timeout=120)
            results = json.loads((ROOT / "tests/skills/.last-run.json").read_text(encoding="utf-8"))["results"]
            meta_path = artifact_dir / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            return completed, next(r for r in results if r.get("id") == case_id), meta
        finally:
            for path in (case_path, session_path, marker):
                if path.exists():
                    path.unlink()
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)


class TimeoutBranchPluginGateTest(unittest.TestCase):
    def test_timeout_branch_cannot_pass_with_a_foreign_review_loop_and_records_plugins_loaded(self):
        from tests.run_skill_smoke_plugin_dir_test import NOOP_SESSION

        sid = "cccccccc-3333-4333-8333-cccccccccccc"
        done = {"type": "result", "subtype": "success", "result": "ok"}
        for case_id, path, expected in (
            ("zz.plugin.timeout-foreign", "/elsewhere/review-loop", "SKIP"),  # timeout branch: never PASS
            ("zz.plugin.timeout-worktree", ROOT.as_posix(), "PASS"),  # control: the same branch does pass
        ):
            init = {"type": "system", "subtype": "init", "plugins": [{"name": "review-loop", "path": path}]}
            with self.subTest(case=case_id):
                completed, record, meta = run_timeout_case(case_id, sid, NOOP_SESSION, [init, done])
                self.assertEqual(record["status"].upper(), expected, completed.stdout + completed.stderr)
                self.assertEqual(meta.get("plugins_loaded"), [{"name": "review-loop", "path": path}])
                if expected != "PASS":
                    self.assertIn("not the worktree", record["reason"])


class SmokeRunWiringTest(unittest.TestCase):
    def test_foreign_review_loop_plugin_fails_the_case_and_worktree_plugin_is_recorded(self):
        from tests.run_skill_smoke_plugin_dir_test import NOOP_SESSION, _run_group_case

        sid = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
        done = {"type": "result", "subtype": "success", "result": "ok"}
        for case_id, path, expected in (
            ("zz.plugin.foreign", "/elsewhere/review-loop", "FAIL"),
            ("zz.plugin.worktree", ROOT.as_posix(), "PASS"),
        ):
            init = {"type": "system", "subtype": "init", "plugins": [{"name": "review-loop", "path": path}]}
            with self.subTest(case=case_id):
                completed, record = _run_group_case(case_id, sid, NOOP_SESSION, [init, done])
                self.assertEqual(record["status"].upper(), expected, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
