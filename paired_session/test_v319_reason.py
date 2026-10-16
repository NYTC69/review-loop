"""V319-REASON: CLI failure reasons with color codes, the brief view of long reasons, and review report rendering."""
import contextlib
import io
import json
import unittest

from paired_session import leak_scan
from paired_session import review_report
from paired_session import test_v317fd as fd

rc = fd.rc
# Built at run time: the repository carries no literal a credential rule matches.
TOKEN = 'gh' + 'p_' + 'A1b2C3d4' * 5
ROUTING = ('Error: failed to start the session: thread/start failed: during startup: the provider returned an error '
           'while preparing the workspace for this turn: workspace routing discovery failed')


class CliFailureReasonTests(unittest.TestCase):
    def test_a_colored_error_line_is_recognized(self):
        stderr = ('\x1b[33mwarning\x1b[0m: update check unavailable\n'
                  '\x1b[1m\x1b[31merror\x1b[0m: workspace routing discovery failed\n'
                  '\x1b[2mFor more information, try --help\x1b[0m\n\x1b[0m\n')
        self.assertEqual(rc.cli_failure_reason('CLI exit 1', '', stderr),
                         'CLI exit 1: error: workspace routing discovery failed')

    def test_a_trailing_color_reset_line_is_not_the_reason(self):
        self.assertEqual(rc.cli_failure_reason('CLI exit 1', '', 'login required\n\x1b[0m\n'), 'CLI exit 1: login required')


class BriefReasonTests(unittest.TestCase):
    def test_a_short_reason_is_whole_and_a_long_one_keeps_its_start_and_end(self):
        self.assertEqual(rc.brief_reason('CLI exit 1:\n  login required'), 'CLI exit 1: login required')
        reason = 'CLI exit 1: ' + ROUTING
        brief = rc.brief_reason(reason)
        self.assertEqual(len(brief), 160)
        self.assertTrue(brief.startswith('CLI exit 1: Error: failed to start the session'))
        self.assertTrue(brief.endswith('workspace routing discovery failed'))
        self.assertIn(' ... ', brief)


class BriefViewTests(unittest.TestCase):
    locals().update({name: getattr(fd.OperatorFailureTests, name) for name in
                    ('setUp', 'tearDown', '_assert_no_real_provider_cli', '_guarded_test_popen', 'coordinator',
                     'fake_codex_cli', 'fake_claude_cli', 'failing_coordinator')})

    def test_status_brief_shows_the_cause_at_the_end_of_a_long_cli_reason(self):
        co = self.failing_coordinator('off', 'codex', stderr='\x1b[31m' + ROUTING + '\x1b[0m\n')
        self.assertEqual(co.drive(), 'HOLD')
        self.assertEqual(co.state['hold_reason'], 'CLI exit 1: ' + ROUTING)   # state.json keeps the whole reason
        terminal = json.loads((co.run_dir / 'progress.jsonl').read_text().splitlines()[-1])
        self.assertEqual((terminal['kind'], terminal['reason']), ('terminal', rc.brief_reason('CLI exit 1: ' + ROUTING)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc.status_brief(co.run_dir, 1)
        self.assertIn('workspace routing discovery failed · ', out.getvalue())


class RedactTests(unittest.TestCase):
    def test_credentials_are_replaced_and_other_text_kept(self):
        generic = 'api_key = "' + 'Zq9' * 8 + '"'
        self.assertEqual(leak_scan.redact(f'401: bad token {TOKEN}; {generic}'),
                         '401: bad token [redacted github-token]; api_key = "[redacted generic-secret-assignment]"')
        for text in ('Invalid API key - Please run /login', 'api_key = "your_api_key_placeholder_here"'):
            self.assertEqual(leak_scan.redact(text), text)


class ReportRenderingTests(unittest.TestCase):
    def render(self, **state):
        return review_report.render({'report': {'complete': False}, 'turns': [], 'config': {}, 'finding_ledger': [], **state})

    def test_the_status_line_stays_one_bounded_line_without_credentials(self):
        text = self.render(hold_reason=f'CLI exit 1: Error: bad token {TOKEN}\n## Fake heading\n| a | b |')
        status = [line for line in text.splitlines() if line.startswith('Status: ')]
        self.assertEqual(status, ['Status: incomplete, the run is on HOLD: CLI exit 1: Error: bad token '
                                  '[redacted github-token] ## Fake heading \\| a \\| b \\|'])
        self.assertNotIn(TOKEN, text)
        self.assertNotIn('\n## Fake heading', text)
        status = next(line for line in self.render(hold_reason='y' * 1500).splitlines() if line.startswith('Status: '))
        self.assertTrue(status.endswith('y' * 1000 + ' ... (truncated)'))

    def test_an_aggregate_secret_scan_finding_counts_once(self):
        scan = {'id': 'F001', 'source': 'secret-scan', 'phase': 'EXEC', 'severity': 'SECURITY', 'file': 'a.py:2',
                'summary': 'the change adds a hardcoded credential at a.py:2 (github-token), a.py:3 (github-token), '
                           'b.py:1 (jwt), +2 more'}
        text = self.render(finding_ledger=[scan])
        self.assertIn('## Security (1)', text)
        self.assertEqual(sum(line.startswith('- **F001**') for line in text.splitlines()), 4)   # still one line per hit
        preflight = [{'id': f'F00{n}', 'source': 'security-preflight', 'phase': 'SECURITY', 'severity': 'CRITICAL',
                      'file': path, 'summary': f'[class: secret] {rule} in {path}:{line}'}
                     for n, path, line, rule in ((2, 'a.py', 2, 'github-token'), (3, 'c.py', 4, 'private-key-block'))]
        text = self.render(finding_ledger=[scan, *preflight])
        self.assertIn('## Critical (2)', text)   # F002 with its merged scan hit, and F003
        self.assertIn('## Security (1)', text)   # F001's remaining hits

    def test_a_line_zero_finding_shows_the_path_alone(self):
        row = {'id': 'F001', 'source': 'adversarial-gate', 'phase': 'EXEC', 'severity': 'HIGH', 'file': 'a.py',
               'line_start': 0, 'line_end': 0, 'summary': 'the module never validates input'}
        text = self.render(finding_ledger=[row, dict(row, id='F002', source='persistent-reviewer')])
        self.assertIn('[gate, EXEC; EXEC reviewer, EXEC, HIGH] `a.py`: the module never validates input', text)
        self.assertIn('  - F002 [EXEC reviewer, EXEC]: the module never validates input', text)
        self.assertNotIn('a.py:0', text)
        self.assertIn('`a.py:3-5`', self.render(finding_ledger=[dict(row, line_start=3, line_end=5)]))


if __name__ == '__main__':
    unittest.main()
