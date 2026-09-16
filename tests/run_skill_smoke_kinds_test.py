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


# ---------------------------------------------------------------------------
# Reviewer-result kinds (Slice 4: live Opus Reviewer smoke for cases 6 / 7)
# ---------------------------------------------------------------------------

RUBRIC_LINES = [
    "  Trigger: a caller passes a negative window size",
    "  Reachability: every public entry point accepts the raw argument",
    "  Impact: the retention sweep deletes records outside the window",
    "  Likelihood: high on any misconfigured deployment",
    "  Fix cost: small; clamp the argument at the boundary",
    "  Cheaper response: a MINOR would leave irreversible deletion in place",
]


def _review(verdict: str, issues=None, strengths: str = "- clear change") -> str:
    text = f"### VERDICT: {verdict}\n\n"
    if issues is not None:
        text += "### Issues\n" + "\n".join(issues) + "\n\n"
    return text + f"### Strengths\n{strengths}\n"


def _complete_critical() -> str:
    return "\n".join(["- [CRITICAL] retention sweep deletes outside the window"] + RUBRIC_LINES
                     + ["  File: `pkg/store.py`, around line 12"])


def _incomplete_critical() -> str:
    return "\n".join(["- [CRITICAL] retention sweep deletes outside the window"] + RUBRIC_LINES[:4]
                     + ["  File: `pkg/store.py`, around line 12"])


def _envelope(result_text: str, **extra) -> dict:
    payload = {"type": "result", "subtype": "success", "is_error": False, "result": result_text}
    payload.update(extra)
    return payload


def _write_fake_claude_payload(bin_dir: Path, payload_text: str) -> None:
    # A fake `claude` whose stdout is exactly `payload_text` (a JSON envelope,
    # or deliberately non-JSON text). The payload lives in a file so quotes
    # and backticks in reviewer text never need shell escaping.
    payload_path = bin_dir / "payload.txt"
    payload_path.write_text(payload_text, encoding="utf-8")
    script_path = bin_dir / "claude"
    script_path.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ncat >/dev/null\n"
        f"cat {json.dumps(str(payload_path))}\n",
        encoding="utf-8",
    )
    script_path.chmod(0o755)


def _run_reviewer_smoke_case(case_id: str, assertions, payload_text: str):
    case_path = ROOT / "tests/skills/smoke" / f"{case_id}.json"
    artifact_dir = ROOT / "tests/skills/.artifacts" / case_id
    last_run_path = ROOT / "tests/skills/.last-run.json"
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir)
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_bin = Path(tmpdir)
        _write_fake_claude_payload(fake_bin, payload_text)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        case_data = {
            "id": case_id,
            "type": "smoke",
            "target": "reviewer",
            "runtime": "claude",
            "requires": ["claude"],
            "setup": {"timeout_seconds": 10},
            "execution_policy": "strict",
            "artifacts": {
                "capture": {
                    "reviewer_prompt": "reviewer_prompt_file",
                    "reviewer_result": "reviewer_result_file",
                },
                "required": ["reviewer_result", "assertions", "meta"],
            },
            "command": [
                "bash", "-lc",
                "printf 'synthetic packet' | claude -p --no-session-persistence --model opus --output-format json",
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
                stdin=subprocess.DEVNULL,
            )
            payload = json.loads(last_run_path.read_text(encoding="utf-8"))
            record = next(candidate for candidate in payload["results"] if candidate.get("id") == case_id)
            measurement_path = artifact_dir / "reviewer-measurement.json"
            measurement = (
                json.loads(measurement_path.read_text(encoding="utf-8")) if measurement_path.exists() else None
            )
            return completed, record, measurement
        finally:
            if case_path.exists():
                case_path.unlink()
            if artifact_dir.exists():
                shutil.rmtree(artifact_dir)


def _assertion_message(record, assertion_id):
    return record["reason"]


class ReviewerNoBlockingCriticalKindTest(unittest.TestCase):
    KIND = "reviewer_no_blocking_critical"

    def test_approve_without_issues_passes(self):
        completed, record, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case6.approve", [self.KIND], json.dumps(_envelope(_review("APPROVE"))))
        self.assertEqual(record["status"], "pass", record)
        self.assertEqual(measurement["verdict"], "APPROVE")
        self.assertEqual(measurement["critical_total"], 0)

    def test_approve_with_minor_only_passes(self):
        _c, record, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case6.minor-only",
            [self.KIND],
            json.dumps(_envelope(_review("APPROVE", ["- [MINOR] naming nit", "  File: `pkg/store.py`"]))),
        )
        self.assertEqual(record["status"], "pass", record)
        self.assertEqual(measurement["critical_total"], 0)

    def test_complete_critical_fails(self):
        _c, record, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case6.complete-critical",
            [self.KIND],
            json.dumps(_envelope(_review("REQUEST_CHANGES", [_complete_critical()]))),
        )
        self.assertEqual(record["status"], "fail", record)
        self.assertIn("[CRITICAL]", record["reason"])
        self.assertEqual(measurement["critical_complete"], 1)

    def test_incomplete_critical_fails(self):
        _c, record, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case6.incomplete-critical",
            [self.KIND],
            json.dumps(_envelope(_review("REQUEST_CHANGES", [_incomplete_critical()]))),
        )
        self.assertEqual(record["status"], "fail", record)
        self.assertEqual(measurement["critical_incomplete"], 1)

    def test_minor_only_request_changes_is_schema_invalid_and_fails(self):
        _c, record, _m = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case6.minor-only-request-changes",
            [self.KIND],
            json.dumps(_envelope(_review("REQUEST_CHANGES", ["- [MINOR] naming nit"]))),
        )
        self.assertEqual(record["status"], "fail", record)
        self.assertIn("schema", record["reason"])

    def test_missing_artifact_fails(self):
        # Non-JSON stdout: the wrapper writes no reviewer-result.json → the
        # kind fails on the missing artifact (never a vacuous pass).
        _c, record, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case6.missing-artifact", [self.KIND], _review("APPROVE"))
        self.assertEqual(record["status"], "fail", record)
        self.assertIn("missing", record["reason"])
        self.assertIsNone(measurement)

    def test_malformed_result_text_fails(self):
        _c, record, _m = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case6.malformed", [self.KIND], json.dumps(_envelope("no verdict header at all")))
        self.assertEqual(record["status"], "fail", record)


class ReviewerCompleteBlockingCriticalKindTest(unittest.TestCase):
    KIND = "reviewer_complete_blocking_critical"

    def test_complete_critical_passes(self):
        _c, record, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case7.complete-critical",
            [self.KIND],
            json.dumps(_envelope(_review("REQUEST_CHANGES", [_complete_critical()]))),
        )
        self.assertEqual(record["status"], "pass", record)
        self.assertEqual(measurement["critical_complete"], 1)

    def test_complete_plus_incomplete_still_passes(self):
        _c, record, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case7.mixed",
            [self.KIND],
            json.dumps(_envelope(_review("REQUEST_CHANGES", [_complete_critical(), _incomplete_critical()]))),
        )
        self.assertEqual(record["status"], "pass", record)
        self.assertEqual((measurement["critical_complete"], measurement["critical_incomplete"]), (1, 1))

    def test_incomplete_critical_only_fails(self):
        _c, record, _m = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case7.incomplete-only",
            [self.KIND],
            json.dumps(_envelope(_review("REQUEST_CHANGES", [_incomplete_critical()]))),
        )
        self.assertEqual(record["status"], "fail", record)
        self.assertIn("finding_triage.py check", record["reason"])

    def test_approve_fails(self):
        _c, record, _m = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case7.approve", [self.KIND], json.dumps(_envelope(_review("APPROVE"))))
        self.assertEqual(record["status"], "fail", record)

    def test_missing_artifact_fails(self):
        _c, record, _m = _run_reviewer_smoke_case(
            "zz.reviewer-kind.case7.missing-artifact", [self.KIND], "plain text, not an envelope")
        self.assertEqual(record["status"], "fail", record)
        self.assertIn("missing", record["reason"])


class ReviewerEnvelopeShapeTest(unittest.TestCase):
    # One case per envelope shape: the smoke report row (reviewer-measurement.json)
    # carries the value when the `claude -p --output-format json` envelope has
    # it, and the literal "N/A" otherwise — never a guessed number.

    def test_full_envelope_carries_values(self):
        _c, record, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.envelope.full",
            ["reviewer_no_blocking_critical"],
            json.dumps(_envelope(
                _review("APPROVE"),
                duration_ms=4321,
                usage={"input_tokens": 1500, "output_tokens": 220},
                total_cost_usd=0.0875,
                modelUsage={"claude-opus-4-1": {"inputTokens": 1500, "outputTokens": 220},
                            "claude-haiku-4-5": {"inputTokens": 10, "outputTokens": 3}},
            )),
        )
        self.assertEqual(record["status"], "pass", record)
        self.assertEqual(measurement["duration_ms"], 4321)
        self.assertEqual(measurement["tokens_in"], 1500)
        self.assertEqual(measurement["tokens_out"], 220)
        self.assertEqual(measurement["cost_usd"], 0.0875)
        self.assertEqual(measurement["model"], "claude-opus-4-1")

    def test_envelope_without_duration_ms(self):
        _c, _r, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.envelope.no-duration",
            ["reviewer_no_blocking_critical"],
            json.dumps(_envelope(_review("APPROVE"), usage={"input_tokens": 5, "output_tokens": 6},
                                 total_cost_usd=0.01, modelUsage={"claude-opus-4-1": {"outputTokens": 6}})),
        )
        self.assertEqual(measurement["duration_ms"], "N/A")
        self.assertEqual((measurement["tokens_in"], measurement["tokens_out"]), (5, 6))

    def test_envelope_without_usage(self):
        _c, _r, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.envelope.no-usage",
            ["reviewer_no_blocking_critical"],
            json.dumps(_envelope(_review("APPROVE"), duration_ms=10, total_cost_usd=0.02)),
        )
        self.assertEqual((measurement["tokens_in"], measurement["tokens_out"]), ("N/A", "N/A"))
        self.assertEqual(measurement["model"], "N/A")
        self.assertEqual(measurement["duration_ms"], 10)

    def test_envelope_without_total_cost_usd(self):
        _c, _r, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.envelope.no-cost",
            ["reviewer_no_blocking_critical"],
            json.dumps(_envelope(_review("APPROVE"), duration_ms=10, usage={"input_tokens": 1, "output_tokens": 2})),
        )
        self.assertEqual(measurement["cost_usd"], "N/A")

    def test_bare_envelope_is_all_na(self):
        _c, _r, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.envelope.bare", ["reviewer_no_blocking_critical"],
            json.dumps(_envelope(_review("APPROVE"))))
        for field in ("duration_ms", "model", "tokens_in", "tokens_out", "cost_usd"):
            self.assertEqual(measurement[field], "N/A", field)

    def test_bool_and_string_values_are_not_numbers(self):
        _c, _r, measurement = _run_reviewer_smoke_case(
            "zz.reviewer-kind.envelope.non-numeric", ["reviewer_no_blocking_critical"],
            json.dumps(_envelope(_review("APPROVE"), duration_ms=True, total_cost_usd="0.5",
                                 usage={"input_tokens": "9", "output_tokens": False})))
        for field in ("duration_ms", "tokens_in", "tokens_out", "cost_usd"):
            self.assertEqual(measurement[field], "N/A", field)


class TriageReportFieldsTest(unittest.TestCase):
    # `run-skill-smoke` reads the `finding_triage.py check` report through
    # this strict projection: a missing key is helper/report contract drift
    # and surfaces as a FAIL record, never as a defaulted measurement.

    def setUp(self):
        import sys
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts.run_skill_smoke_lib import TRIAGE_REPORT_KEYS, triage_report_fields
        self.keys = TRIAGE_REPORT_KEYS
        self.project = triage_report_fields

    def _report(self):
        return {"shape": "reviewer", "verdict": "REQUEST_CHANGES", "inspected": True, "result": "complete",
                "findings": [{"n": 1, "severity": "CRITICAL", "complete": True}], "complete": [1],
                "incomplete": [], "infrastructure": [], "summary": [], "executor_dispatch": True}

    def test_full_report_projects_consumed_keys(self):
        fields = self.project(self._report())
        self.assertEqual(tuple(fields), self.keys)
        self.assertEqual((fields["result"], fields["complete"], fields["incomplete"]), ("complete", [1], []))

    def test_missing_key_raises_key_error_naming_it(self):
        for key in ("findings", "complete", "incomplete", "summary", "result"):
            report = self._report()
            del report[key]
            with self.assertRaises(KeyError) as ctx:
                self.project(report)
            self.assertEqual(ctx.exception.args[0], key)

    def test_finding_without_severity_and_non_object_report_are_rejected(self):
        report = self._report()
        report["findings"] = [{"n": 1}]
        with self.assertRaises(KeyError) as ctx:
            self.project(report)
        self.assertEqual(ctx.exception.args[0], "findings[].severity")
        with self.assertRaises(TypeError):
            self.project(["not", "an", "object"])

    def test_real_helper_report_satisfies_the_projection(self):
        import json as _json
        import subprocess as _subprocess
        import sys as _sys
        with tempfile.TemporaryDirectory() as tmpdir:
            review_path = Path(tmpdir) / "review.md"
            review_path.write_text(_review("REQUEST_CHANGES", [_complete_critical()]), encoding="utf-8")
            completed = _subprocess.run(
                [_sys.executable, str(ROOT / "scripts" / "finding_triage.py"), "check", "--input", str(review_path)],
                cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        fields = self.project(_json.loads(completed.stdout))
        self.assertEqual(fields["complete"], [1])


if __name__ == "__main__":
    unittest.main()
