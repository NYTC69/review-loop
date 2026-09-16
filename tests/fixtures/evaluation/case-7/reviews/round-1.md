### VERDICT: REQUEST_CHANGES

### Issues
- [CRITICAL] `purge()` deletes every file when `keep_days <= 0`, and the new CLI passes the raw `--keep-days` integer through — must be resolved before proceeding
  Trigger: an operator runs the sweep with `--keep-days 0` (a typo, or an empty environment variable coerced to 0)
  Reachability: `pkg/cli.py` added in this change parses the flag with `type=int` and calls `purge()` unvalidated
  Impact: irreversible deletion of every file in the retention directory
  Likelihood: rare, but a single mistyped flag is enough
  Fix cost: small; refuse `keep_days < 1` in `purge()` and validate in the CLI
  Cheaper response: a MINOR leaves an irreversible-data-loss path one flag away
  File: `pkg/retention.py`, lines 12-14

### Strengths
`expired()` is pure and tested.
