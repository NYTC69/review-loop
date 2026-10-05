"""Tests for scripts/m7_grade.py with tiny synthetic inputs."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import copy
import itertools
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'm7_grade.py'
sys.path.insert(0, str(SCRIPT.parent))
import m7_grade  # noqa: E402


def finding(file='a.py', line=10, blocking=True, **extra):
    return dict(file=file, line=line, blocking=blocking, **{'text': 't', **extra})


def ok(*findings):
    return {'status': 'ok', 'findings': list(findings)}


def key(*blockers, split='synthetic'):
    return {'split': split, 'blockers': [{'category': 'logic', **b} for b in blockers], **({} if blockers else {'clean': True})}


def blocker(file='a.py', start=10, **extra):
    return dict(file=file, start=start, **extra)


def manifest(tol=2, arms=('x',)):
    return json.dumps({'line_tolerance': tol, 'arms': list(arms)}).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def bind(res, keys, man=None, over=None):
    """Add the freeze bindings (cases, keys_sha256) to a manifest; re-bind RESULT only if it was bound to it."""
    man = man or manifest()
    body = json.loads(man)
    body.setdefault('cases', [{k: v for k, v in c.items() if k != 'scan'} for c in res['cases']])
    body.setdefault('keys_sha256', sha(json.dumps(keys).encode()))
    new = json.dumps({**body, **(over or {})}).encode()
    return new, (dict(res, manifest_sha256=sha(new)) if res['manifest_sha256'] == sha(man) else res)


def result(*cases, man=None):
    man = man or manifest()
    records = []
    for c, status in cases:
        r = dict(id=c, status=status, base='1'*40, source_base_tree='2'*40, base_tree='3'*40,
                 tree='4'*40, diff_sha256='5'*64, key_sha256='6'*64, scan_sha256=None)
        if status == 'excluded':
            scan = dict(kind='D-b2', case=c, base=r['base'], diff_sha256=r['diff_sha256'],
                        location='pinned source', matched_count=1, matched_unit_sha256=['7'*64])
            r.update(scan=scan, scan_sha256=sha(json.dumps(scan, sort_keys=True).encode()))
        records.append(r)
    return {'manifest_sha256': sha(man), 'cases': records}


def votes(verdict, seed=None, category=None, **extra):
    return [dict(grader=g, vendor=v, model=m, verdict=verdict, seed=seed, category=category,
                 mechanism_match=verdict == 'HIT', rationale='Synthetic independent assessment',
                 code_evidence='a.py:10: synthetic mechanism evidence', **extra)
            for g, v, m in [('g1', 'anthropic', 'opus'), ('g2', 'other-vendor', 'test-model')]]


def adjudication(man, res, findings, kb, decisions=None):
    return {'binding': {'manifest_sha256': sha(man), 'result_sha256': m7_grade.digest(res),
                        'findings_sha256': m7_grade.digest(findings), 'keys_sha256': sha(kb)},
            'arms': decisions or {}}


def legacy_fixture_decisions(recs, keys, tol):
    # Explicit synthetic adjudicator fixture for old score-aggregation tests.
    # This helper is NOT production mechanism detection.
    out = {}
    for cid, rec in recs.items():
        if cid not in keys or rec.get('status') != 'ok': continue
        ds = {}; used = set()
        for f in sorted(rec['findings'], key=lambda f: f.get('line') if type(f.get('line')) is int else -1):
            if type(f.get('line')) is not int: continue
            candidates = [b for b in keys[cid]['blockers'] if m7_grade.hit(f, b, tol)]
            b = next((b for b in candidates if m7_grade.seed_id(b) not in used), None)
            if b is not None:
                sid = m7_grade.seed_id(b); used.add(sid)
                ds[m7_grade.finding_id(f)] = votes('HIT', sid, b['category'])
            else: ds[m7_grade.finding_id(f)] = votes('FP')
        out[cid] = ds
    return out


def exclusion_fixture(cid, reason, res):
    r = next(c for c in res['cases'] if c['id'] == cid)
    def artifact(text): return {'content': text, 'sha256': sha(text.encode())}
    return dict(case=cid, reason=reason, base=r['base'], diff_sha256=r['diff_sha256'], arm='x',
                cause='Synthetic disk/provider outage', artifacts={'transcript': artifact('read /outside/key'),
                'tool_log': artifact('read /outside/key'), 'infrastructure_log': artifact('provider unavailable')},
                violations=[dict(cause='outside case', raw_input='read /outside/key', resolution='/outside/key',
                                 policy_rule='D-b1', artifact='tool_log', line=1)])


class M7GradeTest(unittest.TestCase):
    def grade(self, res, recs, keys, voided=None, man=None, over=None, seen=None):
        man, res = bind(res, keys, man, over)
        seen = keys if seen is None else seen  # the keys the grader actually reads (may differ from the frozen ones)
        findings = {'voided': voided or {}, 'arms': {'x': recs}}
        kb = json.dumps(seen).encode()
        tol = json.loads(man).get('line_tolerance')
        ds = legacy_fixture_decisions(recs, keys, tol) if type(tol) is int else {}
        adj = adjudication(man, res, findings, kb, {'x': ds})
        adj['exclusion_evidence'] = {c: exclusion_fixture(c, reason, res) for c, reason in (voided or {}).items()
                                     if c in {r['id'] for r in res['cases']}}
        return m7_grade.grade(man, res, findings, seen, kb, adj)

    def arm(self, *args, **kw):
        return self.grade(*args, **kw)['arms']['x']

    def test_hit_miss_and_tolerance(self):
        keys = {'c01': key(blocker(end=12), blocker(file='b.py', start=5))}
        arm = self.arm(result(('c01', 'ok')), {'c01': ok(finding(line=14), finding(file='b.py', line=9))}, keys)
        self.assertEqual((arm['hits'], arm['key_blockers'], arm['recall']), (1, 2, 0.5))
        self.assertEqual([i['hit'] for i in arm['per_blocker']], [True, False])
        self.assertEqual(arm['fp_blockers'], 1)  # b.py:9 is 4 away with N=2

    def test_false_positive_and_clean_case(self):
        keys = {'c01': key(), 'c02': key(blocker())}
        recs = {'c01': ok(finding()), 'c02': ok(finding(blocking=False, line=10), finding(line=50))}
        arm = self.arm(result(('c01', 'ok'), ('c02', 'ok')), recs, keys)
        self.assertEqual((arm['hits'], arm['fp_blockers'], arm['clean_case_fp_blockers']), (0, 2, 1))

    def test_matching_is_one_to_one_and_order_independent(self):
        keys = {'c01': key(blocker(start=10), blocker(start=14))}
        for order in ([finding(line=12, text='second mechanism'), finding(line=9, text='first mechanism')], [finding(line=9, text='first mechanism'), finding(line=12, text='second mechanism')]):
            arm = self.arm(result(('c01', 'ok')), {'c01': ok(*order)}, keys)
            self.assertEqual(arm['hits'], 2)
        self.assertEqual(self.arm(result(('c01', 'ok')), {'c01': ok(finding(line=12))}, keys)['hits'], 1)

    def test_arm_failure_is_a_miss_even_with_findings(self):
        keys = {'c01': key(blocker()), 'c02': key(blocker())}
        recs = {'c01': {'status': 'failed', 'findings': [finding()]}}  # c02 missing entirely
        arm = self.arm(result(('c01', 'ok'), ('c02', 'ok')), recs, keys)
        self.assertEqual((arm['hits'], arm['key_blockers'], arm['arm_failures_counted_as_miss']), (0, 2, 2))

    def test_excluded_and_voided_not_counted(self):
        keys = {c: key(blocker()) for c in ('c01', 'c02', 'c03')}
        recs = {c: ok(finding()) for c in keys}
        res = result(('c01', 'ok'), ('c02', 'excluded'), ('c03', 'ok'))
        rep = self.grade(res, recs, keys, voided={'c03': 'D-b1: x'})
        self.assertEqual((rep['counted_cases'], list(rep['excluded_not_counted']), rep['arms']['x']['key_blockers']), (['c01'], ['c02'], 1))
        self.assertEqual(rep['voided_not_counted'], {'c03': 'D-b1: x'})

    def test_clean_case_arm_failure_is_reported(self):
        keys = {'c01': key(), 'c02': key()}
        recs = {'c01': {'status': 'failed', 'findings': []}}  # c02 record missing
        arm = self.arm(result(('c01', 'ok'), ('c02', 'ok')), recs, keys)
        self.assertEqual((arm['clean_case_fp_blockers'], arm['clean_case_arm_failures'], arm['arm_failures_counted_as_miss']), (0, 2, 2))

    def test_lower_tolerance_edge(self):
        keys = {'c01': key(blocker(start=10))}
        for line, hits in ((8, 1), (7, 0)):  # N=2: start - N is inside, one further is not
            self.assertEqual(self.arm(result(('c01', 'ok')), {'c01': ok(finding(line=line))}, keys)['hits'], hits)

    def test_voided_reason_must_be_d_b1_or_infra(self):
        keys = {'c01': key(blocker()), 'c02': key(blocker())}
        res = result(('c01', 'ok'), ('c02', 'ok'))
        for bad in ('D-b1', 'other: x', '', None):
            with self.assertRaises(SystemExit):
                self.arm(res, {}, keys, voided={'c02': bad})
        self.arm(res, {}, keys, voided={'c02': 'infra: provider 503'})

    def test_more_than_two_infra_voided_aborts(self):
        ids = [f'c{n:02d}' for n in range(1, 6)]
        keys = {c: key(blocker()) for c in ids}
        res = result(*[(c, 'ok') for c in ids])
        with self.assertRaises(SystemExit):
            self.arm(res, {}, keys, voided={c: 'infra: down' for c in ids[:3]})
        self.arm(res, {}, keys, voided={**{c: 'infra: down' for c in ids[:2]}, ids[2]: 'D-b1: x'})

    def test_more_than_four_voided_plus_excluded_aborts(self):
        ids = [f'c{n:02d}' for n in range(1, 8)]
        keys = {c: key(blocker()) for c in ids}
        cases = [(c, 'excluded' if n < 2 else 'ok') for n, c in enumerate(ids)]
        voided = {c: 'D-b1: x' for c in ids[2:5]}  # 2 excluded + 3 voided = 5
        with self.assertRaises(SystemExit):
            self.arm(result(*cases), {}, keys, voided=voided)
        self.arm(result(*cases), {}, keys, voided={c: 'D-b1: x' for c in ids[2:4]})  # 4 in total is still allowed

    def test_empty_arm_list_aborts(self):
        man = manifest(arms=())
        with self.assertRaises(SystemExit):
            m7_grade.grade(man, result(('c01', 'ok'), man=man), {'arms': {}}, {'c01': key()})

    def test_security_only_split(self):
        keys = {'c01': key(blocker())}
        sec = finding(blocking=False, security_only=True)
        arm = self.arm(result(('c01', 'ok')), {'c01': ok(sec)}, keys)
        self.assertEqual((arm['recall'], arm['recall_with_security_only'], arm['fp_blockers']), (0.0, 1.0, 0))

    def test_category_and_split_breakdown(self):
        keys = {'c01': key(blocker(category='security-boundary'), split='archived'), 'c02': key(blocker())}
        recs = {'c01': ok(finding()), 'c02': ok()}
        arm = self.arm(result(('c01', 'ok'), ('c02', 'ok')), recs, keys)
        self.assertEqual((arm['by_split']['archived']['recall'], arm['by_split']['synthetic']['recall']), (1.0, 0.0))
        self.assertEqual(arm['by_category']['security-boundary']['hits'], 1)

    def test_fail_closed_inputs(self):
        res, keys = result(('c01', 'ok')), {'c01': key(blocker())}
        bad_findings = (finding(line=True), finding(blocking='true'), finding(blocking=True, security_only=True),
                        finding(blocking=False, security_only=1))
        for bad in bad_findings:
            with self.assertRaises(SystemExit, msg=str(bad)):
                self.arm(res, {'c01': ok(bad)}, keys)
        with self.assertRaises(SystemExit):
            self.arm(res, {'c01': ok()}, {})  # counted case without key
        with self.assertRaises(SystemExit):
            self.arm(res, {'c01': ok()}, {'c01': {'blockers': [], 'clean': True}})  # key without split
        with self.assertRaises(SystemExit):
            self.arm(res, {'c02': ok()}, keys)  # arm record for an unknown case id
        with self.assertRaises(SystemExit):
            self.arm(result(('c01', 'weird')), {}, keys)
        with self.assertRaises(SystemExit):
            self.arm(result(('c01', 'ok'), ('c01', 'excluded')), {}, keys)  # duplicate id
        with self.assertRaises(SystemExit):
            self.arm(res, {}, keys, voided={'c09': 'D-b1: x'})  # voided id not in corpus

    def test_manifest_binding_and_arm_set(self):
        res, keys = result(('c01', 'ok')), {'c01': key(blocker())}
        with self.assertRaises(SystemExit):
            self.arm(res, {}, keys, man=manifest(tol=3))  # hash differs from result manifest_sha256
        for bad in (-1, '2', None):
            man = manifest(tol=bad)
            with self.assertRaises(SystemExit):
                self.arm(result(('c01', 'ok'), man=man), {}, keys, man=man)
        man = manifest(arms=('x', 'y'))  # an expected arm is absent from the findings
        with self.assertRaises(SystemExit):
            self.arm(result(('c01', 'ok'), man=man), {}, keys, man=man)

    def abort(self, reason, *args, **kw):
        with self.assertRaisesRegex(SystemExit, reason):
            self.grade(*args, **kw)

    def test_g1_keys_must_match_frozen_hash(self):
        res, keys = result(('c01', 'ok')), {'c01': key(blocker())}
        self.abort('keys_sha256', res, {}, keys, seen={'c01': key()})  # a different KEYS.json than the frozen one
        self.abort('keys_sha256', res, {}, keys, over={'keys_sha256': 'f' * 64})
        man, bound = bind(res, keys)
        with self.assertRaisesRegex(SystemExit, 'keys_sha256'):  # no key bytes handed over
            m7_grade.grade(man, bound, {'arms': {'x': {}}}, keys)
        with self.assertRaisesRegex(SystemExit, 'keys_sha256'):  # parsed keys differ from the hashed bytes
            m7_grade.grade(man, bound, {'arms': {'x': {}}}, {'c01': key()}, json.dumps(keys).encode())

    def test_g2_case_set_is_bound_to_manifest(self):
        res, keys = result(('c01', 'ok'), ('c02', 'ok')), {'c01': key(blocker()), 'c02': key(blocker())}
        for cases in (['c01', 'c02', 'c02'], ['c01'], ['c01', 'c02', 'c03'], 'c01', None):
            self.abort('manifest cases', res, {}, keys, over={'cases': cases})
        self.abort('manifest cases', res, {}, {'c01': key(blocker())})  # c02 has no key entry
        objs = [dict(c) for c in res['cases']]
        self.assertEqual(self.arm(res, {}, keys, over={'cases': objs})['cases'], 2)  # m7_corpus.py manifest format
        self.abort('manifest cases', res, {}, keys, over={'cases': objs[:1]})
        self.abort('manifest cases', res, {}, keys, over={'cases': [{'id': 'c01'}, {'id': 'c01'}, {}]})

    def test_g3_clean_flag_must_be_explicit(self):
        res = result(('c01', 'ok'))
        for bad in ({'split': 'synthetic', 'blockers': []}, {**key(), 'clean': False}, {**key(blocker()), 'clean': True}):
            self.abort('clean', res, {}, {'c01': bad})

    def test_g4_ranges_must_be_ordered_integers(self):
        for bad in (blocker(start=True), blocker(start='10'), blocker(start=10.0), blocker(end=9), blocker(end=None),
                    blocker(end=True), blocker(end=11.5)):
            self.abort('start/end', result(('c01', 'ok')), {}, {'c01': key(bad)})

    def test_valid_bundle_grades_as_before(self):
        keys = {'c01': key(blocker(end=12), split='archived'), 'c02': key()}
        recs = {'c01': ok(finding(line=14)), 'c02': ok()}
        arm = self.arm(result(('c01', 'ok'), ('c02', 'ok')), recs, keys)
        self.assertEqual((arm['cases'], arm['key_blockers'], arm['hits'], arm['recall'], arm['fp_blockers']), (2, 1, 1, 1.0, 0))
        self.assertEqual((arm['clean_case_fp_blockers'], arm['clean_case_arm_failures'], arm['arm_failures_counted_as_miss']), (0, 0, 0))
        self.assertEqual(arm['per_blocker'], [dict(case='c01', split='archived', category='logic', file='a.py', start=10, seed=m7_grade.seed_id(keys['c01']['blockers'][0]), hit=True, hit_sec=True)])

    def test_cli_refuses_missing_frozen_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            man, res = bind(result(('c01', 'ok')), {'c01': key(blocker())})
            (p / 'm.json').write_bytes(man)
            (p / 'r.json').write_text(json.dumps(res))
            (p / 'f.json').write_text(json.dumps({'arms': {'x': {'c01': ok(finding())}}}))
            (p / 'k.json').write_bytes(json.dumps({'c01': key(blocker())}).encode())
            args = [str(p / n) for n in ('m.json', 'r.json', 'f.json', 'k.json', 'out')]
            run = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn('frozen workspace', run.stderr)
            self.assertFalse((p / 'out.json').exists())


class M7MechanismRegressionTest(unittest.TestCase):
    def bundle(self, fs=None, blockers=None):
        keys = {'c01': key(*(blockers if blockers is not None else [blocker()]))}
        res = result(('c01', 'ok'))
        man, res = bind(res, keys)
        fs = [finding(line=None)] if fs is None else fs
        findings = {'arms': {'x': {'c01': ok(*fs)}}}
        kb = json.dumps(keys).encode()
        return man, res, findings, keys, kb

    def grade(self, bundle, decisions=None, mutate=None):
        man, res, fs, keys, kb = copy.deepcopy(bundle)
        adj = None if decisions is None else adjudication(man, res, fs, kb, {'x': {'c01': decisions}})
        if mutate: mutate(man, res, fs, keys, kb, adj)
        return m7_grade.grade(man, res, fs, keys, kb, adj)

    def test_null_line_requires_adjudication_without_rejection(self):
        rep = self.grade(self.bundle())
        self.assertFalse(rep['score_ready'])
        self.assertEqual(rep['arms']['x']['hits'], 0)
        self.assertEqual(rep['arms']['x']['adjudication_required'], 1)

    def test_null_line_consensus_mechanism_hit(self):
        fs = [finding(line=None)]
        b = self.bundle(fs)
        ds = {m7_grade.finding_id(fs[0]): votes('HIT', m7_grade.seed_id(b[3]['c01']['blockers'][0]), 'logic')}
        rep = self.grade(b, ds)
        self.assertTrue(rep['score_ready'])
        self.assertEqual(rep['arms']['x']['hits'], 1)

    def test_file_line_alone_never_hits(self):
        rep = self.grade(self.bundle([finding()]))
        self.assertEqual(rep['arms']['x']['hits'], 0)
        self.assertFalse(rep['score_ready'])

    def test_blank_missing_nonstring_text_cannot_hit_even_with_votes(self):
        for value in ('', '   ', None, 3):
            f = finding(text=value); bundle = self.bundle([f])
            sid = m7_grade.seed_id(bundle[3]['c01']['blockers'][0])
            rep = self.grade(bundle, {m7_grade.finding_id(f): votes('HIT', sid, 'logic')})
            self.assertEqual((rep['arms']['x']['hits'], rep['arms']['x']['fp_blockers']), (0, 1))
        f = finding(); del f['text']
        self.assertEqual(self.grade(self.bundle([f]))['arms']['x']['hits'], 0)

    def test_wrong_mechanism_partial_and_wrong_category(self):
        f = finding(); bundle = self.bundle([f]); sid = m7_grade.seed_id(bundle[3]['c01']['blockers'][0])
        for verdict in ('PARTIAL', 'MISS', 'FP'):
            rep = self.grade(bundle, {m7_grade.finding_id(f): votes(verdict, sid, 'logic')})
            self.assertEqual(rep['arms']['x']['hits'], 0)
        for vs in (votes('HIT', sid, 'security-boundary'), votes('HIT', sid, 'logic')):
            if vs[0]['category'] == 'logic':
                for v in vs: v['mechanism_match'] = False
            with self.assertRaises(SystemExit): self.grade(bundle, {m7_grade.finding_id(f): vs})

    def test_duplicates_and_key_permutations_keep_class_recall(self):
        f = finding(line=12)
        bs = [blocker(start=10, category='logic'), blocker(start=14, category='state-machine')]
        wanted = {**bs[0]}
        reference = None
        for permutation in itertools.permutations(bs):
            for lines in ((12, 12), (11, 12), (12, 11)):
                fs = [finding(line=n) for n in lines]
                bundle = self.bundle(fs, list(permutation))
                sid = m7_grade.seed_id(wanted)
                rep = self.grade(bundle, {m7_grade.finding_id(f): votes('HIT', sid, 'logic')})['arms']['x']
                self.assertEqual((rep['hits'], rep['duplicates']), (1, 1))
                got = (rep['by_category'], rep['per_blocker'])
                if reference is None: reference = got
                self.assertEqual(got, reference)

    def test_two_distinct_reports_of_one_seed_are_redundant(self):
        fs = [finding(text='mechanism one'), finding(text='same defect in other words')]
        bundle = self.bundle(fs); sid = m7_grade.seed_id(bundle[3]['c01']['blockers'][0])
        ds = {m7_grade.finding_id(f): votes('HIT', sid, 'logic') for f in fs}
        rep = self.grade(bundle, ds)['arms']['x']
        self.assertEqual((rep['hits'], rep['duplicates']), (1, 1))

    def test_third_pass_and_independence(self):
        f = finding(); bundle = self.bundle([f]); sid = m7_grade.seed_id(bundle[3]['c01']['blockers'][0])
        vs = votes('HIT', sid, 'logic'); vs[1] = votes('PARTIAL', sid, 'logic')[1]
        fid = m7_grade.finding_id(f)
        self.assertFalse(self.grade(bundle, {fid: vs})['score_ready'])
        vs.append({**vs[0], 'grader': 'g3'})
        self.assertEqual(self.grade(bundle, {fid: vs})['arms']['x']['hits'], 1)
        vs[1]['vendor'] = vs[0]['vendor']
        with self.assertRaises(SystemExit): self.grade(bundle, {fid: vs})

    def test_valid_not_in_key_separate_from_fp(self):
        f = finding(file='other.py', line=None); bundle = self.bundle([f], [])
        rep = self.grade(bundle, {m7_grade.finding_id(f): votes('VALID-NOT-IN-KEY')})['arms']['x']
        self.assertEqual((rep['hits'], rep['fp_blockers'], rep['valid_not_in_key']), (0, 0, 1))
        bad = votes('VALID-NOT-IN-KEY'); bad[0]['code_evidence'] = ''
        with self.assertRaises(SystemExit): self.grade(bundle, {m7_grade.finding_id(f): bad})

    def test_adjudication_cannot_be_replayed_on_changed_findings(self):
        f = finding(); bundle = self.bundle([f]); sid = m7_grade.seed_id(bundle[3]['c01']['blockers'][0])
        def change(m, r, fs, k, kb, adj): fs['arms']['x']['c01']['findings'][0]['text'] = 'changed'
        with self.assertRaisesRegex(SystemExit, 'adjudication binding'):
            self.grade(bundle, {m7_grade.finding_id(f): votes('HIT', sid, 'logic')}, change)

    def test_wrong_result_base_diff_and_tree_rejected(self):
        for field, value in [('base', 'a'*40), ('diff_sha256', 'a'*64), ('tree', 'a'*40), ('key_sha256','a'*64)]:
            def change(m, r, f, k, kb, adj): r['cases'][0][field] = value
            with self.assertRaisesRegex(SystemExit, 'binding mismatch'):
                self.grade(self.bundle(), mutate=change)

    def test_bare_void_prefix_and_forged_scan_reference_rejected(self):
        man, res, fs, keys, kb = self.bundle()
        fs['voided'] = {'c01': 'D-b1: outside'}
        with self.assertRaisesRegex(SystemExit, 'scan/log evidence'):
            m7_grade.grade(man, res, fs, keys, kb)
        adj = adjudication(man, res, fs, kb)
        adj['exclusion_evidence'] = {'c01': exclusion_fixture('c01', 'D-b1: outside', res)}
        rep = m7_grade.grade(man, res, fs, keys, kb, adj)
        self.assertEqual(rep['counted_cases'], [])
        adj['exclusion_evidence']['c01']['violations'][0]['raw_input'] = 'fabricated command'
        with self.assertRaisesRegex(SystemExit, 'not in referenced'):
            m7_grade.grade(man, res, fs, keys, kb, adj)

    def test_exclusion_without_scan_cannot_remove_denominator(self):
        bundle = list(self.bundle())
        bundle[1]['cases'][0]['status'] = 'excluded'
        with self.assertRaisesRegex(SystemExit, 'frozen status'): self.grade(bundle)

    def test_near_miss_location_not_rescued_by_hit_vote(self):
        f = finding(line=13); bundle = self.bundle([f]); sid=m7_grade.seed_id(bundle[3]['c01']['blockers'][0])
        rep = self.grade(bundle, {m7_grade.finding_id(f): votes('HIT', sid, 'logic')})
        self.assertFalse(rep['score_ready'])
        self.assertEqual((rep['arms']['x']['hits'], rep['arms']['x']['adjudication_required']), (0, 1))


    def test_vendor_normalization_refuses_same_vendor_aliases(self):
        for left, right in [('anthropic', 'Anthropic'), ('anthropic', ' anthropic '),
                            (' ANTHROPIC ', 'Anthropic')]:
            with self.subTest(left=left, right=right):
                vs = votes('FP'); vs[0]['vendor'] = left; vs[1]['vendor'] = right
                with self.assertRaisesRegex(SystemExit, 'different vendor'):
                    m7_grade.consensus(vs)

    def test_vendor_normalization_accepts_distinct_trimmed_vendor(self):
        vs = votes('FP'); vs[0]['vendor'] = ' Anthropic '; vs[1]['vendor'] = ' Other-Vendor '
        self.assertEqual(m7_grade.consensus(vs)['verdict'], 'FP')

    def test_frozen_status_rejects_self_consistent_late_exclusion(self):
        man, res, fs, keys, kb = self.bundle()
        rec = res['cases'][0]
        scan = dict(kind='D-b2', case='c01', base=rec['base'], diff_sha256=rec['diff_sha256'],
                    location='pinned source', matched_count=1, matched_unit_sha256=['7'*64])
        rec.update(status='excluded', scan=scan, scan_sha256=sha(json.dumps(scan, sort_keys=True).encode()))
        with self.assertRaisesRegex(SystemExit, 'frozen status'):
            m7_grade.grade(man, res, fs, keys, kb)

    def test_frozen_scan_rejects_self_consistent_replacement(self):
        keys = {'c01': key(blocker())}; kb = json.dumps(keys).encode()
        man, res = bind(result(('c01', 'excluded')), keys)
        res['cases'][0]['scan']['matched_unit_sha256'] = ['8'*64]
        res['cases'][0]['scan_sha256'] = sha(json.dumps(res['cases'][0]['scan'], sort_keys=True).encode())
        with self.assertRaisesRegex(SystemExit, 'frozen scan'):
            m7_grade.grade(man, res, {'arms': {'x': {}}}, keys, kb)

    def test_md_table_preserves_columns_and_pending_scope(self):
        rep = self.grade(self.bundle([finding()]))
        md = m7_grade.table(rep)
        self.assertIn('| adjudication_required | duplicates | valid_not_in_key |', md)
        self.assertIn('| x | 1 | 1 | 0 | 0.0 | 0.0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |', md)
        self.assertIn('Excluded (not counted): {}', md)
        self.assertFalse(rep['score_ready'])

    def test_successful_cli_grade_writes_json_and_md(self):
        # Reuse the actual corpus fixture; no duplicate freeze implementation.
        from paired_session.test_m7_corpus import M7CorpusTest
        fixture = M7CorpusTest(); fixture.setUp()
        try:
            run = fixture.run_tool(); self.assertEqual(run.returncode, 0, run.stderr)
            mp = fixture.out / 'frozen-manifest.json'; rp = fixture.out / 'result.json'
            keys = {'c01': key(blocker(start=2))}; kb = json.dumps(keys).encode()
            m = json.loads(mp.read_text()); r = json.loads(rp.read_text())
            m.update(arms=['x'], line_tolerance=2, keys_sha256=sha(kb))
            mb = json.dumps(m).encode(); mp.write_bytes(mb)
            r['manifest_sha256'] = sha(mb); rp.write_text(json.dumps(r))
            f = finding(line=None, text='Synthetic missing bounds check')
            fs = {'arms': {'x': {'c01': ok(f)}}}
            ds = {'x': {'c01': {m7_grade.finding_id(f): votes('HIT', m7_grade.seed_id(keys['c01']['blockers'][0]), 'logic')}}}
            adj = adjudication(mb, r, fs, kb, ds)
            kp = fixture.tmp / 'k.json'; kp.write_bytes(kb)
            fp = fixture.tmp / 'f.json'; fp.write_text(json.dumps(fs))
            ap = fixture.tmp / 'adj.json'; ap.write_text(json.dumps(adj))
            out = fixture.tmp / 'grade'
            run = subprocess.run([sys.executable, str(SCRIPT), str(mp), str(rp), str(fp), str(kp), str(out), str(ap)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            rep = json.loads(out.with_suffix('.json').read_text())
            self.assertEqual(rep['arms']['x']['hits'], 1); self.assertTrue(rep['score_ready'])
            self.assertEqual(out.with_suffix('.md').read_text(), m7_grade.table(rep))
        finally:
            fixture.doCleanups()

    def test_duplicate_votes_need_a_scored_target_and_cannot_cycle(self):   # m7-s1 R1
        a, b = finding(text='first report'), finding(text='second report')
        bundle = self.bundle([a, b]); fa, fb = m7_grade.finding_id(a), m7_grade.finding_id(b)
        with self.assertRaisesRegex(SystemExit, 'chains and cycles'):   # A -> B and B -> A erased both blockers
            self.grade(bundle, {fa: votes('DUPLICATE', duplicate_of=fb), fb: votes('DUPLICATE', duplicate_of=fa)})
        rep = self.grade(bundle, {fa: votes('DUPLICATE', duplicate_of=fb)})   # B has no verdict yet: A waits with it
        self.assertEqual((rep['arms']['x']['adjudication_required'], rep['score_ready']), (2, False))
        rep = self.grade(bundle, {fa: votes('DUPLICATE', duplicate_of=fb), fb: votes('FP')})
        self.assertEqual((rep['arms']['x']['fp_blockers'], rep['arms']['x']['duplicates'], rep['score_ready']), (1, 1, True))

    def frozen_cli(self, fixture, prepare=None, env=None, legacy_manifest=False):
        run = fixture.run_tool(); self.assertEqual(run.returncode, 0, run.stderr)
        mp, rp = fixture.out / 'frozen-manifest.json', fixture.out / 'result.json'
        keys = {'c01': key(blocker(start=2))}; kb = json.dumps(keys).encode()
        m, r = json.loads(mp.read_text()), json.loads(rp.read_text())
        m.update(arms=['x'], line_tolerance=2, keys_sha256=sha(kb))
        if legacy_manifest: del m['cases'][0]['worktree_sha256']   # a manifest frozen before m7-s1
        mb = json.dumps(m).encode(); mp.write_bytes(mb)
        r['manifest_sha256'] = sha(mb); rp.write_text(json.dumps(r))
        kp = fixture.tmp / 'k.json'; kp.write_bytes(kb)
        fp = fixture.tmp / 'f.json'; fp.write_text(json.dumps({'arms': {'x': {'c01': ok()}}}))
        if prepare: prepare(fixture.out / 'c01' / 'repo')
        return subprocess.run([sys.executable, str(SCRIPT), str(mp), str(rp), str(fp), str(kp), str(fixture.tmp / 'grade')],
                              capture_output=True, text=True, env={**os.environ, **(env or {})})

    def test_the_cli_checks_disk_bytes_and_allows_only_the_run_artifacts(self):   # m7-s1 R1
        from paired_session.test_m7_corpus import M7CorpusTest, git
        def artifacts(repo):
            (repo / '.review-loop' / 'tmp').mkdir(parents=True)
            (repo / '.review-loop' / 'config.md').write_text('reviewer: subagent\n')
            (repo / '.review-loop' / 'tmp' / 's-reviewer-result.txt').write_text('review\n')
        def flagged(flag, crlf=False):
            def prepare(repo):
                git(repo, 'update-index', flag, 'a.py')
                if crlf:   # a text attribute normalises CRLF back to the indexed blob; the raw bytes still changed
                    (repo / '.git' / 'info').mkdir(exist_ok=True)   # the freeze copies no template
                    (repo / '.git' / 'info' / 'attributes').write_text('a.py text\n')
                    (repo / 'a.py').write_bytes((repo / 'a.py').read_bytes().replace(b'\n', b'\r\n'))
                else:
                    (repo / 'a.py').write_text('changed on disk\n')
            return prepare
        def stray(repo): (repo / 'notes.txt').write_text('new file\n')
        def chmod(repo): git(repo, 'update-index', '--skip-worktree', 'a.py'); (repo / 'a.py').chmod(0o755)   # mode only
        changed = 'differ from the freeze'
        for label, prepare, refused, legacy in (
                ('artifacts', artifacts, None, False), ('skip-worktree', flagged('--skip-worktree'), changed, False),
                ('assume-unchanged', flagged('--assume-unchanged'), changed, False),
                ('crlf skip-worktree', flagged('--skip-worktree', crlf=True), changed, False),
                ('crlf assume-unchanged', flagged('--assume-unchanged', crlf=True), changed, False),
                ('mode', chmod, changed, False), ('stray', stray, 'untracked', False), ('pre-m7-s1 manifest', None, 're-freeze', True)):
            with self.subTest(label):
                fixture = M7CorpusTest(); fixture.setUp()
                try:
                    run = self.frozen_cli(fixture, prepare, legacy_manifest=legacy)
                    if refused: self.assertNotEqual(run.returncode, 0); self.assertIn(refused, run.stderr)
                    else: self.assertEqual(run.returncode, 0, run.stderr)
                finally:
                    fixture.doCleanups()

    def test_a_case_with_a_tracked_but_ignored_file_grades(self):   # m7-s1b: c06/c19 were refused as "untracked changes"
        from paired_session.test_m7_corpus import M7CorpusTest
        fixture = M7CorpusTest(); fixture.setUp()
        try:
            fixture.add_tracked_ignored_file()
            run = self.frozen_cli(fixture)
            self.assertEqual(run.returncode, 0, run.stderr)
        finally:
            fixture.doCleanups()

    def test_the_cli_workspace_check_ignores_a_hostile_global_git_config(self):   # m7-s1
        from paired_session.test_m7_corpus import M7CorpusTest
        fixture = M7CorpusTest(); fixture.setUp()
        try:
            run = fixture.run_tool(); self.assertEqual(run.returncode, 0, run.stderr)
            mp, rp = fixture.out / 'frozen-manifest.json', fixture.out / 'result.json'
            keys = {'c01': key(blocker(start=2))}; kb = json.dumps(keys).encode()
            m, r = json.loads(mp.read_text()), json.loads(rp.read_text())
            m.update(arms=['x'], line_tolerance=2, keys_sha256=sha(kb))
            mb = json.dumps(m).encode(); mp.write_bytes(mb)
            r['manifest_sha256'] = sha(mb); rp.write_text(json.dumps(r))
            kp = fixture.tmp / 'k.json'; kp.write_bytes(kb)
            fp = fixture.tmp / 'f.json'; fp.write_text(json.dumps({'arms': {'x': {'c01': ok()}}}))
            out = fixture.tmp / 'grade'
            env = {**os.environ, **fixture.hostile_git_config()}   # diff.noprefix would change the staged diff bytes
            run = subprocess.run([sys.executable, str(SCRIPT), str(mp), str(rp), str(fp), str(kp), str(out)],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn('staged diff mismatch', run.stderr)
        finally:
            fixture.doCleanups()

    def test_outside_tolerance_hit_is_local_pending_other_hit_survives(self):
        fs = [finding(line=13, text='outside location'), finding(line=10, text='valid location')]
        bundle = self.bundle(fs); sid = m7_grade.seed_id(bundle[3]['c01']['blockers'][0])
        ds = {m7_grade.finding_id(f): votes('HIT', sid, 'logic') for f in fs}
        rep = self.grade(bundle, ds)
        self.assertFalse(rep['score_ready']); arm = rep['arms']['x']
        self.assertEqual((arm['hits'], arm['adjudication_required']), (1, 1))
        self.assertIn('location', arm['details']['c01']['adjudication_required'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
