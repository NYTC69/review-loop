"""WI-DISPFIX: disposition retries and replay of a durable EXEC review."""
import json
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc


class DispositionRetryTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.co = self.coordinator()
        self.co.state.update(phase='EXEC', next='reviewer', exec_rounds=1, sequence=1)
        self.co.record_findings('persistent-reviewer', 'EXEC', 1, [
            {'severity': 'MAJOR', 'file': 'tracked.txt', 'summary': summary}
            for summary in ('First finding', 'Second finding')])
        self.co.save()

    def coordinator(self, action='run'):
        argv = self.h.command('--lifecycle-mode', 'off', '--shadow', 'off', '--adversarial-gate', 'off',
                              '--polish-round', 'off', '--skip-probe')[2:]
        argv[0] = action
        return rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))

    def test_invalid_disposition_then_missing_id_holds_and_marks_both_receipts_discarded(self):
        co = self.co
        first, second = co.state['finding_ledger']
        collect = co._collect

        def collect_dispositions(*args):
            # Keep the fake CLI's verdict and evidence, and vary only its disposition IDs.
            parsed = collect(*args)
            rows = parsed[0]['prior_findings']
            self.assertEqual([row['id'] for row in rows], [first['id'], second['id']])
            if co.state['sequence'] == 2:
                rows.append({**rows[0], 'id': 'F999'})
            else:
                rows.pop()
            return parsed

        with patch.object(co, '_collect', side_effect=collect_dispositions):
            co.reviewer_turn()

        saved = json.loads((self.h.run_dir / 'state.json').read_text())
        [rejected, retry] = saved['turns']
        self.assertEqual(saved['status'], 'HOLD')
        self.assertEqual(saved['hold_reason'], 'reviewer omitted open finding dispositions after retry: ' + second['id'])
        self.assertIsNone(saved['pending_reviewer_result_sequence'])
        self.assertEqual([(row['status'], len(row['status_history'])) for row in saved['finding_ledger']],
                         [('open', 1), ('open', 1)])
        prefix = co.evidence / f"{retry['sequence']:03d}-exec-reviewer"
        prompt = prefix.with_suffix('.prompt.txt').read_text()
        self.assertIn('duplicate or unknown id: F999', prompt)
        self.assertIn('one allowed protocol retry', prompt)
        for turn, reason in ((rejected, 'invalid finding dispositions'), (retry, 'missing finding dispositions')):
            self.assertEqual(turn['discarded'], reason)
            receipt = co.evidence / f"{turn['sequence']:03d}-exec-reviewer.receipt.json"
            self.assertEqual(json.loads(receipt.read_text())['discarded'], reason)
        co.state['pending_reviewer_result_sequence'] = rejected['sequence']
        self.assertIsNone(co._recorded_reviewer_result('EXEC'))

    def test_recorded_exec_review_replays_already_applied_dispositions_once(self):
        class SimulatedCrash(BaseException):
            pass

        co = self.co

        def interrupt_after_dispositions(*args):
            co.save()
            raise SimulatedCrash()

        with patch.object(co, 'render', side_effect=interrupt_after_dispositions), self.assertRaises(SimulatedCrash):
            co.reviewer_turn()

        resumed = self.coordinator(action='resume')
        [receipt] = resumed.state['turns']
        self.assertEqual(resumed.state['pending_reviewer_result_sequence'], receipt['sequence'])
        rows = resumed.state['finding_ledger']
        replayed = resumed._recorded_reviewer_result('EXEC')
        self.assertEqual(replayed, {'answer': receipt['answer'], 'snapshot': receipt['snapshot_before'],
                                    'sequence': receipt['sequence'], 'role': 'reviewer',
                                    'open_finding_ids': [row['id'] for row in rows]})
        history = [(1, 'open'), (receipt['sequence'], 'fixed')]
        for row in rows:
            self.assertEqual([(entry['round'], entry['status']) for entry in row['status_history']], history)
        with patch.object(resumed, 'invoke') as invoke:
            resumed.reviewer_turn()
        invoke.assert_not_called()
        self.assertEqual(resumed.state['status'], 'DONE', resumed.state.get('hold_reason'))
        self.assertIsNone(resumed.state['pending_reviewer_result_sequence'])
        self.assertEqual(resumed.state['turns'], [receipt])
        for row in resumed.state['finding_ledger']:
            self.assertEqual(row['status'], 'fixed')
            self.assertEqual([(entry['round'], entry['status']) for entry in row['status_history']], history)


if __name__ == '__main__':
    unittest.main()
