"""V312-S (owner 2026-10-10): a hardcoded credential on a line the run's change adds is caught on every route, including
`--lifecycle-mode off`, which has no SECURITY stage. A deterministic scan of context/delta.patch turns a hit into a blocking
program finding for the author (path:line and the pattern kind only, never the value); the last EXEC round HOLDs; a report
run lists it; `secret-scan-allow:` work-item lines exempt deliberate fixtures.

Bounded normal-use regressions (owner rule: users and models are assumed benign): the realistic mistake of committing a
real credential, not obfuscated or deliberately hidden secrets. Every credential-shaped value below is built at runtime,
so the repository itself carries no literal the scanner matches.

V313 narrowed the rules on the hits of 704 real commits (one true credential, thirteen commits blocked on a false hit):
V313Tests pins each shape that must still hit and each that no longer does.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import content_rules
from scripts import delivery_scope as ds
from scripts import security_preflight as sp
from paired_session import leak_scan
from paired_session import test_field23_repo_text_scan as f23
from paired_session import test_off_user_surface as tou
from paired_session import test_real_coordinator as trc
from paired_session import test_worktree_lifecycle as twl
from paired_session.lifecycle_test_helpers import use_lifecycle_on

rc = f23.rc
RAND = 'aZ3kQ9mP2xL7vN4bR8tY6wC1'
JWT = ('eyJ' + 'hbGciOiJIUzI1NiJ9' + '.' + 'eyJ' + 'yb2xlIjoic2VydmljZV9yb2xlIn0' + '.' + 'Qm9kZ3hKc2lPa1pXN2VyYm5yX3c'
       + 'aZ3kQ9mP2xL7vN4b')   # V313: a signature-length third segment (43 characters)
VALUES = {'jwt': JWT,
          'sk-key': 'sk-' + RAND + RAND,
          'anthropic-key': 'sk-' + 'ant-api03-' + RAND + RAND,
          'openai-project-key': 'sk-' + 'proj-' + RAND + RAND,
          'aws-access-key-id': 'AKIA' + 'Q3X7M2P9L4K8N6B1',
          'github-token': 'gh' + 'p_' + RAND + 'Ab12CdEf9012',
          'slack-token': 'xox' + 'b-123456789012-1234567890123-' + RAND,
          'google-api-key': 'AI' + 'za' + RAND + 'Kq8Lm2Np4Rt',
          'stripe-live-key': 'sk' + '_live_' + RAND}
MARKER = '-----BEGIN ' + 'RSA PRIVATE KEY-----'
KEY_BODY = (RAND * 3)[:64]   # one line of key material, as base64 text
PRIVATE_KEY = MARKER + '\\n' + KEY_BODY   # V313: a key in one string with \n escapes; the marker alone is not a hit
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
                           ('# -----BEGIN ' + 'ENCRYPTED PRIVATE KEY-----\\n' + KEY_BODY, 'private-key-block')):   # V313: with a key
            with self.subTest(line=line):
                self.assertEqual(leak_scan.scan_line(line), rule)

    def test_the_post_body_scan_applies_every_rule_of_the_table(self):   # scan_file: text the run publishes (ADDED_TEXT)
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

    def test_every_rule_has_a_scope_and_the_whole_delivery_scope_is_the_six_rules_security_had(self):
        scopes = {rule: scope for rule, _, scope, _ in content_rules.RULES}
        self.assertEqual([rule for rule, scope in scopes.items() if scope == content_rules.WHOLE_DELIVERY],
                         ['private-key-block', 'aws-access-key-id', 'github-token', 'slack-token', 'google-api-key',
                          'stripe-live-key'])
        self.assertEqual({rule for rule, scope in scopes.items() if scope == content_rules.ADDED_TEXT} | {content_rules.GENERIC_RULE},
                         {'jwt', 'anthropic-key', 'openai-project-key', 'sk-key', 'generic-secret-assignment'})
        self.assertEqual(content_rules.GENERIC_SCOPE, content_rules.ADDED_TEXT)
        text = '\n'.join([f'KEY = "{JWT}"', f'db_password = "{GENERIC}"', f'T = "{VALUES["github-token"]}"',
                          f'K = "{VALUES["sk-key"]}"'])
        self.assertEqual(content_rules.scan_text(text, content_rules.WHOLE_DELIVERY), [('github-token', 3)])
        self.assertEqual(content_rules.scan_text(text, content_rules.ADDED_TEXT),
                         [('jwt', 1), ('generic-secret-assignment', 2), ('github-token', 3), ('sk-key', 4)])
        with self.assertRaisesRegex(ValueError, 'unknown scan scope'):
            content_rules.scan_text(text, 'everything')

    def test_the_whole_delivery_preflight_ignores_added_text_rules_in_files_the_run_did_not_touch(self):
        with tempfile.TemporaryDirectory() as scratch:   # a repository that already holds samples, as real ones do
            repo = Path(scratch) / 'repo'
            repo.mkdir()
            git = lambda *args: subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
            for args in (('init', '-q'), ('config', 'user.name', 'Fake'), ('config', 'user.email', 'fake@example.test')):
                git(*args)
            files = {'.gitignore': twl.COVERING_GITIGNORE, 'docs/auth.md': f'A sample bearer value: {JWT}\n',
                     'legacy/client.py': f'api_key = "{GENERIC}"\nOPENAI = "{VALUES["sk-key"]}"\n'}
            for name, text in files.items():
                (repo / name).parent.mkdir(parents=True, exist_ok=True)
                (repo / name).write_text(text)
            git('add', '-A')
            git('commit', '-qm', 'existing repository content')
            info = ds.repository(str(repo))
            baseline = ds.build_baseline(info, ['.'], ds.capture_state(info))
            report = sp.scan(str(repo), manifest=ds.build_manifest(baseline, ds.capture_state(info)))
            self.assertEqual((report['status'], report['findings'], report['scanned_files']), ('clean', [], 3))
            (repo / 'settings.txt').write_text(f'x = 1\nTOKEN = "{VALUES["github-token"]}"\n')   # a six-rule value still blocks
            report = sp.scan(str(repo), manifest=ds.build_manifest(baseline, ds.capture_state(info)))
            self.assertEqual(report['status'], 'blocked')
            self.assertEqual([(row['rule'], row['path'], row['line']) for row in report['findings']],
                             [('github-token', 'settings.txt', 2)])
            self.assertNotIn(VALUES['github-token'], json.dumps(report))

    def test_the_coordinator_starts_beside_a_foreign_scripts_package(self):   # gate round: an ordinary Python environment
        with tempfile.TemporaryDirectory() as scratch:   # a user's project root on PYTHONPATH with its own regular package
            (Path(scratch) / 'scripts').mkdir()
            (Path(scratch) / 'scripts' / '__init__.py').write_text('')
            entry = Path(leak_scan.__file__).resolve().parents[1] / 'bin' / 'paired-session'
            result = subprocess.run([sys.executable, str(entry), '--help'], env={**os.environ, 'PYTHONPATH': scratch},
                                    cwd=scratch, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn('permission-probe', result.stdout)
        rules = Path(leak_scan.content_rules.__file__).resolve()             # this repository's table, whatever sys.path holds
        self.assertEqual(rules, Path(leak_scan.__file__).resolve().parents[1] / 'scripts' / 'content_rules.py')

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

    def test_a_private_key_marker_and_its_body_on_the_next_added_line_are_one_hit_at_the_marker(self):   # V313
        end = '-----END ' + 'RSA PRIVATE KEY-----'
        patch_text = ('diff --git a/deploy.py b/deploy.py\nindex 1..2 100644\n--- a/deploy.py\n+++ b/deploy.py\n'
                      f'@@ -1,2 +1,6 @@\n import os\n+{MARKER}\n+{KEY_BODY}\n+{KEY_BODY}\n+{end}\n context = 1\n'
                      f'@@ -9,3 +13,4 @@\n a = 1\n+{MARKER}\n {KEY_BODY}\n b = 2\n'        # the body is not an added line
                      f'@@ -20,2 +25,4 @@\n c = 1\n+KEY = """{MARKER}\n+  {KEY_BODY}\n d = 2\n')
        self.assertEqual(leak_scan.scan_patch(patch_text),
                         [{'path': 'deploy.py', 'line': 2, 'kind': 'private-key-block'},
                          {'path': 'deploy.py', 'line': 26, 'kind': 'private-key-block'}])

    def test_allow_markers(self):
        workitem = ('# Task\nsecret-scan-allow: tests/fixtures/*.json\n- secret-scan-allow: `docs/samples/`\n'
                    'Mentioning secret-scan-allow: inline is not a marker.\n')
        allow = leak_scan.allow_patterns(workitem)
        self.assertEqual(allow, ['tests/fixtures/*.json', 'docs/samples/'])
        self.assertTrue(leak_scan.exempt('tests/fixtures/jwt.json', allow))
        self.assertTrue(leak_scan.exempt('docs/samples/a/b.md', allow))
        self.assertFalse(leak_scan.exempt('src/app.py', allow))


def segment(data) -> str:
    """One base64url segment, unpadded, of a JSON object or of bytes."""
    raw = data if isinstance(data, bytes) else json.dumps(data, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def noise(size: int, seed: str = 'v313') -> bytes:
    """`size` bytes that look random, the same on every run."""
    out = b''
    while len(out) < size:
        out += hashlib.sha256(f'{seed}{len(out)}'.encode()).digest()
    return out[:size]


def letters_and_digits(size: int) -> str:
    return (RAND * (size // len(RAND) + 1))[:size]


class V313Tests(unittest.TestCase):
    """The rules as narrowed on real history: every shape that must still hit, and the false hits that are gone."""
    END = '-----END ' + 'RSA PRIVATE KEY-----'

    def both_scans(self, text: str) -> tuple:
        """([(rule, line)] of the whole-delivery scan, the same of the added-line scan of a new file that holds `text`)."""
        lines = text.split('\n')
        patch_text = (f'diff --git a/k.py b/k.py\nnew file mode 100644\n--- /dev/null\n+++ b/k.py\n@@ -0,0 +1,{len(lines)} @@\n'
                      + ''.join(f'+{line}\n' for line in lines))
        return (content_rules.scan_text(text, content_rules.WHOLE_DELIVERY),
                [(hit['kind'], hit['line']) for hit in leak_scan.scan_patch(patch_text)])

    def test_the_shape_of_the_true_credential_still_hits(self):   # the one true credential of the replay: 36 / 138 / 43
        claims = {'iss': 'supabase', 'ref': letters_and_digits(20).lower(), 'role': 'service_role', 'iat': 1700000000,
                  'exp': 2000000000}
        token = '.'.join((segment({'alg': 'HS256', 'typ': 'JWT'}), segment(claims), segment(noise(32))))
        self.assertEqual([len(part) for part in token.split('.')], [36, 138, 43])
        for line in (f'SUPABASE_KEY = "{token}"', f'key = "{token}"', f'curl -H "Authorization: Bearer {token}"'):
            with self.subTest(line=line[:16]):
                self.assertEqual(leak_scan.scan_line(line), 'jwt')
        self.assertEqual(self.both_scans(f'import os\nSUPABASE_KEY = "{token}"'), ([], [('jwt', 2)]))

    def test_a_private_key_hits_in_both_scans_as_a_block_and_as_one_string(self):
        hit = lambda line: [('private-key-block', line)]
        for form, text, line in (
                ('block', f'x = 1\n{MARKER}\n{KEY_BODY}\n{KEY_BODY}\n{self.END}\ny = 2', 2),
                ('indented block', f'x = 1\nprivate_key: |\n  {MARKER}\n  {KEY_BODY}\n  {self.END}', 3),
                ('CRLF block', f'x = 1\r\n{MARKER}\r\n{KEY_BODY}\r\n{self.END}', 2),
                ('one string', f'x = 1\nPEM = "{MARKER}\\n{KEY_BODY}\\n{self.END}\\n"', 2),
                ('one string, \\r\\n', f'{{\n"private_key": "{MARKER}\\r\\n{KEY_BODY}\\r\\n{self.END}"\n}}', 2)):
            with self.subTest(form=form):
                self.assertEqual(self.both_scans(text), (hit(line), hit(line)))

    def test_a_private_key_marker_without_key_material_is_not_a_hit(self):
        regex = f'const body = pem.replace(/{MARKER}|{self.END}|\\s/g, "");'        # an alternation of the two markers
        template = f'const pem = `{MARKER}\\n${{base64(der)}}\\n{self.END}\\n`;'    # around a value computed at run time
        for form, text in (('regex', regex), ('template', template), ('marker only', f'# {MARKER}'),
                           ('marker and prose', f'{MARKER}\n(paste the key of your provider here)\n{self.END}'),
                           ('short body', f'{MARKER}\n{KEY_BODY[:39]}\n{self.END}')):
            with self.subTest(form=form):
                self.assertEqual(self.both_scans(f'x = 1\n{text}\ny = 2'), ([], []))
                self.assertIsNone(leak_scan.scan_line(text.split('\n')[0]))

    def test_a_jwt_needs_a_signature_length_third_segment(self):
        head = segment({'alg': 'HS256'})
        self.assertEqual(len(head), 20)
        for sizes in ((20, 11), (27, 20), (138, 42)):   # the fake tokens of unit tests: 20/20/11 and 20/27/20
            token = '.'.join((head, *map(letters_and_digits, sizes)))
            for line in (f'let jwt = "{token}"', f'let token = "{token}"', f'"access_token": "{token}",'):
                with self.subTest(sizes=sizes, line=line[:14]):   # not a jwt, and not caught again under a token name
                    self.assertIsNone(leak_scan.scan_line(line))
        token = '.'.join((head, letters_and_digits(27), letters_and_digits(43)))
        self.assertEqual(leak_scan.scan_line(f'let token = "{token}"'), 'jwt')

    def test_signed_data_with_a_certificate_chain_header_is_not_a_credential(self):
        chain = [base64.b64encode(noise(1100, f'cert{n}')).decode() for n in range(3)]
        body, signature = segment(noise(2400, 'payload')), segment(noise(64, 'signature'))
        signed = '.'.join((segment({'alg': 'ES256', 'x5c': chain}), body, signature))   # as an App Store signed transaction
        self.assertGreater(len(signed), 9000)
        self.assertTrue(content_rules.random_like(signed) and not content_rules.placeholder(signed))   # x5c is the reason
        for line in (f'"signedTransactionInfo": "{signed}",', f'"transaction_token": "{signed}"', f'JWS = "{signed}"'):
            with self.subTest(line=line[:24]):
                self.assertIsNone(leak_scan.scan_line(line))
        for name, header in (('no x5c', segment({'alg': 'ES256', 'kid': 'k1'})),
                             ('x5c as a value', segment({'alg': 'ES256', 'kid': 'x5c'})),
                             ('not JSON', segment(b'{"alg":"ES256","x5c":["' + noise(30).hex().encode())),
                             ('not base64', 'eyJ' + letters_and_digits(34))):
            with self.subTest(header=name):   # a header that does not decode as JSON has no x5c
                self.assertEqual(leak_scan.scan_line(f'JWS = "{header}.{body}.{signature}"'), 'jwt')

    def test_publishable_keys_and_public_names_are_not_secrets(self):
        value = letters_and_digits(31)
        for line in (f'SUPABASE_KEY = "sb_publishable_{value}"', f"const supabaseKey = 'sb_publishable_{value}';",
                     f'stripe_key = "pk_live_{value}"', f'stripe_key = "pk_test_{value}"',
                     f'NEXT_PUBLIC_API_KEY = "{value}"', f'"publicKey": "{value}",', f'publishableKey: "{value}"',
                     f'PUBLISHABLE_TOKEN = "{value}"'):
            with self.subTest(line=line.split(value)[0]):
                self.assertIsNone(leak_scan.scan_line(line))
        for line in (f'SUPABASE_KEY = "sb_secret_{value}"', f'stripe_key = "live_{value}"', f'api_key = "{value}"'):
            with self.subTest(line=line.split(value)[0]):
                self.assertEqual(leak_scan.scan_line(line), 'generic-secret-assignment')

    def test_a_composite_id_joined_with_a_bar_or_a_hash_is_not_random(self):
        words = ('mainevent', 'day2flight', 'table14seat')
        for joiner in '|#':
            with self.subTest(joiner=joiner):
                self.assertIsNone(leak_scan.scan_line(f'event_key = "{joiner.join(words)}"'))
                self.assertIsNone(leak_scan.scan_line(f'EXTINCT_KEY = "{joiner.join(words)}"'))
                self.assertEqual(leak_scan.scan_line(f'event_key = "{joiner.join((*words, letters_and_digits(16)))}"'),
                                 'generic-secret-assignment')   # a random run of 16+ characters is still one
        self.assertEqual(leak_scan.scan_line(f'event_key = "{"".join(words)}"'), 'generic-secret-assignment')   # no separator

    def test_the_other_credential_shapes_still_hit(self):
        value = letters_and_digits(40)
        self.assertEqual(leak_scan.scan_line(f'api_key = "{value}"'), 'generic-secret-assignment')
        for prefix, rule in (('sk-', 'sk-key'), ('sk-' + 'ant-', 'anthropic-key'), ('sk-' + 'proj-', 'openai-project-key')):
            with self.subTest(rule=rule):
                self.assertEqual(leak_scan.scan_line(f'client = Client("{prefix}{value}")'), rule)
        five = ('aws-access-key-id', 'github-token', 'slack-token', 'google-api-key', 'stripe-live-key')
        expected = [(rule, line) for line, rule in enumerate(five, 1)]   # the other whole-delivery rules, in both scans
        self.assertEqual(self.both_scans('\n'.join(f'V = "{VALUES[rule]}"' for rule in five)), (expected, expected))


class SecretScanRunTests(unittest.TestCase):
    def setUp(self):
        self.t = f23.RepoTextScanTests('test_history_in_a_new_file_holds')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.calls = []

    def start(self, on=False, workitem_extra='', files=None):
        if on:
            use_lifecycle_on(self, self.t.h)
        if workitem_extra:
            self.t.h.workitem.write_text(self.t.h.workitem.read_text() + workitem_extra)
        self.co = self.t.coordinator(files)
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
                         'left to remove it; remove it from the workspace and resume, or abort (a false positive or a '
                         'deliberate fixture needs a `secret-scan-allow: <path or glob>` line in the work item of a new run: '
                         "this run's work item is frozen)")
        self.assertIn('needs a `secret-scan-allow: <path or glob>` line in the work item of a new run', row['failure_scenario'])
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

    SPACED = ('docs/old name.py', 'new file.py')   # git ends the "+++ b/<path>" header of such a path with a tab

    def spaced_change(self, workitem_extra=''):
        tracked, new = self.SPACED
        co = self.start(workitem_extra=workitem_extra, files={f23.SWIFT: f23.BASE_SWIFT, tracked: 'x = 1\n'})
        self.t.write(tracked, f'x = 1\nKEY = "{JWT}"\n')                     # a tracked file the change modifies
        self.t.write(new, f'KEY = "{JWT}"\n')                                # a new untracked file (the --no-index form)
        return co

    def test_a_path_with_a_space_is_named_exactly_from_the_patch_git_generates(self):
        tracked, new = self.SPACED
        co = self.spaced_change()
        patch_text = co._delta_patch()[0]                                    # the coordinator's own delta, from real git
        for name in self.SPACED:
            self.assertIn(f'+++ b/{name}\t\n', patch_text)
        self.assertEqual(leak_scan.scan_patch(patch_text, co._git_unquote),
                         [{'path': tracked, 'line': 2, 'kind': 'jwt'}, {'path': new, 'line': 1, 'kind': 'jwt'}])
        self.review()
        [row] = self.rows()
        self.assertEqual((row['file'], row['summary']),
                         (f'{tracked}:2', f'the change adds a hardcoded credential at {tracked}:2 (jwt), {new}:1 (jwt)'))

    def test_the_marker_exempts_a_path_with_a_space(self):
        tracked, new = self.SPACED
        co = self.spaced_change(f'secret-scan-allow: {new}\nsecret-scan-allow: {tracked}\n')
        self.review()
        self.assertEqual((co.state['status'], co.state['next']), ('ACTIVE', 'gate'))
        self.assertEqual(self.rows(), [])
        self.assertEqual((co.state['secret_scan']['blocking'], co.state['secret_scan']['exempted']),
                         ([], [f'{tracked}:2 (jwt)', f'{new}:1 (jwt)']))

    def test_the_operators_diff_prefix_settings_do_not_change_the_reported_path(self):   # gate round: an ordinary git setting
        fixture = 'tests/fixtures/token.py'
        co = self.start(workitem_extra='secret-scan-allow: tests/fixtures/*\n',
                        files={f23.SWIFT: f23.BASE_SWIFT, 'app.py': 'x = 1\n'})
        self.t.write('app.py', f'x = 1\nKEY = "{JWT}"\n')                    # tracked: "+++ w/app.py" under mnemonicPrefix
        self.t.write(fixture, f'TOKEN = "{JWT}"\n')                          # untracked, --no-index: "+++ 2/tests/..."
        default = co._delta_patch()[0]
        plain_diff = ['diff', *rc.candidate_tree.NO_EXT_DIFF, '--binary']   # the commands without fixed prefixes
        self.assertEqual(default, co._git([*plain_diff, co.state['base_commit'], '--'])
                         + co._git([*plain_diff, '--no-index', '--', '/dev/null', fixture], ok=(0, 1)))   # unchanged by default
        for setting, header in (('mnemonicPrefix', '+++ w/app.py'), ('noprefix', '+++ app.py')):
            with self.subTest(setting=setting):
                config = self.t.h.root / f'gitconfig-{setting}'
                config.write_text(f'[diff]\n\t{setting} = true\n')
                with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config)}):
                    plain = subprocess.run(['git', 'diff'], cwd=self.t.ws, check=True, capture_output=True, text=True).stdout
                    self.assertIn(header + '\n', plain)                      # the operator's setting is in effect
                    self.assertEqual(co._delta_patch()[0], default)          # and the run's delta is byte-identical
                    self.assertEqual(leak_scan.scan_patch(co._delta_patch()[0], co._git_unquote),
                                     [{'path': 'app.py', 'line': 2, 'kind': 'jwt'}, {'path': fixture, 'line': 1, 'kind': 'jwt'}])
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(self.t.h.root / 'gitconfig-mnemonicPrefix')}):
            self.review()
        [row] = self.rows()
        self.assertEqual((row['file'], row['summary']), ('app.py:2', 'the change adds a hardcoded credential at app.py:2 (jwt)'))
        self.assertEqual(co.state['secret_scan']['exempted'], [f'{fixture}:1 (jwt)'])   # the marker still matches

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

    def test_a_default_route_run_with_an_exempted_fixture_reaches_done_through_security(self):
        fixture = 'fixtures/jwt_sample.py'   # the whole-delivery SECURITY scan does not apply the jwt rule: no HOLD there
        self.workitem.write_text(self.workitem.read_text() + 'secret-scan-allow: fixtures/*\n')
        result = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_AUTHOR_SECRET': fixture})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(twl.DONE, result.stdout)
        state = self.state()
        self.assertEqual((state['config']['lifecycle_mode'], state['status'], state['lifecycle']['stage']), ('on', 'DONE', 'DONE'))
        [security] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
        self.assertEqual((security['status'], security['route'], security['preflight']['status']), ('READY', 'DONE', 'clean'))
        self.assertEqual(state['secret_scan']['exempted'], [f'{fixture}:1 (jwt)'])
        self.assertEqual([row for row in state['finding_ledger'] if row['source'] == 'secret-scan'], [])
        self.assertIn('eyJ' + 'hbGciOiJIUzI1NiJ9', (self.workspace / fixture).read_text())   # the fixture is delivered
        self.assertEqual(self.leaked('eyJ' + 'hbGciOiJIUzI1NiJ9', result.stdout), [])

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
