"""V302-W: DOCS prompts require observed facts and blocking false-claim findings."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc
from paired_session import worktree_lifecycle as wl


class DocsObservedFactsTests(unittest.TestCase):
    def test_docs_prompt_requires_supported_observed_facts_with_and_without_entry(self):
        for docs_file in ('CHANGELOG.md', None):
            with self.subTest(docs_file=docs_file):
                prompt = wl.docs_prompt(docs_file, ['README.md'], 'run-v302w', 'Update docs.')
                self.assertIn('Write only facts supported by the diff, the worktree or a command you ran in this turn',
                              prompt)
                self.assertIn('never claim tests, checks or results that were not observed', prompt)
                self.assertNotIn('review results', prompt)
                if docs_file:
                    self.assertIn('with the work item and the changes', prompt)
                    self.assertIn('Do not state test, check, review or security results unless you ran or read them '
                                  'yourself in this turn and can name them', prompt)
                    self.assertIn('final review, security and commit facts belong to the delivery report, not the docs',
                                  prompt)
                else:
                    self.assertNotIn('Add one entry for run', prompt)

    def test_docs_reviewer_prompt_makes_false_or_unsupported_statements_at_least_major(self):
        co = rc.Coordinator.__new__(rc.Coordinator)
        co.args = SimpleNamespace(test_command='python3 -m unittest')
        with patch.object(co, 'materialize_review_context'), \
                patch.object(co, '_change_noun', return_value='this uncommitted change'), \
                patch.object(co, '_changed_paths', return_value=['README.md']), \
                patch.object(co, '_review_protocol', return_value='Review protocol.'), \
                patch.object(co, '_docs_budget'), \
                patch.object(rc.opv, 'prompt_block', return_value=''), \
                patch.object(co, 'invoke', side_effect=RuntimeError('prompt captured')) as invoke:
            with self.assertRaisesRegex(RuntimeError, 'prompt captured'):
                co._docs_review_turn('candidate-tree', ['README.md'])
        role, phase, prompt, _ = invoke.call_args.args
        self.assertEqual((role, phase), ('reviewer', 'DOCS'))
        self.assertTrue(invoke.call_args.kwargs['fresh'])
        self.assertIn('A documentation statement that is false or not supported by the diff or an observed result '
                      '(for example a claimed test or check that does not exist) is at least MAJOR.', prompt)


if __name__ == '__main__':
    unittest.main()
