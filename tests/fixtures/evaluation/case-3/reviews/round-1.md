### VERDICT: REQUEST_CHANGES

### Issues
- [CRITICAL] `parse_line()` silently drops the last field of a line without a trailing `;` — must be resolved before proceeding
  Trigger: a line such as `a=1;b=2` (no trailing separator) reaches `parse_line`
  Reachability: `parse_lines()` added in this change feeds raw file lines straight in
  Impact: the last key of every such line is lost; reports are silently incomplete
  Likelihood: high — most producers do not emit a trailing separator
  Fix cost: small; iterate every field and skip empty ones
  Cheaper response: a MINOR would leave silent data loss on the new code path
  File: `pkg/b.py`, lines 8-10

### Strengths
`format_summary` is a clean, pure addition with no cross-module coupling.
