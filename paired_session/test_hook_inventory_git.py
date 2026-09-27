import os
import unittest
from unittest.mock import patch

from paired_session import hook_inventory_git as inventory_git


class HookInventoryGitTests(unittest.TestCase):
    def test_ambient_git_selectors_and_config_overrides_are_refused(self):
        values = {
            "GIT_DIR": "/operator/repo/.git",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "/operator/program",
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(
                inventory_git.HookInventoryError, "GIT_CONFIG_KEY_0.*GIT_CONFIG_VALUE_0.*GIT_DIR"
            ):
                inventory_git.checked_git_environment()

    def test_clean_environment_preserves_non_git_variables(self):
        with patch.dict(os.environ, {"PATH": "/bin"}, clear=True):
            self.assertEqual(inventory_git.checked_git_environment(), {"PATH": "/bin"})

    def test_git_plumbing_disables_fsmonitor_and_optional_hook(self):
        result = type("Result", (), {"returncode": 0, "stdout": b"ok", "stderr": b""})()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(inventory_git.subprocess, "run", return_value=result) as run:
                self.assertEqual(
                    inventory_git.git_bytes("/work", "ls-files", suppress_hooks=True),
                    b"ok",
                )
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"])
        self.assertEqual(command[-2:], ["/work", "ls-files"])

    def test_git_failure_is_closed(self):
        result = type("Result", (), {"returncode": 1, "stdout": b"", "stderr": b"bad"})()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(inventory_git.subprocess, "run", return_value=result):
                with self.assertRaisesRegex(inventory_git.HookInventoryError, "inventory command failed"):
                    inventory_git.git_bytes("/work", "config", "--get", "core.hooksPath")


if __name__ == "__main__":
    unittest.main()
