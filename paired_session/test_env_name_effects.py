"""ENV-NAME (docs/env-name-effects.md): a change in the secret env-var names a shell carries feeds the Claude sandbox's denied-env list,
so it changes every flags digest. These tests pin the fixes that need no owner decision: a non-PASS probe report stays current negative
evidence (an acceptance never overrides it), and a voided operator acceptance says that only the names changed and is reported."""
import hashlib
import json
import os
import unittest
from unittest.mock import patch

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
        co = self.t.co()
        self.bound_report(co)
        co.args.reviewer_effort = 'high'
        with patch.dict(os.environ, NEW):
            self.assertEqual(co._probe_negative_status(), '')
        self.assertIsNone(co._probe_env_names)

    def test_a_pass_is_never_negative_evidence(self):
        co = self.t.co()
        self.bound_report(co, 'PASS')
        with patch.dict(os.environ, NEW):
            self.assertEqual(co._probe_negative_status(), '')


class VoidCauseTests(unittest.TestCase):
    def setUp(self):
        self.a = tor.ClaudeAuthorTests('test_the_opt_in_is_operator_only_and_needs_a_reason')
        self.a.setUp()
        self.addCleanup(self.a.doCleanups)

    def test_an_opt_in_voided_by_a_new_name_says_so(self):
        self.a.cli('run', '--author-vendor', 'claude', *tor.OPT_IN)
        record = self.a.state()['claude_author_override']
        self.assertEqual(record['secret_env_names'], self.a.co()._env_names_now())
        self.assertNotIn('ENVNAME_NEW_SESSION_TOKEN', record['secret_env_names'])
        with patch.dict(os.environ, NEW):
            ok, message = self.a.co().claude_author_verified()
        self.assertFalse(ok)                                                 # the void itself is unchanged (D-ENV-1)
        voided = self.a.state()['claude_author_override']['voided']
        self.assertEqual((voided['cause'], voided['names_added'], voided['names_removed']),
                         ('secret env-var names changed', ['ENVNAME_NEW_SESSION_TOKEN'], []))
        self.assertIn('opt-in is void (only the secret env-var names changed: added ENVNAME_NEW_SESSION_TOKEN; removed none)', message)

    def test_a_vanished_name_is_reported_as_removed(self):
        with patch.dict(os.environ, NEW):
            self.a.cli('run', '--author-vendor', 'claude', *tor.OPT_IN)
        ok, message = self.a.co().claude_author_verified()
        self.assertFalse(ok)
        self.assertIn('added none; removed ENVNAME_NEW_SESSION_TOKEN', message)

    def test_a_real_flags_change_and_an_old_record_say_flags_changed(self):
        self.a.cli('run', '--author-vendor', 'claude', *tor.OPT_IN)
        co = self.a.co()
        with patch.dict(os.environ, NEW), patch.object(co, 'author_flags_digest', return_value='f' * 64):
            self.assertFalse(co.claude_author_verified()[0])
        self.assertEqual(self.a.state()['claude_author_override']['voided']['cause'], 'flags changed')
        self.assertIn('opt-in is void (flags changed)', self.a.co().claude_author_verified()[1])
        co = self.a.co()
        co.state['claude_author_override'] = {'actor': 'operator', 'reason': 'x', 'author_flags_digest': co.author_flags_digest(), 'time': 'T'}
        with patch.dict(os.environ, NEW):                                    # a record from before ENV-NAME carries no names
            self.assertFalse(co._claude_optin_current())
        self.assertEqual(co.state['claude_author_override']['voided']['cause'], 'flags changed')
        self.assertIsNone(co._probe_env_names)


class SkipVoidReportTests(unittest.TestCase):
    def setUp(self):
        self.t = tor.ProbeSkipTests('test_a_digest_change_voids_the_acceptance_for_good')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def test_a_voided_acceptance_is_reported_with_its_cause(self):
        self.assertEqual(self.t.cli('run', '--accept-probe-skip', '--reason', 'checked').returncode, 0)
        self.assertEqual(self.t.state()['probe_skip_override']['secret_env_names'], self.t.co()._env_names_now())
        self.assertEqual(self.t.co().probe_gate(), (True, ''))
        with patch.dict(os.environ, NEW):
            passed, reason = self.t.co().probe_gate()
            refused = self.t.cli('run')
        self.assertFalse(passed)
        note = ('the earlier --accept-probe-skip is void (only the secret env-var names changed: added ENVNAME_NEW_SESSION_TOKEN; removed none; '
                'pass --accept-probe-skip --reason TEXT again or run permission-probe)')
        self.assertIn(note, reason)
        self.assertEqual(refused.returncode, 2)
        self.assertIn(note, refused.stdout)
        self.assertEqual(self.t.state()['probe_skip_override']['voided']['cause'], 'secret env-var names changed')

    def test_a_real_flags_change_says_flags_changed(self):
        self.t.cli('run', '--accept-probe-skip', '--reason', 'checked')
        co = self.t.co()
        co.args.reviewer_effort = 'high'
        self.assertIn('the earlier --accept-probe-skip is void (flags changed;', co.probe_gate()[1])

    def test_a_void_from_a_probe_re_run_keeps_its_own_reason(self):   # permission_probe voids it with {'reason': 'permission-probe re-run'}
        co = self.t.co()
        co.state['probe_skip_override'] = {'actor': 'operator', 'reason': 'x', 'reviewer_flags_digest': co.reviewer_flags_digest(),
                                           'author_flags_digest': co.author_flags_digest(), 'gate_flags_digest': co.gate_flags_digest(),
                                           'voided': {'time': 'T', 'reason': 'permission-probe re-run'}}
        self.assertIn('the earlier --accept-probe-skip is void (permission-probe re-run; pass --accept-probe-skip', co.probe_gate()[1])


if __name__ == '__main__':
    unittest.main()
