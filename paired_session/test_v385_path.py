"""V385 (gg-pipeline WI-413, v3.8.5): a work item that moves a reader to a pinned codex-cli put tool-named paths
(runs_live/codex_0166/, node_modules/.bin/codex, /plan/codex_read_units/) and a `# noqa: F401` into the change. The
independence scan turned each into "review history", listed every added line with the bare word codex as a place
(189), and the run could not reach the gate. Repository subject passes now; review history still does."""
import unittest

from paired_session import test_v326_data as v326

WORKITEM = '# Pin the reader\nSwitch the sol reader to the pinned Codex CLI (codex-cli 0.166.0).\n'
BIN = '/Users/u/runs_live/codex_0166/node_modules/.bin/codex'


class ToolPathTests(v326.DataHistoryTests):
    def about_codex(self):
        (self.co.context / 'workitem.md').write_text(WORKITEM)

    def test_tool_named_paths_pass_when_the_work_item_names_the_tool(self):
        self.about_codex()
        self.t.write('docs/reader.md', f'Run `--sol-codex {BIN}`.\nThe pointer is `/board/log/0/plan/codex_read_units/0/model`.\n')
        self.t.write('tests/test_reader.py', f'CLI = "{BIN}"\nassert provider == "codex"\n')
        hits, markers = self.patch_markers()
        self.assertEqual(hits, [])
        self.assertEqual(markers, [[], []])

    def test_without_the_work_item_naming_it_only_the_path_line_is_listed(self):
        self.t.write('docs/reader.md', 'See reader_backends_codex.\nThe binary lives in /opt/codex_0166/bin.\nplain\n')
        hits, _ = self.patch_markers()
        self.assertIn(('docs/reader.md:2', 'codex'), hits)
        self.assertNotIn(('docs/reader.md:1', 'codex'), hits)   # line 1 only contains the word; the old locator listed it

    def test_tool_state_directories_still_hit(self):
        self.about_codex()
        for path, text in (('docs/home.md', 'Sessions are in /home/x/.codex/sessions/.\n'),
                           ('docs/scratch.md', 'Scratch is /tmp/claude-501/run/.\n')):
            with self.subTest(path=path):
                self.t.write(path, text)
                hits, markers = self.patch_markers()
                self.assertTrue(any(where == path + ':1' for where, _ in hits), hits)
                self.assertTrue(markers[0])

    def test_a_tool_named_directory_before_the_binary_and_a_trailing_attribution_still_hit(self):   # v3.8.5 review
        self.about_codex()
        for path, text in (('docs/a.md', 'Use .codex/bin/codex here.\n'), ('docs/b.md', 'Use .codex/codex here.\n'),
                           ('docs/c.md', 'Use a/codex/b/codex here.\n'), ('docs/d.md', 'GPT/Codex signed off on this.\n'),
                           ('docs/e.md', 'bin/codex rejected it.\n')):
            with self.subTest(path=path):
                self.t.write(path, text)
                hits, markers = self.patch_markers()
                self.assertTrue(any(where == path + ':1' for where, _ in hits), hits)
        for text in ('Use .codex/bin/codex here.', '.claude/hooks/claude runs it.', 'GPT/Codex signed off on the plan.',
                     'Sonnet/Claude rejected the split.', 'reviewer/codex signed off.'):
            with self.subTest(plan=text):
                self.assertTrue(self.co._introduced_history('context/plan.md', text), text)
        self.assertEqual(self.co._introduced_history('context/plan.md', 'Run node_modules/.bin/codex for Codex.'), [])

    def test_noqa_codes_are_not_finding_ids(self):
        self.t.write('tests/test_reader.py', 'from helpers import base  # noqa: F401  (autouse)\nimport os  # NOQA: E501, F811\n')
        hits, markers = self.patch_markers()
        self.assertEqual(hits, [])
        self.assertEqual(markers, [[], []])

    def test_a_finding_id_outside_noqa_and_an_attribution_still_hit(self):
        self.about_codex()
        self.t.write('tests/test_reader.py', 'x = 1  # noqa: F401 -- fixes F042\n# Codex approved this\n')
        hits, _ = self.patch_markers()
        self.assertIn(('tests/test_reader.py:1', 'F042'), hits)
        self.assertTrue(any(where == 'tests/test_reader.py:2' for where, _ in hits), hits)


if __name__ == '__main__':
    unittest.main()
