"""V304: bounded normal-use regressions, not a malicious-evasion boundary."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from paired_session import coordinator as rc
from paired_session import worktree_lifecycle as wl
from paired_session import test_v303a as v303a


class FreshInputTests(unittest.TestCase):
    setUp = v303a.FrozenProductNameTests.setUp
    freeze = v303a.FrozenProductNameTests.freeze
    markers = v303a.FrozenProductNameTests.markers

    def test_hyphenated_model_id_exempts_bare_code_name(self):
        self.freeze('Implement gpt-6-astra.')
        self.assertEqual(self.markers('ASTRA = "astra"'), [])
        for line in ('Astra approved this', 'readers/astra/cache', '/opt/astra-tools/'):
            with self.subTest(line=line):
                self.assertTrue(self.markers(line))

    def test_delta_since_last_review_exemption(self):
        self.freeze('Implement gpt-6-astra.')
        with patch.object(self.co, '_git_names', return_value=[]):
            self.assertEqual(self.markers('ASTRA = "astra"', 'context/delta-since-last-review.patch'), [])

    def test_missing_frozen_work_item_does_not_exempt(self):
        self.assertEqual(self.markers('ASTRA = "astra"'), ['ASTRA', 'astra'])

    def test_full_gate_fresh_prompt_reads_added_astra(self):
        self.freeze('Implement the Astra reader.')
        self.co.workitem.write_text('Implement the Astra reader.')
        gate = self.co.run_dir / 'gate.md'
        gate.write_text('Review this change.')
        self.co.args.gate_prompt = str(gate)
        self.co.state.update(finding_ledger=[], sequence=0)
        self.co.evidence.mkdir()
        (self.co.context / 'delta.patch').write_text(
            'diff --git a/reader.py b/reader.py\nnew file mode 100644\n'
            '--- /dev/null\n+++ b/reader.py\n@@ -0,0 +1 @@\n+ASTRA = "astra"\n')
        with patch.object(self.co, '_mask_guidance', side_effect=lambda text: text):
            self.co.assert_fresh_prompt('gate', 'Review this change.')
        self.assertTrue((self.co.evidence / '001-gate.independence-inputs.json').is_file())


class DocsScopeTests(unittest.TestCase):
    def test_writer_supports_results_read_in_this_turn(self):
        for docs_file in (None, 'CHANGELOG.md'):
            prompt = wl.docs_prompt(docs_file, ['README.md'], 'v304', 'Update docs.')
            self.assertIn('a command you ran or results you read yourself in this turn', prompt)
            self.assertIn('never claim tests, checks or results that were not observed', prompt)

    def test_reviewer_scopes_major_rule_and_keeps_nonexistent_results_blocking(self):
        co = object.__new__(rc.Coordinator)
        co.args = SimpleNamespace(test_command='python3 -m unittest')
        with patch.object(co, 'materialize_review_context'), \
                patch.object(co, '_change_noun', return_value='this change'), \
                patch.object(co, '_changed_paths', return_value=[]), \
                patch.object(co, '_review_protocol', return_value=''), \
                patch.object(co, '_docs_budget'), \
                patch.object(rc.opv, 'prompt_block', return_value=''), \
                patch.object(co, 'invoke', side_effect=RuntimeError('captured')) as invoke:
            with self.assertRaisesRegex(RuntimeError, 'captured'):
                co._docs_review_turn('tree', ['README.md'])
        prompt = invoke.call_args.args[2]
        self.assertIn('statement written by the DOCS stage', prompt)
        self.assertIn('Documentation written by the DOCS stage: README.md', prompt)
        self.assertIn('Any claimed test, check or result that does not exist is also at least MAJOR, '
                      'regardless of who wrote it', prompt)
        self.assertIn('Judge other inaccurate documentation with normal severity judgement', prompt)


class EvidenceFallbackTests(unittest.TestCase):
    setUp = v303a.FrozenProductNameTests.setUp

    def test_typed_workspace_read_passes_without_fallback(self):
        calls = [{'tool': 'Read', 'input': {'file_path': str(self.co.workspace / 'tests/evidence/a.txt')}}]
        fallbacks = []
        self.assertIsNone(rc.sensitive_access(calls, 'reviewer', self.co.evidence,
                                             self.co.run_dir / 'rounds', self.co.workspace,
                                             fallbacks=fallbacks))
        self.assertEqual(fallbacks, [])

    def test_unknown_inputs_keep_run_prefixes_without_workspace_false_positives(self):
        rounds = self.co.run_dir / 'rounds'
        for path, expected in ((self.co.workspace / 'tests/evidence/a.txt', None),
                               (self.co.evidence, 'evidence directory'),
                               (self.co.evidence / 'a.txt', 'evidence directory'),
                               (rounds / 'a.md', 'review output directory'),
                               (self.co.run_dir / 'other/a.txt', None)):
            with self.subTest(path=path):
                calls = [{'tool': 'unknown_tool', 'input': {'path': str(path)}}]
                fallbacks = []
                hybrid = rc.sensitive_access(calls, 'reviewer', self.co.evidence, rounds,
                                             self.co.workspace, fallbacks=fallbacks)
                if expected:
                    self.assertIsNotNone(hybrid)
                else:
                    self.assertIsNone(hybrid)
                self.assertEqual(rc._legacy_sensitive_access(calls, 'reviewer', self.co.evidence, rounds), expected)
        for operand in ('evidence/a', './evidence/a', '~/evidence/a', '../evidence/a', 'x/../evidence/a'):
            calls = [{'tool': 'unknown_tool', 'input': {'command': 'cat ' + operand}}]
            self.assertEqual(rc._legacy_sensitive_access(calls, 'reviewer', self.co.evidence, rounds),
                             'evidence directory')
        for operand in ('tests/evidence/a', 'pn/evidence/a'):
            calls = [{'tool': 'unknown_tool', 'input': {'command': 'cat ' + operand}}]
            self.assertIsNone(rc._legacy_sensitive_access(calls, 'reviewer', self.co.evidence, rounds))
        with patch.object(rc.evidence_guard, 'turn_verdicts', side_effect=RuntimeError):
            calls = [{'tool': 'Read', 'input': {'file_path': str(self.co.evidence / 'a.txt')}}]
            self.assertEqual(rc.sensitive_access(calls, 'reviewer', self.co.evidence, rounds), 'evidence directory')


if __name__ == '__main__':
    unittest.main()
