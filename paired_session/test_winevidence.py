"""W4c: provider command evidence on Windows (PowerShell / Git Bash wrappers, Windows paths); every POSIX form pinned.

The samples under testdata/windows/ are modeled on the Codex exec JSONL, the Codex rollout and the Claude stream-json formats;
they are to be replaced by (or confirmed against) samples captured on a real Windows machine.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import tempfile
from typing import Optional
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlparse

from paired_session import coordinator as rc, winplat

DATA = Path(__file__).resolve().parent / 'testdata' / 'windows'
TEST = 'python -m unittest -q'
SESSION = '019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b'
WORKSPACE = 'c:\\users\\yuan\\work\\review loop'
PS = 'C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe'
PWSH = 'C:\\Program Files\\PowerShell\\7\\pwsh.exe'
GIT_BASH = 'C:\\Program Files\\Git\\bin\\bash.exe'


def rows_of(name: str) -> list:
    return [json.loads(line) for line in (DATA / name).read_text(encoding='utf-8').splitlines() if line.strip()]


def codex_rows(*ids) -> list:
    """The exec stream with only the command items `ids` (thread, turn, agent message and usage rows kept)."""
    return [row for row in rows_of('codex-exec-powershell.jsonl')
            if row.get('item', {}).get('type') != 'command_execution' or row['item']['id'] in ids]


def collect(vendor: str, rows: list) -> tuple:
    """The coordinator's own collector over a stdout file holding `rows`: (answer, observed commands, tool calls)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'stdout.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        answer, _, _, _, commands, calls = rc.Coordinator.__new__(rc.Coordinator)._collect(vendor, path)
    return answer, commands, calls


def approves(commands: list) -> bool:
    """The EXEC approval condition of _invoke_once: an observed successful run of the configured test."""
    return any(rc.observed_test_succeeded(row, TEST) for row in commands)


# --- the code before W4c, verbatim, for the POSIX pins -----------------------------------------------------------------------
def old_unwrap(raw_command: str) -> str:
    command = raw_command
    try:
        outer = shlex.split(raw_command)
        if len(outer) == 3 and outer[0].endswith(('sh', 'bash', 'zsh')) and outer[1] in ('-c', '-lc'):
            command = outer[2]
    except ValueError:
        pass
    return command


def old_argv_text(argv: list) -> str:
    return argv[2] if len(argv) == 3 and argv[0].endswith(('sh', 'bash', 'zsh')) and argv[1] in ('-c', '-lc') else shlex.join(argv)


def old_path_of(value) -> Optional[str]:
    url = urlparse(value) if isinstance(value, str) else None
    path = unquote(url.path) if url and url.scheme == 'file' and url.netloc in ('', 'localhost') else value if url and not url.scheme else None
    return path if isinstance(path, str) and os.path.isabs(path) else None


class RolloutHome:
    """A CODEX_HOME whose sessions/ holds one rollout for SESSION; codex_rollout_cwds reads it for 08:15:00Z .. +5 s."""

    START = datetime.fromisoformat('2026-10-27T08:15:00+00:00').timestamp()

    def __init__(self, rows: list):
        self.root = Path(tempfile.mkdtemp())
        sessions = self.root / 'sessions' / '2026' / '10' / '27'
        sessions.mkdir(parents=True)
        (sessions / f'rollout-2026-10-27T08-15-00-{SESSION}.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def cwds(self) -> Optional[dict]:
        try:
            with patch.dict(os.environ, {'CODEX_HOME': str(self.root)}):
                return rc.codex_rollout_cwds(SESSION, self.START, self.START + 5)
        finally:
            shutil.rmtree(self.root, ignore_errors=True)


@patch.object(winplat, 'IS_WINDOWS', True)
class CodexWindowsEventTests(unittest.TestCase):
    """The modeled Codex exec stream: the reviewer's own PowerShell-wrapped test run is self-run evidence; nothing else is."""

    def test_the_reviewers_wrapped_run_of_the_configured_test_is_recognized(self):
        for item in ('item_1', 'item_2', 'item_3'):   # powershell.exe -Command; pwsh -NoProfile + bash -lc; Windows quoting + & bash.exe
            with self.subTest(item=item):
                answer, commands, calls = collect('codex', codex_rows(item))
                self.assertEqual(answer['self_run_evidence'], [TEST])
                self.assertEqual([(row['command'], row['exit_code'], row['error']) for row in commands], [(TEST, 0, False)])
                self.assertNotEqual(commands[0]['raw_command'], TEST)   # the receipt keeps the wrapper as observed
                self.assertEqual(calls, [{'tool': 'command_execution', 'input': {'command': TEST}, 'error': False}])
                self.assertTrue(approves(commands))

    def test_a_different_command_or_an_unrecognized_wrapper_is_not(self):
        for item, command in (('item_0', 'Get-ChildItem -Name'), ('item_4', TEST + ' tests.test_one'),
                              ('item_8', TEST + '; echo done')):
            with self.subTest(item=item):
                commands = collect('codex', codex_rows(item))[1]
                self.assertEqual(commands[0]['command'], command)
                self.assertFalse(approves(commands))
        for item in ('item_6', 'item_7'):   # cmd.exe /c, powershell -EncodedCommand: kept raw, so never the configured test
            with self.subTest(item=item):
                commands = collect('codex', codex_rows(item))[1]
                self.assertEqual(commands[0]['command'], commands[0]['raw_command'])
                self.assertFalse(approves(commands))
                self.assertEqual(rc.configured_test_shortfall(commands, TEST), ('', False))

    def test_a_failed_run_keeps_its_exit_code_and_output_and_is_a_completed_failure(self):
        commands, calls = collect('codex', codex_rows('item_5'))[1:]
        row = commands[0]
        self.assertEqual((row['command'], row['exit_code'], row['error'], row['source']), (TEST, 1, True, 'command_execution'))
        self.assertIn('FAILED (failures=1)', row['output'])
        self.assertTrue(calls[0]['error'])
        self.assertFalse(approves(commands))
        self.assertFalse(rc.command_not_completed(row))
        self.assertIs(rc.completed_test_failure(commands, TEST), row)

    def test_a_started_run_without_completion_is_unfinished_evidence(self):
        commands = collect('codex', codex_rows('item_9'))[1]
        self.assertEqual((commands[0]['command'], commands[0]['exit_code']), (TEST, None))
        self.assertFalse(approves(commands))
        self.assertEqual(rc.configured_test_shortfall(commands, TEST), ('', True))
        self.assertIsNone(rc.completed_test_failure(commands, TEST))

    def test_one_success_among_failures_and_other_commands_approves_as_on_posix(self):
        commands = collect('codex', codex_rows(*(f'item_{n}' for n in range(10))))[1]
        self.assertEqual(len(commands), 10)
        self.assertTrue(approves(commands))

    def test_the_rollout_proves_the_windows_cwd_of_wrapped_commands(self):
        rollout = RolloutHome(rows_of('codex-rollout-powershell.jsonl')).cwds()
        self.assertEqual(rollout['cwd'], WORKSPACE)   # C:\..., c:\... and file:///C:/... spellings are one directory
        self.assertEqual(rollout['commands'], [
            ([PS, '-Command', TEST], WORKSPACE),
            ([PWSH, '-NoProfile', '-Command', "bash -lc 'cat README.md'"], WORKSPACE),
            ([PS, '-Command', 'Get-ChildItem -Name'], WORKSPACE)])
        calls = collect('codex', codex_rows('item_0', 'item_1'))[2]
        guarded, cwd, proven = rc.codex_guard_calls(calls, rollout)
        self.assertEqual((cwd, proven), (Path(WORKSPACE), 2))
        self.assertEqual([call['input'] for call in guarded], [{'command': 'Get-ChildItem -Name', 'workdir': WORKSPACE},
                                                               {'command': TEST, 'workdir': WORKSPACE}])


class ClaudeWindowsStreamTests(unittest.TestCase):
    """Claude Code on Windows runs Bash through Git Bash: the tool_use input is the raw command, as on macOS."""

    def test_the_bash_tool_pair_is_self_run_evidence_on_both_platforms(self):
        for flag in (True, False):
            with self.subTest(windows=flag), patch.object(winplat, 'IS_WINDOWS', flag):
                answer, commands, calls = collect('claude', rows_of('claude-stream-bash.jsonl'))
                self.assertEqual(answer['self_run_evidence'], [TEST])
                self.assertEqual([(row['command'], row['exit_code'], row['error']) for row in commands],
                                 [(TEST, 0, False), (TEST + ' tests.test_one', 0, False), (TEST, -1, True)])
                self.assertTrue(rc.observed_test_succeeded(commands[0], TEST))
                self.assertFalse(any(rc.observed_test_succeeded(row, TEST) for row in commands[1:]))
                self.assertEqual([call['tool'] for call in calls], ['Read', 'Grep', 'Bash', 'Bash', 'Bash'])
                self.assertEqual(calls[0]['input']['file_path'], 'C:\\Users\\Yuan\\work\\review loop\\paired_session\\winplat.py')


class WrapperTests(unittest.TestCase):
    """winplat.windows_wrapped_command and its two tokenizers (pure: every OS)."""

    def test_powershell_and_git_bash_wrappers(self):
        for argv, inner in (
                ([PS, '-Command', TEST], TEST),
                (['powershell', '-c', TEST], TEST),
                (['pwsh.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', TEST], TEST),
                (['/usr/bin/pwsh', '-noprofile', '-COMMAND', TEST], TEST),
                ([PWSH, '-Command', f"bash -lc '{TEST}'"], TEST),
                ([PS, '-Command', f'bash -c "{TEST}"'], TEST),
                ([PS, '-Command', f"& '{GIT_BASH}' -lc '{TEST}'"], TEST),
                ([PS, '-Command', 'bash -c "pytest -k `"a b`""'], 'pytest -k "a b"'),
                ([PS, '-Command', "bash -lc 'it''s'"], "it's"),
                ([PS, '-Command', f'bash -lc "{TEST}" 2>&1'], f'bash -lc "{TEST}" 2>&1'),   # not a plain nested call: kept whole
                ([PS, '-Command', 'bash -lc "pytest $env:ARGS"'], 'bash -lc "pytest $env:ARGS"'),
                ([GIT_BASH, '-lc', TEST], TEST),
                (['C:/Program Files/Git/usr/bin/sh.exe', '-c', TEST], TEST)):
            with self.subTest(argv=argv):
                self.assertEqual(winplat.windows_wrapped_command(argv), inner)

    def test_anything_else_stays_unrecognized(self):
        for argv in ([], [PS], [PS, '-Command'], [PS, '-Command', TEST, 'extra'], [PS, '-EncodedCommand', 'cAB5AA=='],
                     [PS, '-File', 'run.ps1'], [PS, '-WindowStyle', 'Hidden', '-Command', TEST], [PS, '-ExecutionPolicy', '-Command'],
                     ['cmd.exe', '/c', TEST], [GIT_BASH, '-x', TEST], [GIT_BASH, '-lc', TEST, 'extra'], ['fish', '-c', TEST],
                     [PS, '-Command', None], 'powershell -Command x', None):
            with self.subTest(argv=argv):
                self.assertIsNone(winplat.windows_wrapped_command(argv))

    def test_windows_command_line_split(self):
        self.assertEqual(winplat.windows_split(f'{PS} -Command "& \'{GIT_BASH}\' -lc \\"{TEST}\\""'),
                         [PS, '-Command', f'& \'{GIT_BASH}\' -lc "{TEST}"'])
        self.assertEqual(winplat.windows_split('a\\\\b "c d" e\\\\"f g" h\\"i "" "j""k"'),
                         ['a\\\\b', 'c d', 'e\\f g', 'h"i', '', 'j"k'])
        self.assertEqual(winplat.windows_split('  '), [])
        self.assertEqual(winplat.windows_split(None), [])

    def test_powershell_words(self):
        self.assertEqual(winplat.powershell_words("& 'C:\\a b\\bash.exe' -lc 'x y'"), ['&', 'C:\\a b\\bash.exe', '-lc', 'x y'])
        self.assertEqual(winplat.powershell_words('a`"b "c`td" ""'), ['a"b', 'c\td', ''])
        for text in ('bash -lc "x', "bash -lc 'x", 'a; b', 'a | b', '$x', 'a&b', 'a\nb', 'x`', None):
            with self.subTest(text=text):
                self.assertIsNone(winplat.powershell_words(text))


class WindowsPathTests(unittest.TestCase):
    """winplat.windows_abs_path, the Windows branch of codex_rollout_cwds' path_of (pure: every OS)."""

    def test_absolute_windows_spellings_normalize_to_one_case_insensitive_path(self):
        for value in ('C:\\Users\\Yuan\\work\\review loop', 'C:/Users/Yuan/work/review loop', 'c:\\USERS\\yuan\\work\\review loop\\',
                      '/c/Users/Yuan/work/review loop', 'file:///C:/Users/Yuan/work/review%20loop',
                      'file://localhost/c:/Users/Yuan/work/review%20loop', 'C:\\Users\\Yuan\\work\\x\\..\\review loop'):
            with self.subTest(value=value):
                self.assertEqual(winplat.windows_abs_path(value), WORKSPACE)
        self.assertEqual(winplat.windows_abs_path('/c'), 'c:\\')
        self.assertEqual(winplat.windows_abs_path('\\\\server\\share\\repo'), '\\\\server\\share\\repo')

    def test_anything_else_is_no_path(self):
        for value in ('repo', 'C:repo', '/tmp/x', '/Users/yuan/repo', 'file:///tmp/x', 'file://host/C:/x', 'https://x/C:/y',
                      '', None, 7):
            with self.subTest(value=value):
                self.assertIsNone(winplat.windows_abs_path(value))

    def test_the_rollout_path_of_uses_the_windows_branch_only_on_windows(self):
        rows = rows_of('codex-rollout-powershell.jsonl')
        with patch.object(winplat, 'IS_WINDOWS', False):   # POSIX: the pre-W4c reading of the same Windows spellings
            rollout = RolloutHome(rows).cwds()
        items = [row['payload']['item'] for row in rows[3:]]
        self.assertEqual(rollout, {'cwd': None, 'commands': [(item['command'], old_path_of(item['cwd'])) for item in items]})
        self.assertEqual([path for _, path in rollout['commands']], ['/C:/Users/Yuan/work/review loop', None, '/c/Users/Yuan/work/review loop'])


@patch.object(winplat, 'IS_WINDOWS', False)
class PosixPinTests(unittest.TestCase):
    """On POSIX every unwrapping and path decision is the pre-W4c code's, Windows samples included."""

    RAW = ["/bin/zsh -lc 'python3 -m unittest -v'", 'bash -c "a b"', 'sh -lc x', '/bin/bash -lc x y', 'pwsh -c x', 'zsh -x x',
           "bash -lc 'unclosed", '', 'python3 -m unittest', 'env A=1 bash -lc x', '/bin/fish -c x', 'xbash -c y',
           f'"{PS}" -Command "{TEST}"', f'{PS} -Command "{TEST}"', f'"{PWSH}" -NoProfile -Command "bash -lc \'{TEST}\'"']

    def test_command_unwrapping_is_unchanged(self):
        raws = self.RAW + [row['item']['command'] for row in rows_of('codex-exec-powershell.jsonl')
                           if row.get('item', {}).get('type') == 'command_execution']
        for raw in raws:
            with self.subTest(raw=raw):
                self.assertEqual(rc.unwrap_command(raw), old_unwrap(raw))
                rows = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'command': raw, 'exit_code': 0}},
                        {'type': 'item.started', 'item': {'id': 'x', 'type': 'command_execution', 'command': raw}}]
                commands, calls = rc.observed_events('codex', rows)
                self.assertEqual([row['command'] for row in commands], [old_unwrap(raw)] * 2)
                self.assertEqual([call['input']['command'] for call in calls], [old_unwrap(raw)] * 2)

    def test_rollout_argv_text_is_unchanged(self):
        argvs = [['/bin/zsh', '-lc', 'cat a'], ['bash', '-c', 'x'], ['/bin/zsh', '-lc', 'a', 'b'], ['python3', '-m', 'x y'],
                 ['pwsh', '-c', 'x'], [PS, '-Command', TEST], [GIT_BASH, '-lc', TEST]]
        argvs += [row['payload']['item']['command'] for row in rows_of('codex-rollout-powershell.jsonl')[3:]]
        proof = {'cwd': '/w', 'commands': [(argv, '/w') for argv in argvs]}
        for argv in argvs:
            with self.subTest(argv=argv):
                calls = [{'tool': 'command_execution', 'input': {'command': old_argv_text(argv)}, 'error': False}]
                guarded, cwd, proven = rc.codex_guard_calls(calls, proof)
                self.assertEqual((guarded[0]['input'].get('workdir'), proven), ('/w', 1))
                inner = rc.shell_wrapped(argv)
                self.assertEqual(shlex.join(argv) if inner is None else inner, old_argv_text(argv))

    def test_path_of_is_unchanged(self):
        for value in ('/Users/yuan/repo', 'file:///Users/yuan/review%20loop', 'file://localhost/tmp/x', 'file://host/tmp/x',
                      'relative', 'C:\\Users\\Yuan', 'C:/Users/Yuan', '/c/Users/Yuan', 'file:///C:/Users/Yuan', 'https://x/y', '', None):
            with self.subTest(value=value):
                rows = [{'timestamp': '2026-10-27T08:15:01Z', 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'T'}},
                        {'timestamp': '2026-10-27T08:15:01Z', 'type': 'turn_context', 'payload': {'cwd': value}},
                        {'timestamp': '2026-10-27T08:15:02Z', 'type': 'event_msg', 'payload': {
                            'type': 'item_completed', 'turn_id': 'T', 'item': {'type': 'CommandExecution', 'command': ['ls'], 'cwd': value}}}]
                self.assertEqual(RolloutHome(rows).cwds(), {'cwd': old_path_of(value), 'commands': [(['ls'], old_path_of(value))]})


@unittest.skipUnless(winplat.IS_WINDOWS, 'native Windows only')
class NativeWindowsEvidenceTests(unittest.TestCase):
    """windows-latest: the same samples without patching, with real WindowsPath comparisons."""

    def test_a_wrapped_test_run_approves_and_the_rollout_cwd_is_the_workspace(self):
        self.assertTrue(approves(collect('codex', codex_rows('item_1', 'item_2', 'item_3'))[1]))
        self.assertFalse(approves(collect('codex', codex_rows('item_4', 'item_5', 'item_6', 'item_7', 'item_8'))[1]))
        rollout = RolloutHome(rows_of('codex-rollout-powershell.jsonl')).cwds()
        self.assertEqual(Path(rollout['cwd']), Path('C:/Users/Yuan/work/review loop'))
        guarded, cwd, proven = rc.codex_guard_calls(collect('codex', codex_rows('item_0', 'item_1'))[2], rollout)
        self.assertEqual((cwd, proven), (Path('C:\\USERS\\Yuan\\work\\review loop'), 2))


if __name__ == '__main__':
    unittest.main()
