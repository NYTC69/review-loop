"""3.0.0 compatibility for the removed advisory polish switch."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paired_session import coordinator as rc


class PolishRoundCompatibilityTests(unittest.TestCase):
    def test_operator_profile_and_cli_on_start_a_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / 'workspace'
            workspace.mkdir()
            # No git mutations or provider processes are needed for this startup contract.
            (workspace / '.git').mkdir()
            workitem = root / 'WORKITEM.md'
            workitem.write_text('# Toy\nCreate sum_ints.\n')
            profile = root / 'operator.json'
            profile.write_text(json.dumps({'polish_round': 'on', 'lifecycle_mode': 'on'}))
            self.assertNotIn(workspace, profile.parents)

            def start(args):
                co = rc.Coordinator(args)
                self.assertEqual(co.state['status'], 'ACTIVE')
                self.assertEqual(co.state['phase'], 'PLAN')
                self.assertEqual(args.polish_round, 'on')
                self.assertNotIn('polish_round', co.state['config'])
                return 0

            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                stack.enter_context(patch.object(rc, 'program_snapshot', return_value=({'codex_bin': {'path': '/usr/bin/true'}}, None)))
                stack.enter_context(patch.object(rc, 'refuse_default_codex_model_on_old_cli'))
                stack.enter_context(patch.object(rc.Coordinator, '_head_commit', return_value='a' * 40))
                stack.enter_context(patch.object(rc.Coordinator, '_introduced_history', return_value=[]))
                stack.enter_context(patch.object(rc.Coordinator, '_capture_security_baseline', return_value={}))
                stack.enter_context(patch.object(rc.Coordinator, '_start_ignore_coverage', return_value={}))
                dispatch = stack.enter_context(patch.object(rc, '_execute_locked', side_effect=start))
                for name, extra in (('profile', ['--config', str(profile)]),
                                    ('cli', ['--polish-round', 'on'])):
                    with self.subTest(source=name):
                        run = root / name
                        argv = ['run', '--workspace', str(workspace), '--workitem', str(workitem),
                                '--run-dir', str(run), '--test-command', '/usr/bin/true'] + extra
                        self.assertEqual(rc.main(argv), 0)
                        self.assertTrue((run / 'state.json').is_file())
                self.assertEqual(dispatch.call_count, 2)
                self.assertNotIn('--polish-round', rc.parser().format_help())
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    rc.parser().parse_args(argv + ['--polish-round', 'invalid'])
                self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
