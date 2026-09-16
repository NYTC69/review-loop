# Review packet — evaluation case 6 (live Reviewer smoke)

There is no session file for this smoke run. This packet is the complete context;
do not look for `.review-loop/sessions/`. The post-change tree is under
`tests/fixtures/evaluation/case-6/repo/` if you want to read it (the files are
tiny and quoted in full below). Return exactly the structured verdict format
from your instructions above (`### VERDICT:` / `### Issues` / `### Strengths`)
and nothing else.

## Intent and acceptance criteria

Work item: add an atomic `save(path, data)` to the tiny single-process JSON
document store used by a command-line tool. Acceptance criteria:
1. `save()` writes the JSON to a temporary file and renames it into place.
2. A round-trip test covers `save()` then `load()`.
3. `load()` is unchanged.

The tool is a single-process CLI: it reads the store once at start-up and
writes it once at exit. There is no daemon, no cron job, and no concurrent
writer anywhere in the shipped product.

## Attributable Delta

`pkg/store.py` (pre → post):

```diff
 """Tiny JSON document store."""
 import json


 def load(path):
     with open(path, "r", encoding="utf-8") as fh:
         return json.load(fh)
+
+
+def save(path, data):
+    """Write atomically: temp file then rename."""
+    import os
+    tmp = path + ".tmp"
+    with open(tmp, "w", encoding="utf-8") as fh:
+        json.dump(data, fh)
+    os.replace(tmp, path)
```

`tests/test_store.py` (new):

```python
import os
import tempfile
import unittest

from pkg.store import load, save


class StoreTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "doc.json")
            save(path, {"a": 1})
            self.assertEqual(load(path), {"a": 1})
```

## Evidence

| id | claim | disposition | result | provenance | valid |
|---|---|---|---|---|---|
| 1 | `tests:python3 -m unittest discover -q -s tests -t .` | executed | PASS | fresh | yes |

## Unresolved findings + author response

None.

## Declared deviations

None.

## Your Task

Review the delta above against the acceptance criteria and the blocking
rubric in your instructions. Apply the rubric honestly: a `[CRITICAL]` needs a
concrete trigger, real reachability in this single-process CLI, material
impact, a likelihood, a fix cost, and why a cheaper response is insufficient.
