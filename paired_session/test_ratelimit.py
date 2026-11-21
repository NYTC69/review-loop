"""Drive-level quota waits and typed HOLD recovery on fake CLIs."""
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

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

    def cli(self, env, action='run', extra=()):
        if action == 'run':
            return self.h.run_coordinator('--lifecycle-mode', 'on', *extra, env=env)
        return self.h.run_operator_action(action, '--lifecycle-mode', 'on', *extra, env=env)

    @contextmanager
    def clock(self):
        now = int(rc.time.time())
        waits = []
        with patch.object(rc.time, 'time', side_effect=lambda: now + sum(waits)), \
                patch.object(rc, 'sleep_rate_limit', side_effect=waits.append):
            yield now, waits

    def drive(self, env, *extra, action='run'):
        with patch.dict(os.environ, env):
            co = rc.Coordinator(self.h.args(*extra, action=action))
            return co.drive() if action == 'run' else co.resume()

    def done_turns(self, state):
        return [(row['role'], row['phase']) for row in state['turns'] if not row.get('error')]

    def baseline(self):
        self.fresh('baseline')
        self.assertEqual(self.drive(self.docs), 'DONE', self.state().get('hold_reason'))
        return self.state()

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
                with self.clock() as (now, waits):
                    env['FAKE_RATE_LIMIT_RESET_AT'] = str(now + 30)
                    self.assertEqual(self.drive(env), 'DONE', self.state().get('hold_reason'))
                    self.assertEqual(waits, [30])
                state = self.state()
                self.assertNotIn('hold_kind', state)
                self.assertNotIn('rate_limit_wait', state)
                self.assertEqual(self.done_turns(state), self.done_turns(expected))
                for key in ('invocations_used', 'exec_rounds', 'plan_rounds', 'gate_ran'):
                    self.assertEqual(state[key], expected[key], key)
                for key in ('polish_calls', 'specialist_counts'):
                    self.assertEqual(state['lifecycle'].get(key), expected['lifecycle'].get(key), key)
                [failed] = [row for row in state['turns'] if row.get('error')]
                self.assertEqual((failed['role'], failed['phase'], failed['reset_at']), (role, phase, now + 30))
                self.assertFalse(failed['invocation_budget_counted'])

    def test_plan_and_exec_roles(self):
        self.check_sites(SITES[:6])

    def test_retry_after_the_work_item_deadline_holds_without_waiting(self):
        self.fresh('retry-deadline')
        env = self.limited_env(SITES[0][0])
        with self.clock() as (now, waits):
            env['FAKE_RATE_LIMIT_RESET_AT'] = str(now + 30)
            self.assertEqual(self.drive(env, '--wi-deadline', '29'), 'HOLD')
        self.assertEqual(waits, [])
        state = self.state()
        self.assertIn('whole-WI deadline would be reached', state['hold_reason'])
        self.assertEqual(len(state['turns']), 1)
        self.assertNotIn('hold_kind', state)
        self.assertNotIn('rate_limit_wait', state)
        self.assertEqual(state.get('rate_limit_wait_used', 0), 0)

    def test_worktree_lifecycle_stages(self):
        self.check_sites(SITES[6:])

    def test_wait_budget_bounds(self):
        for budget, status in ((30, 'DONE'), (29, 'HOLD'), (0, 'HOLD')):
            with self.subTest(budget=budget):
                self.fresh(f'budget-{budget}')
                env = self.limited_env(SITES[0][0])
                with self.clock() as (now, waits):
                    env['FAKE_RATE_LIMIT_RESET_AT'] = str(now + 30)
                    self.assertEqual(self.drive(env, '--max-rate-limit-wait', str(budget)), status)
                state = self.state()
                self.assertEqual(state['config']['max_rate_limit_wait'], budget)
                self.assertEqual(waits, [30] if budget == 30 else [])
                if budget < 30:
                    self.assertEqual(state['hold_kind'], 'rate_limited')
                    self.assertIn('29 s of the run-wide wait budget remain' if budget else 'waiting is disabled',
                                  state['hold_reason'])
                    self.assertEqual(self.drive(env, action='resume'), 'DONE')
        self.fresh('invalid')
        for value in ('-1', '1.5'):
            result = self.cli({}, extra=('--max-rate-limit-wait', value))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('--max-rate-limit-wait', result.stdout + result.stderr)
            self.assertFalse((self.h.run_dir / 'state.json').exists())
        profile = self.h.root / 'wait.json'
        for value in (-1, 1.5, '30', True, None):
            profile.write_text(json.dumps({'max_rate_limit_wait': value}))
            result = self.cli({}, extra=('--config', str(profile)))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse((self.h.run_dir / 'state.json').exists())
        profile.write_text(json.dumps({'max_rate_limit_wait': 0}))
        self.assert_rate_hold(self.cli(self.limited_env(SITES[0][0]), extra=('--config', str(profile))),
                              'author', 'PLAN')
        self.assertEqual(self.state()['config']['max_rate_limit_wait'], 0)
        refused = self.cli({}, 'resume', extra=('--max-rate-limit-wait', '30'))
        self.assertIn('resume configuration differs: max_rate_limit_wait', refused.stdout)

    def test_unknown_reset_and_non_quota(self):
        env = {**self.limited_env(SITES[0][0], once=False), 'FAKE_RATE_LIMIT_UNKNOWN': '1'}
        with self.clock() as (_, waits):
            self.assertEqual(self.drive(env), 'HOLD')
        self.assertEqual(waits, [30, 60, 120, 240])
        state = self.state()
        self.assertEqual((state['hold_kind'], state['invocations_used'], state['rate_limit_wait_used']),
                         ('rate_limited', 0, 450))
        self.assertIn('backoff exhausted', state['hold_reason'])
        self.assertEqual(len(state['turns']), 5)
        self.assertTrue(all(not row['invocation_budget_counted'] for row in state['turns']))
        self.assertNotIn('rate_limit_wait', state)
        self.assertEqual(self.drive(self.docs, action='resume'), 'DONE')
        for message in ('tool crashed', 'Unauthorized (401)', 'permission denied'):
            with self.subTest(message=message), self.clock() as (_, waits):
                self.fresh('nonquota-' + message.split()[0])
                self.assertEqual(self.drive({'FAKE_PROVIDER_FAIL': message}), 'HOLD')
                state = self.state()
                self.assertIn(message, state['hold_reason'])
                self.assertNotIn('hold_kind', state)
                self.assertEqual((waits, state['invocations_used']), ([], 1))

    def test_repeated_short_known_resets_exhaust_the_retry_slots(self):
        env = {**self.limited_env(SITES[0][0], once=False), 'FAKE_RATE_LIMIT_RETRY_AFTER': '12'}
        with self.clock() as (_, waits):
            self.assertEqual(self.drive(env, '--max-rate-limit-wait', '60'), 'HOLD')
        self.assertEqual(waits, [12, 12, 12, 12])
        state = self.state()
        self.assertEqual((state['hold_kind'], state['invocations_used'], state['rate_limit_wait_used']),
                         ('rate_limited', 0, 48))
        self.assertIn('backoff exhausted', state['hold_reason'])
        self.assertEqual(len(state['turns']), 5)
        self.assertTrue(all(not row['invocation_budget_counted'] for row in state['turns']))

    def test_spawn_failure_on_quota_retry_clears_the_saved_wait(self):
        env = {**self.limited_env(SITES[0][0]), 'FAKE_RATE_LIMIT_RETRY_AFTER': '12'}
        co = rc.Coordinator(self.h.args())
        spawn = rc.winplat.spawn_group

        def spawn_or_fail(*args, **kwargs):
            if co.state['active']['sequence'] == 2:
                raise OSError('quota retry executable missing')
            return spawn(*args, **kwargs)

        with self.clock() as (_, waits), patch.dict(os.environ, env), \
                patch.object(rc.winplat, 'spawn_group', side_effect=spawn_or_fail):
            with self.assertRaisesRegex(RuntimeError, 'failed to start: quota retry executable missing'):
                co.invoke('author', 'PLAN', co._author_prompt(), rc.author_schema())
        self.assertEqual(waits, [12])
        state = self.state()
        self.assertNotIn('rate_limit_wait', state)
        self.assertIsNone(state['active'])
        self.assertEqual(state['invocations_used'], 0)
        self.assertEqual([row['sequence'] for row in state['turns']], [1])
        [failure] = state['spawn_failures']
        self.assertEqual(failure['sequence'], 2)
        self.assertFalse(failure['child_created'])

    def test_an_exhausted_wait_budget_cannot_be_raised_on_resume(self):
        env = {**self.limited_env(SITES[0][0], once=False), 'FAKE_RATE_LIMIT_RETRY_AFTER': '12'}
        with self.clock() as (_, waits):
            self.assertEqual(self.drive(env, '--max-rate-limit-wait', '12'), 'HOLD')
        self.assertEqual(waits, [12])
        state = self.state()
        self.assertIn('only 0 s of the run-wide wait budget remain', state['hold_reason'])
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: max_rate_limit_wait'):
            self.drive({}, '--max-rate-limit-wait', '24', action='resume')
        self.assertEqual(self.state(), state)

    def test_resume_saved_wait(self):
        expected = self.baseline()
        for hard_restart in (False, True):
            with self.subTest(hard_restart=hard_restart), self.clock() as (now, waits):
                self.fresh(f'polish-resume-{hard_restart}')
                env = self.limited_env(SITES[7][0])
                env['FAKE_RATE_LIMIT_RESET_AT'] = str(now + 30)
                def stop(seconds):
                    self.assertEqual(seconds, 30)
                    durable.append(self.state())
                    raise rc.DetachStop()
                durable = []
                with patch.object(rc, 'sleep_rate_limit', side_effect=stop), self.assertRaises(rc.DetachStop):
                    self.drive(env)
                [saved] = durable
                self.assertIn('polish_reservation', saved)
                self.assertNotIn('polish_reservation', self.state())
                if hard_restart:
                    rc.atomic_json(self.h.run_dir / 'state.json', saved)
                self.assertEqual(saved['rate_limit_wait_used'], 30)
                self.assertIsNone(saved['active'])
                self.assertIsNone(saved.get('uncertain_active'))
                self.assertEqual(self.drive(env, action='resume'), 'DONE', self.state().get('hold_reason'))
                self.assertEqual(waits, [30])
                final = self.state()
                self.assertEqual(final['rate_limit_wait_used'], 30)
                self.assertEqual(self.done_turns(final), self.done_turns(expected))
                for key in ('specialist_counts', 'polish_calls'):
                    self.assertEqual(final['lifecycle'][key], expected['lifecycle'][key])
                self.assertNotIn('polish_reservation', final)
                self.assertNotIn('rate_limit_wait', final)

    def test_rate_limits_never_use_up_the_docs_or_security_stage_budget(self):
        for marker, phase, cap in (('Role: security reviewer, fresh. Phase: SECURITY.', 'SECURITY', 3),
                                   ('Role: docs writer, fresh. Phase: DOCS.', 'DOCS', 7)):
            with self.subTest(phase=phase):
                self.fresh('budget-' + phase)
                env = self.limited_env(marker, once=False)
                result = self.cli(env, extra=('--max-rate-limit-wait', '0'))
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
        self.assertNotIn('rate_limit_wait_used', state)

    def test_another_hold_clears_the_rate_limit_kind(self):
        self.fresh('other-hold')
        self.assert_rate_hold(self.cli(self.limited_env('plan author. Phase: PLAN.'),
                                       extra=('--max-rate-limit-wait', '0')), 'author', 'PLAN')
        co = rc.Coordinator(self.h.args(action='resume'))
        co.hold('an unrelated hold')
        self.assertNotIn('hold_kind', self.state())
        self.assertNotIn('rate_limit', self.state())


if __name__ == '__main__':
    unittest.main()
