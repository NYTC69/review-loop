"""v3.6.0 (owner decision): Codex reviewer-type roles (reviewer, shadow, gate, stage reviewers and their probes) run with the Codex
author's sandbox shape (workspace-write, $TMPDIR writable, /tmp excluded, network off), and the optional profile key
extra_writable_roots adds writable roots for the Codex author and those roles alike, and for every Claude role's Bash sandbox. The post-turn guard still undoes a tracked
change and re-dispatches once; ignored paths stay free for them.

Fake CLIs only: these tests prove the coordinator's argv, flags, config and guard logic, not that `codex exec` honours the sandbox.
Only a real permission-probe shows that."""
import json
import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from paired_session import test_operator_roles as tor
from paired_session import test_real_coordinator as trc
from paired_session import timeout_scale as tsc

rc = trc.rc
ALL_CODEX = ('--author-vendor', 'codex', '--reviewer-vendor', 'codex', '--gate-vendor', 'codex')
REVIEWER_ROLES = ('reviewer', 'shadow', 'gate', 'probe', 'gate-probe')


class Harness(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def argv(self, action='run', *extra):
        return [action, '--workspace', str(self.h.workspace), '--workitem', str(self.h.workitem), '--run-dir', str(self.h.run_dir),
                '--codex-bin', str(self.h.fake_codex_cli()), '--claude-bin', str(self.h.fake_claude_cli()), *extra]

    def profiled(self, roots, action='run', *extra):
        """A coordinator built as main() builds it, with an operator profile outside the workspace naming `roots`."""
        profile = self.h.root / f'profile-{len(list(self.h.root.glob("profile-*")))}.json'
        profile.write_text(json.dumps({'extra_writable_roots': roots}))
        argv = self.argv(action, '--config', str(profile), *extra)
        return rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))

    def schema(self):
        path = self.h.root / 'schema.json'
        rc.atomic_json(path, rc.review_schema())
        return path


class ReviewerSandboxTests(Harness):
    def test_every_codex_reviewer_type_command_carries_the_workspace_write_shape(self):
        co = self.h.coordinator(*ALL_CODEX)
        schema = self.schema()
        for role in REVIEWER_ROLES:
            with self.subTest(role=role):
                trc.assert_codex_reviewer_sandbox(self, co.command(role, schema, True))
        author = co.command('author', schema, True)
        self.assertIn('sandbox_workspace_write.writable_roots=' + json.dumps([str(co.author_temp_dir)]), author)   # unchanged without extra roots

    def test_windows_keeps_the_elevated_sandbox_with_the_new_shape(self):
        co = rc.Coordinator.__new__(rc.Coordinator)
        co.args = SimpleNamespace(codex_bin='codex')
        co.state = {'started': {}, 'sessions': {}}
        co._model_effort = mock.Mock(return_value=('model', 'high'))
        for role in ('reviewer', 'shadow', 'gate'):
            with self.subTest(role=role), mock.patch.object(rc.winplat, 'IS_WINDOWS', True):
                command = co._codex_command(role, Path('schema.json'), True)
                self.assertEqual(command.count('windows.sandbox="elevated"'), 1)
                trc.assert_codex_reviewer_sandbox(self, command)

    def test_the_flags_name_the_new_sandbox_so_an_older_probe_pass_stops_matching(self):
        co = self.h.coordinator(*ALL_CODEX)
        flags = co.reviewer_flags()
        self.assertEqual(flags['sandbox'], 'workspace-write')
        self.assertEqual(flags['codex_reviewer_sandbox'], rc.codex_reviewer_sandbox_overrides())
        self.assertNotIn('codex_readonly_profile', flags)
        self.assertEqual(co.gate_flags()['codex_reviewer_sandbox'], rc.codex_reviewer_sandbox_overrides())
        old = {**{k: v for k, v in flags.items() if k != 'codex_reviewer_sandbox'}, 'sandbox': 'read-only', 'codex_readonly_profile': [
            '--config', 'default_permissions="paired_session_readonly"']}
        digest = lambda value: rc.hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertNotEqual(co.reviewer_flags_digest(), digest(old))
        self.h.run_dir = self.h.root / 'claude-roles'
        claude = self.h.coordinator('--reviewer-vendor', 'claude', '--gate-vendor', 'claude').reviewer_flags()
        self.assertEqual(claude['sandbox'], 'restricted-allowlist')
        self.assertNotIn('codex_reviewer_sandbox', claude)


class ExtraWritableRootsTests(Harness):
    def setUp(self):
        super().setUp()
        self.cargo = self.h.root / 'cargo-home'
        self.cargo.mkdir()

    def test_the_roots_reach_the_author_and_every_reviewer_type_command_and_are_recorded(self):
        co = self.profiled([str(self.cargo)], 'run', *ALL_CODEX)
        root = str(self.cargo.resolve())
        self.assertEqual(co.state['config']['extra_writable_roots'], [root])
        schema = self.schema()
        for role in REVIEWER_ROLES:
            with self.subTest(role=role):
                trc.assert_codex_reviewer_sandbox(self, co.command(role, schema, True), extra=[root])
        self.assertEqual(co._author_sandbox_overrides()['sandbox_workspace_write.writable_roots'], [str(co.author_temp_dir), root])
        self.assertIn('sandbox_workspace_write.writable_roots=' + json.dumps([str(co.author_temp_dir), root]), co.command('author', schema, True))
        self.assertEqual(co.reviewer_flags()['codex_reviewer_sandbox']['sandbox_workspace_write.writable_roots'], [root])

    def test_the_roots_reach_the_claude_bash_sandbox_of_every_role_and_are_absent_by_default(self):
        root = str(self.cargo.resolve())
        co = self.profiled([str(self.cargo)])
        for role in ('author', *REVIEWER_ROLES):
            with self.subTest(role=role):
                self.assertEqual(co._claude_sandbox_settings(role)['sandbox']['filesystem']['allowWrite'], [root])
        self.h.run_dir = self.h.root / 'run-plain'
        plain = self.profiled([])
        self.assertNotIn('allowWrite', plain._claude_sandbox_settings('author')['sandbox']['filesystem'])   # default settings unchanged

    def test_a_bad_entry_is_refused_at_start_with_a_clear_message(self):
        a_file = self.h.root / 'a-file'
        a_file.write_text('x')
        for value, message in (('cargo-home', 'must be an absolute path'), (str(self.h.root / 'missing'), 'does not exist'),
                               (str(a_file), 'is not a directory')):
            with self.subTest(value=value):
                self.h.run_dir = self.h.root / ('run-' + Path(value).name)
                with self.assertRaisesRegex(ValueError, 'extra_writable_roots entry ' + message):
                    self.profiled([value])
                self.assertFalse((self.h.run_dir / 'state.json').exists())
        inside = self.h.root / 'run-in' / 'cache'
        inside.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'extra_writable_roots entry is inside the run directory'):
            rc.validate_extra_writable_roots([str(inside)], self.h.root / 'run-in')
        with self.assertRaisesRegex(ValueError, 'extra_writable_roots entry is inside the run directory'):
            rc.validate_extra_writable_roots([str(inside.parent)], self.h.root / 'run-in')
        with self.assertRaisesRegex(ValueError, 'extra_writable_roots entry contains the run directory'):
            rc.validate_extra_writable_roots([str(self.h.root)], self.h.root / 'run-in')
        for bad in ('not-a-list', [1]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'config extra_writable_roots must be an array of strings'):
                self.profiled(bad)

    def test_resume_keeps_the_recorded_list(self):
        root = str(self.cargo.resolve())
        self.profiled([str(self.cargo)], 'run', *ALL_CODEX)
        other = self.h.root / 'other-cache'
        other.mkdir()
        with patch('builtins.print') as printed:
            resumed = self.profiled([str(other)], 'resume', *ALL_CODEX)
        self.assertEqual(resumed._extra_writable_roots(), [root])
        self.assertTrue(any('extra_writable_roots is fixed at run start' in str(call) for call in printed.call_args_list))
        trc.assert_codex_reviewer_sandbox(self, resumed.command('reviewer', self.schema(), True), extra=[root])
        state = json.loads((self.h.run_dir / 'state.json').read_text())   # a run started before the key existed resumes with none
        del state['config']['extra_writable_roots']
        (self.h.run_dir / 'state.json').write_text(json.dumps(state))
        argv = self.argv('resume', *ALL_CODEX)
        old = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))
        self.assertEqual(old._extra_writable_roots(), [])


class GuardTests(Harness):
    def review(self, env, *flags):
        co = self.h.coordinator('--timeout', tsc.scaled_arg(10), '--author-effort', 'low', '--reviewer-effort', 'low', '--gate-effort', 'low',
                                '--test-command', 'python3 -m unittest', *flags)
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        with patch.dict(os.environ, env):
            return co, co.invoke('reviewer', 'PLAN', co._review_prompt('reviewer', 'snapshot'), rc.review_schema())

    def test_a_codex_reviewer_that_edits_a_tracked_file_is_undone_and_re_dispatched_once(self):
        tracked = self.h.workspace / 'tracked.txt'
        before = tracked.read_text()
        with patch('builtins.print') as printed:                                             # the first turn edits, the second is clean
            co, result = self.review({'FAKE_MUTATION': 'checkout', 'FAKE_MUTATION_ONCE': str(self.h.root / 'marker')}, '--reviewer-vendor', 'codex')
        self.assertTrue(any('re-dispatching the reviewer turn once' in str(call) for call in printed.call_args_list))
        turns = [t for t in co.state['turns'] if t.get('role') == 'reviewer']
        self.assertEqual(len(turns), 2)
        self.assertTrue(turns[0]['voided']['restored'])
        self.assertNotIn('voided', turns[1])
        trc.assert_codex_reviewer_sandbox(self, turns[1]['command'])
        self.assertEqual(tracked.read_text(), before)                                        # undone
        self.h.run_dir = self.h.root / 'tracked-edit'                                        # a second edit after the re-dispatch holds
        with self.assertRaisesRegex(RuntimeError, 'reviewer mutated workspace again after one re-dispatch'):
            self.review({'FAKE_PLAN_REVIEWER_MUTATE': '1'}, '--reviewer-vendor', 'codex')
        self.assertEqual(tracked.read_text(), before)                                        # undone

    def test_a_codex_reviewer_write_to_an_ignored_path_is_kept(self):
        with (self.h.workspace / '.git' / 'info' / 'exclude').open('a') as handle:
            handle.write('*.log\n.env\n')
        env_file = self.h.workspace / '.env'
        env_file.write_text('SECRET=1\n')
        for name, kept in (('build.log', None), ('.env', ['.env'])):   # a new ignored file (not a recorded entry) and a changed one
            with self.subTest(name=name):
                self.h.run_dir = self.h.root / ('ignored-' + name.strip('.'))
                co, result = self.review({'FAKE_PLAN_REVIEWER_TOUCH_IGNORED': name}, '--reviewer-vendor', 'codex')
                [turn] = [t for t in co.state['turns'] if t.get('role') == 'reviewer']
                self.assertEqual(turn.get('readonly_ignored_kept'), kept)
                self.assertNotIn('voided', turn)
                self.assertIn('answer', turn)
                self.assertIn('reviewer edit', (self.h.workspace / name).read_text())
        self.assertEqual(env_file.read_text(), 'SECRET=1\nreviewer edit\n')
        self.h.run_dir = self.h.root / 'ignored-claude'                                       # Claude roles are unchanged: still void
        with self.assertRaisesRegex(RuntimeError, 'reviewer changed ignored files in the workspace'):
            self.review({'FAKE_PLAN_REVIEWER_TOUCH_IGNORED': '.env'}, '--reviewer-vendor', 'claude')
        subprocess.run(['git', 'status', '--short'], cwd=self.h.workspace, check=True, capture_output=True)


class ProbeTests(unittest.TestCase):
    locals().update({name: getattr(tor.ProbeSkipTests, name) for name in ('setUp', 'co', 'probe')})

    def test_the_probe_proves_the_workspace_write_and_the_refused_outside_writes(self):
        report = self.probe(self.co('--reviewer-vendor', 'codex', '--gate-vendor', 'codex'), author={'status': 'PASS'})   # the reviewer half only
        self.assertEqual((report['status'], report['snapshot_unchanged']), ('PASS', True), report['failure_reasons'])
        commands = report['write_attempts_denied']
        for marker in ('/tmp/paired-session-codex-escape-', '/context/paired-session-codex-escape-', 'ln '):
            self.assertTrue(any(marker in command and denied for command, denied in commands.items()), marker)
        self.assertFalse([command for command in commands if 'forbidden-probe' in command or command.startswith('rm ')])   # no workspace legs
        self.assertFalse(list(self.h.workspace.glob('.paired-session-reviewer-write-probe-*')))

    def test_an_in_repo_run_dir_is_not_an_outside_target(self):
        self.h.run_dir = self.h.workspace / '.review-loop' / 'runs' / 'probe'
        co = self.co('--reviewer-vendor', 'codex', '--gate-vendor', 'codex')
        targets = co._codex_escape_targets()
        self.assertIn('slash_tmp', targets)
        self.assertFalse({'run_dir', 'context', 'hardlink'} & set(targets), targets)


if __name__ == '__main__':
    unittest.main()
