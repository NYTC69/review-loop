"""Batches b295-field-a and b295-field-b (v2.9.5): field reports from runs on v2.9.4 (FIELD-7, FIELD-8, N4-b..e, OPV-M1)."""
import hashlib
import json
import os
import subprocess
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
                self.assertIn('or unset CODEX_HOME', done.stdout)                         # field-a L4: CODEX_HOME was set
        self.run_dir = self.root / 'missing-intent'                                         # field-a L5: an intent preview dispatches nothing
        self.coordinator().done()
        command = self.command(); command[2] = 'reject'
        intent = subprocess.run([*command, '--text', 'x', '--intent-only', '--skip-probe'], cwd=self.root, text=True, capture_output=True,
                                env={**os.environ, 'CODEX_HOME': str(missing)})
        self.assertEqual(intent.returncode, 0, intent.stdout + intent.stderr)
        self.assertIn('digest', json.loads(intent.stdout))
        self.run_dir = self.root / 'all-claude'                                             # no Codex role: no CODEX_HOME needed
        done = self.run_coordinator('--author-vendor', 'claude', '--reviewer-vendor', 'claude', '--gate-vendor', 'claude',
                                    '--accept-unverified-claude-author', '--reason', 'fake harness', env={'CODEX_HOME': str(missing)})
        self.assertNotIn('CODEX_HOME', done.stdout)

    def test_test_command_shapes_a_dontask_claude_reviewer_cannot_run_warn_at_config_time(self):   # N4-b
        for command in ("/bin/bash -c 'swift-frontend -parse $(find ios -name \"*.swift\")'", 'make test | tee log', 'npm test && npm run lint',
                        'for f in a b; do echo $f; done', 'pytest > out.txt', 'make test; true', 'echo `date`'):
            with self.subTest(command=command):
                self.assertIn(repr(command), rc.dontask_command_hint([command]))
        for command in ('/bin/bash /abs/path/run-tests.sh', 'python3 -m unittest', 'npm test', 'pytest -q tests/ab', 'make transform-check',
                        'make check-for-leaks', '/bin/bash /abs/for/run.sh'):                  # field-a L2
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
        for failed, beside, unexpected, wording, hinted in (([log], [log], [], 'operator-created file outside the run dir?', True),
                                                            ([escape, log], [log], [], 'escape write observed at ' + escape, True),
                                                            ([escape], [], [], 'escape write observed at ' + escape, False),
                                                            ([log], [log], ['Bash: printf x > log'], 'escape write observed at ' + log, False)):   # field-a L1
            with self.subTest(failed=failed, unexpected=unexpected):
                co = self.co()
                author = {'status': 'FAIL', 'model_escape_failed_targets': failed, 'changed_beside_run_dir': beside, 'unexpected_tool_uses': unexpected}
                self.assertEqual(self.probe(co, author)['status'], 'FAIL')
                reason = json.loads((co.run_dir / 'state.json').read_text())['hold_reason']
                self.assertTrue(reason.startswith('1C FAIL: ' + wording), reason)
                self.assertEqual("write launcher logs outside the run dir's parent" in reason, hinted, reason)
                (co.run_dir / 'state.json').unlink()


class FieldOperatorCliTests(unittest.TestCase):                                             # b295-field-b
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def cli(self, action, *extra):
        command = self.command(); command[2] = action
        return subprocess.run([*command, *extra, '--skip-probe'], cwd=self.root, text=True, capture_output=True)

    def attach(self, text='** TEST SUCCEEDED **\n'):
        log = self.root / 'xcodebuild.log'
        log.write_text(text)
        return self.cli('attach-verification', '--command', 'xcodebuild test', '--exit-code', '0', '--log', str(log),
                        '--log-sha256', hashlib.sha256(text.encode()).hexdigest(), '--note', 'ran on the host simulator')

    def test_accept_takes_the_reason_as_its_intent_payload_and_refuses_text(self):          # N4-c, N4-d
        self.coordinator().done()
        for extra in (('--intent-only', '--text', 'looks good'), ('--file', str(self.workitem))):
            with self.subTest(extra=extra):
                refused = self.cli('accept', *extra)
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertIn('REFUSED: accept takes no --text or --file', refused.stdout)
        intent = self.cli('accept', '--reason', 'owner accepts', '--intent-only')
        self.assertEqual(intent.returncode, 0, intent.stdout + intent.stderr)
        done = self.cli('accept', '--reason', 'owner accepts', '--expect', json.loads(intent.stdout)['digest'])
        self.assertEqual((done.returncode, done.stdout.splitlines()[-1]), (0, 'ACCEPTED'), done.stdout + done.stderr)
        self.assertEqual(json.loads((self.run_dir / 'evidence' / 'acceptance.json').read_text())['reason'], 'owner accepts')
        self.assertIn('needs --reason', self.cli('resume', '--reason', 'why').stdout)       # without an --accept-* flag run/resume/reject still refuse it

    def test_operator_evidence_attached_at_done_is_listed_by_accept_while_current(self):    # N4-e
        co = self.coordinator()
        co.done()
        self.assertEqual(self.attach().returncode, 0)
        self.assertEqual(self.attach('second log\n').returncode, 0)
        co = self.coordinator()
        co.state['operator_verifications'][0]['tree_sha256'] = '0' * 64                    # V001 belongs to another tree
        co.save()
        intent = self.cli('accept', '--intent-only')
        done = self.cli('accept', '--expect', json.loads(intent.stdout)['digest'])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('VERIFICATION V002 current for the accepted tree: `xcodebuild test` exit 0', done.stdout)
        self.assertNotIn('V001', done.stdout)
        record = json.loads((self.run_dir / 'evidence' / 'acceptance.json').read_text())
        self.assertEqual([(row['id'], row['note'], row['tree_sha256']) for row in record['operator_verifications']],
                         [('V002', 'ran on the host simulator', record['intent']['tree_sha256'])])
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['operator_verifications'][0]['status'], 'voided')

    def test_the_persistent_reviewer_is_told_when_a_record_it_saw_is_withdrawn(self):      # OPV-M1
        co = self.coordinator()
        self.assertEqual(self.attach().returncode, 0)
        co = self.coordinator()
        co.state['phase'] = 'EXEC'
        tree = rc.git_snapshot(self.workspace)[0]
        self.assertIn('V001', co._review_prompt('reviewer', tree))
        self.assertEqual(self.attach('another\n').returncode, 0)                              # V002 is voided before the reviewer sees it
        co = self.coordinator()
        co.state['phase'] = 'EXEC'
        co.state['operator_verifications'][1]['status'] = 'voided'
        co.state['operator_verifications'][1]['voided'] = {'reason': 'tree changed'}
        (self.workspace / 'tracked.txt').write_text('changed\n')
        tree = rc.git_snapshot(self.workspace)[0]
        prompts = {'reviewer': co._review_prompt('reviewer', tree), 'shadow': co._review_prompt('shadow', tree), 'gate': co._gate_prompt(tree)}
        self.assertIn('Withdrawn operator verification: V001 (tree changed); do not rely on it.', prompts['reviewer'])
        self.assertNotIn('V002', prompts['reviewer'])
        self.assertNotIn('Operator-verified evidence', prompts['reviewer'])
        for role in ('shadow', 'gate'):
            self.assertNotIn('Withdrawn', prompts[role], role)


if __name__ == '__main__':
    unittest.main()
