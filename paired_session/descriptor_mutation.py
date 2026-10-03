"""No-follow file publication with exclusive namespace moves and retained originals."""
import ctypes
import os
from pathlib import PurePosixPath
import stat
import uuid


_LIBC = ctypes.CDLL(None, use_errno=True)


def _move(source_fd, source, target_fd, target):
    try:
        call = _LIBC.renameatx_np
    except AttributeError as exc:
        raise ValueError('exclusive rename unavailable; inspect/recover or abort') from exc
    call.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    call.restype = ctypes.c_int
    if call(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 4):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), source)


def _identity(info):
    return info.st_dev, info.st_ino


def _value(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    if stat.S_ISLNK(info.st_mode):
        return ('120000', os.fsencode(os.readlink(name, dir_fd=fd))), info
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('unsafe mutation target; inspect/recover or abort')
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        with os.fdopen(child, 'rb', closefd=False) as stream:
            raw = stream.read()
        after = os.fstat(child)
        if (_identity(info), info.st_mtime_ns, info.st_ctime_ns) != (
                _identity(after), after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('mutation target changed while reading; inspect/recover or abort')
        return ('100755' if info.st_mode & 0o111 else '100644', raw), info
    finally:
        os.close(child)


def mutate(root, name, allowed, desired, quarantine):
    """Move old entries without overwriting; retain original bytes for operator recovery."""
    parts = PurePosixPath(name).parts
    if not parts or any(part in ('', '.', '..') for part in parts) or str(PurePosixPath(name)) != name:
        raise ValueError('unsafe mutation path')
    if PurePosixPath(name).is_absolute():
        raise ValueError('absolute mutation path')
    descriptors = []
    stage_name = 'paired-session-mutation-' + uuid.uuid4().hex
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(parent)
        root_identity = _identity(os.fstat(parent))
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                os.mkdir(part, 0o755, dir_fd=parent)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(child)
            parent = child
        value, before = _value(parent, parts[-1])
        if value not in allowed:
            raise ValueError('foreign mutation bytes; inspect/recover or abort')
        if value == desired:
            return {'changed': False, 'path': name}
        store = os.open(quarantine, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(store)
        if os.fstat(store).st_dev != os.fstat(parent).st_dev:
            raise ValueError('mutation quarantine must share target filesystem')
        os.mkdir(stage_name, 0o700, dir_fd=store)
        stage = os.open(stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=store)
        descriptors.append(stage)
        if desired is not None:
            mode, raw = desired
            if mode == '120000':
                os.symlink(os.fsdecode(raw), 'new', dir_fd=stage)
            elif mode in ('100644', '100755'):
                fd = os.open('new', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=stage)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o755 if mode == '100755' else 0o644)
                    os.fsync(stream.fileno())
            else:
                raise ValueError('unsupported mutation mode')
        if before is not None:
            _move(parent, parts[-1], stage, 'old')
            try:
                moved, identity = _value(stage, 'old')
                if _identity(identity) != _identity(before) or moved != value:
                    raise ValueError('target swapped')
            except (ValueError, OSError):
                try:
                    _move(stage, 'old', parent, parts[-1])
                except OSError:
                    pass
                raise ValueError('target swapped; originals retained in ' + str(quarantine / stage_name))
        if desired is not None:
            _move(stage, 'new', parent, parts[-1])
        if _identity(os.stat(root, follow_symlinks=False)) != root_identity:
            raise ValueError('mutation root replaced; inspect/recover or abort')
        return {'changed': True, 'path': name, 'originals': str(quarantine / stage_name),
                'parent_identity': list(_identity(os.fstat(parent)))}
    except (OSError, ValueError) as exc:
        raise ValueError('mutation refused; inspect/recover or abort; quarantine ' +
                         str(quarantine / stage_name) + ': ' + str(exc)) from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
