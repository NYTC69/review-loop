"""V317: whole writer rollback preserves new blockers on untouched original code."""
import json
import os
import unittest
from unittest import mock

from paired_session import test_d09_c1b2 as d09

rc = d09.rc


class WriterRollbackTests(unittest.TestCase):
    locals().update({name: getattr(d09.D09C1b2Tests, name) for name in
                    (*d09.twl._HELPERS, 'setUp', 'git', 'state')})

    def change(self):
        d09.D09C1b2Tests.change(self)
        (self.workspace / 'sum_ints.py').write_text(
            'def sum_ints(values):\n    if not all(isinstance(x, int) for x in values):\n'
            '        raise TypeError("ints only")\n    return sum(values)\n')
        return rc.git_snapshot(self.workspace)[0]

    def drive(self, **env):
        with mock.patch.dict(os.environ, env):
            co = rc.Coordinator(rc.parser().parse_args(self.command(*d09.RUN)[2:]))
            return co, co.drive()

    def repaired(self, source):
        self.change()
        observed = []
        rollback = rc.Coordinator._writer_review_rollback

        def capture(co, origin, sequence):
            result = rollback(co, origin, sequence)
            if result:
                observed.append((co.state['next'], co.state['lifecycle']['stage'],
                                 [(r['id'], r['status']) for r in co.blocking_open_findings()],
                                 rc.git_snapshot(co.workspace)[0]))
            return result

        with mock.patch.object(rc.Coordinator, '_writer_review_rollback', capture):
            co, status = self.drive(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py',
                                    FAKE_WRITER_BLOCK_SOURCE=source)
        self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
        state = self.state()
        [row] = [r for r in state['finding_ledger'] if r['file'] == 'sum_ints.py']
        marker = state['lifecycle']['quality_writers']
        self.assertEqual(observed, [('author', 'EXEC', [(row['id'], 'open')], marker['base_oid'])])
        self.assertEqual(row['source'], {'reviewer': 'persistent-reviewer', 'shadow': 'fresh-shadow'}[source])
        self.assertEqual(row['status'], 'fixed')
        self.assertNotIn('withdrawn', [h['status'] for h in row['status_history']])
        self.assertFalse(any(r.get('role') == 'writer-rollback' for r in state['lifecycle']['receipts']))
        [author] = [t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC']
        prompt = (self.run_dir / 'evidence' / f"{author['sequence']:03d}-exec-author.prompt.txt").read_text()
        self.assertIn(row['id'], prompt)
        self.assertIn('sum_ints.py', prompt)
        self.assertIn(row['source'], prompt)
        self.assertIn("The quality writers' change was rolled back; these findings are about the current tree.", prompt)
        self.assertIn('type(x) is int', (self.workspace / 'sum_ints.py').read_text())
        self.assertFalse((self.workspace / 'writer_note.py').exists())
        self.assertEqual(marker['simplifier']['state'], 'rolled-back:review')
        self.assertTrue(marker['simplifier']['test']['passed'])
        self.assertEqual((state['exec_rounds'], state['writer_replay_rounds']), (3, {'start': 1, 'used': 2}))
        after = [(t['role'], t['phase']) for t in state['turns'] if t['sequence'] > author['sequence']]
        for step in (('reviewer', 'EXEC'), ('shadow', 'EXEC'), ('author', 'FINISH')):
            self.assertIn(step, after)
        self.assertNotIn(('gate', 'EXEC'), after)   # WI-GATE1: the gate runs once; a writer replay is not gated
        self.assertEqual([r['stage'] for r in state['lifecycle']['receipts']][-2:], ['DOCS', 'SECURITY'])

    def test_reviewer_blocker_on_original_code_is_delivered_and_fixed(self):
        self.repaired('reviewer')

    def test_shadow_blocker_on_original_code_is_delivered_and_fixed(self):
        self.repaired('shadow')

    def test_a_writer_replay_is_never_gated_so_a_gate_cannot_roll_writers_back(self):
        # WI-GATE1: the gate ran once before the writers; their replay goes to the reviewer and shadow only.
        self.change()
        co, status = self.drive(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py',
                                FAKE_WRITER_BLOCK_SOURCE='gate')
        self.assertEqual(status, 'DONE', co.state.get('hold_reason'))
        state = self.state()
        self.assertEqual(len([t for t in state['turns'] if t['role'] == 'gate']), 1)
        self.assertFalse([r for r in state['finding_ledger'] if r['file'] == 'sum_ints.py'])
        self.assertTrue((self.workspace / 'writer_note.py').exists())
        self.assertNotEqual(state['lifecycle']['quality_writers']['simplifier']['state'], 'rolled-back:review')

    def test_retained_blocker_at_round_limit_is_repaired_without_a_post_writer_gate(self):
        # WI-GATE1: the gate ran once before the writers, so the replay ends with the reviewer's approval; no
        # gate after the writers can reach the round limit and ask for an extension.
        self.change()
        flags = (*d09.RUN, '--max-exec-rounds', '2')
        env = {'FAKE_WRITER_WRITE': 'simplifier,test-writer',
               'FAKE_WRITER_FILE_SIMPLIFIER': 'writer_note.py',
               'FAKE_WRITER_FILE_TEST_WRITER': 'tests/test_extra.py',
               'FAKE_EXEC_REVISE_IF_FILE': str(self.workspace / 'tests/test_extra.py'),
               'FAKE_WRITER_BLOCK_SOURCE': 'gate'}
        done = self.run_coordinator(*flags, env=env)
        self.assertIn(d09.DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertEqual(len([t for t in state['turns'] if t['role'] == 'gate']), 1)
        self.assertFalse(state.get('round_extensions'))
        rolled = [r for r in state['finding_ledger'] if r['file'] == 'sum_ints.py']
        self.assertFalse([r for r in rolled if r['source'] == 'adversarial-gate'])
        self.assertTrue(all(r['status'] == 'fixed' for r in rolled))

    def test_resume_between_rollback_and_author_preserves_the_delivered_finding(self):
        self.change()
        env = {'FAKE_WRITER_WRITE': 'simplifier', 'FAKE_WRITER_FILE': 'writer_note.py',
               'FAKE_WRITER_BLOCK_SOURCE': 'reviewer'}
        rollback = rc.Coordinator._writer_review_rollback

        def pause(co, source, sequence):
            result = rollback(co, source, sequence)
            if result and co.state['next'] == 'author':
                co.hold('test pause between rollback and author')
            return result

        with mock.patch.object(rc.Coordinator, '_writer_review_rollback', pause):
            co, status = self.drive(**env)
        self.assertEqual((status, co.state['next']), ('HOLD', 'author'))
        before = self.state()
        [row] = [r for r in before['finding_ledger'] if r['status'] == 'open']
        delivery = json.loads(before['delivered_review'])
        self.assertEqual(delivery['source'], 'persistent-reviewer')   # WI-GATE1: a writer replay is not gated
        self.assertEqual(delivery['findings'][0]['summary'], row['summary'])
        resumed = self.run_operator_action('resume', *d09.RUN, env=env)
        self.assertIn(d09.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        state = self.state()
        after = [t for t in state['turns'] if t['sequence'] > before['sequence']]
        self.assertEqual((after[0]['role'], after[0]['phase']), ('author', 'EXEC'))
        prompt = (self.run_dir / 'evidence' / f"{after[0]['sequence']:03d}-exec-author.prompt.txt").read_text()
        for text in (row['id'], row['summary'], delivery['context']):
            self.assertIn(text, prompt)
        self.assertEqual(next(r for r in state['finding_ledger'] if r['id'] == row['id'])['status'], 'fixed')
        self.assertEqual(state['exec_rounds'], 3)
        self.assertFalse(state.get('round_extensions'))

    def test_stop_after_gate_route_repair_is_unchanged_by_writer_rollback(self):
        # Invariance check: writers only run with lifecycle on; this off-route check also passes without V317.
        done = self.run_coordinator('--lifecycle-mode', 'off', '--shadow', 'off',
                                    env={'FAKE_GATE_BLOCK_ONCE': str(self.root / 'gate-once')})
        self.assertIn(d09.DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertNotIn('lifecycle', state)
        [row] = state['finding_ledger']
        self.assertEqual((row['file'], row['status']), ('sum_ints.py', 'fixed'))
        authors = [t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC']
        self.assertEqual(len(authors), 2)
        prompt = (self.run_dir / 'evidence' / f"{authors[-1]['sequence']:03d}-exec-author.prompt.txt").read_text()
        self.assertIn(row['id'], prompt)


if __name__ == '__main__':
    unittest.main()
