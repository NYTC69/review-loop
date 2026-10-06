"""RL-WEEKLY (field, poker-news-bot WI-109): the Claude weekly-limit stream shape is a rate limit, with a reset hint;
an ordinary CLI error stays billable. The fixture is the real last three stdout lines of that turn."""
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
           'fake_codex_cli', 'fake_claude_cli', 'coordinator')


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
            with self.subTest(text=text):
                self.assertEqual(rc.classify_rate_limit_failure(1, text + '\n')['kind'], 'rate_limited')

    def test_ordinary_errors_stay_billable(self):
        for stdout in ('{"type":"result","is_error":true,"result":"Error: tool crashed","api_error_status":500}',
                       '{"type":"assistant","is_api_error_message":true,"error":"overloaded"}',
                       '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1791612000}}',
                       '{"type":"result","is_error":true,"result":"You hit a wall in the limit test"}'):
            with self.subTest(stdout=stdout):
                self.assertIsNone(rc.classify_rate_limit_failure(1, '', stdout))
        self.assertIsNone(rc.classify_rate_limit_failure(0, '', '\n'.join(self.lines)))   # a success is never a limit


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


if __name__ == '__main__':
    unittest.main()
