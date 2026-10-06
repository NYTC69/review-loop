---
name: legacy
argument-hint: "<work item description> [--handsfree]"
disable-model-invocation: true
description: >
  Explicit control command: run the existing legacy review-loop workflow on a
  work item, ignoring the `entry` config key. Trigger only when the user names
  `/review-loop:legacy`. Do not trigger on a bare review-loop request.
---

# legacy — explicit legacy review-loop

Run the `review-loop` skill unchanged. First Read
`<support-root>/skills/review-loop/SKILL.md` and
`<support-root>/docs/protocol/loading.md` in full, where `<support-root>` is
this plugin/repository (not the task workspace). Load its protocol through the
shared loading map, not a copy of the workflow:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime claude --stage entry-review-loop
```

Run every loader call as its own Bash command, never chained (rule in `docs/protocol/loading.md`).

Follow that skill and every stage bundle it names exactly as if the user had
invoked `/review-loop`, with two differences: ignore the `entry` key in
`.review-loop/config.md` (never route to paired-session), and do not read or
validate `entry`, and print none of its notices. All other config keys apply as usual.

Print only the one-line deprecation notice, once, before the session file is created:
`review-loop: legacy is deprecated since v2.12.0; the default paired-session entry covers fresh work, review of existing changes and review-pr; code-quality-loop still uses legacy until it is ported; removal is planned after that`
It changes nothing else: routing and the workflow stay as described above.
