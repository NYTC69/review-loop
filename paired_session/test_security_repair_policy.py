import hashlib
import unittest

from paired_session.security_repair_policy import plan_security_repair, repair_digest


RUN = 'run-123'
OID = 'a' * 40


def operator_receipt(proposals, repair_paths=(), *, oid=OID, run_id=RUN):
    digest = repair_digest(run_id, oid, proposals, repair_paths)
    command = f'confirm-ignore --digest {digest}'
    return {'decision': 'confirm', 'digest': digest, 'run_id': run_id,
            'actor': 'operator', 'time': '2026-09-28T17:00:00+09:00',
            'command_sha256': hashlib.sha256(command.encode()).hexdigest()}


class SecurityRepairPolicyTests(unittest.TestCase):
    def plan(self, proposals=(), repair_paths=(), *, allowed=('.gitignore',),
             reserved=(), candidate_files=None, consent=None, oid=OID, run_id=RUN):
        if candidate_files is None:
            candidate_files = tuple(allowed) + tuple(
                path for _, _, matches in proposals for path in matches)
        return plan_security_repair(run_id, oid, proposals, repair_paths,
                                    allowed, reserved, candidate_files, consent)

    def test_broad_ignore_requires_exact_operator_receipt(self):
        for pattern in ('*credentials*', '*secret*'):
            category = 'Cloud credentials' if pattern == '*credentials*' else 'Generic secret files'
            proposals = [(category, pattern, [])]
            with self.subTest(pattern=pattern):
                with self.assertRaisesRegex(ValueError, 'operator confirmation'):
                    self.plan(proposals)
                receipt = operator_receipt(proposals)
                result = self.plan(proposals, consent=receipt)
                self.assertEqual(result.digest, receipt['digest'])
                self.assertEqual(result.fixer_paths, ('.gitignore',))
                self.assertEqual(result.next_phase, 'EXEC')
                self.assertFalse(result.gate_ran)
                self.assertEqual(result.invalidated_receipts, ('*',))
                self.assertTrue(result.security_review_required)
                for field, value in (
                        ('decision', 'decline'), ('digest', '0' * 64),
                        ('run_id', 'other-run'), ('actor', 'writer'),
                        ('time', ''), ('command_sha256', '0' * 64)):
                    with self.subTest(field=field), self.assertRaisesRegex(
                            ValueError, 'operator confirmation|operator declined'):
                        self.plan(proposals, consent={**receipt, field: value})
                with self.assertRaisesRegex(ValueError, 'operator confirmation'):
                    self.plan(proposals, consent=receipt, oid='b' * 40)
                with self.assertRaisesRegex(ValueError, 'operator confirmation'):
                    self.plan(proposals, consent=receipt, run_id='other-run')

    def test_nonbroad_unmatched_can_autofix_but_tracked_match_needs_consent(self):
        unmatched = [('Keys & certificates', '*.pem', [])]
        result = self.plan(unmatched)
        self.assertEqual(result.fixer_paths, ('.gitignore',))
        self.assertEqual(result.approved_patterns, (('Keys & certificates', '*.pem'),))
        self.assertEqual(result.consent_digest, '')
        self.assertEqual(result.next_phase, 'EXEC')
        self.assertEqual(result.invalidated_receipts, ('*',))
        tracked = [('Keys & certificates', '*.pem', ['src/old.pem'])]
        with self.assertRaisesRegex(ValueError, 'operator confirmation'):
            self.plan(tracked)
        receipt = operator_receipt(tracked)
        self.assertEqual(self.plan(tracked, consent=receipt).digest, receipt['digest'])
        with self.assertRaisesRegex(ValueError, 'tracked matches differ'):
            self.plan([('Keys & certificates', '*.key', ['src/old.pem'])], consent=receipt)
        with self.assertRaisesRegex(ValueError, 'operator confirmation'):
            self.plan([('Keys & certificates', '*.pem', ['src/other.pem'])], consent=receipt)
        with self.assertRaisesRegex(ValueError, 'tracked matches differ'):
            self.plan(unmatched, candidate_files=['.gitignore', 'src/old.pem'])
        with self.assertRaisesRegex(ValueError, 'tracked matches differ'):
            self.plan([('Cloud credentials', '.aws/', [])],
                      candidate_files=['.gitignore', 'nested/.aws/credentials'])

    def test_every_repair_requires_exact_grant_and_exec_replay(self):
        result = self.plan((), ['src/app.py'], allowed=['src/app.py'])
        self.assertEqual(result.fixer_paths, ('src/app.py',))
        self.assertEqual(result.next_phase, 'EXEC')
        self.assertFalse(result.gate_ran)
        self.assertEqual(result.invalidated_receipts, ('*',))
        with self.assertRaisesRegex(ValueError, 'exact candidate-file grant'):
            self.plan((), ['src/app.py'], allowed=['src/other.py'])
        with self.assertRaisesRegex(ValueError, 'exact candidate-file grant'):
            self.plan([('Keys & certificates', '*.pem', [])], allowed=['src/app.py'])
        with self.assertRaisesRegex(ValueError, 'reserved docs repair'):
            self.plan((), ['docs/guide.md'], allowed=['docs/guide.md'],
                      reserved=['docs/guide.md'])
        with self.assertRaisesRegex(ValueError, 'ignore repair requires frozen proposals'):
            self.plan((), ['.gitignore'])
        with self.assertRaisesRegex(ValueError, 'exact candidate-file grant'):
            self.plan()

    def test_frozen_patterns_and_ignore_paths_cannot_bypass_consent(self):
        for pattern in ('**/*secret*', '*secret* ', '*', '!*.pem'):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, 'frozen policy'):
                self.plan([('Generic secret files', pattern, [])])
        for pattern in ('service-account*.json', '!*credentials.example*'):
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValueError, 'operator confirmation'):
                self.plan([('Cloud credentials', pattern, [])])
        with self.assertRaisesRegex(ValueError, 'ignore repair requires frozen proposals'):
            self.plan((), ['src/.gitignore'], allowed=['src/.gitignore'])
        with self.assertRaisesRegex(ValueError, 'exact candidate-file grant'):
            self.plan((), ['src'], allowed=['src'], candidate_files=['src/app.py'])

    def test_reserved_docs_prefix_overlap_and_decline(self):
        with self.assertRaisesRegex(ValueError, 'reserved docs repair'):
            self.plan((), ['docs'], allowed=['docs'], reserved=['docs/guide.md'])
        with self.assertRaisesRegex(ValueError, 'reserved docs repair'):
            self.plan((), ['docs/guide.md'], allowed=['docs/guide.md'], reserved=['docs'])
        proposals = [('Cloud credentials', '*credentials*', [])]
        receipt = operator_receipt(proposals)
        with self.assertRaisesRegex(ValueError, 'operator declined'):
            self.plan(proposals, consent={**receipt, 'decision': 'decline'})
        self.assertEqual(self.plan(proposals, consent=receipt).consent_digest, receipt['digest'])

    def test_malformed_proposal_or_oid_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'candidate OID'):
            self.plan([('Keys & certificates', '*.pem', [])], oid='bad')
        for proposals in ([('Keys & certificates', '*.pem\n*.key', [])],
                          [('Keys & certificates', '*.pem', 'src/x.pem')],
                          [('Keys & certificates',)]):
            with self.subTest(proposals=proposals), self.assertRaises(ValueError):
                self.plan(proposals)
        with self.assertRaises(ValueError):
            self.plan([('Keys & certificates', '*.pem', [])], repair_paths=['../outside'])


if __name__ == '__main__':
    unittest.main()
