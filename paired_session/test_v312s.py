"""V312-S (owner 2026-10-10): a hardcoded credential on a line the run's change adds is caught on every route, including
`--lifecycle-mode off`, which has no SECURITY stage. A deterministic scan of context/delta.patch turns a hit into a blocking
program finding for the author (path:line and the pattern kind only, never the value); the last EXEC round HOLDs; a report
run lists it; `secret-scan-allow:` work-item lines exempt deliberate fixtures.

Bounded normal-use regressions (owner rule: users and models are assumed benign): the realistic mistake of committing a
real credential, not obfuscated or deliberately hidden secrets. Every credential-shaped value below is built at runtime,
so the repository itself carries no literal the scanner matches.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from paired_session import leak_scan
from paired_session import test_field23_repo_text_scan as f23
from paired_session import test_off_user_surface as tou
from paired_session import test_real_coordinator as trc
from paired_session import test_worktree_lifecycle as twl
from paired_session.lifecycle_test_helpers import use_lifecycle_on

rc = f23.rc
RAND = 'aZ3kQ9mP2xL7vN4bR8tY6wC1'
JWT = 'eyJ' + 'hbGciOiJIUzI1NiJ9' + '.' + 'eyJ' + 'yb2xlIjoic2VydmljZV9yb2xlIn0' + '.' + 'Qm9kZ3hKc2lPa1pXN2VyYm5yX3c'
VALUES = {'jwt': JWT,
          'sk-key': 'sk-' + RAND + RAND,
          'anthropic-key': 'sk-' + 'ant-api03-' + RAND + RAND,
          'openai-project-key': 'sk-' + 'proj-' + RAND + RAND,
          'aws-access-key-id': 'AKIA' + 'Q3X7M2P9L4K8N6B1',
          'github-token': 'gh' + 'p_' + RAND + 'Ab12CdEf9012',
          'slack-token': 'xox' + 'b-123456789012-1234567890123-' + RAND,
          'google-api-key': 'AI' + 'za' + RAND + 'Kq8Lm2Np4Rt',
          'stripe-live-key': 'sk' + '_live_' + RAND}
PRIVATE_KEY = '-----BEGIN ' + 'RSA PRIVATE KEY-----'
GENERIC = 'Hq7Zr2Lm9Px4Tv8Kw3Ny6'
SERVICE = 'config/service.py'
WITH_SECRET = f'import os\nSUPABASE_KEY = "{JWT}"\n'
REPAIRED = 'import os\nSUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]\n'


class ScannerTests(unittest.TestCase):
    def test_each_pattern_is_detected(self):
        for kind, value in VALUES.items():
            with self.subTest(kind=kind):
                self.assertEqual(leak_scan.scan_line(f'KEY = "{value}"'), kind)
        self.assertEqual(leak_scan.scan_line(PRIVATE_KEY), 'private-key-block')
        for line in (f'db_password = "{GENERIC}"', f'"apiKey": "{GENERIC}",', f"client_secret: '{GENERIC}'",
                     f'const authToken = `{GENERIC}`;', f'API_KEY := "{GENERIC}"'):
            with self.subTest(line=line):
                self.assertEqual(leak_scan.scan_line(line), 'generic-secret-assignment')

    def test_placeholders_lookups_and_normal_code_are_not_detected(self):
        for line in ('api_key = os.getenv("OPENAI_API_KEY_FOR_PROD_2")', 'token = process.env.SUPABASE_SERVICE_ROLE_KEY',
                     'api_key = os.environ.get("OPENAI_KEY", "")', 'secret = config["service"]["secret_key_v2"]',
                     'api_key = "your_api_key_here_123456"', 'token = "${SUPABASE_SERVICE_ROLE_KEY_2024}"',
                     'secret = "<insert-secret-here-1234567>"', 'password = "xxxxxxxxxxxxxxxxxxxxxxxx"',
                     'api_key = "changeme1234567890abcdef"', 'token = "example_token_0123456789abc"',
                     f'token = "dummy{RAND}"', f'token = "test_{RAND}"', 'jwt = "eyJ' + 'x' * 20 + '.' + 'y' * 20 + '.' + 'z' * 20 + '"',
                     'cache_key = "user_profile_v2_settings_2024"',
                     'key: "dashboard.header.title2.subtitle3"', 'password_hint = "must be at least 8 characters long"',
                     'if token == "abcdefghijklmnop1234567": pass', 'pkg = "sk-learn-compatible-estimator-v2"',
                     'token_type = "bearer"', 'sha = "a3f5c9e1b7d24f6a8c0e2b4d6f8a1c3e"'):
            with self.subTest(line=line):
                self.assertIsNone(leak_scan.scan_line(line))

    def test_the_rules_security_had_before_the_shared_table_keep_their_behavior(self):
        for line, rule in (('KEY = "gh' + 'p_' + 'A' * 36 + '"', 'github-token'),   # unfiltered, as SECURITY validated them
                           ('id = "AKIA' + 'IOSFODNN7EXAMPLE"', 'aws-access-key-id'),
                           ('id = "ASIA' + 'Q7' * 8 + '"', 'aws-access-key-id'),
                           ('# -----BEGIN ' + 'ENCRYPTED PRIVATE KEY-----', 'private-key-block')):
            with self.subTest(line=line):
                self.assertEqual(leak_scan.scan_line(line), rule)

    def test_the_security_preflight_uses_the_same_table(self):
        from scripts import security_preflight as sp
        lines = [f'KEY = "{value}"' for value in VALUES.values()] + [PRIVATE_KEY, f'db_password = "{GENERIC}"',
                                                                    'api_key = os.getenv("OPENAI_API_KEY_FOR_PROD_2")']
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, 'body.md')
            with open(path, 'w') as handle:
                handle.write('\n'.join(lines) + '\n')
            found = sp.scan_file(path)
        self.assertEqual([(row['rule'], row['line']) for row in found],
                         [(rule, n) for n, rule in enumerate([*VALUES, 'private-key-block', 'generic-secret-assignment'], 1)])
        self.assertNotIn(JWT, json.dumps(found))

    def test_only_added_lines_count_with_path_and_new_line_number(self):
        patch_text = (f'diff --git a/app.py b/app.py\nindex 1..2 100644\n--- a/app.py\n+++ b/app.py\n'
                      f'@@ -1,3 +1,3 @@\n import os\n-OLD = "{VALUES["github-token"]}"\n+KEY = "{JWT}"\n'
                      f' CONTEXT = "{VALUES["sk-key"]}"\n'
                      f'diff --git a/new file.py b/new file.py\nnew file mode 100644\n--- /dev/null\n+++ "b/new file.py"\n'
                      f'@@ -0,0 +1,2 @@\n+x = 1\n+{PRIVATE_KEY}\n')
        hits = leak_scan.scan_patch(patch_text, rc.Coordinator._git_unquote)
        self.assertEqual(hits, [{'path': 'app.py', 'line': 2, 'kind': 'jwt'},
                                {'path': 'new file.py', 'line': 2, 'kind': 'private-key-block'}])
        self.assertNotIn(JWT, json.dumps(hits))

    def test_allow_markers(self):
        workitem = ('# Task\nsecret-scan-allow: tests/fixtures/*.json\n- secret-scan-allow: `docs/samples/`\n'
                    'Mentioning secret-scan-allow: inline is not a marker.\n')
        allow = leak_scan.allow_patterns(workitem)
        self.assertEqual(allow, ['tests/fixtures/*.json', 'docs/samples/'])
        self.assertTrue(leak_scan.exempt('tests/fixtures/jwt.json', allow))
        self.assertTrue(leak_scan.exempt('docs/samples/a/b.md', allow))
        self.assertFalse(leak_scan.exempt('src/app.py', allow))


class SecretScanRunTests(unittest.TestCase):
    def setUp(self):
        self.t = f23.RepoTextScanTests('test_history_in_a_new_file_holds')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.calls = []

    def start(self, on=False, workitem_extra=''):
        if on:
            use_lifecycle_on(self, self.t.h)
        if workitem_extra:
            self.t.h.workitem.write_text(self.t.h.workitem.read_text() + workitem_extra)
        self.co = self.t.coordinator()
        self.co.state['exec_rounds'] = 1
        self.co.save()
        return self.co

    def invoke(self, dispositions=()):
        test = self

        def fake(role, phase, prompt, schema, fresh=False, **kwargs):
            test.calls.append(role)
            if role == 'gate':
                raise AssertionError('the gate was dispatched')
            snap = rc.git_snapshot(test.t.ws)[0]
            test.co.state['sequence'] += 1
            answer = {'status': 'APPROVE', 'reviewed_snapshot': snap, 'full_review': []}
            if role == 'reviewer':
                answer.update(prior_findings=list(dispositions), self_run_evidence=[{'command': 'python3 -m unittest'}])
            return {'answer': answer, 'snapshot': snap, 'sequence': test.co.state['sequence'], 'role': role}
        return patch.object(self.co, 'invoke', side_effect=fake)

    def review(self, dispositions=()):
        with self.invoke(dispositions), patch.object(self.co, 'render'):
            self.co.reviewer_turn()

    def rows(self):
        return [row for row in self.co.state['finding_ledger'] if row['source'] == 'secret-scan']

    def assert_value_never_written(self, value=JWT):
        skip = (self.co.context, self.co.internal)   # the review views and mirrors of the code itself
        for path in self.co.run_dir.rglob('*'):
            if path.is_file() and not any(parent in skip for parent in path.parents):
                self.assertNotIn(value, path.read_text(errors='replace'), path)
        self.assertNotIn(value, json.dumps(self.co.state))

    def test_off_route_blocks_then_the_author_repair_clears_it(self):
        co = self.start()
        self.assertEqual(co.state['config']['lifecycle_mode'], 'off')
        self.t.write(SERVICE, WITH_SECRET)                                   # a new untracked file of the change
        self.review()
        self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'author'))
        [row] = self.rows()
        self.assertEqual((row['severity'], row['security'], row['file'], row['status']),
                         ('SECURITY', True, f'{SERVICE}:2', 'open'))
        self.assertEqual(row['summary'], f'the change adds a hardcoded credential at {SERVICE}:2 (jwt)')
        self.assertIn(f'{SERVICE}:2 (jwt)', co.state['delivered_review'])
        self.assertEqual(co.state['secret_scan']['blocking'], [f'{SERVICE}:2 (jwt)'])
        self.assert_value_never_written()
        self.t.write(SERVICE, REPAIRED)                                      # the author's repair, in the next round
        co.state.update(next='reviewer', exec_rounds=2)
        self.review([{'id': row['id'], 'disposition': 'still_open', 'evidence': 'not checked'}])
        self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'gate'))   # the program closes its own row
        self.assertEqual(self.rows()[0]['status'], 'fixed')
        self.assertEqual(co.state['secret_scan']['blocking'], [])

    def test_the_on_route_blocks_the_same_way(self):
        co = self.start(on=True)
        self.assertEqual(co.state['config']['lifecycle_mode'], 'on')
        self.t.write(SERVICE, WITH_SECRET)
        self.review()
        self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'author'))
        self.assertEqual([row['file'] for row in self.rows()], [f'{SERVICE}:2'])

    def test_the_last_exec_round_holds_with_a_clear_reason_and_one_row(self):
        co = self.start()
        self.t.write(SERVICE, WITH_SECRET)
        self.review()
        [row] = self.rows()
        co.state.update(next='reviewer', exec_rounds=co.exec_round_limit())
        self.review([{'id': row['id'], 'disposition': 'still_open', 'evidence': 'still there'}])
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertEqual(co.state['hold_reason'],
                         f'secret scan: the change still adds a hardcoded credential at {SERVICE}:2 (jwt); no EXEC round is '
                         'left to remove it; remove it from the workspace and resume, or abort')
        self.assertEqual([r['id'] for r in self.rows()], [row['id']])        # the same hit list stays one row
        self.assert_value_never_written()

    def test_secret_scan_allow_exempts_a_fixture_path(self):
        co = self.start(workitem_extra='secret-scan-allow: tests/fixtures/*\n')
        self.t.write('tests/fixtures/token.py', f'TOKEN = "{JWT}"\n')
        self.t.write(SERVICE, REPAIRED)
        self.review()
        self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'gate'))
        self.assertEqual(self.rows(), [])
        self.assertEqual(co.state['secret_scan'], {'sequence': co.state['secret_scan']['sequence'], 'blocking': [],
                                                   'exempted': ['tests/fixtures/token.py:1 (jwt)']})
        self.assert_value_never_written()

    def test_the_gate_scans_a_tree_that_changed_after_the_review(self):
        co = self.start()
        co.state['next'] = 'gate'
        self.t.write(SERVICE, WITH_SECRET)
        with self.invoke():
            co.gate_turn()
        self.assertEqual(self.calls, [])                                     # no gate turn on a tree with a credential
        self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'author'))
        self.assertEqual(json.loads(co.state['delivered_review'])['source'], 'secret-scan')
        co.state.update(next='gate', exec_rounds=co.exec_round_limit())
        with self.invoke():
            co.gate_turn()
        self.assertEqual(co.state['status'], 'HOLD')
        self.assertTrue(co.state['hold_reason'].startswith(f'secret scan: the change still adds a hardcoded credential at {SERVICE}:2 (jwt)'))
        self.assert_value_never_written()


class SecretScanEndToEndTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp')})
    public_argv = tou.OffUserSurfaceTests.public_argv
    call_public = tou.OffUserSurfaceTests.call_public

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def leaked(self, value, *texts):   # run files the coordinator writes, minus the review views and copies of the code
        skip = {self.run_dir / 'context', self.run_dir / 'internal'}   # and the fresh roles' recorded inputs (the delta)
        return [str(path.relative_to(self.run_dir)) for path in self.run_dir.rglob('*')
                if path.is_file() and not skip & set(path.parents) and not path.name.endswith('.independence-inputs.json')
                and value in path.read_text(errors='replace')] + [
                'output' for text in texts if value in text]

    def test_public_off_run_repairs_the_authors_credential_and_reaches_done(self):
        with patch.dict(os.environ, {'FAKE_AUTHOR_SECRET': 'config_local.py'}):
            code, output = self.call_public(self.public_argv('run', '--skip-probe', '--auto-commit', 'false'))
        self.assertEqual(code, 0, output)
        self.assertIn(twl.DONE, output)
        state = self.state()
        self.assertEqual(state['config']['lifecycle_mode'], 'off')
        [row] = [row for row in state['finding_ledger'] if row['source'] == 'secret-scan']
        self.assertEqual((row['file'], row['status']), ('config_local.py:1', 'fixed'))
        self.assertGreaterEqual(sum(t['role'] == 'author' and t['phase'] == 'EXEC' for t in state['turns']), 2)
        self.assertFalse((self.workspace / 'config_local.py').exists())
        self.assertEqual(self.leaked('eyJ' + 'hbGciOiJIUzI1NiJ9', output), [])

    def test_public_off_run_holds_when_no_exec_round_is_left(self):
        with patch.dict(os.environ, {'FAKE_AUTHOR_SECRET': 'config_local.py'}):
            code, output = self.call_public(self.public_argv('run', '--skip-probe', '--auto-commit', 'false',
                                                             '--max-exec-rounds', '1'))
        state = self.state()
        self.assertEqual(state['status'], 'HOLD', output)
        self.assertTrue(state['hold_reason'].startswith(
            'secret scan: the change still adds a hardcoded credential at config_local.py:1 (jwt)'), state['hold_reason'])
        self.assertEqual(self.leaked('eyJ' + 'hbGciOiJIUzI1NiJ9', output), [])


class ReviewOnlySecretScanTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in
                     ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
                      'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action')})
    leaked = SecretScanEndToEndTests.leaked

    def change(self):
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        (self.workspace / 'config').mkdir()
        (self.workspace / SERVICE).write_text(WITH_SECRET)

    def state(self):
        return json.loads((self.run_dir / 'state.json').read_text())

    def test_a_review_only_report_run_lists_it_as_a_blocking_security_finding(self):
        self.change()
        result = self.run_coordinator('--review-only', '--review-report', '--lifecycle-mode', 'on')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.state()
        self.assertEqual(state['status'], 'REPORTED')
        [row] = [row for row in state['finding_ledger'] if row['source'] == 'secret-scan']
        self.assertEqual((row['severity'], row['security'], row['status']), ('SECURITY', True, 'open'))
        report = (self.run_dir / 'review-report.md').read_text()
        security = report.split('## Security', 1)[1].split('\n## ', 1)[0]
        self.assertIn(f'the change adds a hardcoded credential at {SERVICE}:2 (jwt)', security)
        self.assertEqual(self.leaked(JWT, result.stdout), [])

    def test_a_review_only_run_holds_when_the_fix_rounds_leave_it(self):
        self.change()
        result = self.run_coordinator('--review-only', '--max-exec-rounds', '2')
        state = self.state()
        self.assertEqual(state['status'], 'HOLD', result.stdout + result.stderr)
        self.assertTrue(state['hold_reason'].startswith(
            f'secret scan: the change still adds a hardcoded credential at {SERVICE}:2 (jwt)'), state['hold_reason'])
        self.assertTrue(any(t['role'] == 'author' for t in state['turns']))   # the author had its fix round
        self.assertEqual(self.leaked(JWT, result.stdout), [])


if __name__ == '__main__':
    unittest.main()
