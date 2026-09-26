"""Alternate entry points must not route report-only roles to writable Agents."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRIES = ("skills/review-pr/SKILL.md", "skills/code-quality-loop/SKILL.md")


def fenced_blocks(text):
    return re.findall(r"^```[^\n]*\n(.*?)^```[ \t]*$", text, re.M | re.S)


@pytest.mark.parametrize("entry", ENTRIES)
def test_only_simplifier_retains_a_writable_agent_payload(entry):
    blocks = fenced_blocks((ROOT/entry).read_text())
    writable = [block for block in blocks if re.search(r"^\s*subagent_type:", block, re.M)]
    assert len(writable) == 1
    assert "subagent_type: general-purpose" in writable[0]
    assert "agents/code-simplifier.md" in writable[0]
    assert "apply simplifications directly" in writable[0]
    assert not any("Native reviewer prompt" in block for block in writable)


@pytest.mark.parametrize("entry,report_count", [(ENTRIES[0], 1), (ENTRIES[1], 6)])
def test_report_payloads_use_native_launcher_and_entry_contract(entry, report_count):
    text = (ROOT/entry).read_text()
    blocks = fenced_blocks(text)
    reports = [block for block in blocks if "Native reviewer prompt" in block]
    assert len(reports) == report_count
    assert all("subagent_type:" not in block and "Agent tool parameters" not in block for block in reports)
    contract = "../../docs/protocol/reviewer-runtime.md"
    assert contract in text and text.index(contract) < text.index("## Step" if "review-pr" in entry else "## Initialization")
    launcher = [block for block in blocks if "scripts/run_claude_reviewer.py" in block]
    assert len(launcher) == 1
    assert all(flag in launcher[0] for flag in (
        "--session-id", "--model", "--stage polish", "--role <agent-name>",
        "--timeout-seconds",
    ))
    assert "stream_file" in text and "result_file" in text and "usage_file" in text
    assert "tool_uses" in text and "do not read the" in text
    assert "tool_uses:" not in text


def test_language_review_prompt_receives_caller_evidence_without_command_authority():
    text = (ROOT/"skills/code-quality-loop/SKILL.md").read_text()
    language = next(block for block in fenced_blocks(text) if "Caller Static Analysis Evidence" in block)
    assert "exit statuses" in language and "unavailable tools" in language
    assert "Use read/search tools only" in language
    assert "do not modify files or execute commands" in language
    assert "Use Claude Code's native Bash tool" not in language
    assert "Run static analysis on the changed files" not in language


def test_existing_dispatch_mapping_and_tier_contracts_remain_satisfied():
    mapping = json.loads((ROOT/"tests/skills/contracts/assertion-mapping.json").read_text())
    checked = []

    def walk(value):
        if isinstance(value, dict):
            if value.get("path") in ENTRIES and isinstance(value.get("needle"), str):
                assert value["needle"] in (ROOT/value["path"]).read_text()
                checked.append(value["path"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(mapping)
    assert set(checked) == set(ENTRIES)
    assert len(checked) >= 16


def test_simplifier_still_follows_reports_and_test_quality_stays_after_tests():
    review = (ROOT/ENTRIES[0]).read_text()
    quality = (ROOT/ENTRIES[1]).read_text()
    assert review.index("### Report-only agents") < review.index("### The `simplify` aspect")
    assert quality.index("### Phase 1: REVIEW") < quality.index("### Phase 4: FIX") < quality.index("## Finalize")
    assert quality.index("### Step 2: Simplify") < quality.index("### Step 3: Test consolidation") < quality.index("### Step 4: Test quality gate")
