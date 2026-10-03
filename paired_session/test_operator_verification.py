"""Batch OPV (v2.9.5): operator-run verification evidence bound to the exact workspace tree."""
import hashlib
import json
import types
import unittest

from paired_session import operator_verification as opv
from paired_session import test_real_coordinator as trc

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


if __name__ == '__main__':
    unittest.main()
