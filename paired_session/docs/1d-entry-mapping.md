# 1D entry mapping: `/review-loop` to paired-session (design, v2.9.0)

Design only; nothing here is implemented yet. Sources: `skills/review-loop`, `plan`, `execute`, `review-pr`, `paired-session`, `bin/paired-session --help`, `review-loop-config.example.md`, BACKLOG 1D/1E, `coordinator.py` argparse.

## Decisions
- **S1 switch.** v2.9.0 default stays legacy. Opt-in key `entry: paired-session` in `.review-loop/config.md` routes only fresh `/review-loop <work item>` (Step 1.5 "No prior state") to the paired-session skill. Plan-exists, code-exists (including a dirty tree detected as implemented code) and existing-session detection stay legacy and print `review-loop: entry is paired-session but <state> detected; using legacy workflow`. Default flips at v2.10.0 after 3 stable real runs (gate "3 runs", not the M4 activation A5). Fail-closed: only the exact values `legacy` and `paired-session` are accepted; any other value (quoted or differently cased included) uses legacy and prints `review-loop: entry "<v>" is not valid (legacy|paired-session); using legacy workflow`. A duplicate `entry` key or unreadable config prints `review-loop: entry could not be read (<reason>); using legacy workflow`. A paired-session probe/run HOLD is reported and never falls back to legacy. The key is workspace-committed, so the experimental line (S5) names the file so a cloner sees why they are routed.
- **S2 commands.** `/review-loop:paired-session` stays the explicit new entry. New explicit legacy control `/review-loop:legacy <work item>` ignores `entry`. Existing skill names (`code-quality-loop`, `execute`, `guide`, `paired-session`, `plan`, `reorganize`, `review-loop`, `review-pr`) do not clash, and `legacy` says what it runs. The slash command is Claude-only. The Codex runtime honors `entry` with the same fail-closed routing (exact `paired-session` plus fresh work hands off to `.agents/skills/paired-session`; plan-exists, code-exists and resume stay legacy) and has no slash commands, so its explicit legacy control is the natural-language request "use the legacy review-loop workflow"; its implicit-entry notice names that phrase instead of the slash command.
- **S4 notice.** Implicit `/review-loop` reaching legacy (no `entry` key) prints one line: `review-loop: legacy workflow via implicit entry; set "entry: paired-session" in .review-loop/config.md to opt in, or use /review-loop:legacy explicitly`. Not printed for `entry: legacy`, `/review-loop:legacy`, `plan`, `execute` or `review-pr`. Nothing is removed in v2.9.0.
- **S5 gates.** Real routing needs all of: 1C closed (consolidated independent safety review), M4 fake PLAN-to-close E2E passing (lane A), and M6. Until then the switch may exist but is experimental, and routed runs print `review-loop: paired-session entry is experimental (entry set in .review-loop/config.md)`.

## Entry modes
| Legacy mode | Under `entry: paired-session` |
|---|---|
| Fresh work (`/review-loop <item>`) | `paired-session run` via the skill (probe first). Start prints author, reviewer and gate vendor+model and effective round limits. |
| Plan-only (`/review-loop:plan`) | Stays legacy. Paired plan-only is `/review-loop:paired-session <item> --plan-only` (`run --stop-after-plan`). |
| Execute approved plan (`execute --plan` / `--session`) | Not mapped in v2.9.0; stays legacy. Warning: `review-loop: legacy plans/sessions cannot be imported into paired-session; using legacy workflow`. |
| Explicit resume (existing legacy session) | Always legacy. A paired run resumes only via `paired-session resume` with the original `--workspace`, `--workitem`, `--run-dir`, profile and options; the two never cross. |
| Review-only / `review-pr` | Not mapped in v2.9.0; both stay legacy. Same warning form. |

## Config keys
Set-but-unmapped key: `review-loop: config key <key> is not mapped in v2.9.0 paired-session entry; ignored (/review-loop:legacy honors it)`; warn only when the value differs from the legacy default. Converted keys become explicit CLI options and override the `paired-session.json` or `--config` profile; the start line shows the result.
| Key | paired-session |
|---|---|
| `reviewer_model`, `executor_model` | `--reviewer-model`, `--author-model`. Role models are operator-set (ADR-9): any well-formed model id is accepted, checked against `allowed_models` when that key is set; `""`/`inherit` pass no flag, so the vendor default applies (Claude `claude-opus-5-5`, Codex `gpt-6-luna`). The gate vendor defaults to the author's (ADR-10). An invalid or disallowed id is refused by the coordinator, never substituted. The mapping must also refuse a model id of the other vendor (as ADR-10 M4 does for the gate): the legacy `reviewer_model` targets a Codex reviewer, the paired-session reviewer defaults to Claude. |
| `reviewer`, `judgment_model`, `cheap_model`, `codex_*` | Not mapped. Roles come from the profile (`--config`) and default to author Codex, reviewer Claude, the reverse of legacy (Claude executor, Codex reviewer); the start line shows it. |
| `soft_limit_plan`, `soft_limit_exec` | `--max-plan-rounds`, `--max-exec-rounds`. Cap is hard (HOLD), not a prompt. Unset defaults are plan 3, exec 4 (legacy 3/3). |
| `skip_quality_polish` | Not mapped: the CLI flag is recorded but not read; polish still runs. Warn. Only `--polish-round off` skips it (not derived). |
| `adversarial_gate_skip_paths` | Not mapped: `--skip-globs` is recorded but not read; legacy defaults (`**/SKILL.md` etc.) do not apply. Warn. |
| `docs_file` | `--docs-file`; inert until `lifecycle_mode=on` (M4, refused today), so no CHANGELOG is written. Warn even for the legacy default `CHANGELOG.md`. |
| `auto_commit`, `commit_message_prefix` | Not mapped; with `lifecycle_mode=off` no commit, merge or push happens (DONE is not acceptance). Warn when `auto_commit: true`. |
| `handsfree` (also the flag), `review_focus`, `review_style`, `quality_focus`, `context_persist_threshold` | Not mapped. |
