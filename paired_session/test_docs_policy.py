import unittest
import unicodedata

from paired_session.docs_policy import validate_docs_change


def docs_change(changed, allowed, exec_paths=(), finish_paths=(), polish_paths=(),
                closure_inputs=(), closure_uncertain=False):
    return validate_docs_change(changed, allowed, exec_paths=exec_paths,
                                finish_paths=finish_paths, polish_paths=polish_paths,
                                closure_inputs=closure_inputs,
                                closure_uncertain=closure_uncertain)


class DocsPolicyTests(unittest.TestCase):
    def test_reserved_docs_write_requires_full_rechecks(self):
        result = docs_change(['docs/guide.md'], ['docs/guide.md'])
        self.assertTrue(result.requires_rechecks)
        self.assertEqual(result.invalidated_receipts,
                         ('FINISH', 'POLISH-Q', 'DOCS', 'FINAL-REVIEW', 'TESTS', 'SECURITY'))

    def test_no_docs_write_does_not_invalidate_receipts(self):
        result = docs_change([], ['docs/guide.md'])
        self.assertFalse(result.requires_rechecks)
        self.assertEqual(result.invalidated_receipts, ())

    def test_unreserved_or_prior_write_paths_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'unreserved path'):
            docs_change(['README.md'], ['docs/guide.md'])
        with self.assertRaisesRegex(ValueError, 'EXEC-reviewed path'):
            docs_change(['docs/guide.md'], ['docs/guide.md'], exec_paths=['docs/guide.md'])
        with self.assertRaisesRegex(ValueError, 'EXEC-reviewed path'):
            docs_change(['docs/guide.md'], ['docs/guide.md'], finish_paths=['docs'])

    def test_exec_alias_and_prior_paths_use_separate_validation(self):
        with self.assertRaisesRegex(ValueError, 'EXEC-reviewed path'):
            docs_change(['Docs/Guide.md'], ['Docs/Guide.md'], exec_paths=['docs/guide.md'])
        with self.assertRaisesRegex(ValueError, 'aliases its reserved path'):
            docs_change(['Docs/Guide.md'], ['docs/guide.md'])
        result = docs_change(['docs/guide.md'], ['docs/guide.md'],
                             exec_paths=['skills/x.md', '.gitignore'])
        self.assertTrue(result.requires_rechecks)

    def test_unicode_aliases_and_parent_child_overlap_are_refused(self):
        decomposed = unicodedata.normalize('NFD', 'Docs/Guide.md')
        with self.assertRaisesRegex(ValueError, 'aliases its reserved path'):
            docs_change([decomposed], ['docs/guide.md'])
        with self.assertRaisesRegex(ValueError, 'EXEC-reviewed path'):
            docs_change(['docs/guide.md'], ['docs/guide.md'],
                        closure_inputs=['docs/guide.md/child.md'])

    def test_uncertain_closure_and_non_docs_paths_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'closure is uncertain'):
            docs_change(['docs/guide.md'], ['docs/guide.md'], closure_uncertain=True)
        for uncertain in (None, 0, 1, 'false'):
            with self.subTest(uncertain=uncertain), self.assertRaisesRegex(ValueError, 'boolean'):
                docs_change(['docs/guide.md'], ['docs/guide.md'], closure_uncertain=uncertain)
        for path in ('src/app.py', 'tests/x.py', 'pkg/tests/golden.md',
                     'sub/test/fixtures/expected.txt', 'requirements.txt',
                     'requirements-dev.txt', 'constraints-prod.txt', 'CMakeLists.txt',
                     'pyproject.toml', 'manifest.json', '.gitattributes', '.gitmodules',
                     '.mailmap', 'docs/protocol/x.md'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                docs_change([path], [path])

    def test_path_aliases_globs_and_input_types_are_refused(self):
        for path in ('../secret.md', '/tmp/guide.md', 'docs/*.md', 'docs/.git/config',
                     '.GIT/config', '.git./x', 'skills/x.md', '.claude/x.md',
                     'sub/AGENTS.md', '.gitignore'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                docs_change([path], [path])
        with self.assertRaisesRegex(ValueError, 'list or tuple'):
            validate_docs_change(['docs/guide.md'], ['docs/guide.md'], exec_paths='docs',
                                 finish_paths=[], polish_paths=[], closure_inputs=[],
                                 closure_uncertain=False)


if __name__ == '__main__':
    unittest.main()
