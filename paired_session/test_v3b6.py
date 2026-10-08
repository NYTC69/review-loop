"""V3-B6 (ADR-17 V2): a run saved by review-loop 2.13.x or earlier (state version 1) is refused by every run command, and
as a --supersedes parent, with one line and before any other key of its state is read; `status` still prints it."""
import json
import unittest

from paired_session import test_worktree_lifecycle as twl

rc = twl.rc
OLD = {'version': 1, 'status': 'DONE'}   # no config or any other key: a key read before the gate would raise


class OldStateGateTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})

    def cli(self, action, *extra):
        command = self.command(*extra)
        command[2] = action
        return rc.subprocess.run([*command, '--skip-probe'], cwd=self.root, text=True, capture_output=True)

    def test_every_run_command_refuses_an_old_run_and_status_reads_it(self):
        self.run_dir.mkdir(parents=True)
        path = self.run_dir / 'state.json'
        path.write_text(json.dumps(OLD))
        before = path.read_bytes()
        for action in ('resume', 'accept', 'reject', 'note', 'abort', 'attach-verification', 'permission-probe'):
            with self.subTest(action=action):
                result = self.cli(action)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), 'REFUSED: ' + rc.OLD_STATE_REFUSAL, result.stderr)
                self.assertEqual(path.read_bytes(), before)
        profile = self.root / 'old-profile.json'   # a 2.13.x profile may still set skip_globs: the old-run line comes first
        profile.write_text(json.dumps({'skip_globs': ['generated/**']}))
        result = self.cli('resume', '--config', str(profile))
        self.assertEqual(result.stdout.strip(), 'REFUSED: ' + rc.OLD_STATE_REFUSAL, result.stderr)
        status = self.cli('status')
        self.assertEqual((status.returncode, json.loads(status.stdout)), (0, OLD))
        with self.assertRaisesRegex(ValueError, 'finish or abort this one with review-loop 2.13.x'):
            rc.Coordinator(rc.parser().parse_args(['resume', *self.command()[3:]]))

    def test_an_old_supersedes_parent_is_refused_before_the_child_exists(self):
        parent = self.root / 'old-parent'
        parent.mkdir()
        (parent / 'state.json').write_text(json.dumps(OLD))
        result = self.cli('run', '--supersedes', str(parent))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), 'REFUSED: ' + rc.OLD_STATE_REFUSAL, result.stderr)
        self.assertFalse((self.run_dir / 'state.json').exists())
        with self.assertRaisesRegex(ValueError, 'finish or abort this one with review-loop 2.13.x'):
            rc.Coordinator(rc.parser().parse_args(['run', *self.command('--supersedes', str(parent))[3:]]))
        self.assertFalse((self.run_dir / 'state.json').exists())


if __name__ == '__main__':
    unittest.main()
