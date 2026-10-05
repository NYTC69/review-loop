"""ratelimit (BACKLOG rate-limit row; legacy-replacement evidence 2H "real rate-limit HOLD then recovery"): a provider
rate-limit rejection in any role or stage leaves a clean HOLD with a typed kind (hold_kind "rate_limited", role, phase,
reset hint), uses neither the invocation budget nor a stage budget, and `resume` after the limit clears completes the run
with no duplicated or skipped turn. Drive-level runs on the fake CLIs (FAKE_RATE_LIMIT + _MATCH + _ONCE)."""
import json
import os
import unittest

from paired_session import coordinator as rc
from paired_session import test_worktree_lifecycle as twl

DONE = twl.DONE
LIMIT_TEXT = 'Try again at Sep 26th 5:13 PM'
SITES = (   # (prompt marker, role, phase)
    ('plan author. Phase: PLAN.', 'author', 'PLAN'),
    ('persistent plan-only reviewer. Phase: PLAN.', 'reviewer', 'PLAN'),
    ('implementer. Phase: EXEC.', 'author', 'EXEC'),
    ('Role: reviewer, read-only whole-delta reviewer. Phase: EXEC.', 'reviewer', 'EXEC'),
    ('Role: shadow,', 'shadow', 'EXEC'),
    ('adversarial reviewer with a skepticism-by-default stance', 'gate', 'EXEC'),
    ('Role: finisher, fresh. Phase: FINISH.', 'author', 'FINISH'),
    ('Role: specialist code-reviewer', 'reviewer', 'POLISH-Q'),
    ('Role: docs writer, fresh. Phase: DOCS.', 'author', 'DOCS'),
    ('Role: docs reviewer, fresh. Phase: DOCS.', 'reviewer', 'DOCS'),
    ('Role: security reviewer, fresh. Phase: SECURITY.', 'reviewer', 'SECURITY'),
)


class RateLimitDriveTests(unittest.TestCase):
    def setUp(self):
        self.h = twl.WorktreeLifecycleActivationTests('test_a_no_op_worktree_run_goes_through_every_stage_and_reaches_done_only_after_security')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.docs = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'}   # a DOCS write, so the docs reviewer runs too

    def state(self):
        return json.loads((self.h.run_dir / 'state.json').read_text())

    def fresh(self, name):   # each run starts from the committed tree in its own run dir
        for command in (['git', 'reset', '-q', '--hard', 'HEAD'], ['git', 'clean', '-q', '-fd']):
            rc.subprocess.run(command, cwd=self.h.workspace, check=True)
        self.h.run_dir = self.h.root / name

    def cli(self, env, action='run'):
        if action == 'run':
            return self.h.run_coordinator('--lifecycle-mode', 'on', env=env)
        return self.h.run_operator_action(action, '--lifecycle-mode', 'on', env=env)

    def done_turns(self, state):
        return [(row['role'], row['phase']) for row in state['turns'] if not row.get('error')]

    def baseline(self):
        self.fresh('baseline')
        result = self.cli(self.docs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.done_turns(self.state())

    def limited_env(self, marker, once=True):
        env = {**self.docs, 'FAKE_RATE_LIMIT': '1', 'FAKE_RATE_LIMIT_MATCH': marker}
        if once:
            env['FAKE_RATE_LIMIT_ONCE'] = str(self.h.run_dir) + '.limited'
        return env

    def assert_rate_hold(self, result, role, phase):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual((state['status'], state.get('hold_kind')), ('HOLD', 'rate_limited'), state['hold_reason'])
        self.assertEqual((state['rate_limit']['role'], state['rate_limit']['phase']), (role, phase))
        self.assertIn(LIMIT_TEXT, state['rate_limit']['reset_hint'])
        self.assertIn('rate_limited', state['hold_reason'])
        limited = [row for row in state['turns'] if row.get('error_kind') == 'rate_limited']
        self.assertTrue(limited and all(row['invocation_budget_counted'] is False for row in limited))
        self.assertEqual(state['invocations_used'], sum(row['invocation_budget_counted'] is not False for row in state['turns']))
        self.assertIsNone(state.get('uncertain_active'))

    def check_sites(self, sites):
        expected = self.baseline()
        for marker, role, phase in sites:
            with self.subTest(phase=phase, role=role):
                self.fresh('limited-' + phase + '-' + role)
                env = self.limited_env(marker)
                self.assert_rate_hold(self.cli(env), role, phase)
                resumed = self.cli(env, 'resume')   # the limit has cleared (once)
                self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                self.assertIn(DONE, resumed.stdout)
                state = self.state()
                self.assertNotIn('hold_kind', state)
                self.assertEqual(self.done_turns(state), expected)   # nothing duplicated or skipped
                self.assertEqual([(row['role'], row['phase']) for row in state['turns'] if row.get('error')], [(role, phase)])

    def test_plan_and_exec_roles(self):
        self.check_sites(SITES[:6])

    def test_worktree_lifecycle_stages(self):
        self.check_sites(SITES[6:])

    def test_rate_limits_never_use_up_the_docs_or_security_stage_budget(self):
        for marker, phase, cap in (('Role: security reviewer, fresh. Phase: SECURITY.', 'SECURITY', 3),
                                   ('Role: docs writer, fresh. Phase: DOCS.', 'DOCS', 7)):
            with self.subTest(phase=phase):
                self.fresh('budget-' + phase)
                env = self.limited_env(marker, once=False)
                result = self.cli(env)
                for _ in range(cap):   # more rejections than the stage allows calls
                    self.assert_rate_hold(result, self.state()['rate_limit']['role'], phase)
                    result = self.cli(env, 'resume')
                self.assert_rate_hold(result, self.state()['rate_limit']['role'], phase)
                cleared = self.cli(self.docs, 'resume')
                self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
                self.assertIn(DONE, cleared.stdout)
                self.assertEqual(sum(row.get('error_kind') == 'rate_limited' and row['phase'] == phase
                                     for row in self.state()['turns']), cap + 1)

    def test_a_writer_git_guard_hold_wrapping_a_rate_limit_is_not_typed_as_a_rate_limit(self):
        self.fresh('guard-wraps-limit')
        env = {**self.limited_env('Role: finisher, fresh. Phase: FINISH.'), 'FAKE_RATE_LIMIT_STAGE_FIRST': '1'}
        result = self.cli(env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        state = self.state()
        self.assertIn('changed HEAD, refs or the index; restore the recorded baseline or abort (the turn failed: rate_limited',
                      state['hold_reason'])
        self.assertNotIn('hold_kind', state)   # it needs a manual repair, not just a wait for the limit
        self.assertNotIn('rate_limit', state)

    def test_another_hold_clears_the_rate_limit_kind(self):
        self.fresh('other-hold')
        self.assert_rate_hold(self.cli(self.limited_env('plan author. Phase: PLAN.')), 'author', 'PLAN')
        co = rc.Coordinator(self.h.args(action='resume'))
        co.hold('an unrelated hold')
        self.assertNotIn('hold_kind', self.state())
        self.assertNotIn('rate_limit', self.state())


if __name__ == '__main__':
    unittest.main()
