"""Behavioral coverage for the stage instruction reader using isolated support roots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


READER = Path(__file__).resolve().parents[1] / "scripts" / "read_protocol.py"


def write(root: Path, relative: str, text: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def manifest(root: Path, units: dict, roots: list) -> None:
    write(root, "docs/protocol/loading.json", json.dumps({
        "units": units,
        "stages": {"planning": {"codex": roots, "claude": roots}},
    }))


def run(root: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(READER), "--root", str(root),
         "--runtime", "codex", "--stage", "planning", *args],
        cwd=str(root.parent), capture_output=True, text=True, check=False,
    )
    assert result.returncode == expect, result.stderr
    return result


def digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def bundle(unit: str, path: str, body: str) -> str:
    return f"<!-- {unit}@{digest(body)}; source: {path} -->\n{body}"


@pytest.mark.parametrize("fence", ["```", "~~~~", "````"])
def test_exact_section_keeps_nested_and_fenced_headings(tmp_path, fence):
    selected = (
        "## Selected\n实际指令。\n### Nested\nNested body.\n"
        f"{fence}markdown\n## Selected\n# Fake end\n"
        "```\n## Still fenced\n"
        f"{fence}\nAfter fence.\n"
    ) if fence != "```" else (
        "## Selected\n实际指令。\n### Nested\nNested body.\n"
        "```markdown\n## Selected\n# Fake end\n```\nAfter fence.\n"
    )
    write(tmp_path, "instructions.md",
          "# Document\n## Selected suffix\nExcluded.\n" + selected
          + "\n## Next\nExcluded next section.\n")
    manifest(tmp_path, {"selected": {
        "path": "instructions.md", "sections": ["## Selected"],
    }}, ["selected"])

    result = run(tmp_path)

    assert result.stdout == bundle("selected", "instructions.md", selected)
    assert result.stderr == ""


def test_section_order_follows_manifest_and_child_stops_at_peer(tmp_path):
    write(tmp_path, "instructions.md", "# Root\n## Parent\nParent.\n"
          "### Child\nChild body.\n### Peer\nPeer body.\n## Last\nLast body.\n")
    manifest(tmp_path, {"selected": {
        "path": "instructions.md", "sections": ["## Last", "### Child"],
    }}, ["selected"])

    expected = "## Last\nLast body.\n\n### Child\nChild body.\n"
    assert run(tmp_path).stdout == bundle("selected", "instructions.md", expected)


@pytest.mark.parametrize("failure", ["duplicate", "missing-section", "missing-source"])
def test_resolution_failure_never_emits_partial_bundle(tmp_path, failure):
    write(tmp_path, "ready.md", "Ready instruction.\n")
    units = {"ready": {"path": "ready.md"}, "broken": {
        "path": "broken.md", "sections": ["## Required"],
    }}
    if failure == "duplicate":
        write(tmp_path, "broken.md", "## Required\nFirst.\n## Required\nSecond.\n")
    elif failure == "missing-section":
        write(tmp_path, "broken.md", "## Required suffix\nWrong section.\n")
    manifest(tmp_path, units, ["ready", "broken"])

    result = run(tmp_path, expect=2)

    assert result.stdout == ""
    assert "read_protocol:" in result.stderr
    if failure == "duplicate":
        assert "got 2" in result.stderr
    elif failure == "missing-section":
        assert "got 0" in result.stderr
    else:
        assert "broken.md" in result.stderr


def test_cyclic_prerequisites_fail_without_partial_output(tmp_path):
    write(tmp_path, "ready.md", "Ready instruction.\n")
    manifest(tmp_path, {
        "ready": {"path": "ready.md"},
        "first": {"path": "first.md", "requires": ["second"]},
        "second": {"path": "second.md", "requires": ["first"]},
    }, ["ready", "first"])

    result = run(tmp_path, expect=2)

    assert result.stdout == ""
    assert "cyclic loading prerequisite" in result.stderr


def test_prerequisites_precede_dependents_and_shared_unit_loads_once(tmp_path):
    units = {}
    for name, dependencies in [("shared", []), ("left", ["shared"]),
                               ("right", ["shared"]), ("top", ["left", "right"])]:
        write(tmp_path, name + ".md", name + " instruction.\n")
        units[name] = {"path": name + ".md", "requires": dependencies}
    manifest(tmp_path, units, ["top", "right", "shared"])

    assert run(tmp_path).stdout == "\n".join(
        bundle(name, name + ".md", name + " instruction.\n")
        for name in ["shared", "left", "right", "top"]
    )


def test_loaded_fingerprint_skips_only_same_unit_and_content(tmp_path):
    body = "Identical instruction.\n"
    write(tmp_path, "instructions.md", body)
    manifest(tmp_path, {
        "first": {"path": "instructions.md"},
        "second": {"path": "instructions.md", "requires": ["first"]},
    }, ["second"])
    first_fingerprint = "first@" + digest(body)
    second_fingerprint = "second@" + digest(body)

    assert run(tmp_path, "--loaded", first_fingerprint).stdout == bundle(
        "second", "instructions.md", body)
    assert run(tmp_path, "--loaded", first_fingerprint,
               "--loaded", second_fingerprint).stdout == ""

    changed = "Updated instruction.\n"
    write(tmp_path, "instructions.md", changed)
    assert run(tmp_path, "--loaded", first_fingerprint,
               "--loaded", second_fingerprint).stdout == "\n".join(
        bundle(name, "instructions.md", changed) for name in ["first", "second"]
    )


def test_inventory_contains_metadata_without_instruction_bodies(tmp_path):
    body = "## Instructions\n不可出现在 metadata 中的正文。\n"
    write(tmp_path, "instructions.md", body)
    manifest(tmp_path, {"instructions": {"path": "instructions.md"}}, ["instructions"])

    result = run(tmp_path, "--inventory")

    assert json.loads(result.stdout) == [{
        "unit": "instructions", "path": "instructions.md", "sha256": digest(body),
        "bytes": len(body.encode("utf-8")), "lines": 2,
    }]
    assert "正文" not in result.stdout
    # An inventory is still complete when the caller has already loaded a unit.
    assert run(tmp_path, "--inventory", "--loaded",
               "instructions@" + digest(body)).stdout == result.stdout


@pytest.mark.parametrize("escape", ["parent", "absolute", "symlink"])
def test_source_cannot_escape_support_root(tmp_path, escape):
    root = tmp_path / "support"
    outside = write(tmp_path, "outside.md", "Outside instruction.\n")
    write(root, "ready.md", "Ready instruction.\n")
    if escape == "parent":
        path = "../outside.md"
    elif escape == "absolute":
        path = str(outside)
    else:
        (root / "linked.md").symlink_to(outside)
        path = "linked.md"
    manifest(root, {"ready": {"path": "ready.md"}, "escaped": {"path": path}},
             ["ready", "escaped"])

    result = run(root, expect=2)

    assert result.stdout == ""
    assert "read_protocol:" in result.stderr


def test_invalid_loaded_fingerprint_fails_without_output(tmp_path):
    write(tmp_path, "instructions.md", "Instruction.\n")
    manifest(tmp_path, {"instructions": {"path": "instructions.md"}}, ["instructions"])

    result = run(tmp_path, "--loaded", "instructions@not-a-digest", expect=2)

    assert result.stdout == ""
    assert "invalid --loaded fingerprint" in result.stderr
