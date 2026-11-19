"""WI-GATE1 S3 + S4 (owner 2026-11-19/20): one blocking threshold and the EXEC round rules.

S3: every finding carries a category (correctness, data-loss, security, money, other) and a reachability statement. A row
blocks only with a blocking severity, a blocking category and a reachability; program rows set their own category. A
REVISE whose open rows are all non-blocking is an approval with advisory rows (no fix round); the rows reach the run
backlog at DONE and the workspace BACKLOG.md at accept (in the accepted commit when one is made).
S4 (research-rc R1-R6): R1 three EXEC review rounds before the gate; R2 one gate, then one automatic fix round and at most
one more, then a HOLD that recommends a split (after the gate only the gate items and regressions of the fix block); R3 a
hard cap; R4 stop rules (b) a finding still open in two consecutive reviews, (c) new blockers in two consecutive reviews
after an approval; R5 FINISH, POLISH-Q, DOCS and SECURITY writes, writer replays and a sandbox-only test failure under an
APPROVE use no round. Each round HOLD names its rule."""
import json
import os
from unittest import mock
import unittest

from paired_session import test_gate_once as tgo
from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE
ON, OFF, FAIL, gates = tgo.ON, tgo.OFF, tgo.FAIL, tgo.gates
OTHER = {'category': 'other', 'reachability': ''}
CORRECT = {'category': 'correctness', 'reachability': 'every caller passing a bool'}


def fields(**by_summary):
    return {'FAKE_FINDING_FIELDS': json.dumps(by_summary)}


class ThresholdUnitTests(unittest.TestCase):
    def test_every_finding_schema_requires_category_and_reachability(self):
        for schema in (rc.review_schema(), rc.fresh_review_schema()):
            finding = schema['properties']['full_review']['items']
            self.assertIn('category', finding['required'])
            self.assertIn('reachability', finding['required'])
            self.assertEqual(finding['properties']['category']['enum'],
                             ['correctness', 'data-loss', 'security', 'money', 'other'])

    def test_a_row_blocks_only_with_severity_category_and_reachability(self):
        major = {'severity': 'MAJOR', 'source': 'persistent-reviewer'}
        cases = (({**major, **CORRECT}, True), ({**major, **OTHER}, False),
                 ({**major, 'category': 'money', 'reachability': ' '}, False),   # a blocking category needs its reachability
                 ({**major, 'category': 'data-loss', 'reachability': 'a crash mid-write'}, True),
                 ({'severity': 'MINOR', **CORRECT}, False),
                 ({**major, **CORRECT, 'post_gate_nonblocking': True}, False),
                 (major, True),   # a program row or a row saved before WI-GATE1: its severity alone
                 ({'severity': 'HIGH', 'source': 'adversarial-gate', 'category': 'security', 'reachability': 'x.py:1'}, True))
        for row, blocks in cases:
            with self.subTest(row=row):
                self.assertEqual(rc.blocks_by_severity(row), blocks)

    def test_every_reviewer_type_prompt_carries_the_shared_rule(self):
        for text in ('Only correctness, data loss, security and money issues block.', 'goes to the BACKLOG',
                     rc.THREAT_MODEL_SENTENCE):
            self.assertIn(text, rc.REVIEW_SEVERITY_GUIDANCE)
        self.assertIn('blocking category', rc.worktree_lifecycle.specialist_prompt('code-reviewer', 'body', None, [], 'p'))
        self.assertIn('category other', rc.LEDGER_DISPOSITION_GUIDANCE)

    def test_the_default_exec_cap_is_three(self):
        [action] = [a for a in rc.parser()._actions if '--max-exec-rounds' in a.option_strings]
        self.assertEqual(action.default, 3)


class ThresholdRunTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'args')})
    state, clean = tgo.GateOnceTests.state, tgo.GateOnceTests.clean

    def authors(self, state):
        return [t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC']

    def backlog(self):
        path = self.run_dir / 'backlog.md'
        return path.read_text() if path.exists() else ''

    # --- S3 ---------------------------------------------------------------------------------------------------------------
    def test_a_major_of_category_other_is_an_approval_with_advisory_and_reaches_the_run_backlog(self):
        env = {'FAKE_EXEC_MIXED_REVISE': '1', **fields(**{'major behavior issue': OTHER})}
        result = self.run_coordinator(*OFF, '--adversarial-gate', 'off', env=env)
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual((state['exec_rounds'], len(self.authors(state))), (1, 1))   # no fix round
        self.assertNotIn('advisory_repair_attempts', state)
        self.assertEqual(state['review_verdicts'][-1]['effective_verdict'], 'APPROVE_WITH_ADVISORY')
        row = next(r for r in state['finding_ledger'] if r['summary'] == 'major behavior issue')
        self.assertEqual((row['severity'], row['category'], row['status']), ('MAJOR', 'other', 'open'))
        self.assertIn(f"{row['id']} MAJOR sum_ints.py: major behavior issue", self.backlog())
        self.assertIn('small cleanup', self.backlog())   # the MINOR row too

    def test_the_same_major_with_a_blocking_category_still_starts_a_fix_round(self):
        env = {'FAKE_EXEC_MIXED_REVISE': '1', **fields(**{'major behavior issue': CORRECT})}
        result = self.run_coordinator(*OFF, '--adversarial-gate', 'off', '--max-exec-rounds', '1', env=env)
        self.assertIn('HOLD: EXEC round limit reached', result.stdout, result.stdout + result.stderr)
        self.assertIn('NOTE: round rule R1 stopped the run', result.stdout)

    def test_field_case_a_changelog_wording_finding_never_blocks_and_a_false_behavior_doc_does(self):
        # poker-tools runs 1b959a22, 3812c747: the only open MAJOR was a changelog wording note after review and gate approved
        base = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_REVIEW_BLOCK_ONCE': str(self.root / 'docs-block')}
        for name, extra, docs_fix in (('wording', OTHER, False),
                                      ('false', {'category': 'correctness', 'reachability': 'a user follows the entry'}, True)):
            with self.subTest(case=name):
                self.run_dir = self.root / ('docs-' + name)
                (self.root / 'docs-block').unlink(missing_ok=True)
                result = self.run_coordinator('--lifecycle-mode', 'on', '--max-exec-rounds', '1', '--max-invocations', '60',
                                              env={**base, **fields(**{'docs describe the wrong behavior': extra})})
                self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
                state = self.state()
                self.assertEqual(state['exec_rounds'], 1)   # never an EXEC round, never a HOLD
                docs_writers = [t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'DOCS']
                self.assertEqual(len(docs_writers), 2 if docs_fix else 1)
                row = next(r for r in state['finding_ledger'] if r['source'] == 'docs-reviewer')
                if docs_fix:
                    self.assertEqual(row['status'], 'fixed')
                else:
                    self.assertEqual((row['status'], row['category']), ('open', 'other'))
                    self.assertIn('docs describe the wrong behavior', self.backlog())
                self.clean()

    def test_accept_appends_the_rows_to_backlog_md_created_if_missing(self):
        env = {'FAKE_EXEC_MIXED_REVISE': '1', **fields(**{'major behavior issue': OTHER})}
        self.assertIn(DONE, self.run_coordinator(*OFF, '--adversarial-gate', 'off', env=env).stdout)
        accepted = self.run_operator_action('accept', *OFF, '--adversarial-gate', 'off')
        self.assertIn('ACCEPTED', accepted.stdout, accepted.stdout + accepted.stderr)
        self.assertIn('appended to', accepted.stdout)
        self.assertIn('in the working tree', accepted.stdout)
        text = (self.workspace / 'BACKLOG.md').read_text()
        self.assertTrue(text.startswith('# Backlog\n\n## P2\n\n'), text)
        self.assertIn(f'- major behavior issue (review-loop run {self.run_dir.name}, sum_ints.py) (added ', text)
        self.assertIn('- small cleanup (review-loop run', text)
        state = self.state()
        self.assertIn('untracked: BACKLOG.md', state['acceptance']['uncommitted'])
        self.assertEqual(state['acceptance']['repo_backlog']['rows'], 2)

    def test_accept_with_auto_commit_puts_the_rows_in_the_accepted_commit(self):
        (self.workspace / 'BACKLOG.md').write_text('# Backlog\n\n## P1\n\n- existing item\n')
        rc.subprocess.run(['git', 'add', 'BACKLOG.md'], cwd=self.workspace, check=True)
        rc.subprocess.run(['git', 'commit', '-qm', 'backlog'], cwd=self.workspace, check=True)
        env = {'FAKE_EXEC_MIXED_REVISE': '1', **fields(**{'major behavior issue': OTHER})}
        self.assertIn(DONE, self.run_coordinator('--lifecycle-mode', 'on', '--auto-commit', 'true', env=env).stdout)
        accepted = self.run_operator_action('accept', '--lifecycle-mode', 'on', '--auto-commit', 'true')
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        self.assertIn('in the accepted commit', accepted.stdout)
        committed = self.git('show', 'HEAD:BACKLOG.md')
        self.assertTrue(committed.startswith('# Backlog\n\n## P1\n\n- existing item\n- '), committed)   # appended at the end
        self.assertIn(f'- major behavior issue (review-loop run {self.run_dir.name}, sum_ints.py) (added ', committed)
        self.assertEqual(self.git('status', '--porcelain'), '')
        again = self.run_operator_action('accept', '--lifecycle-mode', 'on', '--auto-commit', 'true')
        self.assertEqual(again.stdout.strip().splitlines()[-1], 'ACCEPTED')
        self.assertEqual(self.git('show', 'HEAD:BACKLOG.md'), committed)   # no second copy

    def git(self, *args):
        return rc.subprocess.run(['git', *args], cwd=self.workspace, check=True, text=True,
                                 stdout=rc.subprocess.PIPE).stdout.strip()

    # --- S4 ---------------------------------------------------------------------------------------------------------------
    def test_r1_three_exec_review_rounds_by_default_then_hold(self):
        result = self.run_coordinator(*OFF, '--adversarial-gate', 'off', env={'FAKE_EXEC_MIXED_REVISE': '1'})
        self.assertIn('HOLD: EXEC round limit reached', result.stdout, result.stdout + result.stderr)
        self.assertIn('NOTE: round rule R1 stopped the run', result.stdout)
        state = self.state()
        self.assertEqual((state['exec_rounds'], state['exec_reviews'], state['round_limit_hold']['rule']), (3, 3, 'R1'))

    def test_r1_a_resume_without_the_flag_keeps_the_saved_cap_of_a_run_made_under_the_old_default(self):
        flags = (*OFF, '--adversarial-gate', 'off')
        env = {'FAKE_EXEC_MIXED_REVISE': '1'}
        held = self.run_coordinator(*flags, '--max-exec-rounds', '4', env=env)   # v3.8.x saved its default 4
        self.assertIn('HOLD: EXEC round limit reached', held.stdout, held.stdout + held.stderr)
        self.assertEqual(self.state()['exec_rounds'], 4)
        resumed = self.run_operator_action('resume', *flags, env=env)
        self.assertNotIn('REFUSED', resumed.stdout + resumed.stderr)
        self.assertIn('HOLD: EXEC round limit reached', resumed.stdout, resumed.stdout + resumed.stderr)
        self.assertEqual(self.state()['config']['max_exec_rounds'], 4)
        changed = self.run_operator_action('resume', *flags, '--max-exec-rounds', '3', env=env)
        self.assertIn('resume configuration differs: max_exec_rounds', changed.stdout + changed.stderr)

    def test_r2_a_gate_fail_gets_two_fix_rounds_then_a_split_hold_without_a_second_gate(self):
        keep = {**FAIL, 'FAKE_EXEC_STILL_OPEN': '[gate FAIL]'}
        held = self.run_coordinator(*OFF, '--max-exec-rounds', '1', env=keep)
        self.assertIn('HOLD: ' + rc.GATE_SPLIT_REASON, held.stdout, held.stdout + held.stderr)
        self.assertIn('NOTE: round rule R2 stopped the run', held.stdout)
        state = self.state()
        self.assertEqual((len(gates(state)), state['exec_rounds'], state['gate_fail_round']['round']), (1, 3, 1))
        plain = self.run_operator_action('resume', *OFF, '--max-exec-rounds', '1', env=keep)
        self.assertIn('HOLD: ' + rc.GATE_SPLIT_REASON, plain.stdout)   # only an extension continues
        resumed = self.run_operator_action('resume', *OFF, '--max-exec-rounds', '1', '--add-rounds', '1', env=FAIL)
        self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        self.assertEqual(len(gates(self.state())), 1)

    def test_r2_a_post_gate_finding_blocks_only_as_a_regression_of_the_fix(self):
        for name, reach, done in (('other', 'every caller', True), ('regression', 'regression: the fix drops a case', False)):
            with self.subTest(case=name):
                self.run_dir = self.root / ('post-' + name)
                result = self.run_coordinator(*OFF, env={**FAIL, 'FAKE_POST_GATE_FINDING': reach})
                state = self.state()
                post = [r for r in state['finding_ledger'] if r['summary'].startswith('post-gate finding')]
                self.assertTrue(post)
                self.assertEqual(len(gates(state)), 1)
                if done:   # the gate item is fixed; the new correctness finding goes to the BACKLOG
                    self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
                    self.assertEqual(state['exec_rounds'], 2)
                    self.assertTrue(all(r.get('post_gate_nonblocking') for r in post))
                    self.assertIn('post-gate finding', self.backlog())
                else:   # the regression of the fix blocks; the next review (no gate item open) has the ordinary threshold
                    self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
                    self.assertEqual(state['exec_rounds'], 3)
                    self.assertEqual([(r['status'], r.get('post_gate_nonblocking')) for r in post], [('fixed', None)])
                self.clean()

    def test_r4c_two_reviews_after_an_approval_each_with_new_blockers_hold_for_a_split(self):
        co = rc.Coordinator(self.args('--lifecycle-mode', 'off'))
        self.assertIsNone(co._update_stop_rules(1, {}, 'APPROVE'))
        for sequence in (2, 3):
            co.record_findings('persistent-reviewer', 'EXEC', sequence, [{'severity': 'MAJOR', 'file': 'a.py',
                               'summary': f'new {sequence}', 'failure_scenario': 'x', **CORRECT}])
            reason = co._update_stop_rules(sequence, {'prior_findings': []}, 'REVISE')
        self.assertTrue(reason.startswith(rc.NEW_BLOCKERS_REASON), reason)
        self.assertIsNone(co._update_stop_rules(3, {'prior_findings': []}, 'REVISE'))   # a replayed verdict counts once
        co.record_findings('persistent-reviewer', 'EXEC', 4, [{'severity': 'MAJOR', 'file': 'a.py', 'summary': 'style',
                           'failure_scenario': 'x', **OTHER}])
        self.assertIsNone(co._update_stop_rules(4, {'prior_findings': []}, 'REVISE'))   # a non-blocking row resets it

    def test_r3_the_hard_cap_guards_a_state_whose_round_count_ran_past_it(self):
        co = rc.Coordinator(self.args('--lifecycle-mode', 'off'))
        co.state.update(exec_rounds=5, gate_fail_round={'round': 5, 'extra': 0, 'sequence': 9})   # e.g. v3.8.x stage writes
        self.assertEqual(co._exec_limits(), (5, 'R3'))
        self.assertEqual(co._exec_limit_reason(), rc.HARD_CAP_REASON)
        co.state['round_extensions'] = [{'phase': 'EXEC', 'n': 2, 'hold_reason': rc.HARD_CAP_REASON}]
        self.assertEqual(co._exec_limits(), (7, 'R3'))   # an extension lands on top of the hard cap too

    def test_r4b_the_same_finding_still_open_in_two_consecutive_reviews_holds_and_asks(self):
        env = {'FAKE_EXEC_STILL_OPEN': 'required exercise'}
        held = self.run_coordinator(*OFF, '--exercise-revisions', '--max-exec-rounds', '5', env=env)
        self.assertIn('HOLD: ' + rc.STILL_OPEN_REASON + ': F', held.stdout, held.stdout + held.stderr)
        self.assertIn('NOTE: round rule R4b stopped the run', held.stdout)
        state = self.state()
        self.assertEqual((state['exec_rounds'], state['next']), (3, 'author'))
        plain = self.run_operator_action('resume', *OFF, '--exercise-revisions', '--max-exec-rounds', '5', env=env)
        self.assertIn('HOLD: ' + rc.STILL_OPEN_REASON, plain.stdout)   # plain resume keeps it: ask the owner
        resumed = self.run_operator_action('resume', *OFF, '--exercise-revisions', '--max-exec-rounds', '5', '--add-rounds', '1')
        self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        self.assertEqual(self.state()['exec_rounds'], 4)

    def test_r5_finish_and_polish_q_writes_use_no_exec_round(self):
        env = {'FAKE_FINISH_WRITE': '1', 'FAKE_SPECIALIST_BLOCK': 'code-reviewer',
               'FAKE_SPECIALIST_BLOCK_ONCE': str(self.root / 'spec-once')}
        result = self.run_coordinator(*ON, '--max-exec-rounds', '1', env=env)
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual(state['exec_rounds'], 1)
        self.assertTrue((self.root / 'spec-once').exists())   # the POLISH-Q fix ran

    def test_r5_an_approval_with_only_a_sandbox_test_failure_holds_for_attach_verification_without_a_round(self):
        argv = ('--lifecycle-mode', 'off', '--reviewer-vendor', 'codex', '--gate-vendor', 'codex', '--shadow', 'off',
                '--adversarial-gate', 'off')
        held = self.run_coordinator(*argv, env={'FAKE_REVIEW_TEST_FAILURE': '1'})
        self.assertIn('HOLD: ' + rc.TEST_EVIDENCE_HOLD, held.stdout, held.stdout + held.stderr)
        state = self.state()
        self.assertEqual((state['exec_rounds'], state['next'], len(self.authors(state))), (1, 'reviewer', 1))
        [row] = [r for r in state['finding_ledger'] if r['source'] == 'test-failure']
        self.assertEqual((row['category'], row['status']), ('correctness', 'open'))
        log = self.root / 'host.log'
        log.write_text('OK\n')
        attached = self.run_operator_action('attach-verification', *argv, '--command', 'python3 -m unittest',
                                            '--exit-code', '0', '--log', str(log), '--log-sha256',
                                            rc.hashlib.sha256(log.read_bytes()).hexdigest(), '--note', 'host run')
        self.assertIn('VERIFICATION: V001', attached.stdout, attached.stdout + attached.stderr)
        resumed = self.run_operator_action('resume', *argv)
        self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        state = self.state()
        self.assertEqual((state['exec_rounds'], len(self.authors(state))), (1, 1))   # no fix round was used


if __name__ == '__main__':
    unittest.main()
