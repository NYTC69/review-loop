"""D09 C1-a (paired_session/docs/d09-cap1-writer-passes.md §3-§5): the quality_writers key and its per-entry defaults,
the per-item marker and its successor copy, the skip rules with their reasons, the headroom formula and the report
lines. No writer leg exists yet (C1-b1): a writer that passes every rule is not reached, and nothing writes."""
import json
from pathlib import Path
import unittest

from paired_session import test_worktree_lifecycle as twl
from paired_session import worktree_lifecycle as wl

rc, DONE = twl.rc, twl.DONE
CODE = ''.join(f'VALUE_{n} = {n}\n' for n in range(30))   # 30 code lines


class D09C1aTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'args', 'git')})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def created(self, *extra, name='run'):   # a new W run (main pipeline unless --review-only), its profile applied
        self.run_dir = self.root / name
        argv = self.command('--lifecycle-mode', 'on', *extra)[2:]
        return rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv))

    def tail(self, co, paths=None, specialists=3):   # the clean POLISH-Q exit, on the tree as it is; nothing may write
        index = lambda: (self.workspace / '.git' / 'index').read_bytes()
        before = (rc.git_snapshot(self.workspace)[0], index(), co.state['exec_rounds'], co.state['invocations_used'],
                  co.state['lifecycle']['stage'], co.state['lifecycle']['epoch'], co.state['next'])
        co._quality_writers_tail(co._changed_paths() if paths is None else paths, specialists,
                                 {'candidate_oid': before[0], 'request_id': 'w-POLISH-Q-0-0'})
        self.assertEqual((rc.git_snapshot(self.workspace)[0], index(), co.state['exec_rounds'], co.state['invocations_used'],
                          co.state['lifecycle']['stage'], co.state['lifecycle']['epoch'], co.state['next']), before)
        return {name: row['state'] for name, row in co.state['lifecycle']['quality_writers'].items() if name != 'base_oid'}

    # --- test 14: the key ---------------------------------------------------------------------------------------------------
    def test_the_key_defaults_by_entry_and_is_frozen(self):
        self.assertEqual(self.created(name='main').state['config']['quality_writers'], 'off')
        (self.workspace / 'change.py').write_text(CODE)
        self.assertEqual(self.created('--review-only', name='review-only').state['config']['quality_writers'], 'both')
        self.assertEqual(self.created('--quality-writers', 'tests', name='explicit').state['config']['quality_writers'], 'tests')
        profile = self.workspace / '.review-loop' / 'paired-session.json'   # a workspace profile may set it (a cost switch)
        profile.parent.mkdir()
        profile.write_text(json.dumps({'quality_writers': 'simplify'}))
        co = self.created(name='workspace-profile')
        self.assertEqual((co.state['config']['quality_writers'], co.state.get('workspace_lifecycle_keys')), ('simplify', None))
        profile.unlink()
        self.run_dir = self.root / 'explicit'
        self.assertEqual(rc.Coordinator(self.args(action='resume')).state['config']['quality_writers'], 'tests')   # kept
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: quality_writers'):
            rc.Coordinator(self.args('--quality-writers', 'off', action='resume'))
        self.created(name='saved-before-the-key')   # a run saved before C1-a has no quality_writers: it is off
        saved = self.state()
        del saved['config']['quality_writers']
        (self.run_dir / 'state.json').write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, 'resume configuration differs: quality_writers'):
            rc.Coordinator(self.args('--quality-writers', 'both', action='resume'))
        for extra in ((), ('--quality-writers', 'off')):
            self.assertEqual(rc.Coordinator(self.args(*extra, action='resume')).args.quality_writers, 'off')
        self.assertIs(self.created(name='explicit-test').state['test_command_explicit'], True)   # the harness passes one
        argv = [arg for arg in self.command('--lifecycle-mode', 'on')[2:] if arg not in ('--test-command', 'python3 -m unittest')]
        argv[argv.index('--run-dir') + 1] = str(self.root / 'default-test')
        self.assertIs(rc.Coordinator(rc.parser().parse_args(argv)).state['test_command_explicit'], False)

    def test_a_successor_of_a_run_saved_before_the_key_stays_off(self):   # a review-only parent would otherwise give both
        (self.workspace / 'change.py').write_text(CODE)
        done = self.run_coordinator('--review-only')
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        saved = self.state()
        del saved['config']['quality_writers']
        (self.run_dir / 'state.json').write_text(json.dumps(saved))
        parent_dir = self.run_dir
        parent = rc.Coordinator(rc.parser().parse_args(self.command('--review-only')[2:]))
        parent.args.action = 'reject'
        start = parent.scope_change('Also reject floats.', None).split('Start: ', 1)[1].split()
        option = lambda name: start[start.index(name) + 1]
        self.assertEqual(json.loads(Path(option('--config')).read_text())['quality_writers'], 'off')
        self.run_dir, self.workitem = Path(option('--run-dir')), Path(option('--workitem'))
        argv = self.command('--config', option('--config'), '--supersedes', str(parent_dir))[2:]
        child = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv)).state
        self.assertEqual((child['config']['review_only'], child['config']['quality_writers']), (True, 'off'))

    def test_the_report_hint_and_writer_lines(self):
        state = {'config': {'quality_writers': 'off'}, 'lifecycle': {}}
        self.assertEqual(wl.writer_report_lines(state), ['- 质量 writer：未开启；' + wl.WRITER_HINT])
        self.assertIn('`--quality-writers both` enables the simplifier and test consolidation (about +10 invocations)',
                      wl.WRITER_HINT)
        state = {'config': {'quality_writers': 'both'}, 'lifecycle': {'quality_writers': {
            'base_oid': 'x', 'simplifier': {'state': 'skipped:budget', 'detail': '需要 14 次调用'}}}}
        self.assertEqual(wl.writer_report_lines(state), ['- 质量 writer simplifier：已跳过（budget，需要 14 次调用）',
                                                         '- 质量 writer test-writer：未执行'])

    # --- test 9: the skip rules --------------------------------------------------------------------------------------------
    def test_the_skip_rules_record_their_reasons(self):
        both = ('--quality-writers', 'both', '--max-invocations', '60')
        (self.workspace / 'small.py').write_text('A = 1\n' * 10)
        self.assertEqual(self.tail(self.created(*both, name='small')), {'simplifier': 'skipped:small', 'test-writer': 'skipped:no-test-file'})
        (self.workspace / 'new_code.py').write_text(CODE)   # a new untracked 30-line file: not small
        (self.workspace / 'tests').mkdir()
        (self.workspace / 'tests' / 'test_new_code.py').write_text('import new_code\n')
        co = self.created(*both, name='eligible')
        self.assertEqual(self.tail(co), {})   # both pass every rule: not reached, no state, nothing written
        self.assertEqual(rc.git_snapshot(self.workspace)[0], co.state['lifecycle']['quality_writers']['base_oid'])
        self.assertEqual(self.tail(self.created('--quality-writers', 'simplify', '--max-invocations', '60', name='simplify')),
                         {'test-writer': 'skipped:off'})
        self.assertEqual(self.tail(self.created('--quality-writers', 'off', name='off')),
                         {'simplifier': 'skipped:off', 'test-writer': 'skipped:off'})
        self.assertEqual(self.tail(self.created(*both, '--skip-quality-polish', 'true', name='skip-polish')),
                         {'simplifier': 'skipped:skip-quality-polish', 'test-writer': 'skipped:skip-quality-polish'})
        co = self.created(*both, name='default-test-command')
        co.state['test_command_explicit'] = False
        self.assertEqual(self.tail(co), {'simplifier': 'skipped:no-test-command', 'test-writer': 'skipped:no-test-command'})
        (self.workspace / 'new_code.py').unlink()
        (self.workspace / 'README.md').write_text('docs\n' * 40)   # docs and tests only: small
        self.assertEqual(self.tail(self.created(*both, name='docs-tests')), {'simplifier': 'skipped:small'})
        for path, test in (('pkg/x_test.go', True), ('web/a.spec.ts', True), ('src/__tests__/a.js', True),
                           ('src/contest.py', False), ('docs/guide.md', False)):
            self.assertIs(wl.is_test_path(path), test, path)
        self.assertEqual([wl.is_code_path(p) for p in ('src/a.py', 'config.yaml', 'docs/a.py', '.env', 'tests/t.py')],
                         [True, False, False, False, False])

    # --- test 10: the headroom formula -------------------------------------------------------------------------------------
    def test_no_headroom_skips_with_the_number(self):
        (self.workspace / 'new_code.py').write_text(CODE)
        (self.workspace / 'tests').mkdir()
        (self.workspace / 'tests' / 'test_new_code.py').write_text('import new_code\n')
        co = self.created('--quality-writers', 'both', '--max-invocations', '60', name='budget')
        co.state['invocations_used'] = 60 - co.state.get('q_reserved', 0) - (2 + 4 + 3) + 1   # one short of W + R + S
        self.assertEqual(self.tail(co), {'simplifier': 'skipped:budget', 'test-writer': 'skipped:budget'})
        self.assertIn('需要 9 次调用，剩余 8 次', co.state['lifecycle']['quality_writers']['simplifier']['detail'])
        co = self.created('--quality-writers', 'both', '--max-invocations', '60', name='rounds')
        co.state['exec_rounds'] = co.exec_round_limit() - 1   # the replay round fits, the fix round does not
        self.assertEqual(self.tail(co), {'simplifier': 'skipped:budget', 'test-writer': 'skipped:budget'})
        cap = None
        for used, expected in ((0, {}), (1, {'simplifier': 'skipped:budget', 'test-writer': 'skipped:budget'})):
            co = self.created('--quality-writers', 'both', '--max-invocations', '60', name=f'polish-edge-{used}')
            cap = co._worktree_run_cap('POLISH-Q')
            co.state['lifecycle']['polish_calls'] = cap - (2 + 3 + 1) + used   # = cap at the edge runs; one more skips
            self.assertEqual(self.tail(co), expected)
        self.assertEqual(cap, 32)

    def test_a_specialist_blocker_is_fixed_before_the_writers_are_checked(self):   # test 9: polish-fix first
        env = {'FAKE_SPECIALIST_BLOCK': 'code-reviewer', 'FAKE_SPECIALIST_BLOCK_ONCE': str(self.root / 'block-once')}
        done = self.run_coordinator('--lifecycle-mode', 'on', '--quality-writers', 'both', '--max-invocations', '60', env=env)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = self.state()
        held, clean = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'POLISH-Q']
        self.assertEqual((held['status'], clean['status']), ('HOLD', 'READY'))
        marker = state['lifecycle']['quality_writers']
        self.assertEqual((marker['base_oid'], marker['simplifier']['receipt'], marker['test-writer']['receipt']),
                         (clean['candidate_oid'], clean['request_id'], clean['request_id']))   # only the clean exit

    # --- tests 3 and 13: the real W path, a DONE reject and a scope-change successor ------------------------------------------
    def test_the_marker_survives_a_reject_and_a_successor_and_nothing_writes(self):
        both = ('--lifecycle-mode', 'on', '--quality-writers', 'both', '--max-invocations', '60')
        done = self.run_coordinator(*both)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        first = self.state()
        marker = first['lifecycle']['quality_writers']
        self.assertEqual((marker['simplifier']['state'], marker['test-writer']['state']), ('skipped:small', 'skipped:no-test-file'))
        self.assertEqual([row['stage'] for row in first['lifecycle']['receipts']], ['FINISH', 'POLISH-Q', 'DOCS', 'SECURITY'])
        self.assertIn('quality-writer · simplifier · skipped:small', done.stdout)
        records = lambda: sorted((self.run_dir / 'evidence').glob('*-polish-q-simplifier.json')) + sorted(
            (self.run_dir / 'evidence').glob('*-polish-q-test-writer.json'))
        self.assertEqual([json.loads(p.read_text())['state'] for p in records()], ['skipped:small', 'skipped:no-test-file'])
        self.assertEqual([str(p) for p in records()], [marker['simplifier']['evidence'], marker['test-writer']['evidence']])
        rejected = self.run_operator_action('reject', *both, '--text', 'Also reject floats.')
        self.assertIn(DONE, rejected.stdout, rejected.stdout + rejected.stderr)
        again = self.state()
        self.assertEqual(again['lifecycle']['quality_writers'], marker)   # skipped once per item, not re-evaluated
        self.assertEqual(len(records()), 2)   # no second record
        self.assertEqual(again['lifecycle']['epoch'], first['lifecycle']['epoch'] + 1)   # the reject's epoch, none from a writer
        resumed = rc.Coordinator(self.args(*both[2:], action='resume'))   # a resume keeps the marker; a later exit adds nothing
        self.assertEqual(resumed.state['lifecycle']['quality_writers'], marker)
        self.assertEqual(self.tail(resumed), {'simplifier': 'skipped:small', 'test-writer': 'skipped:no-test-file'})
        self.assertEqual((resumed.state['lifecycle']['quality_writers'], len(records())), (marker, 2))
        parent_dir = self.run_dir
        parent = rc.Coordinator(self.args(*both[2:], action='reject'))
        start = parent.scope_change('Also reject strings.', None).split('Start: ', 1)[1].split()
        option = lambda name: start[start.index(name) + 1]
        self.assertEqual(json.loads((parent_dir / 'evidence' / 'successor-spec.json').read_text())['quality_writers'], marker)
        self.run_dir, self.workitem = Path(option('--run-dir')), Path(option('--workitem'))
        argv = self.command('--lifecycle-mode', 'on', '--config', option('--config'), '--supersedes', str(parent_dir))[2:]
        child = rc.Coordinator(rc.configure_parser(rc.parser(), argv).parse_args(argv)).state
        self.assertEqual((child['lifecycle']['quality_writers'], child['config']['quality_writers'], child['test_command_explicit']),
                         (marker, 'both', True))


if __name__ == '__main__':
    unittest.main()
