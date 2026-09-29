# Migrating from `/review-loop` to paired-session (EXPERIMENTAL, v2.9.0)

## What paired-session is
paired-session is a coordinator (`bin/paired-session`) that runs one author and one reviewer through PLAN and EXEC, then applies fresh shadow/adversarial checks and stops at DONE (acceptance pending). It owns isolated run artifacts outside the workspace, invocation limits and a permission probe. See [`paired_session/README.md`](../paired_session/README.md).

## Status in v2.9.0
- **EXPERIMENTAL. The legacy workflow stays the default.** Nothing is removed, and without configuration `/review-loop` behaves as before apart from a one-line notice (see below).
- Routing to paired-session is opt-in only. The default is planned to flip in v2.10.0 after 3 stable real runs; that is a plan, not a promise of this release.
- The opt-in works today, but the gates for real routing (1C safety review, M4, M6) are not closed (see Known gaps). They decide when it stops being experimental and can become the default.

## Opt in
Add to `.review-loop/config.md` (the key is documented in `review-loop-config.example.md`):

```
entry: paired-session
```

Only fresh work (`/review-loop <work item>` with no plan, code target or session) is handed to the `paired-session` skill. The Codex runtime honors the same key and hands off to its `paired-session` skill. The key is workspace-committed, so anyone who clones the repository is routed the same way.

The explicit entry `/review-loop:paired-session <work item>` (Claude) is unchanged. `--plan-only` on it maps to `run --stop-after-plan`.

## Opt out or force legacy
- Delete the key or set `entry: legacy`.
- Claude: `/review-loop:legacy <work item>` runs the legacy workflow and ignores `entry` (it does not read or validate it and prints none of its notices).
- Codex has no slash commands. Ask in natural language: "use the legacy review-loop workflow".
- `/review-loop:plan`, `execute` and `review-pr` stay legacy and are not affected by `entry`.

## What is and is not routed
| Situation | Result |
|---|---|
| Fresh work item | paired-session (only when `entry: paired-session`) |
| Plan already exists | legacy |
| Code already implemented (including a dirty tree detected as implemented code) | legacy |
| Existing legacy session / explicit resume | legacy |

Legacy sessions and plans cannot be imported into paired-session. A paired run is resumed only with `paired-session resume` and its original options; the two workflows never cross. If paired-session reports a HOLD, that is reported to you; it never falls back to legacy.

## Notices and warnings you will see
Printed by the `/review-loop` skill text (Claude wording). On Codex the implicit-entry line ends `... to opt in, or ask for "the legacy review-loop workflow" explicitly`; the other four are identical:
- No `entry` key: `review-loop: legacy workflow via implicit entry; set "entry: paired-session" in .review-loop/config.md to opt in, or use /review-loop:legacy explicitly`
- Value other than exactly `legacy` or `paired-session` (quoted or differently cased included): `review-loop: entry "<v>" is not valid (legacy|paired-session); using legacy workflow`
- Duplicate `entry` key or unreadable config: `review-loop: entry could not be read (<reason>); using legacy workflow`
- Routed to paired-session: `review-loop: paired-session entry is experimental (entry set in .review-loop/config.md)`
- Opted in but plan, code or a session already exists: `review-loop: entry is paired-session but <plan exists|code exists|existing session> detected; using legacy workflow`

## Author and reviewer roles are inverted
Legacy: Claude executes, Codex reviews. paired-session defaults to the reverse: Codex is the author and Claude is the reviewer. Roles come from an operator-owned profile outside the workspace passed with `--config`, not from `reviewer` or `executor_model`; without one the defaults above apply. `.review-loop/paired-session.json` may hold limits only (role/vendor/program keys there cause HOLD), and `--config` replaces it rather than layering. Models follow the ADR-8 vendor pins (Claude `claude-opus-5-5`, Codex `gpt-6-luna`).

## Config and mode mapping
The full design is in [`paired_session/docs/1d-entry-mapping.md`](../paired_session/docs/1d-entry-mapping.md). In short: `soft_limit_plan`/`soft_limit_exec` and the model keys are designed to map to explicit paired-session options, and most other legacy keys (`reviewer`, `skip_quality_polish`, `adversarial_gate_skip_paths`, `auto_commit`, `docs_file`, `handsfree`, `review_focus`, ...) are designed as not mapped. **That mapping is a design.** In v2.9.0 the routed paired-session skill reads only `.review-loop/paired-session.json` or `--config`; the design's per-key "not mapped" warnings and its refusal of legacy `reviewer_model`/`executor_model` values are not implemented, so legacy keys are silently not applied (the coordinator itself still enforces the ADR-8 model pins). Set limits and models in the paired-session profile instead; unset defaults are plan 3 / exec 4 rounds (legacy 3/3). Design-doc behavior described as future is not a guarantee.

Also note: paired-session caps are hard (a HOLD), not a prompt; `lifecycle_mode` is `off`, so no commit, merge or push happens and no `docs_file` entry is written.

## Known gaps
Details and evidence: [`paired_session/docs/1c-safety-controls.md`](../paired_session/docs/1c-safety-controls.md).
- **1C is not closed.** That document is an inventory, not an independent closure. Several controls (for example the Codex author sandbox and hook/credential flags) are verified only by offline tests of generated flags; equivalence to a real `codex exec` is unverified. The per-round change detection cannot see gitignored files or `.git`, ignored files are not inventoried on real runs, Git hooks are neither run nor blocked, and `--skip-probe` is unguarded.
- **Lifecycle stages after APPROVE are fake-only.** FINISH through CLOSE (docs, security, delivery) run only with the fake-CLI lifecycle; `lifecycle_mode=on` is refused on the real CLI. A real run ends at DONE, which is not acceptance; you accept or reject it explicitly with `accept`/`reject`.
- **M4 (fake PLAN-to-close E2E) and M6 (installed-CLI re-checks) are pending.**
- No sensitive-path SECURITY preflight on real runs; a probe result of `PASS_RESIDUAL_RISK` still allows a real run.
- accept/reject has no operator authentication.
- Installed Codex writes a trust entry into `~/.codex/config.toml` (outside the worktree); the coordinator only warns, it does not revert it.
Treat routed runs as experimental: a dedicated worktree does not protect `~/.codex/config.toml`; review the result yourself.
