#!/usr/bin/env python3
"""Normalize native reviewer usage and publish immutable invocation records.

Only counters supplied by the runtime are used. See
docs/protocol/usage-accounting.md for scope, resume, and failure semantics.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile


TOKEN_FIELDS = (
    "input_tokens", "uncached_input_tokens", "cached_input_tokens",
    "cache_write_input_tokens", "output_tokens",
)


def _empty_tokens():
    return dict.fromkeys(TOKEN_FIELDS)


def _number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        return None
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return value


def _sum_known(values):
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    total = sum(values)
    return _number(total)


def _token_sum(rows):
    return {field: _sum_known(row[field] for row in rows) for field in TOKEN_FIELDS}


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


class UsageAccumulator:
    """One CLI invocation, fed decoded JSON objects in their original order.

    A resume baseline is a previous summary's ``cumulative_snapshot`` from the
    same native session, captured immediately before this invocation. No baseline
    means resumed cumulative counters cannot be attributed to this invocation.
    """

    def __init__(self, runtime, *, resumed=False, baseline=None):
        if runtime not in ("claude", "codex"):
            raise ValueError("runtime must be claude or codex")
        if baseline is not None and not resumed:
            raise ValueError("baseline requires resumed=True")
        self.runtime = runtime
        self.resumed = bool(resumed)
        self.baseline = baseline
        self.notes = set()
        self.events_seen = 0
        self.session_id = None
        self.actual_model = None
        self.models = set()
        self._snapshot = None
        self._root_usage = None
        self._assistants = {}
        self._incomplete = False
        self._ambiguous = False
        self._activity_after_result = False
        self._result_seen = False
        self._subagents = False

    def _count(self, data, key):
        value = _number(data.get(key), integer=True)
        if key in data and value is None:
            self.notes.add("invalid_token_counter:" + key)
        return value

    def _cost(self, data, key):
        value = _number(data.get(key))
        if key in data and value is None:
            self.notes.add("invalid_cost_counter:" + key)
        return value

    def _claude_tokens(self, data, *, model=False, assistant=False):
        if not isinstance(data, dict):
            return _empty_tokens()
        uncached = self._count(data, "inputTokens" if model else "input_tokens")
        cached = self._count(data, "cacheReadInputTokens" if model else "cache_read_input_tokens")
        written = self._count(data, "cacheCreationInputTokens" if model else "cache_creation_input_tokens")
        # Keep a common definition: all non-cache-read input, including writes.
        uncached_total = _sum_known((uncached, written))
        return {
            "input_tokens": _sum_known((uncached_total, cached)),
            "uncached_input_tokens": uncached_total,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": written,
            # Assistant usage carries a message_start placeholder, not final output.
            "output_tokens": None if assistant else self._count(data, "outputTokens" if model else "output_tokens"),
        }

    def _codex_tokens(self, data):
        if not isinstance(data, dict):
            return _empty_tokens()
        total = self._count(data, "input_tokens")
        cached = self._count(data, "cached_input_tokens")
        uncached = None
        if total is not None and cached is not None:
            if cached > total:
                self.notes.add("cached_input_exceeds_input")
                total = cached = None
            else:
                uncached = total - cached
        written = self._count(data, "cache_write_input_tokens")
        if written is not None and uncached is not None and written > uncached:
            self.notes.add("cache_write_exceeds_uncached_input")
            written = None
        return {
            "input_tokens": total, "uncached_input_tokens": uncached,
            "cached_input_tokens": cached, "cache_write_input_tokens": written,
            "output_tokens": self._count(data, "output_tokens"),
        }

    def _set_session(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            return
        if self.session_id is not None and session_id != self.session_id:
            self._ambiguous = True
            self.notes.add("native_session_changed")
        self.session_id = session_id

    def _set_model(self, model):
        if isinstance(model, str) and model:
            self.actual_model = model
            self.models.add(model)

    def _set_snapshot(self, tokens, cost, source):
        snapshot = {"runtime": self.runtime, "source": source,
                    "session_id": self.session_id, "tokens": tokens, "cost_usd": cost}
        if self._snapshot is not None:
            previous = self._snapshot
            for field in TOKEN_FIELDS:
                before, after = previous["tokens"][field], tokens[field]
                if before is not None and after is not None and after < before:
                    self._ambiguous = True
            if previous["cost_usd"] is not None and cost is not None and cost < previous["cost_usd"]:
                self._ambiguous = True
            if self._ambiguous:
                self.notes.add("cumulative_counter_decreased_or_scope_changed")
        self._snapshot = snapshot

    def consume(self, event):
        """Consume one object; malformed/non-object input becomes an explicit gap."""
        self.events_seen += 1
        if not isinstance(event, dict):
            self.notes.add("invalid_event")
            self._incomplete = True
            return
        if self.runtime == "claude":
            self._consume_claude(event)
        else:
            self._consume_codex(event)

    def _consume_claude(self, event):
        event_type = event.get("type")
        if event.get("parent_tool_use_id") is not None:
            self._subagents = True
            return
        if event_type == "system":
            if event.get("subtype") == "init":
                self._set_session(event.get("session_id"))
                self._set_model(event.get("model"))
            if event.get("subtype") == "conversation_reset":
                self._ambiguous = True
                self.notes.add("conversation_reset_requires_segment_accounting")
            return
        if event_type in ("assistant", "stream_event"):
            self._activity_after_result = self._result_seen
        if event_type == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                return
            self._set_model(message.get("model"))
            message_id = message.get("id")
            if isinstance(message.get("usage"), dict):
                if isinstance(message_id, str) and message_id:
                    self._assistants[message_id] = self._claude_tokens(message["usage"], assistant=True)
                else:
                    self.notes.add("assistant_usage_without_message_id")
                    self._incomplete = True
            return
        if event_type != "result":
            return
        self._set_session(event.get("session_id"))
        self._result_seen = True
        self._activity_after_result = False
        subtype = event.get("subtype")
        if event.get("is_error") is True or (isinstance(subtype, str) and subtype.startswith("error_")):
            self._incomplete = True
            self.notes.add("native_failure_result")
        if event.get("subtype") == "error_during_execution":
            # Native crash results can zero all counters. Keep earlier evidence.
            self._incomplete = True
            self.notes.add("crash_result_counters_not_trusted")
            return
        self._root_usage = self._claude_tokens(event.get("usage"))
        cost = self._cost(event, "total_cost_usd")
        model_usage = event.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            rows, costs = [], []
            for model, counters in model_usage.items():
                if isinstance(model, str) and model:
                    self.models.add(model)
                if not isinstance(counters, dict):
                    self.notes.add("invalid_model_usage")
                    counters = {}
                rows.append(self._claude_tokens(counters, model=True))
                costs.append(self._cost(counters, "costUSD"))
            if "total_cost_usd" not in event:
                cost = _sum_known(costs)
            self._set_snapshot(_token_sum(rows), cost, "claude.modelUsage")
        else:
            # Retain cost's native cumulative scope even without model counters.
            self._set_snapshot(_empty_tokens(), cost, "claude.total_cost_usd")

    def _consume_codex(self, event):
        event_type = event.get("type")
        if event_type == "thread.started":
            self._set_session(event.get("thread_id"))
        elif event_type == "turn.started":
            self._activity_after_result = self._result_seen
        elif event_type == "turn.completed":
            self._result_seen = True
            self._activity_after_result = False
            self._set_snapshot(self._codex_tokens(event.get("usage")), None, "codex.turn.completed")
        elif event_type in ("turn.failed", "error"):
            self._incomplete = True
            self.notes.add("native_failure_event")
        elif event_type in ("item.started", "item.completed"):
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "collab_tool_call":
                self._subagents = True

    def _invocation_delta(self, snapshot, notes):
        if not self.resumed:
            return dict(snapshot["tokens"]), snapshot["cost_usd"]
        baseline = self.baseline
        if not isinstance(baseline, dict):
            notes.add("resume_baseline_missing")
            return _empty_tokens(), None
        if (baseline.get("runtime") != self.runtime
                or baseline.get("source") != snapshot["source"]
                or not snapshot["session_id"]
                or baseline.get("session_id") != snapshot["session_id"]
                or baseline.get("usable") is not True
                or not isinstance(baseline.get("tokens"), dict)):
            notes.add("resume_baseline_scope_mismatch")
            return _empty_tokens(), None
        tokens = _empty_tokens()
        for field in TOKEN_FIELDS:
            before = _number(baseline["tokens"].get(field), integer=True)
            after = snapshot["tokens"][field]
            if before is not None and after is not None:
                if after < before:
                    notes.add("resume_counter_decreased")
                    return _empty_tokens(), None
                tokens[field] = after - before
        before_cost = _number(baseline.get("cost_usd"))
        after_cost = snapshot["cost_usd"]
        cost = None
        if before_cost is not None and after_cost is not None:
            if after_cost < before_cost:
                notes.add("resume_counter_decreased")
                return _empty_tokens(), None
            cost = after_cost - before_cost
        return tokens, cost

    def summary(self):
        """Return content-free accounting; None always means unknown, never zero."""
        notes = set(self.notes)
        tokens, cost = _empty_tokens(), None
        source, scope = "none", "unknown"
        snapshot = None
        if self._snapshot is not None:
            snapshot = dict(self._snapshot, tokens=dict(self._snapshot["tokens"]),
                            usable=not (self._ambiguous or self._incomplete or self._activity_after_result))
            tokens, cost = self._invocation_delta(snapshot, notes)
            source = snapshot["source"]
            scope = "whole_tree" if source == "claude.modelUsage" else "main_agent_thread"
            if source == "claude.total_cost_usd":
                scope = "main_agent_turn"
                # This field is turn scoped, so is safe as a partial fallback.
                if self._root_usage is not None:
                    tokens = dict(self._root_usage)
                source = "claude.result.usage"
                notes.add("main_agent_latest_turn_only")
        elif self._assistants:
            tokens = _token_sum(list(self._assistants.values()))
            source, scope = "claude.assistant.usage", "observed_main_agent_messages"
            notes.add("assistant_output_is_placeholder")
        if self.resumed and self._snapshot is None:
            notes.add("resume_has_no_cumulative_snapshot")
        if self._ambiguous:
            tokens, cost = _empty_tokens(), None
            notes.add("ambiguous_cumulative_scope")
        if self._activity_after_result:
            notes.add("activity_after_last_result")
        if not self._result_seen:
            notes.add("no_terminal_usage")
        if self._subagents and self.runtime == "codex":
            notes.add("codex_subagent_usage_not_reported")
        known = any(value is not None for value in tokens.values()) or cost is not None
        complete = (all(value is not None for value in tokens.values())
                    and source in ("claude.modelUsage", "codex.turn.completed")
                    and not self._incomplete and not self._ambiguous
                    and not self._activity_after_result
                    and not (self.runtime == "codex" and self._subagents))
        return {
            "runtime": self.runtime,
            "accounting_status": "complete" if complete else "partial" if known else "unknown",
            "scope": scope, "source": source, "tokens": tokens, "cost_usd": cost,
            "cost_scope": "whole_tree" if self.runtime == "claude" and cost is not None else None,
            "actual_model": self.actual_model, "models": sorted(self.models),
            "resumed": self.resumed, "native_session_id": self.session_id,
            "events_seen": self.events_seen, "reasons": sorted(notes),
            "cumulative_snapshot": snapshot,
        }


def _artifact(path):
    path = Path(path).resolve()
    result = {"path": str(path), "available": False, "sha256": None, "size_bytes": None}
    try:
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        result.update(available=True, sha256=digest.hexdigest(), size_bytes=size)
    except OSError:
        pass  # A failed launch can legitimately have no raw stream yet.
    return result


def record_invocation(ledger_dir, *, invocation_id, session_id, role, stage, backend,
                      requested_model, status, elapsed_seconds, usage, raw_artifacts,
                      actual_model=None, parent_session_id=None):
    """Atomically create one record. An identical replay is idempotent.

    Concurrent writers cannot replace each other's records. Reusing an ID with
    different metadata raises ValueError; an actual retry needs a fresh ID.
    """
    if not isinstance(invocation_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,179}", invocation_id):
        raise ValueError("invocation_id must be a safe filename component")
    for name, value in (("session_id", session_id), ("role", role), ("stage", stage),
                        ("backend", backend), ("status", status)):
        if not isinstance(value, str) or not value:
            raise ValueError(name + " must be a nonempty string")
    for name, value in (("requested_model", requested_model), ("actual_model", actual_model)):
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(name + " must be a nonempty string or None")
    if parent_session_id is not None and (not isinstance(parent_session_id, str) or not parent_session_id):
        raise ValueError("parent_session_id must be a nonempty string or None")
    if _number(elapsed_seconds) is None:
        raise ValueError("elapsed_seconds must be finite and nonnegative")
    if not isinstance(usage, dict) or usage.get("runtime") not in ("claude", "codex"):
        raise ValueError("usage must be a UsageAccumulator summary")
    usage = json.loads(_json_bytes(usage))
    if status not in ("ok", "success"):
        if usage["accounting_status"] == "complete":
            usage["accounting_status"] = "partial"
        usage["reasons"] = sorted(set(usage["reasons"]) | {"invocation_did_not_succeed"})
        if usage.get("cumulative_snapshot") is not None:
            usage["cumulative_snapshot"]["usable"] = False
    record = {
        "schema_version": 1, "invocation_id": invocation_id, "session_id": session_id,
        "parent_session_id": parent_session_id or session_id,
        "role": role, "stage": stage, "backend": backend,
        "requested_model": requested_model,
        "actual_model": actual_model if actual_model is not None else usage.get("actual_model"),
        "status": status, "elapsed_seconds": elapsed_seconds, "usage": usage,
        "raw_artifacts": [_artifact(path) for path in raw_artifacts],
    }
    payload = _json_bytes(record)
    ledger_dir = Path(ledger_dir)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    destination = ledger_dir / (invocation_id + ".json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".usage-", suffix=".tmp", dir=ledger_dir, delete=False) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != payload:
                raise ValueError("invocation_id already has a different record: " + invocation_id)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def aggregate_records(ledger_dir, *, session_id=None):
    """Sum published records; report partial subtotals separately from totals."""
    records = []
    for path in sorted(Path(ledger_dir).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            usage = record["usage"]
            if (record["schema_version"] != 1 or record["invocation_id"] + ".json" != path.name
                    or usage["accounting_status"] not in ("complete", "partial", "unknown")
                    or _number(record["elapsed_seconds"]) is None):
                raise ValueError("invalid record metadata")
            for field in TOKEN_FIELDS:
                value = usage["tokens"][field]
                if value is not None and _number(value, integer=True) is None:
                    raise ValueError("invalid token counter")
            if usage["cost_usd"] is not None and _number(usage["cost_usd"]) is None:
                raise ValueError("invalid cost counter")
            recorded_session = record["session_id"]
            parent_session = record.get("parent_session_id", recorded_session)
            # Never infer parent ownership from dotted IDs: both session and
            # job IDs may contain dots and concatenated slots can collide.
            if session_id is None or recorded_session == session_id or parent_session == session_id:
                records.append(record)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError("invalid invocation record: " + path.name) from error
    complete = bool(records) and all(row["usage"]["accounting_status"] == "complete" for row in records)
    known_tokens, unknown = {}, {}
    totals = {}
    for field in TOKEN_FIELDS:
        values = [row["usage"]["tokens"][field] for row in records]
        known_tokens[field] = _sum_known(value for value in values if value is not None)
        unknown[field] = sum(value is None for value in values)
        totals[field] = _sum_known(values) if complete else None
    costs = [row["usage"]["cost_usd"] for row in records]
    return {
        "invocations": len(records),
        "status_counts": dict(Counter(row["status"] for row in records)),
        "accounting_counts": dict(Counter(row["usage"]["accounting_status"] for row in records)),
        "scope_counts": dict(Counter(row["usage"]["scope"] for row in records)),
        "tokens": totals, "known_tokens": known_tokens,
        "unknown_token_invocations": unknown,
        "cost_usd": _sum_known(costs) if complete else None,
        "known_cost_usd": _sum_known(cost for cost in costs if cost is not None),
        "unknown_cost_invocations": sum(cost is None for cost in costs),
        "elapsed_seconds": _sum_known(row["elapsed_seconds"] for row in records),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    aggregate = commands.add_parser("aggregate", help="summarize atomic invocation records")
    aggregate.add_argument("--ledger-dir", required=True)
    aggregate.add_argument("--session-id")
    record = commands.add_parser("record", help="account an existing native JSONL stream")
    record.add_argument("--runtime", choices=("claude", "codex"), required=True)
    for option in ("stream", "ledger-dir", "invocation-id", "session-id", "role", "stage", "requested-model", "status"):
        record.add_argument("--" + option, required=True)
    record.add_argument("--elapsed-seconds", type=float, required=True)
    record.add_argument("--actual-model")
    record.add_argument("--raw-artifact", action="append", default=[])
    record.add_argument("--resumed", action="store_true")
    record.add_argument("--baseline", help="JSON containing an earlier cumulative_snapshot")
    args = parser.parse_args(argv)
    try:
        if args.command == "aggregate":
            print(json.dumps(aggregate_records(args.ledger_dir, session_id=args.session_id), sort_keys=True))
        else:
            baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
            accumulator = UsageAccumulator(args.runtime, resumed=args.resumed, baseline=baseline)
            try:
                with Path(args.stream).open(encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        if not line.strip():
                            continue
                        try:
                            accumulator.consume(json.loads(line))
                        except ValueError:
                            accumulator.consume(None)
            except OSError:
                accumulator.notes.add("raw_stream_unavailable")
            path = record_invocation(
                args.ledger_dir, invocation_id=args.invocation_id, session_id=args.session_id,
                role=args.role, stage=args.stage, backend=args.runtime,
                requested_model=args.requested_model, actual_model=args.actual_model,
                status=args.status, elapsed_seconds=args.elapsed_seconds,
                usage=accumulator.summary(), raw_artifacts=[args.stream, *args.raw_artifact],
            )
            print(json.dumps({"record_file": str(path)}))
    except (OSError, ValueError, TypeError) as error:
        parser.exit(2, "usage accounting failed: " + str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
