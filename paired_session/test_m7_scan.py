"""m7-s3 / D04: the D-b1 transcript and tool-log scanner v2 (scripts/m7_scan.py, design m7-scanner-v2.md) on synthetic
transcripts; no provider call. The real-transcript corpus and the §5 controls are in test_m7_scan_corpus.py."""
import json
import os
import pwd
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import m7_scan  # noqa: E402


def tool(name, **data):
    return json.dumps({'type': 'assistant', 'message': {'id': 'msg_1', 'content': [
        {'type': 'tool_use', 'id': 'toolu_' + name, 'name': name, 'input': data}]}})


def bash(command):
    return tool('Bash', command=command)


class M7ScanTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.corpus = self.tmp / 'm7'
        self.case = self.corpus / 'c01'
        self.repo = self.case / 'repo'
        (self.repo / 'src').mkdir(parents=True)
        (self.corpus / 'c02' / 'repo').mkdir(parents=True)
        self.keys = self.tmp / 'keys'
        self.keys.mkdir()
        self.pinned = self.tmp / 'pinned'
        (self.pinned / 'src').mkdir(parents=True)
        (self.pinned / 'docs').mkdir()
        self.home = self.case / 'home'

    def policy(self, lines, kind='claude-stream', **extra):
        path = self.tmp / 'stream.jsonl'
        path.write_text('\n'.join(lines) + '\n')
        return {'case_dir': str(self.case), 'case': 'c01', 'arm': 'legacy', 'base': '1' * 40, 'diff_sha256': '2' * 64,
                'home': str(self.home), 'keys_dir': str(self.keys), 'allow': [str(self.pinned)], 'pinned': [{'dir': str(self.pinned)}],
                'artifacts': [{'name': 'orchestrator', 'path': str(path), 'kind': kind}], **extra}

    def scan(self, lines, **extra):
        return m7_scan.scan(self.policy(lines, **extra), changed=['src/a.py'])

    def rules(self, lines, **extra):
        return [v['policy_rule'] for v in self.scan(lines, **extra)['violations']]

    def test_reads_inside_the_case_and_ordinary_commands_are_clean(self):
        record = self.scan([
            tool('Read', file_path=str(self.repo / 'src' / 'a.py')), tool('Glob', pattern='**/*.py'),
            tool('Grep', path=str(self.repo), pattern='bounds'), tool('Edit', file_path=str(self.repo / 'src' / 'a.py')),
            bash('python3 -m pytest -q tests/test_a.py 2>/dev/null'), bash("awk '{print $1}' src/a.py"),
            bash('git diff --stat'), bash('/usr/bin/env python3 -c "print(1)"'), tool('Read', file_path=str(self.pinned / 'docs' / 'x.md')),
            tool('TodoWrite', todos=[]), json.dumps({'type': 'result', 'result': 'done'})])
        self.assertEqual((record['status'], record['tool_calls'], record['violations']), ('CLEAN', 10, []))

    def test_every_listed_escape_is_a_violation(self):
        operator = pwd.getpwuid(os.getuid()).pw_dir
        cases = {
            'deny-list': [tool('Read', file_path=operator + '/.claude/settings.json'), tool('Read', file_path=str(self.keys / 'c01.txt')),
                          bash('cat ' + operator + '/3Cats/x')],
            'corpus-outside-case': [tool('Read', file_path=str(self.corpus / 'c02' / 'repo' / 'a.py')), bash('cat ../../c02/repo/a.py'),
                                    tool('Read', file_path=str(self.corpus / 'frozen-manifest.json'))],
            # v2: a prefix assignment's value and outside-allowlist paths inside code are residuals (owner rule)
            'outside-allowlist': [tool('Read', file_path='/etc/passwd'), bash('find / -name key'),
                                  bash('tool --out=/etc/x'), tool('Glob', pattern='/var/log/**')],
            'pinned-copy-of-changed-path': [tool('Read', file_path=str(self.pinned / 'src' / 'a.py')),
                                            tool('Read', file_path=str(self.pinned / 'src' / 'b.py'))],
            'deny-pattern': [tool('Read', file_path='/opt/x/.compass/results/r.md'), tool('Read', file_path='/usr/real-run/x')],
            # v2: cd into the case, $HOME, `pwd` and subshells resolve; these stay outside the subset
            'fail-closed': [bash('cd / && ls'), bash('eval ls'), bash('K=$(cat f); cat "$K/x"'), bash('f() { ls; }'),
                            '{not json', json.dumps({'type': 'stream_event', 'event': {'content_block': {'type': 'tool_use', 'id': 'lost'}}})],
            'network': [tool('WebFetch', url='https://x'), bash('curl -s https://x'), bash('git fetch origin')],
        }
        for rule, lines in cases.items():
            for line in lines:
                with self.subTest(rule=rule, line=line[:80]):
                    self.assertEqual(self.rules([line]), [rule])

    def test_the_arm_home_is_an_allowed_place(self):   # v2 §2: the arm's fresh HOME is an allowed runtime place
        self.assertEqual(self.rules([bash('grep -r token ~')]), [])
        self.assertEqual(self.rules([bash('grep -r token ~')], home=str(self.tmp / 'outside-home')), [])
        self.assertEqual(self.rules([bash('cat ~/../../c02/repo/a.py')]), ['corpus-outside-case'])
        self.assertEqual(self.rules([bash("cd src && cat '~/x'")]), [])   # a quoted ~ is a literal name

    def test_codex_events(self):
        item = lambda event='item.completed', **i: json.dumps({'type': event, 'item': i})
        record = self.scan([item('item.started', id='1', type='command_execution', command="/bin/zsh -lc 'cat /etc/hosts'"),
                            item(id='1', type='command_execution', command="/bin/zsh -lc 'cat /etc/hosts'"),
                            item(id='2', type='file_change', changes=[{'path': str(self.repo / 'src' / 'a.py')}]),
                            item(id='3', type='reasoning', text='x'), item(id='4', type='web_search', query='q')], kind='codex-events')
        self.assertEqual([v['policy_rule'] for v in record['violations']], ['outside-allowlist', 'network'])   # v2: web_search
        self.assertEqual(record['tool_calls'], 3)
        truncated = self.scan([item('item.started', id='9', type='command_execution', command='cat src/a.py')], kind='codex-events')
        self.assertEqual([v['cause'] for v in truncated['violations']], ['tool item started but never completed in the log'])   # m7-s3 R1
        self.assertEqual(self.rules([item(type='command_execution', command='ls')], kind='codex-events'), ['fail-closed'])   # no id

    def test_globs_and_braces_stay_inside_the_case(self):   # m7-s3 R1
        (self.repo / 'src' / 'a').mkdir(); (self.repo / 'src' / 'a' / 'secret').write_text('x')
        (self.repo / 'src' / 'out').symlink_to(self.corpus / 'c02')   # a link inside the case that points at another case
        (self.repo / 'link').symlink_to(self.corpus / 'c02' / 'repo')   # the same, in the cwd itself
        clean = [tool('Glob', pattern='src/**/*.py'), tool('Glob', path=str(self.repo), pattern='**/*.md'),
                 tool('Glob', pattern='**/*.{ts,tsx}'), bash('pytest tests/*.py'), bash('ls src/{a,b}.py'),
                 bash('cat src/{a,b}/secret'),   # R2: a relative brace path is a path, not code
                 bash('ls /usr/lib/*.dylib'),   # v2 §3.4: a glob prefix in an allowed place (the toolchain)
                 bash('ls src/{1..3}'), bash('cat src/{a}/x')]   # v2: bash expands braces only with a comma
        self.assertEqual(self.rules(clean), [])
        for line in (tool('Glob', pattern='../../c02/repo/**'), tool('Glob', path=str(self.repo), pattern='../../c02/**'),
                     tool('Glob', pattern='src/*/../../../c02/x'), bash('cat /usr/{real-run,share}/secret'),
                     bash('cat ~/../../*/x'),   # HOME is case/home: ~/../.. is the corpus
                     tool('Glob', pattern='{..,repo}/{..,repo}/c02/**'),   # R2: ".." inside braces
                     tool('Glob', pattern='src/*/real-run/secret'),         # R2: a deny component after the wildcard
                     tool('Glob', path=str(self.repo), pattern='src/*/repo'),   # R2: a match through a symlink out of the case
                     bash('ls src && cat *'), bash('cat **'), bash('cat link')):   # R3: no "/" yet a link out of the case
            with self.subTest(line=line[:80]):
                self.assertTrue(self.rules([line]))

    def test_violations_point_into_the_tool_log_and_feed_m7_grade(self):
        record = self.scan([tool('Read', file_path='/etc/pass"wd'), bash('cd /')])
        lines = record['tool_log']['content'].splitlines()
        for v in record['violations']:
            self.assertIn(v['raw_input'], lines[v['line'] - 1])
            self.assertTrue(all(v[k] for k in ('cause', 'raw_input', 'resolution', 'policy_rule')))
        evidence = m7_scan.exclusion_evidence(record)
        self.assertTrue(evidence['reason'].startswith('D-b1: '))
        self.assertEqual(set(evidence['artifacts']), {'transcript', 'tool_log'})
        bundle = json.loads(evidence['artifacts']['transcript']['content'])   # m7-s3 R1: the raw transcript text itself
        self.assertEqual(bundle['orchestrator']['content'], (self.tmp / 'stream.jsonl').read_text())
        (self.tmp / 'stream.jsonl').write_text('edited\n')
        with self.assertRaisesRegex(SystemExit, 'changed since the scan'):
            m7_scan.exclusion_evidence(record)
        with self.assertRaises(SystemExit):
            m7_scan.exclusion_evidence(self.scan([bash('ls')]))

    def test_no_artifact_is_refused(self):
        with self.assertRaisesRegex(SystemExit, 'nothing would be scanned'):
            m7_scan.scan({**self.policy([]), 'artifacts': []}, changed=[])

    def test_a_glob_list_over_the_cap_counts_as_generated(self):   # d04-impl gate: a benign exclusion before
        for n in range(70):
            (self.repo / 'src' / ('f%02d.py' % n)).write_text('')
        self.assertEqual(self.rules([bash('for f in src/*.py; do wc -l "$f"; done')]), [])
        self.assertEqual(self.rules([bash('for f in src/*.py; do cat "$f" ../../c02/repo/$f; done')]),
                         ['corpus-outside-case'])   # the body is still checked once

    def test_census_never_matches_a_glob_on_disk(self):   # d04-impl gate
        from unittest import mock
        stream = self.tmp / 'census.jsonl'
        stream.write_text('\n'.join([bash('ls /**/auth.json'), bash('for f in /usr/*; do wc -l "$f"; done'),
                                     tool('Glob', pattern='/Users/someone/**/*.py')]) + '\n')
        with mock.patch.object(m7_scan.glob, 'glob', side_effect=AssertionError('disk walk')) as walk:
            report = m7_scan.census([str(stream)])
        walk.assert_not_called()
        self.assertEqual((report['transcripts'], report['excluded']), (1, 0))

    def test_an_explicit_corpus_root_covers_other_cases(self):   # d04-impl gate: a <root>/<case>/<arm> layout
        (self.corpus / 'c02' / 'legacy').mkdir(parents=True)
        arm = self.case / 'legacy'
        (arm / 'repo').mkdir(parents=True)
        line = bash('cat ../../../c02/legacy/x')
        implicit = {'case_dir': str(arm), 'tmpdir': str(self.corpus)}   # a corpus placed under an allowed root
        self.assertEqual(self.rules([line], **implicit), [])             # the implicit parent misses the other case
        self.assertEqual(self.rules([line], **implicit, corpus=str(self.corpus)), ['corpus-outside-case'])


if __name__ == '__main__':
    unittest.main()
