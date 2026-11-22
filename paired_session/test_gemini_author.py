"""WI-GEM2 (ADR-21 amendment 2026-11-19, G2 lifted): agy runs the author (B1: PLAN and EXEC) and the lifecycle writers that
follow the author's vendor (B2: FINISH, DOCS, POLISH-Q), on the fake gemini CLI; no provider is called."""
import contextlib
import io
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import test_gemini_vendor as tgv
from paired_session import test_real_coordinator as trc
from paired_session import test_worktree_lifecycle as twl
from paired_session.lifecycle_test_helpers import use_lifecycle_on

rc = trc.rc
GEMINI_AUTHOR = ['--author-vendor', 'gemini', '--author-model', 'gemini-3.8-pro', '--gate-model', 'gemini-3.8-pro']
PLAN_PROMPT = 'Role: persistent gemini plan author. Phase: PLAN. No delivered review on this turn.'
EXEC_PROMPT = 'Role: persistent gemini implementer. Phase: EXEC. No delivered review on this turn.'


def writer_settings(co) -> str:
    workspace, run_dir = co.workspace.resolve(), co.run_dir.resolve()
    return json.dumps({'permissions': {'allow': [f'write_file({workspace}/*)', 'read_file(*)', 'command(*)'],
                                       'deny': [f'write_file({run_dir}/*)', 'read_url(*)', 'execute_url(*)', 'mcp(*)']}},
                      separators=(',', ':'))


class GeminiAuthorTests(unittest.TestCase):
    """B1: the author's agy argv, its turns and the post-turn guards it shares with a Claude author."""
    argv, args = tgv.GeminiVendorTests.argv, tgv.GeminiVendorTests.args

    def setUp(self):
        tgv.GeminiAdapterTests.setUp(self)

    def coordinator(self, *extra, agy=None):
        return rc.Coordinator(self.args(*GEMINI_AUTHOR, '--gemini-bin', str(agy or self.h.fake_gemini_cli()), *extra))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def receipt(self, co, sequence, phase, role='author'):
        return json.loads((co.evidence / f'{sequence:03d}-{phase.lower()}-{role}.receipt.json').read_text())

    def wrapped_agy(self, code):
        """A fake agy that runs `code` (with `os` and `Path` imported) first, then answers as the fake gemini CLI."""
        path = self.h.root / 'agy-wrapped'
        path.write_text(f'#!{sys.executable}\nimport os, subprocess, sys\nfrom pathlib import Path\n'
                        'if sys.argv[1:] != ["--version"]:\n' + ''.join('    ' + line + '\n' for line in code.splitlines()) +
                        f'sys.exit(subprocess.run([{str(self.h.fake_gemini_cli())!r}, *sys.argv[1:]]).returncode)\n')
        path.chmod(0o755)
        return path

    def test_the_author_argv_carries_the_writer_settings_and_resumes_by_conversation(self):
        co = self.coordinator()
        schema = self.h.root / 'schema.json'
        rc.atomic_json(schema, rc.author_schema())
        agy = str(self.h.fake_gemini_cli())
        self.assertEqual(co.command('author', schema, False, co.workspace), [
            agy, '--print=', '--input-format', 'stream-json', '--output-format', 'stream-json', '--model', 'gemini-3.8-pro',
            '--effort', 'medium', '--json-schema', str(schema), '--sandbox', '--settings', writer_settings(co)])
        manifest = co._role_dispatch_manifest()['role_flags']['author']
        self.assertEqual((manifest['vendor'], manifest['sandbox']['settings'], manifest['sandbox']['TMPDIR']),
                         ('gemini', json.loads(writer_settings(co)), str(co.author_temp_dir)))
        co.invoke('author', 'PLAN', PLAN_PROMPT, rc.author_schema())
        co.invoke('author', 'EXEC', EXEC_PROMPT, rc.author_schema())
        plan, exec_ = self.calls()
        conversation = co.state['sessions']['author']
        self.assertTrue(conversation and co.state['started']['author'])
        self.assertNotIn('--conversation', plan['args'])
        self.assertEqual(exec_['args'][-2:], ['--conversation', conversation])
        self.assertEqual(exec_['args'][exec_['args'].index('--settings') + 1], writer_settings(co))
        self.assertIn('--sandbox', exec_['args'])
        for call in (plan, exec_):   # the author's own TMPDIR, as for a Codex author; cwd = workspace
            self.assertEqual((Path(call['cwd']).resolve(), call['TMPDIR']), (co.workspace.resolve(), str(co.author_temp_dir)))
        self.assertIn('ints only', (co.workspace / 'sum_ints.py').read_text())   # the EXEC author wrote its change
        receipt = self.receipt(co, 2, 'EXEC')
        self.assertEqual((receipt['vendor'], list(receipt['global_gemini_before']), receipt['global_config_changes']['status']),
                         ('gemini', ['gemini_settings'], 'PASS'))
        self.assertNotIn('--conversation', co.command('author', schema, True, co.workspace))   # a fresh writer never resumes

    def test_a_denied_or_failed_author_turn_fails_closed(self):
        cases = (({'FAKE_GEMINI_FAIL': 'denied'}, r'agy denied write_file; a denied permission ends the turn \(ADR-21 G6\)'),
                 ({'FAKE_GEMINI_FAIL': 'error', 'FAKE_GEMINI_FAIL_EXIT': '0'}, 'agy turn status ERROR: fake agy failure'),
                 ({'FAKE_GEMINI_FAIL': 'empty'}, 'agy returned an empty response and no structured_output'))
        for index, (env, message) in enumerate(cases):
            with self.subTest(env=env):
                self.h.run_dir = self.h.root / f'fail-{index}'
                co = self.coordinator()
                with patch.dict(os.environ, env), self.assertRaisesRegex(RuntimeError, message):
                    co.invoke('author', 'EXEC', EXEC_PROMPT, rc.author_schema())
                state = json.loads(co.state_path.read_text())
                self.assertRegex(state['turns'][-1]['error'], message)
                self.assertEqual((state['started']['author'], state['sessions']['author']), (False, None))

    def test_a_gemini_author_that_commits_or_changes_agy_settings_is_caught_like_a_claude_author(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_AUTHOR_COMMIT': '1'}), \
                self.assertRaisesRegex(RuntimeError, 'author changed HEAD or the branch'):
            co.invoke('author', 'EXEC', EXEC_PROMPT, rc.author_schema())
        self.h.run_dir = self.h.root / 'settings-run'
        subprocess.run(['git', 'reset', '-q', '--hard', 'HEAD~1'], cwd=self.h.workspace, check=True)
        settings = self.h.test_home / rc.GEMINI_SETTINGS_PATH
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({'colorScheme': 'dark'}))
        source = self.h.root / 'agy-settings.json'
        source.write_text(json.dumps({'colorScheme': 'light'}))
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_GEMINI_SETTINGS_WRITE': str(source)}), \
                self.assertRaisesRegex(RuntimeError, 'gemini turn changed global config: gemini_settings'):
            co.invoke('author', 'EXEC', EXEC_PROMPT, rc.author_schema())

    def test_a_gemini_author_that_changes_git_config_or_a_run_dir_file_is_caught(self):
        co = self.coordinator(agy=self.wrapped_agy('subprocess.run(["git", "config", "--local", "gem2.stray", "1"], check=True)'))
        with self.assertRaisesRegex(RuntimeError, 'author changed git control files'):
            co.invoke('author', 'PLAN', PLAN_PROMPT, rc.author_schema())
        subprocess.run(['git', 'config', '--local', '--unset', 'gem2.stray'], cwd=self.h.workspace, check=True)
        self.h.run_dir = self.h.root / 'run-dir-write'
        marker = self.h.root / 'run-dir-written'
        co = self.coordinator(agy=self.wrapped_agy(   # the EXEC turn appends to the PLAN turn's receipt in the run dir
            f'target = Path({str(self.h.run_dir / "evidence" / "001-plan-author.receipt.json")!r})\n'
            f'if target.exists() and not Path({str(marker)!r}).exists():\n'
            f'    Path({str(marker)!r}).write_text("x")\n'
            '    target.write_text(target.read_text() + "tampered")'))
        co.invoke('author', 'PLAN', PLAN_PROMPT, rc.author_schema())
        with self.assertRaisesRegex(RuntimeError, 'author changed run-dir files during its turn'):
            co.invoke('author', 'EXEC', EXEC_PROMPT, rc.author_schema())

    def test_a_gemini_author_is_refused_on_windows(self):   # ADR-21 amendment GM1 stays: gemini is not part of ADR-20
        args = types.SimpleNamespace(detach=False, action='run', safety_mode=None, run_dir=str(self.h.root / 'win-run'),
                                     author_vendor='gemini', reviewer_vendor='claude', gate_vendor='claude')
        with patch.object(rc.winplat, 'IS_WINDOWS', True):
            self.assertIn('a gemini role is not supported on Windows', rc.windows_refusal(args))
        self.assertIsNone(rc.windows_refusal(args))

    def test_a_gemini_reviewer_that_writes_a_tracked_file_is_restored_and_redispatched_once(self):
        # agy 1.5.1: under --sandbox a shell write inside the workspace succeeds even with the read-only settings; the
        # role-keyed ReadOnlyTurnVoided guard restores the tree and re-dispatches the turn once
        co = rc.Coordinator(self.args(*tgv.GEMINI_REVIEWER, '--gemini-bin', str(self.h.fake_gemini_cli())))
        once = self.h.root / 'mutated-once'
        out = io.StringIO()
        with patch.dict(os.environ, {'FAKE_MUTATION': 'checkout', 'FAKE_MUTATION_ONCE': str(once)}), contextlib.redirect_stdout(out):
            result = co.invoke('reviewer', 'PLAN', tgv.PLAN_REVIEW, rc.review_schema())
        self.assertTrue(once.exists())
        self.assertEqual((self.h.workspace / 'tracked.txt').read_text(), 'base\n')   # restored
        self.assertEqual(result['answer']['status'], 'APPROVE')
        turns = co.state['turns']
        self.assertEqual([(t['role'], t['vendor'], bool(t.get('error'))) for t in turns],
                         [('reviewer', 'gemini', True), ('reviewer', 'gemini', False)])
        self.assertIn('re-dispatching the reviewer turn once', out.getvalue())
        self.assertIn('your previous answer to this request changed the workspace', self.calls()[1]['stdin'])


class GeminiAuthorRunTests(unittest.TestCase):
    """B1 + B2 end to end: a gemini author (default gemini gate, ADR-10) with a Claude or Codex reviewer reaches DONE."""
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in (*twl._HELPERS, 'fake_gemini_cli')})

    def setUp(self):
        twl.WorktreeLifecycleActivationTests.setUp(self)
        self.log = self.root / 'agy-log.jsonl'
        env = patch.dict(os.environ, {'FAKE_GEMINI_LOG': str(self.log)})
        env.start()
        self.addCleanup(env.stop)

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def test_a_lifecycle_off_run_with_a_claude_reviewer_reaches_done(self):
        done = self.run_coordinator(*GEMINI_AUTHOR, '--gemini-bin', str(self.fake_gemini_cli()))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = self.state()
        self.assertEqual(state['status'], 'DONE')
        self.assertEqual({(t['role'], t['vendor']) for t in state['turns']},
                         {('author', 'gemini'), ('reviewer', 'claude'), ('shadow', 'claude'), ('gate', 'gemini')})

    def test_gemini_author_repairs_the_default_gemini_gates_failure(self):
        done = self.run_coordinator(*GEMINI_AUTHOR, '--gemini-bin', str(self.fake_gemini_cli()),
                                    '--lifecycle-mode', 'off', '--shadow', 'off', env={'FAKE_GATE_BLOCK': '1'})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = self.state()
        self.assertEqual(state['status'], 'DONE')
        self.assertEqual((state['config']['gate_vendor'], state['config']['gate_vendor_source']), ('gemini', 'default'))
        [gate] = [turn for turn in state['turns'] if turn['role'] == 'gate']
        self.assertEqual((gate['vendor'], gate['phase'], gate['answer']['verdict']), ('gemini', 'EXEC', 'FAIL'))
        self.assertEqual({turn['vendor'] for turn in state['turns'] if turn['role'] == 'author'}, {'gemini'})
        [repair, review] = [turn for turn in state['turns'] if turn['sequence'] > gate['sequence']]
        self.assertEqual([(turn['role'], turn['phase'], turn['answer']['status']) for turn in (repair, review)],
                         [('author', 'EXEC', 'READY'), ('reviewer', 'EXEC', 'APPROVE')])
        prompt = (self.run_dir / 'evidence' / f"{repair['sequence']:03d}-exec-author.prompt.txt").read_text()
        self.assertIn('Smallest change: reject bool', prompt)
        self.assertNotEqual(repair['snapshot_before'], repair['snapshot_after'])
        self.assertEqual(review['snapshot_before'], repair['snapshot_after'])
        [item] = state['gate_fail_items']
        [row] = [row for row in state['finding_ledger'] if row['source'] == 'adversarial-gate']
        self.assertEqual((row['id'], row['status'], row['status_history'][-1]['round']),
                         (item['id'], 'fixed', review['sequence']))

    def test_a_lifecycle_on_run_dispatches_every_writer_on_gemini(self):
        use_lifecycle_on(self, self)
        done = self.run_coordinator(*GEMINI_AUTHOR, '--gemini-bin', str(self.fake_gemini_cli()), '--quality-writers', 'both',
                                    '--max-invocations', '60', env={'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'})
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        turns = self.state()['turns']
        self.assertEqual({t['phase'] for t in turns if t['role'] == 'author'}, {'PLAN', 'EXEC', 'FINISH', 'DOCS'})
        self.assertEqual({t['vendor'] for t in turns if t['role'] in ('author', 'gate')}, {'gemini'})
        self.assertEqual({t['vendor'] for t in turns if t['role'] in ('reviewer', 'shadow')}, {'claude'})
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        writers = [c for c in calls if any(rule.startswith('write_file(') for rule in
                                           json.loads(c['args'][c['args'].index('--settings') + 1])['permissions']['allow'])]
        self.assertEqual(len(writers), len([t for t in turns if t['role'] == 'author']))   # only the author gets writer settings
        lifecycle = [c for c in writers if 'Role: persistent' not in json.loads(c['stdin'])['message']['content']]
        self.assertEqual(len(lifecycle), 2)   # the finisher and the docs writer (the quality writers skip this small change)
        self.assertFalse(any('--conversation' in c['args'] for c in lifecycle))   # the lifecycle writers are fresh sessions

    def test_a_gemini_quality_writer_turn_dispatches_and_a_committing_finisher_holds(self):
        use_lifecycle_on(self, self)
        args = self.command(*GEMINI_AUTHOR, '--gemini-bin', str(self.fake_gemini_cli()), '--quality-writers', 'both')[2:]
        co = rc.Coordinator(rc.configure_parser(rc.parser(), args).parse_args(args))   # a W run: the frozen manifest is verified
        result = co.invoke('author', 'POLISH-Q', 'Role: simplifier. Phase: POLISH-Q.', rc.author_schema(), fresh=True)
        self.assertEqual((result['answer']['status'], co.state['turns'][-1]['vendor']), ('READY', 'gemini'))
        self.assertNotIn('--conversation', co.state['turns'][-1]['command'])
        self.run_dir = self.root / 'finish-commit'
        held = self.run_coordinator(*GEMINI_AUTHOR, '--gemini-bin', str(self.fake_gemini_cli()), env={'FAKE_FINISH_COMMIT': '1'})
        self.assertIn('HOLD: finisher changed HEAD, refs or the index', held.stdout, held.stdout + held.stderr)
        self.assertEqual(self.state()['turns'][-1]['vendor'], 'gemini')


if __name__ == '__main__':
    unittest.main()
