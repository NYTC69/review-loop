"""V301: only an explicit resume may raise the saved invocation cap."""
import json
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

    def test_non_resume_actions_keep_frozen_cap(self):
        self.coordinator('--max-invocations', '3')
        for action in ('run', 'permission-probe', 'accept', 'reject', 'note', 'attach-verification'):
            with self.subTest(action=action), self.assertRaisesRegex(
                    ValueError, 'resume configuration differs: max_invocations'):
                self.restored(action, '--max-invocations', '4')


if __name__ == '__main__':
    unittest.main()
