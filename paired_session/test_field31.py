"""FIELD-31 (poker-tools ui02 run-01, v2.12.4, EXEC r3 after an operator reject): the run HELD with "shadow independence
check rejected history in context/delta-since-last-review.patch: F010". The persistent author had written the ledger id into
its own code: a test title "... account-scoped page state (F010)" and a comment "// F010: Range keeps ...". The author prompt
now forbids that, and review history in the run's own change is a blocking author finding that costs a normal EXEC round;
every other source, and the last EXEC round, still HOLD."""
import unittest
from unittest.mock import patch

from paired_session import test_field23_repo_text_scan as f23

rc = f23.rc
TEST = 'web/players.test.js'
WITH_IDS = ("describe('Players account-scoped page state (F010)', () => {\n"
            "  // F010: Range keeps the selection after a cancel.\n"
            "  it('keeps the list', () => {});\n});\n")
REWORDED = ("describe('Players account-scoped page state', () => {\n"
            "  // Range keeps the selection after a cancel.\n"
            "  it('keeps the list', () => {});\n});\n")


class AuthorDeltaHistoryTests(unittest.TestCase):
    def setUp(self):
        self.t = f23.RepoTextScanTests('test_history_in_a_new_file_holds')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.co = self.t.coordinator()
        self.co.state['exec_rounds'] = 1
        self.co.save()
        self.shadows = []

    def invoke(self, dispositions=(), shadow_scans=False):
        co = self

        def fake(role, phase, prompt, schema, fresh=False, **kwargs):
            snap = rc.git_snapshot(co.t.ws)[0]
            co.co.state['sequence'] += 1
            if role == 'shadow':
                co.shadows.append(prompt)
                if shadow_scans:
                    co.co.assert_fresh_prompt('shadow', 'Review the delta.')   # the real launch check
                return {'answer': {'status': 'APPROVE', 'reviewed_snapshot': snap, 'full_review': []},
                        'snapshot': snap, 'sequence': co.co.state['sequence'], 'role': 'shadow'}
            return {'answer': {'status': 'APPROVE', 'reviewed_snapshot': snap, 'prior_findings': list(dispositions),
                               'full_review': [], 'self_run_evidence': [{'command': 'npm test'}]},
                    'snapshot': snap, 'sequence': co.co.state['sequence'], 'role': 'reviewer'}
        return patch.object(self.co, 'invoke', side_effect=fake)

    def review(self, **kwargs):
        with self.invoke(**kwargs), patch.object(self.co, 'render'):
            self.co.reviewer_turn()

    def scan_rows(self):
        return [row for row in self.co.state['finding_ledger'] if row['source'] == 'independence-scan']

    def test_ids_in_the_authors_test_title_and_comment_become_an_author_finding_and_the_run_continues(self):
        self.t.write(TEST, WITH_IDS)
        self.review(shadow_scans=True)                                       # before FIELD-31: the field HOLD at the shadow
        self.assertEqual((self.co.state['status'], self.co.state['next']), ('ACTIVE', 'author'))
        self.assertEqual(self.shadows, [])                                   # no fresh role saw it, no HOLD
        [row] = self.scan_rows()
        self.assertEqual((row['severity'], row['file'], row['status']), ('MAJOR', f'{TEST}:1', 'open'))
        self.assertIn("'F010'", self.co.state['delivered_review'])
        self.assertIn(f'{TEST}:1', self.co.state['delivered_review'])
        self.t.write(TEST, REWORDED)                                         # the author's reword, in the next round
        self.co.state.update(next='reviewer', exec_rounds=2)
        self.review(dispositions=[{'id': row['id'], 'disposition': 'fixed', 'evidence': 'ids removed'}], shadow_scans=True)
        self.assertEqual(len(self.shadows), 1)                               # the shadow launched
        self.assertEqual((self.co.state['status'], self.co.state['next']), ('ACTIVE', 'gate'))
        self.assertEqual(self.scan_rows()[0]['status'], 'fixed')

    def test_ids_in_two_files_are_one_finding_and_one_reword_round_reaches_the_shadow(self):   # FIELD-31b
        self.t.write(TEST, WITH_IDS)
        self.t.write('web/range.js', 'export function keep(range) {\n  // F010: keep the range.\n  return range;\n}\n')
        self.review(shadow_scans=True)
        [row] = self.scan_rows()
        self.assertEqual(row['file'], f'{TEST}:1')
        for where in (f'{TEST}:1', f'{TEST}:2', 'web/range.js:2'):
            self.assertIn(f"'F010' at {where}", row['summary'])
            self.assertIn(where, self.co.state['delivered_review'])
        self.t.write(TEST, REWORDED)                                         # one round fixes both files
        self.t.write('web/range.js', 'export function keep(range) {\n  // Keep the range.\n  return range;\n}\n')
        self.co.state.update(next='reviewer', exec_rounds=2)
        self.review(dispositions=[{'id': row['id'], 'disposition': 'fixed', 'evidence': 'ids removed'}], shadow_scans=True)
        self.assertEqual(len(self.shadows), 1)
        self.assertEqual((self.co.state['status'], self.co.state['next']), ('ACTIVE', 'gate'))

    def test_the_hit_list_is_bounded(self):   # FIELD-31b
        self.t.write('web/many.js', ''.join(f'// F010 note {n}\n' for n in range(12)))
        self.review(shadow_scans=True)
        [row] = self.scan_rows()
        self.assertEqual(row['summary'].count("'F010' at web/many.js:"), 10)
        self.assertIn(', +2 more)', row['summary'])

    def test_the_last_exec_round_still_holds_at_the_shadow(self):
        self.t.write(TEST, WITH_IDS)
        self.co.state['exec_rounds'] = self.co.exec_round_limit()
        with self.assertRaisesRegex(RuntimeError, r'shadow independence check rejected history in context/delta.*: F010'):
            self.review(shadow_scans=True)
        self.assertEqual(self.scan_rows(), [])

    def test_history_outside_the_change_still_holds(self):
        (self.co.context / 'plan.md').write_text('# Plan\n1. Fix F010 from the earlier review.\n')
        with self.assertRaisesRegex(RuntimeError, 'shadow independence check rejected (history|ledger ids) in context/plan.md'):
            self.review(shadow_scans=True)
        self.assertEqual(self.scan_rows(), [])

    def test_without_a_fresh_role_nothing_is_converted(self):
        self.t.write(TEST, WITH_IDS)
        self.co.args.shadow = self.co.args.adversarial_gate = 'off'
        self.review()
        self.assertEqual(self.scan_rows(), [])

    def test_the_author_prompt_carries_the_rule(self):
        self.co.state.update(next='author')
        self.assertIn(rc.AUTHOR_HISTORY_RULE, self.co._author_prompt())
        self.assertIn('Never write finding ids', rc.AUTHOR_HISTORY_RULE)


if __name__ == '__main__':
    unittest.main()
