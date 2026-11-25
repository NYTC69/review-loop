"""WI-DOCSFIX (v3.16.6): operator edits at a DOCS HOLD are never rolled back by the docs writer's outside-write rollback,
a change outside the docs paths goes to the EXEC reviewer, and DOCS budget exhaustion after an operator fix allows one
docs review recheck of the changed tree. Fake provider CLIs only."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE
ON = ('--lifecycle-mode', 'on', '--max-invocations', '80')
DOCS_ENV = {'FAKE_LIFECYCLE_DOCS_FILE': 'CHANGELOG.md'}


class DocsOperatorEditTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})

    # --- helpers ------------------------------------------------------------------------------------------------------
    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def assert_done(self, result):
        self.assertIn(DONE, result.stdout, result.stdout + result.stderr)

    def coordinator_for(self, action='run'):
        command = self.command(*ON)
        command[2] = action
        return rc.Coordinator(rc.parser().parse_args(command[2:]))

    def after(self, state, sequence):
        return [(turn['role'], turn['phase']) for turn in state['turns'] if turn['sequence'] > sequence]

    def docs_rows(self, state):
        return [row for row in state['lifecycle']['receipts'] if row['stage'] == 'DOCS']

    def progress_kinds(self):
        path = self.run_dir / 'progress.jsonl'
        return [json.loads(line)['kind'] for line in path.read_text().splitlines()] if path.is_file() else []

    def hold_at_docs_writer(self, env=None, writer_call=1, cap=None):
        """Drive in-process until the n-th DOCS writer dispatch, which fails with the invocation limit as in the field run:
        the request is pending, no writer turn is recorded."""
        calls = []
        original_invoke, original_cap = rc.Coordinator.invoke, rc.Coordinator._worktree_run_cap

        def invoke(co, role, phase, *args, **kwargs):
            if role == 'author' and phase == 'DOCS':
                calls.append(phase)
                if len(calls) == writer_call:
                    raise RuntimeError('invocation limit reached')
            return original_invoke(co, role, phase, *args, **kwargs)

        def stage_cap(co, stage):
            return cap if stage == 'DOCS' and cap else original_cap(co, stage)

        with mock.patch.dict(os.environ, env or {}), mock.patch.object(rc.Coordinator, 'invoke', invoke), \
                mock.patch.object(rc.Coordinator, '_worktree_run_cap', stage_cap):
            co = self.coordinator_for()
            self.assertEqual(co.drive(), 'HOLD', co.state.get('hold_reason'))
        state = self.state()
        self.assertEqual(state['hold_reason'], 'invocation limit reached')
        self.assertEqual((state['lifecycle']['pending']['stage'], state['lifecycle']['pending']['role']), ('DOCS', 'docs-writer'))
        return state

    def docs_writer_hold(self):
        held = self.run_coordinator(*ON, env={**DOCS_ENV, 'FAKE_DOCS_HOLD': '1'})
        self.assertIn('HOLD: docs writer: Fake docs writer held.', held.stdout, held.stdout + held.stderr)
        return self.state()

    def resume_in_process(self, cap=None, env=None, max_invocations=None):
        original_cap = rc.Coordinator._worktree_run_cap

        def stage_cap(co, stage):
            return cap if stage == 'DOCS' and cap else original_cap(co, stage)

        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), mock.patch.object(rc.Coordinator, '_worktree_run_cap', stage_cap), \
                contextlib.redirect_stdout(out):
            co = self.coordinator_for('resume')
            if max_invocations is not None:
                co.args.max_invocations = max_invocations
            status = co.resume()
        return status, co.state.get('hold_reason', ''), out.getvalue()

    # --- AC1 + AC2: a pending DOCS request (no writer turn recorded yet) ----------------------------------------------------
    def test_operator_code_edit_at_a_pending_docs_writer_goes_to_the_exec_reviewer_and_survives(self):
        held = self.hold_at_docs_writer()
        module = self.workspace / 'sum_ints.py'
        module.write_text(module.read_text() + '# operator code fix\n')
        (self.workspace / 'CHANGELOG.md').write_text('# operator changelog\n')
        self.assert_done(self.run_operator_action('resume', *ON))
        state = self.state()
        self.assertEqual(self.after(state, held['sequence'])[0], ('reviewer', 'EXEC'))   # no docs writer dispatch first
        self.assertIn('# operator code fix', module.read_text())
        self.assertEqual((self.workspace / 'CHANGELOG.md').read_text(), '# operator changelog\n')
        first = self.docs_rows(state)[0]
        self.assertEqual((first['request_id'], first['route'], first['operator_paths'], first['sequence']),
                         (held['lifecycle']['pending']['request_id'], 'EXEC', ['sum_ints.py'], None))
        self.assertNotIn('docs-rollback', self.progress_kinds())
        self.assertIn('CHANGELOG.md', state['lifecycle']['docs_owned'])   # the next DOCS pass reviews the operator's doc edit

    def test_operator_docs_edit_at_a_pending_docs_writer_stays_in_docs_and_is_reviewed(self):
        held = self.hold_at_docs_writer()
        (self.workspace / 'CHANGELOG.md').write_text('# operator changelog\n')
        self.assert_done(self.run_operator_action('resume', *ON))
        state = self.state()
        self.assertEqual(self.after(state, held['sequence']), [('author', 'DOCS'), ('reviewer', 'DOCS'), ('reviewer', 'SECURITY')])
        [docs] = self.docs_rows(state)
        self.assertEqual((docs['route'], docs['docs_paths'], docs['review']['status']), ('SECURITY', ['CHANGELOG.md'], 'APPROVE'))
        self.assertEqual((self.workspace / 'CHANGELOG.md').read_text(), '# operator changelog\n')

    def test_a_genuine_docs_writer_outside_write_is_still_rolled_back_beside_an_operator_docs_edit(self):
        held = self.hold_at_docs_writer()
        (self.workspace / 'CHANGELOG.md').write_text('# operator changelog\n')
        self.assert_done(self.run_operator_action('resume', *ON, env={'FAKE_DOCS_CODE_ONCE': str(self.root / 'docs-code')}))
        state = self.state()
        [docs] = self.docs_rows(state)
        self.assertEqual((docs['route'], docs['rolled_back_outside'], docs['docs_paths']), ('SECURITY', ['sum_ints.py'], ['CHANGELOG.md']))
        self.assertNotIn('# docs comment fix', (self.workspace / 'sum_ints.py').read_text())
        self.assertEqual((self.workspace / 'CHANGELOG.md').read_text(), '# operator changelog\n')
        self.assertNotIn(('reviewer', 'EXEC'), self.after(state, held['sequence']))

    # --- AC2: the fresh-request path (after a DOCS receipt HOLD) behaves the same ------------------------------------------
    def test_operator_code_edit_after_a_docs_writer_hold_goes_to_the_exec_reviewer(self):
        before = self.docs_writer_hold()
        module = self.workspace / 'sum_ints.py'
        module.write_text(module.read_text() + '# operator code fix\n')
        self.assert_done(self.run_operator_action('resume', *ON))
        state = self.state()
        self.assertEqual(self.after(state, before['sequence'])[0], ('reviewer', 'EXEC'))
        self.assertIn('# operator code fix', module.read_text())
        self.assertGreater(state['lifecycle']['epoch'], before['lifecycle']['epoch'])

    # --- AC3: DOCS budget exhaustion after an operator fix -----------------------------------------------------------------
    def test_docs_budget_hold_names_the_route_and_one_recheck_runs_per_operator_tree(self):
        env = {**DOCS_ENV, 'FAKE_DOCS_REVIEW_BLOCK_ONCE': str(self.root / 'docs-block')}
        out = io.StringIO()
        original_cap = rc.Coordinator._worktree_run_cap
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), mock.patch.object(
                rc.Coordinator, '_worktree_run_cap', lambda co, stage: 2 if stage == 'DOCS' else original_cap(co, stage)):
            co = self.coordinator_for()
            self.assertEqual(co.drive(), 'HOLD')
        held = self.state()
        [finding] = [row['id'] for row in held['finding_ledger'] if row['source'] == 'docs-reviewer' and row['status'] == 'open']
        self.assertTrue(held['hold_reason'].startswith('DOCS budget exhausted (2 writer and review calls in this run)'), held['hold_reason'])
        self.assertIn(finding, held['hold_reason'])
        self.assertIn('resume', held['hold_reason'])
        # no operator change: a resume holds again without a dispatch
        status, reason, _ = self.resume_in_process(cap=2, env=env)
        self.assertEqual((status, self.state()['sequence']), ('HOLD', held['sequence']))
        self.assertIn('DOCS budget exhausted', reason)
        # an operator edit that does not fix the entry: one recheck, REVISE, then a HOLD naming the finding
        changelog = self.workspace / 'CHANGELOG.md'
        changelog.write_text(changelog.read_text() + '# operator attempt\n')
        status, reason, _ = self.resume_in_process(cap=2, env=env)
        first = self.state()
        self.assertEqual(self.after(first, held['sequence']), [('reviewer', 'DOCS')])
        self.assertEqual(status, 'HOLD')
        self.assertIn(finding, reason)
        status, _, _ = self.resume_in_process(cap=2, env=env)   # the same tree: no second recheck
        self.assertEqual((status, self.state()['sequence']), ('HOLD', first['sequence']))
        # the fix: the recheck must fit the invocation budget first
        changelog.write_text(changelog.read_text() + '# corrected entry\n')
        status, reason, _ = self.resume_in_process(cap=2, env=env, max_invocations=first['invocations_used'])
        self.assertEqual((status, self.state()['sequence']), ('HOLD', first['sequence']))
        self.assertIn('the DOCS recheck needs at least 1 more invocations; resume with a higher --max-invocations', reason)
        status, reason, _ = self.resume_in_process(cap=2, env=env)
        self.assertEqual(status, 'DONE', reason)
        state = self.state()
        self.assertEqual(self.after(state, first['sequence']), [('reviewer', 'DOCS'), ('reviewer', 'SECURITY')])
        last = self.docs_rows(state)[-1]
        self.assertEqual((last['route'], last['review']['status'], last['review'].get('operator_recheck'), last['writer_skipped']),
                         ('SECURITY', 'APPROVE', True, True))
        self.assertIn('# corrected entry', changelog.read_text())
        self.assertNotIn(('author', 'DOCS'), self.after(state, held['sequence']))   # no docs writer beyond the cap

    # --- AC4: the field run's saved state -------------------------------------------------------------------------------------
    def operator_edits(self, tag):
        module = self.workspace / 'sum_ints.py'
        module.write_text(self.module_base + f'# operator code fix {tag}\n')
        (self.workspace / 'ARCHITECTURE.md').write_text(f'# architecture {tag}\n')
        (self.workspace / 'CLAUDE.md').write_text(f'# instructions {tag}\n')
        (self.workspace / 'CHANGELOG.md').write_text(f'# corrected entry {tag}\n')

    def test_field_state_resume_routes_to_the_exec_reviewer_and_reverts_no_operator_edit(self):
        env = {**DOCS_ENV, 'FAKE_DOCS_REVIEW_BLOCK_ONCE': str(self.root / 'docs-block')}
        self.hold_at_docs_writer(env, writer_call=2)   # DOCS r0 writer + REVISE review, r1 writer hits the invocation limit
        self.module_base = (self.workspace / 'sum_ints.py').read_text()
        self.operator_edits('v1')   # the operator fixes the DOCS findings at that HOLD
        # The v3.16.5 resume: the r1 writer turn is recorded, its rollback put the operator's outside edits back to the
        # pre-request tree, and the DOCS review hit the DOCS budget before the DOCS receipt.
        with mock.patch.dict(os.environ, env):
            co = self.coordinator_for('resume')
            co.state['status'] = 'ACTIVE'
            prompt = rc.worktree_lifecycle.docs_prompt(co.state['config'].get('docs_file'), co._docs_allowlist(),
                                                       co.run_dir.name, co.workitem.read_text(encoding='utf-8'))
            writer = co.invoke('author', 'DOCS', prompt, rc.author_schema(), fresh=True)
            (self.workspace / 'sum_ints.py').write_text(self.module_base)
            for name in ('ARCHITECTURE.md', 'CLAUDE.md'):
                (self.workspace / name).unlink()
            co.progress('docs-rollback', paths=['ARCHITECTURE.md', 'CLAUDE.md', 'sum_ints.py'])
            co.hold('DOCS budget exhausted (7 writer and review calls in this run); abort')
        held = self.state()
        self.assertEqual(held['lifecycle']['pending']['role'], 'docs-writer')
        self.operator_edits('v2')   # re-applied by hand: the tree differs from the writer's snapshot_after
        self.assertNotEqual(rc.git_snapshot(self.workspace)[0], writer['snapshot'])
        rollbacks = self.progress_kinds().count('docs-rollback')
        # a plain resume, with the DOCS cap spent as in the field run (r0 writer, r0 review, r1 writer)
        status, reason, out = self.resume_in_process(cap=3, env=env)
        self.assertEqual(status, 'DONE', reason + out)
        state = self.state()
        after = self.after(state, held['sequence'])
        self.assertEqual(after[0], ('reviewer', 'EXEC'))
        self.assertNotIn(('author', 'DOCS'), after)   # the DOCS cap is spent: no writer, one docs review recheck
        self.assertEqual(after.count(('reviewer', 'DOCS')), 1)
        r1 = next(row for row in self.docs_rows(state) if row['request_id'] == held['lifecycle']['pending']['request_id'])
        self.assertEqual((r1['route'], r1['operator_paths'], r1['sequence']),
                         ('EXEC', ['ARCHITECTURE.md', 'CLAUDE.md', 'sum_ints.py'], writer['sequence']))
        self.assertEqual(self.progress_kinds().count('docs-rollback'), rollbacks)
        self.assertIn('# operator code fix v2', (self.workspace / 'sum_ints.py').read_text())
        for name, text in (('ARCHITECTURE.md', '# architecture v2\n'), ('CLAUDE.md', '# instructions v2\n'),
                           ('CHANGELOG.md', '# corrected entry v2\n')):
            self.assertEqual((self.workspace / name).read_text(), text)

    # --- AC5 ---------------------------------------------------------------------------------------------------------------
    def test_the_invocation_limit_hold_says_attach_verification_keeps_the_saved_cap(self):
        co = self.coordinator_for()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            co.hold('invocation limit reached')
        self.assertIn('attach-verification', out.getvalue())
        self.assertIn('--max-invocations 80', out.getvalue())

    # --- WI-DOCSFIX2 AC1: a docs writer on the last DOCS slot gets its review beyond the cap --------------------------------
    def drive_with_docs_cap(self, cap, env, **patches):
        original_cap = rc.Coordinator._worktree_run_cap
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), mock.patch.object(
                rc.Coordinator, '_worktree_run_cap', lambda co, stage: cap if stage == 'DOCS' else original_cap(co, stage)), \
                contextlib.ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(rc.Coordinator, name, value))
            co = self.coordinator_for()
            return co.drive(), co.state.get('hold_reason', ''), out.getvalue()

    def docs_turns(self, state):
        return [(turn['role'], turn['phase']) for turn in state['turns'] if turn['phase'] == 'DOCS']

    def test_a_docs_writer_on_the_last_docs_slot_gets_its_review_beyond_the_cap(self):
        status, reason, out = self.drive_with_docs_cap(1, DOCS_ENV)
        self.assertEqual(status, 'DONE', reason + out)
        state = self.state()
        self.assertEqual(self.docs_turns(state), [('author', 'DOCS'), ('reviewer', 'DOCS')])
        [docs] = self.docs_rows(state)
        self.assertEqual((docs['route'], docs['review']['status'], docs['review'].get('operator_recheck'), docs['writer_skipped']),
                         ('SECURITY', 'APPROVE', True, False))

    def test_a_revise_of_the_review_beyond_the_cap_holds_without_another_docs_writer(self):
        env = {**DOCS_ENV, 'FAKE_DOCS_REVIEW_BLOCK_ONCE': str(self.root / 'docs-block')}
        status, reason, _ = self.drive_with_docs_cap(1, env)
        held = self.state()
        self.assertEqual(status, 'HOLD')
        self.assertEqual(self.docs_turns(held), [('author', 'DOCS'), ('reviewer', 'DOCS')])
        [finding] = [row['id'] for row in held['finding_ledger'] if row['source'] == 'docs-reviewer' and row['status'] == 'open']
        self.assertTrue(reason.startswith('DOCS budget exhausted (2 writer and review calls in this run)'), reason)
        self.assertIn(finding, reason)
        status, reason, _ = self.resume_in_process(cap=1, env=env)   # the reviewed tree: no second review, no writer
        self.assertEqual((status, self.state()['sequence']), ('HOLD', held['sequence']))
        self.assertIn('DOCS budget exhausted', reason)

    def test_a_run_held_with_an_unreviewed_writer_output_on_the_last_slot_resumes(self):
        # the v3.16.6 state: the last-slot writer turn is recorded, the DOCS budget HOLD came before its review
        budget = RuntimeError('DOCS budget exhausted (1 writer and review calls in this run); fix the documentation in the '
                              'workspace and resume')
        status, reason, _ = self.drive_with_docs_cap(1, DOCS_ENV, _docs_review_turn=mock.Mock(side_effect=budget))
        self.assertEqual(status, 'HOLD', reason)
        held = self.state()
        self.assertEqual(held['lifecycle']['pending']['role'], 'docs-writer')
        status, reason, out = self.resume_in_process(cap=1, env=DOCS_ENV)
        self.assertEqual(status, 'DONE', reason + out)
        state = self.state()
        self.assertEqual(self.after(state, held['sequence']), [('reviewer', 'DOCS'), ('reviewer', 'SECURITY')])
        [docs] = self.docs_rows(state)
        self.assertEqual((docs['route'], docs['sequence']), ('SECURITY', self.docs_writer_sequences(state)[0]))

    def docs_writer_sequences(self, state):
        return [turn['sequence'] for turn in state['turns'] if turn['role'] == 'author' and turn['phase'] == 'DOCS']

    # --- WI-DOCSFIX2 AC2: an errored docs writer turn's outside write is the writer's, not the operator's -------------------
    def errored_docs_writer(self):
        env = {**DOCS_ENV, 'FAKE_DOCS_CODE_ONCE': str(self.root / 'docs-code'), 'FAKE_DOCS_FAIL_ONCE': str(self.root / 'docs-fail')}
        held = self.run_coordinator(*ON, env=env)
        self.assertIn('HOLD', held.stdout, held.stdout + held.stderr)
        state = self.state()
        [writer] = [turn for turn in state['turns'] if turn['role'] == 'author' and turn['phase'] == 'DOCS']
        self.assertTrue(writer.get('error'), writer)
        self.assertIn('# docs comment fix', (self.workspace / 'sum_ints.py').read_text())
        return env, state

    def test_an_errored_docs_writer_outside_write_is_rolled_back_before_the_redispatch(self):
        env, held = self.errored_docs_writer()
        self.assert_done(self.run_operator_action('resume', *ON, env=env))
        state = self.state()
        self.assertEqual(self.after(state, held['sequence']), [('author', 'DOCS'), ('reviewer', 'DOCS'), ('reviewer', 'SECURITY')])
        self.assertNotIn('# docs comment fix', (self.workspace / 'sum_ints.py').read_text())
        self.assertIn('docs-rollback', self.progress_kinds())
        [docs] = self.docs_rows(state)
        self.assertEqual((docs['route'], docs['docs_paths']), ('SECURITY', ['CHANGELOG.md']))

    def test_an_operator_edit_over_an_errored_docs_writer_write_is_never_reverted(self):
        env, held = self.errored_docs_writer()
        module = self.workspace / 'sum_ints.py'
        module.write_text(module.read_text() + '# operator code fix\n')
        self.assert_done(self.run_operator_action('resume', *ON, env=env))
        state = self.state()
        self.assertEqual(self.after(state, held['sequence'])[0], ('reviewer', 'EXEC'))
        self.assertIn('# docs comment fix\n# operator code fix', module.read_text())
        self.assertNotIn('docs-rollback', self.progress_kinds())


if __name__ == '__main__':
    unittest.main()
