"""V301: only an explicit resume may raise the saved invocation cap."""
import copy
import json
import shutil
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
            'fake_codex_cli', 'fake_claude_cli', 'coordinator')


class InvocationCapTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def restored(self, action='resume', *extra, defaults=None):
        parser = rc.parser()
        if defaults:
            parser.set_defaults(**defaults)
        return rc.Coordinator(parser.parse_args([action, '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli()),
            *extra]))

    def resume_without_drive(self, co):
        with patch.object(co, 'drive', return_value='HOLD'):
            self.assertEqual(co.resume(), 'HOLD')
        return json.loads(co.state_path.read_text())

    def test_raise_persists_and_later_dispatch_uses_new_cap(self):
        co = self.coordinator('--max-invocations', '1')
        co._invoke_once('author', 'PLAN', 'Role prompt.', {})
        with self.assertRaisesRegex(RuntimeError, 'invocation limit reached'):
            co._invoke_once('author', 'PLAN', 'Role prompt.', {})
        resumed = self.restored('resume', '--max-invocations', '2')
        saved = self.resume_without_drive(resumed)
        self.assertEqual(saved['config']['max_invocations'], 2)
        self.assertEqual(len(saved['invocation_cap_raises']), 1)
        row = saved['invocation_cap_raises'][0]
        self.assertEqual((row['old'], row['new']), (1, 2))
        self.assertTrue(row['timestamp'])
        events = [json.loads(line) for line in (self.run_dir / 'progress.jsonl').read_text().splitlines()]
        self.assertTrue(any(event['kind'] == 'invocation-cap-raise' and
                            event['old'] == 1 and event['new'] == 2 for event in events))
        later = self.restored()
        self.assertEqual(later.args.max_invocations, 2)
        later._invoke_once('author', 'PLAN', 'Role prompt.', {})
        self.assertEqual(later.state['invocations_used'], 2)
        with self.assertRaisesRegex(RuntimeError, 'invocation limit reached'):
            later._invoke_once('author', 'PLAN', 'Role prompt.', {})

    def test_lower_is_refused(self):
        self.coordinator('--max-invocations', '3')
        with self.assertRaisesRegex(ValueError, '--max-invocations cannot lower saved value 3'):
            self.restored('resume', '--max-invocations', '2')
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['config']['max_invocations'], 3)

    def test_same_is_accepted_without_raise_record(self):
        self.coordinator('--max-invocations', '3')
        saved = self.resume_without_drive(self.restored('resume', '--max-invocations', '3'))
        self.assertEqual(saved['config']['max_invocations'], 3)
        self.assertNotIn('invocation_cap_raises', saved)

    def test_profile_default_does_not_raise_cap(self):
        self.coordinator('--max-invocations', '3')
        co = self.restored(defaults={'max_invocations': 9})
        self.assertFalse(co.args.max_invocations_explicit)
        saved = self.resume_without_drive(co)
        self.assertEqual(saved['config']['max_invocations'], 3)
        self.assertNotIn('invocation_cap_raises', saved)

    def lifecycle_raise(self, flag, value, key):
        original = ('--lifecycle-mode', 'on', '--max-invocations', '3',
                    '--timeout', '10', '--exec-turn-timeout', '7200')
        co = self.coordinator(*original)
        manifest = copy.deepcopy(co.state['role_dispatch_manifest'])
        resumed = self.restored('resume', *original, flag, str(value))
        self.resume_without_drive(resumed)
        expected = copy.deepcopy(manifest)
        expected['config_sha256'] = resumed.state['role_dispatch_manifest']['config_sha256']
        self.assertEqual(resumed.state['role_dispatch_manifest'], expected)
        self.assertNotEqual(expected['config_sha256'], manifest['config_sha256'])
        resumed._invoke_once('author', 'PLAN', 'Role prompt.', {})
        self.assertEqual(resumed.state['invocations_used'], 1)
        self.assertEqual(resumed.state['config'][key], value)
        # Follow-up resume uses original --timeout, but no new raise options.
        later_flags = ('--lifecycle-mode', 'on', '--timeout', '10')
        later = self.restored('resume', *later_flags)
        with patch.object(later, '_refresh_resumed_config_digest') as refresh:
            self.resume_without_drive(later)
            refresh.assert_not_called()
        later._invoke_once('author', 'PLAN', 'Role prompt.', {})
        self.assertEqual(later.state['invocations_used'], 2)
        self.assertEqual(getattr(later.args, key), value)
        # Operator commands restore raised settings, including original timeout flags.
        for action in ('accept', 'reject', 'note'):
            operator = self.restored(action, *original, '--max-invocations',
                                     str(later.state['config']['max_invocations']))
            self.assertEqual(getattr(operator.args, key), value)
            operator._verify_frozen_role_dispatch()
        return later

    def test_lifecycle_invocation_raise_and_followups(self):
        self.lifecycle_raise('--max-invocations', 5, 'max_invocations')

    def test_lifecycle_exec_timeout_raise_and_followups(self):
        self.lifecycle_raise('--exec-turn-timeout', 9000, 'exec_turn_timeout')

    def test_lifecycle_general_timeout_raise_and_followups(self):
        self.lifecycle_raise('--resume-timeout', 20, 'timeout')

    def test_lifecycle_raises_preserve_agent_body_and_role_flag_checks(self):
        agents = self.root / 'role-sources'
        shutil.copytree(rc.HERE.parent / 'agents', agents)
        frozen_role_manifest = rc.frozen_role_manifest

        def manifest_from_copied_agents(config, flags, agents_dir, prompt, bodies):
            return frozen_role_manifest(config, flags, agents, prompt, bodies)

        with patch.object(rc, 'frozen_role_manifest', side_effect=manifest_from_copied_agents):
            co = self.coordinator('--lifecycle-mode', 'on', '--max-invocations', '3')
            frozen = copy.deepcopy(co.state['role_dispatch_manifest'])
            # Drift before the raise must not be laundered by refreshing the config digest.
            body = agents / 'executor.md'
            original_body = body.read_text()
            body.write_text(original_body + '\nChanged agent instructions.\n')
            resumed = self.restored('resume', '--lifecycle-mode', 'on', '--max-invocations', '5')
            self.resume_without_drive(resumed)
            self.assertEqual(resumed.state['role_dispatch_manifest']['agent_body_sha256'],
                             frozen['agent_body_sha256'])
            with self.assertRaisesRegex(RuntimeError, 'frozen role dispatch changed'):
                resumed._invoke_once('author', 'PLAN', 'Role prompt.', {})
            self.assertEqual(resumed.state['invocations_used'], 0)
            # A later edit remains detectable too.
            body.write_text(body.read_text() + '\nAnother edit after the raise.\n')
            with self.assertRaisesRegex(RuntimeError, 'frozen role dispatch changed'):
                self.restored('resume')._invoke_once('author', 'PLAN', 'Role prompt.', {})
            body.write_text(original_body)
            resumed._verify_frozen_role_dispatch()
            resumed.state['role_dispatch_manifest']['role_flags']['author']['model'] = 'gpt-6-sol'
            with self.assertRaisesRegex(RuntimeError, 'frozen role dispatch changed'):
                resumed._verify_frozen_role_dispatch()

    def test_non_resume_actions_keep_frozen_cap(self):
        self.coordinator('--max-invocations', '3')
        for action in ('run', 'permission-probe', 'accept', 'reject', 'note', 'attach-verification'):
            with self.subTest(action=action), self.assertRaisesRegex(
                    ValueError, 'resume configuration differs: max_invocations'):
                self.restored(action, '--max-invocations', '4')


if __name__ == '__main__':
    unittest.main()
