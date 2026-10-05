"""F3 (conservative): ignored executable-config files a writer turn writes are reported, never held."""
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from paired_session import ignored_config
from paired_session import test_real_coordinator as trc
from paired_session.test_worktree_lifecycle import COVERING_GITIGNORE, DONE

rc = trc.rc
_HELPERS = ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'fake_codex_cli',
            'fake_claude_cli', 'command', 'run_coordinator', 'run_operator_action', 'coordinator')


def git(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True, text=True).stdout


def check_ignore(root):
    def ignored(names):
        out = subprocess.run(['git', 'check-ignore', '-z', '--stdin'], cwd=root, input='\0'.join(names) + '\0',
                             capture_output=True, text=True)
        return {name for name in out.stdout.split('\0') if name}
    return ignored


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, 'init', '-q')
        (self.root / '.gitignore').write_text('.vscode/\n.claude/\nbuild/\n.envrc\n.mcp.json\n')
        (self.root / '.envrc').write_text('export A=1\n')   # ignored before the turn and left alone
        git(self.root, 'add', '.gitignore')
        git(self.root, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'base')

    def tearDown(self):
        self.tmp.cleanup()

    def inventory(self):
        return ignored_config.inventory(self.root, check_ignore(self.root))

    def write(self, name, text='x\n'):
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(text)

    def test_new_and_changed_ignored_config_files_are_listed_and_others_are_not(self):
        self.write('.mcp.json', '{}\n')
        git(self.root, 'add', '-f', '.mcp.json')   # tracked: in the review snapshot, never listed here
        before = self.inventory()
        self.assertEqual(sorted(before['entries']), ['.envrc'])
        self.write('.vscode/tasks.json', '{"tasks": []}\n')
        self.write('.claude/commands/deploy.md', 'run it\n')
        self.write('.claude/settings.local.json', '{}\n')
        self.write('build/output.bin', 'not a config\n')   # ignored, but not an executable config
        self.write('.mcp.json', '{"servers": {}}\n')
        self.assertEqual(ignored_config.written(before, self.inventory()),
                         ['.claude/commands/deploy.md', '.claude/settings.local.json', '.vscode/tasks.json'])
        before = self.inventory()
        self.write('.envrc', 'export A=2  # changed\n')
        os.utime(self.root / '.envrc', ns=(1, 1))
        self.assertEqual(ignored_config.written(before, self.inventory()), ['.envrc'])
        (self.root / '.envrc').unlink()   # a removed file is no risk
        self.assertEqual(ignored_config.written(before, self.inventory()), [])

    def test_the_inventory_is_bounded_and_says_so(self):
        for index in range(6):
            self.write(f'.claude/commands/c{index}.md')
        with mock.patch.object(ignored_config, 'LIMIT', 3):
            result = self.inventory()
        self.assertLessEqual(len(result['entries']), 3)
        self.assertIn('partial', result['note'])
        with mock.patch.object(ignored_config, 'SECONDS', -1.0):
            self.assertIn('partial', self.inventory()['note'])

    def test_the_scan_spends_its_budget_per_directory_entry(self):   # R1: no bound is checked only after a full listing
        for index in range(40):
            (self.root / '.claude/commands' / f'empty{index:02d}').mkdir(parents=True)   # no candidate file at all
        with mock.patch.object(ignored_config, 'SCAN_LIMIT', 20):
            self.assertIn('partial', self.inventory()['note'])
        for index in range(40):
            self.write(f'.claude/other{index:02d}.txt')   # many non-candidates next to the settings files
        (self.root / '.claude/commands').rename(self.root / 'elsewhere')
        with mock.patch.object(ignored_config, 'SCAN_LIMIT', 20):
            self.assertIn('partial', self.inventory()['note'])
        with mock.patch.object(ignored_config, 'SECONDS', -1.0):   # no commands directory, past the deadline
            self.assertIn('partial', self.inventory()['note'])
        self.assertNotIn('note', self.inventory())   # within the bounds: complete

    def test_a_symlinked_parent_directory_is_never_followed(self):   # R1
        outside = Path(self.tmp.name + '-outside')
        (outside / 'commands').mkdir(parents=True)
        (outside / 'tasks.json').write_text('{}\n')
        (outside / 'settings.json').write_text('{}\n')
        (outside / 'commands' / 'run.md').write_text('x\n')
        self.addCleanup(lambda: __import__('shutil').rmtree(outside))
        (self.root / '.vscode').symlink_to(outside, target_is_directory=True)
        (self.root / '.claude').symlink_to(outside, target_is_directory=True)
        self.assertEqual(sorted(self.inventory()['entries']), ['.envrc'])

    def test_a_partial_baseline_never_reports_an_existing_file_as_new(self):   # R1
        for index in range(4):
            self.write(f'.claude/commands/c{index}.md')
        with mock.patch.object(ignored_config, 'LIMIT', 3):
            before = self.inventory()
        self.assertIn('partial', before['note'])
        self.assertEqual(ignored_config.written(before, self.inventory()), [])
        self.write(sorted(before['entries'])[-1], 'changed, longer\n')   # a known entry that changed is still reported
        self.assertEqual(ignored_config.written(before, self.inventory()), [sorted(before['entries'])[-1]])

    def test_a_failed_baseline_records_the_note_and_reports_nothing(self):   # R1: the receipt side
        holder = types.SimpleNamespace(state={})
        receipt = {}
        after = {'entries': {'.vscode/tasks.json': [1, 2, 3, 4, '']}}
        rc.Coordinator._record_ignored_config(holder, receipt, {'entries': {}, 'note': 'ignored executable-config inventory '
                                                                'failed (RuntimeError: boom)'}, after)
        self.assertEqual(receipt, {'ignored_config_note': 'ignored executable-config inventory failed (RuntimeError: boom)'})
        self.assertEqual(holder.state, {})
        rc.Coordinator._record_ignored_config(holder, receipt, {'entries': {}}, after)
        self.assertEqual((receipt['ignored_config_written'], holder.state), (['.vscode/tasks.json'],
                                                                             {'ignored_config_written': ['.vscode/tasks.json']}))

    def test_a_failed_inventory_is_a_note_never_an_error(self):
        def broken(names): raise RuntimeError('git check-ignore failed: boom')
        result = ignored_config.inventory(self.root, broken)
        self.assertEqual(result['entries'], {})
        self.assertIn('failed', result['note'])


class IgnoredConfigRunTests(unittest.TestCase):
    locals().update({name: getattr(trc.RealCoordinatorTests, name) for name in _HELPERS})

    def setUp(self):
        trc.RealCoordinatorTests.setUp(self)
        (self.workspace / '.gitignore').write_text(COVERING_GITIGNORE + '.vscode/\nbuild/\n')
        rc.subprocess.run(['git', 'commit', '-qam', 'ignore sensitive files and editor state'], cwd=self.workspace, check=True)

    def test_an_author_writing_an_ignored_task_config_is_reported_and_the_run_continues(self):
        done = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_AUTHOR_WRITE_IGNORED': '.vscode/tasks.json,build/cache.bin'})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        warnings = [line for line in done.stdout.splitlines() if 'ignored executable-config' in line]
        self.assertTrue(warnings and all(line.startswith('WARNING: ') for line in warnings), done.stdout)
        self.assertIn('.vscode/tasks.json', warnings[0])
        self.assertNotIn('build/cache.bin', done.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['ignored_config_written'], ['.vscode/tasks.json'])
        rows = [row for row in state['turns'] if row.get('ignored_config_written')]
        self.assertEqual([(row['role'], row['phase'], row['ignored_config_written']) for row in rows],
                         [('author', 'EXEC', ['.vscode/tasks.json'])])
        brief = self.run_operator_action('status', '--brief', '50')
        self.assertIn('WARNING: ignored executable-config files written by the author: .vscode/tasks.json', brief.stdout)
        accepted = self.run_operator_action('accept', '--lifecycle-mode', 'on', '--reason', 'checked')
        self.assertEqual(accepted.stdout.strip().splitlines()[-1], 'ACCEPTED', accepted.stdout + accepted.stderr)
        report = (self.run_dir / 'delivery-report.md').read_text()
        self.assertIn('## Ignored executable-config files written by the author', report)
        self.assertIn('- `.vscode/tasks.json`', report)
        self.assertNotIn('build/cache.bin', report)

    def test_a_docs_writer_writing_an_ignored_editor_config_is_reported(self):   # FINISH/DOCS writers share the author path
        done = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_LIFECYCLE_DOCS_FILE': '.vscode/settings.json'})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        state = json.loads((self.run_dir / 'state.json').read_text())
        rows = [row for row in state['turns'] if row.get('ignored_config_written')]
        self.assertEqual([(row['role'], row['phase'], row['ignored_config_written']) for row in rows],
                         [('author', 'DOCS', ['.vscode/settings.json'])])
        self.assertIn('WARNING: the author wrote ignored executable-config files that no reviewer sees: .vscode/settings.json',
                      done.stdout)

    def test_an_ignored_non_config_file_gives_no_warning_and_no_report_section(self):
        done = self.run_coordinator('--lifecycle-mode', 'on', env={'FAKE_AUTHOR_WRITE_IGNORED': 'build/cache.bin'})
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        self.assertNotIn('ignored executable-config', done.stdout)
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertNotIn('ignored_config_written', state)
        self.assertFalse([row for row in state['turns'] if row.get('ignored_config_written')])


if __name__ == '__main__':
    unittest.main()
