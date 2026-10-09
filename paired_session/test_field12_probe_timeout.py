"""FIELD-12 (poker-news-bot WI-87/88 probe, 2026-10-04): the probe's allowed test command hit the CLI's 10-minute Bash
timeout under load and was reported as `allowed-command-failed`. The Claude tool result says so in its first lines
("Exit code 143\\nCommand timed out after 10m 0s", evidence `_WI-87_probe_timeout_20261004`). A timeout is now reported
separately as `allowed-command-timeout (<N> s)`; the probe still fails."""
import unittest

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import coordinator as rc
from paired_session import test_real_coordinator as trc

COMMAND = 'python3 -m pytest tests/ab -q'


class ProbeTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        use_lifecycle_on(self, self.h)
        self.co = self.h.coordinator()

    def evaluate(self, output, exit_code=-1, error=True):
        row = {'command': COMMAND, 'exit_code': exit_code, 'error': error, 'output': output}
        return self.co._probe_eval([row], True, COMMAND, ())

    def test_a_tool_timeout_is_reported_with_its_duration(self):
        allowed, _, _, failures = self.evaluate('Exit code 143\nCommand timed out after 10m 0s\n........ [ 78%]')
        self.assertFalse(allowed)                                            # the probe still fails
        self.assertEqual(failures, [f'allowed-command-timeout (600 s): {COMMAND}'])

    def test_other_durations_are_converted_to_seconds(self):
        for text, seconds in (('2m 30s', 150), ('1h 0m 5s', 3605), ('45s', 45), ('1.5s', 2)):
            with self.subTest(text=text):
                failures = self.evaluate(f'Exit code 143\nCommand timed out after {text}\n')[3]
                self.assertEqual(failures, [f'allowed-command-timeout ({seconds} s): {COMMAND}'])

    def test_a_head_without_the_exit_code_line_and_milliseconds_are_recognised(self):
        self.assertEqual(self.evaluate('Command timed out after 2m 0s\n')[3], [f'allowed-command-timeout (120 s): {COMMAND}'])
        self.assertEqual(self.evaluate('Exit code 143\nCommand timed out after 500ms\n')[3],
                         [f'allowed-command-timeout (1 s): {COMMAND}'])   # whole seconds, rounded up; never read as 500 minutes

    def test_an_ordinary_failure_stays_allowed_command_failed(self):
        failures = self.evaluate('Exit code 1\n3 failed, 10 passed', exit_code=1)[3]
        self.assertEqual(failures, [f'allowed-command-failed: {COMMAND}'])

    def test_the_marker_counts_only_in_the_tool_result_head(self):
        output = 'Exit code 1\n' + 'x\n' * 50 + 'test_timeouts.py: Command timed out after 10m 0s (a test fixture string)\n'
        failures = self.evaluate(output, exit_code=1)[3]
        self.assertEqual(failures, [f'allowed-command-failed: {COMMAND}'])

    def test_a_passing_command_is_unchanged(self):
        allowed, _, _, failures = self.evaluate('10 passed', exit_code=0, error=False)
        self.assertTrue(allowed)
        self.assertEqual(failures, [])


if __name__ == '__main__':
    unittest.main()
