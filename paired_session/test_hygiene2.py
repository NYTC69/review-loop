"""HYGIENE-2 (backlog triage 2026-10-06 §2, B4): the persistent reviewer re-reported a still-open finding as a new id
(F003 came back as F006). Its prompt now says not to, and in a worktree-lifecycle run that other roles own unlisted findings."""
import unittest

from paired_session import test_real_coordinator as trc


class OpenFindingPromptTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def finding(self, co, **extra):
        co.record_findings(extra.pop('source', 'persistent-reviewer'), 'EXEC', 3, [
            {'severity': 'MINOR', 'file': 'sum_ints.py', 'summary': 'helper name is vague', 'failure_scenario': 'readability',
             **extra}])

    def test_listed_findings_must_not_be_reported_again(self):
        co = self.h.coordinator()
        self.finding(co)
        prompt = co.open_findings_prompt()
        self.assertIn('Never report a listed finding again as a new one', prompt)
        self.assertNotIn('Other roles own further open findings', prompt)   # nothing hidden outside a W run

    def test_findings_owned_by_other_roles_are_named_as_not_to_be_reported_again(self):
        co = self.h.coordinator('--lifecycle-mode', 'on')
        self.finding(co, source='specialist:pr-test-analyzer')
        prompt = co.open_findings_prompt()
        self.assertIn('Other roles own further open findings that are not listed here', prompt)
        self.assertNotIn('F001', prompt)                                       # still not listed for the reviewer to close


if __name__ == '__main__':
    unittest.main()
