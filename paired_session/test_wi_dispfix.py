"""WI-DISPFIX: disposition retries and replay of a durable EXEC review."""
import json
import os
import unittest
from contextlib import contextmanager, nullcontext
from unittest.mock import patch

from paired_session import test_real_coordinator as trc

rc = trc.rc
EMPTY_EVIDENCE = 'EXEC APPROVE rejected: empty self_run_evidence'
RETRY_MARKER = 'This is the one allowed protocol retry'


class DispositionRetryTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.coordinator_fixture(self)
        self.co = self.start_review()

    def start_review(self, *extra):
        co = self.coordinator('run', *extra)
        co.state.update(phase='EXEC', next='reviewer', exec_rounds=1, sequence=1)
        co.record_findings('persistent-reviewer', 'EXEC', 1, [
            {'severity': 'MAJOR', 'file': 'tracked.txt', 'summary': summary}
            for summary in ('First finding', 'Second finding')])
        co.save()
        return co

    def coordinator(self, action='run', *extra):
        argv = self.h.command('--lifecycle-mode', 'off', '--shadow', 'off', '--adversarial-gate', 'off',
                              '--polish-round', 'off', '--skip-probe', *extra)[2:]
        argv[0] = action
        return rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))

    def prompt(self, co, turn):
        return (co.evidence / f"{turn['sequence']:03d}-exec-reviewer.prompt.txt").read_text()

    @contextmanager
    def quota(self, marker):
        now = int(rc.time.time())
        waits = []
        with patch.dict(os.environ, {
            'FAKE_RATE_LIMIT': '1', 'FAKE_RATE_LIMIT_MATCH': marker,
            'FAKE_RATE_LIMIT_ONCE': str(self.h.run_dir) + '.limited',
            'FAKE_RATE_LIMIT_RESET_AT': str(now + 30),
        }), patch.object(rc.time, 'time', side_effect=lambda: now + sum(waits)), \
                patch.object(rc, 'sleep_rate_limit', side_effect=waits.append):
            yield waits

    def test_unrequested_dispositions_retry_once(self):
        for kind in ('closed', 'open', 'owner'):
            with self.subTest(kind=kind):
                self.h.run_dir = self.h.root / kind
                if kind == 'owner':
                    co = self.start_review('--lifecycle-mode', 'on', '--adversarial-gate', 'on')
                else:
                    co = self.start_review()
                source = {'closed': 'persistent-reviewer', 'open': 'scenario-matrix', 'owner': 'specialist:python-reviewer'}
                [extra] = co.record_findings(source[kind], 'EXEC', 1, [
                    {'severity': 'MINOR', 'file': 'tracked.txt', 'summary': 'Unrequested finding'}], finding_indexes=[2])
                finding_id = extra['id']
                row = co.state['finding_ledger'][-1]
                if kind == 'closed':
                    co.apply_dispositions([{'id': finding_id, 'disposition': 'fixed', 'evidence': 'closed earlier'}],
                                          1, [finding_id])
                original_history = list(row['status_history'])
                collect = co._collect

                def add_unrequested(*args):
                    parsed = collect(*args)
                    self.assertNotIn(finding_id, [entry['id'] for entry in parsed[0]['prior_findings']])
                    if co.state['sequence'] == 2 or kind == 'owner':
                        parsed[0]['prior_findings'].append(
                            {'id': finding_id, 'disposition': 'still_open', 'evidence': 'unrequested'})
                    if co.state['sequence'] == 3:
                        self.assertEqual([entry['status'] for entry in co.state['finding_ledger'][:2]], ['open', 'open'])
                    return parsed

                with patch.object(co, '_collect', side_effect=add_unrequested):
                    co.reviewer_turn()
                self.assertEqual(len(co.state['turns']), 2, co.state.get('hold_reason'))
                rejected, retry = co.state['turns']
                self.assertEqual(rejected['discarded'], 'invalid finding dispositions')
                self.assertEqual(retry['open_finding_ids'], [entry['id'] for entry in co.state['finding_ledger'][:2]])
                self.assertIn('unrequested id: ' + finding_id, self.prompt(co, retry))
                self.assertIn('return exactly the requested dispositions', self.prompt(co, retry))
                self.assertEqual(row['status_history'], original_history)
                if kind == 'owner':
                    self.assertEqual(co.state['status'], 'HOLD')
                    self.assertIn('unrequested id: ' + finding_id, co.state['hold_reason'])
                else:
                    self.assertEqual(co.state['status'], 'DONE', co.state.get('hold_reason'))
                    self.assertEqual([entry['status'] for entry in co.state['finding_ledger'][:2]], ['fixed', 'fixed'])
                    self.assertEqual(co.state['exec_rounds'], 1)

    def test_resume_quota_wait_during_reviewer_retry(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                self.h.run_dir = self.h.root / f'quota-{malformed}'
                co = self.start_review()
                missing_id = co.state['finding_ledger'][-1]['id']
                feedback = 'omitted dispositions for: ' + missing_id
                collect = co._collect

                def omit_disposition(*args):
                    parsed = collect(*args)
                    parsed[0]['prior_findings'].pop()
                    return parsed

                with self.quota(RETRY_MARKER) as waits:
                    with patch.object(co, '_collect', side_effect=omit_disposition), \
                            patch.object(rc, 'sleep_rate_limit', side_effect=rc.DetachStop), \
                            self.assertRaises(rc.DetachStop):
                        co.drive()
                    resumed = self.coordinator('resume')
                    self.assertIsNone(resumed.state['active'])
                    self.assertEqual(resumed.state['rate_limit_wait_used'], 30)
                    self.assertEqual(resumed.state['invocations_used'], 1)
                    collect = resumed._collect
                    with patch.object(resumed, '_collect', side_effect=omit_disposition) if malformed else nullcontext():
                        status = resumed.resume()
                self.assertEqual(waits, [30])
                self.assertEqual(resumed.state['rate_limit_wait_used'], 30)
                self.assertEqual(resumed.state['turns'][1]['error_kind'], 'rate_limited')
                retry = resumed.state['turns'][2]
                self.assertEqual(self.prompt(resumed, retry).count(feedback), 1)
                self.assertEqual(len(resumed.state['turns']), 3)
                self.assertEqual(resumed.state['invocations_used'], 2)
                self.assertEqual(status, 'HOLD' if malformed else 'DONE')
                self.assertNotIn('reviewer_retry', resumed.state)
                if malformed:
                    self.assertEqual(resumed.state['hold_reason'],
                                     'reviewer omitted open finding dispositions after retry: ' + missing_id)
                    self.assertIsNone(resumed.state['pending_reviewer_result_sequence'])

    def test_empty_exec_approval_retries_once(self):
        self.h.run_dir = self.h.root / 'evidence-retry'
        co = self.start_review('--shadow', 'on')
        collect = co._collect
        sessions = []

        def omit_evidence(*args):
            parsed = collect(*args)
            if co.state['active']['role'] != 'reviewer':
                return parsed
            sessions.append(parsed[1])
            if co.state['sequence'] == 2:
                parsed[0]['self_run_evidence'] = []
                parsed[0]['full_review'] = [
                    {'severity': 'MINOR', 'file': 'tracked.txt', 'summary': 'Discarded finding',
                     'failure_scenario': 'A caller receives an incorrect result'}]
            else:
                self.assertEqual([row['status'] for row in co.state['finding_ledger']], ['open', 'open'])
                self.assertFalse(co.state['review_verdicts'])
            return parsed

        with patch.object(co, '_collect', side_effect=omit_evidence):
            co.reviewer_turn()
        self.assertEqual(co.state['status'], 'DONE', co.state.get('hold_reason'))
        rejected, retry, shadow = co.state['turns']
        self.assertEqual([turn['role'] for turn in (rejected, retry, shadow)], ['reviewer', 'reviewer', 'shadow'])
        self.assertEqual(rejected['discarded'], EMPTY_EVIDENCE)
        self.assertTrue(co.configured_test_succeeded(rejected))
        self.assertNotIn('reused_test_evidence', retry)
        self.assertTrue(co.configured_test_succeeded(retry))
        prompt = self.prompt(co, retry)
        self.assertIn(EMPTY_EVIDENCE, prompt)
        self.assertIn('list the checks you actually ran in self_run_evidence', prompt)
        self.assertNotIn('return exactly the requested dispositions', prompt)
        self.assertIn('Rerun the configured test command and report it in self_run_evidence.', prompt)
        self.assertEqual([row['status'] for row in co.state['finding_ledger']], ['fixed', 'fixed'])
        self.assertEqual(co.state['exec_rounds'], 1)
        self.assertTrue(sessions[0])
        self.assertEqual(sessions, [sessions[0], sessions[0]])

    def test_ignored_closed_dispositions_are_audited_on_replay(self):
        co = self.co
        [requested, fixed] = co.state['finding_ledger']
        co.record_findings('persistent-reviewer', 'EXEC', 1, [
            {'severity': 'MAJOR', 'file': 'tracked.txt', 'summary': summary}
            for summary in ('Withdrawn by reviewer', 'Withdrawn by operator')], finding_indexes=[2, 3])
        withdrawn, operator = co.state['finding_ledger'][-2:]
        co.apply_dispositions([
            {'id': fixed['id'], 'disposition': 'fixed', 'evidence': 'earlier fix'},
            {'id': withdrawn['id'], 'disposition': 'withdrawn', 'evidence': 'earlier withdrawal'},
        ], 1, [fixed['id'], withdrawn['id']])
        operator['status'] = 'withdrawn'
        co.state['operator_rulings'] = [{'id': 'O001', 'finding_ids': [operator['id']], 'reason': 'keep behavior'}]
        stale = [{'id': row['id'], 'disposition': disposition, 'evidence': 'stale ' + row['summary']}
                 for row, disposition in ((fixed, 'fixed'), (withdrawn, 'withdrawn'), (operator, 'fixed'))]
        collect = co._collect

        def add_stale_rows(*args):
            parsed = collect(*args)
            parsed[0]['prior_findings'].extend(stale)
            return parsed

        def stop_after_apply(result, *_args):
            self.assertEqual([row['id'] for row in result['answer']['prior_findings']], [requested['id']])
            co.save()
            raise rc.DetachStop()

        with patch.object(co, '_collect', side_effect=add_stale_rows), \
                patch.object(co, 'render', side_effect=stop_after_apply), self.assertRaises(rc.DetachStop):
            co.reviewer_turn()
        resumed = self.coordinator('resume')
        history = [list(row['status_history']) for row in resumed.state['finding_ledger']]
        expected = [{**stale[-1], 'ignored_reason': 'operator-withdrawn'},
                    *({**row, 'ignored_reason': 'closed-unrequested'} for row in stale[:2])]
        receipt_path = co.evidence / '002-exec-reviewer.receipt.json'

        def assert_audit():
            for receipt in (resumed.state['turns'][0], json.loads(receipt_path.read_text())):
                self.assertEqual(receipt['ignored_prior_findings'], expected)
                self.assertEqual(receipt['answer']['prior_findings'][1:], stale)

        assert_audit()
        # Older operator-only receipts could record the same ignored row twice.
        resumed.state['turns'][0]['ignored_prior_findings'].append(expected[0])
        with patch.object(resumed, 'invoke') as invoke:
            self.assertEqual(resumed.resume(), 'DONE')
        invoke.assert_not_called()
        assert_audit()
        self.assertEqual([row['status_history'] for row in resumed.state['finding_ledger']], history)

    def test_rate_limited_hold_keeps_reviewer_retry(self):
        for budget in ('0', '20'):
            with self.subTest(budget=budget):
                self.h.run_dir = self.h.root / f'quota-hold-{budget}'
                options = ('--max-rate-limit-wait', budget)
                co = self.start_review(*options)
                missing_id = co.state['finding_ledger'][-1]['id']
                with self.quota(RETRY_MARKER) as waits, patch.dict(os.environ, {'FAKE_ALWAYS_OMIT_DISPOSITION': '1'}):
                    self.assertEqual(co.drive(), 'HOLD')
                    self.assertEqual(co.state['hold_kind'], 'rate_limited')
                    resumed = self.coordinator('resume', *options)
                    self.assertEqual(resumed.resume(), 'HOLD')
                self.assertEqual(waits, [])
                rejected, quota, retry = resumed.state['turns']
                self.assertEqual(quota['error_kind'], 'rate_limited')
                self.assertEqual(self.prompt(resumed, retry).count('omitted dispositions for: ' + missing_id), 1)
                self.assertEqual(resumed.state['invocations_used'], 2)
                self.assertEqual(rejected['discarded'], retry['discarded'])
                self.assertEqual(resumed.state['hold_reason'],
                                 'reviewer omitted open finding dispositions after retry: ' + missing_id)
                self.assertNotIn('reviewer_retry', resumed.state)
                self.assertIsNone(resumed.state['pending_reviewer_result_sequence'])

    def test_legacy_reviewer_quota_wait_resumes(self):
        co = self.co
        with self.quota('Phase: EXEC.') as waits:
            with patch.object(rc, 'sleep_rate_limit', side_effect=rc.DetachStop), self.assertRaises(rc.DetachStop):
                co.drive()
            resumed = self.coordinator('resume')
            self.assertNotIn('reviewer_retry', resumed.state)
            self.assertEqual(resumed.resume(), 'DONE')
        self.assertEqual(waits, [30])
        quota, review = resumed.state['turns']
        self.assertEqual(quota['error_kind'], 'rate_limited')
        self.assertNotIn(RETRY_MARKER, self.prompt(resumed, review))
        self.assertEqual(resumed.state['invocations_used'], 1)

    def hold_on_failed_evidence_correction(self):
        co = self.co
        # This text does not match the transient dispatch retry classifier.
        failure = 'fake correction dispatch failed'
        with patch.dict(os.environ, {'FAKE_EXEC_NO_EVIDENCE': '1', 'FAKE_PROVIDER_FAIL': failure,
                                     'FAKE_PROVIDER_FAIL_MATCH': RETRY_MARKER}):
            self.assertEqual(co.drive(), 'HOLD')
        self.assertNotEqual(co.state.get('hold_kind'), 'rate_limited')
        self.assertIn(failure, co.state['hold_reason'])
        rejected, failed = co.state['turns']
        self.assertEqual(rejected['discarded'], EMPTY_EVIDENCE)
        self.assertIn('error', failed)
        record = co.state['reviewer_retry']
        self.assertEqual(record['rejected_sequence'], rejected['sequence'])
        self.assertIn(EMPTY_EVIDENCE, record['correction'])
        return record, rejected, failed

    def test_author_turn_in_same_phase_starts_fresh_reviewer_retry(self):
        record, rejected, failed = self.hold_on_failed_evidence_correction()
        resumed = self.coordinator('resume')
        resumed.state['next'] = 'author'
        resumed.save()
        with patch.dict(os.environ, {'FAKE_OMIT_DISPOSITION': '1'}):
            status = resumed.resume()
        author, review, *corrections = resumed.state['turns'][2:]
        self.assertEqual((author['role'], author['phase']), ('author', 'EXEC'))
        self.assertEqual((review['role'], review['phase']), ('reviewer', 'EXEC'))
        self.assertNotIn(RETRY_MARKER, self.prompt(resumed, review))
        self.assertNotIn(record['correction'], self.prompt(resumed, review))
        [correction] = corrections
        prompt = self.prompt(resumed, correction)
        self.assertEqual(prompt.count(RETRY_MARKER), 1)
        self.assertIn('omitted dispositions for: ' + self.co.state['finding_ledger'][-1]['id'], prompt)
        self.assertNotIn(EMPTY_EVIDENCE, prompt)
        self.assertEqual(resumed.state['turns'][:2], [rejected, failed])
        self.assertEqual(status, 'DONE', resumed.state.get('hold_reason'))

    def test_direct_resume_keeps_failed_evidence_correction(self):
        record, rejected, failed = self.hold_on_failed_evidence_correction()
        resumed = self.coordinator('resume')
        session = resumed.state['sessions']['reviewer']
        self.assertEqual(resumed.resume(), 'DONE', resumed.state.get('hold_reason'))
        self.assertEqual(resumed.state['turns'][:2], [rejected, failed])
        [retry] = resumed.state['turns'][2:]
        self.assertEqual(self.prompt(resumed, retry).count(record['correction']), 1)
        self.assertTrue(session)
        at = retry['command'].index('--resume')
        self.assertEqual(retry['command'][at + 1], session)
        self.assertEqual(resumed.state['sessions']['reviewer'], session)

    def test_recorded_retry_replays_without_dispatch(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                self.h.run_dir = self.h.root / f'replay-retry-{malformed}'
                co = self.start_review()
                invoke = co.invoke

                def stop_after_retry(role, phase, prompt, schema):
                    result = invoke(role, phase, prompt, schema)
                    if RETRY_MARKER in prompt:
                        raise rc.DetachStop()
                    return result

                flag = 'FAKE_ALWAYS_OMIT_DISPOSITION' if malformed else 'FAKE_OMIT_DISPOSITION'
                with patch.object(co, 'invoke', side_effect=stop_after_retry), patch.dict(os.environ, {flag: '1'}), \
                        self.assertRaises(rc.DetachStop):
                    co.reviewer_turn()
                resumed = self.coordinator('resume')
                rejected, retry = resumed.state['turns']
                self.assertEqual(rejected['discarded'], 'missing finding dispositions')
                self.assertEqual(resumed.state['pending_reviewer_result_sequence'], retry['sequence'])
                with patch.object(resumed, 'invoke') as dispatch:
                    self.assertEqual(resumed.resume(), 'HOLD' if malformed else 'DONE')
                dispatch.assert_not_called()
                self.assertIsNone(resumed.state['pending_reviewer_result_sequence'])
                self.assertNotIn('reviewer_retry', resumed.state)

    def test_mixed_reviewer_errors_share_one_retry(self):
        for first_empty in (False, True):
            with self.subTest(first_empty=first_empty):
                self.h.run_dir = self.h.root / f'mixed-{first_empty}'
                co = self.start_review()
                collect = co._collect

                def malformed(*args):
                    parsed = collect(*args)
                    if (co.state['sequence'] == 2) == first_empty:
                        parsed[0]['self_run_evidence'] = []
                    else:
                        parsed[0]['prior_findings'].pop()
                    return parsed

                with patch.object(co, '_collect', side_effect=malformed):
                    co.reviewer_turn()
                self.assertEqual(co.state['status'], 'HOLD')
                rejected, retry = co.state['turns']
                saved = json.loads((self.h.run_dir / 'state.json').read_text())
                self.assertEqual(saved['turns'], [rejected, retry])
                for turn in (rejected, retry):
                    receipt = co.evidence / f"{turn['sequence']:03d}-exec-reviewer.receipt.json"
                    self.assertEqual(json.loads(receipt.read_text()), turn)
                self.assertEqual(rejected['discarded'], EMPTY_EVIDENCE if first_empty else 'missing finding dispositions')
                self.assertNotIn('approval_blocked_reason', rejected)
                if first_empty:
                    self.assertEqual(retry['discarded'], 'missing finding dispositions')
                    self.assertNotIn('approval_blocked_reason', retry)
                else:
                    self.assertEqual(retry['approval_blocked_reason'], EMPTY_EVIDENCE)
                    self.assertNotIn('discarded', retry)
                    self.assertTrue(co.configured_test_succeeded(retry))
                    self.assertEqual([row['status'] for row in saved['finding_ledger']], ['fixed', 'fixed'])
                    [verdict] = saved['review_verdicts']
                    self.assertEqual((verdict['sequence'], verdict['reviewer_raw_verdict'], verdict['effective_verdict']),
                                     (retry['sequence'], 'APPROVE', 'APPROVE'))
                    self.assertEqual(co._test_proof(retry['snapshot_after']), {})
                    with patch.dict(co.state, {'pending_reviewer_result_sequence': retry['sequence']}):
                        self.assertIsNone(co._recorded_reviewer_result('EXEC'))
                self.assertIn(EMPTY_EVIDENCE if first_empty else 'omitted dispositions for:',
                              self.prompt(co, co.state['turns'][-1]))
                missing_id = co.state['finding_ledger'][-1]['id']
                self.assertEqual(co.state['hold_reason'],
                                 'reviewer omitted open finding dispositions after retry: ' + missing_id
                                 if first_empty else EMPTY_EVIDENCE)
                self.assertNotIn('reviewer_retry', co.state)

    def test_one_correction_addresses_both_disposition_and_evidence_errors(self):
        co = self.co
        collect = co._collect

        def omit_both(*args):
            parsed = collect(*args)
            if co.state['sequence'] == 2:
                parsed[0]['prior_findings'].pop()
                parsed[0]['self_run_evidence'] = []
            return parsed

        with patch.object(co, '_collect', side_effect=omit_both):
            co.reviewer_turn()
        self.assertEqual(co.state['status'], 'DONE', co.state.get('hold_reason'))
        rejected, retry = co.state['turns']
        self.assertEqual(rejected['discarded'], 'missing finding dispositions')
        prompt = self.prompt(co, retry)
        self.assertEqual(prompt.count(RETRY_MARKER), 1)
        self.assertIn('omitted dispositions for: ' + co.state['finding_ledger'][-1]['id'], prompt)
        self.assertIn('return exactly the requested dispositions', prompt)
        self.assertIn('omitted dispositions for: ' + co.state['finding_ledger'][-1]['id'] +
                      ' and had empty self_run_evidence.', prompt)
        self.assertIn('Rerun the configured test command and report it in self_run_evidence.', prompt)

    def test_second_empty_approval_leaving_blockers_routes_to_author(self):
        co = self.co
        collect = co._collect

        def empty_with_open_rows(*args):
            parsed = collect(*args)
            parsed[0]['self_run_evidence'] = []
            for row in parsed[0]['prior_findings']:
                row['disposition'] = 'still_open'
            return parsed

        with patch.object(co, '_collect', side_effect=empty_with_open_rows):
            co.reviewer_turn()
        self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'author'))
        rejected, retry = co.state['turns']
        self.assertEqual(rejected['discarded'], EMPTY_EVIDENCE)
        self.assertNotIn('discarded', retry)
        self.assertNotIn('approval_blocked_reason', retry)
        self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'], 'REVISE')
        self.assertNotIn('reviewer_retry', co.state)

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
        self.assertIn('return exactly the requested dispositions', prompt)
        self.assertNotIn('If you approve, list the checks', prompt)
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
