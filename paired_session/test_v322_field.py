"""V322-FIELD: actionable refusals without changing the independence or note checks."""
import copy
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
PREFIX = 'next author turn is unavailable; note remains undelivered; '
WORKITEM_RECOVERY = ("this run's frozen work item cannot change. Edit the work-item file {} "
                     'to describe the point in words (no finding ids), then use note --scope-change --text "..." '
                     'and run the printed Probe: and Start: commands (a successor with a fresh PLAN), '
                     'or abort and start a new run')
PLAN_RECOVERY = ('resume does not rewrite the approved plan. Use note --scope-change --text "..." '
                 '(no finding ids) and run the printed Probe: and Start: commands for a successor '
                 'with a fresh PLAN, or abort and start a new run')


class FieldMessageTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in (
        'setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen',
        'fake_codex_cli', 'fake_claude_cli', 'coordinator')})

    def test_ids_are_unique_sorted_bounded_and_from_the_masked_input(self):
        co = self.coordinator('--test-command', 'echo F999')
        for role in ('gate', 'shadow'):
            for name in ('workitem.md', 'plan.md'):
                with self.subTest(role=role, name=name):
                    for other in ('workitem.md', 'plan.md'):
                        (co.context / other).write_text('Describe the feature.\n')
                    (co.context / name).write_text('echo F999\nF008 F003 F008 F001 F007 F002 F004\n')
                    with self.assertRaises(RuntimeError) as caught:
                        co.assert_fresh_prompt(role, 'Review the delta.')
                    recovery = WORKITEM_RECOVERY.format(co.workitem) if name == 'workitem.md' else PLAN_RECOVERY
                    self.assertEqual(str(caught.exception),
                        '{} independence check rejected ledger ids in context/{}: '
                        'F001, F002, F003, F004, F007, …; {}'.format(role, name, recovery))

    def test_single_id_and_fully_masked_input(self):
        co = self.coordinator('--test-command', 'echo F999')
        (co.context / 'plan.md').write_text('echo F999\nF008 F008\n')
        with self.assertRaises(RuntimeError) as caught:
            co.assert_fresh_prompt('gate', 'Review the delta.')
        self.assertEqual(str(caught.exception),
                         'gate independence check rejected ledger ids in context/plan.md: F008; ' + PLAN_RECOVERY)
        (co.context / 'plan.md').write_text('echo F999\n')
        co.assert_fresh_prompt('gate', 'Review the delta.')
        co.assert_fresh_prompt('shadow', 'Review the delta.')

    def test_scope_change_uses_restated_original_and_a_fresh_plan(self):
        co = self.coordinator()
        frozen = (co.context / 'workitem.md').read_text()
        (co.context / 'plan.md').write_text('Implement F008.\n')
        self.workitem.write_text('Describe the requested behavior in words.\n')
        co.state.update(status='HOLD', phase='EXEC', next='author')
        co.save()
        args = rc.parser().parse_args(['resume', '--workspace', str(self.workspace),
            '--workitem', str(self.workitem), '--run-dir', str(self.run_dir),
            '--codex-bin', str(self.fake_codex_cli()), '--claude-bin', str(self.fake_claude_cli())])
        restored = rc.Coordinator(args)
        with patch.object(restored, 'drive', return_value='HOLD'):
            restored.resume()
        self.assertEqual((restored.context / 'workitem.md').read_text(), frozen)
        restored.state['status'] = 'HOLD'
        restored.args.action = 'note'
        commands = restored.scope_change('Describe the implementation point without finding ids.', None)
        self.assertIn('Probe: ', commands)
        self.assertIn('Start: ', commands)
        task = (restored.evidence / 'successor-workitem.md').read_text()
        self.assertIn(self.workitem.read_text(), task)
        self.assertNotIn('F008', task)
        self.assertEqual(restored.state['status'], 'ABORTED')

    def assert_note_refusal(self, co, suffix):
        before = copy.deepcopy(co.state)
        with self.assertRaises(ValueError) as caught:
            co.note('Please clarify the behavior.', None)
        self.assertEqual(str(caught.exception), PREFIX + suffix)
        self.assertEqual(co.state, before)
        self.assertFalse(list(co.evidence.glob('operator-note-*')))

    def test_round_limit_reasons_for_both_author_phases(self):
        co = self.coordinator()
        for phase in ('PLAN', 'EXEC'):
            with self.subTest(phase=phase):
                co.state.update(status='HOLD', phase=phase, next='author', plan_rounds=0, exec_rounds=0)
                co.state[phase.lower() + '_rounds'] = (co.plan_round_limit() if phase == 'PLAN'
                                                       else co.exec_round_limit())
                self.assert_note_refusal(co,
                    phase + ' round limit reached; at the round-limit HOLD use resume --add-rounds N (1-10), '
                    'then add the note at the next eligible PLAN/EXEC HOLD, or note --scope-change')

    def test_invocation_budget_reason(self):
        co = self.coordinator()
        co.state.update(status='HOLD', phase='EXEC', next='author', invocations_used=co.args.max_invocations)
        self.assert_note_refusal(co,
            'invocation budget exhausted; use resume --max-invocations N with a higher cap, '
            'then add the note at the next eligible PLAN/EXEC HOLD')

    def test_active_and_uncertain_turn_recovery_commands(self):
        co = self.coordinator()
        co.state.update(status='HOLD', phase='EXEC', next='author')
        for key in ('active', 'uncertain_active'):
            for phase, command in (('EXEC', 'resume'), ('PROBE', 'permission-probe'),
                                   ('AUTHOR_PERMISSION_PROBE', 'permission-probe')):
                with self.subTest(key=key, phase=phase):
                    co.state.update(active=None, uncertain_active=None)
                    co.state[key] = {'phase': phase, 'role': 'author'}
                    self.assert_note_refusal(co,
                        'an active or uncertain_active turn is recorded; wait for it to finish, or inspect the evidence '
                        'and confirm its process group has stopped before ' + command + ' --retry-uncertain; '
                        'add the note at the next eligible PLAN/EXEC HOLD')

    def test_non_plan_exec_phase_and_available_author_turn(self):
        co = self.coordinator()
        for phase in ('FINISH', 'DOCS', 'POLISH-Q', 'SECURITY', 'PROBE', 'DONE'):
            with self.subTest(phase=phase):
                co.state.update(status='HOLD', phase=phase, next='author')
                self.assert_note_refusal(co,
                    'notes reach only PLAN or EXEC author turns; this run is in ' + phase +
                    ' (at DONE use reject; at an eligible idle HOLD use note --scope-change)')
        co.state['status'] = 'DONE'
        with self.assertRaisesRegex(ValueError, '^note requires a HOLD run; DONE uses reject$'):
            co.note('Please clarify the behavior.', None)
        co.state['status'] = 'HOLD'
        co.state['phase'] = 'EXEC'
        self.assertEqual(co.note('Please clarify the behavior.', None), 'N001')
        self.assertEqual(co.state['operator_notes'][-1]['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
