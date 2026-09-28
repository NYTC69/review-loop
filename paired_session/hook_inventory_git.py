"""Isolated Git plumbing shared by the hook inventory helpers."""
import os
import subprocess
from pathlib import Path

_READ_ONLY_ARGV = {
    ("rev-parse", "--show-toplevel"), ("rev-parse", "--git-common-dir"),
    ("rev-parse", "--git-dir"), ("rev-parse", "--path-format=absolute", "--git-path", "hooks"),
    ("config", "--null", "--type=path", "--get-all", "core.hooksPath"),
    ("config", "--show-origin", "--show-scope", "--get-all", "core.hooksPath"),
    ("ls-files",), ("ls-files", "-z"), ("ls-files", "-z", "--stage"),
}
_UI_ENV = {"GIT_PAGER", "GIT_EDITOR"}


class HookInventoryError(ValueError):
    pass


def checked_git_environment():
    """Refuse ambient Git selectors so inventory observes session semantics."""
    inherited = sorted(key for key in os.environ if key.startswith("GIT_") and key not in _UI_ENV)
    if inherited:
        names = ", ".join(inherited)
        raise HookInventoryError("ambient Git environment requires review: " + names)
    environment = os.environ.copy()
    for key in _UI_ENV:
        environment.pop(key, None)
    return environment


def git_result(root, *args, timeout=10):
    if tuple(args) not in _READ_ONLY_ARGV:
        raise HookInventoryError("Git hook inventory command failed: unreviewed command")
    command = ["git", "-c", "core.fsmonitor=false"]
    selector_read = args[0] == "config" or args[:3] == ("rev-parse", "--path-format=absolute", "--git-path")
    if not selector_read:
        command.extend(("-c", "core.hooksPath=/dev/null"))
    command.extend(("-c", "core.fsmonitor="))
    command.extend(("-C", str(root), *args))
    environment = checked_git_environment()
    try:
        result = subprocess.run(command, env=environment,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise HookInventoryError("Git hook inventory command failed") from exc
    return result


def git_bytes(root, *args, suppress_hooks=True, timeout=10):
    if not args:
        raise HookInventoryError("empty Git hook inventory command")
    if not suppress_hooks and args[0] != "config":
        raise HookInventoryError("hook execution is disabled during inventory")
    result = git_result(root, *args, timeout=timeout)
    if result.returncode:
        raise HookInventoryError("Git hook inventory command failed")
    return result.stdout


def worktree_root(workspace):
    try:
        root = Path(workspace).resolve(strict=True)
        top_text = git_bytes(root, "rev-parse", "--show-toplevel").decode("utf-8", "strict")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise HookInventoryError("invalid Git worktree root") from exc
    top = Path(top_text.rstrip("\n"))
    if top.resolve() != root:
        raise HookInventoryError("workspace is not the worktree root")
    return root
