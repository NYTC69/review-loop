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
                        'content scan skipped oversized pre-existing files, not changed by this run:',
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
        # Skipping a per-file scan still consumes the existing total budget.
        with mock.patch.object(sp, 'MAX_TOTAL_BYTES', sp.MAX_FILE_BYTES):
            report = sp.scan(str(self.workspace), manifest=manifest, run_changes_only=True)
        self.assertEqual(report['status'], 'incomplete')
        self.assertIn('total content scan limit exceeded: large.txt', report['reason'])

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
            co.state['status'] = 'ACTIVE'
            co.worktree_security_turn()
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


if __name__ == '__main__':
    unittest.main()
