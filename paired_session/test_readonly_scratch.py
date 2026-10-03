"""Batch b295-f1 (v2.9.5, FIELD-1): a per-dispatch scratch temp root for Codex read-only roles.

These tests use fake CLIs: they prove the coordinator's argv, environment, lifecycle and probe logic, not that `codex exec` honours the
permission profile. Only a real permission-probe shows that."""
import json
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import test_operator_roles as tor
from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'coordinator')
CODEX_ROLES = ('--reviewer-vendor', 'codex', '--gate-vendor', 'codex')


class ScratchRootTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def test_the_scratch_is_run_owned_private_and_outside_the_snapshot_and_the_author_roots(self):
        co = self.coordinator(*CODEX_ROLES)
        root = co._scratch_root('reviewer')
        self.assertEqual(root, co.run_dir / 'role-tmp' / f"{co.state['sequence'] + 1:03d}-reviewer")
        self.assertEqual((stat.S_IMODE(root.stat().st_mode), stat.S_IMODE(root.parent.stat().st_mode)), (0o700, 0o700))
        for outside in (co.workspace, co.author_temp_dir, *(Path(p) for p in co._author_sandbox_overrides()['sandbox_workspace_write.writable_roots'])):
            self.assertFalse(root == outside or outside in root.parents, outside)
        before = rc.git_snapshot(self.workspace)[0]
        (root / 'scratch.txt').write_text('x')
        self.assertEqual(rc.git_snapshot(self.workspace)[0], before)                       # not in the workspace snapshot
        stale = root
        root = co._scratch_root('gate')                                                     # a leftover (crash, uncertain turn) is swept
        self.assertFalse(stale.exists())
        co._drop_scratch(root)
        self.assertEqual(list((co.run_dir / 'role-tmp').iterdir()), [])
        for role in ('author',):
            self.assertIsNone(co._scratch_root(role))
        self.run_dir = self.root / 'claude-roles'
        claude = self.coordinator('--reviewer-vendor', 'claude', '--gate-vendor', 'claude')   # decision B: Claude roles keep Claude Code's own TMPDIR
        self.assertIsNone(claude._scratch_root('reviewer'))
        with self.assertRaisesRegex(RuntimeError, 'outside role-tmp'):
            co._drop_scratch(co.workspace)

    def test_each_dispatch_gets_its_own_usable_temp_root_which_is_gone_after_the_turn(self):
        log = self.root / 'tmp.jsonl'                                                       # FIELD-6 shape: Claude reviewer, Codex gate
        done = self.run_coordinator('--polish-round', 'off', '--reviewer-vendor', 'claude', '--gate-vendor', 'codex', env={'FAKE_TMP_LOG': str(log)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        readonly = [turn for turn in state['turns'] if turn.get('environment_overrides', {}).get('TMPDIR', '').startswith(str(self.run_dir / 'role-tmp'))]
        self.assertEqual([turn['role'] for turn in readonly], ['gate'])                    # Claude reviewer/shadow: no override (decision B)
        self.assertTrue(all('TMPDIR' not in turn.get('environment_overrides', {}) for turn in state['turns'] if turn['vendor'] == 'claude'))
        roots = [turn['environment_overrides']['TMPDIR'] for turn in readonly]
        for turn in readonly:
            env = turn['environment_overrides']
            self.assertEqual((env['TMP'], env['TEMP'], Path(env['TMPDIR']).parent), (env['TMPDIR'], env['TMPDIR'], self.run_dir / 'role-tmp'))
            self.assertIn('scratch_entries', turn)
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        gate = [row for row in rows if row['prompt'].startswith('You are an adversarial')]
        self.assertTrue(gate and all(row['mkdtemp'].startswith(row['TMPDIR'] + '/') for row in gate))   # a tmp_path-style test has a temp dir
        self.assertTrue(all(row['TMPDIR'] not in roots for row in rows if row['vendor'] == 'codex' and 'implementer' in row['prompt']))
        self.assertEqual(list((self.run_dir / 'role-tmp').iterdir()), [])                   # each one removed after its turn

    def test_the_scratch_is_removed_after_a_failing_and_an_uncertain_turn_and_a_hard_link_there_fails_the_turn(self):
        co = self.coordinator(*CODEX_ROLES, '--quiet-progress')
        co.state['config']['workitem_reviewer_commands'] = []
        with patch.dict(os.environ, {'FAKE_RATE_LIMIT': '1'}), self.assertRaisesRegex(RuntimeError, 'rate_limited'):
            co.invoke('reviewer', 'PLAN', 'Role: reviewer, persistent. Phase: PLAN.', rc.review_schema())
        self.assertEqual(list((co.run_dir / 'role-tmp').iterdir()), [])
        target = co.context / 'workitem.md'
        with patch.dict(os.environ, {'FAKE_CODEX_SCRATCH_HARDLINK': str(target)}), self.assertRaisesRegex(RuntimeError, 'left a hard link in its scratch'):
            co.invoke('reviewer', 'PLAN', 'Role: reviewer, persistent. Phase: PLAN.', rc.review_schema())
        self.assertEqual((list((co.run_dir / 'role-tmp').iterdir()), target.stat().st_nlink), ([], 1))
        evidence = co.evidence / 'operator-note-N001.txt'                                   # the same-volume residual: link, write the same bytes, unlink
        evidence.write_text('operator note\n')
        (co.internal / 'kept.txt').write_text('kept\n')
        for target in (evidence, co.internal / 'kept.txt'):
            with self.subTest(target=target.name), patch.dict(os.environ, {'FAKE_CODEX_LINK_WRITE_UNLINK': str(target)}), \
                    self.assertRaisesRegex(RuntimeError, 'changed run-dir files during its turn .*' + target.name):
                co.invoke('reviewer', 'PLAN', 'Role: reviewer, persistent. Phase: PLAN.', rc.review_schema())
            self.assertEqual(target.stat().st_nlink, 1)
        roots = [turn['environment_overrides']['TMPDIR'] for turn in co.state['turns'] if turn['role'] == 'reviewer']
        self.assertEqual((len(roots), len(set(roots))), (4, 4))                             # never shared across dispatches
        scratch = co._scratch_root('shadow')                                               # an uncertain turn: archived once its child stopped
        (scratch / 'sub').mkdir()
        (scratch / 'sub' / 'f').write_text('x')
        if hasattr(os, 'chflags'):
            os.chflags(scratch / 'sub' / 'f', stat.UF_IMMUTABLE)                            # R1 m1: a role may set uchg in its own scratch
        os.chmod(scratch / 'sub', 0o500)
        co.archive_abandoned_turn({'role': 'shadow', 'vendor': 'codex', 'sequence': 99, 'environment_overrides': {'TMPDIR': str(scratch)}})
        self.assertFalse(scratch.exists())


class ScratchProbeTests(unittest.TestCase):
    locals().update({name: getattr(tor.ProbeSkipTests, name) for name in ('setUp', 'co', 'probe')})

    def test_the_probe_needs_the_scratch_write_and_still_fails_a_workspace_write(self):
        report = self.probe(self.co(*CODEX_ROLES))
        self.assertEqual((report['status'], report['snapshot_unchanged']), ('PASS', True), report['failure_reasons'])
        self.assertTrue(all(report['write_attempts_denied'].values()))
        self.assertNotEqual(report['author_permission_probe']['status'], 'FAIL')           # the author escape probe does not flag role-tmp
        self.assertEqual(report['reviewer_flags']['codex_readonly_profile'], rc.codex_readonly_profile_args())
        self.h.run_dir = self.h.root / 'no-scratch'
        with patch.dict(os.environ, {'FAKE_CODEX_SKIP_SCRATCH': '1'}):
            report = self.probe(self.co(*CODEX_ROLES))
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn('scratch-write-not-observed: ' + rc.SCRATCH_PROBE_COMMAND, report['failure_reasons'])
        self.h.run_dir = self.h.root / 'workspace-granted'                                  # a profile that grants the workspace is caught
        granted = [arg.replace('{"."="read"}', '{"."="write"}') for arg in rc.codex_readonly_profile_args()]
        with patch.object(rc, 'codex_readonly_profile_args', return_value=granted):
            report = self.probe(self.co(*CODEX_ROLES))
        self.assertEqual((report['status'], report['snapshot_unchanged']), ('FAIL', False))
        self.assertFalse(any(report['write_attempts_denied'].values()))

    def test_the_probe_proves_only_the_scratch_is_writable_and_a_hard_link_is_refused(self):   # R1 M1/M2
        report = self.probe(self.co(*CODEX_ROLES))
        commands = report['write_attempts_denied']
        for marker in ('/tmp/paired-session-codex-escape-', '"$TMPDIR/../paired-session-codex-escape-', '/context/paired-session-codex-escape-',
                       '/run/paired-session-codex-escape-', 'ln '):
            self.assertTrue(any(marker in command and denied for command, denied in commands.items()), marker)
        self.assertFalse([p for p in self.h.run_dir.rglob('paired-session-*') if 'codex-escape' in p.name or 'link-source' in p.name])
        for label, marker in (('slash_tmp', '/tmp/paired-session-codex-escape-'), ('role_tmp', '$TMPDIR/..'), ('run_dir', '/escape-run_dir/paired-session-codex-escape-')):
            with self.subTest(label), patch.dict(os.environ, {'FAKE_CODEX_PROBE_ESCAPE': marker}):
                self.h.run_dir = self.h.root / ('escape-' + label)
                report = self.probe(self.co(*CODEX_ROLES))
                self.assertEqual(report['status'], 'FAIL')
                self.assertIn('codex-readonly-write-escaped: ' + label, report['failure_reasons'])
                self.assertFalse(list(Path('/tmp').glob('paired-session-codex-escape-*')) if label == 'slash_tmp' else
                                 [p for p in self.h.run_dir.rglob('paired-session-codex-escape-*')])   # removed after the check
        for flags in (CODEX_ROLES, ('--reviewer-vendor', 'claude', '--gate-vendor', 'codex')):        # the reviewer probe and a Codex gate probe
            with self.subTest(flags=flags), patch.dict(os.environ, {'FAKE_CODEX_PROBE_ESCAPE': 'ln '}):
                self.h.run_dir = self.h.root / ('link-' + flags[1])
                report = self.probe(self.co(*flags))
                probe = report if flags[1] == 'codex' else report['gate_permission_probe']
                self.assertEqual((report['status'], probe['status']), ('FAIL', 'FAIL'))
                self.assertTrue(any('hard link' in reason or 'hardlink' in reason for reason in probe['failure_reasons']), probe['failure_reasons'])
                self.assertFalse(list(self.h.run_dir.glob('paired-session-link-source-*')))

    def test_the_new_profile_changes_the_codex_read_only_flags_digest_only(self):
        co = self.co(*CODEX_ROLES)
        codex_digest, codex_gate = co.reviewer_flags_digest(), co.gate_flags_digest()
        with patch.object(rc, 'codex_readonly_profile_args', return_value=['-c', 'sandbox_mode="read-only"']):
            self.assertNotEqual((co.reviewer_flags_digest(), co.gate_flags_digest()), (codex_digest, codex_gate))
        self.h.run_dir = self.h.root / 'claude-roles'
        claude = self.co('--reviewer-vendor', 'claude', '--gate-vendor', 'claude')
        self.assertNotIn('codex_readonly_profile', claude.reviewer_flags())


if __name__ == '__main__':
    unittest.main()
