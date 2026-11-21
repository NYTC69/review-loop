"""WI-GATE1 S7/S8: PLAN structure, per-round coverage and the single gate audit."""
import json
import unittest

from paired_session import scenario_matrix
from paired_session import test_gate_once as tgo
from paired_session import test_rounds_threshold as threshold
from paired_session.fake_cli import FAKE_SCENARIO_MATRIX, SUPPLEMENTAL_SCENARIO


ROW = 'Boolean input | Raise TypeError | test_rejects_bool'


class ScenarioMatrixParserTests(unittest.TestCase):
    def error(self, plan):
        return scenario_matrix.validation_error(scenario_matrix.section(plan))

    def test_complete_bullet_matrix_and_case_insensitive_labels(self):
        plan = FAKE_SCENARIO_MATRIX.replace('### Input boundaries', '**INPUT boundaries:**').replace(
            '### Repetition and concurrency', '3. **Concurrency**')
        self.assertEqual(self.error('# Plan\n' + plan + '\n## Verification\nRun unittest.'), '')

    def test_na_on_the_label_line(self):
        plan = ['## Scenario matrix', *(f'- **{name}**: N/A: No dependency or input changes.'
                                        for name in scenario_matrix.DIMENSIONS)]
        self.assertEqual(self.error('\n'.join(plan)), '')

    def test_na_with_the_colon_inside_the_bold_label(self):
        plan = ['## Scenario matrix', *(f'- **{name}:** N/A: No dependency or input changes.'
                                        for name in scenario_matrix.DIMENSIONS)]
        self.assertEqual(self.error('\n'.join(plan)), '')
        plan[1] = f'- **Input boundaries:** {ROW}'
        self.assertEqual(self.error('\n'.join(plan)), '')

    def test_numbered_checklist_labels_with_inline_and_following_content(self):
        plan = ['## Scenario matrix', f'1. **Input boundaries** — {ROW}',
                '2. **Dependency failure** — N/A: No dependencies.',
                '3. **Repetition and concurrency**', '   N/A: No retries.',
                '4. **Ordering and state transitions**', '   N/A: No ordering changes.',
                '5. **Partial failure and recovery**', '   N/A: No partial writes.',
                '6. **Permissions and identity**', '   N/A: No access changes.',
                '7. **Compatibility**', '   N/A: No compatibility changes.']
        self.assertEqual(self.error('\n'.join(plan)), '')

    def test_markdown_table_with_header_alignment_and_escaped_pipe(self):
        plan = FAKE_SCENARIO_MATRIX.replace('- ' + ROW,
            '| Scenario | Expected behavior | Test ID |\n| :--- | ---: | --- |\n'
            r'| Boolean input \| invalid type | Raise TypeError | test_rejects_bool |')
        self.assertEqual(self.error(plan), '')

    def test_trivial_requires_a_reason_and_stands_alone(self):
        self.assertEqual(self.error('## Scenario matrix\nTrivial: documentation only.'), '')
        for body in ('Trivial:  ', 'Trivial: documentation only.\n### Input boundaries\n- ' + ROW):
            with self.subTest(body=body):
                self.assertIn('single Trivial: <reason>', self.error('## Scenario matrix\n' + body))

    def test_missing_section_and_dimension_are_named(self):
        self.assertIn('missing the ## Scenario matrix section', self.error('# Plan\nVerification only.'))
        self.assertIn('Compatibility', self.error(FAKE_SCENARIO_MATRIX.split('### Compatibility')[0]))

    def test_malformed_row_identifies_the_dimension_and_format(self):
        for row in ('- Boolean input | | test_rejects_bool',
                    '| Boolean input | Raise TypeError |',
                    '- Boolean input | Raise TypeError | test_rejects_bool | extra'):
            with self.subTest(row=row):
                error = self.error(FAKE_SCENARIO_MATRIX.replace('- ' + ROW, row))
                self.assertIn('Input boundaries, line 3', error)
                self.assertIn('scenario | expected behavior | test id', error)

    def test_na_requires_a_reason_and_stands_alone(self):
        self.assertEqual(self.error(FAKE_SCENARIO_MATRIX.replace('- ' + ROW, '- n/a: No inputs.')), '')
        for row in ('N/A: ', 'N/A: No inputs.\n- ' + ROW):
            with self.subTest(row=row):
                self.assertIn('N/A: <reason>', self.error(FAKE_SCENARIO_MATRIX.replace('- ' + ROW, row)))

    def test_table_headers_and_dividers_alone_do_not_cover_a_dimension(self):
        plan = FAKE_SCENARIO_MATRIX.replace('- ' + ROW,
            '| Scenario | Expected behavior | Test ID |\n| --- | --- | --- |')
        self.assertIn('Input boundaries', self.error(plan))


class ScenarioMatrixRunTests(unittest.TestCase):
    locals().update({name: getattr(threshold.ThresholdRunTests, name) for name in (*threshold.twl._HELPERS, 'args')})
    state, clean = tgo.GateOnceTests.state, tgo.GateOnceTests.clean

    def prompt(self, turn):
        return (self.run_dir / 'evidence' /
                f"{turn['sequence']:03d}-{turn['phase'].lower()}-{turn['role']}.prompt.txt").read_text()

    def test_plan_approval_is_refused_then_a_fixed_matrix_is_approved(self):
        for name, draft, message in (
                ('missing', '', 'missing the ## Scenario matrix section'),
                ('dimension', FAKE_SCENARIO_MATRIX.split('### Compatibility')[0], 'Compatibility')):
            with self.subTest(name=name):
                self.run_dir = self.root / name
                result = self.run_coordinator(*tgo.OFF, '--adversarial-gate', 'off', '--stop-after-plan',
                                              env={'FAKE_FIRST_PLAN_MATRIX': draft})
                self.assertIn(tgo.rc.PLAN_STOP_REASON, result.stdout, result.stdout + result.stderr)
                state = self.state()
                self.assertEqual([(r['reviewer_raw_verdict'], r['effective_verdict'])
                                  for r in state['review_verdicts']], [('APPROVE', 'REVISE'), ('APPROVE', 'APPROVE')])
                [row] = state['finding_ledger']
                self.assertEqual((row['source'], row['severity'], row['category'], row['status']),
                                 ('scenario-matrix', 'MAJOR', 'correctness', 'fixed'))
                self.assertIn(message, state['approve_refusals'][0]['reason'])
                first_author, first_review, repair, _ = state['turns']
                self.assertIn('Include the "## Scenario matrix" section', self.prompt(first_author))
                self.assertIn('silently skipped dimension is a blocking PLAN finding', self.prompt(first_review))
                self.assertIn(message, self.prompt(repair))
                self.assertIn(f'Fix the matrix in plan.md using {scenario_matrix.CHECKLIST}.', self.prompt(repair))
                self.assertEqual(state['phase'], 'EXEC')

    def test_matrix_rows_reach_every_exec_review_shadow_and_gate(self):
        result = self.run_coordinator('--lifecycle-mode', 'off', '--shadow', 'on',
                                      env={'FAKE_GATE_MATRIX_FINDING': 'gap'})
        self.assertIn(tgo.DONE, result.stdout, result.stdout + result.stderr)
        state = self.state()
        exec_turns = [turn for turn in state['turns'] if turn['phase'] == 'EXEC']
        reviews = [turn for turn in exec_turns if turn['role'] == 'reviewer']
        shadows = [turn for turn in exec_turns if turn['role'] == 'shadow']
        self.assertEqual((len(reviews), len(shadows)), (2, 2))
        for turn in reviews + shadows:
            prompt = self.prompt(turn)
            self.assertIn(ROW, prompt)
            self.assertIn('its named test exists and passes', prompt)
        [gate] = tgo.gates(state)
        self.assertIn(ROW, self.prompt(gate))
        self.assertEqual(gate['answer']['verdict'], 'FAIL')
        [item] = state['gate_fail_items']
        self.assertEqual((item['basis'], item['blocking_class'], item['smallest_change']),
                         ('blocking-class', 'correctness', 'add row + handling + test'))
        repair = next(turn for turn in exec_turns if turn['sequence'] > gate['sequence'] and turn['role'] == 'author')
        self.assertIn('add it alongside its test', self.prompt(repair))
        self.assertIn(SUPPLEMENTAL_SCENARIO, (self.workspace / 'test_matrix.py').read_text())
        self.assertIn('Smallest change: add row + handling + test', self.prompt(reviews[-1]))
        self.assertEqual(next(row['status'] for row in state['finding_ledger'] if row['id'] == item['id']), 'fixed')

    def test_exec_review_revises_for_a_row_without_passing_test_evidence(self):
        result = self.run_coordinator(*tgo.OFF, '--max-exec-rounds', '1', env={'FAKE_EXEC_MATRIX_TEST_MISSING': '1'})
        self.assertIn('HOLD: EXEC round limit reached', result.stdout, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'REVISE')
        self.assertIn('No passing evidence for test_rejects_bool', state['finding_ledger'][-1]['summary'])
        self.assertFalse(tgo.gates(state))

    def test_gate_exempts_a_legacy_plan_and_audits_plans_approved_with_the_matrix_requirement(self):
        for name, draft, evidence in (('legacy', FAKE_SCENARIO_MATRIX, ''),
                                      ('missing', FAKE_SCENARIO_MATRIX, 'has no scenario matrix'),
                                      ('trivial', '\n## Scenario matrix\nTrivial: documentation only.', 'claims Trivial')):
            with self.subTest(name=name):
                self.run_dir = self.root / name
                env = {'FAKE_FIRST_PLAN_MATRIX': draft, 'FAKE_GATE_MATRIX_FINDING': 'missing' if name == 'legacy' else name}
                stopped = self.run_coordinator(*tgo.OFF, '--stop-after-plan', env=env)
                self.assertIn(tgo.rc.PLAN_STOP_REASON, stopped.stdout, stopped.stdout + stopped.stderr)
                if name != 'trivial':
                    for path in (self.run_dir / 'plan.md', self.run_dir / 'context' / 'plan.md'):
                        path.write_text(path.read_text().split('## Scenario matrix')[0])
                if name == 'legacy':
                    # v3.8.x entered EXEC without a matrix or the 3.9 round rules marker.
                    state = self.state()
                    state.pop('round_rules')
                    (self.run_dir / 'state.json').write_text(json.dumps(state))
                result = self.run_operator_action('resume', *tgo.OFF, '--stop-after-plan', env=env)
                self.assertIn(tgo.DONE, result.stdout, result.stdout + result.stderr)
                [gate] = tgo.gates(self.state())
                if name == 'legacy':
                    self.assertIn('Skip the scenario matrix audit.', self.prompt(gate))
                    self.assertEqual(gate['answer']['verdict'], 'PASS')
                else:
                    self.assertEqual(gate['answer']['verdict'], 'FAIL')
                    self.assertIn(evidence, gate['answer']['fail_items'][0]['evidence'])
                self.clean()

    def test_review_only_skips_the_matrix_and_still_runs_the_gate(self):
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        result = self.run_coordinator(*tgo.OFF, '--review-only', env={'FAKE_GATE_MATRIX_FINDING': 'missing'})
        self.assertIn(tgo.DONE, result.stdout, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual(state['plan_rounds'], 0)
        [review] = [turn for turn in state['turns'] if turn['role'] == 'reviewer']
        self.assertNotIn('Scenario matrix review:', self.prompt(review))
        [gate] = tgo.gates(state)
        self.assertIn('The scenario matrix requirement and audit are skipped.', self.prompt(gate))
        self.assertEqual(gate['answer']['verdict'], 'PASS')
