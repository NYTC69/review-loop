"""WI-WIN W10: fixes from Yuan's Windows acceptance run (2026-10-27).

1. Read-only files: Git for Windows writes pack/idx and object files read-only; winplat.rmtree (and the standalone copy in
   scripts/materialize_pr.py) clears the attribute and retries once. The retry logic runs on every OS with patched calls;
   NativeWindowsRemoveTests (windows-latest CI) remove a real cloned repository and a plain read-only file.
2. Every Codex role argv carries `-c windows.sandbox="elevated"` on Windows only; the POSIX argv is pinned.
3a. A reviewer / shadow HOLD reason carries the role's own words after the `reviewer HOLD` prefix; r2: so does a DOCS,
   SECURITY or specialist HOLD after its `... without a usable review` prefix (text only).
3b. A Codex config refusal ends with a hint: a dedicated CODEX_HOME for a user-home finding, the named file for a
   project or system finding (r2) (text only).
CoordinatorTextTests drive the coordinator with the POSIX fake-CLI harness (the Windows lifecycle is test_winend2end)."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from paired_session import codex_capability_guard as guard
from paired_session import coordinator as rc
from paired_session import winplat
from scripts import materialize_pr as mp

IS_WINDOWS = os.name == 'nt'
# The reviewer's answer in Yuan's run (Codex reviewer, every shell command rejected by the sandbox policy).
FIELD_EVIDENCE = ('Source inspection attempted with read-only shell commands, but execution policy rejected both '
                  'PowerShell and Bash. Required source verification could not be completed.')
GIT_ENV = {'GIT_AUTHOR_NAME': 'w10', 'GIT_AUTHOR_EMAIL': 'w10@example.invalid', 'GIT_COMMITTER_NAME': 'w10',
           'GIT_COMMITTER_EMAIL': 'w10@example.invalid', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}


def calling_handler(failures):
    """A stand-in for shutil.rmtree that reports each (function, path, exception) to the handler it was given."""
    def fake(path, onexc=None, onerror=None):
        for function, target, exc in failures:
            if onexc is not None:
                onexc(function, target, exc)
            else:
                onerror(function, target, (type(exc), exc, None))
    return mock.Mock(side_effect=fake)


def named(name, side_effect=None):
    function = mock.Mock(side_effect=side_effect)
    function.__name__ = name
    return function


class RmtreeRetryTests(unittest.TestCase):
    """Both helpers, on every OS: which failures get the chmod + one retry, and which are raised as they came."""
    helpers = (('winplat', winplat.rmtree, winplat), ('materialize_pr', mp.rmtree_windows, mp))

    def run_helper(self, helper, module, failures, **kwargs):
        rmtree = calling_handler(failures)
        with mock.patch('shutil.rmtree', rmtree), mock.patch.object(module.os, 'chmod') as chmod:
            helper('tree', **kwargs)
        return rmtree, chmod

    def test_a_permission_error_on_a_removal_clears_read_only_and_retries_once(self):
        for label, helper, module in self.helpers:
            for name in ('unlink', 'remove', 'rmdir'):
                with self.subTest(helper=label, function=name):
                    function = named(name)
                    _, chmod = self.run_helper(helper, module, [(function, 'tree/x.pack', PermissionError(13, 'denied'))])
                    chmod.assert_called_once_with('tree/x.pack', stat.S_IWRITE)
                    function.assert_called_once_with('tree/x.pack')

    def test_other_failures_are_raised_unchanged_without_a_chmod(self):
        for label, helper, module in self.helpers:
            for function, exc in ((named('unlink'), FileNotFoundError(2, 'gone')),
                                  (named('scandir'), PermissionError(13, 'denied')),
                                  (named('open'), PermissionError(13, 'denied'))):
                with self.subTest(helper=label, function=function.__name__, error=type(exc).__name__):
                    with self.assertRaises(type(exc)) as raised:
                        self.run_helper(helper, module, [(function, 'tree/x', exc)])
                    self.assertIs(raised.exception, exc)
                    function.assert_not_called()

    def test_a_retry_that_fails_again_raises_unless_errors_are_ignored(self):
        again = PermissionError(13, 'still denied')
        for label, helper, module in self.helpers:
            with self.subTest(helper=label):
                function = named('unlink', again)
                with self.assertRaises(PermissionError) as raised:
                    self.run_helper(helper, module, [(function, 'tree/x', PermissionError(13, 'denied'))])
                self.assertIs(raised.exception, again)
                function.assert_called_once_with('tree/x')
        function = named('unlink', again)
        self.run_helper(winplat.rmtree, winplat, [(function, 'tree/x', PermissionError(13, 'denied')),
                                                  (named('scandir'), 'tree', OSError(5, 'io'))], ignore_errors=True)
        function.assert_called_once_with('tree/x')

    def test_the_handler_keyword_follows_the_python_version(self):
        for label, helper, module in self.helpers:
            for version, keyword in (((3, 11, 9), 'onerror'), ((3, 12, 0), 'onexc')):
                with self.subTest(helper=label, version=version):
                    with mock.patch.object(module, 'sys', SimpleNamespace(version_info=version)):
                        rmtree, _ = self.run_helper(helper, module, [])
                    self.assertEqual(list(rmtree.call_args.kwargs), [keyword])

    def test_a_real_tree_with_read_only_entries_is_removed(self):
        for label, helper, _ in self.helpers:
            with self.subTest(helper=label), tempfile.TemporaryDirectory() as directory:
                tree = Path(directory) / 'tree'
                (tree / 'objects' / 'pack').mkdir(parents=True)
                for path in (tree / 'objects' / 'pack' / 'pack-1.idx', tree / 'objects' / 'pack' / 'pack-1.pack'):
                    path.write_bytes(b'x')
                    os.chmod(path, stat.S_IREAD)
                helper(tree)
                self.assertFalse(os.path.lexists(tree))


@unittest.skipIf(IS_WINDOWS, 'POSIX call sites')
class PosixCallSiteTests(unittest.TestCase):
    def test_materialize_remove_calls_shutil_rmtree_on_posix(self):
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory) / 'pr' / str(uuid.uuid4())
            (clone / '.git').mkdir(parents=True)
            (clone / '.git' / mp.MARKER).write_text(clone.name + '\n')
            with mock.patch.object(mp, 'rmtree_windows') as windows, mock.patch.object(mp.shutil, 'rmtree') as rmtree:
                mp.remove(clone)
            windows.assert_not_called()
            rmtree.assert_called_once_with(clone.resolve())


@unittest.skipUnless(IS_WINDOWS, 'native Windows read-only attribute')
class NativeWindowsRemoveTests(unittest.TestCase):
    def git(self, *args, cwd=None):
        subprocess.run(['git', *args], cwd=cwd, env={**os.environ, **GIT_ENV}, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_materialize_remove_deletes_a_clone_with_read_only_pack_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.mkdir()
            self.git('init', '-q', cwd=source)
            (source / 'a.txt').write_text('a\n')
            self.git('add', 'a.txt', cwd=source)
            self.git('commit', '-q', '-m', 'a', cwd=source)
            clone = Path(directory) / 'pr' / str(uuid.uuid4())
            self.git('clone', '-q', '--no-local', str(source), str(clone))
            self.git('gc', '-q', cwd=clone)
            (clone / '.git' / mp.MARKER).write_text(clone.name + '\n')
            read_only = [path for path in (clone / '.git' / 'objects').rglob('*')
                         if path.is_file() and not os.stat(path).st_mode & stat.S_IWRITE]
            self.assertTrue(read_only, 'git wrote no read-only object file: the case under test is absent')
            self.assertEqual(mp.remove(clone), str(clone.resolve()))
            self.assertFalse(os.path.lexists(clone))

    def test_winplat_rmtree_deletes_a_plain_read_only_file_and_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            tree = Path(directory) / 'tree'
            (tree / 'sub').mkdir(parents=True)
            (tree / 'sub' / 'locked.txt').write_text('x')
            os.chmod(tree / 'sub' / 'locked.txt', stat.S_IREAD)
            os.chmod(tree / 'sub', stat.S_IREAD)   # FILE_ATTRIBUTE_READONLY on a directory
            winplat.rmtree(tree)
            self.assertFalse(os.path.lexists(tree))


class CodexWindowsSandboxTests(unittest.TestCase):
    ROLES = ('author', 'reviewer', 'shadow', 'gate')

    def command(self, role, windows):
        co = rc.Coordinator.__new__(rc.Coordinator)
        co.args = SimpleNamespace(codex_bin='codex')
        co.state = {'started': {}, 'sessions': {}}
        co._model_effort = mock.Mock(return_value=('model', 'high'))
        co._author_sandbox_config_args = mock.Mock(return_value=['--author-sandbox'])
        with mock.patch.object(rc.winplat, 'IS_WINDOWS', windows):
            return co._codex_command(role, Path('schema.json'), True)

    def test_windows_adds_the_elevated_sandbox_once_for_every_role(self):
        for role in self.ROLES:
            with self.subTest(role=role):
                command = self.command(role, True)
                self.assertEqual(command.count('windows.sandbox="elevated"'), 1)
                at = command.index('windows.sandbox="elevated"')
                self.assertEqual(command[at - 1], '-c')
                self.assertEqual(command[:at + 1], [*self.command(role, False)[:at - 1], *rc.CODEX_WINDOWS_SANDBOX])

    def test_the_posix_argv_is_unchanged(self):
        common = ['codex', 'exec', '-m', 'model', '--json', '--output-schema', 'schema.json',
                  '-c', 'model_reasoning_effort="high"', '-c', 'approval_policy="never"', '-c', 'features.hooks=false',
                  '-c', 'features.plugins=false']
        self.assertEqual(self.command('reviewer', False),
                         [*common, *rc.codex_readonly_profile_args(), '--ignore-rules', '-'])
        self.assertEqual(self.command('author', False), [*common, '--author-sandbox', '--ignore-rules', '-'])
        for role in self.ROLES:
            self.assertNotIn('windows.sandbox="elevated"', self.command(role, False))

    def test_the_guard_passes_a_user_config_with_the_elevated_windows_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            home, workspace = Path(directory) / 'codex-home', Path(directory) / 'workspace'
            home.mkdir()
            workspace.mkdir()
            (home / 'config.toml').write_text('[windows]\nsandbox = "elevated"\n')
            result = guard.inspect(home, workspace, platform='win32')
            self.assertEqual((result['status'], result['issues']), ('PASS', []))


class GuardHintTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home, self.workspace = Path(directory.name) / 'codex-home', Path(directory.name) / 'workspace'
        self.home.mkdir()
        (self.workspace / '.git').mkdir(parents=True)

    def test_a_user_config_finding_gets_the_codex_home_hint_after_the_unchanged_text(self):
        (self.home / 'config.toml').write_text('notify = ["notify.exe"]\n[mcp_servers.node_repl]\ncommand = "node"\n')
        result = guard.inspect(self.home, self.workspace, platform='win32')
        config = self.home.resolve() / 'config.toml'
        self.assertEqual(guard.refusal(result, self.home),
                         f'MCP servers configured in {config}; notify configured in {config}'
                         '; use a dedicated CODEX_HOME directory with its own `codex login` (see README)')

    def test_a_project_config_finding_names_its_file_not_codex_home(self):
        (self.workspace / '.codex').mkdir()
        (self.workspace / '.codex' / 'config.toml').write_text('[mcp_servers.local]\ncommand = "node"\n')
        result = guard.inspect(self.home, self.workspace, platform='win32')
        project = self.workspace.resolve() / '.codex' / 'config.toml'
        self.assertEqual(guard.refusal(result, self.home), f'MCP servers configured in {project}; change or remove that '
                         'setting in the named project or system file (CODEX_HOME does not cover it)')

    def test_system_and_bundle_findings_and_both_kinds_together(self):
        home = self.home.resolve()
        system = {'status': 'FAIL', 'issues': ['notify configured in /etc/codex/config.toml']}
        self.assertEqual(guard.refusal(system, self.home), system['issues'][0] + guard.SOURCE_HINT)
        bundle = {'status': 'FAIL', 'issues': [f'plugin MCP or app bundle configured in {home}/plugins/cache/a/b/1/.mcp.json']}
        self.assertEqual(guard.refusal(bundle, self.home), bundle['issues'][0] + guard.CONFIG_HINT)
        both = {'status': 'FAIL', 'issues': [f'notify configured in {home}/config.toml', system['issues'][0]]}
        self.assertEqual(guard.refusal(both, self.home), '; '.join(both['issues']) + guard.CONFIG_HINT + guard.SOURCE_HINT)
        for issue in ('cannot parse effective Codex config: /h/config.toml', 'macOS MDM Codex policy configured: key'):
            self.assertEqual(guard.refusal({'status': 'FAIL', 'issues': [issue]}, self.home), issue)


class HoldExplanationTests(unittest.TestCase):
    def test_the_field_answer_puts_its_evidence_text_after_the_prefix(self):
        evidence = [{'command': FIELD_EVIDENCE}]
        self.assertEqual(rc.hold_explanation('reviewer HOLD', [], evidence), 'reviewer HOLD: ' + FIELD_EVIDENCE)

    def test_finding_summaries_come_before_the_evidence(self):
        findings = [{'summary': 'Cannot run the tests'}, {'summary': 'the build is\nbroken'}, 'not a row']
        self.assertEqual(rc.hold_explanation('shadow HOLD', findings, [{'command': 'pytest'}]),
                         'shadow HOLD: Cannot run the tests the build is broken')

    def test_one_line_bounded_and_the_bare_prefix_when_nothing_is_said(self):
        text = rc.hold_explanation('reviewer HOLD', [], [{'command': 'a\n\tb  ' + 'x' * 400}])
        self.assertEqual(text, 'reviewer HOLD: ' + ('a b ' + 'x' * 400)[:300])
        for findings, evidence in (([], []), (None, None), ([{'summary': ''}], [{'command': ''}])):
            self.assertEqual(rc.hold_explanation('reviewer HOLD', findings, evidence), 'reviewer HOLD')


@unittest.skipIf(IS_WINDOWS, 'the POSIX fake-CLI harness; the Windows lifecycle is test_winend2end')
class CoordinatorTextTests(unittest.TestCase):
    def setUp(self):
        from paired_session import test_real_coordinator as trc
        self.case = trc.RealCoordinatorTests('test_resume_after_reviewer_hold_dispatches_a_fresh_turn')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(self.case.tearDown)

    def test_a_reviewer_hold_reason_and_terminal_line_carry_its_explanation(self):
        co = self.case.coordinator('--stop-after-plan')
        co.state.update(phase='PLAN', next='reviewer')
        co.save()
        co.materialize_review_context()
        snapshot = rc.git_snapshot(self.case.workspace)[0]
        with mock.patch.dict(os.environ, {'FAKE_REVIEW_HOLD': '1', 'FAKE_REVIEW_HOLD_EVIDENCE': FIELD_EVIDENCE}):
            co.invoke('reviewer', 'PLAN', co._review_prompt('reviewer', snapshot), rc.review_schema())
        resumed = rc.Coordinator(co.args)
        self.assertEqual(resumed.resume(), 'HOLD')
        self.assertEqual(resumed.state['hold_reason'], 'reviewer HOLD: ' + FIELD_EVIDENCE)
        terminal = rc.json.loads((co.run_dir / 'progress.jsonl').read_text().splitlines()[-1])
        self.assertEqual((terminal['kind'], terminal['reason']),
                         ('terminal', rc.brief_reason('reviewer HOLD: ' + FIELD_EVIDENCE)))
        self.assertTrue(terminal['reason'].startswith('reviewer HOLD: Source inspection attempted'))

    def test_a_codex_config_refusal_names_the_codex_home_fix(self):
        from paired_session.lifecycle_test_helpers import use_lifecycle_on
        use_lifecycle_on(self.case, self.case)
        (self.case.test_home / '.codex/config.toml').write_text('notify = "hook"\n')
        co = self.case.coordinator('--author-vendor', 'codex')
        # the permission-probe keeps its pinned source (CODEX_PROBE_SHA256), so its FAIL reason has no hint
        self.assertFalse(co._author_permission_probe()['reason'].endswith(guard.CONFIG_HINT))
        with self.assertRaises(RuntimeError) as raised:
            co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})
        self.assertRegex(str(raised.exception), r'^notify configured in .*; use a dedicated CODEX_HOME directory')
        self.assertEqual(co.state['sequence'], 0)


@unittest.skipIf(IS_WINDOWS, 'the POSIX fake-CLI harness; the Windows lifecycle is test_winend2end')
class StageHoldTextTests(unittest.TestCase):
    """W10 r2: a DOCS, SECURITY or POLISH-Q specialist HOLD keeps its `<role> returned HOLD without a usable review` prefix
    and appends the role's own words; a REVISE without a usable review keeps its old text."""
    HOLD = {'status': 'HOLD', 'full_review': [], 'prior_findings': [], 'self_run_evidence': [{'command': FIELD_EVIDENCE}]}

    def setUp(self):
        from paired_session import test_worktree_lifecycle as twl
        self.case = twl.WorktreeLifecycleActivationTests('test_the_security_budget_caps_reviewer_dispatches')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.addCleanup(self.case.tearDown)

    def test_unusable_review_text(self):
        self.assertEqual(rc.unusable_review('docs reviewer', self.HOLD),
                         'docs reviewer returned HOLD without a usable review: ' + FIELD_EVIDENCE)
        self.assertEqual(rc.unusable_review('docs reviewer', {**self.HOLD, 'self_run_evidence': []}),
                         'docs reviewer returned HOLD without a usable review')
        self.assertEqual(rc.unusable_review('specialist x', {**self.HOLD, 'status': 'REVISE'}),
                         'specialist x returned REVISE without a usable review')

    def test_a_docs_reviewer_hold(self):
        with self.assertRaises(RuntimeError) as raised:
            self.case.docs_review_with(self.HOLD)
        self.assertEqual(str(raised.exception), 'docs reviewer returned HOLD without a usable review: '
                         + FIELD_EVIDENCE + '; resume reviews again')

    def test_a_security_reviewer_hold_is_discarded_as_before(self):
        co, tree = self.case.at_security()
        with mock.patch.object(co, 'materialize_review_context'), mock.patch.object(co, 'render'), \
                mock.patch.object(co, 'invoke', side_effect=self.case.security_turn_stub(co, tree, self.HOLD)), \
                self.assertRaises(RuntimeError) as raised:
            co._security_review_turn(tree, 'w-SECURITY-0-0')
        reason = 'security reviewer returned HOLD without a usable review: ' + FIELD_EVIDENCE
        self.assertEqual(str(raised.exception), reason + '; resume reviews again')
        self.assertEqual((co.state['turns'][-1]['discarded'], co.state['lifecycle'].get('security_turn')),
                         (reason[:200], None))

    def test_a_specialist_hold(self):
        co, tree = self.case.at_security()
        stub = self.case.security_turn_stub(co, tree, self.HOLD)
        with mock.patch.object(co, 'materialize_review_context'), mock.patch.object(co, 'render'), \
                mock.patch.object(co, 'invoke', side_effect=stub), self.assertRaises(RuntimeError) as raised:
            co._specialist_turn('code-reviewer', ['sum_ints.py'], tree)
        self.assertEqual(str(raised.exception),
                         'specialist code-reviewer returned HOLD without a usable review: ' + FIELD_EVIDENCE)


if __name__ == '__main__':
    unittest.main()
