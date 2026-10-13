"""V317-SP: SECURITY blocks the run's changes and records untouched base files."""
import contextlib
import io
import json
import unittest
from unittest import mock

from paired_session import test_worktree_lifecycle as wt
from scripts import delivery_scope as ds
from scripts import security_preflight as sp

rc = wt.rc


class SecurityRunScopeTests(unittest.TestCase):
    locals().update({name: getattr(wt.WorktreeLifecycleActivationTests, name)
                    for name in (*wt._HELPERS, 'args')})
    setUp = wt.WorktreeLifecycleActivationTests.setUp

    def check_kind(self, name, content, note, receipt_key, hold):
        path = self.workspace / name
        path.write_bytes(content)
        rc.subprocess.run(['git', 'add', '-f', name], cwd=self.workspace, check=True)
        rc.subprocess.run(['git', 'commit', '-qm', 'existing file'], cwd=self.workspace, check=True)
        result = self.run_coordinator('--lifecycle-mode', 'on')
        self.assertIn(wt.DONE, result.stdout, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('NOTE: ' + note), 1)
        self.assertIn(name, result.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        [receipt] = [r for r in state['lifecycle']['receipts'] if r['stage'] == 'SECURITY']
        self.assertEqual((receipt['status'], state['status']), ('READY', 'DONE'))
        rows = receipt[receipt_key] if receipt_key == 'preexisting_sensitive_paths' else receipt['preflight'][receipt_key]
        self.assertIn(name, [r['path'] if isinstance(r, dict) else r for r in rows])
        again = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertNotIn('HOLD:', again.stdout)
        self.assertNotIn('NOTE: ' + note, again.stdout)
        self.run_dir = self.root / 'touched-run'
        path.write_bytes(content + b'\n# run edit\n')
        touched = self.run_coordinator('--lifecycle-mode', 'on')
        self.assertIn(hold, touched.stdout, touched.stdout + touched.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD')
        self.assertEqual(state['lifecycle']['stage'], 'SECURITY')

    def test_sensitive_path(self):
        self.check_kind('deploy.pem', b'ordinary file\n',
                        'pre-existing sensitive-looking paths, not changed by this run:',
                        'preexisting_sensitive_paths', 'SECURITY sensitive paths: deploy.pem')

    def test_content_hit(self):
        self.check_kind('settings.txt', ('key = AKIA' + 'ABCDEFGHIJKLMNOP' + '\n').encode(),
                        'pre-existing security content hits, not changed by this run:',
                        'preexisting_findings', 'aws-access-key-id in settings.txt')

    def test_oversized_file(self):
        self.check_kind('large.txt', b'ordinary data\n' * (sp.MAX_FILE_BYTES // 14 + 1),
                        'content scan skipped oversized pre-existing files, unchanged or removed by this run:',
                        'skipped_preexisting_files', 'content scan limit exceeded: large.txt')

    def test_stop_after_gate_has_no_security_stage(self):
        (self.workspace / 'deploy.pem').write_text('ordinary file\n')
        rc.subprocess.run(['git', 'add', '-f', 'deploy.pem'], cwd=self.workspace, check=True)
        result = self.run_coordinator('--lifecycle-mode', 'off')
        self.assertIn(wt.DONE, result.stdout, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertFalse(state.get('lifecycle'))
        self.assertFalse(any(t['phase'] == 'SECURITY' for t in state['turns']))

    def test_preflight_receipt_partitions_hits_and_skips_only_untouched_base_files(self):
        (self.workspace / 'deploy.pem').write_text('ordinary file\n')
        (self.workspace / 'settings.txt').write_text('AKIA' + 'ABCDEFGHIJKLMNOP' + '\n')
        (self.workspace / 'large.txt').write_bytes(b'x' * (sp.MAX_FILE_BYTES + 1))
        rc.subprocess.run(['git', 'add', '-f', 'deploy.pem', 'settings.txt', 'large.txt'],
                          cwd=self.workspace, check=True)
        rc.subprocess.run(['git', 'commit', '-qm', 'base files'], cwd=self.workspace, check=True)
        repo = ds.repository(str(self.workspace))
        state = ds.capture_state(repo)
        manifest = ds.build_manifest(ds.build_baseline(repo, ['.'], state), state)
        report = sp.scan(str(self.workspace), manifest=manifest, run_changes_only=True)
        self.assertEqual(report['status'], 'clean')
        self.assertEqual(report['findings'], [])
        self.assertEqual({r['path'] for r in report['preexisting_findings']}, {'deploy.pem', 'settings.txt'})
        self.assertEqual(report['skipped_preexisting_files'], ['large.txt'])
        expected_bytes = sum(row['size'] for name, row in state['worktree'].items() if name != 'large.txt')
        self.assertEqual(report['scanned_bytes'], expected_bytes)
        # The total budget counts only bytes read, including scans of small old files.
        with mock.patch.object(sp, 'MAX_TOTAL_BYTES', expected_bytes):
            report = sp.scan(str(self.workspace), manifest=manifest, run_changes_only=True)
        self.assertEqual(report['status'], 'clean')
        self.assertEqual(report['scanned_bytes'], expected_bytes)
        with mock.patch.object(sp, 'MAX_TOTAL_BYTES', expected_bytes - 1):
            report = sp.scan(str(self.workspace), manifest=manifest, run_changes_only=True)
        self.assertEqual(report['status'], 'incomplete')
        self.assertIn('content scan limit exceeded', report['reason'])

    def test_note_is_not_reprinted_when_resuming_an_unrelated_security_hold(self):
        (self.workspace / 'deploy.pem').write_text('ordinary file\n')
        rc.subprocess.run(['git', 'add', '-f', 'deploy.pem'], cwd=self.workspace, check=True)
        rc.subprocess.run(['git', 'commit', '-qm', 'base'], cwd=self.workspace, check=True)
        co, _ = wt.WorktreeLifecycleActivationTests.at_security(self)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(co, '_security_review_turn', side_effect=[
                {'status': 'HOLD', 'reason': 'unrelated reviewer failure'}, {'status': 'APPROVE', 'reason': None}]):
            co.worktree_security_turn()
            self.assertEqual(co.state['status'], 'HOLD')
            co = rc.Coordinator(self.args(action='resume'))
            with mock.patch.object(co, '_security_review_turn', return_value={'status': 'APPROVE', 'reason': None}):
                self.assertEqual(co.resume(), 'DONE')
        self.assertEqual(co.state['status'], 'DONE')
        self.assertEqual(output.getvalue().count('NOTE: pre-existing sensitive-looking paths'), 1)
        self.assertNotIn('HOLD: SECURITY sensitive paths', output.getvalue())
        self.assertEqual(len([r for r in co.state['lifecycle']['receipts'] if r['stage'] == 'SECURITY']), 2)

    def test_staged_changes_and_renames_still_block_and_total_limit_is_unchanged(self):
        path = self.workspace / 'settings.txt'
        path.write_text('safe\n')
        rc.subprocess.run(['git', 'add', 'settings.txt'], cwd=self.workspace, check=True)
        rc.subprocess.run(['git', 'commit', '-qm', 'base'], cwd=self.workspace, check=True)
        repo = ds.repository(str(self.workspace))
        baseline = ds.build_baseline(repo, ['.'], ds.capture_state(repo))
        path.write_text('AKIA' + 'ABCDEFGHIJKLMNOP' + '\n')
        rc.subprocess.run(['git', 'add', 'settings.txt'], cwd=self.workspace, check=True)
        path.write_text('safe\n')
        report = sp.scan(str(self.workspace), manifest=ds.build_manifest(baseline, ds.capture_state(repo)),
                         run_changes_only=True)
        self.assertEqual(report['status'], 'blocked')
        self.assertTrue(any(r.get('source') == 'index' for r in report['findings']))
        rc.subprocess.run(['git', 'add', 'settings.txt'], cwd=self.workspace, check=True)
        path.rename(self.workspace / 'api_key.rb')
        report = sp.scan(str(self.workspace), manifest=ds.build_manifest(baseline, ds.capture_state(repo)),
                         run_changes_only=True)
        self.assertTrue(any(r['path'] == 'api_key.rb' for r in report['findings']))
        with mock.patch.object(sp, 'MAX_FILE_BYTES', 1), mock.patch.object(sp, 'MAX_TOTAL_BYTES', 2):
            report = sp.scan(str(self.workspace), manifest=ds.build_manifest(baseline, ds.capture_state(repo)),
                             run_changes_only=True)
        self.assertEqual(report['status'], 'incomplete')

    def commit_files(self, files):
        for name, content in files.items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        rc.subprocess.run(['git', 'add', '-f', *files], cwd=self.workspace, check=True)
        rc.subprocess.run(['git', 'commit', '-qm', 'base fixtures'], cwd=self.workspace, check=True)

    def scan_current(self, baseline):
        repo = ds.repository(str(self.workspace))
        return sp.scan(str(self.workspace), manifest=ds.build_manifest(baseline, ds.capture_state(repo)),
                       run_changes_only=True)

    def test_incomplete_keeps_unchanged_hits_out_of_hold_and_records_notes(self):
        self.commit_files({'api_key.rb': b'ordinary file\n',
                           'settings.txt': ('AKIA' + 'ABCDEFGHIJKLMNOP' + '\n').encode()})
        co, _ = wt.WorktreeLifecycleActivationTests.at_security(self)
        (self.workspace / 'zz-large.txt').write_bytes(b'x' * (sp.MAX_FILE_BYTES + 1))
        co.state['lifecycle']['candidate_oid'] = rc.git_snapshot(self.workspace)[0]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(co, '_security_review_turn',
                                                                  side_effect=AssertionError('review before clean scan')):
            co.worktree_security_turn()
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('zz-large.txt', co.state['hold_reason'])
        self.assertNotIn('api_key.rb', co.state['hold_reason'])
        self.assertNotIn('settings.txt', co.state['hold_reason'])
        [receipt] = [r for r in co.state['lifecycle']['receipts'] if r['stage'] == 'SECURITY']
        self.assertEqual(receipt['preflight']['findings'], [])
        self.assertEqual({r['path'] for r in receipt['preflight']['preexisting_findings']},
                         {'api_key.rb', 'settings.txt'})
        self.assertIn('NOTE: pre-existing security content hits', output.getvalue())

    def test_many_preexisting_hits_are_bounded_and_notes_show_twenty_paths(self):
        names = [f'dist/{n:03d}.js.map' for n in range(501)]
        self.commit_files({name: b'ordinary file\n' for name in names})
        co, _ = wt.WorktreeLifecycleActivationTests.at_security(self)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(co, '_security_review_turn',
                                    return_value={'status': 'APPROVE', 'reason': None}):
            co.worktree_security_turn()
        self.assertEqual(co.state['status'], 'DONE')
        [receipt] = [r for r in co.state['lifecycle']['receipts'] if r['stage'] == 'SECURITY']
        preflight = receipt['preflight']
        self.assertEqual(preflight['findings'], [])
        self.assertEqual(len(preflight['preexisting_findings']), sp.MAX_PREEXISTING_FINDINGS)
        self.assertEqual(preflight['preexisting_truncated'], 2)
        self.assertEqual(len(receipt['preexisting_sensitive_paths']), 501)
        self.assertIn('dist/019.js.map and 481 more (see the SECURITY receipt)', output.getvalue())
        self.assertNotIn('dist/020.js.map', output.getvalue())

    def test_removed_base_paths_are_notes_but_new_or_changed_staged_hits_block(self):
        token = ('AKIA' + 'ABCDEFGHIJKLMNOP' + '\n').encode()
        self.commit_files({'deploy.pem': b'ordinary file\n', 'old.txt': token})
        repo = ds.repository(str(self.workspace))
        baseline = ds.build_baseline(repo, ['.'], ds.capture_state(repo))
        # Unstaged delete: old index paths are informational.
        (self.workspace / 'deploy.pem').unlink()
        (self.workspace / 'old.txt').unlink()
        co, _ = wt.WorktreeLifecycleActivationTests.at_security(self)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(co, '_security_review_turn',
                                    return_value={'status': 'APPROVE', 'reason': None}):
            co.worktree_security_turn()
        self.assertEqual(co.state['status'], 'DONE')
        self.assertIn('pre-existing sensitive-looking paths, removed by this run: deploy.pem', output.getvalue())
        self.assertIn('pre-existing security content hits, removed by this run: old.txt', output.getvalue())
        report = self.scan_current(baseline)
        self.assertEqual(report['status'], 'clean')
        self.assertTrue(all(r['removed'] for r in report['preexisting_findings']))
        # Staged deletion also passes, with nothing left in the index to scan.
        rc.subprocess.run(['git', 'add', '-u'], cwd=self.workspace, check=True)
        self.assertEqual(self.scan_current(baseline)['status'], 'clean')
        # An added file deleted from disk still publishes its staged hits.
        (self.workspace / 'added.pem').write_bytes(token)
        rc.subprocess.run(['git', 'add', '-f', 'added.pem'], cwd=self.workspace, check=True)
        (self.workspace / 'added.pem').unlink()
        report = self.scan_current(baseline)
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual({r['path'] for r in report['findings']}, {'added.pem'})
        # A changed base blob staged before deletion is also still blocking.
        (self.workspace / 'deploy.pem').write_bytes(token)
        rc.subprocess.run(['git', 'add', '-f', 'deploy.pem'], cwd=self.workspace, check=True)
        (self.workspace / 'deploy.pem').unlink()
        report = self.scan_current(baseline)
        self.assertTrue(any(r['path'] == 'deploy.pem' for r in report['findings']))

    def test_renamed_away_base_path_is_note_and_destination_hits_still_block(self):
        self.commit_files({'deploy.pem': b'ordinary file\n'})
        (self.workspace / 'deploy.pem').rename(self.workspace / 'ordinary.txt')
        co, _ = wt.WorktreeLifecycleActivationTests.at_security(self)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(co, '_security_review_turn',
                                    return_value={'status': 'APPROVE', 'reason': None}):
            co.worktree_security_turn()
        self.assertEqual(co.state['status'], 'DONE')
        self.assertIn('removed by this run: deploy.pem', output.getvalue())
        self.run_dir = self.root / 'rename-to-sensitive'
        (self.workspace / 'ordinary.txt').rename(self.workspace / 'api_key.rb')
        co, _ = wt.WorktreeLifecycleActivationTests.at_security(self)
        with contextlib.redirect_stdout(io.StringIO()):
            co.worktree_security_turn()
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('SECURITY sensitive paths: api_key.rb', co.state['hold_reason'])
        self.assertNotIn('deploy.pem', co.state['hold_reason'])
        # A renamed-away content hit is a NOTE only at the old name, not its destination.
        self.run_dir = self.root / 'rename-secret'
        (self.workspace / 'api_key.rb').unlink()
        self.commit_files({'old.txt': ('AKIA' + 'ABCDEFGHIJKLMNOP' + '\n').encode()})
        (self.workspace / 'old.txt').rename(self.workspace / 'new.txt')
        co, _ = wt.WorktreeLifecycleActivationTests.at_security(self)
        with contextlib.redirect_stdout(io.StringIO()):
            co.worktree_security_turn()
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('aws-access-key-id in new.txt', co.state['hold_reason'])
        self.assertNotIn('aws-access-key-id in old.txt', co.state['hold_reason'])

    def test_review_only_base_keeps_committed_additions_blocking(self):
        self.commit_files({'api_key.rb': b'ordinary file\n'})
        co = rc.Coordinator(self.args('--review-only', '--base', 'HEAD~1'))
        tree = rc.git_snapshot(self.workspace)[0]
        co.state['lifecycle'].update(stage='SECURITY', candidate_oid=tree)
        with contextlib.redirect_stdout(io.StringIO()):
            co.worktree_security_turn()
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertIn('SECURITY sensitive paths: api_key.rb', co.state['hold_reason'])
        self.assertEqual({r['path'] for r in co.state['lifecycle']['receipts'][-1]['preflight']['findings']},
                         {'api_key.rb'})

    def test_security_prompt_names_notes_without_values_and_delivery_report_counts_them(self):
        token = 'AKIA' + 'ABCDEFGHIJKLMNOP'
        self.commit_files({'deploy.pem': b'ordinary file\n', 'settings.txt': (token + '\n').encode(),
                           'large.txt': b'x' * (sp.MAX_FILE_BYTES + 1)})
        co, tree = wt.WorktreeLifecycleActivationTests.at_security(self)
        prompts = []
        stub = wt.WorktreeLifecycleActivationTests.security_turn_stub(self, co, tree,
                                      {'status': 'APPROVE', 'full_review': [], 'prior_findings': []})
        def invoke(role, phase, prompt, schema, fresh=False):
            self.assertEqual((role, phase, fresh), ('reviewer', 'SECURITY', True))
            co.assert_fresh_prompt(role, prompt)  # invoke applies this scan to reviewer (it is intentionally a no-op).
            self.assertEqual(sp.scan_file(self.write_prompt(prompt)), [])
            prompts.append(prompt)
            return stub(role, phase, prompt, schema, fresh=fresh)
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(co, 'render'), \
                mock.patch.object(co, 'invoke', side_effect=invoke):
            co.worktree_security_turn()
        [prompt] = prompts
        self.assertIn('deploy.pem', prompt)
        self.assertIn('settings.txt', prompt)
        self.assertIn('aws-access-key-id', prompt)
        self.assertIn('already committed and left unchanged', prompt)
        self.assertIn('SECURITY notes, not findings of this run', prompt)
        self.assertIn("review the run's change as before", prompt)
        self.assertNotIn(token, prompt)
        report = wt.wl.delivery_report(co.state, 'test-run', 'test work',
                                      {'auto_commit': False, 'commit': None})
        self.assertIn('已提交且未改动：敏感路径 1 个，内容命中 2 条，因大小未扫描 1 个文件', report)

    def write_prompt(self, prompt):
        path = self.root / 'security-prompt.txt'
        path.write_text(prompt)
        return str(path)

    def test_added_file_hits_remain_blocking(self):
        repo = ds.repository(str(self.workspace))
        baseline = ds.build_baseline(repo, ['.'], ds.capture_state(repo))
        (self.workspace / 'new.txt').write_text('AKIA' + 'ABCDEFGHIJKLMNOP' + '\n')
        report = self.scan_current(baseline)
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual({r['path'] for r in report['findings']}, {'new.txt'})
        self.assertEqual(report['preexisting_findings'], [])

    def test_added_hits_still_enforce_the_finding_limit(self):
        repo = ds.repository(str(self.workspace))
        baseline = ds.build_baseline(repo, ['.'], ds.capture_state(repo))
        (self.workspace / 'api_key.rb').write_text(('AKIA' + 'ABCDEFGHIJKLMNOP' + '\n') * 3)
        with mock.patch.object(sp, 'MAX_FINDINGS', 2):
            report = self.scan_current(baseline)
        self.assertEqual(report['status'], 'incomplete')
        self.assertIn('finding-count limit exceeded', report['reason'])


if __name__ == '__main__':
    unittest.main()
