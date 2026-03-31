# review-loop

A Claude Code skill that automates the Plan → Review → Execute → CR loop
using an Executor sub-agent and an independent Reviewer. You describe a work
item; the agents drive it to delivery — and you see every issue the Reviewer
catches along the way.

## Why review-loop?

A single AI agent can plan and implement, but it won't catch its own blind
spots. review-loop adds an independent Reviewer (a different AI) that
critiques every plan and every code change before it ships. In practice,
this catches design deviations, missed edge cases, and unauthorized
compromises that a single agent would silently ship.

## How it works

```
You: "/review-loop add rate limiting to the /api/upload endpoint"

Orchestrator (Claude Code main session)
│
├── Context file: .claude/review-loop-sessions/{uuid}.md
│   Single source of truth — both agents read it each round
│
├── [Planning phase]
│   ├── → Executor (sub-agent): draft solution plan
│   ├── → Reviewer (codex exec): review plan
│   ├── ← Live Report: what the Reviewer found
│   └── (iterate until APPROVE or user stops)
│
├── [Execution phase]
│   ├── → Executor (sub-agent): implement approved plan
│   ├── → Reviewer (codex exec): code review + plan conformance check
│   ├── ← Live Report: issues found, conformance violations
│   └── (iterate until APPROVE or user stops)
│
└── Delivery: summary with full findings table + optional commit
```

## Installation

In Claude Code, run:

```
/plugin marketplace add NYTC69/review-loop
/plugin install review-loop@review-loop-marketplace
```

Then `/reload-plugins` or restart the session. The `/review-loop` command
is now available in all your projects.

**Optional**: create a project-level config to customize defaults:

```bash
cp ~/.claude/plugins/cache/review-loop/review-loop-config.example.md .claude/review-loop-config.md
```

## Usage

```bash
# Slash command
/review-loop add pagination to the user list endpoint

# Natural language (auto-triggers)
run review-loop on: refactor the auth middleware to use JWT

# Fully autonomous mode — decision questions go to Reviewer, not you
/review-loop add caching layer to the API --handsfree
```

## Reviewer modes

| Mode | Config | How it works |
|------|--------|-------------|
| **codex** (default) | `reviewer: codex` | Calls `codex exec -s read-only` — cross-AI review |
| **subagent** | `reviewer: subagent` | Claude Code sub-agent with read-only tools (TODO) |

The codex mode gives you independent review from a different AI. The subagent
mode is a fallback for users without a Codex subscription.

## Configuration

All options in `.claude/review-loop-config.md`:

| Key | Default | Description |
|-----|---------|-------------|
| `reviewer` | codex | `"codex"` \| `"subagent"` |
| `reviewer_model` | "" | codex: `-m` flag (empty = codex default); subagent: Agent model |
| `executor_model` | inherit | `"inherit"` \| `"sonnet"` \| `"opus"` |
| `soft_limit_plan` | 3 | After N rounds, ask user to continue if CRITICALs remain |
| `soft_limit_exec` | 3 | Same for execution phase |
| `auto_commit` | false | Stage changed files and commit after delivery |
| `commit_message_prefix` | feat | Conventional commit type prefix |
| `docs_file` | CHANGELOG.md | File to append delivery summary; `""` to skip |
| `handsfree` | false | Make `--handsfree` the default |
| `review_focus` | "" | Project-specific review priorities for code review (free text) |

## Key design features

**Live Reports** — after every review round, the Orchestrator shows you what
the Reviewer found: CRITICAL issues, MINOR suggestions, and the verdict.
You see the value of the review loop in real time.

**Plan Conformance** — the Reviewer checks that the Executor's implementation
stays within the approved plan. If the Executor introduces unauthorized
design decisions (new thresholds, relaxed constraints), it's flagged as
CRITICAL even if the code is technically correct.

**Context file** — all loop state is persisted to
`.claude/review-loop-sessions/{uuid}.md`. Both agents read it each round for
instant context (no cold-start exploration). Session files are preserved for
post-hoc traceability — you can review which round introduced an issue.

**Soft iteration limits** — no hard cap on rounds. When the soft limit is
reached and CRITICALs remain, the Orchestrator asks you whether to continue.
Stuck detection stops the loop if the same issue recurs 3 rounds without
progress.

**Project-specific config** — customize the review loop per project via
`.claude/review-loop-config.md`. Choose your Reviewer backend, set iteration
limits, and define `review_focus` to tell the Reviewer what matters most for
your project (security for web apps, concurrency for backend services,
accessibility for frontend, etc.).

## File structure

```
review-loop/
├── skills/
│   └── review-loop/
│       └── SKILL.md                ← Orchestrator instructions
├── agents/
│   ├── executor.md                 ← Executor sub-agent definition
│   └── reviewer.md                 ← Reviewer definition (also embedded in codex prompt)
├── review-loop-config.example.md   ← Copy to .claude/ and customize
├── .gitignore
├── LICENSE                         ← Apache 2.0
└── README.md                       ← This file
```

## License

Apache 2.0
