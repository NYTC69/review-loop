"""WI-HISTSCAN regressions using the offline coordinator harness."""
import json
import re
import shlex
import subprocess
import unittest

from paired_session import test_real_coordinator as trc
from paired_session.fake_cli import FAKE_SCENARIO_MATRIX
from paired_session.lifecycle_test_helpers import use_lifecycle_on

DECLARATION = 'history-scan-subject: claude codex gemini'
PLAN_PATH = '~/.claude/projects'
ADAPTER = 'src/token_meter/adapters/claude.py'
ADAPTER_TEXT = ('"""Claude session-log adapter."""\n' + '\n' * 90 +
                'class ClaudeAdapter:\n'
                '    vendor = "claude"\n\n'
                '    def projects(self):\n'
                '        return Path.home().joinpath(".claude", "projects")\n')


class HistoryScanSubjectTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        use_lifecycle_on(self, self.h)

    def run_adapter(self, *flags, declaration=''):
        self.h.workitem.write_text('# Toy\nCreate sum_ints and a Claude session-log adapter.\n' + declaration + '\n')
        flags = ('--lifecycle-mode', 'off', '--polish-round', 'off', *flags)
        env = {'FAKE_FIRST_PLAN_MATRIX': f'Read session logs in {PLAN_PATH}.\n' + FAKE_SCENARIO_MATRIX} if declaration else None
        planned = self.h.run_coordinator('--stop-after-plan', *flags, env=env)
        self.assertEqual(planned.returncode, 2, planned.stdout + planned.stderr)
        self.assertEqual(self.state()['phase'], 'EXEC')
        adapter = self.h.workspace / ADAPTER
        adapter.parent.mkdir(parents=True)
        adapter.write_text(ADAPTER_TEXT)
        if not declaration:
            # The field adapter was already present at the previous review.
            argv = self.h.command(*flags)[2:]
            argv[0] = 'resume'
            co = trc.rc.Coordinator(trc.rc.configure_parser(trc.rc.parser(), argv).parse_args(argv))
            co.capture_review_baseline()
            co.save()
        return self.h.run_operator_action('resume', *flags)

    def state(self):
        return json.loads((self.h.run_dir / 'state.json').read_text())

    def test_last_exec_shadow_holds_without_subject(self):
        result = self.run_adapter('--auto-split-rounds', 'false', '--max-exec-rounds', '1')
        state = self.state()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(state['status'], 'HOLD')
        self.assertEqual([row['answer']['status'] for row in state['turns']
                          if row['role'] == 'reviewer' and row['phase'] == 'EXEC'], ['APPROVE'])
        self.assertIn('shadow independence check rejected history in context/delta.patch', state['hold_reason'])
        self.assertEqual(re.findall(r'claude\.py:\d+', state['hold_reason']), ['claude.py:96'])
        self.assertFalse(any(row['role'] == 'shadow' for row in state['turns']))

    def test_declared_subject_reaches_gate(self):
        result = self.run_adapter('--max-exec-rounds', '2', declaration=DECLARATION)
        state = self.state()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(state['status'], 'DONE')
        self.assertFalse(any(row['source'] == 'independence-scan' for row in state['finding_ledger']))
        for role in ('shadow', 'gate'):
            with self.subTest(role=role):
                self.assertTrue(any(row['role'] == role for row in state['turns']))
                audit, = (self.h.run_dir / 'evidence').glob(f'*-{role}.independence-inputs.json')
                inputs = json.loads(audit.read_text())['inputs']
                self.assertIn(DECLARATION, inputs['context/workitem.md']['content'])
                self.assertIn(PLAN_PATH, inputs['context/plan.md']['content'])
                self.assertIn('joinpath(".claude", "projects")', inputs['context/delta.patch']['content'])
                if role == 'shadow':
                    self.assertIn('joinpath(".claude", "projects")',
                                  inputs['context/delta-since-last-review.patch']['content'])

    def coordinator(self, text, *flags):
        self.h.workitem.write_text(text)
        argv = self.h.command(*flags)[2:]
        return trc.rc.Coordinator(trc.rc.configure_parser(trc.rc.parser(), argv).parse_args(argv))

    def patch(self, *lines):
        return ('diff --git a/product.py b/product.py\n--- /dev/null\n+++ b/product.py\n'
                f'@@ -0,0 +1,{len(lines)} @@\n' + ''.join(f'+{line}\n' for line in lines))

    def test_subject_declaration_boundaries(self):
        co = self.coordinator('# Toy\nRead the session directories.\n'
                              'history-scan-subject:\n'
                              'history-scan-subject: claude /codex\n'
                              'history-scan-subject: claude unknown\n'
                              '- history-scan-subject: CODEX codex\n'
                              '1. history-scan-subject: GeMini\n')
        self.assertEqual(co._history_workitem()[1], {'codex', 'gemini'})
        # Undeclared claude, and a declared vendor's component that also names an undeclared tool.
        for path in ('~/.claude/projects', '/opt/codex-opus/sessions', '/opt/claude-501/state'):
            with self.subTest(path=path):
                self.assertTrue(co._introduced_history('context/delta.patch', self.patch(f'root = "{path}"')))
                self.assertTrue(co._introduced_history('context/plan.md', f'Use {path} for input files.'))
        # WI-SUBJDIR: any path component that names declared vendors only is subject matter, not just dot directories.
        for path in ('~/.codex-backup/sessions', '/opt/codex_other/sessions'):
            with self.subTest(path=path):
                self.assertEqual(co._introduced_history('context/delta.patch', self.patch(f'root = "{path}"')), [])
                self.assertEqual(co._introduced_history('context/plan.md', f'Use {path} for input files.'), [])

    def test_declared_paths_pass_plan_and_both_delta_views_with_or_without_gemini_role(self):
        workitem = ('# Toy\n' + DECLARATION + '\n\n## Files to change\n- product.py\n'
                    '\n## Acceptance criteria\n- Read session directories.\n')
        lines = ('Read ~/.claude/projects, ~/.codex/sessions, ~/.codex-paired*/sessions, '
                 '~/.codex-paired-meter/sessions and ~/.gemini.',
                 'Build paths with joinpath(".claude", "projects") and joinpath(".codex", "sessions").')
        for vendor in ('claude', 'gemini'):
            with self.subTest(vendor=vendor):
                original_dir = self.h.run_dir
                self.h.run_dir = original_dir.with_name(f'run-{vendor}')
                try:
                    co = self.coordinator(workitem, '--reviewer-vendor', vendor,
                                          '--reviewer-model', 'gemini-3.1-pro' if vendor == 'gemini' else 'claude-opus-5-5',
                                          '--gemini-bin', str(self.h.fake_gemini_cli()))
                finally:
                    self.h.run_dir = original_dir
                plan = '\n'.join(lines) + '\n' + FAKE_SCENARIO_MATRIX
                (co.context / 'plan.md').write_text(plan)
                self.assertIsNone(co._plan_history_issue())
                self.assertIsNone(co._fast_plan_issue(plan))
                for name in ('context/delta.patch', 'context/delta-since-last-review.patch'):
                    self.assertEqual(co._introduced_history(name, self.patch(*lines)), [])

    # WI-SUBJDIR field plan (token-meter WI-2 Codex log adapter): vendor-named path components that are not dot
    # directories held an approved plan.
    FIELD_PLAN_LINES = (
        '- Per-lane homes are `<repo>/.review-loop/codex-homes/<lane>/` (e.g. '
        '`~/3Cats/token-meter/.review-loop/codex-homes/wi-1e`, 20 files).',
        '- Default patterns: `~/.codex`, `~/.codex-paired*`, `~/paired-runs/codex_home_lanes`, '
        '`~/3Cats/*/.review-loop/codex-homes/*`, `~/Github/*/.review-loop/codex-homes/*`.',
        '- Tests (section 4), fixtures `tests/fixtures/codex/...` (small hand-written JSONL) and '
        '`tests/codex_lines.py` line builders.',
        '- CLAUDE.md "Dev environment": codex/claude-quota sources, `--home`/`--config`, default patterns.',
        '- `test_default_patterns_expand_under_home`: under a fake HOME with `.codex`, `.codex-paired-x`, '
        '`paired-runs/codex_home_lanes` and `3Cats/r/.review-loop/codex-homes/lane1`.',
    )

    def test_declared_vendor_path_components_pass_plan_and_both_delta_views(self):
        workitem = ('# WI-2: Codex adapter, multi-home discovery, quota readings\n\n' + DECLARATION + '\n\n'
                    'Codex session logs are `<home>/sessions/YYYY/MM/DD/rollout-*.jsonl`.\n')
        for vendor in ('claude', 'gemini'):
            with self.subTest(vendor=vendor):
                original_dir = self.h.run_dir
                self.h.run_dir = original_dir.with_name(f'run-{vendor}')
                try:
                    co = self.coordinator(workitem, '--reviewer-vendor', vendor,
                                          '--reviewer-model', 'gemini-3.1-pro' if vendor == 'gemini' else 'claude-opus-5-5',
                                          '--gemini-bin', str(self.h.fake_gemini_cli()))
                finally:
                    self.h.run_dir = original_dir
                plan = '\n'.join(self.FIELD_PLAN_LINES) + '\n' + FAKE_SCENARIO_MATRIX
                (co.context / 'plan.md').write_text(plan)
                self.assertEqual(co._introduced_history('context/plan.md', plan), [])
                self.assertIsNone(co._plan_history_issue())
                patch = self.patch('HOMES = ["~/3Cats/*/.review-loop/codex-homes/*"]',
                                   'FIXTURES = ROOT / "tests/fixtures/codex/sessions"', '# codex/claude-quota sources')
                for name in ('context/delta.patch', 'context/delta-since-last-review.patch'):
                    self.assertEqual(co._introduced_history(name, patch), [])
                self.assertEqual(co._delta_history_hits(patch), [])

    def test_declared_vendor_path_components_keep_attributions_and_undeclared_tools(self):
        co = self.coordinator('# Toy\nRead Codex session data.\nhistory-scan-subject: codex\n')
        self.assertEqual(co._history_workitem()[1], {'codex'})
        for text, marker in (
            ('Read tests/fixtures/codex/sessions; Codex approved it.', 'Codex'),
            ('tests/fixtures/codex approved the plan', 'codex'),
            ('see codex-homes/opus-cache/x', 'opus'),
            ('see codex-homes/claude-state/x', 'claude'),
            ('see /opt/codex-homes/F042/x', 'F042'),
        ):
            with self.subTest(text=text):
                self.assertIn(marker, co._introduced_history('context/plan.md', text))
                for name in ('context/delta.patch', 'context/delta-since-last-review.patch'):
                    self.assertIn(marker, co._introduced_history(name, self.patch(text)))

    def test_vendor_path_components_stay_markers_without_declaration(self):
        co = self.coordinator('# Toy\nRead session data.\n')
        for text in self.FIELD_PLAN_LINES[:3] + self.FIELD_PLAN_LINES[4:]:   # codex/claude-quota alone is slash prose
            with self.subTest(text=text):
                self.assertIn('codex', co._introduced_history('context/plan.md', text))

    def test_subject_keeps_history_markers(self):
        co = self.coordinator('# Toy\nRead Claude session data.\n' + DECLARATION)
        for text, marker in (
            ('~/.codex/F042/sessions', 'F042'),
            ('~/.codex/F042/claude_other/sessions', 'F042'),
            ('~/.codex-paired-F043/sessions', 'F043'),
            ('~/.codex-paired-prior-review/sessions', 'prior-review'),
            ('~/.codex/sessions APPROVE', 'APPROVE'),
            ('~/.codex/sessions prior review', 'prior review'),
            ('~/.codex/sessions Codex approved it', 'Codex'),
            ('~/.codex/sessions Codex.ai reviewed it', 'Codex'),
            ('~/.codex/sessions GPT/Codex signed off', 'Codex'),
            ('logs are in .codex approved by the reviewer', 'codex'),
        ):
            with self.subTest(text=text):
                patch = self.patch(text)
                self.assertIn(marker, co._introduced_history('context/plan.md', text))
                for name in ('context/delta.patch', 'context/delta-since-last-review.patch'):
                    self.assertIn(marker, co._introduced_history(name, patch))
                self.assertIn(('product.py:1', marker), co._delta_history_hits(patch))

    def test_subject_is_limited_to_plan_and_delta(self):
        co = self.coordinator('# Toy\n' + DECLARATION)
        # The declaration is the work item's only mention of Codex.
        self.assertEqual(co._introduced_history('context/delta.patch', self.patch('vendor = "codex"')), [])
        for name in ('prompt', 'gate-template', 'context/status.txt', 'context/delta.stat'):
            with self.subTest(name=name):
                self.assertIn('codex', co._introduced_history(name, 'Read ~/.codex/sessions.'))
        self.assertNotRegex(trc.rc.REVIEW_HISTORY_GUIDANCE, r'(?i)\b(?:claude|codex|gemini|opus|astra)\b|\.[a-z][\w-]*')

    def test_subject_is_frozen_across_resume_and_scope_change(self):
        result = self.run_adapter('--auto-split-rounds', 'false', '--max-exec-rounds', '1')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        with self.h.workitem.open('a') as handle:
            handle.write(DECLARATION + '\n')
        resumed = self.h.run_operator_action('resume', '--lifecycle-mode', 'off', '--polish-round', 'off',
                                             '--max-exec-rounds', '1')
        self.assertEqual(resumed.returncode, 2, resumed.stdout + resumed.stderr)
        self.assertIn('claude.py:96', self.state()['hold_reason'])
        self.assertNotIn(DECLARATION, (self.h.run_dir / 'context/workitem.md').read_text())

        self.h.workitem.write_text('# Toy\nCreate sum_ints and a Claude session-log adapter.\n')
        result = self.h.run_operator_action('note', '--lifecycle-mode', 'off', '--scope-change', '--text', DECLARATION)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        start = shlex.split(result.stdout.strip().removeprefix('Start: '))
        successor = subprocess.run([*start, '--stop-after-plan'], cwd=self.h.root, text=True, capture_output=True)
        self.assertEqual(successor.returncode, 2, successor.stdout + successor.stderr)
        successor_dir = self.h.run_dir.with_name(self.h.run_dir.name + '-successor')
        self.assertIn(DECLARATION, (successor_dir / 'context/workitem.md').read_text())
        depth_limit = subprocess.run([start[0], 'note', *start[2:], '--scope-change', '--text', 'More work.'],
                                     cwd=self.h.root, text=True, capture_output=True)
        self.assertEqual(depth_limit.returncode, 2, depth_limit.stdout + depth_limit.stderr)
        self.assertIn('already a scope-change successor', depth_limit.stdout)
        effective = self.h.run_dir / 'evidence/successor-workitem.md'
        effective.write_text(effective.read_text() + '\nMore work.\n')
        altered = subprocess.run([start[0], 'resume', *start[2:]], cwd=self.h.root, text=True, capture_output=True)
        self.assertEqual(altered.returncode, 2, altered.stdout + altered.stderr)
        self.assertIn('saved supersedes or effective task hash differs', altered.stdout)
