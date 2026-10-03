"""Batch b295-field-a (v2.9.5): field reports from runs on v2.9.4 (FIELD-7, FIELD-8, N4-b)."""
import json
import unittest

from paired_session import test_operator_roles as tor
from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action', 'coordinator')


class FieldCliTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def test_a_missing_codex_home_is_refused_up_front_when_a_role_is_codex(self):        # FIELD-7
        missing = self.root / 'lane-b-codex-home'
        for action in ('permission-probe', 'run'):
            with self.subTest(action=action):
                self.run_dir = self.root / ('missing-' + action)
                done = self.run_operator_action(action, env={'CODEX_HOME': str(missing)})
                self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                self.assertIn(f'REFUSED: CODEX_HOME {missing} is not an existing directory', done.stdout)
                self.assertFalse(missing.exists())
        self.run_dir = self.root / 'all-claude'                                             # no Codex role: no CODEX_HOME needed
        done = self.run_coordinator('--author-vendor', 'claude', '--reviewer-vendor', 'claude', '--gate-vendor', 'claude',
                                    '--accept-unverified-claude-author', '--reason', 'fake harness', env={'CODEX_HOME': str(missing)})
        self.assertNotIn('CODEX_HOME', done.stdout)

    def test_test_command_shapes_a_dontask_claude_reviewer_cannot_run_warn_at_config_time(self):   # N4-b
        for command in ("/bin/bash -c 'swift-frontend -parse $(find ios -name \"*.swift\")'", 'make test | tee log', 'npm test && npm run lint',
                        'for f in a b; do echo $f; done', 'pytest > out.txt', 'make test; true', 'echo `date`'):
            with self.subTest(command=command):
                self.assertIn(repr(command), rc.dontask_command_hint([command]))
        for command in ('/bin/bash /abs/path/run-tests.sh', 'python3 -m unittest', 'npm test', 'pytest -q tests/ab', 'make transform-check'):
            with self.subTest(command=command):
                self.assertEqual(rc.dontask_command_hint([command]), '')
        done = self.run_coordinator('--test-command', "/bin/bash -c 'python3 -m unittest && true'", '--gate-vendor', 'claude')
        self.assertIn('WARNING: a Claude reviewer or gate runs each test/reviewer command', done.stdout)
        self.assertIn('/bin/bash /absolute/path/to/script.sh', done.stdout)
        self.run_dir = self.root / 'plain'
        self.assertNotIn('WARNING', self.run_coordinator('--gate-vendor', 'claude').stdout)


class FieldProbeListingTests(unittest.TestCase):                                            # FIELD-8 / N4-a
    locals().update({name: getattr(tor.ClaudeAuthorProbeTests, name) for name in ('setUp', 'co', 'probe', 'fail_reason')})

    def test_a_file_changed_beside_the_run_dir_is_classified_for_the_operator(self):
        log = self.h.run_dir.parent / 'WI-81.probe.log'
        try: _, out = self.fail_reason({'silent_write': ['{parent}/WI-81.probe.log']})
        finally: log.unlink(missing_ok=True)
        self.assertEqual(out['changed_beside_run_dir'], [str(log)])
        self.assertIn(str(log), out['model_escape_failed_targets'])
        _, out = self.fail_reason({'silent_write': ['{base}/outside/extra.txt']})            # a write in the probe tree is not "beside the run dir"
        self.assertEqual(out['changed_beside_run_dir'], [])


class FieldProbeHoldTests(unittest.TestCase):                                               # FIELD-8 / N4-a
    locals().update({name: getattr(tor.ProbeSkipTests, name) for name in ('setUp', 'co', 'probe')})

    def test_the_hold_names_an_operator_file_outside_the_run_dir(self):
        log = str(self.h.run_dir.parent / 'WI-81.probe.log')
        escape = str(self.h.run_dir.parent / 'paired-session-author-probe-x' / 'outside' / 'w.txt')
        for failed, beside, wording in (([log], [log], 'operator-created file outside the run dir?'),
                                        ([escape, log], [log], 'escape write observed at ' + escape),
                                        ([escape], [], 'escape write observed at ' + escape)):
            with self.subTest(failed=failed):
                co = self.co()
                author = {'status': 'FAIL', 'model_escape_failed_targets': failed, 'changed_beside_run_dir': beside}
                self.assertEqual(self.probe(co, author)['status'], 'FAIL')
                reason = json.loads((co.run_dir / 'state.json').read_text())['hold_reason']
                self.assertTrue(reason.startswith('1C FAIL: ' + wording), reason)
                self.assertEqual("write launcher logs outside the run dir's parent" in reason, bool(beside), reason)
                (co.run_dir / 'state.json').unlink()


if __name__ == '__main__':
    unittest.main()
