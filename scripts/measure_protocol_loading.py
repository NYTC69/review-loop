#!/usr/bin/env python3
"""Reproducible static instruction-byte comparison; never claims live savings.

Baseline imports come from v2.8.0's actual entry contracts. After sizes use the
production loader, including its fingerprints, wrapper and loading contract.
Stage sequences exercise the same-context reuse rule. They are inventory
scenarios, not observed agent decisions, API tokens, or elapsed-time benchmarks.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from read_protocol import ROOT, resolve

BASE = "cadb06c11ba73cef356b6d62f39f4fb96c662a84"
CASES = {
    "planning-only": ("plan", ["planning", "planning-review", "plan-exit"]),
    "one-file-delivery": ("execute", ["execution", "execution-review", "gate", "polish", "docs", "security", "delivery"]),
    "cross-file-repair": ("execute", ["execution", "execution-review", "execution", "execution-review", "gate", "polish", "docs", "security", "delivery"]),
    "stop-resume": ("execute", ["execution", "execution-review", "gate", "stop", "NEW_CONTEXT", "resume", "polish", "docs", "security", "delivery"]),
    "release-delivery": ("review-loop", ["planning", "planning-review", "execution", "execution-review", "gate", "polish", "docs", "security", "delivery"]),
}


def old(root, path):
    return subprocess.check_output(["git", "show", f"{BASE}:{path}"], cwd=root)


def scenario(root, runtime, name, mode, actions):
    prefix = "skills" if runtime == "claude" else ".agents/skills"
    wrapper = f"{prefix}/{mode}/SKILL.md"
    protocols = ["session-file", "executor-output", "reviewer-output"]
    protocols += ["planning"] if mode == "plan" else ["execution"] if mode == "execute" else ["planning", "execution"]
    before_start = sum(len(old(root, path)) for path in [wrapper] + [f"docs/protocol/{p}.md" for p in protocols])
    # Old execute crosses into planning dispatch; count that load in total, not
    # startup unless the actual pre-dispatch path needs it. This conservative
    # startup denominator does not inflate baseline to manufacture savings.
    before_total = before_start + (len(old(root, "docs/protocol/planning.md")) if mode == "execute" else 0)
    seen = set()
    after_total = 0
    emitted_units = []
    context_entry = len((root / wrapper).read_bytes()) + len((root / "docs/protocol/loading.md").read_bytes())
    after_total += context_entry
    trace = []
    sequence = [f"entry-{mode}", "session-init", f"{mode}-init"] + actions
    if mode == "review-loop" and runtime == "codex":
        sequence.insert(3, "plan-init")
    startup = None
    for action in sequence:
        if action == "NEW_CONTEXT":
            seen.clear()
            after_total += context_entry
            before_total += before_start
            trace.append({"action": action, "new_context_instruction_bytes": context_entry})
            continue
        units = resolve(root, runtime, action)
        fresh, reused = [], []
        action_bytes = 0
        for unit in units:
            fp = unit["unit"] + "@" + unit["sha256"]
            if fp in seen:
                reused.append(unit["unit"])
                continue
            seen.add(fp)
            fresh.append(unit["unit"])
            emitted_units.append(unit["unit"])
            action_bytes += len((f"<!-- {fp}; source: {unit['path']} -->\n" + unit["body"] + "\n").encode())
        after_total += action_bytes
        trace.append({"action": action, "emitted": fresh, "reused_in_context": reused, "bytes": action_bytes})
        if startup is None and action in ("planning", "execution"):
            startup = after_total
    return {"runtime": runtime, "case": name, "measurement": "static inventory, not live",
            "baseline_sha": BASE, "before_startup_bytes": before_start,
            "after_startup_bytes": startup,
            "startup_reduction_percent": round(100 * (1 - startup / before_start), 2),
            "before_full_path_bytes": before_total, "after_full_path_bytes": after_total,
            "full_path_reduction_percent": round(100 * (1 - after_total / before_total), 2),
            "trace": trace, "live_rule_bytes": "unverified", "elapsed_s": "unavailable",
            "model_tokens": "unavailable", "cost_usd": "unavailable"}


def report(rows):
    lines = ["# Protocol loading: v2.8.0 vs candidate", "",
             "Static UTF-8 instruction-byte inventory from actual baseline files and the production loader.",
             "Counts include entry/loading text and emitted fingerprint headers. Same-context unchanged units",
             "are delivered once; resume counts fresh context. This is NOT measured model usage or native-runtime execution.",
             "Agent bodies and task packets are unchanged; their repeated prompt delivery is excluded from BOTH",
             "columns here. It must be counted in the separate live >=25% rule-input acceptance test.", "",
             "| Runtime | Case | Startup before → after (bytes) | Reduction | Full path before → after | Reduction |",
             "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['runtime']} | {row['case']} | {row['before_startup_bytes']} → {row['after_startup_bytes']} | {row['startup_reduction_percent']}% | {row['before_full_path_bytes']} → {row['after_full_path_bytes']} | {row['full_path_reduction_percent']}% |")
    lines += ["", "## Observation limits", "",
              "- Live small-task rule-input reduction >=25%: unverified until an observed before/after workflow is captured.",
              "- Native Claude runs: not invoked in this session (user restriction).",
              "- Simulated reading exercises and static inventory cannot be presented as live workflow savings.",
              "- Existing P1-P4 evaluation outcomes are a separate regression check; no old metrics are silently redefined.",
              "- Wall time, model/cache tokens and cost: unavailable in this deterministic report.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = [scenario(args.root, runtime, name, mode, actions)
            for runtime in ("claude", "codex") for name, (mode, actions) in CASES.items()]
    print(json.dumps(rows, indent=2) if args.json else report(rows))


if __name__ == "__main__":
    main()
