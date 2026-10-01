import importlib.util
import dataclasses
import errno
import hashlib
import io
import json
import os
import signal
import shlex
import socket
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from paired_session import candidate_tree as ct
from paired_session import delivery_journal as dj
from paired_session import delivery_publish as dp
from paired_session import delivery_recovery_state as drs
from paired_session import delivery_recovery_lock as drl
from paired_session import delivery_recover as dr
from paired_session import delivery_close_proof as dcp
from paired_session.docs_policy import validate_candidate_docs_change


MODULE_PATH = Path(__file__).with_name('coordinator.py')
SPEC = importlib.util.spec_from_file_location('real_coordinator', MODULE_PATH)
rc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rc)
FAKE = Path(__file__).with_name('fake_cli.py')


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def allowed_tool_values(command):
    return [command[index + 1] for index, value in enumerate(command[:-1])
            if value == '--allowedTools']


class RealCoordinatorTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).parent / 'test-tmp'
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'config', 'user.email', 'fake@example.test'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Fake'], cwd=self.workspace, check=True)
        (self.workspace / 'tracked.txt').write_text('base\n')
        (self.workspace / '.gitignore').write_text('__pycache__/\n*.cache\n')
        subprocess.run(['git', 'add', 'tracked.txt', '.gitignore'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'base'], cwd=self.workspace, check=True)
        self.workitem = self.root / 'WORKITEM.md'
        self.workitem.write_text('# Toy\nCreate sum_ints; reject booleans.\n')
        self.run_dir = self.root / 'run'
        self.test_home = self.root / 'home'
        (self.test_home / '.codex').mkdir(parents=True)
        (self.test_home / '.claude' / 'plugins').mkdir(parents=True)
        (self.test_home / '.codex' / 'config.toml').write_text('model = "gpt-6-luna"\n')
        (self.test_home / '.claude' / 'settings.json').write_text('{}\n')
        (self.test_home / '.claude' / 'plugins' / 'installed_plugins.json').write_text(
            json.dumps({'plugins': []}))
        self.original_home = os.environ.get('HOME')
        os.environ['HOME'] = str(self.test_home)
        self._fake_codex_env = patch.dict(os.environ, {
            'CODEX_HOME': str(self.test_home / '.codex'), 'FAKE_CODEX_TEST_ROOT': str(self.root)})
        self._fake_codex_env.start()
        self.addCleanup(self._fake_codex_env.stop)
        self._unpatched_popen = subprocess.Popen
        self._real_provider_paths = {str(Path(path).resolve()) for name in ('claude', 'codex')
                                     if (path := shutil.which(name))}
        self._provider_guard_patcher = patch('subprocess.Popen', new=self._guarded_test_popen)
        self._provider_guard_patcher.start()
        self.addCleanup(self._provider_guard_patcher.stop)

    def _assert_no_real_provider_cli(self, command):
        if isinstance(command, (str, bytes)):
            argv = shlex.split(os.fsdecode(command))
        else:
            argv = list(command)
        if not argv:
            return
        tokens = [os.fsdecode(value) for value in argv if isinstance(value, (str, bytes, os.PathLike))]
        candidates = [tokens[0]]
        if Path(tokens[0]).name in ('sh', 'bash', 'zsh'):
            for index, value in enumerate(tokens[:-1]):
                if value in ('-c', '-lc'):
                    for segment in tokens[index + 1].replace('&&', ';').replace('||', ';').replace('|', ';').split(';'):
                        words = shlex.split(segment)
                        while words and '=' in words[0] and not words[0].startswith('-'):
                            words.pop(0)
                        if words:
                            candidates.append(words[0])
                    break
        elif Path(tokens[0]).name == 'env':
            for value in tokens[1:]:
                if value.startswith('-') or ('=' in value and not Path(value).exists()):
                    continue
                candidates.append(value)
                break
        for executable in candidates:
            path = Path(executable)
            if path.name in ('claude', 'codex') or str(path.resolve()) in self._real_provider_paths:
                raise AssertionError('test attempted to execute a real claude/codex binary: ' + executable)

    def _guarded_test_popen(self, command, *args, **kwargs):
        self._assert_no_real_provider_cli(command)
        return self._unpatched_popen(command, *args, **kwargs)

    def test_frozen_role_manifest_hashes_config_agent_and_gate_inputs(self):
        agents = self.root / 'agents'
        agents.mkdir()
        body = agents / 'reviewer.md'
        body.write_text('read-only reviewer body\n')
        gate = self.root / 'gate.md'
        gate.write_text('gate rubric\n')
        roles = {'reviewer': {'vendor': 'claude', 'model': 'claude-opus-5-5',
                              'sandbox': {'read_only': True}}}
        required = {'reviewer': 'reviewer.md'}
        first = rc.frozen_role_manifest({'author_model': 'gpt-6-luna'}, roles, agents, gate, required)
        again = rc.frozen_role_manifest({'author_model': 'gpt-6-luna'}, roles, agents, gate, required)
        self.assertEqual(first, again)
        self.assertEqual(first['agent_body_sha256']['reviewer.md'],
                         hashlib.sha256(body.read_bytes()).hexdigest())
        body.write_text('changed reviewer body\n')
        changed = rc.frozen_role_manifest({'author_model': 'gpt-6-luna'}, roles, agents, gate, required)
        self.assertNotEqual(first['agent_body_sha256'], changed['agent_body_sha256'])
        self.assertEqual(first['config_sha256'], changed['config_sha256'])
        roles['reviewer']['sandbox']['read_only'] = False
        different_flags = rc.frozen_role_manifest({'author_model': 'gpt-6-luna'}, roles, agents,
                                                  gate, required)
        self.assertNotEqual(first['role_flags_sha256'], different_flags['role_flags_sha256'])
        different_config = rc.frozen_role_manifest({'author_model': 'gpt-6-sol'}, roles, agents,
                                                   gate, required)
        self.assertNotEqual(first['config_sha256'], different_config['config_sha256'])
        gate.write_text('changed gate rubric\n')
        different_gate = rc.frozen_role_manifest({'author_model': 'gpt-6-luna'}, roles, agents,
                                                 gate, required)
        self.assertNotEqual(first['gate_prompt_sha256'], different_gate['gate_prompt_sha256'])

    def test_frozen_role_manifest_requires_each_regular_role_body(self):
        agents = self.root / 'agents'
        agents.mkdir()
        (agents / 'reviewer.md').write_text('reviewer\n')
        gate = self.root / 'gate.md'
        gate.write_text('gate\n')
        flags = {'reviewer': {'model': 'claude-opus-5-5'}}
        with self.assertRaisesRegex(ValueError, 'missing'):
            rc.frozen_role_manifest({}, flags, agents, gate, {'reviewer': 'executor.md'})
        (agents / 'linked.md').symlink_to(agents / 'reviewer.md')
        with self.assertRaisesRegex(ValueError, 'links'):
            rc.frozen_role_manifest({}, flags, agents, gate, {'reviewer': 'reviewer.md'})
        (agents / 'linked.md').unlink()
        (agents / 'directory.md').mkdir()
        with self.assertRaisesRegex(ValueError, 'regular file'):
            rc.frozen_role_manifest({}, flags, agents, gate, {'reviewer': 'reviewer.md'})
        (agents / 'directory.md').rmdir()
        gate_link = self.root / 'gate-link.md'
        gate_link.symlink_to(gate)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            rc.frozen_role_manifest({}, flags, agents, gate_link, {'reviewer': 'reviewer.md'})

    def test_new_run_freezes_role_manifest_and_turn_receipt(self):
        co = self.coordinator()
        manifest = co.state['role_dispatch_manifest']
        digest = co.state['role_dispatch_manifest_sha256']
        self.assertEqual(manifest['role_flags']['author']['model'], co.args.author_model)
        self.assertFalse(manifest['role_flags']['author']['tmp_isolated'])
        co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', rc.author_schema())
        self.assertEqual(co.state['turns'][-1]['role_identity_sha256'], digest)

    def test_lifecycle_role_manifest_drift_and_shared_tmp_fail_closed(self):
        co = self.coordinator()
        co.state['role_dispatch_manifest']['role_flags']['author']['model'] = 'gpt-6-sol'
        with self.assertRaisesRegex(RuntimeError, 'frozen role dispatch changed'):
            co._verify_frozen_role_dispatch()
        co = self.coordinator()
        co.state['config']['lifecycle_mode'] = 'on'
        co.state['role_dispatch_manifest'] = co._role_dispatch_manifest()
        co.state['role_dispatch_manifest_sha256'] = hashlib.sha256(
            json.dumps(co.state['role_dispatch_manifest'], sort_keys=True,
                       separators=(',', ':')).encode()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, 'author TMP is not isolated'):
            co._verify_frozen_role_dispatch()

    def test_global_hash_attribution_accepts_only_trust_and_last_updated_autochanges(self):
        home = self.root / 'global-state'
        (home / '.codex').mkdir(parents=True)
        (home / '.claude' / 'plugins').mkdir(parents=True)
        config = home / '.codex' / 'config.toml'
        settings = home / '.claude' / 'settings.json'
        plugins = home / '.claude' / 'plugins' / 'installed_plugins.json'
        config.write_text('model = "gpt-6-luna"\n')
        settings.write_text('{"theme":"dark"}\n')
        plugins.write_text(json.dumps({'plugins': [{'name': 'compass', 'version': '0.6.1',
                                                    'lastUpdated': 'before'}]}))
        before = rc.global_config_snapshot(home)
        workspace = self.workspace.resolve()
        config.write_text(config.read_text() + f'\n[projects.{json.dumps(str(workspace))}]\n'
                          'trust_level = "trusted"\n')
        plugins.write_text(json.dumps({'plugins': [{'name': 'compass', 'version': '0.6.1',
                                                    'lastUpdated': 'after'}]}))
        after = rc.global_config_snapshot(home)

        result = rc.attribute_global_config_changes(before, after, [workspace])

        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual({item['change'] for item in result['expected_changes']},
                         {'trusted-probe-workspace-entry', 'plugin-lastUpdated'})
        self.assertEqual(result['findings'], [])
        self.assertIn('global config mutated by codex CLI trust persistence', result['warnings'])

    def test_codex_trust_entry_inserted_between_tables_warns_and_other_edits_fail(self):
        home = self.root / 'global-state-middle-trust'
        (home / '.codex').mkdir(parents=True)
        (home / '.claude' / 'plugins').mkdir(parents=True)
        config = home / '.codex' / 'config.toml'
        config.write_text('model = "gpt-6-sol"\n\n[hooks]\nenabled = true\n')
        (home / '.claude' / 'settings.json').write_text('{}\n')
        (home / '.claude' / 'plugins' / 'installed_plugins.json').write_text('{}\n')
        before = rc.global_config_snapshot(home)
        header = '[projects.' + json.dumps(str(self.workspace.resolve())) + ']\ntrust_level = "trusted"\n\n'
        config.write_text('model = "gpt-6-sol"\n\n' + header + '[hooks]\nenabled = true\n')
        result = rc.attribute_global_config_changes(before, rc.global_config_snapshot(home), [self.workspace])
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['warnings'], ['global config mutated by codex CLI trust persistence'])
        self.assertNotEqual(result['before']['codex_config']['sha256'], result['after']['codex_config']['sha256'])
        config.write_text(config.read_text().replace('enabled = true', 'enabled = false'))
        changed = rc.attribute_global_config_changes(before, rc.global_config_snapshot(home), [self.workspace])
        self.assertEqual(changed['status'], 'FAIL')
        config.write_text('model = "gpt-6-sol"\n\n[hooks]\n' + header + 'enabled = true\n')
        moved_key = rc.attribute_global_config_changes(before, rc.global_config_snapshot(home), [self.workspace])
        self.assertEqual(moved_key['status'], 'FAIL')
        config.write_text('model = "gpt-6-sol"\n\n' + header.split('trust_level')[0]
                          + '[hooks]\nenabled = true\ntrust_level = "trusted"\n')
        split_entry = rc.attribute_global_config_changes(before, rc.global_config_snapshot(home), [self.workspace])
        self.assertEqual(split_entry['status'], 'FAIL')

    def test_effective_codex_home_trust_warning_does_not_touch_default_home(self):
        isolated = self.root / 'isolated-codex-home'
        isolated.mkdir()
        (isolated / 'config.toml').write_bytes((self.test_home / '.codex/config.toml').read_bytes())
        default_before = (self.test_home / '.codex/config.toml').read_bytes()
        command = self.command()
        command[2] = 'permission-probe'
        result = subprocess.run(command, cwd=self.root,
            env={**os.environ, 'CODEX_HOME': str(isolated), 'FAKE_CODEX_AUTO_TRUST_ENTRY': '1'},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['global_config_changes']['status'], 'PASS')
        self.assertIn('global config mutated by codex CLI trust persistence',
                      report['global_config_changes']['warnings'])
        self.assertEqual((self.test_home / '.codex/config.toml').read_bytes(), default_before)
        self.assertNotEqual((isolated / 'config.toml').read_bytes(), default_before)
        before = rc.global_config_snapshot(self.test_home, isolated)
        (self.test_home / '.codex/config.toml').write_bytes(default_before + b'\n[unexpected]\nflag = true\n')
        default_changed = rc.attribute_global_config_changes(
            before, rc.global_config_snapshot(self.test_home, isolated), [self.workspace])
        self.assertEqual(default_changed['status'], 'FAIL')

    def test_relative_codex_home_is_refused_before_run_state(self):
        args = rc.parser().parse_args(self.command()[2:])
        with patch.dict(os.environ, {'CODEX_HOME': 'relative-home'}):
            with self.assertRaisesRegex(ValueError, 'CODEX_HOME must be absolute'):
                rc.Coordinator(args)
        self.assertFalse(self.run_dir.exists())
        with patch.dict(os.environ, {'CODEX_HOME': ''}):
            co = rc.Coordinator(args)
        self.assertEqual(co.global_codex_home, (self.test_home / '.codex').resolve())

    def test_suite_guard_refuses_real_provider_binary_launches(self):
        for binary in ('claude', 'codex'):
            with self.subTest(binary=binary):
                with self.assertRaisesRegex(AssertionError, 'real claude/codex binary'):
                    self._guarded_test_popen([binary, '--version'])
        with self.assertRaisesRegex(AssertionError, 'real claude/codex binary'):
            self._guarded_test_popen(['/bin/sh', '-c', 'codex --version'])

    def test_fake_codex_refuses_config_write_outside_test_root(self):
        outside = self.root.parent / ('outside-' + self.root.name)
        result = subprocess.run([str(self.fake_codex_cli()), 'exec'], input='probe',
            env={**os.environ, 'CODEX_HOME': str(outside), 'FAKE_CODEX_AUTO_TRUST_ENTRY': '1'},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 97)
        self.assertFalse(outside.exists())

    def test_global_hash_attribution_flags_any_non_allowlisted_change(self):
        cases = ('codex-config', 'claude-settings', 'plugin-version')
        for case in cases:
            with self.subTest(case=case):
                home = self.root / ('global-state-' + case)
                (home / '.codex').mkdir(parents=True)
                (home / '.claude' / 'plugins').mkdir(parents=True)
                config = home / '.codex' / 'config.toml'
                settings = home / '.claude' / 'settings.json'
                plugins = home / '.claude' / 'plugins' / 'installed_plugins.json'
                config.write_text('model = "gpt-6-luna"\n')
                settings.write_text('{"theme":"dark"}\n')
                plugins.write_text(json.dumps({'plugins': [{'name': 'compass', 'version': '0.6.1',
                                                            'lastUpdated': 'before'}]}))
                before = rc.global_config_snapshot(home)
                if case == 'codex-config':
                    config.write_text('model = "gpt-6-astra"\n')
                elif case == 'claude-settings':
                    settings.write_text('{"theme":"light"}\n')
                else:
                    plugins.write_text(json.dumps({'plugins': [{'name': 'compass', 'version': '0.7.0',
                                                                'lastUpdated': 'after'}]}))

                result = rc.attribute_global_config_changes(before, rc.global_config_snapshot(home),
                                                            [self.workspace])

                self.assertEqual(result['status'], 'FAIL', result)
                self.assertTrue(result['findings'])

    def test_global_hash_attribution_handles_new_codex_config_and_rejects_bad_trust_entries(self):
        home = self.root / 'global-state-new-codex-config'
        (home / '.codex').mkdir(parents=True)
        (home / '.claude' / 'plugins').mkdir(parents=True)
        (home / '.claude' / 'settings.json').write_text('{}\n')
        (home / '.claude' / 'plugins' / 'installed_plugins.json').write_text('{}\n')
        before = rc.global_config_snapshot(home)
        workspace = self.workspace.resolve()
        config_path = home / '.codex' / 'config.toml'
        header = '[projects.' + json.dumps(str(workspace)) + ']'
        config_path.write_text(header + '\ntrust_level = "trusted"\n')
        result = rc.attribute_global_config_changes(before, rc.global_config_snapshot(home), [workspace])
        self.assertEqual(result['status'], 'PASS', result)

        invalid_entries = (
            '[projects."/tmp/not-the-probe"]\ntrust_level = "trusted"\n',
            header + '\ntrust_level = "untrusted"\n',
            header + '\ntrust_level = "trusted"\napproval_policy = "never"\n',
        )
        for index, addition in enumerate(invalid_entries):
            with self.subTest(index=index):
                case_home = self.root / f'global-state-bad-trust-{index}'
                (case_home / '.codex').mkdir(parents=True)
                (case_home / '.claude' / 'plugins').mkdir(parents=True)
                (case_home / '.codex' / 'config.toml').write_text('model = "gpt-6-luna"\n')
                (case_home / '.claude' / 'settings.json').write_text('{}\n')
                (case_home / '.claude' / 'plugins' / 'installed_plugins.json').write_text('{}\n')
                old = rc.global_config_snapshot(case_home)
                (case_home / '.codex' / 'config.toml').write_text(
                    (case_home / '.codex' / 'config.toml').read_text() + '\n' + addition)
                changed = rc.global_config_snapshot(case_home)
                self.assertEqual(rc.attribute_global_config_changes(old, changed, [workspace])['status'],
                                 'FAIL')

        separated_home = self.root / 'global-state-separated-trust-lines'
        (separated_home / '.codex').mkdir(parents=True)
        (separated_home / '.claude' / 'plugins').mkdir(parents=True)
        config = separated_home / '.codex' / 'config.toml'
        config.write_text('model = "gpt-6-luna"\n')
        (separated_home / '.claude' / 'settings.json').write_text('{}\n')
        (separated_home / '.claude' / 'plugins' / 'installed_plugins.json').write_text('{}\n')
        before = rc.global_config_snapshot(separated_home)
        config.write_text(header + '\nmodel = "gpt-6-luna"\n\ntrust_level = "trusted"\n')
        after = rc.global_config_snapshot(separated_home)
        self.assertEqual(rc.attribute_global_config_changes(before, after, [workspace])['status'], 'FAIL')

    def test_permission_probe_records_known_codex_and_claude_autoupdates(self):
        home = self.root / 'probe-home'
        (home / '.codex').mkdir(parents=True)
        (home / '.claude' / 'plugins').mkdir(parents=True)
        (home / '.codex' / 'config.toml').write_text('model = "gpt-6-luna"\n')
        (home / '.claude' / 'settings.json').write_text('{}\n')
        (home / '.claude' / 'plugins' / 'installed_plugins.json').write_text(
            json.dumps({'plugins': [{'name': 'compass', 'version': '0.6.1', 'lastUpdated': 'before'}]}))
        command = self.command()
        command[2] = 'permission-probe'
        command += ['--claude-bin', str(self.fake_claude_cli())]
        result = subprocess.run(command, cwd=self.root,
                                env={**os.environ, 'HOME': str(home),
                                     'FAKE_CODEX_AUTO_TRUST_ENTRY': '1',
                                     'FAKE_CLAUDE_PLUGIN_LAST_UPDATED': '1'},
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('global config mutated by codex CLI trust persistence', result.stdout)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        changes = report['global_config_changes']
        self.assertEqual(changes['status'], 'PASS', changes)
        self.assertEqual({(row['file'], row['change']) for row in changes['expected_changes']}, {
            ('codex_config', 'trusted-probe-workspace-entry'),
            ('claude_plugins', 'plugin-lastUpdated'),
        })
        self.assertEqual(changes['findings'], [])
        self.assertIn(str(Path(report['author_permission_probe']['workspace']).resolve()), result.stdout)

    def test_product_codex_turn_warns_and_unrelated_global_change_holds(self):
        for unexpected in (False, True):
            with self.subTest(unexpected=unexpected):
                self.run_dir = self.root / ('product-global-' + str(unexpected).lower())
                probe_command = self.command('--exercise-revisions')
                probe_command[2] = 'permission-probe'
                base_env = {**os.environ, 'FAKE_CODEX_AUTO_TRUST_ENTRY': '1'}
                probe = subprocess.run(probe_command, cwd=self.root, env=base_env,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
                run_env = {**base_env, **({'FAKE_CODEX_UNEXPECTED_GLOBAL_CHANGE': '1'} if unexpected else {})}
                run = subprocess.run(self.command('--exercise-revisions'), cwd=self.root, env=run_env,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                state = json.loads((self.run_dir / 'state.json').read_text())
                if unexpected:
                    self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                    self.assertEqual(state['status'], 'HOLD')
                    self.assertIn('global config', state['hold_reason'])
                else:
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    self.assertIn('global config mutated by codex CLI trust persistence', run.stdout)
                    self.assertTrue(any(str(self.workspace) in row.get('global_config_warning', '')
                                        for row in state['turns']))

    def test_permission_probe_fails_on_unrelated_fake_global_change(self):
        home = self.root / 'probe-home-unexpected'
        (home / '.codex').mkdir(parents=True)
        (home / '.claude' / 'plugins').mkdir(parents=True)
        (home / '.codex' / 'config.toml').write_text('model = "gpt-6-luna"\n')
        (home / '.claude' / 'settings.json').write_text('{}\n')
        (home / '.claude' / 'plugins' / 'installed_plugins.json').write_text('{}\n')
        command = self.command()
        command[2] = 'permission-probe'
        result = subprocess.run(command, cwd=self.root,
                                env={**os.environ, 'HOME': str(home),
                                     'FAKE_CODEX_AUTO_TRUST_ENTRY': '1',
                                     'FAKE_CODEX_UNEXPECTED_GLOBAL_CHANGE': '1'},
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 2)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn('unexpected-global-config-change', report['failure_reasons'])
        self.assertEqual(report['global_config_changes']['status'], 'FAIL')
        self.assertTrue(any(row['file'] == 'codex_config'
                            for row in report['global_config_changes']['findings']))

    def test_permission_probe_direct_controls_are_advisory(self):
        for mode in ('readonly', 'no-writable-root', 'escape', 'no-marker'):
            with self.subTest(mode=mode):
                self.run_dir = self.root / ('codex-sandbox-' + mode)
                command = self.command()
                command[2] = 'permission-probe'
                result = subprocess.run(command, cwd=self.root,
                                        env={**os.environ, 'FAKE_CODEX_SANDBOX_MODE': mode},
                                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, 0 if mode == 'no-marker' else 2,
                                 result.stdout + result.stderr)
                report = json.loads((self.run_dir / 'permission-probe.json').read_text())
                self.assertEqual(report['status'], 'PASS' if mode == 'no-marker' else 'FAIL')
                self.assertEqual(report['author_permission_probe']['model_probe'], 'ATTEMPTED')
                advisory = report['author_permission_probe']['advisory_direct_controls']
                self.assertEqual(advisory['note'], 'advisory: not proven policy-equivalent to codex exec')
                checks = report['author_permission_probe']['codex_sandbox_checks']['checks']
                self.assertEqual(advisory['checks'], checks)
                if mode == 'readonly':
                    self.assertFalse(checks['workspace_write_allowed']['policy_observed'])
                    self.assertFalse(checks['run_tmpdir_write_allowed']['policy_observed'])
                    self.assertTrue(checks['external_tmpdir_denied']['policy_observed'])
                    self.assertTrue(checks['slash_tmp_denied']['policy_observed'])
                elif mode == 'no-writable-root':
                    self.assertTrue(checks['workspace_write_allowed']['policy_observed'])
                    self.assertFalse(checks['run_tmpdir_write_allowed']['policy_observed'])
                    self.assertTrue(checks['external_tmpdir_denied']['policy_observed'])
                    self.assertTrue(checks['slash_tmp_denied']['policy_observed'])
                else:
                    self.assertTrue(checks['workspace_write_allowed']['policy_observed'])
                    self.assertTrue(checks['run_tmpdir_write_allowed']['policy_observed'])
                    self.assertFalse(checks['external_tmpdir_denied']['policy_observed'])
                    self.assertFalse(checks['slash_tmp_denied']['policy_observed'])

    def test_codex_direct_contract_refusal_does_not_block_model_probe(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_CODEX_SANDBOX_CONTRACT_REJECT': '1'}):
            report = co._author_permission_probe()
        self.assertEqual(report['status'], 'PASS', report)
        self.assertEqual(report['model_probe'], 'ATTEMPTED')
        self.assertTrue(all(row['status'] == 'PASS' for row in report['model_escape_checks'].values()))
        advisory = report['advisory_direct_controls']
        self.assertEqual(advisory['note'], 'advisory: not proven policy-equivalent to codex exec')
        self.assertTrue(all(row['returncode'] == 2 and row['status'] == 'FAIL'
                            for row in advisory['checks'].values()))
        self.assertEqual(co.author_flags()['codex_cli_version'], 'codex-cli 0.157.0')
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': 'codex-cli 0.158.0'}):
            self.assertNotEqual(co.author_flags()['codex_cli_version'], 'codex-cli 0.157.0')

    def test_codex_capability_config_fails_probe_and_prevents_dispatch(self):
        config = self.test_home / '.codex/config.toml'
        for key, value in (('mcp_servers', '{}'), ('notify', '"hook"')):
            with self.subTest(key=key):
                config.write_text(f'{key} = {value}\n')
                co = self.coordinator('--author-vendor', 'codex')
                result = co._author_permission_probe()
                self.assertEqual(result['status'], 'FAIL')
                label = 'MCP servers' if key == 'mcp_servers' else key
                self.assertIn(label, result['reason'])
                with self.assertRaisesRegex(RuntimeError, label):
                    co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})
                self.assertEqual(co.state['sequence'], 0)

    def test_clean_codex_capability_config_allows_probe(self):
        config = self.test_home / '.codex/config.toml'
        config.write_text('model = "gpt-6-luna"\n')
        co = self.coordinator('--author-vendor', 'codex')
        self.assertEqual(co.codex_capabilities()['status'], 'PASS')

    def test_plugin_bundle_blocks_codex_probe_and_dispatch(self):
        plugin = self.test_home / '.codex/plugins/cache/local/probe/1.0'
        plugin.mkdir(parents=True)
        (plugin / '.mcp.json').write_text('{"mcpServers":{"unsafe":{"command":"node"}}}')
        co = self.coordinator('--author-vendor', 'codex')
        self.assertEqual(co._author_permission_probe()['status'], 'FAIL')
        with self.assertRaisesRegex(RuntimeError, 'plugin MCP'):
            co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})
        self.assertEqual(co.state['sequence'], 0)

    def test_codex_direct_control_pass_cannot_override_model_escape_failure(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_CODEX_SANDBOX_MODE': 'deny',
                                     'FAKE_AUTHOR_ESCAPE_WRITE': 'home'}):
            report = co._author_permission_probe()
        self.assertEqual(report['codex_sandbox_checks']['status'], 'PASS')
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['model_escape_checks']['home']['status'], 'FAIL')

    def test_codex_direct_control_pass_qualifies_untouched_model_unknown(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_CODEX_SANDBOX_MODE': 'deny',
                                     'FAKE_AUTHOR_ESCAPE_SKIP': 'home'}):
            report = co._author_permission_probe()
        self.assertEqual(report['codex_sandbox_checks']['status'], 'PASS')
        self.assertEqual(report['status'], 'PASS_RESIDUAL_RISK')
        self.assertEqual(report['model_escape_checks']['home']['status'], 'UNKNOWN')

    def test_d1b_untouched_directories_support_residual_risk(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_ESCAPE_SKIP': 'all'}):
            report = co._author_permission_probe()
        self.assertEqual(report['d1a_model_verdict'], 'UNKNOWN')
        self.assertEqual(report['d1b_synthetic_verdict'], 'PASS')
        self.assertEqual(report['status'], 'PASS_RESIDUAL_RISK')
        self.assertIn('UNVERIFIED; re-check at M6', report['residual_risk'])
        self.assertTrue(all(row['directory_before'] == row['directory_after']
                            for row in report['model_escape_checks'].values()))
        self.assertEqual(len(report['advisory_direct_controls']['model_target_checks']),
                         5 if sys.platform == 'darwin' else 4)

    def test_d1b_refused_and_synthetic_write_have_distinct_verdicts(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_ESCAPE_SKIP': 'all',
                                     'FAKE_CODEX_SANDBOX_CONTRACT_REJECT': '1'}):
            refused = co._author_permission_probe()
        self.assertEqual((refused['d1b_synthetic_verdict'], refused['status']), ('UNKNOWN', 'UNKNOWN'))
        with patch.dict(os.environ, {'FAKE_AUTHOR_ESCAPE_SKIP': 'all', 'FAKE_CODEX_SANDBOX_MODE': 'escape'}):
            written = co._author_permission_probe()
        self.assertEqual((written['d1b_synthetic_verdict'], written['status']), ('FAIL', 'FAIL'))

    def test_d1b_positive_write_failure_fails(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_POSITIVE_FAIL': 'workspace'}):
            report = co._author_permission_probe()
        self.assertFalse(report['outcomes']['workspace_write_allowed'])
        self.assertEqual(report['status'], 'FAIL')

    def test_author_probe_refuses_live_process_group_before_directory_snapshot(self):
        co = self.coordinator()
        with patch.object(rc, 'retry_killpg_eperm', return_value=None):
            report = co._author_permission_probe()
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn('process group is still alive', report['reason'])
        self.assertTrue(all(not Path(path).exists() for path in report['escape_directory_cleanup']['removed']))

    def test_reviewer_os_unknown_blocks_residual_author_probe(self):
        command = self.command(); command[2] = 'permission-probe'
        result = subprocess.run(command, cwd=self.root,
            env={**os.environ, 'FAKE_AUTHOR_ESCAPE_SKIP': 'all', 'FAKE_CLAUDE_DENY_OS_PROBE': '1'},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['author_permission_probe']['status'], 'PASS_RESIDUAL_RISK')
        self.assertEqual(report['claude_os_denial_probe']['status'], 'UNKNOWN')
        self.assertEqual(report['status'], 'UNKNOWN')

    def test_d1b_residual_probe_allows_run_and_reports_risk(self):
        command = self.command('--exercise-revisions'); command[2] = 'permission-probe'
        env = {**os.environ, 'FAKE_AUTHOR_ESCAPE_SKIP': 'all', 'FAKE_CODEX_AUTO_TRUST_ENTRY': '1'}
        probe = subprocess.run(command, cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'PASS_RESIDUAL_RISK')
        self.assertEqual(report['author_permission_probe']['d1b_synthetic_verdict'], 'PASS')
        run = subprocess.run(self.command('--exercise-revisions'), cwd=self.root, env=env,
                             text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['residual_risk'], 'equivalence to real codex exec UNVERIFIED; re-check at M6')
        self.assertIn('Residual risk: equivalence to real codex exec UNVERIFIED',
                      (self.run_dir / 'review-comparison.md').read_text())

    def test_author_policy_digest_ignores_only_known_trust_entries(self):
        co = self.coordinator()
        config = self.test_home / '.codex/config.toml'
        original = config.read_text()
        before = co.author_flags()['codex_config_sha256']
        for path in (self.run_dir / 'paired-session-author-probe-abc' / 'workspace', self.workspace):
            with config.open('a') as handle:
                handle.write('\n[projects.' + json.dumps(str(path)) + ']\ntrust_level = "trusted"\n')
        self.assertEqual(co.author_flags()['codex_config_sha256'], before)
        with config.open('a') as handle:
            handle.write('\n[projects."/tmp/unrelated"]\ntrust_level = "trusted"\n')
        self.assertNotEqual(co.author_flags()['codex_config_sha256'], before)
        config.write_text(original)

    def test_resume_refuses_probe_with_unattributed_global_config_change(self):
        self.run_dir = self.root / 'resume-global-change'
        initial = self.run_coordinator('--stop-after-plan')
        self.assertIn('HOLD', initial.stdout)
        args = rc.parser().parse_args(self.command()[2:])
        co = rc.Coordinator(args)
        rc.atomic_json(self.run_dir / 'permission-probe.json', {
            'status': 'PASS',
            'reviewer_flags_digest': co.reviewer_flags_digest(),
            'author_flags_digest': co.author_flags_digest(),
            'author_permission_probe': {'status': 'PASS'},
            'global_config_changes': {'status': 'FAIL', 'findings': [
                {'file': 'claude_settings', 'reason': 'unexpected-content-change'}]},
        })
        command = self.command()
        command[2] = 'resume'
        resumed = subprocess.run(command, cwd=self.root, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(resumed.returncode, 2)
        self.assertIn('global config changes were not fully attributed', resumed.stdout)

    def test_author_escape_checks_use_codex_sandbox_with_author_overrides(self):
        co = self.coordinator('--author-vendor', 'codex')
        co.author_temp_dir.mkdir(parents=True, exist_ok=True)
        log = self.root / 'codex-sandbox-log.jsonl'
        target = self.root / 'external-target'
        with patch.dict(os.environ, {'FAKE_CODEX_SANDBOX_MODE': 'deny',
                                     'FAKE_CODEX_SANDBOX_LOG': str(log)}):
            denied = co._codex_sandbox_escape_check(self.workspace, 'external_tmpdir', target)
        self.assertEqual(denied['status'], 'PASS', denied)
        self.assertIn('touch ', denied['command'])
        self.assertTrue(denied['os_denial_observed'])
        self.assertTrue(denied['target_absent_before_cleanup'])
        invocation = json.loads(log.read_text().splitlines()[0])
        self.assertIn('-P', invocation['args'])
        self.assertEqual(denied['copied_policy_files'], ['config.toml'])
        self.assertEqual(denied['source_config_sha256'], denied['copy_config_sha256'])
        self.assertEqual(Path(invocation['codex_home']).parent, self.run_dir)
        self.assertFalse(Path(invocation['codex_home']).exists())
        self.assertEqual(invocation['codex_home_entries'], ['config.toml'])
        self.assertIn('--log-denials', invocation['args'])
        self.assertEqual(invocation['tmpdir'], str(co.author_temp_dir))
        config_values = [invocation['args'][i + 1] for i, arg in
                         enumerate(invocation['args'][:-1]) if arg == '-c']
        expected = co._author_sandbox_config_args()
        self.assertEqual(config_values, expected[1::2])
        self.assertEqual(invocation['command'][0], '/usr/bin/touch')

        escaped_target = self.root / 'escaped-target'
        with patch.dict(os.environ, {'FAKE_CODEX_SANDBOX_MODE': 'escape',
                                     'FAKE_CODEX_SANDBOX_LOG': str(log)}):
            escaped = co._codex_sandbox_escape_check(self.workspace, 'external_tmpdir', escaped_target)
        self.assertEqual(escaped['status'], 'FAIL')
        self.assertFalse(escaped['target_absent_before_cleanup'])
        self.assertTrue(escaped['cleanup_ok'])
        self.assertFalse(escaped_target.exists())

    def test_workspace_program_and_relative_path_shims_cannot_drive_probe(self):
        shim_dir = self.workspace / 'bin'
        shim_dir.mkdir()
        for name in ('touch', 'node', 'codex'):
            shim = shim_dir / name
            shim.write_text('#!/bin/sh\nexit 0\n')
            shim.chmod(0o755)
        with patch.dict(os.environ, {'PATH': 'bin::' + str(shim_dir) + os.pathsep + os.environ['PATH']}):
            co = self.coordinator()
            programs, issue = co._program_state()
            self.assertIsNone(issue)
            self.assertNotIn(str(shim_dir), programs['path_env'])
            self.assertNotIn('::', programs['path_env'])
            self.assertFalse(any(not Path(p).is_absolute() for p in programs['path_env'].split(os.pathsep)))
            target = self.root / 'safe-control-target'
            co._codex_sandbox_escape_check(self.workspace, 'external_tmpdir', target)
        self.run_dir = self.root / 'forbidden-program-run'
        forbidden = self.command('--codex-bin', str(shim_dir / 'codex'))
        forbidden[2] = 'permission-probe'
        result = subprocess.run(forbidden, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD')
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertIn('author-writable root', json.dumps(report['failure_reasons']))

    def test_bound_provider_binary_change_holds_before_dispatch(self):
        co = self.coordinator()
        programs, issue = co._program_state()
        self.assertIsNone(issue)
        binary = Path(programs['codex_bin']['path'])
        binary.write_bytes(binary.read_bytes() + b'\n# changed after probe\n')
        with self.assertRaisesRegex(RuntimeError, 'operator program or PATH changed'):
            co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', rc.author_schema())
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertEqual(co.state['invocations_used'], 0)

    def test_workspace_profile_symlink_to_prior_author_tmp_is_refused(self):
        prior_tmp = self.root / 'prior-run' / 'author-tmp'
        prior_tmp.mkdir(parents=True)
        unsafe = prior_tmp / 'paired-session.json'
        unsafe.write_text(json.dumps({'codex_bin': str(prior_tmp / 'codex')}))
        profile = self.workspace / '.review-loop' / 'paired-session.json'
        profile.parent.mkdir()
        profile.symlink_to(unsafe)
        command = self.command()
        command[2] = 'permission-probe'
        result = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertIn('workspace profile cannot select operator programs',
                      json.dumps(report['failure_reasons']))

    def test_fresh_permission_probe_rebinds_changed_operator_binary(self):
        co = self.coordinator()
        before, issue = co._program_state()
        self.assertIsNone(issue)
        binary = Path(before['codex_bin']['path'])
        binary.write_bytes(binary.read_bytes() + b'\n# upgraded\n')
        co.args.action = 'permission-probe'
        after, issue = co._program_state()
        self.assertIsNone(issue)
        self.assertNotEqual(before['codex_bin']['sha256'], after['codex_bin']['sha256'])
        self.assertEqual(co.state['operator_programs'], after)
        co.args.action = 'run'
        self.assertIsNone(co._program_state()[1])

    def test_done_program_drift_does_not_mutate_acceptance_state(self):
        co = self.coordinator()
        programs, issue = co._program_state()
        self.assertIsNone(issue)
        co.state.update(status='DONE', acceptance_state='PENDING', phase='EXEC')
        co.save()
        binary = Path(programs['codex_bin']['path'])
        binary.write_bytes(binary.read_bytes() + b'\n# updated while DONE\n')
        passed, reason = co.probe_passed()
        self.assertFalse(passed)
        self.assertIn('operator program or PATH changed', reason)
        self.assertEqual(co.state['status'], 'DONE')
        self.assertEqual(co.state['acceptance_state'], 'PENDING')

    def test_done_missing_codex_probe_fails_without_losing_accept(self):
        command = self.command()
        co = rc.Coordinator(rc.parser().parse_args(command[2:]))
        self.assertIsNone(co._program_state()[1])
        co.state.update(status='DONE', acceptance_state='PENDING', phase='EXEC')
        co.state['approved_snapshot'] = rc.git_snapshot(co.workspace)[0]
        co.save()
        Path(co.args.codex_bin).unlink()
        command[2] = 'permission-probe'
        probe = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(probe.returncode, 2)
        self.assertIn('FAIL', probe.stdout)
        self.assertEqual(json.loads(co.state_path.read_text())['status'], 'DONE')
        command[2] = 'accept'
        command.append('--intent-only')
        intent = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(intent.returncode, 0, intent.stdout + intent.stderr)
        command.remove('--intent-only')
        command[2] = 'accept'
        command.extend(['--expect', json.loads(intent.stdout)['digest']])
        accepted = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIn('ACCEPTED', accepted.stdout)

    def test_program_issue_never_executes_version_or_synthetic_control(self):
        co = self.coordinator()
        programs, issue = co._program_state()
        self.assertIsNone(issue)
        binary = Path(programs['codex_bin']['path'])
        binary.write_bytes(binary.read_bytes() + b'\n# changed before control\n')
        self.assertEqual(co._codex_cli_version(), 'UNAVAILABLE')
        with self.assertRaisesRegex(RuntimeError, 'operator program or PATH changed'):
            co._codex_sandbox_escape_check(self.workspace, 'external_tmpdir', self.root / 'target')
        self.assertFalse((self.root / 'target').exists())

    def test_snapshot_action_cannot_choose_workspace_git_from_relative_path(self):
        marker = self.root / 'workspace-git-ran'
        shim = self.workspace / 'git'
        shim.write_text('#!/bin/sh\necho shim > ' + shlex.quote(str(marker)) + '\nexit 0\n')
        shim.chmod(0o755)
        command = self.command()
        command[2] = 'snapshot'
        result = subprocess.run(command, cwd=self.workspace,
                                env={**os.environ, 'PATH': '.:' + os.environ['PATH']},
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_synthetic_codex_contract_refuses_old_argv_and_unknown_version(self):
        co = self.coordinator('--author-vendor', 'codex')
        co.author_temp_dir.mkdir(parents=True, exist_ok=True)
        old = subprocess.run([str(self.fake_codex_cli()), 'sandbox', '-C', str(self.workspace),
            *co._author_sandbox_config_args(), '--', 'touch', str(self.root / 'old-control-target')],
            cwd=self.workspace, text=True, capture_output=True)
        self.assertEqual(old.returncode, 2)
        self.assertFalse((self.root / 'old-control-target').exists())
        with patch.dict(os.environ, {'FAKE_CODEX_VERSION': 'codex-cli 0.158.0'}):
            unknown = co._codex_sandbox_escape_check(self.workspace, 'unknown-version', self.root / 'target')
        self.assertEqual(unknown['status'], 'CONTRACT-FAIL')
        self.assertFalse((self.root / 'target').exists())

    def test_synthetic_profile_rejects_extra_writable_root_and_credentials(self):
        co = self.coordinator('--author-vendor', 'codex')
        co.author_temp_dir.mkdir(parents=True, exist_ok=True)
        (self.test_home / '.codex' / 'auth.json').write_text('{"private":"do not copy"}')
        profile = co._codex_sandbox_profile_args()
        changed = list(profile)
        position = next(i for i, arg in enumerate(changed) if arg.startswith('permissions.paired_session_author.filesystem='))
        changed[position] = changed[position][:-1] + ', "/tmp"="write"}'
        with patch.object(co, '_codex_sandbox_profile_args', return_value=changed):
            refused = co._codex_sandbox_escape_check(self.workspace, 'extra-root', self.root / 'target')
        self.assertEqual(refused['returncode'], 2)
        self.assertEqual(refused['status'], 'FAIL')
        self.assertEqual(refused['copied_policy_files'], ['config.toml'])
        self.assertFalse((self.root / 'target').exists())

    def tearDown(self):
        if self.original_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self.original_home
        self.temp.cleanup()

    def fake_codex_cli(self):
        path = self.root / 'fake-codex'
        script = f'''#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print(os.environ.get("FAKE_CODEX_VERSION", "codex-cli 0.157.0")); sys.exit(0)
if args and args[0] == "sandbox":
    if os.environ.get("FAKE_CODEX_SANDBOX_CONTRACT_REJECT") or "-P" not in args:
        print("error: --permission-profile <NAME> required", file=sys.stderr); sys.exit(2)
    command = args[args.index("--") + 1:] if "--" in args else []
    config = {{}}
    for index, arg in enumerate(args[:-1]):
        if arg in ("-c", "--config") and "=" in args[index + 1]:
            key, raw = args[index + 1].split("=", 1)
            try:
                config[key] = json.loads(raw)
            except ValueError:
                config[key] = raw.strip('"')
    profile = args[args.index("-P") + 1]
    fs = config.get("permissions." + profile + ".filesystem", "")
    home = Path(os.environ.get("CODEX_HOME", "/nonexistent")).resolve()
    test_root = Path(os.environ.get("FAKE_CODEX_TEST_ROOT", "/nonexistent")).resolve()
    expected_fs = {{':root': 'read', ':tmpdir': 'write',
                   **{{str(Path(root).resolve()): 'write' for root in config.get('sandbox_workspace_write.writable_roots', [])}},
                   ':workspace_roots': {{'.': 'write', '.git': 'read', '.agents': 'read', '.codex': 'read', '.aws': 'read'}}}}
    try: actual_fs = json.loads(fs.replace('"=', '":'))
    except (ValueError, AttributeError): actual_fs = None
    if (actual_fs != expected_fs or
            config.get("permissions." + profile + ".network.enabled") is not False or
            config.get("sandbox_workspace_write.network_access") is not False or
            config.get("sandbox_workspace_write.exclude_slash_tmp") is not True or
            config.get("sandbox_workspace_write.exclude_tmpdir_env_var") is not False or
            test_root not in home.parents or not (home / "config.toml").is_file()):
        print("error: unsupported synthetic author policy", file=sys.stderr); sys.exit(2)
    target = Path(command[-1]).resolve() if command else None
    cwd = Path(args[args.index("-C") + 1]).resolve() if "-C" in args else Path.cwd().resolve()
    roots = [Path(item).resolve() for item in config.get("sandbox_workspace_write.writable_roots", [])]
    tmpdir = Path(os.environ.get("TMPDIR", "/nonexistent")).resolve()
    def below(path, root):
        return path == root or root in path.parents
    workspace_write = config.get("sandbox_mode") == "workspace-write"
    in_workspace = bool(target and below(target, cwd))
    in_tmpdir = bool(target and below(target, tmpdir))
    in_roots = bool(target and any(below(target, root) for root in roots))
    direct_slash_tmp = bool(target and target.parent == Path("/tmp").resolve())
    allowed = workspace_write and in_workspace
    if workspace_write and not allowed and in_roots:
        allowed = (not in_tmpdir or config.get("sandbox_workspace_write.exclude_tmpdir_env_var") is False)
        if direct_slash_tmp and config.get("sandbox_workspace_write.exclude_slash_tmp") is True:
            allowed = False
    if workspace_write and direct_slash_tmp and config.get("sandbox_workspace_write.exclude_slash_tmp") is False:
        allowed = True
    record = {{"args": args, "command": command, "config": config,
              "tmpdir": os.environ.get("TMPDIR"), "cwd": os.getcwd(),
              "codex_home": str(home), "codex_home_entries": sorted(os.listdir(home))}}
    log_path = os.environ.get("FAKE_CODEX_SANDBOX_LOG")
    if log_path and not (target and target.name.startswith("paired-session-escape-")):
        log = Path(log_path); log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as handle: handle.write(json.dumps(record) + "\\n")
    mode = os.environ.get("FAKE_CODEX_SANDBOX_MODE", "deny")
    if mode == "no-writable-root":
        if in_tmpdir and not in_workspace:
            allowed = False
    if mode == "readonly":
        allowed = False
    if target and (allowed or mode == "escape"):
        target.write_text("fake sandbox write\\n"); sys.exit(0)
    if mode == "no-marker":
        print("fake sandbox failure without a denial marker", file=sys.stderr); sys.exit(1)
    print("sandbox-exec: deny file-write-create " + str(target) + ": Operation not permitted", file=sys.stderr); sys.exit(1)

prompt = sys.stdin.read()
if args and args[0] == "exec" and os.environ.get("FAKE_CODEX_TOUCH_CLAUDE_SETTINGS"):
    settings = Path.home() / ".claude/settings.json"; root = Path(os.environ["FAKE_CODEX_TEST_ROOT"]).resolve()
    if root not in settings.resolve().parents: sys.exit(97)
    settings.write_text('{{"other_vendor_update":true}}\\n')
if args and args[0] == "exec" and os.environ.get("FAKE_CODEX_AUTO_TRUST_ENTRY"):
    home_text = os.environ.get("CODEX_HOME"); root_text = os.environ.get("FAKE_CODEX_TEST_ROOT")
    if not home_text or not root_text or not Path(home_text).is_absolute() or not Path(root_text).is_absolute():
        print("fake Codex requires an isolated test CODEX_HOME", file=sys.stderr); sys.exit(97)
    root = Path(root_text).resolve(); config_path = Path(home_text).resolve() / "config.toml"
    if root not in config_path.parents or not root.is_dir():
        print("fake Codex config write escaped test root", file=sys.stderr); sys.exit(97)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    entry = "[projects." + json.dumps(str(Path.cwd().resolve())) + "]\\ntrust_level = \\"trusted\\"\\n"
    if not config_path.exists() or entry not in config_path.read_text():
        with config_path.open("a") as handle: handle.write("\\n" + entry)
    if os.environ.get("FAKE_CODEX_UNEXPECTED_GLOBAL_CHANGE"):
        with config_path.open("a") as handle: handle.write("\\n[unrelated]\\nchange = true\\n")
result = subprocess.run([sys.executable, {str(FAKE)!r}, *args], input=prompt, text=True)
sys.exit(result.returncode)
'''
        path.write_text(script)
        path.chmod(0o755)
        return path

    def fake_claude_cli(self):
        path = self.root / 'fake-claude'
        path.write_text(
            f'#!{sys.executable}\n'
            'import json, os, shlex, subprocess, sys\n'
            'from pathlib import Path\n'
            'args = sys.argv[1:]; prompt = sys.stdin.read()\n'
            'if os.environ.get("FAKE_CLAUDE_PLUGIN_LAST_UPDATED"):\n'
            '    path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"\n'
            '    if path.is_file():\n'
            '        data = json.loads(path.read_text()); plugins = data.get("plugins", [])\n'
            '        if isinstance(plugins, list) and plugins:\n'
            '            plugins[0]["lastUpdated"] = "after-fake-probe"; path.write_text(json.dumps(data))\n'
            'if os.environ.get("FAKE_CLAUDE_TOUCH_CODEX_CONFIG"):\n'
            '    root = Path(os.environ["FAKE_CODEX_TEST_ROOT"]).resolve()\n'
            '    config = (Path(os.environ["CODEX_HOME"]) / "config.toml").resolve()\n'
            '    if root not in config.parents: sys.exit(97)\n'
            '    if "[other_vendor_update]" not in config.read_text():\n'
            '        with config.open("a") as handle: handle.write("\\n[other_vendor_update]\\nflag = true\\n")\n'
            'if os.environ.get("FAKE_CLAUDE_INVOCATION_LOG"):\n'
            '    Path(os.environ["FAKE_CLAUDE_INVOCATION_LOG"]).write_text(json.dumps(args))\n'
            'delegate_env = os.environ.copy(); delegate_env.pop("FAKE_SANDBOX_WRITE", None)\n'
            f'result = subprocess.run([sys.executable, {str(FAKE)!r}, *args], input=prompt,\n'
            '                        text=True, capture_output=True, env=delegate_env)\n'
            'if "Role: permission-system probe" in prompt and result.returncode == 0:\n'
            '    pending = {}; output_rows = []\n'
            '    for line in result.stdout.splitlines():\n'
            '        row = json.loads(line)\n'
            '        if row.get("type") == "assistant":\n'
            '            for block in row.get("message", {}).get("content", []):\n'
            '                if block.get("type") == "tool_use" and block.get("name") == "Bash":\n'
            '                    pending[block.get("id")] = block.get("input", {}).get("command", "")\n'
            '        elif row.get("type") == "user":\n'
            '            for block in row.get("message", {}).get("content", []):\n'
            '                command = pending.get(block.get("tool_use_id"), "")\n'
            '                if block.get("type") != "tool_result" or not command:\n'
            '                    continue\n'
            '                is_dedicated = command.startswith("touch ") and ".paired-session-run-dir-probe-" in command\n'
            '                is_os_only = ".paired-session-os-probe-" in command\n'
            '                is_other_probe = ("paired-session-claude-sandbox-" in command or\n'
            '                                  ".paired-session-context-probe-" in command)\n'
            '                if (is_dedicated or is_os_only) and os.environ.get("FAKE_SANDBOX_WRITE"):\n'
            '                    Path(shlex.split(command)[-1]).write_text("escape\\n")\n'
            '                    block["content"] = "fake sandbox escape"; block["is_error"] = False\n'
            '                elif (is_dedicated or is_os_only) and os.environ.get("FAKE_CLAUDE_DENY_OS_PROBE"):\n'
            '                    block["content"] = "Claude requested permissions to use Bash, but you have not granted it yet."; block["is_error"] = True\n'
            '                elif is_dedicated and os.environ.get("FAKE_CLAUDE_DENY_RUN_DIR_ONLY"):\n'
            '                    block["content"] = "Permission to use Bash with command " + command + " has been denied."; block["is_error"] = True\n'
            '                elif (is_dedicated or is_os_only) and os.environ.get("FAKE_SANDBOX_NO_OS_MARKER"):\n'
            '                    block["content"] = "fake sandbox failure without an OS marker"; block["is_error"] = True\n'
            '                elif is_dedicated or is_os_only:\n'
            '                    block["content"] = "zsh: operation not permitted: " + shlex.split(command)[-1]; block["is_error"] = True\n'
            '                elif is_other_probe and os.environ.get("FAKE_CLAUDE_ALLOW_OTHER_PROBE"):\n'
            '                    block["content"] = "zsh: operation not permitted"; block["is_error"] = True\n'
            '                elif is_other_probe:\n'
            '                    block["content"] = "Claude requested permissions to use Bash, but you have not granted it yet."; block["is_error"] = True\n'
            '        output_rows.append(row)\n'
            '    result.stdout = "\\n".join(json.dumps(row) for row in output_rows) + "\\n"\n'
            'sys.stdout.write(result.stdout); sys.stderr.write(result.stderr); sys.exit(result.returncode)\n')
        path.chmod(0o755)
        return path

    def command(self, *extra):
        return [sys.executable, str(MODULE_PATH), 'run', '--workspace', str(self.workspace),
                '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
                '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli()), '--timeout', '10',
                '--author-effort', 'low', '--reviewer-effort', 'low', '--gate-effort', 'low',
                '--test-command', 'python3 -m unittest',
                *extra]

    def run_coordinator(self, *extra, env=None, skip_probe=True):
        merged = os.environ.copy()
        if env:
            merged.update(env)
        command = self.command(*extra)
        if skip_probe:
            command.append('--skip-probe')
        return subprocess.run(command, cwd=self.root, env=merged,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def run_operator_action(self, action, *extra, env=None):
        command = self.command(*extra)
        command[2] = action
        command.append('--skip-probe')
        if action in ('accept', 'reject') and '--scope-change' not in extra:
            intent = command.copy(); intent.append('--intent-only')
            issued = subprocess.run(intent, cwd=self.root, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if issued.returncode:
                return issued
            command.extend(['--expect', json.loads(issued.stdout)['digest']])
        return subprocess.run(command, cwd=self.root, env={**os.environ, **(env or {})}, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def issue_operator_intent(self, action, *extra):
        command = self.command(*extra); command[2] = action
        command.extend(['--intent-only', '--skip-probe'])
        return subprocess.run(command, cwd=self.root, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def coordinator(self, *extra):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli()), *extra])
        return rc.Coordinator(args)

    def test_failed_first_persistent_claude_turn_rotates_session(self):
        for role, flags in (('author', ['--author-vendor', 'claude']),
                            ('reviewer', ['--reviewer-vendor', 'claude'])):
            with self.subTest(role=role):
                self.run_dir = self.root / ('failed-' + role)
                co = self.coordinator(*flags)
                original = co.state['sessions'][role]
                with patch.dict(os.environ, {'FAKE_RATE_LIMIT': '1'}):
                    with self.assertRaisesRegex(RuntimeError, 'rate_limited'):
                        co._invoke_once(role, 'PLAN', 'Role: persistent. Phase: PLAN.', {})
                state = json.loads(co.state_path.read_text())
                self.assertNotEqual(state['sessions'][role], original)
                self.assertFalse(state['started'][role])
                rotated = state['sessions'][role]
                receipt = json.loads((co.evidence / f"001-plan-{role}.receipt.json").read_text())
                self.assertEqual(receipt['error_kind'], 'rate_limited')
                self.assertIn('--session-id', receipt['command'])
                self.assertIn(original, receipt['command'])
                co._invoke_once(role, 'PLAN', 'Role: persistent. Phase: PLAN.', {})
                retry_receipt = json.loads((co.evidence / f"002-plan-{role}.receipt.json").read_text())
                session_index = retry_receipt['command'].index('--session-id')
                self.assertEqual(retry_receipt['command'][session_index:session_index + 2],
                                 ['--session-id', rotated])

    def test_non_rate_limit_failed_first_claude_turn_rotates_and_counts_invocation(self):
        co = self.coordinator('--author-vendor', 'claude')
        original = co.state['sessions']['author']
        failing_cli = self.root / 'fail-claude-cli'
        failing_cli.write_text(f'#!{sys.executable}\nimport sys\nprint("ordinary CLI failure", file=sys.stderr)\nsys.exit(7)\n')
        failing_cli.chmod(0o755)
        co.args.claude_bin = str(failing_cli)

        with self.assertRaisesRegex(RuntimeError, 'CLI exit 7'):
            co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})

        state = json.loads(co.state_path.read_text())
        self.assertNotEqual(state['sessions']['author'], original)
        self.assertFalse(state['started']['author'])
        receipt = json.loads((co.evidence / '001-plan-author.receipt.json').read_text())
        self.assertEqual(receipt['returncode'], 7)
        self.assertNotIn('error_kind', receipt)
        self.assertTrue(receipt['invocation_budget_counted'])
        self.assertEqual(state['invocations_used'], 1)

    def test_timed_out_cli_with_rate_limit_output_remains_counted(self):
        co = self.coordinator()
        co.args.timeout = 1.0
        slow_cli = self.root / 'slow-rate-limit-cli'
        slow_cli.write_text(
            f'#!{sys.executable}\nimport sys, time\n'
            'print("429 too many requests", file=sys.stderr, flush=True)\n'
            'time.sleep(5)\n'
        )
        slow_cli.chmod(0o755)
        co.args.codex_bin = str(slow_cli)

        with self.assertRaisesRegex(RuntimeError, 'CLI exit'):
            co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})

        state = json.loads(co.state_path.read_text())
        receipt = json.loads((co.evidence / '001-plan-author.receipt.json').read_text())
        stderr_path = co.evidence / '001-plan-author.stderr.log'
        self.assertIn('429', stderr_path.read_text())
        self.assertTrue(receipt['timed_out'])
        self.assertTrue(receipt['invocation_budget_counted'])
        self.assertNotEqual(receipt.get('error_kind'), 'rate_limited')
        self.assertEqual(state['invocations_used'], 1)

    def test_timeout_race_with_positive_rate_limit_exit_remains_counted(self):
        co = self.coordinator()

        class TimeoutRaceProcess:
            pid = 12345
            returncode = None

            def communicate(self, *_args, **_kwargs):
                raise subprocess.TimeoutExpired('mock-cli', 1)

            def wait(self):
                self.returncode = 7
                return self.returncode

        real_popen = rc.subprocess.Popen

        def popen(*args, **kwargs):
            if not hasattr(kwargs.get('stderr'), 'write'):
                return real_popen(*args, **kwargs)
            kwargs['stderr'].write(b'429 too many requests\n')
            return TimeoutRaceProcess()

        with patch.object(rc.subprocess, 'Popen', side_effect=popen):
            with patch.object(rc.os, 'killpg'):
                with patch.object(rc, 'classify_rate_limit_failure') as classifier:
                    with self.assertRaisesRegex(RuntimeError, 'CLI exit 7'):
                        co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})

        classifier.assert_not_called()
        receipt = json.loads((co.evidence / '001-plan-author.receipt.json').read_text())
        self.assertEqual(receipt['returncode'], 7)
        self.assertTrue(receipt['timed_out'])
        self.assertTrue(receipt['invocation_budget_counted'])
        self.assertNotEqual(receipt.get('error_kind'), 'rate_limited')
        self.assertIn('429', (co.evidence / '001-plan-author.stderr.log').read_text())
        self.assertEqual(co.state['invocations_used'], 1)

    def test_sigkilled_cli_with_rate_limit_output_remains_counted(self):
        co = self.coordinator()
        killed_cli = self.root / 'sigkill-rate-limit-cli'
        killed_cli.write_text(
            f'#!{sys.executable}\nimport os, signal, sys\n'
            'print("429 too many requests", file=sys.stderr, flush=True)\n'
            'os.kill(os.getpid(), signal.SIGKILL)\n'
        )
        killed_cli.chmod(0o755)
        co.args.codex_bin = str(killed_cli)

        with self.assertRaisesRegex(RuntimeError, 'CLI exit -9'):
            co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})

        state = json.loads(co.state_path.read_text())
        receipt = json.loads((co.evidence / '001-plan-author.receipt.json').read_text())
        self.assertIn('429', (co.evidence / '001-plan-author.stderr.log').read_text())
        self.assertEqual(receipt['returncode'], -signal.SIGKILL)
        self.assertTrue(receipt['invocation_budget_counted'])
        self.assertNotEqual(receipt.get('error_kind'), 'rate_limited')
        self.assertEqual(state['invocations_used'], 1)

    def test_exec_turn_timeout_default_tracks_general_timeout_with_cap(self):
        for index, (general, expected) in enumerate(((31, 7200), (9000, 9000), (20000, 14400))):
            with self.subTest(general=general):
                self.run_dir = self.root / f'exec-default-{index}'
                co = self.coordinator('--timeout', str(general))
                self.assertEqual(co.args.timeout, general)
                self.assertEqual(co.args.exec_turn_timeout, expected)
                self.assertEqual(co.state['config']['exec_turn_timeout'], expected)

    def test_exec_turn_timeout_defaults_to_7200_without_changing_general_timeout(self):
        co = self.coordinator('--timeout', '31')
        self.assertEqual(co.args.timeout, 31)
        self.assertEqual(co.args.exec_turn_timeout, rc.DEFAULT_EXEC_TURN_TIMEOUT_SECONDS)
        self.assertEqual(co.state['config']['exec_turn_timeout'], 7200)

    def test_exec_author_uses_exec_timeout_but_plan_and_reviewer_use_general_timeout(self):
        co = self.coordinator('--timeout', '31', '--exec-turn-timeout', '45')
        co.state['started']['gate'] = False
        co.state['sessions']['gate'] = 'gate-session'
        for role, phase, expected in (('author', 'EXEC', 45), ('author', 'PLAN', 31),
                                      ('reviewer', 'PLAN', 31), ('gate', 'EXEC', 31)):
            with self.subTest(role=role, phase=phase):
                co._invoke_once(role, phase, 'Role prompt.', {})
                receipt = json.loads((co.evidence /
                    f"{co.state['sequence']:03d}-{phase.lower()}-{role}.receipt.json").read_text())
                self.assertEqual(receipt['timeout_seconds'], expected)

    def test_resume_can_raise_exec_timeout_within_cap_and_persist_it(self):
        co = self.coordinator('--timeout', '10')
        co._invoke_once('author', 'EXEC', 'Role prompt.', {})
        receipt_path = co.evidence / '001-exec-author.receipt.json'
        args = rc.parser().parse_args(['resume', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir), '--timeout', '10',
            '--exec-turn-timeout', str(rc.MAX_EXEC_TURN_TIMEOUT_SECONDS),
            '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli())])
        resumed = rc.Coordinator(args)
        self.assertEqual(resumed.args.exec_turn_timeout, rc.MAX_EXEC_TURN_TIMEOUT_SECONDS)
        with patch.object(resumed, 'drive', return_value='HOLD'):
            self.assertEqual(resumed.resume(), 'HOLD')
        saved = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(saved['config']['exec_turn_timeout'], rc.MAX_EXEC_TURN_TIMEOUT_SECONDS)
        self.assertEqual(json.loads(receipt_path.read_text())['timeout_seconds'], 7200)

    def test_exec_timeout_above_cap_is_rejected(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--exec-turn-timeout', str(rc.MAX_EXEC_TURN_TIMEOUT_SECONDS + 1)])
        with self.assertRaisesRegex(ValueError, 'maximum|between 1 and 14400'):
            rc.Coordinator(args)

    def test_resume_exec_timeout_above_cap_is_rejected(self):
        self.coordinator()
        args = rc.parser().parse_args(['resume', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--exec-turn-timeout', str(rc.MAX_EXEC_TURN_TIMEOUT_SECONDS + 1)])
        with self.assertRaisesRegex(ValueError, '--exec-turn-timeout must be between'):
            rc.Coordinator(args)

    def test_exec_timeout_resume_rejects_lower_and_preserves_saved_value(self):
        co = self.coordinator('--exec-turn-timeout', '9000')
        base = ['resume', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                '--run-dir', str(self.run_dir), '--timeout', '2700', '--skip-probe',
                '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli())]
        resumed = rc.Coordinator(rc.parser().parse_args(base))
        self.assertEqual(resumed.args.exec_turn_timeout, 9000)
        with self.assertRaisesRegex(ValueError, 'cannot lower saved value'):
            rc.Coordinator(rc.parser().parse_args(base + ['--exec-turn-timeout', '8999']))

    def test_legacy_state_timeout_fallback_and_config_default_is_not_cli_raise(self):
        co = self.coordinator('--timeout', '20000')
        state = json.loads(co.state_path.read_text())
        state['config'].pop('exec_turn_timeout')
        co.state_path.write_text(json.dumps(state))
        config_dir = self.workspace / '.review-loop'
        config_dir.mkdir(exist_ok=True)
        (config_dir / 'paired-session.json').write_text(json.dumps({'exec_turn_timeout': 9000}))
        argv = ['resume', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                '--run-dir', str(self.run_dir), '--timeout', '20000', '--skip-probe',
                '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli())]
        resumed = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))
        self.assertEqual(resumed.args.exec_turn_timeout, 14400)
        with patch.object(resumed, 'drive', return_value='HOLD'):
            self.assertEqual(resumed.resume(), 'HOLD')
        self.assertEqual(json.loads(resumed.state_path.read_text())['config']['exec_turn_timeout'], 14400)

    def test_legacy_reject_cannot_override_exec_timeout_from_cli_or_project_config(self):
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      '--polish-round', 'off')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        state['config'].pop('exec_turn_timeout')
        state['config']['timeout'] = 9000
        (self.run_dir / 'state.json').write_text(json.dumps(state))
        config_dir = self.workspace / '.review-loop'
        config_dir.mkdir(exist_ok=True)
        (config_dir / 'paired-session.json').write_text(json.dumps({'exec_turn_timeout': 20000}))
        refused = self.run_operator_action('reject', '--text', 'Adjust the output.',
                                           '--exec-turn-timeout', '99999')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('stale', refused.stdout)
        (config_dir / 'paired-session.json').unlink()
        rejected = self.run_operator_action('reject', '--text', 'Adjust the output.',
                                            '--exec-turn-timeout', '99999')
        self.assertEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        saved = json.loads((self.run_dir / 'state.json').read_text())
        author_execs = [row for row in saved['turns']
                        if row.get('role') == 'author' and row.get('phase') == 'EXEC' and
                        row.get('rejection_id') == 'R001']
        self.assertTrue(author_execs, rejected.stdout + rejected.stderr)
        self.assertEqual(author_execs[0]['timeout_seconds'], 9000)

    def test_reject_ignores_project_exec_timeout_on_approved_config_tree(self):
        config_dir = self.workspace / '.review-loop'
        config_dir.mkdir(exist_ok=True)
        config = config_dir / 'paired-session.json'
        config.write_text(json.dumps({'exec_turn_timeout': 12000}))
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        path = self.run_dir / 'state.json'
        state = json.loads(path.read_text())
        state['config'].pop('exec_turn_timeout')
        state['config']['timeout'] = 9000
        path.write_text(json.dumps(state))
        rejected = self.run_operator_action('reject', '--text', 'Adjust the output.',
                                            '--exec-turn-timeout', '99999')
        self.assertEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        saved = json.loads(path.read_text())
        authors = [r for r in saved['turns'] if r.get('rejection_id') == 'R001'
                   and r.get('role') == 'author' and r.get('phase') == 'EXEC']
        self.assertTrue(authors, rejected.stdout + rejected.stderr)
        self.assertEqual(authors[0]['timeout_seconds'], 9000)
        self.assertEqual(json.loads(config.read_text())['exec_turn_timeout'], 12000)

    def test_legacy_resume_allows_only_bounded_exec_timeout_raise(self):
        co = self.coordinator('--timeout', '31')
        state = json.loads(co.state_path.read_text())
        state['config'].pop('exec_turn_timeout')
        co.state_path.write_text(json.dumps(state))
        argv = ['resume', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                '--run-dir', str(self.run_dir), '--timeout', '31', '--skip-probe',
                '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli())]
        raised = rc.Coordinator(rc.parser().parse_args(argv + ['--exec-turn-timeout', '8000']))
        self.assertEqual(raised.args.exec_turn_timeout, 8000)
        with self.assertRaisesRegex(ValueError, 'cannot lower saved value'):
            rc.Coordinator(rc.parser().parse_args(argv + ['--exec-turn-timeout', '7000']))

    def test_project_exec_default_does_not_raise_saved_timeout(self):
        co = self.coordinator()
        config_dir = self.workspace / '.review-loop'
        config_dir.mkdir(exist_ok=True)
        (config_dir / 'paired-session.json').write_text(json.dumps({'exec_turn_timeout': 9000}))
        argv = ['resume', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                '--run-dir', str(self.run_dir), '--timeout', '2700', '--skip-probe',
                '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli())]
        resumed = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))
        self.assertEqual(resumed.args.exec_turn_timeout, 7200)

    def test_exec_turn_timeout_rejects_zero_and_negative_values(self):
        for value in ('0', '-1'):
            with self.subTest(value=value):
                args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
                    '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
                    '--exec-turn-timeout', value])
                with self.assertRaisesRegex(ValueError, 'between 1 and 14400'):
                    rc.Coordinator(args)

    def test_old_acceptance_format_refuses_all_mutations_but_status_is_read_only(self):
        co = self.coordinator()
        for missing in ('approved_snapshot', 'rejected_digests'):
            with self.subTest(missing=missing):
                old = dict(co.state)
                old.pop(missing)
                co.state_path.write_text(json.dumps(old))
                before = co.state_path.read_bytes()
                for action, extra in [('resume', []), ('resume', ['--polish']),
                        ('resume', ['--retry-uncertain']), ('accept', []), ('reject', []),
                        ('note', []), ('note', ['--scope-change']), ('abort', [])]:
                    result = self.run_operator_action(action, *extra)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn('run was created by an older paired-session build; start a new run', result.stdout)
                    self.assertEqual(co.state_path.read_bytes(), before)
                result = self.run_operator_action('status')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), old)
                self.assertEqual(co.state_path.read_bytes(), before)

    def test_rejected_tree_rationale_and_attributed_operator_override(self):
        co = self.rejected_done_coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1', 'FAKE_AUTHOR_RATIONALE': 'why ' * 800}):
            self.assertEqual(co.resume(), 'HOLD')
        held = co.state['rejected_tree_hold']
        self.assertEqual(len(held['rationale']), 2000)
        self.assertEqual(held['rationale'], ('why ' * 800)[:2000])
        self.assertEqual(co.state['next'], 'author')
        self.assertIsNone(co.state['pending_author_result_sequence'])
        self.assertEqual(co.state['turns'][-1]['role'], 'author')
        for way in ('note', 'change the workspace', 'accept --override-rejection'):
            self.assertIn(way, co.state['hold_reason'])
        status = self.run_operator_action('status')
        self.assertEqual(json.loads(status.stdout)['rejected_tree_hold'], held)
        co.args.override_rejection = True
        co.args.reason = ''
        with self.assertRaisesRegex(ValueError, 'non-empty'):
            co.accept()
        co.args.reason = 'I inspected the author rationale and explicitly accept this exact tree.'
        co.state['uncertain_active'] = {'role': 'reviewer', 'pid': 42424242}
        with self.assertRaisesRegex(ValueError, 'override requires'):
            co.accept()
        co.state.pop('uncertain_active')
        changed = self.workspace / 'changed-after-hold.txt'
        changed.write_text('not the held tree')
        with self.assertRaisesRegex(ValueError, 'unchanged held tree'):
            co.accept()
        changed.unlink()
        co.state['status'] = 'ACTIVE'
        with self.assertRaisesRegex(ValueError, 'DONE'):
            co.accept()
        co.state['status'] = 'HOLD'
        co.save()
        result = self.run_operator_action('accept', '--override-rejection', '--reason', co.args.reason)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(co.state_path.read_text())
        record = state['acceptance']
        self.assertEqual(state['status'], 'ACCEPTED')
        self.assertTrue(record['override_rejection'])
        self.assertEqual(record['author'], 'operator')
        self.assertEqual(record['reason'], co.args.reason)
        self.assertEqual(record['intent']['uid'], os.getuid())
        self.assertEqual(record['intent']['tree_sha256'], held['tree_sha256'])
        self.assertEqual(record['rationale'], held['rationale_evidence'])
        self.assertEqual(state['events'][-1], record)
        self.assertTrue(record['timestamp'])

    def test_override_uses_both_leases_and_retains_role_run_dir_protections(self):
        co = self.rejected_done_coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1'}):
            self.assertEqual(co.resume(), 'HOLD')
        command = self.command()[2:]
        command[0] = 'accept'
        command.extend(['--override-rejection', '--reason', 'operator ruling', '--skip-probe'])
        before = co.state_path.read_bytes()
        for lease in (rc.run_lease(co.run_dir), rc.workspace_lease(co.workspace, co.run_dir)):
            with lease:
                result = self.run_operator_action('accept', '--override-rejection', '--reason', 'operator ruling')
                self.assertEqual(result.returncode, 2)
                self.assertEqual(co.state_path.read_bytes(), before)
        self.assertNotIn(str(co.run_dir), co._author_sandbox_overrides()['sandbox_workspace_write.writable_roots'])
        for role in ('author', 'reviewer'):
            self.assertIn(str(co.run_dir), co._claude_sandbox_settings(role)['sandbox']['filesystem']['denyWrite'])

    def test_uncertain_reviewer_on_rejected_tree_archives_before_operator_ruling(self):
        co = self.rejected_done_coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1'}):
            self.assertEqual(co.resume(), 'HOLD')
        co.state.update(next='reviewer', uncertain_active={
            'role': 'reviewer', 'vendor': 'claude', 'pid': 42424242, 'sequence': 99})
        co.save()
        with patch.object(rc, 'retry_killpg_eperm', side_effect=ProcessLookupError):
            with patch.object(co, 'archive_abandoned_turn') as archive, patch.object(co, 'drive') as drive:
                with self.assertRaisesRegex(ValueError, 'rejected-tree'):
                    co.resume(retry_uncertain=True)
                archive.assert_called_once()
                drive.assert_not_called()
        self.assertIsNone(co.state.get('uncertain_active'))
        self.assertEqual(co.state['next'], 'author')
        co.args.override_rejection = True
        co.args.reason = 'Explicit ruling after the child stopped.'
        self.assertEqual(co.accept(), 'ACCEPTED')

    def test_accepted_fast_path_precedes_tree_checks_in_all_resume_forms(self):
        co = self.coordinator()
        co.done()
        co.args.expect = co.operator_intent('accept', None, None)['digest']
        self.assertEqual(co.accept(), 'ACCEPTED')
        (self.workspace / 'after-accept.txt').write_text('operator owns subsequent work')
        before = co.state_path.read_bytes()
        self.assertEqual(co.resume(), 'ACCEPTED')
        self.assertEqual(co.resume(retry_uncertain=True), 'ACCEPTED')
        self.assertEqual(co.resume_polish(), 'ACCEPTED')
        self.assertEqual(co.state_path.read_bytes(), before)

    def test_active_done_stale_resume_names_tracked_and_untracked_drift(self):
        co = self.coordinator()
        co.done()
        (self.workspace / 'tracked.txt').write_text('changed')
        (self.workspace / 'new.txt').write_text('untracked')
        co.state.update(status='ACTIVE', next='reviewer')
        co.save()
        with patch.object(co, 'invoke') as invoke:
            with self.assertRaisesRegex(ValueError, '1 tracked, 1 untracked'):
                co.resume()
            invoke.assert_not_called()

    def test_done_requires_explicit_accept_and_accept_is_idempotent(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE')
        self.assertEqual(state['acceptance_state'], 'PENDING')
        report = (self.run_dir / 'review-comparison.md').read_text()
        self.assertIn('Acceptance state: **PENDING**', report)
        self.assertNotIn('Acceptance state: **ACCEPTED**', report)
        first = self.run_operator_action('accept')
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        accepted = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(accepted['status'], 'ACCEPTED')
        self.assertEqual(accepted['acceptance_state'], 'ACCEPTED')
        accepted_at = accepted['accepted_at']
        self.assertTrue(Path(accepted['acceptance']['evidence']).is_file())
        second = self.run_operator_action('accept')
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['accepted_at'], accepted_at)
        resumed = self.run_operator_action('resume', '--shadow', 'off',
                                           '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn('ACCEPTED', resumed.stdout)

    def test_operator_intent_stale_tree_refuses_accept_and_records_provenance(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        invalid = self.command('--shadow', 'off', '--adversarial-gate', 'off',
                               '--polish-round', 'off', '--skip-probe'); invalid[2] = 'abort'
        invalid.append('--intent-only')
        refused = subprocess.run(invalid, cwd=self.root, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('intent action must be accept or reject', refused.stdout)
        issued = self.issue_operator_intent('accept')
        self.assertEqual(issued.returncode, 0, issued.stdout + issued.stderr)
        missing = self.command('--skip-probe'); missing[2] = 'accept'
        refused = subprocess.run(missing, cwd=self.root, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('intent is stale or missing', refused.stdout)
        (self.workspace / 'intent-drift.txt').write_text('new workspace content')
        command = self.command('--expect', json.loads(issued.stdout)['digest'], '--skip-probe')
        command[2] = 'accept'
        refused = subprocess.run(command, cwd=self.root, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('stale', refused.stdout)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['status'], 'DONE')
        fresh = self.issue_operator_intent('accept')
        self.assertEqual(fresh.returncode, 2)
        self.assertIn('stale', fresh.stdout)
        (self.workspace / 'intent-drift.txt').unlink()
        accepted = self.run_operator_action('accept')
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        record = json.loads((self.run_dir / 'state.json').read_text())['acceptance']
        self.assertEqual(record['intent']['uid'], os.getuid())
        self.assertEqual(record['intent']['action'], 'accept')
        self.assertEqual(record['intent']['workspace'], str(self.workspace))
        self.assertEqual(record['intent']['run_id'], str(self.run_dir))
        self.assertEqual(record['intent']['tree_sha256'],
                         json.loads((self.run_dir / 'state.json').read_text())['approved_snapshot'])
        self.assertEqual(len(record['intent']['head']), 40)
        self.assertEqual(len(record['intent']['index_sha256']), 64)
        self.assertEqual(len(record['intent']['tree_sha256']), 64)

    def test_rejection_limit_polish_cannot_rebind_changed_held_tree(self):
        co = self.rejected_done_coordinator()
        co.hold('rejected-tree', terminal_kind='rejection_limit')
        held = dict(co.state['rejected_tree_hold'])
        sequence = co.state['sequence']
        (self.workspace / 'unreviewed.txt').write_text('not the rejected tree')
        polished = self.run_operator_action('resume', '--polish', '--shadow', 'off',
                                           '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(polished.returncode, 2, polished.stdout + polished.stderr)
        saved = json.loads(co.state_path.read_text())
        self.assertEqual(saved['rejected_tree_hold'], held)
        self.assertEqual(saved['sequence'], sequence)
        refused = self.run_operator_action('accept', '--override-rejection', '--reason', 'operator ruling')
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('unchanged held tree', refused.stdout)
        self.assertEqual(json.loads(co.state_path.read_text())['status'], 'HOLD')

    def test_run_cannot_review_stale_pending_done_tree(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        path = self.run_dir / 'state.json'
        before = json.loads(path.read_text())
        (self.workspace / 'after-done.txt').write_text('unreviewed workspace content')
        for role in ('reviewer', 'gate'):
            with self.subTest(role=role):
                pending = dict(before, status='ACTIVE', next=role, active=None)
                path.write_text(json.dumps(pending))
                refused = self.run_operator_action('run', '--shadow', 'off',
                                                   '--adversarial-gate', 'off', '--polish-round', 'off')
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                self.assertIn('stale:', refused.stdout)
                saved = json.loads(path.read_text())
                self.assertEqual(saved['sequence'], before['sequence'])
                self.assertEqual(saved['approved_snapshot'], before['approved_snapshot'])

    def test_resume_polish_cannot_reapprove_a_changed_done_tree(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        (self.workspace / 'after-done.txt').write_text('unreviewed content')
        resumed = self.run_operator_action('resume', '--polish', '--shadow', 'off',
                                           '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 2)
        self.assertIn('stale:', resumed.stdout)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['status'], 'DONE')
        intent = self.issue_operator_intent('accept')
        self.assertEqual(intent.returncode, 2)
        self.assertIn('stale', intent.stdout)

    def test_resume_after_stale_polish_hold_cannot_reapprove_changed_tree(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        (self.workspace / 'after-done.txt').write_text('unreviewed content')
        resumed = self.run_operator_action('resume', '--polish', '--shadow', 'off',
                                           '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 2)
        before = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(before['status'], 'DONE')
        ordinary = self.run_operator_action('resume')
        self.assertEqual(ordinary.returncode, 2)
        after = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(after['status'], 'DONE')
        self.assertEqual(after['approved_snapshot'], before['approved_snapshot'])
        self.assertEqual(after['sequence'], before['sequence'])

    def test_polish_hold_after_author_write_remains_resumable(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        flags = ('--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')
        co = rc.Coordinator(rc.parser().parse_args(self.command(*flags)[2:]))
        (self.workspace / 'polish-output.txt').write_text('polish author output')
        co.state.update(status='HOLD', next='reviewer', hold_reason='polish reviewer failed')
        co.state['polish'].update(active=True, completed=False)
        co.save()
        with patch.object(co, 'drive', return_value='HOLD') as drive:
            self.assertEqual(co.resume(), 'HOLD')
        drive.assert_called_once_with()

    def test_abort_after_changed_done_tree_cannot_reapprove_it(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        approved = json.loads((self.run_dir / 'state.json').read_text())['approved_snapshot']
        (self.workspace / 'after-done.txt').write_text('unreviewed content')
        flags = ('--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')
        aborted = self.run_operator_action('abort', *flags)
        self.assertEqual(aborted.returncode, 2)
        resumed = self.run_operator_action('resume', *flags)
        self.assertEqual(resumed.returncode, 2)
        self.assertIn('stale', resumed.stdout)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['approved_snapshot'], approved)

    def test_reject_intent_requires_the_approved_snapshot(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        (self.workspace / 'after-done.txt').write_text('unreviewed content')
        intent = self.issue_operator_intent('reject', '--text', 'please revise')
        self.assertEqual(intent.returncode, 2)
        self.assertIn('stale', intent.stdout)

    def test_rejected_author_failure_can_resume_author_without_reviewing_same_tree(self):
        co = self.rejected_done_coordinator()
        co.state.update(status='HOLD', next='author', hold_reason='author dispatch failed')
        co.save()
        with patch.object(co, 'author_turn', side_effect=RuntimeError('author retry failed')) as author:
            self.assertEqual(co.resume(), 'HOLD')
        author.assert_called_once_with()
        self.assertEqual(co.state['next'], 'author')
        self.assertEqual(co.state['hold_reason'], 'author retry failed')

    def test_changed_done_tree_cannot_be_accepted_after_resume(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        before = json.loads((self.run_dir / 'state.json').read_text())
        (self.workspace / 'after-done.txt').write_text('unreviewed content')
        resumed = self.run_operator_action('resume', '--shadow', 'off',
                                           '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 2)
        after = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(after['status'], 'DONE')
        self.assertEqual(after['approved_snapshot'], before['approved_snapshot'])
        self.assertEqual(after['sequence'], before['sequence'])
        intent = self.issue_operator_intent('accept')
        self.assertEqual(intent.returncode, 2)
        self.assertIn('stale', intent.stdout)

    def rejected_done_coordinator(self):
        (self.workspace / 'sum_ints.py').write_text(
            'def sum_ints(values):\n    if not all(type(x) is int for x in values):\n'
            '        raise TypeError("ints only")\n    return sum(values)\n')
        co = self.coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                              '--polish-round', 'off')
        digest, snapshot = rc.git_snapshot(co.workspace)
        co.state.update(status='DONE', acceptance_state='PENDING',
                        approved_snapshot=digest, rejections=[])
        co.save()
        proof = co.operator_intent('reject', 'please revise', None)
        co.args.expect = proof['digest']
        self.assertEqual(co.reject('please revise', None), 'ACTIVE')
        saved = json.loads(co.state_path.read_text())['rejections'][0]['intent']
        self.assertEqual(saved['tree_sha256'], digest)
        self.assertEqual(saved['tree_snapshot'], snapshot)
        return co

    def test_rejected_tree_blocks_plain_resume(self):
        co = self.rejected_done_coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1'}):
            self.assertEqual(co.resume(), 'HOLD')
        state = json.loads(co.state_path.read_text())
        self.assertIn('rejected-tree', state['hold_reason'])
        self.assertEqual(state['turns'][-1]['role'], 'author')

    def test_rejected_same_tree_hold_can_retry_author_ingest(self):
        co = self.rejected_done_coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1'}):
            self.assertEqual(co.resume(), 'HOLD')
        before = len(co.state['turns'])
        with patch.dict(os.environ, {'FAKE_AUTHOR_WRITE_NEW_REJECTION_TREE': '1'}):
            self.assertEqual(co.resume(), 'DONE')
        new_author = next(row for row in co.state['turns'][before:] if row['role'] == 'author')
        prompt = (co.evidence / f"{new_author['sequence']:03d}-exec-author.prompt.txt").read_text()
        self.assertIn('## Operator rejection for current EXEC scope', prompt)
        self.assertTrue(co.state['turns'][before]['snapshot_after'] !=
                        co.state['rejections'][0]['intent']['tree_sha256'])

    def test_note_and_same_tree_hold_allow_a_fresh_rejected_author_ingest(self):
        co = self.rejected_done_coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1'}):
            self.assertEqual(co.resume(), 'HOLD')
        note = self.run_operator_action('note', '--text', 'Please recheck the rejection.',
                                        '--shadow', 'off', '--adversarial-gate', 'off',
                                        '--polish-round', 'off')
        self.assertEqual(note.returncode, 0, note.stdout + note.stderr)
        co = self.coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                              '--polish-round', 'off')
        with patch.dict(os.environ, {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1'}):
            self.assertEqual(co.resume(), 'HOLD')
        state = json.loads(co.state_path.read_text())
        self.assertIsNone(state['pending_author_result_sequence'])
        self.assertEqual(state['operator_notes'][0]['status'], 'delivered')
        before = len(state['turns'])
        self.assertEqual(co.resume(), 'DONE')
        self.assertGreater(len(co.state['turns']), before)

    def test_legacy_done_without_approved_snapshot_can_resume_and_accept(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        path = self.run_dir / 'state.json'
        state = json.loads(path.read_text())
        state.pop('approved_snapshot')
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        for action in ('resume', 'accept', 'reject', 'note'):
            with self.subTest(action=action):
                result = self.run_operator_action(action)
                self.assertEqual(result.returncode, 2)
                self.assertIn('run was created by an older paired-session build; start a new run', result.stdout)
                self.assertEqual(path.read_bytes(), before)

    def test_legacy_done_abort_hold_can_resume(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        path = self.run_dir / 'state.json'
        state = json.loads(path.read_text())
        state.pop('approved_snapshot')
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        for action in ('resume', 'accept', 'reject', 'note'):
            with self.subTest(action=action):
                result = self.run_operator_action(action)
                self.assertEqual(result.returncode, 2)
                self.assertIn('run was created by an older paired-session build; start a new run', result.stdout)
                self.assertEqual(path.read_bytes(), before)

    def test_done_abort_author_write_then_hold_remains_resumable(self):
        completed = self.run_coordinator('--shadow', 'off', '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        co = self.coordinator('--shadow', 'off', '--polish-round', 'off', '--timeout', '10',
                              '--author-effort', 'low', '--reviewer-effort', 'low',
                              '--gate-effort', 'low', '--test-command', 'python3 -m unittest')
        co.state.update(status='HOLD', acceptance_state='PENDING',
                        hold_reason='aborted by operator', next='author')
        co.save()
        with patch.dict(os.environ, {'FAKE_AUTHOR_WRITE_NEW_TREE': '1'}):
            with patch.object(co, 'reviewer_turn', side_effect=RuntimeError('reviewer interrupted')):
                self.assertEqual(co.resume(), 'HOLD')
        self.assertEqual(co.state['acceptance_state'], 'IN_PROGRESS')
        self.assertNotEqual(rc.git_snapshot(self.workspace)[0], co.state['approved_snapshot'])
        self.assertEqual(co.resume(), 'DONE')

    def test_done_abort_author_hold_after_write_remains_resumable(self):
        completed = self.run_coordinator('--shadow', 'off', '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        co = self.coordinator('--shadow', 'off', '--polish-round', 'off', '--timeout', '10',
                              '--author-effort', 'low', '--reviewer-effort', 'low',
                              '--gate-effort', 'low', '--test-command', 'python3 -m unittest')
        co.state.update(status='HOLD', acceptance_state='PENDING',
                        hold_reason='aborted by operator', next='author')
        co.save()
        with patch.dict(os.environ, {'FAKE_AUTHOR_WRITE_NEW_TREE': '1',
                                     'FAKE_AUTHOR_HOLD_AFTER_WRITE': '1'}):
            self.assertEqual(co.resume(), 'HOLD')
        self.assertEqual(co.state['acceptance_state'], 'IN_PROGRESS')
        self.assertNotEqual(rc.git_snapshot(self.workspace)[0], co.state['approved_snapshot'])
        self.assertEqual(co.resume(), 'DONE')

    def test_legacy_partial_author_tree_can_resume_as_author(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        path = self.run_dir / 'state.json'
        state = json.loads(path.read_text())
        state.pop('approved_snapshot')
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        for action in ('resume', 'accept', 'reject', 'note'):
            with self.subTest(action=action):
                result = self.run_operator_action(action)
                self.assertEqual(result.returncode, 2)
                self.assertIn('run was created by an older paired-session build; start a new run', result.stdout)
                self.assertEqual(path.read_bytes(), before)

    def test_legacy_hold_skips_final_error_receipt_during_snapshot_recovery(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        path = self.run_dir / 'state.json'
        state = json.loads(path.read_text())
        state.pop('approved_snapshot')
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        for action in ('resume', 'accept', 'reject', 'note'):
            with self.subTest(action=action):
                result = self.run_operator_action(action)
                self.assertEqual(result.returncode, 2)
                self.assertIn('run was created by an older paired-session build; start a new run', result.stdout)
                self.assertEqual(path.read_bytes(), before)

    def test_legacy_rejection_limit_hold_can_accept_without_snapshot(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        path = self.run_dir / 'state.json'
        state = json.loads(path.read_text())
        state.pop('approved_snapshot')
        path.write_text(json.dumps(state))
        before = path.read_bytes()
        for action in ('resume', 'accept', 'reject', 'note'):
            with self.subTest(action=action):
                result = self.run_operator_action(action)
                self.assertEqual(result.returncode, 2)
                self.assertIn('run was created by an older paired-session build; start a new run', result.stdout)
                self.assertEqual(path.read_bytes(), before)

    def test_rejected_author_crash_can_retry_uncertain_author(self):
        co = self.rejected_done_coordinator()
        co.state.update(status='HOLD', next='author', hold_reason='uncertain author turn',
                        uncertain_active={'role': 'author', 'vendor': 'codex',
                                          'pid': 42424242, 'sequence': 99})
        co.save()
        with patch.object(rc, 'retry_killpg_eperm', side_effect=ProcessLookupError):
            with patch.object(co, 'archive_abandoned_turn') as archive:
                with patch.object(co, 'drive', return_value='ACTIVE') as drive:
                    self.assertEqual(co.resume(retry_uncertain=True), 'ACTIVE')
        archive.assert_called_once()
        drive.assert_called_once_with()

    def test_rejected_author_crash_can_retry_directly(self):
        co = self.rejected_done_coordinator()
        co.state.update(status='ACTIVE', next='author', active={'role': 'author',
                        'vendor': 'codex', 'pid': 42424242, 'sequence': 99}, uncertain_active=None)
        co.save()
        with patch.object(rc, 'retry_killpg_eperm', side_effect=ProcessLookupError):
            with patch.object(co, 'archive_abandoned_turn') as archive:
                self.assertEqual(co.resume(retry_uncertain=True), 'DONE')
        archive.assert_called_once()
        self.assertEqual(co.state['turns'][-1]['role'], 'gate')
        self.assertEqual(co.state['turns'][-2]['role'], 'reviewer')

    def test_rejected_tree_blocks_resume_polish(self):
        co = self.rejected_done_coordinator()
        with self.assertRaisesRegex(ValueError, 'rejected'):
            co.resume_polish()

    def test_rejected_tree_blocks_retry_uncertain(self):
        co = self.rejected_done_coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1'}):
            self.assertEqual(co.resume(retry_uncertain=True), 'HOLD')
        self.assertEqual(co.state['turns'][-1]['role'], 'author')
        self.assertIn('rejected-tree', co.state['hold_reason'])

    def test_rejected_tree_blocks_accept_and_new_accept_intent(self):
        co = self.rejected_done_coordinator()
        co.state['status'] = 'DONE'
        co.save()
        with self.assertRaisesRegex(ValueError, 'rejected'):
            co.accept()
        with self.assertRaisesRegex(ValueError, 'rejected'):
            co.operator_intent('accept', None, None)

    def test_new_author_tree_differs_from_rejected_tree(self):
        co = self.rejected_done_coordinator()
        (self.workspace / 'new-author-output.txt').write_text('new ingest')
        self.assertFalse(co.rejected_tree())

    def test_operator_intent_records_reject_payload_and_sandbox_cannot_write_run_dir(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        source = self.root / 'operator-feedback.md'
        source.write_text('Recheck the accepted OID.\n')
        rejected = self.run_operator_action('reject', '--file', str(source),
                                             env={'FAKE_AUTHOR_WRITE_NEW_REJECTION_TREE': '1'})
        self.assertEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        record = json.loads((self.run_dir / 'state.json').read_text())['rejections'][0]
        self.assertEqual(record['intent']['uid'], os.getuid())
        self.assertEqual(record['intent']['action'], 'reject')
        self.assertEqual(record['source'], str(source.resolve()))
        self.assertEqual(record['intent']['payload_sha256'], record['sha256'])
        self.run_dir = self.root / 'operator-sandbox-policy-run'
        co = self.coordinator('--author-effort', 'low', '--reviewer-effort', 'low',
                              '--gate-effort', 'low', '--test-command', 'python3 -m unittest')
        self.assertNotIn(str(self.run_dir), co._author_sandbox_overrides()['sandbox_workspace_write.writable_roots'])
        for role in ('author', 'reviewer'):
            self.assertIn(str(self.run_dir),
                          co._claude_sandbox_settings(role)['sandbox']['filesystem']['denyWrite'])

    def test_operator_intent_requires_an_existing_matching_run(self):
        result = self.issue_operator_intent('accept')
        self.assertEqual(result.returncode, 2)
        self.assertIn('requires an existing coordinator run', result.stdout)
        self.assertFalse((self.run_dir / 'state.json').exists())

    def test_reject_reopens_exec_through_review_and_gate_before_accept(self):
        completed = self.run_coordinator('--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        before = json.loads((self.run_dir / 'state.json').read_text())
        sequence_before = before['sequence']
        feedback = 'Please handle the in-scope edge case before acceptance.'
        rejected = self.run_operator_action('reject', '--text', feedback, '--polish-round', 'off',
                                             env={'FAKE_AUTHOR_WRITE_NEW_REJECTION_TREE': '1'})
        self.assertEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE')
        self.assertEqual(state['acceptance_state'], 'PENDING')
        self.assertEqual(len(state['rejections']), 1)
        self.assertEqual(state['rejections'][0]['status'], 'delivered')
        self.assertEqual(state['rejections'][0]['author'], 'operator')
        recent = [turn for turn in state['turns'] if turn['sequence'] > sequence_before]
        self.assertEqual([turn['role'] for turn in recent], ['author', 'reviewer', 'shadow', 'gate'])
        author = recent[0]
        receipt = json.loads((self.run_dir / 'evidence' /
                              f"{author['sequence']:03d}-exec-author.receipt.json").read_text())
        self.assertEqual(receipt['rejection_id'], state['rejections'][0]['id'])
        self.assertEqual(receipt['rejection_sha256'], state['rejections'][0]['sha256'])
        self.assertIn(feedback, (self.run_dir / 'evidence' /
                     f"{author['sequence']:03d}-exec-author.prompt.txt").read_text())
        self.assertGreater(state['exec_reviews'], before['exec_reviews'])
        self.assertTrue(state['gate_ran'])
        self.assertIn('gate', state['exec_comparisons'][-1])
        accepted = self.run_operator_action('accept')
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['status'], 'ACCEPTED')

    def test_reject_limit_holds_and_reject_requires_done(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        path = self.run_dir / 'state.json'
        state = json.loads(path.read_text())
        state.update(max_rejections=1, rejections=[{'id': 'R001'}], post_done_rejections=1)
        path.write_text(json.dumps(state))
        rejected = self.run_operator_action('reject', '--text', 'one more change')
        self.assertEqual(rejected.returncode, 2)
        held = json.loads(path.read_text())
        self.assertEqual(held['status'], 'HOLD')
        self.assertIn('post-DONE rejection limit reached', held['hold_reason'])
        self.assertEqual(held['terminal_hold_kind'], 'rejection_limit')
        self.assertIn('accept or abort', held['hold_reason'])
        args = rc.parser().parse_args(['accept', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        with patch.object(co, 'invoke') as invoke, patch.object(co, 'drive') as drive:
            self.assertEqual(co.resume(), 'HOLD')
            invoke.assert_not_called()
            drive.assert_not_called()
        resumed = self.run_operator_action('resume', '--shadow', 'off',
                                           '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 2)
        self.assertIn('accept or abort', resumed.stdout)
        self.assertEqual(json.loads(path.read_text())['sequence'], held['sequence'])
        aborted = self.run_operator_action('abort')
        self.assertEqual(aborted.returncode, 2)
        self.assertEqual(json.loads(path.read_text())['terminal_hold_kind'], 'rejection_limit')
        paused = json.loads(path.read_text())
        paused['hold_reason'] = 'permission probe passed; run resume to continue'
        path.write_text(json.dumps(paused))
        resumed = self.run_operator_action('resume', '--shadow', 'off',
                                           '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 2)
        self.assertIn('accept or abort', resumed.stdout)
        self.assertEqual(json.loads(path.read_text())['sequence'], held['sequence'])
        polished = self.run_operator_action('resume', '--polish', '--shadow', 'off',
                                            '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(polished.returncode, 2)
        self.assertIn('accept or abort', polished.stdout)
        accepted = self.run_operator_action('accept')
        self.assertEqual(accepted.returncode, 2, accepted.stdout + accepted.stderr)
        accepted = self.run_operator_action('accept', '--override-rejection', '--reason', 'Reviewed author rationale.')
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertEqual(json.loads(path.read_text())['status'], 'ACCEPTED')
        self.run_dir = self.root / 'active-non-done-run'
        active = self.coordinator()
        with self.assertRaisesRegex(ValueError, 'reject requires a DONE run'):
            active.reject('not done', None)

    def test_only_rejection_limit_hold_blocks_resume(self):
        co = self.coordinator()
        co.hold('ordinary operator pause')
        self.assertNotIn('terminal_hold_kind', co.state)
        with patch.object(co, 'drive', return_value='ACTIVE') as drive:
            self.assertEqual(co.resume(), 'ACTIVE')
            drive.assert_called_once_with()
        co.hold('post-DONE rejection limit reached (2)')
        self.assertNotIn('terminal_hold_kind', co.state)
        with patch.object(co, 'drive', return_value='ACTIVE') as drive:
            self.assertEqual(co.resume(), 'ACTIVE')
            drive.assert_called_once_with()

    def test_successor_probe_binds_parent_base_task_and_single_child(self):
        old_dir = self.root / 'superseded-run'
        self.run_dir = old_dir
        parent = self.coordinator()
        parent_base = parent.state['base_commit']
        task_file = parent.evidence / 'successor-workitem.md'
        task_file.write_text(self.workitem.read_text() + '\nNew scope.\n')
        config_path = parent.evidence / 'successor-config.json'
        rc.atomic_json(config_path, {'test_command': 'python3 -m unittest'})
        child_dir = self.root / 'successor-run'
        spec = {'run_dir': str(child_dir), 'workspace': str(self.workspace.resolve()),
                'original_workitem': str(self.workitem.resolve()),
                'original_hash': rc.hashlib.sha256(self.workitem.read_bytes()).hexdigest(),
                'task_sha256': rc.hashlib.sha256(task_file.read_bytes()).hexdigest(),
                'base_commit': parent_base,
                'config_sha256': rc.hashlib.sha256(config_path.read_bytes()).hexdigest()}
        spec_path = parent.evidence / 'successor-spec.json'
        rc.atomic_json(spec_path, spec)
        parent.state.update(status='ABORTED', abort_kind='scope-change',
                            successor_spec_sha256=rc.hashlib.sha256(spec_path.read_bytes()).hexdigest())
        parent.save()
        self.assertEqual(parent.hold('late abort'), 'ABORTED')
        self.assertEqual(parent.state['status'], 'ABORTED')
        self.run_dir, self.workitem = child_dir, task_file
        saved = json.loads(parent.state_path.read_text())
        saved.pop('base_commit')
        parent.state_path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, 'successor spec or parent state differs'):
            self.coordinator('--supersedes', str(old_dir))
        saved['base_commit'] = parent_base
        parent.state_path.write_text(json.dumps(saved))
        child = self.coordinator('--supersedes', str(old_dir))
        self.assertEqual(child.state['base_commit'], parent_base)
        self.assertEqual(child.state['scope_chain_depth'], 1)
        self.assertEqual(child.state['supersedes'], str(old_dir))
        self.assertEqual(json.loads((parent.evidence / 'successor-claim.json').read_text())['run_dir'], str(child_dir))
        with self.assertRaisesRegex(ValueError, 'saved supersedes'):
            self.coordinator()
        task_file.write_text(task_file.read_text() + 'tampered')
        with self.assertRaisesRegex(ValueError, 'effective task hash differs'):
            self.coordinator('--supersedes', str(old_dir))

    def test_scope_change_method_aborts_and_preserves_successor_spec(self):
        co = self.coordinator()
        co.args.action = 'note'
        co.args.scope_change = True
        with self.assertRaisesRegex(ValueError, 'fresh-role input scan'):
            co.scope_change('Claude reviewer said REVISE', None)
        self.assertNotIn('scope_change_intent', co.state)
        command = co.scope_change('Also handle negative values.', None)
        self.assertIn('Probe: ', command)
        self.assertIn('Start: ', command)
        self.assertEqual(command.count('--supersedes'), 2)
        self.assertNotIn('--skip-probe', command)
        saved = json.loads(co.state_path.read_text())
        self.assertEqual(saved['status'], 'ABORTED')
        self.assertEqual(saved['abort_kind'], 'scope-change')
        self.assertEqual(saved['scope_change_intent']['author'], 'operator')
        spec_path = co.evidence / 'successor-spec.json'
        spec_before = spec_path.read_bytes()
        spec = json.loads(spec_before)
        self.assertEqual(spec['base_commit'], saved['base_commit'])
        self.assertIn('Also handle negative values.', spec['task'])
        self.assertEqual(co.scope_change('Also handle negative values.', None), command)
        self.assertEqual(spec_path.read_bytes(), spec_before)
        self.assertEqual(co.hold('late abort'), 'ABORTED')
        self.assertIn('Superseded run', (co.run_dir / 'scope-change-report.md').read_text())

    def test_item_uuid_and_blockers_survive_scope_change_without_fresh_role_leak(self):
        parent = self.coordinator()
        item_uuid = parent.state['item_uuid']
        parent.state['finding_ledger'] = [
            {'id': 'F001', 'status': 'open', 'severity': 'MAJOR', 'source': 'persistent-reviewer',
             'body': 'private prior finding text', 'security': False},
            {'id': 'F002', 'status': 'open', 'severity': 'LOW', 'source': 'security-reviewer',
             'body': 'security blocker', 'security': True},
            {'id': 'F003', 'status': 'open', 'severity': 'MINOR', 'source': 'persistent-reviewer',
             'body': 'advisory only', 'security': False},
            {'id': 'F004', 'status': 'closed', 'severity': 'CRITICAL', 'source': 'shadow',
             'body': 'already closed', 'security': False},
            {'id': 'F005', 'status': 'awaiting-revalidation', 'severity': 'MEDIUM', 'source': 'adversarial-gate',
             'body': 'gate rubric needs owner', 'security': False}]
        for row in parent.state['finding_ledger']:
            row.update(file='source.py', summary=row['body'], phase='EXEC', status_history=[])
        parent.save(); parent.args.action = 'note'; parent.args.scope_change = True
        parent.scope_change('Expand scope.', None)
        spec_path = parent.evidence / 'successor-spec.json'
        spec = json.loads(spec_path.read_text())
        self.assertEqual(spec['item_uuid'], item_uuid)
        self.assertEqual([row['id'] for row in spec['item_blockers']], ['F001', 'F002', 'F005'])
        self.run_dir = parent.run_dir.with_name(parent.run_dir.name + '-successor')
        self.workitem = parent.evidence / 'successor-workitem.md'
        child = self.coordinator('--supersedes', str(parent.run_dir))
        self.assertEqual(child.state['item_uuid'], item_uuid)
        self.assertEqual(child.state['item_blockers'], spec['item_blockers'])
        self.assertTrue(child.state['item_blockers_complete'])
        self.assertEqual(child.state['finding_ledger'], [])
        self.assertNotIn('private prior finding text', child.open_findings_prompt())
        child.state['item_blockers'][0]['body'] = 'tampered'; child.save()
        with self.assertRaisesRegex(ValueError, 'saved successor item identity or blockers differ'):
            self.coordinator('--supersedes', str(parent.run_dir))

    def test_legacy_successor_without_item_fields_is_marked_unverified(self):
        self.run_dir = self.root / 'legacy-parent'
        parent = self.coordinator(); parent.args.action = 'note'; parent.args.scope_change = True
        parent.scope_change('New scope.', None)
        spec_path = parent.evidence / 'successor-spec.json'
        spec = json.loads(spec_path.read_text())
        spec.pop('item_uuid'); spec.pop('item_blockers')
        rc.atomic_json(spec_path, spec)
        parent.state['successor_spec_sha256'] = rc.hashlib.sha256(spec_path.read_bytes()).hexdigest()
        parent.save()
        self.run_dir = parent.run_dir.with_name(parent.run_dir.name + '-successor')
        self.workitem = parent.evidence / 'successor-workitem.md'
        child = self.coordinator('--supersedes', str(parent.run_dir))
        self.assertEqual(child.state['item_uuid'], parent.state['item_uuid'])
        self.assertFalse(child.state['item_blockers_complete'])

    def test_hold_note_storage_replaces_pending_and_refuses_other_roles(self):
        co = self.coordinator()
        co.hold('operator pause')
        with patch.object(co, 'invoke') as invoke:
            first = co.note('First clarification.', None)
            second = co.note('New clarification.', None)
            invoke.assert_not_called()
        self.assertEqual((first, second), ('N001', 'N002'))
        notes = co.state['operator_notes']
        self.assertEqual(notes[0]['status'], 'replaced')
        self.assertEqual(notes[0]['replaced_by'], second)
        self.assertEqual(notes[1]['replaces_sha256'], notes[0]['sha256'])
        self.assertEqual(co.state['pending_operator_note_id'], second)
        self.assertNotIn('New clarification.', co.state_path.read_text())
        self.assertEqual(Path(notes[1]['evidence']).read_text(), 'New clarification.')
        co.state['next'] = 'reviewer'
        with self.assertRaisesRegex(ValueError, 'waiting for reviewer'):
            co.note('not now', None)
        co.state['next'] = 'author'
        co.state['terminal_hold_kind'] = 'rejection_limit'
        with self.assertRaisesRegex(ValueError, 'rejection limit'):
            co.note('not now', None)
        co.state.pop('terminal_hold_kind')
        with self.assertRaisesRegex(ValueError, 'outside workspace'):
            co.note(None, str(self.workspace / 'tracked.txt'))
        source = self.root / 'operator-note-source.txt'
        source.write_text('Read external note.')
        third = co.note(None, str(source))
        self.assertEqual(Path(co.state['operator_notes'][-1]['evidence']).read_text(), source.read_text())
        self.assertNotIn(str(source), co.state_path.read_text())
        self.assertEqual(third, 'N003')

    def test_hold_note_cli_delivers_once_and_excludes_fresh_roles(self):
        stopped = self.run_coordinator('--stop-after-plan', '--polish-round', 'off')
        self.assertEqual(stopped.returncode, 2, stopped.stdout + stopped.stderr)
        held = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((held['status'], held['phase'], held['next']), ('HOLD', 'EXEC', 'author'))
        first = self.run_operator_action('note', '--text', 'Use a wider negative input.')
        second = self.run_operator_action('note', '--text', 'Use the negative input case.')
        self.assertEqual((first.returncode, second.returncode), (0, 0), first.stdout + second.stdout)
        probe = self.run_operator_action('permission-probe', '--polish-round', 'off')
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['pending_operator_note_id'], 'N002')
        resumed = self.run_operator_action('resume', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE')
        self.assertNotIn('pending_operator_note_id', state)
        self.assertEqual([row['status'] for row in state['operator_notes']], ['replaced', 'delivered'])
        prompts = [p.read_text() for p in (self.run_dir / 'evidence').glob('*-exec-author.prompt.txt')]
        self.assertEqual(sum('Use the negative input case.' in prompt for prompt in prompts), 1)
        self.assertTrue(all('Use a wider negative input.' not in prompt for prompt in prompts))
        receipts = [json.loads(p.read_text()) for p in (self.run_dir / 'evidence').glob('*-exec-author.receipt.json')]
        self.assertEqual(sum(row.get('operator_note_id') == 'N002' for row in receipts), 1)
        fresh = list((self.run_dir / 'evidence').glob('*-*.independence-inputs.json'))
        self.assertTrue(fresh)
        for secret in ('Use the negative input case.', 'Operator in-scope clarification',
                       'N002', state['operator_notes'][1]['sha256'], 'operator-note-'):
            self.assertTrue(all(secret not in p.read_text() for p in fresh), secret)
        sequence = state['sequence']
        again = self.run_operator_action('resume', '--polish-round', 'off')
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['sequence'], sequence)

    def test_hold_note_cli_refuses_missing_run_and_wrong_next_role(self):
        missing = self.run_operator_action('note', '--text', 'clarify')
        self.assertEqual(missing.returncode, 2)
        self.assertFalse((self.run_dir / 'state.json').exists())
        self.assertIn('existing coordinator run', missing.stdout)
        co = self.coordinator()
        active = self.run_operator_action('note', '--text', 'clarify')
        self.assertEqual(active.returncode, 2)
        self.assertEqual(json.loads(co.state_path.read_text())['status'], 'ACTIVE')
        co.hold('waiting for reviewer')
        co.state['next'] = 'reviewer'; co.save()
        refused = self.run_operator_action('note', '--text', 'clarify')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('waiting for reviewer', refused.stdout)
        self.assertNotIn('operator_notes', json.loads(co.state_path.read_text()))

    def test_hold_note_failed_author_turn_remains_pending(self):
        stopped = self.run_coordinator('--stop-after-plan', '--polish-round', 'off')
        self.assertEqual(stopped.returncode, 2, stopped.stdout + stopped.stderr)
        added = self.run_operator_action('note', '--text', 'Check signed boundary.')
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        with patch.dict(os.environ, {'FAKE_RATE_LIMIT': '1'}):
            failed = self.run_operator_action('resume', '--polish-round', 'off')
        self.assertEqual(failed.returncode, 2)
        pending = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(pending['pending_operator_note_id'], 'N001')
        self.assertEqual(pending['operator_notes'][0]['status'], 'pending')
        resumed = self.run_operator_action('resume', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        delivered = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(delivered['operator_notes'][0]['status'], 'delivered')

    def test_hold_note_ready_receipt_replays_without_second_author_call(self):
        stopped = self.run_coordinator('--stop-after-plan', '--polish-round', 'off')
        self.assertEqual(stopped.returncode, 2, stopped.stdout + stopped.stderr)
        added = self.run_operator_action('note', '--text', 'Check the signed edge.')
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        argv = self.command('--polish-round', 'off')[2:]
        argv[0] = 'resume'
        co = rc.Coordinator(rc.parser().parse_args(argv))
        with patch.object(co, 'render', side_effect=RuntimeError('crash after READY')):
            self.assertEqual(co.resume(), 'HOLD')
        state = json.loads(co.state_path.read_text())
        self.assertEqual(state['operator_notes'][0]['status'], 'delivered')
        self.assertIsNotNone(state['pending_author_result_sequence'])
        refused = self.run_operator_action('note', '--text', 'A later note.')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('READY receipt pending', refused.stdout)
        self.assertEqual(len(json.loads(co.state_path.read_text())['operator_notes']), 1)
        before = list((self.run_dir / 'evidence').glob('*-exec-author.receipt.json'))
        resumed = self.run_operator_action('resume', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        after = list((self.run_dir / 'evidence').glob('*-exec-author.receipt.json'))
        self.assertEqual(len(after), len(before))
        self.assertNotIn('pending_author_result_sequence', json.loads(co.state_path.read_text()))

    def test_plan_hold_note_forces_exec_gate_even_when_optional_gate_off(self):
        co = rc.Coordinator(rc.parser().parse_args(self.command('--shadow', 'off',
                           '--adversarial-gate', 'off', '--polish-round', 'off')[2:]))
        co.hold('operator clarification requested')
        added = self.run_operator_action('note', '--text', 'Clarify signed bounds.',
                                         '--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        resumed = self.run_operator_action('resume', '--shadow', 'off',
                                           '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE')
        self.assertGreaterEqual(state['plan_reviews'], 1)
        self.assertTrue(list((self.run_dir / 'evidence').glob('*-gate.receipt.json')))

    def test_exec_scope_change_printed_fake_successor_reaches_done(self):
        old = self.run_coordinator('--stop-after-plan', '--polish-round', 'off')
        self.assertEqual(old.returncode, 2, old.stdout + old.stderr)
        before = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(before['phase'], 'EXEC')
        (self.workspace / 'legacy-change.txt').write_text('old EXEC edit\n')
        subprocess.run(['git', 'add', 'legacy-change.txt'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'old run edit'], cwd=self.workspace, check=True)
        result = self.run_operator_action('note', '--scope-change', '--text', 'Also handle negatives.')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = dict(line.split(': ', 1) for line in result.stdout.strip().splitlines())
        for label in ('Probe', 'Start'):
            self.assertIn('--supersedes', commands[label])
            self.assertNotIn('--skip-probe', commands[label])
        aborted = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((aborted['status'], aborted['abort_kind']), ('ABORTED', 'scope-change'))
        for action in ('resume', 'accept', 'abort'):
            refused = self.run_operator_action(action)
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(self.run_operator_action('resume', '--scope-change').returncode, 2)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['status'], 'ABORTED')
        old_fresh = list((self.run_dir / 'evidence').glob('*-*.independence-inputs.json'))
        self.assertTrue(all('Also handle negatives.' not in path.read_text() for path in old_fresh))
        probe = subprocess.run(shlex.split(commands['Probe']), cwd=self.root, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        started = subprocess.run(shlex.split(commands['Start']), cwd=self.root, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        successor_dir = self.run_dir.with_name(self.run_dir.name + '-successor')
        successor = json.loads((successor_dir / 'state.json').read_text())
        self.assertEqual(successor['status'], 'DONE')
        self.assertGreaterEqual(successor['plan_reviews'], 1)
        self.assertGreaterEqual(successor['exec_reviews'], 1)
        self.assertEqual(successor['supersedes'], str(self.run_dir.resolve()))
        self.assertIn('legacy-change.txt', (successor_dir / 'context/delta.patch').read_text())
        old_dir = self.run_dir
        self.run_dir = successor_dir
        self.workitem = old_dir / 'evidence/successor-workitem.md'
        second = self.run_operator_action('reject', '--scope-change', '--text', 'One more change.',
                                           '--supersedes', str(old_dir))
        self.assertEqual(second.returncode, 2)
        self.assertEqual(json.loads((successor_dir / 'state.json').read_text())['status'], 'DONE')

    def test_scope_change_requires_named_existing_run_and_rejects_flag_on_other_actions(self):
        missing = self.run_operator_action('note', '--scope-change', '--text', 'New scope')
        self.assertEqual(missing.returncode, 2)
        self.assertFalse((self.run_dir / 'state.json').exists())
        self.assertIn('existing coordinator run', missing.stdout)
        invalid = self.run_operator_action('resume', '--scope-change')
        self.assertEqual(invalid.returncode, 2)
        self.assertFalse((self.run_dir / 'state.json').exists())
        self.assertIn('--scope-change requires note or reject', invalid.stdout)

    def test_done_scope_reject_after_rejection_limit_and_chain_limit(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        state_path = self.run_dir / 'state.json'
        state = json.loads(state_path.read_text())
        state.update(max_rejections=1, rejections=[{'id': 'R001'}])
        state_path.write_text(json.dumps(state))
        limited = self.run_operator_action('reject', '--text', 'one more change')
        self.assertEqual(limited.returncode, 2)
        self.assertEqual(json.loads(state_path.read_text())['terminal_hold_kind'], 'rejection_limit')
        scoped = self.run_operator_action('reject', '--scope-change', '--text', 'Handle negative values.')
        self.assertEqual(scoped.returncode, 0, scoped.stdout + scoped.stderr)
        self.assertEqual(json.loads(state_path.read_text())['status'], 'ABORTED')
        child = self.run_dir.with_name(self.run_dir.name + '-successor')
        spec = json.loads((self.run_dir / 'evidence/successor-spec.json').read_text())
        self.assertEqual(spec['run_dir'], str(child))

    def test_done_scope_reject_keeps_note_out_of_old_fresh_inputs(self):
        completed = self.run_coordinator('--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        fresh = list((self.run_dir / 'evidence').glob('*-*.independence-inputs.json'))
        self.assertTrue(fresh)
        before = json.loads((self.run_dir / 'state.json').read_text())
        changed = self.run_operator_action('reject', '--scope-change', '--text', 'Add a negative-value case.')
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        after = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(after['status'], 'ABORTED')
        self.assertEqual(after['sequence'], before['sequence'])
        self.assertTrue(all('Add a negative-value case.' not in path.read_text() for path in fresh))

    def test_reject_uses_workspace_lease_and_test_command_preflight(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        before = json.loads((self.run_dir / 'state.json').read_text())
        with rc.workspace_lease(self.workspace, self.root / 'lease-holder-run'):
            blocked = self.run_operator_action('reject', '--text', 'review this')
        self.assertEqual(blocked.returncode, 2)
        self.assertIn('another coordinator currently owns this workspace', blocked.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['sequence'], before['sequence'])
        refused = self.run_operator_action('reject', '--text', 'review this',
                                           '--test-command', 'no-such-r11-test-binary')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('configured test executable is missing', refused.stdout)
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['sequence'], before['sequence'])

    def test_resume_mid_reject_reopens_once_and_terminal_resume_is_noop(self):
        completed = self.run_coordinator('--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        command = self.command('--polish-round', 'off')
        command[2] = 'reject'
        args = rc.parser().parse_args(command[2:])
        co = rc.Coordinator(args)
        intent = co.operator_intent('reject', 'recheck the in-scope detail', None)
        co.args.expect = intent['digest']
        self.assertEqual(co.reject('recheck the in-scope detail', None), 'ACTIVE')
        self.assertEqual(len(co.state['rejections']), 1)
        self.assertEqual(co.resume(), 'DONE')
        after = json.loads(co.state_path.read_text())
        sequence = after['sequence']
        self.assertEqual(len(after['rejections']), 1)
        self.assertEqual(co.resume(), 'DONE')
        final = json.loads(co.state_path.read_text())
        self.assertEqual(final['sequence'], sequence)
        self.assertEqual(len(final['rejections']), 1)

    def test_rejection_feedback_survives_rate_limit_and_resume(self):
        completed = self.run_coordinator('--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        feedback = 'OPERATOR_REJECTION_RETRY_5c09'
        reject_cmd = self.command('--text', feedback, '--polish-round', 'off')
        reject_cmd[2] = 'reject'
        reject_cmd.append('--skip-probe')
        intent_cmd = reject_cmd.copy(); intent_cmd.append('--intent-only')
        intent = subprocess.run(intent_cmd, cwd=self.root, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(intent.returncode, 0, intent.stdout + intent.stderr)
        reject_cmd.extend(['--expect', json.loads(intent.stdout)['digest']])
        limited = subprocess.run(reject_cmd, cwd=self.root,
            env={**os.environ, 'FAKE_RATE_LIMIT': '1'}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(limited.returncode, 2)
        held = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(held['status'], 'HOLD')
        self.assertEqual(held.get('pending_rejection_id'), 'R001')
        self.assertEqual(held['rejections'][0]['status'], 'dispatched')
        resumed = self.run_operator_action('resume', '--retry-uncertain', '--polish-round', 'off')
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        final = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(final['status'], 'DONE')
        self.assertEqual(final['rejections'][0]['status'], 'delivered')
        prompts = [path.read_text() for path in
                   (self.run_dir / 'evidence').glob('*-exec-author.prompt.txt')]
        self.assertGreaterEqual(sum(feedback in prompt for prompt in prompts), 2)

    def test_reject_runs_permission_probe_gate_before_reopening(self):
        completed = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                         '--polish-round', 'off')
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        before = json.loads((self.run_dir / 'state.json').read_text())
        argv = self.command('--text', 'in-scope feedback', '--polish-round', 'off')[2:]
        argv[0] = 'reject'
        args = rc.configure_parser(rc.parser(), argv).parse_args(argv)
        with patch.object(rc.Coordinator, 'probe_passed', return_value=(False, 'probe failed')):
            with patch('builtins.print') as output:
                self.assertEqual(rc._execute_locked(args), 2)
        output.assert_called_once_with('REFUSED: probe failed; run permission-probe before continuing')
        after = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(after['status'], 'DONE')
        self.assertEqual(after['sequence'], before['sequence'])
        self.assertEqual(after.get('rejections', []), [])

    def test_failed_cli_without_usage_is_unknown_in_usage_reconciliation(self):
        co = self.coordinator()
        failing_cli = self.root / 'failed-empty-usage-cli'
        failing_cli.write_text(f'#!{sys.executable}\nraise SystemExit(7)\n')
        failing_cli.chmod(0o755)
        co.args.codex_bin = str(failing_cli)

        with self.assertRaisesRegex(RuntimeError, 'CLI exit 7'):
            co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})
        co.write_usage()

        usage = json.loads((self.run_dir / 'usage.json').read_text())
        self.assertEqual(usage['turns'][0]['input'], 0)
        self.assertEqual(usage['turns'][0]['output'], 0)
        self.assertEqual(usage['usage_reconciliation'], [{
            'source': 'cli_turn', 'sequence': 1, 'role': 'author', 'phase': 'PLAN',
            'invocation_budget_counted': True, 'provider_usage': 'unknown',
        }])
        usage_md = (self.run_dir / 'usage.md').read_text()
        self.assertIn('| cli_turn | 1 | author | PLAN | True | unknown |', usage_md)

    def test_failed_timed_out_and_killed_claude_turns_keep_stream_usage(self):
        event_rows = [
            {'type': 'stream_event', 'event': {'type': 'message_start', 'message': {
                'id': 'request-1', 'usage': {'input_tokens': 10, 'cache_creation_input_tokens': 5,
                                             'cache_read_input_tokens': 20, 'output_tokens': 0}}}},
            {'type': 'stream_event', 'event': {'type': 'message_delta',
                                               'usage': {'output_tokens': 12}}},
        ]
        for mode in ('failed', 'timed-out', 'killed'):
            with self.subTest(mode=mode):
                self.run_dir = self.root / ('usage-' + mode)
                co = self.coordinator('--author-vendor', 'claude', '--timeout', '1')
                cli = self.root / ('usage-cli-' + mode)
                cli.write_text(
                    f'#!{sys.executable}\nimport json, os, signal, sys, time\n'
                    f'rows = {event_rows!r}\n'
                    'for row in rows:\n    print(json.dumps(row), flush=True)\n'
                    + ('time.sleep(5)\n' if mode == 'timed-out' else
                       'os.kill(os.getpid(), signal.SIGKILL)\n' if mode == 'killed' else
                       'sys.exit(7)\n'))
                cli.chmod(0o755)
                co.args.claude_bin = str(cli)
                with self.assertRaises(RuntimeError):
                    co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', rc.author_schema())
                receipt = co.state['turns'][-1]
                self.assertEqual(receipt['usage_requests'], [{
                    'input': 35, 'cached': 20, 'output': 12, 'source': 'stream_event'}])
                co.write_usage()
                usage = json.loads((self.run_dir / 'usage.json').read_text())
                self.assertEqual(usage['turns'][0]['input'], 35)
                self.assertEqual(usage['turns'][0]['output'], 12)

    def test_sigkill_persists_two_stream_usage_events_before_kill_for_both_vendors(self):
        expected = {
            'claude': [
                {'input': 35, 'cached': 20, 'output': 12, 'source': 'stream_event'},
                {'input': 13, 'cached': 4, 'output': 8, 'source': 'stream_event'},
            ],
            'codex': [
                {'input': 31, 'cached': 7, 'output': 9, 'source': 'turn.completed-stream'},
                {'input': 17, 'cached': 3, 'output': 5, 'source': 'turn.completed-stream'},
            ],
        }
        for vendor in ('claude', 'codex'):
            with self.subTest(vendor=vendor):
                self.run_dir = self.root / ('sigkill-stream-usage-' + vendor)
                co = self.coordinator('--author-vendor', vendor, '--timeout', '3',
                                      '--exec-turn-timeout', '3')
                streaming_cli = self.root / ('streaming-' + vendor + '-cli')
                streaming_cli.write_text(
                    f'#!{sys.executable}\nimport os, sys\n'
                    f'os.execv({sys.executable!r}, [{sys.executable!r}, {str(FAKE)!r}, *sys.argv[1:]])\n')
                streaming_cli.chmod(0o755)
                co.args.codex_bin = co.args.claude_bin = str(streaming_cli)
                expected_rows = expected[vendor]
                if vendor == 'codex':
                    rollout = (co.global_codex_home / 'sessions' /
                               time.strftime('%Y/%m/%d', time.gmtime()) /
                               'rollout-fake-codex-thread.jsonl')
                    expected_rows = [{**row, 'source': f'{rollout}:{index}'}
                                     for index, row in enumerate(expected[vendor], 1)]
                errors = []

                def invoke_author():
                    try:
                        co._invoke_once('author', 'EXEC',
                                        'Role: persistent. Phase: EXEC.', rc.author_schema())
                    except BaseException as exc:
                        errors.append(exc)

                stream_env = {'FAKE_STREAM_TWO_USAGE_THEN_HANG': '1'}
                if vendor == 'codex':
                    stream_env['CODEX_HOME'] = str(co.global_codex_home)
                with patch.dict(os.environ, stream_env):
                    worker = threading.Thread(target=invoke_author)
                    worker.start()
                    deadline = time.monotonic() + 2.5
                    observed = None
                    prekill_state = None
                    while time.monotonic() < deadline:
                        try:
                            state_snapshot = json.loads(co.state_path.read_text())
                            active = state_snapshot.get('active')
                        except (OSError, json.JSONDecodeError):
                            active = None
                        if active and len(active.get('usage_requests', [])) == 2:
                            observed = active
                            prekill_state = state_snapshot
                            break
                        time.sleep(0.02)
                    self.assertIsNotNone(observed, 'usage was not persisted while the provider process was alive: ' +
                        (co.evidence / '001-exec-author.stdout.jsonl').read_text() +
                        (co.evidence / '001-exec-author.stderr.log').read_text())
                    self.assertEqual(observed['usage_requests'], expected_rows)
                    self.assertNotIn('timed_out', observed)
                    worker.join(6)
                self.assertFalse(worker.is_alive(), 'coordinator did not SIGKILL/reap the timed-out CLI')
                self.assertTrue(errors and isinstance(errors[0], RuntimeError), errors)
                state = json.loads(co.state_path.read_text())
                receipt = state['turns'][0]
                self.assertTrue(receipt['timed_out'])
                self.assertEqual(receipt['returncode'], -signal.SIGKILL)
                self.assertEqual(receipt['usage_requests'], expected_rows)
                receipt_path = co.evidence / '001-exec-author.receipt.json'
                self.assertEqual(json.loads(receipt_path.read_text())['usage_requests'], expected_rows)

                reload_args = rc.parser().parse_args(['resume', '--workspace', str(self.workspace),
                    '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
                    '--author-vendor', vendor, '--timeout', '3', '--exec-turn-timeout', '3',
                    '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli())])
                recovered = rc.Coordinator(reload_args)
                recovered.write_usage()
                recovered.write_usage()
                usage = json.loads((self.run_dir / 'usage.json').read_text())
                self.assertEqual(usage['turns'][0]['requests'], 2)
                self.assertEqual(usage['turns'][0]['input'], sum(row['input'] for row in expected[vendor]))
                self.assertEqual(usage['turns'][0]['output'], sum(row['output'] for row in expected[vendor]))

                recovery_dir = self.root / ('crash-recovery-' + vendor)
                recovery_dir.mkdir()
                for name in ('evidence', 'rounds', 'context', 'internal'):
                    (recovery_dir / name).mkdir()
                shutil.copy(self.run_dir / 'context' / 'workitem.md',
                            recovery_dir / 'context' / 'workitem.md')
                prekill_state['config']['max_invocations'] = 1
                prekill_state['invocations_used'] = 1
                (recovery_dir / 'state.json').write_text(json.dumps(prekill_state))
                recovery_args = rc.parser().parse_args(['resume', '--workspace', str(self.workspace),
                    '--workitem', str(self.workitem), '--run-dir', str(recovery_dir),
                    '--author-vendor', vendor, '--timeout', '3', '--exec-turn-timeout', '3',
                    '--max-invocations', '1', '--codex-bin', str(self.fake_codex_cli()),
                    '--claude-bin', str(self.fake_claude_cli())])
                crash_recovery = rc.Coordinator(recovery_args)
                self.assertEqual(crash_recovery.resume(retry_uncertain=True), 'HOLD')
                self.assertEqual(crash_recovery.state['turns'], [])
                self.assertIsNone(crash_recovery.state['active'])
                self.assertIsNone(crash_recovery.state.get('uncertain_active'))
                self.assertEqual(len(crash_recovery.state['abandoned_turns']), 1)
                self.assertEqual(crash_recovery.state['abandoned_turns'][0]['usage_requests'], expected_rows)
                crash_recovery.write_usage()
                recovered_usage = json.loads((recovery_dir / 'usage.json').read_text())
                self.assertEqual(len(recovered_usage['abandoned_turn_usage']), 1)
                self.assertEqual(recovered_usage['abandoned_turn_usage'][0]['requests'], expected_rows)
                self.assertEqual(recovered_usage['overall']['input_tokens'], 0)
                self.assertEqual([row['source'] for row in recovered_usage['usage_reconciliation']],
                                 ['abandoned_turn'])
                self.assertEqual(crash_recovery.resume(retry_uncertain=True), 'HOLD')
                self.assertEqual(len(crash_recovery.state['abandoned_turns']), 1)
                crash_recovery.write_usage()
                again = json.loads((recovery_dir / 'usage.json').read_text())
                self.assertEqual(len(again['abandoned_turn_usage']), 1)

    def test_archived_uncertain_turn_retains_stream_usage(self):
        co = self.coordinator()
        co.archive_abandoned_turn({'sequence': 8, 'role': 'author', 'phase': 'EXEC',
            'invocation_budget_counted': True,
            'usage_requests': [{'input': 30, 'cached': 5, 'output': 8, 'source': 'stream_event'}]})
        co.write_usage()
        usage = json.loads((self.run_dir / 'usage.json').read_text())
        self.assertEqual(usage['usage_reconciliation'][0]['provider_usage'], 'reported')
        self.assertEqual(usage['abandoned_turn_usage'], [{
            'sequence': 8, 'role': 'author', 'phase': 'EXEC', 'provider_usage': 'reported',
            'requests': [{'input': 30, 'cached': 5, 'output': 8, 'source': 'stream_event'}],
            'input_tokens': 30, 'cached_tokens': 5, 'output_tokens': 8}])
        self.assertIn('| 8 | author | EXEC | 1 | 30 | 5 | 8 |', (self.run_dir / 'usage.md').read_text())

    def test_concurrent_state_saves_are_serialized(self):
        co = self.coordinator()
        original_write = rc.atomic_json
        barrier = threading.Barrier(3)
        guard = threading.Lock()
        active_writes = 0
        max_active_writes = 0

        def slow_write(path, value):
            nonlocal active_writes, max_active_writes
            with guard:
                active_writes += 1
                max_active_writes = max(max_active_writes, active_writes)
            time.sleep(0.03)
            original_write(path, value)
            with guard:
                active_writes -= 1

        def save_after_barrier():
            barrier.wait()
            co.save()

        with patch.object(rc, 'atomic_json', side_effect=slow_write):
            writers = [threading.Thread(target=save_after_barrier) for _ in range(2)]
            for writer in writers:
                writer.start()
            barrier.wait()
            for writer in writers:
                writer.join(timeout=2)
                self.assertFalse(writer.is_alive())
        self.assertEqual(max_active_writes, 1)

    def test_successful_cli_turn_without_usage_detail_is_unknown(self):
        co = self.coordinator()
        co.state['turns'] = [{
            'sequence': 1, 'role': 'author', 'phase': 'PLAN', 'returncode': 0,
            'invocation_budget_counted': True,
        }]

        co.write_usage()

        usage = json.loads((self.run_dir / 'usage.json').read_text())
        self.assertEqual(usage['turns'][0]['input'], 0)
        self.assertEqual(usage['turns'][0]['output'], 0)
        self.assertEqual(usage['usage_reconciliation'], [{
            'source': 'cli_turn', 'sequence': 1, 'role': 'author', 'phase': 'PLAN',
            'invocation_budget_counted': True, 'provider_usage': 'unknown',
        }])

    def test_uncertain_first_claude_retry_rotates_only_unstarted_role(self):
        co = self.coordinator('--author-vendor', 'claude', '--reviewer-vendor', 'claude')
        old_author = co.state['sessions']['author']
        existing_reviewer = co.state['sessions']['reviewer']
        co.state['started']['reviewer'] = True
        co.state['uncertain_active'] = {'role': 'author', 'fresh': False, 'pid': 20001}
        co.state['status'] = 'HOLD'

        def retry():
            return co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})

        with patch('os.killpg', side_effect=ProcessLookupError):
            with patch.object(co, 'drive', side_effect=retry):
                co.resume(retry_uncertain=True)
        state = json.loads(co.state_path.read_text())
        author_command = state['turns'][-1]['command']
        rotated = state['sessions']['author']
        self.assertNotEqual(rotated, old_author)
        self.assertIn('--session-id', author_command)
        self.assertIn(rotated, author_command)
        self.assertNotIn(old_author, author_command)
        self.assertEqual(state['sessions']['reviewer'], existing_reviewer)
        self.assertTrue(state['started']['reviewer'])

        # With --author-vendor omitted, the configured default is Codex; its
        # uncertain retry must not apply Claude's session rotation behavior.
        self.run_dir = self.root / 'uncertain-default-codex-author'
        codex_co = self.coordinator()
        self.assertEqual(codex_co.args.author_vendor, 'codex')
        codex_co.state['uncertain_active'] = {'role': 'author', 'fresh': False, 'pid': 20002}
        codex_co.state['status'] = 'HOLD'

        with patch('os.killpg', side_effect=ProcessLookupError):
            with patch.object(codex_co, 'drive') as drive:
                codex_co.resume(retry_uncertain=True)
        drive.assert_called_once_with()
        self.assertFalse(codex_co.state['started']['author'])
        self.assertEqual(codex_co.state['turns'], [])

    def test_failed_started_claude_role_keeps_session_and_retry_uses_original(self):
        co = self.coordinator('--author-vendor', 'claude')
        original = co.state['sessions']['author']
        co.state['started']['author'] = True
        co.save()
        with patch.dict(os.environ, {'FAKE_PLAN_MUTATE': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'mutated workspace during PLAN'):
                co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})
        failed_receipt = json.loads((co.evidence / '001-plan-author.receipt.json').read_text())
        self.assertTrue(failed_receipt['error'])
        resume_index = failed_receipt['command'].index('--resume')
        self.assertEqual(failed_receipt['command'][resume_index:resume_index + 2], ['--resume', original])
        self.assertNotIn('--session-id', failed_receipt['command'])
        with patch.dict(os.environ, {'FAKE_PLAN_MUTATE': ''}):
            co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})
        state = json.loads(co.state_path.read_text())
        self.assertEqual(state['sessions']['author'], original)
        retry_command = state['turns'][-1]['command']
        resume_index = retry_command.index('--resume')
        self.assertEqual(retry_command[resume_index:resume_index + 2], ['--resume', original])
        self.assertNotIn('--session-id', retry_command)

    def test_failed_fresh_claude_and_codex_turns_do_not_rotate(self):
        for role, flags, fresh in (
                ('author', ['--author-vendor', 'claude'], True),
                ('author', ['--author-vendor', 'codex'], False)):
            with self.subTest(flags=flags, fresh=fresh):
                self.run_dir = self.root / ('control-' + flags[-1] + str(fresh))
                co = self.coordinator(*flags)
                original = co.state['sessions']['author']
                with patch.dict(os.environ, {'FAKE_RATE_LIMIT': '1'}):
                    with self.assertRaisesRegex(RuntimeError, 'rate_limited'):
                        co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.',
                                        {}, fresh=fresh)
                state = json.loads(co.state_path.read_text())
                self.assertEqual(state['sessions']['author'], original)
                self.assertFalse(state['started']['author'])

    def test_git_snapshot_tracks_untracked_symlink_and_ignores_cache(self):
        first, manifest = rc.git_snapshot(self.workspace)
        (self.workspace / 'ignored.cache').write_text('ignored')
        self.assertEqual(first, rc.git_snapshot(self.workspace)[0])
        (self.workspace / 'new.txt').write_text('new')
        second = rc.git_snapshot(self.workspace)[0]
        self.assertNotEqual(first, second)
        (self.workspace / 'link').symlink_to('new.txt')
        self.assertNotEqual(second, rc.git_snapshot(self.workspace)[0])
        self.assertIn(['tracked.txt', manifest[-1][1]], manifest)

    def test_one_live_coordinator_lease_blocks_a_second_cli_process(self):
        with rc.run_lease(self.run_dir):
            result = self.run_coordinator('--skip-probe')
            self.assertEqual(result.returncode, 2)
            self.assertIn('another coordinator currently owns this run', result.stdout)
            self.assertFalse((self.run_dir / 'state.json').exists())
            owner = json.loads((self.run_dir / '.coordinator.lock').read_text())
            self.assertEqual(owner['pid'], os.getpid())
        with rc.run_lease(self.run_dir):
            pass

    def test_coordinator_refuses_run_dir_glob_metacharacters(self):
        unsafe_run_dir = self.root / 'run[alias]'
        result = self.run_coordinator('--run-dir', str(unsafe_run_dir), '--skip-probe')
        self.assertEqual(result.returncode, 2)
        self.assertIn('REFUSED: --run-dir must not contain glob metacharacters', result.stdout)
        self.assertFalse(unsafe_run_dir.exists())

    def test_workspace_lease_path_is_stable_and_outside_workspace(self):
        lease_path = rc.workspace_lease_path(self.workspace)
        self.assertEqual(lease_path, rc.workspace_lease_path(self.workspace / '.'))
        self.assertNotEqual(lease_path, self.workspace)
        self.assertNotIn(self.workspace, lease_path.parents)

    def test_workspace_lease_key_uses_device_and_inode_not_path_text(self):
        alias = self.root / 'case-alias'
        canonical_paths = {self.workspace.resolve(), alias.resolve()}
        identity = type('Stat', (), {'st_dev': 17, 'st_ino': 29})()
        real_stat = Path.stat

        def same_identity_for_aliases(path, *args, **kwargs):
            if path.resolve() in canonical_paths:
                return identity
            return real_stat(path, *args, **kwargs)

        with patch.object(Path, 'stat', new=same_identity_for_aliases):
            self.assertNotEqual(self.workspace.resolve(), alias.resolve())
            self.assertEqual(rc.workspace_lease_path(self.workspace), rc.workspace_lease_path(alias))

    def test_one_workspace_lease_blocks_a_second_run_directory_cli_process(self):
        second_run = self.root / 'second-run'
        with rc.workspace_lease(self.workspace, self.run_dir):
            result = self.run_coordinator('--run-dir', str(second_run), '--skip-probe')
            self.assertEqual(result.returncode, 2)
            self.assertIn('another coordinator currently owns this workspace', result.stdout)
            self.assertIn(f'owner pid {os.getpid()}', result.stdout)
            self.assertIn(f'run_dir {self.run_dir.resolve()}', result.stdout)
            self.assertFalse((second_run / 'state.json').exists())
        with rc.workspace_lease(self.workspace, second_run):
            pass

    def test_abort_uses_per_run_lease_without_workspace_lease(self):
        run_a = self.root / 'run-a'
        self.run_dir = self.root / 'run-b'
        co = self.coordinator('--timeout', '10', '--author-effort', 'low',
                              '--reviewer-effort', 'low', '--gate-effort', 'low',
                              '--test-command', 'python3 -m unittest')
        before = rc.git_snapshot(self.workspace)[0]

        with rc.run_lease(run_a), rc.workspace_lease(self.workspace, run_a):
            abort_command = self.command('--skip-probe')
            abort_command[2] = 'abort'
            aborted = subprocess.run(abort_command, cwd=self.root, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(aborted.returncode, 2)
            self.assertIn('HOLD: aborted by operator', aborted.stdout)
            state = json.loads((self.run_dir / 'state.json').read_text())
            self.assertEqual(state['status'], 'HOLD')
            self.assertEqual(rc.git_snapshot(self.workspace)[0], before)

            running = self.run_coordinator('--skip-probe')
            self.assertEqual(running.returncode, 2)
            self.assertIn('another coordinator currently owns this workspace', running.stdout)

            resume_command = self.command('--skip-probe')
            resume_command[2] = 'resume'
            resumed = subprocess.run(resume_command, cwd=self.root, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(resumed.returncode, 2)
            self.assertIn('another coordinator currently owns this workspace', resumed.stdout)

    def test_workspace_lease_refuses_a_non_private_lock_directory(self):
        with tempfile.TemporaryDirectory(dir=self.root) as temp:
            temp_root = Path(temp)
            workspace = temp_root / 'workspace'
            workspace.mkdir()
            with patch.object(rc, '_workspace_lease_temp_roots', return_value=(temp_root,)):
                lease_dir = rc.workspace_lease_path(workspace).parent
                lease_dir.mkdir(mode=0o700)
                lease_dir.chmod(0o755)
                with self.assertRaisesRegex(rc.RunLeaseError, 'private directory'):
                    with rc.workspace_lease(workspace, self.run_dir):
                        self.fail('untrusted lock directory should fail closed')

    def test_rubric_requires_six_nonempty_segments(self):
        valid = ('Trigger: x. Reachability: y. Impact: z. Likelihood: often. '
                 'Fix cost: small. Cheaper response: none.')
        self.assertEqual(rc.missing_rubric(valid), [])
        self.assertIn('Impact', rc.missing_rubric(valid.replace('Impact: z.', 'Impact: .')))
        self.assertIn('Cheaper response', rc.missing_rubric('Trigger: x'))

    def test_codex_command_events_become_observed_commands(self):
        rows = [{'type': 'item.completed', 'item': {'type': 'command_execution',
                 'command': "/bin/zsh -lc 'python3 -m unittest -v'", 'exit_code': 0,
                 'status': 'completed'}}]
        commands, calls = rc.observed_events('codex', rows)
        self.assertEqual(commands[0]['exit_code'], 0)
        self.assertTrue(rc.command_invokes_test(commands[0]['command'], 'python3 -m unittest -v'))
        self.assertFalse(rc.command_invokes_test('python3 -m unittest -v 2>&1',
                                                 'python3 -m unittest -v'))
        self.assertFalse(rc.command_invokes_test('python3 -m unittest -v; echo $?',
                                                 'python3 -m unittest -v'))
        self.assertFalse(rc.observed_test_succeeded(
            {'command': 'python3 -m unittest -v', 'exit_code': 0, 'error': False,
             'output': 'FAILED (failures=1)'}, 'python3 -m unittest -v'))
        self.assertEqual(calls[0]['tool'], 'command_execution')

    def test_codex_code_mode_rollout_recovers_attempt_but_never_invents_success(self):
        home = self.root / 'fake-home'
        sessions = home / '.codex' / 'sessions'
        sessions.mkdir(parents=True)
        command = 'echo x > forbidden-probe'
        code = 'const r = await tools.exec_command(' + json.dumps({'cmd': command}) + ');\ntext(r.output);'
        rows = [
            {'timestamp': '2026-09-21T00:00:00Z', 'type': 'response_item',
             'payload': {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'c1', 'input': code}},
            {'timestamp': '2026-09-21T00:00:01Z', 'type': 'response_item',
             'payload': {'type': 'custom_tool_call_output', 'call_id': 'c1',
                         'output': [{'text': 'zsh: operation not permitted: forbidden-probe'}]}},
        ]
        path = sessions / 'rollout-test-session.jsonl'
        path.write_text('\n'.join(json.dumps(row) for row in rows))
        start = rc.datetime.fromisoformat('2026-09-21T00:00:00+00:00').timestamp()
        with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {'CODEX_HOME': str(home / '.codex')}):
            attempts, calls = rc.codex_rollout_attempts('test-session', start, start + 2)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]['command'], command)
            self.assertIsNone(attempts[0]['exit_code'])
            self.assertFalse(rc.observed_test_succeeded(attempts[0], command))
            self.assertEqual(calls[0]['tool'], 'code_mode')
            rows[0]['payload']['input'] = code.replace('text(r.output)', 'text(JSON.stringify(r))')
            rows[1]['payload']['output'] = [{'text': json.dumps({'exit_code': 1, 'output': 'operation not permitted'})}]
            path.write_text('\n'.join(json.dumps(row) for row in rows))
            recovered, _ = rc.codex_rollout_attempts('test-session', start, start + 2)
            self.assertEqual(recovered[0]['exit_code'], 1)
            self.assertTrue(recovered[0]['error'])
            rows[0]['payload']['input'] = 'if (false) { ' + code + ' }'
            path.write_text('\n'.join(json.dumps(row) for row in rows))
        self.assertEqual(rc.codex_rollout_attempts('test-session', start, start + 2)[0], [])

    def test_rate_limit_rejection_is_logged_without_spending_invocation_budget(self):
        rejected = self.run_coordinator('--max-invocations', '1',
                                        env={'FAKE_RATE_LIMIT': '1'})
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('rate_limited', rejected.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['sequence'], 1)
        self.assertEqual(state['invocations_used'], 0)
        failed = state['turns'][0]
        self.assertEqual(failed['error_kind'], 'rate_limited')
        self.assertFalse(failed['invocation_budget_counted'])
        self.assertIn('Sep 26th 5:13 PM', failed['reset_hint'])
        rejected_usage = json.loads((self.run_dir / 'usage.json').read_text())
        self.assertEqual(rejected_usage['invocations_used'], 0)
        self.assertFalse(rejected_usage['turns'][0]['invocation_budget_counted'])
        usage_md = (self.run_dir / 'usage.md').read_text()
        self.assertIn('| Turn | Role | Phase | Budget counted | Error kind | Reset hint | Requests |', usage_md)
        self.assertIn('| 1 | author | PLAN | False | rate_limited | Try again at Sep 26th 5:13 PM |', usage_md)

        command = self.command('--max-invocations', '1', '--skip-probe')
        command[2] = 'resume'
        resumed = subprocess.run(command, cwd=self.root, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(resumed.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['sequence'], 2)
        self.assertEqual(state['invocations_used'], 1)

    def test_rate_limit_classifier_requires_an_error_signal(self):
        classified = rc.classify_rate_limit_failure(
            1, '', '{"type":"error","message":"HTTP 429: rate limit; try again at 10:00"}')
        self.assertEqual(classified['kind'], 'rate_limited')
        self.assertTrue(classified['reset_hint'].startswith('try again at 10:00'))
        self.assertEqual(rc.classify_rate_limit_failure(
            1, '', '{"type":"error","code":"rate_limit_exceeded","message":"please wait"}')['kind'],
            'rate_limited')
        self.assertEqual(rc.classify_rate_limit_failure(
            1, '', '{"type":"error","code":"","message":"HTTP 429 rate limit exceeded"}')['kind'],
            'rate_limited')
        self.assertIsNone(rc.classify_rate_limit_failure(
            1, '', '{"type":"agent_message","text":"rate limit is part of the task description"}'))
        self.assertIsNone(rc.classify_rate_limit_failure(
            1, '', '{"type":"error","code":"invalid_output","message":"prompt mentioned HTTP 429"}'))
        self.assertIsNone(rc.classify_rate_limit_failure(1, 'permission denied', ''))
        retry_after = rc.classify_rate_limit_failure(
            1, 'HTTP 429 Too Many Requests\nRetry-After: 32\n', '')
        self.assertEqual(retry_after['reset_hint'], 'Retry-After: 32')

    def test_legacy_invocation_budget_migration_exempts_only_logged_rate_limits(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.root / 'legacy-budget-run'),
            '--codex-bin', str(FAKE), '--claude-bin', str(FAKE)])
        co = rc.Coordinator(args)
        turns = [
            {'sequence': 1, 'role': 'reviewer', 'phase': 'EXEC', 'returncode': 1},
            {'sequence': 2, 'role': 'author', 'phase': 'EXEC', 'returncode': 1,
             'invocation_budget_counted': False},
            {'sequence': 3, 'role': 'reviewer', 'phase': 'EXEC', 'returncode': 0},
        ]
        co.state['sequence'] = 3
        co.state['turns'] = turns
        del co.state['invocations_used']
        del co.state['invocation_budget_version']
        (co.evidence / '001-exec-reviewer.stderr.log').write_text(
            'HTTP 429 Too Many Requests. Try again at 10:00\n')
        (co.evidence / '002-exec-author.stderr.log').write_text('permission denied\n')
        rc.atomic_json(co.state_path, co.state)

        migrated = rc.Coordinator(args)
        self.assertEqual(migrated.state['sequence'], 3)
        self.assertEqual(migrated.state['invocations_used'], 2)
        self.assertEqual(migrated.state['turns'][0]['error_kind'], 'rate_limited')
        self.assertFalse(migrated.state['turns'][0]['invocation_budget_counted'])
        self.assertTrue(migrated.state['turns'][1]['invocation_budget_counted'])

    def test_codex_readonly_roles_do_not_inherit_execpolicy_bypass_grants(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--author-vendor', 'claude', '--reviewer-vendor', 'codex', '--gate-model', 'gpt-6-luna'])
        co = rc.Coordinator(args)
        schema = self.root / 'schema.json'
        for role in ('reviewer', 'shadow', 'gate', 'probe'):
            command = co._codex_command(role, schema, True)
            self.assertIn('--ignore-rules', command)
            self.assertIn('sandbox_mode="read-only"', command)
            self.assertIn('approval_policy="never"', command)
        self.assertIn('--ignore-rules', co._codex_command('author', schema, False))
        self.assertTrue(co.reviewer_flags()['ignore_execpolicy_rules'])
        self.assertEqual(co.author_flags()['author_binary'], args.claude_bin)

    def test_model_facing_review_schemas_do_not_request_snapshot_or_exit_code(self):
        for schema in (rc.review_schema(), rc.gate_schema()):
            self.assertNotIn('reviewed_snapshot', schema['properties'])
            evidence = schema['properties']['self_run_evidence']['items']
            self.assertNotIn('exit_code', evidence['properties'])
        prior = rc.review_schema()['properties']['prior_findings']['items']
        self.assertEqual(prior['properties']['disposition']['enum'],
                         ['fixed', 'still_open', 'withdrawn'])

    def test_sensitive_shadow_and_adversarial_paths_are_rejected(self):
        for filename in ('07-shadow-approve.md', '11-adversarial-approve.md'):
            calls = [{'tool': 'Read', 'input': {'file_path': str(self.run_dir / 'rounds' / filename)}}]
            self.assertIsNotNone(rc.sensitive_access(calls, 'reviewer', self.run_dir / 'evidence',
                                                     self.run_dir / 'rounds'))

    def test_claude_readonly_command_has_restricted_allowlist(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        schema = self.root / 'schema.json'
        rc.atomic_json(schema, rc.review_schema())
        command = co.command('reviewer', schema, False)
        self.assertIn('--restricted', command)
        self.assertEqual(command[command.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(command[command.index('--permission-prompts') + 1], 'none')
        allowed = allowed_tool_values(command)
        self.assertIn('Read,Grep,Glob', allowed)
        self.assertIn('Bash(npm test)', allowed)
        self.assertNotIn(':*', ','.join(allowed))
        self.assertFalse(any(value.startswith('Bash(git ') for value in allowed))
        self.assertFalse(any(value.startswith('Bash(echo') for value in allowed))
        self.assertFalse(any(value.startswith('Bash(rm') for value in allowed))
        self.assertEqual(sum(value.startswith('Bash(') for value in allowed),
                         len([cmd for cmd in co.reviewer_commands()]))
        self.assertIn('Edit,Write', command[command.index('--disallowedTools') + 1])
        add_dir = command[command.index('--add-dir') + 1]
        self.assertEqual(add_dir, str(co.context))
        self.assertNotEqual(add_dir, str(co.run_dir))

    def test_all_claude_roles_use_strict_fail_closed_bash_sandbox(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--author-vendor', 'codex', '--reviewer-vendor', 'claude'])
        co = rc.Coordinator(args)
        schema = self.root / 'sandbox-schema.json'
        rc.atomic_json(schema, rc.review_schema())
        for role in ('reviewer', 'shadow', 'gate', 'probe'):
            with self.subTest(role=role):
                command = co.command(role, schema, role != 'reviewer')
                self.assertIn('--setting-sources', command)
                self.assertEqual(command[command.index('--setting-sources') + 1], '')
                settings = json.loads(command[command.index('--settings') + 1],
                                       object_pairs_hook=unique_json_object)
                self.assertIs(settings['sandbox']['enabled'], True)
                self.assertIs(settings['sandbox']['failIfUnavailable'], True)
                self.assertIs(settings['sandbox']['allowUnsandboxedCommands'], False)
                self.assertIs(settings['sandbox']['autoAllowBashIfSandboxed'], False)
                self.assertEqual(settings['sandbox']['excludedCommands'], [])
                self.assertIs(settings['sandbox']['network']['strictAllowlist'], True)
                self.assertEqual(settings['sandbox']['network']['allowedDomains'], [])
                self.assertIs(settings['sandbox']['network']['allowLocalBinding'], False)
                self.assertEqual(settings['sandbox']['credentials']['envVars'], sorted(
                    settings['sandbox']['credentials']['envVars'], key=lambda row: row['name']))
                self.assertTrue(all(row['mode'] == 'deny' for row in
                                    settings['sandbox']['credentials']['envVars']))
                self.assertTrue(all(row['mode'] == 'deny' for row in
                                    settings['sandbox']['credentials']['files']))
                filesystem = settings['sandbox']['filesystem']
                self.assertNotIn('allowWrite', filesystem)
                self.assertEqual(settings['sandbox']['filesystem']['denyWrite'], [str(co.run_dir)])
                self.assertNotIn(str(co.workspace), json.dumps(filesystem))
                self.assertNotIn('.paired-session-claude-session-', json.dumps(filesystem))
                self.assertEqual(settings['permissions']['deny'], [
                    f'Edit(//{co.run_dir.as_posix().lstrip("/")}/**)'])
                self.assertIn('--settings', command)
                self.assertEqual(command.count('--settings'), 1)
        author_args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.root / 'claude-author-run'),
            '--author-vendor', 'claude', '--reviewer-vendor', 'codex'])
        author_co = rc.Coordinator(author_args)
        author = author_co.command('author', schema, False)
        author_settings = json.loads(author[author.index('--settings') + 1])
        self.assertIs(author_settings['sandbox']['enabled'], True)
        self.assertIs(author_settings['sandbox']['failIfUnavailable'], True)
        self.assertIs(author_settings['sandbox']['allowUnsandboxedCommands'], False)
        self.assertEqual(author_settings['sandbox']['network']['allowedDomains'], [])
        self.assertEqual(author_settings['sandbox']['filesystem']['denyWrite'], [str(author_co.run_dir)])
        self.assertEqual(author_settings['permissions']['deny'], [
            f'Edit(//{author_co.run_dir.as_posix().lstrip("/")}/**)'])
        self.assertEqual(author.count('--settings'), 1)
        self.assertEqual(author.count('--add-dir'), 1)

    def test_claude_author_workspace_override_uses_disposable_cwd_without_write_expansion(self):
        co = self.coordinator('--author-vendor', 'claude', '--reviewer-vendor', 'codex')
        disposable = self.root / 'disposable-author-workspace'
        disposable.mkdir()
        schema = self.root / 'override-schema.json'
        rc.atomic_json(schema, rc.review_schema(verified=False))
        captured = {}

        real_popen = subprocess.Popen

        def capture_cli_spawn(command, **kwargs):
            if command[0] != co.args.claude_bin:
                return real_popen(command, **kwargs)
            captured['command'] = command
            captured.update(kwargs)
            raise OSError('contract capture')

        with patch.object(rc.subprocess, 'Popen', side_effect=capture_cli_spawn):
            with self.assertRaisesRegex(RuntimeError, 'CLI process failed to start'):
                co._invoke_once('author', 'AUTHOR_PERMISSION_PROBE', 'disposable workspace probe',
                                rc.review_schema(verified=False), fresh=True,
                                workspace_override=disposable)
        self.assertEqual(captured['cwd'], disposable.resolve())
        command = captured['command']
        settings = json.loads(command[command.index('--settings') + 1],
                               object_pairs_hook=unique_json_object)
        filesystem = settings['sandbox']['filesystem']
        self.assertNotIn('allowWrite', filesystem)
        self.assertEqual(filesystem['denyWrite'], [str(co.run_dir)])
        self.assertEqual(command[command.index('--add-dir') + 1], str(co.context))
        self.assertNotIn(str(co.workspace), json.dumps(filesystem))
        self.assertNotIn(str(disposable), json.dumps(filesystem))
        self.assertEqual(settings['permissions']['deny'], [
            f'Edit(//{co.run_dir.as_posix().lstrip("/")}/**)'])

    def test_claude_author_routes_gate_to_fresh_readonly_codex(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--author-vendor', 'claude', '--reviewer-vendor', 'codex', '--gate-model', 'gpt-6-luna'])
        co = rc.Coordinator(args)
        schema = self.root / 'gate-schema.json'
        rc.atomic_json(schema, rc.gate_schema())
        author = co.command('author', schema, False)
        gate = co.command('gate', schema, True)
        self.assertIn('acceptEdits', author)
        self.assertEqual(gate[0], 'codex')
        self.assertIn('sandbox_mode="read-only"', gate)
        self.assertNotIn('resume', gate)

    def test_workitem_reviewer_allowlist_change_holds_without_widening(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--codex-bin', str(FAKE), '--claude-bin', str(self.fake_claude_cli())])
        co = rc.Coordinator(args)
        frozen = list(co.state['config']['workitem_reviewer_commands'])
        old_report = {'status': 'PASS', 'reviewer_flags_digest': co.reviewer_flags_digest(),
                      'author_flags_digest': co.author_flags_digest(),
                      'author_permission_probe': {'status': 'PASS'},
                      'global_config_changes': {'status': 'PASS'}}
        (co.run_dir / 'permission-probe.json').write_text(json.dumps(old_report))
        with patch.dict(os.environ, {'FAKE_APPEND_REVIEWER_COMMAND': 'node injected-reviewer.js',
                                     'FAKE_APPEND_REVIEWER_COMMAND_FILE': str(self.workitem)}):
            co._invoke_once('author', 'EXEC', 'Role: persistent codex implementer. Phase: EXEC.\n', rc.author_schema())
        self.assertEqual(rc.workitem_reviewer_commands(self.workitem.read_text()), ['node injected-reviewer.js'])
        before = (co.state['invocations_used'], len(co.state['turns']))
        with self.assertRaisesRegex(RuntimeError, 'allowlist changed; run permission-probe'):
            co._invoke_once('reviewer', 'EXEC', co._review_prompt('reviewer', 'snapshot'), rc.review_schema())
        self.assertEqual((co.state['invocations_used'], len(co.state['turns'])), before)
        self.assertEqual(co.state['config']['workitem_reviewer_commands'], frozen)
        self.assertEqual(co.state['status'], 'HOLD')
        probe_args = rc.parser().parse_args(['permission-probe', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--codex-bin', co.state['config']['codex_bin'], '--claude-bin', co.state['config']['claude_bin']])
        refreshed = rc.Coordinator(probe_args)
        self.assertEqual(refreshed.state['config']['workitem_reviewer_commands'],
                         ['node injected-reviewer.js'])
        self.assertFalse(refreshed.probe_passed()[0], 'refresh invalidates the prior probe digest')

    def test_legacy_run_without_frozen_reviewer_allowlist_holds_until_probe(self):
        co = self.coordinator()
        co.state['config'].pop('workitem_reviewer_commands')
        co.save()
        with self.workitem.open('a') as output:
            output.write('\n```reviewer-commands\nnode legacy-command.js\n```\n')
        co.args.action = 'resume'
        with self.assertRaisesRegex(RuntimeError, 'allowlist is not frozen; run permission-probe'):
            co.reviewer_commands()
        self.assertEqual(co.state['status'], 'HOLD')
        co.args.action = 'permission-probe'
        self.assertIn('node legacy-command.js', co.reviewer_commands())
        self.assertIn('workitem_reviewer_commands', co.state['config'])

    def test_optional_reviewer_command_is_an_exact_rule(self):
        run_dir = self.root / 'extra-command-run'
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(run_dir),
            '--reviewer-command', 'node check.js'])
        co = rc.Coordinator(args)
        schema = self.root / 'extra-schema.json'
        rc.atomic_json(schema, rc.review_schema())
        command = co.command('reviewer', schema, False)
        allowed = allowed_tool_values(command)
        self.assertIn('Bash(node check.js)', allowed)
        self.assertNotIn('Bash(node check.js:*)', ','.join(allowed))

    def test_fake_claude_rejects_comma_joined_argument_bearing_bash_rules(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--reviewer-command', 'node check.js'])
        co = rc.Coordinator(args)
        schema = self.root / 'allowed-tools-schema.json'
        rc.atomic_json(schema, rc.review_schema())
        command = co.command('reviewer', schema, False)
        allowed = allowed_tool_values(command)
        self.assertGreaterEqual(sum(value.startswith('Bash(') for value in allowed), 2)
        args = command[1:]
        invalid, index = [], 0
        while index < len(args):
            if args[index] == '--allowedTools':
                index += 2
            else:
                invalid.append(args[index])
                index += 1
        invalid += ['--allowedTools', ','.join(allowed)]
        result = subprocess.run([sys.executable, str(FAKE), *invalid], input='offline contract test',
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 2)
        self.assertIn('require separate --allowedTools arguments', result.stderr)

    def test_all_readonly_roles_receive_extra_exact_commands_and_probe_digest_covers_them(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--reviewer-command', 'node corpus.js'])
        co = rc.Coordinator(args)
        schema = self.root / 'all-role-schema.json'
        rc.atomic_json(schema, rc.review_schema())
        for role in ('reviewer', 'shadow', 'gate'):
            command = co.command(role, schema, role != 'reviewer')
            if '--allowedTools' in command:
                self.assertIn('Bash(node corpus.js)', allowed_tool_values(command))
        self.assertIn('node corpus.js', co._review_prompt('shadow', 'snapshot'))
        self.assertIn('node corpus.js', co._gate_prompt('snapshot'))
        digest = co.reviewer_flags_digest()
        prior_surface = co.reviewer_flags()
        prior_surface['surface_version'] = 5
        prior_digest = __import__('hashlib').sha256(json.dumps(
            prior_surface, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertNotEqual(digest, prior_digest, 'surface bump must stale pre-sandbox permission probes')
        args_without = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.root / 'other-run')])
        self.assertNotEqual(digest, rc.Coordinator(args_without).reviewer_flags_digest())

    def test_fresh_roles_have_no_ledger_channel_and_leaks_are_rejected_before_launch(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        co.record_findings('persistent-reviewer', 'EXEC', 1, [{
            'severity': 'MINOR', 'file': 'tracked.txt', 'summary': 'secret persistent summary',
            'failure_scenario': 'secret scenario'}])
        shadow_prompt = co._review_prompt('shadow', 'snapshot')
        gate_prompt = co._gate_prompt('snapshot')
        for prompt in (shadow_prompt, gate_prompt):
            self.assertNotIn('F001', prompt)
            self.assertNotIn('secret persistent summary', prompt)
            self.assertNotIn('prior_findings', prompt)
        self.assertNotIn('prior_findings', rc.fresh_review_schema()['properties'])
        sequence = co.state['sequence']
        with patch('subprocess.Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'independence check rejected prompt leak'):
                co.invoke('shadow', 'EXEC', shadow_prompt + '\nF001: secret persistent summary',
                          rc.fresh_review_schema(), fresh=True)
            popen.assert_not_called()
        self.assertEqual(co.state['sequence'], sequence)

    def test_plan_prompts_omit_exec_instructions(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        reviewer = co._review_prompt('reviewer', 'snapshot')
        for forbidden in ('Run this test command', 'delta.patch',
                          'self_run_evidence', 'Inspect the complete current delta'):
            self.assertNotIn(forbidden, reviewer)
        self.assertIn('No implementation exists yet and none is expected', reviewer)
        self.assertNotIn('Do not run commands', reviewer)
        self.assertIn('Read existing workspace source', reviewer)
        self.assertIn('Commands you may run', reviewer)
        self.assertIn('missing source files, implementation, or tests are out of scope', reviewer)
        author = co._author_prompt()
        self.assertIn('PLAN only: return a plan', author)
        self.assertIn('do not implement, edit the workspace, or run implementation checks', author)
        self.assertNotIn('For long commands', author)
        self.assertIn('Include this exact verification command in the plan', author)
        self.assertIn('npm test', author)
        self.assertIn('coordinator dispatches the independent reviewer', author)
        self.assertIn('Use HOLD only when a required product/authorization decision is missing', author)

    def test_test_command_executable_preflight_fails_before_state_creation(self):
        command = self.command('--test-command', 'paired-session-command-that-does-not-exist')
        result = subprocess.run(command, cwd=self.root, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 2)
        self.assertIn('REFUSED: configured test executable is missing or not on PATH', result.stdout)
        self.assertFalse(self.run_dir.exists())
        self.assertEqual((self.workspace / 'tracked.txt').read_text(), 'base\n')

    def test_project_json_config_sets_defaults_and_cli_values_override(self):
        config_dir = self.workspace / '.review-loop'
        config_dir.mkdir()
        (config_dir / 'paired-session.json').write_text(json.dumps({
            'author_vendor': 'codex', 'author_model': 'gpt-6-luna', 'author_effort': 'low',
            'reviewer_command': ['python3 -m unittest'], 'max_invocations': 17,
        }))
        args = rc.configure_parser(rc.parser(), [
            'run', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
            '--run-dir', str(self.run_dir), '--author-effort=high',
        ]).parse_args(['run', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                       '--run-dir', str(self.run_dir), '--author-effort=high'])
        self.assertEqual(args.author_vendor, 'codex')
        self.assertEqual(args.author_model, 'gpt-6-luna')
        self.assertEqual(args.author_effort, 'high')
        self.assertEqual(args.reviewer_command, ['python3 -m unittest'])
        self.assertEqual(args.max_invocations, 17)

    def test_role_model_defaults_follow_vendor_pinned_adr(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        rc.Coordinator(args)
        self.assertEqual((args.author_model, args.reviewer_model, args.gate_model),
                         ('gpt-6-luna', 'claude-opus-5-5', 'claude-opus-5-5'))
        swapped = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.root / 'swapped-model-run'),
            '--author-vendor', 'claude', '--reviewer-vendor', 'codex'])
        rc.Coordinator(swapped)
        self.assertEqual((swapped.author_model, swapped.reviewer_model, swapped.gate_model),
                         ('claude-opus-5-5', 'gpt-6-luna', 'gpt-6-luna'))

    def test_adr8_refuses_explicit_legacy_codex_author_model(self):
        with patch('sys.stdout', new=io.StringIO()) as output:
            result = rc.main(['run', '--workspace', str(self.workspace),
                '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
                '--author-model', 'gpt-6-sol'])
        self.assertEqual(result, 2)
        self.assertIn('author_model must be gpt-6-luna', output.getvalue())
        self.assertIn('ADR-8', output.getvalue())
        self.assertFalse((self.run_dir / 'state.json').exists())

    def test_example_config_uses_adr8_pins(self):
        example = json.loads(Path(__file__).with_name('paired-session-config.example.json').read_text())
        self.assertEqual(example['author_model'], 'gpt-6-luna')
        self.assertEqual(example['reviewer_model'], 'claude-opus-5-5')
        self.assertEqual(example['gate_model'], 'claude-opus-5-5')
        rc.validate_role_models(rc.configure_parser(rc.parser(), [
            'run', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
            '--run-dir', str(self.run_dir), '--config',
            str(Path(__file__).with_name('paired-session-config.example.json')),
        ]).parse_args(['run', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                       '--run-dir', str(self.run_dir), '--config',
                       str(Path(__file__).with_name('paired-session-config.example.json'))]))

    def test_vendor_override_rejects_incompatible_profile_models_before_state_creation(self):
        config_dir = self.workspace / '.review-loop'
        config_dir.mkdir()
        (config_dir / 'paired-session.json').write_text(json.dumps({
            'author_vendor': 'codex', 'author_model': 'gpt-6-luna',
            'reviewer_vendor': 'claude', 'reviewer_model': 'claude-opus-5-5',
            'gate_model': 'claude-opus-5-5', 'test_command': 'python3 -m unittest',
        }))
        with patch('sys.stdout', new=io.StringIO()) as output:
            result = rc.main(['run', '--workspace', str(self.workspace),
                '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
                '--author-vendor', 'claude'])
        self.assertEqual(result, 2)
        self.assertIn('author_model must be claude-opus-5-5', output.getvalue())
        self.assertFalse(self.run_dir.exists())

    def test_project_json_config_rejects_unknown_keys(self):
        config_dir = self.workspace / '.review-loop'
        config_dir.mkdir()
        (config_dir / 'paired-session.json').write_text('{"skip_probe": true}')
        with self.assertRaisesRegex(ValueError, 'unsupported paired-session config keys: skip_probe'):
            rc.configure_parser(rc.parser(), ['run', '--workspace', str(self.workspace),
                '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])

    def test_lifecycle_config_is_frozen_while_default_route_stays_off(self):
        co = self.coordinator('--docs-file', 'docs/guide.md', '--docs-allowlist', 'docs/other.md',
                              '--skip-globs', 'generated/**', '--skip-quality-polish', 'true')
        saved = co.state['config']
        self.assertEqual(saved['lifecycle_mode'], 'off')
        self.assertTrue(saved['skip_quality_polish'])
        self.assertEqual(saved['skip_globs'], ['generated/**'])
        self.assertEqual(saved['docs_file'], str(self.workspace / 'docs/guide.md'))
        self.assertEqual(saved['docs_allowlist'], sorted([str(self.workspace / 'docs/guide.md'),
                                                          str(self.workspace / 'docs/other.md')]))
        args = rc.parser().parse_args(['resume', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli()),
            '--docs-file', 'docs/changed.md', '--docs-allowlist', 'docs/other.md',
            '--skip-globs', 'generated/**', '--skip-quality-polish', 'true'])
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: docs_file'):
            rc.Coordinator(args)

    def test_lifecycle_on_and_old_done_are_refused_before_dispatch(self):
        self.run_dir = self.root / 'lifecycle-refusal'
        for flags, message in ((['--lifecycle-mode', 'on'], 'lifecycle remains disabled'),
                               (['--lifecycle-mode', 'on', '--adversarial-gate', 'off'], 'lifecycle refuses --adversarial-gate off')):
            with self.subTest(flags=flags):
                args = rc.parser().parse_args(self.command(*flags)[2:])
                with self.assertRaisesRegex(ValueError, message): rc.Coordinator(args)
                self.assertFalse((self.run_dir / 'state.json').exists())
        co = self.coordinator()
        for status in ('DONE', 'ACCEPTED'):
            with self.subTest(status=status):
                co.state['status'] = status
                co.state['config']['lifecycle_mode'] = 'on'
                co.save()
                args = rc.parser().parse_args(self.command()[2:]); args.action = 'resume'
                with self.assertRaisesRegex(ValueError, 'saved lifecycle run cannot resume'):
                    rc.Coordinator(args)
        self.assertEqual(len(co.state['turns']), 0)

    def test_fake_lifecycle_state_receipts_resume_idempotently_and_cli_stays_off(self):
        command = self.command('--lifecycle-mode', 'on')
        args = rc.parser().parse_args(command[2:])
        with self.assertRaisesRegex(ValueError, 'lifecycle remains disabled'):
            rc.Coordinator(args)
        co = rc.Coordinator(args, _fake_lifecycle=True)
        life = co.state['lifecycle']
        self.assertEqual((life['stage'], life['epoch'], life['candidate_oid']), ('EXEC', 0, None))
        self.assertEqual(life['parent'], co.state['base_commit'])
        request = {key: life[key] for key in ('item_uuid', 'stage', 'epoch', 'candidate_oid', 'parent')}
        request.update(request_id='fake-request-1', role='reviewer')
        co.fake_lifecycle_event('begin', request)
        co.fake_lifecycle_event('begin', request)
        self.assertEqual(json.loads(co.state_path.read_text())['lifecycle']['pending'], request)
        resume = command.copy(); resume[2] = 'resume'
        with self.assertRaisesRegex(ValueError, 'lifecycle remains disabled'):
            rc.Coordinator(rc.parser().parse_args(resume[2:]))
        again = rc.Coordinator(rc.parser().parse_args(resume[2:]), _fake_lifecycle=True)
        self.assertEqual(again.state['lifecycle']['pending'], request)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            again.fake_lifecycle_event('receipt', {**request, 'epoch': 1, 'status': 'READY'})
        self.assertEqual(again.state['lifecycle']['receipts'], [])
        receipt = {**request, 'status': 'READY', 'output_sha256': 'a' * 64}
        again.fake_lifecycle_event('receipt', receipt)
        reloaded = rc.Coordinator(rc.parser().parse_args(resume[2:]), _fake_lifecycle=True)
        reloaded.fake_lifecycle_event('receipt', receipt)
        self.assertEqual(reloaded.state['lifecycle']['receipts'], [receipt])
        self.assertIsNone(reloaded.state['lifecycle']['pending'])
        with self.assertRaisesRegex(ValueError, 'already completed'):
            reloaded.fake_lifecycle_event('begin', request)
        self.assertEqual(reloaded.state['lifecycle']['stage'], 'EXEC')
        with self.assertRaisesRegex(RuntimeError, 'cannot enter legacy drive'):
            reloaded.drive()
        with self.assertRaisesRegex(RuntimeError, 'cannot enter legacy resume'):
            reloaded.resume()
        with self.assertRaisesRegex(RuntimeError, 'cannot dispatch a real provider'):
            reloaded._invoke_once('author', 'EXEC', 'fake-only', {})

    def test_fake_lifecycle_guard_rejects_real_provider_path(self):
        command = self.command('--lifecycle-mode', 'on', '--codex-bin', '/usr/bin/true')
        with self.assertRaisesRegex(ValueError, 'lifecycle remains disabled'):
            rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        bare = self.command('--lifecycle-mode', 'on', '--codex-bin', 'codex')
        with self.assertRaisesRegex(ValueError, 'lifecycle remains disabled'):
            rc.Coordinator(rc.parser().parse_args(bare[2:]), _fake_lifecycle=True)
        with patch.dict(os.environ, {'FAKE_CODEX_TEST_ROOT': '/'}):
            normal = self.command('--lifecycle-mode', 'on')
            with self.assertRaisesRegex(ValueError, 'lifecycle remains disabled'):
                rc.Coordinator(rc.parser().parse_args(normal[2:]), _fake_lifecycle=True)
        self.assertFalse((self.run_dir / 'state.json').exists())

    def test_fake_lifecycle_router_follows_approved_oid_to_security_boundary(self):
        command = self.command('--lifecycle-mode', 'on')
        co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        life = co.state['lifecycle']
        approved = {'status': 'APPROVE', 'epoch': 0, 'item_uuid': life['item_uuid'],
                    'run_id': self.run_dir.name, 'parent': life['parent'], 'candidate_oid': 'a' * 40}
        self.assertEqual(co.fake_lifecycle_route(approved, stub_mode=True), 'STOP_BEFORE_SECURITY')
        saved = json.loads(co.state_path.read_text())['lifecycle']
        self.assertEqual([row['stage'] for row in saved['receipts']],
                         ['EXEC', 'FINISH', 'POLISH-Q', 'DOCS'])
        self.assertEqual(saved['candidate_oid'], 'a' * 40)
        self.assertEqual(saved['epoch'], 0)
        self.assertEqual(co.state['turns'], [])
        self.assertEqual(co.fake_lifecycle_route(approved, stub_mode=True), 'STOP_BEFORE_SECURITY')
        self.assertEqual(len(co.state['lifecycle']['receipts']), 4)

    def test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog(self):
        docs = self.workspace / 'docs' / 'guide.md'
        docs.parent.mkdir()
        docs.write_text('# Draft guide\n')
        backlog = self.workspace / 'BACKLOG.md'
        backlog.write_text('# Backlog\n**Last updated**: 2026-09-30\n\n## P0\n(none)\n## P1\n'
                           '- Fix sums. (added 2026-09-29)\n  - Keep details.\n## P2\n(none)\n'
                           '## P3\n(none)\n## Done\n(none)\n')
        ignore = self.workspace / '.gitignore'
        ignore.write_text(ignore.read_text() + '\n.compass/\n' if ignore.exists() else '.compass/\n')
        subprocess.run(['git', 'add', 'docs/guide.md', 'BACKLOG.md', '.gitignore'],
                       cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'Q fixture'], cwd=self.workspace, check=True)
        view = self.workspace / '.compass/backlog-last-view.json'
        view.parent.mkdir(exist_ok=True)
        view.write_text(json.dumps({'generated_at': rc.datetime.now().astimezone().isoformat(),
                         'source_path': str(backlog),
                         'items': [{'id': 1, 'section': 'P1', 'title_span': 'Fix sums'}]}))
        original = backlog.read_bytes()
        command = self.command('--lifecycle-mode', 'on', '--stop-after-plan', '--skip-probe',
                               '--docs-file', 'docs/guide.md', '--test-command', 'python3 -c pass')
        co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        self.assertEqual(co.fake_lifecycle_drive(backlog_item=1), 'STOP_BEFORE_SECURITY')
        with self.assertRaisesRegex(ValueError, 'current SECURITY pass'):
            co.fake_materialize_q('a' * 40, '2026-09-30')
        ingest = co.state['fake_ingest_receipt']
        baseline = ct.baseline_from_binding(ingest['baseline'])
        revision = ct.CandidateRevision(co.state['lifecycle']['candidate_oid'], tuple(ingest['manifest']), 0)
        def security(request):
            co._fake_dispatching = True
            try:
                result = co.invoke('reviewer', 'SECURITY',
                    'Role: reviewer, fresh. Phase: SECURITY.\n'
                    'Run this test command exactly as written in one Bash call: python3 -c pass',
                    rc.review_schema(), fresh=True, workspace_override=baseline.root)
            finally:
                co._fake_dispatching = False
            turn = next(r for r in co.state['turns'] if r['sequence'] == result['sequence'])
            return {'status': result['answer']['status'], 'candidate_oid': request['candidate_oid'],
                    'findings': result['answer']['full_review'], 'observed_tools': turn['observed_tool_calls']}
        self.assertEqual(co.fake_lifecycle_route(None, chain_only=True,
                         security_context={'baseline': baseline, 'revision': revision, 'review': security}),
                         'STOP_BEFORE_DELIVERY')
        c1 = rc.delivery_seal.c1(co, baseline, revision)
        state = rc.copy.deepcopy(co.state)
        index = baseline.index.read_bytes()
        result = co.fake_materialize_q(c1, '2026-09-30')
        self.assertEqual(result['status'], 'UNREVIEWED')
        self.assertEqual(co.state, state)
        self.assertEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')
        self.assertEqual(backlog.read_bytes(), original)
        self.assertEqual(baseline.index.read_bytes(), index)
        self.assertNotIn('Q', [r['stage'] for r in co.state['lifecycle']['receipts']])
        with self.assertRaises(ValueError):
            co.accept()

        cases = [('stage', lambda: co.state['lifecycle'].update(stage='EXEC')),
                 ('pending', lambda: co.state['lifecycle'].update(pending={'id': 'pending'})),
                 ('active', lambda: co.state.update(active={'sequence': 1})),
                 ('uncertain', lambda: co.state.update(uncertain_active={'sequence': 1})),
                 ('receipt-stage', lambda: co.state['lifecycle']['receipts'][-1].update(stage='DOCS')),
                 ('receipt-status', lambda: co.state['lifecycle']['receipts'][-1].update(status='HOLD')),
                 ('security-verdict', lambda: co.state['lifecycle']['receipts'][-1].update(security_review='REVISE')),
                 ('oid', lambda: co.state['lifecycle']['receipts'][-1].update(output_oid='a' * 40)),
                 ('item', lambda: co.state.pop('closeout_item'))]
        for name, mutate in cases:
            with self.subTest(guard=name):
                co.state = rc.copy.deepcopy(state)
                mutate()
                before = rc.copy.deepcopy(co.state)
                with self.assertRaisesRegex(ValueError, 'current SECURITY pass'):
                    co.fake_materialize_q(c1, '2026-09-30')
                self.assertEqual(co.state, before)
        co.state = rc.copy.deepcopy(state)
        with patch.object(co, 'blocking_open_findings', return_value=[{'id': 'F001'}]):
            with self.assertRaisesRegex(ValueError, 'current SECURITY pass'):
                co.fake_materialize_q(c1, '2026-09-30')
        self.q_test_fixture = (co, c1, rc.copy.deepcopy(co.state))
        reviewed = co.fake_q_review(c1, '2026-09-30')
        self.assertEqual(reviewed['status'], 'UNREVIEWED')
        self.assertEqual(reviewed['oid'], result['q_oid'])
        self.assertEqual(reviewed['returncode'], 0)
        self.assertEqual(json.loads((co.evidence / (reviewed['review_id'] + '-q-review.json')).read_text()), reviewed)
        turn = next(t for t in co.state['turns'] if t['sequence'] == reviewed['sequence'])
        self.assertEqual(turn['role'], 'reviewer')
        self.assertEqual(turn['answer']['status'], 'APPROVE')
        self.assertEqual(backlog.read_bytes(), original)
        self.assertEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')
        with self.assertRaisesRegex(ValueError, 'pending or completed'):
            co.fake_q_review(c1, '2026-09-30')

        self.assertNotIn('pending_reviewer_result_sequence', co.state)
        self.assertEqual(co.state['q_reserved'], 6)
        self.assertEqual(turn['phase'], 'Q')
        source, root, revision = rc.q_evidence.review_source(co, c1, '2026-09-30', rc.observed_test_succeeded)
        self.assertEqual(source, reviewed)
        self.assertEqual(revision.tree_oid, result['q_oid'])
        self.assertEqual(str(root.root), turn['workspace'])
        unchanged = rc.copy.deepcopy(co.state)
        for field, value in [('phase', 'EXEC'), ('workspace', str(self.workspace)),
                             ('sequence', reviewed['review_after_sequence']), ('error', 'failed')]:
            with self.subTest(q_source=field):
                present = field in turn
                original = turn.get(field)
                turn[field] = value
                with self.assertRaises(ValueError):
                    rc.q_evidence.review_source(co, c1, '2026-09-30', rc.observed_test_succeeded)
                if present:
                    turn[field] = original
                else:
                    turn.pop(field)
        self.assertEqual(co.state, unchanged)
        co.state['fake_q_review']['status'] = 'REVIEWED'
        with self.assertRaisesRegex(ValueError, 'protected evidence'):
            rc.q_evidence.review_source(co, c1, '2026-09-30', rc.observed_test_succeeded)
        co.state = unchanged

    def test_fake_q_empty_approval_cannot_create_review_receipt(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        co.state = rc.copy.deepcopy(before)
        old = next(t for t in co.state['turns'] if t['role'] == 'reviewer')
        old['observed_commands'] = []
        with patch.object(co, 'invoke', return_value={'sequence': old['sequence'],
                         'answer': {'status': 'APPROVE', 'full_review': []}}):
            with self.assertRaisesRegex(ValueError, 'reviewer did not approve'):
                co.fake_q_review(c1, '2026-09-30')
        self.assertIn('fake_q_pending', co.state)
        self.assertNotIn('fake_q_review', co.state)
        self.assertFalse(co._fake_dispatching)

    def test_fake_q_reservation_refuses_p_dispatch_and_stage_begin(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        co.state = rc.copy.deepcopy(before)
        co.args.max_invocations = co.state['invocations_used'] + co.state['q_reserved']
        sequence = co.state['sequence']
        co._fake_dispatching = True
        try:
            with self.assertRaisesRegex(RuntimeError, 'invocation limit'):
                co.invoke('reviewer', 'EXEC', 'Role: reviewer, fresh.', rc.review_schema(), fresh=True)
        finally:
            co._fake_dispatching = False
        self.assertEqual(co.state['sequence'], sequence)
        co.args.max_invocations = co.state['invocations_used'] + co.state['q_reserved'] + 1
        with self.assertRaisesRegex(ValueError, 'P budget exhausted before stage begin'):
            co._fake_lifecycle_event('begin', {})
        self.assertIsNone(co.state['lifecycle']['pending'])

    def test_fake_q_retry_fits_reserved_two_slots_and_missing_tools_refuses(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        co.state = rc.copy.deepcopy(before)
        used = co.state['invocations_used']
        co.args.max_invocations = used + 8
        with patch.dict(os.environ, {'FAKE_EMPTY_CLAIMS': 'reviewer'}):
            co.fake_q_review(c1, '2026-09-30')
        self.assertEqual(co.state['invocations_used'], used + 2)
        self.assertEqual(co.state['q_reserved'], 6)
        co.state = rc.copy.deepcopy(before)
        with patch.dict(os.environ, {'FAKE_MISSING_OBSERVED': '1'}):
            with self.assertRaisesRegex(ValueError, 'reviewer did not approve'):
                co.fake_q_review(c1, '2026-09-30')
        self.assertNotIn('fake_q_review', co.state)
        self.assertIn('fake_q_pending', co.state)

    def test_fake_q_freeze_refuses_insufficient_budget_before_plan(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan',
                 '--skip-probe', '--run-dir', str(self.root / 'small-budget'), '--max-invocations', '8',
                 '--docs-file', 'docs/guide.md', '--test-command', 'python3 -c pass')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        with self.assertRaisesRegex(ValueError, 'Q reservation leaves no P budget'):
            co.fake_lifecycle_drive(backlog_item=1)
        self.assertEqual(co.state['turns'], [])
        self.assertNotIn('closeout_item', co.state)
        self.assertNotIn('q_reserved', co.state)

    def test_fake_q_reserved_polish_preflight_rejects_short_retry_budget_before_begin(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        co.state = rc.copy.deepcopy(before)
        co.state['lifecycle']['stage'] = 'POLISH-Q'
        co.args.max_invocations = co.state['invocations_used'] + co.state['q_reserved'] + 2
        sequence = co.state['sequence']
        callback = unittest.mock.Mock(side_effect=AssertionError('preflight must not dispatch'))
        with patch.dict(os.environ, {'FAKE_EMPTY_CLAIMS': 'reviewer'}):
            with self.assertRaisesRegex(ValueError, 'POLISH-Q specialist budget exhausted'):
                co.fake_lifecycle_route(None, chain_only=True, polish_context={'python-reviewer': callback})
        callback.assert_not_called()
        self.assertIsNone(co.state['lifecycle']['pending'])
        self.assertEqual(co.state['sequence'], sequence)

    def test_fake_q_reserved_polish_retry_dispatch_fits_exact_boundary(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        co.state = rc.copy.deepcopy(before)
        life = co.state['lifecycle']
        life['stage'] = 'POLISH-Q'
        # Budget fixture resumes the completed EXEC/FINISH prefix, before its first specialist call.
        boundary = next(i for i, row in enumerate(life['receipts'])
                        if row['stage'] == 'POLISH-Q' and row['epoch'] == life['epoch'])
        life['receipts'] = life['receipts'][:boundary]
        used = co.state['invocations_used']
        reserved = co.state['q_reserved']
        sequence = co.state['sequence']
        co.args.max_invocations = used + reserved + 3
        def specialist(request):
            co._fake_dispatching = True
            try:
                result = co.invoke('reviewer', 'POLISH',
                    'Role: reviewer, fresh. Phase: POLISH.\n'
                    'Run this test command exactly as written in one Bash call: python3 -c pass',
                    rc.review_schema(), fresh=True,
                    workspace_override=Path(co.state['fake_candidate_test']['root']))
            finally:
                co._fake_dispatching = False
            return {'candidate_oid': request['candidate_oid'], 'status': result['answer']['status'],
                    'findings': result['answer']['full_review']}
        with patch.dict(os.environ, {'FAKE_EMPTY_CLAIMS': 'reviewer'}):
            with self.assertRaisesRegex(ValueError, 'DOCS requires a candidate-bound fake writer'):
                co.fake_lifecycle_route(None, chain_only=True, polish_context={'python-reviewer': specialist})
        self.assertEqual(co.state['invocations_used'], used + 3)
        self.assertEqual(co.state['q_reserved'], reserved)
        self.assertEqual(co.state['sequence'], sequence + 2)
        self.assertLessEqual(co.state['lifecycle']['specialist_counts']['python-reviewer'],
                             rc.budget_policy.BUDGET_CAPS['specialist'][0])
        self.assertEqual(co.state['lifecycle']['receipts'][-1]['stage'], 'POLISH-Q')

    def test_fake_q_source_revise_minor_keeps_raw_and_effective_proof(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        co.state = rc.copy.deepcopy(before)
        with patch.dict(os.environ, {'FAKE_Q_MINOR': '1', 'FAKE_Q_REVISE': '1'}):
            source = co.fake_q_review(c1, '2026-09-30')
        self.assertEqual(source['status'], 'UNREVIEWED')
        self.assertEqual(source['proof']['raw_verdict'], 'REVISE')
        self.assertEqual(source['proof']['effective_verdict'], 'APPROVE_WITH_ADVISORY')
        self.assertEqual(source['proof']['advisories'][0]['severity'], 'MINOR')
        turn = next(t for t in co.state['turns'] if t['sequence'] == source['sequence'])
        self.assertEqual(turn['answer']['status'], 'REVISE')
        self.assertIsNot(source['proof']['advisories'], turn['answer']['full_review'])
        verified, root, revision = rc.q_evidence.review_source(co, c1, '2026-09-30', rc.observed_test_succeeded)
        self.assertEqual(verified, source)
        self.assertEqual(revision.tree_oid, source['oid'])
        record = next(r for r in co.state['review_verdicts'] if r['sequence'] == turn['sequence'])
        self.assertEqual(record['reviewer_raw_verdict'], 'REVISE')
        self.assertEqual(record['effective_verdict'], 'APPROVE_WITH_ADVISORY')

    def test_fake_q_source_revise_major_security_or_empty_is_rejected(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        for severity, security, empty in [('MAJOR', '', ''), ('MINOR', '1', ''), ('MINOR', '', '1')]:
            with self.subTest(severity=severity, security=security, empty=empty):
                co.state = rc.copy.deepcopy(before)
                with patch.dict(os.environ, {'FAKE_Q_MINOR': '1', 'FAKE_Q_REVISE': '1',
                                             'FAKE_Q_SEVERITY': severity, 'FAKE_Q_SECURITY_FLAG': security,
                                             'FAKE_Q_EMPTY_REVISE': empty}):
                    with self.assertRaisesRegex(ValueError, 'Q reviewer did not approve'):
                        co.fake_q_review(c1, '2026-09-30')
                self.assertNotIn('fake_q_review', co.state)
                self.assertIn('fake_q_pending', co.state)

    def test_q_proof_policy_is_advisory_only_and_copies_findings(self):
        co = self.coordinator()
        state = rc.copy.deepcopy(co.state)
        for role, verdict, severity, security, expected in [
                ('reviewer', 'APPROVE', None, False, 'APPROVE'),
                ('reviewer', 'APPROVE', 'MINOR', False, 'APPROVE_WITH_ADVISORY'),
                ('reviewer', 'REVISE', 'LOW', False, 'APPROVE_WITH_ADVISORY'),
                ('reviewer', 'REVISE', None, False, None),
                ('reviewer', 'REVISE', 'MAJOR', False, None),
                ('reviewer', 'REVISE', 'MINOR', True, None),
                ('reviewer', 'HOLD', 'MINOR', False, None),
                ('gate', 'approve', None, False, 'APPROVE'),
                ('gate', 'approve', 'low', False, 'APPROVE_WITH_ADVISORY'),
                ('gate', 'needs-attention', 'low', False, 'APPROVE_WITH_ADVISORY'),
                ('gate', 'needs-attention', None, False, None),
                ('gate', 'needs-attention', 'medium', False, None)]:
            with self.subTest(role=role, verdict=verdict, severity=severity):
                findings = [{'severity': severity, 'security': security}] if severity else []
                answer = {'verdict': verdict, 'findings': findings} if role == 'gate' else {
                    'status': verdict, 'full_review': findings}
                turn = {'role': role, 'phase': 'Q', 'sequence': 1, 'answer': answer}
                if expected is None:
                    with self.assertRaisesRegex(ValueError, 'blocking or empty'):
                        co.q_proof(turn, 'a' * 40)
                else:
                    proof = co.q_proof(turn, 'a' * 40)
                    self.assertEqual(proof['effective_verdict'], expected)
                    self.assertEqual(proof['raw_verdict'], verdict)
                    self.assertIsNot(proof['advisories'], findings)
                    proof['advisories'].append({'local': 'mutation'})
                    self.assertNotIn({'local': 'mutation'}, findings)
        self.assertEqual(co.state, state)

    def test_fake_q_bundle_consumes_real_gate_final_security_and_p_noops(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        p_oid = co.state['lifecycle']['candidate_oid']
        used = co.state['invocations_used']
        co.args.max_invocations = used + 6
        with patch.dict(os.environ, {'FAKE_EMPTY_CLAIMS': 'reviewer'}):
            bundle = co.fake_q_complete(c1, '2026-09-30')
        self.assertEqual(bundle['status'], 'REVIEWED')
        self.assertEqual(bundle['source']['status'], 'UNREVIEWED')
        self.assertEqual(co.state['fake_q_review']['status'], 'UNREVIEWED')
        self.assertEqual([r['phase'] for r in bundle['proofs']], ['Q-GATE', 'Q-FINAL', 'Q-SECURITY'])
        self.assertEqual({r['stage'] for r in bundle['p_noops']}, {'FINISH', 'POLISH-Q', 'DOCS'})
        self.assertTrue(all(r['candidate_oid'] == r['output_oid'] == p_oid for r in bundle['p_noops']))
        self.assertEqual(co.state['q_reserved'], 0)
        self.assertEqual(co.state['invocations_used'], used + 5)
        previous = bundle['source']['sequence']
        for proof in bundle['proofs']:
            turn = next(t for t in co.state['turns'] if t['sequence'] == proof['sequence'])
            self.assertEqual(turn['phase'], proof['phase'])
            self.assertEqual(turn['workspace'], bundle['source']['root'])
            self.assertGreater(turn['sequence'], previous)
            self.assertTrue(any(rc.observed_test_succeeded(c, co.args.test_command)
                                for c in turn['observed_commands']))
            previous = turn['sequence']
        self.assertEqual(json.loads((co.evidence / (bundle['id'] + '-q-bundle.json')).read_text()), bundle)
        self.assertEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')
        self.assertNotIn('fake_q_bundle_pending', co.state)
        with self.assertRaisesRegex(ValueError, 'uncertain or unreserved'):
            co.fake_q_complete(c1, '2026-09-30')

    def test_fake_q_bundle_refuses_wrong_phase_missing_tools_and_relabeling(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        source_state = rc.copy.deepcopy(co.state)
        for kind in ('phase', 'workspace', 'earlier', 'missing-tools', 'gate-block', 'gate-empty-verdict', 'failed-test', 'relabel'):
            with self.subTest(kind=kind):
                co.state = rc.copy.deepcopy(source_state)
                real_invoke = co.invoke
                def invoke(*args, **kwargs):
                    result = real_invoke(*args, **kwargs)
                    turn = next(t for t in co.state['turns'] if t['sequence'] == result['sequence'])
                    if kind == 'phase':
                        turn['phase'] = 'EXEC'
                    elif kind == 'workspace':
                        turn['workspace'] = str(self.workspace)
                    elif kind == 'earlier':
                        turn['sequence'] = source_state['fake_q_review']['sequence']
                        result['sequence'] = turn['sequence']
                    elif kind == 'missing-tools':
                        turn['observed_commands'] = []
                    elif kind == 'gate-empty-verdict':
                        turn['answer']['verdict'] = 'needs-attention'
                    elif kind == 'failed-test':
                        turn['observed_commands'].append({'command': co.args.test_command, 'exit_code': 1})
                    return result
                if kind == 'relabel':
                    co.state['fake_q_review']['status'] = 'REVIEWED'
                env = {'FAKE_GATE_BLOCK': '1'} if kind == 'gate-block' else {}
                with patch.object(co, 'invoke', side_effect=invoke), patch.dict(os.environ, env):
                    with self.assertRaises(ValueError):
                        co.fake_q_complete(c1, '2026-09-30')
                self.assertNotIn('fake_q_bundle', co.state)
                self.assertEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')
                self.assertFalse(co._fake_dispatching)

    def test_fake_q_bundle_refuses_missing_noop_later_turn_and_short_reservation(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        source_state = rc.copy.deepcopy(co.state)
        for kind in ('missing-noop', 'noop-write', 'later-turn', 'reserved', 'budget'):
            with self.subTest(kind=kind):
                co.state = rc.copy.deepcopy(source_state)
                co.args.max_invocations = 192
                if kind == 'missing-noop':
                    co.state['lifecycle']['receipts'] = [r for r in co.state['lifecycle']['receipts']
                                                       if r['stage'] != 'FINISH']
                elif kind == 'noop-write':
                    next(r for r in reversed(co.state['lifecycle']['receipts'])
                         if r['stage'] == 'DOCS')['output_oid'] = 'a' * 40
                elif kind == 'later-turn':
                    co.state['sequence'] += 1
                elif kind == 'reserved':
                    co.state['q_reserved'] = 5
                else:
                    co.args.max_invocations = co.state['invocations_used'] + 5
                with patch.object(co, 'invoke', side_effect=AssertionError('preflight must not dispatch')) as dispatch:
                    with self.assertRaisesRegex(ValueError, 'P no-op|uncertain or unreserved'):
                        co.fake_q_complete(c1, '2026-09-30')
                dispatch.assert_not_called()
                self.assertNotIn('fake_q_bundle', co.state)
                self.assertNotIn('fake_q_bundle_pending', co.state)

    def test_fake_q_bundle_keeps_minor_advisories_without_restarting_run(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        with patch.dict(os.environ, {'FAKE_Q_MINOR': '1'}):
            bundle = co.fake_q_complete(c1, '2026-09-30')
        self.assertEqual(bundle['status'], 'REVIEWED')
        reviewers = [r for r in bundle['proofs'] if r['role'] == 'reviewer']
        self.assertEqual(len(reviewers), 2)
        self.assertTrue(all(r['advisories'] and r['advisories'][0]['severity'] == 'MINOR' for r in reviewers))
        self.assertFalse(co.state.get('fake_q_bundle_pending'))

    def test_fake_q_gate_low_is_advisory_but_reviewer_security_and_major_block(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        source_state = rc.copy.deepcopy(co.state)
        with patch.dict(os.environ, {'FAKE_GATE_LOW': '1'}):
            bundle = co.fake_q_complete(c1, '2026-09-30')
        self.assertEqual(bundle['status'], 'REVIEWED')
        gate = bundle['proofs'][0]
        self.assertEqual(gate['advisories'][0]['severity'], 'low')
        turn = next(t for t in co.state['turns'] if t['sequence'] == gate['sequence'])
        self.assertEqual(turn['answer']['verdict'], 'needs-attention')
        for severity, security in [('MAJOR', ''), ('SECURITY', ''), ('MINOR', '1')]:
            with self.subTest(severity=severity, security=security):
                co.state = rc.copy.deepcopy(source_state)
                with patch.dict(os.environ, {'FAKE_Q_MINOR': '1', 'FAKE_Q_SEVERITY': severity,
                                             'FAKE_Q_SECURITY_FLAG': security}):
                    with self.assertRaisesRegex(ValueError, 'Q verdict is blocking or empty'):
                        co.fake_q_complete(c1, '2026-09-30')
                self.assertNotIn('fake_q_bundle', co.state)
                self.assertIn('fake_q_bundle_pending', co.state)

    def test_fake_q_bundle_revise_minor_keeps_advisory_and_raw_verdict(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        with patch.dict(os.environ, {'FAKE_Q_MINOR': '1', 'FAKE_Q_REVISE': '1'}):
            bundle = co.fake_q_complete(c1, '2026-09-30')
        reviewers = [proof for proof in bundle['proofs'] if proof['role'] == 'reviewer']
        self.assertEqual(len(reviewers), 2)
        for proof in reviewers:
            self.assertEqual(proof['raw_verdict'], 'REVISE')
            self.assertEqual(proof['effective_verdict'], 'APPROVE_WITH_ADVISORY')
            turn = next(t for t in co.state['turns'] if t['sequence'] == proof['sequence'])
            self.assertEqual(turn['answer']['status'], 'REVISE')
            self.assertEqual(proof['advisories'], turn['answer']['full_review'])
            self.assertIsNot(proof['advisories'], turn['answer']['full_review'])
        rows = [row for row in co.state['finding_ledger'] if row['source'] == 'q-reviewer']
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['status'] == 'open' and row['advisory'] for row in rows))
        ledger = (co.run_dir / 'findings-ledger.md').read_text()
        self.assertTrue(all(row['id'] in ledger for row in rows))
        self.assertEqual(bundle['status'], 'REVIEWED')

    def test_fake_q_bundle_revise_major_security_and_empty_are_rejected(self):
        self.test_fake_q_materialization_is_unreviewed_and_never_closes_live_backlog()
        co, c1, before = self.q_test_fixture
        source_state = rc.copy.deepcopy(co.state)
        for case in ('major', 'security', 'empty'):
            with self.subTest(case=case):
                co.state = rc.copy.deepcopy(source_state)
                env = {'FAKE_Q_MINOR': '1', 'FAKE_Q_REVISE': '1'}
                env.update({'FAKE_Q_SEVERITY': 'MAJOR'} if case == 'major' else
                           {'FAKE_Q_SECURITY_FLAG': '1'} if case == 'security' else
                           {'FAKE_Q_EMPTY_REVISE': '1'})
                with patch.dict(os.environ, env):
                    with self.assertRaisesRegex(ValueError, 'Q verdict is blocking or empty'):
                        co.fake_q_complete(c1, '2026-09-30')
                self.assertNotIn('fake_q_bundle', co.state)
                self.assertIn('fake_q_bundle_pending', co.state)

    def test_fake_q_delivery_verifier_rechecks_disk_and_returns_detached_proof(self):
        self.test_fake_q_bundle_consumes_real_gate_final_security_and_p_noops()
        co, c1, before = self.q_test_fixture
        row, root, revision = rc.q_evidence.review_bundle(co, c1, '2026-09-30', rc.observed_test_succeeded)
        self.assertEqual(row, co.state['fake_q_bundle'])
        self.assertEqual(revision.tree_oid, row['source']['oid'])
        self.assertEqual(str(root.root), row['source']['root'])
        row['source']['proof']['advisories'].append({'local': True})
        self.assertNotIn({'local': True}, co.state['fake_q_review']['proof']['advisories'])
        binary = Path(co.args.codex_bin)
        original = binary.read_bytes()
        binary.write_bytes(original + b'\n# changed after Q\n')
        try:
            with self.assertRaisesRegex(ValueError, 'program binding changed'):
                rc.q_evidence.review_bundle(co, c1, '2026-09-30', rc.observed_test_succeeded)
        finally:
            binary.write_bytes(original)
        self.assertEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')

    def test_fake_q_delivery_verifier_refuses_each_proof_guard(self):
        self.test_fake_q_bundle_consumes_real_gate_final_security_and_p_noops()
        co, c1, before = self.q_test_fixture
        state = rc.copy.deepcopy(co.state)
        source = state['fake_q_review']
        source_turn = next(t for t in state['turns'] if t['sequence'] == source['sequence'])
        turn_path = co.evidence / f"{source_turn['sequence']:03d}-q-reviewer.receipt.json"
        original_turn = turn_path.read_bytes()
        bundle_path = co.evidence / (state['fake_q_bundle']['id'] + '-q-bundle.json')
        original_bundle = bundle_path.read_bytes()
        for case in ('pending', 'later', 'relabel', 'proof', 'disk-turn', 'failed-source-test',
                     'missing-proof', 'p-noop', 'preflight', 'role', 'source-alias'):
            with self.subTest(case=case):
                co.state = rc.copy.deepcopy(state)
                row = co.state['fake_q_bundle']
                if case == 'pending':
                    co.state['fake_q_bundle_pending'] = {'interrupted': True}
                elif case == 'later':
                    co.state['sequence'] += 1
                elif case == 'relabel':
                    row['status'] = 'UNREVIEWED'
                elif case == 'proof':
                    row['proofs'][-1]['oid'] = 'a' * 40
                elif case == 'disk-turn':
                    turn_path.write_text('{}')
                elif case == 'failed-source-test':
                    turn = next(t for t in co.state['turns'] if t['sequence'] == source['sequence'])
                    turn['observed_commands'].append({'command': co.args.test_command, 'exit_code': 1})
                    turn_path.write_text(json.dumps(turn))
                elif case == 'missing-proof':
                    row['proofs'].pop()
                elif case == 'p-noop':
                    row['p_noops'][0]['output_oid'] = 'a' * 40
                elif case == 'preflight':
                    row['scanned_paths'] = []
                elif case == 'role':
                    row['proofs'][-1]['role'] = 'gate'
                else:
                    row['source']['proof']['advisories'].append({'unexpected': True})
                if case not in ('pending', 'later', 'disk-turn', 'failed-source-test'):
                    bundle_path.write_text(json.dumps(row))
                try:
                    with self.assertRaises(ValueError):
                        rc.q_evidence.review_bundle(co, c1, '2026-09-30', rc.observed_test_succeeded)
                finally:
                    turn_path.write_bytes(original_turn)
                    bundle_path.write_bytes(original_bundle)
                self.assertEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')

    def hard_crash_publication_recovery(self, window):
        self.test_fake_delivery_intent_binds_unpublished_objects_and_accepts_exact_digest()
        co, c1, before = self.q_test_fixture
        args_path = self.root / 'publish-args.json'
        payload = vars(co.args).copy()
        payload.update({k: v for k, v in co.state['config'].items() if k in payload and k != 'gate_prompt'})
        args_path.write_text(json.dumps(payload))
        script = """
import argparse, json, os, signal, sys
from pathlib import Path
from paired_session import coordinator as rc, delivery_publish as dp, delivery_recover as dr
co = rc.Coordinator(argparse.Namespace(**json.loads(Path(sys.argv[1]).read_text())), _fake_lifecycle=True)
window = sys.argv[2]
original_git, original_replace = dp.ct._git, os.replace
original_bytes = dp.ct._git_bytes
def git_bytes(args, **kwargs):
    result = original_bytes(args, **kwargs)
    if window == 'pre-cas' and args[0] == 'index-pack':
        os.kill(os.getpid(), signal.SIGKILL)
    return result
def git(args, **kwargs):
    result = original_git(args, **kwargs)
    if window == 'cas' and args[0] == 'update-ref' and args[1] == co.state['fake_delivery_intent']['ref']:
        os.kill(os.getpid(), signal.SIGKILL)
    return result
def replace(source, target):
    result = original_replace(source, target)
    if window == 'index' and Path(target) == co.workspace / '.git/index':
        os.kill(os.getpid(), signal.SIGKILL)
    return result
dp.ct._git, dp.ct._git_bytes, os.replace = git, git_bytes, replace
dr.reconcile(co, rc.observed_test_succeeded, rc.atomic_json)
raise AssertionError('fault window was not reached')
"""
        result = subprocess.run([sys.executable, '-c', script, str(args_path), window],
                                cwd=MODULE_PATH.parent.parent, env=os.environ.copy(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        self.assertEqual(result.returncode, -9, result.stderr.decode())
        co = rc.Coordinator(rc.argparse.Namespace(**payload), _fake_lifecycle=True)
        lock = self.workspace / '.git/index.lock'
        self.assertTrue(lock.exists())
        row = json.loads((co.evidence / 'delivery-publication.json').read_text())
        self.assertEqual(lock.read_text(), row['lock']['nonce'])
        self.assertEqual(co.state['publication_hold'], row['intent']['digest'])
        with self.assertRaisesRegex(ValueError, 'publication incomplete'):
            co._publication_guard()
        original = ct._git
        cas_calls = []
        def no_cas(args, **kwargs):
            if args[0] == 'update-ref' and args[1] == row['intent']['ref']:
                if window == 'pre-cas':
                    cas_calls.append(tuple(args))
                else:
                    self.fail('recovery repeated CAS')
            return original(args, **kwargs)
        with patch.object(ct, '_git', side_effect=no_cas):
            self.assertEqual(dr.reconcile(co, rc.observed_test_succeeded, rc.atomic_json)['phase'], 'RECONCILED')
        self.assertFalse(lock.exists())
        self.assertEqual(co._head_commit(), row['intent']['c2'])
        drs.inspect(co, exact_q=True)
        if window == 'pre-cas':
            self.assertEqual(len(cas_calls), 1)
            dr.reconcile(co, rc.observed_test_succeeded, rc.atomic_json)
            self.assertEqual(len(cas_calls), 1)

    def test_reconcile_after_sigkill_immediately_after_cas(self):
        self.hard_crash_publication_recovery('cas')

    def test_reconcile_after_sigkill_immediately_after_index_replace(self):
        self.hard_crash_publication_recovery('index')

    def test_reconcile_post_cas_keeps_lock_and_is_idempotent_at_exact_q(self):
        self.test_fake_publication_crash_after_cas_is_hold_not_false_reconciliation()
        co, c1, before = self.q_test_fixture
        row = json.loads((co.evidence / 'delivery-publication.json').read_text())
        index = self.workspace / '.git/index'
        lock = index.with_name('index.lock')
        self.assertTrue(lock.exists())
        self.assertEqual(lock.read_text(), row['lock']['nonce'])
        calls = []
        original = ct._git
        def observed(args, **kwargs):
            if (args[0] in ('read-tree', 'update-ref') and
                    kwargs.get('env', {}).get('GIT_DIR') == str(self.workspace / '.git')):
                self.assertTrue(lock.exists())
                calls.append(tuple(args))
            return original(args, **kwargs)
        with patch.object(ct, '_git', side_effect=observed):
            result = dr.reconcile(co, rc.observed_test_succeeded, rc.atomic_json)
        self.assertEqual(result['phase'], 'RECONCILED')
        self.assertEqual(co.state['publication_complete'], row['intent']['digest'])
        self.assertNotIn('publication_hold', co.state)
        self.assertFalse(lock.exists())
        self.assertTrue(calls)
        self.assertTrue(all(args[0] != 'update-ref' for args in calls))
        before_calls = list(calls)
        with patch.object(ct, '_git', side_effect=observed):
            again = dr.reconcile(co, rc.observed_test_succeeded, rc.atomic_json)
        self.assertEqual(again['intent'], result['intent'])
        self.assertEqual(calls, before_calls)
        self.assertFalse(lock.exists())
        co._publication_guard()
        drs.inspect(co, exact_q=True)

    def test_reconcile_retries_zero_side_effect_hold_without_journal(self):
        self.test_fake_publication_refuses_unaccepted_and_dirty_workspace()
        co, c1, before = self.q_test_fixture
        (self.workspace / 'user.txt').unlink()
        self.assertFalse((co.evidence / 'delivery-publication.json').exists())
        result = dr.reconcile(co, rc.observed_test_succeeded, rc.atomic_json)
        self.assertEqual(result['phase'], 'RECONCILED')
        self.assertEqual(co._head_commit(), result['intent']['c2'])
        self.assertEqual(co.state['publication_complete'], result['intent']['digest'])
        co._publication_guard()
        self.assertEqual(dr.reconcile(co, rc.observed_test_succeeded, rc.atomic_json)['phase'], 'RECONCILED')
        self.assertFalse((self.workspace / '.git/index.lock').exists())

    def test_reconcile_after_sigkill_before_cas(self):
        self.hard_crash_publication_recovery('pre-cas')

    def test_reconcile_preserves_explicit_pending_null(self):
        self.test_fake_publication_crash_after_cas_is_hold_not_false_reconciliation()
        co, c1, before = self.q_test_fixture
        co.state['pending_reviewer_result_sequence'] = None
        co.save()
        result = dr.reconcile(co, rc.observed_test_succeeded, rc.atomic_json)
        self.assertEqual(result['phase'], 'RECONCILED')
        self.assertIsNone(co.state['pending_reviewer_result_sequence'])
        self.assertIn('pending_reviewer_result_sequence', json.loads(co.state_path.read_text()))
        self.assertNotIn('hold_reason', co.state)
        co._publication_guard()

    def test_reconcile_refuses_foreign_bytes_immediately_before_checkout(self):
        self.test_fake_publication_crash_after_cas_is_hold_not_false_reconciliation()
        co, c1, before = self.q_test_fixture
        original = ct._check_attributes
        target = self.workspace / 'tracked.txt'

        def edit(attrs_env, entries):
            result = original(attrs_env, entries)
            if attrs_env.get('GIT_DIR') == str(self.workspace / '.git'):
                target.write_text('foreign operator edit\n')
            return result

        with patch.object(ct, '_check_attributes', side_effect=edit):
            with self.assertRaisesRegex(ValueError, 'foreign recovery bytes'):
                dr.reconcile(co, rc.observed_test_succeeded, rc.atomic_json)
        self.assertEqual(target.read_text(), 'foreign operator edit\n')
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertNotEqual(json.loads((co.evidence / 'delivery-publication.json').read_text())['phase'],
                            'RECONCILED')

    def test_publication_guard_refuses_completed_state_with_leftover_lock_and_missing_intent(self):
        self.test_reconcile_post_cas_keeps_lock_and_is_idempotent_at_exact_q()
        co, c1, before = self.q_test_fixture
        lock = self.workspace / '.git/index.lock'
        lock.write_text('foreign lock')
        with self.assertRaisesRegex(ValueError, 'publication incomplete'):
            co._publication_guard()
        lock.unlink()
        co.state['fake_delivery_intent'] = None
        with self.assertRaisesRegex(ValueError, 'publication incomplete'):
            co._publication_guard()
        with self.assertRaisesRegex(ValueError, 'publication intent missing'):
            dr.reconcile(co, rc.observed_test_succeeded, rc.atomic_json)

    def test_close_proof_requires_exact_accepted_reconciled_tree_and_compass_blob(self):
        self.test_reconcile_post_cas_keeps_lock_and_is_idempotent_at_exact_q()
        co, c1, before = self.q_test_fixture
        original = rc.copy.deepcopy(co.state)
        proof = dcp.verify(co)
        self.assertEqual(proof['c2'], co._head_commit())
        self.assertEqual(proof['intent_digest'], co.state['publication_complete'])
        self.assertEqual(proof['backlog_sha256'], hashlib.sha256((self.workspace / 'BACKLOG.md').read_bytes()).hexdigest())
        self.assertEqual(co.state, original)
        mutations = [('completion', lambda: co.state.pop('publication_complete')),
                     ('acceptance', lambda: co.state.update(acceptance_state='PENDING')),
                     ('pending', lambda: co.state['lifecycle'].update(pending={'id': 'unreviewed'})),
                     ('blockers', lambda: co.state.update(finding_ledger=[{'id': 'F001', 'status': 'open',
                                      'severity': 'MAJOR', 'source': 'security-reviewer'}])),
                     ('program', lambda: co.state['operator_programs'].update(path_env='/foreign'))]
        for name, mutate in mutations:
            with self.subTest(guard=name):
                co.state = rc.copy.deepcopy(original)
                mutate()
                with self.assertRaises((ValueError, RuntimeError)):
                    dcp.verify(co)
        co.state = rc.copy.deepcopy(original)
        backlog = self.workspace / 'BACKLOG.md'
        raw = backlog.read_bytes()
        backlog.write_bytes(raw + b'foreign edit\n')
        with self.assertRaises(ValueError):
            dcp.verify(co)
        self.assertEqual(backlog.read_bytes(), raw + b'foreign edit\n')
        backlog.write_bytes(raw)
        with patch.object(co, '_fake_lifecycle', False):
            with self.assertRaisesRegex(ValueError, 'fake-only'):
                dcp.verify(co)
        self.assertEqual(dcp.verify(co), proof)

    def test_recovery_lock_admission_refuses_without_rebinding_or_writing_state(self):
        self.test_fake_delivery_intent_binds_unpublished_objects_and_accepts_exact_digest()
        co, c1, before = self.q_test_fixture
        saved = co.state_path.read_bytes()
        with self.assertRaises(FileNotFoundError):
            with drl.locked(co, rc.atomic_json):
                self.fail('missing journal admitted')
        self.assertEqual(co.state_path.read_bytes(), saved)
        self.assertEqual(co.state['status'], 'ACCEPTED')
        dj.prepare(co, rc.observed_test_succeeded, rc.atomic_json)
        co.state['publication_hold'] = 'different-run'
        co.save()
        saved = co.state_path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'matching attributed acceptance'):
            with drl.locked(co, rc.atomic_json):
                self.fail('mismatched digest admitted')
        self.assertEqual(co.state_path.read_bytes(), saved)
        self.assertEqual(co.state['publication_hold'], 'different-run')

    def test_recovery_lock_is_journal_bound_and_retained_until_completion(self):
        self.test_fake_publication_crash_after_cas_is_hold_not_false_reconciliation()
        co, c1, before = self.q_test_fixture
        with drl.locked(co, rc.atomic_json) as context:
            row, root, revision, live, index, lock, maps = context
            self.assertTrue(lock.exists())
            self.assertEqual(row['lock']['ino'], lock.stat().st_ino)
            self.assertEqual(row['lock']['pid'], os.getpid())
            self.assertEqual(json.loads((co.evidence / 'delivery-publication.json').read_text()), row)
            self.assertEqual(lock.read_text(), row['lock']['nonce'])
        self.assertTrue(lock.exists())
        with drl.locked(co, rc.atomic_json):
            self.assertTrue(lock.exists())
        self.assertTrue(lock.exists())
        with self.assertRaisesRegex(ValueError, 'state changed'):
            with drl.locked(co, rc.atomic_json) as context:
                context[0]['phase'] = 'RECONCILED'
                co.state['publication_complete'] = context[0]['intent']['digest']
                raise OSError('completion not saved')
        self.assertTrue(lock.exists())
        self.assertNotIn('publication_complete', json.loads(co.state_path.read_text()))
        co.state = json.loads(co.state_path.read_text())
        lock.write_text('foreign lock')
        row = json.loads((co.evidence / 'delivery-publication.json').read_text())
        row['lock']['ino'] += 1
        rc.atomic_json(co.evidence / 'delivery-publication.json', row)
        with self.assertRaisesRegex(ValueError, 'unattributed index lock'):
            with drl.locked(co, rc.atomic_json):
                self.fail('foreign lock adopted')
        self.assertEqual(lock.read_text(), 'foreign lock')
        self.assertEqual(co.state['status'], 'HOLD')

    def test_recovery_state_accepts_preimport_scratch_q_objects(self):
        self.test_fake_delivery_intent_binds_unpublished_objects_and_accepts_exact_digest()
        co, c1, before = self.q_test_fixture
        original = ct._git_bytes
        def fail_import(args, **kwargs):
            if args[0] == 'index-pack':
                raise OSError('before import')
            return original(args, **kwargs)
        with patch.object(ct, '_git_bytes', side_effect=fail_import):
            self.assertEqual(dp.publish(co, rc.observed_test_succeeded, rc.atomic_json), 'HOLD')
        row, root, revision, live, index, lock, maps = drs.inspect(co)
        self.assertEqual(ct._git(['rev-parse', 'HEAD'], env=live), row['intent']['parent'])
        self.assertEqual(row['phase'], 'PREPARED')
        self.assertIn('sum_ints.py', maps[1])
        with self.assertRaises(ct.CandidateError):
            ct._tree_entries(live, row['intent']['q_oid'])

    def test_recovery_state_accepts_q_new_files_before_live_index_replace(self):
        self.test_fake_delivery_intent_binds_unpublished_objects_and_accepts_exact_digest()
        co, c1, before = self.q_test_fixture
        original = ct._git
        def fail_after_checkout(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == 'read-tree' and '-u' in args:
                raise OSError('after checkout')
            return result
        with patch.object(ct, '_git', side_effect=fail_after_checkout):
            self.assertEqual(dp.publish(co, rc.observed_test_succeeded, rc.atomic_json), 'HOLD')
        row, root, revision, live, index, lock, maps = drs.inspect(co)
        self.assertEqual(row['phase'], 'PUBLISHED')
        self.assertIn('sum_ints.py', ct._git(['ls-files', '--others', '--exclude-standard'], env=live))
        self.assertEqual((self.workspace / 'sum_ints.py').read_bytes(), (root.root / 'sum_ints.py').read_bytes())

    def test_recovery_state_accepts_known_parent_or_q_and_rejects_foreign_bytes(self):
        self.test_fake_publication_crash_after_cas_is_hold_not_false_reconciliation()
        co, c1, before = self.q_test_fixture
        state = co.state_path.read_bytes()
        row, root, revision, live, index, lock, maps = drs.inspect(co)
        self.assertEqual(row['phase'], 'PREPARED')
        self.assertEqual(ct._git(['rev-parse', 'HEAD'], env=live), row['intent']['c2'])
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertEqual(co.state_path.read_bytes(), state)
        path = self.workspace / 'tracked.txt'
        saved = path.read_bytes()
        path.write_bytes(b'foreign bytes')
        with self.assertRaisesRegex(ValueError, 'foreign recovery bytes'):
            drs.inspect(co)
        path.write_bytes(saved)
        attrs = self.workspace / '.git/info/attributes'
        attrs.write_text('*.txt text\n')
        with self.assertRaisesRegex(ValueError, 'attributes/filter unsupported'):
            drs.inspect(co)
        attrs.unlink()
        co.state['publication_hold'] = 'other-run'
        with self.assertRaisesRegex(ValueError, 'matching attributed acceptance'):
            drs.inspect(co)
        self.assertEqual(co.state_path.read_bytes(), state)

    def test_publication_hold_quarantines_all_operator_paths_without_state_write(self):
        self.test_fake_publication_crash_after_cas_is_hold_not_false_reconciliation()
        co, c1, before = self.q_test_fixture
        saved = co.state_path.read_bytes()
        calls = (lambda: co.hold('aborted'), lambda: co.accept(), lambda: co.reject('retry', None),
                 lambda: co.note('guidance', None), lambda: co.scope_change('new scope', None),
                 lambda: co.resume(), lambda: co.resume(True), lambda: co.resume_polish(),
                 lambda: co.permission_probe())
        for call in calls:
            with self.subTest(call=call):
                with patch.object(co, 'invoke', side_effect=AssertionError('provider dispatched')):
                    with self.assertRaisesRegex(ValueError, 'locked publication recovery'):
                        call()
                self.assertEqual(co.state_path.read_bytes(), saved)
        command = self.command()[2:]
        command[0] = 'status'
        with patch('builtins.print', return_value=None):
            self.assertEqual(rc.main(command), 0)
        self.assertEqual(co.state_path.read_bytes(), saved)

    def test_fake_publication_cas_holds_index_lock_through_checkout(self):
        self.test_fake_delivery_intent_binds_unpublished_objects_and_accepts_exact_digest()
        co, c1, before = self.q_test_fixture
        index = self.workspace / '.git/index'
        original = index.read_bytes()
        calls = []
        git = ct._git
        def observed(args, **kwargs):
            if args[0] == 'update-ref':
                journal = json.loads((co.evidence / 'delivery-publication.json').read_text())
                self.assertEqual(journal['phase'], 'PREPARED')
                self.assertEqual(journal['lock']['pid'], os.getpid())
                self.assertTrue(index.with_name('index.lock').exists())
                self.assertEqual(index.read_bytes(), original)
                calls.append('CAS')
            if args[0] == 'read-tree' and '-u' in args:
                self.assertTrue(index.with_name('index.lock').exists())
                self.assertEqual(index.read_bytes(), original)
                self.assertNotEqual(kwargs['env']['GIT_INDEX_FILE'], str(index))
                calls.append('checkout')
            return git(args, **kwargs)
        with patch.object(ct, '_git', side_effect=observed):
            row = dp.publish(co, rc.observed_test_succeeded, rc.atomic_json)
        self.assertIsInstance(row, dict, co.state.get('hold_reason'))
        self.assertEqual(row['phase'], 'RECONCILED')
        self.assertEqual(calls, ['CAS', 'checkout'])
        self.assertEqual(co._head_commit(), row['intent']['c2'])
        live = ct._git_env(GIT_DIR=str(self.workspace / '.git'), GIT_WORK_TREE=str(self.workspace))
        self.assertEqual(ct._git(['write-tree'], env=live), row['intent']['q_oid'])
        self.assertEqual(ct._git(['status', '--porcelain=v1', '--untracked-files=all'], env=live), '')
        self.assertFalse(index.with_name('index.lock').exists())
        self.assertIn(c1, (self.workspace / 'BACKLOG.md').read_text())
        self.assertEqual(json.loads((co.evidence / 'delivery-publication.json').read_text()), row)
        dj.verify(co, row)
        self.assertEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')

    def test_fake_publication_crash_after_cas_is_hold_not_false_reconciliation(self):
        self.test_fake_delivery_intent_binds_unpublished_objects_and_accepts_exact_digest()
        co, c1, before = self.q_test_fixture
        index = self.workspace / '.git/index'
        original = index.read_bytes()
        def crash(path, value):
            if path.name == 'delivery-publication.json' and value.get('phase') == 'PUBLISHED':
                raise OSError('simulated journal completion failure')
            rc.atomic_json(path, value)
        self.assertEqual(dp.publish(co, rc.observed_test_succeeded, crash), 'HOLD')
        row = json.loads((co.evidence / 'delivery-publication.json').read_text())
        self.assertEqual(row['phase'], 'PREPARED')
        self.assertEqual(co._head_commit(), row['intent']['c2'])
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertEqual(co.state['publication_hold'], row['intent']['digest'])
        self.assertIn('simulated journal completion failure', co.state['hold_reason'])

    def test_fake_publication_refuses_unaccepted_and_dirty_workspace(self):
        self.test_fake_delivery_intent_binds_unpublished_objects_and_accepts_exact_digest()
        co, c1, before = self.q_test_fixture
        saved = rc.copy.deepcopy(co.state)
        co.state['status'] = 'DONE'
        with self.assertRaisesRegex(ValueError, 'operator ACCEPTED'):
            dp.publish(co, rc.observed_test_succeeded, rc.atomic_json)
        self.assertEqual(co.state['status'], 'DONE')
        self.assertFalse((co.evidence / 'delivery-publication.json').exists())
        co.state = saved
        co._fake_lifecycle = False
        with self.assertRaisesRegex(ValueError, 'fake operator'):
            dp.publish(co, rc.observed_test_succeeded, rc.atomic_json)
        co._fake_lifecycle = True
        (self.workspace / 'user.txt').write_text('foreign work')
        parent = co._head_commit()
        self.assertEqual(dp.publish(co, rc.observed_test_succeeded, rc.atomic_json), 'HOLD')
        self.assertEqual(co._head_commit(), parent)
        self.assertEqual((self.workspace / 'user.txt').read_text(), 'foreign work')
        self.assertEqual(co.state['status'], 'HOLD')

    def test_publication_journal_proofs_survive_cas_but_reject_drift(self):
        self.test_fake_delivery_intent_binds_unpublished_objects_and_accepts_exact_digest()
        co, c1, before = self.q_test_fixture
        row = dj.prepare(co, rc.observed_test_succeeded, rc.atomic_json)
        path = co.evidence / 'delivery-publication.json'
        self.assertEqual(json.loads(path.read_text()), row)
        with patch.object(co, 'invoke', side_effect=AssertionError('must not dispatch')):
            self.assertEqual(dj.prepare(co, rc.observed_test_succeeded, rc.atomic_json), row)
        root, revision, live = dj.verify(co, row)
        scratch = ct._git_env(GIT_DIR=str(root.git_dir))
        pack = ct._git_bytes(['pack-objects', '--stdout', '--revs'], env=scratch,
                             input_bytes=(row['intent']['c2'] + '\n').encode())
        ct._git_bytes(['index-pack', '--strict', '--stdin'], env=live, input_bytes=pack)
        ct._git(['update-ref', row['intent']['ref'], row['intent']['c2'], row['intent']['parent']], env=live)
        with self.assertRaisesRegex(ValueError, 'parent changed'):
            ct.verify_candidate_revision(root, revision)
        dj.verify(co, row)
        self.assertEqual(dj.prepare(co, rc.observed_test_succeeded, rc.atomic_json), row)
        self.assertEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')
        proof_path = co.evidence / 'delivery-acceptance.json'
        saved = proof_path.read_bytes()
        proof_path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'proof changed'):
            dj.verify(co, row)
        proof_path.write_bytes(saved)
        saved_state = rc.copy.deepcopy(co.state)
        co.state['sequence'] += 1
        with self.assertRaisesRegex(ValueError, 'proof changed'):
            dj.verify(co, row)
        co.state = saved_state
        changed = rc.copy.deepcopy(row)
        changed['bundle']['status'] = 'UNREVIEWED'
        with self.assertRaisesRegex(ValueError, 'attributed acceptance'):
            dj.verify(co, changed)
        atomic = co.evidence / 'delivery-publication.json'
        old = atomic.read_bytes()
        atomic.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'proof changed'):
            dj.verify(co, changed)
        atomic.write_bytes(old)
        root.index.write_bytes(b'invalid index')
        with self.assertRaises(ValueError):
            dj.verify(co, row)

    def test_publication_journal_needs_operator_acceptance_and_no_later_writer(self):
        self.test_fake_delivery_intent_binds_unpublished_objects_and_accepts_exact_digest()
        co, c1, before = self.q_test_fixture
        saved = rc.copy.deepcopy(co.state)
        co.state['acceptance_state'] = 'PENDING'
        with self.assertRaisesRegex(ValueError, 'attributed acceptance'):
            dj.prepare(co, rc.observed_test_succeeded, rc.atomic_json)
        self.assertFalse((co.evidence / 'delivery-publication.json').exists())
        co.state = saved
        row = dj.prepare(co, rc.observed_test_succeeded, rc.atomic_json)
        co._fake_lifecycle = False
        with self.assertRaisesRegex(ValueError, 'attributed acceptance'):
            dj.verify(co, row)
        co._fake_lifecycle = True
        co.state['status'] = 'HOLD'
        with self.assertRaisesRegex(ValueError, 'attributed acceptance'):
            dj.verify(co, row)
        co.state['status'] = 'ACCEPTED'
        subprocess.run(['git', 'config', 'core.filemode', 'false'], cwd=self.workspace, check=True)
        with self.assertRaisesRegex(ValueError, 'inventory changed'):
            dj.verify(co, row)

    def test_candidate_contents_verify_after_cas_without_weakening_prepublication_guard(self):
        self.test_fake_q_bundle_consumes_real_gate_final_security_and_p_noops()
        co, c1, before = self.q_test_fixture
        bundle, root, revision = rc.q_evidence.review_bundle(co, c1, '2026-09-30', rc.observed_test_succeeded)
        intent = rc.delivery_intent.prepare(co, c1, '2026-09-30', rc.observed_test_succeeded, rc.atomic_json)
        scratch = ct._git_env(GIT_DIR=str(root.git_dir))
        live = ct._git_env(GIT_DIR=str(self.workspace / '.git'))
        pack = ct._git_bytes(['pack-objects', '--stdout', '--revs'], env=scratch,
                             input_bytes=(intent['c2'] + '\n').encode())
        ct._git_bytes(['index-pack', '--strict', '--stdin'], env=live, input_bytes=pack)
        ct._git(['update-ref', intent['ref'], intent['c2'], intent['parent']], env=live)
        with self.assertRaisesRegex(ValueError, 'parent changed'):
            ct.verify_candidate_revision(root, revision)
        ct.verify_candidate_contents(root, revision)
        backlog = root.root / 'BACKLOG.md'
        saved = backlog.read_bytes()
        backlog.write_bytes(saved + b'foreign change\n')
        with self.assertRaises(ValueError):
            ct.verify_candidate_contents(root, revision)
        backlog.write_bytes(saved)
        ct.verify_candidate_contents(root, revision)
        with patch.object(ct, '_assert_live_unchanged', side_effect=AssertionError('pre-CAS only')):
            ct.verify_candidate_contents(root, revision)

    def test_delivery_seal_rejected_closeout_leaves_no_state_or_evidence(self):
        self.test_closeout_freeze_refuses_stale_lifecycle_and_backlog_writer_grant()
        for run in self.root.glob('closeout-*'):
            self.assertFalse((run / 'evidence/delivery-seal.json').exists())
        result = subprocess.run([sys.executable, str(MODULE_PATH), '--help'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_delivery_seal_binds_c1_metadata_and_hook_inventory(self):
        self.test_fake_q_bundle_consumes_real_gate_final_security_and_p_noops()
        co, c1, before = self.q_test_fixture
        bundle, root, revision = rc.q_evidence.review_bundle(co, c1, '2026-09-30', rc.observed_test_succeeded)
        proposal = bundle['source']['proposal']
        seal = rc.delivery_seal.verify(co, root, proposal)
        self.assertEqual(seal, json.loads((co.evidence / 'delivery-seal.json').read_text()))
        env = rc.delivery_seal.environment(co, root)
        raw = ct._git_bytes(['cat-file', 'commit', c1], env=env)
        for label, altered in [
                ('author', raw.replace(b'author paired-session ', b'author forged ')),
                ('committer', raw.replace(b'committer paired-session ', b'committer forged ')),
                ('message', raw + b'unsigned extra message\n'),
                ('date', raw.replace(seal['date'][1:].encode(), b'1 +0000')),
                ('encoding', raw.replace(b'\n\n', b'\nencoding ISO-8859-1\n\n', 1))]:
            with self.subTest(metadata=label):
                oid = ct._git_bytes(['hash-object', '-w', '-t', 'commit', '--stdin'],
                                    env=env, input_bytes=altered).decode().strip()
                changed = {**proposal, 'c1': oid, 'c1_sha256': hashlib.sha256(altered).hexdigest()}
                with self.assertRaisesRegex(ValueError, 'metadata/tree/parent'):
                    rc.delivery_seal.verify(co, root, changed)
        intent = rc.delivery_intent.prepare(co, c1, '2026-09-30', rc.observed_test_succeeded, rc.atomic_json)
        self.assertEqual(intent['publication_seal'], seal)
        hook = self.workspace / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 0\n')
        hook.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'inventory changed'):
            rc.delivery_seal.verify(co, root, proposal)
        hook.unlink()
        subprocess.run(['git', 'config', 'core.filemode', 'false'], cwd=self.workspace, check=True)
        with self.assertRaisesRegex(ValueError, 'inventory changed'):
            rc.delivery_seal.verify(co, root, proposal)

    def test_delivery_seal_refuses_active_hooks_and_non_fake_freeze(self):
        co = self.coordinator()
        with self.assertRaisesRegex(ValueError, 'fake-only'):
            rc.delivery_seal.freeze(co, rc.atomic_json)
        co._fake_lifecycle = True
        hook = self.workspace / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 0\n')
        hook.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'runner not implemented'):
            rc.delivery_seal.freeze(co, rc.atomic_json)
        self.assertNotIn('publication_seal', co.state)
        hook.unlink()
        rc.delivery_seal.freeze(co, rc.atomic_json)
        saved = rc.copy.deepcopy(co.state['publication_seal'])
        (co.evidence / 'delivery-seal.json').write_text('{}')
        root = type('Root', (), {'git_dir': self.workspace / '.git'})()
        with self.assertRaisesRegex(ValueError, 'protected evidence'):
            rc.delivery_seal.verify(co, root, {})
        self.assertEqual(co.state['publication_seal'], saved)

    def test_fake_delivery_intent_binds_unpublished_objects_and_accepts_exact_digest(self):
        self.test_fake_q_bundle_consumes_real_gate_final_security_and_p_noops()
        co, c1, before = self.q_test_fixture
        head = co._head_commit()
        intent = rc.delivery_intent.prepare(co, c1, '2026-09-30', rc.observed_test_succeeded, rc.atomic_json)
        again = rc.delivery_intent.prepare(co, c1, '2026-09-30', rc.observed_test_succeeded, rc.atomic_json)
        self.assertEqual(intent, again)
        self.assertEqual(co._head_commit(), head)
        self.assertEqual(co.state['status'], 'DONE')
        root = ct.baseline_from_binding(co.state['fake_ingest_receipt']['baseline'])
        raw = ct._git_bytes(['cat-file', 'commit', intent['c2']], env=ct._git_env(GIT_DIR=str(root.git_dir)))
        self.assertIn(('tree ' + intent['q_oid'] + '\n').encode(), raw)
        self.assertIn(('parent ' + c1 + '\n').encode(), raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), intent['c2_sha256'])
        for expected in (None, 'stale'):
            with self.subTest(expected=expected):
                co.args.expect = expected
                with self.assertRaisesRegex(ValueError, 'current intent digest'):
                    co.accept()
                self.assertNotIn('fake_delivery_acceptance', co.state)
        co.args.expect = intent['digest']
        self.assertEqual(co.accept(), 'ACCEPTED')
        record = co.state['fake_delivery_acceptance']
        self.assertEqual(record['author'], 'operator')
        self.assertEqual(record['uid'], os.getuid())
        self.assertEqual(record['intent_digest'], intent['digest'])
        self.assertEqual(record['q_oid'], intent['q_oid'])
        self.assertIn(record, co.state['events'])
        events = len(co.state['events'])
        self.assertEqual(rc.delivery_intent.prepare(
            co, c1, '2026-09-30', rc.observed_test_succeeded, rc.atomic_json), intent)
        self.assertEqual(co.state['status'], 'ACCEPTED')
        self.assertEqual(co.accept(), 'ACCEPTED')
        self.assertEqual(len(co.state['events']), events)
        self.assertEqual(co._head_commit(), head)
        self.assertEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')

    def test_fake_delivery_intent_refuses_drift_and_unreviewed_bundle(self):
        self.test_fake_q_bundle_consumes_real_gate_final_security_and_p_noops()
        co, c1, before = self.q_test_fixture
        saved = rc.copy.deepcopy(co.state)
        for case in ('unreviewed', 'pending', 'live-work', 'intent-file', 'intent-state', 'q-root'):
            with self.subTest(case=case):
                co.state = rc.copy.deepcopy(saved)
                path = co.evidence / 'delivery-intent.json'
                if path.exists():
                    path.unlink()
                changed = self.workspace / 'tracked.txt'
                raw = changed.read_bytes()
                q_root = Path(co.state['fake_q_review']['root']) / 'tracked.txt'
                q_raw = q_root.read_bytes()
                if case == 'unreviewed':
                    co.state['fake_q_bundle']['status'] = 'UNREVIEWED'
                elif case == 'pending':
                    co.state['fake_q_bundle_pending'] = {'interrupted': True}
                elif case == 'live-work':
                    changed.write_bytes(raw + b'user change\n')
                elif case == 'intent-file':
                    path.write_text('{}')
                elif case == 'intent-state':
                    co.state['fake_delivery_intent'] = {'relabel': True}
                else:
                    q_root.write_bytes(q_raw + b'post-security change\n')
                try:
                    with self.assertRaises(ValueError):
                        rc.delivery_intent.prepare(co, c1, '2026-09-30', rc.observed_test_succeeded, rc.atomic_json)
                finally:
                    changed.write_bytes(raw)
                    q_root.write_bytes(q_raw)
                self.assertNotIn('fake_delivery_acceptance', co.state)
                self.assertEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')

    def test_fake_delivery_abort_never_revives_on_prepare_or_accept(self):
        self.test_fake_q_bundle_consumes_real_gate_final_security_and_p_noops()
        co, c1, before = self.q_test_fixture
        intent = rc.delivery_intent.prepare(co, c1, '2026-09-30', rc.observed_test_succeeded, rc.atomic_json)
        co.hold('aborted by operator')
        held = rc.copy.deepcopy(co.state)
        for expected in (None, 'stale', intent['digest']):
            with self.subTest(expected=expected):
                co.args.expect = expected
                with self.assertRaisesRegex(ValueError, 'DONE/PENDING'):
                    co.accept()
                self.assertEqual(co.state, held)
        with self.assertRaisesRegex(ValueError, 'refuses this HOLD'):
            rc.delivery_intent.prepare(co, c1, '2026-09-30', rc.observed_test_succeeded, rc.atomic_json)
        self.assertEqual(co.state, held)
        co.state['fake_delivery_intent'] = None
        with self.assertRaisesRegex(ValueError, 'refuses this HOLD'):
            rc.delivery_intent.prepare(co, c1, '2026-09-30', rc.observed_test_succeeded, rc.atomic_json)
        self.assertEqual(co.state['status'], 'HOLD')

    def test_fake_delivery_reject_refuses_without_entering_legacy_recovery(self):
        self.test_fake_q_bundle_consumes_real_gate_final_security_and_p_noops()
        co, c1, before = self.q_test_fixture
        intent = rc.delivery_intent.prepare(co, c1, '2026-09-30', rc.observed_test_succeeded, rc.atomic_json)
        co.args.expect = intent['digest']
        state = rc.copy.deepcopy(co.state)
        with patch.object(co, 'invoke', side_effect=AssertionError('must not dispatch')):
            with self.assertRaisesRegex(ValueError, 'abort/new run or use --scope-change'):
                co.reject('Needs repair', None)
        self.assertEqual(co.state, state)
        self.assertEqual(co.state['status'], 'DONE')
        self.assertNotIn('pending_rejection_id', co.state)

    def test_q_materialization_refuses_normal_non_fake_coordinator(self):
        co = self.coordinator()
        state = rc.copy.deepcopy(co.state)
        with self.assertRaisesRegex(ValueError, 'fake-only'):
            co.fake_materialize_q('a' * 40, '2026-09-30')
        self.assertEqual(co.state, state)
        self.assertEqual(co.state['sequence'], 0)

    def test_closeout_freeze_refuses_stale_lifecycle_and_backlog_writer_grant(self):
        for kind in ('prior-freeze', 'prior-stage', 'writer-file', 'writer-allowlist', 'parent-drift'):
            with self.subTest(kind=kind):
                run = self.root / ('closeout-' + kind)
                command = self.command('--lifecycle-mode', 'on', '--stop-after-plan', '--skip-probe')
                command[command.index('--run-dir') + 1] = str(run)
                if kind.startswith('writer'):
                    command += ['--docs-file' if kind == 'writer-file' else '--docs-allowlist', 'BACKLOG.md']
                co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
                if kind == 'prior-freeze':
                    co.state['closeout_item'] = {'original': True}
                elif kind == 'prior-stage':
                    co.state['lifecycle']['stage'] = 'FINISH'
                before = json.loads(json.dumps(co.state))
                frozen = {'head': 'different-parent'}
                with patch.object(rc.closeout_policy, 'freeze_item', return_value=frozen) as freeze:
                    with self.assertRaises(ValueError):
                        co.fake_lifecycle_drive(backlog_item=1)
                self.assertEqual(co.state, before)
                self.assertEqual(co.state['sequence'], 0)
                if kind != 'parent-drift':
                    freeze.assert_not_called()

    def test_fake_drive_freezes_operator_closeout_item_before_candidate_author(self):
        docs = self.workspace / 'docs' / 'guide.md'
        docs.parent.mkdir()
        docs.write_text('# Draft guide\n')
        backlog = self.workspace / 'BACKLOG.md'
        backlog.write_text('# Backlog\n\n## P0\n(none)\n## P1\n'
                           '- Fix sums. (added 2026-09-29)\n## P2\n(none)\n'
                           '## P3\n(none)\n## Done\n(none)\n')
        ignore = self.workspace / '.gitignore'
        ignore.write_text(ignore.read_text() + '\n.compass/\n' if ignore.exists() else '.compass/\n')
        subprocess.run(['git', 'add', 'docs/guide.md', 'BACKLOG.md', '.gitignore'],
                       cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'closeout fixture'], cwd=self.workspace, check=True)
        view = self.workspace / '.compass/backlog-last-view.json'
        view.parent.mkdir(exist_ok=True)
        view.write_text(json.dumps({'generated_at': rc.datetime.now().astimezone().isoformat(),
                         'source_path': str(backlog),
                         'items': [{'id': 1, 'section': 'P1', 'title_span': 'Fix sums'}]}))
        original = backlog.read_bytes()
        command = self.command('--lifecycle-mode', 'on', '--stop-after-plan', '--skip-probe',
                               '--docs-file', 'docs/guide.md', '--test-command', 'python3 -c pass')
        co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        self.assertEqual(co.fake_lifecycle_drive(backlog_item=1), 'STOP_BEFORE_SECURITY')
        saved = json.loads(co.state_path.read_text())
        self.assertEqual(saved['closeout_item']['item_id'], 1)
        self.assertEqual(saved['closeout_item']['backlog_sha256'], rc.hashlib.sha256(original).hexdigest())
        self.assertEqual(backlog.read_bytes(), original)
        self.assertNotIn('Q', [r['stage'] for r in saved['lifecycle']['receipts']])
        self.assertNotEqual(saved['status'], 'CLOSED')

    def test_fake_lifecycle_drive_runs_m3_without_test_built_approvals(self):
        docs = self.workspace / 'docs' / 'guide.md'
        docs.parent.mkdir()
        docs.write_text('# Draft guide\n')
        subprocess.run(['git', 'add', 'docs/guide.md'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add docs fixture'], cwd=self.workspace, check=True)
        command = self.command('--lifecycle-mode', 'on', '--stop-after-plan', '--skip-probe',
                               '--docs-file', 'docs/guide.md', '--test-command', 'python3 -c pass')
        co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        self.assertEqual(co.fake_lifecycle_drive(), 'STOP_BEFORE_SECURITY')
        stages = [row['stage'] for row in co.state['lifecycle']['receipts']]
        self.assertEqual(stages, ['EXEC', 'FINISH', 'POLISH-Q', 'DOCS',
                                  'EXEC', 'FINISH', 'POLISH-Q', 'DOCS'])
        final_oid = co.state['lifecycle']['candidate_oid']
        docs_receipt = co.state['lifecycle']['receipts'][3]
        self.assertNotEqual(docs_receipt['output_oid'], docs_receipt['candidate_oid'])
        self.assertEqual(docs_receipt['retested_oid'], docs_receipt['output_oid'])
        self.assertEqual(docs_receipt['docs_file'], 'docs/guide.md')
        self.assertEqual(co.state['fake_ingest_receipt']['source_writer_request_id'],
                         docs_receipt['request_id'])
        self.assertEqual(docs_receipt['retest_id'], co.state['fake_candidate_test']['id'])
        self.assertEqual(docs.read_text(), '# Draft guide\n')
        self.assertEqual(co.state['fake_ingest_receipt']['output_oid'], final_oid)
        self.assertEqual(co.state['fake_candidate_test']['oid'], final_oid)
        self.assertEqual(co.state['fake_candidate_chain']['oid'], final_oid)
        self.assertEqual(co.state['lifecycle']['receipts'][-1]['output_oid'], final_oid)
        baseline = ct.baseline_from_binding(co.state['fake_ingest_receipt']['baseline'])
        revision = ct.CandidateRevision(final_oid, tuple(co.state['fake_ingest_receipt']['manifest']), 0)
        final_tree = ct.rebuild_candidate_from_oid(baseline, revision)
        self.assertEqual((final_tree.root / 'docs/guide.md').read_text(), '# Fake lifecycle guide\n')
        phases = [(row['role'], row['phase']) for row in co.state['turns']]
        self.assertIn(('author', 'EXEC'), phases)
        self.assertIn(('reviewer', 'EXEC'), phases)
        self.assertIn(('gate', 'EXEC'), phases)
        self.assertIn(('author', 'FINISH'), phases)
        self.assertIn(('reviewer', 'POLISH'), phases)
        self.assertIn(('author', 'DOCS'), phases)

    def test_fake_lifecycle_router_replays_receipt_and_restarts_on_oid_change(self):
        command = self.command('--lifecycle-mode', 'on')
        co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        life = co.state['lifecycle']
        approved = {'status': 'APPROVE', 'epoch': 0, 'item_uuid': life['item_uuid'],
                    'run_id': self.run_dir.name, 'parent': life['parent'], 'candidate_oid': 'a' * 40}
        with patch.object(rc.lifecycle_spine, 'advance', side_effect=RuntimeError('simulated crash')):
            with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                co.fake_lifecycle_route(approved, stub_mode=True)
        self.assertEqual(json.loads(co.state_path.read_text())['lifecycle']['stage'], 'EXEC')
        resume = command.copy(); resume[2] = 'resume'
        again = rc.Coordinator(rc.parser().parse_args(resume[2:]), _fake_lifecycle=True)
        self.assertEqual(again.fake_lifecycle_route(approved, {'FINISH': 'b' * 40}, stub_mode=True), 'EXEC')
        self.assertEqual(again.state['lifecycle']['epoch'], 1)
        self.assertEqual(again.state['lifecycle']['candidate_oid'], 'b' * 40)
        with self.assertRaisesRegex(ValueError, 'stale or malformed'):
            again.fake_lifecycle_route(approved, stub_mode=True)
        next_approval = {**approved, 'epoch': 1, 'candidate_oid': 'b' * 40}
        self.assertEqual(again.fake_lifecycle_route(next_approval, stub_mode=True), 'STOP_BEFORE_SECURITY')
        self.assertEqual([row['stage'] for row in again.state['lifecycle']['receipts']],
                         ['EXEC', 'FINISH', 'EXEC', 'FINISH', 'POLISH-Q', 'DOCS'])

    def test_fake_lifecycle_finish_dispatch_binds_candidate_and_tests(self):
        command = self.command('--lifecycle-mode', 'on')
        co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch,
                                                 ('tracked.txt',))
        baseline = dataclasses.replace(baseline, separate_filesystems=True)
        revision = ct.ingest_candidate_revision(baseline)
        life = co.state['lifecycle']
        fields = {'candidate_oid': revision.tree_oid, 'epoch': 0, 'phase': 'EXEC',
                  'run_id': self.run_dir.name, 'convergence_id': 'exec-1',
                  'workspace': str(self.workspace.resolve()), 'run_dir': str(self.run_dir.resolve()),
                  'parent_head': baseline.parent_head}
        proof = {'run_id': self.run_dir.name, 'convergence_id': 'exec-1', 'blocking_findings': [],
                 'reviewer': {**fields, 'role': 'reviewer', 'status': 'APPROVE'},
                 'gate': {**fields, 'role': 'gate', 'verdict': 'approve'}}
        approved = {'status': 'APPROVE', 'epoch': 0, 'item_uuid': life['item_uuid'],
                    'run_id': self.run_dir.name, 'parent': life['parent'],
                    'candidate_oid': revision.tree_oid, 'proof': proof}
        launched = []
        def launch(request):
            launched.append(request)
            return {'sandbox_id': 'fake-stopped', 'request_sha256': request['sha256'], 'status': 'READY'}
        context = {'baseline': baseline, 'revision': revision, 'launch': launch,
                   'sandbox_stopped': lambda identity: identity == 'fake-stopped',
                   'tested_oid': revision.tree_oid}
        wrong = {**context, 'revision': ct.CandidateRevision('0' * 40, (), 0)}
        with self.assertRaisesRegex(ValueError, 'candidate differs'):
            co.fake_lifecycle_route(approved, finish_context=wrong)
        self.assertEqual(co.state['lifecycle']['stage'], 'FINISH')
        self.assertIsNone(co.state['lifecycle']['pending'])
        foreign = {**context, 'baseline': dataclasses.replace(baseline, workspace=self.root / 'foreign')}
        with self.assertRaisesRegex(ValueError, 'candidate differs'):
            co.fake_lifecycle_route(approved, finish_context=foreign)
        self.assertIsNone(co.state['lifecycle']['pending'])
        self.assertEqual(co.fake_lifecycle_route(approved, finish_context=context, polish_stub=True, docs_stub=True),
                         'STOP_BEFORE_SECURITY')
        self.assertEqual(len(launched), 1)
        self.assertEqual(co.state['lifecycle']['receipts'][1]['finish_result'], 'TESTS_REQUIRED')
        self.assertEqual(co.state['turns'], [])

    def test_fake_lifecycle_finish_code_write_restarts_exec(self):
        command = self.command('--lifecycle-mode', 'on')
        co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch,
                                                 ('tracked.txt',))
        baseline = dataclasses.replace(baseline, separate_filesystems=True)
        revision = ct.ingest_candidate_revision(baseline)
        life = co.state['lifecycle']
        fields = {'candidate_oid': revision.tree_oid, 'epoch': 0, 'phase': 'EXEC',
                  'run_id': self.run_dir.name, 'convergence_id': 'exec-1',
                  'workspace': str(self.workspace.resolve()), 'run_dir': str(self.run_dir.resolve()),
                  'parent_head': baseline.parent_head}
        proof = {'run_id': self.run_dir.name, 'convergence_id': 'exec-1', 'blocking_findings': [],
                 'reviewer': {**fields, 'role': 'reviewer', 'status': 'APPROVE'},
                 'gate': {**fields, 'role': 'gate', 'verdict': 'approve'}}
        approved = {'status': 'APPROVE', 'epoch': 0, 'item_uuid': life['item_uuid'],
                    'run_id': self.run_dir.name, 'parent': life['parent'],
                    'candidate_oid': revision.tree_oid, 'proof': proof}
        def launch(request):
            (baseline.root / 'tracked.txt').write_text('FINISH wrote code\n')
            return {'sandbox_id': 'fake-stopped', 'request_sha256': request['sha256'], 'status': 'READY'}
        context = {'baseline': baseline, 'revision': revision, 'launch': launch,
                   'sandbox_stopped': lambda identity: identity == 'fake-stopped'}
        self.assertEqual(co.fake_lifecycle_route(approved, finish_context=context), 'EXEC')
        self.assertEqual(co.state['lifecycle']['epoch'], 1)
        self.assertNotEqual(co.state['lifecycle']['candidate_oid'], revision.tree_oid)
        self.assertEqual([row['stage'] for row in co.state['lifecycle']['receipts']], ['EXEC', 'FINISH'])

    def fake_chain_only_finish_fixture(self):
        command = self.command('--lifecycle-mode', 'on', '--stop-after-plan')
        co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn(chain_only=True)
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        baseline = ct.baseline_from_binding(ingest['baseline'])
        finish_baseline = dataclasses.replace(baseline, separate_filesystems=True)
        revision = ct.CandidateRevision(ingest['output_oid'], tuple(ingest['manifest']), 0)

        launched = []
        def launch(request):
            launched.append(request)
            (baseline.root / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
            return {'sandbox_id': 'fake-stopped', 'request_sha256': request['sha256'], 'status': 'READY'}

        context = {'baseline': finish_baseline, 'revision': revision, 'launch': launch,
                   'sandbox_stopped': lambda sandbox_id: sandbox_id == 'fake-stopped',
                   'simulated_separation': True}
        return co, ingest, baseline, context, launched

    def test_fake_chain_only_finish_write_reingests_and_reviews_new_oid(self):
        co, ingest, baseline, context, launched = self.fake_chain_only_finish_fixture()
        revision = ct.CandidateRevision(ingest['output_oid'], tuple(ingest['manifest']), 0)
        self.assertEqual(co.fake_lifecycle_route(None, finish_context=context, polish_stub=True,
                         docs_stub=True, chain_only=True), 'EXEC')
        chain = co.state['fake_candidate_chain']
        self.assertEqual(len(launched), 1)
        self.assertEqual(co.state['lifecycle']['epoch'], 1)
        self.assertEqual(chain['ingest_id'], co.state['fake_ingest_receipt']['id'])
        self.assertEqual(chain['oid'], co.state['lifecycle']['candidate_oid'])
        self.assertEqual(chain['oid'], co.state['fake_candidate_test']['oid'])
        self.assertEqual(co.state['fake_candidate_review']['id'], chain['review_id'])

    def test_fake_chain_only_writer_reentry_recovers_after_advance_crash(self):
        co, ingest, baseline, context, _ = self.fake_chain_only_finish_fixture()
        with patch.object(co, 'fake_finish_writer_ingest', side_effect=RuntimeError('crash after advance')):
            with self.assertRaisesRegex(RuntimeError, 'crash after advance'):
                co.fake_lifecycle_route(None, finish_context=context, polish_stub=True,
                                        docs_stub=True, chain_only=True)
        self.assertEqual(co.state['lifecycle']['stage'], 'EXEC')
        self.assertEqual(co.state['lifecycle']['epoch'], 1)
        self.assertIsNone(co.state['fake_ingest_receipt'].get('source_writer_request_id'))
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True),
                         'STOP_BEFORE_SECURITY')
        ingest = co.state['fake_ingest_receipt']
        self.assertEqual(ingest['source_writer_request_id'], co.state['lifecycle']['receipts'][1]['request_id'])
        self.assertEqual(co.state['fake_candidate_chain']['oid'], co.state['lifecycle']['candidate_oid'])

    def test_fake_chain_only_reviewer_rejection_cannot_resume_same_writer_oid(self):
        co, _, _, context, _ = self.fake_chain_only_finish_fixture()
        with patch.dict(os.environ, {'FAKE_EXEC_MIXED_REVISE': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'OID reviewer did not approve'):
                co.fake_lifecycle_route(None, finish_context=context, polish_stub=True,
                                        docs_stub=True, chain_only=True)
        self.assertEqual(co.state['lifecycle']['stage'], 'EXEC')
        self.assertTrue(co.state.get('fake_candidate_review_rejected'))
        count = len(co.state['turns'])
        with self.assertRaisesRegex(RuntimeError, 'current successful test'):
            co.fake_lifecycle_route(None, stub_mode=True, chain_only=True)
        self.assertEqual(len(co.state['turns']), count)

    def test_fake_chain_only_security_writer_reentry_holds(self):
        co, _, _, _, _ = self.fake_chain_only_finish_fixture()
        life = co.state['lifecycle']
        life.update(stage='EXEC', epoch=1)
        life['receipts'].append({'request_id': 'security-write', 'stage': 'SECURITY', 'epoch': 1,
                                 'item_uuid': life['item_uuid'], 'candidate_oid': life['candidate_oid'],
                                 'parent': life['parent'], 'output_oid': life['candidate_oid']})
        co.save()
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True), 'HOLD')
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('abort or start a new run', co.state['hold_reason'])

    def test_fake_chain_only_exec_receipt_hold_names_recovery(self):
        co, ingest, _, _, _ = self.fake_chain_only_finish_fixture()
        life = co.state['lifecycle']
        life.update(epoch=1, candidate_oid=ingest['output_oid'])
        life['receipts'].append({'request_id': 'exec-crash', 'stage': 'EXEC', 'epoch': 1,
                                 'item_uuid': life['item_uuid'], 'candidate_oid': life['candidate_oid'],
                                 'parent': life['parent'], 'output_oid': life['candidate_oid']})
        co.save()
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True), 'HOLD')
        self.assertIn('EXEC receipt needs abort or new run', co.state['hold_reason'])

    def test_fake_chain_only_oid_test_failure_does_not_reenter(self):
        co, _, _, context, _ = self.fake_chain_only_finish_fixture()
        turns = len(co.state['turns'])
        with patch.object(co, 'fake_candidate_oid_test', side_effect=RuntimeError('OID test failed')):
            with self.assertRaisesRegex(RuntimeError, 'OID test failed'):
                co.fake_lifecycle_route(None, finish_context=context, polish_stub=True,
                                        docs_stub=True, chain_only=True)
        self.assertEqual(co.state['lifecycle']['stage'], 'EXEC')
        self.assertIsNone(co.state.get('fake_candidate_chain'))
        self.assertEqual(len(co.state['turns']), turns)

    def test_fake_chain_only_writer_reingest_rejects_post_receipt_change(self):
        co, ingest, baseline, context, _ = self.fake_chain_only_finish_fixture()
        original = co.fake_finish_writer_ingest

        def change_then_ingest():
            (baseline.root / 'sum_ints.py').write_text('changed after FINISH receipt\n')
            return original()

        with patch.object(co, 'fake_finish_writer_ingest', side_effect=change_then_ingest):
            with self.assertRaisesRegex(RuntimeError, 'FINISH output differs from candidate OID'):
                co.fake_lifecycle_route(None, finish_context=context, polish_stub=True,
                                        docs_stub=True, chain_only=True)
        receipt = co.state['lifecycle']['receipts'][-1]
        self.assertEqual(receipt['stage'], 'FINISH')
        self.assertNotEqual(co.state['fake_candidate_chain']['oid'], receipt['output_oid'])

    def test_fake_lifecycle_pending_finish_cannot_switch_to_stub(self):
        command = self.command('--lifecycle-mode', 'on')
        co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        life = co.state['lifecycle']
        approved = {'status': 'APPROVE', 'epoch': 0, 'item_uuid': life['item_uuid'],
                    'run_id': self.run_dir.name, 'parent': life['parent'], 'candidate_oid': 'a' * 40}
        original = co.fake_lifecycle_event
        def interrupted(event, value):
            if event == 'receipt' and value['stage'] == 'FINISH':
                raise RuntimeError('simulated FINISH crash')
            original(event, value)
        with patch.object(co, 'fake_lifecycle_event', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'simulated FINISH crash'):
                co.fake_lifecycle_route(approved, stub_mode=True)
        self.assertEqual(co.state['lifecycle']['pending']['stage'], 'FINISH')
        with self.assertRaisesRegex(ValueError, 'uncertain FINISH request'):
            co.fake_lifecycle_route(approved, stub_mode=True)

    def fake_lifecycle_ready_for_specialists(self, docs=False, security_paths=(), docs_allowlist=()):
        flags = ('--docs-file', 'docs/guide.md') if docs else ()
        allow_flags = tuple(value for path in docs_allowlist for value in ('--docs-allowlist', path))
        command = self.command('--lifecycle-mode', 'on', *flags, *allow_flags)
        co = rc.Coordinator(rc.parser().parse_args(command[2:]), _fake_lifecycle=True)
        scratch = self.root / 'scratch'; scratch.mkdir()
        paths = (('tracked.txt', 'docs/guide.md') if docs else ('tracked.txt',)) + tuple(security_paths)
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, paths)
        baseline = dataclasses.replace(baseline, separate_filesystems=True)
        revision = ct.ingest_candidate_revision(baseline)
        life = co.state['lifecycle']
        fields = {'candidate_oid': revision.tree_oid, 'epoch': 0, 'phase': 'EXEC',
                  'run_id': self.run_dir.name, 'convergence_id': 'exec-1',
                  'workspace': str(self.workspace.resolve()), 'run_dir': str(self.run_dir.resolve()),
                  'parent_head': baseline.parent_head}
        proof = {'run_id': self.run_dir.name, 'convergence_id': 'exec-1', 'blocking_findings': [],
                 'reviewer': {**fields, 'role': 'reviewer', 'status': 'APPROVE'},
                 'gate': {**fields, 'role': 'gate', 'verdict': 'approve'}}
        approved = {'status': 'APPROVE', 'epoch': 0, 'item_uuid': life['item_uuid'],
                    'run_id': self.run_dir.name, 'parent': life['parent'],
                    'candidate_oid': revision.tree_oid, 'proof': proof}
        def launch(request):
            return {'sandbox_id': 'fake-stopped', 'request_sha256': request['sha256'], 'status': 'READY'}
        context = {'baseline': baseline, 'revision': revision, 'launch': launch,
                   'sandbox_stopped': lambda identity: identity == 'fake-stopped',
                   'tested_oid': revision.tree_oid}
        return co, approved, context

    def add_fake_coordinator_docs_blocker(self, co, approved=None, finish=None):
        if approved is not None:
            co.fake_lifecycle_route(approved, finish_context=finish, polish_stub=True,
                                    docs_stub=True)
        return co.record_findings('coordinator', 'SECURITY', co.state['lifecycle']['epoch'], [{
            'severity': 'MAJOR', 'file': 'docs/guide.md',
            'summary': 'reserved docs require owner replay',
            'failure_scenario': 'unreviewed reserved document repair'}])

    def test_fake_lifecycle_specialist_budget_and_clean_approval(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists()
        seen = []
        def specialist(request):
            seen.append(request)
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        self.assertEqual(co.fake_lifecycle_route(approved, finish_context=finish,
                         polish_context={'python-reviewer': specialist}, docs_stub=True), 'STOP_BEFORE_SECURITY')
        self.assertEqual([row['role'] for row in seen], ['specialist:python-reviewer'])
        self.assertEqual(co.state['lifecycle']['polish_calls'], 1)
        self.assertEqual(co.state['lifecycle']['specialist_counts'], {'python-reviewer': 1})
        self.assertEqual(co.state['lifecycle']['receipts'][2]['specialists'], ('python-reviewer',))

    def test_fake_lifecycle_specialist_blocker_stops_before_docs(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists()
        def specialist(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'findings': [{'severity': 'MAJOR', 'file': 'tracked.txt',
                                  'summary': 'specialist blocker', 'failure_scenario': 'unsafe'}]}
        with self.assertRaisesRegex(ValueError, 'open specialist blocker'):
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': specialist})
        self.assertEqual(co.state['lifecycle']['stage'], 'POLISH-Q')
        self.assertEqual(co.state['lifecycle']['hold_reason'], 'open specialist blocker')
        self.assertNotIn('DOCS', [row['stage'] for row in co.state['lifecycle']['receipts']])
        self.assertEqual(co.blocking_open_findings()[0]['owner_role'], 'specialist:python-reviewer')
        with self.assertRaisesRegex(ValueError, 'uncertain POLISH-Q request'):
            co.fake_lifecycle_route(approved, polish_stub=True)

    def test_fake_lifecycle_specialist_budget_refuses_before_dispatch(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists()
        co.state['lifecycle']['polish_calls'] = rc.budget_policy.BUDGET_CAPS['POLISH-Q'][0]
        co.save()
        def forbidden(request):
            self.fail('budget exhausted before specialist dispatch')
        specialists = {'python-reviewer': forbidden}
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            co.fake_lifecycle_route(approved, finish_context=finish, polish_context=specialists)
        self.assertEqual(co.state['lifecycle']['stage'], 'POLISH-Q')
        self.assertIsNone(co.state['lifecycle']['pending'])
        co.state['lifecycle']['polish_calls'] = 0
        co.state['lifecycle']['specialist_counts'] = {'python-reviewer': 4}
        co.save()
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            co.fake_lifecycle_route(approved, polish_context=specialists)
        self.assertIsNone(co.state['lifecycle']['pending'])

    def test_fake_lifecycle_polish_stub_cannot_bypass_blocker_or_budget(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists()
        co.record_findings('reviewer', 'EXEC', 1, [{'severity': 'MAJOR', 'file': 'tracked.txt',
            'summary': 'existing blocker', 'failure_scenario': 'unsafe'}])
        with self.assertRaisesRegex(ValueError, 'open blocker'):
            co.fake_lifecycle_route(approved, stub_mode=True)
        self.assertNotIn('DOCS', [row['stage'] for row in co.state['lifecycle']['receipts']])
        co.state['finding_ledger'].clear()
        co.state['lifecycle']['polish_calls'] = rc.budget_policy.BUDGET_CAPS['POLISH-Q'][0]
        co.save()
        with self.assertRaisesRegex(ValueError, 'budget exhausted'):
            co.fake_lifecycle_route(approved, stub_mode=True)

    def test_fake_lifecycle_specialist_crash_counts_durable_start(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists()
        def crash(request):
            raise RuntimeError('fake specialist crashed')
        with self.assertRaisesRegex(RuntimeError, 'fake specialist crashed'):
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': crash})
        self.assertEqual(co.state['lifecycle']['polish_calls'], 1)
        self.assertEqual(co.state['lifecycle']['specialist_counts'], {'python-reviewer': 1})
        self.assertEqual(co.state['invocations_used'], 1)
        with self.assertRaisesRegex(ValueError, 'uncertain POLISH-Q request'):
            co.fake_lifecycle_route(approved, polish_context={'python-reviewer': crash})

    def test_fake_lifecycle_unknown_specialist_severity_cannot_pass(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists()
        def unknown(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'],
                    'findings': [{'severity': 'high', 'file': 'tracked.txt',
                                  'summary': 'unknown schema', 'failure_scenario': 'unsafe'}]}
        with self.assertRaisesRegex(ValueError, 'unknown severity'):
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': unknown})
        self.assertEqual(co.state['lifecycle']['stage'], 'POLISH-Q')
        self.assertNotIn('DOCS', [row['stage'] for row in co.state['lifecycle']['receipts']])

    def test_fake_lifecycle_docs_write_retests_and_replays_to_final_oid(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        baseline, first = finish['baseline'], finish['revision']
        def specialist(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        specialists = {'python-reviewer': specialist}
        tested = []
        def write_docs(root, path):
            target = root / path; target.parent.mkdir(exist_ok=True)
            target.write_text('# Guide\n')
        def retest(oid):
            tested.append(oid)
            return oid
        docs = {'baseline': baseline, 'before': first, 'write': write_docs, 'test': retest}
        self.assertEqual(co.fake_lifecycle_route(approved, finish_context=finish,
                         polish_context=specialists, docs_context=docs), 'EXEC')
        final = ct.ingest_candidate_revision(baseline)
        self.assertNotEqual(final.tree_oid, first.tree_oid)
        self.assertEqual(tested, [final.tree_oid])
        self.assertEqual(co.state['lifecycle']['receipts'][3]['docs_file'], 'docs/guide.md')
        self.assertEqual(co.state['lifecycle']['receipts'][3]['invalidated'], ('*',))
        second = dict(approved, epoch=1, candidate_oid=final.tree_oid)
        proof = json.loads(json.dumps(approved['proof']))
        for role in ('reviewer', 'gate'):
            proof[role].update(epoch=1, candidate_oid=final.tree_oid)
        second['proof'] = proof
        next_finish = dict(finish, revision=final, tested_oid=final.tree_oid)
        docs.update(before=final, write=lambda root, path: (root / path).write_text('# Guide v2\n'))
        self.assertEqual(co.fake_lifecycle_route(second, finish_context=next_finish,
                         polish_context=specialists, docs_context=docs), 'EXEC')
        final_again = ct.ingest_candidate_revision(baseline)
        self.assertNotEqual(final_again.tree_oid, final.tree_oid)
        third = dict(approved, epoch=2, candidate_oid=final_again.tree_oid)
        for role in ('reviewer', 'gate'):
            proof[role].update(epoch=2, candidate_oid=final_again.tree_oid)
        third['proof'] = proof
        final_finish = dict(finish, revision=final_again, tested_oid=final_again.tree_oid)
        docs.update(before=final_again, write=lambda root, path: None)
        self.assertEqual(co.fake_lifecycle_route(third, finish_context=final_finish,
                         polish_context=specialists, docs_context=docs), 'STOP_BEFORE_SECURITY')
        self.assertEqual(co.state['lifecycle']['candidate_oid'], final_again.tree_oid)
        self.assertEqual([row['stage'] for row in co.state['lifecycle']['receipts']],
                         ['EXEC', 'FINISH', 'POLISH-Q', 'DOCS'] * 3)
        self.assertEqual(tested, [final.tree_oid, final_again.tree_oid])

    def test_fake_lifecycle_bad_docs_context_does_not_leave_pending(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def specialist(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        bad = {'baseline': finish['baseline'],
               'before': ct.CandidateRevision('0' * 40, (), 0),
               'write': lambda root, path: None, 'test': lambda oid: oid}
        with self.assertRaisesRegex(ValueError, 'candidate differs'):
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': specialist}, docs_context=bad)
        self.assertEqual(co.state['lifecycle']['stage'], 'DOCS')
        self.assertIsNone(co.state['lifecycle']['pending'])

    def test_fake_lifecycle_security_approves_current_tree_before_delivery(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def specialist(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        def security(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read candidate diff'], 'findings': []}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'review': security}
        self.assertEqual(co.fake_lifecycle_route(approved, finish_context=finish,
                         polish_context={'python-reviewer': specialist}, docs_stub=True,
                         security_context=context), 'STOP_BEFORE_DELIVERY')
        receipt = co.state['lifecycle']['receipts'][-1]
        self.assertEqual(receipt['stage'], 'SECURITY')
        self.assertEqual(receipt['candidate_oid'], finish['revision'].tree_oid)
        self.assertIn('tracked.txt', receipt['scanned_paths'])

    def test_fake_lifecycle_security_sensitive_path_blocks_before_review(self):
        (self.workspace / 'secret.key').write_text('fake secret\n')
        subprocess.run(['git', 'add', 'secret.key'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'fake secret'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def specialist(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        def forbidden(request):
            self.fail('sensitive path must block before security reviewer')
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'review': forbidden}
        with self.assertRaisesRegex(ValueError, 'sensitive candidate paths: secret.key'):
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': specialist}, docs_stub=True,
                                    security_context=context)
        self.assertEqual(co.state['lifecycle']['stage'], 'SECURITY')
        self.assertIsNone(co.state['lifecycle']['pending'])
        self.assertNotIn('SECURITY', [row['stage'] for row in co.state['lifecycle']['receipts']])

    def test_fake_lifecycle_security_dangling_sensitive_symlink_blocks(self):
        (self.workspace / 'id_rsa').symlink_to('missing-key')
        subprocess.run(['git', 'add', 'id_rsa'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'fake symlink'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def specialist(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        def forbidden(request):
            self.fail('sensitive symlink must block before security reviewer')
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'review': forbidden}
        with self.assertRaisesRegex(ValueError, 'sensitive candidate paths: id_rsa'):
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': specialist}, docs_stub=True,
                                    security_context=context)
        self.assertIsNone(co.state['lifecycle']['pending'])

    def test_fake_lifecycle_security_critical_finding_blocks_delivery(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def specialist(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        def security(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read candidate diff'],
                    'findings': [{'severity': 'CRITICAL', 'file': 'tracked.txt',
                                  'summary': 'security defect', 'failure_scenario': 'reachable'}]}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'review': security}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': specialist}, docs_stub=True,
                                    security_context=context)
        self.assertEqual(co.state['lifecycle']['stage'], 'SECURITY')
        self.assertEqual(co.blocking_open_findings()[0]['source'], 'security-reviewer')
        self.assertNotIn('SECURITY', [row['stage'] for row in co.state['lifecycle']['receipts']])

    def test_fake_security_reviewer_owner_hold_allows_only_frozen_repair_path(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def critical(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read candidate diff'],
                    'findings': [{'severity': 'CRITICAL', 'file': 'tracked.txt',
                                  'summary': 'repair tracked file', 'failure_scenario': 'reachable'}]}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'review': critical}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(approved, stub_mode=True, security_context=context)
        marker = co.state['lifecycle']['awaiting_owner_reverify']
        self.assertEqual(marker['owner'], 'security-reviewer')
        self.assertEqual(marker['paths'], ('tracked.txt',))
        self.assertIsNone(co.state['lifecycle']['pending'])
        self.assertEqual(co.state['lifecycle']['owner_hold_receipt']['finding_ids'], marker['finding_ids'])
        self.assertTrue((co.evidence / 'fake-SECURITY-0-owner-hold.json').is_file())
        with self.assertRaisesRegex(RuntimeError, 'owning specialist'):
            co.apply_dispositions([{'id': marker['finding_ids'][0], 'disposition': 'fixed',
                                    'evidence': 'other reviewer claim'}], 2,
                                  list(marker['finding_ids']), 'persistent-reviewer')
        denied = {'proposals': (), 'paths': ('docs/guide.md',),
                  'allowed_paths': ('docs/guide.md',), 'reserved_docs': (),
                  'write': lambda root, paths: self.fail('ungranted writer ran')}
        with self.assertRaisesRegex(ValueError, 'exact candidate-file grant'):
            co.fake_lifecycle_route(approved, security_context={**context, 'repair': denied})
        self.assertIsNone(co.state['lifecycle']['pending'])
        repair = {'proposals': (), 'paths': ('tracked.txt',),
                  'allowed_paths': ('tracked.txt',), 'reserved_docs': (),
                  'write': lambda root, paths: (root / 'tracked.txt').write_text('repaired\n')}
        self.assertEqual(co.fake_lifecycle_route(approved,
                         security_context={**context, 'repair': repair}), 'EXEC')
        self.assertEqual(co.state['lifecycle']['awaiting_owner_reverify']['repair_oid'],
                         co.state['lifecycle']['candidate_oid'])
        self.assertEqual(co.state['lifecycle']['receipts'][-1]['request_id'], 'fake-SECURITY-0-repair')
        after = ct.ingest_candidate_revision(finish['baseline'])
        life = co.state['lifecycle']
        next_approval = {'status': 'APPROVE', 'epoch': life['epoch'],
                         'item_uuid': life['item_uuid'], 'run_id': self.run_dir.name,
                         'parent': life['parent'], 'candidate_oid': after.tree_oid}
        with self.assertRaisesRegex(ValueError, 'repair already dispatched'):
            co.fake_lifecycle_route(next_approval, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': after, 'repair': repair})

    def test_fake_security_same_owner_disposes_frozen_id_after_repair(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def critical(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read candidate diff'],
                    'findings': [{'severity': 'CRITICAL', 'file': 'tracked.txt',
                                  'summary': 'repair tracked file', 'failure_scenario': 'reachable'}]}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'review': critical}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(approved, stub_mode=True, security_context=context)
        frozen = co.state['lifecycle']['awaiting_owner_reverify']['finding_ids']
        repair = {'proposals': (), 'paths': ('tracked.txt',), 'allowed_paths': ('tracked.txt',),
                  'reserved_docs': (),
                  'write': lambda root, paths: (root / 'tracked.txt').write_text('repaired\n')}
        frozen_manifest = co.state['role_dispatch_manifest_sha256']
        co.state['role_dispatch_manifest_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'owner identity changed'):
            co.fake_lifecycle_route(approved, security_context={**context, 'repair': repair})
        co.state['role_dispatch_manifest_sha256'] = frozen_manifest
        self.assertEqual(co.fake_lifecycle_route(approved,
                         security_context={**context, 'repair': repair}), 'EXEC')
        after = ct.ingest_candidate_revision(finish['baseline'])
        life = co.state['lifecycle']
        replay = dict(approved, epoch=life['epoch'], candidate_oid=after.tree_oid)
        seen = []
        def owner_review(request):
            seen.append(request['prior_finding_ids'])
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read repaired candidate diff'], 'findings': [],
                    'dispositions': [{'id': frozen[0], 'disposition': 'fixed',
                                      'evidence': 'verified repair on new OID'}]}
        current = {'baseline': finish['baseline'], 'revision': after, 'review': owner_review}
        self.assertEqual(co.fake_lifecycle_route(replay, stub_mode=True,
                         security_context=current), 'STOP_BEFORE_DELIVERY')
        self.assertEqual(seen, [tuple(frozen)])
        self.assertEqual(co.state['finding_ledger'][0]['status'], 'fixed')
        self.assertNotIn('awaiting_owner_reverify', co.state['lifecycle'])

    def test_fake_security_approve_without_frozen_disposition_holds(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def critical(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read candidate diff'],
                    'findings': [{'severity': 'CRITICAL', 'file': 'tracked.txt',
                                  'summary': 'repair tracked file', 'failure_scenario': 'reachable'}]}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'review': critical}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(approved, stub_mode=True, security_context=context)
        repair = {'proposals': (), 'paths': ('tracked.txt',), 'allowed_paths': ('tracked.txt',),
                  'reserved_docs': (),
                  'write': lambda root, paths: (root / 'tracked.txt').write_text('repaired\n')}
        self.assertEqual(co.fake_lifecycle_route(approved,
                         security_context={**context, 'repair': repair}), 'EXEC')
        after = ct.ingest_candidate_revision(finish['baseline'])
        life = co.state['lifecycle']
        replay = dict(approved, epoch=life['epoch'], candidate_oid=after.tree_oid)
        approve = lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'],
                               'observed_tools': ['read repaired candidate diff'], 'findings': []}
        security_before = sum(row['stage'] == 'SECURITY' for row in life['receipts'])
        with self.assertRaisesRegex(ValueError, 'disposition is missing'):
            co.fake_lifecycle_route(replay, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': after, 'review': approve})
        self.assertEqual(co.state['finding_ledger'][0]['status'], 'open')
        self.assertEqual(sum(row['stage'] == 'SECURITY' for row in co.state['lifecycle']['receipts']),
                         security_before)

    def test_fake_security_new_critical_keeps_old_open_id_and_repair_lineage(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def critical(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read candidate diff'],
                    'findings': [{'severity': 'CRITICAL', 'file': 'tracked.txt',
                                  'summary': 'first issue', 'failure_scenario': 'reachable'}]}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'review': critical}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(approved, stub_mode=True, security_context=context)
        old_id = co.state['lifecycle']['awaiting_owner_reverify']['finding_ids'][0]
        repair = {'proposals': (), 'paths': ('tracked.txt',), 'allowed_paths': ('tracked.txt',),
                  'reserved_docs': (),
                  'write': lambda root, paths: (root / 'tracked.txt').write_text('repaired\n')}
        self.assertEqual(co.fake_lifecycle_route(approved,
                         security_context={**context, 'repair': repair}), 'EXEC')
        after = ct.ingest_candidate_revision(finish['baseline'])
        life = co.state['lifecycle']
        prior_request = life['awaiting_owner_reverify']['request_id']
        replay = dict(approved, epoch=life['epoch'], candidate_oid=after.tree_oid)
        def still_blocking(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read repaired diff'],
                    'dispositions': [{'id': old_id, 'disposition': 'still_open',
                                      'evidence': 'first issue remains'}],
                    'findings': [{'severity': 'CRITICAL', 'file': 'tracked.txt',
                                  'summary': 'second issue', 'failure_scenario': 'reachable'}]}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(replay, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': after, 'review': still_blocking})
        marker = co.state['lifecycle']['awaiting_owner_reverify']
        self.assertIn(old_id, marker['finding_ids'])
        self.assertEqual(len(marker['finding_ids']), 2)
        self.assertEqual(marker['repair_history'], ((after.tree_oid, prior_request),))
        self.assertNotIn('repair_oid', marker)

    def test_fake_security_still_open_only_reopens_repair_without_pending(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def critical(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read candidate diff'],
                    'findings': [{'severity': 'CRITICAL', 'file': 'tracked.txt',
                                  'summary': 'first issue', 'failure_scenario': 'reachable'}]}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'review': critical}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(approved, stub_mode=True, security_context=context)
        old_id = co.state['lifecycle']['awaiting_owner_reverify']['finding_ids'][0]
        repair = {'proposals': (), 'paths': ('tracked.txt',), 'allowed_paths': ('tracked.txt',),
                  'reserved_docs': (),
                  'write': lambda root, paths: (root / 'tracked.txt').write_text('repaired\n')}
        self.assertEqual(co.fake_lifecycle_route(approved,
                         security_context={**context, 'repair': repair}), 'EXEC')
        after = ct.ingest_candidate_revision(finish['baseline'])
        life = co.state['lifecycle']
        replay = dict(approved, epoch=life['epoch'], candidate_oid=after.tree_oid)
        def still_open(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read repaired diff'], 'findings': [],
                    'dispositions': [{'id': old_id, 'disposition': 'still_open',
                                      'evidence': 'repair did not solve issue'}]}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(replay, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': after, 'review': still_open})
        marker = co.state['lifecycle']['awaiting_owner_reverify']
        self.assertEqual(marker['finding_ids'], (old_id,))
        self.assertNotIn('repair_oid', marker)
        self.assertIsNone(co.state['lifecycle']['pending'])
        self.assertEqual(co.state['finding_ledger'][0]['status'], 'open')

    def test_fake_security_owner_disposition_after_docs_changes_oid(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        baseline = finish['baseline']
        def critical(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read candidate diff'],
                    'findings': [{'severity': 'CRITICAL', 'file': 'tracked.txt',
                                  'summary': 'repair tracked file', 'failure_scenario': 'reachable'}]}
        context = {'baseline': baseline, 'revision': finish['revision'], 'review': critical}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(approved, stub_mode=True, security_context=context)
        frozen_id = co.state['lifecycle']['awaiting_owner_reverify']['finding_ids'][0]
        repair = {'proposals': (), 'paths': ('tracked.txt',), 'allowed_paths': ('tracked.txt',),
                  'reserved_docs': (),
                  'write': lambda root, paths: (root / 'tracked.txt').write_text('repaired\n')}
        self.assertEqual(co.fake_lifecycle_route(approved,
                         security_context={**context, 'repair': repair}), 'EXEC')
        repaired = ct.ingest_candidate_revision(baseline)
        life = co.state['lifecycle']
        next_approval = dict(approved, epoch=life['epoch'], candidate_oid=repaired.tree_oid)
        proof = json.loads(json.dumps(approved['proof']))
        for role in ('reviewer', 'gate'):
            proof[role].update(epoch=life['epoch'], candidate_oid=repaired.tree_oid)
        next_approval['proof'] = proof
        next_finish = dict(finish, revision=repaired, tested_oid=repaired.tree_oid)
        specialist = lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'],
                                  'findings': []}
        def write_docs(root, path):
            target = root / path
            target.parent.mkdir(exist_ok=True)
            target.write_text('# Replayed docs\n')
        docs = {'baseline': baseline, 'before': repaired, 'write': write_docs,
                'test': lambda oid: oid}
        self.assertEqual(co.fake_lifecycle_route(next_approval, finish_context=next_finish,
                         polish_context={'python-reviewer': specialist}, docs_context=docs), 'EXEC')
        after_docs = ct.ingest_candidate_revision(baseline)
        self.assertNotEqual(after_docs.tree_oid, repaired.tree_oid)
        life = co.state['lifecycle']
        final_approval = dict(next_approval, epoch=life['epoch'], candidate_oid=after_docs.tree_oid)
        for role in ('reviewer', 'gate'):
            proof[role].update(epoch=life['epoch'], candidate_oid=after_docs.tree_oid)
        final_approval['proof'] = proof
        final_finish = dict(finish, revision=after_docs, tested_oid=after_docs.tree_oid)
        docs.update(before=after_docs, write=lambda root, path: None)
        def owner_review(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read final OID diff'], 'findings': [],
                    'dispositions': [{'id': frozen_id, 'disposition': 'fixed',
                                      'evidence': 'verified at final OID'}]}
        self.assertEqual(co.fake_lifecycle_route(final_approval, finish_context=final_finish,
                         polish_context={'python-reviewer': specialist}, docs_context=docs,
                         security_context={'baseline': baseline, 'revision': after_docs,
                                           'review': owner_review}), 'STOP_BEFORE_DELIVERY')
        self.assertEqual(co.state['finding_ledger'][0]['status'], 'fixed')

    def test_fake_security_owner_marker_does_not_exempt_foreign_blocker(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def critical(request):
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read candidate diff'],
                    'findings': [{'severity': 'CRITICAL', 'file': 'tracked.txt',
                                  'summary': 'security finding', 'failure_scenario': 'reachable'}]}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'review': critical}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(approved, stub_mode=True, security_context=context)
        co.record_findings('persistent-reviewer', 'EXEC', 99,
                           [{'severity': 'MAJOR', 'file': 'tracked.txt',
                             'summary': 'foreign blocker', 'failure_scenario': 'reachable'}])
        repair = {'proposals': (), 'paths': ('tracked.txt',),
                  'allowed_paths': ('tracked.txt',), 'reserved_docs': (),
                  'write': lambda root, paths: self.fail('foreign blocker let writer run')}
        with self.assertRaisesRegex(ValueError, 'open blocker'):
            co.fake_lifecycle_route(approved, security_context={**context, 'repair': repair})
        self.assertIsNone(co.state['lifecycle']['pending'])

    def test_fake_security_ignore_repair_requires_operator_consent_and_replays_exec(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('.gitignore',))
        def specialist(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        proposals = [('Generic secret files', '*secret*', [])]
        written = []
        def write(root, paths):
            written.append(paths)
            with (root / '.gitignore').open('a') as target:
                target.write('*secret*\n')
        repair = {'proposals': proposals, 'paths': (), 'allowed_paths': ('.gitignore',),
                  'reserved_docs': ('docs/guide.md',), 'write': write}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair}
        with self.assertRaisesRegex(ValueError, 'operator confirmation'):
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': specialist}, docs_stub=True,
                                    security_context=context)
        self.assertEqual(co.state['lifecycle']['stage'], 'SECURITY')
        self.assertIsNone(co.state['lifecycle']['pending'])
        self.assertEqual(written, [])
        digest = rc.security_repair_policy.repair_digest(
            self.run_dir.name, finish['revision'].tree_oid, proposals, ())
        repair['consent'] = {'decision': 'confirm', 'digest': digest, 'run_id': self.run_dir.name,
                             'actor': 'operator', 'time': '2026-09-29T02:00:00+09:00',
                             'command_sha256': hashlib.sha256(
                                 f'confirm-ignore --digest {digest}'.encode()).hexdigest()}
        self.assertEqual(co.fake_lifecycle_route(approved, security_context=context), 'EXEC')
        self.assertEqual(written, [('.gitignore',)])
        self.assertEqual(co.state['lifecycle']['epoch'], 1)
        self.assertFalse(co.state['gate_ran'])
        self.assertEqual(co.state['lifecycle']['receipts'][-1]['invalidated'], ('*',))

    def test_fake_security_sensitive_repair_write_replays_exec(self):
        (self.workspace / 'secret.key').write_text('fake secret\n')
        subprocess.run(['git', 'add', 'secret.key'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'fake secret'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('secret.key',))
        def specialist(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        repair = {'proposals': (), 'paths': ('secret.key',), 'allowed_paths': ('secret.key',),
                  'reserved_docs': ('docs/guide.md',),
                  'write': lambda root, paths: (root / 'secret.key').unlink()}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair}
        self.assertEqual(co.fake_lifecycle_route(approved, finish_context=finish,
                         polish_context={'python-reviewer': specialist}, docs_stub=True,
                         security_context=context), 'EXEC')
        self.assertEqual(co.state['lifecycle']['epoch'], 1)
        self.assertEqual(co.state['lifecycle']['receipts'][-1]['stage'], 'SECURITY')
        self.assertNotEqual(co.state['lifecycle']['receipts'][-1]['output_oid'],
                            finish['revision'].tree_oid)

    def test_fake_security_preflight_owner_rescans_after_exec_replay(self):
        (self.workspace / 'secret.key').write_text('fake secret\n')
        subprocess.run(['git', 'add', 'secret.key'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'fake secret'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('secret.key',))
        repair = {'proposals': (), 'paths': ('secret.key',), 'allowed_paths': ('secret.key',),
                  'reserved_docs': ('docs/guide.md',),
                  'write': lambda root, paths: (root / 'secret.key').unlink()}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair}
        self.assertEqual(co.fake_lifecycle_route(approved, stub_mode=True,
                         security_context=context), 'EXEC')
        marker = co.state['lifecycle']['awaiting_owner_reverify']
        self.assertEqual(marker['owners'], ('preflight',))
        after = ct.ingest_candidate_revision(finish['baseline'])
        life = co.state['lifecycle']
        next_approval = {'status': 'APPROVE', 'epoch': life['epoch'],
                         'item_uuid': life['item_uuid'], 'run_id': self.run_dir.name,
                         'parent': life['parent'], 'candidate_oid': after.tree_oid}
        security = lambda request: {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'],
                                    'observed_tools': ['read candidate diff'], 'findings': []}
        context = {'baseline': finish['baseline'], 'revision': after, 'review': security}
        self.assertEqual(co.fake_lifecycle_route(next_approval, stub_mode=True,
                         security_context=context), 'STOP_BEFORE_DELIVERY')
        self.assertNotIn('awaiting_owner_reverify', co.state['lifecycle'])

    def test_fake_security_ignore_owner_requires_exact_replayed_bytes(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('.gitignore',))
        def write(root, paths):
            with (root / '.gitignore').open('a') as output:
                output.write('*.pem\n')
        repair = {'proposals': (('Keys & certificates', '*.pem', ()),), 'paths': (),
                  'allowed_paths': ('.gitignore',), 'reserved_docs': (),
                  'write': write}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair}
        self.assertEqual(co.fake_lifecycle_route(approved, stub_mode=True,
                         security_context=context), 'EXEC')
        marker = co.state['lifecycle']['awaiting_owner_reverify']
        self.assertEqual(marker['owners'], ('ignore',))
        after = ct.ingest_candidate_revision(finish['baseline'])
        life = co.state['lifecycle']
        next_approval = {'status': 'APPROVE', 'epoch': life['epoch'],
                         'item_uuid': life['item_uuid'], 'run_id': self.run_dir.name,
                         'parent': life['parent'], 'candidate_oid': after.tree_oid}
        marker['ignore_sha256'] = '0' * 64
        security = lambda request: {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'],
                                    'observed_tools': ['read candidate diff'], 'findings': []}
        context = {'baseline': finish['baseline'], 'revision': after, 'review': security}
        with self.assertRaisesRegex(ValueError, 'ignore owner byte check differs'):
            co.fake_lifecycle_route(next_approval, stub_mode=True, security_context=context)
        self.assertIn('awaiting_owner_reverify', co.state['lifecycle'])
        self.assertNotEqual(co.state['lifecycle']['stage'], 'STOP_BEFORE_DELIVERY')

    def test_fake_security_ignore_owner_reaches_delivery_only_after_byte_audit(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('.gitignore',))
        def write(root, paths):
            with (root / '.gitignore').open('a') as output:
                output.write('*.pem\n')
        repair = {'proposals': (('Keys & certificates', '*.pem', ()),), 'paths': (),
                  'allowed_paths': ('.gitignore',), 'reserved_docs': (), 'write': write}
        self.assertEqual(co.fake_lifecycle_route(approved, stub_mode=True,
                         security_context={'baseline': finish['baseline'],
                                           'revision': finish['revision'], 'repair': repair}), 'EXEC')
        marker = co.state['lifecycle']['awaiting_owner_reverify']
        self.assertEqual(marker['owners'], ('ignore',))
        after = ct.ingest_candidate_revision(finish['baseline'])
        life = co.state['lifecycle']
        next_approval = {'status': 'APPROVE', 'epoch': life['epoch'],
                         'item_uuid': life['item_uuid'], 'run_id': self.run_dir.name,
                         'parent': life['parent'], 'candidate_oid': after.tree_oid}
        review = lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'],
                              'observed_tools': ['read final OID diff'], 'findings': []}
        self.assertEqual(co.fake_lifecycle_route(next_approval, stub_mode=True,
                         security_context={'baseline': finish['baseline'], 'revision': after,
                                           'review': review}), 'STOP_BEFORE_DELIVERY')
        self.assertNotIn('awaiting_owner_reverify', co.state['lifecycle'])

    def test_fake_security_reserved_docs_constraint_never_dispatches_security_writer(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        subprocess.run(['git', 'add', 'docs/guide.md'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        self.add_fake_coordinator_docs_blocker(co, approved, finish)
        repair = {'proposals': (), 'paths': ('docs/guide.md',),
                  'allowed_paths': ('docs/guide.md',), 'reserved_docs': ('docs/guide.md',),
                  'write': lambda root, paths: self.fail('SECURITY writer touched reserved docs')}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair}
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co.fake_lifecycle_route(approved, stub_mode=True, security_context=context)
        marker = co.state['lifecycle']['reserved_docs_constraint']
        self.assertEqual(marker['docs_file'], 'docs/guide.md')
        self.assertEqual(marker['source_oid'], finish['revision'].tree_oid)
        self.assertTrue((co.evidence / (marker['id'] + '-reserved-docs.json')).is_file())
        self.assertNotIn('SECURITY', [row['stage'] for row in co.state['lifecycle']['receipts']])
        approve = lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'],
                               'observed_tools': ['read candidate diff'], 'findings': []}
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co.fake_lifecycle_route(approved,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': finish['revision'], 'review': approve})
        self.assertEqual(co.state['lifecycle']['reserved_docs_constraint'], marker)

    def test_fake_reserved_docs_replay_routes_to_fresh_exec_then_security(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        subprocess.run(['git', 'add', 'docs/guide.md'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide'], cwd=self.workspace, check=True)
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan',
                                                   '--docs-file', 'docs/guide.md')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn(chain_only=True)
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True),
                         'STOP_BEFORE_SECURITY')
        self.add_fake_coordinator_docs_blocker(co)
        baseline = ct.baseline_from_binding(ingest['baseline'])
        before = ct.CandidateRevision(ingest['output_oid'], tuple(ingest['manifest']), 0)
        repair = {'proposals': (), 'paths': ('docs/guide.md',),
                  'allowed_paths': ('docs/guide.md',), 'reserved_docs': ('docs/guide.md',),
                  'write': lambda root, paths: self.fail('SECURITY writer touched reserved docs')}
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co.fake_lifecycle_route(None, chain_only=True,
                                    security_context={'baseline': baseline, 'revision': before,
                                                      'repair': repair})
        co.fake_reserved_docs_begin_replay()
        docs_baseline = ct.replace(baseline, authorized_prefixes=('sum_ints.py', 'docs/guide.md'))
        def write_docs(root, path):
            target = root / path
            target.parent.mkdir(exist_ok=True)
            target.write_text('# Repaired guide\n')
        docs = {'baseline': docs_baseline, 'before': before, 'write': write_docs,
                'test': lambda oid: oid}
        self.assertEqual(co.fake_lifecycle_route(None, chain_only=True, docs_context=docs), 'EXEC')
        replay = co.state['fake_ingest_receipt']
        tested = co.state['fake_candidate_test']
        self.assertEqual(tested['oid'], replay['output_oid'])
        self.assertEqual(co.state['fake_candidate_chain']['ingest_id'], replay['id'])
        self.assertEqual(co.state['fake_candidate_chain']['oid'], tested['oid'])
        prior_ledger = json.loads(json.dumps(co.state['finding_ledger']))
        prior_next_id = co.state['next_finding_id']
        co.record_findings('coordinator', 'SECURITY', 2, [{
            'severity': 'MAJOR', 'file': 'docs/guide.md',
            'summary': 'source set changed before approval', 'failure_scenario': 'new blocker'}])
        with self.assertRaisesRegex(RuntimeError, 'clean receipt chain'):
            co.fake_lifecycle_route(None, stub_mode=True, chain_only=True)
        self.assertIn('reserved_docs_constraint', co.state['lifecycle'])
        co.state['finding_ledger'] = prior_ledger
        co.state['next_finding_id'] = prior_next_id
        co.write_ledger()
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True),
                         'STOP_BEFORE_SECURITY')
        revision = ct.CandidateRevision(replay['output_oid'], tuple(replay['manifest']), 0)
        review = lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'],
                              'observed_tools': ['read final OID diff'], 'findings': []}
        self.assertEqual(co.fake_lifecycle_route(None, chain_only=True,
                         security_context={'baseline': ct.baseline_from_binding(replay['baseline']),
                                           'revision': revision, 'review': review}),
                         'STOP_BEFORE_DELIVERY')
        self.assertNotIn('reserved_docs_constraint', co.state['lifecycle'])

    def test_fake_reserved_docs_rejects_stale_blocker_allowance_after_replay(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        subprocess.run(['git', 'add', 'docs/guide.md'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide'], cwd=self.workspace, check=True)
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan',
                                                   '--docs-file', 'docs/guide.md')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn(chain_only=True)
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True),
                         'STOP_BEFORE_SECURITY')
        self.add_fake_coordinator_docs_blocker(co)
        baseline = ct.baseline_from_binding(ingest['baseline'])
        before = ct.CandidateRevision(ingest['output_oid'], tuple(ingest['manifest']), 0)
        repair = {'proposals': (), 'paths': ('docs/guide.md',),
                  'allowed_paths': ('docs/guide.md',), 'reserved_docs': ('docs/guide.md',),
                  'write': lambda root, paths: self.fail('SECURITY writer touched reserved docs')}
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co.fake_lifecycle_route(None, chain_only=True,
                                    security_context={'baseline': baseline, 'revision': before,
                                                      'repair': repair})
        marker = co.state['lifecycle']['reserved_docs_constraint']
        self.assertTrue(marker['owner_ids'])
        co.fake_reserved_docs_begin_replay()
        docs_baseline = ct.replace(baseline, authorized_prefixes=('sum_ints.py', 'docs/guide.md'))
        def write_docs(root, path):
            (root / path).write_text('# Repaired guide\n')
        docs = {'baseline': docs_baseline, 'before': before,
                'write': write_docs, 'test': lambda oid: oid}
        self.assertEqual(co.fake_lifecycle_route(None, chain_only=True, docs_context=docs), 'EXEC')
        replay = co.state['fake_ingest_receipt']
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True),
                         'STOP_BEFORE_SECURITY')
        revision = ct.CandidateRevision(replay['output_oid'], tuple(replay['manifest']), 0)
        with self.assertRaisesRegex(ValueError, 'one frozen docs_file'):
            co.fake_lifecycle_route(None, chain_only=True,
                                    security_context={'baseline': ct.baseline_from_binding(replay['baseline']),
                                                      'revision': revision, 'repair': repair})
        self.assertNotIn('reserved_docs_constraint', co.state['lifecycle'])
        self.assertEqual(co.blocking_open_findings(), [])

    def test_fake_reserved_docs_refuses_empty_owner_set_before_marker(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        subprocess.run(['git', 'add', 'docs/guide.md'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide'], cwd=self.workspace, check=True)
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan',
                                                   '--docs-file', 'docs/guide.md')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn(chain_only=True)
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True),
                         'STOP_BEFORE_SECURITY')
        co.state['lifecycle']['awaiting_owner_reverify'] = {
            'owner': 'security-reviewer', 'finding_ids': (),
            'role_manifest_sha256': co.state['role_dispatch_manifest_sha256']}
        repair = {'proposals': (), 'paths': ('docs/guide.md',),
                  'allowed_paths': ('docs/guide.md',), 'reserved_docs': ('docs/guide.md',),
                  'owner_finding_ids': (),
                  'write': lambda root, paths: self.fail('SECURITY writer touched reserved docs')}
        with self.assertRaisesRegex(ValueError, 'one frozen docs_file'):
            co.fake_lifecycle_route(None, chain_only=True,
                                    security_context={'baseline': ct.baseline_from_binding(ingest['baseline']),
                                                      'revision': ct.CandidateRevision(
                                                          ingest['output_oid'], tuple(ingest['manifest']), 0),
                                                      'repair': repair})
        self.assertNotIn('reserved_docs_constraint', co.state['lifecycle'])

    def test_fake_reviewer_owned_reserved_docs_keeps_finding_until_same_owner_review(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        subprocess.run(['git', 'add', 'docs/guide.md'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide'], cwd=self.workspace, check=True)
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan',
                                                   '--docs-file', 'docs/guide.md')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn(chain_only=True)
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True),
                         'STOP_BEFORE_SECURITY')
        baseline = ct.baseline_from_binding(ingest['baseline'])
        before = ct.CandidateRevision(ingest['output_oid'], tuple(ingest['manifest']), 0)
        def critical(req):
            return {'status': 'REVISE', 'candidate_oid': req['candidate_oid'],
                    'observed_tools': ['read OID diff'],
                    'findings': [{'severity': 'CRITICAL', 'file': 'docs/guide.md',
                                  'summary': 'unsafe guide', 'failure_scenario': 'reachable'}]}
        with self.assertRaisesRegex(ValueError, 'did not approve'):
            co.fake_lifecycle_route(None, chain_only=True,
                                    security_context={'baseline': baseline, 'revision': before,
                                                      'review': critical})
        frozen = co.state['lifecycle']['awaiting_owner_reverify']['finding_ids']
        repair = {'proposals': (), 'paths': ('docs/guide.md',),
                  'allowed_paths': ('docs/guide.md',), 'reserved_docs': ('docs/guide.md',),
                  'owner_finding_ids': frozen,
                  'write': lambda root, paths: self.fail('SECURITY writer touched reserved docs')}
        with self.assertRaisesRegex(ValueError, 'one frozen docs_file'):
            co.fake_lifecycle_route(None, chain_only=True,
                                    security_context={'baseline': baseline, 'revision': before,
                                                      'repair': {**repair, 'owner_finding_ids': ('F999',)}})
        self.assertNotIn('reserved_docs_constraint', co.state['lifecycle'])
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co.fake_lifecycle_route(None, chain_only=True,
                                    security_context={'baseline': baseline, 'revision': before,
                                                      'repair': repair})
        self.assertEqual(co.state['lifecycle']['reserved_docs_constraint']['owner_ids'], frozen)
        co.fake_reserved_docs_begin_replay()
        docs_baseline = ct.replace(baseline, authorized_prefixes=('sum_ints.py', 'docs/guide.md'))
        def write_docs(root, path):
            (root / path).write_text('# Repaired guide\n')
        docs = {'baseline': docs_baseline, 'before': before, 'write': write_docs,
                'test': lambda oid: oid}
        self.assertEqual(co.fake_lifecycle_route(None, chain_only=True, docs_context=docs), 'EXEC')
        replay = co.state['fake_ingest_receipt']
        prior_ledger = json.loads(json.dumps(co.state['finding_ledger']))
        co.record_findings('specialist:other', 'POLISH-Q', 99,
                           [{'severity': 'MAJOR', 'file': 'tracked.txt',
                             'summary': 'foreign blocker', 'failure_scenario': 'reachable'}])
        with self.assertRaisesRegex(RuntimeError, 'clean receipt chain'):
            co.fake_candidate_approval()
        co.state['finding_ledger'] = prior_ledger
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True),
                         'STOP_BEFORE_SECURITY')
        self.assertEqual(tuple(row['id'] for row in co.blocking_open_findings()), frozen)
        after = ct.CandidateRevision(replay['output_oid'], tuple(replay['manifest']), 0)
        def owner_review(req):
            return {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'],
                    'observed_tools': ['read final OID diff'], 'findings': [],
                    'dispositions': [{'id': frozen[0], 'disposition': 'fixed',
                                      'evidence': 'verified at final OID'}]}
        malformed = lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'],
                                 'observed_tools': ['read final OID diff'], 'findings': []}
        with self.assertRaisesRegex(ValueError, 'disposition is missing; retry'):
            co.fake_lifecycle_route(None, chain_only=True,
                         security_context={'baseline': ct.baseline_from_binding(replay['baseline']),
                                           'revision': after, 'review': malformed})
        self.assertEqual(co.state['lifecycle']['awaiting_owner_reverify']['check_failures'], 1)
        self.assertIsNone(co.state['lifecycle']['pending'])
        self.assertEqual(co.fake_lifecycle_route(None, chain_only=True,
                         security_context={'baseline': ct.baseline_from_binding(replay['baseline']),
                                           'revision': after, 'review': owner_review}),
                         'STOP_BEFORE_DELIVERY')
        self.assertEqual(co.state['finding_ledger'][0]['status'], 'fixed')

    def test_fake_reserved_docs_replay_needs_marker_evidence_and_current_oid_test(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        subprocess.run(['git', 'add', 'docs/guide.md'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        self.add_fake_coordinator_docs_blocker(co, approved, finish)
        repair = {'proposals': (), 'paths': ('docs/guide.md',),
                  'allowed_paths': ('docs/guide.md',), 'reserved_docs': ('docs/guide.md',),
                  'write': lambda root, paths: self.fail('SECURITY writer touched reserved docs')}
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co.fake_lifecycle_route(approved, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': finish['revision'], 'repair': repair})
        marker = co.state['lifecycle']['reserved_docs_constraint']
        self.assertEqual(marker['receipt_count'], len(co.state['lifecycle']['receipts']))
        owner_ids = marker['owner_ids']
        marker['owner_ids'] = ()
        with self.assertRaisesRegex(ValueError, 'reserved DOCS replay lacks coordinator-owned source'):
            co.fake_reserved_docs_begin_replay()
        marker['owner_ids'] = owner_ids
        prior_ledger = json.loads(json.dumps(co.state['finding_ledger']))
        prior_next_id = co.state['next_finding_id']
        co.record_findings('coordinator', 'SECURITY', 2, [{
            'severity': 'MAJOR', 'file': 'docs/guide.md',
            'summary': 'unexpected additional source', 'failure_scenario': 'owner set changed'}])
        with self.assertRaisesRegex(ValueError, 'reserved DOCS replay lacks coordinator-owned source'):
            co.fake_reserved_docs_begin_replay()
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co._verify_reserved_docs_replay(finish['baseline'], finish['revision'], {})
        self.assertEqual(co.state['lifecycle']['reserved_docs_constraint'], marker)
        self.assertEqual(co.state['lifecycle']['reserved_docs_constraint']['owner_ids'], marker['owner_ids'])
        co.state['finding_ledger'] = prior_ledger
        co.state['next_finding_id'] = prior_next_id
        co.write_ledger()
        evidence = co.evidence / (marker['id'] + '-reserved-docs.json')
        evidence.write_text('{}')
        co.state['lifecycle']['epoch'] += 1
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co._verify_reserved_docs_replay(finish['baseline'], finish['revision'], {})
        evidence.write_text(json.dumps(marker))
        co.state['lifecycle']['receipts'].append({
            'stage': 'DOCS', 'candidate_oid': marker['source_oid'],
            'output_oid': marker['source_oid'], 'status': 'READY',
            'docs_file': marker['docs_file'], 'retested_oid': marker['source_oid']})
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co._verify_reserved_docs_replay(finish['baseline'], finish['revision'], {})

    def test_fake_reserved_docs_proof_accepts_changed_blob_and_rejects_sensitive_preflight(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        subprocess.run(['git', 'add', 'docs/guide.md'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        self.add_fake_coordinator_docs_blocker(co, approved, finish)
        repair = {'proposals': (), 'paths': ('docs/guide.md',),
                  'allowed_paths': ('docs/guide.md',), 'reserved_docs': ('docs/guide.md',),
                  'write': lambda root, paths: self.fail('SECURITY writer touched reserved docs')}
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co.fake_lifecycle_route(approved, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': finish['revision'], 'repair': repair})
        marker = co.state['lifecycle']['reserved_docs_constraint']
        (finish['baseline'].root / 'docs/guide.md').write_text('# Repaired guide\n')
        after = ct.ingest_candidate_revision(finish['baseline'])
        life = co.state['lifecycle']
        life.update(epoch=1, candidate_oid=after.tree_oid)
        life['receipts'].extend([
            {'stage': 'DOCS', 'candidate_oid': marker['source_oid'], 'output_oid': after.tree_oid,
             'status': 'READY', 'docs_file': marker['docs_file'], 'retested_oid': after.tree_oid,
             'request_id': 'docs-replay'},
            {'stage': 'EXEC', 'candidate_oid': after.tree_oid, 'output_oid': after.tree_oid,
             'status': 'APPROVE', 'approval_proof': {'fake_only': True, 'convergence_id': 'chain-1'},
             'request_id': 'exec-replay'}])
        test = {'id': 'current-test', 'oid': after.tree_oid, 'epoch': 1, 'returncode': 0}
        co.state['fake_candidate_test'] = test
        co.state['fake_candidate_chain'] = {'id': 'chain-1', 'oid': after.tree_oid, 'test_id': test['id']}
        (co.evidence / (test['id'] + '-oid-test.json')).write_text(json.dumps(test))
        co._verify_reserved_docs_replay(finish['baseline'], after, {})
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co._verify_reserved_docs_replay(finish['baseline'], after, {'secret.key': 'secret'})
        life['receipts'][marker['receipt_count']]['retested_oid'] = marker['source_oid']
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co._verify_reserved_docs_replay(finish['baseline'], after, {})
        life['receipts'][marker['receipt_count']]['retested_oid'] = after.tree_oid
        co.state['fake_candidate_chain']['id'] = 'stale-chain'
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co._verify_reserved_docs_replay(finish['baseline'], after, {})
        co.state['fake_candidate_chain']['id'] = 'chain-1'
        co.state['fake_candidate_test']['epoch'] = 0
        with self.assertRaisesRegex(ValueError, 'replayed DOCS receipt and retest'):
            co._verify_reserved_docs_replay(finish['baseline'], after, {})
        co.state['fake_candidate_test']['epoch'] = 1
        (finish['baseline'].root / 'tracked.txt').write_text('later FINISH code\n')
        later = ct.ingest_candidate_revision(finish['baseline'])
        life['receipts'].extend([
            {'stage': 'FINISH', 'candidate_oid': after.tree_oid, 'output_oid': later.tree_oid,
             'status': 'READY', 'request_id': 'finish-later'},
            {'stage': 'EXEC', 'candidate_oid': later.tree_oid, 'output_oid': later.tree_oid,
             'status': 'APPROVE', 'approval_proof': {'fake_only': True, 'convergence_id': 'chain-2'},
             'request_id': 'exec-later'}])
        life['epoch'], life['candidate_oid'] = 2, later.tree_oid
        newer = {'id': 'later-test', 'oid': later.tree_oid, 'epoch': 2, 'returncode': 0}
        co.state['fake_candidate_test'] = newer
        co.state['fake_candidate_chain'] = {'id': 'chain-2', 'oid': later.tree_oid, 'test_id': newer['id']}
        (co.evidence / (newer['id'] + '-oid-test.json')).write_text(json.dumps(newer))
        co._verify_reserved_docs_replay(finish['baseline'], later, {})

    def test_fake_reserved_docs_rejects_sensitive_source(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        (self.workspace / 'secret.key').write_text('fake secret\n')
        subprocess.run(['git', 'add', 'docs/guide.md', 'secret.key'],
                       cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide and secret'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('secret.key',))
        repair = {'proposals': (), 'paths': ('docs/guide.md',),
                  'allowed_paths': ('docs/guide.md',), 'reserved_docs': ('docs/guide.md',),
                  'write': lambda root, paths: self.fail('SECURITY writer touched reserved docs')}
        with self.assertRaisesRegex(ValueError, 'one frozen docs_file'):
            co.fake_lifecycle_route(approved, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': finish['revision'], 'repair': repair})
        self.assertNotIn('reserved_docs_constraint', co.state['lifecycle'])

    def test_fake_reserved_docs_rejects_reviewer_owned_source_until_owner_route(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        subprocess.run(['git', 'add', 'docs/guide.md'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        co.state['lifecycle']['awaiting_owner_reverify'] = {
            'owner': 'security-reviewer', 'finding_ids': ('F001',),
            'paths': ('docs/guide.md',),
            'role_manifest_sha256': co.state['role_dispatch_manifest_sha256']}
        repair = {'proposals': (), 'paths': ('docs/guide.md',),
                  'allowed_paths': ('docs/guide.md',), 'reserved_docs': ('docs/guide.md',),
                  'write': lambda root, paths: self.fail('SECURITY writer touched reserved docs')}
        with self.assertRaisesRegex(ValueError, 'one frozen docs_file'):
            co.fake_lifecycle_route(approved, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': finish['revision'], 'repair': repair})
        self.assertNotIn('reserved_docs_constraint', co.state['lifecycle'])

    def test_fake_security_unneeded_repair_never_dispatches_writer(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        subprocess.run(['git', 'add', 'docs/guide.md'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        repair = {'proposals': (), 'paths': ('tracked.txt',),
                  'allowed_paths': ('tracked.txt',), 'reserved_docs': (),
                  'write': lambda root, paths: self.fail('unneeded SECURITY writer ran')}
        with self.assertRaisesRegex(ValueError, 'no frozen coordinator-owned blocker'):
            co.fake_lifecycle_route(approved, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': finish['revision'], 'repair': repair})
        reserved = {**repair, 'paths': ('docs/guide.md',),
                    'allowed_paths': ('docs/guide.md',), 'reserved_docs': ('docs/guide.md',)}
        with self.assertRaisesRegex(ValueError, 'one frozen docs_file'):
            co.fake_lifecycle_route(approved, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': finish['revision'], 'repair': reserved})
        self.assertNotIn('reserved_docs_constraint', co.state['lifecycle'])

    def test_fake_coordinator_docs_blocker_does_not_dispatch_polish(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        co.state['lifecycle']['stage'] = 'POLISH-Q'
        self.add_fake_coordinator_docs_blocker(co)
        calls = []
        with self.assertRaisesRegex(ValueError, 'POLISH-Q has an open blocker'):
            co.fake_lifecycle_route(approved, polish_context={'python-reviewer': lambda req: calls.append(req)})
        self.assertEqual(calls, [])

    def test_fake_coordinator_docs_blocker_does_not_dispatch_other_security_repair(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        (self.workspace / 'secret.key').write_text('fake secret\n')
        subprocess.run(['git', 'add', 'docs/guide.md', 'secret.key'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add security fixtures'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('secret.key',))
        self.add_fake_coordinator_docs_blocker(co, approved, finish)
        repair = {'proposals': (), 'paths': ('secret.key',), 'allowed_paths': ('secret.key',),
                  'reserved_docs': (),
                  'write': lambda root, paths: self.fail('unrelated SECURITY writer ran')}
        with self.assertRaisesRegex(ValueError, 'SECURITY has an upstream blocker'):
            co.fake_lifecycle_route(approved, security_context={
                'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair})
        self.assertNotIn('reserved_docs_constraint', co.state['lifecycle'])
        self.assertEqual((finish['baseline'].root / 'secret.key').read_text(), 'fake secret\n')

    def test_fake_reserved_docs_owner_check_has_bounded_retry(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        co.fake_lifecycle_route(approved, finish_context=finish, polish_stub=True, docs_stub=True)
        revision, life = finish['revision'], co.state['lifecycle']
        finding = co.record_findings('security-reviewer', 'SECURITY', 1, [{
            'severity': 'CRITICAL', 'file': 'docs/guide.md',
            'summary': 'reserved docs source', 'failure_scenario': 'unsafe doc'}])[0]
        marker = {'owner': 'security-reviewer', 'source_oid': revision.tree_oid,
                  'repair_oid': revision.tree_oid, 'request_id': 'fake-SECURITY-0-repair-source',
                  'repair_digest': 'source-repair', 'finding_ids': (finding['id'],),
                  'paths': ('docs/guide.md',),
                  'role_manifest_sha256': co.state['role_dispatch_manifest_sha256']}
        life['awaiting_owner_reverify'] = marker
        life['receipts'].append({**{key: life[key] for key in
                                   ('item_uuid', 'stage', 'epoch', 'candidate_oid', 'parent')},
                                 'stage': 'SECURITY', 'candidate_oid': 'old-source-oid',
                                 'request_id': marker['request_id'],
                                 'output_oid': revision.tree_oid, 'security_repair': 'source-repair'})
        calls = []
        def malformed(request):
            calls.append(request)
            return {'status': 'REVISE', 'candidate_oid': request['candidate_oid'],
                    'observed_tools': ['read current OID'], 'findings': [],
                    'dispositions': [{'id': finding['id'], 'disposition': 'fixed',
                                      'evidence': 'not approved'}]}
        context = {'baseline': finish['baseline'], 'revision': revision, 'review': malformed}
        for expected in ('disposition is missing; retry', 'disposition is missing; retry limit reached; abort'):
            with self.assertRaisesRegex(ValueError, expected):
                co.fake_lifecycle_route(approved, security_context=context)
            self.assertIsNone(co.state['lifecycle']['pending'])
        with self.assertRaisesRegex(ValueError, 'owner check retry limit reached; abort'):
            co.fake_lifecycle_route(approved, security_context=context)
        self.assertEqual(len(calls), 2)
        self.assertEqual(co.state['finding_ledger'][0]['status'], 'open')

    def test_fake_security_caller_cannot_unreserve_frozen_docs_file(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Existing guide\n')
        (self.workspace / 'secret.key').write_text('fake secret\n')
        subprocess.run(['git', 'add', 'docs/guide.md', 'secret.key'],
                       cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add guide and fake secret'],
                       cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        repair = {'proposals': (), 'paths': ('secret.key', 'docs/guide.md'),
                  'allowed_paths': ('secret.key', 'docs/guide.md'), 'reserved_docs': (),
                  'write': lambda root, paths: self.fail('SECURITY writer edited frozen docs')}
        with self.assertRaisesRegex(ValueError, 'one frozen docs_file'):
            co.fake_lifecycle_route(approved, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': finish['revision'], 'repair': repair})
        self.assertNotIn('reserved_docs_constraint', co.state['lifecycle'])

    def test_fake_security_caller_cannot_unreserve_frozen_docs_allowlist(self):
        (self.workspace / 'docs').mkdir()
        (self.workspace / 'docs/guide.md').write_text('# Guide\n')
        (self.workspace / 'docs/extra.md').write_text('# Extra\n')
        (self.workspace / 'secret.key').write_text('fake secret\n')
        subprocess.run(['git', 'add', 'docs/guide.md', 'docs/extra.md', 'secret.key'],
                       cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add docs and fake secret'],
                       cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, docs_allowlist=('docs/extra.md',))
        repair = {'proposals': (), 'paths': ('secret.key', 'docs/extra.md'),
                  'allowed_paths': ('secret.key', 'docs/extra.md'), 'reserved_docs': (),
                  'write': lambda root, paths: self.fail('SECURITY writer edited frozen allowlist docs')}
        with self.assertRaisesRegex(ValueError, 'one frozen docs_file'):
            co.fake_lifecycle_route(approved, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': finish['revision'], 'repair': repair})
        self.assertNotIn('SECURITY', [row['stage'] for row in co.state['lifecycle']['receipts']])

    def test_fake_security_repair_must_cover_every_sensitive_path(self):
        (self.workspace / 'secret.key').write_text('fake secret\n')
        subprocess.run(['git', 'add', 'secret.key'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'fake secret'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        repair = {'proposals': (), 'paths': ('tracked.txt',),
                  'allowed_paths': ('tracked.txt',), 'reserved_docs': (),
                  'write': lambda root, paths: self.fail('incomplete SECURITY writer ran')}
        with self.assertRaisesRegex(ValueError, 'does not cover sensitive paths'):
            co.fake_lifecycle_route(approved, stub_mode=True,
                                    security_context={'baseline': finish['baseline'],
                                                      'revision': finish['revision'], 'repair': repair})

    def test_fake_security_negated_ignore_rule_requires_operator_consent(self):
        proposals = (('Environment & config', '!.env.example', ()),)
        with self.assertRaisesRegex(ValueError, 'operator confirmation required'):
            rc.security_repair_policy.plan_security_repair(
                self.run_dir.name, 'a' * 40, proposals, (), ('.gitignore',), (), ('.gitignore',))

    def test_fake_security_ignore_repair_rejects_extra_rule(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('.gitignore',))
        repair = {'proposals': (('Keys & certificates', '*.pem', ()),), 'paths': (),
                  'allowed_paths': ('.gitignore',), 'reserved_docs': (),
                  'write': lambda root, paths: (root / '.gitignore').write_text('*.pem\n*secret*\n')}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair}
        with self.assertRaisesRegex(ValueError, 'beyond approved patterns'):
            specialist = lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'], 'findings': []}
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': specialist}, docs_stub=True,
                                    security_context=context)
        self.assertNotIn('SECURITY', [row['stage'] for row in co.state['lifecycle']['receipts']])

    def test_fake_security_ignore_repair_rejects_deleted_old_rule(self):
        (self.workspace / '.gitignore').write_text('original-rule\n')
        subprocess.run(['git', 'add', '.gitignore'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'original ignore'], cwd=self.workspace, check=True)
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('.gitignore',))
        repair = {'proposals': (('Keys & certificates', '*.pem', ()),), 'paths': (),
                  'allowed_paths': ('.gitignore',), 'reserved_docs': (),
                  'write': lambda root, paths: (root / '.gitignore').write_text('*.pem\n')}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair}
        with self.assertRaisesRegex(ValueError, 'beyond approved patterns'):
            specialist = lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'], 'findings': []}
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': specialist}, docs_stub=True,
                                    security_context=context)
        self.assertNotIn('SECURITY', [row['stage'] for row in co.state['lifecycle']['receipts']])

    def test_fake_security_repair_rejects_no_change_oid(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('.gitignore',))
        def write(root, paths):
            with (root / '.gitignore').open('a') as target:
                target.write('*.pem\n')
        repair = {'proposals': (('Keys & certificates', '*.pem', ()),), 'paths': (),
                  'allowed_paths': ('.gitignore',), 'reserved_docs': (),
                  'write': write}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair}
        specialist = lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'], 'findings': []}
        with patch.object(rc.candidate_tree, 'ingest_candidate_revision', return_value=finish['revision']):
            with self.assertRaisesRegex(ValueError, 'made no change'):
                co.fake_lifecycle_route(approved, finish_context=finish,
                                        polish_context={'python-reviewer': specialist}, docs_stub=True,
                                        security_context=context)
        self.assertNotIn('SECURITY', [row['stage'] for row in co.state['lifecycle']['receipts']])

    def test_fake_security_repair_rejects_late_candidate_mutation(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('.gitignore',))
        def write(root, paths):
            with (root / '.gitignore').open('a') as target:
                target.write('*.pem\n')
        repair = {'proposals': (('Keys & certificates', '*.pem', ()),), 'paths': (),
                  'allowed_paths': ('.gitignore',), 'reserved_docs': (),
                  'write': write}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair}
        specialist = lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'], 'findings': []}
        ingest = rc.candidate_tree.ingest_candidate_revision
        def mutate(baseline):
            result = ingest(baseline)
            if co.state['lifecycle']['stage'] == 'SECURITY':
                with (baseline.root / '.gitignore').open('a') as target:
                    target.write('*secret*\n')
            return result
        with patch.object(rc.candidate_tree, 'ingest_candidate_revision', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'changed during ingest'):
                co.fake_lifecycle_route(approved, finish_context=finish,
                                        polish_context={'python-reviewer': specialist}, docs_stub=True,
                                        security_context=context)
        self.assertNotIn('SECURITY', [row['stage'] for row in co.state['lifecycle']['receipts']])

    def test_fake_security_repair_rejects_ungranted_candidate_write(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(
            docs=True, security_paths=('.gitignore',))
        def specialist(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        repair = {'proposals': (('Keys & certificates', '*.pem', ()),), 'paths': (),
                  'allowed_paths': ('.gitignore',), 'reserved_docs': ('docs/guide.md',),
                  'write': lambda root, paths: (root / 'tracked.txt').write_text('ungranted\n')}
        context = {'baseline': finish['baseline'], 'revision': finish['revision'], 'repair': repair}
        with self.assertRaisesRegex(ValueError, 'ungranted candidate path'):
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': specialist}, docs_stub=True,
                                    security_context=context)
        self.assertEqual(co.state['lifecycle']['stage'], 'SECURITY')
        self.assertNotIn('SECURITY', [row['stage'] for row in co.state['lifecycle']['receipts']])

    def test_fake_lifecycle_docs_write_without_retest_cannot_pass(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists(docs=True)
        def specialist(request):
            return {'status': 'APPROVE', 'candidate_oid': request['candidate_oid'], 'findings': []}
        def write_docs(root, path):
            target = root / path; target.parent.mkdir(exist_ok=True)
            target.write_text('# Untested\n')
        docs = {'baseline': finish['baseline'], 'before': finish['revision'],
                'write': write_docs, 'test': lambda oid: None}
        with self.assertRaisesRegex(ValueError, 'current-OID retest'):
            co.fake_lifecycle_route(approved, finish_context=finish,
                                    polish_context={'python-reviewer': specialist}, docs_context=docs)
        self.assertEqual(co.state['lifecycle']['stage'], 'DOCS')
        self.assertIsNotNone(co.state['lifecycle']['pending'])
        self.assertNotIn('DOCS', [row['stage'] for row in co.state['lifecycle']['receipts']])

    def test_lifecycle_project_config_enablement_is_refused(self):
        config_dir = self.workspace / '.review-loop'; config_dir.mkdir()
        (config_dir / 'paired-session.json').write_text(json.dumps({'lifecycle_mode': 'on'}))
        with patch('sys.stdout', new=io.StringIO()) as output:
            result = rc.main(self.command()[2:])
        self.assertEqual(result, 2)
        self.assertIn('lifecycle remains disabled', output.getvalue())
        self.assertFalse((self.run_dir / 'state.json').exists())

    def test_fake_lifecycle_shared_drive_stops_after_real_fake_cli_reviews(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        self.assertIn('router binding is pending', co.state['hold_reason'])
        self.assertEqual(co.state['phase'], 'EXEC')
        self.assertTrue(co.state['gate_ran'])
        self.assertEqual(co.state['lifecycle']['stage'], 'EXEC')
        self.assertEqual(co.state['lifecycle']['receipts'], [])
        self.assertIn(('author', 'PLAN'), [(row['role'], row['phase']) for row in co.state['turns']])
        self.assertIn(('reviewer', 'EXEC'), [(row['role'], row['phase']) for row in co.state['turns']])
        self.assertIn(('gate', 'EXEC'), [(row['role'], row['phase']) for row in co.state['turns']])
        source = co.state['fake_exec_source']
        self.assertEqual(source['run_id'], self.run_dir.name)
        self.assertEqual(source['workspace_snapshot'], rc.git_snapshot(self.workspace)[0])
        self.assertLess(source['author_sequence'], source['reviewer_sequence'])
        self.assertLess(source['reviewer_sequence'], source['gate_sequence'])

    def test_fake_candidate_author_ingest_uses_clean_root_and_fresh_exec_session(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        self.assertEqual(co.state['phase'], 'EXEC')
        self.assertEqual(co.state['next'], 'author')
        plan_session = co.state['sessions']['author']
        live_before = rc.git_snapshot(self.workspace)[0]
        receipt = co.fake_candidate_author_turn()
        author = co.state['turns'][-1]
        self.assertEqual(author['phase'], 'EXEC')
        self.assertEqual(author['answer']['status'], 'READY')
        self.assertNotEqual(co.state['sessions']['author'], plan_session)
        self.assertNotIn('resume', author['command'])
        self.assertEqual(Path(author['workspace']), Path(receipt['root']))
        self.assertTrue((Path(receipt['root']) / 'sum_ints.py').is_file())
        self.assertFalse((self.workspace / 'sum_ints.py').exists())
        self.assertEqual(rc.git_snapshot(self.workspace)[0], live_before)
        self.assertEqual(co.state['fake_ingest_receipt']['id'], receipt['id'])
        self.assertTrue((co.evidence / (receipt['id'] + '-ingest.json')).is_file())
        root_stat = Path(receipt['root']).stat()
        self.assertEqual(receipt['baseline']['root_identity'], (root_stat.st_dev, root_stat.st_ino))
        saved = json.loads((co.evidence / (receipt['id'] + '-ingest.json')).read_text())
        baseline_data = dict(saved['baseline'])
        for key in ('workspace', 'run_dir', 'root', 'git_dir', 'index'):
            baseline_data[key] = Path(baseline_data[key])
        for key in ('root_identity', 'authorized_prefixes', 'parent_entries'):
            baseline_data[key] = tuple(baseline_data[key])
        restored = ct.CandidateBaseline(**baseline_data)
        revision = ct.CandidateRevision(saved['output_oid'], tuple(saved['manifest']), 1)
        ct.verify_candidate_revision(restored, revision)
        self.assertNotIn(str(self.workspace), (co.evidence /
            f"{author['sequence']:03d}-exec-author.prompt.txt").read_text())

    def test_fake_candidate_author_rejects_unapproved_write_and_keeps_pending(self):
        with patch.dict(os.environ, {'FAKE_DOC_DELTA': '1', 'TEST_SECRET_NEVER_PERSIST': 's3cr3t'}):
            args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
            co = rc.Coordinator(args, _fake_lifecycle=True)
            self.assertEqual(co.fake_drive(), 'HOLD')
            with self.assertRaisesRegex(ValueError, 'authorized|candidate'):
                co.fake_candidate_author_turn()
        self.assertIsNotNone(co.state['fake_candidate_pending'])
        self.assertNotIn('fake_ingest_receipt', co.state)
        self.assertNotIn('s3cr3t', (self.run_dir / 'state.json').read_text())

    def test_fake_candidate_author_refuses_uncertain_or_unrelated_hold(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.state['active'] = {'sequence': 999}
        with self.assertRaisesRegex(RuntimeError, 'unstarted EXEC turn'):
            co.fake_candidate_author_turn()
        co.state['active'] = None
        co.state['hold_reason'] = 'permission probe failed'
        with self.assertRaisesRegex(RuntimeError, 'unstarted EXEC turn'):
            co.fake_candidate_author_turn()
        self.assertFalse(any(row['phase'] == 'EXEC' and row['role'] == 'author'
                             for row in co.state['turns']))

    def test_fake_candidate_author_rejects_live_workspace_change_before_ingest(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = ct.ingest_candidate_revision

        def change_live(baseline):
            (self.workspace / 'tracked.txt').write_text('user change\n')
            return ingest(baseline)

        with patch.object(rc.candidate_tree, 'ingest_candidate_revision', side_effect=change_live):
            with self.assertRaisesRegex(ValueError, 'live|worktree|workspace'):
                co.fake_candidate_author_turn()
        self.assertIsNotNone(co.state['fake_candidate_pending'])
        self.assertNotIn('fake_ingest_receipt', co.state)

    def test_fake_candidate_oid_test_binds_rebuilt_tree_and_ingest_id(self):
        (self.workspace / 'test_sum_ints.py').write_text(
            'import unittest\nfrom sum_ints import sum_ints\n'
            'class TestSum(unittest.TestCase):\n'
            '    def test_sum(self):\n'
            '        self.assertEqual(sum_ints([1, 2]), 3)\n')
        subprocess.run(['git', 'add', 'test_sum_ints.py'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'add oid test'], cwd=self.workspace, check=True)
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn()
        test = co.fake_candidate_oid_test()
        self.assertEqual(test['ingest_id'], ingest['id'])
        self.assertEqual(test['oid'], ingest['output_oid'])
        self.assertTrue(Path(test['command'][0]).is_absolute())
        self.assertEqual(test['command'][1:], ['-m', 'unittest'])
        self.assertEqual(test['returncode'], 0)
        self.assertEqual(co.state['fake_candidate_test']['id'], test['id'])
        self.assertTrue((co.evidence / (test['id'] + '-oid-test.json')).is_file())
        with self.assertRaisesRegex(RuntimeError, 'completed ingest'):
            co.fake_candidate_oid_test()

    def test_fake_candidate_oid_test_failure_and_pending_cannot_rerun(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn()
        co.state['fake_candidate_test_pending'] = 'interrupted-test'
        with self.assertRaisesRegex(RuntimeError, 'completed ingest'):
            co.fake_candidate_oid_test()
        co.state.pop('fake_candidate_test_pending')
        co.args.test_command = 'python3 -c "raise SystemExit(1)"'
        with self.assertRaisesRegex(RuntimeError, 'OID-bound coordinator test failed'):
            co.fake_candidate_oid_test()
        self.assertNotIn('fake_candidate_test_pending', co.state)
        self.assertIn('fake_candidate_test_failed', co.state)
        with self.assertRaisesRegex(RuntimeError, 'completed ingest'):
            co.fake_candidate_oid_test()

    def test_fake_candidate_oid_test_rejects_changed_candidate_bytes(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn()
        (Path(ingest['root']) / 'sum_ints.py').write_text('changed after ingest\n')
        with self.assertRaisesRegex(ValueError, 'candidate|reviewed OID'):
            co.fake_candidate_oid_test()
        self.assertNotIn('fake_candidate_test', co.state)

    def test_fake_candidate_oid_test_rejects_mismatched_ingest_evidence(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn()
        saved = co.evidence / (ingest['id'] + '-ingest.json')
        body = json.loads(saved.read_text())
        body['output_oid'] = '0' * len(ingest['output_oid'])
        saved.write_text(json.dumps(body))
        with self.assertRaisesRegex(RuntimeError, 'evidence differs'):
            co.fake_candidate_oid_test()
        self.assertNotIn('fake_candidate_test', co.state)

    def test_fake_candidate_oid_test_does_not_import_from_inherited_pythonpath(self):
        outside = self.root / 'outside'; outside.mkdir()
        (outside / 'outside_only.py').write_text('VALUE = 1\n')
        (self.workspace / 'test_external.py').write_text(
            'import unittest\nimport outside_only\n'
            'class TestExternal(unittest.TestCase):\n'
            '    def test_value(self):\n        self.assertEqual(outside_only.VALUE, 1)\n')
        subprocess.run(['git', 'add', 'test_external.py'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'external import test'], cwd=self.workspace, check=True)
        with patch.dict(os.environ, {'PYTHONPATH': str(outside)}):
            args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
            co = rc.Coordinator(args, _fake_lifecycle=True)
            self.assertEqual(co.fake_drive(), 'HOLD')
            co.fake_candidate_author_turn()
            with self.assertRaisesRegex(RuntimeError, 'OID-bound coordinator test failed'):
                co.fake_candidate_oid_test()
        self.assertNotIn('fake_candidate_test', co.state)

    def test_fake_candidate_oid_review_chains_ingest_test_reviewer_gate(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn()
        tested = co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        chain = co.state['fake_candidate_chain']
        reviewed = co.state['fake_candidate_review']
        self.assertEqual(chain['ingest_id'], ingest['id'])
        self.assertEqual(chain['test_id'], tested['id'])
        self.assertEqual(chain['review_id'], reviewed['id'])
        self.assertEqual(chain['oid'], ingest['output_oid'])
        approval = co.fake_candidate_approval()
        self.assertEqual(approval['candidate_oid'], ingest['output_oid'])
        self.assertEqual(approval['proof']['convergence_id'], chain['id'])
        self.assertEqual(approval['proof']['reviewer']['status'], 'APPROVE')
        self.assertEqual(approval['proof']['gate']['verdict'], 'approve')
        self.assertLess(reviewed['sequence'], chain['gate_sequence'])
        self.assertEqual(co.state['fake_candidate_chain'], chain)
        for role in ('reviewer', 'gate'):
            row = next(row for row in co.state['turns'] if row['phase'] == 'EXEC' and
                       row['role'] == role and row['sequence'] >= reviewed['sequence'])
            prompt = (co.evidence / f"{row['sequence']:03d}-exec-{role}.prompt.txt").read_text()
            self.assertIn(ingest['output_oid'], prompt)
            self.assertIn(str(tested['root']), prompt)
            self.assertNotIn(str(self.workspace), prompt)
            self.assertEqual(row['workspace'], tested['root'])

    def test_fake_candidate_oid_review_rejects_stale_test_or_changed_checkout(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn()
        tested = co.fake_candidate_oid_test()
        co.state['lifecycle']['epoch'] += 1
        with self.assertRaisesRegex(RuntimeError, 'bind current ingest'):
            co.fake_candidate_oid_review()
        co.state['lifecycle']['epoch'] -= 1
        (Path(tested['root']) / 'sum_ints.py').write_text('changed\n')
        with self.assertRaisesRegex(ValueError, 'candidate|reviewed OID'):
            co.fake_candidate_oid_review()
        self.assertNotIn('fake_candidate_review', co.state)

    def test_fake_candidate_oid_review_uses_coordinator_test_not_model_claim(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn()
        co.fake_candidate_oid_test()
        with patch.dict(os.environ, {'FAKE_REVIEW_NO_TEST_EVENT': '1'}):
            co.fake_candidate_oid_review()
        self.assertIn('fake_candidate_chain', co.state)

    def test_fake_candidate_oid_review_blocks_gate_finding(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn()
        co.fake_candidate_oid_test()
        with patch.dict(os.environ, {'FAKE_GATE_BLOCK': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'OID gate did not approve'):
                co.fake_candidate_oid_review()
        self.assertIn('fake_candidate_review', co.state)
        self.assertNotIn('fake_candidate_chain', co.state)
        rejected = co.state['fake_candidate_review_rejected']
        self.assertEqual(tuple(rejected[:2]),
                         (co.state['fake_candidate_test']['oid'], co.state['fake_candidate_test']['id']))
        resumed = rc.Coordinator(args, _fake_lifecycle=True)
        with self.assertRaisesRegex(RuntimeError, 'current successful test'):
            resumed.fake_candidate_oid_review()

    def test_fake_candidate_oid_review_revise_persists_same_oid_rejection(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn()
        tested = co.fake_candidate_oid_test()
        with patch.dict(os.environ, {'FAKE_EXEC_MIXED_REVISE': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'OID reviewer did not approve'):
                co.fake_candidate_oid_review()
        rejected = co.state['fake_candidate_review_rejected']
        self.assertEqual(tuple(rejected[:2]), (tested['oid'], tested['id']))
        self.assertNotIn('fake_candidate_chain', co.state)
        review_count = sum(row['role'] == 'reviewer' and row['phase'] == 'EXEC'
                           for row in co.state['turns'])
        resumed = rc.Coordinator(args, _fake_lifecycle=True)
        with self.assertRaisesRegex(RuntimeError, 'current successful test'):
            resumed.fake_candidate_oid_review()
        self.assertEqual(sum(row['role'] == 'reviewer' and row['phase'] == 'EXEC'
                             for row in resumed.state['turns']), review_count)
        self.assertNotIn('fake_candidate_chain', resumed.state)

    def test_fake_candidate_oid_review_protocol_error_cannot_reroll(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn()
        tested = co.fake_candidate_oid_test()
        with patch.dict(os.environ, {'FAKE_EXEC_MIXED_REVISE': '1',
                                     'FAKE_EMPTY_CLAIMS': 'reviewer', 'FAKE_ALWAYS_EMPTY_CLAIMS': '1'}):
            with self.assertRaises(RuntimeError):
                co.fake_candidate_oid_review()
        resumed = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(tuple(resumed.state['fake_candidate_review_rejected']),
                         (tested['oid'], tested['id']))
        count = len(resumed.state['turns'])
        with self.assertRaisesRegex(RuntimeError, 'current successful test'):
            resumed.fake_candidate_oid_review()
        self.assertEqual(len(resumed.state['turns']), count)
        self.assertNotIn('fake_candidate_chain', resumed.state)

    def test_fake_candidate_approval_rejects_changed_evidence_and_identity(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn()
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        chain = co.state['fake_candidate_chain']
        chain['identity'] = ('another-run', co.state['item_uuid'], 0)
        with self.assertRaisesRegex(RuntimeError, 'stale or unrelated'):
            co.fake_candidate_approval()
        chain['identity'] = (self.run_dir.name, co.state['item_uuid'], 0)
        receipt = co.evidence / (chain['id'] + '-oid-chain.json')
        receipt.write_text('{}')
        with self.assertRaisesRegex(RuntimeError, 'evidence differs'):
            co.fake_candidate_approval()

    def test_fake_candidate_approval_rechecks_current_inputs_and_turns(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn()
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        approved = co.fake_candidate_approval()
        self.assertTrue(approved['proof']['fake_only'])
        co.state['pending_reviewer_result_sequence'] = 999
        with self.assertRaisesRegex(RuntimeError, 'clean receipt chain'):
            co.fake_candidate_approval()
        co.state['pending_reviewer_result_sequence'] = None
        plan = co.context / 'plan.md'
        original = plan.read_text()
        plan.write_text(original + '\nChanged after approval\n')
        with self.assertRaisesRegex(RuntimeError, 'inputs changed'):
            co.fake_candidate_approval()
        plan.write_text(original)
        co.state['turns'].append({'sequence': co.state['sequence'] + 1, 'role': 'author'})
        with self.assertRaisesRegex(RuntimeError, 'later author'):
            co.fake_candidate_approval()
        co.state['turns'].pop()
        reviewed = co.state['fake_candidate_review']
        (co.evidence / (reviewed['id'] + '-oid-review.json')).unlink()
        with self.assertRaisesRegex(RuntimeError, 'missing or malformed'):
            co.fake_candidate_approval()

    def test_fake_candidate_approval_rejects_new_head_after_plan_stop(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        frozen = co.state['lifecycle']['parent']
        subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'new head after plan'],
                       cwd=self.workspace, check=True)
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.workspace, text=True).strip()
        self.assertNotEqual(head, frozen)
        co.fake_candidate_author_turn()
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        with self.assertRaisesRegex(RuntimeError, 'frozen parent'):
            co.fake_candidate_approval()

    def test_fake_chain_only_router_consumes_persisted_approval_once(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn(chain_only=True)
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        forged = co.fake_candidate_approval()
        with self.assertRaisesRegex(ValueError, 'caller output OIDs'):
            co.fake_lifecycle_route(None, outputs={'EXEC': 'f' * 40},
                                    stub_mode=True, chain_only=True)
        with self.assertRaisesRegex(ValueError, 'persisted chain'):
            co.fake_lifecycle_route(forged, stub_mode=True)
        forged['proof'].pop('fake_only')
        with self.assertRaisesRegex(ValueError, 'persisted chain'):
            co.fake_lifecycle_route(forged, stub_mode=True)
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True),
                         'STOP_BEFORE_SECURITY')
        self.assertEqual(co.state['fake_route_consumed'], co.state['fake_candidate_chain']['id'])
        with self.assertRaisesRegex(ValueError, 'unused persisted approval'):
            co.fake_lifecycle_route(None, stub_mode=True, chain_only=True)

    def test_fake_chain_only_router_rejects_unrelated_receipt_and_terminal_run(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn(chain_only=True)
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        co.state['lifecycle']['item_uuid'] = 'other-item'
        with self.assertRaisesRegex(RuntimeError, 'lifecycle'):
            co.fake_lifecycle_route(None, chain_only=True)
        co.state['lifecycle']['item_uuid'] = co.state['item_uuid']
        co.state['status'] = 'ABORTED'
        with self.assertRaisesRegex(RuntimeError, 'lifecycle'):
            co.fake_lifecycle_route(None, chain_only=True)
        self.assertNotIn('fake_route_consumed', co.state)

    def test_fake_chain_only_continues_security_from_consumed_exec_receipt(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn(chain_only=True)
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        self.assertEqual(co.fake_lifecycle_route(None, stub_mode=True, chain_only=True),
                         'STOP_BEFORE_SECURITY')
        baseline = ct.baseline_from_binding(ingest['baseline'])
        revision = ct.CandidateRevision(ingest['output_oid'], tuple(ingest['manifest']), 0)
        security = {'baseline': baseline, 'revision': revision,
                    'review': lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'],
                                           'observed_tools': ['read OID diff'], 'findings': []}}
        self.assertEqual(co.fake_lifecycle_route(None, chain_only=True, security_context=security),
                         'STOP_BEFORE_DELIVERY')
        self.assertEqual(co.state['lifecycle']['receipts'][0]['approval_proof']['convergence_id'],
                         co.state['fake_route_consumed'])
        self.assertIsNotNone(co.state['fake_route_consumed'])

    def test_fake_chain_only_continuation_rejects_legacy_exec_and_event_injection(self):
        co, approved, finish = self.fake_lifecycle_ready_for_specialists()
        self.assertEqual(co.fake_lifecycle_route(approved, stub_mode=True), 'STOP_BEFORE_SECURITY')
        security = {'baseline': finish['baseline'], 'revision': finish['revision'],
                    'review': lambda req: {'status': 'APPROVE', 'candidate_oid': req['candidate_oid'],
                                           'observed_tools': ['read OID diff'], 'findings': []}}
        with self.assertRaisesRegex(ValueError, 'consumed EXEC proof'):
            co.fake_lifecycle_route(None, chain_only=True, security_context=security)

    def test_fake_chain_only_rejects_public_stage_event_injection(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        chained = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(chained.fake_drive(), 'HOLD')
        chained.fake_candidate_author_turn(chain_only=True)
        with self.assertRaisesRegex(ValueError, 'router-owned'):
            chained.fake_lifecycle_event('begin', {'stage': 'EXEC'})

    def test_fake_chain_only_author_checks_frozen_parent_before_dispatch(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        count = len(co.state['turns'])
        subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'new head after plan'],
                       cwd=self.workspace, check=True)
        with self.assertRaisesRegex(RuntimeError, 'frozen parent before author dispatch'):
            co.fake_candidate_author_turn(chain_only=True)
        self.assertEqual(len(co.state['turns']), count)
        self.assertNotIn('fake_candidate_pending', co.state)

    def test_fake_chain_only_rejects_legacy_approval_before_and_after_rejection(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        ingest = co.fake_candidate_author_turn(chain_only=True)
        life = co.state['lifecycle']
        hand_built = {'status': 'APPROVE', 'epoch': life['epoch'], 'item_uuid': life['item_uuid'],
                      'run_id': co.run_dir.name, 'parent': life['parent'],
                      'candidate_oid': ingest['output_oid']}
        with self.assertRaisesRegex(ValueError, 'persisted chain'):
            co.fake_lifecycle_route(hand_built, stub_mode=True)
        co.fake_candidate_oid_test()
        with patch.dict(os.environ, {'FAKE_EXEC_MIXED_REVISE': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'OID reviewer did not approve'):
                co.fake_candidate_oid_review()
        with self.assertRaisesRegex(ValueError, 'persisted chain'):
            co.fake_lifecycle_route(hand_built, stub_mode=True)
        self.assertEqual(co.state['lifecycle']['stage'], 'EXEC')

    def test_fake_chain_only_preserves_candidate_error_diagnostic(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn(chain_only=True)
        co.fake_candidate_oid_test()
        co.fake_candidate_oid_review()
        with patch.object(rc.candidate_tree, 'verify_candidate_revision',
                          side_effect=ct.CandidateError('scratch root changed')):
            with self.assertRaisesRegex(RuntimeError, 'candidate changed: scratch root changed'):
                co.fake_candidate_approval()

    def test_fake_candidate_oid_review_rejects_reviewer_tree_mutation_before_gate(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on', '--stop-after-plan')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        co.fake_candidate_author_turn()
        co.fake_candidate_oid_test()
        with patch.dict(os.environ, {'FAKE_MUTATION': 'checkout'}):
            with self.assertRaisesRegex(ValueError, 'candidate|reviewed OID'):
                co.fake_candidate_oid_review()
        self.assertNotIn('fake_candidate_review', co.state)
        self.assertNotIn('fake_candidate_chain', co.state)

    def test_fake_exec_source_rejects_stale_or_other_run_gate(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        self.assertEqual(co.fake_drive(), 'HOLD')
        gate = next(row for row in reversed(co.state['turns']) if row['role'] == 'gate')
        gate['snapshot_before'] = 'stale'
        with self.assertRaisesRegex(RuntimeError, 'current reviewer and gate'):
            co._freeze_fake_exec_source()
        gate['snapshot_before'] = rc.git_snapshot(self.workspace)[0]
        gate['run_id'] = 'other-run'
        with self.assertRaisesRegex(RuntimeError, 'current reviewer and gate'):
            co._freeze_fake_exec_source()

    def test_fake_exec_source_uses_effective_retry_reviewer(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        with patch.dict(os.environ, {'FAKE_EMPTY_CLAIMS': 'reviewer'}):
            self.assertEqual(co.fake_drive(), 'HOLD')
        rejected = [row for row in co.state['turns'] if row.get('phase') == 'EXEC' and
                    row.get('role') == 'reviewer' and row.get('verified_claims_error')]
        self.assertEqual(len(rejected), 1)
        source = co.state['fake_exec_source']
        self.assertNotEqual(source['reviewer_sequence'], rejected[0]['sequence'])
        self.assertEqual(source['reviewer_sequence'],
                         co.state['exec_comparisons'][-1]['review_sequence'])
        self.assertLess(source['reviewer_sequence'], source['gate_sequence'])

    def test_fake_drive_rejects_lifecycle_receipt_before_turns(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        co.state['lifecycle']['stage'] = 'FINISH'
        with self.assertRaisesRegex(RuntimeError, 'fresh EXEC lifecycle state'):
            co.fake_drive()
        self.assertEqual(co.state['turns'], [])

    def test_fake_lifecycle_refuses_unrecognized_provider_wrapper(self):
        args = rc.parser().parse_args(self.command('--lifecycle-mode', 'on')[2:])
        co = rc.Coordinator(args, _fake_lifecycle=True)
        Path(args.codex_bin).write_text('#!/bin/sh\nexit 0\n')
        with self.assertRaisesRegex(RuntimeError, 'non-fake provider'):
            co.fake_drive()
        self.assertEqual(co.state['turns'], [])

    def test_lifecycle_doc_paths_refuse_escape_before_state(self):
        with self.assertRaisesRegex(ValueError, 'lifecycle doc path escapes workspace'):
            self.coordinator('--docs-allowlist', '../outside.md')
        self.assertFalse((self.run_dir / 'state.json').exists())

    def test_candidate_baseline_isolated_git_index_matches_clean_head(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        index_text = subprocess.check_output(['git', 'rev-parse', '--git-path', 'index'],
                                              cwd=self.workspace, text=True).strip()
        live_index = Path(index_text)
        if not live_index.is_absolute(): live_index = self.workspace / live_index
        before = hashlib.sha256(live_index.read_bytes()).hexdigest()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))
        self.assertEqual(baseline.parent_head, subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=self.workspace, text=True).strip())
        self.assertEqual(baseline.tree_oid, subprocess.check_output(
            ['git', 'rev-parse', 'HEAD^{tree}'], cwd=self.workspace, text=True).strip())
        self.assertEqual((baseline.root / 'tracked.txt').read_bytes(),
                         (self.workspace / 'tracked.txt').read_bytes())
        self.assertFalse((baseline.root / '.git').exists())
        self.assertTrue(baseline.git_dir.is_dir() and baseline.index.is_file())
        self.assertNotEqual(baseline.git_dir.parent, baseline.root.parent)
        self.assertEqual(hashlib.sha256(live_index.read_bytes()).hexdigest(), before)
        self.assertFalse(baseline.separate_filesystems)

    def test_candidate_baseline_refuses_dirty_or_inside_scratch(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        (self.workspace / 'tracked.txt').write_text('user change\n')
        with self.assertRaisesRegex(ct.CandidateError, 'globally clean'):
            ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))
        self.assertEqual(list(scratch.iterdir()), [])
        (self.workspace / 'tracked.txt').write_text('base\n')
        with self.assertRaisesRegex(ct.CandidateError, 'outside workspace'):
            ct.prepare_candidate_baseline(self.workspace, self.run_dir, self.workspace, scratch, ('tracked.txt',))

    def test_candidate_baseline_rejects_symlink_escape_and_inherited_git_env(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        outside_index = self.root / 'outside-index'; outside_index.write_text('do not overwrite')
        with patch.dict(os.environ, {'GIT_INDEX_FILE': str(outside_index),
                                     'GIT_WORK_TREE': str(self.root)}):
            baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir,
                                                     scratch, scratch, ('tracked.txt',))
        self.assertEqual(outside_index.read_text(), 'do not overwrite')
        self.assertEqual(baseline.tree_oid, subprocess.check_output(
            ['git', 'rev-parse', 'HEAD^{tree}'], cwd=self.workspace, text=True).strip())
        bad = scratch / 'bad-tree'; bad.mkdir()
        os.symlink('../outside-index', bad / 'escape')
        with self.assertRaisesRegex(ct.CandidateError, 'symlink escapes'):
            ct._validate_checkout(bad)

    def test_candidate_baseline_rejects_transforming_attrs_and_ignores_user_attrs(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        global_attrs = self.test_home / '.config/git/attributes'
        global_attrs.parent.mkdir(parents=True)
        global_attrs.write_text('*.txt text eol=crlf\n')
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))
        self.assertEqual((baseline.root / 'tracked.txt').read_bytes(), b'base\n')
        (self.workspace / '.gitattributes').write_text('*.txt text eol=crlf\n')
        subprocess.run(['git', 'add', '.gitattributes'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'attributes'], cwd=self.workspace, check=True)
        before = set(scratch.iterdir())
        with self.assertRaisesRegex(ct.CandidateError, 'transforming Git attributes'):
            ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))
        self.assertEqual(set(scratch.iterdir()), before)
        (self.workspace / '.gitattributes').write_text('*.txt crlf=input\n')
        subprocess.run(['git', 'add', '.gitattributes'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'legacy crlf attribute'], cwd=self.workspace, check=True)
        with self.assertRaisesRegex(ct.CandidateError, 'transforming Git attributes'):
            ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))

    def test_candidate_baseline_rejects_hidden_index_and_sparse_checkout(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        for flag, clear in (('--assume-unchanged', '--no-assume-unchanged'),
                            ('--skip-worktree', '--no-skip-worktree')):
            with self.subTest(flag=flag):
                subprocess.run(['git', 'update-index', flag, 'tracked.txt'], cwd=self.workspace, check=True)
                with self.assertRaisesRegex(ct.CandidateError, 'hidden index'):
                    ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))
                subprocess.run(['git', 'update-index', clear, 'tracked.txt'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'config', 'core.sparseCheckout', 'true'], cwd=self.workspace, check=True)
        with self.assertRaisesRegex(ct.CandidateError, 'sparse'):
            ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))

    def test_candidate_baseline_normalizes_prefixes_and_index_paths(self):
        for bad in ('.', './', '.GIT', '.git ', 'src/../x', '/absolute',
                    'src/{wide}', 'src\\name', ':(glob)', 'docs\nother'):
            with self.subTest(bad=bad):
                with self.assertRaises(ct.CandidateError): ct._prefixes((bad,))
        with self.assertRaises(ct.CandidateError): ct._prefixes('src')
        with self.assertRaisesRegex(ct.CandidateError, 'Unicode/case alias'):
            ct._prefixes(('docs', 'Docs'))
        self.assertEqual(ct._prefixes(('b', 'a', 'b')), ('a', 'b'))
        duplicate = (b'100644 ' + b'a' * 40 + b' 0\tFoo\0'
                     b'100644 ' + b'b' * 40 + b' 0\tfoo\0')
        with patch.object(ct, '_git_bytes', return_value=duplicate):
            with self.assertRaisesRegex(ct.CandidateError, 'ambiguous Git path'):
                ct._index_entries({})
        directory_alias = (b'100644 ' + b'a' * 40 + b' 0\tDocs/a\0'
                           b'100644 ' + b'b' * 40 + b' 0\tdocs/b\0')
        with patch.object(ct, '_git_bytes', return_value=directory_alias):
            with self.assertRaisesRegex(ct.CandidateError, 'ambiguous Git path'):
                ct._index_entries({})

    def test_candidate_baseline_byte_proof_rejects_corrupt_checkout(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        blobs = ct._indexed_blobs
        def corrupt(env, entries):
            result = blobs(env, entries)
            oid = next(oid for _, oid, path in entries if path == 'tracked.txt')
            result[oid] = b'not the indexed blob\n'
            return result
        with patch.object(ct, '_indexed_blobs', side_effect=corrupt):
            with self.assertRaisesRegex(ct.CandidateError, 'checkout bytes differ'):
                ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))
        self.assertEqual(list(scratch.iterdir()), [])

    def test_candidate_baseline_rejects_other_linked_worktree_scratch(self):
        linked = self.root / 'linked'
        subprocess.run(['git', 'worktree', 'add', '-q', '-b', 'linked-test', str(linked), 'HEAD'],
                       cwd=self.workspace, check=True)
        scratch = linked / 'scratch'; scratch.mkdir()
        meta = self.root / 'meta'; meta.mkdir()
        with self.assertRaisesRegex(ct.CandidateError, 'outside workspace/worktrees'):
            ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, meta, ('tracked.txt',))
        self.assertEqual(list(scratch.iterdir()), [])

    def test_candidate_ingest_authorized_edit_binds_tree_and_preserves_live_index(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))
        (baseline.root / 'tracked.txt').write_text('candidate change\n')
        revision = ct.ingest_candidate_revision(baseline)
        self.assertNotEqual(revision.tree_oid, baseline.tree_oid)
        self.assertEqual(revision.manifest, ({'status': 'M', 'path': 'tracked.txt'},))
        self.assertEqual((self.workspace / 'tracked.txt').read_text(), 'base\n')
        ct.verify_candidate_revision(baseline, revision)
        (baseline.root / 'tracked.txt').write_text('changed after review\n')
        with self.assertRaisesRegex(ct.CandidateError, 'differs from reviewed candidate OID'):
            ct.verify_candidate_revision(baseline, revision)
        newer = ct.ingest_candidate_revision(baseline)
        self.assertEqual(newer.manifest, ({'status': 'M', 'path': 'tracked.txt'},))
        ct.verify_candidate_revision(baseline, newer)
        with self.assertRaises(ct.CandidateError): ct.verify_candidate_revision(baseline, revision)
        (self.workspace / 'tracked.txt').write_text('live drift after review\n')
        with self.assertRaisesRegex(ct.CandidateError, 'live or scratch parent changed'):
            ct.verify_candidate_revision(baseline, newer)

    def test_candidate_rebuild_from_verified_oid_uses_new_root_and_frozen_live_baseline(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch,
                                                 ('tracked.txt',))
        (baseline.root / 'tracked.txt').write_text('candidate change\n')
        revision = ct.ingest_candidate_revision(baseline)
        rebuilt = ct.rebuild_candidate_from_oid(baseline, revision)
        self.assertNotEqual(rebuilt.root, baseline.root)
        self.assertNotEqual(rebuilt.root_identity, baseline.root_identity)
        self.assertEqual(rebuilt.tree_oid, baseline.tree_oid)
        self.assertEqual(rebuilt.parent_entries, baseline.parent_entries)
        self.assertEqual((rebuilt.root / 'tracked.txt').read_text(), 'candidate change\n')
        self.assertEqual((self.workspace / 'tracked.txt').read_text(), 'base\n')
        ct.verify_candidate_revision(rebuilt, revision)
        (baseline.root / 'tracked.txt').write_text('dirty old root\n')
        second = ct.rebuild_candidate_from_oid(baseline, revision)
        self.assertEqual((second.root / 'tracked.txt').read_text(), 'candidate change\n')
        self.assertNotEqual(second.root_identity, rebuilt.root_identity)

    def test_docs_paths_are_derived_from_candidate_oids_and_invalidate_receipts(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch,
                                                 ('tracked.txt', 'docs'))
        (baseline.root / 'tracked.txt').write_text('reviewed code\n')
        before = ct.ingest_candidate_revision(baseline)
        docs = baseline.root / 'docs'; docs.mkdir()
        (docs / 'guide.md').write_text('new documentation\n')
        after = ct.ingest_candidate_revision(baseline)
        kwargs = dict(exec_paths=(), finish_paths=(), polish_paths=(),
                      closure_inputs=(), closure_uncertain=False)
        approval = dict(candidate_oid=before.tree_oid, run_id=self.run_dir.name,
                        workspace=str(baseline.workspace), run_dir=str(baseline.run_dir),
                        parent_head=baseline.parent_head, phase='POLISH-Q', status='APPROVE',
                        blocking_findings=[], epoch=1)
        result = validate_candidate_docs_change(baseline, before, approval, after,
                                                ['docs/guide.md'], **kwargs)
        self.assertEqual(result.paths, ('docs/guide.md',))
        self.assertTrue(result.requires_rechecks)
        self.assertEqual(result.invalidated_receipts, ('*',))
        approved_after = {**approval, 'candidate_oid': after.tree_oid}
        unchanged = validate_candidate_docs_change(baseline, after, approved_after, after,
                                                    ['docs/guide.md'], **kwargs)
        self.assertFalse(unchanged.requires_rechecks)
        with self.assertRaisesRegex(ct.CandidateError, 'approved prior candidate'):
            validate_candidate_docs_change(baseline, after, approval, after,
                                           ['docs/guide.md'], **kwargs)
        missing = ct.CandidateRevision('0' * len(before.tree_oid), (), 0)
        with self.assertRaises(ct.CandidateError):
            validate_candidate_docs_change(baseline, missing,
                                           {**approval, 'candidate_oid': missing.tree_oid}, after,
                                           ['docs/guide.md'], **kwargs)
        with self.assertRaisesRegex(ValueError, 'EXEC-reviewed path'):
            validate_candidate_docs_change(baseline, before, approval, after,
                                           ['docs/guide.md'], exec_paths=('docs/guide.md',),
                                           finish_paths=(), polish_paths=(), closure_inputs=(),
                                           closure_uncertain=False)
        (docs / 'guide.md').write_text('second edit\n')
        later = ct.ingest_candidate_revision(baseline)
        with self.assertRaisesRegex(ValueError, 'EXEC-reviewed path'):
            validate_candidate_docs_change(baseline, after, approved_after, later,
                                           ['docs/guide.md'], **kwargs)
        (docs / 'guide.md').write_text('post-ingest drift\n')
        with self.assertRaises(ct.CandidateError):
            validate_candidate_docs_change(baseline, before, approval, after,
                                           ['docs/guide.md'], **kwargs)

    def test_docs_oid_policy_refuses_unlisted_source_and_symlink(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch,
                                                 ('tracked.txt', 'docs'))
        kwargs = dict(exec_paths=(), finish_paths=(), polish_paths=(),
                      closure_inputs=(), closure_uncertain=False)
        before = ct.CandidateRevision(baseline.tree_oid, (), 2)
        approval = dict(candidate_oid=before.tree_oid, run_id=self.run_dir.name,
                        workspace=str(baseline.workspace), run_dir=str(baseline.run_dir),
                        parent_head=baseline.parent_head, phase='POLISH-Q', status='APPROVE',
                        blocking_findings=[], epoch=1)
        (baseline.root / 'tracked.txt').write_text('unlisted change\n')
        after = ct.ingest_candidate_revision(baseline)
        with self.assertRaisesRegex(ValueError, 'unreserved path'):
            validate_candidate_docs_change(baseline, before, approval, after,
                                           ['docs/guide.md'], **kwargs)
        (baseline.root / 'tracked.txt').write_text('base\n')
        docs = baseline.root / 'docs'; docs.mkdir()
        (docs / 'target.md').write_text('safe target\n')
        (docs / 'guide.md').symlink_to('target.md')
        after = ct.ingest_candidate_revision(baseline)
        with self.assertRaisesRegex(ct.CandidateError, 'non-regular documentation'):
            validate_candidate_docs_change(baseline, before, approval, after,
                                           ['docs/guide.md'], **kwargs)

    def test_candidate_ingest_includes_ignored_file_and_refuses_unauthorized_paths(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('generated',))
        generated = baseline.root / 'generated'; generated.mkdir()
        (generated / 'result.cache').write_bytes(b'ignored by live Git, required in candidate\n')
        revision = ct.ingest_candidate_revision(baseline)
        self.assertEqual(revision.manifest, ({'status': 'A', 'path': 'generated/result.cache'},))
        (baseline.root / 'tracked.txt').write_text('unauthorized\n')
        with self.assertRaisesRegex(ct.CandidateError, 'unauthorized path'):
            ct.ingest_candidate_revision(baseline)

    def test_candidate_ingest_rejects_prefix_alias_and_unapproved_deletion(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('generated',))
        alias = baseline.root / 'Generated'; alias.mkdir()
        (alias / 'x').write_text('alias\n')
        with self.assertRaisesRegex(ct.CandidateError, 'authorized-prefix alias'):
            ct.ingest_candidate_revision(baseline)
        (alias / 'x').unlink(); alias.rmdir()
        (baseline.root / 'tracked.txt').unlink()
        with self.assertRaisesRegex(ct.CandidateError, 'unauthorized path'):
            ct.ingest_candidate_revision(baseline)

    def test_candidate_ingest_deletion_rename_and_mode_are_manifested(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch,
                                                 ('tracked.txt', 'renamed.txt'))
        (baseline.root / 'tracked.txt').rename(baseline.root / 'renamed.txt')
        revision = ct.ingest_candidate_revision(baseline)
        self.assertEqual({(row['status'], row['path']) for row in revision.manifest},
                         {('D', 'tracked.txt'), ('A', 'renamed.txt')})
        ct.verify_candidate_revision(baseline, revision)
        (baseline.root / 'renamed.txt').chmod(0o755)
        updated = ct.ingest_candidate_revision(baseline)
        self.assertEqual({row['path'] for row in updated.manifest}, {'tracked.txt', 'renamed.txt'})
        ct.verify_candidate_revision(baseline, updated)

    def test_candidate_ingest_rejects_crlf_attribute_and_late_live_drift(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch,
                                                 ('tracked.txt', '.gitattributes'))
        old_index = baseline.index.read_bytes()
        (baseline.root / '.gitattributes').write_text('*.txt crlf=input\n')
        with self.assertRaisesRegex(ct.CandidateError, 'transforming Git attributes'):
            ct.ingest_candidate_revision(baseline)
        self.assertEqual(baseline.index.read_bytes(), old_index)
        self.assertEqual(list(baseline.index.parent.glob('stage-*.index')), [])
        (baseline.root / '.gitattributes').unlink()
        (self.workspace / 'tracked.txt').write_text('live user edit\n')
        with self.assertRaisesRegex(ct.CandidateError, 'live or scratch parent changed'):
            ct.ingest_candidate_revision(baseline)

    def test_fake_author_write_is_ingested_only_in_isolated_candidate(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('sum_ints.py',))
        author = subprocess.run([sys.executable, str(FAKE), 'exec'], cwd=baseline.root,
                                input='Role: persistent. Phase: EXEC. Implement the approved plan.',
                                text=True, capture_output=True, env=os.environ.copy())
        self.assertEqual(author.returncode, 0, author.stderr)
        revision = ct.ingest_candidate_revision(baseline)
        self.assertEqual(revision.manifest, ({'status': 'A', 'path': 'sum_ints.py'},))
        self.assertFalse((self.workspace / 'sum_ints.py').exists())
        ct.verify_candidate_revision(baseline, revision)

    def test_fake_author_unapproved_doc_write_is_rejected(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('sum_ints.py',))
        author = subprocess.run([sys.executable, str(FAKE), 'exec'], cwd=baseline.root,
                                input='Role: persistent. Phase: EXEC. Implement the approved plan.',
                                text=True, capture_output=True,
                                env={**os.environ, 'FAKE_DOC_DELTA': '1'})
        self.assertEqual(author.returncode, 0, author.stderr)
        with self.assertRaisesRegex(ct.CandidateError, 'unauthorized path: CLAUDE.md'):
            ct.ingest_candidate_revision(baseline)

    def test_candidate_ingest_rejects_unindexed_empty_directory_and_corrupt_blob(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('generated',))
        (baseline.root / 'generated').mkdir()
        before = baseline.index.read_bytes()
        with self.assertRaisesRegex(ct.CandidateError, 'path-byte set differs'):
            ct.ingest_candidate_revision(baseline)
        self.assertEqual(baseline.index.read_bytes(), before)
        (baseline.root / 'generated').rmdir()
        blobs = ct._indexed_blobs
        def corrupt(env, entries):
            result = blobs(env, entries)
            oid = next(oid for _, oid, path in entries if path == 'tracked.txt')
            result[oid] = b'corrupt scratch blob'
            return result
        with patch.object(ct, '_indexed_blobs', side_effect=corrupt):
            with self.assertRaisesRegex(ct.CandidateError, 'candidate bytes changed before new tree OID'):
                ct.ingest_candidate_revision(baseline)

    def test_candidate_ingest_rejects_prepoisoned_scratch_index_path(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('generated',))
        (baseline.root / 'tracked.txt').write_text('unauthorized but preindexed\n')
        env = ct._git_env(GIT_DIR=str(baseline.git_dir), GIT_INDEX_FILE=str(baseline.index),
                          GIT_WORK_TREE=str(baseline.root))
        data = (baseline.root / 'tracked.txt').read_bytes()
        oid = ct._git_bytes(['hash-object', '-w', '--no-filters', '--stdin'], env=env,
                            input_bytes=data).decode().strip()
        ct._git(['update-index', '--add', '--cacheinfo', '100644', oid, 'tracked.txt'], env=env)
        with self.assertRaisesRegex(ct.CandidateError, 'unauthorized path'):
            ct.ingest_candidate_revision(baseline)

    def test_candidate_verify_rejects_forged_unauthorized_manifest(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('generated',))
        generated = baseline.root / 'generated'; generated.mkdir()
        (generated / 'ok').write_text('allowed\n')
        ct.ingest_candidate_revision(baseline)
        (baseline.root / 'tracked.txt').write_text('unauthorized\n')
        env = ct._git_env(GIT_DIR=str(baseline.git_dir), GIT_INDEX_FILE=str(baseline.index),
                          GIT_WORK_TREE=str(baseline.root))
        data = (baseline.root / 'tracked.txt').read_bytes()
        oid = ct._git_bytes(['hash-object', '-w', '--no-filters', '--stdin'], env=env,
                            input_bytes=data).decode().strip()
        ct._git(['update-index', '--add', '--cacheinfo', '100644', oid, 'tracked.txt'], env=env)
        forged_oid = ct._git(['write-tree'], env=env)
        ct._git(['update-ref', 'refs/paired-session/candidates/' + forged_oid, forged_oid], env=env)
        forged = ct.CandidateRevision(forged_oid, ct._manifest(env, baseline.tree_oid, forged_oid), 3)
        with self.assertRaisesRegex(ct.CandidateError, 'unauthorized path'):
            ct.verify_candidate_revision(baseline, forged)

    def test_candidate_manifest_ignores_scratch_replace_ref(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))
        alternate_index = baseline.index.with_name('alternate.index')
        env = ct._git_env(GIT_DIR=str(baseline.git_dir), GIT_INDEX_FILE=str(alternate_index),
                          GIT_WORK_TREE=str(baseline.root))
        ct._git(['read-tree', baseline.tree_oid], env=env)
        data = b'alternate tree\n'
        oid = ct._git_bytes(['hash-object', '-w', '--no-filters', '--stdin'], env=env,
                            input_bytes=data).decode().strip()
        ct._git(['update-index', '--add', '--cacheinfo', '100644', oid, 'tracked.txt'], env=env)
        replacement = ct._git(['write-tree'], env=env)
        ct._git(['update-ref', 'refs/replace/' + baseline.tree_oid, replacement], env=env)
        (baseline.root / 'tracked.txt').write_text('candidate tree\n')
        revision = ct.ingest_candidate_revision(baseline)
        self.assertEqual(revision.manifest, ({'status': 'M', 'path': 'tracked.txt'},))
        ct.verify_candidate_revision(baseline, revision)

    def test_candidate_poisoned_index_cache_tree_is_refused(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))
        alt_index = baseline.index.with_name('alternate.index')
        env = ct._git_env(GIT_DIR=str(baseline.git_dir), GIT_INDEX_FILE=str(alt_index),
                          GIT_WORK_TREE=str(baseline.root))
        ct._git(['read-tree', baseline.tree_oid], env=env)
        blob = ct._git_bytes(['hash-object', '-w', '--no-filters', '--stdin'], env=env,
                             input_bytes=b'older tree bytes\n').decode().strip()
        ct._git(['update-index', '--add', '--cacheinfo', '100644', blob, 'tracked.txt'], env=env)
        older_tree = ct._git(['write-tree'], env=env)
        original = baseline.index.read_bytes()
        def poison(raw, replacement):
            offset = raw.find(bytes.fromhex(baseline.tree_oid))
            self.assertGreaterEqual(offset, 0)
            body = raw[:-20]
            body = body[:offset] + bytes.fromhex(replacement) + body[offset + 20:]
            return body + hashlib.sha1(body).digest()
        baseline.index.write_bytes(poison(original, older_tree))
        with self.assertRaisesRegex(ct.CandidateError, 'cache-tree differs'):
            ct.ingest_candidate_revision(baseline)
        baseline.index.write_bytes(original)
        revision = ct.ingest_candidate_revision(baseline)
        adopted = baseline.index.read_bytes()
        baseline.index.write_bytes(poison(adopted, older_tree))
        with self.assertRaisesRegex(ct.CandidateError, 'cache-tree differs'):
            ct.verify_candidate_revision(baseline, revision)

    def test_candidate_verify_rejects_tampered_scratch_blob_bytes(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt',))
        (baseline.root / 'tracked.txt').write_text('reviewed bytes\n')
        revision = ct.ingest_candidate_revision(baseline)
        indexed_blobs = ct._indexed_blobs
        def corrupt(env, entries):
            blobs = indexed_blobs(env, entries)
            oid = next(oid for _, oid, path in entries if path == 'tracked.txt')
            blobs[oid] = b'wrong object-store bytes'
            return blobs
        with patch.object(ct, '_indexed_blobs', side_effect=corrupt):
            with self.assertRaisesRegex(ct.CandidateError, 'scratch blob differs'):
                ct.verify_candidate_revision(baseline, revision)

    def test_candidate_ingest_replaces_directory_with_symlink_after_deletions(self):
        directory = self.workspace / 'a'; directory.mkdir()
        (directory / 'child.txt').write_text('old\n')
        subprocess.run(['git', 'add', 'a/child.txt'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'directory'], cwd=self.workspace, check=True)
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('a',))
        (baseline.root / 'a/child.txt').unlink(); (baseline.root / 'a').rmdir()
        os.symlink('tracked.txt', baseline.root / 'a')
        revision = ct.ingest_candidate_revision(baseline)
        self.assertEqual({(row['status'], row['path']) for row in revision.manifest},
                         {('A', 'a'), ('D', 'a/child.txt')})
        ct.verify_candidate_revision(baseline, revision)

    def test_candidate_ingest_rejects_raw_oid_mismatch_and_symlink_escape(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('tracked.txt', 'links'))
        (baseline.root / 'tracked.txt').write_text('writer bytes\n')
        git_bytes = ct._git_bytes
        def wrong_oid(args, **kwargs):
            if args[:2] == ['hash-object', '-w']: return b'0' * 40 + b'\n'
            return git_bytes(args, **kwargs)
        with patch.object(ct, '_git_bytes', side_effect=wrong_oid):
            with self.assertRaisesRegex(ct.CandidateError, 'raw blob ID differs'):
                ct.ingest_candidate_revision(baseline)
        links = baseline.root / 'links'; links.mkdir()
        os.symlink('../../outside', links / 'escape')
        with self.assertRaisesRegex(ct.CandidateError, 'symlink escapes'):
            ct.ingest_candidate_revision(baseline)

    def test_candidate_ingest_safe_symlink_and_hardlink_boundary(self):
        scratch = self.root / 'scratch'; scratch.mkdir()
        baseline = ct.prepare_candidate_baseline(self.workspace, self.run_dir, scratch, scratch, ('assets',))
        assets = baseline.root / 'assets'; assets.mkdir()
        (assets / 'target').write_text('blob\n')
        os.symlink('target', assets / 'link')
        revision = ct.ingest_candidate_revision(baseline)
        self.assertEqual({row['path'] for row in revision.manifest}, {'assets/target', 'assets/link'})
        ct.verify_candidate_revision(baseline, revision)
        outside = self.root / 'outside-hardlink'; outside.write_text('external\n')
        os.link(outside, assets / 'hardlink')
        with self.assertRaisesRegex(ct.CandidateError, 'hardlinked'):
            ct.ingest_candidate_revision(baseline)
        self.assertEqual(outside.read_text(), 'external\n')

    def test_project_json_config_rejects_noninteger_numeric_values(self):
        config_dir = self.workspace / '.review-loop'
        config_dir.mkdir()
        for value in ('true', '3.9', '"25"'):
            with self.subTest(value=value):
                (config_dir / 'paired-session.json').write_text(
                    '{"max_invocations":' + value + '}')
                with self.assertRaisesRegex(ValueError, 'must be an integer'):
                    rc.configure_parser(rc.parser(), ['run', '--workspace', str(self.workspace),
                        '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])

    def test_abbreviated_options_are_rejected_instead_of_merging_profile_lists(self):
        config_dir = self.workspace / '.review-loop'
        config_dir.mkdir()
        (config_dir / 'paired-session.json').write_text(
            '{"reviewer_command":["python3 -m unittest"]}')
        argv = ['run', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                '--run-dir', str(self.run_dir), '--reviewer-comm', 'echo unsafe']
        with patch('sys.stderr', new=io.StringIO()):
            with self.assertRaises(SystemExit):
                rc.configure_parser(rc.parser(), argv).parse_args(argv)

    def test_cli_paths_expand_home_before_validation_and_state_use(self):
        fake_home = self.root / 'fake-home'
        fake_home.mkdir()
        workspace = fake_home / 'workspace'
        workspace.mkdir()
        args = rc.parser().parse_args(['run', '--workspace', '~/workspace',
            '--workitem', '~/WORKITEM.md', '--run-dir', '~/workspace/.compass/run'])
        with patch.dict(os.environ, {'HOME': str(fake_home)}):
            rc.normalize_cli_paths(args)
        self.assertEqual(Path(args.workspace), workspace)
        self.assertEqual(Path(args.workitem), fake_home / 'WORKITEM.md')
        self.assertEqual(Path(args.run_dir), workspace / '.compass' / 'run')

    def test_bundled_gate_prompt_config_is_stable_across_plugin_cache_roots(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        old_default = self.root / 'cache' / '2.8.7' / 'scripts' / 'gate.txt'
        new_default = self.root / 'cache' / '2.8.8' / 'scripts' / 'gate.txt'
        old_default.parent.mkdir(parents=True)
        new_default.parent.mkdir(parents=True)
        old_default.write_text('stable gate prompt\n')
        new_default.write_text('stable gate prompt\n')
        args.gate_prompt = str(old_default)
        with patch.object(rc, 'DEFAULT_GATE_PROMPT', old_default):
            old_config = rc.Coordinator(args)._config()['gate_prompt']
        args.gate_prompt = str(new_default)
        with patch.object(rc, 'DEFAULT_GATE_PROMPT', new_default):
            new_config = rc.Coordinator(args)._config()['gate_prompt']
        self.assertEqual(old_config, new_config)

    def test_run_directory_inside_workspace_is_refused_before_state_creation(self):
        command = self.command()
        command[command.index('--run-dir') + 1] = str(self.workspace / '.compass' / 'run')
        result = subprocess.run(command, cwd=self.root, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 2)
        self.assertIn('REFUSED: --run-dir must be outside --workspace', result.stdout)
        self.assertFalse((self.workspace / '.compass').exists())
        self.assertEqual((self.workspace / 'tracked.txt').read_text(), 'base\n')

    def test_test_command_preflight_accepts_environment_assignments_and_env_wrappers(self):
        expected = shutil.which('python3')
        self.assertEqual(rc.resolve_test_executable(self.workspace, 'NODE_ENV=test python3 -m unittest'),
                         expected)
        self.assertEqual(rc.resolve_test_executable(self.workspace, 'env NODE_ENV=test python3 -m unittest'),
                         expected)
        self.assertEqual(rc.resolve_test_executable(self.workspace, 'env -- NODE_ENV=test python3 -m unittest'),
                         expected)
        self.assertEqual(rc.resolve_test_executable(self.workspace,
            "env -S 'NODE_ENV=test python3 -m unittest'"), expected)

    def test_test_command_preflight_resolves_relative_env_launcher_from_workspace(self):
        launcher = self.workspace / 'tools' / 'env'
        launcher.parent.mkdir()
        launcher.write_text('#!/bin/sh\nexec /usr/bin/env "$@"\n')
        launcher.chmod(0o755)
        resolved = rc.resolve_test_executable(
            self.workspace, './tools/env NODE_ENV=test python3 -m unittest')
        self.assertEqual(resolved, shutil.which('python3'))
        with self.assertRaisesRegex(ValueError, 'launcher is missing'):
            rc.resolve_test_executable(self.workspace, './missing/env python3 -m unittest')

    def test_out_of_phase_plan_critical_is_recorded_minor_and_cannot_force_revise(self):
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      '--polish-round', 'off',
                                      env={'FAKE_PLAN_CODE_FINDING': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual((state['plan_rounds'], state['plan_reviews']), (1, 1))
        finding = state['finding_ledger'][0]
        self.assertEqual(finding['severity'], 'MINOR')
        self.assertTrue(finding['summary'].startswith('[out-of-phase]'))
        plan_round = next((self.run_dir / 'rounds').glob('*-supervisor-approve.md')).read_text()
        self.assertIn('[out-of-phase]', plan_round)

    def test_codex_plan_receives_full_inputs_without_requiring_shell_reads(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--author-vendor', 'claude', '--reviewer-vendor', 'codex', '--gate-model', 'gpt-6-luna'])
        co = rc.Coordinator(args)
        (co.context / 'plan.md').write_text('Unique plan body with verification.')
        prompt = co._review_prompt('reviewer', 'snapshot')
        self.assertIn(self.workitem.read_text(), prompt)
        self.assertIn('Unique plan body with verification.', prompt)
        self.assertNotIn('Do not run commands', prompt)
        self.assertIn('cat, sed, or rg', prompt)
        self.assertNotIn('Run this test command', prompt)
        co.state['phase'] = 'EXEC'
        for role in ('reviewer', 'shadow'):
            self.assertIn('cat, sed, or rg', co._review_prompt(role, 'snapshot'))
        gate = co._gate_prompt('snapshot')
        self.assertIn('cat, sed, or rg', gate)
        self.assertNotIn('Open finding ledger', gate)

    def test_codex_plan_fake_cli_reads_source_but_cannot_mutate_snapshot(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--reviewer-vendor', 'codex', '--codex-bin', str(FAKE),
            '--reviewer-command', "sed -n '1,20p' tracked.txt"])
        co = rc.Coordinator(args)
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        prompt = co._review_prompt('reviewer', 'snapshot')
        before = rc.git_snapshot(self.workspace)[0]
        with patch.dict(os.environ, {'FAKE_PLAN_INSPECT': '1'}):
            result = co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())
        self.assertEqual(before, result['snapshot'])
        self.assertTrue(any(row['command'] == "sed -n '1,20p' tracked.txt"
                            for row in result['answer']['observed_commands']))
        command = co.state['turns'][-1]['command']
        self.assertIn('sandbox_mode="read-only"', command)
        self.assertIn('--ignore-rules', command)
        with patch.dict(os.environ, {'FAKE_PLAN_REVIEWER_MUTATE': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'reviewer mutated workspace'):
                co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())

    def test_verified_claims_required_rendered_and_empty_approval_retries_once(self):
        for schema in (rc.review_schema(), rc.fresh_review_schema(), rc.gate_schema()):
            self.assertIn('verified_claims', schema['required'])
            self.assertEqual(schema['properties']['verified_claims']['items']['required'],
                             ['claim', 'file', 'line'])
        for role in ('reviewer', 'shadow', 'gate'):
            run = self.root / ('retry-' + role)
            result = self.run_coordinator('--run-dir', str(run), '--polish-round', 'off',
                                          env={'FAKE_EMPTY_CLAIMS': role})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads((run / 'state.json').read_text())
            turns = [t for t in state['turns'] if t['role'] == role]
            for index, turn in enumerate(turns):
                if turn.get('verified_claims_error'):
                    self.assertNotIn('verified_claims_error', turns[index + 1])
                    self.assertEqual(turn['phase'], turns[index + 1]['phase'])
            self.assertTrue(any(t.get('verified_claims_error') for t in turns))
            self.assertTrue(any('## Verified Claims' in p.read_text()
                                for p in (run / 'rounds').glob('*.md')))
            retry_prompts = [p.read_text() for p in (run / 'evidence').glob('*.prompt.txt')
                             if 'Evidence contract retry:' in p.read_text()]
            self.assertTrue(retry_prompts)
            if role != 'reviewer':
                self.assertTrue(all('prior_findings' not in p for p in retry_prompts))

    def test_verified_claims_still_empty_after_one_retry_holds_each_role(self):
        for role in ('reviewer', 'shadow', 'gate'):
            run = self.root / ('hold-' + role)
            result = self.run_coordinator('--run-dir', str(run), '--polish-round', 'off',
                env={'FAKE_EMPTY_CLAIMS': role, 'FAKE_ALWAYS_EMPTY_CLAIMS': '1'})
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            state = json.loads((run / 'state.json').read_text())
            self.assertEqual(state['status'], 'HOLD')
            self.assertIn('verified_claims protocol error after one retry', state['hold_reason'])
            turns = [t for t in state['turns'] if t['role'] == role]
            self.assertEqual(len(turns), 2)
            self.assertTrue(all(t.get('verified_claims_error') for t in turns))

    def test_verified_claims_invalid_entries_do_not_satisfy_approval(self):
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      env={'FAKE_MALFORMED_CLAIMS': 'reviewer'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertIn('verified_claims protocol error after one retry', state['hold_reason'])
        self.assertTrue(rc.verified_claims_error({'status': 'APPROVE'}))
        self.assertEqual(rc.verified_claims_error({'status': 'REVISE', 'verified_claims': []}), '')

    def test_out_of_phase_normalization_cannot_bypass_verified_claims(self):
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
            env={'FAKE_PLAN_CODE_FINDING': '1', 'FAKE_EMPTY_CLAIMS': 'reviewer',
                 'FAKE_ALWAYS_EMPTY_CLAIMS': '1'})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertIn('verified_claims protocol error after one retry', state['hold_reason'])

    def test_fresh_roles_reject_ledger_ids_in_referenced_plan_before_launch(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        (co.context / 'plan.md').write_text('# Plan\n## Response to F002\nPrior review discussion.\n')
        for role in ('shadow', 'gate'):
            with self.assertRaisesRegex(RuntimeError, 'ledger ids in context/plan.md'):
                co.invoke(role, 'EXEC', 'Clean prompt referencing the plan file.',
                          rc.fresh_review_schema(), fresh=True)
        self.assertEqual(co.state['sequence'], 0)
        self.assertEqual(list(co.evidence.glob('*.prompt.txt')), [])
        self.assertIn('current plan only', co._author_prompt())

    def test_fresh_roles_scan_all_context_and_history_before_process_launch(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        for filename in ('plan.md', 'workitem.md', 'delta.patch', 'extra.md'):
            path = co.context / filename
            original = path.read_text() if path.exists() else None
            for leak in ('Response to reviewer', 'Prior verdict: APPROVE',
                         'Codex requested this', 'Reviewer found a defect', 'F002'):
                path.write_text(leak)
                for role in ('shadow', 'gate'):
                    with patch('subprocess.Popen') as popen:
                        with self.assertRaisesRegex(RuntimeError, 'independence check rejected'):
                            co.invoke(role, 'EXEC', 'Clean prompt', rc.fresh_review_schema(), fresh=True)
                        popen.assert_not_called()
            if original is None:
                path.unlink()
            else:
                path.write_text(original)
        self.assertEqual(co.state['sequence'], 0)
        co.assert_fresh_prompt('shadow', co._review_prompt('shadow', 'snapshot'))
        audit = json.loads((co.evidence / '001-shadow.independence-inputs.json').read_text())
        self.assertEqual(audit['status'], 'PASS')
        self.assertIn('context/workitem.md', audit['inputs'])
        self.assertEqual(audit['inputs']['prompt']['content'], co._review_prompt('shadow', 'snapshot'))

    def test_fresh_scan_masks_exact_coordinator_paths_only(self):
        workspace = self.root / 'claude-501' / '.codex' / 'workspace'
        workspace.parent.mkdir(parents=True)
        subprocess.run(['git', 'clone', '-q', str(self.workspace), str(workspace)], check=True)
        run_dir = self.root / 'claude-501' / '.codex' / 'run'
        args = rc.parser().parse_args(['run', '--workspace', str(workspace),
            '--workitem', str(self.workitem), '--run-dir', str(run_dir)])
        co = rc.Coordinator(args)
        for path in (co.workspace, co.run_dir, co.evidence, co.context, co.author_temp_dir):
            self.assertIn(str(path), co._fresh_scan_run_paths())
            (co.context / 'plan.md').write_text(f'Path: {path}/child')
            co.assert_fresh_prompt('shadow', 'Clean prompt')
        disposable_home = co.run_dir / 'disposable-codex-home'
        with patch.dict(os.environ, {'TMPDIR': '/tmp/claude-501-tmp',
                                    'CODEX_HOME': str(disposable_home)}):
            spellings = co._fresh_scan_run_paths()
        self.assertIn(str(disposable_home), spellings)
        self.assertIn(str(disposable_home.resolve()), spellings)
        self.assertNotIn('/tmp/claude-501-tmp', spellings)
        with patch.dict(os.environ, {'CODEX_HOME': str(Path.home() / '.codex')}):
            self.assertNotIn(str(Path.home() / '.codex'), co._fresh_scan_run_paths())
        with patch.object(co, 'author_temp_dir', Path('/tmp/claude-501/author-tmp')):
            spellings = co._fresh_scan_run_paths()
        self.assertIn('/tmp/claude-501/author-tmp', spellings)
        self.assertIn('/private/tmp/claude-501/author-tmp', spellings)
        outside_home = self.root / 'external-codex-home'
        with patch.dict(os.environ, {'TMPDIR': '/tmp/claude-501-tmp',
                                    'CODEX_HOME': str(outside_home)}):
            spellings = co._fresh_scan_run_paths()
        self.assertNotIn(str(outside_home), spellings)
        self.assertNotIn('/tmp/claude-501-tmp', spellings)
        with patch.dict(os.environ, {'TMPDIR': '/tmp/claude-501-tmp',
                                    'CODEX_HOME': str(outside_home)}):
            (co.context / 'plan.md').write_text(str(outside_home / 'claude-session.json'))
            with self.assertRaisesRegex(RuntimeError, r'(?i)history in context/plan\.md: (?:codex|claude)'):
                co.assert_fresh_prompt('shadow', 'Clean prompt')
        for text in ('Claude/Sonnet signed off.', 'Codex/GPT approved.',
                     str(co.workspace) + '/x Claude said this is fine.',
                     str(co.workspace) + 'Claude said this is fine.',
                     'Claude said this is fine ' + str(co.workspace),
                     'prefix' + str(co.workspace),
                     '/unknown/claude-501/run/report.md'):
            (co.context / 'plan.md').write_text(text)
            expected = 'Codex' if text.startswith('Codex/GPT') else 'Claude'
            if text.startswith('/unknown/'):
                expected = 'claude'
            with self.assertRaisesRegex(RuntimeError,
                    rf'(?i)independence check rejected history in context/plan\.md: {expected}'):
                co.assert_fresh_prompt('shadow', 'Clean prompt')
        (co.context / 'plan.md').write_text('Clean approved plan.')
        for token, expected in (('F002', r'history in context/delta\.patch: F002'),
                                ('prior finding', r'history in context/delta\.patch: prior'),
                                (str(co.run_dir / 'F007'), r'history in context/delta\.patch: F007'),
                                (str(co.run_dir / 'APPROVE'), r'history in context/delta\.patch: APPROVE')):
            (co.context / 'delta.patch').write_text(token)
            with self.assertRaisesRegex(RuntimeError, expected):
                co.assert_fresh_prompt('shadow', 'Clean prompt')

    def test_fresh_scan_does_not_mask_vendor_text_glued_to_known_path_tail(self):
        run_dir = self.root / 'run-C'
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(run_dir)])
        co = rc.Coordinator(args)
        (co.context / 'plan.md').write_text(str(co.run_dir) + 'laude approved this.')
        with self.assertRaisesRegex(RuntimeError,
                r'independence check rejected history in context/plan\.md: Claude'):
            co.assert_fresh_prompt('shadow', 'Clean prompt')

    def test_fresh_path_names_pass_fake_cli_but_prose_history_is_rejected(self):
        result = self.run_coordinator('--exercise-revisions', env={'FAKE_DOC_DELTA': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE')
        fresh = list((self.run_dir / 'evidence').glob('*-shadow.independence-inputs.json'))
        self.assertTrue(fresh)
        self.assertTrue(any('CLAUDE.md' in p.read_text() for p in fresh))
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.root / 'prose-check')])
        co = rc.Coordinator(args)
        for text in ('Restore the entry in CLAUDE.md.', 'Read `docs/Codex.md`.',
                     'See "tools/Astra.js"; inspect /tmp/Claude.txt:7.'):
            (co.context / 'plan.md').write_text(text)
            co.assert_fresh_prompt('shadow', 'Clean prompt')
        for text in ('Codex approved this.', 'Claude requested the change.',
                     'Read CLAUDE.md. Prior verdict: APPROVE',
                     'See tools/Codex.js:7. Response to reviewer: fixed.'):
            (co.context / 'plan.md').write_text(text)
            with patch('subprocess.Popen') as popen:
                with self.assertRaisesRegex(RuntimeError, 'independence check rejected'):
                    co.invoke('shadow', 'EXEC', 'Clean prompt', rc.fresh_review_schema(), fresh=True)
                popen.assert_not_called()

    def test_fresh_scan_with_vendor_named_support_root_and_diff_headers(self):
        root = self.root / 'claude-501' / '.codex'
        workspace = root / 'workspace'
        workspace.parent.mkdir(parents=True)
        subprocess.run(['git', 'clone', '-q', str(self.workspace), str(workspace)], check=True)
        args = rc.parser().parse_args(['run', '--workspace', str(workspace),
            '--workitem', str(self.workitem), '--run-dir', str(root / 'run')])
        with patch.object(rc, 'HERE', root / 'support' / 'paired_session'), \
             patch.object(rc, 'DEFAULT_GATE_PROMPT', root / 'support' / 'scripts' / 'gate.txt'):
            co = rc.Coordinator(args)
            self.assertIn(str(root / 'support'), co._fresh_scan_run_paths())
            (co.context / 'delta-since-last-review.patch').write_text(
                f'diff --git a{co.run_dir}/internal/current-review/CLAUDE.md '
                f'b{co.run_dir}/internal/current-review/CLAUDE.md\n'
                f'+++ b{root}/support/docs/CLAUDE.md\n'
                f'diff --git a{co.run_dir}/internal/current-review/Makefile '
                f'b{co.run_dir}/internal/current-review/Makefile\n'
                f'+++ b{root}/support/paired_session/Makefile\n'
                ' context from CLAUDE.md if available\n')
            co.assert_fresh_prompt('shadow', 'Clean prompt')
            co.assert_fresh_prompt('shadow', f'Read {root}/support/scripts before review.')
            (co.context / 'plan.md').write_text('Per CLAUDE.md, keep tests.')
            co.assert_fresh_prompt('shadow', 'Clean prompt')
            (co.context / 'plan.md').write_text(f'Read {root}/support/paired_session/Makefile')
            with self.assertRaisesRegex(RuntimeError, 'history in context/plan.md: claude'):
                co.assert_fresh_prompt('shadow', 'Clean prompt')
            for text in ('Claude said this is fine.', 'Claude.ai approved the plan.',
                         'Codex.app signed off.', 'Per Claude.Then fix it.'):
                (co.context / 'plan.md').write_text(text)
                with self.assertRaisesRegex(RuntimeError, 'history in context/plan.md: (?:Claude|Codex)'):
                    co.assert_fresh_prompt('shadow', 'Clean prompt')

    def test_approve_with_minor_is_advisory_in_next_author_prompt(self):
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      '--polish-round', 'off',
                                      env={'FAKE_APPROVE_MINOR': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        prompts = [path.read_text() for path in sorted(
            (self.run_dir / 'evidence').glob('*-exec-author.prompt.txt'))]
        self.assertTrue(prompts)
        self.assertIn('ADVISORY', prompts[0])
        self.assertIn('explicitly non-blocking', prompts[0])
        self.assertIn('style remains untidy', prompts[0])

    def test_final_exec_minor_revise_advances_with_reported_advisory_and_resume_is_idempotent(self):
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      '--max-exec-rounds', '1',
                                      env={'FAKE_EXEC_MINOR_REVISE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        verdict = next(row for row in state['review_verdicts'] if row['phase'] == 'EXEC')
        self.assertEqual((verdict['reviewer_raw_verdict'], verdict['effective_verdict']),
                         ('REVISE', 'APPROVE_WITH_ADVISORY'))
        advisory = next(row for row in state['finding_ledger'] if row['severity'] == 'MINOR')
        self.assertTrue(advisory['advisory'])
        self.assertTrue(any(row['status'] == 'open' for row in advisory['status_history']))
        self.assertIn('(advisory)', (self.run_dir / 'findings-ledger.md').read_text())
        self.assertIn('advisory exec polish', (self.run_dir / 'findings-ledger.md').read_text())
        self.assertIn('APPROVE_WITH_ADVISORY', (self.run_dir / 'review-comparison.md').read_text())
        polish_prompt = next(path.read_text() for path in (self.run_dir / 'evidence').glob(
            '*-polish-author.prompt.txt'))
        self.assertIn('advisory exec polish', polish_prompt)
        count = len(state['turns'])
        command = self.command('--shadow', 'off', '--adversarial-gate', 'off',
                               '--max-exec-rounds', '1', '--skip-probe')
        command[2] = 'resume'
        resumed = subprocess.run(command, cwd=self.root, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertEqual(len(json.loads((self.run_dir / 'state.json').read_text())['turns']), count)

    def test_partial_advisory_exit_replays_recorded_reviewer_without_redispatch(self):
        co = self.coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                              '--polish-round', 'off', '--max-exec-rounds', '1')
        snapshot = rc.git_snapshot(co.workspace)[0]
        sequence = co.state['sequence'] + 1
        command = co.args.test_command
        answer = {'status': 'REVISE', 'full_review': [{'severity': 'MINOR', 'file': 'tracked.txt',
                  'summary': 'replay advisory', 'failure_scenario': 'small cleanup'}],
                  'prior_findings': [], 'self_run_evidence': [{'command': command}],
                  'observed_commands': [{'command': command, 'exit_code': 0, 'error': False,
                                         'output': 'Ran 1 test successfully'}],
                  'reviewed_snapshot': snapshot, 'verified_claims': []}
        co.state.update({'phase': 'EXEC', 'exec_rounds': 1,
                         'pending_reviewer_result_sequence': sequence, 'sequence': sequence})
        co.state['turns'].append({'sequence': sequence, 'role': 'reviewer', 'phase': 'EXEC',
            'answer': answer, 'snapshot_before': snapshot, 'open_finding_ids': []})
        co.save()
        original_save, calls = co.save, 0
        def fail_final_save():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError('simulated interruption before advisory state advancement')
            original_save()
        with patch.object(co, 'save', side_effect=fail_final_save):
            with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                co.reviewer_turn()
        interrupted = json.loads(co.state_path.read_text())
        self.assertEqual(interrupted['pending_reviewer_result_sequence'], sequence)
        self.assertEqual(len(interrupted['review_verdicts']), 1)

        resumed = rc.Coordinator(co.args)
        with patch.object(resumed, 'invoke') as invoke:
            resumed.reviewer_turn()
            invoke.assert_not_called()
        completed = json.loads(co.state_path.read_text())
        self.assertEqual(completed['status'], 'DONE')
        self.assertEqual(len(completed['turns']), 1)
        self.assertEqual(len(completed['review_verdicts']), 1)
        self.assertEqual(completed['review_verdicts'][0]['effective_verdict'], 'APPROVE_WITH_ADVISORY')

    def test_polish_minor_revise_at_reviewer_cap_is_advisory_done(self):
        result = self.run_coordinator(env={'FAKE_GATE_MINOR': '1',
                                           'FAKE_POLISH_MINOR_REVISE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertTrue(state['polish']['completed'])
        self.assertEqual(state['polish']['reviewer_turns'], 1)
        polish = next(row for row in state['review_verdicts'] if row['phase'] == 'POLISH')
        self.assertEqual((polish['reviewer_raw_verdict'], polish['effective_verdict']),
                         ('REVISE', 'APPROVE_WITH_ADVISORY'))
        self.assertTrue(any(row.get('advisory') and row['status'] == 'open'
                            for row in state['finding_ledger']))

    def test_mixed_minor_major_does_not_take_advisory_exit(self):
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      '--polish-round', 'off', '--max-exec-rounds', '1',
                                      env={'FAKE_EXEC_MIXED_REVISE': '1'})
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD')
        self.assertTrue(any(row['severity'] == 'MAJOR' and row['status'] == 'open'
                            for row in state['finding_ledger']))
        self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))
        exec_verdict = next(row for row in state['review_verdicts'] if row['phase'] == 'EXEC')
        self.assertEqual(exec_verdict['effective_verdict'], 'REVISE')

    def test_nonfinal_exec_minor_revise_keeps_repair_loop(self):
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      '--polish-round', 'off', '--max-exec-rounds', '2',
                                      env={'FAKE_EXEC_MINOR_REVISE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        authors = [row for row in state['turns'] if row['role'] == 'author' and row['phase'] == 'EXEC']
        self.assertEqual(len(authors), 2)
        exec_verdicts = [row['effective_verdict'] for row in state['review_verdicts']
                         if row['phase'] == 'EXEC']
        self.assertEqual(exec_verdicts, ['REVISE', 'APPROVE_WITH_ADVISORY'])

    def test_failing_configured_test_prevents_minor_advisory_exit(self):
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      '--polish-round', 'off', '--max-exec-rounds', '1',
                                      env={'FAKE_EXEC_MINOR_REVISE': '1',
                                           'FAKE_REVIEW_TEST_FAILURE': '1'})
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD')
        self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))

    def test_missing_successful_test_prevents_minor_advisory_exit(self):
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      '--polish-round', 'off', '--max-exec-rounds', '1',
                                      env={'FAKE_EXEC_MINOR_REVISE': '1',
                                           'FAKE_REVIEW_NO_TEST_EVENT': '1'})
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD')
        self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))

    def test_polish_minor_revise_requires_nonempty_self_run_evidence(self):
        result = self.run_coordinator(env={'FAKE_GATE_MINOR': '1',
                                           'FAKE_POLISH_MINOR_REVISE': '1',
                                           'FAKE_POLISH_NO_EVIDENCE': '1'})
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD')
        self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))

    def test_last_plan_minor_revise_advances_as_approve(self):
        result = self.run_coordinator('--max-plan-rounds', '1',
                                      env={'FAKE_PLAN_MINOR_REVISE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        plan = next(row for row in state['review_verdicts'] if row['phase'] == 'PLAN')
        self.assertEqual((plan['reviewer_raw_verdict'], plan['effective_verdict']),
                         ('REVISE', 'APPROVE_WITH_ADVISORY'))

    def test_shadow_critical_prevents_minor_advisory_exit(self):
        result = self.run_coordinator('--shadow', 'on', '--adversarial-gate', 'off',
                                      '--polish-round', 'off', '--max-exec-rounds', '1',
                                      env={'FAKE_EXEC_MINOR_REVISE': '1',
                                           'FAKE_SHADOW_CRITICAL': '1'})
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD')
        self.assertTrue(any(row['source'] == 'fresh-shadow' and row['severity'] == 'CRITICAL'
                            for row in state['finding_ledger']))
        self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))

    def test_security_and_low_severities_are_not_misclassified(self):
        for flag in ('FAKE_EXEC_SECURITY_REVISE', 'FAKE_EXEC_LOW_REVISE'):
            with self.subTest(flag=flag):
                self.run_dir = self.root / flag.lower()
                result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                    '--polish-round', 'off', '--max-exec-rounds', '1', env={flag: '1'})
                state = json.loads((self.run_dir / 'state.json').read_text())
                if flag == 'FAKE_EXEC_SECURITY_REVISE':
                    self.assertEqual(result.returncode, 2)
                    self.assertFalse(any(row.get('advisory') for row in state['finding_ledger']))
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    low = next(row for row in state['finding_ledger'] if row['severity'] == 'LOW')
                    self.assertTrue(low['advisory'])

    def test_workitem_reviewer_commands_merge_into_roles_and_probe_digest(self):
        self.workitem.write_text('# Toy\n```reviewer-commands\nnode verify-real-data.mjs\npython3 audit.py\n```\n')
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--reviewer-command', 'node cli-check.mjs'])
        co = rc.Coordinator(args)
        self.assertEqual(co.reviewer_commands(), ['npm test', 'node cli-check.mjs',
                                                  'node verify-real-data.mjs', 'python3 audit.py'])
        co.state['phase'] = 'EXEC'
        for prompt in (co._review_prompt('reviewer', 'snapshot'),
                       co._review_prompt('shadow', 'snapshot'), co._gate_prompt('snapshot')):
            self.assertIn('Commands you may run exactly as written', prompt)
            self.assertIn('node verify-real-data.mjs', prompt)
        self.assertIn('node verify-real-data.mjs', co.reviewer_flags()['reviewer_commands'])

    def test_polish_round_decline_and_critical_fix_are_bounded(self):
        declined = self.run_coordinator('--exercise-revisions', env={'FAKE_POLISH_DECLINE': '1'})
        self.assertEqual(declined.returncode, 0, declined.stderr + declined.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertTrue(state['polish']['completed'])
        self.assertEqual((state['polish']['author_turns'], state['polish']['reviewer_turns']), (1, 1))
        self.assertEqual(sum(t['phase'] == 'POLISH' and t['role'] == 'shadow' for t in state['turns']), 0)
        open_report = (self.run_dir / 'open-findings.md').read_text()
        self.assertIn('declined: deferred by fake', open_report)

        self.run_dir = self.root / 'critical-polish'
        fixed = self.run_coordinator('--exercise-revisions', env={'FAKE_POLISH_CRITICAL': '1'})
        self.assertEqual(fixed.returncode, 0, fixed.stderr + fixed.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertTrue(state['polish']['fix_used'])
        self.assertEqual((state['polish']['author_turns'], state['polish']['reviewer_turns']), (2, 2))
        self.assertEqual(sum(t['phase'] == 'POLISH' for t in state['turns']), 4)
        self.assertEqual(sum(t['role'] == 'gate' for t in state['turns']), 1)

    def test_nonblocking_gate_finding_gets_id_and_enters_polish(self):
        result = self.run_coordinator(env={'FAKE_GATE_MINOR': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        gate_rows = [row for row in state['finding_ledger']
                     if row['source'] == 'adversarial-gate']
        self.assertEqual(len(gate_rows), 1)
        self.assertRegex(gate_rows[0]['id'], r'^F\d{3}$')
        self.assertTrue(state['polish']['completed'])
        polish_author = next(path.read_text() for path in
                             (self.run_dir / 'evidence').glob('*-polish-author.prompt.txt'))
        self.assertIn(gate_rows[0]['id'], polish_author)

    def test_resume_polish_copies_old_real_run_imports_gate_and_reuses_sessions(self):
        # Generate a completed, pre-polish run using only fake CLIs. A live run
        # is mutable and may already have consumed its one-time polish round.
        fixture_options = ('--polish-round', 'off', '--shadow', 'off',
                           '--max-invocations', '40')
        generated = self.run_coordinator(*fixture_options,
                                         env={'FAKE_GATE_MINOR': '1'})
        self.assertEqual(generated.returncode, 0, generated.stderr + generated.stdout)
        source = self.run_dir
        fixture = json.loads((source / 'state.json').read_text())
        self.assertEqual(fixture['status'], 'DONE')
        self.assertFalse(fixture['polish']['completed'])
        self.assertTrue(fixture['gate_ran'])
        self.assertTrue(any(row['source'] == 'adversarial-gate'
                            for row in fixture['finding_ledger']))
        source_state = (source / 'state.json').read_bytes()
        source_ledger = (source / 'findings-ledger.json').read_bytes()
        self.run_dir = self.root / 'copied-old-run'
        shutil.copytree(source, self.run_dir)
        state_path = self.run_dir / 'state.json'
        state = json.loads(state_path.read_text())
        original_turns = len(state['turns'])
        author_session = state['sessions']['author']
        reviewer_session = state['sessions']['reviewer']
        state['workspace'] = str(self.workspace)
        state['workitem'] = str(self.workitem)
        state['base_commit'] = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=self.workspace, check=True,
            text=True, stdout=subprocess.PIPE).stdout.strip()
        state['finding_ledger'] = [row for row in state['finding_ledger']
                                   if row['source'] != 'adversarial-gate']
        state['config']['codex_bin'] = str(self.fake_codex_cli())
        state['config']['claude_bin'] = str(self.fake_claude_cli())
        del state['config']['author_subagents']  # older runs predate this key
        rc.atomic_json(state_path, state)
        probe_command = self.command(*fixture_options)
        probe_command[2] = 'permission-probe'
        probe_result = subprocess.run(probe_command, cwd=self.root, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(probe_result.returncode, 0, probe_result.stdout + probe_result.stderr)
        command = self.command(*fixture_options, '--polish')
        command[2] = 'resume'
        result = subprocess.run(command, cwd=self.root, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        final = json.loads(state_path.read_text())
        self.assertEqual(final['status'], 'DONE')
        self.assertEqual(final['sessions']['author'], author_session)
        self.assertEqual(final['sessions']['reviewer'], reviewer_session)
        self.assertEqual(len(final['turns']), original_turns + 4)
        self.assertTrue(any(row['source'] == 'adversarial-gate'
                            for row in final['finding_ledger']))
        self.assertTrue(final['polish']['completed'])
        self.assertTrue((self.run_dir / 'open-findings.md').exists())
        self.assertEqual((source / 'state.json').read_bytes(), source_state)
        self.assertEqual((source / 'findings-ledger.json').read_bytes(), source_ledger)

    def test_large_observed_output_is_not_delivered_and_round_render_is_bounded(self):
        result = self.run_coordinator('--exercise-revisions', '--shadow', 'off',
                                      '--adversarial-gate', 'off',
                                      env={'FAKE_HUGE_OUTPUT': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        revision_prompts = [path.read_text() for path in
                            (self.run_dir / 'evidence').glob('*-author.prompt.txt')
                            if 'Delivered review:' in path.read_text()]
        self.assertTrue(revision_prompts)
        self.assertLess(max(map(len, revision_prompts)), 10000)
        self.assertTrue(all('observed_commands' not in prompt and 'line\nline\nline' not in prompt
                            for prompt in revision_prompts))
        reviewer_rounds = list((self.run_dir / 'rounds').glob('*-supervisor-*.md'))
        self.assertTrue(any('(truncated, full output in evidence/' in path.read_text()
                            for path in reviewer_rounds))
        self.assertLess(max(path.stat().st_size for path in reviewer_rounds), 10000)
        receipts = list((self.run_dir / 'evidence').glob('*-reviewer.receipt.json'))
        self.assertTrue(any(path.stat().st_size > 50000 for path in receipts))

    def test_missing_ledger_disposition_retries_once_then_holds(self):
        retry = self.run_coordinator('--exercise-revisions', '--shadow', 'off',
                                     '--adversarial-gate', 'off',
                                     env={'FAKE_OMIT_DISPOSITION': '1'})
        self.assertEqual(retry.returncode, 0, retry.stderr + retry.stdout)
        prompts = [path.read_text() for path in (self.run_dir / 'evidence').glob('*-reviewer.prompt.txt')]
        self.assertEqual(sum('one allowed protocol retry' in prompt for prompt in prompts), 3)

        self.run_dir = self.root / 'always-omit'
        held = self.run_coordinator('--exercise-revisions', '--shadow', 'off',
                                    '--adversarial-gate', 'off',
                                    env={'FAKE_ALWAYS_OMIT_DISPOSITION': '1'})
        self.assertEqual(held.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertIn('omitted open finding dispositions after retry', state['hold_reason'])

    def test_ledger_comparison_and_per_turn_usage_are_terminal_artifacts(self):
        result = self.run_coordinator('--exercise-revisions', env={'FAKE_GATE_BLOCK': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        ledger = json.loads((self.run_dir / 'findings-ledger.json').read_text())
        self.assertGreaterEqual(len(ledger), 3)
        self.assertEqual([row['id'] for row in ledger],
                         [f'F{index:03d}' for index in range(1, len(ledger) + 1)])
        self.assertTrue(all(row['status_history'] for row in ledger))
        self.assertTrue((self.run_dir / 'findings-ledger.md').exists())
        comparison = (self.run_dir / 'review-comparison.md').read_text()
        self.assertIn('EXEC round 1', comparison)
        self.assertIn('| persistent |', comparison)
        self.assertIn('| shadow |', comparison)
        self.assertIn('| gate |', comparison)
        usage = json.loads((self.run_dir / 'usage.json').read_text())
        self.assertEqual(len(usage['turns']), usage['total_cli_turns'])
        usage_md = (self.run_dir / 'usage.md').read_text()
        self.assertIn('## Per turn', usage_md)
        self.assertIn('| Turn | Role | Phase | Budget counted | Error kind | Reset hint | Requests |', usage_md)

    def test_full_fake_run_forces_both_revisions_and_shadow(self):
        result = self.run_coordinator('--exercise-revisions')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE')
        self.assertEqual((state['plan_rounds'], state['exec_rounds']), (2, 2))
        self.assertEqual((state['plan_reviews'], state['exec_reviews']), (2, 2))
        self.assertTrue(state['gate_ran'])
        self.assertEqual(len(state['turns']), 13)
        self.assertEqual(sum(t['role'] == 'shadow' for t in state['turns']), 2)
        self.assertEqual(sum(t['role'] == 'gate' for t in state['turns']), 1)
        self.assertIn('type(x) is int', (self.workspace / 'sum_ints.py').read_text())
        self.assertIn('sum_ints.py', (self.run_dir / 'context' / 'delta.patch').read_text())
        self.assertIn('sum_ints.py', (self.run_dir / 'context' / 'status.txt').read_text())
        self.assertTrue((self.run_dir / 'context' / 'delta.stat').exists())
        self.assertTrue((self.run_dir / 'context' / 'delta-since-last-review.patch').exists())
        self.assertEqual(len(list((self.run_dir / 'rounds').glob('*.md'))), 13)
        prompts = list((self.run_dir / 'evidence').glob('*.prompt.txt'))
        self.assertTrue(prompts)
        self.assertLessEqual(max(len(path.read_text().splitlines()) for path in prompts), 60)
        self.assertTrue(all('Reviewed snapshot must be exactly' not in path.read_text() for path in prompts))
        fresh_prompts = [path.read_text() for path in prompts
                         if '-shadow.prompt.' in path.name or '-gate.prompt.' in path.name]
        self.assertTrue(fresh_prompts)
        self.assertTrue(all(not __import__('re').search(r'\bF\d{3,}\b', text)
                            for text in fresh_prompts))
        fresh_schemas = [json.loads(path.read_text()) for path in
                         (self.run_dir / 'evidence').glob('*-shadow.schema.json')]
        self.assertTrue(all('prior_findings' not in schema['properties']
                            for schema in fresh_schemas))
        usage = json.loads((self.run_dir / 'usage.json').read_text())
        self.assertEqual(usage['waiting_model_calls'], 0)
        self.assertEqual(usage['total_cli_turns'], 13)
        self.assertIn('author', usage['by_role'])
        self.assertEqual(usage['overall']['cli_turns'], 13)
        self.assertIn('uncached_input_tokens', usage['overall'])
        usage_md = (self.run_dir / 'usage.md').read_text()
        self.assertIn('Uncached input', usage_md)
        self.assertIn('author/TOTAL', usage_md)
        self.assertIn('OVERALL', usage_md)
        self.assertTrue(any(e['command'] == 'python3 -m unittest' for
                            t in state['turns'] if t.get('answer') for e in t['answer'].get('self_run_evidence', [])))
        approvals = [t for t in state['turns'] if t['phase'] == 'EXEC' and
                     t.get('answer', {}).get('status') == 'APPROVE']
        self.assertTrue(approvals)
        self.assertTrue(all(t['answer']['observed_commands'] for t in approvals))
        round_text = '\n'.join(p.read_text() for p in (self.run_dir / 'rounds').glob('*.md'))
        self.assertIn('Observed Commands', round_text)
        self.assertIn('Claimed Self Run Evidence', round_text)
        for role in ('shadow', 'gate'):
            files = list((self.run_dir / 'evidence').glob(f'*-{role}.independence-inputs.json'))
            self.assertTrue(files, f'missing {role} launch audit')
            inputs = json.loads(max(files, key=lambda path: path.stat().st_mtime_ns).read_text())['inputs']
            plan = inputs['context/plan.md']['content']
            self.assertIn('Verification: run unittest', plan)
            self.assertNotIn('REVISE', plan)
            materialized = '\n'.join(row['content'] for row in inputs.values())
            for leaked in ('F001', 'required exercise revision', 'findings-ledger.md',
                           'Open finding ledger', 'Delivered review:',
                           '"status":"REVISE"', '"status":"APPROVE"',
                           'reviewer-REVISE', 'reviewer-APPROVE'):
                self.assertNotIn(leaked, materialized, f'{role} saw {leaked}')
        codex_receipts = [turn for turn in state['turns'] if turn['vendor'] == 'codex']
        claude_receipts = [turn for turn in state['turns'] if turn['vendor'] == 'claude']
        self.assertTrue(codex_receipts and claude_receipts)
        self.assertTrue(all(turn['reported_model'] == 'gpt-6-luna' and
                            turn['reported_model_source'] == 'thread.started' and
                            turn['model_identity'] == 'MATCH' for turn in codex_receipts))
        self.assertTrue(all(turn['reported_model'] == 'claude-opus-5-5' and
                            turn['reported_model_source'] == 'session-init' and
                            turn['model_identity'] == 'MATCH' for turn in claude_receipts))

    def test_model_identity_status_requires_exact_id_or_explicit_date_suffix(self):
        requested = 'claude-opus-5-5'
        self.assertEqual(rc.model_identity_status(requested, requested), 'MATCH')
        self.assertEqual(rc.model_identity_status(requested, requested + '-20260925'), 'MATCH')
        for reported in ('claude-opus-5', 'claude-opus-5-50'):
            self.assertEqual(rc.model_identity_status(requested, reported), 'MISMATCH')
        self.assertEqual(rc.model_identity_status(requested, '<synthetic>'), 'UNREPORTED')
        self.assertEqual(rc.model_identity_status('', requested), 'UNREPORTED')

    def test_model_identity_malformed_stream_and_subagent_are_unreported_or_ignored(self):
        malformed = self.root / 'malformed-model.jsonl'
        malformed.write_text('{broken json\n{"type":"thread.started","model":"gpt-6-luna"}\n')
        self.assertEqual(rc.reported_provider_model('codex', malformed), (None, None))
        claude = self.root / 'claude-subagent-model.jsonl'
        claude.write_text('\n'.join([
            json.dumps({'type': 'system', 'subtype': 'init', 'model': '<synthetic>'}),
            json.dumps({'type': 'assistant', 'parent_tool_use_id': 'tool-1',
                        'message': {'model': 'claude-opus-5-5'}}),
        ]) + '\n')
        self.assertEqual(rc.reported_provider_model('claude', claude), (None, None))

    def test_model_identity_mismatch_is_recorded_and_malformed_stream_is_unreported(self):
        cases = (
            ('mismatch', {'FAKE_CODEX_MODEL': 'gpt-6-astra'}, 'codex', 'MISMATCH',
             'thread.started'),
            ('malformed-claude', {'FAKE_MALFORMED_MODEL_STREAM': 'claude'},
             'claude', 'UNREPORTED', None),
            ('synthetic-claude', {'FAKE_CLAUDE_MODEL': '<synthetic>'},
             'claude', 'UNREPORTED', None),
        )
        for name, env, vendor, expected, source in cases:
            with self.subTest(name=name):
                self.run_dir = self.root / ('model-' + name)
                result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', env=env)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                state = json.loads((self.run_dir / 'state.json').read_text())
                receipts = [row for row in state['turns'] if row['vendor'] == vendor]
                self.assertTrue(receipts)
                self.assertTrue(all(row['model_identity'] == expected for row in receipts))
                if source:
                    self.assertTrue(all(row['reported_model_source'] == source for row in receipts))

    def test_permission_probe_runs_exact_allowed_and_checks_writes(self):
        command = self.command('--exercise-revisions')
        command[2] = 'permission-probe'
        sandbox_log = self.root / 'codex-sandbox-invocations.jsonl'
        result = subprocess.run(command, cwd=self.root,
                                env={**os.environ, 'FAKE_CODEX_SANDBOX_MODE': 'deny',
                                     'FAKE_CODEX_SANDBOX_LOG': str(sandbox_log)}, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['author_permission_probe']['status'], 'PASS')
        self.assertTrue(all(report['author_permission_probe']['outcomes'].values()))
        self.assertTrue(report['author_permission_probe']['outcomes']['run_tmpdir_write_allowed'])
        model_checks = report['author_permission_probe']['model_escape_checks']
        expected_model_checks = {'external_tmpdir', 'slash_tmp', 'home', 'workspace_parent'}
        if sys.platform == 'darwin':
            expected_model_checks.add('private_tmp')
        self.assertEqual(set(model_checks), expected_model_checks)
        self.assertTrue(all(row['status'] == 'PASS' and not row['target_exists_after_turn']
                            for row in model_checks.values()))
        sandbox_checks = report['author_permission_probe']['codex_sandbox_checks']
        self.assertEqual(sandbox_checks['status'], 'PASS')
        self.assertEqual(set(sandbox_checks['checks']), {
            'workspace_write_allowed', 'run_tmpdir_write_allowed',
            'external_tmpdir_denied', 'slash_tmp_denied'})
        self.assertTrue(all(sandbox_checks['checks'][name]['policy_observed']
                            and sandbox_checks['checks'][name]['target_present_after_command']
                            and sandbox_checks['checks'][name]['cleanup_ok']
                            for name in ('workspace_write_allowed', 'run_tmpdir_write_allowed')))
        self.assertTrue(all(sandbox_checks['checks'][name]['policy_observed']
                            and sandbox_checks['checks'][name]['os_denial_observed']
                            and sandbox_checks['checks'][name]['target_absent_before_cleanup']
                            and sandbox_checks['checks'][name]['cleanup_ok']
                            for name in ('external_tmpdir_denied', 'slash_tmp_denied')))
        invocations = [json.loads(line) for line in sandbox_log.read_text().splitlines()]
        self.assertEqual(len(invocations), 4)
        expected_tmpdir = self.run_dir / 'author-tmp'
        for invocation in invocations:
            self.assertIn('--log-denials', invocation['args'])
            self.assertEqual(invocation['tmpdir'], str(expected_tmpdir))
            configs = [invocation['args'][i + 1] for i, arg in enumerate(invocation['args'][:-1])
                       if arg == '-c']
            self.assertIn('sandbox_mode="workspace-write"', configs)
            self.assertIn('sandbox_workspace_write.writable_roots=' +
                          json.dumps([str(expected_tmpdir)]), configs)
            self.assertIn('sandbox_workspace_write.exclude_tmpdir_env_var=false', configs)
            self.assertIn('sandbox_workspace_write.exclude_slash_tmp=true', configs)
        self.assertEqual(report['global_config_changes']['status'], 'PASS')
        probe_args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
                                             '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
                                             '--author-effort', 'low', '--reviewer-effort', 'low',
                                             '--gate-effort', 'low', '--test-command', 'python3 -m unittest',
                                             '--timeout', '10', '--exercise-revisions',
                                             '--codex-bin', str(self.fake_codex_cli()),
                                             '--claude-bin', str(self.fake_claude_cli())])
        self.assertEqual(report['author_flags_digest'], rc.Coordinator(probe_args).author_flags_digest())
        state = json.loads((self.run_dir / 'state.json').read_text())
        author_probe_turns = [row for row in state['turns']
                              if row['role'] == 'author' and row['phase'] == 'AUTHOR_PERMISSION_PROBE']
        self.assertEqual(len(author_probe_turns), 1)
        author_probe_command = author_probe_turns[0]['command']
        self.assertIn('--ignore-rules', author_probe_command)
        self.assertIn('sandbox_mode="workspace-write"', author_probe_command)
        expected_tmpdir = rc.Coordinator(probe_args).author_temp_dir
        self.assertIn('sandbox_workspace_write.writable_roots=' +
                      json.dumps([str(expected_tmpdir)]), author_probe_command)
        for setting in ('sandbox_workspace_write.exclude_tmpdir_env_var=false',
                        'sandbox_workspace_write.exclude_slash_tmp=true'):
            self.assertIn(setting, author_probe_command)
        self.assertEqual(author_probe_turns[0]['environment_overrides'],
                         {'TMPDIR': str(expected_tmpdir)})
        self.assertTrue(report['allowed_command_ran'])
        self.assertEqual(len(report['write_attempts_denied']), 10)
        self.assertTrue(all(report['write_attempts_denied'].values()))
        self.assertEqual(set(report['write_attempt_outcomes'].values()), {'denied'})
        self.assertEqual(report['failure_reasons'], [])
        self.assertTrue(report['snapshot_unchanged'])
        probe_prompt = next((self.run_dir / 'evidence').glob('*-probe-probe.prompt.txt')).read_text()
        self.assertIn('authorized test of the harness', probe_prompt)
        self.assertIn('MUST attempt every command', probe_prompt)
        self.assertIn('Exactly one dedicated run-directory touch command', probe_prompt)
        self.assertNotIn('read-only permission probe', probe_prompt)
        main = self.run_coordinator('--exercise-revisions', skip_probe=False)
        self.assertEqual(main.returncode, 0, main.stderr + main.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(len(state['turns']), 15)
        usage = json.loads((self.run_dir / 'usage.json').read_text())
        self.assertIn('probe', usage['by_role'])
        self.assertEqual(usage['by_role']['probe']['cli_turns'], 1)

    def test_author_model_escape_core_statuses_and_cleanup(self):
        cases = (
            ('write-home', 'FAKE_AUTHOR_ESCAPE_WRITE', 'home', 'FAIL', True),
            ('write-parent', 'FAKE_AUTHOR_ESCAPE_WRITE', 'workspace_parent', 'FAIL', True),
            ('write-external', 'FAKE_AUTHOR_ESCAPE_WRITE', 'external_tmpdir', 'FAIL', True),
            ('write-delete', 'FAKE_AUTHOR_ESCAPE_WRITE_THEN_DELETE', 'home', 'FAIL', False),
            ('skip', 'FAKE_AUTHOR_ESCAPE_SKIP', 'home', 'UNKNOWN', False),
            ('malformed', 'FAKE_AUTHOR_ESCAPE_MALFORMED', 'home', 'UNKNOWN', False),
            ('no-os-marker', 'FAKE_AUTHOR_ESCAPE_NO_OS', 'home', 'UNKNOWN', False),
        )
        for name, setting, label, expected, found_expected in cases:
            with self.subTest(name=name):
                self.run_dir = self.root / ('escape-core-' + name)
                co = self.coordinator()
                with patch.dict(os.environ, {setting: label, 'FAKE_CODEX_SANDBOX_MODE': 'deny'}):
                    result = co._author_permission_probe()
                self.assertEqual(result['status'], 'PASS_RESIDUAL_RISK' if expected == 'UNKNOWN' else expected, result)
                self.assertEqual(result['model_escape_checks'][label]['status'], expected)
                found = result['model_escape_targets_found']
                self.assertEqual(bool(found), found_expected)
                self.assertEqual(found, result['model_escape_targets_cleaned'])
                self.assertTrue(all(not Path(path).exists() for path in found))

    def test_dedicated_escape_directory_evidence_and_cleanup(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_ESCAPE_SKIP': 'home'}):
            report = co._author_permission_probe()
        self.assertEqual(report['status'], 'PASS_RESIDUAL_RISK')
        for row in report['model_escape_checks'].values():
            before, after = row['directory_before'], row['directory_after']
            self.assertEqual(before, after)
            self.assertEqual(before['listing'], [])
            self.assertTrue(all(key in before for key in ('st_mtime_ns', 'st_ctime_ns', 'st_nlink')))
        self.assertEqual(len(report['escape_directory_cleanup']['removed']), 4)
        self.assertFalse(report['escape_directory_cleanup']['retained'])
        self.assertTrue(all(not Path(path).exists() for path in report['escape_directory_cleanup']['removed']))

    def test_escape_directory_cleanup_preserves_foreign_file(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_ESCAPE_FOREIGN': 'home'}):
            report = co._author_permission_probe()
        self.assertEqual(report['status'], 'FAIL')
        root = Path(report['model_escape_checks']['home']['target']).parent
        self.assertIn(str(root), report['escape_directory_cleanup']['retained'])
        self.assertEqual((root / 'foreign-file').read_text(), 'not a coordinator sentinel\n')

    def test_escape_directory_write_delete_with_denial_is_unknown(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_ESCAPE_WRITE_DELETE_DENIED': 'home'}):
            report = co._author_permission_probe()
        row = report['model_escape_checks']['home']
        self.assertEqual(row['directory_before']['listing'], row['directory_after']['listing'])
        self.assertNotEqual((row['directory_before']['st_mtime_ns'], row['directory_before']['st_ctime_ns']),
                            (row['directory_after']['st_mtime_ns'], row['directory_after']['st_ctime_ns']))
        self.assertEqual(row['status'], 'UNKNOWN')
        self.assertEqual(report['status'], 'UNKNOWN')
        self.assertFalse(report['model_escape_targets_found'])

    def test_author_model_escape_probe_fail_unknown_and_cleanup(self):
        for mode, setting, expected in (
                ('write', 'FAKE_AUTHOR_ESCAPE_WRITE', 'FAIL'),
                ('skip', 'FAKE_AUTHOR_ESCAPE_SKIP', 'UNKNOWN'),
                ('malformed', 'FAKE_AUTHOR_ESCAPE_MALFORMED', 'UNKNOWN')):
            with self.subTest(mode=mode):
                self.run_dir = self.root / ('escape-' + mode)
                command = self.command()
                command[2] = 'permission-probe'
                result = subprocess.run(command, cwd=self.root,
                    env={**os.environ, setting: 'home', 'FAKE_CODEX_SANDBOX_MODE': 'deny'},
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(result.returncode, 0 if expected == 'UNKNOWN' else 2,
                                 result.stdout + result.stderr)
                report = json.loads((self.run_dir / 'permission-probe.json').read_text())
                author = report['author_permission_probe']
                self.assertEqual(author['status'], 'PASS_RESIDUAL_RISK' if expected == 'UNKNOWN' else expected)
                self.assertEqual(author['model_escape_checks']['home']['status'], expected)
                self.assertEqual(report['status'], 'PASS_RESIDUAL_RISK' if expected == 'UNKNOWN' else expected)
                if mode == 'write':
                    found = author['model_escape_targets_found']
                    self.assertEqual(len(found), 1)
                    self.assertEqual(found, author['model_escape_targets_cleaned'])
                    self.assertFalse(Path(found[0]).exists())
                    state = json.loads((self.run_dir / 'state.json').read_text())
                    self.assertEqual(state['status'], 'HOLD')
                    self.assertIn(found[0], state['hold_reason'])
                else:
                    self.assertEqual(author['model_escape_targets_found'], [])

    def test_author_model_escape_duplicate_success_and_reviewer_fail_precedence(self):
        cases = (
            ('duplicate-success', {'FAKE_AUTHOR_ESCAPE_WRITE_THEN_DELETE': 'home',
                                   'FAKE_AUTHOR_ESCAPE_DUPLICATE': 'home'}, 'FAIL'),
            ('reviewer-fail', {'FAKE_AUTHOR_ESCAPE_SKIP': 'home',
                               'FAKE_REVIEW_NO_TEST_EVENT': '1'}, 'FAIL'),
            ('no-os-marker', {'FAKE_AUTHOR_ESCAPE_NO_OS': 'home'}, 'UNKNOWN'),
            ('clarify', {'FAKE_AUTHOR_ESCAPE_SKIP': 'home',
                         'PAIRED_SESSION_PROBE_CLARIFY': '1'}, 'UNKNOWN'),
        )
        for name, settings, expected in cases:
            with self.subTest(name=name):
                self.run_dir = self.root / ('escape-extra-' + name)
                command = self.command(); command[2] = 'permission-probe'
                result = subprocess.run(command, cwd=self.root,
                    env={**os.environ, **settings, 'FAKE_CODEX_SANDBOX_MODE': 'deny'},
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                overall = 'PASS_RESIDUAL_RISK' if expected == 'UNKNOWN' else expected
                self.assertEqual(result.returncode, 0 if overall == 'PASS_RESIDUAL_RISK' else 2,
                                 result.stdout + result.stderr)
                report = json.loads((self.run_dir / 'permission-probe.json').read_text())
                self.assertEqual(report['status'], overall)
                if name == 'duplicate-success':
                    self.assertEqual(report['author_permission_probe']['model_escape_checks']['home']['status'], 'FAIL')
                    self.assertEqual(report['author_permission_probe']['model_escape_targets_found'], [])
                    self.assertIn('1C FAIL', json.loads((self.run_dir / 'state.json').read_text())['hold_reason'])
                if name == 'clarify':
                    prompt = next((self.run_dir / 'evidence').glob('*-author_permission_probe-author.prompt.txt'))
                    self.assertIn('Clarification:', prompt.read_text())

    def test_claude_permission_probe_requires_observed_os_sandbox_denial_and_cleans_escape(self):
        command = self.command('--author-vendor', 'codex', '--reviewer-vendor', 'claude')
        command[2] = 'permission-probe'
        invocation_log = self.root / 'fake-claude-invocation.json'
        passing = subprocess.run(command, cwd=self.root,
                                 env={**os.environ, 'FAKE_CLAUDE_INVOCATION_LOG': str(invocation_log)},
                                 text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        self.assertEqual(passing.returncode, 0, passing.stderr + passing.stdout)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'PASS')
        self.assertTrue(report['claude_sandbox_write_denied'])
        self.assertEqual(report['claude_os_denial_probe']['status'], 'PASS')
        self.assertTrue(report['claude_os_denial_probe']['command'].startswith('/usr/bin/touch '))
        os_target = Path(report['claude_os_denial_probe']['target'])
        self.assertNotIn(self.run_dir, os_target.parents)
        self.assertFalse(os_target.exists())
        invocation = json.loads(invocation_log.read_text())
        settings = json.loads(invocation[invocation.index('--settings') + 1])
        self.assertIn(str(os_target), settings['sandbox']['filesystem']['denyWrite'])
        self.assertEqual(settings['permissions']['deny'],
                         [f'Edit(//{self.run_dir.as_posix().lstrip("/")}/**)'])
        allowed = allowed_tool_values(invocation)
        bash_allowed = '\n'.join(value for value in allowed if value.startswith('Bash('))
        self.assertEqual(bash_allowed.count('Bash(touch '), 1)
        self.assertIn('.paired-session-run-dir-probe-', bash_allowed)
        self.assertIn('.paired-session-os-probe-', bash_allowed)
        self.assertNotIn('paired-session-claude-sandbox-', bash_allowed)
        self.assertNotIn('.paired-session-context-probe-', bash_allowed)
        transient = [row for row in report['observed_commands']
                     if ('paired-session-claude-sandbox-' in row.get('command', '') or
                     '.paired-session-run-dir-probe-' in row.get('command', '') or
                     '.paired-session-context-probe-' in row.get('command', ''))]
        self.assertEqual(len(transient), 3)
        self.assertTrue(all(row['error'] for row in transient))
        self.assertNotIn(json.dumps(transient), json.dumps(report['reviewer_flags']))
        config = json.loads((self.run_dir / 'state.json').read_text())['config']
        self.assertNotIn(json.dumps(transient), json.dumps(config))
        self.assertEqual(set(report['claude_sandbox_write_denials']), {'host_tmp', 'run_dir', 'context'})
        checks = report['claude_sandbox_write_denials']
        self.assertTrue(checks['run_dir']['failed_at_os_sandbox'])
        self.assertFalse(checks['run_dir']['cli_permission_denied'])
        for label in ('host_tmp', 'context'):
            self.assertTrue(checks[label]['cli_permission_denied'])
            self.assertFalse(checks[label]['failed_at_os_sandbox'])
        for row in transient:
            import shlex
            parts = shlex.split(row['command'])
            target = Path(parts[-1] if parts[0] == 'touch' else parts[parts.index('>') + 1])
            self.assertFalse(target.exists())

        self.run_dir = self.root / 'sandbox-escape-run'
        command = self.command('--author-vendor', 'codex', '--reviewer-vendor', 'claude')
        command[2] = 'permission-probe'
        escaped = subprocess.run(command, cwd=self.root, env={**os.environ, 'FAKE_SANDBOX_WRITE': '1'},
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(escaped.returncode, 2)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['claude_os_denial_probe']['status'], 'FAIL')
        self.assertFalse(report['claude_sandbox_write_denied'])

        self.assertIn('claude_sandbox_write_denials', report)
        self.assertFalse(report['claude_sandbox_write_denials']['run_dir']['failed_at_os_sandbox'])
        self.assertTrue(any(reason.startswith('claude-sandbox-') and 'write-not-denied-at-os' in reason
                            for reason in report['failure_reasons']))
        escaped_rows = [row for row in report['observed_commands']
                        if ('paired-session-claude-sandbox-' in row.get('command', '') or
                        '.paired-session-run-dir-probe-' in row.get('command', '') or
                        '.paired-session-context-probe-' in row.get('command', ''))]
        self.assertEqual(len(escaped_rows), 3)
        import shlex
        def target_path(command):
            parts = shlex.split(command)
            return Path(parts[-1] if parts[0] == 'touch' else parts[parts.index('>') + 1])
        escaped_paths = [target_path(row['command']) for row in escaped_rows]
        self.assertTrue(all(not path.exists() for path in escaped_paths),
                        'coordinator must clean only its unique transient probe files')

        self.run_dir = self.root / 'sandbox-mixed-denial-run'
        command = self.command('--author-vendor', 'codex', '--reviewer-vendor', 'claude')
        command[2] = 'permission-probe'
        mixed = subprocess.run(command, cwd=self.root,
                               env={**os.environ, 'FAKE_CLAUDE_DENY_RUN_DIR_ONLY': '1'},
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(mixed.returncode, 0, mixed.stdout + mixed.stderr)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'PASS')
        self.assertTrue(report['claude_sandbox_write_denials']['run_dir']['cli_permission_denied'])
        self.assertEqual(report['claude_os_denial_probe']['status'], 'PASS')
        self.assertTrue(report['claude_sandbox_write_denied'])
        self.assertEqual(report['claude_flag_semantics'], 'OS-level denial observed for the dedicated OS-only probe')

        self.run_dir = self.root / 'sandbox-no-os-marker-run'
        command = self.command('--author-vendor', 'codex', '--reviewer-vendor', 'claude')
        command[2] = 'permission-probe'
        no_marker = subprocess.run(command, cwd=self.root,
                                   env={**os.environ, 'FAKE_SANDBOX_NO_OS_MARKER': '1'},
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(no_marker.returncode, 2)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['claude_os_denial_probe']['status'], 'UNKNOWN')
        checks = report['claude_sandbox_write_denials']
        self.assertTrue(checks['run_dir']['exact_command_observed_once'] and checks['run_dir']['target_absent'])
        self.assertFalse(checks['run_dir']['failed_at_os_sandbox'])

        self.run_dir = self.root / 'sandbox-cli-layer-only-run'
        command = self.command('--author-vendor', 'codex', '--reviewer-vendor', 'claude')
        command[2] = 'permission-probe'
        cli_blocked = subprocess.run(command, cwd=self.root,
                                     env={**os.environ, 'FAKE_CLAUDE_DENY_OS_PROBE': '1'},
                                     text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(cli_blocked.returncode, 2)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        dedicated = report['claude_sandbox_write_denials']['run_dir']
        self.assertEqual(report['status'], 'UNKNOWN')
        self.assertEqual(report['claude_os_denial_probe']['status'], 'UNKNOWN')
        self.assertTrue(dedicated['cli_permission_denied'])
        self.assertFalse(dedicated['failed_at_os_sandbox'])
        self.assertFalse(report['claude_sandbox_write_denied'])

        self.run_dir = self.root / 'sandbox-more-than-one-allowlisted-run'
        command = self.command('--author-vendor', 'codex', '--reviewer-vendor', 'claude')
        command[2] = 'permission-probe'
        extra_allowed = subprocess.run(command, cwd=self.root,
                                       env={**os.environ, 'FAKE_CLAUDE_ALLOW_OTHER_PROBE': '1'},
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(extra_allowed.returncode, 2)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        for label in ('host_tmp', 'context'):
            self.assertFalse(report['claude_sandbox_write_denials'][label]['cli_permission_denied'])
            self.assertTrue(any(f'claude-sandbox-{label}-write-not-cli-blocked' in reason
                                for reason in report['failure_reasons']))

    def test_claude_probe_runtime_error_records_escape_before_cleanup(self):
        co = self.coordinator('--author-vendor', 'codex', '--reviewer-vendor', 'claude')
        created = []

        def escape_then_fail(role, phase, prompt, schema, **kwargs):
            import shlex
            # Extract the literal unique write commands from the generated probe.
            for line in prompt.splitlines():
                if line.startswith('printf probe > ') or line.startswith('touch '):
                    target = Path(shlex.split(line)[-1])
                    target.write_text('escaped')
                    created.append(target)
            raise RuntimeError('simulated probe failure')

        with patch.object(co, 'invoke', side_effect=escape_then_fail):
            self.assertFalse(co.permission_probe())
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn('claude-sandbox-probe-write-escaped', report['failure_reasons'])
        self.assertEqual(len(created), 3)
        self.assertEqual(report['claude_sandbox_escape_targets_found'], [str(path) for path in created])
        self.assertEqual(report['claude_sandbox_escape_targets_cleaned'], [str(path) for path in created])
        self.assertEqual(report['claude_sandbox_escape_targets_remaining'], [])
        self.assertEqual(set(report['claude_sandbox_write_denials']), {'host_tmp', 'run_dir', 'context'})
        self.assertTrue(all(not check['target_absent']
                            for check in report['claude_sandbox_write_denials'].values()))
        self.assertTrue(all(not path.exists() for path in created))

    def test_claude_probe_reports_a_target_that_cleanup_could_not_remove(self):
        co = self.coordinator('--author-vendor', 'codex', '--reviewer-vendor', 'claude')
        created = []

        def escape_then_fail(role, phase, prompt, schema, **kwargs):
            import shlex
            for line in prompt.splitlines():
                if line.startswith('printf probe > ') or line.startswith('touch '):
                    target = Path(shlex.split(line)[-1])
                    target.write_text('escaped')
                    created.append(target)
            raise RuntimeError('simulated probe failure')

        real_unlink = Path.unlink

        def fail_first_cleanup(path, *args, **kwargs):
            if created and path == created[0]:
                raise OSError('simulated cleanup denial')
            return real_unlink(path, *args, **kwargs)

        try:
            with patch.object(co, 'invoke', side_effect=escape_then_fail):
                with patch.object(Path, 'unlink', new=fail_first_cleanup):
                    self.assertFalse(co.permission_probe())
            report = json.loads((self.run_dir / 'permission-probe.json').read_text())
            self.assertEqual(report['claude_sandbox_escape_targets_found'], [str(path) for path in created])
            self.assertEqual(report['claude_sandbox_escape_targets_cleaned'], [str(path) for path in created[1:]])
            self.assertEqual(report['claude_sandbox_escape_targets_remaining'], [str(created[0])])
            self.assertEqual(report['claude_sandbox_cleanup_errors'], [
                {'path': str(created[0]), 'error': 'OSError'}])
            self.assertIn('claude-sandbox-probe-cleanup-incomplete', report['failure_reasons'])
        finally:
            if created:
                real_unlink(created[0], missing_ok=True)

    def test_author_permission_probe_requires_unique_structured_exit_events(self):
        for mode in ('missing-exit', 'duplicate-event'):
            with self.subTest(mode=mode):
                args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
                    '--workitem', str(self.workitem), '--run-dir', str(self.root / ('probe-' + mode)),
                    '--codex-bin', str(self.fake_codex_cli()), '--author-vendor', 'codex'])
                co = rc.Coordinator(args)

                def simulate(role, phase, prompt, schema, **kwargs):
                    self.assertEqual((role, phase), ('author', 'AUTHOR_PERMISSION_PROBE'))
                    commands = json.loads(prompt.split('PROBE_COMMANDS_JSON: ', 1)[1].splitlines()[0])
                    import shlex
                    for command in commands[:2]:
                        parts = shlex.split(command)
                        target_arg = parts[parts.index('>') + 1]
                        target = (co.author_temp_dir / target_arg[len('$TMPDIR/'):]
                                  if target_arg.startswith('$TMPDIR/') else Path(target_arg))
                        target.write_text('probe\n')
                    rows = [{'command': command,
                             'exit_code': (None if mode == 'missing-exit' and index == 1 else
                                           0 if index < 2 else 126),
                             'error': index >= 2}
                            for index, command in enumerate(commands)]
                    if mode == 'duplicate-event':
                        rows.append(dict(rows[0]))
                    co.state['turns'].append({'pid': 987654321})
                    return {'answer': {'observed_commands': rows}, 'snapshot': 'scratch-snapshot'}

                with patch.object(co, 'invoke', side_effect=simulate):
                    with patch.object(rc, 'retry_killpg_eperm', side_effect=ProcessLookupError):
                        report = co._author_permission_probe()
                self.assertEqual(report['status'], 'FAIL')
                failed_outcome = ('workspace_write_allowed' if mode == 'duplicate-event'
                                  else 'run_tmpdir_write_allowed')
                self.assertFalse(report['outcomes'][failed_outcome])

    def test_fresh_shadow_critical_is_ledgered_and_delivered_even_if_reviewer_approves(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.root / 'shadow-critical-run'),
            '--shadow', 'on', '--author-vendor', 'codex', '--reviewer-vendor', 'claude'])
        co = rc.Coordinator(args)
        co.state['phase'] = 'EXEC'
        co.state['next'] = 'reviewer'
        co.state['exec_rounds'] = 1
        current = rc.git_snapshot(self.workspace)[0]
        persistent = {'answer': {'status': 'APPROVE', 'prior_findings': [], 'full_review': [],
                                 'self_run_evidence': [{'command': 'python3 -m unittest'}],
                                 'reviewed_snapshot': current},
                      'snapshot': current, 'sequence': 1, 'role': 'reviewer'}
        shadow_finding = {'severity': 'CRITICAL', 'file': 'tracked.txt',
                          'summary': 'fresh-role blocker',
                          'failure_scenario': 'the reviewed behavior remains incorrect'}
        shadow = {'answer': {'status': 'APPROVE', 'full_review': [shadow_finding],
                             'self_run_evidence': []},
                  'snapshot': current, 'sequence': 2, 'role': 'shadow'}

        def fake_invoke(role, *_args, **_kwargs):
            return persistent if role == 'reviewer' else shadow

        with patch.object(co, 'materialize_review_context'), patch.object(co, 'invoke', side_effect=fake_invoke):
            co.reviewer_turn()

        ledger_row = next(row for row in co.state['finding_ledger'] if row['source'] == 'fresh-shadow')
        self.assertEqual(ledger_row['severity'], 'CRITICAL')
        self.assertEqual(ledger_row['status'], 'open')
        self.assertEqual(co.state['next'], 'author')
        delivered = json.loads(co.state['delivered_review'])
        self.assertEqual(delivered['status'], 'REVISE')
        self.assertEqual(delivered['source'], 'persistent-reviewer+fresh-shadow')
        self.assertEqual(delivered['findings'][0]['source'], 'fresh-shadow')
        comparison = co.state['exec_comparisons'][0]
        self.assertEqual(comparison['persistent']['verdict'], 'APPROVE')
        self.assertEqual(comparison['effective_verdict'], 'REVISE')
        co.write_comparison()
        comparison_text = (co.run_dir / 'review-comparison.md').read_text()
        self.assertIn('persistent | APPROVE', comparison_text)
        self.assertIn('Effective workflow verdict: **REVISE**', comparison_text)

    def test_done_and_done_resumes_reject_open_blocking_findings(self):
        for action in ('done', 'resume', 'polish'):
            with self.subTest(action=action):
                args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
                    '--workitem', str(self.workitem), '--run-dir', str(self.root / ('done-' + action))])
                co = rc.Coordinator(args)
                co.state['status'] = 'DONE'
                co.state['finding_ledger'].append({
                    'id': 'F900', 'origin_round': 1, 'phase': 'EXEC',
                    'source': 'fresh-shadow', 'severity': 'CRITICAL', 'file': 'tracked.txt',
                    'summary': 'blocking regression', 'failure_scenario': 'unsafe behavior persists',
                    'body': '', 'status': 'open', 'status_history': []})
                if action == 'done':
                    result = co.done()
                elif action == 'resume':
                    result = co.resume()
                else:
                    result = co.resume_polish()
                self.assertEqual(result, 'HOLD')
                self.assertIn('F900', co.state['hold_reason'])

    def test_run_refuses_pass_probe_bound_to_different_author_flags(self):
        command = self.command()
        command[2] = 'permission-probe'
        probe = subprocess.run(command, cwd=self.root, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(probe.returncode, 0, probe.stderr + probe.stdout)
        report_path = self.run_dir / 'permission-probe.json'
        report = json.loads(report_path.read_text())
        report['author_flags_digest'] = '0' * 64
        rc.atomic_json(report_path, report)
        result = self.run_coordinator(skip_probe=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('author flags do not match', result.stdout)

    def test_failed_mutating_probe_always_writes_report(self):
        command = self.command()
        command[2] = 'permission-probe'
        env = os.environ.copy()
        env['FAKE_PROBE_MUTATE'] = '1'
        result = subprocess.run(command, cwd=self.root, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 2)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'FAIL')
        self.assertFalse(report['snapshot_unchanged'])
        self.assertIn('snapshot-mutated', report['failure_reasons'])

    def test_run_refuses_pass_probe_bound_to_different_reviewer_flags(self):
        command = self.command()
        command[2] = 'permission-probe'
        probe = subprocess.run(command, cwd=self.root, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(probe.returncode, 0, probe.stderr + probe.stdout)
        report_path = self.run_dir / 'permission-probe.json'
        report = json.loads(report_path.read_text())
        report['reviewer_flags_digest'] = '0' * 64
        rc.atomic_json(report_path, report)
        result = self.run_coordinator(skip_probe=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('reviewer flags do not match', result.stdout)

    def test_skip_probe_requires_fake_harness_and_fake_cli_wrappers(self):
        command = self.command('--skip-probe')
        env = os.environ.copy(); env.pop('FAKE_CODEX_TEST_ROOT', None)
        refused = subprocess.run(command, cwd=self.root, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('fake test harness', refused.stdout)
        self.assertFalse((self.run_dir / 'state.json').exists())
        invalid = self.root / 'not-fake-codex'
        invalid.write_text('#!/bin/sh\nexit 0\n'); invalid.chmod(0o755)
        command[command.index('--codex-bin') + 1] = str(invalid)
        refused = subprocess.run(command, cwd=self.root, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('fake test harness', refused.stdout)
        self.assertFalse((self.run_dir / 'state.json').exists())

    def test_resume_refuses_when_permission_probe_is_missing_or_stale(self):
        for mode in ('missing', 'stale'):
            with self.subTest(mode=mode):
                self.run_dir = self.root / ('resume-probe-' + mode)
                result = self.run_coordinator('--stop-after-plan')
                self.assertIn('HOLD', result.stdout)
                if mode == 'stale':
                    probe_command = self.command()
                    probe_command[2] = 'permission-probe'
                    probe = subprocess.run(probe_command, cwd=self.root, text=True,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
                    report_path = self.run_dir / 'permission-probe.json'
                    report = json.loads(report_path.read_text())
                    report['author_flags_digest'] = '0' * 64
                    rc.atomic_json(report_path, report)
                command = self.command()
                command[2] = 'resume'
                resumed = subprocess.run(command, cwd=self.root, text=True,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(resumed.returncode, 2)
                expected = ('permission probe author flags do not match' if mode == 'stale'
                            else 'permission-probe.json is missing')
                self.assertIn('REFUSED: ' + expected, resumed.stdout)
                state = json.loads((self.run_dir / 'state.json').read_text())
                self.assertEqual(state['exec_rounds'], 0)

    def test_resume_config_mismatch_is_refused_without_traceback(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--test-command', 'python3 -m unittest'])
        co = rc.Coordinator(args)
        co.state['config']['author_effort'] = 'high'
        co.save()
        command = self.command('--skip-probe')
        command[2] = 'resume'
        result = subprocess.run(command, cwd=self.root, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 2)
        self.assertIn('REFUSED: resume configuration differs: author_effort', result.stdout)
        self.assertNotIn('Traceback', result.stderr)

    def test_resume_timeout_can_only_increase_to_documented_cap(self):
        co = self.coordinator('--timeout', '10')
        co.state.update(status='HOLD', hold_reason='operator pause')
        co.save()

        def resume_args(*extra):
            args = rc.parser().parse_args(['resume', '--workspace', str(self.workspace),
                '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
                '--timeout', '10', '--codex-bin', str(self.fake_codex_cli()),
                '--claude-bin', str(self.fake_claude_cli()), *extra])
            return args

        for requested in ('9', str(rc.MAX_RESUME_TIMEOUT_SECONDS + 1)):
            with self.subTest(requested=requested):
                with self.assertRaisesRegex(ValueError, '--resume-timeout must be between'):
                    rc.Coordinator(resume_args('--resume-timeout', requested))

        resumed = rc.Coordinator(resume_args('--resume-timeout', str(rc.MAX_RESUME_TIMEOUT_SECONDS)))
        self.assertEqual(resumed.args.timeout, rc.MAX_RESUME_TIMEOUT_SECONDS)
        with patch.object(resumed, 'drive', return_value='HOLD'):
            self.assertEqual(resumed.resume(), 'HOLD')
        saved = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(saved['config']['timeout'], rc.MAX_RESUME_TIMEOUT_SECONDS)

    def test_resume_timeout_override_is_rejected_for_run_action(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--resume-timeout', '20'])
        with self.assertRaisesRegex(ValueError, '--resume-timeout is accepted only with resume'):
            rc.Coordinator(args)

    def test_resume_reuses_recorded_reviewer_verdict_before_state_advances(self):
        co = self.coordinator('--stop-after-plan')
        co.state.update(phase='PLAN', next='reviewer')
        co.save()
        co.materialize_review_context()
        snapshot = rc.git_snapshot(self.workspace)[0]
        prompt = co._review_prompt('reviewer', snapshot)

        # Simulate interruption after the fake CLI verdict receipt is durable,
        # before reviewer_turn advances phase/next.
        first = co.invoke('reviewer', 'PLAN', prompt, rc.review_schema())
        self.assertEqual(first['answer']['status'], 'APPROVE')
        self.assertEqual(co.state['next'], 'reviewer')
        self.assertEqual([row['role'] for row in co.state['turns']].count('reviewer'), 1)

        resumed = rc.Coordinator(co.args)
        self.assertEqual(resumed.resume(), 'HOLD')  # expected --stop-after-plan pause
        self.assertEqual(resumed.state['phase'], 'EXEC')
        self.assertEqual(resumed.state['next'], 'author')
        self.assertEqual(resumed.state['plan_reviews'], 1)
        self.assertEqual([row['role'] for row in resumed.state['turns']].count('reviewer'), 1)

    def test_resume_replaying_partly_applied_reviewer_result_is_idempotent(self):
        co = self.coordinator('--stop-after-plan')
        co.state.update(phase='PLAN', next='reviewer')
        co.save()
        co.materialize_review_context()
        snapshot = rc.git_snapshot(self.workspace)[0]
        with patch.dict(os.environ, {'FAKE_APPROVE_MINOR': '1'}):
            co.invoke('reviewer', 'PLAN', co._review_prompt('reviewer', snapshot), rc.review_schema())

        class SimulatedCrash(BaseException):
            pass

        resumed = rc.Coordinator(co.args)
        capture = resumed.capture_review_baseline

        def save_then_interrupt(sequence, phase):
            capture(sequence, phase)
            raise SimulatedCrash()

        with patch.object(resumed, 'capture_review_baseline', side_effect=save_then_interrupt):
            with self.assertRaises(SimulatedCrash):
                resumed.resume()
        self.assertEqual(resumed.state.get('pending_reviewer_result_sequence'), 1)
        self.assertEqual([row['role'] for row in resumed.state['turns']].count('reviewer'), 1)

        recovered = rc.Coordinator(co.args)
        self.assertEqual(recovered.state.get('pending_reviewer_result_sequence'), 1)
        self.assertEqual([row['role'] for row in recovered.state['turns']].count('reviewer'), 1)
        self.assertIsNotNone(recovered._recorded_reviewer_result('PLAN'))
        self.assertEqual(recovered.resume(), 'HOLD')
        self.assertEqual(recovered.state['plan_reviews'], 1)
        self.assertEqual(recovered.state['reviews_completed'], 1)
        self.assertEqual(len(recovered.state['finding_ledger']), 1)
        self.assertEqual([row['role'] for row in recovered.state['turns']].count('reviewer'), 1)

    def test_exec_resume_reuses_reviewer_and_shadow_receipts(self):
        co = self.coordinator('--shadow', 'on', '--adversarial-gate', 'off', '--polish-round', 'off')
        co.state.update(phase='EXEC', next='reviewer', exec_rounds=1)
        co.save()
        co.materialize_review_context()
        snapshot = rc.git_snapshot(self.workspace)[0]
        co.invoke('reviewer', 'EXEC', co._review_prompt('reviewer', snapshot), rc.review_schema())

        class SimulatedCrash(BaseException):
            pass

        resumed = rc.Coordinator(co.args)
        capture = resumed.capture_review_baseline

        def save_then_interrupt(sequence, phase):
            capture(sequence, phase)
            raise SimulatedCrash()

        with patch.object(resumed, 'capture_review_baseline', side_effect=save_then_interrupt):
            with self.assertRaises(SimulatedCrash):
                resumed.resume()

        recovered = rc.Coordinator(co.args)
        self.assertEqual(recovered.resume(), 'DONE')
        self.assertEqual([row['role'] for row in recovered.state['turns']].count('reviewer'), 1)
        self.assertEqual([row['role'] for row in recovered.state['turns']].count('shadow'), 1)
        self.assertEqual(recovered.state['exec_reviews'], 1)
        self.assertEqual(len(recovered.state['exec_comparisons']), 1)
        self.assertEqual(recovered.state['exec_comparisons'][0]['round'], 1)

    def test_resume_after_reviewer_hold_dispatches_a_fresh_turn(self):
        co = self.coordinator('--stop-after-plan')
        co.state.update(phase='PLAN', next='reviewer')
        co.save()
        co.materialize_review_context()
        snapshot = rc.git_snapshot(self.workspace)[0]
        with patch.dict(os.environ, {'FAKE_REVIEW_HOLD': '1'}):
            co.invoke('reviewer', 'PLAN', co._review_prompt('reviewer', snapshot), rc.review_schema())

        resumed = rc.Coordinator(co.args)
        self.assertEqual(resumed.resume(), 'HOLD')
        self.assertIsNone(resumed.state['pending_reviewer_result_sequence'])
        recovered = rc.Coordinator(co.args)
        self.assertEqual(recovered.resume(), 'HOLD')  # second call approves and honors stop-after-plan
        self.assertEqual([row['role'] for row in recovered.state['turns']].count('reviewer'), 2)

    def test_shadow_receipt_replay_revalidates_verified_claims(self):
        co = self.coordinator()
        co.state['turns'].append({'sequence': 3, 'role': 'shadow', 'phase': 'EXEC',
            'snapshot_before': 'snapshot', 'answer': {'status': 'APPROVE', 'verified_claims': [],
            'full_review': [], 'prior_findings': [], 'self_run_evidence': [{'command': 'test'}]}})
        self.assertIsNone(co._recorded_shadow_result('EXEC', 2))

    def test_gate_finding_import_preserves_each_receipt_index(self):
        co = self.coordinator()
        rc.atomic_json(co.evidence / '007-exec-gate.receipt.json', {
            'sequence': 7, 'answer': {'findings': [
                {'severity': 'medium', 'file': 'a.py', 'recommendation': 'first'},
                {'severity': 'low', 'file': 'b.py', 'recommendation': 'second'}]}})
        co.import_gate_findings()
        imported = [row for row in co.state['finding_ledger'] if row['source'] == 'adversarial-gate']
        self.assertEqual([row['finding_index'] for row in imported], [0, 1])

    def test_resume_promotes_active_receipt_to_uncertain_without_replay(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        active = {'pid': os.getpid(), 'role': 'author', 'phase': 'EXEC', 'sequence': 2}
        co.state['active'] = active
        co.state['hold_reason'] = ''
        co.save()
        with patch.object(co, 'drive') as drive:
            self.assertEqual(co.resume(), 'HOLD')
        drive.assert_not_called()
        self.assertEqual(co.state['uncertain_active'], active)
        self.assertIsNone(co.state['active'])
        self.assertIn('uncertain in-flight', co.state['hold_reason'])
        with patch('os.killpg') as kill_group:
            self.assertEqual(co.resume(retry_uncertain=True), 'HOLD')
        kill_group.assert_called_once_with(os.getpid(), 0)
        drive.assert_not_called()
        self.assertIn('still alive', co.state['hold_reason'])

    def test_cli_interrupt_then_fake_cli_resume_recovers_uncertain_turn(self):
        marker = self.root / 'blocking-fake-started'
        stopped_marker = self.root / 'blocking-fake-stopped'
        blocking_cli = self.root / 'blocking-fake-codex'
        blocking_cli.write_text(
            f'#!{sys.executable}\n'
            'import os, signal, sys, time\n'
            'from pathlib import Path\n'
            'marker = Path(os.environ["FAKE_RECOVERY_MARKER"])\n'
            'stopped = Path(os.environ["FAKE_RECOVERY_STOPPED"])\n'
            'def stop(signum, frame):\n'
            '    stopped.write_text(str(os.getpid()))\n'
            '    os._exit(0)\n'
            'signal.signal(signal.SIGTERM, stop)\n'
            'if not marker.exists():\n'
            '    marker.write_text(str(os.getpid()))\n'
            '    time.sleep(120)\n'
            'else:\n'
            f'    os.execv({sys.executable!r}, [{sys.executable!r}, {str(FAKE)!r}, *sys.argv[1:]])\n'
        )
        blocking_cli.chmod(0o755)
        env = {**os.environ, 'FAKE_RECOVERY_MARKER': str(marker),
               'FAKE_RECOVERY_STOPPED': str(stopped_marker)}
        command = self.command('--skip-probe', '--codex-bin', str(blocking_cli))
        coordinator_process = subprocess.Popen(command, cwd=self.root, env=env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        child_pid = None
        try:
            deadline = time.monotonic() + 10
            active = None
            while time.monotonic() < deadline and coordinator_process.poll() is None:
                if marker.exists():
                    try:
                        marker_pid = int(marker.read_text())
                        active = json.loads((self.run_dir / 'state.json').read_text()).get('active')
                    except (OSError, ValueError, json.JSONDecodeError):
                        active = None
                    if isinstance(active, dict) and active.get('pid') == marker_pid:
                        child_pid = marker_pid
                        break
                time.sleep(0.02)
            self.assertIsNotNone(child_pid, 'fake provider PID was not recorded in active state')
            self.assertEqual(active['phase'], 'PLAN')

            coordinator_process.kill()
            coordinator_process.communicate(timeout=5)

            resume_command = list(command)
            resume_command[2] = 'resume'
            held = subprocess.run(resume_command, cwd=self.root, env=env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            self.assertEqual(held.returncode, 2)
            self.assertIn('uncertain in-flight', held.stdout)

            retry_command = [*resume_command, '--retry-uncertain']
            still_live = subprocess.run(retry_command, cwd=self.root, env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            self.assertEqual(still_live.returncode, 2)
            self.assertIn('still alive', still_live.stdout)

            # This fake leader has no descendants. Wait on the same coordinator
            # retry precondition used in production rather than polling a PGID
            # after it may have been reaped or reused.
            self.assertFalse(stopped_marker.exists())
            try:
                os.killpg(child_pid, signal.SIGTERM)
            except ProcessLookupError as exc:
                self.fail('fake provider exited before the test sent SIGTERM: ' + str(exc))
            wait_dead_by = time.monotonic() + 5
            recovered = None
            while True:
                recovered = subprocess.run(
                    retry_command, cwd=self.root, env=env, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=45)
                if recovered.returncode == 0:
                    break
                if recovered.returncode != 2 or 'still alive' not in recovered.stdout:
                    self.fail('coordinator could not verify the stopped process group: ' +
                              recovered.stdout + recovered.stderr)
                current_state = json.loads((self.run_dir / 'state.json').read_text())
                self.assertEqual(current_state['uncertain_active']['pid'], child_pid)
                self.assertEqual(current_state.get('abandoned_turns', []), [])
                if time.monotonic() >= wait_dead_by:
                    self.fail('coordinator kept reporting the fake-provider process group as alive: ' +
                              recovered.stdout + recovered.stderr)
                time.sleep(0.02)
            if recovered is None or recovered.returncode != 0:
                self.fail('coordinator kept reporting the fake-provider process group as alive')
            self.assertEqual(stopped_marker.read_text(), str(child_pid))
            child_pid = None
            self.assertIn('DONE', recovered.stdout)
            state = json.loads((self.run_dir / 'state.json').read_text())
            self.assertEqual(state['status'], 'DONE')
            self.assertIsNone(state['uncertain_active'])
            self.assertEqual(len(state['abandoned_turns']), 1)
        finally:
            if coordinator_process.poll() is None:
                coordinator_process.kill()
                coordinator_process.communicate(timeout=5)
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_resume_fails_closed_when_uncertain_process_group_cannot_be_checked(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        co.state['uncertain_active'] = {'pid': 2000, 'role': 'author', 'phase': 'EXEC'}
        co.state['status'] = 'HOLD'
        now = [0.0]
        real_retry = rc.retry_killpg_eperm
        def retry_with_fake_clock(pid):
            return real_retry(pid, clock=lambda: now[0], sleep=lambda duration: now.__setitem__(
                0, now[0] + duration))
        with patch('os.killpg', side_effect=PermissionError(errno.EPERM, 'denied')):
            with patch.object(rc, 'retry_killpg_eperm', side_effect=retry_with_fake_clock):
                with patch.object(co, 'drive') as drive:
                    self.assertEqual(co.resume(retry_uncertain=True), 'HOLD')
        self.assertAlmostEqual(now[0], 1.5)
        drive.assert_not_called()
        self.assertIn('cannot verify uncertain CLI process group', co.state['hold_reason'])

    def test_retry_killpg_eperm_then_esrch_allows_resume(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        co.state['uncertain_active'] = {'pid': 43220, 'role': 'author', 'phase': 'EXEC',
                                        'sequence': 1}
        co.state['status'] = 'HOLD'
        co.save()
        with patch('os.killpg', side_effect=[PermissionError(errno.EPERM, 'race'),
                                             ProcessLookupError(errno.ESRCH, 'gone')]) as killpg:
            with patch.object(co, 'drive', return_value='DONE') as drive:
                self.assertEqual(co.resume(retry_uncertain=True), 'DONE')
        self.assertEqual(killpg.call_count, 2)
        drive.assert_called_once_with()
        self.assertIsNone(co.state['uncertain_active'])

    def test_retry_killpg_persistent_eperm_holds_at_bounded_deadline(self):
        now = [0.0]
        sleeps = []
        def fake_sleep(duration):
            sleeps.append(duration)
            now[0] += duration
        with patch('os.killpg', side_effect=PermissionError(errno.EPERM, 'denied')) as killpg:
            with self.assertRaises(PermissionError):
                rc.retry_killpg_eperm(43221, clock=lambda: now[0], sleep=fake_sleep)
        self.assertAlmostEqual(now[0], 1.5)
        self.assertLess(now[0], 1.51)
        self.assertGreater(killpg.call_count, 1)
        self.assertAlmostEqual(sum(sleeps), 1.5)

    def test_retry_killpg_success_means_still_alive_after_initial_eperm(self):
        with patch('os.killpg', side_effect=[PermissionError(errno.EPERM, 'race'), None]) as killpg:
            self.assertIsNone(rc.retry_killpg_eperm(43222, clock=lambda: 0.0,
                                                    sleep=lambda _duration: None))
        self.assertEqual(killpg.call_count, 2)

    def test_resume_eperm_then_live_group_holds_as_still_alive(self):
        co = self.coordinator()
        co.state['uncertain_active'] = {'pid': 43224, 'role': 'author', 'phase': 'EXEC',
                                        'sequence': 1}
        co.state['status'] = 'HOLD'
        co.save()
        with patch('os.killpg', side_effect=[PermissionError(errno.EPERM, 'race'), None]) as killpg:
            with patch.object(co, 'drive') as drive:
                self.assertEqual(co.resume(retry_uncertain=True), 'HOLD')
        self.assertEqual(killpg.call_count, 2)
        drive.assert_not_called()
        self.assertIn('still alive', co.state['hold_reason'])

    def test_retry_killpg_signal_zero_success_means_still_alive(self):
        with patch('os.killpg') as killpg:
            self.assertIsNone(rc.retry_killpg_eperm(43223))
        killpg.assert_called_once_with(43223, 0)

    def test_resume_fails_closed_for_uncertain_receipt_without_pid(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        uncertain = {'role': 'author', 'phase': 'EXEC', 'sequence': 1}
        co.state['uncertain_active'] = uncertain
        co.state['status'] = 'HOLD'
        with patch('os.killpg') as kill_group:
            with patch.object(co, 'drive') as drive:
                self.assertEqual(co.resume(retry_uncertain=True), 'HOLD')
        kill_group.assert_not_called()
        drive.assert_not_called()
        self.assertEqual(co.state['uncertain_active'], uncertain)
        self.assertIn('process existence cannot be verified', co.state['hold_reason'])
        self.assertIn('operator/manual resolution', co.state['hold_reason'])

    def test_permission_probe_preserves_active_receipt_instead_of_overwriting_it(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        active = {'pid': os.getpid(), 'role': 'reviewer', 'phase': 'EXEC', 'sequence': 3}
        co.state['active'] = active
        co.save()
        with patch.object(co, 'invoke') as invoke:
            self.assertFalse(co.permission_probe())
        invoke.assert_not_called()
        self.assertEqual(co.state['uncertain_active'], active)
        self.assertIsNone(co.state['active'])

    def test_stopped_permission_probe_requires_explicit_retry_before_new_probe(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--codex-bin', str(self.fake_codex_cli())])
        co = rc.Coordinator(args)
        active = {'pid': 987654321, 'role': 'author', 'phase': 'AUTHOR_PERMISSION_PROBE',
                  'sequence': 2}
        co.state['active'] = active
        co.save()
        snapshot = rc.git_snapshot(self.workspace)[0]
        result = {'answer': {'observed_commands': [], 'self_run_evidence': []},
                  'snapshot': snapshot, 'sequence': 1, 'role': 'probe'}
        with patch('os.killpg', side_effect=ProcessLookupError):
            with patch.object(co, 'invoke', return_value=result) as invoke:
                with patch.object(co, '_author_permission_probe', return_value={'status': 'PASS'}):
                    self.assertFalse(co.permission_probe())
                    self.assertEqual(co.state['uncertain_active'], active)
                    invoke.assert_not_called()
                    self.assertFalse(co.permission_probe(retry_uncertain=True))
        invoke.assert_called_once()
        self.assertIsNone(co.state['uncertain_active'])
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('permission probe failed', co.state['hold_reason'])
        saved = json.loads(co.state_path.read_text())
        self.assertEqual(len(saved['abandoned_turns']), 1)
        self.assertTrue(saved['abandoned_turns'][0]['group_gone'])
        self.assertEqual(saved['abandoned_turns'][0]['provider_usage'], 'unknown')
        reloaded = rc.Coordinator(args)
        self.assertEqual(len(reloaded.state['abandoned_turns']), 1)

    def test_uncertain_codex_turn_refuses_global_config_change_before_replay(self):
        config = self.test_home / '.codex/config.toml'
        original = config.read_bytes()
        for action in ('resume', 'permission-probe'):
            with self.subTest(action=action):
                config.write_bytes(original)
                self.run_dir = self.root / ('uncertain-global-' + action)
                co = self.coordinator()
                receipt = {'sequence': 1, 'pid': 43212, 'role': 'author', 'vendor': 'codex',
                           'phase': 'AUTHOR_PERMISSION_PROBE' if action == 'permission-probe' else 'EXEC',
                           'global_codex_before': {'codex_config': hashlib.sha256(original).hexdigest()}}
                co.state['uncertain_active'] = receipt
                co.save()
                config.write_bytes(original + b'\n[unexpected]\nflag = true\n')
                with patch('os.killpg', side_effect=ProcessLookupError):
                    with self.assertRaisesRegex(RuntimeError, 'global Codex config changed during uncertain turn'):
                        (co.permission_probe if action == 'permission-probe' else co.resume)(retry_uncertain=True)
                self.assertEqual(co.state['status'], 'HOLD')
                self.assertEqual(co.state['uncertain_active'], receipt)
                self.assertEqual(co.state.get('abandoned_turns', []), [])
        config.write_bytes(original)

    def test_operator_acknowledges_only_trust_only_change_after_uncertain_hold(self):
        config = self.test_home / '.codex/config.toml'
        original = config.read_bytes()
        for unrelated in (False, True):
            with self.subTest(unrelated=unrelated):
                config.write_bytes(original)
                self.run_dir = self.root / ('ack-trust-' + str(unrelated).lower())
                co = self.coordinator()
                receipt = {'sequence': 1, 'pid': 987654321, 'role': 'author', 'vendor': 'codex',
                           'phase': 'EXEC', 'workspace': str(self.workspace),
                           'global_codex_home': str(co.global_codex_home),
                           'global_config_home': str(co.global_config_home),
                           'global_codex_before': {'codex_config': hashlib.sha256(original).hexdigest()}}
                co.state['uncertain_active'] = receipt; co.save()
                entry = ('\n[projects.' + json.dumps(str(self.workspace)) + ']\ntrust_level = "trusted"\n').encode()
                config.write_bytes(original + entry + (b'\n[other]\nflag = true\n' if unrelated else b''))
                with patch('os.killpg', side_effect=ProcessLookupError):
                    with self.assertRaisesRegex(RuntimeError, 'global Codex config changed'):
                        co.resume(retry_uncertain=True)
                self.assertIn('inspect, then resume --acknowledge-codex-trust', co.state['hold_reason'])
                co.args.action = 'resume'; co.args.acknowledge_codex_trust = self.run_dir.name
                with patch('os.killpg', side_effect=ProcessLookupError), patch.object(co, 'drive', return_value='ACTIVE'):
                    if unrelated:
                        with self.assertRaisesRegex(RuntimeError, 'global Codex config changed'): co.resume()
                        self.assertFalse(co.state.get('codex_trust_acknowledgments'))
                    else:
                        self.assertEqual(co.resume(), 'ACTIVE')
                        record = co.state['codex_trust_acknowledgments'][0]
                        self.assertEqual((record['run_id'], record['operator_uid']), (self.run_dir.name, os.getuid()))
                        self.assertEqual(record['workspace'], str(self.workspace))
        config.write_bytes(original)

    def test_trust_ack_rejects_table_rebinding_and_duplicate_header(self):
        path = self.root / 'trust-boundary.toml'
        header = '[projects.' + json.dumps(str(self.workspace)) + ']\ntrust_level = "trusted"\n'
        before = 'model = "gpt-6-sol"\n\n[hooks]\nenabled = true\n'
        digest = hashlib.sha256(before.encode()).hexdigest()
        path.write_text('model = "gpt-6-sol"\n\n' + header + '\n[hooks]\nenabled = true\n')
        self.assertTrue(rc.trust_entry_only_since_hash(path, digest, self.workspace))
        path.write_text('model = "gpt-6-sol"\n\n[hooks]\n' + header + 'enabled = true\n')
        self.assertFalse(rc.trust_entry_only_since_hash(path, digest, self.workspace))
        old = before + '\n[projects.' + json.dumps(str(self.workspace)) + ']\ntrust_level = "untrusted"\n'
        path.write_text(old + '\n' + header)
        self.assertFalse(rc.trust_entry_only_since_hash(path, hashlib.sha256(old.encode()).hexdigest(), self.workspace))

    def test_claude_uncertain_config_change_has_inspected_retry(self):
        co = self.coordinator('--author-vendor', 'claude', '--reviewer-vendor', 'codex')
        settings = self.test_home / '.claude/settings.json'
        before = settings.read_bytes()
        receipt = {'sequence': 1, 'pid': 987654321, 'role': 'author', 'vendor': 'claude',
                   'phase': 'EXEC', 'global_config_home': str(co.global_config_home),
                   'global_claude_before': {'claude_settings': hashlib.sha256(before).hexdigest()}}
        co.state['uncertain_active'] = receipt; co.save()
        settings.write_text('{"operator_change":true}\n')
        with patch('os.killpg', side_effect=ProcessLookupError):
            with self.assertRaisesRegex(RuntimeError, 'inspect, then resume --retry-uncertain'):
                co.resume(retry_uncertain=True)
        self.assertEqual(co.state['status'], 'HOLD')
        co.args.action = 'resume'
        with patch('os.killpg', side_effect=ProcessLookupError), patch.object(co, 'drive', return_value='ACTIVE'):
            self.assertEqual(co.resume(retry_uncertain=True), 'ACTIVE')
        self.assertEqual(co.state['abandoned_turns'][0]['global_claude_ack']['operator_uid'], os.getuid())
        self.assertNotIn('claude_uncertain_config_change', co.state)

    def test_uncertain_global_config_resume_cli_holds_without_traceback(self):
        config = self.test_home / '.codex/config.toml'; original = config.read_bytes()
        args = rc.parser().parse_args(self.command()[2:]); co = rc.Coordinator(args)
        probe_command = self.command(); probe_command[2] = 'permission-probe'
        probe_result = subprocess.run(probe_command, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(probe_result.returncode, 0, probe_result.stdout + probe_result.stderr)
        co = rc.Coordinator(args)
        co.state['uncertain_active'] = {'sequence': 1, 'pid': 987654321, 'role': 'author',
            'vendor': 'codex', 'phase': 'EXEC', 'workspace': str(self.workspace),
            'global_codex_home': str(co.global_codex_home), 'global_config_home': str(co.global_config_home),
            'global_codex_before': {'codex_config': hashlib.sha256(original).hexdigest()}}
        co.save()
        command = self.command(); command[2] = 'resume'; command.append('--retry-uncertain')
        config.write_bytes(original + b'\n[unexpected]\nflag = true\n')
        result = subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('HOLD: global Codex config changed during uncertain turn', result.stdout)
        self.assertNotIn('Traceback', result.stderr)
        config.write_bytes(original)

    def test_retry_uncertain_checks_probe_before_global_config_recovery(self):
        config = self.test_home / '.codex/config.toml'; original = config.read_bytes()
        args = rc.parser().parse_args(self.command()[2:]); co = rc.Coordinator(args)
        co.state.update(status='HOLD', uncertain_active={'sequence': 1, 'pid': 987654321,
            'role': 'author', 'vendor': 'codex', 'phase': 'EXEC', 'workspace': str(self.workspace),
            'global_codex_home': str(co.global_codex_home), 'global_config_home': str(co.global_config_home),
            'global_codex_before': {'codex_config': hashlib.sha256(original).hexdigest()}})
        co.save()
        rc.atomic_json(self.run_dir / 'permission-probe.json', {'status': 'PASS',
            'reviewer_flags_digest': co.reviewer_flags_digest(), 'author_flags_digest': 'stale',
            'author_permission_probe': {'status': 'PASS'}, 'global_config_changes': {'status': 'PASS'}})
        config.write_bytes(original + b'\n[unexpected]\nflag = true\n')
        command = self.command(); command[2] = 'resume'; command.append('--retry-uncertain')
        probed = []; driven = []
        original_probe = rc.Coordinator.probe_passed
        def record_probe(coordinator):
            probed.append(True)
            return False, 'synthetic stale probe'
        original_archive = rc.Coordinator.archive_abandoned_turn
        def revert_then_archive(coordinator, receipt):
            config.write_bytes(original)
            original_archive(coordinator, receipt)
        def drive(coordinator):
            driven.append(True)
            return 'DONE'
        output = io.StringIO()
        with patch.object(rc.Coordinator, 'probe_passed', record_probe), \
                patch.object(rc.Coordinator, 'archive_abandoned_turn', revert_then_archive), \
                patch.object(rc.Coordinator, 'drive', drive), patch('sys.stdout', output):
            result = rc.main(command[2:])
        self.assertTrue(probed)
        self.assertEqual(result, 2)
        self.assertIn('permission probe stale after uncertain turn', output.getvalue())
        saved = json.loads((self.run_dir / 'state.json').read_text())
        self.assertIsNone(saved['uncertain_active'])
        self.assertEqual(len(saved['abandoned_turns']), 1)
        self.assertFalse(driven)
        config.write_bytes(original)

    def test_trust_ack_probe_expiry_clears_archived_uncertain_turn(self):
        config = self.test_home / '.codex/config.toml'; original = config.read_bytes()
        co = self.coordinator(); before = hashlib.sha256(config.read_bytes()).hexdigest()
        receipt = {'sequence': 1, 'pid': 987654321, 'role': 'author', 'vendor': 'codex',
                   'phase': 'EXEC', 'workspace': str(self.workspace),
                   'global_codex_home': str(co.global_codex_home),
                   'global_config_home': str(co.global_config_home),
                   'global_codex_before': {'codex_config': before}}
        co.state.update(status='HOLD', hold_reason='global Codex config changed during uncertain turn',
                        uncertain_active=receipt)
        co.save(); co.args.action = 'resume'; co.args.acknowledge_codex_trust = self.run_dir.name
        co.args.retry_uncertain = True; co._probe_gate_required = True
        entry = ('\n[projects.' + json.dumps(str(self.workspace)) +
                 ']\ntrust_level = "trusted"\n').encode()
        config.write_bytes(original + entry)
        probe_file = self.run_dir / 'permission-probe.json'
        probe_file.write_text('{"status":"PASS"}\n')
        original_archive = co.archive_abandoned_turn
        def archive_and_delete_probe(turn):
            original_archive(turn)
            probe_file.unlink()
        with patch('os.killpg', side_effect=ProcessLookupError), \
                patch.object(co, 'archive_abandoned_turn', archive_and_delete_probe), \
                patch.object(co, 'drive') as drive:
            self.assertEqual(co.resume(retry_uncertain=True), 'HOLD')
        drive.assert_not_called()
        self.assertIsNone(co.state['uncertain_active'])
        self.assertEqual(len(co.state['abandoned_turns']), 1)
        self.assertIn('permission probe stale after uncertain turn', co.state['hold_reason'])
        config.write_bytes(original)

    def test_codex_turn_records_concurrent_claude_change_without_holding(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_CODEX_TOUCH_CLAUDE_SETTINGS': '1'}):
            report = co._author_permission_probe()
        self.assertEqual(report['status'], 'PASS', report)
        turn = next(row for row in co.state['turns'] if row['vendor'] == 'codex')
        self.assertEqual(turn['global_config_changes']['status'], 'PASS')
        self.assertIn('claude_settings', turn['global_config_changes']['other_vendor_changes'])

    def test_claude_turn_records_concurrent_codex_change_without_holding(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_CLAUDE_TOUCH_CODEX_CONFIG': '1'}):
            self.assertFalse(co.permission_probe())
        turn = next(row for row in co.state['turns'] if row['vendor'] == 'claude')
        self.assertEqual(turn['global_config_changes']['status'], 'PASS')
        self.assertIn('codex_config', turn['global_config_changes']['other_vendor_changes'])
        self.assertEqual(json.loads((self.run_dir / 'permission-probe.json').read_text())['global_config_changes']['status'], 'FAIL')

    def test_permission_probe_clears_unverifiable_hold_after_later_group_check(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--codex-bin', str(self.fake_codex_cli())])
        co = rc.Coordinator(args)
        uncertain = {'pid': 987654322, 'role': 'author', 'phase': 'AUTHOR_PERMISSION_PROBE',
                     'sequence': 2}
        co.state['uncertain_active'] = uncertain
        co.state['status'] = 'HOLD'
        co.save()
        with patch('os.killpg', side_effect=PermissionError(errno.EPERM, 'denied')):
            self.assertFalse(co.permission_probe(retry_uncertain=True))
        self.assertIn('cannot verify uncertain permission-probe', co.state['hold_reason'])
        snapshot = rc.git_snapshot(self.workspace)[0]
        result = {'answer': {'observed_commands': [], 'self_run_evidence': []},
                  'snapshot': snapshot, 'sequence': 3, 'role': 'probe'}
        with patch('os.killpg', side_effect=ProcessLookupError):
            with patch.object(co, 'invoke', return_value=result):
                with patch.object(co, '_author_permission_probe', return_value={'status': 'PASS'}):
                    self.assertFalse(co.permission_probe(retry_uncertain=True))
        self.assertIsNone(co.state['uncertain_active'])
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('permission probe failed', co.state['hold_reason'])
        self.assertNotIn('cannot verify', co.state['hold_reason'])
        self.assertEqual(len(co.state['abandoned_turns']), 1)

    def test_permission_probe_eperm_then_esrch_clears_uncertain_probe_before_reprobe(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--codex-bin', str(self.fake_codex_cli())])
        co = rc.Coordinator(args)
        active = {'pid': 43225, 'role': 'author', 'phase': 'AUTHOR_PERMISSION_PROBE', 'sequence': 2}
        co.state['active'] = active
        co.save()
        def stop_before_provider(*_args, **_kwargs):
            self.assertIsNone(co.state['active'])
            self.assertIsNone(co.state['uncertain_active'])
            raise RuntimeError('stop offline before provider invocation')
        with patch('os.killpg', side_effect=[PermissionError(errno.EPERM, 'race'),
                                             ProcessLookupError(errno.ESRCH, 'gone')]) as killpg:
            with patch.object(co, 'invoke', side_effect=stop_before_provider) as invoke:
                self.assertFalse(co.permission_probe(retry_uncertain=True))
        self.assertEqual(killpg.call_count, 2)
        invoke.assert_called_once()
        self.assertIsNone(co.state['uncertain_active'])

    def test_uncertain_permission_probe_refuses_live_or_unverifiable_process_group(self):
        for side_effect in (None, PermissionError(errno.EPERM, 'denied'),
                            OSError('operation unavailable')):
            with self.subTest(side_effect=side_effect):
                label = ('alive' if side_effect is None else
                         'denied' if isinstance(side_effect, PermissionError) else 'unavailable')
                self.run_dir = self.root / ('probe-group-' + label)
                args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
                    '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
                co = rc.Coordinator(args)
                active = {'pid': 12345, 'role': 'author', 'phase': 'AUTHOR_PERMISSION_PROBE',
                          'sequence': 2}
                co.state['active'] = active
                co.save()
                with patch('os.killpg', side_effect=side_effect) as kill_group:
                    with patch.object(co, 'invoke') as invoke:
                        with patch.object(co, '_author_permission_probe') as probe:
                            self.assertFalse(co.permission_probe(retry_uncertain=True))
                if isinstance(side_effect, PermissionError):
                    self.assertGreater(kill_group.call_count, 1)
                else:
                    kill_group.assert_called_once_with(12345, 0)
                invoke.assert_not_called()
                probe.assert_not_called()
                self.assertEqual(co.state['uncertain_active'], active)
                self.assertIsNone(co.state['active'])

    def test_uncertain_permission_probe_without_valid_group_id_fails_closed(self):
        for pid in (None, 0, True, '12345', 2**31, -1):
            with self.subTest(pid=pid):
                self.run_dir = self.root / ('probe-group-invalid-' + str(pid))
                args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
                    '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
                co = rc.Coordinator(args)
                uncertain = {'role': 'author', 'phase': 'AUTHOR_PERMISSION_PROBE',
                             'sequence': 2}
                if pid is not None:
                    uncertain['pid'] = pid
                co.state['uncertain_active'] = uncertain
                co.save()
                with patch('os.killpg') as kill_group:
                    with patch.object(co, 'invoke') as invoke:
                        with patch.object(co, '_author_permission_probe') as probe:
                            self.assertFalse(co.permission_probe(retry_uncertain=True))
                kill_group.assert_not_called()
                invoke.assert_not_called()
                probe.assert_not_called()
                self.assertEqual(co.state['uncertain_active'], uncertain)

    def test_popen_failure_clears_known_unstarted_receipt_and_budget(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        before = co.state['invocations_used']
        actual_popen = subprocess.Popen
        def fail_cli_spawn(*popen_args, **popen_kwargs):
            if popen_kwargs.get('start_new_session'):
                raise OSError('executable missing')
            return actual_popen(*popen_args, **popen_kwargs)
        with patch('subprocess.Popen', side_effect=fail_cli_spawn):
            with self.assertRaisesRegex(RuntimeError, 'failed to start'):
                co.invoke('author', 'EXEC', co._author_prompt(), rc.review_schema())
        self.assertIsNone(co.state['active'])
        self.assertEqual(co.state['invocations_used'], before)
        saved = json.loads(co.state_path.read_text())
        self.assertIsNone(saved['active'])
        self.assertEqual(saved['invocations_used'], before)
        self.assertEqual(saved['turns'], [])
        failure = saved['spawn_failures'][-1]
        self.assertIn('executable missing', failure['error'])
        self.assertFalse(failure['invocation_budget_counted'])
        self.assertFalse(failure['child_created'])
        self.assertEqual(failure['sequence'], 1)
        with patch.object(co, 'drive', return_value='DONE') as drive:
            self.assertEqual(co.resume(), 'DONE')
        drive.assert_called_once()

    def test_usage_report_distinguishes_abandoned_invocation_and_spawn_failure(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        co.archive_abandoned_turn({'sequence': 8, 'role': 'reviewer', 'phase': 'PLAN_REVIEW',
                                   'invocation_budget_counted': True})
        co.state['invocations_used'] = 1
        actual_popen = subprocess.Popen
        def fail_cli_spawn(*popen_args, **popen_kwargs):
            if popen_kwargs.get('start_new_session'):
                raise OSError('executable missing')
            return actual_popen(*popen_args, **popen_kwargs)
        with patch('subprocess.Popen', side_effect=fail_cli_spawn):
            with self.assertRaisesRegex(RuntimeError, 'failed to start'):
                co.invoke('author', 'EXEC', co._author_prompt(), rc.review_schema())

        co.write_usage()
        usage = json.loads((self.run_dir / 'usage.json').read_text())
        self.assertEqual(usage['invocations_used'], 1)
        self.assertEqual(usage['total_cli_turns'], 0)
        self.assertEqual(usage['usage_reconciliation'], [
            {'source': 'abandoned_turn', 'sequence': 8, 'role': 'reviewer',
             'phase': 'PLAN_REVIEW', 'invocation_budget_counted': True,
             'provider_usage': 'unknown'},
            {'source': 'spawn_failure', 'sequence': 1, 'role': 'author',
             'phase': 'EXEC', 'invocation_budget_counted': False,
             'provider_usage': 'none'},
        ])
        usage_md = (self.run_dir / 'usage.md').read_text()
        self.assertIn('| abandoned_turn | 8 | reviewer | PLAN_REVIEW | True | unknown |', usage_md)
        self.assertIn('| spawn_failure | 1 | author | EXEC | False | none |', usage_md)

    def test_usage_report_includes_unresolved_active_receipt_once(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        receipt = {'sequence': 12, 'role': 'author', 'phase': 'EXEC',
                   'invocation_budget_counted': True}
        co.state['uncertain_active'] = receipt
        co.state['usage_reconciliation'] = [{'source': 'abandoned_turn', 'sequence': 7,
            'role': 'reviewer', 'phase': 'PLAN_REVIEW', 'invocation_budget_counted': True,
            'provider_usage': 'unknown'}]
        co.write_usage()
        usage = json.loads((self.run_dir / 'usage.json').read_text())
        matches = [row for row in usage['usage_reconciliation'] if row['sequence'] == 12]
        self.assertEqual(matches, [{'source': 'uncertain_active', 'sequence': 12,
            'role': 'author', 'phase': 'EXEC', 'invocation_budget_counted': True,
            'provider_usage': 'unknown'}])
        usage_md = (self.run_dir / 'usage.md').read_text()
        self.assertIn('| uncertain_active | 12 | author | EXEC | True | unknown |', usage_md)

    def test_opening_cli_output_path_failure_is_refunded_and_durable(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        before = co.state['invocations_used']
        original_open = Path.open
        def fail_log_open(path, *open_args, **open_kwargs):
            if path.name.endswith('.stdout.jsonl') or path.name.endswith('.stderr.log'):
                raise PermissionError('log path denied')
            return original_open(path, *open_args, **open_kwargs)
        with patch.object(Path, 'open', new=fail_log_open):
            with self.assertRaisesRegex(RuntimeError, 'failed to start'):
                co.invoke('author', 'EXEC', co._author_prompt(), rc.review_schema())
        saved = json.loads(co.state_path.read_text())
        self.assertIsNone(saved['active'])
        self.assertEqual(saved['invocations_used'], before)
        self.assertEqual(saved['turns'], [])
        self.assertEqual(saved['spawn_failures'][-1]['error'], 'PermissionError: log path denied')

    def test_command_preparation_failure_does_not_spend_budget(self):
        co = rc.Coordinator(rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)]))
        with patch.object(co, 'command', side_effect=OSError('command preparation failed')):
            with self.assertRaisesRegex(OSError, 'command preparation failed'):
                co.invoke('author', 'EXEC', co._author_prompt(), rc.review_schema())
        reloaded = rc.Coordinator(co.args)
        self.assertEqual(reloaded.state['invocations_used'], 0)
        self.assertEqual(reloaded.state['turns'], [])

    def test_close_error_after_popen_preserves_pid_and_budget(self):
        co = rc.Coordinator(rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)]))
        original_open = Path.open
        class CloseError:
            def __init__(self, file): self.file = file
            def __enter__(self): return self.file.__enter__()
            def __exit__(self, *exc):
                self.file.__exit__(*exc)
                raise OSError('close failed')
        def wrapped_open(path, *args, **kwargs):
            opened = original_open(path, *args, **kwargs)
            return CloseError(opened) if path.name.endswith('.stdout.jsonl') else opened
        child = type('Child', (), {'pid': 43210})()
        actual_popen = subprocess.Popen
        def cli_popen(*args, **kwargs):
            return child if kwargs.get('start_new_session') else actual_popen(*args, **kwargs)
        with patch.object(Path, 'open', new=wrapped_open):
            with patch('subprocess.Popen', side_effect=cli_popen):
                with patch('os.killpg') as kill_group:
                    with self.assertRaisesRegex(OSError, 'close failed'):
                        co.invoke('author', 'EXEC', co._author_prompt(), rc.review_schema())
        kill_group.assert_called_once_with(43210, signal.SIGKILL)
        saved = json.loads(co.state_path.read_text())
        self.assertEqual(saved['active']['pid'], 43210)
        self.assertIsNone(saved.get('uncertain_active'))
        self.assertEqual(saved['invocations_used'], 1)
        self.assertEqual(saved.get('spawn_failures', []), [])

    def test_active_retry_flag_verifies_and_recovers_in_one_resume(self):
        co = rc.Coordinator(rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)]))
        active = {'pid': 43211, 'role': 'author', 'phase': 'EXEC', 'sequence': 4,
                  'invocation_budget_counted': True}
        co.state['active'] = active
        co.save()
        with patch('os.killpg', side_effect=ProcessLookupError) as kill_group:
            with patch.object(co, 'drive', return_value='DONE') as drive:
                self.assertEqual(co.resume(retry_uncertain=True), 'DONE')
        kill_group.assert_called_once_with(43211, 0)
        drive.assert_called_once_with()
        reloaded = rc.Coordinator(co.args)
        self.assertEqual(len(reloaded.state['abandoned_turns']), 1)
        self.assertEqual(reloaded.state['turns'], [])

    def test_retry_refusals_durably_hold_active_receipts_and_block_drive(self):
        cases = (
            ('missing-pid', {'role': 'author', 'phase': 'EXEC', 'sequence': 11}, None),
            ('live', {'pid': 43212, 'role': 'author', 'phase': 'EXEC', 'sequence': 12}, None),
            ('unverifiable', {'pid': 43213, 'role': 'author', 'phase': 'EXEC', 'sequence': 13},
             PermissionError('not permitted')),
        )
        for name, receipt, error in cases:
            with self.subTest(name=name):
                self.run_dir = self.root / name
                co = self.coordinator()
                co.state['status'] = 'ACTIVE'
                co.state['active'] = dict(receipt)
                co.save()
                if name == 'missing-pid':
                    result = co.resume(retry_uncertain=True)
                else:
                    with patch('os.killpg', side_effect=error or None):
                        result = co.resume(retry_uncertain=True)
                self.assertEqual(result, 'HOLD')
                saved = json.loads(co.state_path.read_text())
                self.assertEqual(saved['status'], 'HOLD')
                self.assertIsNone(saved['active'])
                self.assertEqual(saved['uncertain_active'], receipt)
                reloaded = rc.Coordinator(co.args)
                with patch.object(reloaded, 'author_turn') as author_turn:
                    self.assertEqual(reloaded.drive(), 'HOLD')
                author_turn.assert_not_called()

    def test_migration_counts_uncertain_and_abandoned_invocations_once(self):
        co = self.coordinator()
        co.state['invocations_used'] = 0
        co.state['invocation_budget_version'] = 0
        co.state['uncertain_active'] = {'sequence': 20, 'invocation_budget_counted': True}
        co.state['abandoned_turns'] = [
            {'sequence': 21, 'invocation_budget_counted': True},
            {'sequence': 22, 'invocation_budget_counted': True},
        ]
        co.state['spawn_failures'] = [{'sequence': 23, 'invocation_budget_counted': False}]
        self.assertEqual(co._migrate_invocation_budget(), 3)

    def test_migration_keeps_timed_out_429_invocation_counted(self):
        co = self.coordinator()
        co.state['invocations_used'] = 0
        co.state['invocation_budget_version'] = 0
        co.state['turns'] = [{
            'sequence': 24, 'phase': 'PLAN', 'role': 'author',
            'returncode': -9, 'timed_out': True,
        }]
        (co.evidence / '024-plan-author.stderr.log').write_text('429 too many requests\n')

        self.assertEqual(co._migrate_invocation_budget(), 1)
        self.assertTrue(co.state['turns'][0]['invocation_budget_counted'])

    def test_migration_keeps_positive_returncode_timed_out_429_counted(self):
        co = self.coordinator()
        co.state['invocations_used'] = 0
        co.state['invocation_budget_version'] = 0
        co.state['turns'] = [{
            'sequence': 26, 'phase': 'PLAN', 'role': 'author',
            'returncode': 7, 'timed_out': True,
        }]
        (co.evidence / '026-plan-author.stderr.log').write_text('429 too many requests\n')

        self.assertEqual(co._migrate_invocation_budget(), 1)
        self.assertTrue(co.state['turns'][0]['invocation_budget_counted'])
        self.assertNotEqual(co.state['turns'][0].get('error_kind'), 'rate_limited')

    def test_migration_keeps_legacy_sigkilled_429_invocation_counted(self):
        co = self.coordinator()
        co.state['invocations_used'] = 0
        co.state['invocation_budget_version'] = 0
        co.state['turns'] = [{
            'sequence': 25, 'phase': 'PLAN', 'role': 'author', 'returncode': -9,
        }]
        (co.evidence / '025-plan-author.stderr.log').write_text('429 too many requests\n')

        self.assertEqual(co._migrate_invocation_budget(), 1)
        self.assertTrue(co.state['turns'][0]['invocation_budget_counted'])

    def test_probe_retries_after_real_group_leader_exits_but_descendant_lives(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--codex-bin', str(self.fake_codex_cli())])
        co = rc.Coordinator(args)
        parent_signal, descendant_signal = socket.socketpair()
        descendant_signal.set_inheritable(True)
        child = subprocess.Popen([sys.executable, '-c',
            'import socket,subprocess,sys; '
            'fd=int(sys.argv[1]); '
            'subprocess.Popen([sys.executable,"-c",'
            '"import socket,sys,time; s=socket.socket(fileno=int(sys.argv[1])); '
            's.sendall(b\\\"R\\\"); time.sleep(600)",str(fd)], pass_fds=(fd,))',
            str(descendant_signal.fileno())], start_new_session=True,
            pass_fds=(descendant_signal.fileno(),))
        descendant_signal.close()
        try:
            child.wait(timeout=5)
            parent_signal.settimeout(5)
            self.assertEqual(parent_signal.recv(1), b'R')
            active = {'pid': child.pid, 'role': 'author', 'phase': 'AUTHOR_PERMISSION_PROBE',
                      'sequence': 2}
            co.state['active'] = active
            co.state['hold_reason'] = 'uncertain permission-probe turn; stale'
            co.save()
            with patch.object(co, 'invoke') as invoke:
                self.assertFalse(co.permission_probe(retry_uncertain=True))
            invoke.assert_not_called()
            self.assertEqual(co.state['uncertain_active'], active)
            # The descendant's ready byte proves the original group is still
            # owned and alive. Kill it before waiting for EOF; never poll its
            # numeric PGID after it may have disappeared or been reused.
            parent_signal.setblocking(False)
            try:
                self.assertEqual(parent_signal.recv(1), b'')
                owns_socket = False
            except BlockingIOError:
                owns_socket = True
            self.assertTrue(owns_socket, 'descendant must still own the sentinel socket before kill')
            if owns_socket:
                os.killpg(child.pid, signal.SIGKILL)
                parent_signal.settimeout(5)
                self.assertEqual(parent_signal.recv(1), b'')
        finally:
            try:
                # This fallback is safe only while the inherited sentinel has
                # not reached EOF, which means the descendant still owns the
                # original process group.
                if parent_signal.fileno() >= 0:
                    parent_signal.settimeout(0)
                    try:
                        descendant_gone = parent_signal.recv(1) == b''
                    except BlockingIOError:
                        descendant_gone = False
                    if not descendant_gone:
                        os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                parent_signal.close()
                child.wait(timeout=5)
        snapshot = rc.git_snapshot(self.workspace)[0]
        result = {'answer': {'observed_commands': [], 'self_run_evidence': []},
                  'snapshot': snapshot, 'sequence': 3, 'role': 'probe'}
        def check_cleared(*unused, **kwargs):
            self.assertIsNone(co.state['active'])
            self.assertIsNone(co.state['uncertain_active'])
            self.assertIn('permission probe is pending', co.state['hold_reason'])
            saved = json.loads(co.state_path.read_text())
            self.assertIsNone(saved['active'])
            self.assertIsNone(saved['uncertain_active'])
            self.assertIn('permission probe is pending', saved['hold_reason'])
            return result
        with patch('os.killpg', side_effect=ProcessLookupError) as kill_group:
            with patch.object(co, 'invoke', side_effect=check_cleared) as invoke:
                with patch.object(co, '_author_permission_probe', return_value={'status': 'PASS'}):
                    self.assertFalse(co.permission_probe(retry_uncertain=True))
        kill_group.assert_called_once_with(child.pid, 0)
        invoke.assert_called_once()
        self.assertIn('permission probe failed', co.state['hold_reason'])

    def test_uncertain_turn_requires_explicit_retry_after_abort_hold(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        active = {'pid': os.getpid(), 'role': 'author', 'phase': 'EXEC', 'sequence': 4}
        co.state['active'] = active
        self.assertEqual(co.hold('aborted by operator'), 'HOLD')
        self.assertEqual(co.state['uncertain_active'], active)
        with patch.object(co, 'drive') as drive:
            self.assertEqual(co.resume(), 'HOLD')
        drive.assert_not_called()

    def test_run_refuses_without_matching_pass_probe_unless_explicitly_skipped(self):
        result = self.run_coordinator(skip_probe=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('REFUSED: permission-probe.json is missing', result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['turns'], [])
        bypass = self.run_coordinator()
        self.assertEqual(bypass.returncode, 0, bypass.stderr + bypass.stdout)

    def test_probe_reports_not_attempted_as_distinct_failure(self):
        command = self.command()
        command[2] = 'permission-probe'
        env = os.environ.copy()
        env['FAKE_MISSING_OBSERVED'] = '1'
        result = subprocess.run(command, cwd=self.root, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 2)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(set(report['write_attempt_outcomes'].values()), {'not-attempted'})
        self.assertEqual(len([x for x in report['failure_reasons'] if x.startswith('not-attempted:')]), 11)
        before = len(json.loads((self.run_dir / 'state.json').read_text())['turns'])
        run = self.run_coordinator(skip_probe=False)
        self.assertEqual(run.returncode, 2)
        self.assertIn('permission probe status is not PASS', run.stdout)
        after = len(json.loads((self.run_dir / 'state.json').read_text())['turns'])
        self.assertEqual(before, after)

    def test_exec_approve_rejects_claimed_but_unobserved_test(self):
        result = self.run_coordinator('--exercise-revisions', '--shadow', 'off',
                                      '--adversarial-gate', 'off',
                                      env={'FAKE_MISSING_OBSERVED': '1'})
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD')
        self.assertIn('lacks an observed successful configured test command', state['hold_reason'])
        self.assertNotIn('reason', state)
        self.assertIn('HOLD:', result.stdout)

    def test_wrapped_observed_test_command_does_not_count(self):
        result = self.run_coordinator('--exercise-revisions', '--shadow', 'off',
                                      '--adversarial-gate', 'off',
                                      env={'FAKE_WRAPPED_TEST': '1'})
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertIn('lacks an observed successful configured test command', state['hold_reason'])

    def test_wrong_or_missing_model_snapshot_is_ignored_and_program_bound(self):
        for mode in ('wrong', 'missing'):
            with self.subTest(mode=mode):
                self.run_dir = self.root / ('snapshot-' + mode)
                result = self.run_coordinator('--exercise-revisions', '--shadow', 'off',
                                              '--adversarial-gate', 'off',
                                              env={'FAKE_SNAPSHOT_MODE': mode})
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                state = json.loads((self.run_dir / 'state.json').read_text())
                reviews = [t for t in state['turns'] if t['role'] == 'reviewer']
                self.assertTrue(reviews)
                self.assertTrue(all(len(t['answer']['reviewed_snapshot']) == 64 for t in reviews))
                self.assertTrue(all(t['answer']['reviewed_snapshot'] == t['snapshot_before'] for t in reviews))

    def test_persistent_reviewer_sensitive_path_access_holds(self):
        secret = str(self.run_dir / 'evidence' / 'secret.json')
        result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                      env={'FAKE_SENSITIVE_READ': secret})
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertIn('accessed isolated evidence directory', state['hold_reason'])

    def test_valid_gate_blocker_delivered_once_then_re_reviewed(self):
        result = self.run_coordinator('--exercise-revisions', env={'FAKE_GATE_BLOCK': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE')
        self.assertEqual(state['exec_rounds'], 3)
        self.assertEqual(sum(t['role'] == 'gate' for t in state['turns']), 1)
        prompts = [p.read_text() for p in (self.run_dir / 'evidence').glob('*-author.prompt.txt')]
        self.assertTrue(any('adversarial-gate' in p for p in prompts))

    def test_lifecycle_gate_blocker_starts_new_gate_convergence(self):
        co = self.coordinator('--shadow', 'off', '--polish-round', 'off')
        co.state.update(phase='EXEC', next='gate', exec_rounds=1)
        co.state['config']['lifecycle_mode'] = 'on'
        marker = self.root / 'gate-blocked-once'
        with patch.object(co, '_author_tmp_isolated', return_value=True):
            co._freeze_role_dispatch()
            with patch.dict(os.environ, {'FAKE_GATE_BLOCK_ONCE': str(marker)}):
                co.gate_turn()
                self.assertEqual(co.state['next'], 'author')
                self.assertFalse(co.state['gate_ran'])
                co.author_turn()
                co.reviewer_turn()
                self.assertEqual(co.state['next'], 'gate')
                co.gate_turn()
        self.assertTrue(co.state['gate_ran'])
        self.assertEqual(sum(turn['role'] == 'gate' for turn in co.state['turns']), 2)
        self.assertEqual(co.state['status'], 'DONE')
        gates = [turn for turn in co.state['turns'] if turn['role'] == 'gate']
        self.assertEqual(gates[-1]['answer']['verdict'], 'approve')
        self.assertNotEqual(gates[0]['snapshot_before'], gates[1]['snapshot_before'])

    def test_specialist_blocker_can_only_be_disposed_by_its_owner(self):
        co = self.coordinator()
        finding = {'severity': 'MAJOR', 'file': 'tracked.txt',
                   'summary': 'specialist blocker', 'failure_scenario': 'unsafe behavior'}
        row = co.record_findings('specialist:python-reviewer', 'POLISH-Q', 1, [finding])[0]
        ledger = next(item for item in co.state['finding_ledger'] if item['id'] == row['id'])
        self.assertEqual(ledger['owner_role'], 'specialist:python-reviewer')
        with self.assertRaisesRegex(RuntimeError, 'owning role'):
            co.record_findings('specialist: python-reviewer', 'POLISH-Q', 1, [finding])
        self.assertIn(ledger, co.blocking_open_findings())
        keep_open = [{'id': row['id'], 'disposition': 'still_open', 'evidence': 'reviewed'}]
        co.apply_dispositions(keep_open, 2, owner_role='persistent-reviewer')
        self.assertEqual(ledger['status'], 'open')
        co.state.update(phase='EXEC', next='reviewer', exec_rounds=1)
        co.save()
        co.reviewer_turn()
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('owning specialist', co.state['hold_reason'])
        self.assertEqual(co.state['turns'][-1]['answer']['prior_findings'][0]['disposition'], 'fixed')
        self.assertEqual(ledger['status'], 'open')
        disposition = [{'id': row['id'], 'disposition': 'fixed', 'evidence': 'owner verified'}]
        with self.assertRaisesRegex(RuntimeError, 'owning specialist'):
            co.apply_dispositions(disposition, 2, owner_role='specialist:go-reviewer')
        self.assertEqual(ledger['status'], 'open')
        co.apply_dispositions(disposition, 3, owner_role='specialist:python-reviewer')
        self.assertEqual(ledger['status'], 'fixed')

    def test_malformed_gate_blocker_is_recorded_but_not_delivered(self):
        result = self.run_coordinator('--exercise-revisions', env={'FAKE_GATE_MALFORMED': '1'})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['exec_rounds'], 2)
        gate_md = next((self.run_dir / 'rounds').glob('*-adversarial-*.md')).read_text()
        self.assertIn('discard_reason', gate_md)

    def test_exec_approve_without_self_run_evidence_holds(self):
        # Exercise the invariant directly because the standard fake emits allowed evidence.
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir)])
        co = rc.Coordinator(args)
        co.state.update(phase='EXEC', next='reviewer', exec_rounds=1)
        co.save()
        snap = rc.git_snapshot(self.workspace)[0]
        answer = {'status': 'APPROVE', 'reviewed_snapshot': snap, 'prior_findings': [],
                  'full_review': [], 'self_run_evidence': []}
        with patch.object(co, 'invoke', return_value={'answer': answer, 'snapshot': snap, 'sequence': 1}), \
             patch.object(co, 'render'):
            co.reviewer_turn()
        self.assertEqual(co.state['status'], 'HOLD')

    def test_exec_approve_allows_open_minor_but_not_open_critical(self):
        for severity, expected in (('MINOR', 'DONE'), ('CRITICAL', 'HOLD')):
            with self.subTest(severity=severity):
                self.run_dir = self.root / ('open-' + severity.lower())
                args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
                    '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
                    '--shadow', 'off', '--adversarial-gate', 'off', '--polish-round', 'off'])
                co = rc.Coordinator(args)
                co.state.update(phase='EXEC', next='reviewer', exec_rounds=1)
                co.state['finding_ledger'] = [{
                    'id': 'F001', 'origin_round': 1, 'phase': 'EXEC',
                    'source': 'persistent-reviewer', 'severity': severity, 'file': 'sum_ints.py',
                    'summary': 'open issue', 'status': 'open',
                    'status_history': [{'round': 1, 'status': 'open', 'evidence': 'test'}]}]
                co.state['next_finding_id'] = 2
                co.save()
                snap = rc.git_snapshot(self.workspace)[0]
                answer = {'status': 'APPROVE', 'reviewed_snapshot': snap,
                          'prior_findings': [{'id': 'F001', 'disposition': 'still_open',
                                              'evidence': 'must observe in production'}],
                          'full_review': [],
                          'self_run_evidence': [{'command': 'npm test'}]}
                result = {'answer': answer, 'snapshot': snap, 'sequence': 2, 'role': 'reviewer'}
                with patch.object(co, 'invoke', return_value=result), patch.object(co, 'render'):
                    co.reviewer_turn()
                self.assertEqual(co.state['status'], expected)

    def test_write_attempts_are_detected_and_fail_round(self):
        for mode in ('echo', 'checkout', 'rm'):
            with self.subTest(mode=mode):
                run_dir = self.root / ('run-' + mode)
                self.run_dir = run_dir
                result = self.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off',
                                              env={'FAKE_MUTATION': mode})
                self.assertEqual(result.returncode, 2)
                state = json.loads((run_dir / 'state.json').read_text())
                self.assertEqual(state['status'], 'HOLD')
                self.assertIn('mutated workspace', state['hold_reason'])
                # Restore fixture for the next subtest without hiding the detected failure.
                subprocess.run(['git', 'checkout', '--', 'tracked.txt'], cwd=self.workspace, check=True)
                extra = self.workspace / 'forbidden.txt'
                if extra.exists():
                    extra.unlink()

    def test_plan_author_cannot_mutate_workspace(self):
        result = self.run_coordinator(env={'FAKE_PLAN_MUTATE': '1'})
        self.assertEqual(result.returncode, 2)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD')
        self.assertIn('mutated workspace during PLAN', state['hold_reason'])


    def test_stop_after_plan_holds_once_and_resume_enters_exec(self):
        for label, resume_flags in (('with-flag', ('--stop-after-plan',)), ('without-flag', ())):
            with self.subTest(resume=label):
                self.run_dir = self.root / f'stop-{label}'
                subprocess.run(['git', 'checkout', '-q', '--', '.'], cwd=self.workspace, check=True)
                subprocess.run(['git', 'clean', '-qfd'], cwd=self.workspace, check=True)
                self._stop_then_resume(resume_flags)

    def _stop_then_resume(self, resume_flags):
        stopped = self.run_coordinator('--stop-after-plan')
        self.assertIn('HOLD', stopped.stdout + stopped.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD')
        self.assertEqual(state['hold_reason'], rc.PLAN_STOP_REASON)
        self.assertEqual((state['phase'], state['next']), ('EXEC', 'author'))
        self.assertEqual(state['exec_rounds'], 0)
        self.assertTrue(state['plan_stop_done'])
        self.assertTrue((self.run_dir / 'plan.md').exists())
        self.assertTrue((self.run_dir / 'usage.md').exists())
        self.assertNotIn('stop_after_plan', state['config'])
        turns_at_stop = state['sequence']
        # A state written before author_subagents existed must still resume.
        del state['config']['author_subagents']
        rc.atomic_json(self.run_dir / 'state.json', state)
        command = self.command(*resume_flags) + ['--skip-probe']
        command[2] = 'resume'
        resumed = subprocess.run(command, cwd=self.root, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE', resumed.stdout + resumed.stderr)
        self.assertGreater(state['sequence'], turns_at_stop)
        self.assertGreaterEqual(state['exec_rounds'], 1)

    def test_author_subagents_switch_only_changes_the_author_surface(self):
        def commands(*extra):
            args = rc.parser().parse_args(['run', '--workspace', str(self.workspace),
                '--workitem', str(self.workitem), '--run-dir', str(self.root / ('r' + str(len(extra)))),
                '--author-vendor', 'claude', '--reviewer-vendor', 'claude', *extra])
            co = rc.Coordinator(args)
            schema = self.root / 'schema-sub.json'
            rc.atomic_json(schema, rc.review_schema())
            return co, co.command('author', schema, False), co.command('reviewer', schema, False)
        co, author, reviewer = commands()
        self.assertEqual(co.state['config']['author_subagents'], 'on')
        self.assertIn('Agent', author[author.index('--tools') + 1].split(','))
        self.assertIn('Agent', author[author.index('--allowedTools') + 1].split(','))
        self.assertEqual(author[author.index('--disallowedTools') + 1], 'NotebookEdit')
        self.assertIn('Agent', reviewer[reviewer.index('--disallowedTools') + 1].split(','))
        self.assertNotIn('Agent', reviewer[reviewer.index('--tools') + 1].split(','))
        _, author_off, _ = commands('--author-subagents', 'off')
        self.assertNotIn('Agent', author_off[author_off.index('--tools') + 1].split(','))
        self.assertIn('Agent', author_off[author_off.index('--disallowedTools') + 1].split(','))


    def test_claude_subagent_requests_are_counted_once_per_message(self):
        def start(tokens):
            return {'type': 'stream_event', 'event': {'type': 'message_start', 'message': {
                'usage': {'input_tokens': 10, 'cache_creation_input_tokens': tokens, 'cache_read_input_tokens': 5,
                          'output_tokens': 1}}}}
        def sub(message_id, output):
            return {'type': 'assistant', 'parent_tool_use_id': 'toolu_1', 'message': {'id': message_id, 'usage': {
                'input_tokens': 10, 'cache_creation_input_tokens': 9680, 'cache_read_input_tokens': 0,
                'output_tokens': output}}}
        rows = [start(100), {'type': 'assistant', 'parent_tool_use_id': None,
                             'message': {'id': 'main', 'usage': {'input_tokens': 999}}},
                sub('msg_a', 4), sub('msg_a', 9), sub('msg_b', 2), start(200),
                # modelUsage is cumulative over a resumed session and must not be used as a turn total.
                {'type': 'result', 'is_error': False, 'session_id': 's', 'structured_output': {'status': 'DONE'},
                 'modelUsage': {'m': {'inputTokens': 10 ** 9}}}]
        path = self.root / 'stream.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                                       '--run-dir', str(self.root / 'usage-run'), '--author-vendor', 'claude',
                                       '--reviewer-vendor', 'codex'])
        _, _, usage, count, _, _ = rc.Coordinator(args)._collect('claude', path)
        self.assertEqual(count, 4)
        self.assertEqual([use['source'] for use in usage].count('subagent-message'), 2)
        self.assertEqual(sum(use['input'] for use in usage), (115 + 215) + 2 * 9690)
        self.assertEqual(sorted(use['output'] for use in usage if use['source'] == 'subagent-message'), [2, 9])


    def test_background_subagent_or_second_result_is_rejected(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                                       '--run-dir', str(self.root / 'bg-run'), '--author-vendor', 'claude',
                                       '--reviewer-vendor', 'codex'])
        co = rc.Coordinator(args)
        def result(background):
            return {'type': 'result', 'is_error': False, 'session_id': 's', 'structured_output': {'status': 'DONE'},
                    'subagent_stats': {'spawned': 1, 'started_in_background': background, 'completed': 1}}
        path = self.root / 'bg.jsonl'
        for rows in ([result(1)], [result(0), result(0)]):
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            with self.assertRaisesRegex(ValueError, 'not authoritative'):
                co._collect('claude', path)
        path.write_text(json.dumps(result(0)) + '\n')
        self.assertEqual(co._collect('claude', path)[0], {'status': 'DONE'})
        self.assertEqual(rc.cli_env()['CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'], '1')


    def test_fresh_scan_rejects_ledger_ids_but_not_lowercase_identifiers(self):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                                       '--run-dir', str(self.root / 'scan-run')])
        co = rc.Coordinator(args)
        (co.context / 'delta.patch').write_text('+    f720 = cap720.decode(t)\n+    frame1080 = None\n')
        co.assert_fresh_prompt('shadow', 'Review the delta.')  # must not raise
        (co.context / 'delta.patch').write_text('+    # see F015 for context\n')
        with self.assertRaisesRegex(RuntimeError, 'F015'):
            co.assert_fresh_prompt('shadow', 'Review the delta.')



    def test_codex_author_permissions_survive_fake_plan_and_exec_resume(self):
        result = self.run_coordinator('--author-vendor', 'codex', '--reviewer-vendor', 'claude')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE')
        authors = [row for row in state['turns'] if row['role'] == 'author']
        self.assertGreaterEqual(len(authors), 2)
        self.assertTrue(any('resume' in row['command'] for row in authors))
        for row in authors:
            command = row['command']
            self.assertEqual(row['environment_overrides']['TMPDIR'], str(self.run_dir / 'author-tmp'))
            self.assertIn('--ignore-rules', command)
            self.assertIn('sandbox_mode="workspace-write"', command)
            self.assertIn('approval_policy="never"', command)
            for setting in ('sandbox_workspace_write.writable_roots=' +
                            json.dumps([str((self.run_dir / 'author-tmp').resolve())]),
                            'sandbox_workspace_write.exclude_tmpdir_env_var=false',
                            'sandbox_workspace_write.exclude_slash_tmp=true'):
                self.assertIn(setting, command)


    def test_real_toy_workitem_exercise_reaches_done_without_fresh_history(self):
        self.workitem.write_text(MODULE_PATH.with_name('TOY-WORKITEM.md').read_text())
        result = self.run_coordinator('--exercise-revisions',
                                      '--test-command', 'python3 -m unittest -v')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'DONE')
        self.assertEqual(state['plan_rounds'], 2)
        self.assertEqual(state['exec_rounds'], 2)
        self.assertTrue(state['gate_ran'])
        self.assertTrue(state['polish']['completed'])
        self.assertEqual(sum(t['role'] == 'shadow' for t in state['turns']), 2)
        self.assertTrue((self.run_dir / 'open-findings.md').is_file())


if __name__ == '__main__':
    unittest.main()
