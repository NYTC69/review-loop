"""V319-HOLD: HOLDs whose named exits work, driven through the fake provider CLIs.

1. At the post-DONE rejection limit, a tree the operator changes and resumes is reviewed and gated before DONE, on the
   stop-after-gate route and on the full lifecycle; `reject --scope-change` from that HOLD starts a working successor.
2. SECURITY classifies a file name with a TAB or a backslash; a name it still cannot classify HOLDs with an exit that works.
3. The PLAN round-limit advisory exit, reached by a driven run (not through the one-attempt rule).
The rewrite-only PLAN review with only advisory rows left open is in test_v317_plan."""
import json
import subprocess
import unittest

from paired_session import test_worktree_lifecycle as twl

rc = twl.rc
ON = ('--lifecycle-mode', 'on', '--max-invocations', '80')
OFF = ('--lifecycle-mode', 'off', '--shadow', 'off')
QUIET = {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1', 'FAKE_AUTHOR_WRITE_NEW_TREE': ''}
LIMIT = 'rejected-tree; post-DONE rejection limit reached; note and resume, reject --scope-change, or abort'
RELEASED = '31b6593'   # v3.1.8 rc2: the release before V319-HOLD
LAUNCHER = """import sys
sys.path[0] = {folder!r}   # as when the coordinator file runs itself: its sibling modules of this checkout
sys.argv[0] = __file__ = {module!r}
exec(compile(open({source!r}).read(), __file__, 'exec'))
"""
UNCLASSIFIABLE_EXIT = ('; rename or remove the unclassifiable path and resume (EXEC review and gate replay, then '
                       'FINISH..SECURITY, within the EXEC round limit), or abort')


class HoldExitTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in twl._HELPERS})

    @classmethod
    def setUpClass(cls):
        # The released coordinator source of this clone's history, run in place of the file (as test_v318cap loads one).
        try:
            cls.released_source = subprocess.check_output(
                ['git', 'show', RELEASED + ':paired_session/coordinator.py'], cwd=twl.trc.MODULE_PATH.parent,
                text=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError:
            cls.released_source = None   # a shallow checkout: only the upgrade test skips

    released = None   # the launcher path while the released coordinator drives the run

    def command(self, *extra):   # the product default (efficient), as test_v317g1
        command = [arg for arg in twl.trc.RealCoordinatorTests.command(self, *extra) if arg != '--strict']
        if self.released:
            command[1] = str(self.released)
        return command

    def released_launcher(self):
        if self.released_source is None:
            self.skipTest(RELEASED + ' coordinator source unavailable (e.g. shallow checkout)')
        source, launcher = self.root / 'released-coordinator.txt', self.root / 'released-launcher.py'
        source.write_text(self.released_source)
        module = twl.trc.MODULE_PATH.resolve()
        launcher.write_text(LAUNCHER.format(folder=str(module.parent), module=str(module), source=str(source)))
        return launcher

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
                # the successor the printed Start command names, with the fake CLIs, through its own gate to DONE
                self.run_dir, workitem = parent.with_name(parent.name + '-successor'), self.workitem
                self.workitem = evidence / 'successor-workitem.md'
                try:
                    started = self.run_coordinator(*mode, '--config', str(evidence / 'successor-config.json'),
                                                   '--supersedes', str(parent), env=QUIET)
                finally:
                    self.workitem = workitem
                self.assertIn(twl.DONE, started.stdout, started.stdout + started.stderr)
                turns = self.state()['turns']   # its own PLAN, EXEC review and gate
                self.assertEqual([(turn['role'], turn['phase']) for turn in turns[:2]],
                                 [('author', 'PLAN'), ('reviewer', 'PLAN')])
                self.assertIn(('gate', 'EXEC'), [(turn['role'], turn['phase']) for turn in turns])
                self.clean_workspace()

    def test_a_rejection_limit_hold_saved_by_the_release_before_is_gated_after_an_edit(self):
        launcher = self.released_launcher()
        for mode in (OFF, ON):
            with self.subTest(mode=mode[1]):
                self.run_dir, self.released = self.root / ('upgrade-' + mode[1]), launcher
                (self.workspace / 'sum_ints.py').write_text(
                    'def sum_ints(values):\n    if not all(type(x) is int for x in values):\n'
                    '        raise TypeError("ints only")\n    return sum(values)\n')
                done = self.run_coordinator(*mode, env=QUIET)
                self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
                for _ in range(rc.DEFAULT_MAX_REJECTIONS):   # the default cap, used up by ordinary repaired rejections
                    repaired = self.run_operator_action('reject', *mode, '--text', 'Check input validation.',
                                                        env={**QUIET, 'FAKE_AUTHOR_WRITE_NEW_TREE': '1'})
                    self.assertIn(twl.DONE, repaired.stdout, repaired.stdout + repaired.stderr)
                held = self.run_operator_action('reject', *mode, '--text', 'Check one more input.', env=QUIET)
                self.assertIn('HOLD: ' + LIMIT, held.stdout, held.stdout + held.stderr)
                saved = self.state()
                self.assertIs(saved['gate_ran'], True)   # what the release before saved at this HOLD
                held_at, self.released = saved['sequence'], None
                (self.workspace / 'sum_ints.py').write_text(   # upgraded: the operator's own change, then a plain resume
                    'def sum_ints(values):\n    if not all(type(x) is int for x in values):\n'
                    '        raise TypeError("integers only")\n    return sum(values)\n')
                resumed = self.run_operator_action('resume', *mode, env=QUIET)
                self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
                state = self.state()
                after = [(turn['role'], turn['phase']) for turn in state['turns'] if turn['sequence'] > held_at]
                self.assertEqual(after[:2], [('author', 'EXEC'), ('reviewer', 'EXEC')])
                self.assertIn(('gate', 'EXEC'), after)
                gate = [turn for turn in state['turns'] if turn['role'] == 'gate'][-1]
                if mode is OFF:
                    self.assertEqual(gate['snapshot_before'], state['approved_snapshot'])
                else:
                    self.assertEqual(state['lifecycle']['exec_convergence']['gate_sequence'], gate['sequence'])
                    self.assertEqual(after[-1], ('reviewer', 'SECURITY'))
                self.clean_workspace()

    def test_a_gate_round_limit_hold_saved_by_the_release_before_gates_the_repair(self):
        launcher = self.released_launcher()
        for mode in (OFF, ON):
            with self.subTest(mode=mode[1]):
                self.run_dir, self.released = self.root / ('upgrade-gate-' + mode[1]), launcher
                env = {'FAKE_GATE_BLOCK_ONCE': str(self.run_dir) + '.blocked'}
                held = self.run_coordinator(*mode, '--max-exec-rounds', '1', env=env)
                self.assertIn('HOLD: EXEC round limit reached after adversarial gate', held.stdout, held.stdout + held.stderr)
                saved = self.state()
                # what the release before saved: the off route kept the blocking gate as run, the full lifecycle did not
                self.assertEqual((saved['gate_ran'], saved.get('force_gate_after_reject')),
                                 (True, False) if mode is OFF else (False, False))
                held_at, self.released = saved['sequence'], None
                resumed = self.run_operator_action('resume', *mode, '--max-exec-rounds', '1', '--add-rounds', '1', env=env)
                self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
                state = self.state()
                after = [turn for turn in state['turns'] if turn['sequence'] > held_at]
                self.assertEqual([(turn['role'], turn['phase']) for turn in after[:2]], [('author', 'EXEC'), ('reviewer', 'EXEC')])
                gates = [turn for turn in state['turns'] if turn['role'] == 'gate']
                self.assertEqual(len(gates), 2)
                self.assertGreater(gates[-1]['sequence'], held_at)
                self.assertNotEqual(gates[0]['snapshot_before'], gates[-1]['snapshot_before'])
                if mode is OFF:
                    self.assertEqual(gates[-1]['snapshot_before'], state['approved_snapshot'])
                else:
                    self.assertEqual(state['lifecycle']['exec_convergence']['gate_sequence'], gates[-1]['sequence'])
                blocker = [row for row in state['finding_ledger'] if row['source'] == 'adversarial-gate'
                           and row['severity'].upper() in ('HIGH', 'CRITICAL')]
                self.assertEqual([row['status'] for row in blocker], ['fixed'])
                self.clean_workspace()

    def test_a_tree_the_gate_rejected_is_gated_again_after_its_repair(self):
        for mode in (OFF, ON):
            for limit in (False, True):   # a round still left, or the round limit and resume --add-rounds 1
                with self.subTest(mode=mode[1], at_limit=limit):
                    self.run_dir = self.root / f'gate-repair-{mode[1]}-{limit}'
                    rounds = ('--max-exec-rounds', '1') if limit else ()
                    env = {'FAKE_GATE_BLOCK_ONCE': str(self.run_dir) + '.blocked'}
                    result = self.run_coordinator(*mode, *rounds, env=env)
                    if limit:
                        self.assertIn('HOLD: EXEC round limit reached after adversarial gate', result.stdout,
                                      result.stdout + result.stderr)
                        result = self.run_operator_action('resume', *mode, *rounds, '--add-rounds', '1', env=env)
                    self.assertIn(twl.DONE, result.stdout, result.stdout + result.stderr)
                    state = self.state()
                    gates = [turn for turn in state['turns'] if turn['role'] == 'gate']
                    self.assertEqual(len(gates), 2)
                    first, second = gates
                    repair = [(turn['role'], turn['phase']) for turn in state['turns']
                              if first['sequence'] < turn['sequence'] < second['sequence']]
                    self.assertEqual([turn for turn in repair if turn[0] != 'shadow'],
                                     [('author', 'EXEC'), ('reviewer', 'EXEC')])
                    self.assertNotEqual(first['snapshot_before'], second['snapshot_before'])   # the repaired tree
                    if mode is OFF:
                        self.assertEqual(second['snapshot_before'], state['approved_snapshot'])
                    else:
                        self.assertEqual(state['lifecycle']['exec_convergence']['gate_sequence'], second['sequence'])
                    blocker = [row for row in state['finding_ledger'] if row['source'] == 'adversarial-gate'
                               and row['severity'].upper() in ('HIGH', 'CRITICAL')]
                    self.assertEqual([row['status'] for row in blocker], ['fixed'])
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
