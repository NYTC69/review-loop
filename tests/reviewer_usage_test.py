"""Native-shaped usage fixtures, without invoking a paid model or local history.

Claude fields/scopes: code.claude.com/docs/en/agent-sdk/cost-tracking (2.1.278).
Codex fields and cumulative emission: openai/codex rust-v0.155.1,
codex-rs/exec/src/{exec_events,event_processor_with_jsonl_output}.rs.
Values and IDs below are synthetic; schema and event boundaries are native.
"""

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from reviewer_usage import TOKEN_FIELDS, UsageAccumulator, aggregate_records, record_invocation


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reviewer_usage.py"


def claude_result(*, factor=1, cost=0.05, session="native-session", **changes):
    event = {
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": session, "uuid": "result-1", "result": "PRIVATE_RESULT",
        "total_cost_usd": cost,
        "usage": {"input_tokens": 60, "cache_read_input_tokens": 300,
                  "cache_creation_input_tokens": 30, "output_tokens": 12},
        "modelUsage": {"claude-sonnet-4-6": {
            "inputTokens": 100 * factor, "cacheReadInputTokens": 400 * factor,
            "cacheCreationInputTokens": 50 * factor, "outputTokens": 20 * factor,
            "costUSD": cost,
        }},
    }
    event.update(changes)
    return event


def assistant(message_id="msg-1", **changes):
    event = {"type": "assistant", "parent_tool_use_id": None,
             "message": {"id": message_id, "model": "claude-sonnet-4-6",
                         "content": [{"type": "text", "text": "PRIVATE_TEXT"}],
                         "usage": {"input_tokens": 100, "cache_read_input_tokens": 400,
                                   "cache_creation_input_tokens": 50, "output_tokens": 1}}}
    event.update(changes)
    return event


def codex_result(factor=1):
    return {"type": "turn.completed", "usage": {
        "input_tokens": 550 * factor, "cached_input_tokens": 400 * factor,
        "cache_write_input_tokens": 50 * factor, "output_tokens": 20 * factor,
        "reasoning_output_tokens": 5 * factor,
    }}


def accumulate(runtime, events, **kwargs):
    accumulator = UsageAccumulator(runtime, **kwargs)
    for event in events:
        accumulator.consume(event)
    return accumulator.summary()


EXPECTED = {"input_tokens": 550, "uncached_input_tokens": 150,
            "cached_input_tokens": 400, "cache_write_input_tokens": 50,
            "output_tokens": 20}


class NativeUsageTest(unittest.TestCase):
    def test_claude_snapshot_supersedes_partials_assistants_root_and_children(self):
        child = claude_result(factor=100, parent_tool_use_id="tool-child")
        events = [
            {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6",
             "session_id": "native-session"},
            {"type": "stream_event", "event": {"type": "message_delta",
             "usage": {"output_tokens": 8}}},
            assistant(), assistant(), child, claude_result(), claude_result(),
        ]
        summary = accumulate("claude", events)
        self.assertEqual(summary["tokens"], EXPECTED)
        self.assertEqual(summary["cost_usd"], 0.05)
        self.assertEqual(summary["accounting_status"], "complete")
        self.assertEqual(summary["scope"], "whole_tree")
        self.assertEqual(summary["actual_model"], "claude-sonnet-4-6")
        self.assertNotIn("PRIVATE", json.dumps(summary))

    def test_distinct_model_rows_are_added_once_without_aggregate_double_count(self):
        event = claude_result()
        event["modelUsage"]["claude-haiku-4-5"] = copy.deepcopy(next(iter(event["modelUsage"].values())))
        summary = accumulate("claude", [event])
        self.assertEqual(summary["tokens"], {key: value * 2 for key, value in EXPECTED.items()})
        self.assertEqual(summary["cost_usd"], 0.05)  # Root cost has priority.
        del event["total_cost_usd"]
        self.assertEqual(accumulate("claude", [event])["cost_usd"], 0.1)

    def test_multiple_results_are_cumulative_snapshots_not_summands(self):
        summary = accumulate("claude", [claude_result(), claude_result(factor=3, cost=0.15)])
        self.assertEqual(summary["tokens"]["input_tokens"], 1650)
        self.assertEqual(summary["cost_usd"], 0.15)

    def test_root_only_fallback_is_explicitly_latest_main_agent_turn(self):
        summary = accumulate("claude", [claude_result(modelUsage=None), claude_result(modelUsage={})])
        self.assertEqual(summary["tokens"]["input_tokens"], 390)
        self.assertEqual(summary["accounting_status"], "partial")
        self.assertEqual(summary["scope"], "main_agent_turn")
        self.assertEqual(summary["cost_scope"], "whole_tree")

    def test_failure_assistant_inputs_deduplicate_ids_and_ignore_output_placeholder(self):
        summary = accumulate("claude", [assistant(), assistant(), assistant("msg-2"),
                                         assistant("child", parent_tool_use_id="tool-child")])
        self.assertEqual(summary["tokens"]["input_tokens"], 1100)
        self.assertIsNone(summary["tokens"]["output_tokens"])
        self.assertIsNone(summary["cost_usd"])
        self.assertEqual(summary["accounting_status"], "partial")

    def test_partial_messages_alone_do_not_invent_complete_usage(self):
        summary = accumulate("claude", [{"type": "stream_event", "event": {
            "type": "message_delta", "usage": {"output_tokens": 9}}}])
        self.assertEqual(summary["accounting_status"], "unknown")
        self.assertTrue(all(value is None for value in summary["tokens"].values()))

    def test_assistant_without_native_id_is_not_safe_to_sum(self):
        event = assistant()
        del event["message"]["id"]
        summary = accumulate("claude", [event])
        self.assertEqual(summary["accounting_status"], "unknown")
        self.assertIn("assistant_usage_without_message_id", summary["reasons"])

    def test_zeroed_crash_result_preserves_earlier_snapshot_as_partial(self):
        crash = claude_result(factor=0, cost=0, subtype="error_during_execution", is_error=True)
        summary = accumulate("claude", [claude_result(), assistant("new-message"), crash])
        self.assertEqual(summary["tokens"], EXPECTED)
        self.assertEqual(summary["accounting_status"], "partial")
        self.assertFalse(summary["cumulative_snapshot"]["usable"])
        first_turn_crash = accumulate("claude", [assistant(), crash])
        self.assertEqual(first_turn_crash["tokens"]["input_tokens"], 550)
        self.assertIsNone(first_turn_crash["tokens"]["output_tokens"])

    def test_noncrash_failure_keeps_reported_usage_and_failure_state(self):
        summary = accumulate("claude", [claude_result(subtype="error_max_budget_usd", is_error=True)])
        self.assertEqual(summary["tokens"], EXPECTED)
        self.assertEqual(summary["accounting_status"], "partial")

    def test_claude_resume_subtracts_matching_snapshot_not_current_result_usage(self):
        baseline = accumulate("claude", [claude_result()])["cumulative_snapshot"]
        summary = accumulate("claude", [claude_result(factor=3, cost=0.15)], resumed=True, baseline=baseline)
        self.assertEqual(summary["tokens"], {key: value * 2 for key, value in EXPECTED.items()})
        self.assertAlmostEqual(summary["cost_usd"], 0.1)
        self.assertEqual(summary["cumulative_snapshot"]["tokens"]["input_tokens"], 1650)

    def test_resume_without_baseline_or_other_session_is_unknown(self):
        baseline = accumulate("claude", [claude_result(session="different")])["cumulative_snapshot"]
        for candidate in (None, baseline, {}):
            with self.subTest(candidate=candidate):
                summary = accumulate("claude", [claude_result()], resumed=True, baseline=candidate)
                self.assertEqual(summary["accounting_status"], "unknown")
                self.assertIsNone(summary["cost_usd"])
                self.assertTrue(all(value is None for value in summary["tokens"].values()))

    def test_resume_missing_baseline_fields_remain_unknown(self):
        baseline = accumulate("claude", [claude_result()])["cumulative_snapshot"]
        baseline["tokens"]["output_tokens"] = None
        baseline["cost_usd"] = None
        summary = accumulate("claude", [claude_result(factor=2)], resumed=True, baseline=baseline)
        self.assertEqual(summary["tokens"]["input_tokens"], 550)
        self.assertIsNone(summary["tokens"]["output_tokens"])
        self.assertIsNone(summary["cost_usd"])

    def test_counter_reset_and_session_change_do_not_fabricate_delta(self):
        for events in ([claude_result(factor=2), claude_result()],
                       [claude_result(), claude_result(session="new-session")],
                       [claude_result(), {"type": "system", "subtype": "conversation_reset"}]):
            with self.subTest(events=len(events)):
                summary = accumulate("claude", events)
                self.assertEqual(summary["accounting_status"], "unknown")
                self.assertFalse(summary["cumulative_snapshot"]["usable"])
        baseline = accumulate("claude", [claude_result(factor=2)])["cumulative_snapshot"]
        self.assertEqual(accumulate("claude", [claude_result()], resumed=True,
                                    baseline=baseline)["accounting_status"], "unknown")

    def test_malformed_numbers_are_unknown_without_string_float_bool_coercion(self):
        for invalid in (-1, True, "100", 1.5, float("nan"), float("inf"), None, [], {}):
            with self.subTest(invalid=str(invalid)):
                event = claude_result(total_cost_usd=invalid)
                event["modelUsage"]["claude-sonnet-4-6"]["inputTokens"] = invalid
                summary = accumulate("claude", [event])
                self.assertIsNone(summary["tokens"]["input_tokens"])
                if invalid == 1.5:
                    self.assertEqual(summary["cost_usd"], 1.5)  # Fractional USD is valid.
                else:
                    self.assertIsNone(summary["cost_usd"])
                self.assertEqual(summary["tokens"]["cached_input_tokens"], 400)
                json.dumps(summary, allow_nan=False)

    def test_missing_counters_are_not_zero_and_zero_is_preserved(self):
        event = claude_result(factor=0, cost=0)
        self.assertEqual(accumulate("claude", [event])["tokens"], dict.fromkeys(TOKEN_FIELDS, 0))
        del event["modelUsage"]["claude-sonnet-4-6"]["cacheCreationInputTokens"]
        summary = accumulate("claude", [event])
        self.assertIsNone(summary["tokens"]["cache_write_input_tokens"])
        self.assertEqual(summary["tokens"]["output_tokens"], 0)
        self.assertEqual(summary["accounting_status"], "partial")

    def test_codex_cached_input_is_subset_and_reasoning_is_not_extra_output(self):
        summary = accumulate("codex", [{"type": "thread.started", "thread_id": "thread-1"}, codex_result()])
        self.assertEqual(summary["tokens"], EXPECTED)
        self.assertEqual(summary["accounting_status"], "complete")
        self.assertIsNone(summary["cost_usd"])
        self.assertIsNone(summary["actual_model"])

    def test_codex_multiple_completed_events_and_resume_use_cumulative_deltas(self):
        initial = [{"type": "thread.started", "thread_id": "thread-1"}, codex_result()]
        baseline = accumulate("codex", initial)["cumulative_snapshot"]
        events = [{"type": "thread.started", "thread_id": "thread-1"}, codex_result(2), codex_result(2)]
        self.assertEqual(accumulate("codex", events)["tokens"]["input_tokens"], 1100)
        self.assertEqual(accumulate("codex", events, resumed=True, baseline=baseline)["tokens"], EXPECTED)
        self.assertEqual(accumulate("codex", events, resumed=True)["accounting_status"], "unknown")

    def test_codex_failed_or_missing_final_has_unknowns_not_zero_cost(self):
        summary = accumulate("codex", [{"type": "turn.failed", "error": {"message": "PRIVATE_ERROR"}}])
        self.assertEqual(summary["accounting_status"], "unknown")
        self.assertIsNone(summary["cost_usd"])
        self.assertNotIn("PRIVATE", json.dumps(summary))

    def test_codex_subagent_items_are_not_summed_into_root_counters(self):
        summary = accumulate("codex", [{"type": "item.completed", "item": {
            "type": "collab_tool_call", "tool": "spawn_agent", "usage": {"input_tokens": 9999}}}, codex_result()])
        self.assertEqual(summary["tokens"], EXPECTED)
        self.assertEqual(summary["accounting_status"], "partial")

    def test_invalid_codex_cache_counter_is_not_clamped_to_zero(self):
        event = codex_result()
        event["usage"]["cached_input_tokens"] = 900
        summary = accumulate("codex", [event])
        self.assertIsNone(summary["tokens"]["input_tokens"])
        self.assertIsNone(summary["tokens"]["uncached_input_tokens"])
        self.assertEqual(summary["accounting_status"], "partial")

    def test_new_activity_invalid_events_and_missing_terminal_mark_partial(self):
        for runtime, events in (("claude", [claude_result(), assistant()]),
                                ("codex", [codex_result(), {"type": "turn.started"}]),
                                ("codex", [None, codex_result()])):
            summary = accumulate(runtime, events)
            self.assertEqual(summary["accounting_status"], "partial")
            self.assertFalse(summary["cumulative_snapshot"]["usable"])


class UsageLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "stream.jsonl"
        self.raw.write_text(json.dumps(claude_result()) + "\n")
        self.kwargs = dict(session_id="review-session", role="reviewer", stage="code-review",
                           backend="claude", requested_model="sonnet", status="ok", elapsed_seconds=1.5,
                           usage=accumulate("claude", [claude_result()]), raw_artifacts=[self.raw])

    def record(self, invocation_id="call-1", **changes):
        return record_invocation(self.root / "ledger", invocation_id=invocation_id, **(self.kwargs | changes))

    def test_record_has_reproducible_raw_evidence_and_idempotent_replay(self):
        path = self.record()
        original = path.read_bytes()
        self.assertEqual(self.record(), path)
        self.assertEqual(path.read_bytes(), original)
        record = json.loads(original)
        self.assertEqual(record["raw_artifacts"][0]["sha256"], hashlib.sha256(self.raw.read_bytes()).hexdigest())
        self.assertEqual(record["requested_model"], "sonnet")
        self.assertIsNone(record["actual_model"])
        self.assertNotIn("PRIVATE", original.decode())
        self.assertEqual(aggregate_records(path.parent)["invocations"], 1)

    def test_retry_uses_new_id_and_conflicting_replay_cannot_overwrite(self):
        path = self.record()
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "different record"):
            self.record(elapsed_seconds=2)
        self.assertEqual(path.read_bytes(), original)
        self.record("call-2")
        totals = aggregate_records(path.parent)
        self.assertEqual(totals["tokens"]["input_tokens"], 1100)
        self.assertEqual(totals["elapsed_seconds"], 3)

    def test_concurrent_distinct_invocations_and_same_id_replays_are_atomic(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            paths = list(pool.map(self.record, ["same-id"] * 20 + [f"retry-{i}" for i in range(20)]))
        self.assertEqual(len(set(paths)), 21)
        summary = aggregate_records(self.root / "ledger")
        self.assertEqual(summary["invocations"], 21)
        self.assertEqual(summary["tokens"]["input_tokens"], 21 * 550)
        self.assertEqual(list((self.root / "ledger").glob("*.tmp")), [])

    def test_concurrent_conflicting_writers_publish_only_one_complete_record(self):
        def write(elapsed):
            try:
                self.record(elapsed_seconds=elapsed)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(write, range(10)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(aggregate_records(self.root / "ledger")["invocations"], 1)

    def test_failure_timeout_cancel_without_result_each_get_distinct_record(self):
        for status in ("command_execution", "timeout", "cancelled"):
            path = self.record(status, status=status, usage=UsageAccumulator("claude").summary(),
                               raw_artifacts=[self.root / "not-created"])
            record = json.loads(path.read_text())
            self.assertEqual(record["status"], status)
            self.assertEqual(record["usage"]["accounting_status"], "unknown")
            self.assertFalse(record["raw_artifacts"][0]["available"])
        totals = aggregate_records(self.root / "ledger")
        self.assertEqual(totals["invocations"], 3)
        self.assertIsNone(totals["tokens"]["input_tokens"])
        self.assertIsNone(totals["known_tokens"]["input_tokens"])
        self.assertEqual(totals["unknown_cost_invocations"], 3)

    def test_failure_after_result_retains_known_usage_without_claiming_complete_total(self):
        path = self.record(status="timeout")
        record = json.loads(path.read_text())
        self.assertEqual(record["usage"]["accounting_status"], "partial")
        self.assertFalse(record["usage"]["cumulative_snapshot"]["usable"])
        self.assertEqual(self.kwargs["usage"]["accounting_status"], "complete")
        totals = aggregate_records(path.parent)
        self.assertIsNone(totals["tokens"]["input_tokens"])
        self.assertEqual(totals["known_tokens"]["input_tokens"], 550)

    def test_unknown_cost_and_partial_tokens_remain_visible_in_aggregation(self):
        path = self.record()
        self.record("codex", backend="codex", requested_model=None, usage=accumulate("codex", [codex_result()]))
        totals = aggregate_records(path.parent)
        self.assertEqual(totals["tokens"]["input_tokens"], 1100)
        self.assertIsNone(totals["cost_usd"])
        self.assertEqual(totals["known_cost_usd"], 0.05)
        self.assertEqual(totals["unknown_cost_invocations"], 1)
        self.assertEqual(aggregate_records(path.parent, session_id="another")["invocations"], 0)

    def test_parent_session_aggregation_includes_dotted_parallel_slots(self):
        self.record()
        self.record("parallel-call", session_id="review-session.job-a", parent_session_id="review-session")
        self.assertEqual(aggregate_records(self.root / "ledger", session_id="review-session")["invocations"], 2)
        self.assertEqual(aggregate_records(self.root / "ledger", session_id="review")["invocations"], 0)

    def test_dotted_job_slots_are_attributed_by_parent_field_without_collisions(self):
        self.record("slot-one", session_id="s.a.b", parent_session_id="s.a")
        self.record("slot-two", session_id="s.a.b", parent_session_id="s")
        self.assertEqual(aggregate_records(self.root / "ledger", session_id="s.a")["invocations"], 1)
        self.assertEqual(aggregate_records(self.root / "ledger", session_id="s")["invocations"], 1)

    def test_corrupt_published_record_fails_aggregation_closed(self):
        path = self.record()
        path.write_text('{"schema_version":1}')
        with self.assertRaisesRegex(ValueError, "invalid invocation record"):
            aggregate_records(path.parent)

    def test_unsafe_ids_and_invalid_elapsed_are_rejected(self):
        for value in ("../escape", "", "/absolute"):
            with self.assertRaises(ValueError):
                self.record(value)
        for elapsed in (-1, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.record(elapsed_seconds=elapsed)

    def test_cli_records_existing_stream_then_aggregates_bounded_json(self):
        self.raw.write_text("invalid-json\n" + self.raw.read_text())
        args = [sys.executable, str(SCRIPT), "record", "--runtime", "claude",
                "--stream", str(self.raw), "--ledger-dir", str(self.root / "ledger"),
                "--invocation-id", "cli-call", "--session-id", "review-session",
                "--role", "reviewer", "--stage", "code-review", "--requested-model", "sonnet",
                "--status", "ok", "--elapsed-seconds", "1.5"]
        result = subprocess.run(args, text=True, capture_output=True, check=True)
        record = json.loads(Path(json.loads(result.stdout)["record_file"]).read_text())
        self.assertEqual(record["usage"]["accounting_status"], "partial")
        self.assertNotIn("PRIVATE", result.stdout + result.stderr)
        result = subprocess.run([sys.executable, str(SCRIPT), "aggregate", "--ledger-dir", str(self.root / "ledger")],
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["invocations"], 1)


if __name__ == "__main__":
    unittest.main()
