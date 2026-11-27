"""WI-SWITCH (v3.18.0): at a HOLD (or a stopped quota wait) resume moves a role to another vendor and the run goes on."""
import contextlib
import copy
import io
import json
import os
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc
from paired_session import test_worktree_lifecycle as twl
from paired_session.test_v317g1 import digest

DONE = twl.DONE
FIXED = 'role models are fixed for this run'
EXEC_REVIEW = 'Role: reviewer, read-only whole-delta reviewer. Phase: EXEC.'
EXEC_AUTHOR = 'implementer. Phase: EXEC.'
GATE = 'adversarial reviewer with a skepticism-by-default stance'
DOCS_WRITER = 'Role: docs writer, fresh. Phase: DOCS.'
CODEX_REVIEWER = ('--reviewer-vendor', 'codex', '--reviewer-model', 'gpt-6.1-sol')
TO_CLAUDE = ('--reviewer-vendor', 'claude', '--reviewer-model', 'claude-opus-5-5')
TO_GEMINI = ('--reviewer-vendor', 'gemini', '--reviewer-model', 'gemini-3.8-flash-medium')


class VendorSwitchTests(unittest.TestCase):
    def setUp(self):
        self.h = twl.WorktreeLifecycleActivationTests('test_a_no_op_worktree_run_goes_through_every_stage_and_reaches_done_only_after_security')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.docs = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'}

    # ---- helpers ----
    def state(self):
        return json.loads((self.h.run_dir / 'state.json').read_text())

    def state_bytes(self):
        return (self.h.run_dir / 'state.json').read_bytes()

    def argv(self, action, *extra):
        argv = self.h.command('--lifecycle-mode', 'on', *extra)[2:]
        argv[0] = action
        return argv

    def main(self, action, *extra, env=None, drive=None):
        """The real main() in-process; drive, when given, wraps Coordinator.drive (it gets the coordinator and the original)."""
        out = io.StringIO()
        original = rc.Coordinator.drive
        with patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out), contextlib.ExitStack() as stack:
            if drive is not None:
                stack.enter_context(patch.object(rc.Coordinator, 'drive', autospec=True,
                                                 side_effect=lambda co: drive(co, original)))
            code = rc.main(self.argv(action, *extra))
        return code, out.getvalue()

    def limited(self, marker, once=False):
        env = {**self.docs, 'FAKE_RATE_LIMIT': '1', 'FAKE_RATE_LIMIT_MATCH': marker}
        if once:
            env['FAKE_RATE_LIMIT_ONCE'] = str(self.h.run_dir) + '.limited'
        return env

    def quota_hold(self, marker, *flags):
        code, output = self.main('run', *flags, '--max-rate-limit-wait', '0', env=self.limited(marker))
        self.assertEqual(code, 2, output)
        state = self.state()
        self.assertEqual((state['status'], state.get('hold_kind')), ('HOLD', 'rate_limited'), state.get('hold_reason'))
        return state

    def held_run(self, *flags):
        """A cheap run at a HOLD: the state is created, no turn runs."""
        co = rc.Coordinator(self.h.args(*flags))
        co.hold('test hold')
        return co

    def receipt(self, turn):
        name = f"{turn['sequence']:03d}-{turn['phase'].lower()}-{turn['role']}.receipt.json"
        return json.loads((self.h.run_dir / 'evidence' / name).read_text())

    def after(self, state, sequence):
        return [row for row in state['turns'] if row['sequence'] > sequence and not row.get('error')]

    def assert_counted(self, state):
        self.assertEqual(state['invocations_used'], sum(row['invocation_budget_counted'] is not False for row in state['turns']))

    # ---- AC1 ----
    def test_reviewer_vendor_switch_at_quota_hold_keeps_rounds_and_ledger(self):
        # the exercise rule's PLAN finding is in the ledger; the codex reviewer is held on its EXEC review (the field case)
        held = self.quota_hold(EXEC_REVIEW, *CODEX_REVIEWER, '--exercise-revisions')
        self.assertEqual((held['rate_limit']['role'], held['phase'], held['exec_rounds']), ('reviewer', 'EXEC', 1))
        self.assertIn('resume --reviewer-vendor VENDOR --reviewer-model MODEL', held['hold_reason'])
        self.assertTrue(held['finding_ledger'] and held['plan_reviews'])
        resumed, prompts = [], []
        review_prompt = rc.Coordinator._review_prompt

        def spy(co, role, *args):
            prompt = review_prompt(co, role, *args)
            prompts.append((role, co.state['phase'], prompt))
            return prompt

        def snapshot(co, original):
            resumed.append(copy.deepcopy(co.state))
            return original(co)

        with patch.object(rc.Coordinator, '_review_prompt', spy):
            code, output = self.main('resume', *TO_CLAUDE, '--exercise-revisions', env=self.docs, drive=snapshot)
        self.assertEqual(code, 0, output)
        [first_prompt, *later] = [prompt for role, phase, prompt in prompts if (role, phase) == ('reviewer', 'EXEC')]
        self.assertIn('This is a replacement reviewer session with no thread history', first_prompt)
        self.assertTrue(all('Use your persistent thread history' in prompt for prompt in later))
        self.assertIn(DONE, output)
        self.assertIn('NOTE: resumed with role changes: reviewer_vendor codex -> claude, reviewer_model gpt-6.1-sol -> claude-opus-5-5',
                      output)
        [start] = resumed
        for key in ('phase', 'exec_rounds', 'plan_rounds', 'exec_reviews', 'invocations_used', 'turns'):
            self.assertEqual(start[key], held[key], key)
        self.assertEqual([(row['id'], row['status']) for row in start['finding_ledger']],
                         [(row['id'], row['status']) for row in held['finding_ledger']])
        self.assertFalse(start['started']['reviewer'])
        session = start['sessions']['reviewer']
        self.assertTrue(session)
        self.assertNotEqual(session, held['sessions']['reviewer'])
        final = self.state()
        self.assertEqual(final['status'], 'DONE')
        self.assert_counted(final)
        new = self.after(final, held['sequence'])
        reviews = [row for row in new if row['role'] in ('reviewer', 'shadow')]
        self.assertTrue({'reviewer', 'shadow'} <= {row['role'] for row in reviews})
        self.assertTrue(all(row['vendor'] == 'claude' and row['model'] == 'claude-opus-5-5' for row in reviews), reviews)
        first = next(row for row in new if row['role'] == 'reviewer' and row['phase'] == 'EXEC')
        command = self.receipt(first)['command']
        self.assertEqual(command[command.index('--session-id') + 1], session)
        self.assertTrue(all(row['vendor'] == 'codex' for row in new if row['role'] in ('author', 'gate')))
        self.assertEqual([row['key'] for row in final['role_resume_changes']], ['reviewer_vendor', 'reviewer_model'])

    # ---- AC2 ----
    def test_reviewer_switch_to_gemini_binds_agy(self):
        held = self.quota_hold(EXEC_REVIEW, *CODEX_REVIEWER)
        saved = self.state_bytes()
        code, output = self.main('resume', *TO_GEMINI, '--gemini-bin', str(self.h.root / 'no-such-agy'), env=self.docs)
        self.assertEqual(code, 2, output)
        self.assertIn('REFUSED', output)
        self.assertEqual(self.state_bytes(), saved)
        code, output = self.main('resume', *TO_GEMINI, '--gemini-bin', str(twl.trc.RealCoordinatorTests.fake_gemini_cli(self.h)), env=self.docs)
        self.assertEqual(code, 0, output)
        self.assertIn(DONE, output)
        self.assertIn('NOTE: operator program or PATH changed since run start (changed: gemini_bin)', output)
        final = self.state()
        self.assertIn('gemini_bin', final['operator_programs'])
        new = self.after(final, held['sequence'])
        reviews = [row for row in new if row['role'] in ('reviewer', 'shadow')]
        self.assertTrue({'EXEC', 'DOCS', 'SECURITY'} <= {row['phase'] for row in reviews}, reviews)
        self.assertIn('shadow', {row['role'] for row in reviews})
        self.assertTrue(all(row['vendor'] == 'gemini' for row in reviews), reviews)

    # ---- AC3 ----
    def test_author_vendor_switch_continues_exec(self):
        agy = ('--gemini-bin', str(twl.trc.RealCoordinatorTests.fake_gemini_cli(self.h)))   # a reviewer that is neither old nor new author
        held = self.quota_hold(EXEC_AUTHOR, *TO_GEMINI, *agy)
        self.assertEqual((held['rate_limit']['role'], held['phase']), ('author', 'EXEC'))
        (self.h.workspace / 'partial.txt').write_text('kept\n')   # what the limited author turn left behind
        prompts = []
        author_prompt = rc.Coordinator._author_prompt

        def spy(co, *args, **kwargs):
            prompt = author_prompt(co, *args, **kwargs)
            prompts.append((prompt, (self.h.workspace / 'partial.txt').read_text()))
            return prompt

        with patch.object(rc.Coordinator, '_author_prompt', spy):
            code, output = self.main('resume', '--author-vendor', 'claude', '--author-model', 'claude-opus-5-5', *agy, env=self.docs)
        self.assertEqual(code, 0, output)
        self.assertIn(DONE, output)
        prompt, partial = prompts[0]
        self.assertIn('Approved plan: ', prompt)
        self.assertEqual(partial, 'kept\n')
        final = self.state()
        new = self.after(final, held['sequence'])
        authors = [row for row in new if row['role'] == 'author']
        self.assertTrue({'EXEC', 'FINISH', 'DOCS'} <= {row['phase'] for row in authors}, authors)
        self.assertTrue(all(row['vendor'] == 'claude' for row in authors), authors)
        command = self.receipt(authors[0])['command']
        self.assertIn('--session-id', command)
        self.assertTrue(all(row['vendor'] == 'gemini' for row in new if row['role'] in ('reviewer', 'shadow')))
        self.assertTrue(all(row['vendor'] == 'codex' for row in new if row['role'] == 'gate'))
        self.assertEqual(final['config']['gate_vendor'], 'codex')   # Q2: the gate keeps its vendor

    # ---- AC4 ----
    def test_gate_vendor_switch(self):
        held = self.quota_hold(GATE)
        self.assertEqual(held['rate_limit']['role'], 'gate')
        code, output = self.main('resume', '--gate-vendor', 'claude', '--gate-model', 'claude-opus-5-5', env=self.docs)
        self.assertEqual(code, 0, output)
        self.assertIn(DONE, output)
        gates = [row for row in self.after(self.state(), held['sequence']) if row['role'] == 'gate']
        self.assertEqual([(row['vendor'], row['model']) for row in gates], [('claude', 'claude-opus-5-5')])
        self.assertEqual(self.state()['config']['gate_vendor_source'], held['config']['gate_vendor_source'])

        for command in (['git', 'reset', '-q', '--hard', 'HEAD'], ['git', 'clean', '-q', '-fd']):   # a second run from the base tree
            rc.subprocess.run(command, cwd=self.h.workspace, check=True)
        self.h.run_dir = self.h.root / 'gate-ran'
        held = self.quota_hold(DOCS_WRITER)
        self.assertTrue(held['gate_ran'])
        gates = [row for row in held['turns'] if row['role'] == 'gate' and not row.get('error')]
        code, output = self.main('resume', '--gate-vendor', 'claude', '--gate-model', 'claude-opus-5-5', env=self.docs)
        self.assertEqual(code, 0, output)
        self.assertIn(DONE, output)
        self.assertEqual([row for row in self.state()['turns'] if row['role'] == 'gate' and not row.get('error')], gates)

    # ---- AC5 ----
    def test_vendor_switch_rebuilds_only_that_slot_and_audits(self):
        co = self.held_run(*CODEX_REVIEWER)
        before = self.state()
        old = before['role_dispatch_manifest']
        code, output = self.main('resume', *TO_CLAUDE, drive=lambda co, _: 'HOLD')
        self.assertEqual(code, 2, output)
        self.assertIn('role-switch', output)
        state = self.state()
        manifest = state['role_dispatch_manifest']
        self.assertEqual(manifest['role_flags_sha256'], digest(manifest['role_flags']))
        self.assertEqual(manifest['config_sha256'], digest(state['config']))
        self.assertEqual(state['role_dispatch_manifest_sha256'], digest(manifest))
        flags = manifest['role_flags']
        self.assertEqual((flags['reviewer']['reviewer_vendor'], flags['reviewer']['reviewer_model']), ('claude', 'claude-opus-5-5'))
        self.assertIn('claude_bash_sandbox', flags['reviewer'])
        self.assertNotIn('codex_reviewer_sandbox', flags['reviewer'])
        self.assertEqual(flags['shadow'], flags['reviewer'])
        self.assertEqual(flags['author'], old['role_flags']['author'])
        for key in ('agent_body_sha256', 'role_agents', 'agents_dir', 'gate_prompt_sha256', 'gate_prompt_path'):
            self.assertEqual(manifest[key], old[key], key)
        rows = state['role_resume_changes']
        self.assertEqual([(row['key'], row['old'], row['new']) for row in rows],
                         [('reviewer_vendor', 'codex', 'claude'), ('reviewer_model', 'gpt-6.1-sol', 'claude-opus-5-5')])
        for row in rows:
            self.assertEqual(row['hold_reason'], 'test hold')
            self.assertTrue(row['timestamp'])
        self.assertEqual(rows[0]['slots'], {name: {'old_sha256': digest(old['role_flags'][name]), 'new_sha256': digest(flags[name])}
                                            for name in ('reviewer', 'shadow')})
        self.assertNotIn('slots', rows[1])
        resumed = rc.Coordinator(self.h.args(action='resume'))
        resumed._verify_frozen_role_dispatch()   # the next lifecycle dispatch accepts the rebuilt slot
        self.assertEqual(resumed.args.reviewer_vendor, 'claude')
        self.assertEqual(co.state['config']['reviewer_vendor'], 'codex')

    # ---- AC6 ----
    def test_vendor_switch_refusals(self):
        def refused(message, *flags, action='resume'):
            saved = self.state_bytes()
            code, output = self.main(action, *flags, drive=lambda co, _: self.fail('a refused switch dispatched'))
            self.assertEqual(code, 2, output)
            self.assertIn(message, output)
            self.assertEqual(self.state_bytes(), saved)

        co = self.held_run()
        for status in ('DONE', 'ACTIVE'):
            with self.subTest(status=status):
                co.state.update(status=status, hold_reason='')
                co.save()
                refused(FIXED + ': reviewer_vendor differs from the saved run; a vendor switch needs a HOLD',
                        '--reviewer-vendor', 'codex', '--reviewer-model', 'gpt-6-sol')
        co.hold('test hold')
        cases = (('needs an explicit --reviewer-model', ('--reviewer-vendor', 'codex')),
                 ('gate_model gpt-6-sol does not belong to the claude vendor', ('--gate-vendor', 'claude', '--gate-model', 'gpt-6-sol')),
                 ('the author and the reviewer would both run claude claude-opus-5-5',
                  ('--author-vendor', 'claude', '--author-model', 'claude-opus-5-5')))
        for message, flags in cases:
            with self.subTest(message=message):
                refused(message, *flags)
        for action in ('note', 'abort'):
            with self.subTest(action=action):
                refused(FIXED, '--reviewer-vendor', 'codex', '--reviewer-model', 'gpt-6-sol',
                        *(('--text', 'x') if action == 'note' else ()), action=action)
        profile = self.h.workspace / '.review-loop' / 'paired-session.json'
        profile.parent.mkdir(exist_ok=True)
        profile.write_text(json.dumps({'reviewer_vendor': 'codex'}))
        refused(FIXED + ': reviewer_vendor differs from the saved run', '--reviewer-model', 'gpt-6-sol')
        profile.unlink()

        self.h.run_dir = self.h.root / 'policy-run'
        policy = self.h.root / 'operator.json'
        policy.write_text(json.dumps({'allowed_models': {'codex': ['gpt-6.1-sol'],
                                                         'claude': ['claude-opus-5-5', 'claude-sonnet-5-5']}}))
        argv = self.argv('run', '--config', str(policy), '--gate-vendor', 'claude', '--gate-model', 'claude-sonnet-5-5')
        rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv)).hold('test hold')
        refused('not in allowed_models', '--reviewer-vendor', 'codex', '--reviewer-model', 'gpt-6-sol')
        refused('the author and the gate would both run claude claude-sonnet-5-5',
                '--author-vendor', 'claude', '--author-model', 'claude-sonnet-5-5')

        self.h.run_dir = self.h.root / 'polish-run'
        argv = self.h.command('--lifecycle-mode', 'off')[2:]
        rc.Coordinator(rc.parser().parse_args(argv)).hold('test hold')
        saved = self.state_bytes()
        argv[0] = 'resume'
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = rc.main([*argv, '--polish', '--reviewer-vendor', 'codex', '--reviewer-model', 'gpt-6-sol'])
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn(FIXED, out.getvalue())
        self.assertEqual(self.state_bytes(), saved)

    # ---- AC7 ----
    def test_vendor_switch_with_uncertain_turn(self):
        co = self.held_run(*CODEX_REVIEWER)
        co.state.update(status='HOLD', hold_reason='interrupted', uncertain_active={
            'sequence': 1, 'role': 'reviewer', 'phase': 'PLAN', 'vendor': 'codex', 'pid': 12345, 'job': 'test-job', 'fresh': False})
        co.save()
        code, output = self.main('resume', *TO_CLAUDE, drive=lambda co, _: self.fail('uncertain run dispatched'))
        self.assertEqual(code, 2, output)
        self.assertIn('NOTE: Ignored role flags: --reviewer-vendor, --reviewer-model.', output)
        self.assertEqual(self.state()['config'], co.state['config'])
        self.assertNotIn('role_resume_changes', self.state())
        with patch.object(rc, 'probe_group', side_effect=ProcessLookupError):
            code, output = self.main('resume', *TO_CLAUDE, '--retry-uncertain', drive=lambda co, _: 'HOLD')
        self.assertIn('NOTE: resumed with role changes: reviewer_vendor codex -> claude', output)
        state = self.state()
        self.assertTrue(state['abandoned_turns'][0]['group_gone'])
        self.assertEqual(state['config']['reviewer_vendor'], 'claude')
        self.assertEqual(state['role_resume_changes'][0]['hold_reason'], 'interrupted')

    # ---- AC8 ----
    def test_vendor_switch_from_stopped_quota_wait(self):
        env = self.limited(EXEC_REVIEW, once=True)
        now = int(rc.time.time())
        env['FAKE_RATE_LIMIT_RESET_AT'] = str(now + 30)

        def stop(seconds):
            raise rc.DetachStop()

        with patch.dict(os.environ, env), patch.object(rc, 'sleep_rate_limit', side_effect=stop), self.assertRaises(rc.DetachStop):
            rc.Coordinator(self.h.args(*CODEX_REVIEWER)).drive()
        stopped = self.state()
        self.assertEqual(stopped['status'], 'ACTIVE')
        self.assertEqual(stopped['rate_limit_wait']['dispatch']['vendor'], 'codex')
        with patch.object(rc, 'sleep_rate_limit', side_effect=AssertionError('the dropped wait slept')):
            code, output = self.main('resume', *TO_CLAUDE, env=self.docs)
        self.assertEqual(code, 0, output)
        self.assertIn(DONE, output)
        self.assertNotIn('saved quota retry does not match', output)
        final = self.state()
        self.assertNotIn('rate_limit_wait', final)
        self.assertEqual(final['role_resume_changes'][0]['key'], 'reviewer_vendor')
        reviews = [row for row in self.after(final, stopped['sequence']) if row['role'] == 'reviewer']
        self.assertTrue(reviews and all(row['vendor'] == 'claude' for row in reviews))


if __name__ == '__main__':
    unittest.main()
