### VERDICT: REQUEST_CHANGES

### Issues
- [CRITICAL] `load()` takes no lock and verifies no checksum (gate finding re-asserted with the full rubric) — must be resolved before proceeding
  Trigger: the nightly job rewrites the store while the CLI reads it
  Reachability: documented concurrent operation
  Impact: a torn read yields a partial document
  Likelihood: moderate in the overlap window
  Fix cost: large, but the operator guide promises concurrent safety
  Cheaper response: a MINOR contradicts the documented guarantee
  File: `pkg/store.py`, lines 5-7

### Strengths
`save()` is atomic.
