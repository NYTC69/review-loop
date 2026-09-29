---
name: legacy
argument-hint: "<work item description> [--handsfree]"
description: >
  Explicit control command: run the existing legacy review-loop workflow on a
  work item, ignoring the `entry` config key. Trigger only when the user names
  `/review-loop:legacy` or explicitly asks for the legacy workflow.
---

# legacy — explicit legacy review-loop

Run the `review-loop` skill (`skills/review-loop/SKILL.md`) unchanged. Load its
protocol through the shared loading map, not a copy of the workflow:

```bash
python3 <support-root>/scripts/read_protocol.py --runtime claude --stage entry-review-loop
```

Follow that skill and every stage bundle it names exactly as if the user had
invoked `/review-loop`, with two differences: ignore the `entry` key in
`.review-loop/config.md` (never route to paired-session), and print no
entry-routing or implicit-entry notice. All other config keys apply as usual.
