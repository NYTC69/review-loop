"""V317-B3: every author-side turn that can change the tree after an operator ruling is told about the ruling and asked
not to reintroduce the change it withdrew; a run without a ruling builds every prompt byte for byte as before."""
import json
import unittest

from paired_session import test_d09_c1b2 as writers
from paired_session import test_v317b as v317b
from paired_session import test_v317g1 as g1
from paired_session import test_worktree_lifecycle as twl
from paired_session import worktree_lifecycle as wl

rc, DONE = twl.rc, twl.DONE
REASON = 'Every caller passes an already sorted sequence; reading the first and last element is the approved behavior.'
NOT_AGAIN = 'Do not reintroduce the change these findings asked for.'
KEEP_RULING = ('When an open finding cannot be fixed without going against one of these decisions, keep the decision, '
               'leave that finding as it is, and say so in body.')
MAJOR_SUMMARY = 'major behavior issue'   # FAKE_EXEC_MIXED_REVISE: one MAJOR and one MINOR row in the first EXEC review


class AuthorRulingTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in twl._HELPERS})
    locals().update({name: getattr(v317b.WithdrawalTests, name) for name in ('state', 'cli_withdraw', 'row', 'ready')})

    def prompts(self, after, phase):
        """The dispatched author prompts of one phase after sequence `after`, as the evidence recorded them."""
        turns = [t for t in self.state()['turns'] if t['sequence'] > after and t['role'] == 'author' and t['phase'] == phase]
        return [(self.run_dir / 'evidence' / f"{t['sequence']:03d}-{phase.lower()}-author.prompt.txt").read_text()
                for t in turns]

    def held_with_a_ruling(self, *flags):
        """The field sequence: a round-limit HOLD with a MAJOR and a MINOR row open, then the MAJOR withdrawn."""
        writers.D09C1b2Tests.change(self)   # an uncommitted change under review, large enough for the quality writers
        held = self.run_coordinator(*flags, env={'FAKE_EXEC_MIXED_REVISE': '1'})
        self.assertIn('HOLD: EXEC round limit reached', held.stdout, held.stdout + held.stderr)
        state = self.state()
        [major] = [row for row in state['finding_ledger'] if row['status'] == 'open' and row['severity'] == 'MAJOR']
        self.assertTrue(any(row['status'] == 'open' and row['severity'] == 'MINOR' for row in state['finding_ledger']))
        self.cli_withdraw([major['id']], *flags, reason=REASON)
        return major, state['sequence']

    def assert_notice(self, prompt, major):
        self.assertIn('## Operator withdrawals', prompt)
        self.assertIn(f"- O001: {REASON}", prompt)
        self.assertIn(f"  - {major['id']}: {major['summary']}", prompt)
        self.assertIn(NOT_AGAIN, prompt)
        self.assertIn(KEEP_RULING, prompt)

    def field(self, route):
        """Withdraw the MAJOR at the round-limit HOLD, then `resume --add-rounds 1`: every later author-side prompt
        carries the ruling; the fresh shadow and gate keep the decision without ids."""
        flags = ('--review-only', '--lifecycle-mode', route, '--max-exec-rounds', '1', '--max-invocations', '100')
        major, before = self.held_with_a_ruling(*flags)
        self.assertEqual(major['summary'], MAJOR_SUMMARY)
        resumed = self.run_operator_action('resume', *flags, '--add-rounds', '1')
        self.assertIn(DONE, resumed.stdout, resumed.stdout + resumed.stderr)
        exec_prompts = self.prompts(before, 'EXEC')
        self.assertTrue(exec_prompts)
        self.assertIn('"source": "round-limit-extension"', exec_prompts[0])   # the round-2 turn of the field run
        self.assertNotIn(f'"id": "{major["id"]}"', exec_prompts[0])           # the withdrawn row is not delivered
        for prompt in exec_prompts:
            self.assert_notice(prompt, major)
        for role in ('shadow', 'gate'):
            turns = [t for t in self.state()['turns'] if t['sequence'] > before and t['role'] == role]
            self.assertTrue(turns, role)
            for turn in turns:
                text = (self.run_dir / 'evidence' / f"{turn['sequence']:03d}-{turn['phase'].lower()}-{role}.prompt.txt").read_text()
                self.assertIn('operator decision: ' + REASON, text)
                for value in (major['id'], 'O001', 'Operator withdrawals', NOT_AGAIN):
                    self.assertNotIn(value, text)
        return major, before

    def test_field_sequence_stop_after_gate_route(self):
        self.field('off')

    def test_field_sequence_full_lifecycle_also_tells_finish_quality_writers_and_docs(self):
        major, before = self.field('on')
        for phase in ('FINISH', 'POLISH-Q', 'DOCS'):
            prompts = self.prompts(before, phase)
            self.assertTrue(prompts, phase)
            for prompt in prompts:
                self.assert_notice(prompt, major)

    def test_shadow_and_gate_prompts_with_a_ruling_pass_the_fresh_check_without_ids(self):
        co, row = self.ready('on')
        co.args.reason = REASON
        co.args.expect = co.operator_intent('withdraw-findings', None, None)['digest']
        co.withdraw_findings()
        co.state['phase'] = 'EXEC'
        co.materialize_review_context()
        snapshot = rc.git_snapshot(self.workspace)[0]
        for role, prompt in (('shadow', co._review_prompt('shadow', snapshot)), ('gate', co._gate_prompt(snapshot))):
            with self.subTest(role=role):
                co.assert_fresh_prompt(role, prompt)
                self.assertIn('operator decision: ' + REASON, prompt)
                for value in (row['id'], 'O001', NOT_AGAIN, row['summary']):
                    self.assertNotIn(value, prompt)
        reviewer = co._review_prompt('reviewer', snapshot)   # the persistent reviewer's notice is unchanged
        self.assertIn('\n## Operator withdrawals\n' + f"- O001: {row['id']}: {REASON}" +
                      '\nNo disposition is due for these withdrawn ids. Do not raise the same objection again as a new finding.\n',
                      reviewer)
        self.assertNotIn(NOT_AGAIN, reviewer)

    def test_without_a_ruling_the_exec_author_prompt_is_unchanged(self):
        co = g1.TextTests.exec_run(self)
        co.state['phase'] = 'EXEC'
        co.state['delivered_review'] = delivered = json.dumps({'source': 'persistent-reviewer', 'status': 'REVISE',
                                                              'findings': []})
        self.assertEqual(co._author_prompt(), '\n'.join([   # the whole prompt as the base code builds it
            'Role: persistent codex implementer. Phase: EXEC.',
            f'Workspace: {co.workspace}', f'Work item: {co.context / "workitem.md"}', g1.BLOCKING_TASK,
            co.author_control_contract(), 'Delivered review:\n' + delivered, rc.AUTHOR_HISTORY_RULE,
            'Do not commit or push. Do not load review-loop skills. Do not edit outside the workspace.',
            'For long commands, use the longest single wait your tool permits. Do not wait for another model.',
            'Return only JSON matching the supplied schema. READY means this turn is complete; HOLD means blocked.']))
        self.assertEqual(co.operator_rulings_prompt('author'), '')

    def test_without_a_ruling_finish_and_writer_prompts_are_unchanged(self):
        writers.D09C1b2Tests.change(self)
        done = self.run_coordinator(*writers.RUN)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        self.assertNotIn('operator_rulings', state)
        reserved = 'Reserved for the DOCS stage; do not edit: CHANGELOG.md.'
        [finish] = self.prompts(0, 'FINISH')
        plan = (self.run_dir / 'context' / 'plan.md').read_text()
        # V318-CAP: FINISH/DOCS use the default two-hour author dispatch budget, less V319-RULE's 600 s answer margin.
        author_rule = rc.LONG_COMMAND_RULE.replace('gets 3600000', 'gets 6600000')
        self.assertEqual(finish, wl.finish_prompt(plan, 'python3 -m unittest', reserved + ' ' + author_rule, True))
        quality = self.prompts(0, 'POLISH-Q')
        self.assertTrue(quality)
        body = wl.agent_body((rc.HERE.parent / 'agents' / 'code-simplifier.md').read_bytes())
        for prompt in quality:
            simplifier = prompt.startswith('Role: simplifier,')
            marker = 'preserving its behavior: ' if simplifier else 'Consolidate the changed test files only: '
            paths = prompt.split(marker, 1)[1].split('. ', 1)[0].split(', ')   # the run's changed paths, as dispatched
            self.assertEqual(prompt, wl.writer_prompt('simplifier' if simplifier else 'test-writer', body if simplifier else '',
                                                      paths, 'python3 -m unittest', reserved) + '\n' + rc.LONG_COMMAND_RULE)
        [docs] = self.prompts(0, 'DOCS')
        config = state['config']
        allow = sorted(rc.Path(path).relative_to(self.workspace.resolve()).as_posix() for path in config['docs_allowlist'])
        self.assertEqual(docs, wl.docs_prompt(config.get('docs_file'), allow, self.run_dir.name,
                                              self.workitem.read_text()) + rc.LONG_COMMAND_RULE_HEADING + author_rule)


if __name__ == '__main__': unittest.main()
