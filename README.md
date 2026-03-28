# review-loop

A Claude Code skill that automates the Plan → Review → Execute → CR loop
using two specialized sub-agents. You describe a work item; the agents drive
it to delivery.

## How it works

```
You: "run review-loop on: add rate limiting to the /api/upload endpoint"

Orchestrator (Claude Code main session)
│
├── [Planning phase]
│   ├── → Executor: draft solution plan
│   ← Executor: returns plan
│   ├── → Reviewer: review plan  
│   ← Reviewer: APPROVE / REQUEST_CHANGES + feedback
│   └── (iterate up to max_plan_iterations)
│
├── [Execution phase]
│   ├── → Executor: implement approved plan
│   ← Executor: returns change summary
│   ├── → Reviewer: code review
│   ← Reviewer: APPROVE / REQUEST_CHANGES + feedback
│   └── (iterate up to max_exec_iterations)
│
└── Delivery: optional git commit + CHANGELOG update + summary to you
```

---

## Installation

### 1. Install the skill

```bash
# Project-level (recommended — keeps it with your repo)
mkdir -p .claude/skills
cp -r review-loop .claude/skills/

# OR global (available in all your projects)
mkdir -p ~/.claude/skills
cp -r review-loop ~/.claude/skills/
```

### 2. Install the sub-agent definitions

```bash
# Project-level
mkdir -p .claude/agents
cp review-loop/agents/*.md .claude/agents/

# OR global
mkdir -p ~/.claude/agents
cp review-loop/agents/*.md ~/.claude/agents/
```

### 3. (Optional) Create a project config

```bash
cp review-loop/review-loop-config.example.md .claude/review-loop-config.md
# Edit to your preferences
```

---

## Usage

Just describe what you want done:

```
# Natural triggers
run review-loop on: [your work item description]
start agent loop for: [work item]
let the agents handle: [work item]

# Or just describe the task — the skill auto-triggers on relevant phrases
Add pagination to the user list endpoint. 
Acceptance criteria: page size default 20, max 100, returns total_count.
```

The Orchestrator will ask ONE clarifying question if needed, then start the loop
autonomously. You'll get a status report when it's done (or if it hits a blocker).

---

## Configuration reference

All options in `.claude/review-loop-config.md`:

| Key | Default | Description |
|-----|---------|-------------|
| `max_plan_iterations` | 3 | Max Executor↔Reviewer rounds in planning phase |
| `max_exec_iterations` | 3 | Max rounds in execution phase |
| `executor_model` | inherit | `inherit` \| `sonnet` \| `opus` |
| `reviewer_model` | inherit | `inherit` \| `sonnet` \| `opus` |
| `reviewer_readonly` | true | Restrict Reviewer to read-only tools |
| `auto_commit` | false | `git commit` after execution phase |
| `commit_message_prefix` | feat | Conventional commit type prefix |
| `docs_file` | CHANGELOG.md | File to append delivery summary; `""` to skip |

### Tips
- **Use `opus` for the Reviewer** on complex changes where missing a subtle bug is costly
- **Lower `max_plan_iterations` to 2** for well-scoped, familiar tasks
- **Set `auto_commit: true`** only in projects with a solid test suite
- **Set `docs_file: ""`** if you manage your changelog manually

---

## Adapting for other AI providers

The sub-agents are defined as plain markdown files in `.claude/agents/`. The
`executor.md` and `reviewer.md` system prompts are provider-agnostic — you can
adapt this pattern to any agent framework that supports custom agent definitions
with scoped tool access.

---

## File structure

```
review-loop/
├── SKILL.md                        ← Orchestrator instructions (loaded by Claude Code)
├── agents/
│   ├── executor.md                 ← Executor sub-agent definition
│   └── reviewer.md                 ← Reviewer sub-agent definition (read-only tools)
├── review-loop-config.example.md    ← Copy to .claude/ and customize
└── README.md                       ← This file
```
