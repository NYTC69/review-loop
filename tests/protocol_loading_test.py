"""Behavioral coverage for the stage instruction reader using isolated support roots."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


READER = Path(__file__).resolve().parents[1] / "scripts" / "read_protocol.py"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from read_protocol import resolve  # noqa: E402 -- repository-local script import


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


def test_output_file_avoids_stdout_truncation_and_is_complete(tmp_path):
    root = tmp_path / "support"
    body = "Instruction line.\n" * 5000
    write(root, "instructions.md", body)
    manifest(root, {"instructions": {"path": "instructions.md"}}, ["instructions"])

    result = run(root, "--output", ".review-loop/tmp/bundle.md")

    receipt = json.loads(result.stdout)
    expected = bundle("instructions", "instructions.md", body)
    assert receipt == {
        "protocol_output": ".review-loop/tmp/bundle.md",
        "sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest(),
        "bytes": len(expected.encode("utf-8")),
        "lines": len(expected.splitlines()),
    }
    assert result.stderr == ""
    assert (tmp_path / ".review-loop/tmp/bundle.md").read_text() == expected
    assert not list((tmp_path / ".review-loop/tmp").glob(".protocol-*.tmp"))


@pytest.mark.parametrize("target", ["bundle.md", "../bundle.md", ".review-loop/elsewhere/bundle.md"])
def test_output_file_must_stay_in_workspace_protocol_tmp(tmp_path, target):
    root = tmp_path / "support"
    write(root, "instructions.md", "Instruction.\n")
    manifest(root, {"instructions": {"path": "instructions.md"}}, ["instructions"])

    result = run(root, "--output", target, expect=2)

    assert result.stdout == ""
    assert "read_protocol:" in result.stderr


def run_with_tmpdir(root: Path, tmpdir: Path, *args: str, expect: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(READER), "--root", str(root), "--runtime", "codex", "--stage", "planning", *args],
        cwd=str(root.parent), capture_output=True, text=True, check=False,
        env={**os.environ, "TMPDIR": str(tmpdir)},
    )
    assert result.returncode == expect, result.stderr
    return result


def user_root(temp: Path) -> Path:
    return temp.resolve() / f"review-loop-protocol-{os.getuid()}"


def test_output_may_go_to_the_protocol_temp_root_outside_the_worktree(tmp_path):
    """FIELD-18: the entry loads must not leave files in the product worktree (cwd)."""
    root, temp = tmp_path / "work" / "support", tmp_path / "systemtmp"   # cwd (the worktree) is tmp_path/work
    temp.mkdir()
    body = "Instruction.\n"
    write(root, "instructions.md", body)
    manifest(root, {"instructions": {"path": "instructions.md"}}, ["instructions"])
    target = user_root(temp) / "a1b2c3" / "protocol-codex-planning.md"

    result = run_with_tmpdir(root, temp, "--output", str(target))

    assert json.loads(result.stdout)["protocol_output"] == str(target)
    assert target.read_text() == bundle("instructions", "instructions.md", body)
    assert user_root(temp).stat().st_mode & 0o777 == 0o700   # created private for this user
    assert not (tmp_path / "work" / ".review-loop").exists()   # nothing written in the worktree


@pytest.mark.parametrize("relative", ["bundle.md", "review-loop-protocol/bundle.md",
                                      "review-loop-protocol-0x/bundle.md", "elsewhere/review-loop-protocol/b.md"])
def test_an_absolute_output_outside_both_roots_is_refused(tmp_path, relative):
    root, temp = tmp_path / "work" / "support", tmp_path / "systemtmp"   # cwd (the worktree) is tmp_path/work
    temp.mkdir()
    write(root, "instructions.md", "Instruction.\n")
    manifest(root, {"instructions": {"path": "instructions.md"}}, ["instructions"])

    result = run_with_tmpdir(root, temp, "--output", str(temp.resolve() / relative), expect=2)

    assert result.stdout == ""
    assert "read_protocol:" in result.stderr
    assert not (temp / relative).exists()


def test_a_protocol_temp_root_symlinked_into_the_worktree_is_refused(tmp_path):
    root, temp = tmp_path / "work" / "support", tmp_path / "systemtmp"   # cwd (the worktree) is tmp_path/work
    temp.mkdir()
    write(root, "instructions.md", "Instruction.\n")
    manifest(root, {"instructions": {"path": "instructions.md"}}, ["instructions"])
    inside = tmp_path / "work" / "planted"   # inside the product worktree (the run's cwd)
    inside.mkdir(mode=0o700)
    user_root(temp).symlink_to(inside, target_is_directory=True)

    result = run_with_tmpdir(root, temp, "--output", str(user_root(temp) / "x" / "bundle.md"), expect=2)

    assert result.stdout == ""
    assert "read_protocol:" in result.stderr
    assert list(inside.iterdir()) == []


def test_a_shared_or_open_protocol_temp_root_is_refused(tmp_path):
    root, temp = tmp_path / "work" / "support", tmp_path / "systemtmp"   # cwd (the worktree) is tmp_path/work
    temp.mkdir()
    write(root, "instructions.md", "Instruction.\n")
    manifest(root, {"instructions": {"path": "instructions.md"}}, ["instructions"])
    user_root(temp).mkdir(mode=0o755)
    user_root(temp).chmod(0o755)

    result = run_with_tmpdir(root, temp, "--output", str(user_root(temp) / "x" / "bundle.md"), expect=2)

    assert "must be a directory owned by this user with mode 0700" in result.stderr
    assert not (user_root(temp) / "x").exists()


# Restored from the deleted tests/protocol_loading_graph_test.py (LG-DEL-2 gate): they cover the production
# loading map that the review-loop and paired-session entries still load.
def test_codex_umbrella_keeps_handsfree_flag_override():
    text = "\n".join(u["body"] for u in resolve(ROOT, "codex", "entry-review-loop"))
    assert "when present it overrides the config" in text


def test_every_declared_bundle_resolves_and_only_references_repo_files():
    config = json.loads((ROOT / "docs/protocol/loading.json").read_text())
    for stage, branches in config["stages"].items():
        for runtime in branches:
            units = resolve(ROOT, runtime, stage)
            assert units
            assert len({u["unit"] for u in units}) == len(units)
            for unit in units:
                (ROOT / unit["path"]).resolve().relative_to(ROOT)
