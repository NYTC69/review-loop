"""FIELD-21 (poker-news-bot WI-102, v2.10.0, 2026-10-05): a Claude author EXEC turn held "claude turn changed global config:
claude_plugins" because a NEW Claude Code session outside the run materialized review-loop 2.10.1 and rewrote that plugin's entry
in installed_plugins.json (version, installPath, gitCommitSha and lastUpdated together). RF-4 recognized only version/lastUpdated.
A normal plugin update is now recognized precisely (normal_plugin_update); efficient mode voids such a turn and re-dispatches it
once on a fresh global-config baseline, strict mode holds with the hint; anything else stays a hard finding."""
import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
KEY = 'review-loop@review-loop-marketplace'


class Field21Tests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        for module in {id(m): m for m in (rc, sys.modules.get('paired_session.coordinator')) if m}.values():
            pin = patch.object(module, 'DEFAULT_SAFETY_MODE', 'efficient')   # the product default, which the harness pins to strict
            pin.start()
            self.addCleanup(pin.stop)
        self.plugins = self.h.test_home / '.claude' / 'plugins'
        for version in ('2.10.0', '2.10.1', '2.10.2'):
            self.install_dir(version).mkdir(parents=True)
        self.base = {'version': 2, 'plugins': {KEY: [self.entry('2.10.0', 'c8336e7', 't0')],
                                               'other@market': [{'scope': 'user', 'version': '1.0', 'lastUpdated': 't0'}]}}
        (self.plugins / 'installed_plugins.json').write_text(json.dumps(self.base, indent=2))

    def install_dir(self, version, plugin='review-loop', market='review-loop-marketplace'):
        return self.plugins / 'cache' / market / plugin / version

    def entry(self, version, sha, updated, install=None):
        return {'scope': 'user', 'installPath': str(install or self.install_dir(version)), 'version': version,
                'installedAt': '2026-09-15T11:03:19.683Z', 'lastUpdated': updated, 'gitCommitSha': sha}

    def updated(self, version='2.10.1', **changes):   # the document after an update of KEY, with optional edits to its entry
        doc = copy.deepcopy(self.base)
        doc['plugins'][KEY][0] = {**self.entry(version, '5b9b8bb', 't1'), **changes}
        return doc

    def snap(self, doc):
        return {'path': str(self.plugins / 'installed_plugins.json'), 'document': doc, 'error': None}

    def recognized(self, doc, before=None):
        return rc.normal_plugin_update(self.snap(before or self.base), self.snap(doc))

    # --- recognition --------------------------------------------------------------------------------------------------------------
    def test_a_normal_update_is_recognized_precisely(self):
        update = self.recognized(self.updated())
        self.assertEqual(update, [{'plugin': KEY, 'entry': 0, 'fields': ['gitCommitSha', 'installPath', 'lastUpdated', 'version'],
                                   'version': ['2.10.0', '2.10.1']}])
        self.assertTrue(rc.plugin_version_bump_only([{'file': 'claude_plugins'}], {'claude_plugins': self.snap(self.base)},
                                                    {'claude_plugins': self.snap(self.updated())}))
        version_only = copy.deepcopy(self.base)
        version_only['plugins'][KEY][0].update(version='2.10.1', lastUpdated='t1')   # RF-4's bump, installPath unchanged
        self.assertIsNotNone(self.recognized(version_only))

    def test_anything_else_is_not_a_normal_update(self):
        elsewhere = self.h.root / 'elsewhere' / '2.10.1'
        elsewhere.mkdir(parents=True)
        link = self.install_dir('2.10.9')
        link.symlink_to(elsewhere)
        (self.h.root / 'outside' / 'review-loop' / '2.10.1').mkdir(parents=True)   # a marketplace level that links out of the cache
        (self.plugins / 'cache' / 'linked-market').symlink_to(self.h.root / 'outside')
        linked = {'version': 2, 'plugins': {'review-loop@linked-market': [self.entry('2.10.0', 'c8336e7', 't0')]}}
        moved = copy.deepcopy(linked)
        moved['plugins']['review-loop@linked-market'][0] = self.entry(
            '2.10.1', '5b9b8bb', 't1', install=self.install_dir('2.10.1', market='linked-market'))
        self.assertTrue(self.install_dir('2.10.1', market='linked-market').is_dir())
        self.assertIsNone(self.recognized(moved, before=linked))
        extra = self.updated(); extra['plugins'][KEY][0]['scope'] = 'project'
        added_entry = self.updated(); added_entry['plugins'][KEY].append(self.entry('2.10.1', '5b9b8bb', 't1'))
        added_plugin = self.updated(); added_plugin['plugins']['new@market'] = [{'version': '1'}]
        new_key = self.updated(); new_key['plugins'][KEY][0]['hooks'] = 'x'
        top = self.updated(); top['version'] = 3
        nested = self.updated(gitCommitSha={'sha': 'x'})
        cases = {'installPath elsewhere': self.updated(installPath=str(elsewhere)),
                 'installPath not the new version': self.updated(installPath=str(self.install_dir('2.10.2'))),
                 'installPath of another plugin': self.updated(installPath=str(self.install_dir('2.10.1', plugin='compass'))),
                 'install directory missing': self.updated(version='2.10.5'),
                 'install directory is a link': self.updated(version='2.10.9'),
                 'version with a slash': self.updated(version='2.10.1/x', installPath=str(self.install_dir('2.10.1') / 'x')),
                 'another field changed': extra, 'entry added': added_entry, 'plugin added': added_plugin,
                 'key added': new_key, 'top-level change': top, 'nested value': nested, 'no change': self.base}
        for label, doc in cases.items():
            with self.subTest(label):
                self.assertIsNone(self.recognized(doc))
        self.assertIsNone(rc.normal_plugin_update({**self.snap(self.base), 'error': 'ValueError'}, self.snap(self.updated())))

    # --- in a run -----------------------------------------------------------------------------------------------------------------
    def queue(self, docs):
        queue = self.h.root / f'updates-{self.h.run_dir.name}.json'
        queue.write_text(json.dumps(list(docs)))
        return queue

    def launch(self, *docs, strict=False, extra=(), env=None, role='author'):
        command = [a for a in self.h.command('--author-vendor', 'claude', '--shadow', 'off', '--adversarial-gate', 'off', *extra)
                   if strict or a != '--strict']
        if strict: command.append('--skip-probe')
        result = subprocess.run(command, cwd=self.h.root, env={**os.environ, 'FAKE_PLUGIN_UPDATES': str(self.queue(docs)), **(env or {})},
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        state = json.loads((self.h.run_dir / 'state.json').read_text())
        turns = [t for t in state['turns'] if t.get('role') == role and t.get('phase') == 'EXEC']
        return result, state, turns

    def test_efficient_voids_and_re_dispatches_a_turn_hit_by_a_normal_update_once(self):
        result, state, authors = self.launch(self.updated())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state['status'], 'DONE')
        self.assertIn('re-dispatching the author turn once on a fresh global-config baseline', result.stdout)
        void, again = authors[0], authors[1]
        self.assertIn('a normal plugin update outside the run: ' + KEY + ' 2.10.0 -> 2.10.1', void['error'])
        self.assertEqual(void['global_config_changes']['plugin_update'][0]['version'], ['2.10.0', '2.10.1'])
        self.assertNotIn('error', again)
        self.assertIn('recognized as a normal plugin update',
                      (self.h.run_dir / 'evidence' / f"{again['sequence']:03d}-exec-author.prompt.txt").read_text())

    def test_another_check_of_the_same_turn_still_leads(self):
        co = self.h.coordinator('--author-vendor', 'claude')   # the model identity check comes after the global-config check
        with patch.dict(os.environ, {'FAKE_PLUGIN_UPDATES': str(self.queue([self.updated()])), 'FAKE_CLAUDE_MODEL': 'claude-other-9'}):
            with self.assertRaisesRegex(RuntimeError, 'model identity mismatch') as raised:
                co._invoke_once('author', 'EXEC', 'Role: persistent claude implementer. Phase: EXEC.', {})
        self.assertNotIsInstance(raised.exception.__cause__, rc.PluginUpdateTurnVoided)
        self.assertEqual(co.state['turns'][-1]['global_config_changes']['plugin_update'][0]['plugin'], KEY)
        self.h.run_dir = self.h.root / 'approval'   # an EXEC approval without the observed test, in a reviewer turn hit by an update
        (self.plugins / 'installed_plugins.json').write_text(json.dumps(self.base, indent=2))
        result, state, reviews = self.launch(self.updated(), extra=('--reviewer-vendor', 'claude'), role='reviewer', env={
            'FAKE_REVIEW_NO_TEST_EVENT': '1', 'FAKE_PLUGIN_UPDATES_MATCH': 'whole-delta reviewer. Phase: EXEC.'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('observed successful configured test command', state['hold_reason'])
        self.assertNotIn('re-dispatching', result.stdout)
        self.assertEqual(reviews[0]['global_config_changes']['plugin_update'][0]['plugin'], KEY)

    def test_a_second_update_in_the_same_slot_holds_with_the_hint(self):
        result, state, authors = self.launch(self.updated(), self.updated('2.10.2', gitCommitSha='next', lastUpdated='t2'))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('again after one re-dispatch', state['hold_reason'])
        self.assertIn(rc.PLUGIN_UPDATE_HINT, state['hold_reason'])
        self.assertEqual(len(authors), 2)

    def test_strict_holds_with_the_hint_and_records_the_update(self):
        result, state, authors = self.launch(self.updated(), strict=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(state['hold_reason'], 'claude turn changed global config: claude_plugins' + rc.PLUGIN_UPDATE_HINT)
        [turn] = authors
        self.assertEqual(turn['global_config_changes']['plugin_update'][0]['plugin'], KEY)

    def test_an_abnormal_change_stays_a_hard_finding_in_efficient_mode(self):
        extra = self.updated(); extra['plugins'][KEY][0]['scope'] = 'project'
        added = self.updated(); added['plugins'][KEY].append(self.entry('2.10.1', '5b9b8bb', 't1'))
        for label, doc in (('elsewhere', self.updated(installPath=str(self.h.root))), ('extra', extra), ('added', added)):
            with self.subTest(label):
                self.h.run_dir = self.h.root / ('abnormal-' + label)
                (self.plugins / 'installed_plugins.json').write_text(json.dumps(self.base, indent=2))
                result, state, authors = self.launch(doc)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(state['hold_reason'], 'claude turn changed global config: claude_plugins')   # no hint, no re-run
                self.assertEqual(len(authors), 1)
                self.assertNotIn('plugin_update', authors[0]['global_config_changes'])


if __name__ == '__main__':
    unittest.main()
