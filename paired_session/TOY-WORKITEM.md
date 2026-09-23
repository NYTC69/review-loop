# Toy work item: strict integer sum

Create `sum_ints.py` with `sum_ints(values)` and stdlib `unittest` coverage in
`test_sum_ints.py`.

Acceptance criteria:

- Accept a list or tuple of integers and return its sum; an empty sequence returns `0`.
- Reject booleans, floats, strings, nested sequences, and non-list/tuple inputs with
  `TypeError`. Python's `bool` subclassing `int` is the deliberate edge case.
- Run `python3 -m unittest -v` and leave it passing.
- Do not commit or push.

```reviewer-commands
python3 -m unittest discover -v
```
