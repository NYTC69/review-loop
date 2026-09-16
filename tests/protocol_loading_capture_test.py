"""Deterministic capture fixtures use the production CLI's complete output.

These tests establish delivery evidence only; they cannot establish that a model
subsequently followed the loaded rules.
"""
import ast
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.read_protocol import resolve  # noqa: E402 -- repository-local imports
from scripts.run_skill_smoke_lib import parse_stream_json_capture, protocol_stages_loaded  # noqa: E402


def loader_pair(stage="entry-plan", runtime="claude", loaded=(), context="root", tool_id="load-1"):
    args = ["scripts/read_protocol.py", "--runtime", runtime, "--stage", stage]
    for fingerprint in loaded:
        args += ["--loaded", fingerprint]
    run = subprocess.run([sys.executable, *args], cwd=ROOT, text=True, capture_output=True, check=True)
    scope = {} if context == "root" else {"parent_tool_use_id": context}
    return [
        {"type": "assistant", **scope, "message": {"content": [
            {"type": "tool_use", "name": "Bash", "id": tool_id,
             "input": {"command": "python3 " + " ".join(args)}}]}},
        {"type": "user", **scope, "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id,
             "content": run.stdout, "is_error": False}]}},
    ]


def capture(events):
    events = [*events, {"type": "result", "subtype": "success", "result": "done"}]
    return parse_stream_json_capture("\n".join(json.dumps(event) for event in events))[0]


def assertion(runtime="claude", stages=None, context="root"):
    return {"kind": "tool_use_protocol_stages_loaded", "artifact": "tool-use-events.json",
            "runtime": runtime, "stages": stages or ["entry-plan"], "context": context}


def runner_functions():
    # Load only runner definitions, avoiding its CLI and external runtimes.
    source = (ROOT / "scripts/run-skill-smoke").read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    selected = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in (
                "evaluate_mapping", "require_mapping_object", "_resolve_smoke_assertion_entry"):
            selected.append(node)
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and
                target.id == "_SMOKE_ALLOWED_OVERRIDE_KEYS" for target in node.targets):
            selected.append(node)
    namespace = {"json": json, "Path": Path, "protocol_stages_loaded": protocol_stages_loaded}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "runner-functions", "exec"), namespace)
    return namespace


class ProtocolLoadingCaptureTest(unittest.TestCase):
    def assert_loaded(self, payload, expected=True, **kwargs):
        passed, message = protocol_stages_loaded(payload, assertion(**kwargs))
        self.assertEqual(passed, expected, message)

    def test_complete_source_bodies_both_runtimes(self):
        for runtime in ("claude", "codex"):
            with self.subTest(runtime=runtime):
                payload = capture(loader_pair(runtime=runtime))
                self.assert_loaded(payload, runtime=runtime)
                event = payload["events"][0]
                units = resolve(ROOT, runtime, "entry-plan")
                self.assertEqual(event["tool"], "ProtocolLoad")
                self.assertEqual(event["units"], [u["unit"] + "@" + u["sha256"] for u in units])
                self.assertEqual(event["instruction_bytes"], sum(u["bytes"] for u in units))
                self.assertEqual(event["context"], "root")
                self.assertEqual(event["role"], "assistant")

    def test_no_result_does_not_count_tool_intent(self):
        payload = capture(loader_pair()[:1])
        self.assert_loaded(payload, False)
        self.assertEqual(payload["events"], [])

    def test_unpaired_result_does_not_count(self):
        pair = loader_pair()
        pair[1]["message"]["content"][0]["tool_use_id"] = "unrelated"
        self.assert_loaded(capture(pair), False)

    def test_failed_result_does_not_count_even_with_full_output(self):
        pair = loader_pair()
        pair[1]["message"]["content"][0]["is_error"] = True
        self.assert_loaded(capture(pair), False)

    def test_partial_wrong_hash_wrong_source_inventory_do_not_count(self):
        pair = loader_pair()
        body = pair[1]["message"]["content"][0]["content"]
        unit = resolve(ROOT, "claude", "entry-plan")[0]
        variants = {
            "truncated": body[:-1],
            "missing-first-body": body[body.index("-->") + 3:],
            "wrong-hash": body.replace(unit["sha256"], "0" * 64, 1),
            "wrong-source": body.replace("source: ", "source: elsewhere/", 1),
            "inventory": json.dumps([{k: v for k, v in unit.items() if k != "body"}]),
        }
        for name, content in variants.items():
            with self.subTest(name=name):
                mutated = copy.deepcopy(pair)
                mutated[1]["message"]["content"][0]["content"] = content
                self.assert_loaded(capture(mutated), False)

    def test_inventory_command_is_not_loading_even_with_matching_body(self):
        pair = loader_pair()
        pair[0]["message"]["content"][0]["input"]["command"] += " --inventory"
        self.assert_loaded(capture(pair), False)

    def test_command_runtime_stage_must_match_actual_body(self):
        for old, new in (("--runtime claude", "--runtime codex"),
                         ("--stage entry-plan", "--stage entry-execute")):
            with self.subTest(new=new):
                pair = loader_pair()
                command = pair[0]["message"]["content"][0]["input"]
                command["command"] = command["command"].replace(old, new)
                self.assert_loaded(capture(pair), False)

    def test_shell_prefix_pipeline_and_duplicate_options_fail_closed(self):
        for mutate in (lambda c: "echo " + c, lambda c: c + " | head -10",
                       lambda c: c + " --stage entry-plan", lambda c: c + "\necho ok"):
            pair = loader_pair()
            command = pair[0]["message"]["content"][0]["input"]
            command["command"] = mutate(command["command"])
            self.assert_loaded(capture(pair), False)

    def test_repeat_reuses_only_previously_observed_fingerprints(self):
        first = loader_pair()
        fingerprints = capture(first)["events"][0]["units"]
        second = loader_pair(loaded=fingerprints, tool_id="load-2")
        payload = capture(first + second)
        self.assert_loaded(payload)
        self.assertEqual(payload["events"][1]["emitted"], [])
        self.assertEqual(payload["events"][1]["reused"], fingerprints)
        self.assertEqual(payload["events"][1]["instruction_bytes"], 0)
        self.assertEqual(payload["events"][1]["output_bytes"], 0)
        self.assert_loaded(capture(second), False)

    def test_next_stage_combines_new_bodies_with_prior_context(self):
        first = loader_pair()
        fingerprints = capture(first)["events"][0]["units"]
        second = loader_pair(stage="planning", loaded=fingerprints, tool_id="load-planning")
        payload = capture(first + second)
        self.assert_loaded(payload, stages=["entry-plan", "planning"])
        self.assertGreater(payload["events"][1]["instruction_bytes"], 0)
        self.assert_loaded(payload, False, stages=["execution"])

    def test_later_observation_cannot_retroactively_cover_earlier_reuse(self):
        first = loader_pair(tool_id="load-full")
        fingerprints = capture(first)["events"][0]["units"]
        second = loader_pair(loaded=fingerprints, tool_id="load-reused")
        self.assert_loaded(capture(second + first), False)
        normalized = capture(first + second)
        normalized["events"].reverse()
        self.assert_loaded(normalized, False)

    def test_reuse_requires_content_observed_before_tool_call(self):
        first = loader_pair(tool_id="load-full")
        fingerprints = capture(first)["events"][0]["units"]
        second = loader_pair(loaded=fingerprints, tool_id="load-reused")
        # Both calls issued before either result: even if the full result comes
        # first, the second call has not yet observed those instructions.
        self.assert_loaded(capture([first[0], second[0], first[1], second[1]]), False)

    def test_cross_context_reuse_refused_but_fresh_agent_load_works(self):
        first = loader_pair()
        fingerprints = capture(first)["events"][0]["units"]
        second = loader_pair(loaded=fingerprints, context="agent-1", tool_id="load-2")
        self.assert_loaded(capture(first + second), False, context="agent-1")
        agent = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "agent-1", "name": "Agent",
             "input": {"subagent_type": "general-purpose"}}]}}
        fresh = loader_pair(context="agent-1", tool_id="load-3")
        payload = capture([agent, *fresh])
        self.assert_loaded(payload, context="agent-1")
        self.assert_loaded(payload, False)
        self.assertEqual(payload["events"][1]["role"], "general-purpose")
        self.assertEqual(payload["events"][0], {"tool": "Agent", "subagent_type": "general-purpose"})

    def test_mismatched_result_context_fails(self):
        pair = loader_pair(context="agent-1")
        pair[1].pop("parent_tool_use_id")
        self.assert_loaded(capture(pair), False, context="agent-1")

    def test_compaction_invalidates_cached_content_in_capture_and_replay(self):
        first = loader_pair()
        fingerprints = capture(first)["events"][0]["units"]
        cached = loader_pair(loaded=fingerprints, tool_id="cached")
        boundary = {"type": "system", "subtype": "compact_boundary"}
        self.assert_loaded(capture([*first, boundary, *cached]), False)
        fresh = loader_pair(tool_id="fresh")
        good = capture([*first, boundary, *fresh])
        self.assert_loaded(good)
        self.assertEqual(good["events"][1], {"tool": "ProtocolContextReset",
                         "context": "root", "reason": "compact_boundary"})
        replay = capture(first + cached)
        replay["events"].insert(1, good["events"][1])
        self.assert_loaded(replay, False)

    def test_content_text_blocks_supported(self):
        pair = loader_pair()
        result = pair[1]["message"]["content"][0]
        result["content"] = [{"type": "text", "text": result["content"]}]
        self.assert_loaded(capture(pair))

    def test_unknown_capture_shapes_and_errors_fail(self):
        good = capture(loader_pair())
        for key, value in (("parse_errors", 1), ("schema_errors", ["drift"]),
                           ("protocol_errors", ["truncated"]), ("schema_version", 99),
                           ("events", "unknown")):
            with self.subTest(key=key):
                payload = copy.deepcopy(good)
                payload[key] = value
                self.assert_loaded(payload, False)
        malformed = parse_stream_json_capture('{"type":"assistant","message":[]}\n{broken\n')[0]
        self.assert_loaded(malformed, False)
        for mutation in ({"type": "unknown"}, {"type": "assistant", "message": "unknown"}):
            self.assert_loaded(capture([*loader_pair(), mutation]), False)

    def test_unknown_stage_and_forged_normalized_load_fail(self):
        payload = capture(loader_pair())
        self.assert_loaded(payload, False, stages=["unknown-stage"])
        for field, value in (("units", []), ("instruction_bytes", 1),
                             ("output_bytes", 1), ("emitted", "unknown"), ("role", None)):
            with self.subTest(field=field):
                mutated = copy.deepcopy(payload)
                mutated["events"][0][field] = value
                self.assert_loaded(mutated, False)

    def test_runner_dispatches_new_assertion_kind(self):
        namespace = runner_functions()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            artifact = directory / "tool-use-events.json"
            artifact.write_text(json.dumps(capture(loader_pair())))
            mapping = {"smoke_assertions": {"loaded": assertion()}, "smoke_groups": {}}
            results, status, _ = namespace["evaluate_mapping"]("fixture", ["loaded"], mapping, directory)
            self.assertEqual(status, "pass", results)
            artifact.write_text("{broken")
            results, status, _ = namespace["evaluate_mapping"]("fixture", ["loaded"], mapping, directory)
            self.assertEqual(status, "fail", results)

    def test_real_migrated_case_resolves_and_validates_stage_overrides(self):
        mapping = json.loads((ROOT / "tests/skills/contracts/assertion-mapping.json").read_text())
        case = json.loads((ROOT / "tests/skills/smoke/execute.from-plan.smoke.claude.json").read_text())
        entry = next(item for item in case["assertions"] if isinstance(item, dict) and
                     item["id"] == "execute_protocol_imports_read")
        namespace = runner_functions()
        events = []
        for index, stage in enumerate(entry["overrides"]["stages"]):
            events.extend(loader_pair(stage=stage, tool_id=f"stage-{index}"))
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "tool-use-events.json").write_text(json.dumps(capture(events)))
            results, status, _ = namespace["evaluate_mapping"](case["id"], [entry], mapping, directory)
            self.assertEqual(status, "pass", results)
            for invalid in ([], "execution", ["unknown-stage"]):
                bad = copy.deepcopy(entry)
                bad["overrides"]["stages"] = invalid
                results, status, _ = namespace["evaluate_mapping"](case["id"], [bad], mapping, directory)
                self.assertEqual(status, "fail", results)

    def test_real_review_loop_nested_all_any_groups(self):
        mapping = json.loads((ROOT / "tests/skills/contracts/assertion-mapping.json").read_text())
        definitions = mapping["smoke_assertions"]
        stages = definitions["review_loop_common_stages_loaded"]["stages"]
        events = []
        for index, stage in enumerate(stages):
            events.extend(loader_pair(stage=stage, tool_id=f"stage-{index}"))
        evaluate = runner_functions()["evaluate_mapping"]
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "tool-use-events.json").write_text(json.dumps(capture(events)))
            session = directory / "session-final.md"
            session.write_text(definitions["plan_source_review_only"]["needle"])
            results, status, _ = evaluate("fixture", ["review_loop_protocol_imports_read"], mapping, directory)
            self.assertEqual(status, "pass", results)
            # all(common, any(planned, review-only)) must require route evidence.
            session.write_text("no route provenance")
            results, status, _ = evaluate("fixture", ["review_loop_protocol_imports_read"], mapping, directory)
            self.assertEqual(status, "fail", results)


if __name__ == "__main__":
    unittest.main()
