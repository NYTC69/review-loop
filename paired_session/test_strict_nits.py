"""STRICT-NITS (BACKLOG triage 2026-10-06, strict paths): a replaced operator override is kept in <key>_history (P0-2 b).
P0-2 (d) was reverted after the final gate (it broke previously passing tests); a direct resume()/reject() call can still
change state before drive() refuses, and the CLI precheck refuses first (recorded residual)."""
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


if __name__ == '__main__':
    unittest.main()
