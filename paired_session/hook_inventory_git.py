"""Isolated Git plumbing shared by the hook inventory helpers."""
import os
import subprocess
from pathlib import Path


class HookInventoryError(ValueError):
    pass


def checked_git_environment():
    """Refuse ambient Git selectors so inventory observes session semantics."""
    inherited = sorted(key for key in os.environ if key.startswith("GIT_"))
    if inherited:
        names = ", ".join(inherited)
        raise HookInventoryError("ambient Git environment requires review: " + names)
    return os.environ.copy()


def git_bytes(root, *args, suppress_hooks=False):
    command = ["git", "-c", "core.fsmonitor=false"]
    if suppress_hooks:
        command.extend(("-c", "core.hooksPath=/dev/null"))
    command.extend(("-C", str(root), *args))
    result = subprocess.run(
        command,
        env=checked_git_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise HookInventoryError("Git hook inventory command failed")
    return result.stdout


def worktree_root(workspace):
    root = Path(workspace).resolve(strict=True)
    top = Path(git_bytes(root, "rev-parse", "--show-toplevel").decode().strip())
    if top.resolve() != root:
        raise HookInventoryError("workspace is not the worktree root")
    return root
