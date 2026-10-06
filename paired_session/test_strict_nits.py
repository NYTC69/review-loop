"""STRICT-NITS (BACKLOG triage 2026-10-06, strict or direct-call paths): a replaced operator override is kept in
<key>_history (P0-2 b); resume, reject and resume --polish refuse an unverified dispatch contract before changing state (P0-2 d)."""
import json
import os
import unittest
from unittest.mock import patch

from paired_session import test_operator_roles as tor

rc = tor.rc


class OverrideHistoryTests(unittest.TestCase):
    def test_a_replaced_override_is_kept_in_its_history(self):
        state = {}
        rc.replace_override(state, 'codex_cli_override', {'version': 'a', 'reason': 'first'})
        self.assertNotIn('codex_cli_override_history', state)   # nothing replaced yet
        state['codex_cli_override']['voided'] = {'version_seen': 'b'}
        rc.replace_override(state, 'codex_cli_override', {'version': 'b', 'reason': 'second'})
        self.assertEqual(state['codex_cli_override'], {'version': 'b', 'reason': 'second'})
        self.assertEqual(state['codex_cli_override_history'], [{'version': 'a', 'reason': 'first', 'voided': {'version_seen': 'b'}}])

    def test_the_cli_keeps_the_voided_codex_override(self):
        h = tor.CodexContractTests('test_verified_version_passes')
        h.setUp()
        self.addCleanup(h.doCleanups)
        first = h.cli('run', tor.UNVERIFIED, '--accept-unverified-codex-cli', '--reason', 'first version')
        self.assertIn('reason', h.recorded_override(), first.stdout)
        h.cli('run', tor.OTHER, '--accept-unverified-codex-cli', '--reason', 'second version')
        state = json.loads((h.h.run_dir / 'state.json').read_text())
        self.assertEqual((state['codex_cli_override']['version'], state['codex_cli_override']['reason']), (tor.OTHER, 'second version'))
        [old] = state['codex_cli_override_history']
        self.assertEqual((old['version'], old['reason']), (tor.UNVERIFIED, 'first version'))
        self.assertNotEqual(old['version'], state['codex_cli_override']['version'])   # the replaced record keeps its version


class RefuseBeforeStateTests(unittest.TestCase):
    setUp = tor.CodexContractTests.setUp

    def check(self, status, call):
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': tor.UNVERIFIED}), \
                patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False):
            co = self.h.coordinator('--gate-vendor', 'claude', '--test-command', 'python3 -m unittest')
            self.assertTrue(co.strict)   # the harness pins strict
            co.state.update(status=status, hold_reason='test hold' if status == 'HOLD' else '')
            co.save()
            before = self.run_state(co)
            with self.assertRaisesRegex(ValueError, 'unverified codex sandbox contract: ' + tor.UNVERIFIED):
                call(co)
            self.assertEqual(self.run_state(co), before)   # the refusal changed nothing of the run

    @staticmethod
    def run_state(co):   # the contract check may record the bound operator programs; that is not a run change
        state = json.loads((co.run_dir / 'state.json').read_text())
        state.pop('operator_programs', None)
        return state

    def test_resume_refuses_before_changing_state(self):
        self.check('HOLD', lambda co: co.resume())

    def test_reject_refuses_before_changing_state(self):
        self.check('DONE', lambda co: co.reject('needs another round', None))

    def unverified(self):
        stack = __import__('contextlib').ExitStack()
        stack.enter_context(patch.dict(os.environ, {'FAKE_CODEX_VERSION': tor.UNVERIFIED}))
        stack.enter_context(patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False))
        self.addCleanup(stack.close)
        return self.h.coordinator('--gate-vendor', 'claude', '--test-command', 'python3 -m unittest')

    def test_paths_that_do_not_dispatch_are_not_refused(self):   # R1: no false refusal
        co = self.unverified()
        co.state.update(status='HOLD', active={'pid': 4242, 'role': 'author', 'phase': 'EXEC', 'sequence': 3})
        self.assertEqual(co.resume(), 'HOLD')   # an interrupted turn is recorded as uncertain, as before
        self.assertIn('uncertain in-flight CLI turn', co.state['hold_reason'])
        co.state.update(status='DONE', active=None, uncertain_active=None, rejections=[{}, {}], max_rejections=2)
        with patch.object(co, 'operator_intent', return_value={'tree_sha256': 'x'}):
            self.assertEqual(co.reject('one more', None), 'HOLD')   # the rejection limit HOLDs without a dispatch
        self.assertEqual(co.state.get('terminal_hold_kind'), 'rejection_limit')


if __name__ == '__main__':
    unittest.main()
