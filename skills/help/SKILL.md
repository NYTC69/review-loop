---
name: help
description: >
  Show review-loop usage guide: how it works, commands, configuration,
  and key features. Use when the user asks for help with review-loop.
disable-model-invocation: true
---

Display the following guide to the user exactly as written:

---

# review-loop — Quick Reference

## How it works

```
/review-loop <work item description> [--handsfree]

Orchestrator (this session)
│
├── Context file: .claude/review-loop-sessions/{uuid}.md
│   Single source of truth — both agents read it each round
│
├── [Planning phase]
│   → Executor drafts plan → Reviewer critiques → iterate
│
├── [Execution phase]
│   → Executor implements → Reviewer does CR → iterate
│
└── Delivery: findings table + time breakdown + optional commit
```

## Usage

```bash
# Basic — starts full plan→review→implement→CR loop
/review-loop add rate limiting to the /api/upload endpoint

# Handsfree — decision questions go to Reviewer, not you
/review-loop refactor auth middleware --handsfree

# If code is already written, it auto-detects and skips to CR
/review-loop review the changes I just made to the parser
```

## Configuration

Create `.claude/review-loop-config.md` in your project to customize:

| Key | Default | Description |
|-----|---------|-------------|
| `reviewer` | codex | `"codex"` \| `"subagent"` |
| `reviewer_model` | "" | codex: `-m` flag (empty = codex default) |
| `executor_model` | inherit | `"inherit"` \| `"sonnet"` \| `"opus"` |
| `soft_limit_plan` | 3 | Rounds before asking to continue |
| `soft_limit_exec` | 3 | Same for execution phase |
| `auto_commit` | false | Commit after delivery |
| `handsfree` | false | Default to handsfree mode |
| `review_focus` | "" | Project-specific review priorities (free text) |

### review_focus examples

```yaml
# Backend
review_focus: |
  - Concurrency: race conditions, deadlocks, mutex usage
  - Test coverage: error paths, not just happy paths

# Frontend
review_focus: |
  - Security: XSS, CSRF, input sanitization
  - Accessibility: WCAG, keyboard nav, screen reader

# Web API
review_focus: |
  - Security: auth checks, rate limiting, input validation
  - API contract: backward compatibility, proper status codes
```

## Key features

- **Live Reports** — see what the Reviewer found after every round
- **Plan Conformance** — flags unauthorized Executor deviations as CRITICAL
- **Context file** — persistent session for traceability and fast agent startup
- **Soft limits + stuck detection** — no hard cap, smart stopping
- **Project-specific config** — tailor review priorities per project

## More info

GitHub: https://github.com/NYTC69/review-loop
