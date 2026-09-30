"""PR1 (v2.9.1 pre-release review of lane B): F2 workspace git hardening and F3 workspace profile before state."""
import contextlib
import io
import json
import os
import subprocess
import sys
import types
import unittest
import unittest.mock

from paired_session import author_write_guard as guard
from paired_session import candidate_tree as tree
from paired_session import test_real_coordinator as trc

rc = trc.rc


class Fixture(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)


class WorkspaceGitHardeningTests(Fixture):
    """F2: no coordinator git call in the author's workspace runs a program the workspace files choose."""

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.h.workspace), *args], check=True, stdout=subprocess.DEVNULL)

    def rig(self):
        """A repo whose .git/config names fsmonitor, external-diff and textconv programs that leave marker files."""
        ws, markers = self.h.workspace, {}
        keys = {'fsmonitor': 'core.fsmonitor', 'external': 'diff.external', 'textconv': 'diff.rig.textconv'}
        for name, key in keys.items():
            markers[name] = self.h.root / (name + '.marker')
            script = self.h.root / (name + '.sh')
            script.write_text(f'#!/bin/sh\ntouch "{markers[name]}"\n' + ('cat "$1"\n' if name == 'textconv' else 'exit 0\n'))
            script.chmod(0o755)
            self.git('config', key, str(script))
        (ws / '.gitattributes').write_text('*.rig diff=rig\n')
        (ws / 'tracked.rig').write_text('one\n')
        self.git('add', '.gitattributes', 'tracked.rig')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'core.fsmonitor=false',
                 '-c', 'diff.external=', 'commit', '-qm', 'rig')
        (ws / 'tracked.rig').write_text('two\n')
        (ws / 'untracked.rig').write_text('new\n')
        return markers

    def test_workspace_git_config_cannot_run_a_program_from_any_coordinator_git_call(self):
        markers = self.rig()
        co = self.h.coordinator()
        for command in (['diff'], ['diff', '--no-ext-diff'], ['status']):   # control: plain git runs them, so a clean result below means something
            subprocess.run(['git', '-C', str(self.h.workspace), *command], check=True, stdout=subprocess.DEVNULL)
        self.assertEqual({k: m.exists() for k, m in markers.items()}, {k: True for k in markers})
        for marker in markers.values(): marker.unlink()
        co.state['reviews_completed'] = 1
        (co.internal / 'last-review').mkdir(exist_ok=True)
        co.materialize_review_context()
        rc.git_snapshot(self.h.workspace)
        co._head_commit(); co._workspace_names(); co._git(['diff', '--stat'])
        self.assertEqual({k: m.exists() for k, m in markers.items()}, {k: False for k in markers})
        self.assertIn('tracked.rig', (co.context / 'delta.patch').read_text())     # the review context is still produced

    def test_a_workspace_gitattributes_cannot_select_a_filter_driver_for_a_coordinator_git_call(self):
        ws, marker, script = self.h.workspace, self.h.root / 'filter.marker', self.h.root / 'filter.sh'
        script.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n')
        script.chmod(0o755)
        self.git('config', 'filter.x.clean', str(script))                             # a driver the repo already had; the author adds only .gitattributes
        (ws / '.gitattributes').write_text('tracked.txt filter=x\n')
        (ws / 'tracked.txt').write_text('changed\n')
        subprocess.run(['git', '-C', str(ws), 'diff'], check=True, stdout=subprocess.DEVNULL)
        self.assertTrue(marker.exists())                                              # control: plain git runs the driver
        marker.unlink()
        co = self.h.coordinator()
        co.materialize_review_context()
        rc.git_snapshot(ws)
        co._head_commit(); co._workspace_names(); co._git(['diff', '--stat']); co._git(['status', '--short'])
        self.assertFalse(marker.exists())
        self.assertIn('tracked.txt', (co.context / 'delta.patch').read_text())

    def test_one_shared_hardening_constant_feeds_every_workspace_git_helper(self):
        self.assertIs(guard.GIT_NO_EXEC, tree.GIT_NO_EXEC)
        self.assertEqual(sorted(v.split('=')[0] for v in tree.GIT_NO_EXEC if v != '-c'),
                         ['core.fsmonitor', 'core.hooksPath', 'core.pager', 'core.sshCommand', 'diff.external'])
        self.assertEqual(tree.NO_EXT_DIFF, ('--no-ext-diff', '--no-textconv'))
        self.assertEqual(tree.git_command('status'), ['git', *tree.GIT_NO_EXEC, '-c', 'core.attributesFile=/dev/null', *tree._attr_source(), 'status'])


class FilterDriverTests(Fixture):
    """G1/G2: a filter driver the repo (or the operator) already configured never runs for a coordinator git call, whatever attribute source selects it."""

    def wrapper(self):
        """A codex CLI that, during an exec turn, appends each [path, text] of RIG_WRITES (JSON) under the workspace, then runs the fake CLI."""
        real = self.h.fake_codex_cli()
        path = self.h.root / 'wrapper-codex'
        path.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                        'if sys.argv[1:2] == ["exec"]:\n'
                        '    for name, text in json.loads(os.environ.get("RIG_WRITES", "[]")):\n'
                        '        open(os.path.join(os.getcwd(), name), "a").write(text)\n'
                        f'os.execv({str(real)!r}, [{str(real)!r}] + sys.argv[1:])\n')
        path.chmod(0o755)
        return path

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.h.workspace), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def rig(self, source):
        ws, marker, script = self.h.workspace, self.h.root / 'filter.marker', self.h.root / 'filter.sh'
        script.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n')
        script.chmod(0o755)
        (ws / 'tracked.txt').write_text('one\n')
        self.git('add', 'tracked.txt')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'tracked')
        for key in ('clean', 'process'): self.git('config', 'filter.x.' + key, str(script))   # a driver the repo already had
        writes = [['tracked.txt', 'two\n']]
        if source == 'worktree':
            writes.append(['.gitattributes', 'tracked.txt filter=x\n'])
        elif source == 'attributes-file':
            (ws / 'attrs').write_text('')
            self.git('config', 'core.attributesFile', str(ws / 'attrs'))
            writes.append(['attrs', 'tracked.txt filter=x\n'])
        else:
            (ws / '.git' / 'info').mkdir(exist_ok=True)
            (ws / '.git' / 'info' / 'attributes').write_text('tracked.txt filter=x\n')
        return marker, writes

    def check(self, source):
        marker, writes = self.rig(source)
        co = self.h.coordinator('--codex-bin', str(self.wrapper()))
        co.state['phase'] = 'EXEC'                                                 # only an EXEC author turn may change the workspace
        with unittest.mock.patch.dict(os.environ, {'RIG_WRITES': json.dumps(writes)}):
            co.author_turn()
        self.assertNotEqual(co.state.get('status'), 'HOLD', co.state.get('hold_reason'))
        co.materialize_review_context()
        rc.git_snapshot(self.h.workspace)
        co._head_commit(); co._workspace_names(); co._git(['diff', '--stat']); co._git(['status', '--short'])
        self.assertFalse(marker.exists())
        self.assertIn('tracked.txt', (co.context / 'delta.patch').read_text())
        self.git('diff')                                                           # control: plain git runs the driver on this rig
        self.assertTrue(marker.exists())

    def test_a_worktree_gitattributes_added_by_the_author_selects_no_running_driver(self):
        self.check('worktree')

    def test_a_core_attributesfile_pointing_at_a_workspace_file_selects_no_running_driver(self):
        self.check('attributes-file')

    def test_a_preexisting_info_attributes_rule_selects_no_running_driver(self):
        self.check('info')

    def test_a_missing_object_never_lazy_fetches_through_a_configured_transport_program(self):
        ws, marker, script = self.h.workspace, self.h.root / 'ext.marker', self.h.root / 'ext.sh'
        script.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        script.chmod(0o755)
        (ws / 'tracked.txt').write_text('one\n')
        self.git('add', 'tracked.txt')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'tracked')
        blob = subprocess.run(['git', '-C', str(ws), 'rev-parse', 'HEAD:tracked.txt'], text=True, capture_output=True, check=True).stdout.strip()
        (ws / '.git' / 'objects' / blob[:2] / blob[2:]).unlink()                     # the diff below needs this blob
        for key, value in (('remote.origin.url', f'ext::{script}'), ('remote.origin.promisor', 'true'),
                           ('extensions.partialclone', 'origin'), ('protocol.ext.allow', 'always')):
            self.git('config', key, value)
        (ws / 'tracked.txt').write_text('two\n')
        subprocess.run(['git', '-C', str(ws), 'diff', '--stat', 'HEAD'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(marker.exists())                                             # control: plain git lazy-fetches and runs the program
        marker.unlink()
        co = self.h.coordinator()
        with self.assertRaises(RuntimeError):                                        # fails closed on the missing object
            co._git(['diff', '--stat', 'HEAD'])
        self.assertFalse(marker.exists())

    def test_a_global_config_include_pointing_into_the_workspace_is_covered_by_the_control_digest(self):
        ws = self.h.workspace
        (ws / 'git-settings').write_text('')
        config = self.h.root / 'global-gitconfig'
        config.write_text(f'[include]\n\tpath = {ws / "git-settings"}\n')
        co = self.h.coordinator('--codex-bin', str(self.wrapper()))
        co.state['phase'] = 'EXEC'
        env = {'GIT_CONFIG_GLOBAL': str(config), 'RIG_WRITES': json.dumps([['git-settings', '[core]\n\tpager = x\n']])}
        with unittest.mock.patch.dict(os.environ, env):
            with self.assertRaisesRegex(RuntimeError, 'author changed git control files: effective-config--global'):
                co.author_turn()

    def test_a_filter_driver_name_git_cannot_pass_safely_fails_closed(self):
        co = self.h.coordinator()
        self.git('config', 'filter.a=b.clean', 'true')
        with self.assertRaisesRegex(RuntimeError, 'unsupported git filter driver name'):
            tree.git_command('status', cwd=self.h.workspace)
        with self.assertRaisesRegex(RuntimeError, 'unsupported git filter driver name'):   # a new Coordinator refuses too
            self.h.coordinator()
        self.assertEqual(co.drive(), 'HOLD')                                         # before a turn: the drive loop HOLDs
        self.assertIn('unsupported git filter driver name', co.state['hold_reason'])

    def test_an_author_turn_that_adds_an_unsafe_filter_name_still_holds_with_the_control_files_reason(self):
        co = self.h.coordinator('--codex-bin', str(self.wrapper()))
        writes = [['.git/config', '[filter "a=b"]\n\tclean = true\n']]
        with unittest.mock.patch.dict(os.environ, {'RIG_WRITES': json.dumps(writes)}):
            self.assertEqual(co.drive(), 'HOLD')
        self.assertRegex(co.state['hold_reason'], r'^author changed git control files: .*/\.git/config')
        self.assertIn('effective-config--local', co.state['hold_reason'])


class GitControlFileDetectionTests(Fixture):
    """D1: the author may write .git control files; the coordinator never runs them and HOLDs when they change."""

    def wrapper(self, marker):
        """A codex CLI that, when RIG_GIT_CONFIG is set, appends it to .git/config during an exec turn, then runs the fake CLI."""
        real = self.h.fake_codex_cli()
        path = self.h.root / 'wrapper-codex'
        path.write_text(f'#!{sys.executable}\nimport os, sys\n'
                        'if sys.argv[1:2] == ["exec"] and os.environ.get("RIG_GIT_CONFIG"):\n'
                        '    open(os.path.join(os.getcwd(), os.environ.get("RIG_WRITE_TO", ".git/config")), "a").write(os.environ["RIG_GIT_CONFIG"])\n'
                        f'os.execv({str(real)!r}, [{str(real)!r}] + sys.argv[1:])\n')
        path.chmod(0o755)
        return path

    def rig(self, key='fsmonitor'):
        marker = self.h.root / 'fsmonitor.marker'
        script = self.h.root / 'fsmonitor.sh'
        script.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
        script.chmod(0o755)
        return marker, f'[core]\n\t{key} = {script}\n'

    def test_an_author_turn_that_writes_core_fsmonitor_into_git_config_holds_and_never_runs_it(self):
        marker, section = self.rig()
        co = self.h.coordinator('--codex-bin', str(self.wrapper(marker)))
        with unittest.mock.patch.dict(os.environ, {'RIG_GIT_CONFIG': section}):
            self.assertEqual(co.drive(), 'HOLD')
        self.assertRegex(co.state['hold_reason'], r'^author changed git control files: .*/\.git/config')
        self.assertFalse(marker.exists())

    def indirect(self, link):
        """A .git/config that reaches a workspace file (by [include] or by being a symlink to it); the author later writes that file."""
        marker, script = self.h.root / 'filter.marker', self.h.root / 'filter.sh'
        script.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n')
        script.chmod(0o755)
        config = self.h.workspace / '.git' / 'config'
        if link:
            (self.h.workspace / 'cfg-target').write_text(config.read_text())
            config.unlink()
            config.symlink_to(self.h.workspace / 'cfg-target')
            target = '.git/config'
        else:
            config.write_text(config.read_text() + '[include]\n\tpath = ../local-git-config\n')
            (self.h.workspace / 'local-git-config').write_text('')
            target = 'local-git-config'
        co = self.h.coordinator('--codex-bin', str(self.wrapper(marker)))
        with unittest.mock.patch.dict(os.environ, {'RIG_GIT_CONFIG': f'[filter "x"]\n\tclean = {script}\n', 'RIG_WRITE_TO': target}):
            self.assertEqual(co.drive(), 'HOLD')
        return co, marker

    def test_a_file_pulled_in_by_include_is_covered(self):
        co, marker = self.indirect(link=False)
        self.assertRegex(co.state['hold_reason'], r'^author changed git control files: effective-config--local')
        self.assertFalse(marker.exists())

    def test_a_symlinked_git_config_is_hashed_by_its_target_bytes(self):
        co, marker = self.indirect(link=True)
        self.assertRegex(co.state['hold_reason'], r'^author changed git control files: .*/\.git/config')
        self.assertFalse(marker.exists())

    def test_hooks_and_info_attributes_are_covered_and_an_unchanged_turn_does_not_hold(self):
        co = self.h.coordinator('--codex-bin', str(self.wrapper(None)))
        dirs, before = tree_state = rc.git_control_state(self.h.workspace)
        self.assertEqual(len(dirs), 1)
        self.assertEqual(before[str(dirs[0] / 'info' / 'attributes')], 'MISSING')
        hook = dirs[0] / 'hooks' / 'post-commit'
        hook.write_text('#!/bin/sh\n')
        (dirs[0] / 'info').mkdir(exist_ok=True)
        (dirs[0] / 'info' / 'attributes').write_text('* filter=x\n')
        changed = {k for k, v in rc.git_control_state(self.h.workspace, dirs)[1].items() if before.get(k) != v}
        self.assertEqual(changed, {str(hook), str(dirs[0] / 'info' / 'attributes')})
        hook.unlink()
        (dirs[0] / 'info' / 'attributes').unlink()
        self.assertEqual(rc.git_control_state(self.h.workspace, dirs)[1], before)
        co.author_turn()                                                           # a normal turn: no HOLD
        self.assertNotEqual(co.state.get('status'), 'HOLD')


class ControlStateFailureTests(Fixture):
    """K1: whatever goes wrong taking or comparing the git control state ends in a persisted HOLD, never an escaping exception."""

    def wrapper(self):
        """A codex CLI that, during an exec turn, runs the Python in RIG_PY inside the workspace, then runs the fake CLI."""
        real = self.h.fake_codex_cli()
        path = self.h.root / 'wrapper-codex'
        path.write_text(f'#!{sys.executable}\nimport os, sys\n'
                        'if sys.argv[1:2] == ["exec"]: exec(os.environ.get("RIG_PY", ""))\n'
                        f'os.execv({str(real)!r}, [{str(real)!r}] + sys.argv[1:])\n')
        path.chmod(0o755)
        return path

    def restore(self):
        for path in [self.h.workspace / '.git' / 'config', *(self.h.workspace / '.git' / 'hooks').glob('*')]:
            if path.exists(): path.chmod(0o644)

    def drive(self, code, **env):
        self.addCleanup(self.restore)
        co = self.h.coordinator('--codex-bin', str(self.wrapper()))
        with unittest.mock.patch.dict(os.environ, {'RIG_PY': code, **env}):
            self.assertEqual(co.drive(), 'HOLD')
        saved = json.loads((co.run_dir / 'state.json').read_text())
        self.assertEqual(saved['status'], 'HOLD')
        self.assertIsNone(saved['active'])
        self.assertTrue(saved['turns'])                                               # the active turn completed with its receipt
        return co, saved

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file permissions')
    def test_an_author_turn_that_makes_git_config_unreadable_holds_with_a_persisted_state(self):
        co, saved = self.drive('os.chmod(".git/config", 0)')
        self.assertRegex(saved['hold_reason'], r'^author changed git control files: .*/\.git/config')

    @unittest.skipIf(os.geteuid() == 0, 'root ignores file permissions')
    def test_an_author_turn_that_adds_an_unreadable_hook_holds_with_a_persisted_state(self):
        co, saved = self.drive('open(".git/hooks/pre-commit", "w").write("#!/bin/sh\\n"); os.chmod(".git/hooks/pre-commit", 0)')
        self.assertRegex(saved['hold_reason'], r'^author changed git control files: .*/hooks/pre-commit')

    def test_a_filter_name_that_makes_git_command_raise_a_non_runtime_error_holds(self):
        real = tree.git_command
        def raising(*args, **kwargs):                                                 # the R2 shape: CandidateError (a ValueError) from inside git_command
            if 'a=b' in (self.h.workspace / '.git' / 'config').read_text(): raise tree.CandidateError('unsafe filter name')
            return real(*args, **kwargs)
        with unittest.mock.patch.object(tree, 'git_command', raising):
            co, saved = self.drive('open(".git/config", "a").write("[filter \\"a=b\\"]\\n\\tclean = true\\n")')
        self.assertRegex(saved['hold_reason'], r'^author changed git control files: .*effective-config--local')

    def test_any_exception_while_taking_the_state_is_the_unreadable_hold_and_makes_no_further_git_call(self):
        real = rc._git_control_state
        calls = []
        def failing(workspace, dirs=None):
            calls.append(dirs)
            if len(calls) > 1: raise PermissionError('denied')                        # only the post-turn read fails: nothing is compared
            return real(workspace, dirs)
        with unittest.mock.patch.object(rc, '_git_control_state', failing):
            co, saved = self.drive('pass')
        self.assertEqual(saved['hold_reason'], 'git control state unreadable: PermissionError: denied')
        self.assertNotIn('snapshot_after', saved['turns'][-1])                         # no git snapshot after the turn

    def test_a_fifo_replacing_git_config_during_the_turn_holds_instead_of_blocking_git(self):
        co, saved = self.drive('os.unlink(".git/config"); os.mkfifo(".git/config")')   # git would block reading it without a writer
        self.assertRegex(saved['hold_reason'], r'^(author changed git control files: .*/\.git/config|git control state unreadable: )')
        self.assertNotIn('snapshot_after', saved['turns'][-1])

    def test_a_fifo_behind_a_config_include_holds_before_the_turn(self):
        ws = self.h.workspace
        (ws / '.git' / 'config').write_text((ws / '.git' / 'config').read_text() + '[include]\n\tpath = ../inc\n')
        (ws / 'inc').write_text('')
        co = self.h.coordinator('--codex-bin', str(self.wrapper()))
        (ws / 'inc').unlink()
        os.mkfifo(ws / 'inc')
        self.assertEqual(co.drive(), 'HOLD')
        saved = json.loads((co.run_dir / 'state.json').read_text())
        self.assertEqual(saved['status'], 'HOLD')
        self.assertRegex(saved['hold_reason'], r'^git control state unreadable: ')
        self.assertEqual(saved['turns'], [])                                          # no turn was dispatched

    def fifo_attributes(self, link):
        """A pre-existing .git/info/attributes FIFO (or a link to one) that the author never touches: git reads it in diff."""
        info = self.h.workspace / '.git' / 'info'
        info.mkdir(exist_ok=True)
        co = self.h.coordinator('--codex-bin', str(self.wrapper()))
        target = info / ('attributes-target' if link else 'attributes')
        os.mkfifo(target)
        if link: (info / 'attributes').symlink_to(target)
        self.assertEqual(co.drive(), 'HOLD')
        saved = json.loads((co.run_dir / 'state.json').read_text())
        self.assertEqual(saved['status'], 'HOLD')
        self.assertIsNone(saved['active'])
        return saved

    def test_a_preexisting_fifo_info_attributes_holds_instead_of_blocking_a_git_call(self):
        saved = self.fifo_attributes(link=False)
        self.assertRegex(saved['hold_reason'], r'^git control state unreadable: .*info/attributes')
        self.assertEqual(saved['turns'], [])                                          # refused before the turn, before any git call

    def test_a_link_to_a_fifo_as_info_attributes_holds_instead_of_blocking_a_git_call(self):
        saved = self.fifo_attributes(link=True)
        self.assertRegex(saved['hold_reason'], r'^git control state unreadable: .*info/attributes')
        self.assertEqual(saved['turns'], [])

    def test_a_non_regular_control_file_is_a_fixed_marker_not_an_error(self):
        hooks = self.h.workspace / '.git' / 'hooks'
        (hooks / 'sub').mkdir(exist_ok=True)
        self.assertEqual(rc.git_control_state(self.h.workspace)[1][str(hooks / 'sub')], 'NONREGULAR')


class WorkspaceProfileBeforeStateTests(Fixture):
    """F3: the workspace-profile refusal comes before any role, model or gate choice reaches state.json."""

    def args(self, action='run'):
        h = self.h
        argv = [action, '--workspace', str(h.workspace), '--workitem', str(h.workitem), '--run-dir', str(h.run_dir),
                '--codex-bin', str(h.fake_codex_cli()), '--claude-bin', str(h.fake_claude_cli()),
                '--timeout', '10', '--author-effort', 'low', '--reviewer-effort', 'low', '--gate-effort', 'low',
                '--test-command', 'python3 -m unittest']
        return rc.configure_parser(rc.parser(), argv).parse_args(argv)

    def write_profile(self, values):
        path = self.h.workspace / '.review-loop' / 'paired-session.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(values))
        return path

    def main(self, action):
        command = self.h.command()
        command[2] = action
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rc.main(command[2:])
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue())

    def test_a_workspace_profile_is_refused_before_state_exists_and_never_reaches_resume(self):
        baseline = rc.resolve_role_model_defaults(self.args())
        picks = (('reviewer_model', 'gpt-6-astra'), ('gate_vendor', 'claude' if baseline.gate_vendor == 'codex' else 'codex'))
        self.assertNotEqual([getattr(baseline, k) for k, _ in picks], [v for _, v in picks])
        state_path = self.h.run_dir / 'state.json'
        for key, value in picks:
            with self.subTest(key=key):
                profile = self.write_profile({key: value})
                for action in ('run', 'resume'):
                    with self.assertRaisesRegex(ValueError, 'workspace profile cannot select operator programs: ' + key):
                        rc.Coordinator(self.args(action))
                    refused = self.main(action)
                    self.assertEqual(refused.returncode, 2, refused.stdout)
                    self.assertIn('workspace profile cannot select operator programs', refused.stdout)
                self.assertFalse(state_path.exists())
                profile.unlink()
        rc.Coordinator(self.args('resume'))                                        # a fresh run: defaults, nothing from the profile
        config = json.loads(state_path.read_text())['config']
        for key, value in picks:
            self.assertEqual(config[key], getattr(baseline, key), key)
            self.assertNotEqual(config[key], value)
        self.write_profile({'gate_vendor': picks[1][1]})                           # the profile is back: a restore refuses it, never adopts it
        with self.assertRaisesRegex(ValueError, 'role models are fixed for this run: gate_vendor'):
            rc.Coordinator(self.args('resume'))
        self.assertEqual(json.loads(state_path.read_text())['config'], config)

    def test_permission_probe_reports_a_workspace_profile_but_freezes_nothing_from_it(self):
        baseline = rc.resolve_role_model_defaults(self.args())
        self.write_profile({'reviewer_model': 'gpt-6-astra'})
        command = self.h.command()
        command[2] = 'permission-probe'
        result = subprocess.run(command, cwd=self.h.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        report = json.loads((self.h.run_dir / 'permission-probe.json').read_text())
        self.assertIn('workspace profile cannot select operator programs', json.dumps(report['failure_reasons']))
        state_path = self.h.run_dir / 'state.json'
        self.assertEqual(json.loads(state_path.read_text())['config']['reviewer_model'], baseline.reviewer_model)
        for action in ('run', 'resume'):                                           # still refused while the profile exists
            self.assertEqual(self.main(action).returncode, 2, action)
        (self.h.workspace / '.review-loop' / 'paired-session.json').unlink()
        rc.Coordinator(self.args('resume'))                                        # the frozen command-line/default values, never the profile's
        self.assertEqual(json.loads(state_path.read_text())['config']['reviewer_model'], baseline.reviewer_model)


if __name__ == '__main__':
    unittest.main()
