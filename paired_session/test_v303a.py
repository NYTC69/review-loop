"""V303-A: product model names in the frozen work item are not review history.

Like FIELD-23, this guards accidental carry-over; deliberate evasion is an
accepted residual, not a reason to expand the scanner's trust boundary.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from paired_session import coordinator as rc


class FrozenProductNameTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.co = object.__new__(rc.Coordinator)
        self.co.context = root / 'context'
        self.co.context.mkdir()
        self.co.workspace = root / 'workspace'
        self.co.run_dir = root
        self.co.evidence = root / 'evidence'
        self.co.author_temp_dir = root / 'author-tmp'
        self.co.internal = root / 'internal'
        self.co.workitem = root / 'original.md'
        self.co.workitem.write_text('# Mutable original\nClaude and Codex\n')
        self.co.args = SimpleNamespace(test_command='', reviewer_command=[])
        self.co.state = {'base_commit': 'base', 'config': {'workitem_reviewer_commands': []}}
        self.base_blob = patch.object(self.co, '_base_blob', return_value=None)
        self.base_blob.start()
        self.addCleanup(self.base_blob.stop)

    def freeze(self, text):
        (self.co.context / 'workitem.md').write_text(text)

    def markers(self, text, name='context/delta.patch'):
        if name.endswith('.patch'):
            text = ('diff --git a/pn/ab/key_vote.py b/pn/ab/key_vote.py\n'
                    'new file mode 100644\n--- /dev/null\n+++ b/pn/ab/key_vote.py\n'
                    '@@ -0,0 +1 @@\n+' + text + '\n')
        fresh = self.co._fresh_history_text(name, text)
        return self.co._history_markers(name, fresh)

    def test_new_file_astra_is_exempt_when_the_frozen_work_item_names_it(self):
        self.freeze('# Reader\nImplement the Astra reader model.\n')
        self.assertEqual(self.markers('ASTRA = "astra"'), [])

    def test_new_file_astra_still_reports_when_work_item_does_not_name_it(self):
        self.freeze('# Reader\nImplement the reader model.\n')
        self.assertEqual(self.markers('ASTRA = "astra"'), ['ASTRA', 'astra'])

    def test_attribution_stays_visible_when_the_work_item_names_astra(self):
        self.freeze('# Reader\nImplement Astra.\n')
        self.assertIn('Astra', self.markers('Astra approved this'))

    def test_only_the_named_model_is_exempt_on_a_mixed_line(self):
        self.freeze('# Reader\nImplement Opus.\n')
        self.assertEqual(self.markers('MODELS = ("Opus", "Claude")'), ['Claude'])

    def test_names_require_the_same_whole_word(self):
        for text in ('Astral reader', 'Astra-only reader', 'gpt-6-astra reader'):
            with self.subTest(workitem=text):
                self.freeze(text)
                self.assertIn('Astra', self.markers('Astra'))

    def test_other_history_markers_and_directory_paths_remain_visible(self):
        self.freeze('Implement Astra and Opus.')
        for text, marker in (('Per Astra.ai', 'Astra'), ('/opt/astra-tools/', 'astra'),
                             ('readers/astra/cache', 'astra'), ('Astra/GPT approved this', 'Astra'),
                             ('Astra F042', 'F042'), ('Opus APPROVE', 'APPROVE'),
                             ('Opus REVISE', 'REVISE')):
            with self.subTest(text=text):
                self.assertIn(marker, self.markers(text))

    def test_plan_stat_and_status_share_the_exception(self):
        self.freeze('Implement Astra.')
        for name in ('context/plan.md', 'context/delta.stat', 'context/status.txt'):
            with self.subTest(name=name):
                self.assertEqual(self.co._introduced_history(name, 'Astra'), [])

    def test_plan_model_suffix_attribution_still_reports(self):
        self.freeze('Implement Codex.')
        self.assertIn('Codex', self.markers('Codex/GPT approved the split.', 'context/plan.md'))

    def test_prompt_and_gate_template_keep_their_scan(self):
        self.freeze('Implement Astra.')
        for name in ('prompt', 'gate-template'):
            with self.subTest(name=name), patch.object(self.co, '_mask_guidance', side_effect=lambda text: text):
                self.assertEqual(self.markers('Astra', name), ['Astra'])


if __name__ == '__main__':
    unittest.main()
