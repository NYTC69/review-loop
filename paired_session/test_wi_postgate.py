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

    def test_backlog_rows_go_to_the_end_of_their_section_or_a_new_one_before_done(self):
        for text, priority, expected in (
                ('# Backlog\n\n## P2\n\n', 'P2', '# Backlog\n\n## P2\n\n- row\n'),   # the empty-file template
                ('# Backlog\n\n## P2\n\n', 'P1', '# Backlog\n\n## P1\n\n- row\n\n## P2\n\n'),
                ('# B\n\n## P2 — normal\n\n- old\n\n## Done (recent 5)\n\n- done\n', 'P2',
                 '# B\n\n## P2 — normal\n\n- old\n- row\n\n## Done (recent 5)\n\n- done\n'),
                ('# B\n\n## P1 — high\n\n## P3\n\n- later\n\n## Done\n- done', 'P2',
                 '# B\n\n## P1 — high\n\n## P2\n\n- row\n\n## P3\n\n- later\n\n## Done\n- done\n'),
                ('# B\n\n## P1\n\n- item\n## Done\n', 'P2', '# B\n\n## P1\n\n- item\n\n## P2\n\n- row\n\n## Done\n'),
                ('# B\n\n## P20\n\n- x\n', 'P2', '# B\n\n## P20\n\n- x\n\n## P2\n\n- row\n')):
            with self.subTest(text=text, priority=priority):
                self.assertEqual(rc.backlog_insert(text, priority, ['- row\n']), expected)


class PostGateRunTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})
    locals().update({name: getattr(docsfix.DocsOperatorEditTests, name) for name in (
        'state', 'assert_done', 'coordinator_for', 'after', 'docs_rows', 'docs_cap', 'resume_in_process',
        'drive_with_docs_cap')})

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
        result = self.run_coordinator(*ON, env=env)   # step 2 (owner Q2): the forwarded row blocks SECURITY and gets the
        self.assert_done(result)                       # narrow author loop; the security reviewer closes it on the fixed tree
        state = self.state()
        [row] = [row for row in state['finding_ledger'] if row['source'] == 'specialist:code-reviewer']
        [polish] = self.receipts(state, 'POLISH-Q')
        self.assertEqual((polish['status'], polish.get('report_only'), polish['to_security']), ('READY', None, [row['id']]))
        security = min(turn['sequence'] for turn in state['turns'] if turn['phase'] == 'SECURITY')
        after = self.after(state, self.last_polish_turn(state))
        self.assertNotIn(('author', 'EXEC'), after[:after.index(('reviewer', 'SECURITY'))])   # no fix between POLISH-Q and SECURITY
        held = self.receipts(state, 'SECURITY')[0]
        self.assertEqual((held['status'], held['route']), ('HOLD', 'SECURITY-FIX'))
        self.assertIn(row['id'], held['review']['reason'])   # it blocks at SECURITY
        self.assertEqual(self.after(state, security)[0], ('author', 'EXEC'))
        self.assertEqual((row['status'], state['lifecycle']['security_fix_history'][0]['finding_ids']), ('fixed', [row['id']]))
        prompt = (self.run_dir / 'evidence' / f"{security:03d}-security-reviewer.prompt.txt").read_text()
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

    # --- step 3: repository BACKLOG sections, migration failures, the DOCS budget receipt after a writer -------------------
    COMPASS_BACKLOG = ('# Backlog\n\n## P0 — blocker\n\n## P1 — high priority\n\n- existing p1\n\n'
                       '## P2 — normal\n\n- existing p2\n\n## Done (recent 5)\n\n- done item\n')

    def test_accept_writes_only_open_rows_under_their_priority_sections(self):
        (self.workspace / 'BACKLOG.md').write_text(self.COMPASS_BACKLOG)
        rc.subprocess.run(['git', 'add', 'BACKLOG.md'], cwd=self.workspace, check=True)
        rc.subprocess.run(['git', 'commit', '-qm', 'backlog'], cwd=self.workspace, check=True)
        self.assert_done(self.run_coordinator(*ON, env=BLOCK))
        state = self.state()
        [blocker] = [row for row in state['finding_ledger'] if row['source'] == 'specialist:code-reviewer']
        fixed = {'id': 'F900', 'status': 'fixed', 'status_history': [], 'source': 'docs-reviewer', 'phase': 'DOCS',
                 'severity': 'MINOR', 'file': 'CHANGELOG.md', 'summary': 'stale wording fixed later'}
        state['finding_ledger'].append(fixed)   # listed at an earlier DONE, fixed before this accept (the field run)
        state['backlog'] += [{'source': 'docs-reviewer', 'phase': 'DOCS', 'sequence': 1, 'severity': 'MINOR',
                              'file': 'CHANGELOG.md', 'summary': fixed['summary'], 'failure_scenario': '', 'id': 'F900'},
                             {'source': 'persistent-reviewer', 'phase': 'PLAN', 'sequence': 1, 'severity': 'MINOR',
                              'file': 'notes.md', 'summary': 'an observation without a ledger id', 'failure_scenario': ''}]
        (self.run_dir / 'state.json').write_text(json.dumps(state))
        self.accept()
        text = (self.workspace / 'BACKLOG.md').read_text()
        self.assertNotIn('stale wording fixed later', text)
        p1, p2, done = (text.index(heading) for heading in ('## P1 —', '## P2 —', '## Done'))
        decide = text.index('- [P1 needs owner decision] specialist blocker (review-loop run')
        observation = text.index('- an observation without a ledger id (review-loop run')
        self.assertTrue(p1 < text.index('- existing p1') < decide < p2 < text.index('- existing p2') < observation < done, text)
        self.assertTrue(text.endswith('## Done (recent 5)\n\n- done item\n'), text)
        self.assertEqual(blocker['status'], 'open')

    def test_a_failing_post_gate_migration_holds_instead_of_a_traceback(self):
        for error in (RuntimeError('migration boom'), KeyError('fix_base')):
            with self.subTest(error=type(error).__name__):
                self.run_dir = self.root / f'migration-{type(error).__name__}'
                status, reason, _ = self.drive_with_docs_cap(None, {}, _migrate_security_loop=mock.Mock(side_effect=error))
                self.assertEqual(status, 'HOLD')
                self.assertIn('post-gate migration', reason)
                self.assertIn(str(error.args[0]), reason)
                state = self.state()
                self.assertEqual((state['status'], state['next'], state['turns']), ('HOLD', 'author', []))

    def test_a_budget_spent_after_a_writer_keeps_the_rollback_and_carried_records(self):
        original = rc.Coordinator._docs_beyond_cap

        def beyond(co, request, tree, need=1, docs=True):   # the writer had room; its review finds the budget spent
            return 'spent' if need == 1 else original(co, request, tree, need, docs)

        env = {**DOCS_ENV, 'FAKE_DOCS_CODE_ONCE': str(self.root / 'docs-code')}
        with mock.patch.object(rc.opv, 'carry_docs_only', return_value=['V001']):
            status, reason, out = self.drive_with_docs_cap(None, env, _docs_beyond_cap=beyond)
        self.assertEqual(status, 'DONE', reason + out)
        [docs] = self.docs_rows(self.state())
        self.assertEqual((docs['route'], docs['budget_spent'], docs['rolled_back_outside'], docs['opv_carried']),
                         ('SECURITY', True, ['sum_ints.py'], ['V001']))
        self.assertTrue(docs['rollback_diff'])
        self.assertNotIn('# docs comment fix', (self.workspace / 'sum_ints.py').read_text())


SECURITY_ONCE = {'FAKE_SECURITY_MINOR_SEVERITY': 'MAJOR'}   # one blocking security-reviewer row (no category: it blocks)
SECURITY_ALWAYS = {'FAKE_Q_MINOR': '1', 'FAKE_Q_SEVERITY': 'CRITICAL', 'FAKE_Q_SECURITY_FLAG': '1'}   # a new one every review
LOOP = [('author', 'EXEC'), ('reviewer', 'EXEC'), ('author', 'FINISH'), ('author', 'DOCS'), ('reviewer', 'SECURITY')]


class SecurityLoopTests(unittest.TestCase):
    """WI-POSTGATE step 2 (plan 2.E): the narrow SECURITY loop. A SECURITY HOLD on security-reviewer findings gets one author
    fix (at most SECURITY_FIX_LOOPS), the EXEC reviewer reviews only the fix delta (no shadow, no re-gate), FINISH runs, a docs
    touch-up without a docs review, then SECURITY again. Scan HOLDs, the cap and an author HOLD stay with the operator, whose
    fix takes the same narrow chain."""
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})
    locals().update({name: getattr(docsfix.DocsOperatorEditTests, name) for name in ('state', 'assert_done', 'after')})
    locals().update({name: getattr(PostGateRunTests, name) for name in ('receipts', 'accept', 'assert_history_kept')})

    def env(self, extra=None):
        return {**SECURITY_ONCE, 'FAKE_SECURITY_MINOR_ONCE': str(self.root / 'security-once'), **(extra or {})}

    def drive(self, *extra, env=None, action='run', **patches):
        """In-process drive (resume with action='resume'); `patches`: module attributes of rc (SECURITY_FIX_LOOPS) or
        Coordinator methods."""
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out), contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(rc if name.isupper() else rc.Coordinator, name, value))
            command = self.command(*ON, *extra)
            command[2] = action
            co = rc.Coordinator(rc.parser().parse_args(command[2:]))
            status = co.resume() if action == 'resume' else co.drive()
        return status, co.state.get('hold_reason', ''), out.getvalue()

    def first_security(self, state):
        return min(turn['sequence'] for turn in state['turns'] if turn['phase'] == 'SECURITY')

    def prompt(self, sequence):
        [path] = (self.run_dir / 'evidence').glob(f'{sequence:03d}-*.prompt.txt')
        return path.read_text()

    def loop_review(self, state, after):
        return next(turn for turn in state['turns'] if turn['sequence'] > after and turn['role'] == 'reviewer'
                    and turn['phase'] == 'EXEC')

    # --- AC4: the author loop -----------------------------------------------------------------------------------------------
    def test_a_blocking_security_finding_gets_the_narrow_author_loop(self):
        result = self.run_coordinator(*ON, env=self.env({**DOCS_ENV, 'FAKE_DOCS_TOUCHUP_WRITE': '1'}))
        self.assert_done(result)
        state = self.state()
        held = self.first_security(state)
        self.assertEqual(self.after(state, held), LOOP)   # no shadow, POLISH-Q, docs reviewer or gate
        [row] = [row for row in state['finding_ledger'] if row['source'] == 'security-reviewer']
        self.assertEqual(row['status'], 'fixed')
        self.assertEqual([item['route'] for item in self.receipts(state, 'SECURITY')], ['SECURITY-FIX', 'DONE'])
        life = state['lifecycle']
        self.assertNotIn('security_fix', life)
        [loop] = life['security_fix_history']
        self.assertEqual((loop['loop'], loop['by'], loop['finding_ids'], loop['mirror'], loop['outcome']),
                         (1, 'author', [row['id']], True, 'passed'))
        author = next(turn for turn in state['turns'] if turn['sequence'] > held)
        self.assertIn(row['id'], self.prompt(author['sequence']))   # the author gets the security finding
        review = self.loop_review(state, held)
        prompt = self.prompt(review['sequence'])
        self.assertIn('delta-since-security-hold.patch', prompt)
        self.assertIn('regression:', prompt)
        self.assertIn('+# security fix', (self.run_dir / 'context' / 'delta-since-security-hold.patch').read_text())
        touchup = self.receipts(state, 'DOCS')[-1]
        self.assertEqual((touchup['touchup'], touchup['route'], touchup['docs_paths']), (True, 'SECURITY', ['CHANGELOG.md']))
        docs = self.prompt(next(turn['sequence'] for turn in state['turns'] if turn['sequence'] > held and turn['phase'] == 'DOCS'))
        self.assertIn('sum_ints.py', docs.split('Narrow security fix', 1)[1])
        self.assertIn(row['id'], docs)
        findings = (self.run_dir / 'open-findings.md').read_text()
        self.assertIn('touch-up', findings.split(DECISION, 1)[1])
        report = self.accept()
        self.assertIn('touch-up', report.split(DECISION, 1)[1].split('\n## ', 1)[0])
        self.assertIn(f"SECURITY 窄修复环（WI-POSTGATE）：第 1 轮 作者修复 {row['id']}，SECURITY 通过", report)

    def test_without_docs_config_the_loop_has_no_docs_touch_up(self):
        self.assert_done(self.run_coordinator(*ON, '--docs-file', '', env=self.env()))
        state = self.state()
        self.assertEqual(self.after(state, self.first_security(state)), [step for step in LOOP if step != ('author', 'DOCS')])
        touchup = self.receipts(state, 'DOCS')[-1]
        self.assertEqual((touchup['touchup'], touchup['skipped'], touchup['route']),
                         (True, 'no docs path configured', 'SECURITY'))

    def test_the_security_fix_uses_no_exec_round(self):
        status, reason, out = self.drive(env=self.env(), SECURITY_FIX_LOOPS=0)
        self.assertEqual(status, 'HOLD', out)
        rounds = self.state()['exec_rounds']
        status, reason, out = self.drive(env=self.env(), action='resume')   # the same tree, the cap back at 2: loop 1
        self.assertEqual(status, 'DONE', reason + out)
        self.assertEqual(self.state()['exec_rounds'], rounds)

    # --- the loop cap -------------------------------------------------------------------------------------------------------
    def test_a_third_security_failure_holds_with_the_security_budget_spent(self):
        held = self.run_coordinator(*ON, env=SECURITY_ALWAYS)
        state = self.state()
        self.assertEqual(state['status'], 'HOLD', held.stdout + held.stderr)
        self.assertEqual([item['route'] for item in self.receipts(state, 'SECURITY')], ['SECURITY-FIX', 'SECURITY-FIX', 'HOLD'],
                         state['hold_reason'])
        self.assertEqual([(row['loop'], row['by'], row['outcome']) for row in state['lifecycle']['security_fix_history']],
                         [(1, 'author', 'held'), (2, 'author', 'held')])
        self.assertRegex(state['hold_reason'], r'^security reviewer findings: F\d+; the automatic security fix ran 2 times '
                                               r'\(its cap is 2\)')
        self.assertIn('SECURITY budget', state['hold_reason'])   # 3 reviews: no room for a 4th
        self.assertEqual(len([turn for turn in state['turns'] if turn['phase'] == 'SECURITY']), 3)
        again = self.run_operator_action('resume', *ON)
        self.assertIn('HOLD: security findings need a fix on a new tree, not a re-review', again.stdout, again.stdout + again.stderr)

    def test_an_operator_fix_after_the_security_budget_is_spent_holds_without_a_dispatch(self):
        held = self.run_coordinator(*ON, env=SECURITY_ALWAYS)
        before = self.state()
        self.assertEqual(before['status'], 'HOLD', held.stdout + held.stderr)
        self.assertIn('the SECURITY budget is spent (3 of 3 reviews): abort', before['hold_reason'])
        module = self.workspace / 'sum_ints.py'
        module.write_text(module.read_text() + '# operator security fix\n')
        again = self.run_operator_action('resume', *ON)
        state = self.state()
        self.assertEqual(state['status'], 'HOLD', again.stdout + again.stderr)
        self.assertIn('the SECURITY budget is spent (3 of 3 reviews): abort', state['hold_reason'])
        self.assertEqual((state['sequence'], len(state['turns'])), (before['sequence'], len(before['turns'])))   # no dispatch
        self.assertEqual((state['lifecycle']['stage'], state['lifecycle'].get('security_fix')), ('SECURITY', None))
        self.assertEqual(state['lifecycle']['security_fix_history'], before['lifecycle']['security_fix_history'])

    # --- AC5: the operator variant ------------------------------------------------------------------------------------------
    def test_the_operator_fix_takes_the_narrow_chain(self):
        status, reason, out = self.drive(env=self.env(), SECURITY_FIX_LOOPS=0)
        self.assertEqual(status, 'HOLD', out)
        self.assertIn('fix them outside the run and resume (narrow security loop)', reason)
        before = self.state()
        module = self.workspace / 'sum_ints.py'
        module.write_text(module.read_text() + '# operator security fix\n')
        self.assert_done(self.run_operator_action('resume', *ON))
        state = self.state()
        self.assertEqual(self.after(state, before['sequence']),
                         [('reviewer', 'EXEC'), ('author', 'FINISH'), ('author', 'DOCS'), ('reviewer', 'SECURITY')])
        self.assertEqual([item['route'] for item in self.receipts(state, 'SECURITY')], ['HOLD', 'SECURITY-FIX', 'DONE'])
        [loop] = state['lifecycle']['security_fix_history']
        self.assertEqual((loop['by'], loop['mirror'], loop['hold_tree']), ('operator', True, before['lifecycle']['candidate_oid']))
        self.assertIn('delta-since-security-hold.patch', self.prompt(self.loop_review(state, before['sequence'])['sequence']))
        self.assertIn('+# operator security fix', (self.run_dir / 'context' / 'delta-since-security-hold.patch').read_text())

    def test_an_operator_fix_of_tests_only_skips_the_docs_touch_up(self):
        status, _, out = self.drive(env=self.env(), SECURITY_FIX_LOOPS=0)
        self.assertEqual(status, 'HOLD', out)
        before = self.state()
        (self.workspace / 'tests').mkdir(exist_ok=True)
        (self.workspace / 'tests' / 'test_security_case.py').write_text('# operator regression test\n')
        self.assert_done(self.run_operator_action('resume', *ON))
        state = self.state()
        self.assertEqual(self.after(state, before['sequence']), [('reviewer', 'EXEC'), ('author', 'FINISH'), ('reviewer', 'SECURITY')])
        self.assertEqual(self.receipts(state, 'DOCS')[-1]['skipped'], 'only docs and tests changed since the security hold')

    def test_an_author_hold_in_the_loop_leaves_the_fix_to_the_operator(self):
        held = self.run_coordinator(*ON, env=self.env({'FAKE_SECURITY_FIX_HOLD': '1'}))
        state = self.state()
        self.assertEqual((state['status'], state['next']), ('HOLD', 'reviewer'), held.stdout + held.stderr)
        self.assertTrue(state['hold_reason'].startswith('implementer: '), state['hold_reason'])
        self.assertIn('the EXEC reviewer reviews your change (narrow security loop)', held.stdout)
        fix = state['lifecycle']['security_fix']
        self.assertEqual((fix['by'], fix['author_held']), ('author', state['sequence']))
        module = self.workspace / 'sum_ints.py'
        module.write_text(module.read_text() + '# operator security fix\n')
        self.assert_done(self.run_operator_action('resume', *ON))
        self.assertEqual(self.after(self.state(), state['sequence']), LOOP[1:])

    def test_a_scan_hold_stays_with_the_operator(self):
        def blocked(co, request_id):
            return {'status': 'blocked', 'reason': 'security preflight blocked (exit 1); fake-rule in sum_ints.py',
                    'removed_paths': [], 'preexisting_findings': [], 'findings': []}
        status, reason, out = self.drive(_security_preflight=blocked)
        self.assertEqual(status, 'HOLD', out)
        state = self.state()
        self.assertEqual([item['route'] for item in self.receipts(state, 'SECURITY')], ['HOLD'])
        self.assertNotIn('security_fix', state['lifecycle'])
        self.assertTrue(state['lifecycle']['security_hold']['mirror'])   # the operator's later fix gets the patch

    # --- AC8b: v3.16.x states -----------------------------------------------------------------------------------------------
    def legacy_security_hold(self):
        """A v3.16.x SECURITY HOLD on a blocking security-reviewer row: route HOLD, no hold record, no loop."""
        status, _, out = self.drive(env=self.env(), SECURITY_FIX_LOOPS=0, _record_security_hold=lambda co, *a: None)
        self.assertEqual(status, 'HOLD', out)
        state = self.state()
        self.assertFalse({'security_hold', 'security_fix', 'security_fix_history'} & set(state['lifecycle']))
        return state

    def test_a_saved_security_hold_on_the_same_tree_resumes_into_author_loop_1(self):
        before = self.legacy_security_hold()
        self.assert_done(self.run_operator_action('resume', *ON))
        state = self.state()
        self.assertEqual(self.after(state, before['sequence']), LOOP)
        self.assert_history_kept(before, state, appended=True)   # the security reviewer closes its row on the fixed tree
        [migration] = state['post_gate_migration']
        self.assertEqual((migration['from'], migration['to']), ('security-hold', 'SECURITY-FIX'))
        [loop] = state['lifecycle']['security_fix_history']
        self.assertEqual((loop['loop'], loop['by'], loop['mirror']), (1, 'author', True))

    def legacy_security_operator_route(self):
        """A v3.16.x SECURITY HOLD, the operator's fix, then the v3.16.x operator route: EXEC reopened, HOLD before the
        reviewer. Returns the legacy hold state."""
        before = self.legacy_security_hold()
        module = self.workspace / 'sum_ints.py'
        module.write_text(module.read_text() + '# operator security fix\n')
        co = rc.Coordinator(rc.parser().parse_args(['resume', *self.command(*ON)[3:]]))
        life = co.state['lifecycle']   # what v3.16.x did at SECURITY on the changed tree, then a HOLD before the reviewer
        request = rc.worktree_lifecycle.stage_request(life, 'security')
        life = rc.lifecycle_spine.complete(rc.lifecycle_spine.begin(life, request), {
            **request, 'status': 'HOLD', 'output_oid': rc.git_snapshot(self.workspace)[0], 'route': 'EXEC'})
        co.state['lifecycle'] = {**life, 'stage': 'EXEC', 'epoch': life['epoch'] + 1, 'candidate_oid': None}
        co.state.update(phase='EXEC', next='reviewer', delivered_review='', status='HOLD', hold_reason='invocation limit reached')
        co.save()
        return before

    def test_exec_reopened_by_the_v316_security_operator_route_resumes_as_an_operator_loop(self):
        before = self.legacy_security_operator_route()
        saved = self.state()
        self.assert_done(self.run_operator_action('resume', *ON))
        state = self.state()
        self.assertEqual(self.after(state, saved['sequence']),
                         [('reviewer', 'EXEC'), ('author', 'FINISH'), ('author', 'DOCS'), ('reviewer', 'SECURITY')])
        self.assert_history_kept(saved, state, appended=True)
        [migration] = state['post_gate_migration']
        self.assertEqual((migration['from'], migration['to']), ('security-exec', 'SECURITY-FIX'))
        [loop] = state['lifecycle']['security_fix_history']
        self.assertEqual((loop['by'], loop['mirror'], loop['hold_tree']), ('operator', False, before['lifecycle']['candidate_oid']))
        prompt = self.prompt(self.loop_review(state, saved['sequence'])['sequence'])
        self.assertIn('sum_ints.py', prompt.split('## Narrow security loop', 1)[1])   # no mirror: the changed paths

    def test_the_v316_security_operator_route_holds_without_a_dispatch_when_the_security_budget_is_spent(self):
        self.legacy_security_operator_route()
        saved = self.state()
        status, reason, out = self.drive(action='resume', _worktree_run_cap=lambda co, stage: 1)   # 1 review used of 1
        self.assertEqual(status, 'HOLD', out)
        self.assertEqual(reason, "the operator's change cannot be rechecked: the SECURITY budget is spent (1 of 1 reviews): abort")
        state = self.state()
        self.assertEqual((state['sequence'], len(state['turns'])), (saved['sequence'], len(saved['turns'])))   # no dispatch
        self.assertNotIn('security_fix', state['lifecycle'])


if __name__ == '__main__':
    unittest.main()
