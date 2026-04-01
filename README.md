# review-loop

A Claude Code plugin that provides two complementary code review workflows:

1. **review-loop** — Dual-agent orchestration: Plan → Review → Execute → CR, driven by an Executor and an independent Reviewer.
2. **code-quality-loop** — Iterative code-level review-fix loop: review → auto-fix → re-review until clean, with test consolidation.

Both workflows use a shared set of specialized review agents included in the plugin.

## Why review-loop?

A single AI agent can plan and implement, but it won't catch its own blind spots. review-loop adds an independent Reviewer that critiques every plan and every code change before it ships. In practice, this catches design deviations, missed edge cases, and unauthorized compromises that a single agent would silently ship.

## Workflows

### `/review-loop` — Dual-Agent Orchestration

End-to-end delivery: describe a work item, and the agents drive it from plan to implementation with independent review at every step.

```
You: "/review-loop add rate limiting to the /api/upload endpoint"

Orchestrator (Claude Code main session)
│
├── Context file: .claude/review-loop-sessions/{uuid}.md
│
├── [Planning phase]
│   ├── Executor (sub-agent): draft solution plan
│   ├── Reviewer (codex / sub-agent): review plan
│   ├── Live Report: what the Reviewer found
│   └── iterate until APPROVE or user stops
│
├── [Execution phase]
│   ├── Executor (sub-agent): implement approved plan
│   ├── Reviewer (codex / sub-agent): code review + plan conformance
│   ├── Live Report: issues found, conformance violations
│   └── iterate until APPROVE or user stops
│
└── Delivery: summary with full findings table + optional commit
```

**Handsfree mode**: add `--handsfree` to let the Reviewer make decisions autonomously — all decisions are logged in the delivery summary.

### `/review-loop:code-quality-loop` — Iterative Code Quality

Focused on code already written. Reviews `git diff`, auto-fixes issues, and repeats until clean or stuck. Finishes with a simplify pass and test consolidation.

```
Pre-loop:  go-reviewer agent (Go only, once)
Loop R1:   review-pr (code, errors, comments, types) → triage → fix
Loop R2+:  review-pr (code, errors) → triage → fix → repeat
Finalize:  code-simplifier agent → build → test consolidation → review-pr (tests)
```

### `/review-loop:review-pr` — Standalone Code Review

Run specialized review agents on demand, targeting specific aspects of code quality.

```bash
/review-loop:review-pr                    # full review (all aspects)
/review-loop:review-pr code errors        # specific aspects only
/review-loop:review-pr tests              # test coverage only
/review-loop:review-pr all parallel       # all agents in parallel
```

## Installation

In Claude Code, run:

```
/install-plugin https://github.com/NYTC69/review-loop
```

Then start a new session. All commands are now available.

**Optional**: create a project-level config to customize defaults:

```bash
cp review-loop-config.example.md .claude/review-loop-config.md
```

## Configuration

All options in `.claude/review-loop-config.md` (for the `review-loop` workflow):

| Key | Default | Description |
|-----|---------|-------------|
| `reviewer` | codex | `"codex"` \| `"subagent"` |
| `reviewer_model` | "" | codex: `-m` flag; subagent: Agent model (empty = inherit) |
| `executor_model` | inherit | `"inherit"` \| `"sonnet"` \| `"opus"` |
| `soft_limit_plan` | 3 | After N rounds, ask user to continue if CRITICALs remain |
| `soft_limit_exec` | 3 | Same for execution phase |
| `auto_commit` | false | Stage changed files and commit after delivery |
| `commit_message_prefix` | feat | Conventional commit type prefix |
| `docs_file` | CHANGELOG.md | File to append delivery summary; `""` to skip |
| `handsfree` | false | Make `--handsfree` the default |
| `review_focus` | "" | Project-specific review priorities (free text) |

### Reviewer modes

| Mode | Config | How it works |
|------|--------|-------------|
| **codex** (default) | `reviewer: codex` | Calls `codex exec -s read-only` — cross-AI review |
| **subagent** | `reviewer: subagent` | Claude Code sub-agent with read-only tools — no external CLI required |

## Included Agents

All agents are bundled in `agents/` and available to every workflow:

| Agent | Role |
|-------|------|
| **executor** | Plans and implements code changes (used by review-loop) |
| **reviewer** | Independent plan/code reviewer with structured verdicts (used by review-loop) |
| **code-reviewer** | General code review against CLAUDE.md and best practices |
| **code-simplifier** | Simplifies code for clarity and maintainability |
| **silent-failure-hunter** | Finds silent failures, broad catches, and inadequate error handling |
| **pr-test-analyzer** | Reviews test coverage quality and identifies critical gaps |
| **comment-analyzer** | Verifies comment accuracy and flags comment rot |
| **type-design-analyzer** | Analyzes type encapsulation, invariants, and design quality |
| **go-reviewer** | Go-specific static analysis (go vet, staticcheck, golangci-lint, race, govulncheck) |

## Key Design Features

- **Live Reports** — after every review round, see what the Reviewer found in real time
- **Plan Conformance** — flags unauthorized design deviations as CRITICAL, even if code is technically correct
- **Context File** — loop state persisted to `.claude/review-loop-sessions/{uuid}.md` for traceability
- **Soft Limits + Stuck Detection** — no hard cap on rounds; asks to continue at soft limit; stops if same issue recurs 3 rounds
- **Self-Contained** — all agents and skills bundled in the plugin, no external dependencies required (codex CLI optional for cross-AI review)

## File Structure

```
review-loop/
├── skills/
│   ├── review-loop/SKILL.md          # Dual-agent orchestration
│   ├── code-quality-loop/SKILL.md    # Iterative review-fix loop
│   ├── review-pr/SKILL.md           # Standalone code review
│   └── guide/SKILL.md               # Usage guide
├── agents/
│   ├── executor.md                   # Executor sub-agent
│   ├── reviewer.md                   # Independent Reviewer
│   ├── code-reviewer.md             # General code review
│   ├── code-simplifier.md           # Code simplification
│   ├── silent-failure-hunter.md     # Error handling audit
│   ├── pr-test-analyzer.md          # Test coverage analysis
│   ├── comment-analyzer.md          # Comment accuracy check
│   ├── type-design-analyzer.md      # Type design analysis
│   └── go-reviewer.md               # Go static analysis
├── review-loop-config.example.md     # Config template
├── .claude-plugin/                   # Plugin metadata
├── LICENSE                           # Apache 2.0
└── README.md
```

## License

Apache 2.0
