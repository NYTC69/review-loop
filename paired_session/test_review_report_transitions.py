"""LG2-a2: report-only EXEC/gate/POLISH-Q transitions and applicable permission probes."""
import json
import os
import subprocess
import unittest
from unittest import mock

from paired_session import test_real_coordinator as trc
from paired_session import worktree_lifecycle as wl

rc = trc.rc
HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
           'fake_codex_cli', 'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action')
FLAGS = ('--review-only', '--review-report', '--lifecycle-mode', 'on')
STOP = 'report sequence stopped after POLISH-Q; SECURITY pending (LG2-a3)'


class ReviewReportTransitionTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in HELPERS})

    def change(self):
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def args(self, *extra, action='run'):
        command = self.command(*FLAGS, *extra)
        command[2] = action
        return rc.parser().parse_args(command[2:])

    def assert_report_path(self, result):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual((state['status'], state['hold_reason'], state['next']), ('HOLD', STOP, 'security'))
        turns = state['turns']
        self.assertEqual([(t['role'], t['phase']) for t in turns[:3]],
                         [('reviewer', 'EXEC'), ('shadow', 'EXEC'), ('gate', 'EXEC')])
        self.assertTrue(all(t['role'] != 'author' for t in turns))
        self.assertTrue(all(t['phase'] in ('EXEC', 'POLISH-Q') for t in turns))
        [receipt] = state['lifecycle']['receipts']
        self.assertEqual((receipt['stage'], receipt['status']), ('POLISH-Q', 'READY'))
        self.assertEqual(receipt['specialists'], list(wl.specialists(['sum_ints.py'])))
        self.assertEqual(len(receipt['specialist_turns']), len(receipt['specialists']))
        self.assertNotIn('fix_base', state['lifecycle'])
        self.assertEqual((state['plan_rounds'], state['exec_rounds']), (0, 1))
        return state

    def test_revise_and_specialist_blockers_report_without_author_or_round_limit(self):
        self.change()
        state = self.assert_report_path(self.run_coordinator(*FLAGS, '--max-exec-rounds', '1', env={
            'FAKE_EXEC_MIXED_REVISE': '1', 'FAKE_SPECIALIST_BLOCK': 'code-reviewer'}))
        self.assertEqual(state['exec_comparisons'][0]['persistent']['verdict'], 'REVISE')
        self.assertTrue(any(f['summary'] == 'specialist blocker' and f['status'] == 'open'
                            for f in state['finding_ledger']))
        self.assertTrue(any(f['severity'] == 'MAJOR' and f['status'] == 'open' for f in state['finding_ledger']))

    def test_approve_runs_gate_and_specialists_once_and_resume_keeps_the_temporary_hold(self):
        self.change()
        state = self.assert_report_path(self.run_coordinator(*FLAGS))
        self.assertEqual(state['exec_comparisons'][0]['persistent']['verdict'], 'APPROVE')
        result = self.run_operator_action('resume', '--lifecycle-mode', 'on')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.state()['hold_reason'], STOP)
        self.assertEqual(self.state()['turns'], state['turns'])

    def test_gate_request_changes_still_runs_polish_q_and_keeps_findings_open(self):
        self.change()
        state = self.assert_report_path(self.run_coordinator(*FLAGS, env={'FAKE_GATE_BLOCK': '1'}))
        self.assertEqual(state['exec_comparisons'][0]['gate']['verdict'], 'needs-attention')   # gate schema's REQUEST_CHANGES verdict
        self.assertTrue(any(f['source'] == 'adversarial-gate' and f['status'] == 'open'
                            for f in state['finding_ledger']))
        self.assertTrue(state['gate_ran'])

    def test_shadow_blocker_still_runs_gate_and_polish_q(self):
        self.change()
        state = self.assert_report_path(self.run_coordinator(*FLAGS, env={'FAKE_SHADOW_CRITICAL': '1'}))
        self.assertEqual(state['exec_comparisons'][0]['shadow']['verdict'], 'APPROVE')
        self.assertTrue(any(f['source'] == 'fresh-shadow' and f['status'] == 'open'
                            for f in state['finding_ledger']))

    def test_category_a_void_restores_once_and_holds_on_a_second_violation(self):
        for strict in (True, False):
            for once in (True, False):
                with self.subTest(strict=strict, once=once):
                    self.run_dir = self.root / f'void-{strict}-{once}'
                    self.change()
                    before = rc.git_snapshot(self.workspace)[0]
                    command = self.command(*FLAGS, '--skip-probe')
                    if not strict: command.remove('--strict')
                    env = {**os.environ, 'FAKE_MUTATION': 'echo'}
                    if once: env['FAKE_MUTATION_ONCE'] = str(self.root / f'marker-{strict}')
                    result = subprocess.run(command, cwd=self.root, env=env, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    state = self.state()
                    voids = [t for t in state['turns'] if t.get('voided')]
                    self.assertEqual(len(voids), 1 if once else 2)
                    self.assertTrue(all(t['voided']['restored'] for t in voids))
                    self.assertTrue(all(t['role'] != 'author' for t in state['turns']))
                    self.assertEqual(rc.git_snapshot(self.workspace)[0], before)
                    self.assertFalse((self.workspace / 'forbidden.txt').exists())
                    if once: self.assertEqual(state['hold_reason'], STOP)
                    else:
                        self.assertIn('mutated workspace again after one re-dispatch', state['hold_reason'])
                        self.assertFalse(any(t['role'] == 'gate' for t in state['turns']))

    def test_permission_probe_skips_author_but_proves_reviewer_and_gate_surfaces(self):
        for author, gate in (('codex', 'codex'), ('claude', 'claude')):
            with self.subTest(author=author, gate=gate):
                self.run_dir = self.root / f'probe-{author}'
                self.change()
                args = self.args('--author-vendor', author, '--gate-vendor', gate, action='permission-probe')
                co = rc.Coordinator(args)
                with mock.patch.object(co, '_author_permission_probe', side_effect=AssertionError('author probe dispatched')):
                    self.assertTrue(co.permission_probe())
                report = json.loads((self.run_dir / 'permission-probe.json').read_text())
                self.assertEqual(report['author_permission_probe']['status'], 'NOT-APPLICABLE')
                self.assertTrue(report['allowed_command_ran'])
                self.assertTrue(report['write_attempts_denied'])
                roles = [t['role'] for t in co.state['turns']]
                self.assertEqual(roles, ['probe', 'gate-probe'] if gate == 'codex' else ['probe'])
                self.assertEqual(report['gate_permission_probe']['status'], 'PASS' if gate == 'codex' else 'NOT_NEEDED')
                self.assertEqual(co.probe_passed(), (True, ''))
                with mock.patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False), \
                        mock.patch.object(co, 'codex_contract_verified', side_effect=AssertionError('unused author check')), \
                        mock.patch.object(co, 'claude_author_verified', side_effect=AssertionError('unused author check')), \
                        mock.patch.object(co, '_drive_loop', return_value='HOLD'):
                    self.assertEqual(co.drive(), 'HOLD')

    def test_the_gate_cannot_be_turned_off_in_report_mode(self):
        self.change()
        result = self.run_coordinator('--review-only', '--review-report', '--adversarial-gate', 'off')   # lifecycle off;
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)            # lifecycle on refuses it already
        self.assertIn('REFUSED: --review-report refuses --adversarial-gate off', result.stdout)
        self.assertFalse((self.run_dir / 'state.json').exists())

    def test_lifecycle_off_report_holds_after_the_gate_without_a_writer(self):
        self.change()
        result = self.run_coordinator('--review-only', '--review-report')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual(state['hold_reason'], 'report POLISH-Q requires --lifecycle-mode on; start a new run')
        self.assertEqual([t['role'] for t in state['turns']], ['reviewer', 'shadow', 'gate'])

    def test_polish_q_tree_change_holds_and_never_routes_specialist_blockers_to_a_fix(self):
        self.change()
        co = rc.Coordinator(self.args())
        co.state['lifecycle'].update(stage='POLISH-Q', candidate_oid=rc.git_snapshot(self.workspace)[0])
        co.state['next'] = 'polish-q'

        def specialist(*_):
            (self.workspace / 'sum_ints.py').write_text('changed by a specialist\n')
            return {'sequence': 1}
        blockers = [{'id': 'F001', 'owner_role': 'specialist:code-reviewer'}]
        with mock.patch.object(rc.worktree_lifecycle, 'specialists', return_value=('code-reviewer',)), \
                mock.patch.object(co, 'materialize_review_context'), \
                mock.patch.object(co, '_specialist_turn', side_effect=specialist), \
                mock.patch.object(co, 'blocking_open_findings', return_value=blockers):
            co.worktree_polish_turn()
        self.assertEqual((co.state['status'], co.state['hold_reason']),
                         ('HOLD', 'POLISH-Q tree differs from the reviewed report tree; inspect, then abort'))
        self.assertNotEqual(co.state['next'], 'polish-fix')
        self.assertNotIn('fix_base', co.state['lifecycle'])

    def test_writer_routes_and_direct_author_dispatch_are_refused(self):
        self.change()
        co = rc.Coordinator(self.args())
        for next_role in ('author', 'finish', 'docs', 'polish-fix', 'polish-recheck'):
            co.state.update(status='ACTIVE', next=next_role)
            with mock.patch.object(co, 'invoke', side_effect=AssertionError('writer reached')):
                self.assertEqual(co.drive(), 'HOLD')
            self.assertEqual(co.state['hold_reason'], 'report mode refuses a writer or unsupported phase')
        with self.assertRaisesRegex(RuntimeError, 'report mode refuses every author dispatch'):
            co.invoke('author', 'EXEC', 'write', rc.author_schema())
        self.assertEqual(co.state['turns'], [])


if __name__ == '__main__':
    unittest.main()
