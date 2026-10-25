"""V328-MASK: exempt bare names use the bare-name scan's masking boundaries."""
import unittest

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_field23_repo_text_scan as f23

FIXTURE = 'tests/ab/fixtures/wi291/historical.json'
PROVIDER = ('{"name": "codex", "version": "codex-cli 0.159.2", '
            '"provider_exit_error_class": "CodexProviderExitError"}\n')


class MaskHistoryTests(unittest.TestCase):
    def setUp(self):
        self.t = f23.RepoTextScanTests('test_history_in_a_new_file_holds')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        use_lifecycle_on(self, self.t.h)
        self.co = self.t.coordinator()
        self.co.state['exec_rounds'] = 1
        self.co.save()

    def patch_markers(self):
        self.co.materialize_review_context()
        delta = (self.co.context / 'delta.patch').read_text()
        hits = self.co._delta_history_hits(delta)
        markers = []
        for name in ('delta.patch', 'delta-since-last-review.patch'):
            text = (self.co.context / name).read_text()
            self.assertIn('+', text)
            markers.append(self.co._introduced_history('context/' + name, text))
        return hits, markers

    def test_json_hyphen_compound_has_no_hits(self):
        (self.co.context / 'workitem.md').write_text('# Toy\nImplement a codex reader.\n')
        self.t.write(FIXTURE, PROVIDER)
        hits, markers = self.patch_markers()
        self.assertEqual(hits, [])
        self.assertEqual(markers, [[], []])
        for role in ('shadow', 'gate'):
            self.co.assert_fresh_prompt(role, 'Review the delta.')

    def test_source_hyphen_compound_named_in_workitem_has_no_hits(self):
        (self.co.context / 'workitem.md').write_text('# Toy\nImplement a codex reader.\n')
        self.t.write('tests/reader.py', 'version = "codex-cli 0.159.2"\n')
        hits, markers = self.patch_markers()
        self.assertEqual(hits, [])
        self.assertEqual(markers, [[], []])

    def test_source_hyphen_compound_not_named_in_workitem_still_hits(self):
        (self.co.context / 'workitem.md').write_text('# Toy\nImplement a reader.\n')
        self.t.write('tests/reader.py', 'version = "codex-cli 0.159.2"\n')
        hits, markers = self.patch_markers()
        self.assertIn(('tests/reader.py:1', 'codex'), hits)
        for found in markers:
            self.assertIn('codex', found)

    def test_json_attribution_still_hits(self):
        self.t.write(FIXTURE, '{"name": "codex", "note": "Codex approved"}\n')
        hits, markers = self.patch_markers()
        self.assertIn((FIXTURE + ':1', 'Codex'), hits)
        for found in markers:
            self.assertIn('Codex', found)
        for role in ('shadow', 'gate'):
            with self.assertRaisesRegex(RuntimeError, 'independence check rejected history'):
                self.co.assert_fresh_prompt(role, 'Review the delta.')


if __name__ == '__main__':
    unittest.main()
