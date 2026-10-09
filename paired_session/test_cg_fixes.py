"""Batch CG (v2.9.2): Codex plugin scope, missing config.toml, probe guard re-check, exact trust attribution, Claude .cc-writes."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import claude_author_probe as cap
from paired_session import codex_capability_guard as guard
from paired_session import test_operator_roles as tor
from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'coordinator')


def trust(path):
    return '[projects.' + json.dumps(str(path)) + ']\ntrust_level = "trusted"\n'


class PluginBundleScopeTests(unittest.TestCase):
    def inspect(self, config_text):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, workspace = root / 'home', root / 'workspace'
            workspace.mkdir()
            bundle = home / 'plugins/cache/chatgpt-global/documents/1.0'
            bundle.mkdir(parents=True)
            (bundle / '.app.json').write_text('{"apps":{}}')
            if config_text is not None: (home / 'config.toml').write_text(config_text)
            return guard.inspect(home, workspace)

    def test_a_cached_bundle_is_a_finding_unless_the_effective_config_disables_plugins(self):
        for text in (None, 'model = "x"\n', '[features]\nplugins = true\n', '[features]\nremote_plugin = false\napps = false\n',
                     '# plugins = false\n', '[profiles.p.features]\nplugins = false\n', '[features]\nplugins = false\nplugins = true\n'):
            with self.subTest(config=text):
                result = self.inspect(text)
                self.assertEqual(result['status'], 'FAIL', result)
                self.assertTrue(any('plugin MCP or app bundle' in issue for issue in result['issues']), result)
        for text in ('[features]\nplugins = false\n', '[ features ]\napps = false\nplugins = false  # off\n', 'features.plugins = false\n'):
            with self.subTest(config=text):
                self.assertEqual(self.inspect(text)['status'], 'PASS')

    def test_the_launch_flag_makes_cached_bundles_inert_and_they_stay_recorded(self):   # rel210-fixCG
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home, workspace = root / 'home', root / 'workspace'
            workspace.mkdir()
            app = home / 'plugins/cache/chatgpt-global/documents-router/0.1.3/.app.json'
            manifest = home / 'plugins/cache/market/tool/1.0/.codex-plugin/plugin.json'
            for path, text in ((app, '{"apps":{"documents":{}}}'), (manifest, '{"mcpServers":"./.mcp.json"}')):
                path.parent.mkdir(parents=True)
                path.write_text(text)
            result = guard.inspect(home, workspace, platform='linux', launch_plugins_off=True)
            self.assertEqual((result['status'], result['issues']), ('PASS', []), result)
            self.assertEqual(set(result['plugin_bundles_inert']), {str(app.resolve()), str(manifest.resolve())})
            self.assertEqual(result['plugin_bundles_inert'][str(app.resolve())], hashlib.sha256(app.read_bytes()).hexdigest())
            with patch.object(Path, 'read_bytes', side_effect=OSError('denied')):   # an unreadable bundle stays an issue
                self.assertIn('cannot inspect plugin capability bundle: ' + str(app.resolve()),
                              guard.inspect(home, workspace, platform='linux', launch_plugins_off=True)['issues'])
            (home / 'config.toml').write_text('[mcp_servers.x]\ncommand = "touch"\n')   # config MCP is not a bundle
            result = guard.inspect(home, workspace, platform='linux', launch_plugins_off=True)
            self.assertEqual(result['status'], 'FAIL')
            self.assertIn('MCP servers configured in ' + str(home.resolve() / 'config.toml'), result['issues'])

    def test_mcp_servers_and_other_capabilities_still_fail_when_plugins_are_off(self):
        for extra in ('[mcp_servers.evil]\ncommand = "touch"\n', 'notify = ["touch"]\n'):
            result = self.inspect('[features]\nplugins = false\n' + extra)
            self.assertEqual(result['status'], 'FAIL', result)


class CodexDispatchTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def test_every_codex_dispatch_disables_plugins_and_the_flags_bind_it(self):
        use_lifecycle_on(self, self)
        co = self.coordinator('--author-vendor', 'codex', '--reviewer-vendor', 'codex', '--gate-vendor', 'codex')
        schema = Path(self.root) / 'schema.json'
        for role in ('author', 'reviewer', 'gate', 'probe', 'gate-probe'):
            command = co._codex_command(role, schema, True)
            self.assertIn(('-c', 'features.plugins=false'), list(zip(command, command[1:])), role)
        for flags in (co.author_flags(), co.reviewer_flags(), co.gate_flags()):
            self.assertEqual(flags['codex_plugins_argv'], ['-c', 'features.plugins=false'])
        digests = (co.author_flags_digest(), co.reviewer_flags_digest(), co.gate_flags_digest())
        with patch.object(rc, 'CODEX_PLUGINS_OFF', ()):
            self.assertTrue(all(old != new for old, new in zip(digests, (co.author_flags_digest(), co.reviewer_flags_digest(), co.gate_flags_digest()))))

    def test_every_codex_role_argv_carries_the_launch_flag_so_the_guard_treats_bundles_as_inert(self):   # rel210-fixCG
        co = self.coordinator('--author-vendor', 'codex', '--reviewer-vendor', 'codex', '--gate-vendor', 'codex')
        schema = Path(self.root) / 'schema.json'
        for role in ('author', 'reviewer', 'shadow', 'probe', 'gate', 'gate-probe'):   # W roles dispatch as author or reviewer
            command = co.command(role, schema, True)
            self.assertIn(('-c', 'features.plugins=false'), list(zip(command, command[1:])), role)
        bundle = self.test_home / '.codex/plugins/cache/chatgpt-global/documents-router/0.1.3/.app.json'
        bundle.parent.mkdir(parents=True)
        bundle.write_text('{"apps":{}}')
        result = co.codex_capabilities()
        self.assertEqual((result['status'], list(result['plugin_bundles_inert'])), ('PASS', [str(bundle.resolve())]))
        with patch.object(rc, 'CODEX_PLUGINS_OFF', ()):   # without the launch flag the bundle is live again
            self.assertIn('plugin MCP or app bundle configured in ' + str(bundle), co.codex_capabilities()['issues'])
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        result = co.invoke('reviewer', 'PLAN', co._review_prompt('reviewer', 'snapshot'), rc.review_schema())
        turn = next(row for row in co.state['turns'] if row['sequence'] == result['sequence'])
        self.assertEqual(turn['codex_plugin_bundles_inert'], {str(bundle.resolve()): hashlib.sha256(bundle.read_bytes()).hexdigest()})
        with patch.object(co, '_codex_command', return_value=['codex', 'exec', '-']), \
                self.assertRaisesRegex(RuntimeError, 'Codex argv lacks -c features.plugins=false'):   # fail closed at dispatch
            co._invoke_once('reviewer', 'PLAN', co._review_prompt('reviewer', 'snapshot'), rc.review_schema())

    def test_a_missing_config_toml_is_an_empty_config_everywhere(self):
        use_lifecycle_on(self, self)
        config = self.test_home / '.codex/config.toml'
        co = self.coordinator('--author-vendor', 'codex')
        config.write_bytes(b'')
        empty = co._codex_policy_digest()
        config.unlink()
        self.assertEqual(co._codex_config_bytes(), b'')
        self.assertIsNotNone(empty)
        self.assertEqual(co._codex_policy_digest(), empty)
        self.assertEqual(co.author_flags()['codex_config_sha256'], empty)
        config.write_text(trust(self.workspace.resolve()))   # codex creates the file with only the trust block
        self.assertEqual(co._codex_policy_digest(), empty)
        config.write_text('model = "x"\n')
        self.assertNotEqual(co._codex_policy_digest(), empty)
        config.unlink()
        sandbox_home = co.run_dir / 'x'
        with patch.object(co, '_program_state', return_value=({}, None)), patch.object(co, '_codex_sandbox_profile_args', return_value=[]):
            outcome = co._codex_sandbox_escape_check(self.workspace, 'probe', sandbox_home)   # reads config.toml for the synthetic home
        self.assertNotIn('error', outcome)
        self.assertEqual(outcome['source_config_sha256'], hashlib.sha256(b'').hexdigest())

    def test_a_probe_that_adds_a_capability_fails_with_the_finding_named(self):
        use_lifecycle_on(self, self)
        config = self.test_home / '.codex/config.toml'
        for inject in (False, True):
            with self.subTest(inject=inject):
                self.run_dir = self.root / f'recheck-{inject}'
                config.write_text('model = "gpt-6-luna"\n')
                co = self.coordinator('--author-vendor', 'codex', '--reviewer-vendor', 'codex')
                real = co._author_permission_probe

                def probe_turn():
                    result = real()
                    if inject: config.write_text(config.read_text() + '[mcp_servers.late]\ncommand = "touch"\n')   # as Codex could during its turn
                    return result

                with patch.object(co, '_author_permission_probe', side_effect=probe_turn):
                    co.permission_probe()
                report = json.loads((co.run_dir / 'permission-probe.json').read_text())
                reasons = [r for r in report['failure_reasons'] if r.startswith('codex-capability-guard-after-probe')]
                self.assertEqual(bool(reasons), inject, report['failure_reasons'])
                if inject:
                    self.assertEqual(report['status'], 'FAIL')
                    self.assertIn('MCP servers', reasons[0])


class TrustAttributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def git(self, cwd, *args):
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.test', *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def attribute(self, before, after, workspaces):
        return rc._only_codex_workspace_trust_append({'raw': before}, {'raw': after}, workspaces)

    def test_a_new_block_before_a_later_table_is_attributed_even_with_an_older_trust_block(self):
        ws = self.root / 'ws'
        ws.mkdir()
        old = '[projects."/old"]\ntrust_level = "trusted"\n'
        before = 'model = "x"\n\n' + old + '\n[features]\nplugins = false\n'
        after = 'model = "x"\n\n' + old + '\n' + trust(ws) + '\n[features]\nplugins = false\n'
        self.assertEqual(self.attribute(before, after, [ws]), [str(ws)])
        self.assertEqual(self.attribute(before, after.replace('plugins = false', 'plugins = true'), [ws]), [])
        self.assertEqual(self.attribute(before, after.replace('model = "x"', 'model = "y"'), [ws]), [])
        self.assertEqual(self.attribute(before, after, [self.root / 'other']), [])
        appended = before + '\n' + trust(ws)
        self.assertEqual(self.attribute(before, appended, [ws]), [str(ws)])
        self.assertEqual(self.attribute(before, appended + trust(ws), [ws]), [])      # a second copy of the block is a leftover

    def test_a_block_that_adopts_the_keys_of_the_table_around_it_is_not_attributed(self):
        ws = self.root / 'ws'
        ws.mkdir()
        before = '[hooks]\nenabled = true\n'
        self.assertEqual(self.attribute(before, '[hooks]\n' + trust(ws) + 'enabled = true\n', [ws]), [])
        self.assertEqual(self.attribute(before, before + trust(ws) + 'extra = 1\n', [ws]), [])

    def test_two_workspaces_and_a_missing_before_file(self):
        a, b = self.root / 'a', self.root / 'b'
        a.mkdir(); b.mkdir()
        self.assertEqual(self.attribute('', trust(a) + '\n' + trust(b), [a, b]), sorted([str(a), str(b)]))
        self.assertEqual(self.attribute('', trust(a), [a, b]), [str(a)])

    def test_a_linked_worktree_may_be_trusted_through_its_own_main_checkout_only(self):
        main, other = self.root / 'main', self.root / 'other'
        for repo in (main, other):
            repo.mkdir()
            self.git(repo, 'init', '-q')
            (repo / 'f.txt').write_text('x\n')
            self.git(repo, 'add', 'f.txt')
            self.git(repo, 'commit', '-qm', 'c')
        linked = self.root / 'linked'
        self.git(main, 'worktree', 'add', '-q', str(linked))
        before = 'model = "x"\n\n[features]\nplugins = false\n'
        with_main = 'model = "x"\n\n' + trust(main) + '\n[features]\nplugins = false\n'
        self.assertEqual(self.attribute(before, with_main, [linked]), [str(main)])
        self.assertEqual(self.attribute(before, with_main, [other]), [])                  # not that workspace's repository
        self.assertEqual(self.attribute(before, with_main.replace(str(main), str(self.root)), [linked]), [])   # a parent directory is not the main root
        self.assertEqual(self.attribute(before, with_main.replace(str(main), str(other)), [linked]), [])
        self.assertEqual(self.attribute(before, with_main, [main]), [str(main)])
        config = self.root / 'config.toml'
        config.write_text(with_main)
        self.assertTrue(rc.trust_entry_only_since_hash(config, hashlib.sha256(before.encode()).hexdigest(), linked))
        self.assertFalse(rc.trust_entry_only_since_hash(config, hashlib.sha256(before.encode()).hexdigest(), other))
        config.write_text(trust(main))
        self.assertTrue(rc.trust_entry_only_since_hash(config, None, linked))              # a missing file is an empty file

    def test_only_a_linked_worktree_of_a_normal_checkout_has_a_main_root(self):
        main = self.root / 'main'
        main.mkdir()
        self.git(main, 'init', '-q')
        (main / 'f.txt').write_text('x\n')
        self.git(main, 'add', 'f.txt')
        self.git(main, 'commit', '-qm', 'c')
        sub = main / 'sub'
        sub.mkdir()
        bare = self.root / 'bare.git'
        self.git(self.root, 'clone', '-q', '--bare', str(main), str(bare))
        from_bare = self.root / 'from-bare'
        self.git(bare, 'worktree', 'add', '-q', str(from_bare))
        self.assertEqual(rc._codex_trust_paths(sub), [str(sub)])             # a subdirectory is not a linked worktree
        self.assertEqual(rc._codex_trust_paths(from_bare), [str(from_bare)])   # the common dir of a bare repository is not `.git`
        self.assertEqual(rc._codex_trust_paths(self.root / 'missing'), [str(self.root / 'missing')])

    def test_the_policy_digest_strips_the_main_checkout_trust_of_a_linked_workspace_only(self):
        helper = trc.RealCoordinatorTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.addCleanup(helper.tearDown)
        main = helper.workspace
        linked = helper.root / 'linked'
        self.git(main, 'worktree', 'add', '-q', str(linked))
        helper.workspace = linked
        co = helper.coordinator('--author-vendor', 'codex')
        config = helper.test_home / '.codex/config.toml'
        before = co._codex_policy_digest()
        config.write_text(config.read_text() + '\n' + trust(main.resolve()) + '\n' + trust(linked.resolve()))
        self.assertEqual(co._codex_policy_digest(), before)
        config.write_text(config.read_text() + '\n' + trust(helper.root.resolve()))
        self.assertNotEqual(co._codex_policy_digest(), before)


class ClaudeCcWritesTests(unittest.TestCase):
    locals().update({name: getattr(tor.ClaudeAuthorProbeTests, name) for name in ('setUp', 'co', 'probe')})

    def probe_with(self, build):
        """Run the author probe; `build(ws)` makes what the real CLI left behind in the probe workspace once the turn is over."""
        co = self.co()
        real = co.invoke

        def invoke(*args, **kwargs):
            result = real(*args, **kwargs)
            build(Path(kwargs['workspace_override']))
            return result

        with patch.object(co, 'invoke', side_effect=invoke):
            return self.probe(co=co)[1]

    def test_the_cli_created_empty_directories_are_not_an_escape(self):
        out = self.probe_with(lambda ws: (ws / '.claude/.cc-writes').mkdir(parents=True, mode=0o700))
        self.assertEqual((out['status'], out['model_escape_failed_targets']), ('PASS', []), out)
        out = self.probe_with(lambda ws: (ws / '.claude').mkdir())     # only .claude, no .cc-writes
        self.assertEqual(out['status'], 'PASS', out)

    def test_anything_else_under_claude_stays_an_escape(self):
        def file_in_cc_writes(ws):
            (ws / '.claude/.cc-writes').mkdir(parents=True)
            (ws / '.claude/.cc-writes/x').write_text('x')

        def settings(ws):
            (ws / '.claude/.cc-writes').mkdir(parents=True)
            (ws / '.claude/settings.json').write_text('{}')

        def linked_cc_writes(ws):
            (ws / '.claude').mkdir()
            os.symlink(ws.parent / 'outside', ws / '.claude/.cc-writes')

        def linked_claude(ws): os.symlink(ws.parent / 'outside', ws / '.claude')
        def claude_file(ws): (ws / '.claude').write_text('x')
        def claude_dir_with_other_dir(ws): (ws / '.claude/other').mkdir(parents=True)
        for build, flagged in ((file_in_cc_writes, ('.claude/.cc-writes', '.claude/.cc-writes/x')), (settings, ('.claude/settings.json',)),
                               (linked_cc_writes, ('.claude/.cc-writes',)), (linked_claude, ('.claude',)), (claude_file, ('.claude',)),
                               (claude_dir_with_other_dir, ('.claude/other',))):
            with self.subTest(build=build.__name__):
                out = self.probe_with(build)
                self.assertEqual(out['status'], 'FAIL', out)
                self.assertTrue(all(any(t.endswith('/workspace/' + name) for t in out['model_escape_failed_targets']) for name in flagged), out)

    def test_cli_created_dirs_need_this_uid_and_real_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            (ws / '.claude/.cc-writes').mkdir(parents=True)
            kind = lambda p: (os.lstat(p).st_mode & 0o170000,)
            listing = {str(ws / '.claude'): kind(ws / '.claude'), str(ws / '.claude/.cc-writes'): kind(ws / '.claude/.cc-writes')}
            self.assertEqual(cap.cli_created_dirs(ws, listing, listing), {str(ws / '.claude'), str(ws / '.claude/.cc-writes')})
            with patch.object(os, 'getuid', return_value=os.getuid() + 1):
                self.assertEqual(cap.cli_created_dirs(ws, listing, listing), set())
            self.assertEqual(cap.cli_created_dirs(ws, listing, {}), set())      # absent from one listing
