import hashlib
import os
import stat
from pathlib import Path
from importlib import import_module
_git = import_module(('paired_session.' if __package__ else '') + 'hook_inventory_git')
HookInventoryError, git_bytes, worktree_root = _git.HookInventoryError, _git.git_bytes, _git.worktree_root
_RUNNABLE = {'pre-commit', 'prepare-commit-msg', 'commit-msg'}
def inventory(workspace):
    root = worktree_root(workspace)
    config = git_bytes(root, 'config', '--null', '--list', '--show-origin', '--show-scope')
    fields = config.split(b'\0')
    if not fields or fields[-1] or (len(fields) - 1) % 3: raise HookInventoryError('malformed Git config inventory')
    if any(row.split(b'\n', 1)[0].lower().startswith(b'hook.') for row in fields[2:-1:3]):
        raise HookInventoryError('config-defined Git hook requires explicit support')
    raw = git_bytes(root, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks').rstrip(b'\n')
    hooks = Path(os.fsdecode(raw))
    if not hooks.is_absolute() or hooks.is_symlink():
        raise HookInventoryError('hook directory is not an absolute non-symlink path')
    rows = []
    if hooks.exists() and hooks != Path('/dev/null'):
        directory = os.open(hooks, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            before = os.fstat(directory)
            names = sorted(os.listdir(directory))
            for name in names:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise HookInventoryError('non-file hook entry: ' + name)
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    first = os.fstat(fd)
                    data = b''
                    while block := os.read(fd, 1024 * 1024):
                        data += block
                    last = os.fstat(fd)
                finally:
                    os.close(fd)
                fields = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
                if any(getattr(first, key) != getattr(last, key) for key in fields):
                    raise HookInventoryError('hook changed during inventory: ' + name)
                if first.st_mode & 0o111 and not name.endswith('.sample') and name not in _RUNNABLE:
                    raise HookInventoryError('unsupported active Git hook: ' + name)
                rows.append((name, first.st_mode, hashlib.sha256(data).hexdigest()))
            after = os.fstat(directory)
            if before.st_ino != after.st_ino or names != sorted(os.listdir(directory)):
                raise HookInventoryError('hook directory changed during inventory')
        finally:
            os.close(directory)
    return {'workspace': str(root), 'hooks_dir': str(hooks),
            'config_sha256': hashlib.sha256(config).hexdigest(), 'entries': rows}
def require_unchanged(workspace, frozen):
    current = inventory(workspace)
    if current != frozen:
        raise HookInventoryError('Git hook inventory changed')
    return current
