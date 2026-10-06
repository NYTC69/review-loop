"""m7-s3: producer receipts, the legacy first-review source, the normaliser and grader records (scripts/m7_collect.py),
and a FAKE rehearsal of the legacy side and the collector end to end: a frozen case, the real reviewer launcher driven by a
fake `claude` (a tool_uses-0 call, then a clean retry), the D-b1 scan, signed receipts, normalised findings, signed grader
records and the m7_grade CLI. No provider call is made."""
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import m7_collect  # noqa: E402
import m7_grade  # noqa: E402
import m7_scan  # noqa: E402
from reviewer_permissions import REQUIRED_CLAUDE_FLAGS  # noqa: E402

REVIEW = """### VERDICT: REQUEST_CHANGES

### Issues
- [CRITICAL] f() drops the bounds check before summing — must be resolved before proceeding
  Trigger: an empty list reaches total_without_bounds_check
  Reachability: every call
  Impact: wrong totals
  Likelihood: high
  Fix cost: one line
  Cheaper response: none
  File: `a.py`, around line 2
- [MINOR] name the helper more clearly — recommended improvement
  File: `a.py`, around line 1

### Strengths
- small diff
"""

FAKE_CLAUDE = r'''
import json, os, sys
from pathlib import Path
if sys.argv[1:] == ["--help"]:
    print(os.environ["FAKE_HELP"]); sys.exit(0)
sys.stdin.read()
counter = Path(os.environ["FAKE_COUNTER"]); calls = int(counter.read_text() or "0") + 1 if counter.exists() else 1
counter.write_text(str(calls))
emit = lambda e: print(json.dumps(e), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "sess-fake-%d" % calls, "model": "claude-opus-5-5"})
if calls > 1:   # the first call answers without reading anything: tool_uses 0, the launcher refuses it
    block = {"type": "tool_use", "id": "toolu_%d" % calls, "name": "Read", "input": {"file_path": os.environ["FAKE_READ"]}}
    emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": block}})
    emit({"type": "assistant", "session_id": "sess-fake-%d" % calls, "message": {"id": "msg_fake_%d" % calls, "content": [block]}})
emit({"type": "result", "result": os.environ["FAKE_REVIEW"], "session_id": "sess-fake-%d" % calls,
      "modelUsage": {"claude-opus-5-5": {"inputTokens": 10, "outputTokens": 5}}})
'''


class CollectUnitTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def test_keygen_receipts_and_tampering(self):
        secret = m7_collect.keygen(self.tmp / 'harness' / 'secret')
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        with self.assertRaises(SystemExit): m7_collect.keygen(secret)
        key = m7_collect.load_secret(secret)
        (self.tmp / 'a.txt').write_text('raw review\n')
        receipt = m7_collect.sign(key, {'kind': 'x', 'artifacts': {'a.txt': m7_collect.sha(b'raw review\n')}})
        self.assertEqual(m7_collect.verify(key, receipt, self.tmp)['kind'], 'x')
        for broken in ({**receipt, 'payload': {**receipt['payload'], 'kind': 'y'}}, {**receipt, 'hmac_sha256': '0' * 64}):
            with self.assertRaisesRegex(SystemExit, 'HMAC'): m7_collect.verify(key, broken)
        with self.assertRaisesRegex(SystemExit, 'HMAC'): m7_collect.verify(bytes(32), receipt)
        (self.tmp / 'a.txt').write_text('edited later\n')
        with self.assertRaisesRegex(SystemExit, 'artifact changed'): m7_collect.verify(key, receipt, self.tmp)
        secret.chmod(0o644)
        with self.assertRaisesRegex(SystemExit, 'readable by others'): m7_collect.load_secret(secret)

    def test_normalise_legacy(self):
        record = m7_collect.normalise_legacy(REVIEW)
        self.assertEqual(record['status'], 'ok')
        crit, minor = record['findings']
        self.assertEqual((crit['file'], crit['line'], crit['blocking'], crit['security_only']), ('a.py', None, True, False))
        self.assertEqual(crit['text'], 'f() drops the bounds check before summing\nScenario: an empty list reaches total_without_bounds_check')
        self.assertEqual((minor['blocking'], minor['text']), (False, 'name the helper more clearly\nScenario: '))
        strengths = '\n### Strengths\n- small diff\n'
        self.assertEqual(m7_collect.normalise_legacy('### VERDICT: APPROVE\n\n### Issues\n- None.\n' + strengths), {'status': 'ok', 'findings': []})
        self.assertEqual(m7_collect.normalise_legacy('### VERDICT: APPROVE\n' + strengths)['status'], 'ok')
        for bad in ('looks fine', '### VERDICT: APPROVE\n### Issues\n- [MAJOR] x\n',   # m7-s3 R1: schema breaks are arm failures
                    '### VERDICT: APPROVE\n', '### VERDICT: APPROVE\n\n### Issues\n- None.\n',   # R2: Strengths is always required
                    '### VERDICT: REQUEST_CHANGES\n', '### VERDICT: REQUEST_CHANGES\n\n### Issues\n- None.\n',
                    '### VERDICT: REQUEST_CHANGES\n\n### Issues\n- [MINOR] only a nit\n', '### VERDICT: APPROVE\n\n### Issues\n\n### Strengths\n',
                    '### VERDICT: APPROVE\n\n### Issues\nNo issues found.\n', REVIEW.replace('REQUEST_CHANGES', 'APPROVE'),
                    REVIEW + '\n### VERDICT: APPROVE\n'):
            with self.subTest(bad=bad[:60]):
                self.assertEqual(m7_collect.normalise_legacy(bad)['status'], 'failed')

    def test_normalise_paired(self):
        row = lambda severity, security=False: {'severity': severity, 'file': 'a.py', 'summary': 's ' + severity,
                                                'failure_scenario': 'when x', 'security': security}
        record = m7_collect.normalise_paired({'full_review': [row('CRITICAL', True), row('MAJOR'), row('MINOR', True), row('SECURITY'), row('LOW')]})
        self.assertEqual([(f['blocking'], f['security_only']) for f in record['findings']],
                         [(True, False), (True, False), (False, True), (False, True), (False, False)])
        self.assertEqual(record['findings'][0]['text'], 's CRITICAL\nScenario: when x')
        self.assertEqual(m7_collect.normalise_paired({})['status'], 'failed')

    def test_grader_records_need_two_initial_votes_then_at_most_one_third(self):
        key = bytes(range(32))
        vote = {'verdict': 'FP', 'rationale': 'r', 'code_evidence': 'a.py:1'}
        bound = {'manifest_sha256': 'm', 'findings_sha256': 'f'}
        record = lambda grader, vendor, model, kind, votes, **over: m7_collect.sign(key, {
            'kind': 'm7-grader-record', 'pass': kind, 'grader': grader, 'vendor': vendor, 'model': model, 'case': 'c01',
            'arm_label': 'A', 'arm_guess': 'paired', 'votes': votes, **bound, **over})
        initial = [record('g1', 'anthropic', 'claude-opus-5-5', 'initial', {'f1': vote}),
                   record('g2', 'openai', 'gpt-6.1-sol', 'initial', {'f1': vote})]
        third = record('g3', 'anthropic', 'claude-opus-5-5', 'third', {'f1': vote})
        doc = m7_collect.adjudication(key, [third, *initial], {'A': 'legacy'}, bound)
        self.assertEqual([v['grader'] for v in doc['arms']['legacy']['c01']['f1']], ['g1', 'g2', 'g3'])
        self.assertEqual(doc['arm_guesses']['c01'][0]['actual'], 'legacy')
        for bad in ([initial[0]], [*initial, initial[0]], [*initial, third, third]):
            with self.assertRaises(SystemExit): m7_collect.adjudication(key, bad, {'A': 'legacy'}, bound)
        with self.assertRaisesRegex(SystemExit, 'unknown arm label'): m7_collect.adjudication(key, initial, {'B': 'legacy'}, bound)
        other = record('g2', 'openai', 'gpt-6.1-sol', 'initial', {'f1': vote}, findings_sha256='other')   # m7-s3 R1
        with self.assertRaisesRegex(SystemExit, 'another manifest or findings'):
            m7_collect.adjudication(key, [initial[0], other], {'A': 'legacy'}, bound)


class FakeRehearsalTest(unittest.TestCase):
    """Legacy side and collector end to end on the fake; never scored."""

    def setUp(self):
        from paired_session.test_m7_corpus import M7CorpusTest
        self.fixture = M7CorpusTest(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        run = self.fixture.run_tool(); self.assertEqual(run.returncode, 0, run.stderr)
        self.tmp, self.out = self.fixture.tmp, self.fixture.out
        self.case_dir, self.repo = self.out / 'c01', self.out / 'c01' / 'repo'
        self.key = m7_collect.load_secret(m7_collect.keygen(self.tmp / 'harness' / 'secret'))
        # The operator finalises the frozen manifest (arms, tolerance, keys digest) before any arm runs.
        self.keys = {'c01': {'split': 'synthetic', 'blockers': [{'file': 'a.py', 'start': 2, 'category': 'logic'}]}}
        self.kb = json.dumps(self.keys).encode()
        manifest = {**json.loads((self.out / 'frozen-manifest.json').read_text()), 'arms': ['legacy', 'paired'],
                    'line_tolerance': 2, 'keys_sha256': m7_collect.sha(self.kb)}
        self.mb = json.dumps(manifest).encode(); (self.out / 'frozen-manifest.json').write_bytes(self.mb)
        result = json.loads((self.out / 'result.json').read_text()); result['manifest_sha256'] = m7_collect.sha(self.mb)
        (self.out / 'result.json').write_text(json.dumps(result))
        self.frozen, self.case, self.result = manifest, manifest['cases'][0], result

    def legacy_reviews(self):   # the real launcher, twice, as the legacy orchestrator would call it
        bin_dir = self.tmp / 'bin'; bin_dir.mkdir()
        (bin_dir / 'claude').write_text(f'#!{sys.executable}\n' + FAKE_CLAUDE); (bin_dir / 'claude').chmod(0o755)
        tmp_dir = self.repo / '.review-loop' / 'tmp'; tmp_dir.mkdir(parents=True)
        (tmp_dir / 's1-reviewer-prompt.txt').write_text('review the staged diff\n')
        env = {'PATH': f'{bin_dir}:/usr/bin:/bin', 'HOME': str(self.case_dir / 'home'), 'FAKE_HELP': ' '.join(REQUIRED_CLAUDE_FLAGS),
               'FAKE_COUNTER': str(self.tmp / 'calls'), 'FAKE_READ': str(self.repo / 'a.py'), 'FAKE_REVIEW': REVIEW}
        command = [sys.executable, str(SCRIPTS / 'run_claude_reviewer.py'), '--session-id', 's1', '--model', 'claude-opus-5-5',
                   '--stage', 'execution', '--role', 'reviewer', '--tmp-dir', '.review-loop/tmp', '--timeout-seconds', '60']
        outputs = [subprocess.run(command, cwd=self.repo, env=env, capture_output=True, text=True) for _ in range(2)]
        self.assertEqual([o.returncode for o in outputs], [8, 0], [o.stdout + o.stderr for o in outputs])
        lines = []   # the orchestrator's stream-json: each launcher call and its stdout as the tool result
        for n, output in enumerate(outputs, 1):
            call = {'type': 'tool_use', 'id': f'toolu_o{n}', 'name': 'Bash',
                    'input': {'command': 'python3 /pinned/review-loop/scripts/run_claude_reviewer.py --session-id s1 --tmp-dir .review-loop/tmp'}}
            lines.append(json.dumps({'type': 'assistant', 'session_id': 'orch-1', 'message': {'id': f'msg_o{n}', 'content': [call]}}))
            lines.append(json.dumps({'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': f'toolu_o{n}', 'content': output.stdout}]}}))
        orchestrator = self.case_dir / 'run' / 'orchestrator.stream.jsonl'
        orchestrator.write_text('\n'.join(lines) + '\n')
        return orchestrator, tmp_dir

    def policy(self, arm, artifacts):
        return {'case_dir': str(self.case_dir), 'case': 'c01', 'arm': arm, 'base': self.case['base'],
                'diff_sha256': self.case['diff_sha256'], 'home': str(self.case_dir / 'home'), 'keys_dir': str(self.tmp / 'keys'),
                'allow': ['/pinned/review-loop'], 'artifacts': artifacts}

    def collect(self, extra_orchestrator_line=None):
        orchestrator, tmp_dir = self.legacy_reviews()
        if extra_orchestrator_line:
            orchestrator.write_text(orchestrator.read_text() + extra_orchestrator_line + '\n')
        first = m7_collect.legacy_first_review(orchestrator, tmp_dir)
        self.assertEqual(first['result'].read_text(), REVIEW)   # the clean retry, not the tool_uses-0 call
        streams = sorted(tmp_dir.glob('s1-reviewer-*/stream.jsonl'))
        self.assertEqual(len(streams), 2)
        artifacts = [{'name': 'orchestrator', 'path': str(orchestrator), 'kind': 'claude-stream'}] + [
            {'name': 'reviewer-' + p.parent.name[-6:], 'path': str(p), 'kind': 'claude-stream'} for p in streams]
        legacy_scan = m7_scan.scan(self.policy('legacy', artifacts))
        legacy = m7_collect.arm_receipt(self.key, self.case, self.frozen_sha(), 'legacy', 1, self.case_dir,
                                        {'orchestrator': orchestrator, 'raw_review': first['result'], 'reviewer_stream': first['stream']},
                                        m7_collect.normalise_legacy(first['result'].read_text()), legacy_scan)
        paired_stream = self.case_dir / 'run' / '007-exec-reviewer.stdout.jsonl'
        paired_stream.write_text(json.dumps({'type': 'assistant', 'session_id': 'paired-1', 'message': {'id': 'msg_p', 'content': [
            {'type': 'tool_use', 'id': 'toolu_p', 'name': 'Read', 'input': {'file_path': str(self.repo / 'a.py')}}]}}) + '\n')
        answer = {'full_review': [{'severity': 'MAJOR', 'file': 'a.py', 'summary': 'bounds check removed from f()',
                                   'failure_scenario': 'an empty list', 'security': False}]}
        paired_scan = m7_scan.scan(self.policy('paired', [{'name': 'reviewer', 'path': str(paired_stream), 'kind': 'claude-stream'}]))
        paired = m7_collect.arm_receipt(self.key, self.case, self.frozen_sha(), 'paired', 1, self.case_dir,
                                        {'reviewer_stream': paired_stream}, m7_collect.normalise_paired(answer), paired_scan)
        return legacy_scan, legacy, paired_scan, paired

    def frozen_sha(self):
        return m7_collect.sha((self.out / 'frozen-manifest.json').read_bytes())

    def grade_inputs(self, receipts, voided=None):
        findings = m7_collect.findings_document(self.key, receipts, ['legacy', 'paired'], self.mb, self.out, 1, voided)
        return self.keys, self.kb, self.mb, self.result, findings

    def grader_records(self, findings, keys, binding):
        seed = m7_grade.seed_id(keys['c01']['blockers'][0])
        records = []
        for label, arm in (('A', 'legacy'), ('B', 'paired')):
            votes = {}
            for f in findings['arms'][arm]['c01']['findings']:
                if not f['blocking']: continue
                votes[m7_grade.finding_id(f)] = {'verdict': 'HIT', 'seed': seed, 'category': 'logic', 'mechanism_match': True,
                                                 'rationale': 'removes the bounds check', 'code_evidence': 'a.py:2'}
            for grader, vendor, model in (('g1', 'anthropic', 'claude-opus-5-5'), ('g2', 'openai', 'gpt-6.1-sol')):
                records.append(m7_collect.sign(self.key, {'kind': 'm7-grader-record', 'pass': 'initial', 'grader': grader,
                                                          'vendor': vendor, 'model': model, 'case': 'c01', 'arm_label': label,
                                                          'arm_guess': 'legacy', 'votes': votes,
                                                          'manifest_sha256': binding['manifest_sha256'],
                                                          'findings_sha256': binding['findings_sha256']}))
        return records

    def run_grade(self, keys, kb, result, findings, adjudication):
        paths = {}
        for name, doc in (('keys', None), ('findings', findings), ('adjudication', adjudication)):
            paths[name] = self.tmp / (name + '.json')
            if doc is None: paths[name].write_bytes(kb)
            else: paths[name].write_text(json.dumps(doc))
        out = self.tmp / 'grade'
        run = subprocess.run([sys.executable, str(SCRIPTS / 'm7_grade.py'), str(self.out / 'frozen-manifest.json'), str(self.out / 'result.json'),
                              str(paths['findings']), str(paths['keys']), str(out), str(paths['adjudication'])], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(out.with_suffix('.json').read_text())

    def test_clean_rehearsal_scores_through_the_grade_cli(self):
        legacy_scan, legacy, paired_scan, paired = self.collect()
        self.assertEqual((legacy_scan['status'], paired_scan['status']), ('CLEAN', 'CLEAN'))
        self.assertGreaterEqual(legacy_scan['tool_calls'], 3)   # two launcher calls and the reviewer's Read
        ids = legacy['payload']['provider_ids']
        self.assertIn('orch-1', ids['session_ids']); self.assertIn('msg_fake_2', ids['message_ids'])
        m7_collect.verify(self.key, legacy, self.case_dir)   # every collected byte is still the signed one
        keys, kb, mb, result, findings = self.grade_inputs([legacy, paired])
        binding = {'manifest_sha256': m7_collect.sha(mb), 'keys_sha256': m7_collect.sha(kb),
                   'result_sha256': m7_grade.digest(result), 'findings_sha256': m7_grade.digest(findings)}
        adjudication = m7_collect.adjudication(self.key, self.grader_records(findings, keys, binding), {'A': 'legacy', 'B': 'paired'}, binding)
        report = self.run_grade(keys, kb, result, findings, adjudication)   # .review-loop/ artifacts stay allowed (m7-s1)
        self.assertTrue(report['score_ready'])
        self.assertEqual((report['arms']['legacy']['hits'], report['arms']['paired']['hits']), (1, 1))

    def test_a_scan_violation_voids_the_case_with_bound_evidence(self):
        operator = pwd.getpwuid(os.getuid()).pw_dir
        leak = json.dumps({'type': 'assistant', 'session_id': 'orch-1', 'message': {'id': 'msg_leak', 'content': [
            {'type': 'tool_use', 'id': 'toolu_leak', 'name': 'Read', 'input': {'file_path': operator + '/.claude/CLAUDE.md'}}]}})
        legacy_scan, legacy, _, paired = self.collect(leak)
        self.assertEqual(legacy_scan['status'], 'VIOLATION')
        self.assertEqual(legacy_scan['violations'][0]['policy_rule'], 'deny-list')
        with self.assertRaisesRegex(SystemExit, 'not CLEAN'):
            m7_collect.findings_document(self.key, [legacy, paired], ['legacy', 'paired'], self.mb, self.out, 1)
        evidence = m7_scan.exclusion_evidence(legacy_scan)
        keys, kb, mb, result, findings = self.grade_inputs([legacy, paired], {'c01': evidence['reason']})
        binding = {'manifest_sha256': m7_collect.sha(mb), 'keys_sha256': m7_collect.sha(kb),
                   'result_sha256': m7_grade.digest(result), 'findings_sha256': m7_grade.digest(findings)}
        adjudication = m7_collect.adjudication(self.key, [], {}, binding, {'c01': evidence})
        report = self.run_grade(keys, kb, result, findings, adjudication)
        self.assertEqual((report['counted_cases'], list(report['voided_not_counted'])), ([], ['c01']))

    def test_a_forged_edited_or_replayed_receipt_never_reaches_the_findings(self):
        _, legacy, _, paired = self.collect()
        document = lambda receipts, mb=None, window=1: m7_collect.findings_document(
            self.key, receipts, ['legacy', 'paired'], mb or self.mb, self.out, window)
        forged = {**legacy, 'payload': {**legacy['payload'], 'findings_record': {'status': 'ok', 'findings': []}}}
        with self.assertRaisesRegex(SystemExit, 'HMAC'): document([forged, paired])
        with self.assertRaisesRegex(SystemExit, 'two receipts'): document([legacy, legacy])
        # m7-s3 R1: replayed into another window or another frozen manifest, or bytes changed after signing
        with self.assertRaisesRegex(SystemExit, 'another manifest, window'): document([legacy, paired], window=2)
        with self.assertRaisesRegex(SystemExit, 'another manifest, window'): document([legacy, paired], mb=self.mb + b' ')
        raw = self.case_dir / sorted(legacy['payload']['artifacts'])[0]
        raw.write_bytes(raw.read_bytes() + b'edited after signing\n')
        with self.assertRaisesRegex(SystemExit, 'artifact changed'): document([legacy, paired])

    def test_a_receipt_signs_only_bytes_the_scan_covered(self):   # m7-s3 R1
        legacy_scan, _, _, _ = self.collect()
        orchestrator = Path(legacy_scan['artifacts']['orchestrator']['path'])
        sign = lambda artifacts, scan: m7_collect.arm_receipt(self.key, self.case, m7_collect.sha(self.mb), 'legacy', 1, self.case_dir,
                                                              artifacts, {'status': 'ok', 'findings': []}, scan)
        unscanned = self.case_dir / 'run' / 'other.stream.jsonl'; unscanned.write_text('{}\n')
        with self.assertRaisesRegex(SystemExit, 'not D-b1 scanned'): sign({'orchestrator': orchestrator, 'other': unscanned}, legacy_scan)
        with self.assertRaisesRegex(SystemExit, 'not this case'): sign({'orchestrator': orchestrator}, {**legacy_scan, 'base': '0' * 40})
        orchestrator.write_text(orchestrator.read_text() + '{"type": "result"}\n')
        with self.assertRaisesRegex(SystemExit, 'changed since the D-b1 scan'): sign({'orchestrator': orchestrator}, legacy_scan)


if __name__ == '__main__':
    unittest.main()
