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
import stat
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


def outside_root():
    """The per-user temp-area root for bundles that must stay out of the product worktree (paired-session entry,
    FIELD-18). The last component is not resolved: write_bundle refuses it when it is a symlink."""
    return Path(tempfile.gettempdir()).resolve() / f"review-loop-protocol-{os.getuid()}"


def checked_outside_root(create=False):
    """The root must be a real directory owned by this user with no group/other access (shared /tmp)."""
    root = outside_root()
    if create:
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"protocol temp root {root} must be a directory owned by this user with mode 0700")
    return root


def write_bundle(path, content):
    """Atomically write a bundle only under the per-user temp-area root, outside the product worktree."""
    workspace = Path.cwd().resolve()
    requested = Path(path)
    if not requested.is_absolute():
        raise ValueError(f"--output must be an absolute path under {outside_root()}")
    target = requested.resolve()
    target.relative_to(outside_root())
    checked_outside_root(create=True)
    try:
        target.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise ValueError("--output must not resolve into the workspace")
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
                        help="atomically write output to an absolute path under "
                             "<system temp>/review-loop-protocol-<uid>/ (outside the product worktree)")
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
