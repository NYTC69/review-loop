"""Batch b295-f1 (v2.9.5, FIELD-1): a per-dispatch scratch temp root for Codex read-only roles.

These tests use fake CLIs: they prove the coordinator's argv, environment, lifecycle and probe logic, not that `codex exec` honours the
permission profile. Only a real permission-probe shows that."""
import json
import os
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import test_operator_roles as tor
from paired_session import test_real_coordinator as trc
from paired_session import timeout_scale as tsc

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
        co.state.setdefault('spawn_failures', []).append({'sequence': int(stale.name.split('-')[0]), 'child_created': False})   # f1d: a recorded never-started turn
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


class NoFollowCleanupTests(unittest.TestCase):                                              # b296-f1b (v2.9.6 cross-vendor review)
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def outside(self):
        target = self.root / 'outside'
        (target / 'keep').mkdir(parents=True)
        (target / 'keep' / 'child.txt').write_text('keep\n')
        os.chmod(target, 0o755)
        return target

    def assert_untouched(self, target):
        self.assertEqual(((target / 'keep' / 'child.txt').read_text(), stat.S_IMODE(target.stat().st_mode)), ('keep\n', 0o755))

    def test_a_symlinked_or_loose_role_tmp_is_refused_and_nothing_outside_is_touched(self):
        co = self.coordinator(*CODEX_ROLES)
        target = self.outside()
        (co.run_dir / 'role-tmp').symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'role-tmp is not a real directory'):
            co._scratch_root('reviewer')
        with self.assertRaisesRegex(RuntimeError, 'role-tmp is not a real directory'):
            co._drop_scratch(co.run_dir / 'role-tmp' / 'keep')
        self.assert_untouched(target)
        self.assertEqual(sorted(os.listdir(target)), ['keep'])
        (co.run_dir / 'role-tmp').unlink()
        (co.run_dir / 'role-tmp').mkdir(mode=0o755)
        os.chmod(co.run_dir / 'role-tmp', 0o755)
        with self.assertRaisesRegex(RuntimeError, 'must be a 0700 directory'):
            co._scratch_root('reviewer')

    def test_a_symlinked_scratch_is_refused_and_a_link_inside_a_scratch_is_removed_as_a_link(self):
        co = self.coordinator(*CODEX_ROLES)
        target = self.outside()
        os.mkdir(co.run_dir / 'role-tmp', 0o700)
        (co.run_dir / 'role-tmp' / '001-reviewer').symlink_to(target)                       # a leftover scratch that is a link
        co.state.setdefault('spawn_failures', []).append({'sequence': 1, 'child_created': False})   # f1d: its turn never started, so the type check decides
        with self.assertRaisesRegex(RuntimeError, 'unexpected entry in run_dir/role-tmp, refused'):
            co._scratch_root('reviewer')
        with self.assertRaisesRegex(RuntimeError, 'unexpected entry'):
            co._drop_scratch(co.run_dir / 'role-tmp' / '001-reviewer')
        self.assert_untouched(target)
        (co.run_dir / 'role-tmp' / '001-reviewer').unlink()
        scratch = co._scratch_root('shadow')
        (scratch / 'to-dir').symlink_to(target)                                              # links inside the scratch pointing outside
        (scratch / 'to-file').symlink_to(target / 'keep' / 'child.txt')
        (scratch / 'sub').mkdir()
        (scratch / 'sub' / 'deep').symlink_to(target / 'keep')
        (scratch / 'sub' / 'locked').write_text('x')
        os.chmod(scratch / 'sub' / 'locked', 0)                                               # R1: unreadable, flagged file and flagged dir
        if hasattr(os, 'chflags'):
            os.chmod(scratch / 'sub' / 'locked', 0o600)
            os.chflags(scratch / 'sub' / 'locked', stat.UF_IMMUTABLE)
            os.chflags(scratch / 'sub', stat.UF_IMMUTABLE)
        co._drop_scratch(scratch)
        self.assertFalse(scratch.exists() or scratch.is_symlink())
        self.assert_untouched(target)
        self.assertEqual(stat.S_IMODE((target / 'keep').stat().st_mode), 0o755)

    def test_a_missing_role_tmp_needs_no_cleanup_and_a_chmod_of_the_scratch_root_fails_the_turn(self):   # R1 LOW-1, LOW-2
        co = self.coordinator(*CODEX_ROLES)
        co._drop_scratch(co.run_dir / 'role-tmp' / '001-reviewer')                            # nothing to remove, no error
        scratch = co._scratch_root('reviewer')
        os.chmod(scratch, 0o500)
        with self.assertRaisesRegex(ValueError, 'scratch temp root mode changed'):
            rc.scratch_listing(scratch)
        co._drop_scratch(scratch)
        self.assertFalse(scratch.exists())

    def test_cleanup_first_stops_what_is_left_of_the_turns_process_group(self):
        co = self.coordinator(*CODEX_ROLES)
        left = subprocess.Popen(['sleep', '30'], start_new_session=True)
        for _ in range(100):                                                                 # the child has become its group's leader
            if os.getpgid(left.pid) == left.pid: break
            __import__('time').sleep(0.02)
        co.state['turns'].append({'sequence': 7, 'role': 'shadow', 'pid': left.pid})
        co._stop_turn_group(7)
        self.assertIsNotNone(left.wait(timeout=tsc.scaled(5)))
        co._stop_turn_group(8)                                                               # no child started: nothing to stop

    def test_a_leftover_scratch_is_swept_only_after_its_turn_group_is_confirmed_gone(self):   # b296-f1c
        co = self.coordinator(*CODEX_ROLES, '--quiet-progress')
        left = co._scratch_root('reviewer')                                                  # a scratch kept by a turn whose group survived
        (left / 'work.txt').write_text('x')
        sequence = int(left.name.split('-', 1)[0])
        co.state['turns'].append({'sequence': sequence, 'role': 'reviewer', 'phase': 'EXEC', 'vendor': 'codex', 'pid': 424242})
        with patch.object(rc, 'retry_killpg_eperm', return_value=None), patch.object(rc.os, 'killpg') as killpg, \
                self.assertRaisesRegex(RuntimeError, 'leftover read-only role scratch .*' + left.name + ' kept: the process group of that turn still answers'):
            co._scratch_root('gate')                                                         # the group still answers: HOLD, directory kept
        killpg.assert_not_called()                                                            # R1: an old pid is probed, never signalled
        self.assertEqual((co.state['status'], (left / 'work.txt').read_text()), ('HOLD', 'x'))
        self.assertIn(str(left), co.state['hold_reason'])
        self.assertEqual(sorted(os.listdir(left.parent)), [left.name])                       # no new scratch either
        dead = subprocess.Popen(['true'], start_new_session=True)
        dead.wait()
        co.state['turns'][-1]['pid'] = dead.pid                                               # the group is gone: swept, the new scratch made
        fresh = co._scratch_root('gate')
        self.assertEqual(sorted(os.listdir(left.parent)), [fresh.name])
        self.assertFalse(left.exists())

    def test_only_esrch_or_a_never_started_record_lets_the_sweep_delete_a_leftover(self):   # b296-f1d
        co = self.coordinator(*CODEX_ROLES, '--quiet-progress')
        def leftover(pid=None, started=True):
            scratch = co._scratch_root('reviewer')
            (scratch / 'work.txt').write_text('x')
            sequence = int(scratch.name.split('-', 1)[0])
            if pid is not None:
                co.state['turns'].append({'sequence': sequence, 'role': 'reviewer', 'phase': 'EXEC', 'vendor': 'codex', 'pid': pid})
            if not started:
                co.state.setdefault('spawn_failures', []).append({'sequence': sequence, 'child_created': False})
            co.state['sequence'] = sequence                                                   # the next dispatch gets the next sequence
            return scratch
        for label, pid, probe in (('no receipt', None, None), ('invalid pid', 1, None),
                                  ('EPERM', 4242, PermissionError(1, 'Operation not permitted')), ('other error', 4242, OSError(5, 'I/O error'))):
            with self.subTest(label):
                scratch = leftover(pid)
                with patch.object(rc, 'retry_killpg_eperm', side_effect=probe) as probed, patch.object(rc.os, 'killpg') as killpg, \
                        self.assertRaisesRegex(RuntimeError, 'leftover read-only role scratch .*' + scratch.name + ' kept'):
                    co._scratch_root('gate')
                killpg.assert_not_called()
                self.assertEqual(((scratch / 'work.txt').read_text(), co.state['status']), ('x', 'HOLD'))
                co._drop_scratch(scratch)                                                     # reset for the next case
                co.state['status'] = 'ACTIVE'
        for label, pid, started in (('ESRCH', 4242, True), ('never started', None, False)):
            with self.subTest(label):
                scratch = leftover(pid, started)
                with patch.object(rc, 'retry_killpg_eperm', side_effect=ProcessLookupError(3, 'No such process')):
                    fresh = co._scratch_root('gate')
                self.assertFalse(scratch.exists())
                self.assertEqual(os.listdir(scratch.parent), [fresh.name])
                co._drop_scratch(fresh)

    def test_the_ctime_exemption_is_exactly_state_json_and_progress_jsonl(self):
        co = self.coordinator(*CODEX_ROLES)
        for name in ('state.json', 'progress.jsonl', 'state.json.backup', 'progress.jsonl.1'):
            (co.run_dir / name).write_text('x')
        seen = {Path(path).name for path in rc.run_dir_inodes(co.run_dir, set(), co.evidence / '001-exec-reviewer')}
        self.assertTrue({'state.json.backup', 'progress.jsonl.1'} <= seen)
        self.assertFalse({'state.json', 'progress.jsonl'} & seen)


class ExplicitDenialTests(unittest.TestCase):                                               # b296-f1b
    locals().update({name: getattr(tor.ProbeSkipTests, name) for name in ('setUp', 'co', 'probe')})

    def test_only_an_explicit_sandbox_denial_counts_as_refused(self):
        row = lambda code, output: {'error': code != 0, 'exit_code': code, 'output': output}
        for code, output in ((1, 'ln: x: Operation not permitted'), (1, 'zsh: permission denied: x'), (2, 'Read-only file system')):
            self.assertTrue(rc.explicit_denial(row(code, output)), output)
        for code, output in ((127, 'zsh: command not found: ln'), (126, 'Permission denied'), (1, 'ln: invalid option'),
                             (1, 'zsh: command not found: Operation not permitted'), (0, 'Operation not permitted'), (None, 'Operation not permitted')):
            self.assertFalse(rc.explicit_denial(row(code, output)), (code, output))
        for env, needle in (({'FAKE_CODEX_PROBE_NOT_FOUND': 'ln '}, 'ln '), ({'FAKE_CODEX_PROBE_OTHER_ERROR': '/tmp/paired-session-codex-escape-'}, '/tmp/'),
                            ({'FAKE_CODEX_PROBE_OTHER_ERROR': 'forbidden-probe'}, 'forbidden-probe')):
            with self.subTest(env=env), patch.dict(os.environ, env):
                self.h.run_dir = self.h.root / ('unknown-' + needle.strip(' /'))
                report = self.probe(self.co(*CODEX_ROLES))
                self.assertEqual(report['status'], 'UNKNOWN', report['failure_reasons'])        # 127 / another error is never PASS
                self.assertTrue(any(reason.startswith('denial-not-explicit: ') and needle in reason for reason in report['failure_reasons']))
        with patch.dict(os.environ, {'FAKE_CODEX_PROBE_NOT_FOUND': 'ln '}):                  # the Codex gate probe too
            self.h.run_dir = self.h.root / 'unknown-gate'
            report = self.probe(self.co('--reviewer-vendor', 'claude', '--gate-vendor', 'codex'))
            self.assertEqual((report['status'], report['gate_permission_probe']['status']), ('UNKNOWN', 'UNKNOWN'))


class ProbeTrackedFileTests(unittest.TestCase):                                              # b296-f1f
    locals().update({name: getattr(tor.ProbeSkipTests, name) for name in ('setUp', 'co', 'probe', 'seed', 'entries', 'forget_report', 'reuse')})

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.h.workspace, check=True, capture_output=True, text=True).stdout

    def legs(self, report, tracked):
        return [command for command in report['write_attempts_denied'] if command.startswith(('git --literal-pathspecs checkout -- ', 'rm '))], \
               ['git --literal-pathspecs checkout -- ' + tracked, 'rm ' + tracked]

    def test_a_workspace_without_tracked_txt_probes_an_existing_tracked_file_and_passes(self):   # the field shape (P1/P2, codex-cli 0.160.0)
        ws = self.h.workspace
        self.git('rm', '-q', 'tracked.txt')
        (ws / 'src').mkdir()
        (ws / 'src' / 'app.py').write_text('x = 1\n')
        self.git('add', 'src/app.py')
        self.git('commit', '-qm', 'no tracked.txt')
        for flags in (CODEX_ROLES, ('--reviewer-vendor', 'claude', '--gate-vendor', 'codex')):   # the Codex reviewer probe and a Codex gate probe
            with self.subTest(flags=flags):
                self.h.run_dir = self.h.root / ('field-' + flags[1])
                report = self.probe(self.co(*flags))
                probe = report if flags[1] == 'codex' else report['gate_permission_probe']
                self.assertEqual((report['status'], probe['status']), ('PASS', 'PASS'), probe['failure_reasons'])
                self.assertEqual((report['probe_tracked_file'], probe['probe_tracked_file']), ('.gitignore', '.gitignore'))   # first ls-files entry
                self.assertEqual(*self.legs(probe, '.gitignore'))
                self.assertTrue(all(probe['write_attempts_denied'].values()))
        self.assertEqual(sorted(p.name for p in ws.iterdir() if p.name != '.git'), ['.gitignore', 'src'])   # nothing created or changed
        self.git('diff', '--exit-code')
        self.h.run_dir = self.h.root / 'old-literal'                                         # the fake reproduces the field failure of the old literal
        with patch.object(rc, 'probe_tracked_file', return_value='tracked.txt'):
            report = self.probe(self.co(*CODEX_ROLES))
        self.assertNotEqual(report['status'], 'PASS')
        self.assertIn('denial-not-explicit: rm tracked.txt', report['failure_reasons'])
        self.assertEqual(report['write_attempt_outcomes']['git --literal-pathspecs checkout -- tracked.txt'], 'denied')   # index.lock first, as in the field

    def test_the_fake_denies_checkout_at_the_index_lock_and_rm_needs_the_file_on_disk(self):   # b296-f1g: a tracked file deleted from disk
        (self.h.workspace / 'tracked.txt').unlink()                                          # (unsandboxed git would restore it from the index)
        with patch.object(rc, 'probe_tracked_file', return_value='tracked.txt'):
            report = self.probe(self.co(*CODEX_ROLES))
        outcomes = report['write_attempt_outcomes']
        self.assertEqual((outcomes['git --literal-pathspecs checkout -- tracked.txt'], outcomes['rm tracked.txt']), ('denied', 'unknown'))
        row = next(row for row in report['observed_commands'] if row['command'] == 'git --literal-pathspecs checkout -- tracked.txt')
        self.assertTrue(rc.explicit_denial(row), row)                                        # the literal checkout leg still yields an explicit denial
        self.assertIn('index.lock', row['output'])

    def test_the_pick_skips_links_missing_and_unsafe_names_and_a_path_that_needs_quoting_is_quoted(self):
        ws = self.h.workspace
        self.git('rm', '-q', 'tracked.txt', '.gitignore')
        (ws / '-dash.txt').write_text('d\n')
        (ws / 'a-link').symlink_to('-dash.txt')
        (ws / 'b-gone.txt').write_text('g\n')
        (ws / 'c\tmid.txt').write_text('t\n')
        (ws / 'd-real').mkdir()
        (ws / 'd-real' / 'f.txt').write_text('f\n')
        quoted = "e dir/it's.txt"
        (ws / 'e dir').mkdir()
        (ws / quoted).write_text('q\n')
        self.git('add', '--', '-dash.txt', 'a-link', 'b-gone.txt', 'c\tmid.txt', 'd-real/f.txt', quoted)
        self.git('commit', '-qm', 'pick')
        (ws / 'b-gone.txt').unlink()                                                        # tracked but gone on disk
        (ws / 'd-real').rename(ws / 'd-elsewhere')                                           # tracked path now through a symlinked directory
        (ws / 'd-real').symlink_to('d-elsewhere')
        self.assertEqual(rc.probe_tracked_file(ws), quoted)
        self.git('checkout', '--', 'b-gone.txt')
        self.assertEqual(rc.probe_tracked_file(ws), 'b-gone.txt')
        (ws / 'b-gone.txt').write_text('uncommitted\n')                                    # R1: an unmodified file beats a quote-safe one with
        self.assertEqual(rc.probe_tracked_file(ws), quoted)                                  # unstaged changes; a modified one is still a fallback
        (ws / quoted).write_text('uncommitted\n')
        self.assertEqual(rc.probe_tracked_file(ws), 'b-gone.txt')
        self.git('checkout', '--', quoted)
        (ws / 'b-gone.txt').unlink()
        self.h.run_dir = self.h.root / 'quoted'
        report = self.probe(self.co(*CODEX_ROLES))
        self.assertEqual((report['status'], report['probe_tracked_file']), ('PASS', quoted), report['failure_reasons'])
        self.assertEqual(*self.legs(report, "'e dir/it'\"'\"'s.txt'"))
        self.assertEqual((ws / quoted).read_text(), 'q\n')

    def test_a_glob_like_name_is_checked_out_literally_and_a_sibling_keeps_its_unstaged_bytes(self):   # b296-f1g
        ws = self.h.workspace
        self.git('rm', '-q', 'tracked.txt', '.gitignore')
        (ws / '*.txt').write_text('glob name\n')
        (ws / 'a.txt').write_text('a base\n')
        self.git('--literal-pathspecs', 'add', '--', '*.txt', 'a.txt')
        self.git('commit', '-qm', 'glob-like name')
        self.assertEqual(rc.probe_tracked_file(ws), 'a.txt')                                 # defense in depth: a plain name is preferred
        (ws / 'a.txt').write_text('unstaged work\n')
        self.assertEqual(rc.probe_tracked_file(ws), '*.txt')                                 # ... but never over unstaged changes, and never refused
        self.assertIn('a.txt', self.git('ls-files', '--', '*.txt').split())                  # as a plain pathspec, '*.txt' would reach a.txt
        report = self.probe(self.co(*CODEX_ROLES))
        self.assertEqual((report['status'], report['probe_tracked_file']), ('PASS', '*.txt'), report['failure_reasons'])
        self.assertEqual(*self.legs(report, "'*.txt'"))
        self.h.run_dir = self.h.root / 'escape-literal'                                     # a broken surface: the checkout really runs
        with patch.dict(os.environ, {'FAKE_CODEX_PROBE_ESCAPE': 'pathspecs checkout'}):
            report = self.probe(self.co(*CODEX_ROLES))
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['write_attempt_outcomes']["git --literal-pathspecs checkout -- '*.txt'"], 'no-trace', report['failure_reasons'])   # it ran, exit 0
        self.assertIn("no-explicit-denial: git --literal-pathspecs checkout -- '*.txt'", report['failure_reasons'])
        self.assertEqual(((ws / 'a.txt').read_text(), (ws / '*.txt').read_text()), ('unstaged work\n', 'glob name\n'))
        self.git('checkout', '--', '*.txt')                                                  # the sensitivity check: without --literal-pathspecs
        self.assertEqual((ws / 'a.txt').read_text(), 'a base\n')                            # the same name resets the sibling

    def test_a_refusal_leaves_the_shared_cache_entry_alone(self):                            # b296-f1g: no probe evidence, so no F4 void
        co, entry = self.seed(*CODEX_ROLES)
        before = entry.read_bytes()
        self.git('rm', '-q', 'tracked.txt', '.gitignore')
        self.git('commit', '-qm', 'no tracked file')
        later = self.co(*CODEX_ROLES)                                                        # the same run dir: the same cache key
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False):
            self.assertFalse(later.permission_probe())
        self.assertEqual((self.entries(), entry.read_bytes()), ([entry], before))            # not voided, like an aborted probe turn
        ok, why = self.reuse(later)
        self.assertEqual((ok, why), (False, 'probe cache: the last permission-probe did not complete'))   # this run never revives it
        shutil.rmtree(self.h.run_dir)                                                        # a new run with the same key reuses the flag-surface PASS
        ok, why = self.reuse(self.co(*CODEX_ROLES))
        self.assertEqual((ok, why), (True, ''))

    def test_a_workspace_with_no_tracked_regular_file_is_refused_before_any_turn(self):
        ws = self.h.workspace
        self.git('rm', '-q', '--cached', 'tracked.txt')
        (ws / 'tracked.txt').unlink()
        (ws / '.gitignore').unlink()                                                         # the only tracked file is gone on disk
        self.git('commit', '-qm', 'only .gitignore left')
        co = self.co(*CODEX_ROLES)
        sequence = co.state['sequence']
        (co.run_dir / 'permission-probe.json').write_text('{"status": "PASS"}\n')            # an older report: superseded, not left valid
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False):
            self.assertFalse(co.permission_probe())
            self.assertFalse(co.probe_passed()[0])
        self.assertEqual((co.state['sequence'], co.state['turns'], co.state['status']), (sequence, [], 'HOLD'))
        self.assertIn('permission probe refused before any turn: the workspace has no tracked regular file', co.state['hold_reason'])
        self.assertFalse((co.run_dir / 'permission-probe.json').exists())
        self.assertTrue((co.run_dir / 'permission-probe.superseded.json').exists())
        self.assertEqual(sorted(p.name for p in ws.iterdir()), ['.git'])

    def test_a_delete_that_lands_fails_the_probe_and_the_file_is_not_restored(self):
        ws = self.h.workspace
        for flags in (CODEX_ROLES, ('--reviewer-vendor', 'claude', '--gate-vendor', 'codex')):
            with self.subTest(flags=flags), patch.dict(os.environ, {'FAKE_CODEX_PROBE_ESCAPE': 'rm '}):
                self.h.run_dir = self.h.root / ('rm-' + flags[1])
                co = self.co(*flags)
                report = self.probe(co)
                probe = report if flags[1] == 'codex' else report['gate_permission_probe']
                self.assertEqual((report['status'], probe['status']), ('FAIL', 'FAIL'))
                self.assertFalse(os.path.lexists(ws / '.gitignore'))                         # never silently restored
                self.assertTrue(any(reason.startswith('probe-tracked-file-escaped: .gitignore was deleted') for reason in probe['failure_reasons']), probe['failure_reasons'])
                self.assertIn('NOT restored', report['message'])
                self.assertIn('probe-tracked-file-escaped: .gitignore', co.state['hold_reason'])
                self.git('checkout', '--', '.gitignore')                                     # the test restores it for the next case
        self.h.run_dir = self.h.root / 'rm-error'                                            # R1: the delete lands, then the turn errors
        co = self.co(*CODEX_ROLES)
        invoke = co.invoke
        def landed_then_error(role, *args, **kwargs):
            result = invoke(role, *args, **kwargs)
            if role == 'probe': raise RuntimeError('fake turn error after the turn')
            return result
        with patch.dict(os.environ, {'FAKE_CODEX_PROBE_ESCAPE': 'rm '}), patch.object(co, 'invoke', side_effect=landed_then_error):
            report = self.probe(co)
        self.assertEqual(report['status'], 'FAIL')
        self.assertFalse(os.path.lexists(ws / '.gitignore'))
        self.assertIn('NOT restored', report['message'])
        self.assertIn('probe-tracked-file-escaped: .gitignore', co.state['hold_reason'])


class ExecProfileSelectionTests(unittest.TestCase):                                          # b296-f1e
    locals().update({name: getattr(tor.ProbeSkipTests, name) for name in ('setUp', 'co', 'probe')})

    def test_codex_exec_refuses_a_permission_profile_flag_and_the_probe_fails_on_one(self):
        profile = rc.codex_readonly_profile_args()
        for flag in (['-P', 'paired_session_readonly'], ['--permission-profile', 'paired_session_readonly'], ['--permission-profile=paired_session_readonly']):
            with self.subTest(flag=flag[0]):                                                 # codex-cli 0.160.0: `codex exec` has no -P
                done = subprocess.run([sys.executable, str(trc.FAKE), 'exec', *flag, *profile[2:], '-'], input='Role: reviewer, persistent. Phase: PLAN.',
                                      text=True, capture_output=True, timeout=30)
                self.assertEqual((done.returncode, done.stdout), (2, ''))
                self.assertEqual(done.stderr, f"error: unexpected argument '{flag[0].split('=')[0]}' found\n")
        old = ['-P', rc.CODEX_READONLY_PROFILE, *profile[2:]]                                 # the v2.9.6 candidate's argv
        with patch.object(rc, 'codex_readonly_profile_args', return_value=old):
            report = self.probe(self.co(*CODEX_ROLES))
        self.assertEqual((report['status'], report['failure_reasons']), ('FAIL', ['probe-turn-error: CLI exit 2']))   # R1 l1: the refusal, not a missed scratch write


if __name__ == '__main__':
    unittest.main()
