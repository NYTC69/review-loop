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

from scripts.run_skill_smoke_lib import claude_plugin_source

SMOKE_DIR = ROOT / "tests/skills/smoke"
NO_PLUGIN_CASES = {"reviewer.case6.smoke.claude", "reviewer.case7.smoke.claude"}
NOOP_PLAN_CASES = {
    "execute.from-plan.smoke.claude",
    "execute.session-resume.smoke.claude",
    "execute.stop-after-before-polish.smoke.claude",
    "execute.stop-after-before-security.smoke.claude",
    "execute.stop-after-polish.smoke.claude",
}
GROUP_ID = "agent_calls_subagent_or_direct_noop"


class ClaudePluginSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / ".claude-plugin").mkdir()
        (self.root / ".claude-plugin/plugin.json").write_text('{"version": "9.9.9"}', encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_direct_command_records_dir_and_manifest_version(self):
        root = self.root.as_posix()
        command = ["claude", "-p", "--plugin-dir", root, "--add-dir", root, "--", "prompt"]
        self.assertEqual(
            claude_plugin_source(command, self.root), {"plugin_dir": root, "plugin_version": "9.9.9"}
        )

    def test_bash_wrapped_command_uses_worktree_argument(self):
        root = self.root.as_posix()
        script = 'WT="$1"; cd "$WT"; exec claude -p --plugin-dir "$WT" -- prompt'
        command = ["bash", "-lc", script, "--", root]
        self.assertEqual(claude_plugin_source(command, self.root)["plugin_dir"], root)

    def test_command_without_plugin_dir_records_nothing(self):
        self.assertEqual(claude_plugin_source(["claude", "-p", "--", "x"], self.root), {})

    def test_other_plugin_dir_is_refused(self):
        with self.assertRaises(ValueError):
            claude_plugin_source(["claude", "--plugin-dir", "/elsewhere"], self.root)
        with self.assertRaises(ValueError):
            claude_plugin_source(["claude", "--plugin-dir"], self.root)
        with self.assertRaises(ValueError):
            claude_plugin_source(["bash", "-lc", 'claude --plugin-dir "$WT"', "--", "/elsewhere"], self.root)

    def test_every_plugin_smoke_case_loads_the_worktree_plugin(self):
        seen = 0
        for path in sorted(SMOKE_DIR.glob("*.smoke.claude.json")):
            if path.name.removesuffix(".json") in NO_PLUGIN_CASES:
                continue
            with self.subTest(case=path.name):
                command = json.loads(path.read_text(encoding="utf-8"))["command"]
                source = claude_plugin_source([ROOT.as_posix() if x == "__WORKTREE__" else x for x in command], ROOT)
                manifest = json.loads((ROOT / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
                self.assertEqual(source, {"plugin_dir": ROOT.as_posix(), "plugin_version": manifest["version"]})
                seen += 1
        self.assertGreaterEqual(seen, 8)


class DirectNoopAssertionCasesTest(unittest.TestCase):
    def test_only_noop_plan_cases_use_the_group_and_others_keep_min_one(self):
        for path in sorted(SMOKE_DIR.glob("*.smoke.claude.json")):
            name = path.name.removesuffix(".json")
            ids = [a if isinstance(a, str) else a.get("id") for a in json.loads(path.read_text())["assertions"]]
            with self.subTest(case=name):
                if name in NOOP_PLAN_CASES:
                    self.assertIn(GROUP_ID, ids)
                    self.assertNotIn("agent_calls_used_at_least_one_subagent", ids)
                else:
                    self.assertNotIn(GROUP_ID, ids)


def _agent_event():
    block = {"type": "tool_use", "name": "Agent", "input": {"subagent_type": "general-purpose"}}
    return {"type": "assistant", "message": {"content": [block]}}


def _run_group_case(case_id, session_id, session_text, stream_lines):
    case_path = SMOKE_DIR / f"{case_id}.json"
    artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
    session_path = ROOT / ".review-loop/sessions" / f"{session_id}.md"
    marker = ROOT / ".review-loop/tmp/smoke-session-uuid"
    last_run = ROOT / "tests/skills/.last-run.json"
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(session_text, encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmpdir:
        fake = Path(tmpdir) / "claude"
        payload = "\n".join(json.dumps(line) for line in stream_lines)
        fake.write_text(
            f"#!/usr/bin/env bash\ncat >/dev/null\nprintf '%s\\n' '{payload}'\n", encoding="utf-8"
        )
        fake.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{tmpdir}{os.pathsep}{env.get('PATH', '')}"
        case = {
            "id": case_id,
            "type": "smoke",
            "target": "execute",
            "runtime": "claude",
            "requires": ["claude"],
            "setup": {"timeout_seconds": 20},
            "artifacts": {
                "capture": {
                    "session_path": "latest_session",
                    "session_final": "latest_session",
                    "tool_use_events": "stream_json_read_events",
                },
                "required": ["session_path", "session_final", "tool_use_events", "assertions", "meta"],
            },
            "command": [
                "python3",
                "-c",
                (
                    "import pathlib, subprocess, sys\n"
                    "tmp = pathlib.Path(sys.argv[1]) / '.review-loop' / 'tmp'\n"
                    "tmp.mkdir(parents=True, exist_ok=True)\n"
                    f"(tmp / 'smoke-session-uuid').write_text({session_id!r}, encoding='utf-8')\n"
                    f"print('.review-loop/sessions/{session_id}.md', flush=True)\n"
                    "subprocess.run(['claude', '-p', 'x'], stdin=subprocess.DEVNULL, check=True)\n"
                ),
                "__WORKTREE__",
            ],
            "assertions": [GROUP_ID],
        }
        try:
            case_path.write_text(json.dumps(case, indent=2) + "\n", encoding="utf-8")
            completed = subprocess.run(
                ["bash", "scripts/run-skill-smoke", "--case", case_id],
                cwd=ROOT, capture_output=True, text=True, env=env,
            )
            results = json.loads(last_run.read_text(encoding="utf-8"))["results"]
            return completed, next(r for r in results if r.get("id") == case_id)
        finally:
            for path in (case_path, session_path, marker):
                if path.exists():
                    path.unlink()
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)


NOOP_SESSION = (
    "## Current Review Packet\n\n### Attributable Delta\n"
    "| snapshot pre | snapshot post | path | pre_blob | post_blob |\n|---|---|---|---|---|\n"
    "| 0 | 0 | (no attributable change) | — | — |\n\n"
    "## Review History\n\n### Execution Round 1\n- Author route: orchestrator-direct\n"
    "- Reviewer verdict: APPROVE\n\n## Session Metadata\n- entry_point: execute\n"
)


class DirectNoopGroupBehaviourTest(unittest.TestCase):
    def test_zero_agent_calls_pass_only_with_direct_route_and_empty_delta(self):
        session_id = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
        no_agents = [{"type": "result", "subtype": "success", "result": "ok"}]
        changed = NOOP_SESSION.replace("(no attributable change) | — | — |", "`README.md` | aaa | bbb |")
        executor = NOOP_SESSION.replace("orchestrator-direct", "executor")
        cases = [
            ("zz.direct-noop.pass", NOOP_SESSION, no_agents, "PASS"),
            ("zz.direct-noop.changed-delta", changed, no_agents, "FAIL"),
            ("zz.direct-noop.executor-route", executor, no_agents, "FAIL"),
            ("zz.direct-noop.subagent", executor, [_agent_event()] + no_agents, "PASS"),
        ]
        for case_id, session, stream, expected in cases:
            with self.subTest(case=case_id):
                completed, record = _run_group_case(case_id, session_id, session, stream)
                self.assertEqual(record["status"].upper(), expected, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
