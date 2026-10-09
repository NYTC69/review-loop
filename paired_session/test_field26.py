"""FIELD-26 (LG2 closing check, PR #6): the fresh shadow held at EXEC r1 with "shadow independence check rejected history in
context/delta.patch: codex". The PR adds `.codex-plugin/plugin.json`, whose content names the vendor; FIELD-23 exempts only
text already at the base, never a new file. In a review-only run the change as created is the user's code, so it is
exempt; text a later fix round adds is scanned as before. Second part: review-report.md lists every open finding (the
run's LOW F003 was missing)."""
import subprocess
import unittest

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import review_report
from paired_session import test_real_coordinator as trc

rc = trc.rc
PLUGIN = '.codex-plugin/plugin.json'
PLUGIN_TEXT = '{\n  "name": "review-loop",\n  "keywords": ["codex", "claude"],\n  "longDescription": "Codex and Claude roles"\n}\n'
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli', 'fake_claude_cli', 'command')


class ReviewOnlyCreationTextTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def write(self, name, text):
        (self.workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (self.workspace / name).write_text(text)

    def coordinator(self, *flags):
        command = self.command(*flags)
        return rc.Coordinator(rc.parser().parse_args(command[2:]))

    def scan(self, co, role):
        co.materialize_review_context()
        co.assert_fresh_prompt(role, 'Review the delta.')

    def test_a_review_only_change_that_names_a_vendor_passes_the_shadow_and_gate_scan(self):
        self.write(PLUGIN, PLUGIN_TEXT)   # untracked: a --no-index section
        self.write('sum_ints.py', 'def sum_ints(values):\n    # works with Codex and Claude roles\n    return sum(values)\n')
        co = self.coordinator('--review-only', '--lifecycle-mode', 'on')
        for role in ('shadow', 'gate'):
            self.scan(co, role)
        self.assertIn('+  "keywords": ["codex", "claude"],', (co.context / 'delta.patch').read_text())

    def test_a_committed_pr_shape_passes_too(self):   # the materialized clone: the change is base..HEAD, all committed
        self.write(PLUGIN, PLUGIN_TEXT)
        subprocess.run(['git', 'add', PLUGIN], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add the Codex plugin manifest'], cwd=self.workspace, check=True)
        self.write('notes.md', 'notes\n')
        co = self.coordinator('--review-only', '--base', 'HEAD~1', '--lifecycle-mode', 'on', '--review-report')
        self.scan(co, 'shadow')

    def test_the_same_words_added_after_creation_are_still_caught(self):
        self.write(PLUGIN, PLUGIN_TEXT)
        co = self.coordinator('--review-only', '--lifecycle-mode', 'on')
        self.write('fix.py', 'x = 1  # Codex\n')   # a fix round's new text: not in the change as created
        with self.assertRaisesRegex(RuntimeError, r'shadow independence check rejected history in context/delta\.patch: Codex'):
            self.scan(co, 'shadow')
        (self.workspace / 'fix.py').unlink()
        self.write(PLUGIN, PLUGIN_TEXT + '// Codex approved this\n')   # in the created file, but a new whole match
        with self.assertRaisesRegex(RuntimeError, 'gate independence check rejected history'):
            self.scan(co, 'gate')

    def test_an_ordinary_run_is_unchanged(self):
        use_lifecycle_on(self, self)
        co = self.coordinator()
        self.write(PLUGIN, PLUGIN_TEXT)
        with self.assertRaisesRegex(RuntimeError, 'shadow independence check rejected history in context/delta.patch'):
            self.scan(co, 'shadow')


class EveryOpenFindingIsReportedTests(unittest.TestCase):
    def test_low_high_medium_and_unknown_severities_are_rendered(self):
        rows = [{'id': f'F00{n}', 'severity': severity, 'source': source, 'phase': 'EXEC', 'file': 'a.py', 'summary': severity.lower(),
                 'status': 'open'} for n, (severity, source) in enumerate(
                     (('MINOR', 'persistent-reviewer'), ('LOW', 'persistent-reviewer'), ('HIGH', 'adversarial-gate'),
                      ('MEDIUM', 'adversarial-gate'), ('INFO', 'fresh-shadow')), 1)]
        text = review_report.render({'report': {'complete': True}, 'turns': [], 'config': {}, 'finding_ledger': rows})
        for row in rows:
            self.assertIn(f"- **{row['id']}** ", text)
        self.assertIn('## Suggestions (2)', text)
        self.assertIn('- **F002** [EXEC reviewer, EXEC, LOW]', text)
        self.assertIn('## Important (2)', text)
        self.assertIn('- **F003** [gate, EXEC, HIGH]', text)
        self.assertIn('## Other (1)', text)
        self.assertIn('- **F001** [EXEC reviewer, EXEC] ', text)   # a canonical severity shows no tag
        self.assertNotIn('## Other', review_report.render({'report': {'complete': True}, 'turns': [], 'config': {},
                                                           'finding_ledger': rows[:1]}))


if __name__ == '__main__':
    unittest.main()
