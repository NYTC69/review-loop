"""FIELD-28 (poker-news-bot v2.12.3, AAB): the run root sat inside the project's git repository, so the role TMPDIRs under
the run directory were inside it too; the project's own scratch check refused them and fell back to /private/tmp, which the
Codex read-only sandbox denies, and the gate probe's test command failed. run, resume and permission-probe now warn (stderr,
a warning only) when the run directory is inside a git work tree."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
WARNING = 'WARNING: the run directory '
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli', 'fake_claude_cli',
           'command', 'run_coordinator')


class GitWorkTreeRootTests(unittest.TestCase):
    def test_the_nearest_git_directory_or_file_is_found(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        (root / 'repo' / '.git').mkdir(parents=True)
        (root / 'worktree').mkdir()
        (root / 'worktree' / '.git').write_text('gitdir: /elsewhere\n')   # a linked worktree has a .git file
        (root / 'repo' / 'a' / 'runs').mkdir(parents=True)
        (root / 'outside').mkdir()
        self.assertEqual(rc.git_work_tree_root(root / 'repo' / 'a' / 'runs'), root / 'repo')
        self.assertEqual(rc.git_work_tree_root(root / 'worktree'), root / 'worktree')
        self.assertIsNone(rc.git_work_tree_root(root / 'outside'))


class RunStartWarningTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def test_a_run_directory_inside_a_repository_warns_once_on_stderr(self):
        repo = rc.git_work_tree_root(self.run_dir.parent)   # the harness root sits inside this checkout
        self.assertIsNotNone(repo)
        # Freeze the v3.3.0 layout before dispatch: no scratch_root in state.
        with patch.object(rc, 'new_scratch_root', side_effect=lambda run: run):
            co = rc.Coordinator(rc.parser().parse_args(self.command()[2:]))
        co.state.pop('scratch_root')
        co.save()
        inside = self.run_coordinator()
        self.assertEqual(inside.stderr.count(WARNING), 1, inside.stderr)
        self.assertIn(f'{WARNING}{self.run_dir} is inside the git repository {repo}', inside.stderr)
        self.assertIn('Put the run root outside any repository.', inside.stderr)
        self.assertNotIn(WARNING, inside.stdout)

    def test_a_new_run_under_repository_runs_does_not_warn(self):
        self.run_dir = self.workspace / '.review-loop/runs/new-layout'
        result = self.run_coordinator()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        scratch = Path(state['scratch_root'])
        self.addCleanup(shutil.rmtree, scratch, True)
        self.assertIsNone(rc.git_work_tree_root(scratch))
        self.assertNotIn(WARNING, result.stderr + result.stdout)

    def test_a_run_directory_outside_any_repository_does_not_warn(self):
        outside = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, outside, True)
        self.assertIsNone(rc.git_work_tree_root(outside))
        self.run_dir = outside / 'run'
        result = self.run_coordinator()
        self.assertNotIn(WARNING, result.stderr + result.stdout)


if __name__ == '__main__':
    unittest.main()
