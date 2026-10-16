"""V319-HOLD: HOLDs whose named exits work, driven through the fake provider CLIs.

1. At the post-DONE rejection limit, a tree the operator changes and resumes is reviewed and gated before DONE, on the
   stop-after-gate route and on the full lifecycle; `reject --scope-change` from that HOLD starts a working successor.
2. SECURITY classifies a file name with a TAB or a backslash; a name it still cannot classify HOLDs with an exit that works.
3. The PLAN round-limit advisory exit, reached by a driven run (not through the one-attempt rule).
The rewrite-only PLAN review with only advisory rows left open is in test_v317_plan."""
import json
import unittest

from paired_session import test_worktree_lifecycle as twl

rc = twl.rc
ON = ('--lifecycle-mode', 'on', '--max-invocations', '80')
OFF = ('--lifecycle-mode', 'off', '--shadow', 'off')
QUIET = {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1', 'FAKE_AUTHOR_WRITE_NEW_TREE': ''}
LIMIT = 'rejected-tree; post-DONE rejection limit reached; note and resume, reject --scope-change, or abort'
UNCLASSIFIABLE_EXIT = ('; rename or remove the unclassifiable path and resume (EXEC review and gate replay, then '
                       'FINISH..SECURITY, within the EXEC round limit), or abort')


class HoldExitTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in twl._HELPERS})

    def command(self, *extra):   # the product default (efficient), as test_v317g1
        return [arg for arg in twl.trc.RealCoordinatorTests.command(self, *extra) if arg != '--strict']

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def clean_workspace(self):
        rc.subprocess.run(['git', 'checkout', '-q', '--', '.'], cwd=self.workspace, check=True)
        rc.subprocess.run(['git', 'clean', '-qfd'], cwd=self.workspace, check=True)

    def at_rejection_limit(self, mode):
        """DONE, then a reject at the saved cap of zero: the rejection-limit HOLD (as test_v317g1)."""
        self.run_dir = self.root / ('limit-' + mode[1])
        (self.workspace / 'sum_ints.py').write_text(
            'def sum_ints(values):\n    if not all(type(x) is int for x in values):\n'
            '        raise TypeError("ints only")\n    return sum(values)\n')
        done = self.run_coordinator(*mode, env=QUIET)
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        state['max_rejections'] = 0
        (self.run_dir / 'state.json').write_text(json.dumps(state))
        held = self.run_operator_action('reject', *mode, '--text', 'Recheck integer inputs.', env=QUIET)
        self.assertIn('HOLD: ' + LIMIT, held.stdout, held.stdout + held.stderr)
        return self.state()['sequence']

    def test_an_operator_edit_after_the_rejection_limit_is_reviewed_and_gated_before_done(self):
        for mode in (OFF, ON):   # the stop-after-gate route and the full lifecycle
            with self.subTest(mode=mode[1]):
                held_at = self.at_rejection_limit(mode)
                unchanged = self.run_operator_action('resume', *mode, env=QUIET)   # the rejected tree keeps the HOLD
                self.assertIn('HOLD: ' + LIMIT, unchanged.stdout, unchanged.stdout + unchanged.stderr)
                self.assertEqual(self.state()['sequence'], held_at)
                (self.workspace / 'sum_ints.py').write_text(   # the operator's own change, then a plain resume
                    'def sum_ints(values):\n    if not all(type(x) is int for x in values):\n'
                    '        raise TypeError("integers only")\n    return sum(values)\n')
                resumed = self.run_operator_action('resume', *mode, env=QUIET)
                self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
                state = self.state()
                after = [turn for turn in state['turns'] if turn['sequence'] > held_at]
                gates = [turn for turn in after if turn['role'] == 'gate']
                self.assertTrue(gates, [(turn['role'], turn['phase']) for turn in after])
                self.assertIn('raise TypeError("integers only")', (self.workspace / 'sum_ints.py').read_text())
                if mode is OFF:   # DONE carries exactly the tree the new gate reviewed
                    self.assertEqual(gates[-1]['snapshot_before'], state['approved_snapshot'])
                else:             # FINISH..SECURITY ran after the new EXEC review and gate converged
                    self.assertGreater(state['lifecycle']['exec_convergence']['gate_sequence'], held_at)
                    self.assertEqual(after[-1]['phase'], 'SECURITY')
                self.clean_workspace()

    def test_reject_scope_change_from_the_rejection_limit_hold_starts_a_successor(self):
        for mode in (OFF, ON):
            with self.subTest(mode=mode[1]):
                self.at_rejection_limit(mode)
                parent = self.run_dir
                changed = self.run_operator_action('reject', *mode, '--scope-change', '--text', 'New scope: include floats.')
                self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
                self.assertEqual((self.state()['status'], self.state()['abort_kind']), ('ABORTED', 'scope-change'))
                self.assertIn('Start: ', changed.stdout)
                evidence = parent / 'evidence'
                task = (evidence / 'successor-workitem.md').read_text()
                self.assertTrue(task.endswith('## Operator scope change\nNew scope: include floats.\n'), task)
                # the successor the printed Start command names, with the fake CLIs: a fresh PLAN, approved
                self.run_dir, workitem = parent.with_name(parent.name + '-successor'), self.workitem
                self.workitem = evidence / 'successor-workitem.md'
                try:
                    started = self.run_coordinator(*mode, '--config', str(evidence / 'successor-config.json'),
                                                   '--supersedes', str(parent), '--stop-after-plan')
                finally:
                    self.workitem = workitem
                self.assertIn('HOLD: ' + rc.PLAN_STOP_REASON, started.stdout, started.stdout + started.stderr)
                self.clean_workspace()

    def test_security_classifies_tab_and_backslash_names(self):
        for index, (name, held) in enumerate((('notes\tdraft.txt', None), ('docs\\draft.md', None),
                                              ('keys\\id_rsa', 'HOLD: SECURITY sensitive paths: keys\\id_rsa (ssh-private-key)'))):
            with self.subTest(name=name):
                self.run_dir = self.root / f'name-{index}'
                (self.workspace / name).write_text('plain notes\n')   # an untracked file the run delivers
                result = self.run_coordinator(*ON)
                self.assertNotIn('unclassifiable', result.stdout, result.stdout + result.stderr)
                if held:
                    self.assertIn(held + '\n', result.stdout, result.stdout + result.stderr)
                else:
                    self.assertIn(twl.DONE, result.stdout, result.stdout + result.stderr)
                (self.workspace / name).unlink()

    def test_an_unclassifiable_name_names_exits_that_work(self):
        name = 'notes\ndraft.txt'   # a line break: still not classifiable
        reason = 'SECURITY sensitive paths: ' + name + ' (unclassifiable path)' + UNCLASSIFIABLE_EXIT
        for step in ('rename', 'remove', 'abort'):
            with self.subTest(step=step):
                self.run_dir = self.root / ('unclassifiable-' + step)
                (self.workspace / name).write_text('plain notes\n')
                held = self.run_coordinator(*ON)
                self.assertIn('HOLD: ' + reason, held.stdout, held.stdout + held.stderr)
                self.assertEqual(self.state()['hold_reason'], reason)
                if step == 'abort':
                    aborted = self.run_operator_action('abort', *ON)
                    self.assertIn('HOLD: aborted by operator', aborted.stdout, aborted.stdout + aborted.stderr)
                    (self.workspace / name).unlink()
                    continue
                if step == 'rename':
                    (self.workspace / name).rename(self.workspace / 'notes-draft.txt')
                else:
                    (self.workspace / name).unlink()
                epoch = self.state()['lifecycle']['epoch']
                resumed = self.run_operator_action('resume', *ON)
                self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
                self.assertGreater(self.state()['lifecycle']['epoch'], epoch)   # EXEC review and gate ran on the new tree
                self.clean_workspace()

    def test_the_plan_round_limit_advisory_exit_in_a_driven_run(self):
        # one PLAN round: the first advisory REVISE is already at the cap, so no advisory-only repair attempt is spent
        self.run_dir = self.root / 'plan-limit'
        done = self.run_coordinator(*OFF, '--adversarial-gate', 'off', '--max-plan-rounds', '1', '--max-exec-rounds', '1',
                                    env={'FAKE_PLAN_MINOR_REVISE': '1', 'FAKE_EXEC_STILL_OPEN': 'plan advisory'})
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertEqual(state['plan_rounds'], 1)
        self.assertNotIn('PLAN', state.get('advisory_repair_attempts', {}))
        self.assertEqual([(r['reviewer_raw_verdict'], r['effective_verdict']) for r in state['review_verdicts']
                          if r['phase'] == 'PLAN'], [('REVISE', 'APPROVE_WITH_ADVISORY')])
        [row] = [row for row in state['finding_ledger'] if row['phase'] == 'PLAN']
        self.assertEqual((row['status'], row.get('advisory')), ('open', True))
        [author] = [t for t in state['turns'] if t['role'] == 'author' and t['phase'] == 'EXEC']
        prompt = (self.run_dir / 'evidence' / ('%03d-exec-author.prompt.txt' % author['sequence'])).read_text()
        self.assertIn(row['id'], prompt)
        self.assertIn('These findings are explicitly non-blocking.', prompt)


if __name__ == '__main__':
    unittest.main()
