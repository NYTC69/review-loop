# Protocol loading

`loading.json` names the authoritative sources and prerequisites for each
runtime/action. `scripts/read_protocol.py` emits their exact text; inventory
output alone is not a read. Resolve support paths against this plugin/repository,
but keep the user's workspace as cwd for git, config, session and task actions.

Read the entry skill and this contract, then load `entry-plan`, `entry-execute`
or `entry-review-loop` before interpreting flags or touching session state:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime <claude|codex> --stage <stage>
```

Read the complete emitted bundle before its action. Missing source, ambiguous
section, cyclic prerequisites or truncated output blocks that action; do not
guess a rule. Follow scoped section references when a claim needs further
detail. Cross-reference/audit links are not instructions to preload every file.

| Next action | Load before acting |
|---|---|
| Initialize a fresh session | `session-init` (schema, lock, dirty baseline, snapshot, packet, timing) |
| Resume / accept drift / backfill / batch start | `resume` |
| Draft/revise a plan via Executor | `planning` |
| Prepare Reviewer request / handle an Executor question | matching review bundle before dispatching Reviewer |
| Review a plan / parse verdict / decide plan loop or exit | `planning-review` |
| Choose author / implement / collect execution result | `execution` |
| Review implementation / parse verdict / decide execution loop | `execution-review` |
| Terminal adversarial gate, including revalidation | `gate` |
| Quality polish | `polish` |
| Documentation consistency | `docs` |
| Security preflight | `security` |
| Deliver, or stop/abort and update baseline/release lock | `delivery` / `stop` |
| Codex orchestrator's Claude-CLI reviewer fan-out N>1 | `parallel-review` in addition to current review stage; not applicable to the Claude orchestrator or local Codex reviewer |
| Optional context-persist step becomes applicable | `context-persist` |
| Dispute a finding or record/concur/revalidate triage state | `dispute` before invoking the state-writing triage command |

On planning APPROVE, load `plan-exit` ONLY for the plan-only entry.
The umbrella promotes the plan per the shared planning loop, keeps its lock
and session, then loads execution; it must not run the plan-only exit procedure.

The lifecycle and all `--stop-after` boundaries remain unchanged. Stage loading
does not authorize a stage, a write, an external action, or a skip. Read-only
and plan-only scope still govern. A fresh independent Reviewer receives its
agent body, current packet, exact target, output contract and review-only
constraint; it does not inherit the orchestrator's full protocol corpus.

Within ONE live context, reuse a unit only while its full text is still available
and its source content is unchanged. Pass its emitted `UNIT@SHA256` fingerprint
with `--loaded` to avoid duplicate delivery. A changed fingerprint reloads it.
Do not persist fingerprints as proof of model memory: after compaction, process
restart, resume in a new context, or agent change, load required units afresh.
The session/ledger binds task evidence, not instruction availability. Every
work/review/stage bundle includes the schema, lock and packet prerequisites
needed by its actions, so a fresh context loads its full closure. Full evidence
record/derivation rules load before execution or a state-writing dispute;
planning's read-only rubric check needs only its output/retry rules.

For framework self-modification, the controlling invocation uses an explicitly
frozen support revision; candidate text is tested separately. Never hot-switch
its rules or let a reviewing agent start another orchestration workflow.

If shell loading is unavailable, read the exact sources/sections and prerequisite
units listed in `loading.json` for the current action. This has the same contract;
do not silently omit a prerequisite. Whole-file reads are permitted when a
client cannot select sections, but report their full cost in measurements.
