"""V325-CFG (gg-pipeline WI-266h, v3.2.4): a 598 s Claude gate turn held "claude turn changed global config: claude_settings"
because the owner's interactive Claude Code session added an "env" entry to ~/.claude/settings.json meanwhile; `resume` re-ran
the gate from scratch. Efficient mode now keeps a read-only Claude turn (reviewer, shadow, gate) whose only finding is that
change, records it and prints a NOTE; author turns, strict mode and any other finding still hold, with a hint that the change
may come from another process."""
import hashlib
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
NEW_SETTINGS = '{\n  "env": {"OWNER_FLAG": "1"}\n}\n'
GATE = 'You are an adversarial reviewer'
REVIEWER = 'whole-delta reviewer. Phase: EXEC.'
AUTHOR = 'implementer. Phase: EXEC.'
HOLD = 'claude turn changed global config: claude_settings'


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ConcurrentSettingsChangeTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        for module in {id(m): m for m in (rc, sys.modules.get('paired_session.coordinator')) if m}.values():
            pin = patch.object(module, 'DEFAULT_SAFETY_MODE', 'efficient')   # the product default, which the harness pins to strict
            pin.start()
            self.addCleanup(pin.stop)
        self.settings = self.h.test_home / '.claude' / 'settings.json'
        self.old_settings = self.settings.read_text()

    def launch(self, match, *extra, strict=False, env=None):
        source = self.h.root / f'settings-{self.h.run_dir.name}.json'
        source.write_text(NEW_SETTINGS)
        command = [a for a in self.h.command('--author-vendor', 'claude', '--reviewer-vendor', 'claude', '--gate-vendor', 'claude',
                                             '--shadow', 'off', *extra) if strict or a != '--strict']
        if strict: command.append('--skip-probe')
        result = subprocess.run(command, cwd=self.h.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={
            **os.environ, 'FAKE_CLAUDE_SETTINGS_WRITE': str(source), 'FAKE_CLAUDE_SETTINGS_MATCH': match, **(env or {})})
        state = json.loads((self.h.run_dir / 'state.json').read_text())
        self.assertEqual(self.settings.read_text(), NEW_SETTINGS)   # the change did happen during the run
        return result, state

    def turns(self, state, role):
        return [t for t in state['turns'] if t.get('role') == role and t.get('phase') == 'EXEC']

    def test_efficient_keeps_a_read_only_turn_and_records_the_change(self):
        result, state = self.launch(GATE, '--adversarial-gate', 'on')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state['status'], 'DONE')
        self.assertIn('NOTE: ~/.claude/settings.json changed during the gate turn; the read-only turn cannot write it, so the '
                      'turn is kept and the next turn starts from the new settings', result.stdout)
        [gate] = self.turns(state, 'gate')   # one gate turn: kept, not re-run
        self.assertNotIn('error', gate)
        self.assertEqual(gate['answer']['verdict'], 'approve')
        changes = gate['global_config_changes']
        self.assertEqual((changes['status'], changes['findings']), ('PASS', []))
        self.assertEqual(changes['concurrent_settings_change'], {'before': sha(self.old_settings), 'after': sha(NEW_SETTINGS)})

    def test_an_author_turn_still_holds_with_the_hint(self):
        result, state = self.launch(AUTHOR, '--adversarial-gate', 'off')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(state['hold_reason'], HOLD + rc.GLOBAL_CONFIG_HINT)
        self.assertIn('may come from another process on this machine', state['hold_reason'])
        self.assertNotIn('concurrent_settings_change', self.turns(state, 'author')[0]['global_config_changes'])

    def test_strict_mode_still_holds_a_gate_turn(self):
        result, state = self.launch(GATE, '--adversarial-gate', 'on', strict=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(state['hold_reason'], HOLD + rc.GLOBAL_CONFIG_HINT)
        self.assertNotIn('NOTE: ~/.claude/settings.json', result.stdout)

    def test_a_settings_change_with_another_finding_still_holds(self):
        queue = self.h.root / 'plugins-queue.json'   # a registry change that is not a normal plugin update, in the same turn
        queue.write_text(json.dumps([{'plugins': [], 'unexpected': True}]))
        result, state = self.launch(GATE, '--adversarial-gate', 'on',
                                    env={'FAKE_PLUGIN_UPDATES': str(queue), 'FAKE_PLUGIN_UPDATES_MATCH': GATE})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(state['hold_reason'], HOLD + ', claude_plugins' + rc.GLOBAL_CONFIG_HINT)
        self.assertNotIn('concurrent_settings_change', self.turns(state, 'gate')[0]['global_config_changes'])

    def test_the_next_turn_takes_a_fresh_baseline(self):
        result, state = self.launch(REVIEWER, '--adversarial-gate', 'off', '--exercise-revisions')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state['status'], 'DONE')
        first, second = self.turns(state, 'reviewer')[:2]
        self.assertEqual(first['global_config_changes']['concurrent_settings_change']['after'], sha(NEW_SETTINGS))
        self.assertEqual(second['global_claude_before']['claude_settings'], sha(NEW_SETTINGS))
        changes = second['global_config_changes']
        self.assertEqual((changes['status'], changes['findings']), ('PASS', []))
        self.assertNotIn('concurrent_settings_change', changes)


if __name__ == '__main__':
    unittest.main()
