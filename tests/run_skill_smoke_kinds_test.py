import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _write_fake_claude(bin_dir: Path, stream_lines: list) -> None:
    script_path = bin_dir / "claude"
    payload = "\n".join(json.dumps(line) for line in stream_lines)
    script_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                "cat >/dev/null",
                f"printf '%s\\n' '{payload}'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)


def _run_smoke_case(case_id: str, assertions, stream_lines):
    case_path = ROOT / "tests/skills/smoke" / f"{case_id}.json"
    artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
    last_run_path = ROOT / "tests/skills/.last-run.json"
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_bin = Path(tmpdir)
        _write_fake_claude(fake_bin, stream_lines)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        case_data = {
            "id": case_id,
            "type": "smoke",
            "target": "review-loop",
            "runtime": "claude",
            "requires": ["claude"],
            "setup": {"timeout_seconds": 10},
            "execution_policy": "strict",
            "artifacts": {
                "capture": {
                    "tool_use_events": "stream_json_read_events",
                },
                "required": [
                    "tool_use_events",
                    "assertions",
                    "meta",
                ],
            },
            "command": [
                "claude",
                "-p",
                "--no-session-persistence",
                "--",
                "Synthetic smoke run.",
            ],
            "assertions": assertions,
        }
        try:
            case_path.write_text(json.dumps(case_data, indent=2) + "\n", encoding="utf-8")
            completed = subprocess.run(
                ["bash", "scripts/run-skill-smoke", "--case", case_id],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env=env,
            )
            payload = json.loads(last_run_path.read_text(encoding="utf-8"))
            record = next(
                candidate for candidate in payload["results"] if candidate.get("id") == case_id
            )
            return completed, record
        finally:
            if case_path.exists():
                case_path.unlink()
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)


def _agent_event(subagent_type: str, tool: str = "Agent"):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": tool,
                    "input": {"subagent_type": subagent_type},
                }
            ]
        },
    }


def _read_event(target: str):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": target},
                }
            ]
        },
    }


_RESULT_OK = {"type": "result", "subtype": "success", "result": "ok"}


class ToolUseMinCountTest(unittest.TestCase):
    def test_passes_when_count_meets_min(self):
        case_id = "zz.tool-use-min-count.pass"
        completed, record = _run_smoke_case(
            case_id,
            [{"id": "agent_calls_used_at_least_one_subagent"}],
            [_agent_event("general-purpose"), _RESULT_OK],
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_fails_when_zero_agent_events(self):
        case_id = "zz.tool-use-min-count.zero-agent"
        completed, record = _run_smoke_case(
            case_id,
            [{"id": "agent_calls_used_at_least_one_subagent"}],
            [_read_event("docs/protocol/planning.md"), _RESULT_OK],
        )
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("agent/subagent calls < min", record["reason"])

    def test_fails_when_artifact_missing(self):
        # Synthetically inject a contract that points at a missing
        # artifact filename so the kind takes the missing-artifact path.
        case_id = "zz.tool-use-min-count.artifact-missing"
        completed, record = _run_smoke_case(
            case_id,
            [
                {
                    "id": "agent_calls_used_at_least_one_subagent",
                    "overrides": {"artifact": "definitely-not-a-real-artifact.json"},
                }
            ],
            [_agent_event("general-purpose"), _RESULT_OK],
        )
        self.assertEqual(record["status"], "fail")
        self.assertIn("definitely-not-a-real-artifact.json is missing", record["reason"])

    def test_passes_with_explicit_min_three_against_five_agent_events(self):
        case_id = "zz.tool-use-min-count.min-three-pass"
        events = [
            _agent_event("general-purpose"),
            _agent_event("general-purpose"),
            _agent_event("general-purpose"),
            _agent_event("general-purpose"),
            _agent_event("general-purpose"),
            _RESULT_OK,
        ]
        completed, record = _run_smoke_case(
            case_id,
            [
                {
                    "id": "agent_calls_used_at_least_one_subagent",
                    "overrides": {"min": 3},
                }
            ],
            events,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_fails_with_only_read_events_and_no_agent_calls(self):
        case_id = "zz.tool-use-min-count.read-only"
        completed, record = _run_smoke_case(
            case_id,
            [{"id": "agent_calls_used_at_least_one_subagent"}],
            [
                _read_event("docs/protocol/planning.md"),
                _read_event("docs/protocol/execution.md"),
                _RESULT_OK,
            ],
        )
        self.assertEqual(record["status"], "fail")
        self.assertIn("agent/subagent calls < min", record["reason"])

    def test_passes_with_min_zero_and_zero_agent_events(self):
        # min: 0 demoted via per-fixture override always vacuously
        # passes. The schema_errors path (truncated stream) is irrelevant
        # at this knob.
        case_id = "zz.tool-use-min-count.min-zero-vacuous"
        completed, record = _run_smoke_case(
            case_id,
            [
                {
                    "id": "agent_calls_used_at_least_one_subagent",
                    "overrides": {"min": 0, "_comment": "vacuous"},
                }
            ],
            [_read_event("docs/protocol/planning.md"), _RESULT_OK],
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_fails_with_min_one_and_zero_agent_events_even_when_truncated(self):
        # Truncated stream (no `type=result`) and zero Agent events. The
        # default `min: 1` MUST still fail — truncation does not silence
        # a real "no Agent dispatch happened" violation.
        case_id = "zz.tool-use-min-count.min-one-truncated-fails"
        completed, record = _run_smoke_case(
            case_id,
            [{"id": "agent_calls_used_at_least_one_subagent"}],
            [_read_event("docs/protocol/planning.md")],
        )
        self.assertEqual(record["status"], "fail")
        self.assertIn("agent/subagent calls < min", record["reason"])

    def test_fails_with_schema_errors_and_no_events(self):
        # Schema-drift error state when `schema_errors=1, events=[]`.
        # Validates B3's schema-drift error handling: when artifact has
        # schema_errors but no captured events, with min=1 (default), B3 FAILS.
        # This differs from test_fails_when_artifact_missing because here
        # the artifact EXISTS (but contains empty events + schema errors).
        case_id = "zz.tool-use-min-count.schema-drift-no-events"

        # Pass an empty stream_lines list, which will cause parse_stream_json_capture
        # to generate schema_errors (both "no assistant" and "no result" errors)
        # and an empty events list. This naturally creates the artifact state
        # we want to test.
        completed, record = _run_smoke_case(
            case_id,
            [{"id": "agent_calls_used_at_least_one_subagent"}],
            []  # Empty stream generates schema_errors + empty events
        )

        self.assertEqual(record["status"], "fail")
        self.assertIn("CLI stream schema drift", record["reason"])


class ToolUseAgentSubagentTypeWhitelistTest(unittest.TestCase):
    def test_passes_when_all_general_purpose(self):
        case_id = "zz.tool-use-whitelist.pass"
        completed, record = _run_smoke_case(
            case_id,
            [{"id": "agent_calls_use_general_purpose_subagent_type"}],
            [_agent_event("general-purpose"), _RESULT_OK],
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_fails_with_schema_errors_and_no_events_for_whitelist(self):
        # Parallel to ToolUseMinCountTest::test_fails_with_schema_errors_and_no_events
        # but for B4 (agent_subagent_type_whitelist). B4 should also fail when
        # artifact contains `events: [], schema_errors: [...]` with the same
        # drift-handling pattern as the forbidden kind (consistent behavior).
        case_id = "zz.tool-use-whitelist.schema-drift-no-events"

        # Pass an empty stream_lines list to generate schema_errors + empty events.
        # B4 should follow the same drift path as B3.
        completed, record = _run_smoke_case(
            case_id,
            [{"id": "agent_calls_use_general_purpose_subagent_type"}],
            []  # Empty stream generates schema_errors + empty events
        )

        self.assertEqual(record["status"], "fail")
        self.assertIn("CLI stream schema drift", record["reason"])

    def test_fails_when_review_loop_reviewer_used(self):
        case_id = "zz.tool-use-whitelist.fail"
        completed, record = _run_smoke_case(
            case_id,
            [{"id": "agent_calls_use_general_purpose_subagent_type"}],
            [_agent_event("review-loop:reviewer"), _RESULT_OK],
        )
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "fail")
        self.assertIn("review-loop:reviewer", record["reason"])

    def test_fails_with_mixed_pass_and_fail(self):
        case_id = "zz.tool-use-whitelist.mixed"
        completed, record = _run_smoke_case(
            case_id,
            [{"id": "agent_calls_use_general_purpose_subagent_type"}],
            [
                _agent_event("general-purpose"),
                _agent_event("review-loop:reviewer"),
                _RESULT_OK,
            ],
        )
        self.assertEqual(record["status"], "fail")
        self.assertIn("review-loop:reviewer", record["reason"])

    def test_passes_when_no_subagent_type_events(self):
        # Vacuous pass: events captured but none carry subagent_type
        # (e.g. only Read events).
        case_id = "zz.tool-use-whitelist.no-subagent-events"
        completed, record = _run_smoke_case(
            case_id,
            [{"id": "agent_calls_use_general_purpose_subagent_type"}],
            [_read_event("docs/protocol/planning.md"), _RESULT_OK],
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")


class SmokeIdResolverOverrideTest(unittest.TestCase):
    def test_override_with_allowed_key_passes(self):
        case_id = "zz.smoke-id-resolver.allowed-key"
        completed, record = _run_smoke_case(
            case_id,
            [
                {
                    "id": "agent_calls_used_at_least_one_subagent",
                    "overrides": {"min": 0},
                }
            ],
            [_read_event("docs/protocol/planning.md"), _RESULT_OK],
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")

    def test_override_with_unwhitelisted_non_underscore_key_fails(self):
        case_id = "zz.smoke-id-resolver.unwhitelisted"
        completed, record = _run_smoke_case(
            case_id,
            [
                {
                    "id": "agent_calls_used_at_least_one_subagent",
                    "overrides": {"unknown_field": "x"},
                }
            ],
            [_agent_event("general-purpose"), _RESULT_OK],
        )
        self.assertEqual(record["status"], "fail")
        self.assertIn("unknown_field", record["reason"])

    def test_missing_id_falls_through_to_inline_branch(self):
        # An inline assertion object (no `id`) is rejected by the
        # require_assertion_list shape check before the resolver runs.
        case_id = "zz.smoke-id-resolver.missing-id"
        completed, record = _run_smoke_case(
            case_id,
            [{"overrides": {"min": 0}}],
            [_RESULT_OK],
        )
        self.assertEqual(record["status"], "fail")
        # Either parsed as inline-shape error or missing 'id' error.
        self.assertTrue(
            "missing string 'id'" in record["reason"]
            or "must be a non-empty string or object" in record["reason"]
            or "unexpected keys" in record["reason"],
            record["reason"],
        )

    def test_min_value_bool_is_rejected(self):
        # Regression: `isinstance(True, int)` is True in Python, so without
        # an explicit bool guard `"min": false` silently became `min == 0`
        # (vacuous-pass forever) and `"min": true` became `min == 1`. Both
        # bool values must now fail with the non-negative-integer error.
        for bool_value in (False, True):
            case_id = f"zz.smoke-id-resolver.min-bool-{str(bool_value).lower()}"
            completed, record = _run_smoke_case(
                case_id,
                [
                    {
                        "id": "agent_calls_used_at_least_one_subagent",
                        "overrides": {"min": bool_value},
                    }
                ],
                [_agent_event("general-purpose"), _RESULT_OK],
            )
            self.assertEqual(record["status"], "fail", f"min={bool_value!r}: {record}")
            self.assertIn("non-negative integer", record["reason"], f"min={bool_value!r}")

    def test_unknown_id_with_overrides_surfaces_dropped_keys(self):
        # Regression: previously an unknown id silently discarded supplied
        # overrides and reported a generic "unknown id" error. The error
        # must now name the dropped keys (sorted, without `_`-prefixed
        # metadata) so the silent-discard is surfaced.
        case_id = "zz.smoke-id-resolver.unknown-id-with-overrides"
        completed, record = _run_smoke_case(
            case_id,
            [
                {
                    "id": "definitely_no_such_assertion_id",
                    "overrides": {"min": 0, "_comment": "irrelevant"},
                }
            ],
            [_RESULT_OK],
        )
        self.assertEqual(record["status"], "fail")
        self.assertIn("unknown", record["reason"].lower())
        self.assertIn("min", record["reason"])
        # `_comment` is metadata — must not appear in the dropped-keys list.
        self.assertNotIn("_comment", record["reason"])

    def test_underscore_prefixed_override_key_is_ignored_silently(self):
        # `_comment` and other `_`-prefixed metadata are silently dropped
        # during merge; they don't reach contract validation, and they
        # don't end up on the resolved entry. With `min: 0` retained, the
        # assertion vacuously passes.
        case_id = "zz.smoke-id-resolver.underscore-ignored"
        completed, record = _run_smoke_case(
            case_id,
            [
                {
                    "id": "agent_calls_used_at_least_one_subagent",
                    "overrides": {"min": 0, "_comment": "rationale prose"},
                }
            ],
            [_read_event("docs/protocol/planning.md"), _RESULT_OK],
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(record["status"], "pass")


if __name__ == "__main__":
    unittest.main()
