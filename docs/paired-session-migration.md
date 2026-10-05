# Migrating from `/review-loop` to paired-session (v2.10.0 default entry)

## What paired-session is
paired-session is a coordinator (`bin/paired-session`) that runs one author and one reviewer through PLAN and EXEC, applies fresh shadow/adversarial checks, and then, with `lifecycle_mode=on`, runs finish, quality polish, docs and security turns before DONE (acceptance pending). It owns isolated run artifacts outside the workspace, invocation limits and a permission probe. See [`paired_session/README.md`](../paired_session/README.md); the entry-switch design is [`paired_session/docs/v2.10-entry-switch.md`](../paired_session/docs/v2.10-entry-switch.md).

## Status in v2.10.0
- **paired-session is the default entry.** A fresh `/review-loop <work item>` (Claude) or a fresh review-loop request (Codex) with no `entry` key in `.review-loop/config.md` hands off to the `paired-session` skill, which runs the coordinator with `--lifecycle-mode on`.
- A request to review code that already exists (code-exists) hands off as `run --review-only` (D-LG1): no PLAN phase; the EXEC review of the change against `HEAD`, or `--base <ref>` when you name a base, is round 1. Unrelated dirty work is not a code-exists signal.
- Plan-exists and existing-session states stay legacy, as do `/review-loop:plan`, `execute` and `review-pr`. Legacy `/review-loop:execute --review-only` stays legacy until its retirement; its paired-session equivalent is `run --review-only`, and its `--stop-after exec-round` maps to the operator CLI options `--max-exec-rounds 1 --lifecycle-mode off --adversarial-gate off` (not a skill route).
- Nothing is removed: the legacy workflow stays available through `entry: legacy`, `/review-loop:legacy`, or (Codex) "use the legacy review-loop workflow".
- New runs are `efficient` by default and need no permission probe (see Safety modes). In strict mode the probe PASS is bound to the plugin version: after upgrading, a strict run directory needs a new `permission-probe`. Finish a run with the version that started it. If a v2.9.x run is nevertheless continued under v2.10.0, it resumes strict, needs a new probe, keeps `lifecycle_mode=off` and ends at DONE.

## Status in v2.9.x (for reference)
In v2.9.x the legacy workflow was the default and `entry: paired-session` was an experimental opt-in; without the key `/review-loop` printed a one-line implicit-entry notice.

## Default and opt-out
To keep the legacy workflow, add to `.review-loop/config.md` (the key is documented in `review-loop-config.example.md`):

```
entry: legacy
```

`entry: legacy` is also valid on v2.9.x, so you can set it before upgrading. The key is workspace-committed, so anyone who clones the repository is routed the same way. Other ways to the legacy workflow:
- Claude: `/review-loop:legacy <work item>` runs the legacy workflow and ignores `entry` (it does not read or validate it and prints none of its notices).
- Codex has no slash commands. Ask in natural language: "use the legacy review-loop workflow".
- `/review-loop:plan`, `execute` and `review-pr` stay legacy and are not affected by `entry`.

`entry: paired-session` routes the same way as the missing key but prints the explicit-entry notice; a failed check before the coordinator starts then refuses instead of falling back (unavailable background or outside-sandbox execution is reported as HOLD). The explicit entry `/review-loop:paired-session <work item>` (Claude) remains available and, like the default entry, runs with `--lifecycle-mode on`. `--plan-only` on it maps to `run --stop-after-plan`.

## What is and is not routed
| Situation | Result |
|---|---|
| Fresh work item, `entry` absent or `paired-session` | paired-session |
| `entry: legacy`, an invalid value, or an unreadable config | legacy (the invalid and unreadable cases print a warning) |
| Plan already exists | legacy |
| Code already implemented (task-relevant changes; unrelated dirty work does not count) | paired-session `run --review-only` |
| Existing legacy session / explicit resume | legacy |

Before the handoff the entry checks the host (macOS, key absent only), the CLIs the roles need, background or outside-sandbox execution and the Codex home; the paired-session skill then establishes a dedicated worktree and a test command, asking only when it cannot. With the key absent, a failed check before the first `bin/paired-session` command falls back to legacy with a notice; with `entry: paired-session` it refuses. From the first `bin/paired-session` command on, a refusal or HOLD is reported and never falls back to legacy. Legacy sessions and plans cannot be imported into paired-session. A paired run is resumed only with `paired-session resume` and its original options; the two workflows never cross.

## Notices and warnings you will see
Printed by the `/review-loop` skill text (Claude wording; Codex names "the legacy review-loop workflow" instead of the slash command):
- No `entry` key: `review-loop: paired-session is the default entry; set "entry: legacy" in .review-loop/config.md or use /review-loop:legacy for the legacy workflow`
- `entry: paired-session`: `review-loop: paired-session entry (entry set in .review-loop/config.md)`
- Value other than exactly `legacy` or `paired-session` (quoted or differently cased included): `review-loop: entry "<v>" is not valid (legacy|paired-session); using legacy workflow`
- Duplicate `entry` key or unreadable config: `review-loop: entry could not be read (<reason>); using legacy workflow`
- A plan or a session already exists: `review-loop: entry is paired-session but <plan exists|existing session> detected; using legacy workflow`
- A failed check before the coordinator starts, key absent: `review-loop: paired-session default entry ...; using legacy workflow` (the reason names the host, the missing CLI, the unanswered question or the unavailable execution)
- A failed check before the coordinator starts, `entry: paired-session`: `review-loop: paired-session entry refused (<reason>); set "entry: legacy" or use /review-loop:legacy`

## Author and reviewer roles are inverted
Legacy: Claude executes, Codex reviews. paired-session defaults to the reverse: Codex is the author and Claude is the reviewer. Roles come from an operator-owned profile outside the workspace: the one you name, otherwise `~/.config/review-loop/paired-session.json` when it exists (it then replaces `.review-loop/paired-session.json`); not from `reviewer` or `executor_model`; without one the defaults above apply. `.review-loop/paired-session.json` may hold non-program limits only (role/vendor/program keys there are refused), and `--config` replaces it rather than layering. Models are operator-set (ADR-9); a role without one gets its vendor's default (Claude `claude-opus-5-5`, Codex `gpt-6.1-sol`, ADR-12), and the Step 3.4 gate defaults to the author's vendor (ADR-10), so the default gate is Codex.

## Safety modes
New runs are `efficient` by default ([`paired_session/docs/efficient-mode.md`](../paired_session/docs/efficient-mode.md)): every sandbox and the secret and global-config checks apply, but `run` needs no permission-probe PASS, the `--accept-*` waivers are unnecessary (a NOTE says so) and the evidence guard only logs. `--strict`, or `"safety_mode": "strict"` in the operator profile, restores the probe PASS gate, the Claude-author opt-in and the holding evidence guard, and a strict lifecycle run refuses `--accept-unverified-claude-author` and `--accept-probe-skip` (D-7). The mode is fixed when the run is created; a run started on v2.9.x resumes strict. Two HOLDs apply in both modes: a reviewer, gate or shadow turn that changes the workspace is void (the workspace is restored and the turn re-dispatched once; a second change or a failed restore is a HOLD), and an author turn that changes HEAD or the branch (a commit, reset or checkout) is a HOLD.

## Config mapping
Set legacy keys map to one-run paired-session options: `docs_file` → `--docs-file`, `skip_quality_polish` → `--skip-quality-polish`, `soft_limit_plan` / `soft_limit_exec` → `--max-plan-rounds` / `--max-exec-rounds`; the start line shows the effective values and which came from `.review-loop/config.md`. `auto_commit: true` in `.review-loop/config.md` is not applied (set `auto_commit` in the operator profile; with it, `accept` makes one local commit of the accepted tree) and prints a warning; `reviewer_model` / `executor_model` print a warning when set to anything other than empty or `inherit` (models come from the profile). Other legacy keys (`reviewer`, `review_focus`, `review_style`, `quality_focus`, ...) are not mapped; `handsfree` only means that the paired-session skill cannot ask its stage A questions, and it never runs `accept` or `reject`. paired-session caps are hard (a HOLD), not a prompt; unset defaults are plan 3 / exec 4 rounds (legacy 3/3).

## Known gaps
Details and evidence: [`paired_session/docs/1c-safety-controls.md`](../paired_session/docs/1c-safety-controls.md) and ADR-11 in `DECISIONS.md` (added with the lifecycle in the same release).
- **1C remains an inventory, not a closure.** Owner decision E-12 (2026-10-04) means it no longer gates the default entry; it does not mean these gaps are fixed. The lifecycle runs with a sandboxed author, fresh reviewers and the adversarial gate on; a probe PASS or PASS_RESIDUAL_RISK is required only in strict mode.
- Several controls (for example the Codex author sandbox and the hook/credential flags) are verified only by offline tests of generated flags; their equivalence to a real `codex exec` is unverified.
- A headless `claude -p` session cannot drive a run the usual way: the skill starts `run` in the background and ends its turn to wait, and `-p` then exits and kills the background run mid-turn. Use an interactive session. If you must run headless, say so in the prompt, so the agent keeps its turn open and polls the run until its command has exited. A run cut off this way is recovered with `--retry-uncertain` (`resume`, or `permission-probe` for a cut-off probe); the agent checks the cut-off turn and asks you first.
- The per-round change detection cannot see gitignored files or `.git`, ignored files are not inventoried on real runs, and Git hooks are neither run nor blocked.
- `accept` and `reject` have no operator authentication. DONE is not acceptance: the agent runs `accept` or `reject` only on your explicit decision in the conversation.
- The candidate-tree isolation track (separate candidate checkouts and coordinator-run test sandboxes) remains later hardening, not part of v2.10.0. There is no Compass BACKLOG close stage (owner decision D-3).
- External delivery (push, PR, merge) is not available; with `auto_commit` on, acceptance makes one local commit only.
- Installed Codex writes a trust entry into `~/.codex/config.toml` (outside the worktree); the coordinator attributes and reports it but does not revert it.
- Concurrent runs need one absolute `CODEX_HOME` each, and concurrent permission probes can fail each other; see [`paired_session/docs/concurrent-runs.md`](../paired_session/docs/concurrent-runs.md).
