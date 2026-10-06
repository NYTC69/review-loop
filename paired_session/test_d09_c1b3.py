"""D09 C1-b3 (paired_session/docs/d09-cap1-writer-passes.md §2 "Local test executor", §3 skip rules, §5 tests 4 and 8):
the local test executor (the explicit test command under /bin/sh -c in the workspace, the run's --timeout, the log in
evidence, the exit code only, a changed live tree fails), the green baseline before the writers, and the local check
that keeps a writer change (the replay follows) or rolls it back (`rolled-back:tests`). Every workspace here carries one
passing unittest module, so `python3 -m unittest` is green on every Python version (3.12+ fails a run with no tests)."""
import json
from pathlib import Path
import unittest

from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE
CODE = ''.join(f'VALUE_{n} = {n}\n' for n in range(30))
RUN = ('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '80')
GREEN = 'import unittest\n\n\nclass Smoke(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n'
NOTE = {'FAKE_WRITER_WRITE': 'simplifier', 'FAKE_WRITER_FILE': 'writer_note.py'}


class D09C1b3Tests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'git')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def run_writers(self, *extra, smoke=GREEN, env=None):
        (self.workspace / 'new_code.py').write_text(CODE)
        (self.workspace / 'tests').mkdir()
        (self.workspace / 'tests' / 'test_new_code.py').write_text('import new_code\n')
        (self.workspace / 'test_smoke.py').write_text(smoke)
        base = rc.git_snapshot(self.workspace)[0]
        done = self.run_coordinator(*RUN, *extra, env=env or {})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        return state, state['lifecycle']['quality_writers'], base

    def script(self, body):   # a test command with shell syntax goes in a script, as the CLI warning asks
        path = self.root / 'test-script.sh'
        path.write_text(body + '\n')
        return '/bin/bash ' + str(path)

    def writer_turns(self, state):
        return [t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'POLISH-Q']

    # --- §5 test 1 with the real check: a green local run keeps the write, then the replay ----------------------------------
    def test_a_green_local_run_keeps_the_write_and_the_replay_follows(self):
        state, marker, base = self.run_writers(env=NOTE)
        self.assertEqual((marker['baseline']['passed'], marker['baseline']['exit']), (True, 0))
        self.assertIn('OK', Path(marker['baseline']['log']).read_text())
        simplifier = marker['simplifier']
        self.assertEqual((simplifier['state'], simplifier['test']['passed'], simplifier['test']['command']),
                         ('wrote', True, 'python3 -m unittest'))
        self.assertTrue(Path(simplifier['test']['log']).is_file())
        self.assertEqual((state['lifecycle']['epoch'], state['exec_rounds']), (1, 2))   # the replay ran
        self.assertTrue((self.workspace / 'writer_note.py').is_file())
        self.assertEqual(state['invocations_used'], len([t for t in state['turns'] if t.get('invocation_budget_counted') is not False]))

    # --- §5 test 8: the executor and the baseline -------------------------------------------------------------------------
    def test_a_red_baseline_skips_both_writers_without_a_writer_call(self):
        state, marker, base = self.run_writers(smoke=GREEN.replace('assertTrue(True)', 'assertTrue(False)'), env=NOTE)
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('skipped:no-green-baseline',) * 2)
        self.assertEqual((marker['baseline']['passed'], marker['baseline']['exit']), (False, 1))
        self.assertIn('FAILED', Path(marker['baseline']['log']).read_text())
        self.assertEqual(self.writer_turns(state), [])
        self.assertEqual((rc.git_snapshot(self.workspace)[0], state['lifecycle']['epoch']), (base, 0))

    def test_a_baseline_that_writes_to_the_tree_is_restored_and_skips(self):
        state, marker, base = self.run_writers('--test-command', self.script('echo stray > stray.txt'), env=NOTE)
        self.assertEqual((marker['baseline']['tree_changed'], marker['simplifier']['state']), (True, 'skipped:no-green-baseline'))
        self.assertFalse((self.workspace / 'stray.txt').exists())
        self.assertEqual(rc.git_snapshot(self.workspace)[0], base)

    def test_a_baseline_that_rewrites_a_tracked_file_fails_is_restored_and_skips(self):   # §5 test 8
        state, marker, base = self.run_writers('--test-command', self.script('echo touched >> tracked.txt'), env=NOTE)
        baseline = marker['baseline']
        self.assertEqual((baseline['exit'], baseline['tree_changed'], baseline['passed'], baseline['reason']),
                         (0, True, False, 'changed the tree'))   # exit 0, still red
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('skipped:no-green-baseline',) * 2)
        self.assertEqual(((self.workspace / 'tracked.txt').read_text(), rc.git_snapshot(self.workspace)[0]), ('base\n', base))
        self.assertEqual(self.writer_turns(state), [])

    def test_a_failing_local_run_rolls_the_write_back(self):
        state, marker, base = self.run_writers('--test-command', 'test ! -e writer_note.py', env=NOTE)
        self.assertEqual((marker['baseline']['passed'], marker['simplifier']['state']), (True, 'rolled-back:tests'))
        self.assertEqual((marker['simplifier']['test']['exit'], marker['simplifier']['test']['reason']), (1, 'exit 1'))
        self.assertEqual((rc.git_snapshot(self.workspace)[0], state['lifecycle']['epoch']), (base, 0))   # no replay

    def test_a_local_run_that_rewrites_a_tracked_file_fails(self):
        command = self.script('if [ -e writer_note.py ]; then echo touched >> tracked.txt; fi')
        state, marker, base = self.run_writers('--test-command', command, env=NOTE)
        test = marker['simplifier']['test']
        self.assertEqual((marker['simplifier']['state'], test['exit'], test['tree_changed'], test['reason']),
                         ('rolled-back:tests', 0, True, 'changed the tree'))
        self.assertEqual((self.workspace / 'tracked.txt').read_text(), 'base\n')
        self.assertEqual(rc.git_snapshot(self.workspace)[0], base)

    def test_a_local_run_that_times_out_fails(self):
        state, marker, base = self.run_writers('--test-command', self.script('if [ -e writer_note.py ]; then sleep 60; fi'),
                                               '--timeout', '4', env=NOTE)
        test = marker['simplifier']['test']
        self.assertEqual((marker['simplifier']['state'], test['exit'], test['reason']),
                         ('rolled-back:tests', None, 'timed out after 4s'))
        self.assertEqual(rc.git_snapshot(self.workspace)[0], base)

    def test_the_write_boundary_names_reserved_docs_and_review_loop_config(self):   # §2, before the local run
        (self.workspace / 'new_code.py').write_text(CODE)
        co = rc.Coordinator(rc.parser().parse_args(self.command(*RUN)[2:]))
        with (self.workspace / '.gitignore').open('a') as handle:   # a repository that does not ignore .review-loop/
            handle.write('!.review-loop/\n')
        leg = {'capture': co._writer_capture(self.root / 'capture')}
        (self.workspace / '.review-loop').mkdir()
        (self.workspace / '.review-loop' / 'config.md').write_text('entry: legacy\n')
        (self.workspace / 'CHANGELOG.md').write_text('# entry\n')   # the review-only W default docs file, reserved for DOCS
        (self.workspace / 'new_code.py').write_text(CODE + 'X = 1\n')   # an ordinary code edit is inside the grant
        self.assertEqual(co._writer_boundary(leg), ['.review-loop/config.md', 'CHANGELOG.md'])
        self.assertEqual(leg['boundary'], ['.review-loop/config.md', 'CHANGELOG.md'])

    def test_a_leftover_child_of_the_test_command_cannot_change_the_tree_later(self):   # C1-b3 gate LOW 1 and 2
        (self.workspace / 'new_code.py').write_text(CODE)
        co = rc.Coordinator(rc.parser().parse_args(self.command(*RUN)[2:]))
        co.args.test_command = self.script('(sleep 2; echo late > late.txt) &\nexit 0')   # a background child outlives it
        run = co._test_run('leftover')
        self.assertEqual((run['passed'], run['exit']), (True, 0))
        rc.time.sleep(3)
        self.assertFalse((self.workspace / 'late.txt').exists())   # the process group was killed before the snapshot

    def test_the_npm_test_default_alone_skips_both_writers(self):
        argv = [arg for arg in self.command(*RUN) if arg not in ('--test-command', 'python3 -m unittest')]
        (self.workspace / 'new_code.py').write_text(CODE)
        (self.workspace / 'tests').mkdir()
        (self.workspace / 'tests' / 'test_new_code.py').write_text('import new_code\n')
        done = rc.subprocess.run([*argv, '--skip-probe'], cwd=self.root, text=True, capture_output=True,
                                 env={**rc.os.environ, **NOTE})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        marker = self.state()['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('skipped:no-test-command',) * 2)
        self.assertNotIn('baseline', marker)


if __name__ == '__main__':
    unittest.main()
