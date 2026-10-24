"""V327-RETRY (real run a277c903, v3.2.6 rc2, and gg-pipeline WI-282a, v3.2.6): a role turn whose CLI exits at once with a
transient provider error ("Selected model is at capacity", Anthropic's overloaded_error / 529) and shows no tool call is
refunded and re-dispatched once after a short backoff, with a NOTE. A second such failure, or one after a tool call, still
holds, with a hint that resume retries the turn. A Codex "unauthorized (401)" is never retried: its HOLD names the
CODEX_HOME whose login is missing or stale. Drives run in process on the fake CLIs (FAKE_PROVIDER_FAIL + _MATCH + _ONCE)."""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
AUTHOR = 'implementer. Phase: EXEC.'
CAPACITY = 'Selected model is at capacity. Please try a different model.'
UNAUTHORIZED = 'workspace routing discovery unauthorized (401)'


class TransientRetryTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        for module in {id(m): m for m in (rc, sys.modules.get('paired_session.coordinator')) if m}.values():
            pin = patch.object(module, 'TRANSIENT_RETRY_SECONDS', 0)
            pin.start()
            self.addCleanup(pin.stop)

    def drive(self, fail, once=True, **env):
        env = {'FAKE_PROVIDER_FAIL': fail, 'FAKE_PROVIDER_FAIL_MATCH': AUTHOR, **env}
        if once:
            env['FAKE_PROVIDER_FAIL_ONCE'] = str(self.h.root / 'failed-once')
        out = io.StringIO()
        with patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            code = rc.main([*self.h.command('--shadow', 'off')[2:], '--skip-probe'])
        return code, out.getvalue(), json.loads((self.h.run_dir / 'state.json').read_text())

    def failed(self, state):
        return [row for row in state['turns'] if row.get('error')]

    def assert_budget(self, state):
        self.assertEqual(state['invocations_used'], sum(row['invocation_budget_counted'] is not False for row in state['turns']))

    def test_a_capacity_error_before_any_work_is_retried_once_and_not_charged(self):
        code, out, state = self.drive(CAPACITY)
        self.assertEqual((code, state['status']), (0, 'DONE'), out + str(state.get('hold_reason')))
        self.assertIn(f'NOTE: transient provider error ({CAPACITY}); retrying the author turn once in 0 s', out)
        [failed] = self.failed(state)
        self.assertEqual((failed['role'], failed['phase'], failed['error_kind'], failed['invocation_budget_counted']),
                         ('author', 'EXEC', 'transient', False))
        self.assertEqual(failed['error'], f'CLI exit 1: {CAPACITY}' + rc.TRANSIENT_HOLD_HINT)
        retry = next(row for row in state['turns'] if row['sequence'] == failed['sequence'] + 1)
        self.assertEqual((retry['role'], retry['phase'], retry.get('error')), ('author', 'EXEC', None))
        self.assert_budget(state)

    def test_a_codex_401_is_not_retried_and_names_the_codex_home(self):
        code, out, state = self.drive(UNAUTHORIZED, once=False)
        self.assertEqual((code, state['status']), (2, 'HOLD'), out)
        self.assertNotIn('NOTE: transient provider error', out)
        [failed] = self.failed(state)
        self.assertEqual(state['hold_reason'], f'CLI exit 1: {UNAUTHORIZED}; the Codex login in {failed["global_codex_home"]} is '
                         "missing or stale: refresh that home's auth.json (copy a current login or log in there), then resume")
        self.assertTrue(failed['invocation_budget_counted'])
        self.assertNotIn('error_kind', failed)
        self.assert_budget(state)

    def test_a_second_capacity_error_holds_with_the_resume_hint(self):
        code, out, state = self.drive(CAPACITY, once=False)
        self.assertEqual((code, state['status']), (2, 'HOLD'), out)
        self.assertEqual(out.count('NOTE: transient provider error'), 1)
        self.assertEqual(state['hold_reason'], f'CLI exit 1: {CAPACITY}; a transient provider error: resume retries the turn')
        failed = self.failed(state)
        self.assertEqual([(row['role'], row['phase'], row['error_kind'], row['invocation_budget_counted']) for row in failed],
                         [('author', 'EXEC', 'transient', False)] * 2)
        self.assertNotIn('hold_kind', state)
        self.assert_budget(state)

    def assert_plain_hold(self, text):
        code, out, state = self.drive(text, once=False)
        self.assertEqual((code, state['status']), (2, 'HOLD'), out)
        self.assertEqual(state['hold_reason'], f'CLI exit 1: {text}')
        self.assertNotIn('transient provider error', out)
        [failed] = self.failed(state)
        self.assertTrue(failed['invocation_budget_counted'])
        self.assertNotIn('error_kind', failed)

    def test_an_invalid_api_key_holds_as_before_without_a_retry(self):
        self.assert_plain_hold('invalid api key')

    def test_a_generic_cli_error_holds_as_before_without_a_retry(self):
        self.assert_plain_hold('something went wrong')

    def test_a_capacity_error_after_a_tool_call_holds_with_the_hint(self):
        code, out, state = self.drive(CAPACITY, FAKE_PROVIDER_FAIL_AFTER_TOOL='1')
        self.assertEqual((code, state['status']), (2, 'HOLD'), out)
        self.assertNotIn('NOTE: transient provider error', out)
        self.assertEqual(state['hold_reason'], f'CLI exit 1: {CAPACITY}' + rc.TRANSIENT_HOLD_HINT)
        [failed] = self.failed(state)
        self.assertTrue(failed['invocation_budget_counted'])   # the provider did work: the attempt stays counted
        self.assertNotIn('error_kind', failed)
        self.assert_budget(state)


class TransientClassifierTests(unittest.TestCase):
    def test_only_the_explicit_capacity_and_overload_texts_are_transient(self):
        claude_overloaded = json.dumps({'type': 'result', 'is_error': True, 'result': 'API Error: 529 {"type":"error","error":'
                                        '{"type":"overloaded_error","message":"Overloaded"}}'})
        codex_capacity = json.dumps({'type': 'turn.failed', 'error': {'message': CAPACITY}})
        for stderr, stdout in (('', claude_overloaded), ('', codex_capacity), (CAPACITY + '\n', ''),
                               ('', json.dumps({'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}})),
                               ('HTTP 529 Overloaded\n', '')):
            with self.subTest(stderr=stderr, stdout=stdout):
                self.assertTrue(rc.classify_transient_failure(1, stderr, stdout))
        for stderr, stdout in (('', json.dumps({'type': 'error', 'message': UNAUTHORIZED})), ('invalid api key\n', ''),
                               ('HTTP 429 rate limit\n', ''),
                               ('', json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': CAPACITY}}))):
            with self.subTest(stderr=stderr, stdout=stdout):
                self.assertFalse(rc.classify_transient_failure(1, stderr, stdout))
        self.assertFalse(rc.classify_transient_failure(0, CAPACITY, ''))
        self.assertFalse(rc.classify_transient_failure(-9, CAPACITY, ''))


if __name__ == '__main__':
    unittest.main()
