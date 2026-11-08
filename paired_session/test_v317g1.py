"""V317-G1, as changed by v3.7.0: a run whose frozen bundled gate template is another review-loop version's continues with this
version's template after one NOTE (the config, the program record and, for a lifecycle run, only the gate-prompt fields of the
frozen role dispatch manifest are re-frozen), driven through fake provider CLIs; and the texts of the advisory-only repair attempt, the PLAN advisory approval and the submodule auto_commit refusal."""
import contextlib
import hashlib
import io
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
NOTE = ("NOTE: this run was started by another review-loop version (the bundled gate prompt differs); "
        "the run continues with this version's gate prompt")
LIMIT = 'rejected-tree; post-DONE rejection limit reached'
BLOCKING_TASK = 'Fix every delivered blocking finding, rerun relevant checks, and summarize the result in body.'
ADVISORY_TASK = ('The delivered review left only non-blocking rows open; they do not block approval. Fix the ones worth '
                 'fixing now and rerun relevant checks; in body, say which rows you fixed and which you left and why.')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class OtherVersionRunTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in twl._HELPERS})

    def command(self, *extra):
        return twl.trc.RealCoordinatorTests.command(self, *extra)

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

    def current(self):
        return '<bundled-default>:' + hashlib.sha256(rc.DEFAULT_GATE_PROMPT.read_bytes()).hexdigest()

    def assert_refrozen(self, result, before_config, intent=False):
        """One NOTE (on stderr for --intent-only, whose stdout stays one JSON document), the bundled prompt of this version in
        the config and the program record, one refreeze row, nothing else."""
        noted = result.stderr if intent else result.stdout
        self.assertEqual(noted.count(NOTE), 1, result.stdout + result.stderr)
        if intent:
            self.assertNotIn(NOTE, result.stdout)
            json.loads(result.stdout)                                          # the complete stdout parses
        self.assertNotIn('permission-probe', result.stdout + result.stderr)
        self.assertNotIn('operator program or PATH changed', result.stdout + result.stderr)   # the program record moved with the config
        state = self.state()
        self.assertEqual(state['config']['gate_prompt'], self.current())
        self.assertEqual(state['operator_programs']['gate_prompt']['sha256'], self.current().split(':', 1)[1])
        [row] = state['gate_prompt_refreezes']
        self.assertEqual((row['from'], row['to']), (before_config['gate_prompt'], self.current()))
        self.assertEqual({k: v for k, v in state['config'].items() if k != 'gate_prompt'},
                         {k: v for k, v in before_config.items() if k != 'gate_prompt'})
        if manifest := state.get('role_dispatch_manifest'):
            self.assertEqual(manifest['gate_prompt_sha256'], hashlib.sha256(rc.DEFAULT_GATE_PROMPT.read_bytes()).hexdigest())
            self.assertEqual(manifest['config_sha256'], digest(state['config']))
            self.assertEqual(state['role_dispatch_manifest_sha256'], digest(manifest))

    def test_reject_at_done_continues_with_this_versions_gate_prompt(self):
        for mode in (OFF, ON):   # the stop-after-gate route and the default full lifecycle
            with self.subTest(mode=mode[1]):
                self.run_dir = self.root / ('reject-' + mode[1])
                self.done(*mode)
                before = self.state()
                manifest = before.get('role_dispatch_manifest')
                issued = self.action('reject', *mode, '--text', 'Please handle an edge case.', '--intent-only')
                self.assertEqual(issued.returncode, 0, issued.stdout + issued.stderr)
                self.assert_refrozen(issued, before['config'], intent=True)          # the first step re-freezes, once
                rejected = self.action('reject', *mode, '--text', 'Please handle an edge case.',
                                       '--expect', json.loads(issued.stdout)['digest'])
                self.assertEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
                self.assertIn(twl.DONE, rejected.stdout)
                self.assertNotIn(NOTE, rejected.stdout)
                self.assertEqual(len(self.state()['rejections']), 1)
                if manifest:   # only the gate-prompt fields, the config digest and the manifest digest moved
                    after = self.state()['role_dispatch_manifest']
                    changed = {k for k in set(manifest) | set(after) if manifest.get(k) != after.get(k)}
                    self.assertEqual(changed, {'gate_prompt_sha256', 'config_sha256'} | (
                        {'gate_prompt_path'} if manifest['gate_prompt_path'] != after['gate_prompt_path'] else set()))
                accepted = self.run_operator_action('accept', *mode)
                self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
                self.assertEqual(self.state()['status'], 'ACCEPTED')

    def test_resume_at_a_hold_continues_with_this_versions_gate_prompt(self):
        for mode in (OFF, ON):
            for extra in ((), ('--add-rounds', '1')):
                with self.subTest(mode=mode[1], extra=extra):
                    self.run_dir = self.root / ('held-' + mode[1] + str(len(extra)))
                    self.held(*mode)
                    before = self.state()['config']
                    resumed = self.run_operator_action('resume', *mode)
                    self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
                    self.assert_refrozen(resumed, before)
                    again = self.run_operator_action('resume', *mode)   # recorded once: no second NOTE
                    self.assertNotIn(NOTE, again.stdout)
                    self.assertEqual(len(self.state()['gate_prompt_refreezes']), 1)

    def test_abort_records_no_refreeze(self):
        self.held(*OFF)
        aborted = self.run_operator_action('abort', *OFF)
        self.assertEqual(aborted.returncode, 2, aborted.stdout + aborted.stderr)
        self.assertIn('HOLD: aborted by operator', aborted.stdout)
        self.assertNotIn(NOTE, aborted.stdout)
        self.assertNotIn('gate_prompt_refreezes', self.state())

    def test_a_lifecycle_manifest_that_does_not_bind_the_old_prompt_is_not_rewritten(self):
        self.held(*ON)
        state = self.state()
        state['role_dispatch_manifest']['gate_prompt_sha256'] = '0' * 64   # not the old bundled prompt: left alone
        state['role_dispatch_manifest_sha256'] = digest(state['role_dispatch_manifest'])
        (self.run_dir / 'state.json').write_text(json.dumps(state))
        resumed = self.run_operator_action('resume', *ON)
        self.assertEqual(resumed.returncode, 2, resumed.stdout + resumed.stderr)
        self.assertIn('frozen role dispatch changed', resumed.stdout)
        self.assertEqual(self.state()['role_dispatch_manifest']['gate_prompt_sha256'], '0' * 64)

    def test_a_lifecycle_manifest_with_another_difference_is_still_refused(self):
        self.held(*ON)
        state = self.state()
        state['role_dispatch_manifest']['role_flags']['author']['model'] = 'another-model'
        state['role_dispatch_manifest']['role_flags_sha256'] = digest(state['role_dispatch_manifest']['role_flags'])
        state['role_dispatch_manifest_sha256'] = digest(state['role_dispatch_manifest'])
        (self.run_dir / 'state.json').write_text(json.dumps(state))
        resumed = self.run_operator_action('resume', *ON)
        self.assertEqual(resumed.returncode, 2, resumed.stdout + resumed.stderr)
        self.assertIn('frozen role dispatch changed; abort or start a new run', resumed.stdout)
        self.assertEqual(self.state()['role_dispatch_manifest']['role_flags']['author']['model'], 'another-model')

    def test_rejected_tree_hold_texts_are_the_ordinary_ones(self):
        self.done(*OFF)
        for kind, text in ((None, 'rejected-tree; note, change the workspace and resume, note --scope-change, or abort'),
                           ('rejection_limit', LIMIT + '; note and resume, reject --scope-change, or abort')):
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

    def test_withdraw_findings_at_a_round_limit_hold_works_after_the_refreeze(self):
        for mode in (OFF, ON):
            with self.subTest(mode=mode[1]):
                self.run_dir = self.root / ('withdraw-' + mode[1])
                held = self.run_coordinator(*mode, '--max-exec-rounds', '1', env={'FAKE_EXEC_MIXED_REVISE': '1'})
                self.assertIn('HOLD: EXEC round limit reached', held.stdout, held.stdout + held.stderr)
                self.from_other_version()
                before = self.state()['config']
                [major] = [row['id'] for row in self.state()['finding_ledger']
                           if row['status'] == 'open' and row['severity'] == 'MAJOR']
                withdraw = ('--max-exec-rounds', '1', '--finding', major, '--reason', 'Reading the first element is approved.')
                issued = self.action('withdraw-findings', *mode, *withdraw, '--intent-only')
                self.assertEqual(issued.returncode, 0, issued.stdout + issued.stderr)
                self.assert_refrozen(issued, before, intent=True)
                done = self.action('withdraw-findings', *mode, *withdraw, '--expect', json.loads(issued.stdout)['digest'])
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertTrue(self.state()['operator_rulings'])

    def test_a_second_difference_is_refused_before_anything_is_recorded(self):
        self.held(*OFF)
        before = self.files()
        for flag, value, key in (('--max-exec-rounds', '9', 'max_exec_rounds'), ('--reviewer-command', 'python3 --version', 'reviewer_command')):
            for action in ('resume', 'abort'):
                with self.subTest(key=key, action=action):
                    refused = self.run_operator_action(action, *OFF, flag, value)
                    self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                    self.assertEqual([line for line in refused.stdout.splitlines() if line.startswith('REFUSED')],
                                     ['REFUSED: resume configuration differs: ' + key])
                    self.assertNotIn(NOTE, refused.stdout)
                    self.assertEqual(self.files(), before)

    def test_note_scope_change_at_a_hold_still_works(self):
        self.held(*OFF)
        changed = self.run_operator_action('note', *OFF, '--scope-change', '--text', 'New scope: include floats.')
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        self.assertEqual(self.state()['status'], 'ABORTED')

    def test_a_user_gate_prompt_file_keeps_its_rules(self):
        prompt, moved = self.root / 'gate.txt', self.root / 'moved-gate.txt'
        template = rc.DEFAULT_GATE_PROMPT.read_text()
        prompt.write_text(template); moved.write_text(template)
        own = (*OFF, '--gate-prompt', str(prompt))
        result = self.run_coordinator(*own, '--stop-after-plan')
        self.assertIn('HOLD: ' + rc.PLAN_STOP_REASON, result.stdout, result.stdout + result.stderr)
        self.assertEqual(self.state()['config']['gate_prompt'], str(prompt.resolve()))
        refused = self.run_operator_action('resume', *OFF, '--gate-prompt', str(moved))   # another file: still refused
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual([line for line in refused.stdout.splitlines() if line.startswith('REFUSED')], [PREFIX])
        prompt.write_text(template + '\nA changed line.\n')                               # the same file, changed: re-frozen
        resumed = self.run_operator_action('resume', *own)
        self.assertIn(twl.DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        self.assertEqual(resumed.stdout.count('NOTE: operator program or PATH changed since run start (changed: gate_prompt); '
                                              'the run continues with the new state'), 1, resumed.stdout)
        self.assertNotIn(NOTE, resumed.stdout)
        self.assertNotIn('gate_prompt_refreezes', self.state())

    def test_the_program_record_of_such_a_run_is_refrozen_with_the_rest(self):
        self.done(*OFF)
        command = self.command(*OFF); command[2] = 'reject'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            co = rc.Coordinator(rc.parser().parse_args([*command[2:], '--text', 'x']))
            self.assertIsNone(co._program_state()[1])
        self.assertEqual(out.getvalue().count(NOTE), 1, out.getvalue())
        self.assertNotIn('operator program or PATH changed', out.getvalue())


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
