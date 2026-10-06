"""ROLE-NITS: the strict codex-cli contract check follows every dispatched Codex role (an AAB run checks it for the
gate; AAA does not; efficient mode is unchanged)."""
import contextlib
import io
import os
import types
import unittest
from unittest.mock import patch

from paired_session import test_operator_roles as tor

rc = tor.rc
CLAUDE_AUTHOR = ('--author-vendor', 'claude', '--reviewer-vendor', 'claude',
                 '--accept-unverified-claude-author', '--reason', 'role-nits test')   # past the Claude-author preview refusal


class DispatchedCodexContractTests(unittest.TestCase):
    setUp = tor.CodexContractTests.setUp

    def cli(self, gate, *, strict=True):
        command = self.h.command('--gate-vendor', gate, *CLAUDE_AUTHOR)
        if not strict:
            command.remove('--strict')
        out = io.StringIO()
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': tor.UNVERIFIED}), \
                patch.object(rc, 'DEFAULT_SAFETY_MODE', 'strict' if strict else 'efficient'), \
                patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), \
                contextlib.redirect_stdout(out):
            code = rc.main(command[2:])
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue())

    def test_an_aab_strict_run_checks_the_codex_cli_for_its_gate(self):
        refused = self.cli('codex')
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn('unverified codex sandbox contract: ' + tor.UNVERIFIED, refused.stdout)

    def test_an_all_claude_run_does_not(self):
        result = self.cli('claude')
        self.assertNotIn('unverified codex sandbox contract', result.stdout)

    def test_efficient_mode_is_unchanged(self):
        self.h.run_dir = self.h.root / 'efficient'
        result = self.cli('codex', strict=False)
        self.assertNotIn('unverified codex sandbox contract', result.stdout)

    def test_dispatched_vendors_name_the_gate(self):
        co = self.h.coordinator('--gate-vendor', 'codex', '--author-vendor', 'claude', '--reviewer-vendor', 'claude',
                                '--test-command', 'python3 -m unittest')
        self.assertEqual(co.dispatched_vendors(), ('claude', 'claude', 'codex'))


if __name__ == '__main__':
    unittest.main()
