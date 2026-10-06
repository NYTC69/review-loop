"""F7 (1C / R22-1, LOW): `resume --retry-uncertain` (and `permission-probe --retry-uncertain`) compared the global config when
it archived the stopped turn, then went on to dispatch; a change after that compare and before the next turn's baseline was
taken silently into that baseline. The hashes the recovery validated are now checked against the next turn's own baseline
(a change in between HOLDs before the child exists or the budget counts), and the Codex trust-entry check uses the same bytes
it hashed."""
import hashlib
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc   # the module the harness's Coordinator comes from (patches must land there)


class RecoveryToctouTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)

    def config(self, relative):
        path = self.h.test_home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists(): path.write_text('{}\n')
        self.addCleanup(path.write_bytes, path.read_bytes())
        return path

    def uncertain(self, vendor, key, path, phase='EXEC', *extra):
        co = self.h.coordinator('--skip-probe', *extra)
        receipt = {'sequence': 1, 'pid': 987654321, 'role': 'author', 'vendor': vendor, 'phase': phase,
                   'workspace': str(self.h.workspace), 'global_codex_home': str(co.global_codex_home),
                   'global_config_home': str(co.global_config_home),
                   f'global_{vendor}_before': {key: hashlib.sha256(path.read_bytes()).hexdigest()}}
        co.state.update(status='HOLD', hold_reason='uncertain CLI turn', uncertain_active=receipt)
        co.save(); co.args.action = 'resume'
        return co

    def test_a_change_up_to_the_next_baseline_holds_before_the_child_or_the_budget(self):
        for vendor, key, relative in (('codex', 'codex_config', '.codex/config.toml'),
                                      ('claude', 'claude_settings', '.claude/settings.json')):
            with self.subTest(vendor=vendor):
                self.h.run_dir = self.h.root / ('f7-' + vendor)
                path = self.config(relative)
                original = path.read_bytes()
                co = self.uncertain(vendor, key, path)
                used, real_env = co.state['invocations_used'], rc.cli_env
                def env_then_change():   # the last step before invoke() takes the next turn's baseline
                    path.write_bytes(original + b'\n# changed\n')
                    return real_env()
                with patch('os.killpg', side_effect=ProcessLookupError), patch.object(rc, 'cli_env', env_then_change):
                    self.assertEqual(co.resume(retry_uncertain=True), 'HOLD')
                self.assertIn(f'global config changed during uncertain-turn recovery ({key})', co.state['hold_reason'])
                self.assertEqual(co.state['invocations_used'], used)
                self.assertEqual(list(co.evidence.glob('*.stdout.jsonl')), [])   # no child started
                self.assertNotIn('recovery_config_hashes', co.state)
                self.assertEqual([row['sequence'] for row in co.state['abandoned_turns']], [1])
                with patch('os.killpg', side_effect=ProcessLookupError), \
                        patch.object(co, 'drive', return_value='DONE'):   # reported once: a later resume takes a fresh baseline
                    self.assertEqual(co.resume(), 'DONE')
                path.write_bytes(original)

    def test_no_change_recovers_and_the_next_baseline_clears_the_hashes(self):
        path = self.h.test_home / '.codex/config.toml'
        co = self.uncertain('codex', 'codex_config', path)
        with patch('os.killpg', side_effect=ProcessLookupError), patch.object(co, 'drive', return_value='DONE') as drive:
            self.assertEqual(co.resume(retry_uncertain=True), 'DONE')
        drive.assert_called_once()
        self.assertIsNone(co.state['uncertain_active'])
        self.assertEqual(co.state['recovery_config_hashes'], {'codex_config': hashlib.sha256(path.read_bytes()).hexdigest()})
        self.assertIsNone(co._recovery_config_issue(rc.global_config_snapshot(co.global_config_home, co.global_codex_home)))
        self.assertNotIn('recovery_config_hashes', co.state)

    def test_permission_probe_retry_holds_on_a_change_before_its_baseline(self):
        path = self.config('.codex/config.toml')
        original = path.read_bytes()
        co = self.uncertain('codex', 'codex_config', path, 'AUTHOR_PERMISSION_PROBE')
        archive = co.archive_abandoned_turn
        def archive_then_change(turn):
            verified = archive(turn)
            path.write_bytes(original + b'\n# changed\n')   # after the archive compare, before the probe's baseline
            return verified
        with patch('os.killpg', side_effect=ProcessLookupError), \
                patch.object(co, 'archive_abandoned_turn', archive_then_change), \
                patch.object(co, 'invoke', side_effect=AssertionError('no probe turn may start')) as invoke:
            self.assertFalse(co.permission_probe(retry_uncertain=True))
        invoke.assert_not_called()
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('global config changed during uncertain-turn recovery (codex_config)', co.state['hold_reason'])
        self.assertEqual([row['sequence'] for row in co.state['abandoned_turns']], [1])
        self.assertIsNone(co.state['uncertain_active'])
        self.assertNotIn('recovery_config_hashes', co.state)

    def test_a_failure_before_the_baseline_keeps_the_hashes_for_the_next_dispatch(self):
        path = self.config('.codex/config.toml')
        original = path.read_bytes()
        co = self.uncertain('codex', 'codex_config', path)
        limit, co.args.max_invocations = co.args.max_invocations, co.state['invocations_used']
        with patch('os.killpg', side_effect=ProcessLookupError):
            self.assertEqual(co.resume(retry_uncertain=True), 'HOLD')   # invoke() stops before its baseline
        self.assertIn('invocation limit reached', co.state['hold_reason'])
        self.assertIn('codex_config', co.state['recovery_config_hashes'])   # never compared, so still pending
        path.write_bytes(original + b'\n# changed\n')
        co.args.max_invocations = limit
        self.assertEqual(co.resume(), 'HOLD')
        self.assertIn('global config changed during uncertain-turn recovery (codex_config)', co.state['hold_reason'])
        self.assertEqual(list(co.evidence.glob('*.stdout.jsonl')), [])
        self.assertNotIn('recovery_config_hashes', co.state)

    def test_permission_probe_retry_holds_on_a_change_inside_the_probe_dispatch(self):
        path = self.config('.codex/config.toml')
        original = path.read_bytes()
        co = self.uncertain('codex', 'codex_config', path, 'AUTHOR_PERMISSION_PROBE')
        real_env, used = rc.cli_env, co.state['invocations_used']
        def env_then_change():   # after the probe's outer check, right before invoke()'s own baseline
            path.write_bytes(original + b'\n# changed\n')
            return real_env()
        outer = co._recovery_config_issue
        def outer_check(snapshot, consume=True):
            issue = outer(snapshot, consume)
            if not consume: checks.append(issue)   # the probe's outer peek, as opposed to invoke()'s baseline
            return issue
        checks = []
        with patch('os.killpg', side_effect=ProcessLookupError), patch.object(rc, 'cli_env', env_then_change), \
                patch.object(co, '_recovery_config_issue', outer_check):
            self.assertFalse(co.permission_probe(retry_uncertain=True))
        self.assertEqual(checks, [None])   # the outer check passed; only invoke()'s own baseline saw the change
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('global config changed during uncertain-turn recovery (codex_config)',
                      (co.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(co.state['invocations_used'], used)
        self.assertEqual(list(co.evidence.glob('*.stdout.jsonl')), [])   # no probe child started
        self.assertNotIn('recovery_config_hashes', co.state)

    def test_the_trust_check_uses_the_bytes_the_caller_hashed(self):
        path = self.h.test_home / '.codex/config.toml'
        before = path.read_bytes()
        entry = ('\n[projects."' + str(self.h.workspace) + '"]\ntrust_level = "trusted"\n').encode()
        digest = hashlib.sha256(before).hexdigest()
        self.assertTrue(rc.trust_entry_only_since_hash(path, digest, self.h.workspace, before + entry))
        path.write_bytes(before + entry + b'model = "other"\n')   # the file changed after the caller read it
        self.addCleanup(path.write_bytes, before)
        self.assertTrue(rc.trust_entry_only_since_hash(path, digest, self.h.workspace, before + entry))   # judged on the hashed bytes
        self.assertFalse(rc.trust_entry_only_since_hash(path, digest, self.h.workspace))


if __name__ == '__main__':
    unittest.main()
