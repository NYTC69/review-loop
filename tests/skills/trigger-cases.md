# Skill trigger cases (manual release checklist)

A manual check that each entry skill's `description` routes natural requests to the right skill. It is not an
automated test: pytest and `scripts/run-skill-lint` do not read this file.

## How to run

1. Install the release candidate on the host (Claude Code: `/plugin update`; Codex: `codex plugin add
   review-loop@review-loop-marketplace`), then open a **fresh session per host** in a test repository.
2. Type each request below as a new message, without a slash command. One fresh session per skill section is
   enough; start another one if an earlier request already loaded a skill.
3. Record which skill loaded first (Claude Code shows the skill call; in Codex, ask "which skill did you use?" or
   read the session log). Stop each run before it starts a coordinator command.
4. A case passes when the expected skill loads (should trigger, paraphrase) or when the named skill does not load
   and the request goes to the skill in brackets, or to none (should NOT trigger). Fill in the results table with
   the date, host version and plugin version. Target: every "should trigger" case passes; note any miss as a
   description fix for the next release.

Handoffs are expected: review-loop, review-pr and code-quality-loop hand off to paired-session after their entry
step. Only the first skill that loads counts.

## Claude Code

### review-loop

Should trigger:
- "Run review-loop on adding retry support to the HTTP client."
- "Start review loop for this task: migrate the config loader to TOML."
- "Let the agents handle the pagination fix."
- "Use the review loop to implement the plan in PLAN.md."

Paraphrase should trigger:
- "Plan, implement and review this change with independent reviewers: add rate limiting to the API."
- "Drive this feature through plan, review, implement and code review: export to CSV."
- "I want the full plan-review-implement cycle on the login refactor."
- "Review my changes." (review-loop: its description covers "a review of existing code" as a review-only run.
  Note: this bare request also overlaps review-pr, which covers a report on the local change, and
  code-quality-loop, which reviews and fixes the uncommitted change. If another skill loads first, record it
  as ambiguous, not as a failure.)

Should NOT trigger:
- "Review PR 42 and write a report." (review-pr)
- "How do I configure review-loop?" (guide)
- "Reorganize src/engine.go without changing behavior." (reorganize)

### paired-session

Should trigger:
- "Use paired-session for this task: add a health-check endpoint."
- "Run the paired-session coordinator on the cache invalidation fix."
- "Start a paired-session run with --plan-only for the schema change."

Paraphrase should trigger:
- "Drive this through the paired-session coordinator up to my acceptance: rename the user table."
- "I want an explicit paired-session run, not the review-loop entry, for the queue rewrite."

Should NOT trigger (as the first skill):
- "Run review-loop on the logging cleanup." (review-loop, which then hands off)
- "Review PR 42." (review-pr, which then hands off)
- "Clean up my uncommitted changes." (code-quality-loop, which then hands off)
- "What is review-loop?" (guide)

### review-pr

Should trigger:
- "Review PR 123."
- "Review https://github.com/acme/app/pull/88."
- "Run review-pr on this branch against main."
- "Review the error handling and tests in PR 57."

Paraphrase should trigger:
- "Give me a review report of my current diff; don't change anything."
- "Can you look over pull request 12 and tell me what's wrong with it?"
- "Write a code review of the feature/search branch."

Should NOT trigger:
- "Review my uncommitted changes and fix what you find." (code-quality-loop)
- "Implement the search filter and have it reviewed." (review-loop)
- "Restructure the utils directory." (reorganize)

### code-quality-loop

Should trigger:
- "Run code-quality-loop."
- "Clean up my changes before I commit."
- "Review and fix my diff until it's clean."
- "Polish this before I commit, max 3 rounds."

Paraphrase should trigger:
- "Go over my uncommitted work, fix the problems and simplify it."
- "Iterate review and fixes on the working tree until there's nothing left."

Should NOT trigger:
- "Review PR 123." (review-pr)
- "Write a review report of my diff without changing it." (review-pr)
- "Add a CLI flag for verbose output and get it reviewed." (review-loop)
- "Reorganize src/parser.rs into sections." (reorganize)

### reorganize

Should trigger:
- "Reorganize src/engine.go."
- "Reorganize the files in my diff."
- "Restructure this module and add section comments."

Paraphrase should trigger:
- "Tidy up the layout of src/core without changing behavior."
- "Rearrange the functions in handlers.py and pull out the duplicated code."

Should NOT trigger:
- "Review and fix my uncommitted changes." (code-quality-loop)
- "Review PR 7." (review-pr)
- "Refactor the payment flow and have it reviewed." (review-loop)

### guide

Should trigger:
- "How does review-loop work?"
- "Show me the review-loop guide."
- "Help with review-loop."

Paraphrase should trigger:
- "What commands does this review plugin have?"
- "Which settings can I put in .review-loop/config.md?"

Should NOT trigger:
- "Run review-loop on the auth fix." (review-loop)
- "Review PR 9." (review-pr)

## Codex

Natural language only; Codex has no slash commands for these skills.

### review-loop

Should trigger:
- "Run review-loop on adding retry support to the HTTP client."
- "Plan, implement and review this: add rate limiting to the API."
- "Let the agents handle the pagination fix."
- "Review my changes."

Paraphrase should trigger:
- "Use the review loop to implement the plan in PLAN.md."
- "Take this task through planning, implementation and independent review: export to CSV."
- "Check the change I just made with review-loop."
- "Use the legacy review-loop workflow." (the entry loads and refuses it)

Should NOT trigger:
- "Review PR 123." (review-pr)
- "How do I use review-loop in Codex?" (guide)

### paired-session

Should trigger:
- "Use paired-session for this task: add a health-check endpoint."
- "Run the paired-session coordinator on the cache invalidation fix."

Paraphrase should trigger:
- "I want an explicit paired-session run for the queue rewrite, up to my acceptance."
- "Have paired-session review PR 42." (its Review-PR entry)

Should NOT trigger (as the first skill):
- "Run review-loop on the logging cleanup." (review-loop, which then hands off)
- "Review PR 42." (review-pr, which then hands off)
- "What does the entry key do?" (guide)

### review-pr

Should trigger:
- "Review PR 123."
- "Review https://github.com/acme/app/pull/88."
- "Run review-pr on the feature/search branch."

Paraphrase should trigger:
- "Write a review report for the release-2.1 ref."
- "Look over pull request 12 and tell me what's wrong, but don't post anything."

Should NOT trigger:
- "Review my changes." (review-loop)
- "Implement the search filter and get it reviewed." (review-loop)
- "Review-loop help." (guide)

### guide

Should trigger:
- "How does review-loop work?"
- "Review-loop help."
- "What does the entry key do?"

Paraphrase should trigger:
- "How do I configure this review plugin?"
- "What can review-loop do in Codex?"

Should NOT trigger:
- "Run review-loop on the auth fix." (review-loop)
- "Review PR 9." (review-pr)

## Results

| Date | Host and version | Plugin version | Skill | Passed | Failed cases | Notes |
|---|---|---|---|---|---|---|
