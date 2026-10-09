"""Batch RF (v2.9.2 release fixes): cross-vendor probe skip, Claude plugin auto-update, APPROVE with open blocking findings, TOML-aware trust attribution."""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_operator_roles as tor
from paired_session import test_real_coordinator as trc

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'coordinator')


def trust(path):
    return '[projects.' + json.dumps(str(path)) + ']\ntrust_level = "trusted"\n'


class CrossVendorProbeSkipTests(unittest.TestCase):
    locals().update({name: getattr(tor.ProbeSkipTests, name) for name in ('setUp', 'co', 'cli', 'state', 'probe', 'entries')})
    SKIP = ('--accept-probe-skip', '--reason', 'checked by hand')

    def write_report(self, co, gate_probe):
        report = {'status': 'PASS', 'reviewer_flags_digest': co.reviewer_flags_digest(), 'author_flags_digest': co.author_flags_digest(),
                  'gate_flags_digest': co.gate_flags_digest(), 'gate_permission_probe': gate_probe,
                  'author_permission_probe': {'status': 'PASS', 'd1a_model_verdict': 'UNKNOWN', 'd1b_synthetic_verdict': 'PASS'},
                  'global_config_changes': {'status': 'PASS'}}
        (co.run_dir / 'permission-probe.json').write_text(json.dumps(report))

    def test_a_cross_vendor_skip_is_refused_without_a_bound_passing_gate_probe(self):
        refused = self.cli('run', '--gate-vendor', 'codex', *self.SKIP)
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn('REFUSED: gate vendor codex differs from reviewer vendor claude', refused.stdout)
        self.assertIn('permission-probe', refused.stdout)
        self.assertNotIn('probe_skip_override', self.state())

    def test_a_same_vendor_skip_is_unchanged(self):
        accepted = self.cli('run', *self.SKIP)
        self.assertEqual(accepted.returncode, 0, accepted.stdout)
        self.assertEqual(self.state()['probe_skip_override']['actor'], 'operator')

    def test_a_cross_vendor_run_with_a_passing_gate_probe_is_unaffected(self):
        co = self.co('--gate-vendor', 'codex')
        self.write_report(co, {'status': 'PASS'})
        accepted = self.cli('run', '--gate-vendor', 'codex', *self.SKIP)
        self.assertEqual(accepted.returncode, 0, accepted.stdout)
        self.assertIn('probe_skip_override', self.state())

    def test_a_saved_acceptance_and_a_gate_probe_for_other_flags_do_not_cover_a_cross_vendor_gate(self):
        co = self.co('--gate-vendor', 'codex')
        co.state['probe_skip_override'] = {'actor': 'operator', 'reason': 'x', 'reviewer_flags_digest': co.reviewer_flags_digest(),
                                           'author_flags_digest': co.author_flags_digest(), 'gate_flags_digest': co.gate_flags_digest()}
        with patch.object(rc.lifecycle_spine, 'fake_dispatch_guard', return_value=False):
            self.assertFalse(co._probe_skip_accepted())               # saved state alone, no probe
            self.write_report(co, {'status': 'PASS'})
            self.assertTrue(co._probe_skip_accepted())
            self.write_report(co, {'status': 'FAIL'})
            self.assertFalse(co._probe_skip_accepted())
            self.write_report(co, {'status': 'PASS'})
            report = json.loads((co.run_dir / 'permission-probe.json').read_text())
            report['gate_flags_digest'] = '0' * 64                    # a gate probe bound to other flags
            (co.run_dir / 'permission-probe.json').write_text(json.dumps(report))
            self.assertFalse(co._probe_skip_accepted())


class ClaudeAutoUpdateTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def test_the_child_env_disables_the_updater_and_unsets_plugin_force(self):
        with patch.dict(os.environ, {'FORCE_AUTOUPDATE_PLUGINS': '1', 'DISABLE_AUTOUPDATER': '0'}):
            env = rc.cli_env()
        self.assertEqual(env['DISABLE_AUTOUPDATER'], '1')
        self.assertNotIn('FORCE_AUTOUPDATE_PLUGINS', env)

    def test_claude_cli_version_runs_in_the_claude_child_env(self):   # G-b, RF follow-up (a)
        seen = []
        def fake(argv, **kwargs):
            seen.append((list(argv), kwargs.get('env')))
            return type('R', (), {'stdout': 'claude 1.0\n'})()
        with patch.dict(os.environ, {'FORCE_AUTOUPDATE_PLUGINS': '1'}), patch.object(rc.subprocess, 'run', fake):
            self.assertEqual(rc.claude_cli_version('claude'), 'claude 1.0')
        (argv, env), = seen
        self.assertEqual((argv, env['DISABLE_AUTOUPDATER']), (['claude', '--version'], '1'))
        self.assertNotIn('FORCE_AUTOUPDATE_PLUGINS', env)

    def test_the_claude_flags_digests_bind_the_child_env_and_codex_only_runs_do_not(self):
        use_lifecycle_on(self, self)
        co = self.coordinator('--author-vendor', 'claude', '--reviewer-vendor', 'claude', '--gate-vendor', 'claude')
        self.assertEqual(co.reviewer_flags()['claude_child_env'], rc.CLAUDE_CHILD_ENV)
        self.assertEqual(co.author_flags()['claude_child_env'], rc.CLAUDE_CHILD_ENV)
        digests = (co.author_flags_digest(), co.reviewer_flags_digest(), co.gate_flags_digest())
        with patch.object(rc, 'CLAUDE_CHILD_ENV', {}):
            self.assertTrue(all(old != new for old, new in zip(digests, (co.author_flags_digest(), co.reviewer_flags_digest(), co.gate_flags_digest()))))
        self.run_dir = self.root / 'codex-only'
        codex = self.coordinator('--author-vendor', 'codex', '--reviewer-vendor', 'codex', '--gate-vendor', 'codex')
        self.assertNotIn('claude_child_env', codex.reviewer_flags())

    def plugins(self, **entries):
        return {'document': {'version': 2, 'plugins': entries}, 'error': None}

    def test_the_hold_hint_appears_only_for_a_plugin_version_or_lastupdated_bump(self):
        old = self.plugins(a=[{'version': '1.0', 'lastUpdated': '1', 'installPath': '/p/a'}])
        bumped = self.plugins(a=[{'version': '1.1', 'lastUpdated': '2', 'installPath': '/p/a'}])
        only = [{'file': 'claude_plugins', 'reason': 'unexpected-content-change'}]
        check = lambda findings, before, after: rc.plugin_version_bump_only(findings, {'claude_plugins': before}, {'claude_plugins': after})
        self.assertTrue(check(only, old, bumped))
        self.assertFalse(check(only, old, self.plugins(a=[{'version': '1.1', 'lastUpdated': '2', 'installPath': '/elsewhere'}])))   # another field changed
        self.assertFalse(check(only, old, self.plugins(a=old['document']['plugins']['a'], b=[{'version': '1'}])))                  # a plugin was added
        self.assertFalse(check(only, old, old))
        self.assertFalse(check(only + [{'file': 'claude_settings', 'reason': 'unexpected-content-change'}], old, bumped))
        self.assertFalse(check([{'file': 'claude_settings', 'reason': 'unexpected-content-change'}], old, bumped))
        self.assertFalse(check(only, old, {'document': None, 'error': 'ValueError'}))
        self.assertIn('`resume` re-runs the turn on a fresh baseline', rc.PLUGIN_UPDATE_HINT)

    def test_a_dropped_or_added_version_key_is_not_a_bump(self):   # G-b, RF follow-up (b): the key structure is kept, only the values are ignored
        entry = {'version': '1.0', 'lastUpdated': '1', 'installPath': '/p/a'}
        old = self.plugins(a=[entry])
        only = [{'file': 'claude_plugins', 'reason': 'unexpected-content-change'}]
        check = lambda before, after: rc.plugin_version_bump_only(only, {'claude_plugins': before}, {'claude_plugins': after})
        self.assertTrue(check(old, self.plugins(a=[{**entry, 'version': '2.0', 'lastUpdated': '9'}])))
        self.assertFalse(check(old, self.plugins(a=[{k: v for k, v in entry.items() if k != 'version'}])))          # version key dropped
        self.assertFalse(check(old, self.plugins(a=[{k: v for k, v in entry.items() if k != 'lastUpdated'}])))      # lastUpdated key dropped
        self.assertFalse(check(self.plugins(a=[{'installPath': '/p/a'}]), self.plugins(a=[{**entry, 'installPath': '/p/a'}])))   # version keys added


class ApproveWithOpenBlockingFindingsTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def test_an_approve_that_opens_a_security_finding_goes_to_the_author_without_a_hold(self):
        result = self.run_coordinator('--max-plan-rounds', '3', env={'FAKE_PLAN_APPROVE_SECURITY': '1'})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual(state['status'], 'DONE')
        plan = [row for row in state['review_verdicts'] if row['phase'] == 'PLAN']
        self.assertEqual([(row['reviewer_raw_verdict'], row['effective_verdict']) for row in plan], [('APPROVE', 'REVISE'), ('APPROVE', 'APPROVE')])
        self.assertEqual(state['plan_rounds'], 2)                              # the author revised once more
        (refusal,) = state['approve_refusals']
        self.assertEqual((refusal['phase'], refusal['reason']), ('PLAN', 'APPROVE rejected with open blocking findings: F001'))
        self.assertTrue(any(turn.get('approve_refusal') == refusal['reason'] for turn in state['turns']))
        self.assertEqual([row for row in state['finding_ledger'] if row['status'] == 'open' and row.get('security')], [])

    def test_at_the_round_limit_the_usual_limit_hold_applies_and_done_is_never_reached(self):
        for limit in ('1', '3'):
            with self.subTest(limit=limit):
                self.run_dir = self.root / ('limit-' + limit)
                result = self.run_coordinator('--max-plan-rounds', limit, env={'FAKE_PLAN_APPROVE_SECURITY': 'always'})
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                state = self.state()
                self.assertEqual((state['status'], state['hold_reason'], state['plan_rounds']), ('HOLD', 'PLAN round limit reached', int(limit)))
                self.assertEqual(len(state['approve_refusals']), int(limit))
                self.assertNotEqual(state['phase'], 'EXEC')
                self.assertTrue(any(row['status'] == 'open' and row.get('security') for row in state['finding_ledger']))

    def test_the_reviewer_prompt_says_the_approve_is_converted(self):
        use_lifecycle_on(self, self)
        co = self.coordinator()
        prompt = co._review_prompt('reviewer', '')
        self.assertIn('converted to REVISE', prompt)
        self.assertIn('close the finding with evidence', prompt)


class TomlTopLevelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def git(self, cwd, *args):
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.test', *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def attribute(self, before, after, workspaces):
        return rc._only_codex_workspace_trust_append({'raw': before}, {'raw': after}, workspaces)

    def test_a_block_inserted_inside_a_multiline_string_is_not_attributed(self):
        ws = self.root / 'ws'
        ws.mkdir()
        for quote in ('"""', "'''"):
            with self.subTest(quote=quote):
                before = f'developer_instructions = {quote}\nfirst\n[section]\nmore\n{quote}\n[features]\nplugins = false\n'
                after = before.replace('[section]', trust(ws) + '[section]', 1)
                self.assertEqual(self.attribute(before, after, [ws]), [])
        normal = 'model = "x"\n\n[features]\nplugins = false\n'
        self.assertEqual(self.attribute(normal, 'model = "x"\n\n' + trust(ws) + '\n[features]\nplugins = false\n', [ws]), [str(ws)])
        closed = 'text = """\na\n"""\n\n[features]\nplugins = false\n'      # the string is closed before the insertion point
        self.assertEqual(self.attribute(closed, closed.replace('\n[features]', trust(ws) + '\n[features]'), [ws]), [str(ws)])

    def test_the_tokenizer_respects_escapes_comments_single_line_strings_and_brackets(self):
        top = rc._toml_top_level_at
        self.assertTrue(top('a = "x\\"y"\n# it\'s a comment """\nb = \'q\'\n', len('a = "x\\"y"\n# it\'s a comment """\nb = \'q\'\n')))
        self.assertFalse(top('a = [\n  1,\n', len('a = [\n  1,\n')))               # an array is still open
        self.assertFalse(top('a = """x\n', len('a = """x\n')))                    # unterminated multi-line string
        self.assertFalse(top('a = "x\n', len('a = "x\n')))                       # unterminated single-line string
        self.assertFalse(top('a = """x""""\n', len('a = """x""""\n')))              # four closing quotes: ambiguous
        self.assertFalse(top('a = 1\nb', len('a = 1\nb')))                        # not at the start of a line
        self.assertTrue(top('a = "\\\\"\n', len('a = "\\\\"\n')))                  # an escaped backslash does not escape the quote
        self.assertFalse(top('a = 1\n]\n', len('a = 1\n]\n')))

    def repo(self, name):
        repo = self.root / name
        repo.mkdir()
        self.git(repo, 'init', '-q')
        (repo / 'f.txt').write_text('x\n')
        self.git(repo, 'add', 'f.txt')
        self.git(repo, 'commit', '-qm', 'c')
        return repo

    def test_a_linked_worktree_acknowledges_both_blocks_in_any_order_and_nothing_extra(self):
        main = self.repo('main')
        linked = self.root / 'linked'
        self.git(main, 'worktree', 'add', '-q', str(linked))
        before = 'model = "x"\n\n[features]\nplugins = false\n'
        digest = hashlib.sha256(before.encode()).hexdigest()
        config = self.root / 'config.toml'
        for first, second in ((main, linked), (linked, main)):
            after = 'model = "x"\n\n' + trust(first) + '\n' + trust(second) + '\n[features]\nplugins = false\n'
            for text, expected in ((after, True), (after.replace('model = "x"', 'model = "y"'), False), (after + 'extra = 1\n', False),
                                   (after.replace('plugins = false', 'plugins = true'), False), (after + trust(main), False)):
                with self.subTest(order=(first.name, second.name), expected=expected, text=text[-30:]):
                    config.write_text(text)
                    self.assertEqual(rc.trust_entry_only_since_hash(config, digest, linked), expected)
        config.write_text('model = "x"\n\n' + trust(main) + '\n' + trust(linked) + '\n[features]\nplugins = false\n')
        self.assertFalse(rc.trust_entry_only_since_hash(config, digest, self.repo('other')))   # not this workspace's repository
        config.write_text('developer_instructions = """\n[features]\n"""\n')
        multi = 'developer_instructions = """\n' + trust(main) + trust(linked) + '[features]\n"""\n'
        config.write_text(multi)
        self.assertFalse(rc.trust_entry_only_since_hash(config, hashlib.sha256('developer_instructions = """\n[features]\n"""\n'.encode()).hexdigest(), linked))


if __name__ == '__main__':
    unittest.main()
