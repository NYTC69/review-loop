#!/usr/bin/env python3
"""Read stage-specific instructions without interpreting workflow decisions.

The declarative map is the loading SSOT. Markdown headings inside fences are
not section boundaries. A failed dependency/section resolution emits no bundle.
Loaded fingerprints are caller-local: never infer model context from a session
file, and drop them after compaction/resume or for a different agent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def headings(text):
    result = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            run = marker.group(1)
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not stripped[len(run):].strip():
                fence = None
        elif fence is None:
            match = re.match(r"^(#{1,6}) +(.+?)\s*$", line)
            if match:
                result.append((len(match.group(1)), line.strip(), offset))
        offset += len(line)
    return result


def section(text, title):
    marks = headings(text)
    matches = [i for i, (_, heading, _) in enumerate(marks) if heading == title]
    if len(matches) != 1:
        raise ValueError(f"expected one section {title!r}, got {len(matches)}")
    i = matches[0]
    level, _, start = marks[i]
    end = next((pos for depth, _, pos in marks[i + 1:] if depth <= level), len(text))
    return text[start:end].rstrip() + "\n"


def local_path(root, relative):
    candidate = (root / relative).resolve()
    candidate.relative_to(root.resolve())
    return candidate


def resolve(root, runtime, stage):
    config = json.loads((root / "docs/protocol/loading.json").read_text())
    roots = config["stages"][stage][runtime]
    resolved = []
    done, visiting = set(), set()

    def visit(name):
        if name in visiting:
            raise ValueError(f"cyclic loading prerequisite: {name}")
        if name in done:
            return
        visiting.add(name)
        unit = config["units"][name]
        for dep in unit.get("requires", []):
            visit(dep)
        text = local_path(root, unit["path"]).read_text(encoding="utf-8")
        body = "\n".join(section(text, h) for h in unit["sections"]) if "sections" in unit else text
        for title in unit.get("exclude", []):
            excluded = section(text, title)
            if excluded.rstrip() not in body:
                raise ValueError(f"excluded section {title!r} is not inside unit {name}")
            body = body.replace(excluded.rstrip(), "", 1)
        digest = hashlib.sha256(body.encode()).hexdigest()
        resolved.append({"unit": name, "path": unit["path"], "body": body,
                         "sha256": digest, "bytes": len(body.encode()),
                         "lines": len(body.splitlines())})
        visiting.remove(name)
        done.add(name)

    for name in roots:
        visit(name)
    return resolved


def write_bundle(path, content):
    """Atomically write a bundle only inside the task's ignored tmp area."""
    workspace = Path.cwd().resolve()
    requested = Path(path)
    if requested.is_absolute():
        target = requested.resolve()
    else:
        if requested.parts[:2] != (".review-loop", "tmp"):
            raise ValueError("--output must be inside .review-loop/tmp")
        target = (workspace / requested).resolve()
    allowed = (workspace / ".review-loop" / "tmp").resolve()
    target.relative_to(allowed)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".protocol-", suffix=".tmp",
            dir=target.parent, delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="support repository, not task workspace")
    parser.add_argument("--runtime", choices=("claude", "codex"), required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--loaded", action="append", default=[], metavar="UNIT@SHA256",
                        help="only units still available in THIS live context; repeatable")
    parser.add_argument("--inventory", action="store_true", help="metadata only; does not count as reading instructions")
    parser.add_argument("--output", type=Path,
                        help="atomically write output under task workspace .review-loop/tmp")
    args = parser.parse_args(argv)
    try:
        units = resolve(args.root, args.runtime, args.stage)
        seen = set(args.loaded)
        for item in seen:
            if not re.fullmatch(r"[a-z0-9_-]+@[a-f0-9]{64}", item):
                raise ValueError("invalid --loaded fingerprint")
        if args.inventory:
            rendered = json.dumps(
                [{k: v for k, v in unit.items() if k != "body"} for unit in units],
                indent=2,
            ) + "\n"
        else:
            chunks = []
            for unit in units:
                fingerprint = unit["unit"] + "@" + unit["sha256"]
                if fingerprint not in seen:
                    chunks.append(f"<!-- {fingerprint}; source: {unit['path']} -->\n{unit['body']}")
            rendered = "\n".join(chunks)
        if args.output:
            write_bundle(args.output, rendered)
            print(json.dumps({
                "protocol_output": str(args.output),
                "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                "bytes": len(rendered.encode("utf-8")),
                "lines": len(rendered.splitlines()),
            }, sort_keys=True))
        else:
            print(rendered, end="")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"read_protocol: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
