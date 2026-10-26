"""WI-WIN: the platform seam for native Windows (stdlib + ctypes only).

On POSIX every helper runs exactly the code its call site ran before, so macOS behaviour and bytes do not change.
On Windows, where a POSIX hardening (0700 + uid ownership, O_NOFOLLOW reads) has no cheap equivalent, the helper
uses the simple reasonable check: a real directory or file (no symlink, no reparse point) under the user's own
profile or temp directory. Users and models are assumed to act in good faith; these are guards against mistakes.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Optional

IS_WINDOWS = os.name == 'nt'
_REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


def user_token() -> str:
    """The per-user part of lock, temp and record names: the uid on POSIX, the (file-name safe) user name on Windows."""
    if not IS_WINDOWS:
        return str(os.getuid())
    import getpass
    return re.sub(r'[^A-Za-z0-9._-]', '_', os.environ.get('USERNAME') or getpass.getuser()) or 'user'


def uid():
    """The operator id written into records: os.getuid() on POSIX (an int, as before), the user name on Windows."""
    return os.getuid() if not IS_WINDOWS else user_token()


def _reparse_or_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & _REPARSE)


def _under_user_roots(path: Path) -> bool:
    target = Path(path).resolve()
    for root in (Path.home(), Path(tempfile.gettempdir())):
        root = root.resolve()
        if target == root or root in target.parents:
            return True
    return False


def owned(info: os.stat_result) -> bool:
    """This user owns the entry: st_uid on POSIX; on Windows st_uid is always 0, so ownership is not checked."""
    return IS_WINDOWS or info.st_uid == os.getuid()


def check_private_dir(path, mask: int = 0o077) -> bool:
    """POSIX: lstat(path) is a directory owned by this uid with none of `mask`'s permission bits (the call sites' checks).
    Windows: a real directory (no symlink, no reparse point) under the user's profile or temp root. lstat errors raise."""
    info = os.lstat(path)
    if not IS_WINDOWS:
        return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & mask
    return stat.S_ISDIR(info.st_mode) and not _reparse_or_link(info) and _under_user_roots(Path(path))


def private_file_ok(info: os.stat_result, mask: int = 0o077) -> bool:
    """A no-follow stat of a private regular file: POSIX owner + mode as the call sites check; Windows: a regular file that
    is no symlink or reparse point."""
    if not IS_WINDOWS:
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & mask
    return stat.S_ISREG(info.st_mode) and not _reparse_or_link(info)


def pid_alive(pid: int) -> bool:
    """POSIX: os.kill(pid, 0) (a PermissionError means alive; other OSErrors raise). Windows: OpenProcess +
    GetExitCodeProcess == STILL_ACTIVE; os.kill there would terminate the process."""
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    if not 0 < pid <= 0xFFFFFFFF:
        return False
    handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5   # ERROR_ACCESS_DENIED: it exists; anything else (87: no such pid) is gone
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259   # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def fsync_dir(path) -> None:
    """Flush a directory entry after a rename (POSIX); Windows cannot open a directory for fsync, so it is a no-op."""
    if IS_WINDOWS:
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace(src, dst) -> None:
    """os.replace; on Windows a reader (antivirus, indexer, another reader) briefly holding dst gives a sharing violation,
    so retry a few times before raising."""
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    for attempt in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def lock_exclusive(fd: int) -> None:
    """Nonblocking lease lock; Windows reserves byte 0, leaving holder JSON at offset 1 readable."""
    if not IS_WINDOWS:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    import errno
    import msvcrt
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise BlockingIOError(errno.EAGAIN, 'Resource temporarily unavailable') from exc
        raise


def unlock(fd: int) -> None:
    if not IS_WINDOWS:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    import msvcrt
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def read_holder(fd: int, limit: int) -> str:
    """Windows-only optional diagnostics: never read the byte another process has locked."""
    try:
        os.lseek(fd, 1, os.SEEK_SET)
        return os.read(fd, limit).decode('utf-8', 'replace').strip()
    except OSError:
        return ''


def write_holder(fd: int, payload: str) -> None:
    """Windows-only: keep the locked byte allocated, and replace the JSON beyond it."""
    data = payload.encode('utf-8')
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, b'\0' + data)
    os.ftruncate(fd, len(data) + 1)
    os.fsync(fd)


def holder_text(path, limit: int) -> str:
    """Windows-only reader for lease discovery, using the same unlocked metadata range."""
    with open(path, 'rb') as stream:
        stream.seek(1)
        return stream.read(limit).decode('utf-8', 'replace')


def temp_root() -> Path:
    """The shared temp root the POSIX call sites name literally (/tmp); the user's temp directory on Windows."""
    return Path(tempfile.gettempdir()) if IS_WINDOWS else Path('/tmp')


def devnull_path() -> str:
    """The null device for git config values (core.hooksPath, core.attributesFile, GIT_CONFIG_GLOBAL)."""
    return os.devnull if IS_WINDOWS else '/dev/null'


def read_regular_file_nofollow(path) -> bytes:
    """Read a regular file without following a link; ValueError when it cannot be opened that way, is not a regular file or
    changed while reading. POSIX: an O_NOFOLLOW open checked with fstat. Windows (no O_NOFOLLOW): lstat refuses a symlink,
    reparse point or non-regular file, then open, read and compare size and mtime before and after."""
    if IS_WINDOWS:
        try:
            before = os.lstat(path)
        except OSError as exc:
            raise ValueError('role source cannot be opened without following links') from exc
        if _reparse_or_link(before):
            raise ValueError('role source cannot be opened without following links')
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('role source is not a regular file')
        try:
            with open(path, 'rb') as stream:
                content = stream.read()
            after = os.lstat(path)
        except OSError as exc:
            raise ValueError('role source cannot be opened without following links') from exc
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(content) != after.st_size:
            raise ValueError('role source changed while reading')
        return content
    nofollow = getattr(os, 'O_NOFOLLOW', None)
    if nofollow is None:
        raise ValueError('role source no-follow reads are unavailable')
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, 'O_CLOEXEC', 0) |
                     getattr(os, 'O_NONBLOCK', 0))
    except OSError as exc:
        raise ValueError('role source cannot be opened without following links') from exc
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        os.close(fd)
        raise ValueError('role source is not a regular file')
    with os.fdopen(fd, 'rb') as stream:
        content = stream.read()
        after = os.fstat(stream.fileno())
    fields = ('st_dev', 'st_ino', 'st_mtime_ns', 'st_ctime_ns', 'st_size')
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise ValueError('role source changed while reading')
    return content


def job_name(tag: str) -> str:
    """W2b: a Job Object name unique to one dispatch: Local\\review-loop-<tag>-<random>, file-name safe characters only."""
    import uuid
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', tag)[:120]
    return f'Local\\review-loop-{safe}-{uuid.uuid4().hex[:12]}'


def check_command_line(argv: list) -> int:
    """Windows CreateProcess counts UTF-16 code units, including quoting and the final NUL."""
    import subprocess
    size = len(subprocess.list2cmdline(argv).encode('utf-16-le')) // 2 + 1
    if IS_WINDOWS and size > 32767:
        raise ValueError(f'Windows CLI command line is {size} UTF-16 units (CreateProcess limit 32767); '
                         'reduce inline --settings / --json-schema or other arguments')
    return size


def spawn_group(argv: list, tag: str, **kwargs):
    """W2b: start argv as the leader of its own process tree; (process, job name or None). POSIX: today's
    subprocess.Popen(argv, ..., start_new_session=True), no job. Windows: through winjob.py, which joins a fresh named Job
    Object before starting argv; a missing argv[0] raises FileNotFoundError here, as Popen does on POSIX."""
    import subprocess
    if not IS_WINDOWS:
        return subprocess.Popen(argv, **kwargs, start_new_session=True), None
    import errno
    import shutil
    import sys
    if shutil.which(argv[0]) is None:
        raise FileNotFoundError(errno.ENOENT, 'No such file or directory', argv[0])
    name = job_name(tag)
    launcher = [sys.executable, '-I', str(Path(__file__).resolve().with_name('winjob.py')), name, '--', *argv]
    check_command_line(argv)
    check_command_line(launcher)
    flags = kwargs.pop('creationflags', 0) | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(launcher, creationflags=flags, **kwargs), name


def _job_kernel():
    try:
        from paired_session import winjob
    except ImportError:   # the coordinator's flat-import mode
        import winjob
    return winjob.Kernel()


def kill_group(pid: int, job: Optional[str] = None) -> None:
    """W2b: kill a process tree spawn_group started. POSIX: os.killpg(pid, SIGKILL) (ProcessLookupError when gone).
    Windows: TerminateJobObject on the job opened by name; ProcessLookupError when no job of that name exists."""
    if not IS_WINDOWS:
        import signal
        os.killpg(pid, signal.SIGKILL)
        return
    if not job:
        raise ProcessLookupError('no job name is recorded for that process tree')
    kernel = _job_kernel()
    handle = kernel.open(job)
    try:
        kernel.terminate(handle, 1)
    finally:
        kernel.close(handle)


def probe_job(job: Optional[str]) -> None:
    """W2b, Windows: return while the job has a live process; ProcessLookupError when it is gone (no job of that name, or
    no active process left). Any other failure raises OSError. The POSIX probe is coordinator.retry_killpg_eperm."""
    if not job:
        raise OSError('no job name is recorded for that process tree')
    kernel = _job_kernel()
    handle = kernel.open(job)
    try:
        if kernel.active(handle) == 0:
            raise ProcessLookupError(f'job {job} has no active process')
    finally:
        kernel.close(handle)


def write_text(path, value: str) -> None:
    """Path.write_text as UTF-8 with LF kept as LF on every platform (Python 3.9's write_text has no newline argument)."""
    with open(path, 'w', encoding='utf-8', newline='') as handle:
        handle.write(value)
