"""V322-PROBE: freeze-check the Claude author probe against the run's real context."""
import copy
import json
import os
import sys
import unittest
from unittest.mock import patch

from paired_session import claude_author_probe as cap
from paired_session import test_real_coordinator as harness
from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session.test_operator_roles import FAKE_AUTHOR, LABELS

rc = harness.rc


class ClaudeProbeDispatchTests(unittest.TestCase):
    locals().update({name: getattr(harness.RealCoordinatorTests, name) for name in (
        'setUp', 'tearDown', 'coordinator', 'fake_codex_cli', 'fake_claude_cli',
        '_guarded_test_popen', '_assert_no_real_provider_cli')})

    def claude_run(self):
        use_lifecycle_on(self, self)
        # A real subprocess emits Claude stream-json, but deliberately declines the probe's tool calls.
        fake = self.root / 'fake-author-claude'
        fake.write_text(FAKE_AUTHOR.replace('#!PYTHON', '#!' + sys.executable).replace('LABELS', repr(LABELS)))
        fake.chmod(0o755)
        scenario = patch.dict(os.environ, {'FAKE_AUTHOR_SCENARIO': json.dumps({
            'refuse': True, 'answer': {'status': 'HOLD', 'findings': []}})})
        scenario.start()
        self.addCleanup(scenario.stop)
        settle = patch.object(cap, 'SETTLE_SECONDS', 0.05)
        settle.start()
        self.addCleanup(settle.stop)
        co = self.coordinator('--lifecycle-mode', 'on', '--author-vendor', 'claude',
                              '--claude-bin', str(fake))
        self.assertEqual(co.state['config']['lifecycle_mode'], 'on')
        return co

    def test_probe_reaches_model_and_preserves_frozen_manifest(self):
        co = self.claude_run()
        context = co.context
        keys = ('role_dispatch_manifest', 'role_dispatch_manifest_sha256')
        frozen = {key: copy.deepcopy(co.state[key]) for key in keys}
        out = co._author_permission_probe()
        self.assertNotIn('frozen role dispatch changed', out.get('reason', ''), out)
        self.assertEqual(out['status'], 'UNKNOWN', out)
        self.assertTrue(out['model_refused'], out)
        turn = co.state['turns'][-1]
        self.assertEqual((turn['role'], turn['phase']), ('author', 'AUTHOR_PERMISSION_PROBE'))
        rows = rc.read_json_lines(co.evidence / f'{turn["sequence"]:03d}-author_permission_probe-author.stdout.jsonl')
        self.assertTrue(any(row.get('type') == 'result' for row in rows))
        self.assertEqual(co.context, context)
        self.assertEqual({key: co.state[key] for key in keys}, frozen)
        saved = json.loads(co.state_path.read_text())
        self.assertEqual({key: saved[key] for key in keys}, frozen)
        co._verify_frozen_role_dispatch()

    def test_probe_rejects_a_changed_frozen_digest_before_swapping_context(self):
        co = self.claude_run()
        co.state['role_dispatch_manifest_sha256'] = '0' * 64
        co.save()
        context = co.context
        with patch.object(co, 'invoke', side_effect=AssertionError('must refuse before dispatch')):
            out = co._author_permission_probe()
        self.assertEqual(out['status'], 'FAIL', out)
        self.assertIn('frozen role dispatch changed', out['reason'])
        self.assertEqual(co.context, context)
        self.assertEqual(co.state['turns'], [])

    def test_ordinary_author_phases_still_verify(self):
        co = self.claude_run()
        co.state['role_dispatch_manifest_sha256'] = '0' * 64
        co.save()
        for phase in ('PLAN', 'EXEC', 'FINISH', 'DOCS'):
            with self.subTest(phase=phase), self.assertRaisesRegex(RuntimeError, 'frozen role dispatch changed'):
                co._invoke_once('author', phase, 'Role: persistent. Phase: ' + phase + '.', rc.author_schema())
        self.assertEqual(co.state['turns'], [])


if __name__ == '__main__':
    unittest.main()
