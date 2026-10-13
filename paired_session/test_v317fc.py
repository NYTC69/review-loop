"""V317-FC: driven resume regressions, using only the existing fake provider CLIs."""
import json
import os
import unittest
from unittest import mock

from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE
ON = ('--lifecycle-mode', 'on', '--max-invocations', '80')


class ResumeAccountingTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def assert_done(self, result):
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)

    def stage_limit(self, stage, env):
        flags = (*ON, '--max-exec-rounds', '1')
        held = self.run_coordinator(*flags, env=env)
        reason = 'EXEC round limit reached after a ' + stage + ' write'
        self.assertIn('HOLD: ' + reason, held.stdout, held.stdout + held.stderr)
        before = self.state()
        self.assertEqual((before['exec_rounds'], before['next']), (2, 'reviewer'))
        resumed = self.run_operator_action('resume', *flags, env=env)
        self.assertIn('HOLD: ' + reason, resumed.stdout, resumed.stdout + resumed.stderr)
        self.assertIn('NEXT: resume --add-rounds N', resumed.stdout)
        after = self.state()
        for key in ('sequence', 'turns', 'round_limit_hold', 'lifecycle'):
            self.assertEqual(after[key], before[key])
        self.assertFalse(after.get('round_extensions'))
        self.assert_done(self.run_operator_action('resume', *flags, '--add-rounds', '1', env=env))
        done = self.state()
        self.assertEqual((done['exec_rounds'], done['config']['max_exec_rounds']), (2, 1))
        self.assertEqual([(r['phase'], r['n']) for r in done['round_extensions']], [('EXEC', 1)])
        later = [(t['role'], t['phase']) for t in done['turns'] if t['sequence'] > before['sequence']]
        self.assertIn(('reviewer', 'EXEC'), later)
        self.assertIn(('shadow', 'EXEC'), later)
        self.assertIn(('gate', 'EXEC'), later)

    def test_finish_over_limit_plain_resume_holds_then_extension_continues(self):
        self.stage_limit('FINISH', {'FAKE_FINISH_WRITE': '1'})

    def test_docs_over_limit_plain_resume_holds_then_extension_continues(self):
        self.stage_limit('DOCS', {'FAKE_DOCS_CODE_ONCE': str(self.root / 'docs-once')})

    def equal_limit(self, mode):
        flags = ('--lifecycle-mode', mode, '--max-invocations', '80', '--max-exec-rounds', '1')
        self.assertIn('HOLD: EXEC round limit reached', self.run_coordinator(*flags, env={'FAKE_EXEC_MIXED_REVISE': '1'}).stdout)
        before = self.state()
        self.assertEqual(before['exec_rounds'], 1)
        # The operator repairs the implementation and asks the reviewer to inspect it again.
        module = self.workspace / 'sum_ints.py'
        module.write_text(module.read_text() + '# operator repair\n')
        self.assert_done(self.run_operator_action('resume', *flags))
        after = self.state()
        self.assertFalse(after.get('round_extensions'))
        self.assertEqual(after['exec_rounds'], 1)
        self.assertEqual(next(t for t in after['turns'] if t['sequence'] > before['sequence'])['role'], 'reviewer')

    def test_equal_limit_plain_resume_reviews_manual_repair_lifecycle_on(self):
        self.equal_limit('on')

    def test_equal_limit_plain_resume_reviews_manual_repair_stop_after_gate(self):
        self.equal_limit('off')

    def writer(self, retry, stopped):
        (self.workspace / 'new_code.py').write_text(''.join('VALUE_%s = %s\n' % (n, n) for n in range(30)))
        (self.workspace / 'test.py').write_text('import unittest\nclass Smoke(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n')
        flags = (*ON, '--review-only', '--quality-writers', 'simplify')
        env = {'FAKE_WRITER_WRITE': 'simplifier', 'FAKE_WRITER_FILE': 'writer_note.py',
               'FAKE_WRITER_WRITE_WITH_TOOLS_ONLY': '1'}
        if retry:
            env['FAKE_WRITER_NO_TOOLS_ONCE'] = str(self.root / 'empty-once')
        with mock.patch.dict(os.environ, env):
            co = rc.Coordinator(rc.parser().parse_args(self.command(*flags)[2:]))
            if stopped:
                original = rc.subprocess.Popen
                ended = False
                def launch(*args, **kwargs):
                    proc = original(*args, **kwargs)   # preserve the fixture's fake-provider guard
                    original_wait = proc.wait
                    def wait(*wait_args, **wait_kwargs):
                        nonlocal ended
                        code = original_wait(*wait_args, **wait_kwargs)
                        if (not ended and proc.args[:2] == ['/bin/sh', '-c']
                                and (self.workspace / 'writer_note.py').exists()):
                            ended = True
                            # End the host turn at local-command completion, before its result is saved.
                            raise KeyboardInterrupt('host turn ended during local test wait')
                        return code
                    proc.wait = wait
                    return proc
                with mock.patch.object(rc.subprocess, 'Popen', side_effect=launch), self.assertRaises(KeyboardInterrupt):
                    co.drive()
                saved = self.state()
                life = saved['lifecycle']
                self.assertEqual((life['writer_counts']['simplifier'], life['writer_leg']['attempts']), (2 if retry else 1,) * 2)
                self.assertIsNotNone(life['pending'])
                self.assertNotIn('test', life['writer_leg'])
                resumed = self.run_operator_action('resume', *flags, env=env)
                self.assert_done(resumed)
            else:
                self.assertEqual(co.drive(), 'DONE', co.state.get('hold_reason'))
        state = self.state()
        writer = state['lifecycle']['quality_writers']['simplifier']
        self.assertEqual((writer['state'], writer['attempts']), ('wrote', 2 if retry else 1))
        self.assertEqual(state['lifecycle']['writer_counts']['simplifier'], 2 if retry else 1)
        turns = [t for t in state['turns'] if t['phase'] == 'POLISH-Q' and t['role'] == 'author']
        self.assertEqual(len(turns), 2 if retry else 1)
        self.assertEqual(state['lifecycle']['polish_calls'], len([t for t in state['turns'] if t['phase'] == 'POLISH-Q']))
        self.assertEqual((self.workspace / 'writer_note.py').read_text(), '# simplifier edit\n')

    def test_second_successful_writer_attempt_survives_local_test_stop_and_resume(self):
        self.writer(retry=True, stopped=True)

    def test_second_successful_writer_attempt_uninterrupted(self):
        self.writer(retry=True, stopped=False)

    def test_first_successful_writer_attempt_survives_local_test_stop_and_resume(self):
        self.writer(retry=False, stopped=True)

    def docs_hold(self, target='CHANGELOG.md', **extra):
        env = {'FAKE_LIFECYCLE_DOCS_FILE': target, 'FAKE_DOCS_HOLD': '1', **extra}
        held = self.run_coordinator(*ON, env=env)
        self.assertIn('HOLD: docs writer: Fake docs writer held.', held.stdout, held.stdout + held.stderr)
        state = self.state()
        [receipt] = [r for r in state['lifecycle']['receipts'] if r['stage'] == 'DOCS']
        self.assertEqual(receipt['status'], 'HOLD')
        self.assertEqual(receipt['output_oid'], rc.git_snapshot(self.workspace)[0])
        return receipt, state

    def test_docs_allowed_write_then_hold_continues_from_original_base(self):
        first, before = self.docs_hold()
        self.assert_done(self.run_operator_action('resume', *ON))   # next writer is a no-op; still reviews the held write
        state = self.state()
        rows = [r for r in state['lifecycle']['receipts'] if r['stage'] == 'DOCS']
        self.assertEqual(len(rows), 2)
        second = rows[1]
        self.assertEqual((second['candidate_oid'], second['docs_paths'], second['route']),
                         (first['candidate_oid'], ['CHANGELOG.md'], 'SECURITY'))
        self.assertEqual(second['review']['status'], 'APPROVE')
        self.assertGreater(second['sequence'], before['sequence'])
        self.assertEqual(list(state['lifecycle']['docs_owned']), ['CHANGELOG.md'])

    def docs_refused(self, before):
        again = self.run_operator_action('resume', *ON)
        self.assertIn('HOLD: DOCS tree differs from the POLISH-Q-approved tree; restore it or abort', again.stdout,
                      again.stdout + again.stderr)
        self.assertEqual(self.state()['sequence'], before['sequence'])

    def test_docs_outside_allowlist_write_then_hold_still_refuses(self):
        _, before = self.docs_hold(target='docs/other.md')
        self.docs_refused(before)

    def test_docs_denied_write_then_hold_still_refuses(self):
        _, before = self.docs_hold(target='CLAUDE.md')
        self.docs_refused(before)

    def test_docs_external_tree_change_after_hold_still_refuses(self):
        _, before = self.docs_hold()
        (self.workspace / 'CHANGELOG.md').write_text('# operator changed this\n')
        self.docs_refused(before)


if __name__ == '__main__':
    unittest.main()
