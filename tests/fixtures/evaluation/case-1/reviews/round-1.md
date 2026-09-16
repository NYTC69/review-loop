### VERDICT: APPROVE

### Issues
- [MINOR] `window()` slices `size + 1` items; the docstring promises the first `size`.
  File: `pkg/calc.py`, lines 13-15

### Strengths
Read-only pass: `clamp` is correct and the module is small; the window slice is a MINOR the owner may fix later.
