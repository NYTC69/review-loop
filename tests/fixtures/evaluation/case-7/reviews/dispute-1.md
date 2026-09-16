Reachability: `keep_days` is validated by the configuration loader before `purge()` is called.
Impact: bounded by that validation.
Fix cost: small, but the guard would duplicate upstream validation.
Requested disposition: MINOR (document the precondition).
