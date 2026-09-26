# Reviewer invocation usage

`scripts/reviewer_usage.py` accounts for one native CLI process at a time. It
uses reported counters, never a prompt-length estimate or a price table. An
unsuccessful invocation still gets a record, even when every usage field is
unknown. This ledger measures reported usage, not subscription quota or billing.

## Integration

```python
from reviewer_usage import UsageAccumulator, record_invocation

usage = UsageAccumulator(runtime="claude")  # or "codex"
for event in decoded_native_events:
    usage.consume(event)

# Close raw files after process teardown, including timeout/cancellation paths.
record_path = record_invocation(
    ledger_dir, invocation_id=unique_invocation_id, session_id=review_session_id,
    role="reviewer", stage="code-review", backend="claude",
    requested_model="sonnet", status=final_status, elapsed_seconds=elapsed,
    usage=usage.summary(), raw_artifacts=[stream_path, stderr_path, prompt_path],
)
```

`actual_model` is optional on `record_invocation`. Otherwise it comes only from
native root-model metadata; the requested model is not proof of the model used.
Either model can be `None` when unavailable. `models` lists model names observed
in Claude metadata, which can include historical models in a resumed snapshot.
The accumulator ignores content and retains no prompts, responses, or errors.
Call `consume(None)` for an undecodable event to preserve an accounting gap.

The caller must retain separate raw artifacts for every invocation. The record
stores absolute paths, SHA-256 digests, byte counts, and availability; it neither
copies nor deletes artifacts. Missing files are explicit, including failed
launches. Reusing a mutable stream path destroys evidence even if a ledger
record still exists. Do not record until all writers have closed the raw files.

## Counter definitions

All token values are nonnegative integers or `null`. Negative, fractional,
boolean, string, and nonfinite token values are rejected without coercion.
Missing fields remain `null`; a reported zero remains zero. Cost accepts finite,
nonnegative numbers only, and remains `null` unless the runtime supplies it.

| Normalized field | Meaning |
| --- | --- |
| `input_tokens` | All input, including cache reads and writes |
| `uncached_input_tokens` | All input except cache reads; includes cache writes |
| `cached_input_tokens` | Input read from cache |
| `cache_write_input_tokens` | Input written to cache, a subset of uncached input |
| `output_tokens` | Reported output; reasoning is not added again |

Thus `input_tokens = uncached_input_tokens + cached_input_tokens`. Do not add
cache writes to that total again. For Claude, normalized uncached input is the
native `input_tokens + cache_creation_input_tokens`; its native input field
alone excludes cache activity. For Codex it is native input minus cached input.
Costs from multiple models are added only when a root cost is absent.

## Native event scope

Claude precedence is the latest root `modelUsage` snapshot, then the latest
root `usage` as a partial main-agent-turn observation, then deduplicated
assistant input by `message.id`. Child events and partial stream counters are
never added to an aggregate. Assistant output is unknown because native
assistant messages carry an initial placeholder. A crash result can zero its
counters, so earlier observations survive as partial evidence. These distinctions
follow the [Claude SDK cost and usage documentation](https://code.claude.com/docs/en/agent-sdk/cost-tracking).

Codex uses the latest `turn.completed.usage`, without summing successive
snapshots. In the installed CLI version, its emitter reads thread cumulative
totals despite the event type's per-turn description: see
[the 0.155.1 emitter](https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/exec/src/event_processor_with_jsonl_output.rs)
and [event schema](https://github.com/openai/codex/blob/rust-v0.155.1/codex-rs/exec/src/exec_events.rs).
Codex exec supplies neither a price nor an actual-model field here. Collaboration
items do not provide a safe whole-tree total; their presence marks root totals
partial. Tool payloads and rollout-history counters are not parsed as exec usage.

`accounting_status` is `complete`, `partial`, or `unknown`. Complete means all
normalized token fields are reported for the selected native scope, with no
observed gap. Optional cost can still be unknown. `scope` describes tokens;
`cost_scope` separately identifies Claude's whole-tree cost. A failure, timeout,
or cancellation record downgrades complete accounting to partial and retains
all observed numbers. New activity after the last result is also partial.

## Resume and resets

For a resumed CLI process, use
`UsageAccumulator(runtime, resumed=True, baseline=previous_cumulative_snapshot)`.
The baseline must be the immediately preceding reliable snapshot of the same
native session, source, and runtime, taken before the invocation. Supply the
`cumulative_snapshot` object, not its enclosing ledger record. The helper
subtracts it field by field; missing baseline counters stay unknown. Never use
the previous invocation's already-subtracted token totals as a baseline.

Claude 2.1.277 and later restore cumulative spend on resume, while older releases
used different behavior. The default deliberately assumes the current cumulative
contract. Without a matching baseline, resumed cumulative tokens and cost are
unknown, though a latest root-turn fallback can still be a partial observation.
The original cumulative snapshot remains visible separately for auditing.

Decreasing cumulative counters, a native session change, or a conversation reset
make attribution unknown. This helper does not guess how to splice reset
segments. Split them into separately proven segments outside the helper. A
snapshot from a failed/incomplete invocation is marked unusable as a baseline;
recover an authoritative pre-invocation snapshot before accounting a resume.

## Atomic records and reporting

The ledger is a directory of `<invocation_id>.json` files, not a shared append
buffer. A flushed temporary file is published through an atomic, non-replacing
hard link. Readers see a complete record, and concurrent writers cannot lose
each other's entries. Use a local filesystem that supports hard links.
Replaying the same ID with identical evidence/metadata is idempotent. Reusing
it for changed data raises `ValueError`; each actual retry needs a new ID.

```sh
python3 scripts/reviewer_usage.py aggregate --ledger-dir .review-loop/tmp/usage
python3 scripts/reviewer_usage.py aggregate --ledger-dir .review-loop/tmp/usage --session-id SESSION
```

`--session-id` matches an explicit `parent_session_id` recorded for parallel
jobs, or the exact session ID for standalone invocations. It does not parse a
dotted filename slot, because session and job IDs may contain dots. `aggregate_records(ledger_dir, session_id=None)`
provides the same data in Python.
`tokens` and `cost_usd` are totals only when all relevant records are complete
and their respective fields are known. `known_tokens` and `known_cost_usd` are
observed subtotals; always display their accounting/unknown counts alongside
them. Missing usage is never silently turned into zero. Elapsed time is the sum
of invocation durations, not wall-clock time for concurrent work. An aggregation
sees published records present during its directory scan; repeat it after all
jobs finish for the final report. Corrupt published records fail aggregation.

For an existing Claude or Codex JSONL artifact, `record --help` lists the offline
recorder arguments. It accepts runtime/session/role/stage/status/model metadata,
elapsed seconds, additional raw artifact paths, and an optional resume baseline.
It does not launch a model. Tests use synthetic values in native event shapes
verified against Claude Code 2.1.278 documentation and Codex CLI 0.155.1 sources;
no private history or live paid run is required.
