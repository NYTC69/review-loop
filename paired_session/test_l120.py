"""L120 (legacy-map row, owner 2026-10-05 "keep (fill the gap)"): both accept routes write delivery-report.md with the legacy
Step 4 summary fields (findings per severity with their final status, PLAN/EXEC rounds, verdicts, tokens per vendor), and
the accept output names the report."""
import json
import unittest

from paired_session import review_report
from paired_session import test_worktree_lifecycle as twl

rc, DONE = twl.rc, twl.DONE
ADVISORY = {'FAKE_EXEC_MINOR_REVISE': '1'}   # every EXEC review leaves a MINOR finding; the final round lets it pass


class DeliveryReportTests(unittest.TestCase):
    locals().update({name: getattr(twl.WorktreeLifecycleActivationTests, name) for name in (*twl._HELPERS, 'setUp', 'git')})

    def accept_and_check(self, run, title):
        done = self.run_coordinator(*run, env=ADVISORY)
        self.assertIn(DONE, done.stdout, done.stdout + done.stderr)
        accepted = self.run_operator_action('accept', *run)
        report = self.run_dir / 'delivery-report.md'
        self.assertEqual(accepted.stdout.strip().splitlines()[-2:], [f'REPORT: {report}', 'ACCEPTED'],
                         accepted.stdout + accepted.stderr)
        state, text = json.loads((self.run_dir / 'state.json').read_text()), report.read_text()
        self.assertTrue(text.startswith(title), text)
        self.assertTrue(text.endswith(review_report.delivery_section(state)), text)   # the same section on both routes
        self.assertIn('- Critical：0 条', text)
        self.assertIn(f"- PLAN {state['plan_rounds']} 轮，EXEC {state['exec_rounds']} 轮", text)
        vendor_of = {turn['sequence']: turn['vendor'] for turn in state['turns']}
        usage = json.loads((self.run_dir / 'usage.json').read_text())['turns']   # write_usage's own per-turn rows
        for vendor in set(vendor_of.values()):
            rows = [row for row in usage if vendor_of[row['turn']] == vendor]
            self.assertIn(f"- {vendor}：{len(rows)} 次调用，输入 {sum(r['input'] for r in rows)}（其中缓存 "
                          f"{sum(r['cached'] for r in rows)}），输出 {sum(r['output'] for r in rows)}", text)
        return state, text

    def test_the_section_counts_every_ledger_row_once_with_its_final_status(self):
        ledger = [{'id': 'F001', 'severity': 'CRITICAL', 'status': 'fixed'}, {'id': 'F002', 'severity': 'HIGH', 'status': 'open'},
                  {'id': 'F003', 'severity': 'MAJOR', 'status': 'withdrawn'}, {'id': 'F004', 'severity': 'MEDIUM', 'status': 'open'},
                  {'id': 'F005', 'severity': 'LOW', 'status': 'open'}, {'id': 'F006', 'severity': 'MINOR', 'status': 'fixed'},
                  {'id': 'F007', 'severity': 'NIT', 'status': 'open'}]
        turns = [{'vendor': 'claude', 'usage_requests': [{'input': 10, 'cached': 4, 'output': 2}, {'input': 5, 'cached': 1, 'output': 3}]},
                 {'vendor': 'codex', 'usage_requests': [{'input': 7, 'cached': 0, 'output': 1}]}, {'vendor': 'codex'}]
        state = {'finding_ledger': ledger, 'turns': turns, 'plan_rounds': 2, 'exec_rounds': 3,
                 'exec_comparisons': [{'persistent': {'verdict': 'APPROVE'}, 'gate': {'verdict': 'REVISE'}}]}
        self.assertEqual(review_report.delivery_section(state).splitlines(), [
            '## 发现（按严重度，最终状态）', '- Critical：1 条（fixed 1）', '- Security：0 条',
            '- Important：3 条（open 2，withdrawn 1）；未关闭：F002, F004', '- Suggestions：2 条（open 1，fixed 1）；未关闭：F005',
            '- Other：1 条（open 1）；未关闭：F007', '', '## 轮次', '- PLAN 2 轮，EXEC 3 轮', '', '## 判定（最后一轮）',
            '- EXEC reviewer：APPROVE', '- gate：REVISE', '', '## Token 用量（按供应商，明细见 usage.md）',
            '- claude：1 次调用，输入 15（其中缓存 5），输出 5', '- codex：2 次调用，输入 7（其中缓存 0），输出 1；1 次无用量记录'])
        self.assertEqual(review_report.delivery_section({}).splitlines()[-1], '- 无')   # no turns, no verdicts

    def test_the_w_route_report_carries_the_summary_fields(self):
        (self.workspace / 'sum_ints.py').write_text('def sum_ints(values):\n    return sum(values)\n')
        state, text = self.accept_and_check(('--review-only', '--lifecycle-mode', 'on', '--max-invocations', '80',
                                             '--max-exec-rounds', '1'), '# 交付报告（worktree lifecycle）')
        minor = [row for row in state['finding_ledger'] if review_report._section(row) == 'MINOR']
        open_ids = [row['id'] for row in minor if row['status'] == 'open']
        self.assertTrue(open_ids)   # the advisory MINOR finding stays open on W
        self.assertIn(f'- Suggestions：{len(minor)} 条（', text)
        self.assertIn('；未关闭：' + ', '.join(open_ids), text)
        gate = state['exec_comparisons'][-1]['gate']['verdict']
        self.assertIn(f'- gate：{gate}', text)
        self.assertIn('- security reviewer：', text)

    def test_the_lifecycle_off_route_writes_the_same_report(self):
        state, text = self.accept_and_check(('--lifecycle-mode', 'off', '--shadow', 'off', '--adversarial-gate', 'off',
                                             '--max-exec-rounds', '1'), '# 交付报告（lifecycle off）')
        self.assertIn('- 交付：lifecycle off 不提交，改动留在工作区', text)
        self.assertIn(f"- 用量：调用 {state['invocations_used']} 次", text)
        self.assertIn('- EXEC reviewer：', text)


if __name__ == '__main__':
    unittest.main()
