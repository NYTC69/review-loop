"""WI-GEM B1 (ADR-20 G1-G4, G9, G10): gemini as a third vendor value; validation and binding only, no agy dispatch."""
import contextlib
import io
import json
import types
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc
from paired_session import timeout_scale as tsc

rc = trc.rc
B2 = 'gemini roles cannot be dispatched yet (WI-GEM B2)'
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
                with self.assertRaisesRegex(ValueError, B2.replace('(', r'\(').replace(')', r'\)')):
                    rc.refuse_gemini_dispatch(a)

    def test_b1_refuses_a_gemini_role_before_state_or_any_turn(self):
        for flags in (GEMINI_REVIEWER, GEMINI_GATE, [*GEMINI_REVIEWER, *GEMINI_GATE]):
            self.assert_refused(B2, *flags)
            with self.subTest(coordinator=flags), self.assertRaises(ValueError) as refused:
                rc.Coordinator(self.args(*flags))
            self.assertEqual(str(refused.exception), B2)
            self.assertFalse(self.h.run_dir.exists())

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
    def test_vendor_tables_refuse_gemini_until_b2_and_an_unknown_vendor(self):
        self.assertEqual(rc.vendor_entry({'codex': 1, 'claude': 2}, 'claude'), 2)
        with self.assertRaises(ValueError) as gemini:
            rc.vendor_entry({'codex': 1, 'claude': 2}, 'gemini')
        self.assertEqual(str(gemini.exception), B2)
        with self.assertRaisesRegex(ValueError, "unknown vendor 'mistral'"):
            rc.vendor_entry({'codex': 1, 'claude': 2}, 'mistral')
        co = rc.Coordinator(self.args())
        schema = self.h.root / 'schema.json'
        rc.atomic_json(schema, rc.review_schema())
        for vendor, message in (('gemini', B2), ('mistral', "unknown vendor 'mistral'")):
            co.args.reviewer_vendor = vendor
            for call in (lambda: co.command('reviewer', schema, True), lambda: co._vendor_binary(vendor), co.reviewer_flags):
                with self.subTest(vendor=vendor, call=call), self.assertRaises(ValueError) as refused:
                    call()
                self.assertEqual(str(refused.exception), message)


if __name__ == '__main__':
    unittest.main()
