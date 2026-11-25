"""WI-POLBUDGET: resume a delivery run held at the POLISH-Q owner budget."""
import hashlib
import json
import os
import unittest
from unittest import mock

from paired_session import test_worktree_lifecycle as twl

rc = twl.rc
RUN = ('--lifecycle-mode', 'on', '--max-invocations', '80')
OWNER = 'code-reviewer'


class PolishBudgetRecoveryTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in twl._HELPERS})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def save_state(self, state):
        (self.run_dir / 'state.json').write_text(json.dumps(state))

    def resume(self):
        return self.run_operator_action('resume', *RUN, env=self.fake_env)

    def hold_at_budget(self, owners=OWNER):
        self.fake_env = {'FAKE_POLISH_BUDGET_OWNERS': owners}
        result = self.run_coordinator(*RUN, env=self.fake_env)
        self.assertIn('HOLD: POLISH-Q blockers remain after the fix:', result.stdout, result.stdout + result.stderr)
        result = self.resume()
        self.assertIn('HOLD: POLISH-Q blockers remain after the fix:', result.stdout, result.stdout + result.stderr)
        result = self.resume()
        reason = 'POLISH-Q specialist budget exhausted: ' + OWNER
        self.assertIn('HOLD: ' + reason, result.stdout, result.stdout + result.stderr)
        state = self.state()
        life = state['lifecycle']
        rows = [row for row in state['finding_ledger'] if row.get('security') and row['status'] == 'open']
        self.assertEqual({row['owner_role'] for row in rows}, {'specialist:' + name for name in owners.split(',')})
        self.assertTrue(all(row['severity'] == 'MAJOR' for row in rows))
        self.assertEqual((state['status'], state['hold_reason'], life['stage'], state['next']),
                         ('HOLD', reason, 'POLISH-Q', 'polish-fix'))
        self.assertEqual(life['fix_base'], rc.git_snapshot(self.workspace)[0])
        self.assertEqual({name: life['specialist_counts'][name] for name in owners.split(',')},
                         dict.fromkeys(owners.split(','), 3))
        return state

    def edit(self, comment='# operator fix'):
        path = self.workspace / 'sum_ints.py'
        path.write_text(path.read_text() + comment + '\n')
        return rc.git_snapshot(self.workspace)[0]

    def attach(self, tree):
        log = self.root / 'host.log'
        log.write_text('Ran 12 tests\nOK\n')
        result = self.run_operator_action('attach-verification', *RUN, '--command', 'python3 -m unittest',
                                          '--exit-code', '0', '--log', str(log),
                                          '--log-sha256', hashlib.sha256(log.read_bytes()).hexdigest(),
                                          '--note', 'ran on the host')
        self.assertIn(f'VERIFICATION: V001 bound to tree {tree}', result.stdout, result.stdout + result.stderr)

    def assert_no_dispatch(self, before, reason):
        result = self.resume()
        self.assertIn('HOLD: ' + reason, result.stdout, result.stdout + result.stderr)
        after = self.state()
        self.assertEqual((after['status'], after['hold_reason'], after['next']), ('HOLD', reason, 'polish-fix'))
        for key in ('sequence', 'invocations_used', 'turns'):
            self.assertEqual(after[key], before[key])
        return after

    def assert_security_withdrawal_refused(self):
        before = self.state()
        for row in before['finding_ledger']:
            if row.get('security') and row['status'] == 'open':
                result = self.run_operator_action('withdraw-findings', *RUN, '--finding', row['id'],
                                                  '--reason', 'Operator verified this behavior.', '--intent-only')
                self.assertIn('cannot be withdrawn', result.stdout + result.stderr)
        self.assertEqual(self.state()['finding_ledger'], before['finding_ledger'])

    def test_operator_fix_rechecks_owner_before_exec_replay(self):
        before = self.hold_at_budget()
        tree = self.edit()
        self.attach(tree)
        result = self.resume()
        after = self.state()
        turns = after['turns'][len(before['turns']):]
        self.assertEqual([(row['role'], row['phase']) for row in turns[:3]],
                         [('reviewer', 'POLISH-Q'), ('reviewer', 'EXEC'), ('shadow', 'EXEC')],
                         result.stdout + result.stderr)
        self.assertIn(twl.DONE, result.stdout, result.stdout + result.stderr)
        self.assertEqual(turns[0]['snapshot_before'], tree)
        self.assertEqual(len([row for row in after['turns'] if row['role'] == 'gate']), 1)
        self.assertEqual(after['exec_rounds'], before['exec_rounds'])
        self.assertIn(OWNER, result.stdout)
        self.assertEqual(after['lifecycle']['operator_fixes'], [
            {'tree': tree, 'base': before['lifecycle']['fix_base'], 'owners': [OWNER], 'sequence': before['sequence']}])
        fix = after['lifecycle']['polish_fixes'][-1]
        self.assertEqual((fix['tree'], fix['author_sequence'], fix['operator']), (tree, None, True))
        [recheck] = fix['recheck_turns']
        self.assertEqual((recheck['name'], recheck['sequence'], recheck['reviewed_snapshot']),
                         (OWNER, turns[0]['sequence'], tree))
        prompt = (self.run_dir / 'evidence' / f"{recheck['sequence']:03d}-polish-q-reviewer.prompt.txt").read_text()
        self.assertIn('V001', prompt)
        self.assertEqual([row['status'] for row in after['finding_ledger'] if row.get('security')], ['fixed'])

    def test_same_tree_with_or_without_verification_keeps_budget_hold(self):
        before = self.hold_at_budget()
        self.assert_security_withdrawal_refused()
        for attach in (False, True):
            with self.subTest(attach=attach):
                if attach:
                    self.attach(before['lifecycle']['fix_base'])
                after = self.assert_no_dispatch(before, before['hold_reason'])
                self.assertNotIn('operator_fixes', after['lifecycle'])

    def test_owner_still_open_holds_on_the_rechecked_tree(self):
        before = self.hold_at_budget()
        tree = self.edit('# incomplete operator edit')
        [finding] = [row for row in before['finding_ledger'] if row.get('security')]
        reason = 'POLISH-Q blockers remain after the fix: ' + finding['id']
        result = self.resume()
        self.assertIn('HOLD: ' + reason, result.stdout, result.stdout + result.stderr)
        after = self.state()
        life = after['lifecycle']
        self.assertEqual(life['fix_base'], tree)
        self.assertEqual(after['invocations_used'], before['invocations_used'] + 1)
        self.assertEqual(life['polish_calls'], before['lifecycle']['polish_calls'] + 1)
        self.assertEqual(life['specialist_counts'][OWNER], 1)
        self.assert_security_withdrawal_refused()
        resumed = self.assert_no_dispatch(after, reason)
        self.assertEqual(resumed['lifecycle']['operator_fixes'], life['operator_fixes'])

    def test_run_wide_allowance_cannot_fund_all_owner_rechecks(self):
        before = self.hold_at_budget(OWNER + ',python-reviewer')
        before['lifecycle']['polish_calls'] = rc.budget_policy.BUDGET_CAPS['POLISH-Q'][0] - 3
        self.save_state(before)
        self.edit()
        after = self.assert_no_dispatch(before, before['hold_reason'])
        self.assertNotIn('operator_fixes', after['lifecycle'])

    def test_operator_recheck_prechecks_the_invocation_limit(self):
        before = self.hold_at_budget()
        co = rc.Coordinator(rc.parser().parse_args(self.command(*RUN)[2:]))
        before['invocations_used'] = co.invocation_limit() - before.get('q_reserved', 0)
        self.save_state(before)
        tree = self.edit()
        reason = ('the POLISH-Q recheck needs at least 1 more invocations; '
                  'resume with a higher --max-invocations, or abort')
        after = self.assert_no_dispatch(before, reason)
        self.assertNotIn('operator_fixes', after['lifecycle'])
        result = self.run_operator_action('resume', '--lifecycle-mode', 'on', '--max-invocations', '120', env=self.fake_env)
        self.assertIn(twl.DONE, result.stdout, result.stdout + result.stderr)
        final = self.state()
        self.assertEqual(final['turns'][len(before['turns'])]['role'], 'reviewer')
        self.assertEqual(final['turns'][len(before['turns'])]['phase'], 'POLISH-Q')
        self.assertEqual(final['lifecycle']['operator_fixes'][0]['tree'], tree)

    def test_interrupted_recheck_reuses_saved_owner_progress(self):
        second = 'python-reviewer'
        before = self.hold_at_budget(OWNER + ',' + second)
        tree = self.edit()
        command = self.command(*RUN)
        command[2] = 'resume'
        co = rc.Coordinator(rc.parser().parse_args(command[2:]))
        specialist_turn = co._specialist_turn

        def interrupt(name, paths, snapshot):
            if name == second:
                raise RuntimeError('interrupted before the second owner dispatch')
            return specialist_turn(name, paths, snapshot)

        with mock.patch.dict(os.environ, self.fake_env), mock.patch.object(co, '_specialist_turn', side_effect=interrupt):
            self.assertEqual(co.resume(), 'HOLD')
        interrupted = self.state()
        life = interrupted['lifecycle']
        self.assertEqual(interrupted['next'], 'polish-recheck')
        self.assertEqual(life['recheck']['tree'], tree)
        self.assertEqual(list(life['recheck']['done']), [OWNER])
        self.assertEqual(interrupted['invocations_used'], before['invocations_used'] + 1)
        first_receipt = life['recheck']['done'][OWNER]
        result = self.resume()
        self.assertIn(twl.DONE, result.stdout, result.stdout + result.stderr)
        final_life = self.state()['lifecycle']
        self.assertEqual(len(life['operator_fixes']), 1)
        self.assertEqual(final_life['operator_fixes'], life['operator_fixes'])
        first_receipt_after, second_receipt = final_life['polish_fixes'][-1]['recheck_turns']
        self.assertEqual(first_receipt_after, first_receipt)
        self.assertEqual((second_receipt['name'], second_receipt['sequence']), (second, interrupted['sequence'] + 1))

    def test_legacy_state_resumes_in_place_with_frozen_role_dispatch(self):
        before = self.hold_at_budget()
        self.assertEqual(before['version'], 2)
        self.assertNotIn('operator_fixes', before['lifecycle'])
        before['config'].pop('auto_split_rounds', None)   # v3.15.6 predates automatic round splitting
        manifest = before['role_dispatch_manifest']

        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

        manifest['config_sha256'] = digest(before['config'])
        paths = {key: manifest[key] for key in ('agents_dir', 'gate_prompt_path')}
        for key, path in paths.items():
            manifest[key] = str(self.root / 'old-install' / os.path.basename(path))
        before['role_dispatch_manifest_sha256'] = digest(manifest)
        self.save_state(before)
        self.edit()
        result = self.resume()
        self.assertIn('rebound the frozen role dispatch paths', result.stdout, result.stdout + result.stderr)
        self.assertIn(twl.DONE, result.stdout, result.stdout + result.stderr)
        after = self.state()
        self.assertEqual((after['version'], after['base_commit']), (before['version'], before['base_commit']))
        self.assertEqual(after['role_dispatch_manifest'], {**manifest, **paths})
        self.assertEqual(after['turns'][:len(before['turns'])], before['turns'])
