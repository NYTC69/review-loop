"""FIELD-10 (poker-tools sw/run-01, v2.9.4): reject on DONE was refused with the generic Claude-author message.

Root cause, from the run's superseded and current probe reports: the reject ran in a different shell. Its
environment had one more secret-looking variable name (CLAUDE_CODE_MESSAGING_TOKEN) and one PATH entry less.
The env-derived credential deny list is part of the reviewer/author flag digests, so probe_passed() refused,
and claude_author_verified() printed only the generic text.
"""
import json
import os
import unittest
from unittest.mock import patch

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import coordinator as rc
from paired_session import test_operator_roles as tor
from paired_session import test_real_coordinator as trc


def env_names(settings):
    return {entry['name'] for entry in settings['sandbox']['credentials']['envVars']}


class SecretEnvNameReuseTests(unittest.TestCase):
    """A Codex author with a Claude reviewer: the reviewer flags carry the env-derived deny list."""

    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        guard = patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False)
        guard.start()
        self.addCleanup(guard.stop)

    def write_report(self, co):
        report = {'status': 'PASS', 'reviewer_flags': co.reviewer_flags(),
                  'reviewer_flags_digest': co.reviewer_flags_digest(), 'author_flags_digest': co.author_flags_digest(),
                  'gate_flags': co.gate_flags(), 'gate_flags_digest': co.gate_flags_digest(),
                  'gate_permission_probe': {'status': 'NOT_NEEDED', 'reason': 'gate vendor equals reviewer vendor'},
                  'author_permission_probe': {'status': 'PASS'}, 'global_config_changes': {'status': 'PASS'}}
        (co.run_dir / 'permission-probe.json').write_text(json.dumps(report))

    def test_a_new_secret_env_name_does_not_void_the_recorded_pass(self):
        use_lifecycle_on(self, self.h)
        co = self.h.coordinator('--gate-vendor', 'claude')
        self.write_report(co)
        self.assertEqual(co.probe_passed(), (True, ''))
        with patch.dict(os.environ, {'FIELD10_NEW_MESSAGING_TOKEN': 'x'}):
            self.assertEqual(co.probe_passed(), (True, ''))
            # the real dispatch still denies the new name: the probe's list is used only for the comparison
            self.assertIn('FIELD10_NEW_MESSAGING_TOKEN', env_names(co._claude_sandbox_settings('reviewer')))

    def test_a_vanished_secret_env_name_does_not_void_the_recorded_pass(self):
        use_lifecycle_on(self, self.h)
        co = self.h.coordinator('--gate-vendor', 'claude')
        with patch.dict(os.environ, {'FIELD10_OLD_TOKEN': 'x'}):
            self.write_report(co)
        self.assertEqual(co.probe_passed(), (True, ''))
        self.assertNotIn('FIELD10_OLD_TOKEN', env_names(co._claude_sandbox_settings('reviewer')))

    def test_a_relevant_flag_change_still_refuses_with_its_reason(self):
        use_lifecycle_on(self, self.h)
        co = self.h.coordinator('--gate-vendor', 'claude')
        self.write_report(co)
        co.args.reviewer_effort = 'high'
        self.assertEqual(co.probe_passed(), (False, 'permission probe reviewer flags do not match this run'))
        with patch.dict(os.environ, {'FIELD10_NEW_MESSAGING_TOKEN': 'x'}):   # an env change does not mask a relevant one
            self.assertEqual(co.probe_passed(), (False, 'permission probe reviewer flags do not match this run'))

    def test_a_failed_comparison_reports_the_check_that_still_fails(self):
        use_lifecycle_on(self, self.h)
        co = self.h.coordinator('--gate-vendor', 'claude')
        self.write_report(co)
        co.args.gate_effort = 'high'                                         # the gate changed for real ...
        with patch.dict(os.environ, {'FIELD10_NEW_MESSAGING_TOKEN': 'x'}):   # ... and the reviewer flags differ only by env names
            self.assertEqual(co.probe_passed(), (False, 'permission probe gate flags do not match this run'))

    def test_a_path_change_refuses_and_names_what_changed(self):
        use_lifecycle_on(self, self.h)
        co = self.h.coordinator('--gate-vendor', 'claude')
        self.assertIsNone(co._program_state()[1])                            # frozen as permission_probe would
        self.write_report(co)
        with patch.dict(os.environ, {'PATH': os.environ.get('PATH', '') + ':/field10-extra-path-entry'}):
            ok, reason = co.probe_passed()
        self.assertFalse(ok)
        self.assertIn('configured operator program or PATH changed since permission probe', reason)
        self.assertIn('path_env', reason)


class ClaudeAuthorRejectOnDoneTests(unittest.TestCase):
    """The FIELD-10 path itself: a Claude author run with a recorded Claude author probe PASS."""

    def setUp(self):
        self.t = tor.ClaudeAuthorProbeTests('test_the_claude_version_call_of_the_author_probe_runs_in_the_claude_child_env')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def test_reject_on_done_reuses_the_pass_when_only_secret_env_names_changed(self):
        co, out = self.t.probe()
        self.t.write_report(co, out, extra={'author_flags': co.author_flags()})   # a real report carries it (permission_probe)
        co.state['status'] = 'DONE'
        co.save()
        self.assertEqual(co.claude_author_verified(), (True, ''))
        with patch.dict(os.environ, {'FIELD10_NEW_MESSAGING_TOKEN': 'x'}):
            self.assertEqual(co.probe_passed(), (True, ''))
            self.assertEqual(co.claude_author_verified(), (True, ''))

    def test_the_comparison_never_voids_an_operator_opt_in_that_holds_in_the_current_environment(self):
        co, out = self.t.probe()
        waived = {**out, 'status': 'UNKNOWN', 'model_escape_failed_targets': []}   # an author-only UNKNOWN that the opt-in waives (HL-FIX)
        self.t.write_report(co, waived, status='UNKNOWN', extra={
            'author_flags': co.author_flags(), 'failure_reasons': ['author-model-escape-unknown'],
            'allowed_command_ran': True, 'snapshot_unchanged': True, 'write_attempts_denied': {'x': True}})
        with patch.dict(os.environ, {'FIELD10_NEW_MESSAGING_TOKEN': 'x'}):   # the opt-in was given in this (the reject) shell
            co.state['claude_author_override'] = {'actor': 'operator', 'reason': 'checked by hand',
                                                  'author_flags_digest': co.author_flags_digest(), 'time': 'T'}
            self.assertEqual(co.probe_passed(), (True, ''))
            self.assertNotIn('voided', co.state['claude_author_override'])
            self.assertTrue(co._claude_optin_current())

    def test_an_exception_during_the_comparison_clears_the_probe_names(self):
        co, out = self.t.probe()
        self.t.write_report(co, out, extra={'author_flags': co.author_flags()})
        calls = iter([(False, 'permission probe reviewer flags do not match this run'), RuntimeError('boom')])
        def once():
            item = next(calls)
            if isinstance(item, Exception): raise item
            return item
        with patch.object(co, '_probe_passed_once', side_effect=once), \
                patch.dict(os.environ, {'FIELD10_NEW_MESSAGING_TOKEN': 'x'}):
            with self.assertRaisesRegex(RuntimeError, 'boom'):
                co.probe_passed()
            self.assertIsNone(co._probe_env_names)
            self.assertIn('FIELD10_NEW_MESSAGING_TOKEN', env_names(co._claude_sandbox_settings('author')))

    def test_the_refusal_names_the_failing_probe_check(self):
        use_lifecycle_on(self, self.t.h)
        co = self.t.co()
        ok, message = co.claude_author_verified()
        self.assertFalse(ok)
        self.assertIn(tor.REFUSAL, message)
        self.assertIn('no Claude author probe PASS is recorded', message)
        (co.run_dir / 'permission-probe.json').write_text(json.dumps({'author_permission_probe': {'claude_author_status': 'PASS'}}))
        with patch.object(co, 'probe_passed', return_value=(False, 'permission probe reviewer flags do not match this run')):
            ok, message = co.claude_author_verified()
        self.assertFalse(ok)
        self.assertIn(tor.REFUSAL, message)
        self.assertIn('the recorded probe does not pass: permission probe reviewer flags do not match this run', message)


if __name__ == '__main__':
    unittest.main()
