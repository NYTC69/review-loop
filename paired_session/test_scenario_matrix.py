"""WI-GATE1 S7/S8: PLAN structure, per-round coverage and the single gate audit."""
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from paired_session import scenario_matrix
from paired_session import test_gate_once as tgo
from paired_session import test_rounds_threshold as threshold
from paired_session.fake_cli import FAKE_SCENARIO_MATRIX, SUPPLEMENTAL_SCENARIO


ROW = 'Boolean input | Raise TypeError | test_rejects_bool'
TOOL_PATH_MATRIX = FAKE_SCENARIO_MATRIX.replace(ROW, ROW.replace('Boolean input', 'Boolean input in `/tmp/claude-501/frames/`'))


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
        plan = ['## Scenario matrix', f'1. Input boundaries — {ROW}',
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

    def test_trivial_requires_a_reason_on_the_first_content_line(self):
        self.assertEqual(self.error('## Scenario matrix\nTrivial: documentation only.'), '')
        self.assertEqual(self.error('## Scenario matrix\nTrivial: documentation only.\n\nNotes for the reviewer.'), '')
        self.assertIn('Trivial: <reason>', self.error('## Scenario matrix\nTrivial:  '))
        self.assertIn('Input boundaries', self.error('## Scenario matrix\nNotes first.\nTrivial: documentation only.'))

    def test_missing_section_and_dimension_are_named(self):
        self.assertIn('missing the ## Scenario matrix section', self.error('# Plan\nVerification only.'))
        self.assertIn('Compatibility', self.error(FAKE_SCENARIO_MATRIX.split('### Compatibility')[0]))
        error = self.error('## Scenario matrix\n\n## Verification\nRun unittest.')
        self.assertTrue(all(name in error for name in scenario_matrix.DIMENSIONS), error)

    def test_malformed_rows_are_ignored_and_do_not_cover_a_dimension(self):
        for row in ('- Boolean input | | test_rejects_bool',
                    '| Boolean input | Raise TypeError |',
                    '- Boolean input | BACKLOG: deferred',
                    '- Boolean input | Raise TypeError | test_rejects_bool | extra'):
            with self.subTest(row=row):
                self.assertIn('Input boundaries', self.error(FAKE_SCENARIO_MATRIX.replace('- ' + ROW, row)))
                self.assertEqual(self.error(FAKE_SCENARIO_MATRIX.replace('- ' + ROW, row + '\n- ' + ROW)), '')

    def test_na_requires_a_reason_and_can_accompany_rows(self):
        self.assertEqual(self.error(FAKE_SCENARIO_MATRIX.replace('- ' + ROW, '- n/a: No inputs.')), '')
        self.assertEqual(self.error(FAKE_SCENARIO_MATRIX.replace('- ' + ROW, 'N/A: No inputs.\n- ' + ROW)), '')
        self.assertEqual(self.error(FAKE_SCENARIO_MATRIX.replace('- ' + ROW, 'x | N/A: No inputs. | none')), '')
        for row in ('N/A: ', 'x | N/A: | none', 'N/A: | No inputs. | none'):
            with self.subTest(row=row):
                self.assertIn('N/A: <reason> for Input boundaries',
                              self.error(FAKE_SCENARIO_MATRIX.replace('- ' + ROW, row)))

    def test_repeated_label_and_prose_preserve_dimension_coverage(self):
        plan = FAKE_SCENARIO_MATRIX.replace('### Input boundaries\n- ' + ROW,
            f'### Input boundaries\n- {ROW}\n### Input boundaries\n- BACKLOG note')
        plan = plan.replace('### Dependency failure\n', '### Dependency failure\n'
                            'This long prose line mentions input without introducing a new dimension.\n')
        self.assertEqual(self.error(plan), '')

    def test_table_headers_and_dividers_alone_do_not_cover_a_dimension(self):
        plan = FAKE_SCENARIO_MATRIX.replace('- ' + ROW,
            '| Scenario | Expected behavior | Test ID |\n| --- | --- | --- |')
        self.assertIn('Input boundaries', self.error(plan))

    def test_prose_does_not_switch_dimension(self):
        for prose in ('Dependency note: none', '- Dependency note: none'):
            with self.subTest(prose=prose):
                plan = FAKE_SCENARIO_MATRIX.replace('- ' + ROW, prose + '\n- ' + ROW)
                self.assertEqual(self.error(plan), '')
                without_dependency = plan.replace('### Dependency failure\nN/A: The helper has no dependencies.\n', '')
                self.assertEqual(self.error(without_dependency), 'missing rows or N/A: <reason> for Dependency failure')

    def test_plain_labels_spell_and_as_ampersand_or_slash_and_may_carry_a_note(self):
        # Opus R1 note 1: these unmarked spellings validated before WI-FASTPLAN and must keep validating.
        for plain, name in (('Repetition & concurrency:', 'Repetition and concurrency'),
                            ('Repetition/concurrency:', 'Repetition and concurrency'),
                            ('Ordering & state transitions:', 'Ordering and state transitions'),
                            ('Partial failure / recovery:', 'Partial failure and recovery'),
                            ('Input boundaries (blocking):', 'Input boundaries')):
            with self.subTest(label=plain):
                self.assertEqual(scenario_matrix._label_and_content(plain)[0], name)
        for prose in ('Dependency note: none', 'Input note (blocking): none'):
            with self.subTest(prose=prose):
                self.assertEqual(scenario_matrix._label_and_content(prose)[0], '')

    def test_real_run_plan_matrices(self):
        # Preserve the matrices and notes from actual runs.
        plans = (
            ('realrun-resume/run2/context/plan.md', """## Scenario matrix
Format: label line per dimension; rows are `scenario | expected behavior | test id`. Only blocking-class rows (correctness, data loss) carry a test id. Non-blocking items are listed under BACKLOG below the matrix.

Input boundaries:
- empty list raises ValueError | raises ValueError, no result returned | test_median_empty_raises
- single value returns that value | returns the element unchanged | test_median_single_value
- even count, int pair, even sum, values above 2**53 | exact int `10**20 + 1` | test_median_large_int_exact
- even count, int pair, even sum, small values | result type int | test_median_int_result_type
- even count, int pair, odd sum | float 1.5 (exactness not required) | test_median_odd_int_sum_returns_float

Dependency failure: N/A: pure function with no external calls, I/O, or timeouts.

Repetition and concurrency: N/A: stateless function with no shared state; repeated calls are covered by the non-mutation row below.

Ordering and state transitions:
- unsorted input gives the same result as sorted input | returns the median of the sorted order | test_median_unsorted_input
- caller's list is not reordered or mutated | list equals its pre-call contents | test_median_does_not_modify_input

Partial failure and recovery: N/A: no side effects or multi-step writes; an exception leaves no partial state.

Permissions and identity: N/A: no access control, identity, or ownership involved.

Compatibility:
- existing `mean` behavior unchanged | mean([1, 2, 3]) == 2 | test_mean
- module gains one public name; `from stats import mean` still works | import succeeds, existing test passes | test_mean
"""),
            ('realrun2-plan-01.md', """## Scenario matrix
1. **Input boundaries**
   - Empty list raises ValueError | `ValueError` raised, no other output | test_median_empty_raises (blocking: acceptance criterion 2)
   - Single element | returns that element with its type | test_median_single_value (blocking: correctness)
   - Large ints above 2**53 with even sum | exact int result, no float rounding | test_median_large_int_exact (blocking: correctness, criterion 4)
   - Odd-sum ints (e.g. `[1, 2]`) | float 1.5 | test_median_even_count_float_result (blocking: correctness)
   - Bool values, non-numeric values (strings), NaN | BACKLOG: behavior is whatever Python comparison/arithmetic gives; no spec requirement. Not blocking.
2. **Dependency failure**: N/A: pure in-memory function with no external calls, timeouts, or services.
3. **Repetition and concurrency**: N/A: stateless function; no shared state, retries, or writes. Duplicate values are ordinary inputs covered by the value-based tests.
4. **Ordering and state transitions**
   - Unsorted input | correct middle value regardless of input order | test_median_unsorted_input (blocking: correctness)
   - Caller list mutated by call | caller's list unchanged after call | test_median_does_not_mutate_input (blocking: data integrity, criterion 3)
   - Invalid state transitions / stale state: N/A: no state machine or cached data.
5. **Partial failure and recovery**: N/A: single atomic computation with no multi-step writes, cleanup, or resume.
6. **Permissions and identity**: N/A: no access control, identity, or ownership involved.
7. **Compatibility**
   - Existing `mean` behavior and `test_mean` | unchanged, still passes | existing test_mean in `python3 -m unittest -q` (blocking: regression)
   - Int result type for even-sum int inputs | `type(...) is int` | test_median_int_result_type_even_sum (blocking: correctness, criterion 4)
   - Negative even-sum ints | exact int result | test_median_negative_int_even_sum (blocking: correctness)
   - Float/mixed inputs | float mean of the two middle values | test_median_even_count and test_median_even_count_float_result cover the float path; the float-only case `median([1.5, 2.5, 0.5]) == 1.5` is in test_median_odd_count (blocking: correctness)

Matrix notes for reviewer:
- Blocking-class rows above all have a named test.
- BACKLOG (non-blocking): bool/str/NaN inputs; float overflow for `(a + b) / 2` near `sys.float_info.max` (could be rewritten as `a / 2 + b / 2`; not required by criteria); precision loss when an odd-sum int pair is divided into a float for values above 2**53 (criteria only require exact int when the sum is even); accepting non-list iterables such as generators (`sorted` would consume them; `len` would fail first on generators, so behavior is a TypeError, acceptable for "keep it small").
- Trivial exemption: not claimed; this change has logic and tests.

## Risks and unresolved items
"""),
            ('realrun2-plan-03.md', """## Scenario matrix
Format: each dimension has a label line; rows are `scenario | expected behavior | test id`. A dimension with no applicable scenario has an `N/A: <reason>` line directly under its label.

**1. Input boundaries**
Empty list raises ValueError | `ValueError` raised | test_median_empty_raises
Single element | returns that element with its type | test_median_single_value
Even-sum ints above 2**53 | exact int, no float rounding | test_median_large_int_exact
Odd-sum int pair `[1, 2]` | float `1.5` | test_median_even_count_float_result
Bool, string, or NaN values | BACKLOG: follows Python comparison/arithmetic; no spec requirement; non-blocking | none
Near-float-max middle pair | BACKLOG: `a / 2 + b / 2` avoids overflow by design; no dedicated test required | none

**2. Dependency failure**
N/A: pure in-memory function with no external calls, timeouts, or services.

**3. Repetition and concurrency**
N/A: stateless function with no shared state, retries, or writes; duplicate values are ordinary inputs covered by the value tests.

**4. Ordering and state transitions**
Unsorted input | correct middle value regardless of order | test_median_unsorted_input
Caller's list mutated by call | caller's list unchanged | test_median_does_not_mutate_input
Invalid transitions and stale state | N/A: no state machine or cached data; nothing to transition | none

**5. Partial failure and recovery**
N/A: single atomic computation with no multi-step writes, cleanup, or resume.

**6. Permissions and identity**
N/A: no access control, identity, or ownership is involved.

**7. Compatibility**
Existing `mean` and `test_mean` | unchanged; still passes | existing test_mean (run via `python3 -m unittest -q`)
Even-sum int inputs | `type(...) is int` | test_median_int_result_type_even_sum
Negative even-sum ints | exact int | test_median_negative_int_even_sum
Float and mixed inputs | float mean of middle pair | test_median_even_count, test_median_odd_count (float-only `[1.5, 2.5, 0.5]`)

Matrix notes:
- Every blocking-class row above has a named test.
- BACKLOG (non-blocking): bool/string/NaN inputs; precision loss when an odd-sum int pair becomes a float above 2**53 (criteria require exactness only for even sums); non-list iterables (generators raise TypeError from `len`, acceptable for a small change).
- No trivial exemption is claimed; the change has logic and tests.

## Risks and unresolved items
"""),
        )
        for name, plan in plans:
            with self.subTest(plan=name):
                self.assertEqual(self.error(plan), '')


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

    def test_gate_matrix_excerpt_survives_malformed_verdict_retry(self):
        counter = self.root / 'gate-replies.count'
        result = self.run_coordinator(*tgo.OFF, env={'FAKE_FIRST_PLAN_MATRIX': TOOL_PATH_MATRIX,
            'FAKE_GATE_REPLIES': 'empty,ok', 'FAKE_GATE_REPLIES_COUNT': str(counter)})
        self.assertIn(tgo.DONE, result.stdout, result.stdout + result.stderr)
        self.assertEqual(counter.read_text(), '2')
        first, retry = tgo.gates(self.state())
        self.assertEqual(first['gate_malformed'], 'FAIL with no fail_items')
        self.assertEqual(retry['answer']['verdict'], 'PASS')
        excerpt = '\n\nScenario matrix from plan.md:\n' + scenario_matrix.section(TOOL_PATH_MATRIX) + '\n'
        retry_note = ('\n\nNote from the coordinator: your previous answer was not a valid verdict ('
                      'FAIL with no fail_items). Review again and answer with a complete verdict.')
        first_prompt = self.prompt(first)
        self.assertTrue(first_prompt.endswith(excerpt))
        for turn, expected in ((first, first_prompt), (retry, first_prompt + retry_note)):
            sequence = turn['sequence']
            evidence = self.run_dir / 'evidence'
            dispatched = (evidence / f'{sequence:03d}-exec-gate.prompt.txt').read_bytes()
            self.assertEqual(dispatched, expected.encode('utf-8'))
            scan = json.loads((evidence / f'{sequence:03d}-gate.independence-inputs.json').read_text())
            self.assertEqual(scan['inputs']['prompt'], {
                'content': expected, 'sha256': hashlib.sha256(dispatched).hexdigest()})

    def test_gate_scans_history_after_matrix_excerpt(self):
        co = self.coordinator(*tgo.OFF)
        co.state['phase'] = 'EXEC'
        (co.context / 'plan.md').write_text('# Plan\n' + TOOL_PATH_MATRIX)
        counter = self.root / 'gate-replies.count'
        with patch.dict(os.environ, {'FAKE_GATE_REPLIES': 'empty,ok', 'FAKE_GATE_REPLIES_COUNT': str(counter)}), \
                patch.object(tgo.rc, 'gate_answer_error', return_value='prior notes in /tmp/codex-reviews/'), \
                self.assertRaisesRegex(RuntimeError, 'gate independence check rejected history in prompt: codex'):
            co._gate_answer()
        self.assertEqual(counter.read_text(), '1')

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
