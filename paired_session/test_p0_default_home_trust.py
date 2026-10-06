"""P0 (owner 2026-10-06): with an isolated CODEX_HOME, another process's trust entries in the DEFAULT ~/.codex/config.toml
never void a turn; any other default-config change, an own-path entry there, and every change to the isolated config
still fail. A run on the default home is unchanged. No test touches the real ~/.codex: HOME is a temp dir."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from paired_session import test_real_coordinator as trc

rc = trc.rc
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
           'fake_codex_cli', 'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action')
BASE = 'model = "gpt-6.1-sol"\n\n[projects."/other/a"]\ntrust_level = "trusted"\n'


def trust(path):
    return '\n[projects.' + json.dumps(path) + ']\ntrust_level = "trusted"\n'


class DefaultHomeAttributionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.home, self.iso, self.ws = root / 'home', root / 'iso-codex', root / 'ws'
        for path in (self.home / '.codex', self.iso, self.ws):
            path.mkdir(parents=True)
        self.default = self.home / '.codex' / 'config.toml'
        self.default.write_text(BASE)
        (self.iso / 'config.toml').write_text('model = "gpt-6.1-sol"\nsandbox_mode = "read-only"\n')

    def check(self, edit, codex_home=None):
        codex_home = codex_home or self.iso
        before = rc.global_config_snapshot(self.home, codex_home)
        edit()
        after = rc.global_config_snapshot(self.home, codex_home)
        changes = rc.attribute_global_config_changes(before, after, [self.ws])
        return rc.reclassify_default_home_trust(changes, before, after, [self.ws])

    def test_foreign_trust_add_and_removal_are_expected(self):
        for name, edit, delta in (
                ('add', lambda: self.default.write_text(BASE + trust('/other/b')), {'added': ['/other/b'], 'removed': []}),
                ('restore', lambda: self.default.write_text('model = "gpt-6.1-sol"\n'), {'added': [], 'removed': ['/other/a']}),
                ('both', lambda: self.default.write_text('model = "gpt-6.1-sol"\n' + trust('/other/c')),
                 {'added': ['/other/c'], 'removed': ['/other/a']})):
            with self.subTest(name):
                self.default.write_text(BASE)
                changes = self.check(edit)
                self.assertEqual((changes['status'], changes['findings']), ('PASS', []))
                self.assertIn({'file': 'codex_default_config', 'change': 'foreign-default-home-trust-entry', **delta},
                              changes['expected_changes'])

    def test_a_restore_that_removes_many_foreign_tables_is_expected(self):
        many = ''.join(trust(f'/other/w{index}') for index in range(8))   # more than the old cap of 6
        self.default.write_text(BASE + many)
        changes = self.check(lambda: self.default.write_text(BASE))
        self.assertEqual(changes['status'], 'PASS', changes['findings'])
        [row] = [row for row in changes['expected_changes'] if row['change'] == 'foreign-default-home-trust-entry']
        self.assertEqual(row['removed'], sorted(f'/other/w{index}' for index in range(8)))

    def test_headers_inside_a_multiline_string_are_not_tables(self):
        text = BASE + "notes = '''\n[projects.\"\\U00000061\"]\ntrust_level = \"trusted\"\n'''\n"   # TOML text, not JSON-decodable
        self.default.write_text(text)
        changes = self.check(lambda: self.default.write_text(text + trust('/other/b')))
        self.assertEqual(changes['status'], 'PASS', changes['findings'])
        self.default.write_text(BASE)   # a real top-level header that does not decode stays a finding (conservative)
        changes = self.check(lambda: self.default.write_text(BASE + '\n[projects."\\U00000061"]\ntrust_level = "trusted"\n'))
        self.assertEqual(changes['status'], 'FAIL')

    def test_thousands_of_existing_tables_are_checked_in_linear_time(self):
        import time
        many = BASE + ''.join(trust(f'/old/w{index}') for index in range(3000))
        self.default.write_text(many)
        began = time.monotonic()
        changes = self.check(lambda: self.default.write_text(many + trust('/other/new')))
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(changes['status'], 'PASS', changes['findings'])

    def test_own_path_spellings_still_fail(self):
        link = self.ws.parent / 'ws-link'
        link.symlink_to(self.ws)
        spellings = [str(link), str(self.ws) + '/']
        if os.uname().sysname == 'Darwin':
            spellings.append(str(self.ws.resolve()).upper())
        for spelling in spellings:
            with self.subTest(spelling):
                self.default.write_text(BASE)
                changes = self.check(lambda: self.default.write_text(BASE + trust(spelling)))
                self.assertEqual(changes['status'], 'FAIL')

    def test_an_undecodable_file_fails_closed(self):
        self.default.write_bytes(b'# \xff\n' + BASE.encode())
        changes = self.check(lambda: self.default.write_bytes(b'# \xff\n' + (BASE + trust('/other/b')).encode()))
        self.assertEqual(changes['status'], 'FAIL')

    def test_other_default_config_changes_still_fail(self):
        own = str(self.ws.resolve())
        for name, text in (('own path trust', BASE + trust(own)),                       # the run's own Codex escaped
                           ('model change', BASE.replace('gpt-6.1-sol', 'other')),       # decision (a)
                           ('trust plus a model change', BASE.replace('gpt-6.1-sol', 'other') + trust('/other/b')),
                           ('trust table with another key', BASE + trust('/other/b') + 'approval = "never"\n'),
                           ('untrusted level', BASE + '\n[projects."/other/b"]\ntrust_level = "untrusted"\n')):
            with self.subTest(name):
                self.default.write_text(BASE)
                changes = self.check(lambda: self.default.write_text(text))
                self.assertEqual(changes['status'], 'FAIL')
                self.assertEqual(changes['findings'], [{'file': 'codex_default_config', 'reason': 'unexpected-content-change'}])

    def test_the_isolated_config_keeps_its_check(self):
        def edit():
            (self.iso / 'config.toml').write_text('model = "gpt-6.1-sol"\nsandbox_mode = "danger-full-access"\n')
            self.default.write_text(BASE + trust('/other/b'))   # a foreign default-home entry at the same time
        changes = self.check(edit)
        self.assertEqual(changes['findings'], [{'file': 'codex_config', 'reason': 'unexpected-content-change'}])
        self.assertEqual(changes['status'], 'FAIL')

    def test_a_default_home_run_is_unchanged(self):
        codex = self.home / '.codex'
        before = rc.global_config_snapshot(self.home, codex)
        self.assertNotIn('codex_default_config', before)
        self.default.write_text(BASE + trust('/other/b'))
        after = rc.global_config_snapshot(self.home, codex)
        plain = rc.attribute_global_config_changes(before, after, [self.ws])
        self.assertEqual(rc.reclassify_default_home_trust(json.loads(json.dumps(plain)), before, after, [self.ws]), plain)


class DefaultHomeTurnTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def run_with_foreign_default(self, text):
        (self.test_home / '.codex' / 'config.toml').write_text(BASE)   # the harness HOME's default config
        iso = self.root / 'iso-codex'   # this run's own Codex home, not the default
        iso.mkdir()
        (iso / 'config.toml').write_text('model = "gpt-6-luna"\n')
        foreign = self.root / 'foreign.toml'
        foreign.write_text(text)
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      env={'CODEX_HOME': str(iso), 'FAKE_FOREIGN_DEFAULT_CONFIG': str(foreign)})
        self.assertTrue((self.root / 'foreign.toml.done').exists(), result.stdout + result.stderr)   # the edit happened in a turn
        return result, json.loads((self.run_dir / 'state.json').read_text())

    def test_a_foreign_trust_entry_during_an_author_turn_does_not_void_it(self):
        result, state = self.run_with_foreign_default(BASE + trust('/Users/someone/paired-runs/ws15'))
        self.assertNotIn('changed global config', result.stdout + str(state.get('hold_reason')))
        recorded = [row for turn in state['turns'] for row in (turn.get('global_config_changes') or {}).get('expected_changes', [])
                    if row['change'] == 'foreign-default-home-trust-entry']
        self.assertEqual(recorded, [{'file': 'codex_default_config', 'change': 'foreign-default-home-trust-entry',
                                     'added': ['/Users/someone/paired-runs/ws15'], 'removed': []}])

    def test_a_foreign_trust_entry_during_the_permission_probe_passes_it(self):
        import subprocess
        (self.test_home / '.codex' / 'config.toml').write_text(BASE)
        iso = self.root / 'iso-codex'
        iso.mkdir()
        (iso / 'config.toml').write_text('model = "gpt-6-luna"\n')
        foreign = self.root / 'foreign.toml'
        foreign.write_text(BASE + trust('/Users/someone/paired-runs/ws15'))
        command = self.command()
        command[2] = 'permission-probe'
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, env={
            **os.environ, 'CODEX_HOME': str(iso), 'FAKE_FOREIGN_DEFAULT_CONFIG': str(foreign),
            'FAKE_FOREIGN_DEFAULT_CONFIG_ON': 'probe'})
        self.assertTrue((self.root / 'foreign.toml.done').exists(), result.stdout + result.stderr)   # written during the probe
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        changes = report['global_config_changes']
        self.assertEqual((changes['status'], changes['findings']), ('PASS', []), result.stdout)
        self.assertIn({'file': 'codex_default_config', 'change': 'foreign-default-home-trust-entry',
                       'added': ['/Users/someone/paired-runs/ws15'], 'removed': []}, changes['expected_changes'])
        self.assertNotIn('unexpected-global-config-change', report.get('failure_reasons', []))

    def test_a_foreign_non_trust_change_still_holds(self):   # decision (a)
        result, state = self.run_with_foreign_default(BASE.replace('gpt-6.1-sol', 'other-model'))
        self.assertEqual(state['status'], 'HOLD', result.stdout + result.stderr)
        self.assertIn('codex turn changed global config: codex_default_config', state['hold_reason'])


if __name__ == '__main__':
    unittest.main()
