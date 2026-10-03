"""Tests for scripts/m7_corpus.py against a throwaway git repo in a temp dir."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'm7_corpus.py'
KEY_TEXT = 'return total_without_bounds_check(items)'
KEY_UNIT = 'return total_with_bounds_check(items)'  # the fix text: in the key, never in the frozen case


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
                               'GIT_COMMITTER_EMAIL': 't@t', 'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin',
                               'HOME': str(cwd)}).stdout.strip()


class M7CorpusTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / 'src'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        (self.repo / 'a.py').write_text('def f(items):\n    return len(items)\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'base')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        (self.repo / 'a.py').write_text(f'def f(items):\n    {KEY_TEXT}\n')
        self.diff = git(self.repo, 'diff') + '\n'
        git(self.repo, 'checkout', '-q', '--', 'a.py')
        self.diff_path = self.tmp / 'c01.diff'
        self.diff_path.write_text(self.diff)
        (self.tmp / 'keys').mkdir()
        self.key = self.tmp / 'keys' / 'c01.txt'
        self.key.write_text(f'seeded blocker in a.py line 2\n    {KEY_UNIT}\n')
        self.pin = self.tmp / 'plugin'
        self.pin.mkdir()
        (self.pin / 'README.md').write_text('an unrelated tool source file\n')
        self.out = self.tmp / 'out'

    def dir_pin(self, path=None):
        path = Path(path or self.pin)
        lines = sorted(f'{f.relative_to(path)} {hashlib.sha256(f.read_bytes()).hexdigest()}'
                       for f in path.rglob('*') if f.is_file() and not f.is_symlink())
        return {'dir': str(path), 'expected_sha256': hashlib.sha256('\n'.join(lines).encode()).hexdigest()}

    def run_tool(self, diff_sha=None, key=None, pins=None, base=None):
        manifest = {'repo': str(self.repo), 'out_root': str(self.out), 'pins': pins or [self.dir_pin()],
                    'cases': [{'id': 'c01', 'base': base or self.base, 'diff_path': str(self.diff_path),
                               'diff_sha256': diff_sha or hashlib.sha256(self.diff.encode()).hexdigest(),
                               'key_path': str(key or self.key)}]}
        path = self.tmp / 'manifest.json'
        path.write_text(json.dumps(manifest))
        return subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)

    def test_hash_mismatch_fails_and_leaves_no_case(self):
        r = self.run_tool(diff_sha='0' * 64)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('mismatch', r.stderr)
        self.assertFalse((self.out / 'c01').exists())
        self.assertFalse((self.out / 'result.json').exists())

    def test_ok_case_has_no_git_and_no_key(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        case = self.out / 'c01'
        self.assertFalse(any(case.rglob('.git')))
        self.assertIn(KEY_TEXT, (case / 'a.py').read_text())  # the frozen diff was applied
        key_bytes = self.key.read_bytes()
        self.assertFalse(any(p.is_file() and p.read_bytes() == key_bytes for p in case.rglob('*')))
        result = json.loads((self.out / 'result.json').read_text())['cases'][0]
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['diff_sha256'], hashlib.sha256(self.diff.encode()).hexdigest())
        self.assertTrue(self.key.exists())

    def test_key_hit_in_pinned_dir_is_excluded(self):
        (self.pin / 'skill.md').write_text('notes\n\treturn   total_with_bounds_check(items)  \n')  # whitespace differs
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.loads((self.out / 'result.json').read_text())['cases'][0]
        self.assertEqual(result['status'], 'excluded')
        self.assertIn('answer-key text', result['reason'])
        self.assertFalse((self.out / 'c01').exists())

    def test_key_hit_in_pinned_commit_is_excluded(self):
        (self.repo / 'leak.py').write_text(f'x = 1\n{KEY_UNIT}\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-qm', 'leak')
        pins = [{'repo': str(self.repo), 'commit': git(self.repo, 'rev-parse', 'HEAD')}]
        self.assertEqual(self.run_tool(pins=pins).returncode, 0)
        self.assertEqual(json.loads((self.out / 'result.json').read_text())['cases'][0]['status'], 'excluded')

    def test_key_inside_repo_or_case_dir_is_refused(self):
        inside = self.repo / 'key.txt'
        inside.write_text(f'{KEY_TEXT}\n')
        r = self.run_tool(key=inside)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('key_path', r.stderr)
        self.assertFalse((self.out / 'c01').exists())

    def test_out_root_inside_git_repo_is_refused(self):
        self.out = self.repo / 'out'
        r = self.run_tool()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('out_root', r.stderr)

    def test_bad_empty_or_missing_pin_is_refused(self):
        (self.tmp / 'empty').mkdir()
        for pin in ({'dir_': str(self.pin)}, {'dir': str(self.tmp / 'empty')}, {'dir': str(self.tmp / 'nope')},
                    {'repo': str(self.repo), 'sha': self.base}):
            r = self.run_tool(pins=[self.dir_pin(), pin])  # a good pin must not hide a bad one
            self.assertNotEqual(r.returncode, 0, pin)
            self.assertFalse((self.out / 'c01').exists(), pin)
            self.assertFalse((self.out / 'result.json').exists(), pin)

    def test_failure_after_clone_removes_case_dir(self):
        self.base = '0' * 40  # valid hash, unknown commit: clone succeeds, checkout fails
        r = self.run_tool()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.out / 'c01').exists())

    def assert_refused(self, r, needle):
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(needle, r.stderr)
        self.assertFalse((self.out / 'c01').exists())
        self.assertFalse((self.out / 'result.json').exists())

    def test_c1_key_text_in_frozen_case_is_excluded(self):
        key = self.tmp / 'keys' / 'c01-case.txt'
        key.write_text(f'seeded blocker in a.py line 2\n    {KEY_TEXT}\n')  # present in the patched a.py
        self.assertEqual(self.run_tool(key=key).returncode, 0)
        result = json.loads((self.out / 'result.json').read_text())['cases'][0]
        self.assertEqual((result['status'], result['reason'], result['location']),
                         ('excluded', 'key text in frozen case', 'frozen case'))
        self.assertFalse((self.out / 'c01').exists())

    def test_c2_result_json_holds_no_key_text_only_counts(self):
        (self.pin / 'skill.md').write_text(f'{KEY_UNIT}\n')
        self.assertEqual(self.run_tool().returncode, 0)
        raw = (self.out / 'result.json').read_text()
        self.assertNotIn('total_with_bounds_check', raw)
        self.assertNotIn('matched_units', raw)
        case = json.loads(raw)['cases'][0]
        self.assertEqual((case['status'], case['matched_count'], case['location']), ('excluded', 1, 'pinned source'))

    def test_c3_repo_pin_needs_full_commit_sha(self):
        pin = {'repo': str(self.repo), 'commit': 'HEAD'}
        self.assert_refused(self.run_tool(pins=[pin]), 'full 40-hex SHA')

    def test_c3_dir_pin_needs_matching_digest(self):
        self.assert_refused(self.run_tool(pins=[{'dir': str(self.pin), 'expected_sha256': '0' * 64}]),
                            'expected_sha256 mismatch')
        self.assert_refused(self.run_tool(pins=[{'dir': str(self.pin)}]), 'dir+expected_sha256')

    def test_c3_dir_pin_with_symlink_is_refused(self):
        pin = self.dir_pin()  # digest of the regular files, so only the symlink can fail
        os.symlink(self.pin / 'README.md', self.pin / 'link.md')
        self.assert_refused(self.run_tool(pins=[pin]), 'symlink in pinned dir')

    def test_c3_dir_pin_with_unreadable_file_is_refused(self):
        secret = self.pin / 'secret.md'
        secret.write_text('unreadable notes\n')
        pin = self.dir_pin()  # digest taken while readable, so only the mode can fail
        secret.chmod(0)
        self.addCleanup(secret.chmod, 0o644)
        if os.access(secret, os.R_OK):
            self.skipTest('running with privileges that ignore file modes')
        self.assert_refused(self.run_tool(pins=[pin]), 'unreadable file')

    def test_c4_case_base_needs_full_sha(self):
        self.assert_refused(self.run_tool(base='HEAD'), 'base must be a full 40-hex SHA')

    def test_valid_manifest_freezes_and_records_identities(self):
        commit = git(self.repo, 'rev-parse', 'HEAD')
        pins = [self.dir_pin(), {'repo': str(self.repo), 'commit': commit}]
        self.assertEqual(self.run_tool(pins=pins).returncode, 0)
        result = json.loads((self.out / 'result.json').read_text())
        case = result['cases'][0]
        self.assertEqual((case['status'], case['base_tree']), ('ok', git(self.repo, 'rev-parse', self.base + '^{tree}')))
        self.assertEqual(result['pins'], [pins[0], {**pins[1], 'tree': git(self.repo, 'rev-parse', commit + '^{tree}')}])
        self.assertIn(KEY_TEXT, (self.out / 'c01' / 'a.py').read_text())
        self.assertFalse(any((self.out / 'c01').rglob('.git')))


if __name__ == '__main__':
    unittest.main()
