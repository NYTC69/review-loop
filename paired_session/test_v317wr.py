"""V317: whole writer rollback preserves new blockers on untouched original code."""
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
        self.assertEqual(row['source'], {'reviewer': 'persistent-reviewer', 'shadow': 'fresh-shadow',
                                         'gate': 'adversarial-gate'}[source])
        self.assertEqual(row['status'], 'fixed')
        self.assertNotIn('withdrawn', [h['status'] for h in row['status_history']])
        self.assertFalse(any(r.get('role') == 'writer-rollback' for r in state['lifecycle']['receipts']))
        [author] = [t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC']
        prompt = (self.run_dir / 'evidence' / f"{author['sequence']:03d}-exec-author.prompt.txt").read_text()
        self.assertIn(row['id'], prompt)
        self.assertIn('sum_ints.py', prompt)
        self.assertIn('type(x) is int', (self.workspace / 'sum_ints.py').read_text())
        self.assertFalse((self.workspace / 'writer_note.py').exists())
        self.assertEqual(marker['simplifier']['state'], 'rolled-back:review')
        self.assertTrue(marker['simplifier']['test']['passed'])
        self.assertEqual((state['exec_rounds'], state['writer_replay_rounds']), (3, {'start': 1, 'used': 2}))
        after = [(t['role'], t['phase']) for t in state['turns'] if t['sequence'] > author['sequence']]
        for step in (('reviewer', 'EXEC'), ('shadow', 'EXEC'), ('gate', 'EXEC'), ('author', 'FINISH')):
            self.assertIn(step, after)
        self.assertEqual([r['stage'] for r in state['lifecycle']['receipts']][-2:], ['DOCS', 'SECURITY'])

    def test_reviewer_blocker_on_original_code_is_delivered_and_fixed(self):
        self.repaired('reviewer')

    def test_shadow_blocker_on_original_code_is_delivered_and_fixed(self):
        self.repaired('shadow')

    def test_gate_blocker_on_original_code_is_delivered_and_fixed(self):
        self.repaired('gate')

    def test_retained_blocker_at_round_limit_holds_before_author(self):
        self.change()
        rollback = rc.Coordinator._writer_review_rollback

        def at_limit(co, source, sequence):
            if co.state['lifecycle'].get('writer_replay'):
                co.state['exec_rounds'] = co.exec_round_limit()
            return rollback(co, source, sequence)

        with mock.patch.object(rc.Coordinator, '_writer_review_rollback', at_limit):
            co, status = self.drive(FAKE_WRITER_WRITE='simplifier', FAKE_WRITER_FILE='writer_note.py',
                                    FAKE_WRITER_BLOCK_SOURCE='reviewer')
        self.assertEqual((status, co.state['hold_reason']), ('HOLD', 'EXEC round limit reached'))
        self.assertEqual(co.state['next'], 'author')
        [row] = co.blocking_open_findings()
        self.assertIn(row['id'], co.state['delivered_review'])
        self.assertFalse(any(t['role'] == 'author' and t['phase'] == 'EXEC' for t in co.state['turns']))
        self.assertFalse(any(r.get('role') == 'writer-rollback' for r in co.state['lifecycle']['receipts']))

    def test_stop_after_gate_route_still_repairs_original_code(self):
        # Writers only run with lifecycle on; off retains the ordinary EXEC repair path.
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
