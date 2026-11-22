"""WI-FASTPLAN delivery routing and first-draft recovery with fake providers."""
import json
import unittest
from unittest.mock import patch

from paired_session import test_gate_once as tgo
from paired_session import test_worktree_lifecycle as twl
from paired_session.fake_cli import FAKE_SCENARIO_MATRIX


WORKITEM = ('# Small delivery\n\n## Files to change\n- sum_ints.py\n\n'
            '## Acceptance criteria\n1. Sum integers and reject booleans.\n')


class FastPlanTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in twl._HELPERS})
    state = tgo.GateOnceTests.state
    clean = tgo.GateOnceTests.clean

    def test_fast_plan_enters_exec(self):
        self.workitem.write_text(WORKITEM)
        result = self.run_coordinator(*tgo.OFF, '--fast-plan', '--stop-after-plan')
        self.assertIn(tgo.rc.PLAN_STOP_REASON, result.stdout, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual((state['phase'], state['next']), ('EXEC', 'author'))
        self.assertEqual((state['plan_rounds'], state['plan_reviews']), (1, 0))
        self.assertFalse(state['review_verdicts'])
        self.assertNotIn('legacy_plan', state)
        self.assertTrue(state['config']['fast_plan'])
        self.assertEqual(state['fast_plan']['outcome'], 'skipped')
        [author] = state['turns']
        prompt = self.run_dir / 'evidence' / f"{author['sequence']:03d}-plan-author.prompt.txt"
        self.assertIn('Carry forward the declared files to change and acceptance criteria', prompt.read_text())
        status = self.run_operator_action('status')
        self.assertEqual(json.loads(status.stdout), state)
        self.assertEqual(status.stderr.splitlines(), ['Fast PLAN: skipped (small work item with a valid first draft)'])
        brief = self.run_operator_action('status', '--brief')
        self.assertNotIn('Fast PLAN:', brief.stdout + brief.stderr)

    def test_fast_plan_runs_exec_review_shadow_and_gate(self):
        self.workitem.write_text(WORKITEM.replace('- sum_ints.py', '- sum_ints.py\n2. test_sum_ints.py\n+ README.md'))
        result = self.run_coordinator('--lifecycle-mode', 'off', '--shadow', 'on', '--fast-plan')
        self.assertIn(tgo.DONE, result.stdout, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual([(turn['phase'], turn['role']) for turn in state['turns']],
                         [('PLAN', 'author'), ('EXEC', 'author'), ('EXEC', 'reviewer'),
                          ('EXEC', 'shadow'), ('EXEC', 'gate')])
        self.assertEqual((state['plan_rounds'], state['plan_reviews']), (1, 0))
        [gate] = tgo.gates(state)
        self.assertEqual(gate['answer']['verdict'], 'PASS')
        prompt = (self.run_dir / 'evidence' / f"{gate['sequence']:03d}-exec-gate.prompt.txt").read_text()
        self.assertIn('Plan (PLAN review skipped: small work item):', prompt)
        self.assertIn('Scenario matrix audit applies.', prompt)
        self.assertNotIn('legacy_plan', state)

    def test_fast_plan_eligibility_and_fallback(self):
        cases = (
            ('off', WORKITEM, FAKE_SCENARIO_MATRIX, ''),
            ('no-files-heading', WORKITEM.replace('## Files to change', '## Files'), FAKE_SCENARIO_MATRIX, 'Files to change'),
            ('empty-files', WORKITEM.replace('- sum_ints.py', '- '), FAKE_SCENARIO_MATRIX, 'Files to change'),
            ('too-many-files', WORKITEM.replace('- sum_ints.py', '- a\n- b\n- c\n- d'), FAKE_SCENARIO_MATRIX, 'Files to change'),
            ('glob', WORKITEM.replace('sum_ints.py', 'sum_[int].py'), FAKE_SCENARIO_MATRIX, 'Files to change'),
            ('no-criteria', WORKITEM.replace('1. Sum integers and reject booleans.', 'Sum integers.'), FAKE_SCENARIO_MATRIX, 'Acceptance criteria'),
            ('matrix', WORKITEM, '', 'missing the ## Scenario matrix section'),
            ('history', WORKITEM, FAKE_SCENARIO_MATRIX + '\nUse `~/.codex/sessions/`.\n', 'codex'),
        )
        for name, workitem, draft, reason in cases:
            with self.subTest(name=name):
                self.run_dir = self.root / name
                self.workitem.write_text(workitem)
                fast = ('--fast-plan',) if reason else ()
                result = self.run_coordinator(*tgo.OFF, *fast, '--stop-after-plan', env={'FAKE_FIRST_PLAN_MATRIX': draft})
                self.assertIn(tgo.rc.PLAN_STOP_REASON, result.stdout, result.stdout + result.stderr)
                state = self.state()
                self.assertEqual(state['plan_reviews'], 2 if name in ('matrix', 'history') else 1)
                if reason:
                    decision = state['fast_plan']
                    self.assertEqual(decision['outcome'], 'fallback')
                    self.assertIn(reason, decision['reason'])
                    status = self.run_operator_action('status')
                    self.assertEqual(json.loads(status.stdout), state)
                    self.assertIn(f"Fast PLAN: fallback ({decision['reason']})", status.stderr)
                else:
                    self.assertNotIn('fast_plan', state)
                    refused = self.run_coordinator(*tgo.OFF, '--fast-plan')
                    self.assertEqual(refused.returncode, 2)
                    self.assertIn('fast_plan (fixed when the run was created)', refused.stdout)

    def test_fast_plan_counts_only_top_level_files(self):
        nested = '  - sum_ints.py\n    - Keep helper *.py private\n    + Retain comments\n    1. Keep signatures'
        two_more = '\n  2. test_sum_ints.py\n     - Exercise booleans\n  + README.md'
        for name, files, outcome in (
                ('one', nested, 'skipped'),
                ('three', nested + two_more, 'skipped'),
                ('four', nested + two_more + '\n  * CHANGELOG.md', 'fallback'),
                ('glob', nested + '\n  - docs/*.md', 'fallback')):
            with self.subTest(name=name):
                self.run_dir = self.root / name
                self.workitem.write_text(WORKITEM.replace('- sum_ints.py', files))
                stopped = self.run_coordinator(*tgo.OFF, '--fast-plan', '--stop-after-plan')
                self.assertEqual(stopped.returncode, 2, stopped.stdout + stopped.stderr)
                state = self.state()
                self.assertEqual(state['fast_plan']['outcome'], outcome)
                self.assertEqual(state['plan_reviews'], int(outcome == 'fallback'))

    def test_stop_after_plan_message_covers_both_routes(self):
        self.workitem.write_text(WORKITEM)
        reason = 'PLAN complete, stopped by --stop-after-plan. Resume enters EXEC.'
        for name, flags in (('ordinary', ()), ('fast', ('--fast-plan',))):
            with self.subTest(route=name):
                self.run_dir = self.root / name
                stopped = self.run_coordinator(*tgo.OFF, *flags, '--stop-after-plan')
                self.assertEqual(stopped.returncode, 2, stopped.stdout + stopped.stderr)
                self.assertEqual(stopped.stdout.splitlines()[-1], 'HOLD: ' + reason)
                state = self.state()
                self.assertEqual((state['status'], state['phase'], state['next'], state['hold_reason']),
                                 ('HOLD', 'EXEC', 'author', reason))
                self.assertTrue(state['plan_stop_done'])
                self.assertEqual(state['plan_reviews'], int(name == 'ordinary'))
                event = json.loads((self.run_dir / 'progress.jsonl').read_text().splitlines()[-1])
                self.assertEqual((event['kind'], event['reason']), ('terminal', reason))
                self.assertIn(f'terminal · HOLD · {reason}', stopped.stdout)
                brief = self.run_operator_action('status', '--brief', '1')
                self.assertIn(f'terminal · HOLD · {reason}', brief.stdout)
                done = self.run_operator_action('resume', *tgo.OFF, '--stop-after-plan')
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn(tgo.DONE, done.stdout)
                after = self.state()
                self.assertEqual(after['turns'][:len(state['turns'])], state['turns'])
                self.assertEqual((after['turns'][len(state['turns'])]['phase'],
                                  after['turns'][len(state['turns'])]['role']), ('EXEC', 'author'))
                events = [json.loads(line) for line in (self.run_dir / 'progress.jsonl').read_text().splitlines()]
                self.assertEqual([row for row in events if row.get('reason') == reason], [event])
                self.clean()

    def test_fast_plan_checks_history_only_when_a_fresh_role_will_scan(self):
        self.workitem.write_text(WORKITEM)
        draft = FAKE_SCENARIO_MATRIX + '\nCodex approved these steps.\n'
        result = self.run_coordinator(*tgo.OFF, '--adversarial-gate', 'off', '--fast-plan', '--stop-after-plan',
                                      env={'FAKE_FIRST_PLAN_MATRIX': draft})
        self.assertIn(tgo.rc.PLAN_STOP_REASON, result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.state()['fast_plan']['outcome'], 'skipped')

    def test_fast_plan_resumes_saved_handoff(self):
        self.workitem.write_text(WORKITEM.replace('Files to change', 'FILES TO CHANGE'))
        args = tgo.rc.parser().parse_args(self.command(*tgo.OFF, '--fast-plan', '--stop-after-plan')[2:])
        co = tgo.rc.Coordinator(args)

        with patch.object(co, 'render', side_effect=RuntimeError('interrupted after saving the author receipt')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted after saving'):
                co.author_turn()
        saved = self.state()
        self.assertEqual((saved['plan_rounds'], saved['pending_author_result_sequence']), (0, 1))

        stopped = self.run_operator_action('resume', *tgo.OFF, '--stop-after-plan')
        self.assertIn(tgo.rc.PLAN_STOP_REASON, stopped.stdout, stopped.stdout + stopped.stderr)
        state = self.state()
        self.assertEqual((state['phase'], state['next'], state['plan_rounds'], state['plan_reviews']), ('EXEC', 'author', 1, 0))
        self.assertTrue(state['plan_stop_done'])
        self.assertIsNone(state.get('pending_author_result_sequence'))
        self.assertEqual(state['turns'], saved['turns'])
        plans = [self.run_dir / name for name in ('plan.md', 'plan-01.md', 'context/plan.md')]
        self.assertEqual([path.read_text() for path in plans], [saved['turns'][0]['answer']['body'].rstrip() + '\n'] * 3)

        done = self.run_operator_action('resume', *tgo.OFF, '--stop-after-plan')
        self.assertIn(tgo.DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertEqual((state['plan_rounds'], state['plan_reviews']), (1, 0))
        self.assertIsNone(state.get('pending_author_result_sequence'))
        self.assertEqual(state['fast_plan']['outcome'], 'skipped')

    def test_fast_plan_is_run_only_and_cli_only(self):
        refused = self.run_operator_action('resume', *tgo.OFF, '--fast-plan')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('--fast-plan is only for run', refused.stdout)
        profile = self.root / 'profile.json'
        profile.write_text('{"fast_plan": true}')
        refused = self.run_coordinator(*tgo.OFF, '--config', str(profile))
        self.assertEqual(refused.returncode, 2)
        self.assertIn('fast_plan', refused.stdout)

    def test_review_only_refuses_fast_plan(self):
        for flags in ((), ('--review-report',), ('--advisory-fix-round', 'true')):
            with self.subTest(flags=flags):
                result = self.run_coordinator(*tgo.OFF, '--review-only', '--fast-plan', *flags)
                self.assertEqual(result.returncode, 2)
                self.assertIn('--fast-plan does not apply', result.stdout)
                self.assertFalse((self.run_dir / 'state.json').exists())
