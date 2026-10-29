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
B2 = 'this gemini role path cannot be dispatched: gemini runs read-only roles only (ADR-20 G2)'
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
                rc.refuse_gemini_probe(a)   # dispatchable outside a permission probe

    def test_a_gemini_reviewer_with_stage_reviews_creates_its_run(self):   # B2b: lifecycle on and report mode run gemini
        co = rc.Coordinator(self.args(*GEMINI_REVIEWER, '--lifecycle-mode', 'on', '--gemini-bin', str(self.h.fake_gemini_cli())))
        self.assertEqual((co.state['config']['lifecycle_mode'], co.args.reviewer_vendor), ('on', 'gemini'))
        report = self.resolved(*GEMINI_REVIEWER)
        report.review_report = True
        rc.refuse_gemini_probe(report)

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
        for role, phase in (('probe', 'PROBE'), ('gate-probe', 'PROBE'), ('gate', 'PLAN')):
            with self.subTest(role=role, phase=phase), self.assertRaisesRegex(RuntimeError, fr'{role} {phase} turns cannot run gemini \(ADR-20 G2, G4\)'):
                co.invoke(role, phase, PLAN_REVIEW, rc.review_schema(), fresh=True)
        self.assertEqual((co.state['turns'], co.state['invocations_used'], self.calls()), ([], 0, []))

    def test_a_permission_probe_with_a_gemini_reviewer_or_gate_is_refused_before_any_turn(self):   # B2a r1 (Codex review)
        refusal = 'permission-probe cannot run with a gemini reviewer or gate'
        agy = ['--gemini-bin', str(self.h.fake_gemini_cli()), '--lifecycle-mode', 'off']   # lifecycle on refuses a gemini reviewer first
        for name, flags in (('gate', [*GEMINI_GATE, *agy]), ('reviewer', [*GEMINI_REVIEWER, *agy]),
                            ('both', [*GEMINI_REVIEWER, *GEMINI_GATE, *agy])):
            self.h.run_dir = self.h.root / ('new-' + name)
            self.assert_refused(refusal, *flags, actions=('permission-probe',))   # a new run: before any state
            self.h.run_dir = self.h.root / ('saved-' + name)
            rc.Coordinator(self.args(*flags))
            with self.subTest(run='saved', roles=name):   # a restored run: its frozen vendors, before the reviewer or author probe
                refused = self.main('permission-probe', *flags)
                self.assertEqual(refused.returncode, 2, refused.stdout)
                self.assertIn('REFUSED: ' + refusal, refused.stdout)
                state = json.loads((self.h.run_dir / 'state.json').read_text())
                self.assertEqual((state['sequence'], state['turns'], state['invocations_used'], state.get('active')), (0, [], 0, None))
                self.assertFalse((self.h.run_dir / 'permission-probe.json').exists())
                self.assertEqual(list((self.h.run_dir / 'evidence').glob('*')) if (self.h.run_dir / 'evidence').exists() else [], [])
        self.assertEqual(self.calls(), [])

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


class GeminiParityTests(unittest.TestCase):
    """WI-GEM B2b: global config (G8), agy version, prompt wording, the independence scan, recovery usage and typed evidence."""
    argv, args, resolved, main = GeminiVendorTests.argv, GeminiVendorTests.args, GeminiVendorTests.resolved, GeminiVendorTests.main
    setUp, coordinator, calls, receipt = (GeminiAdapterTests.setUp, GeminiAdapterTests.coordinator, GeminiAdapterTests.calls,
                                          GeminiAdapterTests.receipt)

    def settings(self, document):
        path = self.h.test_home / rc.GEMINI_SETTINGS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
        return path

    def test_agy_settings_are_snapshotted_for_gemini_turns_only(self):
        home = self.h.test_home
        self.settings({'colorScheme': 'dark', 'model': 'gemini-3.8-flash', 'trustedWorkspaces': ['/a']})
        (home / '.gemini' / 'antigravity-cli' / 'antigravity-oauth-token').write_text('secret')
        self.assertEqual(set(rc.global_config_snapshot(home)), {'codex_config', 'claude_settings', 'claude_plugins'})   # codex, claude
        before = rc.global_config_snapshot(home, gemini=True)
        self.assertEqual(before['gemini_settings']['path'], str(home / '.gemini' / 'antigravity-cli' / 'settings.json'))
        self.assertNotIn('secret', json.dumps(before))
        for document, expected in (({'colorScheme': 'dark', 'model': 'gemini-3.8-pro', 'trustedWorkspaces': ['/a', '/b']},
                                     [{'file': 'gemini_settings', 'change': 'agy-preference', 'keys': ['model', 'trustedWorkspaces']}]),
                                    ({'colorScheme': 'light', 'model': 'gemini-3.8-flash', 'trustedWorkspaces': ['/a']}, None)):
            with self.subTest(document=document):
                self.settings(document)
                changes = rc.attribute_global_config_changes(before, rc.global_config_snapshot(home, gemini=True))
                if expected:
                    self.assertEqual((changes['status'], changes['expected_changes']), ('PASS', expected))
                else:
                    self.assertEqual(changes['findings'], [{'file': 'gemini_settings', 'reason': 'unexpected-content-change'}])

    def test_a_gemini_turn_keeps_an_agy_preference_change_and_holds_on_any_other(self):
        self.settings({'colorScheme': 'dark', 'model': 'gemini-3.8-flash', 'trustedWorkspaces': []})
        source = self.h.root / 'agy-settings.json'
        source.write_text(json.dumps({'colorScheme': 'dark', 'model': 'gemini-3.8-pro', 'trustedWorkspaces': [str(self.h.workspace)]}))
        co, out = self.coordinator(), io.StringIO()
        with patch.dict(os.environ, {'FAKE_GEMINI_SETTINGS_WRITE': str(source)}), contextlib.redirect_stdout(out):
            co._invoke_once('reviewer', 'PLAN', PLAN_REVIEW, rc.review_schema())
        receipt = self.receipt(co, 1, 'PLAN', 'reviewer')
        self.assertEqual(list(receipt['global_gemini_before']), ['gemini_settings'])
        self.assertEqual(receipt['global_config_changes']['expected_changes'],
                         [{'file': 'gemini_settings', 'change': 'agy-preference', 'keys': ['model', 'trustedWorkspaces']}])
        self.assertIn('NOTE: agy updated its own settings (model, trustedWorkspaces) during the reviewer turn', out.getvalue())
        source.write_text(json.dumps({'colorScheme': 'light'}))
        Path(str(source) + '.done').unlink()
        with patch.dict(os.environ, {'FAKE_GEMINI_SETTINGS_WRITE': str(source)}), \
                self.assertRaisesRegex(RuntimeError, 'gemini turn changed global config: gemini_settings'):
            co._invoke_once('reviewer', 'PLAN', PLAN_REVIEW, rc.review_schema())

    def test_a_claude_reviewer_turn_records_no_gemini_label(self):
        self.settings({'model': 'gemini-3.8-flash'})
        co = rc.Coordinator(self.args())
        co._invoke_once('reviewer', 'PLAN', PLAN_REVIEW, rc.review_schema())
        receipt = self.receipt(co, 1, 'PLAN', 'reviewer')
        self.assertEqual(receipt['vendor'], 'claude')
        self.assertEqual(set(receipt['global_config_changes']['before']), {'codex_config', 'claude_settings', 'claude_plugins'})
        self.assertNotIn('gemini_cli_version', receipt)
        self.assertNotIn('gemini_cli_version', co.state)

    def test_the_agy_version_is_recorded_per_turn_and_a_change_is_a_note(self):
        co, out = self.coordinator(), io.StringIO()
        with contextlib.redirect_stdout(out):
            co._invoke_once('reviewer', 'PLAN', PLAN_REVIEW, rc.review_schema())
            with patch.dict(os.environ, {'FAKE_GEMINI_VERSION': '1.4.11'}):
                co._invoke_once('reviewer', 'PLAN', PLAN_REVIEW, rc.review_schema())
        first, second = self.receipt(co, 1, 'PLAN', 'reviewer'), self.receipt(co, 2, 'PLAN', 'reviewer')
        self.assertEqual((first['gemini_cli_version'], 'gemini_cli_version_previous' in first), ('1.4.10', False))
        self.assertEqual((second['gemini_cli_version'], second['gemini_cli_version_previous']), ('1.4.11', '1.4.10'))
        self.assertEqual(co.state['gemini_cli_version'], '1.4.11')
        self.assertIn('NOTE: agy version changed since the previous gemini turn (1.4.10 -> 1.4.11)', out.getvalue())

    def test_gemini_prompts_name_agy_tools_and_codex_and_claude_wording_is_unchanged(self):
        claude = rc.Coordinator(self.args())
        self.h.run_dir = self.h.root / 'codex-run'
        codex = rc.Coordinator(self.args('--reviewer-vendor', 'codex'))
        self.assertEqual(claude.inspection_prompt('reviewer'),
                         'Use Read/Grep/Glob on context and workspace; Git through Bash is intentionally unavailable.')
        self.assertTrue(codex.inspection_prompt('reviewer').startswith('For file inspection, use read-only shell reads (cat, sed, or rg)'))
        self.assertIn('Analyse those concerns with Read/Grep/Glob instead.', claude._review_protocol([]))
        self.h.run_dir = self.h.root / 'gemini-run'
        co = self.coordinator()
        for role in ('reviewer', 'shadow', 'gate'):
            self.assertIn('your file reading and search tools', co.inspection_prompt(role))
            self.assertNotIn('Read/Grep/Glob', co.inspection_prompt(role))
        self.assertIn('Analyse those concerns with your file reading and search tools instead.', co._review_protocol([]))
        for each in (co, claude):
            (each.context / 'plan.md').write_text('Plan body\n')
            each.state['phase'] = 'PLAN'
        self.assertIn('## Plan content\nPlan body', co._review_prompt('reviewer', ''))   # inline, as for Codex
        self.assertNotIn('## Plan content', claude._review_prompt('reviewer', ''))

    def test_the_independence_scan_counts_gemini_only_in_a_gemini_run(self):
        claude = rc.Coordinator(self.args())
        self.h.run_dir = self.h.root / 'gemini-run'
        gemini = self.coordinator()
        text = 'Claude approved it; Gemini found it; gemini-3.8-flash said so.'
        self.assertEqual(claude._history_markers('prompt', text), ['Claude', 'Claude'])   # unchanged: the attribution and the bare name
        self.assertEqual(gemini._history_markers('prompt', text), ['Claude', 'Claude', 'Gemini', 'gemini'])
        for co in (claude, gemini):
            co.state.update(phase='EXEC', exec_rounds=1, next='author')
        self.assertIn(rc.AUTHOR_HISTORY_RULE, claude._author_prompt())
        self.assertNotIn('Gemini', claude._author_prompt())
        self.assertIn(rc.GEMINI_AUTHOR_HISTORY_RULE, gemini._author_prompt())
        self.assertIn('(Claude, Codex, Opus, Astra, Gemini)', rc.GEMINI_AUTHOR_HISTORY_RULE)
        self.assertEqual(gemini._introduced_history('context/workitem.md', 'Run the reviewer on Gemini.'), [])   # operator input

    def test_recovery_rebuilds_gemini_usage_and_notes_a_settings_change(self):
        co = self.coordinator()
        self.settings({'model': 'gemini-3.8-pro'})
        rows = [{'event': 'init', 'conversation_id': 'conv-9', 'init': {'model': 'gemini-3.8-flash-medium'}},
                {'event': 'result', 'result': {'status': 'SUCCESS', 'conversation_id': 'conv-9',
                                               'usage': {'input_tokens': 70, 'output_tokens': 20, 'cache_read_tokens': 30}}}]
        (co.evidence / '001-exec-reviewer.stdout.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        receipt = {'sequence': 1, 'role': 'reviewer', 'phase': 'EXEC', 'vendor': 'gemini', 'usage_requests': [], 'model_requests': 0,
                   'global_gemini_before': {'gemini_settings': 'stale'}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            co.archive_abandoned_turn(receipt)
        row = co.state['abandoned_turns'][-1]
        self.assertEqual((row['usage_requests'], row['provider_usage']),
                         ([{'input': 100, 'cached': 30, 'output': 20, 'source': 'result-stream'}], 'reported'))
        self.assertEqual(list(row['global_gemini_after']), ['gemini_settings'])
        self.assertIn('NOTE: agy settings changed during the uncertain gemini turn (gemini_settings)', out.getvalue())

    def test_an_agy_read_file_step_is_a_typed_read_for_the_evidence_guard(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_GEMINI_READ_FILE': str(self.h.workspace / 'tracked.txt')}):
            co._invoke_once('reviewer', 'PLAN', PLAN_REVIEW, rc.review_schema())
        receipt = self.receipt(co, 1, 'PLAN', 'reviewer')
        self.assertIn({'tool': 'read_file', 'input': {'Path': str(self.h.workspace / 'tracked.txt')}, 'error': False},
                      receipt['observed_tool_calls'])
        self.assertEqual(receipt['evidence_guard']['fallbacks'], 0)
        evidence = co.evidence / '001-plan-reviewer.receipt.json'
        for tool in ('read_file', 'write_file'):
            with self.subTest(tool=tool):
                self.assertEqual(rc.sensitive_access([{'tool': tool, 'input': {'Path': str(evidence)}}], 'reviewer', co.evidence,
                                                     co.rounds, self.h.workspace), 'evidence directory')
        fallbacks = []   # a tool step agy names otherwise is recorded through the fallback guard, never a crash
        self.assertIsNone(rc.sensitive_access([{'tool': 'view_file', 'input': {'Path': 'missing.txt'}}], 'reviewer', co.evidence,
                                              co.rounds, self.h.workspace, fallbacks=fallbacks))
        self.assertEqual(fallbacks, ['unknown tool view_file'])


class GeminiStageReviewTests(unittest.TestCase):
    """WI-GEM B2b: with lifecycle on and in report mode the stage reviews (DOCS, SECURITY, POLISH-Q) run on a gemini reviewer."""
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in (
        'setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli', 'fake_claude_cli',
        'fake_gemini_cli', 'command')})

    def setUp(self):
        from paired_session.test_worktree_lifecycle import COVERING_GITIGNORE   # security preflight is clean
        trc.RealCoordinatorTests.setUp(self)
        (self.workspace / '.gitignore').write_text(COVERING_GITIGNORE)
        subprocess.run(['git', 'commit', '-qam', 'ignore sensitive files'], cwd=self.workspace, check=True)

    def run_pair(self, flags, markers=(), env=None, prepare=lambda: None):
        """The same efficient run with codex+claude and with a gemini reviewer and gate: (role, phase, status) per turn.
        Each run gets its own file for every name in `markers` (a once-only fake behaviour)."""
        outcomes = {}
        for name, extra in (('codex-claude', ()), ('gemini', (*GEMINI_REVIEWER, *GEMINI_GATE, '--gemini-bin', str(self.fake_gemini_cli())))):
            self.run_dir = self.root / ('run-' + name)
            subprocess.run(['git', 'checkout', '-q', '--', '.'], cwd=self.workspace, check=True)
            subprocess.run(['git', 'clean', '-qfd'], cwd=self.workspace, check=True)
            prepare()
            marker = {key: str(self.root / (name + '-' + key)) for key in markers}
            command = [arg for arg in self.command(*flags, *extra) if arg != '--strict']
            done = subprocess.run(command, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env={**os.environ, **(env or {}), **marker})
            state = json.loads((self.run_dir / 'state.json').read_text())
            outcomes[name] = (done.returncode, state['status'], [
                (t['role'], t['phase'], t.get('discarded'), (t.get('answer') or {}).get('status', (t.get('answer') or {}).get('verdict')))
                for t in state['turns']])
            if name == 'gemini':
                self.assertEqual({t['vendor'] for t in state['turns'] if t['role'] != 'author'}, {'gemini'}, done.stdout)
                gemini_state = state
        self.assertEqual(outcomes['gemini'], outcomes['codex-claude'], outcomes)
        return gemini_state

    def test_lifecycle_on_runs_the_stage_reviews_on_gemini(self):
        state = self.run_pair(['--lifecycle-mode', 'on'], markers=('FAKE_SPECIALIST_NO_TOOLS_ONCE',),
                              env={'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'})   # a docs write gets a docs review
        self.assertEqual((state['status'], state['lifecycle']['stage']), ('DONE', 'DONE'))
        self.assertEqual([row['stage'] for row in state['lifecycle']['receipts']], ['FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])
        stages = [t for t in state['turns'] if t['phase'] in ('DOCS', 'SECURITY', 'POLISH-Q') and t['role'] == 'reviewer']
        self.assertEqual({t['phase'] for t in stages}, {'DOCS', 'SECURITY', 'POLISH-Q'})
        self.assertTrue(all('--conversation' not in t['command'] for t in stages))   # stage reviews are fresh
        discarded = [t for t in stages if t.get('discarded')]   # a gemini specialist turn without tool steps is retried once
        self.assertEqual([(t['phase'], t['discarded'], t['observed_tool_calls']) for t in discarded], [('POLISH-Q', 'no tool calls', [])])
        self.assertTrue(all(t['observed_tool_calls'] for t in stages if not t.get('discarded')))

    def test_report_mode_runs_on_a_gemini_reviewer(self):
        def change():
            (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        state = self.run_pair(['--review-only', '--review-report', '--lifecycle-mode', 'on'], prepare=change)
        self.assertEqual((state['status'], state['lifecycle']['stage']), ('REPORTED', 'REPORTED'))
        self.assertEqual(sum(t['phase'] == 'SECURITY' for t in state['turns']), 1)
        self.assertTrue(any(t['phase'] == 'POLISH-Q' for t in state['turns']))


if __name__ == '__main__':
    unittest.main()
