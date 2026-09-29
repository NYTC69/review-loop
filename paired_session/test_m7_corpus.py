"""Tests for scripts/m7_corpus.py against a throwaway git repo in a temp dir."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'm7_corpus.py'
KEY_TEXT = 'return total_without_bounds_check(items)'


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
        self.key.write_text(f'seeded blocker in a.py line 2\n    {KEY_TEXT}\n')
        self.pin = self.tmp / 'plugin'
        self.pin.mkdir()
        (self.pin / 'README.md').write_text('an unrelated tool source file\n')
        self.out = self.tmp / 'out'

    def run_tool(self, diff_sha=None, key=None, pins=None):
        manifest = {'repo': str(self.repo), 'out_root': str(self.out), 'pins': pins or [{'dir': str(self.pin)}],
                    'cases': [{'id': 'c01', 'base': self.base, 'diff_path': str(self.diff_path),
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
        (self.pin / 'skill.md').write_text('notes\n\treturn   total_without_bounds_check(items)  \n')  # whitespace differs
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.loads((self.out / 'result.json').read_text())['cases'][0]
        self.assertEqual(result['status'], 'excluded')
        self.assertIn('answer-key text', result['reason'])
        self.assertFalse((self.out / 'c01').exists())

    def test_key_hit_in_pinned_commit_is_excluded(self):
        (self.repo / 'leak.py').write_text(f'x = 1\n{KEY_TEXT}\n')
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
            r = self.run_tool(pins=[{'dir': str(self.pin)}, pin])  # a good pin must not hide a bad one
            self.assertNotEqual(r.returncode, 0, pin)
            self.assertFalse((self.out / 'c01').exists(), pin)
            self.assertFalse((self.out / 'result.json').exists(), pin)

    def test_failure_after_clone_removes_case_dir(self):
        self.base = '0' * 40  # valid hash, unknown commit: clone succeeds, checkout fails
        r = self.run_tool()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.out / 'c01').exists())


if __name__ == '__main__':
    unittest.main()
