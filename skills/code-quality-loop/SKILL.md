---
name: code-quality-loop
description: "Iterative review-fix loop on the uncommitted change: reviews, fixes, simplifies and consolidates tests, as a paired-session review-only run (`run --review-only`). The legacy loop was removed in v2.13.0: `entry: legacy`, `--legacy` and `--reorganize` are refused."
argument-hint: "[max-rounds]"
---

# Code Quality Loop

Automated code review cycle: review -> fix -> re-review until clean. Focuses on code-level quality (correctness, style, error handling, tests).
It runs as a paired-session review-only run (Step 0).

## Step 0 — Entry: paired route (the legacy workflow was removed in v2.13.0)

Resolve `entry` in `.review-loop/config.md` (exact values `legacy` and `paired-session`): `legacy` is refused with
`code-quality-loop: the legacy workflow was removed in v2.13.0; remove "entry: legacy" from .review-loop/config.md (paired-session is the only entry)`; an invalid value prints
`code-quality-loop: entry "<v>" is not valid (paired-session is the only entry); using paired-session`, a duplicate key or
an unreadable config prints `code-quality-loop: entry could not be read (<reason>); using paired-session`, and both
continue as absent. Then:
- `--legacy` in `$ARGUMENTS`: refused with `code-quality-loop: the legacy workflow was removed in v2.13.0; run /review-loop:code-quality-loop without --legacy`.
- A host that is not macOS (`uname -s` is not `Darwin`): refused with `code-quality-loop: paired-session needs macOS; Linux and other hosts are not supported`.
- Otherwise the paired route. This skill owns the notice lines of steps 1 and 3: print each verbatim as visible reply text (not only in a tool call), as the first output after resolving `entry` and before any other tool call or skill invocation, also on a headless run. The paired-session skill prints them only when they are missing.
  1. Print `code-quality-loop: paired-session review-only run (entry set in .review-loop/config.md)` when the
     key is set, or
     `code-quality-loop: paired-session review-only run (the default entry)`
     when it is absent.
  2. Map the arguments; a refused one stops here with its line, before any handoff:
     - `[max-rounds]` N: an integer of at least 2 becomes `--max-exec-rounds N` (round 1 reviews the existing
       change; the cap ends in a HOLD) and wins over `soft_limit_exec`; absent, the paired-session skill's usual
       mapping applies (`soft_limit_exec`, else the coordinator default). Anything else is refused:
       `code-quality-loop: max-rounds must be an integer of at least 2 on the paired route (round 1 reviews the existing change)`.
     - `--skip-reorganize`: print `code-quality-loop: --skip-reorganize has no effect; the paired route never reorganizes`.
     - `--reorganize`: refused,
       `code-quality-loop: reorganize is not part of the paired route; run /review-loop:reorganize <files> after the run`.
     - Any other argument: refused,
       `code-quality-loop: unknown argument <arg>; usage: /review-loop:code-quality-loop [max-rounds]`.
     - `auto_commit: false` in `.review-loop/config.md`: hand over `--auto-commit false` (absent or `true`: the
       review-only default, one local commit at `accept`).
     - `judgment_model` or `cheap_model` set in `.review-loop/config.md`: print, per key,
       `code-quality-loop: <key> in .review-loop/config.md is not applied by paired-session; models come from the operator profile`.
       (`review_focus`, `review_style` and `quality_focus` are passed to the run by the paired-session skill.)
  3. Print
     `code-quality-loop: the paired route reviews, fixes, simplifies and consolidates tests, and accept makes one local commit (never a push; `auto_commit: false` in .review-loop/config.md keeps it uncommitted); it does not reorganize, run static-analysis artifacts, load a design document or sweep project docs`.
  4. Invoke the `paired-session` skill with `--code-quality-loop` and the mapped `--max-exec-rounds N` and
     `--auto-commit false` (if any), and end this skill. It follows the shared contract's Code-quality-loop entry: a review-only run on
     the uncommitted change with the quality writers on, ending at DONE (or HOLD); `accept` writes the delivery report.
  5. If the paired-session skill reports `stage A failure: <reason>`, report
     `code-quality-loop: paired-session entry refused (<reason>)` and stop (nothing falls back).
