"""RL-WEEKLY (field, poker-news-bot WI-109): the Claude weekly-limit stream shape is a rate limit, with a reset hint;
an ordinary CLI error stays billable. The fixture is the real last three stdout lines of that turn.
V315 (field, 2026-10-12): the Codex usage-limit message, which codex-cli 0.160.0 writes with a typographic apostrophe."""
import json
import os
import time
import unittest
from datetime import datetime
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
CODEX_LIMITED = {'kind': 'rate_limited', 'reset_hint': 'try again at Oct 18th, 2026 12:57 PM.',
                 'reset_at': datetime.fromisoformat('2026-10-18T12:57:00+09:00').timestamp()}


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.lines = FIXTURE.read_text().splitlines()
        tz = patch.dict(os.environ, {'TZ': 'Asia/Tokyo'})
        tz.start()
        time.tzset()
        self.addCleanup(lambda: (tz.stop(), time.tzset()))
        self.now = datetime.fromisoformat('2026-10-09T12:00:00+00:00').timestamp()
        clock = patch.object(rc.time, 'time', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def test_the_real_weekly_limit_tail_is_rate_limited_with_the_exact_reset(self):
        self.assertEqual(rc.classify_rate_limit_failure(1, '', '\n'.join(self.lines)),
                         {'kind': 'rate_limited', 'reset_hint': 'resets at 2026-10-10 15:00 JST (seven_day window)',
                          'reset_at': 1791612000})

    def test_each_signal_alone_is_enough(self):
        for line in self.lines[1:]:   # the assistant error message, then the result event
            with self.subTest(type=json.loads(line)['type']):
                self.assertEqual(rc.classify_rate_limit_failure(1, '', line),
                                 {'kind': 'rate_limited', 'reset_hint': 'resets Oct 10 at 3pm (Asia/Tokyo)',
                                  'reset_at': 1791612000})
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

    def test_another_character_in_the_apostrophe_slot_is_not_a_limit(self):   # V316 (v3.1.5 gate note)
        for slot in ('`', 'x'):
            message = f'You{slot}ve hit your usage limit'
            with self.subTest(slot=slot):
                self.assertIsNone(rc.classify_rate_limit_failure(1, message + '\n'))
                self.assertIsNone(rc.classify_rate_limit_failure(1, '', json.dumps({'type': 'error', 'message': message})))

    def test_try_again_in_and_try_again_later_are_reset_hints(self):
        for hint in ('Try again in 3 days 2 hours 5 minutes.', 'Try again later.'):
            message = 'You’ve hit your usage limit. ' + hint
            for case, (stderr, stdout) in {'stderr': (message + '\n', ''),
                                           'event': ('', json.dumps({'type': 'error', 'message': message}))}.items():
                with self.subTest(hint=hint, case=case):
                    expected = {'kind': 'rate_limited', 'reset_hint': hint}
                    if hint.startswith('Try again in'):
                        expected['reset_at'] = self.now + 266700
                    self.assertEqual(rc.classify_rate_limit_failure(1, stderr, stdout), expected)
        for message in ('The tool crashed; try again later.', 'Network error. Try again in 5 minutes.'):   # a cue is not a limit
            with self.subTest(message=message):
                self.assertIsNone(rc.classify_rate_limit_failure(1, message + '\n'))
                self.assertIsNone(rc.classify_rate_limit_failure(1, '', json.dumps({'type': 'error', 'message': message})))

    def test_specific_hints_beat_try_again_later_across_lines_and_sources(self):
        generic = 'You’ve hit your usage limit. Try again later.'
        for hint in ('retry-after: 120', 'resets at 5pm', 'try again at 5pm', 'try again in 120 seconds'):
            event = json.dumps({'type': 'error', 'message': hint, 'code': 'rate_limit_exceeded'})
            for stderr, stdout in ((generic + '\n' + hint, ''),
                                   ('', json.dumps({'type': 'error', 'message': generic + '\n' + hint})),
                                   (generic, event)):
                with self.subTest(hint=hint, stderr=stderr):
                    expected = {'kind': 'rate_limited', 'reset_hint': hint}
                    if '120' in hint:
                        expected['reset_at'] = self.now + 120
                    self.assertEqual(rc.classify_rate_limit_failure(1, stderr, stdout), expected)
        self.assertEqual(rc.classify_rate_limit_failure(1, generic)['reset_hint'], 'Try again later.')

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

    def test_claude_window_cancels_only_earlier_quota_signals(self):
        signals = ({'type': 'assistant', 'is_api_error_message': True, 'error': 'rate_limit'},
                   {'type': 'result', 'is_error': True, 'api_error_status': 429})
        for info in ({'status': 'allowed_warning'}, {'status': 'rejected', 'isUsingOverage': True}):
            window = json.dumps({'type': 'rate_limit_event', 'rate_limit_info': info})
            for signal in signals:
                with self.subTest(window=info, signal=signal):
                    error = json.dumps(signal)
                    self.assertEqual(rc.classify_rate_limit_failure(1, '', '\n'.join([window, error])),
                                     {'kind': 'rate_limited', 'reset_hint': None})
                    self.assertIsNone(rc.classify_rate_limit_failure(1, '', '\n'.join([error, window])))

    def test_agy_429_requires_a_quota_status_or_code(self):
        for message, limited in (('SyntaxError in src/app.py:429', False),
                                 ('429: request throttled', True), ('HTTP 429', True),
                                 ('Provider error code: 429', True), ('status=429', True)):
            with self.subTest(message=message):
                stdout = json.dumps({'event': 'result', 'result': {'status': 'ERROR', 'error': message}})
                expected = {'kind': 'rate_limited', 'reset_hint': None} if limited else None
                self.assertEqual(rc.classify_rate_limit_failure(0, '', stdout, vendor='gemini'), expected)

    def test_provider_reset_values(self):
        for hint, epoch in (
            ('Try again in 2h 3m 4s.', self.now + 7384),
            ('resets in 5 minutes', self.now + 300),
            ('available again at 2026-10-09T12:09:00Z.', self.now + 540),
            ('resets at 2026-10-09T21:09:00+0900', self.now + 540),
            ('Retry-After: Fri, 09 Oct 2026 12:10:00 GMT', self.now + 600),
            ('resets Oct 9 at 8pm (Asia/Tokyo)', self.now - 3600),
            ('try again at Oct 9th 10:00 PM', self.now + 3600),
            ('resets at 5pm', None),
            ('try again at Oct 9th 10:00 PM (CST)', None),
            ('resets at 2026-52-99T00:00:00Z', None),
            ('resets at 2026-10-09T12:09:00+05:60', None),
            ('try again in 2m 3m', None),
        ):
            with self.subTest(hint=hint):
                limit = rc.classify_rate_limit_failure(1, 'HTTP 429 Too Many Requests\n' + hint)
                self.assertEqual(limit.get('reset_at'), epoch)
                self.assertEqual(limit['reset_hint'], hint)
        event = lambda **error: json.dumps({'type': 'error', 'error': {'code': 'quota_exceeded', **error}})
        self.assertEqual(rc.classify_rate_limit_failure(1, '', event(
            retry_delay={'seconds': 20, 'nanos': 500_000_000}, message='try again in 60 seconds'))['reset_at'],
            self.now + 20.5)
        self.assertEqual(rc.classify_rate_limit_failure(1, '', event(
            resetsAt='2026-10-09T12:00:30+00:00'))['reset_at'], self.now + 30)
        window = json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {
            'status': 'rejected', 'resetsAt': 'invalid', 'retry_after': 15}})
        self.assertEqual(rc.classify_rate_limit_failure(1, '', window)['reset_at'], self.now + 15)
        self.assertIsNone(rc.classify_rate_limit_failure(1, 'unauthorized\nRetry-After: 900',
                                                        json.dumps({'event': 'note', 'retryDelay': '2s'})))
        self.now = datetime.fromisoformat('2026-12-31T14:50:00+00:00').timestamp()
        self.assertEqual(rc.classify_rate_limit_failure(1, "You've hit your usage limit. Try again at Jan 1 at 12am.")[
            'reset_at'], self.now + 600)
        self.now = datetime.fromisoformat('2026-12-31T15:10:00+00:00').timestamp()
        self.assertEqual(rc.classify_rate_limit_failure(1, "You've hit your usage limit. Try again at Dec 31 at 11:59pm.")[
            'reset_at'], self.now - 660)
        for status in ('allowed', 'allowed_warning'):
            tail = '\n'.join([*self.lines, json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': status}})])
            self.assertIsNone(rc.classify_rate_limit_failure(1, '', tail))


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
        held = self.run_coordinator('--max-invocations', '1', '--max-rate-limit-wait', '0',
                                    env={'FAKE_RATE_LIMIT': '1', 'FAKE_RATE_LIMIT_CODEX_USAGE': '1'})
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
