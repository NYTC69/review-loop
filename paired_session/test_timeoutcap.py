"""timeoutcap (backlog audit #7, BACKLOG "General `--timeout` has no upper bound"): `--timeout` is bounded to 1..86400 seconds
and refused outside that range before any run state exists; the default stays 2700. 86400, not the 14400 EXEC cap: an existing
run (test_legacy_state_timeout_fallback_and_config_default_is_not_cli_raise) starts with --timeout 20000."""
import contextlib
import io
import unittest
from unittest.mock import patch

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_real_coordinator as trc

rc = trc.rc


class TimeoutCapTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def test_the_default_is_unchanged(self):
        use_lifecycle_on(self, self.h)
        self.assertEqual(rc.parser().parse_args(['run', '--workspace', 'w', '--workitem', 'i', '--run-dir', 'r']).timeout, 2700)
        self.assertEqual(rc.DEFAULT_TIMEOUT_SECONDS, 2700)
        self.assertEqual(self.h.coordinator().state['config']['timeout'], 2700)

    def test_the_cap_is_accepted_and_a_value_outside_the_range_is_refused_before_state(self):
        use_lifecycle_on(self, self.h)
        self.assertEqual(rc.MAX_TIMEOUT_SECONDS, 86400)
        self.h.run_dir = self.h.root / 'at-cap'
        self.assertEqual(self.h.coordinator('--timeout', '86400').state['config']['timeout'], 86400)
        for value in ('86401', '0', '-5'):
            with self.subTest(value=value):
                self.h.run_dir = self.h.root / ('refused' + value)
                with self.assertRaisesRegex(ValueError, r'--timeout must be between 1 and 86400 seconds'):
                    self.h.coordinator('--timeout', value)
                self.assertFalse(self.h.run_dir.exists())

    def test_the_cli_refuses_it_before_any_lease_run_dir_or_detached_child(self):
        for extra in ((), ('--detach',)):
            with self.subTest(extra=extra):
                self.h.run_dir = self.h.root / ('cli' + ''.join(extra))
                command = self.h.command('--timeout', '100000', *extra)
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                        patch.object(rc, 'detach', side_effect=AssertionError('no detached child may start')), \
                        patch.object(rc, 'run_lease', side_effect=AssertionError('no lease may be taken')):
                    code = rc.main(command[2:])
                self.assertEqual(code, 2)
                self.assertIn('REFUSED: --timeout must be between 1 and 86400 seconds', out.getvalue())
                self.assertFalse(self.h.run_dir.exists())
        self.assertIn('at most 86400 seconds', ' '.join(rc.parser().format_help().split()))


if __name__ == '__main__':
    unittest.main()
