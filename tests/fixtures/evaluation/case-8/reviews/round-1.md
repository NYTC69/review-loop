### VERDICT: REQUEST_CHANGES

### Issues
- [CRITICAL] `window()` returns `size + 1` items — must be resolved before proceeding
  Trigger: any caller asks for the first `size` items
  Reachability: `window()` is exported and used by the report path
  Impact: one extra element in every window; wrong totals
  Likelihood: certain on every call
  Fix cost: trivial; slice `[:size]` plus a regression test
  Cheaper response: a MINOR ships a wrong result in a release
  File: `pkg/calc.py`, lines 13-15
- [CRITICAL] `load()` swallows `OSError` and `ValueError` and returns `{}` — must be resolved before proceeding
  Trigger: the store file is missing or corrupt
  Reachability: every start-up reads the store through `load()`
  Impact: a corrupt or missing store is silently treated as empty and later overwritten by `save()`
  Likelihood: moderate — disk full and half-written files happen
  Fix cost: small; let the exception propagate and add a regression test
  Cheaper response: a MINOR leaves silent data loss in a release
  File: `pkg/store.py`, lines 5-10

### Strengths
`expired_names` and the atomic `save` are sound; the release note lists the surface.
