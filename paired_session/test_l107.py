"""L107 (owner 2026-10-07, ADR-15: retire `adversarial_gate_skip_paths`): `skip_globs` / `--skip-globs` stay accepted and
frozen for old profiles and saved runs, but nothing reads them: the adversarial gate always runs."""
import json
import unittest

from paired_session import test_real_coordinator as trc

rc = trc.rc


class SkipGlobsRetiredTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in (
        'setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli', 'fake_claude_cli',
        'command', 'run_coordinator', 'coordinator')})

    def test_a_skip_glob_matching_every_path_still_runs_the_gate(self):
        done = self.run_coordinator('--skip-globs', '**')
        self.assertIn('DONE', done.stdout, done.stdout + done.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['config']['skip_globs'], ['**'])   # frozen as before
        self.assertTrue(any(turn['role'] == 'gate' for turn in state['turns']))
        self.assertTrue(state['exec_comparisons'][-1]['gate'])

    def test_a_profile_with_skip_globs_still_loads(self):
        profile = self.workspace / '.review-loop' / 'paired-session.json'
        profile.parent.mkdir()
        profile.write_text(json.dumps({'skip_globs': ['generated/**'], 'max_exec_rounds': 2}))
        argv = ['run', '--workspace', str(self.workspace), '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
                '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli())]
        config = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv)).state['config']
        self.assertEqual((config['skip_globs'], config['max_exec_rounds']), (['generated/**'], 2))

    def test_the_help_says_it_is_retired(self):
        self.assertIn('retired (L107)', rc.parser().format_help().replace('\n', ' '))


if __name__ == '__main__':
    unittest.main()
