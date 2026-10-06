"""igncache (does not reproduce): a read-only role whose allowed test command runs Python tests while an ignored `__pycache__/`
and `.pytest_cache/` already exist (git lists each collapsed, as one entry the eff-e ignored-entry check compares by lstat).
The turn's verdict stands and nothing is voided: every CLI child gets PYTHONDONTWRITEBYTECODE=1 from cli_env(), so the command
writes no byte code, and pytest's own cache writes land inside `.pytest_cache/v/cache`, which leaves the collapsed entry unchanged.
Residual (not changed here): a command that re-enables byte code or adds a direct entry to an existing ignored directory still
voids the verdict."""
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from paired_session import readonly_guard as rg
from paired_session import test_real_coordinator as trc
from paired_session import timeout_scale as tsc

rc = trc.rc   # the module the harness's Coordinator comes from
PYTEST = importlib.util.find_spec('pytest')
SITE = {'PYTHONPATH': os.path.dirname(os.path.dirname(PYTEST.origin))} if PYTEST else {}   # the harness moves HOME (user site)
FIXTURE = ('import os\nimport unittest\n\n\nclass CacheFixtureTest(unittest.TestCase):\n    def test_fixture(self):\n'
           "        if os.environ.get('IGNCACHE_MARK'): open(os.environ['IGNCACHE_MARK'], 'a').write('ran\\n')\n")


class IgnoredCacheTests(unittest.TestCase):
    def setUp(self):
        self.h = trc.RealCoordinatorTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.addCleanup(self.h.tearDown)
        self.ws = self.h.workspace
        with (self.ws / '.git' / 'info' / 'exclude').open('a') as handle:
            handle.write('.pytest_cache/\n')
        (self.ws / 'test_cache_fixture.py').write_text(FIXTURE)
        subprocess.run(['git', 'add', 'test_cache_fixture.py'], cwd=self.ws, check=True)
        subprocess.run(['git', 'commit', '-qm', 'fixture'], cwd=self.ws, check=True)

    def run_tests(self, command):   # a developer's earlier local run, byte code on
        done = subprocess.run(command, cwd=self.ws, capture_output=True, text=True, env={**os.environ, **SITE, 'PYTHONDONTWRITEBYTECODE': ''})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def review(self, command, mark):   # one PLAN review whose fake CLI runs the allowed test command in the workspace
        co = self.h.coordinator('--timeout', tsc.scaled_arg(10), '--test-command', shlex.join(command))
        (co.context / 'plan.md').write_text('Plan: inspect existing tracked source.')
        with patch.dict(os.environ, {**SITE, 'IGNCACHE_MARK': str(mark), 'FAKE_PLAN_REVIEWER_RUN_TEST': shlex.join(command)}):
            co.invoke('reviewer', 'PLAN', co._review_prompt('reviewer', 'snapshot'), rc.review_schema())   # the child env is cli_env()'s
        [turn] = [t for t in co.state['turns'] if t.get('role') == 'reviewer']
        return turn

    def test_the_allowed_test_command_with_existing_ignored_caches_keeps_the_verdict(self):
        self.assertEqual(rc.cli_env()['PYTHONDONTWRITEBYTECODE'], '1')   # what keeps byte code out of the workspace
        python = [sys.executable, '-X', 'pycache_prefix=']   # byte code next to the source even where the build sets a prefix
        for label, command, caches in (('unittest', [*python, '-m', 'unittest'], ['__pycache__']),
                                       ('pytest', [*python, '-m', 'pytest', '-q'], ['__pycache__', '.pytest_cache'])):
            with self.subTest(command=label):
                if label == 'pytest' and not PYTEST: self.skipTest('pytest is not installed')
                self.h.run_dir = self.h.root / ('igncache-' + label)
                for cache in ('__pycache__', '.pytest_cache'): shutil.rmtree(self.ws / cache, ignore_errors=True)
                self.run_tests(command)   # the caches exist before the turn
                with (self.ws / 'test_cache_fixture.py').open('a') as handle:   # the author's edit: the byte code is stale now
                    handle.write('\n\nclass SecondTest(unittest.TestCase):\n    def test_second(self):\n        pass\n')
                recorded = rg.capture(self.ws, self.h.root / ('keep-' + label))
                self.assertEqual(sorted(recorded['ignored_meta']), sorted(caches))   # collapsed: one entry each
                pycache = os.stat(self.ws / '__pycache__').st_mtime_ns
                mark = self.h.root / ('ran-' + label)
                turn = self.review(command, mark)   # no exception: the verdict stands
                self.assertEqual(mark.read_text(), 'ran\n')   # the command ran inside the turn
                self.assertNotIn('voided', turn)
                self.assertEqual(rg.ignored_changes(self.ws, recorded), [])
                self.assertEqual(os.stat(self.ws / '__pycache__').st_mtime_ns, pycache)   # no byte code written
                if label == 'pytest':   # pytest did write its cache, inside the collapsed directory
                    self.assertIn('SecondTest', (self.ws / '.pytest_cache' / 'v' / 'cache' / 'nodeids').read_text())
                subprocess.run(['git', 'checkout', '-q', '--', 'test_cache_fixture.py'], cwd=self.ws, check=True)


if __name__ == '__main__':
    unittest.main()
