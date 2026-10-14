"""V317-G1: a run whose frozen bundled gate template is another review-loop version's, driven through fake provider CLIs;
and the texts of the advisory-only repair attempt, the PLAN advisory approval and the submodule auto_commit refusal."""
import hashlib
import json
import os
import shutil
import unittest
from unittest.mock import patch

from paired_session import test_v317_once as once
from paired_session import test_worktree_lifecycle as twl
from paired_session.test_field11_plan_ledger_ids import PLAN_WITH_IDS

rc = twl.rc
ON = ('--lifecycle-mode', 'on', '--max-invocations', '80')
OFF = ('--lifecycle-mode', 'off', '--shadow', 'off')
PREFIX = 'REFUSED: resume configuration differs: gate_prompt'
OTHER_VERSION = '; this run was started by another review-loop version (the bundled gate prompt differs): '
AT_HOLD = OTHER_VERSION + ('continue it with that version, or with this one abort it, record a scope change '
                           '(`note --scope-change`) or start a new run')
AT_DONE = OTHER_VERSION + ('continue it with that version, or with this one accept it, abort it, record a scope change '
                           '(`reject --scope-change`) or start a new run')
AT_LIMIT = AT_HOLD.replace('`note ', '`reject ')
LIMIT = 'rejected-tree; post-DONE rejection limit reached'
REFREEZE = 'run `permission-probe` to re-freeze, then resume'
BLOCKING_TASK = 'Fix every delivered blocking finding, rerun relevant checks, and summarize the result in body.'
ADVISORY_TASK = ('The delivered review left only non-blocking rows open; they do not block approval. Fix the ones worth '
                 'fixing now and rerun relevant checks; in body, say which rows you fixed and which you left and why.')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class OtherVersionRunTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in twl._HELPERS})

    def command(self, *extra):   # the product default (efficient): its HOLD is the one that named permission-probe
        return [arg for arg in twl.trc.RealCoordinatorTests.command(self, *extra) if arg != '--strict']

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def files(self):
        """What a refused command must leave as it was: the state, the ledger and the rejection records."""
        return ((self.run_dir / 'state.json').read_bytes(), (self.run_dir / 'findings-ledger.json').read_bytes(),
                sorted(path.name for path in (self.run_dir / 'evidence').glob('rejection-*')))

    def from_other_version(self):
        """Rewrite the frozen hashes of the bundled gate template the way a release with another template text wrote them."""
        state = self.state()
        other = hashlib.sha256(b'the bundled gate template of another release\n').hexdigest()
        self.assertTrue(state['config']['gate_prompt'].startswith('<bundled-default>:'))
        state['config']['gate_prompt'] = '<bundled-default>:' + other
        state['operator_programs']['gate_prompt']['sha256'] = other
        if manifest := state.get('role_dispatch_manifest'):
            manifest.update(gate_prompt_sha256=other, config_sha256=digest(state['config']))
            state['role_dispatch_manifest_sha256'] = digest(manifest)
        (self.run_dir / 'state.json').write_text(json.dumps(state))

    def done(self, *mode):
        result = self.run_coordinator(*mode)
        self.assertIn(twl.DONE, result.stdout, result.stdout + result.stderr)
        self.from_other_version()

    def held(self, *mode):
        result = self.run_coordinator(*mode, '--stop-after-plan')
        self.assertIn('HOLD: ' + rc.PLAN_STOP_REASON, result.stdout, result.stdout + result.stderr)
        self.from_other_version()

    def snapshot(self, name):
        shutil.copytree(self.run_dir, self.root / name, symlinks=True)
        return self.root / name

    def restore(self, copy):
        shutil.rmtree(self.run_dir)
        shutil.copytree(copy, self.run_dir, symlinks=True)

    def action(self, action, *extra, env=None):
        """One operator command exactly as given (no intent step of the harness)."""
        command = self.command(*extra); command[2] = action
        return rc.subprocess.run([*command, '--skip-probe'], cwd=self.root, env={**os.environ, **(env or {})}, text=True,
                                 stdout=rc.subprocess.PIPE, stderr=rc.subprocess.PIPE)

    def assert_refused(self, result, exits, before):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(PREFIX + exits, result.stdout)
        self.assertNotIn('permission-probe', result.stdout)
        self.assertNotIn('then resume', result.stdout)
        self.assertEqual(self.files(), before)

    def test_reject_at_done_is_refused_before_any_record_and_accept_still_works(self):
        for mode in (OFF, ON):   # the stop-after-gate route and the default full lifecycle
            with self.subTest(mode=mode[1]):
                self.run_dir = self.root / ('reject-' + mode[1])
                self.done(*mode)
                before = self.files()
                refused = self.run_operator_action('reject', '--text', 'Please handle an edge case.')   # the two-step form
                self.assert_refused(refused, AT_DONE, before)
                direct = self.command(); direct[2] = 'reject'   # the --text form with a digest in hand
                direct += ['--skip-probe', '--text', 'Please handle an edge case.', '--expect', '0' * 64]
                refused = rc.subprocess.run(direct, cwd=self.root, text=True, stdout=rc.subprocess.PIPE, stderr=rc.subprocess.PIPE)
                self.assert_refused(refused, AT_DONE, before)
                state = self.state()
                self.assertEqual((state['status'], state['acceptance_state'], state.get('rejections', [])), ('DONE', 'PENDING', []))
                accepted = self.run_operator_action('accept')
                self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
                self.assertEqual(self.state()['status'], 'ACCEPTED')

    def test_rejection_limit_hint_names_only_working_exits_and_each_of_them_works(self):
        quiet = {'FAKE_AUTHOR_NO_REJECTION_CHANGE': '1', 'FAKE_AUTHOR_WRITE_NEW_TREE': ''}
        for mode in (OFF, ON):
            with self.subTest(mode=mode[1]):
                self.run_dir = run_dir = self.root / ('limit-' + mode[1])
                (self.workspace / 'sum_ints.py').write_text(   # the same starting code on each route (as test_v317msg)
                    'def sum_ints(values):\n    if not all(type(x) is int for x in values):\n'
                    '        raise TypeError("ints only")\n    return sum(values)\n')
                done = self.run_coordinator(*mode, env=quiet)
                self.assertIn(twl.DONE, done.stdout, done.stdout + done.stderr)
                state = self.state()
                state['max_rejections'] = 0   # the saved cap reaches the rejection-limit HOLD directly (as test_v317fb)
                (self.run_dir / 'state.json').write_text(json.dumps(state))
                held = self.run_operator_action('reject', *mode, '--text', 'Recheck integer inputs.', env=quiet)
                self.assertIn('HOLD: ' + LIMIT + '; note and resume, reject --scope-change, or abort', held.stdout)
                started = self.snapshot('started-' + mode[1])   # the HOLD as the version that started the run left it
                self.from_other_version()
                upgraded = self.snapshot('upgraded-' + mode[1])
                # the ordinary first acceptance step, and on the stop-after-gate route a direct accept
                for extra in (('--intent-only',), *([()] if mode is OFF else [])):
                    refused = self.action('accept', *mode, *extra)
                    self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                    self.assertEqual([line for line in refused.stdout.splitlines() if line.startswith('REFUSED')],
                                     ['REFUSED: ' + LIMIT + AT_LIMIT])
                    self.assertNotIn('note and resume', refused.stdout)
                    self.assertEqual(self.state()['hold_reason'], LIMIT + AT_LIMIT)
                    self.assertEqual(self.state().get('operator_notes', []), [])
                # every step the text names: a scope change, abort, a new run, and the version that started the run
                changed = self.run_operator_action('reject', *mode, '--scope-change', '--text', 'New scope: include floats.')
                self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
                self.assertEqual(self.state()['status'], 'ABORTED')
                self.restore(upgraded)
                aborted = self.run_operator_action('abort', *mode)
                self.assertIn('HOLD: aborted by operator', aborted.stdout, aborted.stdout + aborted.stderr)
                self.run_dir = self.root / ('new-' + mode[1])
                fresh = self.run_coordinator(*mode, '--stop-after-plan')
                self.assertIn('HOLD: ' + rc.PLAN_STOP_REASON, fresh.stdout, fresh.stdout + fresh.stderr)
                self.run_dir = run_dir
                self.restore(started)
                noted = self.run_operator_action('note', *mode, '--text', 'Recheck integer inputs.', env=quiet)
                self.assertEqual(noted.returncode, 0, noted.stdout + noted.stderr)
                resumed = self.run_operator_action('resume', *mode, env={'FAKE_AUTHOR_NO_REJECTION_CHANGE': '',
                                                                         'FAKE_AUTHOR_WRITE_NEW_TREE': '1'})
                self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)

    def test_rejected_tree_hold_texts_of_such_a_run(self):
        self.done(*OFF)
        for kind, text in ((None, 'rejected-tree' + AT_HOLD), ('rejection_limit', LIMIT + AT_LIMIT)):
            with self.subTest(kind=kind):
                command = self.command(*OFF); command[2] = 'accept'
                co = rc.Coordinator(rc.parser().parse_args(command[2:]))
                co.hold('rejected-tree', terminal_kind=kind)
                self.assertEqual(co.state['hold_reason'], text)

    def test_reject_scope_change_at_done_still_works(self):
        self.done(*OFF)
        changed = self.run_operator_action('reject', *OFF, '--scope-change', '--text', 'New scope: include floats.')
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        self.assertEqual(self.state()['status'], 'ABORTED')

    def test_resume_and_probe_at_a_hold_are_refused_with_the_exits_and_abort_works(self):
        for mode in (OFF, ON):
            self.run_dir = self.root / ('held-' + mode[1])
            self.held(*mode)
            before = self.files()
            for action, extra in (('resume', ()), ('resume', ('--add-rounds', '1')), ('permission-probe', ())):
                with self.subTest(mode=mode[1], action=action, extra=extra):
                    self.assert_refused(self.run_operator_action(action, *mode, *extra), AT_HOLD, before)
            with self.subTest(mode=mode[1], action='abort'):
                aborted = self.run_operator_action('abort', *mode)
                self.assertEqual(aborted.returncode, 2, aborted.stdout + aborted.stderr)
                self.assertIn('HOLD: aborted by operator', aborted.stdout)
                self.assertEqual((self.state()['status'], self.state()['hold_reason']), ('HOLD', 'aborted by operator'))
                refused = self.run_operator_action('resume', *mode)   # an aborted run is not continued here either
                self.assertIn(PREFIX + AT_HOLD, refused.stdout)

    def test_a_second_difference_keeps_the_plain_refusal_and_abort_keeps_its_other_checks(self):
        self.held(*OFF)
        before = self.files()
        # max_exec_rounds is compared before gate_prompt, reviewer_command after it
        for flag, value, key, first in (('--max-exec-rounds', '9', 'max_exec_rounds', 'max_exec_rounds'),
                                        ('--reviewer-command', 'python3 --version', 'reviewer_command', 'gate_prompt')):
            for action, differs in (('resume', first), ('permission-probe', first), ('abort', key)):
                with self.subTest(key=key, action=action):
                    refused = self.run_operator_action(action, *OFF, flag, value)
                    self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                    self.assertEqual([line for line in refused.stdout.splitlines() if line.startswith('REFUSED')],
                                     ['REFUSED: resume configuration differs: ' + differs])
                    self.assertNotIn('another review-loop version', refused.stdout)
                    self.assertEqual(self.files(), before)

    def test_note_scope_change_at_a_hold_still_works(self):
        self.held(*OFF)
        changed = self.run_operator_action('note', *OFF, '--scope-change', '--text', 'New scope: include floats.')
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        self.assertEqual(self.state()['status'], 'ABORTED')

    def test_a_user_gate_prompt_file_keeps_its_texts(self):
        prompt, moved = self.root / 'gate.txt', self.root / 'moved-gate.txt'
        template = rc.DEFAULT_GATE_PROMPT.read_text()
        prompt.write_text(template); moved.write_text(template)
        own = (*OFF, '--gate-prompt', str(prompt))
        result = self.run_coordinator(*own, '--stop-after-plan')
        self.assertIn('HOLD: ' + rc.PLAN_STOP_REASON, result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.state()['config']['gate_prompt'], str(prompt.resolve()))
        refused = self.run_operator_action('resume', *OFF, '--gate-prompt', str(moved))   # another file
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual([line for line in refused.stdout.splitlines() if line.startswith('REFUSED')], [PREFIX])
        prompt.write_text(template + '\nA changed line.\n')                               # the same file, changed
        held = self.run_operator_action('resume', *own)
        self.assertEqual(held.returncode, 2, held.stdout + held.stderr)
        self.assertIn('HOLD: operator program or PATH changed since run start (changed: gate_prompt); ' + REFREEZE, held.stdout)
        self.assertNotIn('another review-loop version', refused.stdout + held.stdout)

    def test_the_program_hold_names_the_working_exits(self):
        self.done(*OFF)
        command = self.command(*OFF); command[2] = 'accept'
        co = rc.Coordinator(rc.parser().parse_args(command[2:]))
        issue = co._program_state()[1]
        self.assertEqual(issue, 'operator program or PATH changed since run start (changed: gate_prompt)' + AT_DONE)


class TextTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in twl._HELPERS})
    record_review = once.AdvisoryOnceTests.record_review

    def exec_run(self):
        co = self.coordinator(*OFF, '--adversarial-gate', 'off', '--max-exec-rounds', '4')
        (co.context / 'plan.md').write_text('Implement the helper.\n')
        co.state['exec_rounds'] = 1
        return co

    def test_the_advisory_only_repair_attempt_says_so_and_gives_each_open_row_with_its_summary(self):
        for own in ('MINOR', None):   # a finding of its own, or only an earlier row left open
            with self.subTest(own=own):
                self.run_dir = self.root / ('repair-' + str(own))
                co = self.exec_run()
                [row] = co.record_findings('adversarial-gate', 'EXEC', 1, [
                    {'severity': 'LOW', 'file': 'tracked.txt', 'summary': 'simplify the helper', 'failure_scenario': 'reads oddly'}])
                first = self.record_review(co, 'EXEC', own)
                co.reviewer_turn()
                self.assertEqual((co.state['next'], co.state['advisory_repair_attempts']), ('author', {'EXEC': first}))
                delivered = json.loads(co.state['delivered_review'])
                self.assertIs(delivered['advisory_only'], True)
                self.assertEqual([r.get('summary') for r in delivered['findings']], ['advisory note'] if own else [])
                self.assertEqual(delivered['prior_findings'], [{'id': row['id'], 'fixed': False, 'evidence': 'not addressed',
                                                                'summary': 'simplify the helper'}])
                prompt = co._author_prompt()
                self.assertIn(ADVISORY_TASK, prompt)
                self.assertNotIn('blocking finding', prompt)

    def test_every_other_revise_keeps_its_delivered_review_and_author_prompt(self):
        blocker = {'id': 'F002', 'severity': 'MAJOR', 'file': 'tracked.txt', 'summary': 'advisory note',
                   'failure_scenario': 'small detail', 'source': 'persistent-reviewer'}
        for name, severity, changes, findings in (('blocking', 'MAJOR', {}, [blocker]),
                                                  ('attempt-used', None, {'reviewed_snapshot': '0' * 40}, [])):
            with self.subTest(name=name):
                self.run_dir = self.root / name
                co = self.exec_run()
                self.record_review(co, 'EXEC', 'MINOR')
                co.reviewer_turn()   # the one advisory-only repair attempt
                co.state['exec_rounds'] = 2
                self.record_review(co, 'EXEC', severity, **changes)
                co.reviewer_turn()
                self.assertEqual((co.state['next'], co.state['review_verdicts'][-1]['effective_verdict']), ('author', 'REVISE'))
                delivered = json.dumps({   # the whole text, key for key as before this change
                    'source': 'persistent-reviewer', 'status': 'REVISE', 'findings': findings,
                    'prior_findings': [{'id': 'F001', 'fixed': False, 'evidence': 'not addressed'}]}, ensure_ascii=False)
                self.assertEqual(co.state['delivered_review'], delivered)
                self.assertEqual(co._author_prompt(), '\n'.join([   # the whole prompt of an off-route EXEC repair turn
                    'Role: persistent codex implementer. Phase: EXEC.',
                    f'Workspace: {co.workspace}', f'Work item: {co.context / "workitem.md"}', BLOCKING_TASK,
                    co.author_control_contract(), 'Delivered review:\n' + delivered, rc.AUTHOR_HISTORY_RULE,
                    'Do not commit or push. Do not load review-loop skills. Do not edit outside the workspace.',
                    'For long commands, use the longest single wait your tool permits. Do not wait for another model.',
                    'Return only JSON matching the supplied schema. READY means this turn is complete; HOLD means blocked.']))

    def test_plan_history_text_after_an_advisory_approval_names_the_open_rows(self):
        for verdict, attempts, text in (('REVISE', {'PLAN': 5}, 'The plan was approved with advisory rows left open (F001), but it'),
                                        ('APPROVE', {}, 'The reviewer approved the plan, but it')):
            with self.subTest(verdict=verdict):
                self.run_dir = self.root / ('plan-' + verdict.lower())
                co = self.coordinator('--lifecycle-mode', 'off')
                co.state.update(phase='PLAN', next='reviewer', plan_rounds=2, advisory_repair_attempts=attempts)
                (co.context / 'plan.md').write_text(PLAN_WITH_IDS)
                [row] = co.record_findings('persistent-reviewer', 'PLAN', 1, [
                    {'severity': 'MINOR', 'file': 'plan.md', 'summary': 'plan advisory', 'failure_scenario': 'detail missing'}])
                snapshot = rc.git_snapshot(co.workspace)[0]
                answer = {'status': verdict, 'reviewed_snapshot': snapshot, 'full_review': [], 'self_run_evidence': [],
                          'prior_findings': [{'id': row['id'], 'disposition': 'still_open', 'evidence': 'not addressed'}]}
                result = {'answer': answer, 'snapshot': snapshot, 'sequence': 10, 'role': 'reviewer'}
                with patch.object(co, 'invoke', return_value=result), patch.object(co, 'render'):
                    co.reviewer_turn()
                self.assertEqual((co.state['status'], co.state['phase'], co.state['next']), ('ACTIVE', 'PLAN', 'author'))
                self.assertEqual(co.state['review_verdicts'][-1]['effective_verdict'],
                                 'REVISE')   # FIELD-11 refuses the approval; the advisory conversion came first
                self.assertEqual(bool(co.state['finding_ledger'][0].get('advisory')), verdict == 'REVISE')
                self.assertTrue(co.state['delivered_review'].startswith(text + ' carries review history ('),
                                co.state['delivered_review'])
                self.assertNotIn('plan_history_rewrite', co.state)

    def test_submodule_auto_commit_refusal_names_a_working_exit(self):
        co = self.coordinator(*ON)
        git = lambda *args: rc.subprocess.run(['git', *args], cwd=self.workspace, check=True)
        head = rc.subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.workspace, text=True).strip()
        git('update-index', '--add', '--cacheinfo', '160000,' + head + ',vendor/lib')
        git('commit', '-qm', 'a submodule')
        with self.assertRaises(ValueError) as caught:
            co._commit_refusals(['tracked.txt'])
        self.assertEqual(str(caught.exception), 'auto_commit refuses a repository with submodules; abort and commit the tree '
                                                'yourself (auto_commit is frozen at run start)')


if __name__ == '__main__':
    unittest.main()
