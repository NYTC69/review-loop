"""Method-scoped lifecycle selection without importing coordinator test fixtures."""
import os
from unittest.mock import patch


def use_lifecycle_on(test, harness):
    """Select on for this test, leaving shared fixture defaults unchanged.

    Disable the shared Python-API off default and legacy removed-option fixtures.
    Explicitly select on when the harness exposes command.
    Restore the fixture environment and command at cleanup.
    """
    lifecycle = patch.dict(os.environ, {'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '0',
                                      'PAIRED_SESSION_INTERNAL_TEST_REMOVED_OPTIONS': '0'})
    lifecycle.start()
    test.addCleanup(lifecycle.stop)
    command = getattr(harness, 'command', None)
    if command is None:
        return
    cli = patch.object(harness, 'command',
                       new=lambda *extra: command('--lifecycle-mode', 'on', *extra))
    cli.start()
    test.addCleanup(cli.stop)
