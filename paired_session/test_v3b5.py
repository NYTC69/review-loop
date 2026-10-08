"""V3-B5 (ADR-17 V1): `--lifecycle-mode` defaults to 'on' for a new run started from the CLI; a saved run keeps the mode it
was created with (a resume without the flag never meets a refusal for the default); a Coordinator built directly from
parsed arguments without the flag keeps the older 'off' unless the run is saved."""
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

    def test_a_saved_off_run_resumes_off_without_the_flag(self):
        held = self.run_coordinator('--stop-after-plan')   # the harness creates it with --lifecycle-mode off
        self.assertIn('stopped by --stop-after-plan', held.stdout, held.stdout + held.stderr)
        resumed = self.cli('resume')
        self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        self.assertNotIn('cannot resume', resumed.stdout)
        state = self.state()
        self.assertEqual(state['config']['lifecycle_mode'], 'off')
        self.assertNotIn('lifecycle', state)

    def test_the_python_api_keeps_off_for_a_new_run_and_the_saved_mode_otherwise(self):
        argv = self.without_harness_default(self.command())[2:]
        co = rc.Coordinator(rc.parser().parse_args(argv))
        self.assertEqual(co.state['config']['lifecycle_mode'], 'off')
        self.assertIsNone(rc.parser().parse_args(argv).lifecycle_mode)   # argparse leaves the choice to the run
        self.run_dir = self.root / 'w-run'
        rc.Coordinator(rc.parser().parse_args(self.command('--lifecycle-mode', 'on')[2:]))
        resume = rc.parser().parse_args(self.without_harness_default(self.command())[2:])
        resume.action = 'resume'
        self.assertEqual(rc.Coordinator(resume).args.lifecycle_mode, 'on')


if __name__ == '__main__':
    unittest.main()
