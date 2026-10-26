"""WI-WIN W9: UTF-8 pipe output without ambient Python encoding overrides."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from paired_session import winplat

REPO = Path(__file__).resolve().parents[1]
CHINESE = '中文任务：实现求和并验证结果。'


def clean_env():
    env = os.environ.copy()
    for key in ('PYTHONIOENCODING', 'PYTHONUTF8'):
        env.pop(key, None)
    return env


class StdioPolicyTests(unittest.TestCase):
    def test_windows_streams_encode_unicode_and_escape_surrogates(self):
        buffers = [io.BytesIO(), io.BytesIO()]
        streams = [io.TextIOWrapper(raw, encoding='cp1252') for raw in buffers]
        with mock.patch.object(winplat, 'IS_WINDOWS', True), \
                mock.patch.object(sys, 'stdout', streams[0]), mock.patch.object(sys, 'stderr', streams[1]):
            winplat.configure_stdio()
            for stream in streams:
                stream.write(CHINESE + '\udcff')
                stream.flush()
            encoded = [raw.getvalue() for raw in buffers]
        self.assertEqual(encoded, [(CHINESE + '\\udcff').encode('utf-8')] * 2)

    def test_posix_streams_are_not_touched(self):
        streams = [mock.Mock(), mock.Mock()]
        with mock.patch.object(winplat, 'IS_WINDOWS', False), \
                mock.patch.object(sys, 'stdout', streams[0]), mock.patch.object(sys, 'stderr', streams[1]):
            winplat.configure_stdio()
        for stream in streams:
            stream.reconfigure.assert_not_called()

    def test_replacement_streams_without_reconfigure(self):
        with mock.patch.object(winplat, 'IS_WINDOWS', True), \
                mock.patch.object(sys, 'stdout', io.StringIO()), mock.patch.object(sys, 'stderr', None):
            winplat.configure_stdio()


@unittest.skipUnless(os.name == 'nt', 'native Windows pipe encoding')
class WindowsPipeTests(unittest.TestCase):
    def run_pipe(self, args, env):
        result = subprocess.run([sys.executable, *args], cwd=REPO, env=env, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
        # Decode strictly: mojibake and ANSI bytes must not silently pass.
        return result.stdout.decode('utf-8'), result.stderr.decode('utf-8')

    def test_full_status_entry_with_chinese_task_and_run_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / 'workspace'
            workspace.mkdir()
            run = root / '张三的运行'
            run.mkdir()
            state = {'workitem': CHINESE, 'run_dir': str(run)}
            (run / 'state.json').write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            task = root / 'WORKITEM.md'
            task.write_text(CHINESE, encoding='utf-8')
            entry = str(REPO / 'bin/paired-session')
            args = [entry, 'status', '--workspace', str(workspace), '--workitem', str(task), '--run-dir', str(run)]
            env = clean_env()
            # Disable interpreter UTF-8 mode. ACP can itself be UTF-8 on a runner;
            # the second invocation forces cp1252 on the actual piped streams.
            normal, _ = self.run_pipe(['-X', 'utf8=0', *args], env)
            bootstrap = ('import runpy, sys; '
                         'sys.stdout.reconfigure(encoding="cp1252"); '
                         'sys.stderr.reconfigure(encoding="cp1252"); '
                         'sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")')
            forced, _ = self.run_pipe(['-X', 'utf8=0', '-c', bootstrap, *args], env)
            for output in (normal, forced):
                self.assertEqual(json.loads(output), state)
                self.assertIn(CHINESE, output)
                self.assertIn('张三的运行', output)

    def test_documented_bundle_directory_snippet_with_unicode_temp_path(self):
        line = next(line for line in (REPO / 'skills/paired-session/SKILL.md').read_text(encoding='utf-8').splitlines()
                    if line.startswith('`python -X utf8 -c '))
        snippet = line.split('-c "', 1)[1].rsplit('"`', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / '张三'
            profile.mkdir()
            env = clean_env()
            env.update(TEMP=str(profile), TMP=str(profile))
            output, _ = self.run_pipe(['-X', 'utf8', '-c', snippet], env)
            self.assertIn('张三', output)
            self.assertTrue(output.strip().startswith(profile.as_posix() + '/review-loop-protocol-'))


if __name__ == '__main__':
    unittest.main()
