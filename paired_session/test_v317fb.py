"""V317-FB operator recoveries, driven through fake provider CLIs."""
import json
import os
import unittest
from unittest.mock import patch

from paired_session import test_worktree_lifecycle as twl

rc = twl.rc
ON = ('--lifecycle-mode', 'on', '--max-invocations', '80')
OFF = ('--lifecycle-mode', 'off', '--shadow', 'off')
RESTORE = ('work-item reviewer allowlist changed; restore the reviewer-commands block of the work item '
           '(then run permission-probe again if it was already run) and resume, or abort and start a new run')


class OperatorRecoveryTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in twl._HELPERS})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def git(self, *args):
        return rc.subprocess.check_output(['git', *args], cwd=self.workspace, text=True).strip()

    def done(self, *flags):
        result = self.run_coordinator(*flags)
        self.assertIn(twl.DONE, result.stdout, result.stdout + result.stderr)

    def test_filemode_and_autocrlf_refusal_and_retry(self):
        for key, value, hint in (
                ('core.fileMode', 'false', 'set it to true and accept again, or abort and commit the tree yourself (auto_commit is frozen at run start)'),
                ('core.autocrlf', 'input', 'unset it (or set it to false in this repository) and accept again')):
            with self.subTest(key=key), patch.dict('os.environ', {
                    'GIT_CONFIG_GLOBAL': str(self.test_home / '.gitconfig')}):
                self.run_dir = self.root / key
                # Use the global autocrlf case: a repository override must also recover it.
                if key == 'core.autocrlf':
                    (self.test_home / '.gitconfig').write_text('[core]\n\tautocrlf = input\n')
                else:
                    self.git('config', key, value)
                self.assertEqual(self.git('config', '--get', key), value)
                self.done(*ON, '--auto-commit', 'true')
                for extra in ((), ('--auto-commit', 'false')):
                    refused = self.run_operator_action('accept', *extra)
                    self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                    self.assertIn(hint, refused.stdout)
                    self.assertTrue(self.state()['config']['auto_commit'])
                self.git('config', key, 'true' if key == 'core.fileMode' else 'false')
                accepted = self.run_operator_action('accept')
                self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
                self.assertIsNotNone(self.state()['acceptance']['delivery']['commit'])
                if key == 'core.autocrlf':
                    (self.test_home / '.gitconfig').unlink()
                    self.git('config', '--unset', key)

    def test_sparse_refusal_and_new_run_without_commit(self):
        self.git('update-index', '--skip-worktree', 'tracked.txt')
        self.done(*ON, '--auto-commit', 'true')
        refused = self.run_operator_action('accept', '--auto-commit', 'false')
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('abort and commit the tree yourself, or start a new run with auto_commit false (the value is frozen at run start)', refused.stdout)
        aborted = self.run_operator_action('abort', *ON)
        self.assertIn('aborted by operator', aborted.stdout)
        self.run_dir = self.root / 'sparse-new'
        self.done(*ON, '--auto-commit', 'false')
        accepted = self.run_operator_action('accept')
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
        self.assertIsNone(self.state()['acceptance']['delivery']['commit'])

    def test_real_submodule_start_refusal_and_stop_after_gate_recovery(self):
        source = self.root / 'submodule-source'
        source.mkdir()
        for args in (('init', '-q'), ('config', 'user.email', 'fake@example.test'),
                     ('config', 'user.name', 'Fake')):
            rc.subprocess.run(['git', *args], cwd=source, check=True)
        (source / 'lib.txt').write_text('local submodule fixture\n')
        rc.subprocess.run(['git', 'add', 'lib.txt'], cwd=source, check=True)
        rc.subprocess.run(['git', 'commit', '-qm', 'submodule base'], cwd=source, check=True)
        self.git('-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', '--', str(source), 'vendor/lib')
        self.git('commit', '-qm', 'real submodule')
        for value in ('true', 'false'):
            self.run_dir = self.root / ('submodule-' + value)
            refused = self.run_coordinator(*ON, '--auto-commit', value)
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertIn('submodules/gitlinks are unsupported by this manifest version', refused.stdout)
            self.assertIn('use --lifecycle-mode off (stop-after-gate) for this repository', refused.stdout)
            self.assertIn('a review-pr report run needs lifecycle on and cannot review it', refused.stdout)
        self.run_dir = self.root / 'submodule-off'
        self.done(*OFF)
        self.assertEqual(self.state()['config']['lifecycle_mode'], 'off')
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        self.run_dir = self.root / 'submodule-review-only-off'
        self.done(*OFF, '--review-only')
        self.run_dir = self.root / 'submodule-report-on'
        report = ('--review-only', '--review-report', '--auto-commit', 'false')
        refused = self.run_coordinator(*ON, *report)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('worktree lifecycle cannot capture its delivery baseline', refused.stdout)
        self.assertIn('a review-pr report run needs lifecycle on and cannot review it', refused.stdout)
        self.run_dir = self.root / 'submodule-report-off'
        held = self.run_coordinator(*OFF, *report)
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        self.assertIn('report POLISH-Q requires --lifecycle-mode on; start a new run', held.stdout)
        self.assertTrue(self.state()['config']['review_report'])
        self.assertEqual(self.state()['status'], 'HOLD')

    def test_review_only_default_accept_reports_only_the_skip_reason(self):
        for key, value, reason in (
                ('core.autocrlf', 'input', 'auto_commit refuses core.autocrlf'),
                ('core.fileMode', 'false', 'auto_commit refuses core.fileMode false')):
            with self.subTest(key=key):
                self.run_dir = self.root / ('review-default-' + key)
                self.git('config', key, value)
                (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
                self.done(*ON, '--review-only')
                self.assertEqual(self.state()['config']['auto_commit_source'], 'review-only-default')
                accepted = self.run_operator_action('accept')
                self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
                self.assertEqual(self.state()['status'], 'ACCEPTED')
                self.assertEqual(self.state()['acceptance']['delivery']['commit_skipped'], reason)
                [line] = [line for line in accepted.stdout.splitlines() if line.startswith('UNCOMMITTED:')]
                self.assertIn('UNCOMMITTED: no commit was made (auto_commit skipped: ' + reason + '); commit these yourself:', line)
                report = (self.run_dir / 'delivery-report.md').read_text()
                self.assertIn('auto_commit 已跳过（' + reason + '）', report)
                for text in (line, report):
                    for hint in ('accept again', 'unset it', 'set it to true', 'start a new run', 'abort and commit', 'auto_commit is frozen'):
                        self.assertNotIn(hint, text)
                self.git('config', key, 'true') if key == 'core.fileMode' else self.git('config', '--unset', key)

    def test_allowlist_restore_before_and_after_probe(self):
        original = self.workitem.read_text()
        started = self.run_coordinator(*ON, '--stop-after-plan')
        self.assertIn('HOLD', started.stdout, started.stdout + started.stderr)
        self.workitem.write_text(original + '\n```reviewer-commands\npython3 -m unittest\n```\n')
        held = self.run_operator_action('resume', *ON)
        self.assertIn(RESTORE, held.stdout)
        # The invocation-time check carries the same recovery text, without widening the frozen list.
        command = self.command(*ON); command[2] = 'accept'
        co = rc.Coordinator(rc.parser().parse_args(command[2:]))
        with self.assertRaisesRegex(RuntimeError, 'restore the reviewer-commands block'):
            co.reviewer_commands()
        self.workitem.write_text(original)
        resumed = self.run_operator_action('resume', *ON)
        self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        self.run_dir = self.root / 'probe-before-restore'
        started = self.run_coordinator(*ON, '--stop-after-plan')
        self.assertIn('HOLD', started.stdout, started.stdout + started.stderr)
        self.workitem.write_text(original + '\n```reviewer-commands\npython3 -m unittest\n```\n')
        probe = self.run_operator_action('permission-probe', *ON)
        self.assertNotEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        resumed = self.run_operator_action('resume', *ON)
        self.assertIn('frozen role dispatch changed; abort or start a new run', resumed.stdout)
        self.workitem.write_text(original)
        probe = self.run_operator_action('permission-probe', *ON)
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        resumed = self.run_operator_action('resume', *ON)
        self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)

    def test_all_codex_roles_still_require_claude_binding(self):
        for mode in (ON, OFF):
            with self.subTest(mode=mode):
                self.run_dir = self.root / ('binding-' + mode[1])
                command = [arg for arg in self.command(*mode, '--reviewer-vendor', 'codex',
                           '--claude-bin', str(self.root / 'missing-claude')) if arg != '--strict']
                held = rc.subprocess.run(command, cwd=self.root, text=True,
                                         stdout=rc.subprocess.PIPE, stderr=rc.subprocess.PIPE)
                self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
                self.assertIn('configured claude_bin is missing or inside an author-writable root', held.stdout)
                self.assertEqual(self.state()['turns'], [])

    def test_credential_env_names_hint_and_restore(self):
        added, removed = 'V3110_ADDED_TOKEN', 'V3110_REMOVED_TOKEN'
        value = 'v3110-private-value-never-print'
        with patch.dict(os.environ, {removed: value}):
            os.environ.pop(added, None)
            started = self.run_coordinator(*ON, '--stop-after-plan')
            self.assertIn('HOLD', started.stdout, started.stdout + started.stderr)
            frozen = self.state()['role_dispatch_manifest']
            del os.environ[removed]
            os.environ[added] = value
            held = self.run_operator_action('resume', *ON)
            self.assertEqual(self.state()['status'], 'HOLD')
            reason = self.state()['hold_reason']
            self.assertIn('names added: ' + added + '; removed: ' + removed, reason)
            self.assertIn('restore the environment (unset ' + added + '; export ' + removed + ')', reason)
            self.assertIn('run resume again to continue the run', reason)
            self.assertEqual(len(reason.splitlines()), 1)
            self.assertIn(reason, held.stdout)
            status = self.run_operator_action('status')
            self.assertEqual(json.loads(status.stdout)['hold_reason'], reason)
            brief = self.run_operator_action('status', '--brief')
            self.assertIn(rc.brief_reason(reason), brief.stdout)
            self.assertIn('run resume again to continue the run', brief.stdout)
            self.assertIn('Hold reason: ' + reason, (self.run_dir / 'review-comparison.md').read_text())
            self.assertEqual(self.state()['role_dispatch_manifest'], frozen)
            self.assertNotIn(value, (self.run_dir / 'state.json').read_text())
            del os.environ[added]
            os.environ[removed] = value
            resumed = self.run_operator_action('resume', *ON)
            self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
            for output in (started.stdout, held.stdout, status.stdout, brief.stdout, resumed.stdout):
                self.assertNotIn(value, output)
            for path in self.run_dir.rglob('*'):
                if path.is_file() and path.suffix in ('.json', '.jsonl', '.md'):
                    self.assertNotIn(value, path.read_text(), str(path))

    def test_off_allowlist_hint_stays(self):
        self.done(*OFF)
        self.workitem.write_text(self.workitem.read_text() + '\n```reviewer-commands\npython3 -m unittest\n```\n')
        held = self.run_operator_action('resume', *OFF)
        self.assertIn('work-item reviewer allowlist changed; run permission-probe before continuing', held.stdout)

    def test_rejection_limit_accept_refused_and_scope_change_works_on_both_routes(self):
        for mode in (ON, OFF):
            with self.subTest(mode=mode):
                self.run_dir = self.root / ('reject-' + mode[1])
                self.done(*mode)
                # A zero rejection budget isolates the real rejection-limit route after fake-CLI DONE.
                path = self.run_dir / 'state.json'
                state = self.state()
                state['max_rejections'] = 0
                path.write_text(json.dumps(state))
                held = self.run_operator_action('reject', '--text', 'Please handle an edge case.')
                self.assertIn('post-DONE rejection limit reached', held.stdout)
                hint = 'note and resume, reject --scope-change, or abort'
                self.assertIn(hint, held.stdout)
                refused = self.run_operator_action('accept')
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                changed = self.run_operator_action('reject', *mode, '--scope-change', '--text', 'New scope: include floats.')
                self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
                self.assertEqual(self.state()['status'], 'ABORTED')


if __name__ == '__main__':
    unittest.main()
