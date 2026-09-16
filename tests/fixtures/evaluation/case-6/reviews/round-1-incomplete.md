### VERDICT: REQUEST_CHANGES

### Issues
- [CRITICAL] `load()` can observe a document replaced between open and read — must be resolved before proceeding
  Trigger: a second process replaces the file during `load()`
  Impact: a stale document is returned once
  Likelihood: very low
  Cheaper response: a MINOR would leave it unaddressed
  File: `pkg/store.py`, lines 5-7

### Strengths
`save()` is atomic.
