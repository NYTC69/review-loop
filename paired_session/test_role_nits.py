"""ROLE-NITS: dispatched_vendors names every Codex role that dispatches, the gate included (the strict codex-cli contract check
that used it was removed in v3.7.0)."""
import unittest

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_operator_roles as tor

rc = tor.rc


class DispatchedVendorTests(unittest.TestCase):
    setUp = tor.OperatorCliTests.setUp

    def test_dispatched_vendors_name_the_gate(self):
        use_lifecycle_on(self, self.h)
        co = self.h.coordinator('--gate-vendor', 'codex', '--author-vendor', 'claude', '--reviewer-vendor', 'claude',
                                '--test-command', 'python3 -m unittest')
        self.assertEqual(co.dispatched_vendors(), ('claude', 'claude', 'codex'))


if __name__ == '__main__':
    unittest.main()
