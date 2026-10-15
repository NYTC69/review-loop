"""V318-CAP: each fake-CLI dispatch gives commands its own turn budget."""
import copy
import os
import subprocess
import types
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc
from paired_session import test_worktree_lifecycle as twl

rc = trc.rc


class DispatchBudgetTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in twl._HELPERS})

    @classmethod
    def setUpClass(cls):
        # Execute the released source in this clone, with its original file location so
        # bundled assets and imports resolve identically. No other repository is read.
        cls.released = None
        try:
            source = subprocess.check_output(
                ['git', 'show', '59ce540:paired_session/coordinator.py'], cwd=trc.MODULE_PATH.parent,
                text=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError:
            return   # Only the release-dependent tests need to skip in shallow checkouts.
        cls.released = types.ModuleType('v318cap_released_coordinator')
        cls.released.__file__ = str(trc.MODULE_PATH)
        exec(compile(source, str(trc.MODULE_PATH), 'exec'), cls.released.__dict__)

    def released_coordinator(self):
        if self.released is None:
            self.skipTest('59ce540 coordinator source unavailable (e.g. shallow checkout)')
        return self.released

    def prepare(self, *extra, released=False):
        module = self.released_coordinator() if released else rc
        with patch.object(trc, 'rc', module):
            co = self.coordinator('--lifecycle-mode', 'off', *extra)
        co.state['phase'] = 'EXEC'
        (co.context / 'plan.md').write_text('# Plan\nImplement the feature.\n')
        return co

    def dispatch(self, co, role, phase='EXEC', maximum=None):
        co.state['phase'] = phase
        if role == 'author':
            prompt = (co._author_prompt() if phase in ('PLAN', 'EXEC') else
                      f'Role: persistent implementer. Phase: {phase}.\n' + rc.LONG_COMMAND_RULE)
            schema = rc.author_schema()
        else:
            prompt = co._gate_prompt('snapshot') if role == 'gate' else co._review_prompt(role, 'snapshot')
            schema = rc.gate_schema() if role == 'gate' else rc.review_schema()
        seen = []
        popen = rc.subprocess.Popen

        def capture(*args, **kwargs):
            if str(getattr(kwargs.get('stdout'), 'name', '')).endswith('.stdout.jsonl'):
                seen.append(kwargs['env'].copy())
            return popen(*args, **kwargs)

        overrides = {'BASH_MAX_TIMEOUT_MS': maximum or '1'}
        if phase == 'DOCS':
            overrides['FAKE_LIFECYCLE_DOCS_FILE'] = 'docs/guide.md'
        with patch.dict(os.environ, overrides), \
                patch.object(rc.subprocess, 'Popen', side_effect=capture):
            result = co._invoke_once(role, phase, prompt, schema, fresh=role in ('shadow', 'gate'))
        prefix = co.evidence / f"{result['sequence']:03d}-{phase.lower()}-{role}"
        actual = prefix.with_suffix('.prompt.txt').read_text()
        return actual, seen[-1], prompt

    def test_four_hour_review_roles_and_larger_operator_maximum(self):
        co = self.prepare('--timeout', '14400')
        for role in ('reviewer', 'gate', 'shadow'):
            actual, env, original = self.dispatch(co, role)
            self.assertEqual(env['BASH_MAX_TIMEOUT_MS'], '14400000')
            self.assertEqual(actual, original.replace('gets 3600000 for a long command.',
                                                      'gets 14400000 for a long command.'))
            co.assert_fresh_prompt(role, actual)
        actual, env, _ = self.dispatch(co, 'gate', maximum='20000000')
        self.assertEqual(env['BASH_MAX_TIMEOUT_MS'], '20000000')
        self.assertIn('gets 14400000 for a long command.', actual)

    def test_default_review_dispatch_bytes_match_release_and_exec_author_grows(self):
        released = self.released_coordinator()
        co = self.prepare()
        # Same state/paths: compare the complete prompt builder output against 59ce540.
        old = object.__new__(released.Coordinator)
        old.__dict__.update(co.__dict__)
        for role in ('reviewer', 'gate'):
            actual, env, _ = self.dispatch(co, role)
            baseline = old._gate_prompt('snapshot') if role == 'gate' else old._review_prompt(role, 'snapshot')
            self.assertEqual(actual, baseline)
            with patch.dict(os.environ, {'BASH_MAX_TIMEOUT_MS': '1'}):
                baseline_env = released.cli_env()
            if co._role_vendor(role) == 'codex':
                baseline_env['CODEX_HOME'] = str(co.global_codex_home)
            self.assertEqual(env, baseline_env)
        actual, env, _ = self.dispatch(co, 'author')
        self.assertEqual(co.args.exec_turn_timeout, 7200)
        self.assertEqual(env['BASH_MAX_TIMEOUT_MS'], '7200000')
        self.assertIn('gets 7200000 for a long command.', actual)

    def test_author_exec_finish_docs_use_author_timeout_other_phases_use_general(self):
        co = self.prepare('--timeout', '3600', '--exec-turn-timeout', '10800')
        for phase in ('EXEC', 'FINISH', 'DOCS', 'PLAN'):
            actual, env, original = self.dispatch(co, 'author', phase)
            expected = '3600000' if phase == 'PLAN' else '10800000'
            self.assertEqual(env['BASH_MAX_TIMEOUT_MS'], expected)
            if phase == 'PLAN':
                self.assertEqual(actual, original)
            else:
                self.assertIn(f'gets {expected} for a long command.', actual)

    def test_all_short_turns_keep_prompt_and_environment(self):
        co = self.prepare('--timeout', '3600', '--exec-turn-timeout', '3600')
        for role in ('author', 'reviewer', 'shadow', 'gate'):
            actual, env, original = self.dispatch(co, role)
            self.assertEqual(actual, original)
            self.assertEqual(env['BASH_MAX_TIMEOUT_MS'], '3600000')

    def test_released_frozen_state_resumes_and_gate_uses_four_hours(self):
        co = self.prepare('--timeout', '14400', released=True)
        self.assertIsNone(co._program_state()[1])
        frozen = copy.deepcopy({key: co.state[key] for key in
                                ('config', 'role_dispatch_manifest', 'operator_programs')})
        co.save()
        args = copy.deepcopy(co.args)
        args.action = 'resume'
        resumed = rc.Coordinator(args)
        self.assertIsNone(resumed._program_state()[1])
        self.assertEqual({key: resumed.state[key] for key in frozen}, frozen)
        self.assertEqual(resumed._role_dispatch_manifest(), frozen['role_dispatch_manifest'])
        actual, env, _ = self.dispatch(resumed, 'gate')
        self.assertEqual(env['BASH_MAX_TIMEOUT_MS'], '14400000')
        self.assertIn('gets 14400000 for a long command.', actual)
        self.assertEqual({key: resumed.state[key] for key in frozen}, frozen)

    def test_resume_timeout_raises_review_and_gate_command_budgets(self):
        co = self.prepare()
        co.save()
        args = copy.deepcopy(co.args)
        args.action = 'resume'
        args.resume_timeout = 14400
        resumed = rc.Coordinator(args)
        for role in ('reviewer', 'gate'):
            actual, env, _ = self.dispatch(resumed, role)
            self.assertEqual(env['BASH_MAX_TIMEOUT_MS'], '14400000')
            self.assertIn('gets 14400000 for a long command.', actual)


if __name__ == '__main__':
    unittest.main()
