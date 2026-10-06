"""LG2-c: `simplify` on the paired review-pr route is refused with the --legacy pointer; a no-test report run takes an
approval with empty self_run_evidence as static (the b2 gate LOW), while an ordinary run still HOLDs."""
import json
import subprocess
import unittest
from pathlib import Path

from paired_session import review_post
from paired_session import test_review_report_b2 as b2

rc = b2.rc


class ReviewPrEntryTests(unittest.TestCase):
    locals().update({name: getattr(b2.NoTestReportRunTests, name)
                     for name in (*b2.HELPERS, 'notest', 'cli', 'coordinator', 'change')})

    def test_simplify_is_refused_with_the_legacy_pointer(self):
        self.change()
        for aspects in ('simplify', 'code,simplify'):
            with self.subTest(aspects=aspects):
                self.run_dir = self.root / aspects.replace(',', '-')
                with self.assertRaisesRegex(ValueError, r'--aspects simplify is not part of a paired review-pr .*'
                                                        r'/review-loop:review-pr --legacy simplify'):
                    self.coordinator(*b2.NO_TEST, '--aspects', aspects)
                self.assertFalse((self.run_dir / 'state.json').exists())   # refused before any state
        refused = self.cli(*b2.NO_TEST, '--aspects', 'simplify')
        self.assertEqual(refused.returncode, 2)
        self.assertIn('/review-loop:review-pr --legacy simplify', refused.stdout)

    def test_an_empty_evidence_approval_is_static_in_a_no_test_report_run_only(self):
        self.change()
        result = self.cli(*b2.NO_TEST, env={'FAKE_EXEC_NO_EVIDENCE': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertNotIn('empty self_run_evidence', str(state.get('hold_reason')))
        [reviewer] = [t for t in state['turns'] if (t['role'], t['phase']) == ('reviewer', 'EXEC') and not t.get('discarded')]
        self.assertTrue(reviewer['static_untested_approval'])
        self.assertIn('Status: REPORTED (complete)', (self.run_dir / 'review-report.md').read_text())
        self.run_dir = self.root / 'tested'   # a report run with a test command keeps the HOLD
        held = self.run_coordinator(*b2.FLAGS, env={'FAKE_EXEC_NO_EVIDENCE': '1'})
        self.assertNotEqual(held.returncode, 0)
        self.assertIn('EXEC APPROVE rejected: empty self_run_evidence',
                      json.loads((self.run_dir / 'state.json').read_text())['hold_reason'])


    def materialized_pr(self):
        """An offline PR: a bare "target" with refs/pull/7/head, materialized by scripts/materialize_pr.py (LG2-d)."""
        from scripts import materialize_pr as mp
        git = lambda repo, *a: subprocess.check_output(['git', '-C', str(repo), *a], text=True).strip()   # noqa: E731
        operator = self.root / 'operator'
        operator.mkdir()
        git(operator, 'init', '-q', '-b', 'main')
        git(operator, 'config', 'user.email', 't@example.invalid'); git(operator, 'config', 'user.name', 'T')
        (operator / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        git(operator, 'add', '.'); git(operator, 'commit', '-qm', 'base')
        base = git(operator, 'rev-parse', 'HEAD')
        git(operator, 'checkout', '-qb', 'topic')
        (operator / 'sum_ints.py').write_text('def sum_ints(values):\n    # add them\n    return sum(values)\n')
        git(operator, 'commit', '-qam', 'topic')
        head = git(operator, 'rev-parse', 'HEAD')
        target = self.root / 'target.git'
        subprocess.run(['git', 'clone', '-q', '--bare', str(operator), str(target)], check=True)
        git(target, 'update-ref', 'refs/pull/7/head', head)
        pr = {'kind': 'pr', 'operator_repo': str(operator), 'target_url': str(target), 'repository': 'owner/target',
              'number': 7, 'url': 'https://github.com/owner/target/pull/7', 'is_cross_repository': False,
              'head': {'oid': head, 'source': str(target), 'fetch_ref': 'refs/pull/7/head'},
              'base': {'oid': base, 'source': str(target), 'fetch_ref': 'refs/heads/main'}}
        result = mp.materialize(pr, self.root / 'state')
        self.addCleanup(lambda: Path(result['workspace']).is_dir() and mp.remove(result['workspace']))
        pins = self.root / 'pr-pins.json'
        pins.write_text(json.dumps(result))
        return result, pins

    def test_materialized_pins_reach_the_report_and_the_post(self):
        result, pins = self.materialized_pr()
        self.workspace = Path(result['workspace'])
        done = self.cli(*b2.NO_TEST, '--base', result['merge_base'], '--review-pr-pins', str(pins))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['review_pr'], {'Target repository': 'owner/target', 'PR URL': 'https://github.com/owner/target/pull/7',
                                              'Head': result['head']['oid'], 'Base (pinned)': result['base']['oid'],
                                              'Merge base': result['merge_base']})
        text = (self.run_dir / 'review-report.md').read_text()
        self.assertIn('Status: REPORTED (complete)', text)
        self.assertIn('- PR URL: `https://github.com/owner/target/pull/7`', text)
        plan = review_post.prepare(self.run_dir)
        self.assertEqual((plan['url'], plan['head'], plan['repository']),
                         ('https://github.com/owner/target/pull/7', result['head']['oid'], 'owner/target'))
        calls = []
        runner = lambda argv, **_: calls.append(argv) or subprocess.CompletedProcess(   # noqa: E731
            argv, 0, json.dumps({'headRefOid': result['head']['oid']}), '')
        self.assertEqual(review_post.post(self.run_dir, plan['digest'], runner=runner), plan['url'])
        self.assertEqual(calls[-1][:5], ['gh', 'pr', 'review', plan['url'], '--comment'])

    def test_pins_that_do_not_describe_the_run_are_refused(self):
        result, pins = self.materialized_pr()
        self.workspace = Path(result['workspace'])
        other = self.root / 'other-pins.json'   # pins of another head than the workspace's HEAD
        other.write_text(json.dumps({**result, 'head': {**result['head'], 'oid': '0' * 40}}))
        with self.assertRaisesRegex(ValueError, '--review-pr-pins does not describe this run'):
            self.coordinator(*b2.NO_TEST, '--base', result['merge_base'], '--review-pr-pins', str(other))
        self.assertFalse((self.run_dir / 'state.json').exists())
        with self.assertRaisesRegex(ValueError, '--review-pr-pins needs --review-report'):
            self.coordinator('--review-pr-pins', str(pins))


if __name__ == '__main__':
    unittest.main()
