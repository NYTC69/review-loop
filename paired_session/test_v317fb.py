"""V317-FB operator recoveries, driven through fake provider CLIs."""
import json
import unittest

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
                ('core.autocrlf', 'input', 'unset it and accept again')):
            with self.subTest(key=key):
                self.run_dir = self.root / key
                self.git('config', key, value)
                self.done(*ON, '--auto-commit', 'true')
                for extra in ((), ('--auto-commit', 'false')):
                    refused = self.run_operator_action('accept', *extra)
                    self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                    self.assertIn(hint, refused.stdout)
                    self.assertTrue(self.state()['config']['auto_commit'])
                self.git('config', key, 'true') if key == 'core.fileMode' else self.git('config', '--unset', key)
                accepted = self.run_operator_action('accept')
                self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
                self.assertIsNotNone(self.state()['acceptance']['delivery']['commit'])

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

    def test_submodule_run_is_refused_before_accept_even_without_auto_commit(self):
        self.git('update-index', '--add', '--cacheinfo', '160000,' + self.git('rev-parse', 'HEAD') + ',vendor/lib')
        self.git('commit', '-qm', 'fake submodule')
        for value in ('true', 'false'):
            self.run_dir = self.root / ('submodule-' + value)
            refused = self.run_coordinator(*ON, '--auto-commit', value)
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertIn('submodules/gitlinks are unsupported by this manifest version', refused.stdout)

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
                hint = ('note and resume, reject --scope-change, or abort' if mode == ON else 'reject --scope-change or abort')
                self.assertIn(hint, held.stdout)
                refused = self.run_operator_action('accept')
                self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                changed = self.run_operator_action('reject', *mode, '--scope-change', '--text', 'New scope: include floats.')
                self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
                self.assertEqual(self.state()['status'], 'ABORTED')


if __name__ == '__main__':
    unittest.main()
