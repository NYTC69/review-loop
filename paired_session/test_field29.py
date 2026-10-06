"""FIELD-29 (v2.12.5 release check): from a checkout under /private/tmp/claude-501/..., runs HELD at EXEC r1 with "shadow
independence check rejected history in context/plan.md: claude": the review scope's "Test command:" line (and every prompt
line quoting the test command) carries the configured command, whose absolute path names a vendor. The operator's configured
commands are configuration, not review history; everything else stays scanned."""
import unittest

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
        co = self.coordinator()
        co.state.update(phase='EXEC', next='reviewer')
        (co.context / 'plan.md').write_text('# Plan\n1. Add sum_ints.\n')
        self.write('sum_ints.py', 'def sum_ints(values):\n    return sum(values)\n')
        co.materialize_review_context()
        for role in ('shadow', 'gate'):
            co.assert_fresh_prompt(role, self.prompt(co))

    def test_the_same_words_written_by_an_author_are_still_caught(self):
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


if __name__ == '__main__':
    unittest.main()
