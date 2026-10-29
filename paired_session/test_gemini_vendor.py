"""WI-GEM B1 (ADR-20 G1-G4, G9, G10): gemini as a third vendor value; B2a (G5-G7): the agy adapter for the read-only roles."""
import contextlib
import io
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import test_real_coordinator as trc
from paired_session import timeout_scale as tsc

rc = trc.rc
B2 = 'this gemini role path cannot be dispatched yet (WI-GEM B2b)'
GEMINI_REVIEWER = ['--reviewer-vendor', 'gemini', '--reviewer-model', 'gemini-3.8-flash-medium']
GEMINI_GATE = ['--gate-vendor', 'gemini', '--gate-model', 'gemini-3.8-pro']


def executable(path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{version}"\n')
    path.chmod(0o755)
    return path


class GeminiVendorTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        efficient = patch.object(rc, 'DEFAULT_SAFETY_MODE', 'efficient')   # the harness pins strict; gemini runs efficient only (G4)
        efficient.start()
        self.addCleanup(efficient.stop)

    def argv(self, action, *extra):
        h = self.h
        return [action, '--workspace', str(h.workspace), '--workitem', str(h.workitem), '--run-dir', str(h.run_dir),
                '--codex-bin', str(h.fake_codex_cli()), '--claude-bin', str(h.fake_claude_cli()),
                '--timeout', tsc.scaled_arg(10), '--test-command', 'python3 -m unittest', *extra]

    def args(self, *extra, action='run'):
        argv = self.argv(action, *extra)
        return rc.configure_parser(rc.parser(), argv).parse_args(argv)

    def resolved(self, *extra):
        return rc.resolve_role_model_defaults(self.args(*extra))

    def main(self, action, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rc.main(self.argv(action, *extra))
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue())

    def assert_refused(self, message, *extra, actions=('run', 'resume', 'permission-probe')):
        for action in actions:
            with self.subTest(action=action, flags=extra):
                refused = self.main(action, *extra)
                self.assertEqual(refused.returncode, 2, refused.stdout)
                self.assertIn('REFUSED: ' + message, refused.stdout)
                self.assertFalse(self.h.run_dir.exists())

    def operator_profile(self, values):
        path = self.h.root / 'operator-profile.json'   # outside the workspace and run dir: operator-owned
        path.write_text(json.dumps(values))
        return path

    # ---- G1: the vendor value, --gemini-bin and the profile key ----
    def test_gemini_is_a_vendor_value_and_an_unknown_vendor_is_refused_by_argparse(self):
        a = self.args(*GEMINI_REVIEWER, *GEMINI_GATE)
        self.assertEqual((a.author_vendor, a.reviewer_vendor, a.gate_vendor, a.gemini_bin), ('codex', 'gemini', 'gemini', 'agy'))
        for flag in ('--author-vendor', '--reviewer-vendor', '--gate-vendor'):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_:
                self.args(flag, 'mistral')
            self.assertEqual(exit_.exception.code, 2)
        self.assertEqual(self.args('--gemini-bin', '/opt/agy').gemini_bin, '/opt/agy')
        self.assertEqual(self.args('--config', str(self.operator_profile({'gemini_bin': '/opt/agy'}))).gemini_bin, '/opt/agy')
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            rc.main(['--help'])
        self.assertIn('--gemini-bin', out.getvalue())

    def test_a_workspace_profile_cannot_select_gemini_bin(self):
        path = self.h.workspace / '.review-loop' / 'paired-session.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'gemini_bin': '/opt/agy'}))
        _, issue = rc.program_snapshot(self.h.workspace, self.h.run_dir, self.h.run_dir / 'author-tmp',
                                       self.h.fake_codex_cli(), self.h.fake_claude_cli(), rc.DEFAULT_GATE_PROMPT, None)
        self.assertEqual(issue, 'workspace profile cannot select operator programs: gemini_bin')

    # ---- G2, G10: gemini reviewer and gate pass validation; B1 refuses their dispatch before any state ----
    def test_a_gemini_reviewer_and_gate_pass_validation(self):
        for flags in (GEMINI_REVIEWER, GEMINI_GATE, [*GEMINI_REVIEWER, *GEMINI_GATE]):
            with self.subTest(flags=flags):
                a = self.resolved(*flags)
                rc.validate_role_models(a)
                self.assertEqual((a.author_vendor, a.author_model), ('codex', rc.CODEX_DEFAULT_MODEL))
                self.assertEqual(rc.required_programs(a), ('claude_bin', 'codex_bin', 'gemini_bin'))
                rc.refuse_gemini_dispatch(a)   # B2a: dispatchable (the harness runs lifecycle off)

    def test_a_gemini_reviewer_with_stage_reviews_is_refused_before_state_or_any_turn(self):
        self.assert_refused(rc.GEMINI_STAGE_REVIEWS, *GEMINI_REVIEWER, '--lifecycle-mode', 'on', actions=('run', 'permission-probe'))
        with self.assertRaises(ValueError) as refused:
            rc.Coordinator(self.args(*GEMINI_REVIEWER, '--lifecycle-mode', 'on'))
        self.assertEqual(str(refused.exception), rc.GEMINI_STAGE_REVIEWS)
        self.assertFalse(self.h.run_dir.exists())
        report = self.resolved(*GEMINI_REVIEWER)
        report.review_report = True
        with self.assertRaisesRegex(ValueError, 'cannot run gemini yet'):
            rc.refuse_gemini_dispatch(report)
        rc.refuse_gemini_dispatch(self.resolved(*GEMINI_GATE, '--lifecycle-mode', 'on'))   # a gemini gate runs only the EXEC gate

    def test_a_gemini_author_is_refused(self):
        self.assert_refused('author_vendor gemini is not supported yet (ADR-20 G2)', '--author-vendor', 'gemini', '--author-model', 'gemini-3.8-pro')
        self.assert_refused('author_vendor gemini is not supported yet (ADR-20 G2)', '--author-vendor', 'gemini', actions=('run',))   # before the model rule

    # ---- G4: efficient mode only ----
    def test_a_gemini_role_with_strict_mode_is_refused(self):
        self.assert_refused('gemini roles run in efficient mode only (ADR-20 G4)', *GEMINI_REVIEWER, '--strict')
        self.assert_refused('gemini roles run in efficient mode only (ADR-20 G4)', *GEMINI_GATE, '--strict', actions=('run',))
        profile = self.operator_profile({'safety_mode': 'strict', 'gate_vendor': 'gemini', 'gate_model': 'gemini-3.8-pro'})
        self.assert_refused('gemini roles run in efficient mode only (ADR-20 G4)', '--config', str(profile), actions=('run',))

    # ---- G3, G9: models ----
    def test_a_gemini_role_needs_an_explicit_model(self):
        self.assert_refused('reviewer_model is required when reviewer_vendor is gemini', '--reviewer-vendor', 'gemini', actions=('run',))
        self.assert_refused('gate_model is required when gate_vendor is gemini', '--gate-vendor', 'gemini', actions=('run',))
        with self.assertRaisesRegex(ValueError, 'gate_model is required when gate_vendor is gemini'):
            self.resolved(*GEMINI_REVIEWER, '--gate-vendor', 'gemini')

    def test_any_well_formed_model_id_runs_under_vendor_gemini(self):   # G9: agy serves claude-* and gpt-oss models too
        a = self.resolved('--reviewer-vendor', 'gemini', '--reviewer-model', 'claude-opus-4-6-thinking',
                          '--gate-vendor', 'gemini', '--gate-model', 'gpt-oss-120b')
        rc.validate_role_models(a)
        self.assertEqual((a.reviewer_model, a.gate_model, a.gate_vendor_source), ('claude-opus-4-6-thinking', 'gpt-oss-120b', 'operator'))
        with self.assertRaisesRegex(ValueError, 'reviewer_model is not a well-formed model id'):
            rc.validate_role_models(self.resolved('--reviewer-vendor', 'gemini', '--reviewer-model', 'bad model'))

    def test_allowed_models_accepts_a_gemini_list_and_enforces_it(self):
        allowed = {'codex': [rc.CODEX_DEFAULT_MODEL], 'claude': ['claude-opus-5-5'], 'gemini': ['gemini-3.8-pro']}
        rc.validate_allowed_models(allowed)
        a = self.resolved(*GEMINI_GATE)
        a.allowed_models = allowed
        rc.validate_role_models(a)
        a = self.resolved('--reviewer-vendor', 'gemini', '--reviewer-model', 'claude-opus-4-6-thinking')
        a.allowed_models = allowed
        with self.assertRaisesRegex(ValueError, 'reviewer_model claude-opus-4-6-thinking is not in allowed_models for the gemini role'):
            rc.validate_role_models(a)
        profile = self.operator_profile({'allowed_models': allowed})
        self.assertEqual(self.args('--config', str(profile)).allowed_models, allowed)

    # ---- ADR-10 M4 with three vendors ----
    def test_a_foreign_gate_model_is_checked_against_every_other_vendor(self):
        for flags, text in (
                (['--gate-model', 'gemini-3.8-pro'],
                 "gate_model gemini-3.8-pro belongs to gemini, but the gate now defaults to the author's vendor codex; pass --gate-vendor gemini to keep it"),
                (['--author-vendor', 'claude', '--reviewer-vendor', 'codex', '--gate-model', 'gemini-3.8-pro'],
                 "belongs to gemini, but the gate now defaults to the author's vendor claude"),
                (['--gate-vendor', 'codex', '--gate-model', 'gemini-3.8-pro'],
                 'belongs to gemini, but the gate vendor is codex; pass --gate-vendor gemini or a codex model'),
                (['--author-vendor', 'claude', '--reviewer-vendor', 'codex', '--gate-model', 'gpt-6.1-sol'],   # the two-vendor case, no KeyError
                 "belongs to codex, but the gate now defaults to the author's vendor claude")):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, text.replace('(', r'\(')):
                self.resolved(*flags)
        self.assert_refused("gate_model gemini-3.8-pro belongs to gemini, but the gate now defaults to the author's vendor codex",
                            '--gate-model', 'gemini-3.8-pro')
        profile = self.operator_profile({'allowed_models': {'gemini': ['house-model']}, 'gate_model': 'house-model'})
        with self.assertRaisesRegex(ValueError, 'belongs to gemini'):   # an id the allowlist assigns to gemini counts as known
            self.resolved('--config', str(profile))
        for model in ('claude-opus-4-6-thinking', 'gpt-oss-120b', 'gemini-3.8-pro'):   # G9: nothing is foreign to a gemini gate
            with self.subTest(model=model):
                a = self.resolved(*GEMINI_REVIEWER, '--gate-vendor', 'gemini', '--gate-model', model)
                self.assertEqual((a.gate_vendor, a.gate_model), ('gemini', model))

    # ---- G1: codex and claude are always bound (V317-FB); gemini_bin only when a role runs gemini ----
    def test_a_codex_and_claude_run_binds_no_agy(self):
        args = self.args('--gemini-bin', str(self.h.root / 'no-such-agy'))
        co = rc.Coordinator(args)
        self.assertEqual(rc.required_programs(co.args), ('claude_bin', 'codex_bin'))
        programs, issue = co._program_state()
        self.assertIsNone(issue)
        self.assertEqual(set(programs), {'codex_bin', 'claude_bin', 'gate_prompt', 'path_env'})
        self.assertEqual(co.state['operator_programs'], programs)

    def test_an_all_codex_run_still_requires_claude_bin(self):
        refused = self.main('run', '--reviewer-vendor', 'codex', '--gate-vendor', 'codex', '--claude-bin', str(self.h.root / 'no-such-claude'))
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn('configured claude_bin is missing or inside an author-writable root', refused.stdout)
        state = self.h.run_dir / 'state.json'   # as test_v317fb: refused before any turn
        self.assertEqual(json.loads(state.read_text())['turns'] if state.exists() else [], [])

    def test_a_gemini_role_requires_gemini_bin(self):
        h = self.h
        def snap(gemini_bin):
            return rc.program_snapshot(h.workspace, h.run_dir, h.run_dir / 'author-tmp', h.fake_codex_cli(), h.fake_claude_cli(),
                                       rc.DEFAULT_GATE_PROMPT, None, gemini_bin=gemini_bin)
        self.assertEqual(rc.required_programs(self.resolved(*GEMINI_GATE)), ('claude_bin', 'codex_bin', 'gemini_bin'))
        _, issue = snap(str(h.root / 'no-such-agy'))
        self.assertIn('configured gemini_bin is missing or inside an author-writable root', issue)
        agy = executable(h.root / 'bin' / 'agy', '1.4.10')
        programs, issue = snap(str(agy))
        self.assertIsNone(issue)
        self.assertEqual(set(programs), {'codex_bin', 'claude_bin', 'gemini_bin', 'gate_prompt', 'path_env'})
        self.assertEqual(programs['gemini_bin']['path'], str(agy.resolve()))

    def test_a_gemini_bin_update_in_place_is_a_normal_update(self):
        agy = executable(self.h.root / 'bin' / 'agy', '1.4.11')
        old = {'gemini_bin': {'path': str(agy), 'sha256': 'a'}}
        new = {'gemini_bin': {'path': str(agy), 'sha256': 'b'}}
        updates = rc.normal_cli_updates(old, new)
        self.assertEqual([(u['program'], u['new_version']) for u in updates], [('gemini_bin', '1.4.11')])

    # ---- G1: explicit vendor tables fail closed ----
    def test_a_vendor_table_without_an_entry_fails_closed(self):
        self.assertEqual(rc.vendor_entry({'codex': 1, 'claude': 2}, 'claude'), 2)
        with self.assertRaises(ValueError) as gemini:
            rc.vendor_entry({'codex': 1, 'claude': 2}, 'gemini')
        self.assertEqual(str(gemini.exception), B2)
        with self.assertRaisesRegex(ValueError, "unknown vendor 'mistral'"):
            rc.vendor_entry({'codex': 1, 'claude': 2}, 'mistral')
        co = rc.Coordinator(self.args())
        schema = self.h.root / 'schema.json'
        rc.atomic_json(schema, rc.review_schema())
        co.args.reviewer_vendor = 'mistral'
        for call in (lambda: co.command('reviewer', schema, True), lambda: co._vendor_binary('mistral'), co.reviewer_flags):
            with self.subTest(call=call), self.assertRaises(ValueError) as refused:
                call()
            self.assertEqual(str(refused.exception), "unknown vendor 'mistral'")


SETTINGS = '{"permissions":{"deny":["write_file(*)","read_url(*)","execute_url(*)","mcp(*)"],"allow":["command(*)"]}}'
PLAN_REVIEW = 'Role: reviewer, persistent plan-only reviewer. Phase: PLAN.'
GATE_PROMPT = 'You are an adversarial reviewer.\nRun this test command exactly as written in one Bash call: python3 -m unittest\n'


class GeminiAdapterTests(unittest.TestCase):
    """WI-GEM B2a (ADR-20 G5-G7): agy runs the reviewer, the shadow and the gate on the fake gemini CLI."""
    argv, args, resolved, main, assert_refused = (GeminiVendorTests.argv, GeminiVendorTests.args, GeminiVendorTests.resolved,
                                                  GeminiVendorTests.main, GeminiVendorTests.assert_refused)

    def setUp(self):
        GeminiVendorTests.setUp(self)
        self.log = self.h.root / 'agy-log.jsonl'
        env = patch.dict(os.environ, {'FAKE_GEMINI_LOG': str(self.log)})
        env.start()
        self.addCleanup(env.stop)

    def coordinator(self, *extra, action='run'):
        return rc.Coordinator(self.args(*GEMINI_REVIEWER, *GEMINI_GATE, '--gemini-bin', str(self.h.fake_gemini_cli()), *extra, action=action))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def receipt(self, co, sequence, phase, role):
        return json.loads((co.evidence / f'{sequence:03d}-{phase.lower()}-{role}.receipt.json').read_text())

    def test_the_argv_is_read_only_and_resumes_by_conversation(self):
        co = self.coordinator()
        schema = self.h.root / 'schema.json'
        rc.atomic_json(schema, rc.review_schema())
        agy = str(self.h.fake_gemini_cli())
        base = ['--print=', '--input-format', 'stream-json', '--output-format', 'stream-json']
        self.assertEqual(co.command('reviewer', schema, False), [
            agy, *base, '--model', 'gemini-3.8-flash-medium', '--effort', 'medium', '--json-schema', str(schema),
            '--sandbox', '--settings', SETTINGS])
        co.state['started']['reviewer'], co.state['sessions']['reviewer'] = True, 'conv-1'
        self.assertEqual(co.command('reviewer', schema, False)[-2:], ['--conversation', 'conv-1'])
        for role in ('shadow', 'gate'):   # fresh roles never resume
            command = co.command(role, schema, True)
            self.assertEqual(command[:6], [agy, *base])
            self.assertNotIn('--conversation', command)
            for flag in ('--dangerously-skip-permissions', '--print-timeout', '--mode', '-p'):
                self.assertFalse(any(arg == flag or arg.startswith(flag + '=') for arg in command), flag)
        self.assertEqual(co.command('gate', schema, True)[6:8], ['--model', 'gemini-3.8-pro'])
        flags = co.reviewer_flags()
        self.assertEqual((flags['reviewer_binary'], flags['permission_mode'], flags['sandbox'], flags['gemini_settings']),
                         (agy, 'settings-deny', 'agy-sandbox', rc.GEMINI_READONLY_SETTINGS))
        with self.assertRaisesRegex(ValueError, 'ADR-20 G2'):
            co._gemini_command('author', schema, False)

    def test_a_persistent_reviewer_gets_the_prompt_on_stdin_and_resumes_its_conversation(self):
        co = self.coordinator()
        prompt = PLAN_REVIEW + ' ' + 'x' * 120_000   # a long prompt goes on stdin, never in argv
        co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())   # invoke() makes the scratch TMPDIR
        first = self.calls()[0]
        self.assertEqual(first['stdin'], json.dumps({'event': 'user', 'message': {'content': prompt}}) + '\n')
        self.assertNotIn(prompt, first['args'])
        self.assertEqual(Path(first['cwd']).resolve(), self.h.workspace.resolve())
        self.assertEqual(Path(first['TMPDIR']).parent, co.run_dir / 'role-tmp')   # the per-dispatch scratch, as for Codex
        receipt = self.receipt(co, 1, 'PLAN', 'reviewer')
        self.assertEqual((receipt['vendor'], receipt['reported_model'], receipt['reported_model_source'], receipt['model_identity']),
                         ('gemini', 'gemini-3.8-flash-medium', 'init', 'MATCH'))
        self.assertEqual(receipt['usage_requests'], [{'input': 100, 'cached': 30, 'output': 20, 'source': 'result'}])
        conversation = co.state['sessions']['reviewer']
        self.assertTrue(conversation and co.state['started']['reviewer'])
        co._invoke_once('reviewer', 'PLAN', PLAN_REVIEW, rc.review_schema())
        self.assertEqual(self.calls()[1]['args'][-2:], ['--conversation', conversation])
        self.assertEqual(co.state['sessions']['reviewer'], conversation)
        with patch.dict(os.environ, {'FAKE_GEMINI_OTHER_CONVERSATION': 'someone-else'}), \
                self.assertRaisesRegex(RuntimeError, 'reviewer resumed a different session'):
            co._invoke_once('reviewer', 'PLAN', PLAN_REVIEW, rc.review_schema())

    def test_a_gemini_gate_approval_counts_the_test_run_from_a_tool_step(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_GEMINI_TOOL_ERROR': '1'}):   # a tool step in ERROR mid-turn does not fail the turn
            result = co._invoke_once('gate', 'EXEC', GATE_PROMPT, rc.gate_schema(), fresh=True)
        self.assertEqual(result['answer']['verdict'], 'approve')
        observed = self.receipt(co, 1, 'EXEC', 'gate')['observed_commands']
        self.assertEqual([(row['command'], row['exit_code'], row['source']) for row in observed], [('python3 -m unittest', 0, 'run_command')])
        self.assertNotIn('--conversation', self.calls()[0]['args'])
        with patch.dict(os.environ, {'FAKE_MISSING_OBSERVED': '1'}), \
                self.assertRaisesRegex(RuntimeError, 'gate EXEC approval lacks an observed successful configured test command'):
            co._invoke_once('gate', 'EXEC', GATE_PROMPT, rc.gate_schema(), fresh=True)

    def test_parsing_reads_tool_steps_and_falls_back_to_the_response(self):
        rows = [{'event': 'step_update', 'step_update': {'step_index': 2, 'state': 'ACTIVE', 'step_type': 'tool', 'tool_name': 'run_command',
                                                         'tool_info': {'parameters': {'CommandLine': 'make test'}}}},
                {'event': 'step_update', 'step_update': {'step_index': 3, 'state': 'DONE', 'step_type': 'tool', 'tool_name': 'run_command',
                                                         'tool_info': {'parameters': {'CommandLine': 'false'}, 'output': 'Command exited with code 1.\n'}}},
                {'event': 'result', 'result': {'status': 'SUCCESS', 'conversation_id': 'c1', 'response': '{"verdict": "approve"}'}}]
        commands, calls = rc.observed_events('gemini', rows)
        self.assertEqual([(row['command'], row['exit_code'], row['error'], row.get('evidence_kind')) for row in commands],
                         [('make test', None, False, 'started; no exit status when the turn ended'), ('false', 1, True, None)])
        self.assertEqual(calls[1], {'tool': 'command_execution', 'input': {'command': 'false'}, 'error': True})
        self.assertEqual(rc.gemini_turn(rows, '', '')[:2], ({'verdict': 'approve'}, 'c1'))
        self.assertTrue(rc.failed_turn_did_work('gemini', self._lines(rows[:1])))

    def _lines(self, rows):
        path = self.h.root / 'stream.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def test_each_hazard_fails_the_turn_with_its_reason(self):
        cases = (({'FAKE_GEMINI_FAIL': 'denied'}, r'agy denied write_file; a denied permission ends the turn \(ADR-20 G6\)'),
                 ({'FAKE_GEMINI_FAIL': 'empty'}, 'agy returned an empty response and no structured_output'),
                 ({'FAKE_GEMINI_FAIL': 'error'}, 'CLI exit 1: fake agy failure'),
                 ({'FAKE_GEMINI_FAIL': 'error', 'FAKE_GEMINI_FAIL_EXIT': '0', 'FAKE_GEMINI_ERROR': 'quota zq-17 left the building'},
                  'agy turn status ERROR: quota zq-17 left the building'),   # unknown error text, verbatim
                 ({'FAKE_GEMINI_FAIL': 'no-result'}, 'agy emitted no result'),
                 ({'FAKE_GEMINI_MODEL': 'gemini-other'},
                  'model identity mismatch: reviewer configured gemini-3.8-flash-medium, CLI reported gemini-other'))
        for index, (env, message) in enumerate(cases):
            with self.subTest(env=env):
                self.h.run_dir = self.h.root / f'hazard-{index}'
                co = self.coordinator()
                with patch.dict(os.environ, env), self.assertRaisesRegex(RuntimeError, message):
                    co._invoke_once('reviewer', 'PLAN', PLAN_REVIEW, rc.review_schema())
                state = json.loads(co.state_path.read_text())
                self.assertRegex(state['turns'][-1]['error'], message)
                self.assertFalse(state['started']['reviewer'])
                self.assertIsNone(state['sessions']['reviewer'])
        self.h.run_dir = self.h.root / 'no-structured'
        with patch.dict(os.environ, {'FAKE_GEMINI_NO_STRUCTURED': '1'}):   # G7: the response is the fallback answer
            self.assertEqual(self.coordinator()._invoke_once('reviewer', 'PLAN', PLAN_REVIEW, rc.review_schema())['answer']['status'], 'APPROVE')

    def test_other_role_paths_refuse_gemini_before_any_turn(self):
        co = self.coordinator()
        for role, phase in (('reviewer', 'DOCS'), ('reviewer', 'SECURITY'), ('reviewer', 'POLISH-Q'), ('probe', 'PROBE'),
                            ('gate-probe', 'PROBE'), ('gate', 'PLAN')):
            with self.subTest(role=role, phase=phase), self.assertRaisesRegex(RuntimeError, f'{role} {phase} turns cannot run gemini yet'):
                co.invoke(role, phase, PLAN_REVIEW, rc.review_schema(), fresh=True)
        self.assertEqual((co.state['turns'], co.state['invocations_used'], self.calls()), ([], 0, []))

    def test_a_frozen_gemini_bin_is_rebound_on_resume(self):
        co = self.coordinator()
        frozen = co._program_state()[0]
        self.assertIn('gemini_bin', co.state['operator_programs'])
        resumed = self.coordinator(action='resume')
        self.assertEqual(resumed._program_state(), (frozen, None))
        other = self.h.root / 'other-agy'
        other.write_text(self.h.fake_gemini_cli().read_text())
        other.chmod(0o755)
        resumed.args.gemini_bin = str(other)
        _, issue = resumed._program_state()
        self.assertRegex(issue, r'operator program or PATH changed since run start \(changed: gemini_bin\)')

    def test_a_saved_run_without_gemini_bin_resumes_unchanged(self):
        co = rc.Coordinator(self.args())
        frozen = co._program_state()[0]
        resumed = rc.Coordinator(self.args('--gemini-bin', str(self.h.fake_gemini_cli()), action='resume'))
        self.assertEqual(resumed._program_state(), (frozen, None))
        self.assertNotIn('gemini_bin', json.loads(resumed.state_path.read_text())['operator_programs'])

    def run_cli(self, *flags):
        h = self.h
        command = [arg for arg in h.command('--exercise-revisions', *flags) if arg != '--strict']   # gemini roles run efficient (G4)
        return subprocess.run(command, cwd=h.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_a_codex_author_with_gemini_reviewer_and_gate_ends_like_codex_and_claude(self):
        h, outcomes = self.h, {}
        for name, flags in (('codex-claude', ()), ('gemini', (*GEMINI_REVIEWER, *GEMINI_GATE, '--gemini-bin', str(h.fake_gemini_cli())))):
            h.run_dir = h.root / ('run-' + name)
            subprocess.run(['git', 'checkout', '-q', '--', '.'], cwd=h.workspace, check=True)
            subprocess.run(['git', 'clean', '-qfd'], cwd=h.workspace, check=True)
            done = self.run_cli(*flags)
            state = json.loads((h.run_dir / 'state.json').read_text())
            outcomes[name] = (done.returncode, done.stdout.splitlines()[-1:], state['status'],
                              [(t['role'], t['phase'], (t.get('answer') or {}).get('status', (t.get('answer') or {}).get('verdict')))
                               for t in state['turns']])
            if name == 'gemini':
                self.assertEqual({t['vendor'] for t in state['turns'] if t['role'] != 'author'}, {'gemini'})
                reviewer_calls = [c['args'] for c in self.calls() if '--conversation' in c['args']]
                self.assertTrue(reviewer_calls)   # the persistent reviewer resumed its conversation after the exercise REVISE
                self.assertEqual({c[c.index('--conversation') + 1] for c in reviewer_calls}, {state['sessions']['reviewer']})
        self.assertEqual(outcomes['gemini'], outcomes['codex-claude'], outcomes)
        self.assertEqual(outcomes['gemini'][2], 'DONE', outcomes)
        self.assertLessEqual({'reviewer', 'shadow', 'gate'}, {role for role, _, _ in outcomes['gemini'][3]})


if __name__ == '__main__':
    unittest.main()
