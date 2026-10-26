"""WI-WIN W1: paired_session.winplat on both platforms. The POSIX tests pin each helper's POSIX branch to the behaviour of the
code it replaced; the Windows tests (run by the windows-latest CI job, see windows-ci-modules.txt) cover the Windows branches,
importing every coordinator module, UTF-8/LF text artifacts, the real role manifest and the protocol loader."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paired_session import winplat

REPO = Path(__file__).resolve().parents[1]
POSIX = not winplat.IS_WINDOWS


class TempDirTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()


@unittest.skipUnless(POSIX, 'POSIX branches')
class PosixBranchTests(TempDirTest):
    def test_user_token_and_uid_are_the_getuid_values(self):
        self.assertEqual(winplat.user_token(), str(os.getuid()))
        self.assertEqual(winplat.uid(), os.getuid())
        self.assertIs(type(winplat.uid()), int)

    def test_fixed_paths_are_unchanged(self):
        self.assertEqual(winplat.temp_root(), Path('/tmp'))
        self.assertEqual(winplat.devnull_path(), '/dev/null')

    def test_check_private_dir_is_owner_and_mode(self):
        private = self.root / 'private'; private.mkdir(mode=0o700); private.chmod(0o700)
        self.assertTrue(winplat.check_private_dir(private))
        private.chmod(0o750)
        self.assertFalse(winplat.check_private_dir(private))
        self.assertTrue(winplat.check_private_dir(private, mask=0o022))   # the cache-root variant
        private.chmod(0o700)
        link = self.root / 'link'; link.symlink_to(private)
        self.assertFalse(winplat.check_private_dir(link))   # lstat: a symlink is not a directory
        regular = self.root / 'file'; regular.write_bytes(b''); regular.chmod(0o600)
        self.assertFalse(winplat.check_private_dir(regular))
        with patch('os.getuid', return_value=os.getuid() + 1):
            self.assertFalse(winplat.check_private_dir(private))
        with self.assertRaises(FileNotFoundError):
            winplat.check_private_dir(self.root / 'missing')

    def test_private_file_ok_is_owner_and_mode(self):
        regular = self.root / 'lease.lock'; regular.write_bytes(b'{}'); regular.chmod(0o600)
        self.assertTrue(winplat.private_file_ok(os.lstat(regular)))
        regular.chmod(0o640)
        self.assertFalse(winplat.private_file_ok(os.lstat(regular)))
        self.assertFalse(winplat.private_file_ok(os.lstat(self.root)))
        regular.chmod(0o600)
        self.assertTrue(winplat.owned(os.lstat(regular)))
        with patch('os.getuid', return_value=os.getuid() + 1):
            self.assertFalse(winplat.private_file_ok(os.lstat(regular)))
            self.assertFalse(winplat.owned(os.lstat(regular)))

    def test_pid_alive_is_kill_zero(self):
        self.assertTrue(winplat.pid_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, '-c', 'pass']); child.wait()
        self.assertFalse(winplat.pid_alive(child.pid))
        with patch('os.kill', side_effect=PermissionError) as kill:
            self.assertTrue(winplat.pid_alive(12345))   # another user's live process
        kill.assert_called_once_with(12345, 0)
        with patch('os.kill', side_effect=OSError(22, 'invalid')):
            with self.assertRaises(OSError):
                winplat.pid_alive(12345)   # as before: other errors reach the caller's own handler

    def test_fsync_dir_opens_the_directory(self):
        winplat.fsync_dir(self.root)
        with self.assertRaises(FileNotFoundError):
            winplat.fsync_dir(self.root / 'missing')

    def test_read_regular_file_nofollow_posix(self):
        regular = self.root / 'executor.md'; regular.write_bytes(b'body\r\n')
        self.assertEqual(winplat.read_regular_file_nofollow(regular), b'body\r\n')
        link = self.root / 'link.md'; link.symlink_to(regular)
        with self.assertRaisesRegex(ValueError, 'without following links'):
            winplat.read_regular_file_nofollow(link)
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            winplat.read_regular_file_nofollow(self.root)


class CrossPlatformTests(TempDirTest):
    def test_replace_moves_over_an_existing_file(self):
        src, dst = self.root / 'a.tmp', self.root / 'a'
        src.write_bytes(b'new'); dst.write_bytes(b'old')
        winplat.replace(src, dst)
        self.assertEqual(dst.read_bytes(), b'new')
        self.assertFalse(src.exists())

    def test_write_text_is_utf8_with_lf(self):
        target = self.root / 'note.txt'
        winplat.write_text(target, 'Ärger — 中文\nzwei\n')
        self.assertEqual(target.read_bytes(), 'Ärger — 中文\nzwei\n'.encode('utf-8'))

    def test_every_non_test_module_imports(self):
        names = sorted(path.stem for path in (REPO / 'paired_session').glob('*.py')
                       if not path.name.startswith('test_') and path.stem not in ('__init__', 'fake_cli'))
        self.assertIn('coordinator', names)
        for name in names:
            with self.subTest(module=name):
                importlib.import_module('paired_session.' + name)

    def test_work_item_text_round_trips_as_utf8_lf_bytes(self):
        from paired_session import coordinator as rc
        text = '# Arbeit — 中文 ✓\n\nZeile zwei: café\n'
        path = self.root / 'context' / 'workitem.md'
        rc.atomic_text(path, text)
        self.assertEqual(path.read_bytes(), text.encode('utf-8'))   # no CRLF, no code page
        self.assertEqual(path.read_text(encoding='utf-8'), text)
        value = {'title': text, 'n': 1}
        rc.atomic_json(self.root / 'state.json', value)
        self.assertEqual((self.root / 'state.json').read_bytes(),
                         (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode('utf-8'))   # the format is unchanged
        self.assertFalse((self.root / 'state.json.tmp').exists())

    def test_frozen_role_manifest_on_the_bundled_agents(self):
        from paired_session import coordinator as rc
        agents, gate = REPO / 'agents', rc.DEFAULT_GATE_PROMPT
        flags = {'author': {'vendor': 'codex'}, 'reviewer': {'vendor': 'claude'}}
        required = {'author': 'executor.md', 'reviewer': 'reviewer.md'}
        manifest = rc.frozen_role_manifest({}, flags, agents, gate, required)
        expected = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in agents.glob('*.md')}
        self.assertEqual(manifest['agent_body_sha256'], expected)
        self.assertEqual(manifest['gate_prompt_sha256'], hashlib.sha256(gate.read_bytes()).hexdigest())
        self.assertEqual(rc.frozen_role_manifest({}, flags, agents, gate, required), manifest)   # revalidation

    def test_protocol_bundle_builds_from_outside_the_repository(self):
        temp = self.root / 'systemtmp'; temp.mkdir()
        outside = self.root / 'elsewhere'; outside.mkdir()
        env = {**os.environ, 'TMPDIR': str(temp), 'TEMP': str(temp), 'TMP': str(temp)}
        script = REPO / 'scripts' / 'read_protocol.py'
        target = temp.resolve() / f'review-loop-protocol-{winplat.user_token()}' / 'bundle' / 'protocol.md'
        args = [sys.executable, str(script), '--runtime', 'claude', '--stage', 'entry-paired-session']
        written = subprocess.run([*args, '--output', str(target)], cwd=outside, env=env, capture_output=True, check=False)
        self.assertEqual(written.returncode, 0, written.stderr)
        report = json.loads(written.stdout)
        data = target.read_bytes()
        self.assertEqual(report['sha256'], hashlib.sha256(data).hexdigest())   # LF bytes: the hash matches the file
        self.assertNotIn(b'\r\n', data)
        printed = subprocess.run(args, cwd=outside, env=env, capture_output=True, check=False)
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertEqual(printed.stdout, data)
        self.assertEqual(list(outside.iterdir()), [])   # nothing written in the cwd


@unittest.skipUnless(winplat.IS_WINDOWS, 'Windows branches')
class WindowsBranchTests(TempDirTest):
    def test_user_token_and_fixed_paths(self):
        self.assertRegex(winplat.user_token(), r'\A[A-Za-z0-9._-]+\Z')
        self.assertEqual(winplat.uid(), winplat.user_token())
        self.assertEqual(winplat.temp_root(), Path(tempfile.gettempdir()))
        self.assertEqual(winplat.devnull_path(), os.devnull)

    def test_pid_alive_of_self_and_of_an_exited_child(self):
        self.assertTrue(winplat.pid_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, '-c', 'pass']); child.wait()
        self.assertFalse(winplat.pid_alive(child.pid))
        self.assertFalse(winplat.pid_alive(0))

    def test_fsync_dir_is_a_no_op(self):
        winplat.fsync_dir(self.root)

    def test_check_private_dir(self):
        self.assertTrue(winplat.check_private_dir(self.root))
        regular = self.root / 'file'; regular.write_bytes(b'')
        self.assertFalse(winplat.check_private_dir(regular))
        system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        self.assertFalse(winplat.check_private_dir(system))   # not under the profile or the temp directory
        self.assertTrue(winplat.private_file_ok(os.lstat(regular)))
        self.assertFalse(winplat.private_file_ok(os.lstat(self.root)))

    def test_read_regular_file_nofollow_windows(self):
        regular = self.root / 'executor.md'; regular.write_bytes(b'body\r\n')
        self.assertEqual(winplat.read_regular_file_nofollow(regular), b'body\r\n')
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            winplat.read_regular_file_nofollow(self.root)
        link = self.root / 'link.md'
        try:
            os.symlink(regular, link)
        except OSError:
            self.skipTest('symlink creation is not permitted here')
        with self.assertRaisesRegex(ValueError, 'without following links'):
            winplat.read_regular_file_nofollow(link)
        dir_link = self.root / 'dir-link'
        os.symlink(self.root, dir_link, target_is_directory=True)
        self.assertFalse(winplat.check_private_dir(dir_link))


if __name__ == '__main__':
    unittest.main()
