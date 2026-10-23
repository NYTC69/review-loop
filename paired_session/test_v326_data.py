"""V326-DATA: saved reader/provider data is not review history.

Reuse FIELD-31's offline harness (stub CLIs and the real-provider guard).
Both fresh delta inputs must agree with the author's independence scan.
"""
import unittest

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_field23_repo_text_scan as f23

rc = f23.rc
FIXTURE = 'tests/ab/fixtures/wi278/260825/plan.json'
READERS = '"reader": "codex", "provider": "claude"'


class DataHistoryTests(unittest.TestCase):
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

    def test_json_reader_provider_data_has_no_hits(self):
        self.t.write(FIXTURE, '{' + READERS + '}\n')
        hits, markers = self.patch_markers()
        self.assertEqual(hits, [])
        self.assertEqual(markers, [[], []])
        self.assertEqual(self.co._author_delta_history('EXEC', 1, False), [])
        for role in ('shadow', 'gate'):
            self.co.assert_fresh_prompt(role, 'Review the delta.')

    def test_all_data_extensions_exempt_bare_names(self):
        for extension in ('json', 'jsonl', 'ndjson', 'yaml', 'yml', 'csv', 'tsv'):
            with self.subTest(extension=extension):
                self.t.write('fixtures/readers.' + extension, 'codex, claude, Opus, Astra, gpt-6-astra\n')
                hits, markers = self.patch_markers()
                self.assertEqual(hits, [])
                self.assertEqual(markers, [[], []])

    def test_attribution_and_finding_id_on_the_same_line_still_hit(self):
        for sequence, (note, expected) in enumerate((('Codex approved this', 'Codex'), ('F012', 'F012')), 1):
            with self.subTest(note=note):
                self.t.write(FIXTURE, '{' + READERS + ', "note": "' + note + '"}\n')
                hits, markers = self.patch_markers()
                self.assertIn((FIXTURE + ':1', expected), hits)
                for found in markers:
                    self.assertIn(expected, found)
                    self.assertNotIn('claude', found)
                [row] = self.co._author_delta_history('EXEC', sequence, False)
                self.assertEqual(row['severity'], 'MAJOR')
                self.assertIn(repr(expected), row['summary'])
                for role in ('shadow', 'gate'):
                    with self.assertRaisesRegex(RuntimeError, 'independence check rejected history'):
                        self.co.assert_fresh_prompt(role, 'Review the delta.')

    def test_vendor_directory_in_data_still_hits(self):
        self.t.write(FIXTURE, '{' + READERS + ', "path": "/home/x/.codex/sessions/"}\n')
        hits, markers = self.patch_markers()
        self.assertIn((FIXTURE + ':1', 'codex'), hits)
        for found in markers:
            self.assertIn('codex', found)

    def test_verdict_on_the_same_data_line_still_hits(self):
        self.t.write(FIXTURE, '{' + READERS + ', "status": "APPROVE"}\n')
        hits, markers = self.patch_markers()
        self.assertIn((FIXTURE + ':1', 'APPROVE'), hits)
        self.assertEqual(markers, [['APPROVE'], ['APPROVE']])

    def test_source_docs_and_comments_keep_the_existing_rule(self):
        for path, text in (('tests/readers.py', 'assert reader == "codex"\n'),
                           ('docs/readers.md', 'The reader is codex.\n'),
                           ('readers.py', '# reader: codex\n')):
            self.t.write(path, text)
        hits, markers = self.patch_markers()
        for path in ('tests/readers.py', 'docs/readers.md', 'readers.py'):
            self.assertIn((path + ':1', 'codex'), hits)
        for found in markers:
            self.assertIn('codex', found)

    def test_source_keeps_the_workitem_name_exemption(self):
        # The existing exemption reads the frozen work item, not the fixture's contents.
        (self.co.context / 'workitem.md').write_text('# Toy\nImplement a codex reader.\n')
        self.t.write('tests/readers.py', 'assert reader == "codex"\n')
        hits, markers = self.patch_markers()
        self.assertEqual(hits, [])
        self.assertEqual(markers, [[], []])


if __name__ == '__main__':
    unittest.main()
