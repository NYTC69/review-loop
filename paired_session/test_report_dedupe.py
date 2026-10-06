"""REPORT-DEDUPE: review-report.md merges the same issue reported by several roles (render time only; the ledger is unchanged).
Field shape: the v2.12.3 review-pr closing check on NYTC69/review-loop#6 listed 9 Suggestions for 3 issues."""
import copy
import re
import unittest

from paired_session import review_report

FIELD = (   # the closing check's ledger rows, verbatim (id, severity, source, phase, file, summary)
    ('F001', 'MINOR', 'persistent-reviewer', 'EXEC', '.codex-plugin/plugin.json', '[class: stale-copied-metadata] The top-level description was copied word for word from .claude-plugin/plugin.json and says "12 agents, 5 skills". The Codex manifest exposes 4 skills (.agents/skills/{execute,guide,plan,review-loop}), and there are only 2 Codex agents (.codex/agents/*.toml). Even the Claude-side count (7 skills under ./skills/) no longer matches "5".'),
    ('F002', 'LOW', 'persistent-reviewer', 'EXEC', 'tests/skills/contracts/review-loop.json', '[class: missing-version-pin-contract] The plugin_version_pinned_* contracts pin version 2.7.4 in .claude-plugin/plugin.json and marketplace.json, but nothing checks the new .codex-plugin/plugin.json.'),
    ('F003', 'MINOR', 'fresh-shadow', 'EXEC', '.codex-plugin/plugin.json', "The top-level description is copied word for word from .claude-plugin/plugin.json and claims '12 agents, 5 skills'. The Codex manifest only exposes the 4 skills under .agents/skills/ and the 2 Codex agents at .codex/agents/*.toml."),
    ('F004', 'LOW', 'fresh-shadow', 'EXEC', 'tests/skills/contracts/review-loop.json', 'The version-pin contracts (plugin_version_pinned_*) check .claude-plugin/plugin.json and marketplace.json but not the new .codex-plugin/plugin.json, so the Codex manifest version is not tied to a release check.'),
    ('F006', 'MINOR', 'specialist:code-reviewer', 'POLISH-Q', '.codex-plugin/plugin.json', 'The description is copied from the Claude manifest and claims "12 agents, 5 skills". The Codex manifest only exposes the 4 skills under .agents/skills/ plus 2 agents in .codex/agents/*.toml, and its own longDescription says the ./skills/ tree is intentionally not exposed.'),
    ('F007', 'MINOR', 'specialist:code-reviewer', 'POLISH-Q', '.codex-plugin/plugin.json', 'No lint contract pins the version in the new Codex manifest. tests/skills/contracts/review-loop.json:666-683 checks only .claude-plugin/plugin.json and marketplace.json, so the next version bump can leave the Codex manifest behind without any lint failure.'),
    ('F008', 'MINOR', 'specialist:silent-failure-hunter', 'POLISH-Q', '.codex-plugin/plugin.json', 'The new Codex manifest\'s version (2.7.4) has no version-pin contract. tests/skills/contracts/review-loop.json pins "version": "2.7.4" only for .claude-plugin/plugin.json and .claude-plugin/marketplace.json, so the check misses this manifest\'s version entirely.'),
    ('F009', 'MINOR', 'specialist:silent-failure-hunter', 'POLISH-Q', '.codex-plugin/plugin.json', 'The top-level "description" was copied word for word from the Claude manifest and claims "12 agents, 5 skills". The Codex install actually exposes 4 skills under .agents/skills/ (execute, guide, plan, review-loop) and 2 agents under .codex/agents/*.toml, which the manifest\'s own longDescription admits.'),
    ('F011', 'MINOR', 'specialist:pr-test-analyzer', 'POLISH-Q', '.codex-plugin/plugin.json', 'The description says "12 agents, 5 skills", but this Codex manifest exposes only the 4 skills under .agents/skills/ and the 2 Codex agents under .codex/agents/. Its own longDescription says the other skills are deliberately not exposed, so the summary contradicts it.'),
)


def ledger(rows=FIELD, **extra):
    return [{'id': i, 'severity': sev, 'source': src, 'owner_role': src, 'phase': phase, 'file': path, 'summary': text,
             'status': 'open', **extra} for i, sev, src, phase, path, text in rows]


def report(rows):
    state = {'report': {'complete': True}, 'turns': [], 'config': {}, 'finding_ledger': rows}
    return review_report.render(state), state


def finding_lines(text):
    return [line for line in text.splitlines() if line.startswith('- **F')]


class ReportDedupeTests(unittest.TestCase):
    def test_the_field_shape_merges_by_issue_and_keeps_every_id(self):
        rows = ledger()
        before = copy.deepcopy(rows)
        text, _ = report(rows)
        lines = finding_lines(text)
        self.assertEqual(len(lines), 3, '\n'.join(lines))   # 9 rows, 3 issues: the copied description, and the
        self.assertIn('## Suggestions (9)', text)            # version pin in two files (never merged across files)
        self.assertTrue(lines[0].startswith('- **F001, F003, F006, F009, F011** [EXEC reviewer, EXEC; shadow, EXEC; '
                                            'specialist code-reviewer, POLISH-Q; specialist silent-failure-hunter, POLISH-Q; '
                                            'specialist pr-test-analyzer, POLISH-Q] `.codex-plugin/plugin.json`: '))
        self.assertTrue(lines[0].endswith('(5 similar reports, each listed; they may still be separate issues)'))
        self.assertTrue(lines[1].startswith('- **F002, F004** [EXEC reviewer, EXEC; shadow, EXEC, LOW] '
                                            '`tests/skills/contracts/review-loop.json`: '))
        self.assertTrue(lines[2].startswith('- **F007, F008** [specialist code-reviewer, POLISH-Q; specialist '
                                            'silent-failure-hunter, POLISH-Q] `.codex-plugin/plugin.json`: '))
        self.assertEqual(sorted(re.findall(r'F\d{3}', ' '.join(line.split('**')[1] for line in lines))),
                         [row[0] for row in FIELD])   # every id, once
        self.assertEqual(rows, before)   # render time only: the ledger is unchanged
        for _, _, _, _, _, summary in FIELD:
            self.assertEqual(text.count(summary), 1)   # every report's summary is still shown, once
        self.assertIn(f'  - F003 [shadow, EXEC]: {FIELD[2][5]}', text)

    def test_a_merge_of_near_identical_wording_hides_no_summary(self):   # R1: two parameters, one word apart
        rows = ledger((('F001', 'MINOR', 'persistent-reviewer', 'EXEC', 'cfg.py', 'The timeout parameter accepts negative values without validation.'),
                       ('F002', 'MINOR', 'fresh-shadow', 'EXEC', 'cfg.py', 'The retries parameter accepts negative values without validation.')))
        text = report(rows)[0]
        for row in rows:
            self.assertIn(row['summary'], text)
        self.assertNotIn('same issue', text)   # R2: a group is not claimed to be one issue

    def test_different_issues_in_one_file_stay_separate(self):
        rows = ledger(tuple(row for row in FIELD if row[0] in ('F001', 'F007')))
        self.assertEqual(len(finding_lines(report(rows)[0])), 2)

    def test_never_across_files_severities_or_the_security_flag(self):
        same = FIELD[2]   # one summary, varied on one key at a time
        for changed in (('F012', 'MINOR', same[2], same[3], 'other/file.json', same[5]),
                        ('F012', 'LOW', same[2], same[3], same[4], same[5])):
            with self.subTest(changed=changed[1:2] + changed[4:5]):
                self.assertEqual(len(finding_lines(report(ledger((same, changed)))[0])), 2)
        rows = ledger(((same[0], 'CRITICAL', *same[2:]), ('F012', 'CRITICAL', *same[2:])))
        rows[1]['security'] = True
        self.assertEqual(len(finding_lines(report(rows)[0])), 2)
        rows[1]['security'] = False
        self.assertEqual(len(finding_lines(report(rows)[0])), 1)   # the same issue, same keys: merged

    def test_a_single_row_keeps_its_format(self):
        text, _ = report(ledger(FIELD[:1]))
        self.assertEqual(finding_lines(text), [f'- **F001** [EXEC reviewer, EXEC] `.codex-plugin/plugin.json`: {FIELD[0][5]}'])


if __name__ == '__main__':
    unittest.main()
