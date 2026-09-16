### VERDICT: REQUEST_CHANGES

### Issues
- [CRITICAL] `window()` returns `size + 1` items — must be resolved before proceeding
  Trigger: any caller asks for the first `size` items
  Reachability: every call site of `window()` in the package hits the slice
  Impact: callers receive one extra element and downstream totals are wrong
  Likelihood: certain on every call
  Fix cost: trivial; slice `[:size]` and add a focused test
  Cheaper response: a MINOR would ship a wrong result in the same module this fix touches
  File: `pkg/calc.py`, lines 13-15

### Strengths
The `clamp` fix is correct and the new test pins the upper bound.
