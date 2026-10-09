"""v2.9.7 OPV: a record is voided by every tree the coordinator observed in an author turn, failed turns included.

v2.9.5 known limitation (Codex MEDIUM, opv R1 L5): an author turn that changed the tree and then failed (CLI non-zero)
did not void the record; once the tree was restored, the old record was shown again."""
import json
import os
import types
import unittest
from unittest.mock import patch

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import operator_verification as opv
from paired_session import test_operator_verification as tov
from paired_session import test_real_coordinator as trc

rc = trc.rc


class FailedAuthorTurnTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in tov._HELPERS})
    log = tov.OperatorVerificationTests.log
    attach = tov.OperatorVerificationTests.attach

    def exec_coordinator(self):
        co = self.coordinator()
        co.state.update(phase='EXEC', next='author')
        co.save()
        return co

    def shown(self, co):
        return opv.prompt_block(co, rc.git_snapshot(self.workspace)[0], rc.atomic_json)

    def test_a_failed_turn_that_changed_the_tree_voids_the_record_even_after_the_tree_is_restored(self):
        co = self.exec_coordinator()
        self.attach(co)
        tree = rc.git_snapshot(self.workspace)[0]
        module = self.workspace / 'sum_ints.py'
        original = module.read_text() if module.exists() else None
        with patch.dict(os.environ, {'FAKE_AUTHOR_FAIL_AFTER_WRITE': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'CLI exit 1'):
                co.invoke('author', 'EXEC', co._author_prompt(), rc.author_schema())
        failed = co.state['turns'][-1]
        self.assertEqual((failed['role'], failed['snapshot_before']), ('author', tree))
        self.assertNotEqual(failed['snapshot_after'], tree)                  # the failed turn changed the tree
        if original is None: module.unlink()
        else: module.write_text(original)                                   # ... and the tree is restored
        self.assertEqual(rc.git_snapshot(self.workspace)[0], tree)
        row = co.state['operator_verifications'][0]
        self.assertEqual((row['status'], row['voided']['reason'], row['voided']['tree_seen']), ('voided', 'tree changed', failed['snapshot_after']))
        self.assertEqual(self.shown(co), '')                                 # the old record is not shown again
        self.assertEqual(json.loads((self.run_dir / 'evidence' / 'operator-verification-V001.json').read_text())['status'], 'voided')

    def test_an_unknown_tree_voids_the_record_when_a_child_ran_without_a_recorded_turn(self):
        use_lifecycle_on(self, self)
        co = self.exec_coordinator()
        self.attach(co)
        def interrupted(role, phase, fresh, call):
            co.state['active'] = {'sequence': co.state['sequence'] + 1, 'role': 'author', 'pid': 99999}   # the child ran; no end snapshot
            raise KeyboardInterrupt
        co._progress_dispatch = interrupted
        with self.assertRaises(KeyboardInterrupt):
            co.invoke('author', 'EXEC', 'p', {})
        row = co.state['operator_verifications'][0]
        self.assertEqual((row['status'], row['voided']['tree_seen']), ('voided', None))

    def test_a_recorded_failed_turn_without_an_end_snapshot_voids_on_an_unknown_tree(self):
        co = self.exec_coordinator()
        self.attach(co)
        tree = rc.git_snapshot(self.workspace)[0]
        def control_problem(role, phase, fresh, call):   # as _invoke_once records a turn whose git control files changed
            co.state['sequence'] += 1
            co.state['turns'].append({'sequence': co.state['sequence'], 'role': 'author', 'snapshot_before': tree,
                                      'error': 'author changed git control files: HEAD'})
            raise RuntimeError('author changed git control files: HEAD')
        co._progress_dispatch = control_problem
        with self.assertRaisesRegex(RuntimeError, 'git control files'):
            co.invoke('author', 'EXEC', 'p', {})
        row = co.state['operator_verifications'][0]
        self.assertEqual((row['status'], row['voided']['tree_seen']), ('voided', None))

    def test_a_resume_after_the_coordinator_was_killed_during_an_author_turn_voids_the_record(self):
        co = self.exec_coordinator()
        self.attach(co)
        co.state['active'] = {'sequence': co.state['sequence'] + 1, 'role': 'author', 'workspace': str(self.workspace), 'pid': 99999}
        co.save()                                                             # the coordinator died; the child's tree is unknown
        self.assertEqual(rc.Coordinator(co.args).resume(), 'HOLD')
        row = json.loads((self.run_dir / 'state.json').read_text())['operator_verifications'][0]
        self.assertEqual((row['status'], row['voided']['tree_seen']), ('voided', None))

    def test_a_run_or_abort_first_after_the_kill_voids_the_record_too(self):
        for action in ('run', 'abort'):
            with self.subTest(action=action):
                self.run_dir = self.root / ('killed-' + action)
                co = self.exec_coordinator()
                self.attach(co)
                co.state['active'] = {'sequence': co.state['sequence'] + 1, 'role': 'author', 'workspace': str(self.workspace), 'pid': 99999}
                co.save()
                restored = rc.Coordinator(co.args)
                if action == 'run': self.assertEqual(restored.drive(), 'HOLD')
                else: restored.hold('aborted by operator')
                row = json.loads((self.run_dir / 'state.json').read_text())['operator_verifications'][0]
                self.assertEqual((row['status'], row['voided']['tree_seen']), ('voided', None))

    def test_a_killed_probe_turn_in_its_own_tree_keeps_the_record(self):
        co = self.exec_coordinator()
        self.attach(co)
        co.state['active'] = {'sequence': co.state['sequence'] + 1, 'role': 'author', 'workspace': str(self.root / 'probe-tree'), 'pid': 99999}
        co.save()
        self.assertEqual(rc.Coordinator(co.args).resume(), 'HOLD')
        self.assertEqual(json.loads((self.run_dir / 'state.json').read_text())['operator_verifications'][0]['status'], 'current')

    def test_a_refusal_before_any_child_ran_keeps_the_record(self):
        use_lifecycle_on(self, self)
        co = self.exec_coordinator()
        self.attach(co)
        def refused(role, phase, fresh, call):
            raise RuntimeError('invocation limit reached')
        co._progress_dispatch = refused
        with self.assertRaisesRegex(RuntimeError, 'invocation limit'):
            co.invoke('author', 'EXEC', 'p', {})
        self.assertEqual(co.state['operator_verifications'][0]['status'], 'current')
        self.assertIn('Operator-verified evidence for this exact tree', self.shown(co))

    def test_a_turn_that_started_on_another_tree_voids_the_record(self):
        co = self.exec_coordinator()
        self.attach(co)
        tree = rc.git_snapshot(self.workspace)[0]
        def recorded(role, phase, fresh, call):   # a turn the coordinator saw start on tree X and end on the record's tree
            co.state['sequence'] += 1
            co.state['turns'].append({'sequence': co.state['sequence'], 'role': 'author', 'snapshot_before': 'x' * 64, 'snapshot_after': tree})
            return {'snapshot': tree, 'answer': {}, 'sequence': co.state['sequence']}
        co._progress_dispatch = recorded
        co.invoke('author', 'EXEC', 'p', {})
        row = co.state['operator_verifications'][0]
        self.assertEqual((row['status'], row['voided']['tree_seen']), ('voided', 'x' * 64))


if __name__ == '__main__':
    unittest.main()
