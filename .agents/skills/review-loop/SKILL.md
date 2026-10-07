---
name: review-loop
description: Codex review-loop entry. Resolves the `entry` key and hands a work item (fresh work, an existing plan, or a review of existing code) to the Codex paired-session skill, which drives plan → review → implement → CR up to acceptance.
---

# review-loop — codex entry

Read `docs/protocol/loading.md`. This entry hands off to paired-session, so its
load must leave no file in the product worktree: first print a fresh bundle
directory outside it as its own command,
`python3 -c 'import os, tempfile, uuid; print(os.path.join(os.path.realpath(tempfile.gettempdir()), f"review-loop-protocol-{os.getuid()}", uuid.uuid4().hex[:12]))'`,
then run, with that printed directory written out literally as `<bundle-dir>`:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime codex --stage entry-review-loop --output <bundle-dir>/protocol-codex-entry-review-loop.md
```

Resolve <support-root> to this plugin/repository, not the task workspace. Cwd,
complete reads of the output file and one Bash command per loader call follow `loading.md`;
a missing, unreadable, or incompletely read file blocks the action.
`docs/protocol/loading.json` is the action/prerequisite map for both runtimes.

Every review-loop request hands off to paired-session per the entry procedures (fresh work; an existing plan as the work item; a code target as `run --review-only`; `entry: legacy`, the explicit legacy request and a legacy session resume are refused): the legacy workflow was removed in v2.13.0.
The procedures live in `references/entry.md` (loaded above). This entry creates no session file or lock;
the paired-session skill owns the run from the handoff on. Preserve unrelated dirty work.
