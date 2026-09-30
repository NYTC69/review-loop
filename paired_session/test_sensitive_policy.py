import re
import unittest
from pathlib import Path

from paired_session.sensitive_policy import sensitive_path_category


LEGACY_GREP_CHAINS = (
    ((r'\.(pem|key|crt|cert|cer|p12|pfx|jks|keystore|ppk|asc|gpg|pgp)$', True, False),),
    ((r'(^|/)(\.env|\.env\..+)$', True, False),
     (r'\.(example|sample)(\.[^/]*)?$', True, True)),
    ((r'\.(env)$', True, False),),
    ((r'(^|/)[^/]*(credentials?|secrets?|api[-_.]?key|auth[-_.]?token|passwd|shadow)[^/]*$',
      True, False), (r'\.(example|sample)(\.[^/]*)?$', True, True)),
    ((r'(^|/)id_(rsa|dsa|ecdsa|ed25519)', True, False),),
    ((r'(^|/)service-account[^/]*\.json$', True, False),),
    ((r'(^|/)\.(aws|gcloud)/', True, False),),
    ((r'\.(sqlite3?|db|dump|sql\.gz)$', True, False),),
    ((r'(\.tfstate|\.tfvars)($|\.)', True, False), (r'\.example$', True, True)),
    ((r'(^|/)\.terraform/', False, False),),
    ((r'\.map$', True, False),),
    ((r'\.log$', True, False),),
    ((r'(^|/)logs/', False, False),),
)


def parse_grep_chains(block):
    matcher = re.compile(r"\s*\|\s*grep\s+(-v\s+)?-(i)?E\s+'([^']+)'")
    chains, pending = [], ''
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        continued = line.endswith('\\')
        pending += ' ' + (line[:-1] if continued else line)
        if continued:
            continue
        command = pending.strip()
        if not command.startswith('git ls-files'):
            raise AssertionError('legacy sensitive-file grep syntax is not covered')
        position, chain = len('git ls-files'), []
        while position < len(command):
            match = matcher.match(command, position)
            if match is None:
                raise AssertionError('legacy sensitive-file grep syntax is not covered')
            chain.append((match.group(3), bool(match.group(2)), bool(match.group(1))))
            position = match.end()
        if not chain:
            raise AssertionError('legacy sensitive-file grep syntax is not covered')
        chains.append(tuple(chain))
        pending = ''
    if pending:
        raise AssertionError('legacy sensitive-file command is incomplete')
    return tuple(chains)


def protocol_grep_chains():
    source = Path(__file__).resolve().parents[1] / 'docs/protocol/execution.md'
    section = source.read_text(encoding='utf-8').split('### 3.7.1', 1)[1].split('### 3.7.2', 1)[0]
    return parse_protocol_section(section)


def parse_protocol_section(section):
    if section.count('```') != 2 or section.count('```bash') != 1:
        raise AssertionError('legacy sensitive-file code fence is not covered')
    block = section.split('```bash', 1)[1].split('```', 1)[0]
    return parse_grep_chains(block)


def legacy_sensitive(path, chains):
    return any(all(bool(re.search(pattern, path, (re.I | re.A) if ignore_case else 0)) != invert
                   for pattern, ignore_case, invert in chain) for chain in chains)


class SensitivePolicyTests(unittest.TestCase):
    def test_unrecognized_legacy_grep_syntax_cannot_be_silently_skipped(self):
        for command in ("git ls-files | grep -Ei '\\.kdbx$'",
                        "git ls-files | grep -i -E '\\.kdbx$'",
                        'git ls-files | grep -Ei "\\.kdbx$"'):
            with self.subTest(command=command), self.assertRaisesRegex(
                    AssertionError, 'syntax is not covered'):
                parse_grep_chains(command)

    def test_legacy_parser_rejects_unknown_command_or_option(self):
        commands = (
            "git ls-files | egrep -i '\\.kdbx$'",
            "git ls-files | rg '\\.kdbx$'",
            "git ls-files | awk '/\\.kdbx$/'",
            "git ls-files -- '*.kdbx'",
            "git ls-files | grep -iE '\\.kdbx$' -v",
            "git ls-files | grep -iE '\\.kdbx$' -e 'other'",
            "git ls-files | grep -iE '\\.kdbx$' -w",
            "git diff --cached --name-only | grep -iE '\\.kdbx$'",
        )
        for command in commands:
            with self.subTest(command=command), self.assertRaisesRegex(
                    AssertionError, 'syntax is not covered'):
                parse_grep_chains(command)

    def test_protocol_section_rejects_extra_or_changed_code_fences(self):
        original = "```bash\ngit ls-files | grep -iE '\\.pem$'\n```"
        self.assertEqual(len(parse_protocol_section(original)), 1)
        for section in (
                original + "\n```bash\ngit ls-files | grep -iE '\\.kdbx$'\n```",
                original + "\n```sh\ngit ls-files | grep -iE '\\.kdbx$'\n```",
                original + "\n```\ngit ls-files | grep -iE '\\.kdbx$'\n```",
                original.replace('```bash', '```sh')):
            with self.subTest(section=section), self.assertRaisesRegex(
                    AssertionError, 'code fence is not covered'):
                parse_protocol_section(section)

    def test_legacy_categories_and_protocol_rule_table_match(self):
        chains = protocol_grep_chains()
        self.assertEqual(chains, LEGACY_GREP_CHAINS)
        paths = [
            *('keys/file' + ext for ext in ('.pem', '.key', '.crt', '.cert', '.cer', '.p12',
                '.pfx', '.jks', '.keystore', '.ppk', '.asc', '.gpg', '.pgp')),
            '.env', '.env.production', 'config/.env.d/prod', 'production.env', 'app.example.env',
            '.env.example', '.env.sample', '.env.sample.local', 'app.example.env',
            'credential.txt', 'credentials.txt', 'secret.txt', 'secrets.txt', 'api-key',
            'api_key', 'apikey', 'auth-token', 'auth_token', 'authtoken', 'passwd', 'shadow',
            'secret.example.txt', 'secret.sample.json',
            *('keys/id_' + alg for alg in ('rsa', 'dsa', 'ecdsa', 'ed25519')),
            'service-account.json', 'service-account-prod.json', 'service-account.example.json',
            '.aws/credentials', '.gcloud/config.json',
            *('db/data' + ext for ext in ('.sqlite', '.sqlite3', '.db', '.dump', '.sql.gz')),
            'main.tfstate', 'main.tfstate.backup', 'dev.tfvars', 'dev.tfvars.sample',
            'a.tfstate.example', '.terraform/providers/x', '.Terraform/cache/x',
            'assets/app.js.map', 'assets/APP.MAP', 'logs/build.log', 'logs/build',
            'src/logs/a.ts', 'Logs/LogView.tsx', 'app/[id]/page.tsx',
            'docs/security-guide.md', 'src/readme.txt', 'pkg/tests/golden.md',
        ]
        paths.extend(path.upper() for path in tuple(paths))
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(sensitive_path_category(path) is not None,
                                 legacy_sensitive(path, chains))

    def test_malformed_or_git_ambiguous_paths_fail_closed(self):
        paths = ('../secret.pem', '/etc/passwd', 'docs//id_rsa', 'safe/../id_rsa',
                 '.git./config', 'safe.txt\nid_rsa', 'keys\\id_rsa',
                 ':(literal)id_rsa', 'a\x00.pem', 'a\x7f.pem')
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                sensitive_path_category(path)

    def test_every_legacy_alternation_and_exception_in_both_directions(self):
        chains = protocol_grep_chains()
        pairs = [(f'keys/file{ext}', f'keys/file{ext}x') for ext in
                 ('.pem', '.key', '.crt', '.cert', '.cer', '.p12', '.pfx', '.jks',
                  '.keystore', '.ppk', '.asc', '.gpg', '.pgp')]
        pairs += [
            ('.env', 'env.local'), ('.env.prod', 'env.prod'),
            ('production.env', 'production.envelope'),
            ('credential.txt', 'credenzial.txt'),
            ('credentials.txt', 'credenzials.txt'),
            ('secret.txt', 'secrt.txt'), ('secrets.txt', 'secrts.txt'),
            ('api-key.txt', 'api-kay.txt'), ('api_key.txt', 'api_kay.txt'),
            ('api.key.txt', 'api.kay.txt'), ('apikey.txt', 'apikay.txt'),
            ('auth-token.txt', 'auth-tokan.txt'), ('auth_token.txt', 'auth_tokan.txt'),
            ('auth.token.txt', 'auth.tokan.txt'), ('authtoken.txt', 'authtokan.txt'),
            ('passwd.txt', 'passwed.txt'), ('shadow.txt', 'shadaw.txt'),
            ('id_rsa', 'id_unknown'), ('id_dsa', 'id_unknown'),
            ('id_ecdsa', 'id_unknown'), ('id_ed25519', 'id_unknown'),
            ('service-account-prod.json', 'service-acount-prod.json'),
            ('.aws/file.txt', '.awss/file.txt'),
            ('.gcloud/config', '.gcloudx/config'),
            ('data.sqlite', 'data.sqlitex'), ('data.sqlite3', 'data.sqlite3x'),
            ('data.db', 'data.dbx'), ('data.dump', 'data.dumpx'),
            ('data.sql.gz', 'data.sql.gzip'),
            ('dev.tfstate', 'dev.tfstates'), ('dev.tfstate.backup', 'dev.tfstatex.backup'),
            ('dev.tfvars', 'dev.tfvarsx'), ('dev.tfvars.sample', 'dev.tfvarsx.sample'),
            ('.terraform/cache/x', '.terraformx/cache/x'),
            ('assets/app.js.map', 'assets/app.js.maps'),
            ('build.log', 'build.logs'), ('logs/build', 'logbook/build'),
        ]
        for positive, negative in pairs:
            with self.subTest(positive=positive, negative=negative):
                self.assertTrue(legacy_sensitive(positive, chains))
                self.assertIsNotNone(sensitive_path_category(positive))
                self.assertFalse(legacy_sensitive(negative, chains))
                self.assertIsNone(sensitive_path_category(negative))
        exceptions = (
            ('.env.example', False), ('.env.sample', False),
            ('.env.sample.local', False), ('secret.example.txt', False),
            ('credentials.sample.json', False), ('dev.tfstate.example', False),
            ('dev.tfvars.example', False), ('.env.ſample', True),
            ('credentials.ſample.json', True),
            ('myid_rsa', False), ('x.aws/file', False),
            ('catalogs/file', False), ('xservice-account.json', False),
            ('foo.env.local', False), ('credentials/readme.md', False),
            ('.env.samples', True), ('secrets.examples.txt', True),
            ('dev.tfstate.example.bak', True),
        )
        for path, expected in exceptions:
            with self.subTest(exception=path):
                self.assertEqual(legacy_sensitive(path, chains), expected)
                self.assertEqual(sensitive_path_category(path) is not None, expected)


if __name__ == '__main__':
    unittest.main()
