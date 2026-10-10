"""v297-eg-wire: the hybrid wiring of the typed-operation evidence guard (owner 2026-10-04). A call the guard resolves is decided by
it (PROTECTED holds, ALLOW passes where the old substring match held by mistake); a call it cannot resolve falls back to the
narrowed fallback guard (v3.0.4: literal run-directory spellings, bare or parent-hop evidence/ paths, role isolation; a nested
workspace path such as tests/evidence/ no longer holds), and the receipt counts those fallbacks. The post-dispatch call site gets the call's own
context (Popen cwd, child env without secret-named variables, writable roots, configured commands); Codex keeps a command's workdir;
admission refuses only a configured command that names a protected path."""
import json
import shlex
import sys
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
                legacy = rc._legacy_sensitive_access([{'tool': 'Bash', 'input': {'command': command}}], 'reviewer', self.evidence, self.rounds)
                if 'tests/evidence/' in command:
                    self.assertIsNone(legacy)
                else:
                    self.assertIsNotNone(legacy)
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
        result = self.h.run_coordinator('--shadow', 'off', '--gate-vendor', 'codex', '--gate-model', 'gpt-6.1-sol', '--test-command', 'ls evidence/')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = json.loads((self.h.run_dir / 'state.json').read_text())
        self.assertIn('gate accessed isolated evidence directory', state['hold_reason'])
        self.assertEqual([t['evidence_guard']['fallbacks'] for t in state['turns'] if t.get('role') == 'reviewer' and 'evidence_guard' in t][-1:], [0])

    T0 = '2026-09-21T00:00:01Z'

    def codex_turn(self, items, commands, *, context=True, started=('T1',), session='S1', copies=1, writable=()):
        """A Codex turn as _invoke_once sees it: stdout command events and the rollout (CODEX_HOME/sessions) with Codex's own records."""
        sessions = self.h.root / 'codex-home' / 'sessions'
        sessions.mkdir(parents=True, exist_ok=True)
        for old in sorted(sessions.glob('*.jsonl')): old.unlink()
        rows = [{'timestamp': self.T0, 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': t}} for t in started]
        rows += [{'timestamp': self.T0, 'type': 'turn_context', 'payload': {'cwd': cwd}}
                 for cwd in (context if isinstance(context, list) else [str(self.h.workspace)] if context else [])]
        rows += [{'timestamp': item.get('at', self.T0), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': item.get('turn', 'T1'),
                  'item': {'type': 'CommandExecution', 'command': ['/bin/zsh', '-lc', item['command']], 'cwd': item.get('cwd', self.h.workspace.as_uri())}}}
                 for item in items]
        for copy in range(copies):
            (sessions / f'rollout-2026-09-2{copy + 1}-{session}.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        _, calls = rc.observed_events('codex', [{'type': 'item.completed', 'item': {'type': 'command_execution', 'command': f'/bin/zsh -lc {json.dumps(c)}',
                                                                                    'exit_code': 0}} for c in commands])
        start = rc.datetime.fromisoformat('2026-09-21T00:00:00+00:00').timestamp()
        with mock.patch.dict('os.environ', {'CODEX_HOME': str(sessions.parent)}):
            return calls, rc.codex_guard_calls(calls, rc.codex_rollout_cwds('S1', start, start + 2, writable))

    def test_a_rollout_proven_cwd_resolves_codex_commands(self):   # v297-eg-cwd
        write, same_name = 'printf x > ../run/evidence/a', 'cat tests/evidence/a.txt'
        calls, (guarded, cwd, proven) = self.codex_turn([{'command': write}, {'command': same_name}], [write, same_name])
        self.assertEqual((cwd, proven), (self.h.workspace, 2))
        self.assertEqual([c['input'] for c in calls], [{'command': write}, {'command': same_name}])   # the receipt keeps them as observed
        for call, desired in ((guarded[0], 'evidence directory'), (guarded[1], None)):
            fallbacks = []
            self.assertEqual(rc.sensitive_access([call], 'author', self.evidence, self.rounds, cwd, {}, (self.h.workspace,), (), fallbacks), desired)
            self.assertEqual(fallbacks, [])   # decided by the guard on the proven cwd
        self.assertIsNone(rc._legacy_sensitive_access(calls[1:], 'author', self.evidence, self.rounds))   # workspace false HOLD fixed

    def test_an_unproven_codex_cwd_behaves_exactly_as_today(self):   # no proven cwd: the narrowed fallback guard decides
        same_name, other = 'cat evidence/a.txt', str(self.h.root.as_uri())
        for label, items, commands, kw in (
                ('another turn', [{'command': same_name, 'turn': 'T2'}], [same_name], {}),
                ('outside the window', [{'command': same_name, 'at': '2026-09-21T00:00:09Z'}], [same_name], {}),
                ('two cwds', [{'command': same_name}, {'command': same_name, 'cwd': other}], [same_name], {}),
                ('fewer records than events', [{'command': same_name}], [same_name, same_name], {}),
                ('not a file URL', [{'command': same_name, 'cwd': 'https://example.invalid' + str(self.h.workspace)}], [same_name], {}),
                ('a relative cwd', [{'command': same_name, 'cwd': 'workspace'}], [same_name], {}),
                ('no record', [], [same_name], {}), ('no session', [{'command': same_name}], [same_name], {'session': 'S2'}),
                ('two rollouts', [{'command': same_name}], [same_name], {'copies': 2}),
                ('no task_started', [{'command': same_name}], [same_name], {'started': ()}),
                ('two turns in the window', [{'command': same_name}], [same_name], {'started': ('T1', 'T0')}),
                ('a role can write the sessions dir', [{'command': same_name}], [same_name], {'writable': (None, self.h.workspace, self.h.root)})):
            with self.subTest(label):
                calls, (guarded, cwd, proven) = self.codex_turn(items, commands, **kw)
                self.assertEqual((cwd, proven, guarded), (None, 0, calls))
                fallbacks = []
                self.assertEqual(rc.sensitive_access(guarded, 'author', self.evidence, self.rounds, cwd, {}, (self.h.workspace,), (), fallbacks),
                                 rc._legacy_sensitive_access(calls, 'author', self.evidence, self.rounds))
                self.assertEqual(len(fallbacks), 1)   # the first unresolved call already holds in the fallback guard

    def test_code_mode_cells_get_the_turn_cwd_only_when_every_command_is_proven(self):
        cell = {'tool': 'code_mode', 'input': {'code': 'const r = await tools.exec_command({"cmd": "cat tests/evidence/a.txt"});\ntext(r);'}}
        for items, commands, desired_cwd in (([{'command': 'ls'}], ['ls'], self.h.workspace), ([{'command': 'ls'}], ['ls', 'pwd'], None),
                                              ([], [], self.h.workspace)):
            with self.subTest(commands=commands):
                calls, (guarded, cwd, _) = self.codex_turn(items, commands)
                self.assertEqual(cwd, desired_cwd)
                fallbacks = []
                rc.sensitive_access([*guarded, cell], 'author', self.evidence, self.rounds, cwd, {}, (self.h.workspace,), (), fallbacks)
                self.assertEqual(len(fallbacks), 0 if desired_cwd else len(commands) - len(items) + 1)
        _, (_, cwd, _) = self.codex_turn([{'command': 'ls'}], ['ls'], context=False)
        self.assertIsNone(cwd)   # no turn_context: the cells' cwd is not proven
        _, (_, cwd, _) = self.codex_turn([{'command': 'ls'}], ['ls'], context=[str(self.h.workspace), str(self.h.root)])
        self.assertIsNone(cwd)   # two turn cwds: neither is proven
        _, (_, cwd, _) = self.codex_turn([{'command': 'ls'}], ['ls'], writable=(self.h.workspace,))
        self.assertEqual(cwd, self.h.workspace)   # the sessions dir is outside every writable root
        proof = {'cwd': str(self.h.workspace), 'commands': []}
        for changes, desired in (([{'path': str(self.h.workspace / 'a.py')}], self.h.workspace), ([{'path': 'a.py'}], None),
                                 ({'a.py': {}}, None), ([None], None), ([{'path': 7}], None)):
            with self.subTest(changes=changes):   # a relative patch path may have been applied in another workdir
                self.assertEqual(rc.codex_guard_calls([{'tool': 'file_change', 'input': {'changes': changes}}], proof)[1], desired)

    def test_a_codex_gate_with_a_proven_cwd_passes_a_same_name_path(self):
        (self.h.workspace / 'tests' / 'evidence').mkdir(parents=True)
        result = self.h.run_coordinator('--shadow', 'off', '--gate-vendor', 'codex', '--gate-model', 'gpt-6.1-sol', '--test-command', 'ls tests/evidence/',
                                        env={'FAKE_CODEX_ROLLOUT_CWD': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        gate = [t['evidence_guard'] for t in json.loads((self.h.run_dir / 'state.json').read_text())['turns'] if t.get('role') == 'gate']
        self.assertEqual(gate[-1:], [{'fallbacks': 0, 'fallback_reasons': [], 'codex_cwd_proven': 1}])

    def test_a_rollout_a_role_can_write_proves_nothing(self):   # CODEX_HOME inside the workspace (codex.cache is git-ignored)
        (self.h.workspace / 'tests' / 'evidence').mkdir(parents=True)
        inside = self.h.workspace / 'codex.cache'
        inside.mkdir()
        (inside / 'config.toml').write_text((self.h.test_home / '.codex' / 'config.toml').read_text())
        result = self.h.run_coordinator('--shadow', 'off', '--gate-vendor', 'codex', '--gate-model', 'gpt-6.1-sol', '--test-command', 'ls evidence/',
                                        env={'FAKE_CODEX_ROLLOUT_CWD': '1', 'CODEX_HOME': str(inside)})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(sorted((inside / 'sessions').glob('rollout-*.jsonl')))   # the record exists, and is not trusted
        self.assertIn('gate accessed isolated evidence directory', json.loads((self.h.run_dir / 'state.json').read_text())['hold_reason'])

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
        self.assertEqual(len(fallbacks), 1)   # no workdir, no known cwd: the narrowed fallback guard decides (a parent hop holds)

    def test_admission_refuses_only_a_configured_command_that_names_a_protected_path(self):
        self.assertIsNone(rc.configured_command_issue(self.h.workspace, self.h.run_dir, [
            'python3 -m unittest', 'python3 -c pass', "/bin/bash -c 'python3 -m unittest && true'", 'cat "$(x)"', 'for f in a; do :; done']))
        self.assertIn('names a protected path (evidence directory)',
                      rc.configured_command_issue(self.h.workspace, self.h.run_dir, ['npm test', f'/bin/bash {self.evidence}/x.sh']))
        command = shlex.quote(sys.executable) + ' -m unittest > ../run/evidence/out'   # an executable every runner has (no PATH lookup)
        result = self.h.run_coordinator('--test-command', command)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f'REFUSED: configured command {command!r} names a protected path', result.stdout)
        self.assertFalse((self.h.run_dir / 'state.json').exists())

    def test_admission_of_a_restored_run_checks_its_saved_commands(self):   # eg-wire R1 LOW: the CLI args keep their defaults
        self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--stop-after-plan')
        state_path = self.h.run_dir / 'state.json'
        self.assertTrue(state_path.is_file())
        state = json.loads(state_path.read_text())
        for key, value in (('test_command', 'pytest > ../run/evidence/out'), ('reviewer_command', ['true', 'cat ../run/rounds/x.md']),
                           ('workitem_reviewer_commands', ['ls ../run/evidence'])):
            with self.subTest(key):
                state_path.write_text(json.dumps({**state, 'config': {**state['config'], key: value}}))
                for action in ('run', 'resume'):
                    command = self.h.command('--shadow', 'off', '--adversarial-gate', 'off'); command[2] = action
                    result = rc.subprocess.run([*command, '--skip-probe'], cwd=self.h.root, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn('REFUSED: configured command ', result.stdout)
                    self.assertIn('names a protected path', result.stdout)
                note = self.h.run_operator_action('note', '--text', 'a note dispatches no turn')
                self.assertNotIn('names a protected path', note.stdout + note.stderr)
        self.assertEqual(rc._saved_configured_commands(self.h.root / 'missing'), [])
        state_path.write_text(json.dumps({**state, 'config': {**state['config'], 'reviewer_command': 'not a list', 'test_command': 7}}))
        self.assertEqual(rc._saved_configured_commands(self.h.run_dir), state['config'].get('workitem_reviewer_commands') or [])


if __name__ == '__main__':
    unittest.main()
