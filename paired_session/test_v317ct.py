"""V317-CT: long command budgets and useful approval HOLD causes, with fake CLIs."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc
from paired_session import test_v317ff as ff
from paired_session import test_worktree_lifecycle as twl

rc = trc.rc
TIMEOUT_VARS = ('BASH_DEFAULT_TIMEOUT_MS', 'BASH_MAX_TIMEOUT_MS')


class CommandEnvironmentTests(unittest.TestCase):
    def test_unset_smaller_larger_and_non_integer_values(self):
        for supplied in (None, '600000', '7200000', 'not-an-integer'):
            with self.subTest(supplied=supplied), patch.dict(os.environ, {}, clear=True):
                if supplied is not None:
                    os.environ.update(dict.fromkeys(TIMEOUT_VARS, supplied))
                env = rc.cli_env()
                expected = '7200000' if supplied == '7200000' else str(rc.LONG_COMMAND_TIMEOUT_MS)
                self.assertEqual([env[name] for name in TIMEOUT_VARS], [expected, expected])
                self.assertEqual(os.environ.get(TIMEOUT_VARS[0]), supplied)

    def test_variables_keep_their_independent_larger_values(self):
        with patch.dict(os.environ, dict(zip(TIMEOUT_VARS, ('7200000', 'bad')))):
            env = rc.cli_env()
        self.assertEqual(env[TIMEOUT_VARS[0]], '7200000')
        self.assertEqual(env[TIMEOUT_VARS[1]], str(rc.LONG_COMMAND_TIMEOUT_MS))

    def test_only_the_last_rule_sentence_changes(self):
        original = ('Run each command to completion before the next one and never end your turn while a command is still running. '
                    'A result with a session_id and no exit_code is still running: poll that session until the result carries an '
                    'exit_code (code-mode cells of exactly: const r = await tools.write_stdin({"session_id":<that id>,"chars":"",'
                    '"yield_time_ms":30000}); text(JSON.stringify(r));). Polls are not extra commands. '
                    'A Bash tool call that takes a timeout parameter gets 600000 for a long command.')
        self.assertEqual(rc.LONG_COMMAND_TIMEOUT_MS, 3600000)
        self.assertEqual(rc.LONG_COMMAND_RULE, original.replace('gets 600000', f'gets {rc.LONG_COMMAND_TIMEOUT_MS}'))


class PromptTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in twl._HELPERS})

    # Exercise the actual FINISH and both POLISH-Q writer dispatch call sites.
    test_finisher_and_both_quality_writer_call_sites_include_long_command_rule = (
        ff.PromptAndPreflightTests.test_finisher_and_both_quality_writer_call_sites_include_long_command_rule)

    def test_allowed_command_and_probe_call_sites_and_fresh_prompts(self):
        co = self.coordinator('--lifecycle-mode', 'off')
        co.args.test_command = 'python3 -m unittest'
        co.state['phase'] = 'EXEC'
        (co.context / 'plan.md').write_text('# Plan\nImplement the feature.\n')
        prompts = [co.allowed_command_prompt(),
                   co._review_prompt('reviewer', 'snapshot')]
        for vendor in ('claude', 'codex'):
            prompts.append(co._probe_prompt(vendor, co.args.test_command, (), []))
        for role in ('shadow', 'gate'):
            prompt = co._gate_prompt('snapshot') if role == 'gate' else co._review_prompt(role, 'snapshot')
            prompts.append(prompt)
            co.assert_fresh_prompt(role, prompt)
        for prompt in prompts:
            self.assertIn(rc.LONG_COMMAND_RULE, prompt)

    def test_command_budget_does_not_change_frozen_resume_values(self):
        with patch.object(rc, 'LONG_COMMAND_TIMEOUT_MS', 600000):
            co = self.coordinator('--lifecycle-mode', 'off')
            self.assertIsNone(co._program_state()[1])
            frozen = copy.deepcopy({key: co.state[key] for key in
                                    ('config', 'role_dispatch_manifest', 'operator_programs')})
        args = copy.deepcopy(co.args)
        args.action = 'resume'
        resumed = rc.Coordinator(args)
        self.assertIsNone(resumed._program_state()[1])
        self.assertEqual({key: resumed.state[key] for key in frozen}, frozen)
        self.assertEqual(resumed._role_dispatch_manifest(), frozen['role_dispatch_manifest'])


class ApprovalEvidenceTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in twl._HELPERS})

    def check_approval(self, role, timed_out):
        # Substitute only the failed tool result in a test-local copy of the fake CLI.
        fake = self.root / 'timeout-fake.py'
        source = trc.FAKE.read_text()
        if timed_out:
            old = "'FAILED fake configured test' if test_failure else"
            self.assertEqual(source.count(old), 1)
            source = source.replace(old, "'Exit code 143\\nCommand timed out after 10m 0s\\n...' if test_failure else")
        fake.write_text(source)
        with patch.object(trc, 'FAKE', fake):
            co = self.coordinator('--lifecycle-mode', 'off', '--reviewer-vendor', 'claude',
                                  '--gate-vendor', 'claude',
                                  '--test-command', 'python3 -m unittest')
        co.state['phase'] = 'EXEC'
        (co.context / 'plan.md').write_text('# Plan\nImplement the feature.\n')
        snapshot = rc.git_snapshot(self.workspace)[0]
        prompt = co._gate_prompt(snapshot) if role == 'gate' else co._review_prompt(role, snapshot)
        schema = rc.gate_schema() if role == 'gate' else rc.review_schema()
        prefix = f'{role} EXEC approval lacks an observed successful configured test command'
        expected = prefix + (" (the command timed out after 10m 0s in the role's command tool)" if timed_out else '')
        with patch.dict(os.environ, {'FAKE_REVIEW_TEST_FAILURE': '1'}):
            with self.assertRaises(RuntimeError) as caught:
                co.invoke(role, 'EXEC', prompt, schema, fresh=role == 'gate')
        self.assertEqual(str(caught.exception), expected)
        co.hold(str(caught.exception))
        state = json.loads(co.state_path.read_text())
        self.assertEqual((state['status'], state['hold_reason']), ('HOLD', expected))
        receipt = state['turns'][-1]
        self.assertEqual(receipt['error'], expected)
        row = next(row for row in receipt['observed_commands'] if row['command'] == co.args.test_command)
        self.assertEqual((row['exit_code'], row['error']), (-1, True))
        if timed_out:
            self.assertEqual(rc.tool_timeout_seconds([row]), 600)
        self.assertIn('approve', (co.evidence / f"{receipt['sequence']:03d}-exec-{role}.stdout.jsonl").read_text().lower())

    def test_gate_tool_timeout_holds_with_cause(self):
        self.check_approval('gate', True)

    def test_gate_failed_test_keeps_existing_reason(self):
        self.check_approval('gate', False)

    def test_persistent_reviewer_tool_timeout_holds_with_cause(self):
        self.check_approval('reviewer', True)

    def test_persistent_reviewer_failed_test_keeps_existing_reason(self):
        self.check_approval('reviewer', False)


if __name__ == '__main__':
    unittest.main()
