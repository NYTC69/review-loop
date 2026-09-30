"""Tests for scripts/m7_grade.py with tiny synthetic inputs."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'm7_grade.py'
sys.path.insert(0, str(SCRIPT.parent))
import m7_grade  # noqa: E402


def finding(file='a.py', line=10, blocking=True, **extra):
    return dict(file=file, line=line, blocking=blocking, text='t', **extra)


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
    body.setdefault('cases', [c['id'] for c in res['cases']])
    body.setdefault('keys_sha256', sha(json.dumps(keys).encode()))
    new = json.dumps({**body, **(over or {})}).encode()
    return new, (dict(res, manifest_sha256=sha(new)) if res['manifest_sha256'] == sha(man) else res)


def result(*cases, man=None):
    man = man or manifest()
    return {'manifest_sha256': hashlib.sha256(man).hexdigest(), 'cases': [{'id': c, 'status': s} for c, s in cases]}


class M7GradeTest(unittest.TestCase):
    def grade(self, res, recs, keys, voided=None, man=None, over=None, seen=None):
        man, res = bind(res, keys, man, over)
        seen = keys if seen is None else seen  # the keys the grader actually reads (may differ from the frozen ones)
        return m7_grade.grade(man, res, {'voided': voided or {}, 'arms': {'x': recs}}, seen, json.dumps(seen).encode())

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
        for order in ([finding(line=12), finding(line=9)], [finding(line=9), finding(line=12)]):
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
        bad_findings = (finding(line=None), finding(line=True), finding(blocking='true'), finding(blocking=True, security_only=True),
                        finding(blocking=False, security_only=1), finding(blocking=False, security_only=True, line=None))
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
        objs = [{'id': 'c01', 'base': 'b', 'diff_path': 'd', 'diff_sha256': 'h', 'key_path': 'k'}, {'id': 'c02'}]
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
        self.assertEqual(arm['per_blocker'], [dict(case='c01', split='archived', category='logic', file='a.py', start=10, hit=True, hit_sec=True)])

    def test_cli_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            man, res = bind(result(('c01', 'ok')), {'c01': key(blocker())})
            (p / 'm.json').write_bytes(man)
            (p / 'r.json').write_text(json.dumps(res))
            (p / 'f.json').write_text(json.dumps({'arms': {'x': {'c01': ok(finding())}}}))
            (p / 'k.json').write_bytes(json.dumps({'c01': key(blocker())}).encode())
            args = [str(p / n) for n in ('m.json', 'r.json', 'f.json', 'k.json', 'out')]
            subprocess.run([sys.executable, str(SCRIPT), *args], check=True)
            self.assertEqual(json.loads((p / 'out.json').read_text())['arms']['x']['hits'], 1)
            self.assertIn('| x |', (p / 'out.md').read_text())


if __name__ == '__main__':
    unittest.main()
