"""ADVFIX (owner 2026-10-06, "加一轮修非阻塞"; field run ws28 changed no code with 14 MINOR/LOW findings open): with
--advisory-fix-round, the first clean POLISH-Q exit gives the author one fix round for the open non-blocking findings,
then the normal chain (EXEC reviewer, shadow, gate, FINISH, specialists), once per run, before the quality writers, on
its own round outside --max-exec-rounds."""
import json
import unittest

from paired_session import test_worktree_lifecycle as twl
from paired_session import worktree_lifecycle as wl

rc, DONE = twl.rc, twl.DONE
RUN = ('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '80', '--max-exec-rounds', '1')
ADVISORY = {'FAKE_EXEC_MINOR_REVISE': '1'}   # every EXEC review leaves a MINOR finding; the final round lets it pass


class AdvisoryFixRoundTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'git')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def run_review_only(self, *extra):
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        done = self.run_coordinator(*RUN, *extra, env=ADVISORY)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        return self.state()

    def advisory_prompts(self):
        return [p for p in (self.run_dir / 'evidence').glob('*-exec-author.prompt.txt') if 'Advisory fix round' in p.read_text()]

    def test_off_by_default_changes_nothing(self):
        state = self.run_review_only()
        self.assertNotIn('advisory_fix', state)
        self.assertEqual([row['stage'] for row in state['lifecycle']['receipts']], ['FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])
        self.assertEqual([t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC'], [])
        self.assertTrue(state['config']['advisory_fix_round'] is False)
        self.assertEqual(wl.advisory_report_line(state), '- 非阻塞修复轮（advisory fix round）：未开启')

    def test_one_round_runs_with_a_re_review_and_never_a_second(self):
        state = self.run_review_only('--advisory-fix-round', 'true')
        record = state['advisory_fix']
        self.assertEqual(record['state'], 'ran')
        self.assertTrue(record['findings'])
        [prompt] = self.advisory_prompts()   # exactly one advisory author turn
        text = prompt.read_text()
        for finding_id in record['findings']:
            self.assertIn(finding_id, text)   # the open non-blocking findings are listed
        self.assertIn('give a one-line reason in body (dismissed)', text)
        self.assertEqual([row['stage'] for row in state['lifecycle']['receipts']],
                         ['FINISH', 'POLISH-Q', 'FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])   # the re-review chain, then on
        author = next(t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC')
        after = [(t['role'], t['phase']) for t in state['turns'] if t['sequence'] > author['sequence']]
        for step in (('reviewer', 'EXEC'), ('shadow', 'EXEC'), ('gate', 'EXEC'), ('author', 'FINISH')):
            self.assertIn(step, after)
        self.assertEqual(state['exec_rounds'], 2)   # max 1 + its own round: no round-limit HOLD
        self.assertTrue(any(row['status'] == 'open' and row['severity'] == 'MINOR' for row in state['finding_ledger']))   # stays advisory
        self.assertTrue(wl.advisory_report_line(state).startswith('- 非阻塞修复轮（advisory fix round）：已运行（'))

    def test_a_pure_advisory_revise_after_the_round_never_opens_a_second_one(self):   # ordinary rounds left (max 4)
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        mark = self.root / 'advisory-ran'
        done = self.run_coordinator('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '80',
                                    '--advisory-fix-round', 'true',
                                    env={'FAKE_EXEC_APPROVE_MINOR': '1', 'FAKE_AUTHOR_ADVISORY_TOUCH': str(mark),
                                         'FAKE_EXEC_MINOR_REVISE_IF_FILE': str(mark)})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertTrue(mark.exists())   # the round ran; its re-review was a pure-advisory REVISE
        self.assertEqual(len([t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC']), 1)
        verdicts = [row['effective_verdict'] for row in state['review_verdicts'] if row['phase'] == 'EXEC']
        self.assertEqual(verdicts[-1], 'APPROVE_WITH_ADVISORY')
        self.assertEqual(state['exec_rounds'], 2)   # 1 + the round; 3 ordinary rounds were still left, none used

    def test_the_round_then_a_writer_replay_both_run_on_their_own_rounds(self):   # with D09 quality writers
        (self.workspace / 'new_code.py').write_text(''.join(f'VALUE_{n} = {n}\n' for n in range(30)))   # the fake author
        (self.workspace / 'tests').mkdir()                                                             # edits sum_ints.py
        (self.workspace / 'tests' / 'test_new_code.py').write_text('import new_code\n')
        (self.workspace / 'test.py').write_text('import unittest\n\n\nclass Smoke(unittest.TestCase):\n'
                                                '    def test_ok(self):\n        self.assertTrue(True)\n')
        done = self.run_coordinator('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '100',
                                    '--max-exec-rounds', '1', '--advisory-fix-round', 'true',
                                    env={'FAKE_EXEC_APPROVE_MINOR': '1', 'FAKE_WRITER_WRITE': 'simplifier',
                                         'FAKE_WRITER_FILE': 'writer_note.py'})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertEqual(state['advisory_fix']['state'], 'ran')
        self.assertEqual(state['lifecycle']['quality_writers']['simplifier']['state'], 'wrote')
        # the round first (epoch 1), then the writers on the fixed tree and their replay (epoch 2)
        self.assertEqual([row['stage'] for row in state['lifecycle']['receipts']],
                         ['FINISH', 'POLISH-Q', 'FINISH', 'POLISH-Q', 'POLISH-Q', 'POLISH-Q', 'FINISH', 'POLISH-Q', 'DOCS',
                          'SECURITY'])
        self.assertEqual((state['advisory_fix']['epoch'], state['lifecycle']['epoch']), (1, 2))
        self.assertEqual((state['exec_rounds'], state['writer_replay_rounds']), (3, {'start': 2, 'used': 1}))
        self.assertTrue((self.workspace / 'writer_note.py').is_file())

    def test_no_headroom_or_no_findings_skips_with_the_reason_once(self):
        (self.workspace / 'sum_ints.py').write_text('X = 1\n')
        co = rc.Coordinator(rc.parser().parse_args(self.command(*RUN, '--advisory-fix-round', 'true')[2:]))
        self.assertFalse(co._advisory_fix_round(4))
        self.assertEqual(co.state['advisory_fix'], {'state': 'skipped', 'reason': 'no open non-blocking findings'})
        co.state.pop('advisory_fix')
        co.record_findings('persistent-reviewer', 'EXEC', 1, [
            {'severity': 'MINOR', 'file': 'sum_ints.py', 'summary': 'naming', 'failure_scenario': 'x'}])
        co.state['invocations_used'] = 80 - co.state.get('q_reserved', 0) - 8   # one short of 1 author + 4 + 4 specialists
        self.assertFalse(co._advisory_fix_round(4))
        self.assertEqual(co.state['advisory_fix']['state'], 'skipped')
        self.assertIn('需要 9 次调用，剩余 8 次', co.state['advisory_fix']['reason'])
        self.assertIn('已跳过（需要 9 次调用', wl.advisory_report_line(co.state))
        co.state['invocations_used'] = 0
        self.assertFalse(co._advisory_fix_round(4))   # once per run: a recorded skip is final

    def test_report_mode_refuses_the_option(self):
        (self.workspace / 'sum_ints.py').write_text('X = 1\n')
        with self.assertRaisesRegex(ValueError, '--review-report refuses --advisory-fix-round true'):
            rc.Coordinator(rc.parser().parse_args(self.command(*RUN, '--review-report', '--advisory-fix-round', 'true')[2:]))


if __name__ == '__main__':
    unittest.main()
