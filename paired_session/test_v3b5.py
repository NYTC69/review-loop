"""CLI lifecycle defaults; OFF-2 Python defaults are covered in test_off_user_surface."""
import json
import unittest

from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE


class LifecycleDefaultTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})

    def without_harness_default(self, command):   # the harness passes 'off' for tests written before the flip
        index = command.index('--lifecycle-mode')
        self.assertEqual(command[index + 1], 'off')
        return command[:index] + command[index + 2:]

    def cli(self, action, *extra):
        command = self.without_harness_default(self.command(*extra))
        command[2] = action
        return rc.subprocess.run([*command, '--skip-probe'], cwd=self.root, text=True, capture_output=True)

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def test_a_new_cli_run_without_the_flag_is_a_worktree_lifecycle_run(self):
        done = self.cli('run')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertEqual(state['config']['lifecycle_mode'], 'on')
        self.assertEqual(state['lifecycle']['stage'], 'DONE')


if __name__ == '__main__':
    unittest.main()
