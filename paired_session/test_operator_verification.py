"""Batch OPV (v2.9.5): operator-run verification evidence bound to the exact workspace tree."""
import hashlib
import json
from pathlib import Path
import types
import unittest

from paired_session import operator_verification as opv
from paired_session import test_real_coordinator as trc
from paired_session import test_worktree_lifecycle as twl

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_operator_action', 'coordinator')
HEADER = 'Operator-verified evidence for this exact tree'


class OperatorVerificationTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def log(self, text='Test Suite All tests passed\n** TEST SUCCEEDED **\n'):
        path = self.root / 'xcodebuild.log'
        path.write_text(text)
        return path, hashlib.sha256(text.encode()).hexdigest()

    def attach_cli(self, *extra, note='ran xcodebuild test on the host simulator', log=None):
        path, digest = log or self.log()
        return self.run_operator_action('attach-verification', '--command', 'xcodebuild test -scheme App', '--exit-code', '0',
                                        '--log', str(path), '--log-sha256', digest, *(('--note', note) if note is not None else ()), *extra)

    def attach(self, co, log_text=None, **over):
        path, digest = self.log(log_text) if log_text else self.log()
        args = types.SimpleNamespace(**{'command': 'xcodebuild test -scheme App', 'cwd': None, 'exit_code': 0, 'log': str(path),
                                        'log_sha256': digest, 'note': 'ran on the host', **over})
        return opv.attach(co, args, rc.git_snapshot(self.workspace)[0], rc.atomic_json)

    def prompts(self, co):
        digest = rc.git_snapshot(self.workspace)[0]
        co.state['phase'] = 'EXEC'
        return {role: co._review_prompt(role, digest) for role in ('reviewer', 'shadow')} | {'gate': co._gate_prompt(digest)}

    def test_the_finisher_sees_a_current_record_and_the_sandbox_rule_and_nothing_without_one(self):   # FIELD poker-tools v3.7.1
        co = self.coordinator()
        self.assertEqual(co._finish_operator_evidence(), '')   # no record: the FINISH prompt keeps its bytes
        self.attach(co)
        tree = rc.git_snapshot(self.workspace)[0]
        block = co._finish_operator_evidence()
        self.assertIn(HEADER, block)
        self.assertIn(f'(snapshot {tree})', block)
        self.assertIn('command `xcodebuild test -scheme App`', block)
        self.assertTrue(block.endswith(rc.FINISH_OPERATOR_TEST_RULE))
        (self.workspace / 'later.txt').write_text('a later change\n')   # another tree: the record is withdrawn
        self.assertEqual(co._finish_operator_evidence(), '')
        self.assertEqual(co.state['operator_verifications'][0]['status'], 'voided')

    def test_a_record_binds_to_the_tree_and_reaches_reviewer_shadow_and_gate_prompts_scan_clean(self):
        self.coordinator()
        done = self.attach_cli()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        co = self.coordinator()
        tree = rc.git_snapshot(self.workspace)[0]
        row = co.state['operator_verifications'][0]
        self.assertIn(f'VERIFICATION: V001 bound to tree {tree}', done.stdout)
        self.assertEqual((row['tree_sha256'], row['status'], row['actor'], row['exit_code'], row['cwd']), (tree, 'current', 'operator', 0, str(self.workspace)))
        self.assertEqual(hashlib.sha256((self.run_dir / 'evidence' / 'operator-verification-V001.log').read_bytes()).hexdigest(), row['log_sha256'])
        self.assertEqual(json.loads((self.run_dir / 'evidence' / 'operator-verification-V001.json').read_text()), row)
        self.assertEqual((co.state['status'], co.state['phase'], co.state['next'], co.state['finding_ledger']), ('ACTIVE', 'PLAN', 'author', []))   # no verdict input changes
        for role, prompt in self.prompts(co).items():
            with self.subTest(role=role):
                self.assertIn(HEADER, prompt)
                self.assertIn(f'(snapshot {tree})', prompt)
                self.assertIn('command `xcodebuild test -scheme App`', prompt)
                self.assertIn('    | ** TEST SUCCEEDED **', prompt)
                self.assertIn('It is not a decision', prompt)
                self.assertNotIn('host simulator', prompt)                   # the operator note stays in state and evidence
                if role != 'reviewer':
                    co.assert_fresh_prompt(role, prompt)                     # the fresh-role history scan passes
        co.state['phase'] = 'PLAN'
        self.assertNotIn(HEADER, co._review_prompt('reviewer', tree))     # the plan review has no tree to verify

    def test_a_tree_change_voids_the_record_for_good(self):
        co = self.coordinator()
        self.attach(co)
        tracked = self.workspace / 'tracked.txt'
        tracked.write_text('changed\n')
        self.assertFalse(any(HEADER in prompt for prompt in self.prompts(co).values()))
        tracked.write_text('base\n')                                        # the same tree again: a voided record stays void
        self.assertFalse(any(HEADER in prompt for prompt in self.prompts(co).values()))
        self.assertEqual((co.state['operator_verifications'][0]['status'], co.state['operator_verifications'][0]['voided']['reason']), ('voided', 'tree changed'))
        self.assertEqual(json.loads((self.run_dir / 'evidence' / 'operator-verification-V001.json').read_text())['status'], 'voided')
        self.attach(co)                                                      # an author turn: unchanged tree keeps it, a changed or unknown tree voids it
        tree = rc.git_snapshot(self.workspace)[0]
        co._progress_dispatch = lambda role, phase, fresh, call: {'snapshot': tree, 'answer': {}, 'sequence': 1}
        co.invoke('author', 'EXEC', 'p', {})
        self.assertEqual(co.state['operator_verifications'][1]['status'], 'current')
        co._progress_dispatch = lambda role, phase, fresh, call: {'snapshot': 'probe-tree', 'answer': {}, 'sequence': 2}
        co.invoke('author', 'PLAN', 'p', {}, workspace_override=self.root)   # a probe turn in its own tree leaves the record alone
        self.assertEqual(co.state['operator_verifications'][1]['status'], 'current')
        co._progress_dispatch = lambda role, phase, fresh, call: {'snapshot': None, 'answer': {}, 'sequence': 2}
        co.invoke('author', 'EXEC', 'p', {})
        self.assertEqual(co.state['operator_verifications'][1]['status'], 'voided')
        self.attach(co)                                                      # an altered log copy voids it too
        (self.run_dir / 'evidence' / 'operator-verification-V003.log').write_text('** TEST SUCCEEDED ** (edited)\n')
        self.assertFalse(any(HEADER in prompt for prompt in self.prompts(co).values()))
        self.assertEqual(co.state['operator_verifications'][2]['voided']['reason'], 'log copy changed')

    def test_passing_test_record_lookup(self):
        co = self.coordinator()
        tree = rc.git_snapshot(self.workspace)[0]
        invokes = lambda command: command.strip() == 'xcodebuild test -scheme App'
        find = lambda digest: opv.passing_test_record(co, digest, invokes, rc.atomic_json)
        self.attach(co)
        self.attach(co)
        self.assertEqual(find(tree)['id'], 'V002')                          # the newest qualifying record
        self.assertIsNone(find(None))                                        # an unknown tree finds nothing and voids nothing
        self.assertEqual({row['status'] for row in co.state['operator_verifications']}, {'current'})
        self.attach(co, exit_code=1)
        self.attach(co, command='xcodebuild build -scheme App')
        self.assertEqual(find(tree)['id'], 'V002')                          # a non-zero exit or another command never qualifies
        self.assertIsNone(find('0' * 64))                                    # another tree: nothing, and the records are voided
        self.assertEqual({row['status'] for row in co.state['operator_verifications']}, {'voided'})

    def test_attach_is_refused_while_a_turn_is_active_in_done_and_without_a_run(self):
        self.assertIn('REFUSED: attach-verification requires an existing coordinator run', self.attach_cli().stdout)
        co = self.coordinator()
        for key, value in (('active', {'sequence': 1}), ('uncertain_active', {'sequence': 1}), ('status', 'ACCEPTED')):   # N4-e: DONE is allowed now
            with self.subTest(**{key: value}):
                saved = co.state.get(key)
                co.state[key] = value
                co.save()
                done = self.attach_cli()
                self.assertEqual(done.returncode, 2)
                self.assertIn('REFUSED: attach-verification requires an idle ACTIVE, HOLD or DONE run', done.stdout)
                co.state[key] = saved
                co.save()
        co.state['status'] = 'HOLD'
        co.save()
        self.assertEqual(self.attach_cli().returncode, 0)                   # idle HOLD is allowed

    def test_attach_refuses_an_empty_note_a_hash_mismatch_and_bad_paths(self):
        co = self.coordinator()
        path, _ = self.log()
        for label, over, message in (('empty note', {'note': '  \n'}, 'non-empty --note'), ('no note', {'note': None}, 'non-empty --note'),
                                     ('sha256 mismatch', {'log_sha256': '0' * 64}, 'log sha256 mismatch'),
                                     ('log in workspace', {'log': str(self.workspace / 'tracked.txt')}, 'outside the workspace and run dir'),
                                     ('cwd outside', {'cwd': str(self.root)}, '--cwd must be the workspace'),
                                     ('ledger id in log', {'log_text': 'fixes F001\n'}, 'fresh-role input scan'),
                                     ('vendor claim in log', {'log_text': 'Codex approved this\n'}, 'fresh-role input scan')):
            with self.subTest(label):
                with self.assertRaisesRegex(ValueError, message):
                    self.attach(co, **over)
        self.assertNotIn('operator_verifications', co.state)
        done = self.attach_cli(note='')
        self.assertEqual(done.returncode, 2)
        self.assertIn('REFUSED: attach-verification needs', done.stdout)
        self.assertEqual(self.attach_cli(log=(path, 'f' * 64)).returncode, 2)

class DocsCarryOverTests(unittest.TestCase):
    """v3.7.2 (field: range-build-tool V002-V004): a DOCS writer change limited to DOCS allowlist paths keeps the operator's
    record current, so a DOCS reviewer whose sandbox cannot run the configured test does not HOLD; any other change voids it."""
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in twl._HELPERS})
    setUp = twl.WorktreeLifecycleActivationTests.setUp

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def held_at_docs_with_a_record(self):
        held = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_DOCS_HOLD': '1'})   # the writer holds without a change
        self.assertIn('HOLD: docs writer: Fake docs writer held.', held.stdout, held.stdout + held.stderr)
        tree = rc.git_snapshot(self.workspace)[0]
        self.assertEqual(tree, self.state()['lifecycle']['candidate_oid'])
        log = self.root / 'host-test.log'
        log.write_text('Ran 12 tests\nOK\n')
        attached = self.run_operator_action('attach-verification', '--lifecycle-mode', 'on', '--command', 'python3 -m unittest',
                                            '--exit-code', '0', '--log', str(log), '--log-sha256',
                                            hashlib.sha256(log.read_bytes()).hexdigest(), '--note', 'ran on the host')
        self.assertIn(f'VERIFICATION: V001 bound to tree {tree}', attached.stdout, attached.stdout + attached.stderr)
        return tree

    def test_a_docs_only_write_carries_the_record_and_settles_the_docs_test_check(self):
        old = self.held_at_docs_with_a_record()
        env = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_REVIEW_NO_TEST_ONCE': str(self.root / 'no-test')}
        done = self.run_operator_action('resume', '--lifecycle-mode', 'on', env=env)
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        self.assertIn('NOTE: operator verification V001 carried over to the DOCS tree (docs-only change: CHANGELOG.md)', done.stdout)
        state, tree = self.state(), rc.git_snapshot(self.workspace)[0]
        docs = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS' and row['status'] == 'READY'][-1]
        self.assertEqual((docs['opv_carried'], docs['review']['operator_test_evidence'], docs['review']['observed_test']),
                         (['V001'], 'V001', None))
        [row] = state['operator_verifications']
        self.assertEqual((row['status'], row['tree_sha256']), ('current', tree))
        [carry] = row['carried']
        self.assertEqual((carry['from_tree'], carry['to_tree'], carry['paths'], carry['by']), (old, tree, ['CHANGELOG.md'], 'docs-writer'))
        prompt = (self.run_dir / 'evidence' / f"{docs['review']['sequence']:03d}-docs-reviewer.prompt.txt").read_text()
        self.assertIn(f'Carried over from tree {old[:12]}: only DOCS allowlist paths changed by the docs writer: CHANGELOG.md',
                      prompt)

    def test_a_docs_finding_fixed_by_the_author_keeps_the_record_for_the_next_docs_review(self):
        old = self.held_at_docs_with_a_record()
        env = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_REVIEW_BLOCK_ONCE': str(self.root / 'docs-block'),
               'FAKE_DOCS_REVIEW_NO_TEST': '1'}   # no docs review runs the test: each relies on the host record
        done = self.run_operator_action('resume', '--lifecycle-mode', 'on', env=env)
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        self.assertIn("carried over to the author's docs-only change (CHANGELOG.md)", done.stdout)
        state, tree = self.state(), rc.git_snapshot(self.workspace)[0]
        first, second = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS' and row['status'] == 'READY']
        self.assertEqual((first['route'], first['review']['operator_test_evidence'], first['opv_carried']),
                         ('EXEC', 'V001', ['V001']))   # the writer carried it to its tree; the docs finding went to the author
        self.assertEqual((second['docs_only'], second['route'], second['review']['operator_test_evidence'],
                          second['review']['observed_test']), (True, 'SECURITY', 'V001', None))
        [row] = state['operator_verifications']
        self.assertEqual((row['status'], row['tree_sha256'], [carry['by'] for carry in row['carried']]),
                         ('current', tree, ['docs-writer', 'author']))
        self.assertEqual((row['carried'][0]['from_tree'], row['carried'][1]['from_tree']),
                         (old, first['output_oid']))   # the author's carry starts from the DOCS tree the review sent back

    def test_a_docs_write_outside_the_allowlist_voids_the_record(self):
        self.held_at_docs_with_a_record()
        env = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md', 'FAKE_DOCS_CODE_ONCE': str(self.root / 'code-once')}
        self.run_operator_action('resume', '--lifecycle-mode', 'on', '--max-invocations', '60', env=env)
        [row] = self.state()['operator_verifications']
        self.assertEqual((row['status'], row['voided']['reason']), ('voided', 'tree changed'))
        self.assertNotIn('carried', row)

    def test_carry_docs_only_moves_only_intact_records_bound_to_the_trees_it_left(self):
        co = self.coordinator()
        tree = rc.git_snapshot(self.workspace)[0]
        log = self.root / 'host.log'
        log.write_text('OK\n')
        args = types.SimpleNamespace(command='python3 -m unittest', cwd=None, exit_code=0, log=str(log),
                                     log_sha256=hashlib.sha256(b'OK\n').hexdigest(), note='host run')
        opv.attach(co, args, tree, rc.atomic_json)
        self.assertEqual(opv.carry_docs_only(co, {'another-tree'}, 'new-tree', ['CHANGELOG.md'], 5, rc.atomic_json), [])
        row = co.state['operator_verifications'][0]
        evidence = Path(row['log_evidence'])
        evidence.write_text('changed\n')                                        # a changed log copy is never carried
        self.assertEqual(opv.carry_docs_only(co, {tree}, 'new-tree', ['CHANGELOG.md'], 5, rc.atomic_json), [])
        evidence.write_text('OK\n')
        self.assertEqual(opv.carry_docs_only(co, {tree}, 'new-tree', ['CHANGELOG.md'], 5, rc.atomic_json), ['V001'])
        self.assertEqual((row['tree_sha256'], row['carried'][0]['sequence']), ('new-tree', 5))
        self.assertEqual(json.loads((co.evidence / 'operator-verification-V001.json').read_text()), row)


if __name__ == '__main__':
    unittest.main()
