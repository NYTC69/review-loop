"""V317-P: prompt inclusion and disposition guidance, with fake CLIs only."""
import unittest
from unittest import mock

from paired_session import test_review_report_b1 as b1
from paired_session import test_real_coordinator as trc
from paired_session import worktree_lifecycle as wl
from paired_session.lifecycle_test_helpers import use_lifecycle_on

rc = trc.rc
THRESHOLD = ('Report a new finding only when you would ask the author to change the work for a concrete failure '
             'or material waste in normal supported use; there is no target finding count, and full_review: [] is a normal answer.')
SEVERE_HARM = ('Rare but realistically reachable credential exposure, authorization bypass, irreversible data loss, '
               'destructive action, or comparable harm remains blocking.')


class PromptTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in b1.HELPERS})

    def coordinator(self, *flags):
        return rc.Coordinator(rc.parser().parse_args(self.command(*flags)[2:]))

    def rows(self, co):
        for index, (source, severity, security) in enumerate((
                ('persistent-reviewer', 'MINOR', False), ('persistent-reviewer', 'MAJOR', False),
                ('adversarial-gate', 'LOW', False), ('adversarial-gate', 'HIGH', False),
                ('fresh-shadow', 'LOW', True))):
            co.record_findings(source, 'EXEC', index + 1, [
                {'severity': severity, 'security': security, 'file': 'tracked.txt', 'summary': 'issue ' + str(index)}])
        return co.open_findings()

    def test_built_prompts_and_fake_ledger_parser_on_every_route(self):
        use_lifecycle_on(self, self)
        (self.workspace / 'tracked.txt').write_text('changed\n')
        for name, flags in (
                ('full', ('--lifecycle-mode', 'on')),
                ('off', ('--lifecycle-mode', 'off')),
                ('review-only', ('--review-only', '--lifecycle-mode', 'on')),
                ('report', b1.FLAGS + ('--no-test-command',))):
            with self.subTest(route=name):
                self.run_dir = self.root / name
                co = self.coordinator(*flags)
                rows = self.rows(co)
                (co.context / 'plan.md').write_text('# Plan\nReview supported behavior.\n')
                snap = rc.git_snapshot(self.workspace)[0]
                for phase in ('PLAN', 'EXEC'):
                    co.state['phase'] = phase
                    prompt = co._review_prompt('reviewer', snap)
                    self.assertIn(THRESHOLD, prompt)
                    for sentence in (
                            'A still_open disposition records that a finding is unresolved and does not by itself require REVISE.',
                            "Reviewer CRITICAL/MAJOR/SECURITY findings, security-tagged rows, and an adversarial gate's CRITICAL/HIGH block.",
                            "A gate's MEDIUM/LOW and a reviewer's MINOR/LOW without the security tag are advisory; you may APPROVE while such rows stay open.",
                            "Do not raise another role's advisory row to a blocker only because it is still open.",
                            'Keep giving every listed row its disposition; withdraw with evidence a row that does not meet the reporting standard only if you may withdraw it:',
                            'rows owned by a specialist or security-reviewer may be fixed or withdrawn only by their owner.',
                            rc.APPROVE_CONVERSION_NOTE, rc.REVIEW_SEVERITY_GUIDANCE):
                        self.assertIn(sentence, prompt)
                    for row in rows:
                        self.assertIn(f"- {row['id']}: [source={row['source']}; severity={row['severity']}" +
                                      ('; security-tagged' if row['security'] else '') + f"] {row['summary']}", prompt)
                shadow = co._review_prompt('shadow', snap)
                self.assertIn(THRESHOLD, shadow)
                self.assertNotIn('prior_findings', shadow)
                gate = co._gate_prompt(snap)
                normalized = ' '.join(gate.split())
                self.assertIn('You are an adversarial reviewer with a skepticism-by-default stance.', gate)
                self.assertIn('These are topics to inspect, not categories that need a finding.', gate)
                self.assertIn('do not downgrade them to medium/low.', gate)
                self.assertIn('Report a medium or low finding only when you would really ask the author to change the work for normal supported use.', normalized)
                self.assertIn('There is no target number of findings;', normalized)
                self.assertIn('`findings: []` with `verdict: "approve"` is a normal answer.', normalized)
                self.assertIn(SEVERE_HARM, normalized)
                co.materialize_review_context()
                co.assert_fresh_prompt('shadow', shadow)
                co.assert_fresh_prompt('gate', gate)
                # Exercise the actual fake CLI's ledger block and ID parser, not a copy of it.
                result = co.invoke('reviewer', 'EXEC', prompt, rc.review_schema())
                self.assertEqual([r['id'] for r in result['answer']['prior_findings']], [r['id'] for r in rows])

    def test_built_specialist_and_security_prompts_show_owned_severity(self):
        use_lifecycle_on(self, self)
        co = self.coordinator('--lifecycle-mode', 'on')
        snap = rc.git_snapshot(self.workspace)[0]
        for source, severity in (('specialist:code-reviewer', 'MAJOR'), ('security-reviewer', 'LOW')):
            co.record_findings(source, 'EXEC', 1, [{'severity': severity, 'file': 'tracked.txt', 'summary': 'owned'}])
            row = co.open_findings()[-1]
            with mock.patch.object(co, 'invoke', side_effect=RuntimeError('captured')) as invoke:
                with self.assertRaisesRegex(RuntimeError, 'captured'):
                    if source == 'security-reviewer':
                        co._security_review_turn(snap, 'security-request')
                    else:
                        co._specialist_turn('code-reviewer', ['tracked.txt'], snap)
            self.assertIn(f"- {row['id']}: [severity={severity}] owned\n", invoke.call_args.args[2])

    def test_empty_ledger_keeps_disposition_guidance(self):
        co = self.coordinator()
        prompt = co.open_findings_prompt()
        self.assertIn('Return an empty prior_findings array.', prompt)
        self.assertIn('does not by itself require REVISE', prompt)

    def test_owned_rows_have_severity_and_withdrawal_ownership_is_unchanged(self):
        use_lifecycle_on(self, self)
        co = self.coordinator('--lifecycle-mode', 'off')
        for source in ('specialist:code-reviewer', 'security-reviewer'):
            co.record_findings(source, 'EXEC', 1, [{'severity': 'MINOR', 'file': 'tracked.txt', 'summary': 'owned'}])
            row = co.open_findings()[-1]
            expected = f"- {row['id']}: [severity=MINOR] owned\n"
            self.assertIn(expected, wl.owned_ledger([row]))
            self.assertIn(expected, wl.specialist_prompt('code-reviewer', 'Instructions', None, [row], 'Protocol'))
            with self.assertRaisesRegex(RuntimeError, 'requires its owning specialist'):
                co.apply_dispositions([{'id': row['id'], 'disposition': 'withdrawn', 'evidence': 'not material'}],
                                      2, expected_ids=[row['id']])
            self.assertEqual(co.apply_dispositions(
                [{'id': row['id'], 'disposition': 'withdrawn', 'evidence': 'not material'}],
                2, expected_ids=[row['id']], owner_role=source), [])


if __name__ == '__main__':
    unittest.main()
