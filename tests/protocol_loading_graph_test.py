"""Action prerequisites and cross-runtime coverage of the production loading map."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from read_protocol import resolve  # noqa: E402 -- repository-local script imports
from measure_protocol_loading import CASES, scenario  # noqa: E402


def names(runtime, stage):
    return {u["unit"] for u in resolve(ROOT, runtime, stage)}


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("stage", ["planning", "planning-review", "execution", "execution-review", "gate", "polish", "docs", "security", "delivery"])
def test_fresh_context_gets_cross_cutting_safety_before_action(runtime, stage):
    actual = names(runtime, stage)
    required = {"session-shape", "lock", "baseline", "snapshot", "packet", "exit-guards", "model-tiers", runtime + "-config"}
    if stage not in ("planning", "planning-review"):
        required.add("evidence")
    assert required <= actual


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_plan_does_not_preload_downstream_procedures(runtime):
    for stage in ["entry-plan", "session-init", "plan-init", "planning", "planning-review"]:
        assert not {"gate", "polish", "docs", "security", "delivery", "exec-start"} & names(runtime, stage)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_review_loads_real_validation_retry_and_dispute_contracts(runtime):
    for stage in ["planning-review", "execution-review"]:
        units = resolve(ROOT, runtime, stage)
        assert {"review-schema", "triage", "reviewer-dispatch"} <= {x["unit"] for x in units}
        assert "question-policy" in {x["unit"] for x in units}
        text = "\n".join(x["body"] for x in units)
        assert "The orchestrator never implements a CRITICAL that failed triage" in text
        assert "Release the single-writer lock before exiting" in text
    dispute = "\n".join(x["body"] for x in resolve(ROOT, runtime, "dispute"))
    assert "The Reviewer's verdict is never overridden" in dispute
    assert "evidence" in names(runtime, "dispute")


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_compacted_execution_and_downstream_keep_stop_and_cap_rules(runtime):
    for stage in ["execution", "execution-review", "gate", "polish", "docs", "security", "delivery"]:
        assert {"stop-flags", "caps", "question-policy"} <= names(runtime, stage)


def test_codex_umbrella_keeps_handsfree_flag_override():
    text = "\n".join(u["body"] for u in resolve(ROOT, "codex", "entry-review-loop"))
    assert "when present it overrides the config" in text


def test_codex_config_has_no_nested_reviewer_procedure():
    config = next(u for u in resolve(ROOT, "codex", "entry-plan") if u["unit"] == "codex-config")
    assert "### Default Reviewer Path" not in config["body"]
    assert "### Optional Local Reviewer Path" not in config["body"]


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_review_only_dispatch_preserves_auditable_skip_marker(runtime):
    text = "\n".join(u["body"] for u in resolve(ROOT, runtime, "execution-review"))
    assert "- Executor backend: skipped (review-only first round)" in text


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_umbrella_retains_lock_at_plan_approval(runtime):
    prefix = "skills" if runtime == "claude" else ".agents/skills"
    wrapper = (ROOT / prefix / "review-loop/SKILL.md").read_text()
    assert "only the plan-only entry loads `plan-exit`" in wrapper
    assert "retains its session/lock" in wrapper
    assert f"{runtime}-plan-exit" not in names(runtime, "planning-review")


def test_codex_only_parallel_transport_is_not_available_to_claude():
    with pytest.raises(KeyError):
        resolve(ROOT, "claude", "parallel-review")
    assert "parallel-review" in names("codex", "parallel-review")


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("stage", ["polish", "docs", "security"])
def test_downstream_agents_cannot_pass_with_zero_tools(runtime, stage):
    text = "\n".join(u["body"] for u in resolve(ROOT, runtime, stage))
    assert "returning `tool_uses: 0`, discard its result and retry once" in text
    assert "skipped by this guard is not a successful check" in " ".join(text.split())


def test_every_declared_bundle_resolves_and_only_references_repo_files():
    config = json.loads((ROOT / "docs/protocol/loading.json").read_text())
    for stage, branches in config["stages"].items():
        for runtime in branches:
            units = resolve(ROOT, runtime, stage)
            assert units
            assert len({u["unit"] for u in units}) == len(units)
            for unit in units:
                (ROOT / unit["path"]).resolve().relative_to(ROOT)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("case", ["planning-only", "one-file-delivery"])
def test_startup_measured_from_production_units_reduces_at_least_40_percent(runtime, case):
    mode, actions = CASES[case]
    row = scenario(ROOT, runtime, case, mode, actions)
    assert row["startup_reduction_percent"] >= 40, row
    assert row["live_rule_bytes"] == "unverified"


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_resume_reloads_after_context_loss(runtime):
    mode, actions = CASES["stop-resume"]
    row = scenario(ROOT, runtime, "stop-resume", mode, actions)
    reset = next(i for i, x in enumerate(row["trace"]) if x["action"] == "NEW_CONTEXT")
    assert "session-shape" in row["trace"][reset + 1]["emitted"]
    assert "lock" in row["trace"][reset + 1]["emitted"]
    assert "evidence" in row["trace"][reset + 1]["emitted"]


def test_behavior_engines_untouched_by_loading_refactor():
    for name in ["evidence_ledger.py", "finding_triage.py", "adversarial_gate_adapter.py", "adversarial_gate_invoke.py", "review_verification.py"]:
        path = "scripts/" + name
        original = subprocess.check_output(["git", "show", "cadb06c:" + path], cwd=ROOT)
        assert (ROOT / path).read_bytes() == original, path
