"""FIELD-27 (FIELD-26 gate MEDIUM): the same false HOLD for PATHS. A review-only change that adds an extension-less path
naming a vendor or a review (`bin/codex-run`, `.claude/hooks/pre-commit`) held at "shadow independence check rejected
history in context/delta.stat: codex", and `docs/gate-review-notes.md` was refused at creation ("review-history wording
'gate-review'"). The change as created is the user's code: its paths pass; a path a later fix round adds is still caught;
ordinary runs are unchanged; a ledger-id or verdict shaped path stays refused (FIELD-11). A scope-change successor's user
content is its parent's change as created."""
import json
import subprocess
import unittest
from pathlib import Path

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_field26 as f26

rc = f26.rc
PATHS = {'bin/codex-run': '#!/bin/sh\nexit 0\n', '.claude/hooks/pre-commit': '#!/bin/sh\nexit 0\n',
         'docs/gate-review-notes.md': '# Notes\n\nNothing here.\n'}


class PathsAsCreatedTests(unittest.TestCase):
    locals().update({name: getattr(f26.ReviewOnlyCreationTextTests, name)
                     for name in (*f26.HELPERS, 'write', 'coordinator', 'scan')})

    def add(self, files=PATHS):
        for name, text in files.items():
            self.write(name, text)

    def test_vendor_and_review_named_paths_pass_uncommitted(self):
        self.add()
        co = self.coordinator('--review-only', '--lifecycle-mode', 'on')
        for role in ('shadow', 'gate'):
            self.scan(co, role)
        self.assertIn('bin/codex-run', (co.context / 'status.txt').read_text())

    def test_vendor_and_review_named_paths_pass_committed(self):   # the materialized PR clone: base..HEAD, all committed
        self.add()
        subprocess.run(['git', 'add', *PATHS], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add hooks and notes'], cwd=self.workspace, check=True)
        co = self.coordinator('--review-only', '--base', 'HEAD~1', '--lifecycle-mode', 'on', '--review-report')
        for role in ('shadow', 'gate'):
            self.scan(co, role)
        self.assertIn('bin/codex-run', (co.context / 'delta.stat').read_text())

    def test_a_path_added_after_creation_is_still_caught(self):
        self.add()
        co = self.coordinator('--review-only', '--lifecycle-mode', 'on')
        self.write('bin/codex-later', '#!/bin/sh\nexit 0\n')   # a fix round's new path
        with self.assertRaisesRegex(RuntimeError, r'shadow independence check rejected history in context/(delta\.stat|status\.txt): codex'):
            self.scan(co, 'shadow')

    def test_an_ordinary_run_is_unchanged(self):
        use_lifecycle_on(self, self)
        co = self.coordinator()
        self.add({'bin/codex-run': '#!/bin/sh\nexit 0\n'})
        with self.assertRaisesRegex(RuntimeError, r'shadow independence check rejected history in context/(delta\.stat|status\.txt): codex'):
            self.scan(co, 'shadow')

    def test_the_scope_masks_only_this_runs_listed_paths(self):
        self.add()
        co = self.coordinator('--review-only', '--lifecycle-mode', 'on')
        scope = (co.context / 'plan.md').read_text()
        self.assertIn('untracked: docs/gate-review-notes.md', scope)
        self.assertEqual(co._introduced_history('context/plan.md', scope), [])
        self.assertTrue(co._introduced_history('context/plan.md', scope + 'untracked: docs/gate-review-notes.md\n'))   # not the frozen scope
        masked = rc.mask_initial_change(scope + 'untracked: docs/F001.md\nA\tAPPROVE.txt\n')
        self.assertIn('untracked: docs/F001.md', masked)   # FIELD-11: ledger-id and verdict shaped paths stay visible
        self.assertIn('A\tAPPROVE.txt', masked)
        self.assertIn('untracked: <repo-path>', masked)

    def test_a_scope_change_successor_inherits_the_parents_creation_mirror(self):
        parent = self.root / 'parent'
        (parent).mkdir()
        (parent / 'state.json').write_text(json.dumps({'review_only': {'mirror': '/parent/internal/review-start'}}))
        self.add()
        co = self.coordinator('--review-only', '--lifecycle-mode', 'on')
        self.assertEqual(co.state['review_only']['history_mirror'], co.state['review_only']['mirror'])   # its own, for a new run
        co.args.supersedes = str(parent)
        self.assertEqual(co._parent_history_mirror(), '/parent/internal/review-start')
        (parent / 'state.json').write_text(json.dumps({'review_only': {'mirror': '/p/m', 'history_mirror': '/grand/m'}}))
        self.assertEqual(co._parent_history_mirror(), '/grand/m')
        co.state['review_only']['history_mirror'] = str(Path('/grand/m'))
        self.assertEqual(co._review_start_root(), Path('/grand/m'))


if __name__ == '__main__':
    unittest.main()
