"""FIELD-29 (v2.12.5 release check): from a checkout under /private/tmp/claude-501/..., runs HELD at EXEC r1 with "shadow
independence check rejected history in context/plan.md: claude": the review scope's "Test command:" line (and every prompt
line quoting the test command) carries the configured command, whose absolute path names a vendor. The operator's configured
commands are configuration, not review history; everything else stays scanned."""
import unittest

from paired_session.lifecycle_test_helpers import use_lifecycle_on
from paired_session import test_field26 as f26

rc = f26.rc
TEST = '/opt/claude-tools/codex-scripts/run_tests.sh --gate-review'   # a vendor and a review word in a real path
LINT = '/opt/codex-scripts/lint.sh'


class ConfiguredCommandTextTests(unittest.TestCase):
    locals().update({name: getattr(f26.ReviewOnlyCreationTextTests, name) for name in (*f26.HELPERS, 'write', 'scan')})

    def coordinator(self, *flags):
        command = self.command(*flags)
        at = command.index('--test-command')
        command[at + 1] = TEST
        return rc.Coordinator(rc.parser().parse_args([*command[2:], '--reviewer-command', LINT]))

    def prompt(self, co):   # the rendered prompt lines that quote the configured commands
        return 'Review the delta.\n' + co._test_instruction() + '\n' + co.allowed_command_prompt()

    def test_a_review_only_scope_and_prompts_quoting_the_commands_pass(self):
        self.write('sum_ints.py', 'def sum_ints(values):\n    return sum(values)\n')
        co = self.coordinator('--review-only', '--lifecycle-mode', 'on')
        self.assertIn(f'Test command: {TEST}', (co.context / 'plan.md').read_text())
        for role in ('shadow', 'gate'):
            co.materialize_review_context()
            co.assert_fresh_prompt(role, self.prompt(co))

    def test_an_ordinary_run_prompt_quoting_the_commands_passes(self):
        use_lifecycle_on(self, self)
        co = self.coordinator()
        co.state.update(phase='EXEC', next='reviewer')
        (co.context / 'plan.md').write_text('# Plan\n1. Add sum_ints.\n')
        self.write('sum_ints.py', 'def sum_ints(values):\n    return sum(values)\n')
        co.materialize_review_context()
        for role in ('shadow', 'gate'):
            co.assert_fresh_prompt(role, self.prompt(co))

    def test_the_same_words_written_by_an_author_are_still_caught(self):
        use_lifecycle_on(self, self)
        co = self.coordinator()
        co.state.update(phase='EXEC', next='reviewer')
        self.write('sum_ints.py', 'def sum_ints(values):\n    return sum(values)\n')
        for text, marker in (('# Plan\n1. Run /opt/claude-tools/other.sh first.\n', 'claude'),   # not the configured command
                             ('# Plan\n1. Claude approved this plan.\n', 'Claude')):
            with self.subTest(marker=marker):
                (co.context / 'plan.md').write_text(text)
                co.materialize_review_context()
                with self.assertRaisesRegex(RuntimeError, 'shadow independence check rejected history in context/plan.md: ' + marker):
                    co.assert_fresh_prompt('shadow', self.prompt(co))


class ShortCommandTests(unittest.TestCase):   # FIELD-29 gate: a short command must not rewrite the paths that contain it
    def test_a_short_command_keeps_patch_header_paths_so_the_base_exemption_still_applies(self):
        from paired_session import test_field23_repo_text_scan as f23
        h = f23.RepoTextScanTests('test_history_in_a_new_file_holds')
        h.setUp()
        use_lifecycle_on(self, h.h)
        self.addCleanup(h.doCleanups)
        name, base = 'tests/pytest_helpers.py', '# Claude reads this helper.\nVALUE = 1\n'
        h.commit({name: base})
        args = rc.parser().parse_args(['run', '--workspace', str(h.ws), '--workitem', str(h.h.workitem),
                                       '--run-dir', str(h.h.run_dir), '--test-command', 'pytest'])
        co = rc.Coordinator(args)
        co.state.update(phase='EXEC', next='reviewer')
        (co.context / 'plan.md').write_text('# Plan\n1. Extend the helper.\n')
        co.capture_review_baseline()
        co.save()
        h.write(name, base + '# Claude reads this helper.\n')   # the same base line again: FIELD-23 exempts it
        co.materialize_review_context()
        self.assertIn('+++ b/tests/pytest_helpers.py', (co.context / 'delta.patch').read_text())
        for role in ('shadow', 'gate'):
            co.assert_fresh_prompt(role, 'Review the delta.\n' + co._test_instruction())   # the prompt's "pytest" is still masked


if __name__ == '__main__':
    unittest.main()
