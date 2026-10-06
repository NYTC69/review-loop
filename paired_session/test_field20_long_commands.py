"""FIELD-20 (poker-news-bot WI-101, 2026-10-05): a long configured test command must be run to completion and observed with its
real exit code. codex-cli 0.160.0's exec_command returns after about 10 s with a session_id and no exit_code; the probe prompt
allowed only one exec_command cell per command, so the model moved on and the turn's end killed the suite (rollout: exit -1
after 104.7 s). Prompts now tell every role to poll a still-running command to completion; a command never observed to
completion is reported as such, not as an ordinary failure; one observed completed exit-0 run is still required."""
import os
import re
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc
from paired_session import evidence_guard as eg
from paired_session import test_operator_roles as tor
from paired_session import test_real_coordinator as trc
from paired_session.test_readonly_scratch import CODEX_ROLES

NOT_COMPLETED = 'allowed-command-not-completed (no exit status: still running or killed when the turn ended): '


class LongProbeCommandTests(unittest.TestCase):
    locals().update({name: getattr(tor.ProbeSkipTests, name) for name in ('setUp', 'co', 'probe')})

    def report(self, state=None, **env):
        self.h.run_dir = self.h.root / ('run-' + (state or 'plain') + '-' + '-'.join(env))
        with patch.dict(os.environ, {**({'FAKE_CODEX_LONG': state} if state else {}), **env}):
            return self.probe(self.co(*CODEX_ROLES))

    def test_a_command_that_yields_and_then_completes_with_exit_0_passes(self):
        report = self.report('yield')
        self.assertEqual(report['status'], 'PASS', report['failure_reasons'])
        self.assertTrue(report['allowed_command_ran'])

    def test_a_command_never_completed_or_killed_gets_its_own_reason(self):
        for state in ('started-only', 'killed'):
            with self.subTest(state=state):
                report = self.report(state)
                self.assertNotEqual(report['status'], 'PASS')
                self.assertFalse(report['allowed_command_ran'])
                reasons = report['failure_reasons']
                self.assertTrue(any(reason.startswith(NOT_COMPLETED) for reason in reasons), reasons)
                self.assertFalse(any(reason.startswith('allowed-command-failed: ') for reason in reasons), reasons)

    def test_an_ordinary_failure_stays_allowed_command_failed(self):
        report = self.report(FAKE_CODEX_PROBE_OTHER_ERROR='python3 -m unittest')
        self.assertTrue(any(reason.startswith('allowed-command-failed: ') for reason in report['failure_reasons']),
                        report['failure_reasons'])
        self.assertFalse(any(reason.startswith(NOT_COMPLETED) for reason in report['failure_reasons']))

    def test_the_probe_prompt_tells_both_vendors_to_wait_before_the_allowed_command(self):
        co = self.co(*CODEX_ROLES)
        for vendor in ('codex', 'claude'):
            prompt = co._probe_prompt(vendor, 'python3 -m unittest', ('echo x > forbidden-probe',), [])
            self.assertIn(rc.LONG_COMMAND_RULE, prompt)
            self.assertLess(prompt.index(rc.LONG_COMMAND_RULE), prompt.index('Allowed exact command:\npython3 -m unittest'))
        self.assertIn(rc.LONG_COMMAND_RULE, co.allowed_command_prompt())   # reviewer, gate, docs and specialist prompts
        self.assertIsNone(re.search(r'\b(?:Claude|Codex|Opus|Astra)\b', rc.LONG_COMMAND_RULE, re.I))   # fresh-role history scan


class LongReviewerTestCommandTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def test_an_exec_approval_whose_test_was_not_observed_to_completion_holds_with_that_reason(self):
        result = self.h.run_coordinator('--reviewer-vendor', 'codex', '--shadow', 'off', '--adversarial-gate', 'off',
                                        env={'FAKE_CODEX_LONG': 'started-only'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = trc.json.loads((self.h.run_dir / 'state.json').read_text())
        self.assertIn('EXEC approval lacks an observed successful configured test command; the configured test was not '
                      'observed to completion', state['hold_reason'])

    def test_an_exec_approval_whose_test_yielded_and_completed_is_accepted(self):
        result = self.h.run_coordinator('--reviewer-vendor', 'codex', '--shadow', 'off', '--adversarial-gate', 'off',
                                        env={'FAKE_CODEX_LONG': 'yield'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ObservedEventTests(unittest.TestCase):
    def test_a_started_command_without_completion_is_observed_without_an_exit_status(self):
        rows = [{'type': 'item.started', 'item': {'id': 'c1', 'type': 'command_execution', 'command': "/bin/zsh -lc 'pytest -q'",
                                                  'exit_code': None, 'status': 'in_progress'}},
                {'type': 'item.started', 'item': {'id': 'c2', 'type': 'command_execution', 'command': "/bin/zsh -lc 'ls'",
                                                  'exit_code': None, 'status': 'in_progress'}},
                {'type': 'item.completed', 'item': {'id': 'c2', 'type': 'command_execution', 'command': "/bin/zsh -lc 'ls'",
                                                    'exit_code': 0, 'status': 'completed', 'aggregated_output': 'a'}}]
        commands, calls = rc.observed_events('codex', rows)
        self.assertEqual([(row['command'], row['exit_code']) for row in commands], [('ls', 0), ('pytest -q', None)])
        self.assertEqual([call['input']['command'] for call in calls], ['ls', 'pytest -q'])   # the guard still sees the started command
        self.assertTrue(rc.command_not_completed(commands[1]))
        self.assertFalse(rc.command_not_completed(commands[0]))

    def test_only_codex_rows_without_a_real_exit_status_count_as_not_completed(self):
        self.assertTrue(rc.command_not_completed({'exit_code': -1, 'source': 'command_execution'}))
        self.assertTrue(rc.command_not_completed({'exit_code': None, 'source': '/x/rollout.jsonl:3'}))
        self.assertFalse(rc.command_not_completed({'exit_code': 1, 'source': 'command_execution'}))
        self.assertFalse(rc.command_not_completed({'exit_code': -1, 'source': 'Bash tool_use/tool_result'}))   # a Claude error


class PollCellTests(unittest.TestCase):
    def test_only_an_input_free_literal_poll_cell_is_recognised(self):
        good = 'const r = await tools.write_stdin({"session_id":20075,"chars":"","yield_time_ms":30000}); text(JSON.stringify(r));'
        self.assertTrue(eg.code_mode_poll(good))
        for bad in (good.replace('"chars":""', '"chars":"y\\n"'), good.replace('20075', '"20075"'),
                    good + ' other();', good.replace('write_stdin', 'exec_command'), None):
            self.assertFalse(eg.code_mode_poll(bad), bad)


if __name__ == '__main__':
    unittest.main()
