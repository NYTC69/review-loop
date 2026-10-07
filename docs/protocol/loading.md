# Protocol loading

`loading.json` names the authoritative sources and prerequisites for each
runtime/action. `scripts/read_protocol.py` emits their exact text; inventory
output alone is not a read. Resolve support paths against this plugin/repository,
but keep the user's workspace as cwd for git, config and task actions.

Two stages remain (the legacy workflow and its stages were removed in v2.13.0/v2.13.1):

| Next action | Load before acting |
|---|---|
| review-loop entry skills (routing and the stage A checks) | `entry-review-loop` |
| paired-session entry skills | `entry-paired-session` |

Both stages may precede a paired-session run, so they write to an absolute
`<system temp>/review-loop-protocol-<uid>/<id>/` path printed by the entry skill, never into the worktree:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime <claude|codex> --stage <stage> --output <bundle-dir>/protocol-<runtime>-<stage>.md
```

Every `read_protocol.py` call is its own Bash command, with no `&&`, `;`, pipes,
loops, command substitution or shell variables. Write `--loaded` values out literally.

On exit 0, determine the output file's line count and read it completely in
bounded chunks before its action. The loader writes atomically and emits only a
compact hash/size receipt to stdout, avoiding host tool-output truncation. Missing
source, ambiguous section, cyclic prerequisites, missing output, or an
incompletely read file blocks that action; do not guess a rule. Follow scoped
section references when a claim needs further detail. Cross-reference/audit
links are not instructions to preload every file.

Stage loading does not authorize a stage, a write, an external action, or a skip.

Within ONE live context, reuse a unit only while its full text is still available
and its source content is unchanged. Pass its emitted `UNIT@SHA256` fingerprint
with `--loaded` to avoid duplicate delivery. A changed fingerprint reloads it.
Do not persist fingerprints as proof of model memory: after compaction, process
restart, resume in a new context, or agent change, load required units afresh.

Before acting on framework self-modification, or when shell loading is unavailable,
read `loading-special-cases.md` in full.
