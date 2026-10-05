# Protocol loading: special cases

Read on demand, per `loading.md`, when one of these cases applies.

## Framework self-modification

For framework self-modification, the controlling invocation uses an explicitly
frozen support revision; candidate text is tested separately. Never hot-switch
its rules or let a reviewing agent start another orchestration workflow.

## Loading without a shell

If shell loading is unavailable, read the exact sources/sections and prerequisite
units listed in `loading.json` for the current action. This has the same contract;
do not silently omit a prerequisite. Whole-file reads are permitted when a
client cannot select sections, but report their full cost in measurements.
