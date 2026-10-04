"""v297-eg-wire: the hybrid wiring of the typed-operation evidence guard (owner 2026-10-04). A call the guard resolves is decided by
it (PROTECTED holds, ALLOW passes where the old substring match held by mistake); a call it cannot resolve falls back to the
pre-v2.9.7 substring guard exactly as before, and the receipt counts those fallbacks. The post-dispatch call site gets the call's own
context (Popen cwd, child env without secret-named variables, writable roots, configured commands); Codex keeps a command's workdir;
admission refuses only a configured command that names a protected path."""
import json
import unittest
from unittest import mock

from paired_session import test_real_coordinator as trc

rc = trc.rc


class EvidenceGuardWiringTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.evidence, self.rounds = self.h.run_dir / 'evidence', self.h.run_dir / 'rounds'

    def access(self, command, role='reviewer', **kw):
        return rc.sensitive_access([{'tool': 'Bash', 'input': {'command': command}}], role, self.evidence, self.rounds,
                                   kw.pop('cwd', self.h.workspace), **kw)

    def test_same_name_workspace_paths_no_longer_hold(self):   # the substring guard's false positives
        (self.h.workspace / 'tests' / 'evidence').mkdir(parents=True)
        for command in ('cat tests/evidence/a.txt', 'pytest -q tests/evidence/', "rg -n 'evidence/|rounds/' src", "grep -R 'evidence/' tests",
                        f"rg -n '{self.evidence}' src"):
            with self.subTest(command=command):
                self.assertIsNotNone(rc._legacy_sensitive_access([{'tool': 'Bash', 'input': {'command': command}}], 'reviewer', self.evidence, self.rounds))
                self.assertIsNone(self.access(command))
        result = self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--test-command', 'ls tests/evidence/',   # relative: the call's cwd
                                        env={'FAKE_SENSITIVE_READ': str(self.h.workspace / 'tests' / 'evidence' / 'a.txt')})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('hold_reason', json.loads((self.h.run_dir / 'state.json').read_text()))

    def test_a_protected_access_through_a_parseable_form_still_holds(self):
        for command, reason in ((f'echo x > {self.evidence}/a', 'evidence directory'), ('cat ../run/evidence/a.txt', 'evidence directory'),
                                ('cd .. && cat run/rounds/03-reviewer.md', 'review output directory'),   # the substring guard missed this one
                                ('cat ../run/rounds/07-shadow-approve.md', 'shadow output')):
            with self.subTest(command=command):
                self.assertEqual(self.access(command), reason)
        result = self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                        env={'FAKE_SENSITIVE_READ': str(self.evidence / 'secret.json')})
        self.assertEqual(result.returncode, 2)
        self.assertIn('accessed isolated evidence directory', json.loads((self.h.run_dir / 'state.json').read_text())['hold_reason'])

    def test_an_unparseable_form_behaves_exactly_as_today_and_is_counted(self):
        commands = ['cat "$(resolve)"', 'cat "$(resolve)" evidence/a.txt', f'cat {self.evidence}/x; echo $(date)', 'for f in *; do cat $f; done',
                    'for f in evidence/*; do cat $f; done', "python3 -c 'print(1)'", "python3 -c \"open('evidence/a').read()\"",
                    "python3 - <<'EOF'\nprint(1)\nEOF", 'SDK=$(xcrun --show-sdk-path) && ls "$SDK"', f'ls `echo {self.rounds}`',
                    'cat rounds/x-adversarial-1.md; echo $(true)', 'eval "cat x"']
        for role in ('author', 'reviewer', 'shadow', 'gate', 'probe'):
            for command in commands:
                with self.subTest(role=role, command=command):
                    fallbacks = []
                    call = [{'tool': 'Bash', 'input': {'command': command}}]
                    hybrid = rc.sensitive_access(call, role, self.evidence, self.rounds, self.h.workspace, {}, (self.h.workspace,), (), fallbacks)
                    legacy = rc._legacy_sensitive_access(call, role, self.evidence, self.rounds)
                    if str(self.evidence) in command or str(self.rounds) in command:   # a literal protected root: the guard's backstop holds first
                        self.assertEqual((hybrid, fallbacks), ('evidence or review output path in unresolved input', []))
                        self.assertIsNotNone(legacy)
                    else:
                        self.assertEqual((hybrid, len(fallbacks)), (legacy, 1))

    def test_an_unresolved_call_leaves_the_rest_of_the_turn_to_the_substring_guard(self):   # it may have run cd or export
        for first, second in (('cd ../run && ls $(echo .)', 'cat evidence/001-plan-author.prompt.txt'),
                              ('export HOME=../run; $(true)', 'cat ~/evidence/x')):
            with self.subTest(first=first):
                fallbacks = []
                calls = [{'tool': 'Bash', 'input': {'command': c}} for c in (first, second)]
                self.assertEqual(rc.sensitive_access(calls, 'reviewer', self.evidence, self.rounds, self.h.workspace,
                                                     {'HOME': str(self.h.test_home)}, (self.h.workspace,), (), fallbacks), 'evidence directory')
                self.assertEqual(len(fallbacks), 2)

    def test_a_codex_native_command_has_no_known_cwd(self):   # its event carries no workdir: relative operands fall back
        result = self.h.run_coordinator('--shadow', 'off', '--gate-vendor', 'codex', '--gate-model', 'gpt-6-luna', '--test-command', 'ls tests/evidence/')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = json.loads((self.h.run_dir / 'state.json').read_text())
        self.assertIn('gate accessed isolated evidence directory', state['hold_reason'])
        self.assertEqual([t['evidence_guard']['fallbacks'] for t in state['turns'] if t.get('role') == 'reviewer' and 'evidence_guard' in t][-1:], [0])

    def test_the_receipt_records_the_fallbacks(self):
        result = self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--test-command', 'python3 -m unittest $(true)')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipts = [t for t in json.loads((self.h.run_dir / 'state.json').read_text())['turns'] if 'evidence_guard' in t]
        self.assertTrue(receipts)
        self.assertTrue(any(t['evidence_guard']['fallbacks'] >= 1 and 'command substitution' in t['evidence_guard']['fallback_reasons'][0]
                            for t in receipts), receipts)

    def test_a_configured_inline_command_is_resolved_not_a_fallback(self):   # the call site passes the configured bytes
        result = self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--test-command', "python3 -c 'import sys'")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipts = [t['evidence_guard'] for t in json.loads((self.h.run_dir / 'state.json').read_text())['turns'] if 'evidence_guard' in t]
        self.assertTrue(receipts)
        self.assertFalse([r for t in receipts for r in t['fallback_reasons'] if 'inline code' in r], receipts)
        fallbacks = []
        self.access("python3 -c 'import sys'", fallbacks=fallbacks)
        self.assertEqual(len(fallbacks), 1)   # the same bytes, not configured: unresolved

    def test_a_secret_named_variable_never_expands_into_a_reason(self):
        fallbacks = []
        self.access('cat "$FIELD_EG_TOKEN/a"', env={'FIELD_EG_TOKEN': 'secret-value-1234'}, fallbacks=fallbacks)
        self.assertTrue(fallbacks and '$FIELD_EG_TOKEN' in fallbacks[0])
        self.assertNotIn('secret-value-1234', fallbacks[0])
        self.assertIsNone(self.access('cat "$HOME/x"', env={'HOME': str(self.h.test_home)}))

    def test_a_guard_failure_falls_back_for_every_call_and_reasons_are_bounded(self):
        fallbacks = []
        with mock.patch.object(rc.evidence_guard, 'turn_verdicts', side_effect=RecursionError):
            self.assertEqual(self.access('cat evidence/a', fallbacks=fallbacks), 'evidence directory')
            self.assertIsNone(self.access('cat src/a.py', fallbacks=fallbacks))
        self.assertEqual(fallbacks, ['evidence guard error: RecursionError'] * 2)
        fallbacks = []
        self.assertIsNone(self.access('cat x*/../' + 'a' * 400, fallbacks=fallbacks))
        self.assertEqual((len(fallbacks), len(fallbacks[0])), (1, 300))

    def test_codex_keeps_the_workdir_of_a_command(self):
        rows = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'command': "bash -lc 'cat rounds/x.md'", 'exit_code': 0,
                                                    'cwd': str(self.h.run_dir)}},
                {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'ls', 'exit_code': 0}}]
        _, calls = rc.observed_events('codex', rows)
        self.assertEqual([c['input'] for c in calls], [{'command': 'cat rounds/x.md', 'workdir': str(self.h.run_dir)}, {'command': 'ls'}])
        self.assertEqual(rc.sensitive_access(calls, 'reviewer', self.evidence, self.rounds, None), 'review output directory')
        fallbacks = []
        _, calls = rc.observed_events('codex', [{'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'cat x/../evidence/a',
                                                                                    'exit_code': 0}}])
        self.assertEqual(rc.sensitive_access(calls, 'reviewer', self.evidence, self.rounds, None, fallbacks=fallbacks), 'evidence directory')
        self.assertEqual(len(fallbacks), 1)   # no workdir, no known cwd: the substring guard decides, as before

    def test_admission_refuses_only_a_configured_command_that_names_a_protected_path(self):
        self.assertIsNone(rc.configured_command_issue(self.h.workspace, self.h.run_dir, [
            'python3 -m unittest', 'python3 -c pass', "/bin/bash -c 'python3 -m unittest && true'", 'cat "$(x)"', 'for f in a; do :; done']))
        self.assertIn('names a protected path (evidence directory)',
                      rc.configured_command_issue(self.h.workspace, self.h.run_dir, ['npm test', f'/bin/bash {self.evidence}/x.sh']))
        result = self.h.run_coordinator('--test-command', 'pytest > ../run/evidence/out')
        self.assertEqual(result.returncode, 2)
        self.assertIn("REFUSED: configured command 'pytest > ../run/evidence/out' names a protected path", result.stdout)
        self.assertFalse((self.h.run_dir / 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
