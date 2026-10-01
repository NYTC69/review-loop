"""Batch PL (v2.9.3): live progress events (stdout + RUN/progress.jsonl), status --brief, allowlist structure check."""
import json
import os
import re
import stat
import subprocess
import types
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'coordinator')
PROMPT_SENTINEL, OUTPUT_SENTINEL, FAILURE_TEXT = 'PROMPT-SENTINEL-9f31', 'OUTPUT-SENTINEL-c7a2', 'bool is accepted as int'
LIVE = re.compile(r'^\[\d\d:\d\d:\d\d\] ')
FLAGS = ('--exercise-revisions', '--shadow', 'off', '--polish-round', 'off', '--gate-vendor', 'claude')


class ProgressTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def rows(self):
        return [json.loads(line) for line in (self.run_dir / 'progress.jsonl').read_text().splitlines()]

    def run_with_sentinels(self, *extra):
        self.workitem.write_text(self.workitem.read_text() + PROMPT_SENTINEL + '\n')
        return self.run_coordinator(*FLAGS, *extra, env={'FAKE_AUTHOR_RATIONALE': OUTPUT_SENTINEL})

    def test_a_run_writes_the_event_sequence_to_jsonl_and_stdout(self):
        done = self.run_with_sentinels()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        rows = self.rows()
        self.assertEqual([row['seq'] for row in rows], list(range(1, len(rows) + 1)))
        phases = [row['label'] for row in rows if row['kind'] == 'phase']
        self.assertEqual(phases[:3], ['PLAN r1', 'PLAN r2', 'EXEC r1'])
        ends = [row for row in rows if row['kind'] == 'dispatch' and row['step'] == 'end']
        self.assertTrue(ends and all(row['outcome'] == 'ok' and isinstance(row['seconds'], int) for row in ends))
        self.assertTrue({'role', 'vendor', 'model', 'effort', 'fresh'} <= set(ends[0]))
        self.assertTrue(any(row['kind'] == 'finding' and row['status'] == 'open' for row in rows))
        self.assertIn('REVISE', [row['verdict'] for row in rows if row['kind'] == 'verdict'])
        self.assertEqual(rows[-1]['kind'], 'terminal')
        self.assertEqual((rows[-1]['status'], rows[-1]['invocation_cap']), ('DONE', 25))
        self.assertGreater(rows[-1]['invocations_used'], 0)
        live = [line for line in done.stdout.splitlines() if LIVE.match(line)]
        self.assertEqual(live, [rc.progress_line(row) for row in rows])
        self.assertEqual(done.stdout.splitlines()[-1], 'DONE (acceptance pending)')   # the final line is unchanged
        self.assertEqual(stat.S_IMODE((self.run_dir / 'progress.jsonl').stat().st_mode), 0o600)

    def test_no_prompt_or_output_text_reaches_the_jsonl_or_stdout(self):
        done = self.run_with_sentinels()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        evidence = ''.join(path.read_text() for path in (self.run_dir / 'evidence').glob('*.prompt.txt'))
        self.assertIn(PROMPT_SENTINEL, (self.run_dir / 'context' / 'workitem.md').read_text())   # file content the providers read
        self.assertIn(FAILURE_TEXT, evidence)                          # prompt text carrying the reviewer's failure_scenario
        for text in ((self.run_dir / 'progress.jsonl').read_text(), done.stdout):
            self.assertNotIn(FAILURE_TEXT, text)
        self.assertIn(OUTPUT_SENTINEL, ''.join(path.read_text() for path in (self.run_dir / 'evidence').glob('*.stdout.jsonl')))
        for text in ((self.run_dir / 'progress.jsonl').read_text(), done.stdout):
            self.assertNotIn(PROMPT_SENTINEL, text)
            self.assertNotIn(OUTPUT_SENTINEL, text)

    def test_an_author_hold_reason_is_withheld(self):
        held = self.run_coordinator(*FLAGS, env={'FAKE_AUTHOR_HOLD_AFTER_WRITE': '1', 'FAKE_AUTHOR_RATIONALE': OUTPUT_SENTINEL})
        terminal = self.rows()[-1]
        self.assertEqual((terminal['status'], terminal['reason']), ('HOLD', 'implementer: (author text withheld)'))
        self.assertTrue(held.stdout.splitlines()[-1].startswith('HOLD: implementer: '))   # the final line keeps its reason

    def test_quiet_progress_drops_the_event_lines_but_not_the_final_line_or_the_jsonl(self):
        loud = self.run_coordinator(*FLAGS)
        loud_final = [line for line in loud.stdout.splitlines() if not LIVE.match(line)]
        self.run_dir = self.root / 'quiet'
        quiet = self.run_coordinator(*FLAGS, '--quiet-progress')
        self.assertEqual(quiet.returncode, 0, quiet.stdout + quiet.stderr)
        self.assertFalse([line for line in quiet.stdout.splitlines() if LIVE.match(line)])
        self.assertEqual(quiet.stdout.splitlines(), loud_final)
        self.assertEqual(self.rows()[-1]['kind'], 'terminal')

    def test_a_progress_write_failure_changes_nothing_but_one_warning(self):
        reference = self.run_coordinator(*FLAGS)
        self.run_dir = self.root / 'broken'
        self.run_dir.mkdir()
        target = self.root / 'elsewhere.jsonl'
        target.write_text('untouched\n')
        (self.run_dir / 'progress.jsonl').symlink_to(target)          # O_NOFOLLOW: refused, never followed
        broken = self.run_coordinator(*FLAGS)
        self.assertEqual(broken.returncode, reference.returncode, broken.stdout + broken.stderr)
        self.assertEqual(broken.stdout.splitlines()[-1], reference.stdout.splitlines()[-1])
        self.assertEqual(broken.stdout.count('WARNING: progress log unavailable'), 1)
        self.assertEqual(target.read_text(), 'untouched\n')
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['status'], 'DONE')

    def test_a_failing_stdout_never_raises_out_of_progress(self):
        class Broken:
            def write(self, text): raise BrokenPipeError('closed')
            def flush(self): raise BrokenPipeError('closed')
        run_dir = self.root / 'closed'
        run_dir.mkdir()
        stub = types.SimpleNamespace(run_dir=run_dir, args=types.SimpleNamespace(quiet_progress=False), _progress_seq_n=None,
                                     _progress_label=None, _progress_warned=False)
        with patch('sys.stdout', Broken()):
            rc.Coordinator.progress(stub, 'phase')
            rc.Coordinator.progress(stub, 'phase')
        self.assertEqual(len((run_dir / 'progress.jsonl').read_text().splitlines()), 2)

    def test_status_brief_does_not_block_on_a_fifo_progress_file(self):
        run_dir = self.root / 'fifo'
        run_dir.mkdir()
        os.mkfifo(run_dir / 'progress.jsonl')
        with patch('builtins.print') as output:
            self.assertEqual(rc.status_brief(run_dir, 5), 0)
        output.assert_called_once_with('no progress events yet')

    def test_probe_turns_get_their_own_kinds_and_are_never_real_turns(self):
        command = self.command('--gate-vendor', 'codex', '--exercise-revisions')
        command[2] = 'permission-probe'
        probed = subprocess.run(command, cwd=self.root, env={**os.environ, 'FAKE_CODEX_SANDBOX_MODE': 'deny'}, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertIn(probed.returncode, (0, 2), probed.stdout + probed.stderr)
        kinds = {row['kind'] for row in self.rows()}
        self.assertEqual(kinds & {'probe:reviewer', 'probe:author-escape', 'probe:gate'}, {'probe:reviewer', 'probe:author-escape', 'probe:gate'})
        self.assertFalse(kinds & {'dispatch', 'phase', 'verdict'})
        self.assertFalse([row for row in self.rows() if row['kind'].startswith('probe') and 'label' in row])
        self.assertFalse([line for line in probed.stdout.splitlines() if LIVE.match(line) and 'running author' in line])
        self.assertTrue(probed.stdout.splitlines()[-1].startswith(('PASS', 'FAIL')))

    def test_dispatch_outcomes_name_rate_limits_and_probe_roles(self):
        co = self.coordinator()
        with patch.dict(os.environ, {'FAKE_RATE_LIMIT': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'rate_limited'):
                co.invoke('author', 'PLAN', 'Role: persistent. Phase: PLAN.', {})
        end = self.rows()[-1]
        self.assertEqual((end['kind'], end['step'], end['outcome']), ('dispatch', 'end', 'rate-limited'))
        co.progress('probe:reviewer', step='start')
        self.assertNotIn('label', self.rows()[-1])

    def test_an_rf5_conversion_is_named_in_the_verdict_event(self):
        co = self.coordinator()
        co.record_review_verdict(1, 'EXEC', 'APPROVE', 'REVISE', rf5=True)
        row = self.rows()[-1]
        self.assertTrue(row['rf5_converted'])
        self.assertIn('RF-5: APPROVE converted', rc.progress_line(row))

    def test_status_brief_prints_the_last_events_and_plain_status_is_unchanged(self):
        self.assertEqual(self.run_coordinator(*FLAGS, '--quiet-progress').returncode, 0)
        rows = self.rows()
        command = self.command(); command[2] = 'status'
        brief = subprocess.run([*command, '--brief', '3'], cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(brief.stdout.splitlines(), [rc.progress_line(row) for row in rows[-3:]])
        default = subprocess.run([*command, '--brief'], cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(len(default.stdout.splitlines()), min(20, len(rows)))
        plain = subprocess.run(command, cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(plain.stdout, (self.run_dir / 'state.json').read_text() + '\n')
        (self.run_dir / 'progress.jsonl').unlink()
        empty = subprocess.run([*command, '--brief'], cwd=self.root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(empty.stdout, 'no progress events yet\n')


class AllowedModelsStructureTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def args(self, allowed, gate_model='gpt-6-luna'):
        args = rc.parser().parse_args(['run', '--workspace', str(self.workspace), '--workitem', str(self.workitem),
                                       '--run-dir', str(self.run_dir), '--gate-model', gate_model])
        args.allowed_models = allowed
        return args

    def test_a_malformed_allowlist_is_a_configuration_refusal_not_a_crash(self):
        for allowed in (['claude-opus-5-5'], {'claude': None}, {'codex': 'gpt-6-luna'}, {'claude': [1]}):
            with self.subTest(allowed=allowed):
                with self.assertRaisesRegex(ValueError, 'allowed_models must be an object'):
                    rc.refuse_foreign_gate_model(self.args(allowed))
                with self.assertRaisesRegex(ValueError, 'allowed_models must be an object'):
                    rc.resolve_role_model_defaults(self.args(allowed))

    def test_a_well_formed_allowlist_is_unchanged(self):
        rc.refuse_foreign_gate_model(self.args({'codex': ['gpt-6-luna'], 'claude': ['claude-opus-5-5']}))
        with self.assertRaisesRegex(ValueError, 'belongs to claude'):
            rc.refuse_foreign_gate_model(self.args({'claude': ['claude-opus-5-5']}, 'claude-opus-5-5'))
