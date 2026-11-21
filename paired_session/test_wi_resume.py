"""WI-RESUME tests with the coordinator's fake provider CLIs."""
import contextlib
import io
import json
import shutil
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc
from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session.test_v317g1 import digest
from paired_session.test_worktree_lifecycle import COVERING_GITIGNORE

rc = trc.rc
REBOUND = 'NOTE: review-loop install moved'
CHANGED = 'NOTE: resumed with role changes'
DRIFT = 'frozen role dispatch changed'


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        use_lifecycle_on(self, self.h)
        (self.h.workspace / '.gitignore').write_text(COVERING_GITIGNORE)
        rc.subprocess.run(['git', 'commit', '-qam', 'ignore sensitive files'],
                          cwd=self.h.workspace, check=True)
        self.flags = ('--timeout', trc.tsc.scaled_arg(10), '--test-command', 'python3 -m unittest')

    def state(self):
        return json.loads((self.h.run_dir / 'state.json').read_text())

    def argv(self, *extra):
        command = self.h.command(*extra)
        index = command.index('--author-effort')
        del command[index:index + 6]   # Exercise omitted efforts with the stock meaning of explicit flags.
        return command[2:]

    def cli(self, action, *extra, drive=None):
        argv = self.argv(*extra)
        argv[0] = action
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            if drive is None:
                code = rc.main(argv)
            else:
                with patch.object(rc.Coordinator, 'drive', autospec=True, side_effect=drive):
                    code = rc.main(argv)
        return code, out.getvalue()

    def dispatch(self, co):
        return co._invoke_once('author', co.state['phase'], co._author_prompt(), rc.author_schema())

    def assert_bound(self, state):
        manifest = state['role_dispatch_manifest']
        self.assertEqual(manifest['role_flags_sha256'], digest(manifest['role_flags']))
        self.assertEqual(manifest['config_sha256'], digest(state['config']))
        self.assertEqual(state['role_dispatch_manifest_sha256'], digest(manifest))

    @contextlib.contextmanager
    def installs(self):
        old = self.h.root / 'original-install'
        shutil.copytree(rc.HERE.parent / 'agents', old / 'agents')
        prompt = old / 'gate.txt'
        shutil.copyfile(rc.DEFAULT_GATE_PROMPT, prompt)
        freeze = rc.frozen_role_manifest

        def snapshot(config, flags, agents_dir, gate_prompt, bodies):
            return freeze(config, flags, rc.DEFAULT_GATE_PROMPT.parent / 'agents', gate_prompt, bodies)

        with patch.object(rc, 'frozen_role_manifest', side_effect=snapshot):
            yield old, prompt

    def test_install_move(self):
        with self.installs() as (old, prompt):
            with patch.object(rc, 'DEFAULT_GATE_PROMPT', prompt):
                co = self.h.coordinator(*self.flags, '--gate-prompt', str(prompt))
                self.dispatch(co)
            sessions = dict(self.state()['sessions'])
            saved = co.state_path.read_bytes()
            new = self.h.root / 'moved-install'
            old.rename(new)
            new_prompt = new / prompt.name
            flags = ('--gate-prompt', str(new_prompt))
            with patch.object(rc, 'DEFAULT_GATE_PROMPT', new_prompt):
                def verified_turn(resumed):
                    self.dispatch(resumed)
                    return 'HOLD'

                code, output = self.cli('resume', *flags, drive=verified_turn)
                self.assertEqual(code, 2, output)
                self.assertEqual(output.count(REBOUND), 1, output)
                self.assertNotIn('operator program or PATH changed', output)
                state = self.state()
                [move] = state['role_manifest_rebinds']
                self.assertEqual(move['old']['agents_dir'], str(old / 'agents'))
                self.assertEqual(move['new']['gate_prompt_path'], str(new_prompt))
                self.assertFalse(old.exists())
                self.assertEqual(state['sessions'], sessions)
                self.assert_bound(state)
                _, output = self.cli('resume', *flags, drive=verified_turn)
                self.assertNotIn(REBOUND, output)
                self.assertEqual(self.state()['role_manifest_rebinds'], [move])

                body = new / 'agents' / 'executor.md'
                for drift in ('agent', 'digest', 'role flags', 'bundled prompt'):
                    with self.subTest(drift=drift):
                        co.state_path.write_bytes(saved)
                        if drift == 'bundled prompt':
                            with new_prompt.open('ab') as stream:
                                stream.write(b'\nNew release gate prompt.\n')
                            _, output = self.cli('resume', *flags, drive=verified_turn)
                            self.assertIn('the bundled gate prompt differs', output)
                            self.assertEqual(output.count(REBOUND), 1)
                            self.assert_bound(self.state())
                            continue
                        state = self.state()
                        manifest = state['role_dispatch_manifest']
                        original = body.read_bytes()
                        if drift == 'agent':
                            body.write_bytes(original + b'\nChanged instructions.\n')
                        elif drift == 'digest':
                            state['role_dispatch_manifest_sha256'] = 'wrong'
                        else:
                            manifest['role_flags']['author']['subagents'] = 'different'
                            manifest['role_flags_sha256'] = digest(manifest['role_flags'])
                            state['role_dispatch_manifest_sha256'] = digest(manifest)
                        rc.atomic_json(co.state_path, state)
                        try:
                            _, output = self.cli('resume', *flags, drive=verified_turn)
                            self.assertIn(DRIFT, output)
                            self.assertNotIn('role_manifest_rebinds', self.state())
                        finally:
                            body.write_bytes(original)

                # A custom prompt keeps its original path and must still match its frozen bytes.
                self.h.run_dir = self.h.root / 'custom-run'
                custom = self.h.root / 'custom.txt'
                custom.write_text('Custom gate instructions.\n')
                self.h.coordinator(*self.flags, '--gate-prompt', str(custom))
                custom.write_text('Modified custom gate instructions.\n')
                _, output = self.cli('resume', '--gate-prompt', str(custom), drive=verified_turn)
                self.assertIn(DRIFT, output)
                self.assertNotIn('role_manifest_rebinds', self.state())

    def test_resume_role_policy(self):
        with self.installs() as (old, prompt):
            profile = self.h.root / 'operator.json'
            policy = {'codex': ['gpt-6.1-sol', 'gpt-6-sol'], 'claude': ['claude-opus-5-5', 'claude-sonnet-5-5']}
            profile.write_text(json.dumps({'allowed_models': policy, 'author_effort': 'low',
                                           'author_model': 'gpt-6-sol'}))
            config_flags = ('--config', str(profile))
            with patch.object(rc, 'DEFAULT_GATE_PROMPT', prompt):
                argv = self.argv('--gate-prompt', str(prompt), *config_flags,
                                 '--author-effort', 'high', '--author-model', 'gpt-6.1-sol')
                co = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))
                co.state['status'] = 'DONE'
                co.save()
                blocked = ('--gate-prompt', str(prompt), '--gate-model=gpt-6-sol', *config_flags)
                self.assertEqual(self.cli('resume', *blocked)[0], 0)
                self.assertNotIn('role_resume_changes', self.state())

                def restore_only(args):
                    restored = rc.Coordinator(args)
                    for key in rc.ROLE_EFFORT_DESTS:
                        self.assertEqual(getattr(restored.args, key), co.state['config'][key])
                    return 0

                for action, *options in (('run',), ('accept',), ('reject',), ('note',),
                                         ('note', '--scope-change'), ('withdraw-findings',),
                                         ('attach-verification',), ('abort',)):
                    with self.subTest(action=action, options=options):
                        flags = ('--gate-prompt', str(prompt), *config_flags, *options)
                        with patch.object(rc, '_execute_locked', side_effect=restore_only) as execute:
                            code, output = self.cli(action, *flags, '--author-effort=low',
                                                    '--reviewer-effort=low', '--gate-effort=low')
                        self.assertEqual(code, 0, output)
                        execute.assert_called_once()
                        for flag in ('--author-model=gpt-6-sol', '--author-vendor=claude'):
                            code, output = self.cli(action, *flags, flag)
                            self.assertEqual(code, 2, output)
                            self.assertIn('role models are fixed', output)
                code, output = self.cli('status', '--gate-prompt', str(prompt), *config_flags,
                                        '--author-effort=low', '--author-model=gpt-6-sol')
                self.assertEqual(code, 0, output)
                self.assertEqual(json.loads(output), self.state())
                for flag, reason in (('--author-vendor=claude', 'role models are fixed'),
                                     ('--author-model=bad model', 'well-formed model id'),
                                     ('--author-model=gpt-6-luna', 'not in allowed_models'),
                                     ('--author-model=claude-opus-5-5', 'not in allowed_models')):
                    code, output = self.cli('resume', *blocked, flag)
                    self.assertEqual(code, 2, output)
                    self.assertIn(reason, output)

                co.state.update(status='HOLD', hold_reason='interrupted', uncertain_active={
                    'sequence': 1, 'role': 'reviewer', 'phase': 'PLAN', 'vendor': 'claude',
                    'pid': 12345, 'job': 'test-job', 'fresh': True})
                co.state['turns'].append({'role': 'gate', 'phase': 'EXEC', 'sequence': 0,
                                          'answer': {'verdict': 'PASS', 'findings': []}})
                co.save()
                self.cli('resume', *blocked, drive=lambda _: self.fail('uncertain run dispatched'))
                self.assertNotIn('role_resume_changes', self.state())
                with patch.object(rc, 'probe_group', return_value=None):
                    _, output = self.cli('resume', *blocked, '--retry-uncertain')
                    self.assertIn('process group is still alive', output)
                self.assertNotIn('role_resume_changes', self.state())

            new = old.with_name('moved-install')
            old.rename(new)
            new_prompt = new / prompt.name
            flags = ('--gate-prompt', str(new_prompt), *config_flags, '--reviewer-model', 'claude-sonnet-5-5',
                     '--reviewer-effort=high', '--gate-model=gpt-6-sol', '--gate-effort', 'high',
                     '--skip-quality-polish', 'true')

            def verify(resumed):
                self.assertTrue(resumed.state['gate_ran'])
                self.assertEqual(len(resumed._gate_turns()), 1)
                with patch.object(resumed, 'start_polish_or_done') as advance:
                    resumed.gate_turn()
                advance.assert_called_once_with()
                self.dispatch(resumed)
                return 'HOLD'

            with patch.object(rc, 'DEFAULT_GATE_PROMPT', new_prompt):
                with patch.object(rc, 'probe_group', side_effect=ProcessLookupError):
                    _, output = self.cli('resume', *flags, '--retry-uncertain', drive=verify)
                for note in (REBOUND, CHANGED, 'NOTE: skip_quality_polish is now true'):
                    self.assertEqual(output.count(note), 1, output)
                state = self.state()
                self.assertEqual(state['config']['author_model'], 'gpt-6.1-sol')
                self.assertEqual(state['config']['author_effort'], 'high')
                self.assertEqual(state['config']['allowed_models'], policy)
                self.assertTrue(state['abandoned_turns'][0]['group_gone'])
                self.assertEqual({row['key'] for row in state['role_resume_changes']},
                                 {'reviewer_model', 'reviewer_effort', 'gate_model', 'gate_effort'})
                self.assertNotEqual(state['sessions']['reviewer'], co.state['sessions']['reviewer'])
                self.assert_bound(state)
                _, output = self.cli('resume', *flags, drive=verify)
                self.assertNotIn('NOTE:', output)
                for key in ('role_resume_changes', 'role_manifest_rebinds', 'quality_polish_downgrades'):
                    self.assertEqual(self.state()[key], state[key])
                self.assert_bound(self.state())

    def test_replacement_dispatch(self):
        with patch.dict(rc.os.environ, {'FAKE_UNIQUE_CODEX_THREAD': '1'}):
            co = self.h.coordinator(*self.flags)
            self.dispatch(co)
            initial = self.state()['sessions']['author']
            plan = co.context / 'plan.md'
            plan.write_text('Approved test plan.\n')
            co.state.update(phase='EXEC', next='author', plan_rounds=1)
            baseline = co._author_prompt()
            co.save()

            def attempt(resumed):
                prompt = resumed._author_prompt()
                line = f'Approved plan: {plan.resolve()}\n'
                self.assertIn(line, prompt)
                self.assertEqual(prompt.replace(line, ''), baseline)
                return dispatch_replacement(resumed)

            def dispatch_replacement(resumed):
                self.dispatch(resumed)
                self.assertEqual(resumed._author_prompt(), baseline)
                return 'HOLD'

            _, output = self.cli('resume', '--author-model=gpt-6-sol', drive=attempt)
            self.assertIn('model', output)
            self.assertIn('mismatch', output.lower())
            self.assertTrue(self.state()['replacement_author_session'])
            self.assertFalse(self.state()['started']['author'])
            self.assertIsNone(self.state()['sessions']['author'])
            with patch.dict(rc.os.environ, {'FAKE_CODEX_MODEL': 'gpt-6-sol'}):
                _, output = self.cli('resume', drive=attempt)
                self.assertNotIn('HOLD: frozen role dispatch changed', output)
                state = self.state()
                self.assertNotIn('replacement_author_session', state)
                new = state['sessions']['author']
                self.assertTrue(new)
                self.assertNotEqual(new, initial)
                _, output = self.cli('resume', '--author-effort', 'high', drive=dispatch_replacement)
                self.assertIn(CHANGED, output)
                self.assertEqual(self.state()['sessions']['author'], new)
                self.assert_bound(self.state())

                collect = rc.Coordinator._collect

                def wrong_session(resumed, *args):
                    answer, _, *evidence = collect(resumed, *args)
                    return answer, initial, *evidence

                with patch.object(rc.Coordinator, '_collect', wrong_session):
                    _, output = self.cli('resume', drive=dispatch_replacement)
                self.assertIn('resumed a different session', output)
                self.assertEqual(self.state()['sessions']['author'], new)

            co.state = self.state()
            co.state['phase'] = 'PLAN'
            co.state['replacement_author_session'] = True
            self.assertIn(f'Current draft: {plan.resolve()}', co._author_prompt())
