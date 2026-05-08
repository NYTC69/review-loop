#!/usr/bin/env python3
"""Session-replay parser for `subagent_type` audit over .review-loop/sessions/*.md.

Walks the session-corpus directory, enumerates every `subagent_type` value
mentioned per file, and flags any `review-loop:*` value as an anomaly.

Per CLAUDE.md "Plugin agent type sandbox bug (CRITICAL)", any plugin-defined
`subagent_type` (e.g. `review-loop:executor`) silently runs with `tool_uses:
0` and hallucinated output. The mandated invocation is
`subagent_type: general-purpose` with the agent body inlined.

Output: JSON by default; --text emits a human-readable table.
Exit 1 if any anomaly is detected, 0 otherwise. --exit-zero forces 0.

Stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# Primary regex: `subagent_type: <value>` where <value> is either wrapper-quoted
# (preserving inner chars) OR the literal `general-purpose` OR a `review-loop:`
# prefixed token. The `\b` left anchor rejects keys like `requires_subagent_type`.
SUBAGENT_TYPE_RE = re.compile(
    r"\bsubagent_type\s*:\s*"
    r"(?:"
        r"`(?P<bt>[A-Za-z0-9_:*<>\-]+)`"
        r"|'(?P<sq>[A-Za-z0-9_:*<>\-]+)'"
        r"|\"(?P<dq>[A-Za-z0-9_:*<>\-]+)\""
        r"|(?P<bare>general-purpose|review-loop:[A-Za-z0-9_:*<>\-]+)"
    r")"
)

# Closed-set known agent names. Secondary regex catches REAL bare-form
# occurrences (no `subagent_type:` precedent) of `review-loop:<known-name>`.
AGENT_NAMES = (
    "executor", "reviewer", "code-reviewer", "silent-failure-hunter",
    "comment-analyzer", "type-design-analyzer", "pr-test-analyzer",
    "code-simplifier", "go-reviewer", "rust-reviewer", "python-reviewer",
    "frontend-security-reviewer",
)
BARE_REVIEW_LOOP_RE = re.compile(
    r"review-loop:(?P<agent>" + "|".join(re.escape(n) for n in AGENT_NAMES) + r")"
    r"(?![A-Za-z0-9_\-])"
)


def _extract_value(match: "re.Match[str]") -> str:
    """Return first non-None capture from the primary regex."""
    return (
        match.group("bt")
        or match.group("sq")
        or match.group("dq")
        or match.group("bare")
    )


def scan_line(line: str, line_no: int, hits: dict, sites: list) -> None:
    """Scan a single line; mutate `hits` (counts) and `sites` (anomaly sites).

    Counts contract: unique (value, line_no) pairs per file. Same value on the
    same line counts once. Implements span-overlap dedup so a line like
    `` `subagent_type: review-loop:executor` `` is counted exactly once even
    though both primary and secondary regexes would match it.
    """
    seen_pairs: set = set()
    primary_spans: list = []

    # Primary pass.
    for m in SUBAGENT_TYPE_RE.finditer(line):
        value = _extract_value(m)
        primary_spans.append(m.span())
        key = (value, line_no)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        hits[value] = hits.get(value, 0) + 1
        if value.startswith("review-loop:"):
            sites.append({"value": value, "line": line_no})

    # Secondary pass — skip any match whose span is fully contained in a
    # primary span on the same line.
    for m in BARE_REVIEW_LOOP_RE.finditer(line):
        s_start, s_end = m.span()
        if any(p_start <= s_start and s_end <= p_end for (p_start, p_end) in primary_spans):
            continue
        value = "review-loop:" + m.group("agent")
        key = (value, line_no)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        hits[value] = hits.get(value, 0) + 1
        sites.append({"value": value, "line": line_no})


def scan_file(path: Path) -> dict | None:
    """Scan a single .md file; return a per-file record.

    Returns None and emits one stderr line if the file is unreadable
    (any OSError on read_text or stat — covers PermissionError,
    FileNotFoundError race-vs-glob, IsADirectoryError, transient FS).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError as exc:
        sys.stderr.write(f"replay_sessions: unreadable file: {path}: {exc}\n")
        return None
    hits: dict = {}
    sites: list = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        scan_line(line, line_no, hits, sites)
    anomaly_values = sorted({s["value"] for s in sites})
    return {
        "path": str(path),
        "mtime": mtime,
        "counts": hits,
        "anomaly": bool(sites),
        "anomaly_values": anomaly_values,
        "anomaly_sites": sites,
    }


def build_report(root: Path) -> dict:
    """Walk `root` (single-level glob *.md), build the full report dict."""
    files = []
    files_with_anomaly = 0
    total_anomaly_occurrences = 0
    unreadable = 0
    for path in sorted(root.glob("*.md")):
        record = scan_file(path)
        if record is None:
            unreadable += 1
            continue
        files.append(record)
        if record["anomaly"]:
            files_with_anomaly += 1
            total_anomaly_occurrences += len(record["anomaly_sites"])
    return {
        "files": files,
        "summary": {
            "files_scanned": len(files),
            "files_with_anomaly": files_with_anomaly,
            "total_anomaly_occurrences": total_anomaly_occurrences,
            "unreadable_files": unreadable,
        },
    }


def render_text(report: dict) -> str:
    """Render the report as a human-readable fixed-width table."""
    lines = []
    lines.append("Session-replay parser report")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"{'PATH':<50}  {'ANOM':<5}  COUNTS")
    lines.append("-" * 60)
    for record in report["files"]:
        path_short = record["path"]
        if len(path_short) > 50:
            path_short = "..." + path_short[-47:]
        anom = "YES" if record["anomaly"] else "no"
        counts_str = ", ".join(f"{k}={v}" for k, v in sorted(record["counts"].items()))
        lines.append(f"{path_short:<50}  {anom:<5}  {counts_str}")
        if record["anomaly"]:
            for site in record["anomaly_sites"]:
                lines.append(f"  -> line {site['line']}: {site['value']}")
    lines.append("-" * 60)
    s = report["summary"]
    lines.append(
        f"Summary: scanned={s['files_scanned']}  "
        f"with_anomaly={s['files_with_anomaly']}  "
        f"total_anomaly_occurrences={s['total_anomaly_occurrences']}  "
        f"unreadable={s['unreadable_files']}"
    )
    return "\n".join(lines) + "\n"


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit subagent_type values across .review-loop/sessions/*.md"
    )
    parser.add_argument(
        "--root",
        default=".review-loop/sessions",
        help="Directory to scan (single-level *.md glob). Default: .review-loop/sessions",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Emit a human-readable table instead of JSON.",
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="Always exit 0 even if anomalies are detected.",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        sys.stderr.write(f"replay_sessions: root not found or not a directory: {root}\n")
        return 2

    report = build_report(root)

    if args.text:
        sys.stdout.write(render_text(report))
    else:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")

    if args.exit_zero:
        return 0
    if report["summary"]["unreadable_files"] > 0:
        return 3
    return 1 if report["summary"]["files_with_anomaly"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
