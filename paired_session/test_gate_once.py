"""WI-GATE1 (owner 2026-11-19): the adversarial gate runs once per work item.

The gate has no history and checks the whole change against the acceptance criteria: PASS, or FAIL with items that each
carry a basis (an unmet criterion or a blocking class), evidence, the smallest change and the proof. A FAIL goes to the
author; every later EXEC reviewer round carries each open FAIL item in full as a mandatory check that closes only as
fixed. No route runs the gate again: not a reject, a note, an extension, a lifecycle change (FINISH, POLISH-Q fix, DOCS,
SECURITY, a writer replay) or a resume of a state an earlier version saved with gate_ran reset. A malformed answer is not
a verdict: the gate turn is dispatched once more, then the run HOLDs. Owner Q3: a FAIL at the EXEC round cap gets one
fix + review round beyond it; owner Q4: every operator rejection note reaches every later EXEC review round."""
import json
import os
from unittest import mock
import unittest

from paired_session import test_worktree_lifecycle as twl
from paired_session import test_d09_c1b2 as d09

rc, DONE = twl.rc, twl.DONE
ON = ('--lifecycle-mode', 'on', '--max-invocations', '80')
OFF = ('--lifecycle-mode', 'off', '--shadow', 'off')
FAIL = {'FAKE_GATE_BLOCK': '1'}   # the gate fails every time it is asked: one gate turn proves it was asked once
ITEM = {'basis': 'blocking-class', 'criterion': '', 'blocking_class': 'correctness', 'evidence': 'sum_ints.py:1 accepts bool',
        'smallest_change': 'reject bool', 'proof': 'test_sum_ints rejects True'}


def gates(state):
    return [turn for turn in state['turns'] if turn.get('role') == 'gate']


class GateContractTests(unittest.TestCase):
    """The schema check alone (no run)."""

    def answer(self, verdict='FAIL', items=None, met=True):
        return {'verdict': verdict, 'criteria': [{'criterion': 'rejects bool', 'met': met, 'evidence': 'x.py:1'}],
                'fail_items': [dict(ITEM)] if items is None else items, 'verified_claims': [], 'self_run_evidence': []}

    def test_valid_verdicts(self):
        self.assertEqual(rc.gate_answer_error(self.answer()), '')
        self.assertEqual(rc.gate_answer_error(self.answer('PASS', [])), '')
        self.assertEqual(rc.gate_answer_error(self.answer(items=[{**ITEM, 'basis': 'criterion', 'criterion': 'rejects bool',
                                                                   'blocking_class': ''}])), '')

    def test_each_missing_field_is_malformed(self):
        cases = {'evidence': '', 'smallest_change': '', 'proof': ' '}
        for field, value in cases.items():
            with self.subTest(field=field):
                self.assertIn(field, rc.gate_answer_error(self.answer(items=[{**ITEM, field: value}])))
        self.assertIn('no quoted criterion', rc.gate_answer_error(self.answer(items=[{**ITEM, 'basis': 'criterion'}])))
        self.assertIn('blocking_class', rc.gate_answer_error(self.answer(items=[{**ITEM, 'blocking_class': ''}])))
        self.assertIn('basis', rc.gate_answer_error(self.answer(items=[{**ITEM, 'basis': 'style'}])))
        self.assertEqual(rc.gate_answer_error(self.answer(items=[])), 'FAIL with no fail_items')

    def test_pass_with_an_unmet_criterion_or_items_is_malformed(self):
        self.assertIn('PASS with an unmet criterion', rc.gate_answer_error(self.answer('PASS', [], met=False)))
        self.assertEqual(rc.gate_answer_error(self.answer('PASS')), 'PASS with fail_items')
        self.assertIn('verdict', rc.gate_answer_error({**self.answer(), 'verdict': 'needs-attention'}))

    def test_the_schema_requires_every_field(self):
        schema = rc.gate_schema()
        self.assertEqual(schema['properties']['verdict']['enum'], ['PASS', 'FAIL'])
        item = schema['properties']['fail_items']['items']
        self.assertEqual(sorted(item['required']), sorted(item['properties']))
        self.assertNotIn('findings', schema['properties'])


class GateOnceTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'args')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def prompts(self, log):
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def assert_one_gate_then_done(self, result, state=None):
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
        state = state or self.state()
        self.assertEqual(len(gates(state)), 1, [(t['role'], t['phase']) for t in state['turns']])
        self.assertTrue(state['gate_ran'])
        self.assertNotIn('force_gate_after_reject', state)
        return state

    def clean(self):
        rc.subprocess.run(['git', 'checkout', '-q', '--', '.'], cwd=self.workspace, check=True)
        rc.subprocess.run(['git', 'clean', '-qfd'], cwd=self.workspace, check=True)

    # --- the main path ----------------------------------------------------------------------------------------------------
    def test_a_gate_fail_goes_to_the_author_and_every_later_review_carries_its_items_then_done(self):
        for mode in (OFF, ON):
            with self.subTest(mode=mode[1]):
                self.run_dir = self.root / ('main-' + mode[1])
                log = self.root / f'prompts-{mode[1]}.jsonl'
                result = self.run_coordinator(*mode, env={**FAIL, 'FAKE_REVIEW_PROMPT_LOG': str(log), 'FAKE_FINISH_WRITE': '1'})
                state = self.assert_one_gate_then_done(result)
                [gate] = gates(state)
                [item] = state['gate_fail_items']
                self.assertEqual({key: item[key] for key in ITEM}, {**ITEM, 'evidence': 'sum_ints.py:1 accepts bool input'})
                row = next(r for r in state['finding_ledger'] if r['id'] == item['id'])
                self.assertEqual((row['source'], row['severity'], row['status']), ('adversarial-gate', 'HIGH', 'fixed'))
                self.assertEqual([r['id'] for r in state['finding_ledger'] if r['source'] == 'adversarial-gate'], [item['id']])
                author = next(t for t in state['turns'] if t['sequence'] > gate['sequence'])
                self.assertEqual((author['role'], author['phase']), ('author', 'EXEC'))
                delivered = (self.run_dir / 'evidence' / f"{author['sequence']:03d}-exec-author.prompt.txt").read_text()
                self.assertIn('Smallest change: reject bool', delivered)
                later = self.prompts(log)[1:]   # the first EXEC review ran before the gate
                self.assertTrue(later)
                first = later[0]
                for text in ('## Mandatory gate checks', f"{item['id']} (gate FAIL item)", 'Basis: blocking class: correctness',
                             'Evidence: sum_ints.py:1 accepts bool input', 'Smallest change: reject bool',
                             'Proof it is fixed: test_sum_ints rejects True'):
                    self.assertIn(text, first)
                if mode is ON:   # FINISH wrote: the re-check goes to the reviewer only; the convergence cites the one gate
                    self.assertEqual(state['lifecycle']['exec_convergence']['gate_sequence'], gate['sequence'])
                    self.assertGreaterEqual(len(later), 2)
                self.clean()

    def test_a_withdrawn_gate_item_stays_open_until_fixed(self):
        log = self.root / 'prompts.jsonl'
        result = self.run_coordinator(*OFF, env={**FAIL, 'FAKE_EXEC_WITHDRAW_GATE_ONCE': str(self.root / 'withdrawn'),
                                                 'FAKE_REVIEW_PROMPT_LOG': str(log)})
        state = self.assert_one_gate_then_done(result)
        [item] = state['gate_fail_items']
        row = next(r for r in state['finding_ledger'] if r['id'] == item['id'])
        self.assertEqual([h['status'] for h in row['status_history']], ['open', 'still_open', 'fixed'])
        self.assertTrue(row['status_history'][1]['evidence'].startswith('withdrawal refused'))
        self.assertTrue(state['approve_refusals'])   # RF-5: the APPROVE with the item still open was a REVISE
        self.assertEqual(sum('## Mandatory gate checks' in prompt for prompt in self.prompts(log)), 2)

    # --- owner Q3: one extra round after a FAIL at the cap ------------------------------------------------------------------
    def test_a_gate_fail_at_the_round_cap_gets_one_extra_fix_round(self):
        result = self.run_coordinator(*OFF, '--max-exec-rounds', '1', env=FAIL)
        state = self.assert_one_gate_then_done(result)
        self.assertEqual(state['exec_rounds'], 2)
        self.assertEqual(state['gate_extra_round']['at_round'], 1)

    def test_a_revise_in_the_extra_round_holds_and_add_rounds_continues_without_a_gate(self):
        keep = {**FAIL, 'FAKE_EXEC_STILL_OPEN': '[gate FAIL]'}
        held = self.run_coordinator(*OFF, '--max-exec-rounds', '1', env=keep)
        self.assertIn('HOLD: EXEC round limit reached after adversarial gate', held.stdout, held.stdout + held.stderr)
        state = self.state()
        self.assertEqual((len(gates(state)), state['exec_rounds'], state['turns'][-1]['role']), (1, 2, 'reviewer'))
        resumed = self.run_operator_action('resume', *OFF, '--max-exec-rounds', '1', '--add-rounds', '1', env=FAIL)
        self.assert_one_gate_then_done(resumed)

    def test_the_extra_round_policy_is_one_switch(self):
        with mock.patch.object(rc.Coordinator, 'gate_fail_extra_round', return_value=False), mock.patch.dict(os.environ, FAIL):
            co = rc.Coordinator(rc.parser().parse_args(self.command(*OFF, '--max-exec-rounds', '1', '--skip-probe')[2:]))
            self.assertEqual(co.drive(), 'HOLD')
        self.assertEqual(co.state['hold_reason'], 'EXEC round limit reached after adversarial gate')
        self.assertEqual((len(gates(co.state)), co.state['next']), (1, 'gate'))   # add-rounds hands the FAIL to the author

    # --- operator actions ----------------------------------------------------------------------------------------------------
    def test_every_rejection_note_reaches_every_later_review_and_no_gate_reruns(self):
        log = self.root / 'prompts.jsonl'
        env = {**FAIL, 'FAKE_REVIEW_PROMPT_LOG': str(log), 'FAKE_AUTHOR_REJECTION_ID': '1'}
        self.assert_one_gate_then_done(self.run_coordinator(*OFF, env=env))
        for number, text in ((1, 'Recheck integer inputs.'), (2, 'Keep the error message short.')):
            before = len(self.prompts(log))
            rejected = self.run_operator_action('reject', *OFF, '--text', text, env=env)   # reject drives the run on
            self.assert_one_gate_then_done(rejected)
            after = self.prompts(log)[before:]
            self.assertTrue(after)
            for prompt in after:
                self.assertIn('## Operator rejection notes', prompt)
                self.assertIn('[R001]\nRecheck integer inputs.', prompt)   # the first note, also after the second reject
                if number == 2:
                    self.assertIn('[R002]\nKeep the error message short.', prompt)

    def test_an_operator_note_on_the_repair_turn_does_not_regate(self):
        for mode in (OFF, ON):   # the repair turn (it carries the gate FAIL) is rate limited once: a HOLD that takes a note
            with self.subTest(mode=mode[1]):
                self.run_dir = self.root / ('note-' + mode[1])
                limited = {**FAIL, 'FAKE_RATE_LIMIT': '1', 'FAKE_RATE_LIMIT_MATCH': '"source": "adversarial-gate"',
                           'FAKE_RATE_LIMIT_ONCE': str(self.run_dir) + '.limited'}
                held = self.run_coordinator(*mode, env=limited)
                self.assertIn('HOLD:', held.stdout, held.stdout + held.stderr)
                self.assertEqual(self.state()['next'], 'author')
                noted = self.run_operator_action('note', *mode, '--text', 'Keep the TypeError message.', env=FAIL)
                self.assertEqual(noted.returncode, 0, noted.stdout + noted.stderr)
                state = self.assert_one_gate_then_done(self.run_operator_action('resume', *mode, env=FAIL))
                self.assertEqual(state['operator_notes'][0]['status'], 'delivered')
                self.clean()

    def test_a_state_saved_with_gate_ran_reset_after_a_fail_is_not_gated_again(self):
        held = self.run_coordinator(*OFF, '--max-invocations', '5', env=FAIL)
        self.assertIn('HOLD: invocation limit reached', held.stdout, held.stdout + held.stderr)
        state = self.state()   # what v3.8.6 saved after a gate FAIL: gate_ran reset, the gate forced, no FAIL items
        state.update(gate_ran=False, force_gate_after_reject=True)
        state.pop('gate_fail_items')
        (self.run_dir / 'state.json').write_text(json.dumps(state))
        log = self.root / 'prompts.jsonl'
        resumed = self.run_operator_action('resume', *OFF, '--max-invocations', '80', env={**FAIL, 'FAKE_REVIEW_PROMPT_LOG': str(log)})
        state = self.assert_one_gate_then_done(resumed)
        [item] = state['gate_fail_items']
        self.assertEqual(item['basis'], 'legacy')
        self.assertIn(f"{item['id']} (gate FAIL item)", self.prompts(log)[0])

    def test_a_legacy_regate_queued_after_the_reviewer_approved_is_settled_without_a_gate(self):
        self.assert_one_gate_then_done(self.run_coordinator(*OFF, '--max-invocations', '80', env=FAIL))
        co = rc.Coordinator(self.args('--lifecycle-mode', 'off', '--shadow', 'off', action='resume'))
        co.state.update(status='ACTIVE', next='gate', gate_ran=False, phase='EXEC')   # an earlier version queued a re-gate
        with mock.patch.object(co, 'invoke', side_effect=AssertionError('a second gate')), \
                mock.patch.object(co, 'start_polish_or_done', return_value='DONE') as done:
            co.gate_turn()
        done.assert_called_once()
        self.assertTrue(co.state['gate_ran'])

    # --- malformed answers ---------------------------------------------------------------------------------------------------
    def test_a_malformed_answer_is_dispatched_once_more_then_holds(self):
        for field in ('evidence', 'smallest_change', 'proof', 'criterion', 'blocking_class', 'none', 'unmet'):
            with self.subTest(field=field):
                self.run_dir = self.root / ('malformed-' + field)
                held = self.run_coordinator(*OFF, env={'FAKE_GATE_MALFORMED': field})
                self.assertIn('HOLD: malformed gate verdict', held.stdout, held.stdout + held.stderr)
                state = self.state()
                self.assertEqual([bool(turn.get('gate_malformed')) for turn in gates(state)], [True, True])
                self.assertFalse(state['gate_ran'])
                self.assertNotIn('gate_fail_items', state)
                self.assertFalse([r for r in state['finding_ledger'] if r['source'] == 'adversarial-gate'])
                self.clean()

    def test_one_malformed_answer_then_a_verdict_is_the_single_gate(self):
        result = self.run_coordinator(*OFF, env={**FAIL, 'FAKE_GATE_MALFORMED_ONCE': str(self.root / 'malformed-once')})
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual([bool(turn.get('gate_malformed')) for turn in gates(state)], [True, False])
        self.assertIn('not a valid verdict', (self.run_dir / 'evidence' / f"{gates(state)[1]['sequence']:03d}-exec-gate.prompt.txt").read_text())
        self.assertEqual(len(state['gate_fail_items']), 1)

    # --- lifecycle stages after the gate -------------------------------------------------------------------------------------
    def test_a_polish_q_fix_is_reviewed_without_a_gate(self):
        env = {**FAIL, 'FAKE_SPECIALIST_BLOCK': 'code-reviewer', 'FAKE_SPECIALIST_BLOCK_ONCE': str(self.root / 'block-once')}
        state = self.assert_one_gate_then_done(self.run_coordinator(*ON, env=env))
        self.assertEqual(len(state['lifecycle']['polish_fixes']), 1)
        fix = state['lifecycle']['polish_fixes'][0]['author_sequence']
        self.assertIn(('reviewer', 'EXEC'), [(t['role'], t['phase']) for t in state['turns'] if t['sequence'] > fix])

    def test_a_docs_write_outside_the_allowlist_is_rolled_back_and_exec_is_not_reopened(self):
        env = {**FAIL, 'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_CODE_ONCE': str(self.root / 'docs-code-once')}
        state = self.assert_one_gate_then_done(self.run_coordinator(*ON, env=env))
        [docs] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS']
        self.assertEqual((docs['route'], docs['docs_paths'], docs['rolled_back_outside']), ('SECURITY', ['CHANGELOG.md'], ['sum_ints.py']))
        self.assertNotIn('# docs comment fix', (self.workspace / 'sum_ints.py').read_text())
        self.assertTrue((self.workspace / 'CHANGELOG.md').is_file())
        self.assertEqual([t['phase'] for t in state['turns'] if t['sequence'] > docs['sequence']], ['DOCS', 'SECURITY'])

    def test_a_docs_review_revise_goes_back_to_the_docs_writer(self):
        env = {**FAIL, 'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_REVIEW_BLOCK_ONCE': str(self.root / 'docs-block')}
        state = self.assert_one_gate_then_done(self.run_coordinator(*ON, env=env))
        docs = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS']
        self.assertEqual([row['route'] for row in docs], ['DOCS', 'SECURITY'])
        self.assertEqual({row['epoch'] for row in docs}, {docs[0]['epoch']})   # EXEC was not reopened
        between = [(t['role'], t['phase']) for t in state['turns'] if docs[0]['sequence'] < t['sequence'] <= docs[1]['sequence']]
        self.assertEqual(between, [('reviewer', 'DOCS'), ('author', 'DOCS')])
        self.assertIn('# corrected entry', (self.workspace / 'CHANGELOG.md').read_text())
        self.assertNotIn('docs_feedback', state['lifecycle'])

    def test_a_security_stage_change_goes_to_the_reviewer_only(self):
        co = rc.Coordinator(self.args())
        tree = rc.git_snapshot(self.workspace)[0]
        co.state.update(gate_ran=True, phase='SECURITY', next='security')
        co.state['lifecycle'].update(stage='SECURITY', candidate_oid='0' * 64)
        co.worktree_security_turn()
        self.assertEqual((co.state['next'], co.state['gate_ran'], co.state['lifecycle']['stage']), ('reviewer', True, 'EXEC'))
        self.assertNotEqual(tree, '0' * 64)


class WriterReplayGateOnceTests(unittest.TestCase):
    locals().update({name: getattr(d09.D09C1b2Tests, name) for name in (*twl._HELPERS, 'setUp', 'git', 'state', 'change', 'drive')})

    def test_a_writer_replay_is_reviewed_by_the_reviewer_and_shadow_without_a_gate(self):
        self.change()
        co, status = self.drive(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py', **FAIL)
        self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
        state = self.state()
        self.assertEqual(len(gates(state)), 1)
        writer = state['lifecycle']['quality_writers']['simplifier']
        self.assertEqual(writer['state'], 'wrote')
        replay = [(t['role'], t['phase']) for t in state['turns'] if t['sequence'] > writer['sequence']]
        for step in (('reviewer', 'EXEC'), ('shadow', 'EXEC')):
            self.assertIn(step, replay)
        self.assertNotIn(('gate', 'EXEC'), replay)


if __name__ == '__main__':
    unittest.main()
