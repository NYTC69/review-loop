"""WI-SWITCH (v3.18.0): at a HOLD (or a stopped quota wait) resume moves a role to another vendor and the run goes on."""
import argparse
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
CLAUDE_MODEL = rc.resolve_role_model_defaults(argparse.Namespace(   # the claude default (the model the fake CLI reports)
    author_vendor='claude', reviewer_vendor='claude', gate_vendor='claude', author_model=None, reviewer_model=None,
    gate_model=None)).reviewer_model
OTHER_CLAUDE = 'claude-sonnet-5-5'
TO_CLAUDE = ('--reviewer-vendor', 'claude', '--reviewer-model', CLAUDE_MODEL)
TO_GEMINI = ('--reviewer-vendor', 'gemini', '--reviewer-model', 'gemini-3.8-flash-medium')
GEMINI_GATE = ('--gate-vendor', 'gemini', '--gate-model', 'gemini-3.8-flash-medium')


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

    def intent(self, action, *extra):
        """An --intent-only command: stdout must be the intent JSON alone; the NOTEs go to stderr."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rc.main(self.argv(action, *extra, '--intent-only'))
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        self.assertIn('digest', json.loads(out.getvalue()))
        return err.getvalue()

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

    def base_tree_run(self, name):
        """Another run in this test from the base tree: the workspace is reset and the run dir is new."""
        for command in (['git', 'reset', '-q', '--hard', 'HEAD'], ['git', 'clean', '-q', '-fd']):
            rc.subprocess.run(command, cwd=self.h.workspace, check=True)
        self.h.run_dir = self.h.root / name

    def fake_agy(self):
        """The path of the fake Gemini CLI (agy) the real-coordinator tests use."""
        return str(twl.trc.RealCoordinatorTests.fake_gemini_cli(self.h))

    def agy_copy(self, name):
        """An executable copy of the fake agy at another path (a second --gemini-bin); returns the Path."""
        path = self.h.root / name
        path.write_text(rc.Path(self.fake_agy()).read_text(encoding='utf-8'))
        path.chmod(0o755)
        return path

    def write_profile(self, keys):
        """Write the workspace profile (.review-loop/paired-session.json) and return its path."""
        profile = self.h.workspace / '.review-loop' / 'paired-session.json'
        profile.parent.mkdir(exist_ok=True)
        profile.write_text(json.dumps(keys))
        return profile

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
        self.assertIn('NOTE: resumed with role changes: reviewer_vendor codex -> claude, reviewer_model gpt-6.1-sol -> ' + CLAUDE_MODEL,
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
        self.assertTrue(all(row['vendor'] == 'claude' and row['model'] == CLAUDE_MODEL for row in reviews), reviews)
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
        code, output = self.main('resume', *TO_GEMINI, '--gemini-bin', self.fake_agy(), env=self.docs)
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

    def test_a_plain_resume_after_a_switch_to_gemini_keeps_the_agy_path(self):
        agy = self.fake_agy()
        cases = (('reviewer', CODEX_REVIEWER, TO_GEMINI),
                 ('gate', (), GEMINI_GATE))
        for role, start, switch in cases:
            with self.subTest(role=role):
                self.base_tree_run('agy-' + role)
                self.held_run(*start)
                code, output = self.main('resume', *switch, '--gemini-bin', agy, drive=lambda co, _: co.hold('test hold') or 'HOLD')
                self.assertEqual(code, 2, output)
                [row] = [row for row in self.state()['role_resume_changes'] if row['key'] == role + '_vendor']
                self.assertEqual(row['gemini_bin'], agy)
                code, output = self.main('resume', env=self.docs)   # no --gemini-bin: the saved path, not the default agy
                self.assertEqual(code, 0, output)
                self.assertIn(f'NOTE: using the saved --gemini-bin {agy} ' + (   # WI-AGYBIN: a frozen slot fixes the path
                    '(pass --gemini-bin to change it)' if role == 'gate' else '(fixed while the frozen gemini reviewer slot uses it)'),
                    output)
                self.assertIn(DONE, output)
                self.assertIn(f'NOTE: using the saved --gemini-bin {agy}', self.intent('accept', '--reason', 'owner accepts'))
                rows = [row for row in self.state()['turns'] if row['role'] in ((role,) if role == 'gate' else (role, 'shadow'))
                        and not row.get('error')]
                self.assertTrue(rows and all(row['vendor'] == 'gemini' for row in rows), rows)

    # ---- WI-AGYBIN (v3.18.1): the run's agy path stays saved while a role runs gemini, and is dropped when none does ----
    def drop_agy_from_path(self):
        """PATH without an agy for the whole test (this machine may have a real one): the default --gemini-bin agy binds nothing."""
        dirs = os.environ.get('PATH', '').split(os.pathsep)
        patcher = patch.dict(os.environ, {'PATH': os.pathsep.join(d for d in dirs if not os.path.exists(os.path.join(d, 'agy')))})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_switch_away_from_gemini_drops_the_agy_binding(self):
        self.drop_agy_from_path()
        held = self.quota_hold(EXEC_AUTHOR, *TO_GEMINI, '--gemini-bin', self.fake_agy())
        self.assertIn('gemini_bin', held['operator_programs'])
        code, output = self.main('resume', *TO_CLAUDE, env=self.docs, drive=lambda co, _: co.hold('test hold') or 'HOLD')
        self.assertEqual(code, 2, output)
        self.assertNotIn('gemini-bin', output)
        code, output = self.main('resume', env=self.docs)   # no --gemini-bin, no agy on PATH, no role on gemini
        self.assertEqual(code, 0, output)
        self.assertIn(DONE, output)
        self.assertNotIn('gemini-bin', output)
        self.assertNotIn('gemini_bin', output)
        final = self.state()
        self.assertNotIn('gemini_bin', final['operator_programs'])
        self.assertTrue(all(row['vendor'] != 'gemini' for row in self.after(final, held['sequence'])))

    def test_a_gemini_gate_from_run_start_keeps_the_agy_path(self):
        self.drop_agy_from_path()
        agy = self.fake_agy()
        gate = (*GEMINI_GATE, '--gemini-bin', agy)

        def slot_switched_away():   # the only frozen gemini slot moves to claude; the gate stays gemini
            self.quota_hold(EXEC_AUTHOR, *gate, '--reviewer-vendor', 'gemini', '--reviewer-model', 'gemini-3.8-pro')
            code, output = self.main('resume', *TO_CLAUDE, env=self.docs, drive=lambda co, _: co.hold('test hold') or 'HOLD')
            self.assertEqual(code, 2, output)

        cases = (('held before any turn', lambda: self.held_run(*gate)),
                 ('quota hold', lambda: self.quota_hold(EXEC_AUTHOR, *gate)),
                 ('slot switched away', slot_switched_away))
        for name, start in cases:
            with self.subTest(start=name):
                self.base_tree_run('gate-agy-' + name.replace(' ', '-'))
                start()
                code, output = self.main('resume', env=self.docs)   # no --gemini-bin: the saved path
                self.assertEqual(code, 0, output)
                self.assertIn(DONE, output)
                self.assertIn(f'NOTE: using the saved --gemini-bin {agy} (pass --gemini-bin to change it)', output)
                gates = [row for row in self.state()['turns'] if row['role'] == 'gate' and not row.get('error')]
                self.assertTrue(gates and all(row['vendor'] == 'gemini' for row in gates), gates)

    def test_an_explicit_gemini_bin_on_resume_wins_and_is_kept(self):
        self.drop_agy_from_path()
        other = self.agy_copy('other-agy')
        self.quota_hold(EXEC_AUTHOR, *GEMINI_GATE, '--gemini-bin', self.fake_agy())
        code, output = self.main('resume', '--gemini-bin', str(other), env=self.docs)
        self.assertEqual(code, 0, output)
        self.assertIn(DONE, output)
        self.assertNotIn('using the saved --gemini-bin', output)
        self.assertIn(f'NOTE: using the saved --gemini-bin {other} (pass --gemini-bin to change it)',
                      self.intent('accept', '--reason', 'owner accepts'))

    def test_a_lifecycle_off_run_keeps_a_later_gemini_bin_over_the_frozen_slot(self):
        """Lifecycle off does not verify the frozen slots, so a later accepted --gemini-bin wins over the slot's path."""
        self.drop_agy_from_path()
        old, other = self.agy_copy('old-agy'), self.agy_copy('other-agy')
        off = ('--lifecycle-mode', 'off')
        held = self.quota_hold(EXEC_AUTHOR, *off, *TO_GEMINI, '--gemini-bin', str(old))
        self.assertEqual(held['role_dispatch_manifest']['role_flags']['reviewer']['reviewer_binary'], str(old))
        # the new path is accepted and dispatched with (the author's retry binds it), then the author is held on quota again
        code, output = self.main('resume', *off, '--gemini-bin', str(other), '--max-rate-limit-wait', '0',
                                 env=self.limited(EXEC_AUTHOR))
        self.assertEqual(code, 2, output)
        self.assertNotIn('using the saved --gemini-bin', output)
        state = self.state()
        self.assertEqual((state['hold_kind'], state['gemini_bin']), ('rate_limited', str(other)), state.get('hold_reason'))
        self.assertEqual(state['role_dispatch_manifest']['role_flags']['reviewer']['reviewer_binary'], str(old))
        old.unlink()   # the replaced agy is gone: a command that went back to it would stop on the program binding
        # a gate switch keeps the unswitched gemini reviewer slot, frozen with the old path; lifecycle off does not hold to it
        code, output = self.main('resume', *off, *GEMINI_GATE, env=self.docs)
        self.assertEqual(code, 0, output)
        self.assertIn(DONE, output)
        self.assertIn(f'NOTE: using the saved --gemini-bin {other} (pass --gemini-bin to change it)', output)
        self.assertNotIn('fixed while', output)
        self.assertNotIn('changed: gemini_bin', output)
        self.assertEqual(self.state()['gemini_bin'], str(other))
        note = self.intent('accept', *off, '--reason', 'owner accepts')
        self.assertIn(f'NOTE: using the saved --gemini-bin {other} (pass --gemini-bin to change it)', note)

    # ---- AC3 ----
    def test_author_vendor_switch_continues_exec(self):
        agy = ('--gemini-bin', self.fake_agy())   # a reviewer that is neither old nor new author
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
            code, output = self.main('resume', '--author-vendor', 'claude', '--author-model', CLAUDE_MODEL, *agy, env=self.docs)
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
        code, output = self.main('resume', '--gate-vendor', 'claude', '--gate-model', CLAUDE_MODEL, env=self.docs)
        self.assertEqual(code, 0, output)
        self.assertIn(DONE, output)
        gates = [row for row in self.after(self.state(), held['sequence']) if row['role'] == 'gate']
        self.assertEqual([(row['vendor'], row['model']) for row in gates], [('claude', CLAUDE_MODEL)])
        self.assertEqual(self.state()['config']['gate_vendor_source'], held['config']['gate_vendor_source'])

        # a second run held after the gate ran (on the DOCS writer): the gate's turns are not re-run
        self.base_tree_run('gate-ran')
        held = self.quota_hold(DOCS_WRITER)
        self.assertTrue(held['gate_ran'])
        gates = [row for row in held['turns'] if row['role'] == 'gate' and not row.get('error')]
        code, output = self.main('resume', '--gate-vendor', 'claude', '--gate-model', CLAUDE_MODEL, env=self.docs)
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
        self.assertEqual((flags['reviewer']['reviewer_vendor'], flags['reviewer']['reviewer_model']), ('claude', CLAUDE_MODEL))
        self.assertIn('claude_bash_sandbox', flags['reviewer'])
        self.assertNotIn('codex_reviewer_sandbox', flags['reviewer'])
        self.assertEqual(flags['shadow'], flags['reviewer'])
        self.assertEqual(flags['author'], old['role_flags']['author'])
        for key in ('agent_body_sha256', 'role_agents', 'agents_dir', 'gate_prompt_sha256', 'gate_prompt_path'):
            self.assertEqual(manifest[key], old[key], key)
        rows = state['role_resume_changes']
        self.assertEqual([(row['key'], row['old'], row['new']) for row in rows],
                         [('reviewer_vendor', 'codex', 'claude'), ('reviewer_model', 'gpt-6.1-sol', CLAUDE_MODEL)])
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

    def test_vendor_switch_refuses_when_an_unswitched_claude_slot_drifts(self):
        # the kept claude slot was built under the run-start env-var names; a switch from a shell with another secret-looking
        # name is refused before any state write, with the env hint, and succeeds from the run's environment
        cases = (('reviewer', ('--author-vendor', 'claude', '--author-model', CLAUDE_MODEL, *CODEX_REVIEWER),
                  ('--reviewer-vendor', 'claude', '--reviewer-model', OTHER_CLAUDE)),
                 ('author', (), ('--author-vendor', 'claude', '--author-model', OTHER_CLAUDE)))
        for role, start, switch in cases:
            with self.subTest(role=role):
                self.h.run_dir = self.h.root / ('drift-' + role)
                self.held_run(*start)
                saved = self.state_bytes()
                code, output = self.main('resume', *switch, env={'SWITCH_DRIFT_TOKEN': 'x'},
                                         drive=lambda co, _: self.fail('a refused switch dispatched'))
                self.assertEqual(code, 2, output)
                self.assertIn('credential env-var names added: SWITCH_DRIFT_TOKEN; removed: none; restore the environment '
                              '(unset SWITCH_DRIFT_TOKEN) and run the switching resume again', output)
                self.assertEqual(self.state_bytes(), saved)
                code, output = self.main('resume', *switch, drive=lambda co, _: 'HOLD')
                self.assertEqual(code, 2, output)
                self.assertEqual(self.state()['config'][role + '_model'], OTHER_CLAUDE)
                rc.Coordinator(self.h.args(action='resume'))._verify_frozen_role_dispatch()

    def test_profile_naming_the_switched_away_vendor_keeps_the_saved_one(self):
        self.held_run(*CODEX_REVIEWER)
        code, output = self.main('resume', *TO_CLAUDE, drive=lambda co, _: co.hold('test hold') or 'HOLD')
        self.assertEqual(code, 2, output)
        profile = self.write_profile({'reviewer_vendor': 'codex'})
        note = 'NOTE: the workspace profile names reviewer_vendor codex, which this run switched away from on resume; keeping the saved claude'
        code, output = self.main('note', '--text', 'x')
        self.assertEqual(code, 0, output)
        self.assertIn(note, output)
        code, output = self.main('resume', drive=lambda co, _: 'HOLD')
        self.assertEqual(code, 2, output)
        self.assertIn(note, output)
        self.assertNotIn(FIXED, output)
        state = self.state()
        self.assertEqual(state['config']['reviewer_vendor'], 'claude')
        self.assertEqual(len(state['role_resume_changes']), 2)
        profile.write_text(json.dumps({'reviewer_vendor': 'gemini'}))   # a vendor the run never had is still refused
        code, output = self.main('resume', drive=lambda co, _: self.fail('dispatched'))
        self.assertEqual(code, 2, output)
        self.assertIn(FIXED + ': reviewer_vendor differs from the saved run', output)
        profile.unlink()   # a workspace profile with a vendor key holds at dispatch; the run reaches DONE without it
        code, output = self.main('resume', env=self.docs)
        self.assertEqual(code, 0, output)
        self.assertIn(DONE, output)
        profile.write_text(json.dumps({'reviewer_vendor': 'codex'}))
        self.assertIn(note, self.intent('accept', '--reason', 'owner accepts'))   # stdout stays the intent JSON

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
                 # the saved model of a switched role is checked against the new vendor too
                 ('author_model gpt-6.1-sol does not belong to the claude vendor',
                  ('--author-vendor', 'claude', '--author-model', 'gpt-6.1-sol')),
                 (f'reviewer_model {CLAUDE_MODEL} does not belong to the codex vendor',
                  ('--reviewer-vendor', 'codex', '--reviewer-model', CLAUDE_MODEL)),
                 (f'the author and the reviewer would both run claude {CLAUDE_MODEL}',
                  ('--author-vendor', 'claude', '--author-model', CLAUDE_MODEL)))
        for message, flags in cases:
            with self.subTest(message=message):
                refused(message, *flags)
        for action in ('note', 'abort'):
            with self.subTest(action=action):
                refused(FIXED, '--reviewer-vendor', 'codex', '--reviewer-model', 'gpt-6-sol',
                        *(('--text', 'x') if action == 'note' else ()), action=action)
        profile = self.write_profile({'reviewer_vendor': 'codex'})   # a profile vendor key is refused like the flag
        refused(FIXED + ': reviewer_vendor differs from the saved run', '--reviewer-model', 'gpt-6-sol')
        profile.unlink()

        self.h.run_dir = self.h.root / 'policy-run'
        policy = self.h.root / 'operator.json'
        policy.write_text(json.dumps({'allowed_models': {'codex': ['gpt-6.1-sol'],
                                                         'claude': [CLAUDE_MODEL, OTHER_CLAUDE]}}))
        argv = self.argv('run', '--config', str(policy), '--gate-vendor', 'claude', '--gate-model', OTHER_CLAUDE)
        rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv)).hold('test hold')
        refused('not in allowed_models', '--reviewer-vendor', 'codex', '--reviewer-model', 'gpt-6-sol')
        refused(f'the author and the gate would both run claude {OTHER_CLAUDE}',
                '--author-vendor', 'claude', '--author-model', OTHER_CLAUDE)

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
