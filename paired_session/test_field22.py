"""FIELD-22 (supervisor's parallel real runs on v2.11.1): two runs on the default ~/.codex. Run A's Codex author turn held
"codex turn changed global config: codex_config" because run B appended its own workspace trust entry meanwhile. An
exact trust append for the workspace of ANOTHER live paired-session run (a private lease record in this user's
workspace-lease directory, pid alive, state ACTIVE) is now an expected change (`trusted-concurrent-run-workspace`);
everything else stays a hard finding. `resume --acknowledge-codex-trust` also accepts a trust-only append for a
workspace with any such lease record (finished runs too); a completed-turn HOLD recovers with a plain `resume`."""
import hashlib
import json
import os
import subprocess
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
ENTRY = lambda path: '\n[projects.' + json.dumps(str(path)) + ']\ntrust_level = "trusted"\n'


class Field22Tests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.config = self.h.test_home / '.codex' / 'config.toml'
        self.original = self.config.read_bytes()
        self.addCleanup(self.config.write_bytes, self.original)
        self.leases = ExitStack()
        self.addCleanup(self.leases.close)

    def other_run(self, name, status='ACTIVE', hold=True):
        """Another paired-session run: its workspace, run dir (state status) and workspace lease (held by this process)."""
        workspace, run_dir = (self.h.root / ('ws-' + name)).resolve(), self.h.root / ('run-' + name)
        workspace.mkdir()
        run_dir.mkdir()
        (run_dir / 'state.json').write_text(json.dumps({'status': status, 'workspace': str(workspace)}))
        (run_dir / '.coordinator.lock').write_text(json.dumps({'pid': os.getpid()}))   # as run_lease writes it
        self.addCleanup(lambda: rc.workspace_lease_path(workspace).unlink(missing_ok=True) if workspace.exists() else None)
        if hold:
            self.leases.enter_context(rc.workspace_lease(workspace, run_dir))
        else:   # a finished run: the lease file stays, nobody holds it
            with rc.workspace_lease(workspace, run_dir):
                pass
        return workspace, run_dir

    # --- discovery and attribution ----------------------------------------------------------------------------------------
    def test_lease_discovery_needs_a_live_active_run_and_a_genuine_lease_file(self):
        live, live_run = self.other_run('live')
        held_not_active, _ = self.other_run('hold', status='HOLD')
        finished, finished_run = self.other_run('done', status='ACTIVE', hold=False)
        found = rc.concurrent_run_workspaces(self.h.workspace)
        self.assertEqual(found.get(str(live)), live_run.name)
        self.assertNotIn(str(held_not_active), found)
        self.assertNotIn(str(self.h.workspace.resolve()), found)
        lease = rc.workspace_lease_path(finished)
        lease.write_text(json.dumps({**json.loads(lease.read_text()), 'pid': 987654321}))   # its process is gone
        self.assertNotIn(str(finished), rc.concurrent_run_workspaces(self.h.workspace))
        self.assertEqual(rc.concurrent_run_workspaces(self.h.workspace, live=False).get(str(finished)), finished_run.name)
        forged = lease.with_name('0' * 64 + '.lock')   # a record whose file name is not that workspace's lease path
        forged.write_text(json.dumps({'pid': os.getpid(), 'run_dir': str(finished_run), 'workspace': str(finished)}))
        forged.chmod(0o600)
        self.addCleanup(forged.unlink, missing_ok=True)
        lease.unlink()
        self.assertNotIn(str(finished), rc.concurrent_run_workspaces(self.h.workspace, live=False))

    def test_lease_records_need_a_matching_run_dir_and_bad_json_is_skipped(self):   # field22 R1
        other, other_run = self.other_run('b')
        state = other_run / 'state.json'
        state.write_text(json.dumps({'status': 'ACTIVE', 'workspace': str(self.h.root / 'elsewhere')}))
        self.assertNotIn(str(other), rc.concurrent_run_workspaces(self.h.workspace, live=False))   # the run dir names another workspace
        state.write_text(json.dumps({'status': 'ACTIVE', 'workspace': str(other)}))
        (other_run / '.coordinator.lock').write_text(json.dumps({'pid': 987654321}))
        self.assertNotIn(str(other), rc.concurrent_run_workspaces(self.h.workspace))   # another process holds the run lease
        self.assertIn(str(other), rc.concurrent_run_workspaces(self.h.workspace, live=False))
        for bad_state in ('null', '[]', '"ACTIVE"'):   # the host's real lease records may be listed too: look only at ours
            state.write_text(bad_state)
            self.assertNotIn(str(other), rc.concurrent_run_workspaces(self.h.workspace, live=False))
        state.write_text(json.dumps({'status': 'ACTIVE', 'workspace': str(other)}))
        lease = rc.workspace_lease_path(other)
        self.leases.close()   # release the lease before overwriting its record
        for bad_lease in ('[]', 'null', '{"workspace": 3}'):
            lease.write_text(bad_lease)
            self.assertNotIn(str(other), rc.concurrent_run_workspaces(self.h.workspace, live=False))

    def test_attribution_names_the_concurrent_run_and_keeps_the_rest_strict(self):
        other, other_run = self.other_run('b')
        before = rc.global_config_snapshot(self.h.test_home)
        self.config.write_bytes(self.original + ENTRY(other).encode())
        after = rc.global_config_snapshot(self.h.test_home)
        concurrent = {str(other): other_run.name}
        result = rc.attribute_global_config_changes(before, after, [self.h.workspace], concurrent)
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(result['expected_changes'], [{'file': 'codex_config', 'change': 'trusted-concurrent-run-workspace',
                                                       'workspaces': [str(other)], 'runs': [other_run.name]}])
        self.assertEqual(result['warnings'], [])
        self.assertEqual(rc.attribute_global_config_changes(before, after, [self.h.workspace])['status'], 'FAIL')   # as before
        self.config.write_bytes(self.original + ENTRY(other).encode() + b'\n[other]\nflag = true\n')
        mixed = rc.attribute_global_config_changes(before, rc.global_config_snapshot(self.h.test_home), [self.h.workspace], concurrent)
        self.assertEqual(mixed['status'], 'FAIL')

    def linked_worktree(self, name):   # a linked worktree: Codex trusts it and its main checkout root
        main, tree = self.h.root / ('main-' + name), self.h.root / ('wt-' + name)
        env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}
        subprocess.run(['git', 'init', '-q', str(main)], check=True, env=env)
        subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'base'], cwd=main, check=True, env=env)
        subprocess.run(['git', 'worktree', 'add', '-q', str(tree)], cwd=main, check=True, env=env)
        self.assertEqual(len(rc._codex_trust_paths(tree)), 2)
        return tree.resolve()

    def test_acknowledgment_removes_four_new_blocks_over_older_ones(self):   # field22 R2: two linked worktrees, two blocks each
        own, other = self.linked_worktree('a'), self.linked_worktree('b')
        older = [self.h.root / f'older{i}' for i in range(8)]
        for path in older: path.mkdir()
        base = self.original + b''.join(ENTRY(path.resolve()).encode() for path in older)
        added = b''.join(ENTRY(path).encode() for workspace in (own, other) for path in rc._codex_trust_paths(workspace))
        self.config.write_bytes(base + added)
        digest = hashlib.sha256(base).hexdigest()
        self.assertTrue(rc.trust_entry_only_since_hash(self.config, digest, own, extra=[other, *older]))
        self.assertFalse(rc.trust_entry_only_since_hash(self.config, digest, own, extra=[*older]))   # B not lease-recorded
        self.config.write_bytes(base + added + b'\n[other]\nflag = true\n')
        self.assertFalse(rc.trust_entry_only_since_hash(self.config, digest, own, extra=[other, *older]))

    # --- in a run ---------------------------------------------------------------------------------------------------------
    def launch(self, *extra, env=None):
        result = self.h.run_coordinator('--shadow', 'off', '--adversarial-gate', 'off', *extra, env=env)
        state = json.loads((self.h.run_dir / 'state.json').read_text())
        authors = [t for t in state['turns'] if t.get('role') == 'author' and t.get('phase') == 'EXEC']
        return result, state, authors

    def test_another_live_runs_trust_append_during_a_codex_author_turn_passes(self):
        other, other_run = self.other_run('b')
        result, state, authors = self.launch(env={'FAKE_CONCURRENT_TRUST_APPEND': str(other)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state['status'], 'DONE')
        changes = authors[0]['global_config_changes']
        self.assertEqual(changes['status'], 'PASS')
        self.assertIn({'file': 'codex_config', 'change': 'trusted-concurrent-run-workspace', 'workspaces': [str(other)],
                       'runs': [other_run.name]}, changes['expected_changes'])

    def test_an_append_without_a_live_run_still_holds_and_a_plain_resume_recovers(self):
        held, _ = self.other_run('held', status='HOLD')   # leased but not ACTIVE
        for label, target in (('unleased', self.h.root / 'ws-unleased'), ('not-active', held)):
            with self.subTest(label):
                self.h.run_dir = self.h.root / ('run-a-' + label)
                self.config.write_bytes(self.original)
                once = self.h.root / ('once-' + label)
                env = {'FAKE_CONCURRENT_TRUST_APPEND': str(target), 'FAKE_CONCURRENT_TRUST_ONCE': str(once)}
                result, state, authors = self.launch(env=env)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(state['hold_reason'], 'codex turn changed global config: codex_config')
                command = self.h.command('--shadow', 'off', '--adversarial-gate', 'off') + ['--skip-probe']
                command[2] = 'resume'
                again = subprocess.run(command, cwd=self.h.root, env={**os.environ, **env}, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr)   # a fresh baseline: the turn re-runs
                self.assertEqual(json.loads((self.h.run_dir / 'state.json').read_text())['status'], 'DONE')

    def test_trust_acknowledgment_accepts_another_runs_entry_and_nothing_else(self):
        finished, _ = self.other_run('done', status='HOLD', hold=False)   # a lease record of a run that has ended
        history = b''.join(ENTRY(self.other_run(f'old{i}', status='HOLD', hold=False)[0]).encode() for i in range(8))
        for label, target, accepted, base in (('paired-workspace', finished, True, self.original),
                                              ('unrelated', self.h.root / 'ws-x', False, self.original),
                                              ('after eight older trust blocks', finished, True, self.original + history)):   # field22 R1
            with self.subTest(label):
                self.original = base
                self.config.write_bytes(self.original)
                self.h.run_dir = self.h.root / ('ack-' + label)
                co = self.h.coordinator()
                receipt = {'sequence': 1, 'pid': 987654321, 'role': 'author', 'vendor': 'codex', 'phase': 'EXEC',
                           'workspace': str(self.h.workspace), 'global_codex_home': str(co.global_codex_home),
                           'global_config_home': str(co.global_config_home),
                           'global_codex_before': {'codex_config': hashlib.sha256(self.original).hexdigest()}}
                co.state['uncertain_active'] = receipt; co.save()
                self.config.write_bytes(self.original + ENTRY(target).encode())
                with patch('os.killpg', side_effect=ProcessLookupError):
                    with self.assertRaisesRegex(RuntimeError, 'global Codex config changed'):
                        co.resume(retry_uncertain=True)
                co.args.action = 'resume'; co.args.acknowledge_codex_trust = self.h.run_dir.name
                with patch('os.killpg', side_effect=ProcessLookupError), patch.object(co, 'drive', return_value='ACTIVE'):
                    if accepted:
                        self.assertEqual(co.resume(), 'ACTIVE')
                        self.assertEqual(co.state['codex_trust_acknowledgments'][0]['run_id'], self.h.run_dir.name)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'global Codex config changed'): co.resume()
                        self.assertFalse(co.state.get('codex_trust_acknowledgments'))


if __name__ == '__main__':
    unittest.main()
