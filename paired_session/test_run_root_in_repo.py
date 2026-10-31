"""RR1: repository run roots, excluded coordinator writes and external scratch."""
import copy
import json
import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import readonly_guard as rg
from paired_session import test_real_coordinator as trc

rc = trc.rc
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
           'fake_codex_cli', 'fake_claude_cli', 'command', 'run_coordinator', 'coordinator', 'run_operator_action')


class RunRootTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def test_ignored_run_root_and_other_repository_are_accepted(self):
        run = self.workspace / '.review-loop/runs/id'
        rc.prepare_run_directory(self.workspace, run, True)
        ignore = self.workspace / '.review-loop/.gitignore'
        self.assertEqual(ignore.read_bytes(), b'*\n!config.md\n')
        rc.prepare_run_directory(self.workspace, run)
        rc.prepare_run_directory(self.root / 'lane', run)  # another checkout is outside the workspace

    def test_other_inside_locations_are_refused(self):
        for path in (self.workspace, self.workspace / 'run', self.workspace / '.review-loop/runs',
                     self.workspace / '.review-loop/codex-home/id'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, '--run-dir must be outside --workspace'):
                rc.prepare_run_directory(self.workspace, path, True)
        rc.prepare_run_directory(self.workspace, self.root / '.review-loop/runs/id', True)

    def test_existing_ignore_is_never_overwritten_and_nonignored_run_is_refused(self):
        ignore = self.workspace / '.review-loop/.gitignore'
        ignore.parent.mkdir()
        ignore.write_text('!runs/\n')
        self.run_dir = self.workspace / '.review-loop/runs/id'
        result = self.run_coordinator()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('REFUSED:', result.stdout)
        self.assertIn('is not gitignored', result.stdout)
        self.assertFalse((self.run_dir / 'state.json').exists())
        self.assertEqual(ignore.read_text(), '!runs/\n')
        ignore.write_text('runs/\n')
        rc.prepare_run_directory(self.workspace, self.run_dir, True)
        self.assertEqual(ignore.read_text(), 'runs/\n')

    def test_readonly_capture_excludes_internal_roots_but_checks_siblings(self):
        run = self.workspace / '.review-loop/runs/id'
        rc.prepare_run_directory(self.workspace, run, True)
        run.mkdir(parents=True)
        home = self.workspace / '.review-loop/codex-home'
        home.mkdir()
        for name in ('pr', 'logs'):
            (self.workspace / '.review-loop' / name).mkdir()
        sibling = self.workspace / '.review-loop/other'
        sibling.write_text('before')
        recorded = rg.capture(self.workspace, self.root / 'capture')
        before = rc.git_snapshot(self.workspace)[0]
        (run / 'state.json').write_text('{}')
        (home / 'config.toml').write_text('changed')
        for name in ('pr', 'logs'):
            (self.workspace / '.review-loop' / name / 'new-child').mkdir()
        self.assertEqual(rg.ignored_changes(self.workspace, recorded), [])
        self.assertEqual(rc.git_snapshot(self.workspace)[0], before)
        sibling.write_text('after, different length')
        self.assertIn('.review-loop/other', rg.ignored_changes(self.workspace, recorded))
        self.assertNotIn('.review-loop', recorded['ignored_meta'])
        from paired_session import author_write_guard
        inventory = author_write_guard.snapshot(self.workspace)
        self.assertFalse(any('/' + prefix + '/' in name
                             for name in inventory for prefix in rg.INTERNAL_WORKSPACE_DIRS))

    def test_new_run_in_repo_reaches_accepted_with_external_role_scratch(self):
        self.run_dir = self.workspace / '.review-loop/runs/id'
        result = self.run_coordinator('--reviewer-vendor', 'claude', '--gate-vendor', 'codex', '--polish-round', 'off')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        active_state = json.loads((self.run_dir / 'state.json').read_text())
        scratch = Path(active_state['scratch_root'])
        self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), 0o700)
        accepted = self.run_operator_action('accept', '--reviewer-vendor', 'claude', '--gate-vendor', 'codex',
                                            '--polish-round', 'off', '--reason', 'RR1 fixture accepted')
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['acceptance_state'], 'ACCEPTED')
        scratch = Path(state['scratch_root'])
        self.addCleanup(shutil.rmtree, scratch, True)
        self.assertIsNone(rc.git_work_tree_root(scratch))
        self.assertFalse(scratch.exists())
        self.assertNotIn('WARNING: the run directory', result.stderr)
        turns = [t for t in state['turns'] if t['role'] == 'gate']
        self.assertTrue(turns)
        for turn in turns:
            env = turn['environment_overrides']
            self.assertEqual(Path(env['TMPDIR']).parent, scratch / 'role-tmp')
            self.assertEqual(env['TMP'], env['TMPDIR'])
            self.assertEqual(env['TEMP'], env['TMPDIR'])
            self.assertFalse(Path(env['TMPDIR']).exists())
            trc.assert_codex_readonly_profile(self, turn['command'])
        co = rc.Coordinator(rc.parser().parse_args(self.command('--reviewer-vendor', 'claude', '--gate-vendor', 'codex', '--polish-round', 'off')[2:]))
        self.assertFalse(scratch.exists())
        self.assertEqual(co.author_temp_dir, scratch / 'author-tmp')
        self.assertIn(str(co.author_temp_dir), co._author_sandbox_overrides()['sandbox_workspace_write.writable_roots'])
        mirror = self.root / 'mirror'
        co._mirror_workspace(mirror)
        self.assertFalse((mirror / '.review-loop/runs').exists())

    def test_external_mirrors_missing_resume_and_no_workspace_copies_in_repo(self):
        self.run_dir = self.workspace / '.review-loop/runs/mirrors'
        co = self.coordinator()
        self.addCleanup(shutil.rmtree, co.scratch_base, True)
        marker = self.workspace / 'unique-workspace.test.js'
        marker.write_text('workspace test fixture')
        co.capture_review_baseline()
        archive = co._capture_baseline('review-start')
        co.state['review_only'] = {'mirror': str(archive)}
        co.save()
        marker.write_text('changed workspace test fixture')
        co.materialize_review_context()
        self.assertIn('changed workspace test fixture', (co.context / 'delta-since-last-review.patch').read_text())
        self.assertEqual(list((self.workspace / '.review-loop').rglob(marker.name)), [])
        shutil.rmtree(co.scratch_base)
        restored = self.coordinator()
        restored.materialize_review_context()
        self.assertIn('changed workspace test fixture', (restored.context / 'delta-since-last-review.patch').read_text())
        self.assertIsNone(restored._missing_review_start_issue())
        self.assertEqual((restored._review_start_root() / marker.name).read_text(), 'workspace test fixture')
        self.assertEqual(list((self.workspace / '.review-loop').rglob(marker.name)), [])
        archive.unlink()
        self.assertEqual(restored.resume(), 'HOLD')
        self.assertIn('FIELD-27 review-start mirror is missing', restored.state['hold_reason'])

    def test_partial_mirrors_are_restored_once_per_coordinator_and_capture_invalidates(self):
        self.run_dir = self.workspace / '.review-loop/runs/partial'
        co = self.coordinator()
        self.addCleanup(shutil.rmtree, co.scratch_base, True)
        marker = self.workspace / 'unchanged.txt'
        marker.write_text('unchanged workspace content')
        co.capture_review_baseline()
        archive = co._capture_baseline('review-start')
        co.state['review_only'] = {'mirror': str(archive)}
        co.save()
        expected = co._review_start_paths()
        co._baseline('last-review')
        for name in ('last-review', 'review-start'):
            (co.mirror_base / name / marker.name).unlink()
        # Includes a partial tree left by an interrupted capture: the intact archive wins.
        (co.mirror_base / 'last-review/partial-only').write_text('incomplete capture')
        restored = self.coordinator()
        with patch.object(rc.tarfile, 'open', wraps=rc.tarfile.open) as opened:
            restored.materialize_review_context()
            self.assertEqual((restored.context / 'delta-since-last-review.patch').read_text(), '')
            self.assertEqual(restored._review_start_paths(), expected)
            restored.materialize_review_context()
            self.assertEqual(restored._review_start_paths(), expected)
            self.assertEqual(opened.call_count, 2)
        self.assertFalse((restored.mirror_base / 'last-review/partial-only').exists())
        marker.write_text('new baseline')
        restored.capture_review_baseline()
        with patch.object(rc.tarfile, 'open', wraps=rc.tarfile.open) as opened:
            self.assertEqual((restored._baseline('last-review') / marker.name).read_text(), 'new baseline')
            restored._baseline('last-review')
            self.assertEqual(opened.call_count, 1)

    def test_baseline_archive_roundtrip_and_path_hygiene(self):
        import io
        import tarfile
        co = self.coordinator()
        self.addCleanup(shutil.rmtree, co.scratch_base, True)
        (self.workspace / 'empty').touch()
        executable = self.workspace / 'executable'
        executable.write_bytes(b'#!/bin/sh\n')
        executable.chmod(0o755)
        (self.workspace / 'link').symlink_to('/outside/target')
        archive = co._capture_baseline('last-review')
        shutil.rmtree(co.scratch_base)
        root = co._baseline('last-review')
        self.assertEqual((root / 'empty').read_bytes(), b'')
        self.assertEqual(stat.S_IMODE((root / 'executable').stat().st_mode), 0o755)
        self.assertEqual(os.readlink(root / 'link'), '/outside/target')
        for name in ('/absolute', '../escape'):
            with self.subTest(name=name):
                with tarfile.open(archive, 'w') as bundle:
                    member = tarfile.TarInfo(name)
                    member.size = 1
                    bundle.addfile(member, io.BytesIO(b'x'))
                shutil.rmtree(root, ignore_errors=True)
                with self.assertRaisesRegex(ValueError, 'unsafe baseline archive member'):
                    co._baseline('last-review')

    def test_legacy_baselines_remain_expanded(self):
        with patch.object(rc, 'new_scratch_root', side_effect=lambda run: run):
            co = self.coordinator()
        co.state.pop('scratch_root', None)
        for name in ('last-review', 'review-start'):
            root = co._capture_baseline(name)
            self.assertEqual(root, co.internal / name)
            self.assertTrue(root.is_dir())
            self.assertFalse((co.internal / (name + '.tar')).exists())
        co.state['review_only'] = {'mirror': str(root)}
        self.assertIsNone(co._missing_review_start_issue())
        self.assertEqual(co._review_start_root(), root)

    def test_terminal_cleanup_preserves_legacy_and_removes_new_scratch(self):
        for legacy in (False, True):
            self.run_dir = self.root / ('cleanup-legacy' if legacy else 'cleanup-new')
            with patch.object(rc, 'new_scratch_root', side_effect=(lambda run: run) if legacy else rc.new_scratch_root):
                co = self.coordinator()
            if legacy:
                co.state.pop('scratch_root')
            for status in ('ACCEPTED', 'REPORTED', 'ABORTED'):
                co.scratch_base.mkdir(parents=True, exist_ok=True)
                co._progress_terminal(status)
                self.assertEqual(co.scratch_base.exists(), legacy)

    def test_permission_probe_in_repo_keeps_synthetic_homes_external(self):
        self.run_dir = self.workspace / '.review-loop/runs/permission-probe'
        rc.prepare_run_directory(self.workspace, self.run_dir, create_ignore=True)
        log = self.root / 'sandbox-homes.jsonl'
        command = self.command('--gate-vendor', 'claude')
        command[2] = 'permission-probe'
        result = subprocess.run(command, cwd=self.root, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env={**os.environ,
                                    'FAKE_CODEX_SANDBOX_MODE': 'deny',
                                    'FAKE_CODEX_SANDBOX_LOG': str(log)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads((self.run_dir / 'permission-probe.json').read_text())
        self.assertEqual(report['status'], 'PASS', report)
        state = json.loads((self.run_dir / 'state.json').read_text())
        scratch = Path(state['scratch_root'])
        self.addCleanup(shutil.rmtree, scratch, True)
        self.assertIsNone(rc.git_work_tree_root(scratch))
        invocations = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(len(invocations), 4)
        for invocation in invocations:
            home = Path(invocation['codex_home'])
            self.assertEqual(home.parent, scratch)
            self.assertEqual(invocation['codex_home_entries'], ['config.toml'])
            self.assertFalse(home.exists())

    def test_sandbox_home_uses_effective_scratch_and_is_cleaned_on_success_and_error(self):
        real_run = subprocess.run
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.run_dir = (self.root / 'legacy-sandbox-run' if legacy else
                                self.workspace / '.review-loop/runs/sandbox-home')
                if legacy:
                    with patch.object(rc, 'new_scratch_root', side_effect=lambda run: run):
                        co = self.coordinator('--author-vendor', 'codex')
                    co.state.pop('scratch_root')
                    co.save()
                co = self.coordinator('--author-vendor', 'codex')
                if not legacy:
                    self.addCleanup(shutil.rmtree, co.scratch_base, True)
                    self.assertIsNone(rc.git_work_tree_root(co.scratch_base))
                else:
                    self.assertEqual(co.scratch_base, self.run_dir)
                    self.assertNotIn('scratch_root', co.state)
                co.author_temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = co._codex_config_bytes()
                for fail in (False, True):
                    with self.subTest(subprocess_error=fail):
                        homes = []

                        def dispatch(argv, *args, **kwargs):
                            if 'sandbox' in argv:
                                home = Path(kwargs['env']['CODEX_HOME'])
                                homes.append(home)
                                self.assertEqual(home.parent, co.scratch_base)
                                self.assertTrue(home.name.startswith('codex-sandbox-home-'))
                                self.assertEqual(sorted(p.name for p in home.iterdir()), ['config.toml'])
                                self.assertEqual((home / 'config.toml').read_bytes(), source)
                                if not legacy:
                                    self.assertIsNone(rc.git_work_tree_root(home))
                                if fail:
                                    raise subprocess.TimeoutExpired(argv, 30)
                            return real_run(argv, *args, **kwargs)

                        with patch.object(rc.subprocess, 'run', side_effect=dispatch):
                            result = co._codex_sandbox_escape_check(
                                self.workspace, 'external_tmpdir', self.root / 'sandbox-denied-target')
                        self.assertEqual(result['status'], 'FAIL' if fail else 'PASS', result)
                        if fail:
                            self.assertIn('TimeoutExpired', result['error'])
                        self.assertEqual(len(homes), 1)
                        self.assertFalse(homes[0].exists())

    def test_legacy_state_keeps_frozen_paths_on_resume_status_abort_stop(self):
        # Build exactly the old path-dependent manifest, then remove the new optional field.
        with patch.object(rc, 'new_scratch_root', side_effect=lambda run: run):
            co = rc.Coordinator(rc.parser().parse_args(self.command('--reviewer-vendor', 'claude', '--gate-vendor', 'codex', '--polish-round', 'off')[2:]))
        co.state.pop('scratch_root')
        co.save()
        frozen = copy.deepcopy(co.state['role_dispatch_manifest'])
        agents = {p.name: p.read_bytes() for p in (self.run_dir / 'agents').glob('*.md')}
        for action in ('status', 'resume', 'stop', 'abort'):
            command = self.command('--reviewer-vendor', 'claude', '--gate-vendor', 'codex', '--polish-round', 'off')
            command[2] = action
            restored = rc.Coordinator(rc.parser().parse_args(command[2:]))
            self.assertEqual(restored.author_temp_dir, self.run_dir / 'author-tmp')
            self.assertEqual(restored.scratch_base, self.run_dir)
            self.assertEqual(restored.mirror_base, self.run_dir / 'internal')
            self.assertEqual(restored.state['role_dispatch_manifest'], frozen)
            self.assertNotIn('scratch_root', restored.state)
            command.append('--skip-probe')
            result = subprocess.run(command, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(result.returncode, 2 if action == 'abort' else 0, result.stdout + result.stderr)
            if action == 'resume':
                self.assertIn('WARNING: the run directory ' + str(self.run_dir), result.stderr)
            disk = json.loads((self.run_dir / 'state.json').read_text())
            self.assertNotIn('scratch_root', disk)
            self.assertEqual(disk['role_dispatch_manifest'], frozen)
        self.assertEqual(agents, {p.name: p.read_bytes() for p in (self.run_dir / 'agents').glob('*.md')})


if __name__ == '__main__':
    unittest.main()
