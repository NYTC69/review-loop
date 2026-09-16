### VERDICT: REQUEST_CHANGES

### Issues
- [CRITICAL] `load()` can observe a document replaced between open and read (no lock, no checksum) — must be resolved before proceeding
  Trigger: a second process replaces the file while `load()` is between `open` and `json.load`
  Reachability: requires a concurrent writer racing a single-process CLI that only reads at start-up
  Impact: a stale or partially observed document is returned once; the next run re-reads
  Likelihood: very low — the store is used by one process at a time
  Fix cost: large; advisory locking plus a sidecar checksum file and its maintenance
  Cheaper response: a MINOR would leave a theoretical torn read unaddressed
  File: `pkg/store.py`, lines 5-7

### Strengths
`save()` is atomic (temp file + rename) and covered by a round-trip test.
