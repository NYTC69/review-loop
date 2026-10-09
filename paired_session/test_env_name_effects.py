"""ENV-NAME (docs/env-name-effects.md): a change in the secret env-var names a shell carries feeds the Claude sandbox's denied-env list,
so it changes every flags digest. These tests pin the fixes that need no owner decision: a non-PASS probe report stays current negative
evidence (an acceptance never overrides it), and current negative evidence is never bypassed."""
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import coordinator as rc
from paired_session import test_operator_roles as tor

NEW = {'ENVNAME_NEW_SESSION_TOKEN': 'x'}


class NegativeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.t = tor.ProbeSkipTests('test_a_digest_change_voids_the_acceptance_for_good')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def bound_report(self, co, status='FAIL'):
        report = {'status': status, 'probe_turn': 3, 'reviewer_flags': co.reviewer_flags(), 'reviewer_flags_digest': co.reviewer_flags_digest(),
                  'author_flags_digest': co.author_flags_digest(), 'gate_flags_digest': co.gate_flags_digest()}
        raw = json.dumps(report).encode()
        (co.run_dir / 'permission-probe.json').write_bytes(raw)
        co.state['permission_probe'] = {'sha256': hashlib.sha256(raw).hexdigest(), 'turn': 3}
        co.save()

    def test_a_failed_report_stays_current_when_only_the_names_change(self):
        co = self.t.co()
        self.bound_report(co)
        self.assertEqual(co._probe_negative_status(), 'FAIL')
        with patch.dict(os.environ, NEW):
            self.assertEqual(co._probe_negative_status(), 'FAIL')     # before ENV-NAME: '' (the FAIL was hidden)
            refused = self.t.cli('run', '--accept-probe-skip', '--reason', 'x')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('the current permission probe is FAIL', refused.stdout)
        self.assertNotIn('probe_skip_override', self.t.state())

    def test_a_real_flags_change_still_makes_the_report_stale(self):
        use_lifecycle_on(self, self.t.h)
        co = self.t.co()
        self.bound_report(co)
        co.args.reviewer_effort = 'high'
        with patch.dict(os.environ, NEW):
            self.assertEqual(co._probe_negative_status(), '')
        self.assertIsNone(co._probe_env_names)

    def test_a_pass_is_never_negative_evidence(self):
        use_lifecycle_on(self, self.t.h)
        co = self.t.co()
        self.bound_report(co, 'PASS')
        with patch.dict(os.environ, NEW):
            self.assertEqual(co._probe_negative_status(), '')


if __name__ == '__main__':
    unittest.main()
