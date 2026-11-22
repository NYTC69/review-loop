"""WI-GATE1 S3 + S4 (owner 2026-11-19/20): one blocking threshold and the EXEC round rules.

S3: every finding carries a category (correctness, data-loss, security, money, other) and a reachability statement. A row
blocks only with a blocking severity and a blocking category (an empty reachability never demotes it); program rows set
their own category. A
REVISE whose open rows are all non-blocking is an approval with advisory rows (no fix round); the rows reach the run
backlog at DONE and the workspace BACKLOG.md at accept (in the accepted commit when one is made).
S4 (research-rc R1-R6): R1 three EXEC review rounds before the gate; R2 one gate, then one automatic fix round and at most
one more, then a HOLD that recommends a split (after the gate only the gate items and regressions of the fix block); R3 a
hard cap; R4 stop rules (b) a finding still open in two consecutive reviews, (c) new blockers in two consecutive reviews
after an approval; R5 FINISH, POLISH-Q, DOCS and SECURITY writes, writer replays and a sandbox-only test failure under an
APPROVE use no round. Each round HOLD names its rule."""
import contextlib
import io
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

    def test_a_row_blocks_with_a_blocking_severity_and_category(self):
        major = {'severity': 'MAJOR', 'source': 'persistent-reviewer'}
        cases = (({**major, **CORRECT}, True), ({**major, **OTHER}, False),
                 ({**major, 'category': 'money', 'reachability': ' '}, True),   # R2 F1: an empty reachability never demotes it
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

    def test_r2f1_a_blocking_category_with_an_empty_reachability_still_blocks(self):
        env = {'FAKE_EXEC_MIXED_REVISE': '1', **fields(**{'major behavior issue': {'category': 'correctness', 'reachability': ''}})}
        result = self.run_coordinator(*OFF, '--adversarial-gate', 'off', '--max-exec-rounds', '1', env=env)
        self.assertIn('HOLD: EXEC round limit reached', result.stdout, result.stdout + result.stderr)
        row = next(r for r in self.state()['finding_ledger'] if r['summary'] == 'major behavior issue')
        self.assertEqual((row['category'], row['status']), ('correctness', 'open'))
        self.assertNotIn('major behavior issue', self.backlog())

    def test_r2f8_an_advisory_only_review_without_test_evidence_holds_instead_of_an_author_round(self):
        env = {'FAKE_EXEC_MIXED_REVISE': '1', 'FAKE_MISSING_OBSERVED': '1', **fields(**{'major behavior issue': OTHER})}
        held = self.run_coordinator(*OFF, '--adversarial-gate', 'off', env=env)
        self.assertIn('HOLD: ' + rc.TEST_EVIDENCE_HOLD, held.stdout, held.stdout + held.stderr)
        state = self.state()
        self.assertEqual((state['exec_rounds'], len(self.authors(state)), state['next']), (1, 1, 'reviewer'))

    def test_r3f8_an_advisory_only_review_with_a_failed_test_holds_and_shows_the_failure(self):
        env = {'FAKE_EXEC_MINOR_REVISE': '1', 'FAKE_REVIEW_TEST_FAILURE': '1'}
        for shadow in ('off', 'on'):
            with self.subTest(shadow=shadow):
                self.run_dir = self.root / ('test-failure-' + shadow)
                held = self.run_coordinator('--lifecycle-mode', 'off', '--shadow', shadow, '--reviewer-vendor', 'codex',
                                            '--adversarial-gate', 'off', env=env)
                if shadow == 'off':
                    self.assertIn('HOLD: ' + rc.TEST_EVIDENCE_HOLD, held.stdout, held.stdout + held.stderr)
                    self.assertIn('exit status 1', held.stdout)
                    self.assertIn('FAILED fake test', held.stdout)
                else:   # the fresh shadow's own evidence HOLD comes first; it gives the same attach-verification route
                    self.assertIn('record it with attach-verification, then resume', held.stdout, held.stdout + held.stderr)
                state = self.state()
                self.assertEqual((state['exec_rounds'], len(self.authors(state)), state['next']), (1, 1, 'reviewer'))
                self.assertFalse(any(rc.blocks_by_severity(row) for row in state['finding_ledger']))
                self.clean()

    def test_r2f9_the_advisory_fix_round_is_refused_on_a_delivery_run(self):
        result = self.run_coordinator(*ON, '--advisory-fix-round', 'true')
        self.assertIn('REFUSED: ' + rc.ADVISORY_FIX_ROUND_REFUSAL, result.stdout + result.stderr)
        self.assertFalse((self.run_dir / 'state.json').exists())

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

    def test_r2f3_a_failed_commit_leaves_no_backlog_drift_and_the_next_accept_commits_the_rows(self):
        env = {'FAKE_EXEC_MIXED_REVISE': '1', **fields(**{'major behavior issue': OTHER})}
        flags = ('--lifecycle-mode', 'on', '--auto-commit', 'true')
        self.assertIn(DONE, self.run_coordinator(*flags, env=env).stdout)
        before = self.git('status', '--porcelain')
        failed = self.run_operator_action('accept', *flags, env={'GIT_COMMITTER_NAME': '', 'GIT_AUTHOR_NAME': ''})
        self.assertIn('commit-tree', failed.stdout + failed.stderr)   # the identity error, after the backlog was planned
        self.assertFalse((self.workspace / 'BACKLOG.md').exists())   # nothing written before the commit exists
        self.assertEqual(self.git('status', '--porcelain'), before)
        accepted = self.run_operator_action('accept', *flags)
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        self.assertIn('- major behavior issue (review-loop run', self.git('show', 'HEAD:BACKLOG.md'))
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_r3f11_accept_and_journal_replay_keep_the_tracked_backlog_spelling(self):
        (self.workspace / 'Backlog.md').write_text('# Backlog\n\n- existing item\n')
        self.git('add', 'Backlog.md')
        self.git('commit', '-qm', 'mixed-case backlog fixture')
        flags = ('--lifecycle-mode', 'on', '--auto-commit', 'true')
        self.assertIn(DONE, self.run_coordinator(*flags, env={'FAKE_EXEC_MINOR_REVISE': '1'}).stdout)
        done = self.state()
        accepted = self.run_operator_action('accept', *flags)
        self.assertIn('ACCEPTED', accepted.stdout, accepted.stdout + accepted.stderr)
        self.assertEqual([name for name in self.git('ls-tree', '--name-only', 'HEAD').splitlines()
                          if name.casefold() == 'backlog.md'], ['Backlog.md'])
        self.assertEqual(self.git('status', '--porcelain'), '')
        committed = self.git('show', 'HEAD:Backlog.md')
        self.assertIn('- existing item\n- advisory exec polish', committed)
        journal = json.loads((self.run_dir / 'evidence/delivery-commit.json').read_text())
        self.assertEqual(journal['backlog']['path'], str(self.workspace / 'Backlog.md'))

        # Simulate interruption after the commit and backlog write but before the acceptance state was saved.
        (self.run_dir / 'state.json').write_text(json.dumps(done))
        command = self.command(*flags, '--skip-probe', '--expect', journal['intent']['digest'])
        command[2] = 'accept'
        replayed = rc.subprocess.run(command, cwd=self.root, text=True, capture_output=True)
        self.assertIn('ACCEPTED', replayed.stdout, replayed.stdout + replayed.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD'), journal['commit'])
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('show', 'HEAD:Backlog.md'), committed)

    def test_r2f4_a_symlinked_or_ignored_backlog_is_never_written(self):
        env = {'FAKE_EXEC_MIXED_REVISE': '1', **fields(**{'major behavior issue': OTHER})}
        flags = ('--lifecycle-mode', 'on', '--auto-commit', 'true')
        shared = self.root / 'shared-backlog.md'
        for case in ('symlink', 'ignored'):
            with self.subTest(case=case):
                self.run_dir = self.root / case
                if case == 'symlink':
                    shared.write_text('# Shared\n')
                    (self.workspace / 'BACKLOG.md').symlink_to(shared)
                else:
                    (self.workspace / 'BACKLOG.md').unlink()
                    (self.workspace / '.git' / 'info' / 'exclude').write_text('BACKLOG.md\n')
                rc.subprocess.run(['git', 'add', '-A'], cwd=self.workspace, check=True)
                rc.subprocess.run(['git', 'commit', '-qm', 'setup ' + case], cwd=self.workspace, check=True)
                self.assertIn(DONE, self.run_coordinator(*flags, env=env).stdout)
                accepted = self.run_operator_action('accept', *flags)
                self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
                self.assertIn('NOTE: the non-blocking findings were not written to', accepted.stdout)
                self.assertIn('major behavior issue', (self.run_dir / 'backlog.md').read_text())
                if case == 'symlink':
                    self.assertTrue((self.workspace / 'BACKLOG.md').is_symlink())
                    self.assertEqual(shared.read_text(), '# Shared\n')
                else:
                    self.assertFalse((self.workspace / 'BACKLOG.md').exists())
                    self.assertNotIn('BACKLOG.md', self.git('ls-tree', '--name-only', 'HEAD'))

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
        for name, reach in (('other', 'every caller'), ('regression', 'regression: the fix drops a case'),
                            ('then-other', 'regression: the fix drops a case|every caller')):
            with self.subTest(case=name):
                self.run_dir = self.root / ('post-' + name)
                result = self.run_coordinator(*OFF, env={**FAIL, 'FAKE_POST_GATE_FINDING': reach})
                state = self.state()
                post = [r for r in state['finding_ledger'] if r['summary'].startswith('post-gate finding')]
                self.assertEqual(len(gates(state)), 1)
                if name == 'other':   # the gate item is fixed; the new correctness finding goes to the BACKLOG
                    self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
                    self.assertEqual(state['exec_rounds'], 2)
                    self.assertTrue(post and all(r.get('post_gate_nonblocking') for r in post))
                    self.assertIn('post-gate finding', self.backlog())
                elif name == 'regression':   # R4c: after the approval, two reviews in a row each bring a new regression
                    self.assertIn('HOLD: ' + rc.NEW_BLOCKERS_REASON, result.stdout, result.stdout + result.stderr)
                    self.assertIn('NOTE: round rule R4c stopped the run', result.stdout)
                    self.assertEqual((state['exec_rounds'], state['next']), (3, 'author'))
                else:   # S3 R2 F2: the repair scope lasts until a repair review passes, past the last gate item
                    self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
                    self.assertEqual(state['exec_rounds'], 3)
                    self.assertEqual([(r['status'], bool(r.get('post_gate_nonblocking'))) for r in post],
                                     [('fixed', False), ('open', True)])
                    self.assertIsNotNone(state['gate_fail_round']['closed_at'])
                self.clean()

    def test_r2f2_a_shadow_blocker_in_the_gate_repair_is_confirmed_by_the_reviewer_first(self):
        for confirm, rounds in (('withdrawn', 2), ('still_open', 3)):
            with self.subTest(confirm=confirm):
                self.run_dir = self.root / confirm
                marker = str(self.root / (confirm + '.gate'))
                env = {'FAKE_GATE_BLOCK_ONCE': marker, 'FAKE_SHADOW_BLOCKER_IF_FILE': marker, 'FAKE_CONFIRM_SHADOW': confirm}
                result = self.run_coordinator('--lifecycle-mode', 'off', env=env)
                self.assertIn(DONE, result.stdout, result.stdout + result.stderr)
                state = self.state()
                [row] = [r for r in state['finding_ledger'] if r['summary'] == 'shadow regression']
                self.assertEqual(row['status'], 'withdrawn' if confirm == 'withdrawn' else 'fixed')
                self.assertEqual(bool(row.get('confirmed_regression')), confirm == 'still_open')
                self.assertEqual((state['exec_rounds'], len(gates(state))), (rounds, 1))
                reviews = [t for t in state['turns'] if t['role'] == 'reviewer' and t['phase'] == 'EXEC'
                           and t['sequence'] > row['origin_round']]
                self.assertEqual(reviews[0]['answer']['prior_findings'][0]['disposition'], confirm)   # no author before it
                self.assertNotIn('shadow regression', self.backlog())
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

    def test_r3f7_two_distinct_confirmed_shadow_regressions_stop_at_r4c(self):
        marker = str(self.root / 'gate-failed')
        flags = ('--lifecycle-mode', 'off', '--max-exec-rounds', '1')
        keep = {**FAIL, 'FAKE_EXEC_STILL_OPEN': '[gate FAIL]'}
        first = self.run_coordinator(*flags, env=keep)
        self.assertIn('HOLD: ' + rc.GATE_SPLIT_REASON, first.stdout, first.stdout + first.stderr)
        rounds_before_resume = self.state()['exec_rounds']
        (self.root / 'gate-failed').touch()
        env = {'FAKE_SHADOW_BLOCKER_IF_FILE': marker, 'FAKE_SHADOW_BLOCKER_REPEATS': '2',
               'FAKE_CONFIRM_SHADOW': 'still_open'}
        held = self.run_operator_action('resume', *flags, '--add-rounds', '3', env=env)
        state = self.state()
        self.assertIn('NOTE: round rule R4c stopped the run', held.stdout, held.stdout + held.stderr)
        self.assertEqual((state['exec_rounds'], len(self.authors(state)), len(gates(state))),
                         (rounds_before_resume + 2, rounds_before_resume + 2, 1))
        self.assertEqual(state['stop_rules']['new_blocking_streak'], 2)

    def test_r3f12_a_valid_shadow_finding_outside_the_gate_repair_stays_open_in_both_backlogs(self):
        marker = str(self.root / 'gate-failed')
        env = {'FAKE_GATE_BLOCK_ONCE': marker, 'FAKE_SHADOW_BLOCKER_IF_FILE': marker,
               'FAKE_CONFIRM_SHADOW': 'non_blocking', 'FAKE_REVIEW_PROMPT_LOG': str(self.root / 'reviews.jsonl')}
        flags = ('--lifecycle-mode', 'off')
        done = self.run_coordinator(*flags, env=env)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        [row] = [r for r in state['finding_ledger'] if r['summary'] == 'shadow regression']
        self.assertEqual((row['status'], row['category'], rc.blocks_by_severity(row)), ('open', 'correctness', False))
        self.assertTrue(row.get('post_gate_nonblocking'))
        self.assertFalse(row.get('needs_confirmation'))
        self.assertEqual((state['exec_rounds'], len(self.authors(state)), len(gates(state))), (2, 2, 1))
        self.assertIn('shadow regression', self.backlog())
        prompts = [json.loads(line) for line in (self.root / 'reviews.jsonl').read_text().splitlines()]
        self.assertIn('non_blocking', next(prompt for prompt in prompts if 'shadow finding to confirm' in prompt))
        accepted = self.run_operator_action('accept', *flags)
        self.assertIn('ACCEPTED', accepted.stdout, accepted.stdout + accepted.stderr)
        self.assertIn('- shadow regression (review-loop run', (self.workspace / 'BACKLOG.md').read_text())

    def test_r4f14_a_non_blocking_shadow_disposition_approves_at_the_gate_repair_limit(self):
        marker = str(self.root / 'gate-failed')
        env = {'FAKE_GATE_BLOCK_ONCE': marker, 'FAKE_SHADOW_BLOCKER_IF_FILE': marker,
               'FAKE_EXEC_WITHDRAW_GATE_ONCE': str(self.root / 'first-repair'),
               'FAKE_CONFIRM_SHADOW': 'non_blocking', 'FAKE_CONFIRM_SHADOW_REVISE': '1'}
        result = self.run_coordinator('--lifecycle-mode', 'off', '--max-exec-rounds', '1', env=env)
        state = self.state()
        self.assertEqual((state['exec_rounds'], len(self.authors(state)), len(gates(state))), (3, 3, 1))
        verdict = state['review_verdicts'][-1]
        [review] = [turn for turn in state['turns'] if turn['sequence'] == verdict['sequence']]
        gate_item = next(row for row in state['finding_ledger'] if row['source'] == 'adversarial-gate')
        shadow_item = next(row for row in state['finding_ledger'] if row['source'] == 'fresh-shadow')
        self.assertEqual({row['id']: row['disposition'] for row in review['answer']['prior_findings']},
                         {gate_item['id']: 'fixed', shadow_item['id']: 'non_blocking'})
        self.assertEqual(review['answer']['full_review'], [])
        self.assertTrue(any(command['command'] == 'python3 -m unittest' and command['exit_code'] == 0
                            for command in review['observed_commands']))
        self.assertEqual(verdict['reviewer_raw_verdict'], 'REVISE')
        self.assertEqual(verdict['effective_verdict'], 'APPROVE_WITH_ADVISORY', result.stdout + result.stderr)
        self.assertIn(DONE, result.stdout)
        self.assertFalse(state.get('round_limit_hold'))
        self.assertIn('shadow regression', self.backlog())

    def test_mixed_shadow_non_blocking_dispositions_proceed_after_gate_repair(self):
        marker = str(self.root / 'gate-failed')
        env = {'FAKE_GATE_BLOCK_ONCE': marker, 'FAKE_SHADOW_BLOCKER_IF_FILE': marker,
               'FAKE_SHADOW_INCLUDE_MINOR': '1', 'FAKE_CONFIRM_SHADOW': 'non_blocking',
               'FAKE_EXEC_NON_BLOCKING': 'shadow naming'}
        done = self.run_coordinator('--lifecycle-mode', 'off', env=env)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        [major, minor] = [row for row in state['finding_ledger'] if row['source'] == 'fresh-shadow']
        verdict = state['review_verdicts'][-1]
        self.assertEqual(verdict['reviewer_raw_verdict'], 'APPROVE')
        [review] = [turn for turn in state['turns'] if turn['sequence'] == verdict['sequence']]
        raw = {row['id']: row['disposition'] for row in review['answer']['prior_findings']}
        self.assertEqual((raw[major['id']], raw[minor['id']]), ('non_blocking', 'non_blocking'))
        self.assertEqual((major['status'], major['status_history'][-1]['status']), ('open', 'non_blocking'))
        self.assertTrue(major['post_gate_nonblocking'])
        self.assertEqual((minor['status'], minor['status_history'][-1]['status']), ('open', 'still_open'))
        self.assertFalse(rc.blocks_by_severity(minor))
        self.assertEqual(done.stdout.count(
            'NOTE: non_blocking recorded as still_open for findings that need no confirmation: ' + minor['id']), 1)

    def test_non_blocking_on_an_ordinary_major_converts_approve_to_revise(self):
        co = rc.Coordinator(self.args('--lifecycle-mode', 'off', '--shadow', 'off'))
        co.state.update(phase='EXEC', next='reviewer', exec_rounds=1, sequence=1)
        finding = {'severity': 'MAJOR', 'file': 'sum_ints.py', 'failure_scenario': 'wrong result', **CORRECT}
        co.record_findings('persistent-reviewer', 'EXEC', 1, [{**finding, 'summary': 'ordinary blocker'}])
        co.record_findings('fresh-shadow', 'EXEC', 1, [
            {**finding, 'summary': 'shadow regression', 'needs_confirmation': True}])
        major, shadow = co.state['finding_ledger']
        env = {'FAKE_CONFIRM_SHADOW': 'non_blocking', 'FAKE_EXEC_NON_BLOCKING': 'ordinary blocker'}
        with mock.patch.dict(os.environ, env):
            co.reviewer_turn()
        verdict = co.state['review_verdicts'][-1]
        self.assertEqual((verdict['reviewer_raw_verdict'], verdict['effective_verdict']), ('APPROVE', 'REVISE'))
        self.assertEqual(co.state['next'], 'author')
        self.assertEqual(co.blocking_open_findings(), [major])
        self.assertEqual(major['status_history'][-1]['status'], 'still_open')
        self.assertFalse(json.loads(co.state['delivered_review'])['prior_findings'][0]['fixed'])
        self.assertEqual(co.state['stop_rules']['still_open'], [major['id']])

        # The original answer can be replayed after the ledger has already consumed it.
        [review] = [turn for turn in co.state['turns'] if turn['sequence'] == verdict['sequence']]
        history = [list(row['status_history']) for row in (major, shadow)]
        args = (review['answer']['prior_findings'], verdict['sequence'],
                [major['id'], shadow['id']], 'persistent-reviewer')
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(co.apply_dispositions(*args, validate_only=True), [])
            self.assertEqual(co.apply_dispositions(*args), [])
        self.assertEqual([row['status_history'] for row in (major, shadow)], history)
        self.assertEqual(stdout.getvalue(), '')

    def test_r2f7_r4c_counts_the_shadow_s_new_blockers_too(self):
        co = rc.Coordinator(self.args('--lifecycle-mode', 'off'))
        self.assertIsNone(co._update_stop_rules(1, {}, 'APPROVE'))
        reasons = []
        for reviewer in (2, 4):   # each review: no reviewer row, one new shadow blocker (the shadow turn follows the reviewer's)
            [row] = co.record_findings('fresh-shadow', 'EXEC', reviewer + 1, [{'severity': 'MAJOR', 'file': 'a.py',
                                       'summary': f'shadow {reviewer}', 'failure_scenario': 'x', **CORRECT}])
            next(r for r in co.state['finding_ledger'] if r['id'] == row['id'])['source'] = 'fresh-shadow'
            reasons.append(co._update_stop_rules(reviewer, {'prior_findings': []}, 'REVISE'))
        self.assertIsNone(reasons[0])
        self.assertTrue(reasons[1].startswith(rc.NEW_BLOCKERS_REASON), reasons)
        self.assertEqual(co.state['stop_rules']['new_blocking_streak'], 2)

    def test_r2f5_a_saved_v386_state_is_recounted_with_its_gate_fail_baseline(self):
        co = rc.Coordinator(self.args('--lifecycle-mode', 'off'))
        turn = lambda sequence, role, phase, **more: {'sequence': sequence, 'role': role, 'phase': phase, 'answer': {}, **more}
        turns = [turn(1, 'author', 'PLAN'), turn(2, 'reviewer', 'PLAN'), turn(3, 'author', 'EXEC'), turn(4, 'reviewer', 'EXEC'),
                 turn(5, 'gate', 'EXEC', answer={'findings': []}), turn(6, 'author', 'EXEC'), turn(7, 'reviewer', 'EXEC'),
                 turn(8, 'author', 'FINISH'), turn(9, 'reviewer', 'EXEC'),   # v3.8.6 charged the FINISH write a round
                 turn(10, 'author', 'EXEC'), turn(11, 'reviewer', 'POLISH-Q')]   # and the POLISH-Q fix another
        [gate_row] = co.record_findings('adversarial-gate', 'EXEC', 5, [{'severity': 'HIGH', 'file': 'a.py',
                                        'summary': 'gate blocker', 'failure_scenario': 'x'}])
        co.state.update(turns=turns, exec_rounds=4, gate_fail_items=[{'id': gate_row['id'], 'sequence': 5}], sequence=11)
        co.state.pop('round_rules')   # what v3.8.6 saved
        co._settle_saved_rounds()
        self.assertEqual(co.state['exec_rounds'], 2)   # rounds 3 and 6: the FINISH write and the POLISH-Q fix are not counted
        self.assertEqual((co.state['gate_fail_round']['round'], co._exec_limits()), (1, (3, 'R2')))   # at most 1 + 1 after it
        self.assertIsNone(co.state['gate_fail_round'].get('closed_at'))   # its gate item is still open
        co.state['exec_rounds'] = 9
        co._settle_saved_rounds()   # a state of this release is never recounted
        self.assertEqual(co.state['exec_rounds'], 9)

    def test_r3f5_uncredited_author_receipts_do_not_raise_the_legacy_count_or_gate_baseline(self):
        co = rc.Coordinator(self.args('--lifecycle-mode', 'off'))
        turns = [
            {'sequence': seq, 'role': role, 'phase': 'EXEC', 'answer': answer}
            for seq, role, answer in (
                (1, 'author', {'status': 'HOLD'}),
                (2, 'author', {'status': 'READY'}),  # rejected tree, retried before reaching review
                (3, 'author', {'status': 'READY'}),
                (4, 'reviewer', {'status': 'APPROVE'}),
                (5, 'gate', {'findings': []}),
                (6, 'author', {'status': 'HOLD'}),
                (7, 'author', {'status': 'READY'}),
                (8, 'reviewer', {'status': 'REVISE'}),
            )
        ]
        [gate_row] = co.record_findings('adversarial-gate', 'EXEC', 5, [{'severity': 'HIGH', 'file': 'a.py',
                                        'summary': 'gate blocker', 'failure_scenario': 'x'}])
        co.state.update(turns=turns, exec_rounds=3, gate_fail_items=[{'id': gate_row['id'], 'sequence': 5}], sequence=8)
        co.state.pop('round_rules')
        co._settle_saved_rounds()
        self.assertEqual((co.state['exec_rounds'], co.state['gate_fail_round']['round'], co._exec_limits()),
                         (2, 1, (3, 'R2')))

    def test_r3_the_hard_cap_guards_a_state_whose_round_count_ran_past_it(self):
        co = rc.Coordinator(self.args('--lifecycle-mode', 'off'))
        co.state.update(exec_rounds=5, gate_fail_round={'round': 5, 'extra': 0, 'sequence': 9})   # e.g. v3.8.x stage writes
        self.assertEqual(co._exec_limits(), (5, 'R3'))
        self.assertEqual(co._exec_limit_reason(), rc.HARD_CAP_REASON)
        co.state['round_extensions'] = [{'phase': 'EXEC', 'n': 2, 'hold_reason': rc.HARD_CAP_REASON}]
        self.assertEqual(co._exec_limits(), (7, 'R3'))   # an extension lands on top of the hard cap too

    def test_r2f6_the_hard_cap_is_five_whatever_the_pre_gate_cap_says(self):
        co = rc.Coordinator(self.args('--lifecycle-mode', 'off', '--max-exec-rounds', '4'))
        co.state.update(exec_rounds=4, gate_fail_round={'round': 4, 'extra': 0, 'sequence': 9})   # a gate FAIL in round 4
        self.assertEqual(co._exec_limits(), (5, 'R3'))
        self.run_dir = self.root / 'cap7'
        result = self.run_coordinator(*OFF, '--adversarial-gate', 'off', '--max-exec-rounds', '7',
                                      env={'FAKE_EXEC_MIXED_REVISE': '1'})
        self.assertIn('HOLD: ' + rc.HARD_CAP_REASON, result.stdout, result.stdout + result.stderr)
        self.assertIn('NOTE: round rule R3 stopped the run', result.stdout)
        self.assertEqual(self.state()['exec_rounds'], 5)

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


    def attach(self, argv, name):
        log = self.root / (name + '.log')
        log.write_text('OK\n')
        return self.run_operator_action('attach-verification', *argv, '--command', 'python3 -m unittest', '--exit-code', '0',
                                        '--log', str(log), '--log-sha256', rc.hashlib.sha256(log.read_bytes()).hexdigest(),
                                        '--note', 'host run')

    def test_r2f10_a_gate_needing_a_host_record_for_the_current_tree_holds_before_dispatch(self):
        argv = ('--lifecycle-mode', 'off', '--reviewer-vendor', 'codex', '--gate-vendor', 'codex', '--shadow', 'off')
        held = self.run_coordinator(*argv, '--max-invocations', '5', env={'FAKE_REVIEW_TEST_FAILURE': '1'})
        self.assertIn('HOLD: ' + rc.TEST_EVIDENCE_HOLD, held.stdout, held.stdout + held.stderr)
        self.assertEqual(self.state()['invocations_used'], 4)
        self.assertIn('VERIFICATION: V001', self.attach(argv, 'v1').stdout)
        stopped = self.run_operator_action('resume', *argv)   # the reviewer approves on V001, then the budget stops it
        self.assertIn('HOLD: invocation limit reached', stopped.stdout, stopped.stdout + stopped.stderr)
        self.assertEqual((self.state()['next'], gates(self.state())), ('gate', []))
        (self.workspace / 'notes.txt').write_text('edited after the approval\n')   # V001 is not bound to this tree
        again = self.run_operator_action('resume', *argv, '--max-invocations', '25')
        self.assertIn('HOLD: ' + rc.TEST_EVIDENCE_HOLD, again.stdout, again.stdout + again.stderr)
        self.assertEqual(gates(self.state()), [])   # the gate was not dispatched
        self.assertIn('VERIFICATION: V002', self.attach(argv, 'v2').stdout)
        done = self.run_operator_action('resume', *argv, '--max-invocations', '25')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        self.assertEqual(len(gates(self.state())), 1)

    def test_r3f10_advisory_approval_binds_host_evidence_before_the_gate(self):
        argv = ('--lifecycle-mode', 'off', '--reviewer-vendor', 'codex', '--gate-vendor', 'codex', '--shadow', 'off')
        env = {'FAKE_EXEC_MINOR_REVISE': '1', 'FAKE_MISSING_OBSERVED': '1'}
        held = self.run_coordinator(*argv, '--max-invocations', '5', env=env)
        self.assertIn('HOLD: ' + rc.TEST_EVIDENCE_HOLD, held.stdout, held.stdout + held.stderr)
        self.assertIn('VERIFICATION: V001', self.attach(argv, 'advisory').stdout)
        stopped = self.run_operator_action('resume', *argv, env=env)
        self.assertIn('HOLD: invocation limit reached', stopped.stdout, stopped.stdout + stopped.stderr)
        approved = self.state()
        verdict = approved['review_verdicts'][-1]
        self.assertEqual(verdict['effective_verdict'], 'APPROVE_WITH_ADVISORY')
        self.assertEqual((approved['next'], gates(approved)), ('gate', []))
        (self.workspace / 'notes.txt').write_text('edited after the advisory approval\n')
        resumed = self.run_operator_action('resume', *argv, '--max-invocations', '25')
        self.assertEqual(gates(self.state()), [], resumed.stdout + resumed.stderr)
        self.assertIn('HOLD: ' + rc.TEST_EVIDENCE_HOLD, resumed.stdout, resumed.stdout + resumed.stderr)
        [receipt] = [turn for turn in approved['turns'] if turn['sequence'] == verdict['sequence']]
        self.assertEqual(receipt['operator_test_evidence'], 'V001')

if __name__ == '__main__':
    unittest.main()
