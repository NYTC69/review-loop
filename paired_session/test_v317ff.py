"""V317-FF: normal DOCS ownership, useful preflight reasons and shared credential rules."""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from paired_session import leak_scan
from paired_session import test_worktree_lifecycle as twl
from scripts import content_rules
from scripts import security_preflight as sp

rc = twl.rc
PLAN_NOTICE = 'Documentation written by the DOCS stage after implementation, not by this plan: '
REVIEW_NOTICE = ('These documentation files are written by the DOCS stage after this review, '
                 'and the implementer must not edit them: ')
EXAMPLE = 'AKIA' + 'IOSFODNN7EXAMPLE'
REAL = 'AKIA' + 'Q3X7M2P9L4K8N6B1'


class PromptAndPreflightTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in twl._HELPERS})

    def prompts(self, co):
        (co.context / 'plan.md').write_text('# Plan\nImplement the feature and run the configured checks.\n')
        snapshot = rc.git_snapshot(self.workspace)[0]
        co.state['phase'] = 'PLAN'
        rows = [('plan-author', co._author_prompt(), PLAN_NOTICE),
                ('plan-reviewer', co._review_prompt('reviewer', snapshot), REVIEW_NOTICE)]
        co.state['phase'] = 'EXEC'
        rows += [('exec-author', co._author_prompt(), 'Reserved for the DOCS stage; do not edit: '),
                 ('exec-reviewer', co._review_prompt('reviewer', snapshot), REVIEW_NOTICE),
                 ('shadow', co._review_prompt('shadow', snapshot), REVIEW_NOTICE),
                 ('gate', co._gate_prompt(snapshot), REVIEW_NOTICE)]
        return rows

    def test_full_lifecycle_prompts_share_configured_docs_paths_and_fresh_scans_pass(self):
        co = self.coordinator('--lifecycle-mode', 'on', '--docs-file', 'docs/changes.md',
                              '--docs-allowlist', 'docs/guide.md')
        for role, prompt, notice in self.prompts(co):
            with self.subTest(role=role):
                self.assertIn(notice + 'docs/changes.md, docs/guide.md.', prompt)
                co.assert_fresh_prompt(role, prompt)

    def test_reserved_document_names_are_masked_for_scans_and_visible_to_models(self):
        paths = ('docs/gate-review.md', 'docs/F100.md')
        for mode in ('on', 'off'):
            with self.subTest(mode=mode):
                self.run_dir = self.root / ('reserved-names-' + mode)
                co = self.coordinator('--lifecycle-mode', mode, '--docs-allowlist', paths[0],
                                      '--docs-allowlist', paths[1])
                for role, prompt, _ in self.prompts(co):
                    if role not in ('shadow', 'gate'):
                        continue
                    for path in paths:
                        if mode == 'on':
                            self.assertIn(path, prompt)
                            self.assertNotIn(path, co._fresh_history_text('prompt', prompt))
                        else:
                            self.assertNotIn(path, prompt)
                    co.assert_fresh_prompt(role, prompt)
                    receipt = json.loads((co.evidence / f'{co.state["sequence"] + 1:03d}-{role}.independence-inputs.json').read_text())
                    self.assertEqual(receipt['inputs']['prompt']['content'], prompt)
                if mode == 'on':
                    self.assertIn('gate-review', co._introduced_history('context/plan.md', paths[0]))
                    self.assertIn('F100', co._introduced_history('context/plan.md', paths[1]))

    def test_default_docs_file_is_reserved_in_plan_and_reviews(self):
        co = self.coordinator('--lifecycle-mode', 'on')
        for role, prompt, notice in self.prompts(co):
            with self.subTest(role=role):
                self.assertIn(notice + 'CHANGELOG.md.', prompt)
                co.assert_fresh_prompt(role, prompt)

    def test_stop_after_gate_prompts_have_no_docs_stage_notice(self):
        co = self.coordinator('--lifecycle-mode', 'off', '--docs-file', 'docs/changes.md',
                              '--docs-allowlist', 'docs/guide.md')
        for role, prompt, notice in self.prompts(co):
            with self.subTest(role=role):
                self.assertNotIn(notice, prompt)
                co.assert_fresh_prompt(role, prompt)

    def test_review_only_reserves_future_docs_but_not_pre_owned_docs(self):
        (self.workspace / 'CHANGELOG.md').write_text('The existing change includes documentation.\n')
        co = self.coordinator('--lifecycle-mode', 'on', '--review-only', '--docs-allowlist', 'docs/guide.md')
        self.assertIn('CHANGELOG.md', co.state['lifecycle']['docs_pre_owned'])
        for role in ('reviewer', 'shadow', 'gate'):
            prompt = co._gate_prompt('') if role == 'gate' else co._review_prompt(role, '')
            self.assertIn(REVIEW_NOTICE + 'docs/guide.md.', prompt)
            self.assertNotIn(REVIEW_NOTICE + 'CHANGELOG.md', prompt)
        self.assertIn('Part of the change under review; edit only to fix a delivered finding: CHANGELOG.md.',
                      co._author_prompt())

    def test_report_mode_prompts_do_not_reserve_docs(self):
        (self.workspace / 'tracked.txt').write_text('changed\n')
        co = self.coordinator('--lifecycle-mode', 'on', '--review-only', '--review-report')
        for role in ('reviewer', 'shadow', 'gate'):
            prompt = co._gate_prompt('') if role == 'gate' else co._review_prompt(role, '')
            self.assertNotIn(REVIEW_NOTICE, prompt)
            co.assert_fresh_prompt(role, prompt)

    def test_finisher_and_both_quality_writer_call_sites_include_long_command_rule(self):
        co = self.coordinator('--lifecycle-mode', 'on', '--quality-writers', 'both')
        (co.context / 'plan.md').write_text('# Plan\nImplement the feature.\n')
        class Captured(Exception):
            pass
        seen = []
        def capture(request, role, prompt):
            built = prompt()
            self.assertIn(rc.LONG_COMMAND_RULE, built)
            if role == 'finisher':
                self.assertLess(built.index(rc.LONG_COMMAND_RULE), built.index('Approved plan:\n'))
            seen.append(role)
            raise Captured()
        with mock.patch.object(co, '_lifecycle_writer', side_effect=capture), \
                mock.patch.object(co, '_green_baseline', return_value=True):
            with self.assertRaises(Captured):
                co.worktree_finish_turn()
            tree = rc.git_snapshot(self.workspace)[0]
            co.state['lifecycle'].update(stage='POLISH-Q', candidate_oid=tree,
                                         quality_writers={'base_oid': tree})
            for writer, next_turn in (('simplifier', 'polish-simplify'), ('test-writer', 'polish-tests')):
                co.state['next'] = next_turn
                co.state['lifecycle']['pending'] = None
                co.state['lifecycle'].pop('writer_leg', None)
                with self.assertRaises(Captured):
                    co.worktree_writer_turn()
        self.assertEqual(seen, ['finisher', 'POLISH-Q simplifier', 'POLISH-Q test-writer'])

    def test_incomplete_preflight_report_reason_reaches_security_hold(self):
        co = self.coordinator('--lifecycle-mode', 'on')
        reason = str(sp._scan_limit_error('large.bin', sp.MAX_FILE_BYTES + 1))
        document = {'status': 'incomplete', 'reason': reason, 'findings': [], 'ignore_coverage': []}
        real = co._security_script
        def script(name, *args):
            if name != 'security_preflight.py':
                return real(name, *args)
            Path(args[args.index('--output') + 1]).write_text(json.dumps(document))
            return subprocess.CompletedProcess([], 3, '', '')
        tree = rc.git_snapshot(self.workspace)[0]
        co.state['lifecycle'].update(stage='SECURITY', candidate_oid=tree, receipts=[
            {'stage': 'DOCS', 'epoch': 0, 'request_id': 'w-DOCS-0-0', 'route': 'SECURITY', 'output_oid': tree}])
        co.state.update(next='security', phase='EXEC')
        with mock.patch.object(co, '_security_script', side_effect=script):
            co.worktree_security_turn()
        self.assertIn('security preflight incomplete (exit 3); ' + reason, co.state['hold_reason'])

    def test_preflight_reason_lists_each_path_rule_once_without_dropping_hits(self):
        co = self.coordinator('--lifecycle-mode', 'on')
        rows = [{'path': 'settings.py', 'rule': 'aws-access-key-id', 'line': line} for line in (1, 1, 2)]
        rows += [{'path': 'other.py', 'rule': 'aws-access-key-id', 'line': 1},
                 {'path': 'settings.py', 'rule': 'github-token', 'line': 1}]
        document = {'status': 'blocked', 'findings': rows, 'ignore_coverage': []}
        real = co._security_script
        def script(name, *args):
            if name != 'security_preflight.py':
                return real(name, *args)
            Path(args[args.index('--output') + 1]).write_text(json.dumps(document))
            return subprocess.CompletedProcess([], 1, '', '')
        with mock.patch.object(co, '_security_script', side_effect=script):
            result = co._security_preflight('w-SECURITY-0-0')
        for path, rule in {(row['path'], row['rule']) for row in rows}:
            self.assertEqual(result['reason'].count(rule + ' in ' + path), 1)
        self.assertEqual(result['findings'], rows)
        self.assertEqual(json.loads(Path(result['report']).read_text()), document)

    def test_full_lifecycle_security_hold_deduplicates_path_rule_but_keeps_receipt_hits(self):
        (self.workspace / 'tracked.txt').write_text('AWS_ID = "' + REAL + '"\n')
        # Keep the key in HEAD so the added-text scan leaves it to SECURITY.
        # A run-owned comment edit makes this file blocking under SP.
        subprocess.run(['git', 'add', 'tracked.txt'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'credential fixture'], cwd=self.workspace, check=True)
        with (self.workspace / 'tracked.txt').open('a') as stream:
            stream.write('# run edit\n')
        result = self.run_coordinator('--lifecycle-mode', 'on')
        state = json.loads((self.run_dir / 'state.json').read_text())
        self.assertEqual(state['status'], 'HOLD', result.stdout + result.stderr)
        needle = 'aws-access-key-id in tracked.txt'
        self.assertEqual(state['hold_reason'].count(needle), 1, state['hold_reason'])
        [receipt] = [r for r in state['lifecycle']['receipts'] if r['stage'] == 'SECURITY']
        hits = [r for r in receipt['preflight']['findings'] if r['rule'] == 'aws-access-key-id' and r['path'] == 'tracked.txt']
        self.assertEqual(len(hits), 2)
        raw = json.loads(Path(receipt['preflight']['report']).read_text())
        self.assertEqual({r.get('source', 'worktree') for r in raw['findings'] if r['rule'] == 'aws-access-key-id'},
                         {'worktree', 'index'})

    def test_aws_documentation_fixture_completes_on_both_lifecycle_routes(self):
        (self.workspace / 'aws_fixture.py').write_text('AWS_ID = "' + EXAMPLE + '"\n')
        for mode in ('off', 'on'):
            with self.subTest(mode=mode):
                self.run_dir = self.root / ('aws-example-' + mode)
                result = self.run_coordinator('--lifecycle-mode', mode)
                self.assertIn(twl.DONE, result.stdout, result.stdout + result.stderr)
                state = json.loads((self.run_dir / 'state.json').read_text())
                self.assertFalse(any(row.get('security') for row in state['finding_ledger']))
                if mode == 'on':
                    [receipt] = [row for row in state['lifecycle']['receipts'] if row['stage'] == 'SECURITY']
                    self.assertEqual(receipt['preflight']['status'], 'clean')

    def test_refusal_explains_ordinary_review_phrase_on_both_routes(self):
        self.workitem.write_text("Show the customer's previous review on the product page.\n")
        for mode in ('on', 'off'):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError) as caught:
                    self.coordinator('--lifecycle-mode', mode)
                self.assertIn("The phrase reads as this run's review history; say it another way", str(caught.exception))
                self.assertIn('put it in backticks when the repository already contains it', str(caught.exception))
                self.assertFalse(self.run_dir.exists())

    def test_backticks_exempt_only_existing_repository_phrase_at_creation(self):
        phrase = 'previous review'
        (self.workspace / 'tracked.txt').write_text('Display the ' + phrase + '.\n')
        subprocess.run(['git', 'add', 'tracked.txt'], cwd=self.workspace, check=True)
        subprocess.run(['git', 'commit', '-qm', 'product wording'], cwd=self.workspace, check=True)
        for mode in ('on', 'off'):
            self.run_dir = self.root / ('quote-' + mode)
            self.workitem.write_text('Show the customer\'s `' + phrase + '` on the product page.\n')
            self.coordinator('--lifecycle-mode', mode)
            self.run_dir = self.root / ('absent-' + mode)
            self.workitem.write_text('Show the `earlier verdict` on the product page.\n')
            with self.assertRaisesRegex(ValueError, 'independence check rejected history'):
                self.coordinator('--lifecycle-mode', mode)


class ContentAndLimitTests(unittest.TestCase):
    def test_aws_example_is_not_a_hit_in_security_or_added_text_but_real_id_hits(self):
        for scope in (content_rules.WHOLE_DELIVERY, content_rules.ADDED_TEXT):
            self.assertEqual(content_rules.scan_text(EXAMPLE, scope), [])
            self.assertEqual(content_rules.scan_text(REAL, scope), [('aws-access-key-id', 1)])
        self.assertIsNone(leak_scan.scan_line('id = "' + EXAMPLE + '"'))
        self.assertEqual(leak_scan.scan_line('id = "' + REAL + '"'), 'aws-access-key-id')
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'body.md'
            path.write_text(EXAMPLE + '\n' + REAL)
            self.assertEqual(sp.scan_file(str(path)), [{'rule': 'aws-access-key-id', 'line': 2}])

    def assert_limit_reason(self, error, path, size, left=None, more_than=None):
        reason = str(error)
        measured = 'size ' + str(size) + ' bytes' if more_than is None else 'more than ' + str(more_than) + ' bytes'
        for needle in (path, measured, 'file limit ' + str(sp.MAX_FILE_BYTES) + ' bytes',
                       'total limit ' + str(sp.MAX_TOTAL_BYTES) + ' bytes'):
            self.assertIn(needle, reason)
        if size > sp.MAX_FILE_BYTES:
            self.assertIn('file limit exceeded', reason)
        else:
            self.assertIn('total limit exceeded after ' + str(sp.MAX_TOTAL_BYTES - left) + ' bytes scanned', reason)

    def test_worktree_file_and_total_scan_limits_identify_path_size_and_both_limits(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'large.bin'
            for size, left in ((sp.MAX_FILE_BYTES + 1, sp.MAX_TOTAL_BYTES), (6, 5)):
                with path.open('wb') as handle:
                    handle.truncate(size)
                with self.assertRaises(sp.ScanError) as caught:
                    sp._read_and_scan(scratch, path.name, {}, left)
                self.assert_limit_reason(caught.exception, path.name, size, left)

    def test_total_limit_reports_prior_bytes_for_a_file_below_both_limits(self):
        with tempfile.TemporaryDirectory() as scratch, \
                mock.patch.object(sp, 'MAX_FILE_BYTES', 1000), mock.patch.object(sp, 'MAX_TOTAL_BYTES', 700):
            path = Path(scratch) / 'c_big.bin'
            path.write_bytes(b'x' * 300)
            with self.assertRaises(sp.ScanError) as caught:
                sp._read_and_scan(scratch, path.name, {}, 200)
            self.assert_limit_reason(caught.exception, path.name, 300, 200)
            self.assertIn('total limit exceeded after 500 bytes scanned', str(caught.exception))
            proc = mock.Mock(stdout=io.BytesIO(b'x' * 300), returncode=0)
            with mock.patch.object(sp.subprocess, 'Popen', return_value=proc):
                with self.assertRaises(sp.ScanError) as staged:
                    sp._scan_git_blob('.', path.name, {'oid': 'fake'}, 200)
            self.assert_limit_reason(staged.exception, path.name, 201, 200, 200)

    def test_index_and_published_file_scan_limits_identify_path_and_limits(self):
        for size, left in ((sp.MAX_FILE_BYTES + 1, sp.MAX_TOTAL_BYTES), (6, 5)):
            proc = mock.Mock(stdout=io.BytesIO(b'x' * size), returncode=0)
            with mock.patch.object(sp.subprocess, 'Popen', return_value=proc):
                with self.assertRaises(sp.ScanError) as caught:
                    sp._scan_git_blob('.', 'staged.bin', {'oid': 'fake'}, left)
            self.assert_limit_reason(caught.exception, 'staged.bin', size, left, min(sp.MAX_FILE_BYTES, left))
            self.assertNotIn('size ', str(caught.exception))
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / 'body.md'
            with path.open('wb') as handle:
                handle.truncate(sp.MAX_FILE_BYTES + 1)
            with self.assertRaises(sp.ScanError) as caught:
                sp.scan_file(str(path))
            self.assert_limit_reason(caught.exception, str(path), sp.MAX_FILE_BYTES + 1)


if __name__ == '__main__':
    unittest.main()
