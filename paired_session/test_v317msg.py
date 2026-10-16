"""V317-MSG: operator recovery hints use the current public surface on both routes."""
import json
import os
import re
import unittest
from unittest.mock import patch

from paired_session import test_worktree_lifecycle as twl

rc = twl.rc


class RecoveryHintTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name)
                     for name in (*twl._HELPERS, 'setUp')})

    def setUp(self):
        twl.WorktreeLifecycleActivationTests.setUp(self)
        public = patch.dict(os.environ, {'PAIRED_SESSION_INTERNAL_TEST_REMOVED_OPTIONS': '0',
                                         'PAIRED_SESSION_INTERNAL_TEST_LIFECYCLE_OFF': '0'})
        public.start()
        self.addCleanup(public.stop)

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def assert_public_hint(self, hint, commands):
        # Parse the actual flag spellings in the emitted hint with concrete values for N/TEXT.
        flags = set(re.findall(r'--[a-z][a-z-]*', hint))
        parsed_flags = set()
        for command in commands:
            args = rc.parser().parse_args(self.command()[3:] + command)
            self.assertFalse(rc.refuse_removed_options(args))
            parsed_flags.update(word for word in command if word.startswith('--'))
        self.assertEqual(flags, parsed_flags, hint)
        self.assertNotIn('accept', hint)

    def round_limit(self, route, phase):
        flags = ('--lifecycle-mode', route, '--shadow', 'off', '--exercise-revisions',
                 '--max-' + phase.lower() + '-rounds', '1', '--max-invocations', '60')
        held = self.run_coordinator(*flags)
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        self.assertIn('HOLD: ' + phase + ' round limit reached', held.stdout)
        [hint] = [line for line in held.stdout.splitlines() if line.startswith('NEXT:')]
        self.assertEqual(hint, 'NEXT: resume --add-rounds N (1-10) continues this run with N more rounds; '
                              'note --scope-change starts a successor run; or abort')
        self.assert_public_hint(hint, [['resume', '--add-rounds', '1'], ['note', '--scope-change'], ['abort']])
        resumed = self.run_operator_action('resume', *flags, '--add-rounds', '1')
        self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)

    def test_plan_round_limit_hint_on_both_routes(self):
        for route in ('off', 'on'):
            with self.subTest(route=route):
                self.run_dir = self.root / ('plan-' + route)
                self.round_limit(route, 'PLAN')

    def test_exec_round_limit_hint_on_both_routes(self):
        for route in ('off', 'on'):
            with self.subTest(route=route):
                self.run_dir = self.root / ('exec-' + route)
                self.round_limit(route, 'EXEC')

    def rejected_tree(self, route, terminal=False):
        flags = ('--lifecycle-mode', route, '--shadow', 'off', '--max-invocations', '60')
        # Give each route the same initial code; the previous subcase's fake author
        # output must not make this route's recovery write an unchanged tree.
        (self.workspace / 'sum_ints.py').write_text(
            'def sum_ints(values):\n    if not all(type(x) is int for x in values):\n'
            '        raise TypeError("ints only")\n    return sum(values)\n')
        env = {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1', 'FAKE_AUTHOR_WRITE_NEW_TREE': ''}
        done = self.run_coordinator(*flags, env=env)
        self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
        if terminal:
            # The saved rejection cap is the existing fixture's way to reach this HOLD directly.
            state = self.state()
            state['max_rejections'] = 0
            (self.run_dir / 'state.json').write_text(json.dumps(state))
        held = self.run_operator_action('reject', *flags, '--text', 'Recheck integer inputs.',
                                        env=env)
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        reason = self.state()['hold_reason']
        self.assertIn('HOLD: ' + reason, held.stdout)
        self.assertIn('rejected-tree', reason)
        hint = reason.split('; ', 1)[1]
        if terminal:
            self.assertEqual(hint, 'post-DONE rejection limit reached; note and resume, reject --scope-change, or abort')
            self.assert_public_hint(hint, [['note'], ['resume'], ['reject', '--scope-change'], ['abort']])
        elif route == 'off':
            self.assertEqual(hint, 'note, change the workspace and resume, note --scope-change, or abort')
            self.assert_public_hint(hint, [['note'], ['resume'], ['note', '--scope-change'], ['abort']])
        else:
            self.assert_public_hint(hint, [['note'], ['abort']])
        # The same HOLD reason is persisted in the operator report and progress stream.
        self.assertIn(reason, (self.run_dir / 'review-comparison.md').read_text())
        self.assertIn(reason, (self.run_dir / 'progress.jsonl').read_text())

        if terminal:
            unchanged = self.run_operator_action('resume', *flags, env=env)
            self.assertEqual(unchanged.returncode, 2, unchanged.stdout + unchanged.stderr)
            self.assertEqual(self.state()['hold_reason'], reason)
            noted = self.run_operator_action('note', *flags, '--text', 'Recheck integer inputs.', env=env)
            self.assertEqual(noted.returncode, 0, noted.stdout + noted.stderr)
            self.assertNotIn('terminal_hold_kind', self.state())
            # Keep credential environment names stable; allow the fake author to apply the note.
            resumed = self.run_operator_action('resume', *flags, env={'FAKE_AUTHOR_NO_REJECTION_CHANGE': '', 'FAKE_AUTHOR_WRITE_NEW_TREE': '1'})
            self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
            self.assertEqual(self.state()['operator_notes'][-1]['status'], 'delivered')

    def test_rejected_tree_hint_on_both_routes(self):
        for route in ('off', 'on'):
            with self.subTest(route=route):
                self.run_dir = self.root / ('rejected-' + route)
                self.rejected_tree(route)

    def test_rejection_limit_hint_on_both_routes(self):
        for route in ('off', 'on'):
            with self.subTest(route=route):
                self.run_dir = self.root / ('terminal-' + route)
                self.rejected_tree(route, terminal=True)

    def test_saved_off_voided_probe_waiver_recommends_only_permission_probe(self):
        # A format-2 run from before removal could record a waiver. Create it through
        # the old fake-CLI surface, then resume through today's public surface.
        flags = ('--lifecycle-mode', 'off', '--gate-vendor', 'claude', '--shadow', 'off')
        with patch.dict(os.environ, {'PAIRED_SESSION_INTERNAL_TEST_REMOVED_OPTIONS': '1'}):
            held = self.run_coordinator(*flags, '--accept-probe-skip', '--reason', 'old operator waiver',
                                        '--stop-after-plan')
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        self.assertIn('probe_skip_override', self.state())
        command = self.command(*flags)
        command[2] = 'resume'
        resumed = rc.subprocess.run(command, cwd=self.root, text=True, capture_output=True,
                                    env={**os.environ, 'FAKE_SECRET': 'changed credential environment'})
        self.assertEqual(resumed.returncode, 2, resumed.stdout + resumed.stderr)
        self.assertIn('the earlier --accept-probe-skip is void', resumed.stdout)
        self.assertIn('; run permission-probe)', resumed.stdout)
        self.assertNotIn('pass --accept-probe-skip', resumed.stdout)
        self.assertNotIn('--reason TEXT again', resumed.stdout)
        # The removed flag above names the historical waiver, not a recommendation. Take the hint from the
        # REFUSED line (the last occurrence): the progress line before it now shows the reason's head (V319-REASON).
        hint = resumed.stdout.rsplit('; run permission-probe)', 1)[1]
        self.assertIn('run permission-probe before continuing', hint)
        self.assert_public_hint(hint, [['permission-probe']])


if __name__ == '__main__':
    unittest.main()
