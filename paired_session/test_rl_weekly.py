"""RL-WEEKLY (field, poker-news-bot WI-109): the Claude weekly-limit stream shape is a rate limit, with a reset hint;
an ordinary CLI error stays billable. The fixture is the real last three stdout lines of that turn.
V315 (field, 2026-10-12): the Codex usage-limit message, which codex-cli 0.160.0 writes with a typographic apostrophe."""
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
FIXTURE = Path(__file__).with_name('testdata') / 'claude_weekly_limit_tail3.jsonl'
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
           'fake_codex_cli', 'fake_claude_cli', 'coordinator', 'command', 'run_coordinator')
CODEX_LIMIT = ('You\u2019ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits '
               'or try again at Oct 18th, 2026 12:57 PM.')   # V315: what codex-cli 0.160.0 printed, U+2019 included
CODEX_EVENTS = ('{"type":"error","message":"%s"}' % CODEX_LIMIT, '{"type":"turn.failed","error":{"message":"%s"}}' % CODEX_LIMIT)
CODEX_STDERR = 'ERROR codex_core::session: failed to record rollout items: thread 0199f3a1 not found\n'   # unrelated
CODEX_LIMITED = {'kind': 'rate_limited', 'reset_hint': 'try again at Oct 18th, 2026 12:57 PM.'}


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.lines = FIXTURE.read_text().splitlines()
        tz = patch.dict(os.environ, {'TZ': 'Asia/Tokyo'})
        tz.start()
        time.tzset()
        self.addCleanup(lambda: (tz.stop(), time.tzset()))

    def test_the_real_weekly_limit_tail_is_rate_limited_with_the_exact_reset(self):
        self.assertEqual(rc.classify_rate_limit_failure(1, '', '\n'.join(self.lines)),
                         {'kind': 'rate_limited', 'reset_hint': 'resets at 2026-10-10 15:00 JST (seven_day window)'})

    def test_each_signal_alone_is_enough(self):
        for line in self.lines[1:]:   # the assistant error message, then the result event
            with self.subTest(type=json.loads(line)['type']):
                self.assertEqual(rc.classify_rate_limit_failure(1, '', line),
                                 {'kind': 'rate_limited', 'reset_hint': 'resets Oct 10 at 3pm (Asia/Tokyo)'})
        self.assertEqual(rc.classify_rate_limit_failure(1, '', self.lines[0])['kind'], 'rate_limited')

    def test_limit_wordings(self):
        for text in ("You've hit your session limit", "You've hit your 5-hour limit", "You've hit your Opus limit",
                     "You've hit your usage limit"):
            for apostrophe in ("'", '\u2019'):   # V315: the typographic apostrophe too
                with self.subTest(text=text, apostrophe=ascii(apostrophe)):
                    self.assertEqual(rc.classify_rate_limit_failure(1, text.replace("'", apostrophe) + '\n')['kind'], 'rate_limited')
        for text in ('You have hit your usage limit', 'You have hit your 5-hour limit'):   # V315: the pattern read " a" for " ha"
            with self.subTest(text=text):
                self.assertEqual(rc.classify_rate_limit_failure(1, text + '\n'), {'kind': 'rate_limited', 'reset_hint': None})
        for text in ('You ave hit your usage limit', 'You have hit an unexpected error'):   # the typo's own wording; not a limit
            with self.subTest(text=text):
                self.assertIsNone(rc.classify_rate_limit_failure(1, text + '\n'))

    def test_the_codex_usage_limit_with_a_typographic_apostrophe_is_rate_limited(self):   # V315
        self.assertIn('\u2019', CODEX_LIMIT)
        cases = {'the real turn': (CODEX_STDERR, '\n'.join(CODEX_EVENTS)), 'error alone': (CODEX_STDERR, CODEX_EVENTS[0]),
                 'turn.failed alone': (CODEX_STDERR, CODEX_EVENTS[1]), 'stderr only': (CODEX_LIMIT + '\n', '')}
        for case, (stderr, stdout) in cases.items():
            with self.subTest(case=case):
                self.assertEqual(rc.classify_rate_limit_failure(1, stderr, stdout), CODEX_LIMITED)
        self.assertIsNone(rc.classify_rate_limit_failure(1, CODEX_STDERR))   # the unrelated stderr line alone

    def test_a_typographic_apostrophe_alone_is_not_a_limit(self):   # V315
        for message in ('The patch tool couldn\u2019t write the file', 'You\u2019ve hit an unexpected error',
                        'You\u2019ve reached the end of the limit test'):
            with self.subTest(message=message):
                self.assertIsNone(rc.classify_rate_limit_failure(1, '', json.dumps({'type': 'error', 'message': message})))
                self.assertIsNone(rc.classify_rate_limit_failure(1, message + '\n'))

    def test_ordinary_errors_stay_billable(self):
        for stdout in ('{"type":"result","is_error":true,"result":"Error: tool crashed","api_error_status":500}',
                       '{"type":"assistant","is_api_error_message":true,"error":"overloaded"}',
                       '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1791612000}}',
                       '{"type":"result","is_error":true,"result":"You hit a wall in the limit test"}'):
            with self.subTest(stdout=stdout):
                self.assertIsNone(rc.classify_rate_limit_failure(1, '', stdout))
        self.assertIsNone(rc.classify_rate_limit_failure(0, '', '\n'.join(self.lines)))   # a success is never a limit

    def test_an_overage_window_or_a_later_allowed_event_is_not_a_limit(self):   # gate B1
        ordinary = '{"type":"result","is_error":true,"result":"Error: tool crashed","api_error_status":500}'
        event = lambda **info: json.dumps({'type': 'rate_limit_event', 'rate_limit_info': info})
        for events in ([event(status='rejected', isUsingOverage=True, resetsAt=1791612000)],
                       [event(status='rejected', overageStatus='allowed')],
                       [event(status='rejected', resetsAt=1791612000), event(status='allowed_warning')]):
            with self.subTest(events=events):
                self.assertIsNone(rc.classify_rate_limit_failure(1, '', '\n'.join([*events, ordinary])))
        rejected_last = [event(status='allowed'), event(status='rejected', isUsingOverage=False, resetsAt=1791612000)]
        self.assertEqual(rc.classify_rate_limit_failure(1, '', '\n'.join([*rejected_last, ordinary]))['kind'], 'rate_limited')


class WeeklyLimitTurnTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def test_a_claude_author_turn_that_hits_the_weekly_limit_is_not_charged(self):
        co = self.coordinator('--author-vendor', 'claude')
        cli = self.root / 'weekly-limit-cli'
        cli.write_text(f'#!/bin/sh\ncat {FIXTURE}\nexit 1\n')
        cli.chmod(0o755)
        co.args.claude_bin = str(cli)
        with self.assertRaisesRegex(RuntimeError, 'rate_limited'):
            co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})
        receipt = json.loads((co.evidence / '001-plan-author.receipt.json').read_text())
        self.assertEqual(receipt['error_kind'], 'rate_limited')
        self.assertFalse(receipt['invocation_budget_counted'])
        self.assertEqual(json.loads(co.state_path.read_text())['invocations_used'], 0)

    def test_a_codex_author_turn_that_hits_the_usage_limit_holds_as_rate_limited(self):   # V315
        held = self.run_coordinator('--max-invocations', '1', env={'FAKE_RATE_LIMIT': '1', 'FAKE_RATE_LIMIT_CODEX_USAGE': '1'})
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((state['status'], state.get('hold_kind')), ('HOLD', 'rate_limited'), state['hold_reason'])
        self.assertEqual(state['rate_limit'], {'role': 'author', 'phase': 'PLAN', 'sequence': 1,
                                               'reset_hint': CODEX_LIMITED['reset_hint']})
        [turn] = state['turns']
        self.assertEqual((turn['vendor'], turn['error_kind'], turn['invocation_budget_counted']), ('codex', 'rate_limited', False))
        self.assertEqual(state['invocations_used'], 0)


if __name__ == '__main__':
    unittest.main()
