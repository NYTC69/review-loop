import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paired_session import hook_inventory_git as inventory_git
from paired_session import timeout_scale as tsc


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
            with patch.object(inventory_git.subprocess, "run", return_value=result) as run:
                with self.assertRaisesRegex(inventory_git.HookInventoryError, "inventory command failed"):
                    inventory_git.git_bytes("/work", "ls-files", "-z")
                run.assert_called_once()

    def test_ui_git_environment_is_removed_and_selectors_fail_before_spawn(self):
        with patch.dict(os.environ, {"GIT_PAGER": "pager", "GIT_EDITOR": "editor"}, clear=True):
            self.assertEqual(inventory_git.checked_git_environment(), {})
        with patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": "x=y"}, clear=True):
            with patch.object(inventory_git.subprocess, "run") as run:
                with self.assertRaisesRegex(inventory_git.HookInventoryError, "GIT_CONFIG_PARAMETERS"):
                    inventory_git.git_bytes("/work", "rev-parse", "--show-toplevel")
                run.assert_not_called()

    def test_inventory_git_api_is_read_only_and_suppresses_hooks_by_default(self):
        result = type("Result", (), {"returncode": 0, "stdout": b"ok", "stderr": b""})()
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(inventory_git.subprocess, "run", return_value=result) as run:
                self.assertEqual(inventory_git.git_bytes("/work", "ls-files"), b"ok")
                command = run.call_args.args[0]
                self.assertIn("core.hooksPath=/dev/null", command)
                inventory_git.git_result("/work", "config", "--null", "--type=path",
                                         "--get-all", "core.hooksPath")
                self.assertNotIn("core.hooksPath=/dev/null", run.call_args.args[0])
                with self.assertRaisesRegex(inventory_git.HookInventoryError, "unreviewed"):
                    inventory_git.git_bytes("/work", "show", "HEAD")
                with self.assertRaisesRegex(inventory_git.HookInventoryError, "unreviewed"):
                    inventory_git.git_bytes("/work", "status", "--short")
                with self.assertRaisesRegex(inventory_git.HookInventoryError, "unreviewed"):
                    inventory_git.git_bytes("/work", "config", "--unset", "core.hooksPath")
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(run.call_args.kwargs["timeout"], 10)
                self.assertNotIn("GIT_PAGER", run.call_args.kwargs["env"])

    def test_invalid_root_error_is_normalized(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(inventory_git.HookInventoryError, "command failed"):
                inventory_git.git_bytes("\0", "ls-files")

    def test_worktree_root_uses_real_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("GIT_")}
            environment["HOME"] = temporary
            with patch.dict(os.environ, environment, clear=True):
                subprocess.run(["git", "init", "-q", str(root)], check=True)
                self.assertEqual(inventory_git.worktree_root(root), root.resolve())

    def test_empty_fsmonitor_override_beats_real_repository_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            template = Path(temporary) / "empty-template"
            template.mkdir()
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("GIT_")}
            environment["HOME"] = temporary
            with patch.dict(os.environ, environment, clear=True):
                subprocess.run(["git", "init", "-q", "--template=" + str(template), str(root)], check=True)
                subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root),
                                "config", "user.name", "Inventory"], check=True)
                subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root),
                                "config", "user.email", "inventory@example.test"], check=True)
                tracked = root / "tracked.txt"
                tracked.write_text("tracked\n")
                subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root),
                                "add", "tracked.txt"], check=True)
                subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root),
                                "commit", "-qm", "base"], check=True)
                marker = Path(temporary) / "fsmonitor-ran"
                monitor = Path(temporary) / "fsmonitor.sh"
                monitor.write_text("#!/bin/sh\nprintf called > " + str(marker) + "\nexit 1\n")
                monitor.chmod(0o755)
                subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root),
                                "config", "core.fsmonitor", str(monitor)], check=True)
                environment = inventory_git.checked_git_environment()
                inventory_git.git_bytes(root, "ls-files", "-z", "--stage")
                self.assertFalse(marker.exists())
                positive = ["git", "-C", str(root), "ls-files", "-z", "--stage"]
                subprocess.run(positive, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=tsc.scaled(10), check=True)
                self.assertTrue(marker.exists(), "same-command fsmonitor control did not execute")


if __name__ == "__main__":
    unittest.main()
