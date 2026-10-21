"""V324-TEST: a persistent EXEC approval over its own completed failing test returns to the author."""
import json
import os
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
HELPERS = ('tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
           'fake_codex_cli', 'fake_claude_cli', 'command')
PREFIX = 'reviewer EXEC approval lacks an observed successful configured test command'


class FailedTestApprovalTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def setUp(self):
        trc.RealCoordinatorTests.setUp(self)
        passing = patch.dict(os.environ, {'FAKE_CODEX_LONG': 'yield'})
        passing.start()
        self.addCleanup(passing.stop)

    def coordinator(self, *extra):
        self.run_dir = self.root / ('run-' + str(len(list(self.root.glob('run-*')))))
        co = rc.Coordinator(rc.parser().parse_args(self.command(
            '--reviewer-vendor', 'codex', '--gate-vendor', 'codex',
            '--shadow', 'off', '--adversarial-gate', 'off', '--skip-probe', *extra)[2:]))
        co.state.update(phase='EXEC', next='author')
        co.author_turn()
        return co

    def failing_review(self, co):
        with patch.dict(os.environ, {'FAKE_REVIEW_TEST_FAILURE': '1'}):
            co.reviewer_turn()

    def test_completed_failed_approval_delivers_blocker_to_next_author(self):
        co = self.coordinator()
        self.failing_review(co)
        self.assertEqual((co.state['status'], co.state['phase'], co.state['next']),
                         ('ACTIVE', 'EXEC', 'author'))
        [row] = co.blocking_open_findings()
        self.assertEqual((row['source'], row['severity'], row['status']), ('test-failure', 'MAJOR', 'open'))
        self.assertIn('exit status 1', row['summary'])
        self.assertIn('FAILED fake test', row['failure_scenario'])
        delivered = json.loads(co.state['delivered_review'])
        self.assertEqual(delivered['status'], 'REVISE')
        self.assertEqual(delivered['findings'][0]['source'], 'test-failure')
        self.assertEqual(delivered['findings'][0]['id'], row['id'])
        self.assertIn(row['summary'], co._author_prompt())
        self.assertEqual(co.state['turns'][-1]['answer']['status'], 'APPROVE')

    def test_persistent_failure_returns_to_author_before_shadow_dispatch(self):
        co = self.coordinator('--shadow', 'on')
        self.failing_review(co)
        self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'author'))
        self.assertEqual([turn['role'] for turn in co.state['turns']], ['author', 'reviewer'])
        self.assertEqual(co.blocking_open_findings()[0]['source'], 'test-failure')

    def test_next_round_passing_approval_closes_program_row_and_proceeds(self):
        for gate in ('off', 'on'):
            with self.subTest(gate=gate):
                co = self.coordinator('--adversarial-gate', gate)
                self.failing_review(co)
                [row] = co.blocking_open_findings()
                co.author_turn()
                self.assertEqual(co.state['exec_rounds'], 2)
                invoke = co.invoke

                def still_open(*args, **kwargs):
                    result = invoke(*args, **kwargs)
                    # The clean observed run, rather than a reviewer disposition, must close the program row.
                    for prior in result['answer'].get('prior_findings', []):
                        prior.update(disposition='still_open', evidence='not disposed by the reviewer')
                    return result

                with patch.object(co, 'invoke', side_effect=still_open):
                    co.reviewer_turn()
                self.assertEqual(row['status'], 'fixed')
                self.assertEqual(row['status_history'][-1]['evidence'],
                                 'configured test command: observed successful run')
                self.assertFalse(co.blocking_open_findings())
                if gate == 'on':
                    self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'gate'))
                else:
                    self.assertEqual(co.state['status'], 'DONE')

    def test_missing_and_unfinished_runs_keep_old_hold(self):
        for env, suffix in (({'FAKE_MISSING_OBSERVED': '1'}, ''),
                            ({'FAKE_CODEX_LONG': 'started-only'},
                             '; the configured test was not observed to completion (no exit status when the turn ended)'),
                            ({'FAKE_CODEX_LONG': 'killed'},
                             '; the configured test was not observed to completion (no exit status when the turn ended)')):
            with self.subTest(env=env):
                co = self.coordinator()
                with patch.dict(os.environ, env):
                    self.assertEqual(co._drive_loop(), 'HOLD')
                self.assertEqual(co.state['hold_reason'], PREFIX + suffix)
                self.assertFalse(co.open_findings())
                self.assertEqual(co.state['next'], 'reviewer')

    def test_shadow_and_gate_failed_approvals_keep_old_hold(self):
        for role in ('shadow', 'gate'):
            with self.subTest(role=role):
                co = self.coordinator('--shadow', 'on' if role == 'shadow' else 'off',
                                      '--adversarial-gate', 'on' if role == 'gate' else 'off')
                observe = rc.observed_events

                def failing(vendor, events):
                    rows, calls = observe(vendor, events)
                    rows = [row for row in rows
                            if not rc.command_invokes_test(row['command'], co.args.test_command)]
                    rows.append({'command': co.args.test_command, 'exit_code': 1,
                                 'error': True, 'output': 'FAILED fake test', 'source': 'command_execution'})
                    return rows, calls

                # Finish the persistent approval before injecting the fresh role's failing evidence.
                if role == 'gate':
                    co.reviewer_turn()
                with patch.object(rc, 'observed_events', side_effect=failing):
                    if role == 'shadow':
                        # Let the persistent reviewer pass; only the shadow's invocation gets the failure.
                        invoke = co.invoke

                        def fresh_failure(name, *args, **kwargs):
                            with patch.object(rc, 'observed_events', side_effect=failing if name == 'shadow' else observe):
                                return invoke(name, *args, **kwargs)

                        with patch.object(co, 'invoke', side_effect=fresh_failure):
                            self.assertEqual(co._drive_loop(), 'HOLD')
                    else:
                        self.assertEqual(co._drive_loop(), 'HOLD')
                self.assertEqual(co.state['hold_reason'],
                                 f'{role} EXEC approval lacks an observed successful configured test command')
                self.assertFalse(co.open_findings())

    def test_last_round_uses_normal_round_limit_hold(self):
        co = self.coordinator('--max-exec-rounds', '1')
        self.failing_review(co)
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertEqual(co.state['hold_reason'], 'EXEC round limit reached')
        self.assertEqual(co.state['round_limit_hold']['hold_reason'], 'EXEC round limit reached')
        self.assertEqual(co.blocking_open_findings()[0]['source'], 'test-failure')
        with self.assertRaises(ValueError) as caught:
            co.note('Repair the failing tests.', None)
        self.assertIn('resume --add-rounds N (1-10)', str(caught.exception))
        self.assertIn('note --scope-change', str(caught.exception))

    def test_failure_detection_excludes_timeout_and_zero_exit_failure_text(self):
        command = 'python3 -m unittest'
        for row in ({'exit_code': 0, 'output': 'FAILED test'},
                    {'exit_code': -1, 'output': 'Command timed out after 1000ms',
                     'source': 'Bash tool_use/tool_result'},
                    {'exit_code': None}, {'exit_code': -1}):
            with self.subTest(row=row):
                self.assertIsNone(rc.completed_test_failure([{'command': command, **row}], command))


if __name__ == '__main__':
    unittest.main()
