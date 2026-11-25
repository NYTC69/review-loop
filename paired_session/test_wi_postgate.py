"""WI-POSTGATE (v3.17.0) step 1: after the gate, POLISH-Q reports instead of blocking in a delivery run, its security rows
go to the SECURITY reviewer, DOCS never HOLDs on findings or its budget, and the rows that would have blocked are listed for
the owner's decision (delivery report, open-findings.md, P1 backlog). Fake provider CLIs only."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from paired_session import test_wi_docsfix as docsfix
from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE
ON = ('--lifecycle-mode', 'on', '--max-invocations', '80')
DOCS_ENV = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'}
BLOCK = {'FAKE_SPECIALIST_BLOCK': 'code-reviewer'}
DECISION = '## 不阻塞、需要你决定'


class PostGateClassTests(unittest.TestCase):
    def classify(self, row, review_only=False):
        blocks = rc.blocks_by_severity(row) and not row.get('out_of_delta')
        return rc.worktree_lifecycle.post_gate_class({'status': 'open', **row}, review_only, blocks)

    def test_the_post_gate_classes(self):
        specialist = {'phase': 'POLISH-Q', 'source': 'specialist:code-reviewer', 'owner_role': 'specialist:code-reviewer'}
        docs = {'phase': 'DOCS', 'source': 'docs-reviewer'}
        for base in (specialist, docs):
            for row, expected in (({'severity': 'CRITICAL'}, 'decide'),   # no category: an old or program row
                                  ({'severity': 'MAJOR', 'category': 'correctness'}, 'decide'),
                                  ({'severity': 'MAJOR', 'category': 'data-loss'}, 'decide'),
                                  ({'severity': 'MAJOR', 'category': 'money'}, 'decide'),
                                  ({'severity': 'MAJOR', 'category': 'security'}, 'security'),
                                  ({'severity': 'MAJOR', 'security': True}, 'security'),
                                  ({'severity': 'MINOR', 'security': True}, 'report'),
                                  ({'severity': 'MAJOR', 'category': 'other'}, 'report'),
                                  ({'severity': 'CRITICAL', 'out_of_delta': True}, 'report')):
                with self.subTest(base=base['phase'], row=row):
                    self.assertEqual(self.classify({**base, **row}), expected)
        self.assertIsNone(self.classify({**specialist, 'severity': 'CRITICAL'}, review_only=True))   # Q1: the fix leg stays
        self.assertEqual(self.classify({**docs, 'severity': 'CRITICAL'}, review_only=True), 'decide')   # DOCS: both kinds
        for row in ({'phase': 'EXEC', 'source': 'persistent-reviewer', 'severity': 'CRITICAL'},
                    {'phase': 'SECURITY', 'source': 'security-reviewer', 'owner_role': 'security-reviewer', 'severity': 'MAJOR'},
                    {'phase': 'DOCS', 'source': 'persistent-reviewer', 'severity': 'CRITICAL'}):
            with self.subTest(row=row):
                self.assertIsNone(self.classify(row))
        self.assertIsNone(rc.worktree_lifecycle.post_gate_class({**docs, 'status': 'fixed', 'severity': 'CRITICAL'}, False, True))

    def test_owner_decision_lines_are_risk_first(self):
        ledger = [{'id': 'F001', 'status': 'open', 'phase': 'POLISH-Q', 'source': 'specialist:code-reviewer',
                   'owner_role': 'specialist:code-reviewer', 'severity': 'MAJOR', 'category': 'correctness',
                   'file': 'a.py', 'summary': 'wrong total'},
                  {'id': 'F002', 'status': 'open', 'phase': 'POLISH-Q', 'source': 'specialist:code-reviewer',
                   'owner_role': 'specialist:code-reviewer', 'severity': 'MAJOR', 'category': 'data-loss',
                   'file': 'b.py', 'summary': 'drops rows'},
                  {'id': 'F003', 'status': 'open', 'phase': 'DOCS', 'source': 'docs-reviewer', 'severity': 'MINOR',
                   'file': 'CHANGELOG.md', 'summary': 'wording'}]
        state = {'finding_ledger': ledger, 'lifecycle': {'receipts': [{'stage': 'DOCS', 'budget_spent': True}]}}
        lines = rc.worktree_lifecycle.owner_decision_lines(state, {'F001': 'decide', 'F002': 'decide', 'F003': 'report'})
        self.assertTrue(lines[0].startswith('- **Data loss** F002 '), lines)   # risk-first, then ledger order
        self.assertTrue(lines[1].startswith('- F001 '), lines)
        self.assertIn('F003', lines[2])
        self.assertIn('DOCS', lines[2])
        self.assertEqual(rc.worktree_lifecycle.owner_decision_lines({**state, 'lifecycle': {'receipts': []}}, {}), [])


class PostGateRunTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})
    locals().update({name: getattr(docsfix.DocsOperatorEditTests, name) for name in (
        'state', 'assert_done', 'coordinator_for', 'after', 'docs_rows', 'resume_in_process', 'drive_with_docs_cap')})

    def receipts(self, state, stage):
        return [row for row in state['lifecycle']['receipts'] if row['stage'] == stage]

    def last_polish_turn(self, state):
        return max(turn['sequence'] for turn in state['turns'] if turn['phase'] == 'POLISH-Q')

    def accept(self):
        accepted = self.run_operator_action('accept', *ON, '--reason', 'looks right')
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        return (self.run_dir / 'delivery-report.md').read_text()

    # --- AC1 + AC6: a specialist blocker is reported, not fixed -------------------------------------------------------------
    def test_a_specialist_blocker_is_reported_for_the_owners_decision_and_the_run_reaches_done(self):
        result = self.run_coordinator(*ON, env=BLOCK)
        self.assert_done(result)
        state = self.state()
        [row] = [row for row in state['finding_ledger'] if row['source'] == 'specialist:code-reviewer']
        self.assertEqual((row['status'], row['severity']), ('open', 'CRITICAL'))
        [polish] = self.receipts(state, 'POLISH-Q')
        self.assertEqual((polish['status'], polish['report_only'], polish.get('to_security')), ('READY', [row['id']], None))
        after = self.after(state, self.last_polish_turn(state))
        self.assertEqual(after[-1], ('reviewer', 'SECURITY'))
        self.assertFalse(set(after[:-1]) - {('author', 'DOCS'), ('reviewer', 'DOCS')}, after)   # no fix leg, no EXEC replay
        self.assertNotIn('fix_base', state['lifecycle'])
        self.assertIn('report-only', result.stdout)
        self.assertIn(row['id'], result.stdout)
        [entry] = [entry for entry in state['backlog'] if entry.get('id') == row['id']]
        self.assertEqual(entry['priority'], 'P1')
        self.assertIn(f"- [P1] [specialist:code-reviewer POLISH-Q #{entry['sequence']}] {row['id']} CRITICAL",
                      (self.run_dir / 'backlog.md').read_text())
        findings = (self.run_dir / 'open-findings.md').read_text()
        self.assertIn(DECISION, findings)
        self.assertIn(row['id'], findings.split(DECISION, 1)[1])
        self.assertIn('reject --text', findings)
        self.assertIn('needs your decision', result.stdout)
        report = self.accept()
        self.assertIn(DECISION, report)
        self.assertLess(report.index('结论'), report.index(DECISION))
        self.assertIn(row['id'], report.split(DECISION, 1)[1].split('\n## ', 1)[0])
        backlog = (self.workspace / 'BACKLOG.md').read_text()
        self.assertIn('- [P1 needs owner decision] specialist blocker (review-loop run', backlog)

    # --- AC2: a security-class specialist row goes to the SECURITY reviewer -------------------------------------------------
    def test_a_security_specialist_row_is_forwarded_to_the_security_reviewer(self):
        env = {**BLOCK, 'FAKE_SPECIALIST_SECURITY': '1', 'FAKE_SECURITY_KEEP_OPEN_ONCE': str(self.root / 'keep-open')}
        held = self.run_coordinator(*ON, env=env)
        self.assertIn('HOLD: security reviewer findings: ', held.stdout, held.stdout + held.stderr)
        state = self.state()
        [row] = [row for row in state['finding_ledger'] if row['source'] == 'specialist:code-reviewer']
        self.assertEqual(row['status'], 'open')
        self.assertIn(row['id'], state['hold_reason'])
        [polish] = self.receipts(state, 'POLISH-Q')
        self.assertEqual((polish['status'], polish.get('report_only'), polish['to_security']), ('READY', None, [row['id']]))
        after = self.after(state, self.last_polish_turn(state))
        self.assertEqual(after[-1], ('reviewer', 'SECURITY'))
        self.assertNotIn(('author', 'EXEC'), after)
        security = next(turn for turn in reversed(state['turns']) if turn['phase'] == 'SECURITY')
        prompt = (self.run_dir / 'evidence' / f"{security['sequence']:03d}-security-reviewer.prompt.txt").read_text()
        ledger = prompt.split('Open finding ledger (', 1)[1]
        self.assertIn(f"- {row['id']}: ", ledger)
        self.assertIn('forwarded from POLISH-Q', ledger)

    def test_the_security_reviewer_closes_a_forwarded_row_and_the_run_reaches_done(self):
        self.assert_done(self.run_coordinator(*ON, env={**BLOCK, 'FAKE_SPECIALIST_SECURITY': '1'}))
        state = self.state()
        [row] = [row for row in state['finding_ledger'] if row['source'] == 'specialist:code-reviewer']
        security = next(turn for turn in reversed(state['turns']) if turn['phase'] == 'SECURITY')
        self.assertEqual((row['status'], row['status_history'][-1]['round']), ('fixed', security['sequence']))
        co = self.coordinator_for('resume')
        next(item for item in co.state['finding_ledger'] if item['id'] == row['id'])['status'] = 'open'
        with self.assertRaisesRegex(RuntimeError, 'requires its owning specialist'):   # nobody else gains ownership
            co.apply_dispositions([{'id': row['id'], 'disposition': 'fixed', 'evidence': 'not mine'}], 999, [row['id']],
                                  'persistent-reviewer')

    # --- AC3 + AC6: DOCS never HOLDs on findings or its budget --------------------------------------------------------------
    def test_docs_spends_its_budget_and_moves_on_with_the_leftover_rows(self):
        env = {**DOCS_ENV, **BLOCK, 'FAKE_DOCS_REVIEW_BLOCK': '1'}
        status, reason, out = self.drive_with_docs_cap(4, env)
        self.assertEqual(status, 'DONE', reason + out)
        state = self.state()
        docs_turns = [(turn['role'], turn['phase']) for turn in state['turns'] if turn['phase'] == 'DOCS']
        self.assertEqual(docs_turns, [('author', 'DOCS'), ('reviewer', 'DOCS')] * 2)
        last = self.docs_rows(state)[-1]
        leftover = [row['id'] for row in state['finding_ledger'] if row['source'] == 'docs-reviewer' and row['status'] == 'open']
        self.assertTrue(leftover)
        self.assertEqual((last['status'], last['route'], last['budget_spent'], last['leftover']), ('READY', 'SECURITY', True, leftover))
        self.assertFalse([row for row in state['lifecycle']['receipts'] if row['status'] == 'HOLD'])
        self.assertFalse(state.get('hold_reason'))
        self.assertIn('DOCS budget', out)
        self.assertIn(leftover[0], {entry.get('id') for entry in state['backlog']})   # same file and summary: listed once
        report = self.accept()
        section = report.split(DECISION, 1)[1].split('\n## ', 1)[0]
        [blocker] = [row['id'] for row in state['finding_ledger'] if row['source'] == 'specialist:code-reviewer']
        for finding in (blocker, *leftover):
            self.assertIn(finding, section)
        self.assertIn('DOCS', section)

    def test_a_docs_writer_runs_only_with_room_for_its_review(self):
        status, reason, out = self.drive_with_docs_cap(1, DOCS_ENV)
        self.assertEqual(status, 'DONE', reason + out)
        state = self.state()
        self.assertFalse([turn for turn in state['turns'] if turn['phase'] == 'DOCS'])
        [docs] = self.docs_rows(state)
        self.assertEqual((docs['route'], docs['budget_spent'], docs['leftover']), ('SECURITY', True, []))

    def test_a_saved_docs_budget_hold_resumes_to_security(self):
        env = {**DOCS_ENV, 'FAKE_DOCS_REVIEW_BLOCK_ONCE': str(self.root / 'docs-block')}
        budget = RuntimeError('DOCS budget exhausted (2 writer and review calls in this run); fix the documentation in '
                              'the workspace and resume')   # the v3.16.x HOLD at the next DOCS writer
        status, reason, _ = self.drive_with_docs_cap(2, env, _docs_spent=mock.Mock(side_effect=budget))
        self.assertEqual(status, 'HOLD', reason)
        held = self.state()
        status, reason, out = self.resume_in_process(cap=2, env=env)
        self.assertEqual(status, 'DONE', reason + out)
        state = self.state()
        self.assertEqual(self.after(state, held['sequence']), [('reviewer', 'SECURITY')])
        self.assertTrue(self.docs_rows(state)[-1]['budget_spent'])

    # --- AC8a: a v3.16.x state at a POLISH-Q fix HOLD -----------------------------------------------------------------------
    def legacy_hold(self, env, **patches):
        """Drive with the v3.16.x POLISH-Q rule (no post-gate class): a specialist blocker goes to the fix leg."""
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.ExitStack() as stack, \
                mock.patch.object(rc.worktree_lifecycle, 'post_gate_class', lambda *_: None, create=True):
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(rc.Coordinator, name, value))
            co = self.coordinator_for()
            self.assertEqual(co.drive(), 'HOLD', out.getvalue())
        return self.state()

    def assert_history_kept(self, before, after, appended=False):
        self.assertEqual(after['lifecycle']['receipts'][:len(before['lifecycle']['receipts'])], before['lifecycle']['receipts'])
        self.assertEqual(after['turns'][:len(before['turns'])], before['turns'])
        ledger = {row['id']: row for row in after['finding_ledger']}
        if not appended:
            self.assertEqual([ledger[row['id']] for row in before['finding_ledger']], before['finding_ledger'])
        for row in before['finding_ledger'] if appended else ():   # a later owner pass may only append dispositions
            later = ledger[row['id']]
            self.assertEqual(later['status_history'][:len(row['status_history'])], row['status_history'])
            self.assertEqual({key: value for key, value in later.items() if key not in ('status', 'status_history')},
                             {key: value for key, value in row.items() if key not in ('status', 'status_history')})

    def test_a_saved_polish_fix_hold_on_the_reviewed_tree_resumes_to_docs_without_an_author_turn(self):
        limit = RuntimeError('the POLISH-Q fix needs at least 2 more invocations; resume with a higher --max-invocations, or abort')
        before = self.legacy_hold(BLOCK, worktree_polish_fix_turn=mock.Mock(side_effect=limit))
        life = before['lifecycle']
        self.assertEqual((before['next'], life['stage'], life['fix_base']), ('polish-fix', 'POLISH-Q', life['candidate_oid']))
        self.assert_done(self.run_operator_action('resume', *ON, env=BLOCK))
        state = self.state()
        after = self.after(state, before['sequence'])
        self.assertNotIn('author', {role for role, phase in after if phase != 'DOCS'})
        self.assertEqual(after[-1], ('reviewer', 'SECURITY'))
        self.assertNotIn(('reviewer', 'EXEC'), after)
        self.assert_history_kept(before, state)
        [migration] = state['post_gate_migration']
        self.assertEqual((migration['from'], migration['to']), ('polish-fix', 'DOCS'))
        self.assertFalse({'fix_base', 'recheck'} & set(state['lifecycle']))
        self.assertEqual(state['lifecycle']['epoch'], life['epoch'])

    def test_a_saved_polish_fix_hold_after_a_fix_resumes_to_the_exec_reviewer(self):
        before = self.legacy_hold(BLOCK)   # the fix wrote, the owner found a new blocker: "blockers remain after the fix"
        self.assertTrue(before['hold_reason'].startswith('POLISH-Q blockers remain after the fix'), before['hold_reason'])
        self.assertNotEqual(before['lifecycle']['fix_base'], before['lifecycle']['candidate_oid'])
        self.assert_done(self.run_operator_action('resume', *ON, env=BLOCK))
        state = self.state()
        after = self.after(state, before['sequence'])
        self.assertEqual(after[0], ('reviewer', 'EXEC'))
        self.assertNotIn(('author', 'EXEC'), after)
        self.assert_history_kept(before, state, appended=True)   # the owner re-reviews in the next POLISH-Q pass
        [migration] = state['post_gate_migration']
        self.assertEqual((migration['from'], migration['to']), ('polish-fix', 'EXEC'))
        self.assertEqual(state['lifecycle']['epoch'], before['lifecycle']['epoch'] + 1)

    # --- AC9: a --review-only run keeps the POLISH-Q fix leg ----------------------------------------------------------------
    def test_a_review_only_run_keeps_the_polish_q_fix_leg(self):
        env = {**BLOCK, 'FAKE_SPECIALIST_BLOCK_ONCE': str(self.root / 'block-once')}
        module = self.workspace / 'sum_ints.py'   # the existing change under review
        module.write_text('def sum_ints(values):\n    if not all(type(x) is int for x in values):\n'
                          '        raise TypeError("ints only")\n    return sum(values)\n')
        self.assert_done(self.run_coordinator('--review-only', *ON, env=env))
        state = self.state()
        [row] = [row for row in state['finding_ledger'] if row['source'] == 'specialist:code-reviewer']
        self.assertEqual(row['status'], 'fixed')
        first = self.receipts(state, 'POLISH-Q')[0]
        self.assertEqual(first['status'], 'HOLD')
        self.assertNotIn('report_only', first)
        self.assertIn(('author', 'EXEC'), self.after(state, first['specialist_turns'][-1]['sequence']))
        self.assertIn('# specialist fix', (self.workspace / 'sum_ints.py').read_text())
        self.assertNotIn('post_gate_migration', state)


if __name__ == '__main__':
    unittest.main()
