"""WI-WIN W2b: the Windows Job Object launcher for one coordinator-started process tree (stdlib + ctypes, Python 3.9).

Usage: <python> -I <absolute path>/paired_session/winjob.py Local\\review-loop-<tag> -- <argv...>

winplat.spawn_group starts this file directly (no `-m`, no package import, no PYTHONPATH, any cwd). It creates the
named Job Object, assigns itself to it before the target exists, then starts the target in the inherited cwd and
environment with this process's standard handles (the coordinator's stdin pipe and its stdout/stderr files reach the
target unchanged), waits for it and exits with its exit code. Every descendant starts inside the job; no
JOB_OBJECT_LIMIT_BREAKAWAY_OK / SILENT_BREAKAWAY_OK is set, so none can leave it.

Handle ownership: the launcher is the only long-lived holder of the job handle and holds it for its whole life. The
coordinator never keeps one: winplat.kill_group / probe_job open the job by its name (saved in the turn receipt) for one
operation and close it again, so a restarted coordinator finds the job exactly as the one that started it.
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is never set: a coordinator that dies mid-turn leaves the turn running (as a POSIX
process group does), and resume finds it alive by name. A Windows object name lives only while a handle to it is open,
so once the target has exited the launcher ends whatever the target left running in the job (TerminateJobObject with
the target's exit code, which ends the launcher too) before its handle closes: a job whose name is gone has no live
process. The one gap: a launcher killed from outside (Task Manager) drops the name while the target may still run.
Users and models are assumed to act in good faith (owner rule); this is a guard against mistakes, not an adversary.

Exit codes: the target's; 125 the job cannot be created or joined (the name is taken, not Windows, an API failure);
127 the target cannot be started; 2 a malformed command line. A one-line reason goes to stderr (the turn's stderr log).
"""
import os
import re
import subprocess
import sys

EXIT_USAGE, EXIT_JOB, EXIT_START = 2, 125, 127
NAME_RE = re.compile(r'Local\\review-loop-[A-Za-z0-9._-]{1,180}')
ERROR_ALREADY_EXISTS, ERROR_FILE_NOT_FOUND = 183, 2


class Kernel:
    """The few kernel32 calls the launcher and winplat need; each failure raises OSError with the Windows error code."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        k = self._k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.OpenJobObjectW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        k.OpenJobObjectW.restype = wintypes.HANDLE
        k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k.TerminateJobObject.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD))
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.ExitProcess.argtypes = (wintypes.UINT,)

        class Accounting(ctypes.Structure):   # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
            _fields_ = [('TotalUserTime', ctypes.c_int64), ('TotalKernelTime', ctypes.c_int64),
                        ('ThisPeriodTotalUserTime', ctypes.c_int64), ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                        ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD),
                        ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]
        self._accounting = Accounting

    def _error(self, what: str) -> OSError:
        code = self._ctypes.get_last_error()
        return OSError(None, f'{what} failed (WinError {code})', None, code)

    def create(self, name: str):
        """A new named job; an existing one of that name is refused (each dispatch has its own name)."""
        self._ctypes.set_last_error(0)
        job = self._k.CreateJobObjectW(None, name)
        if not job:
            raise self._error('CreateJobObjectW')
        if self._ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            self._k.CloseHandle(job)
            raise OSError(None, f'job {name} already exists', None, ERROR_ALREADY_EXISTS)
        return job

    def open(self, name: str):
        """The job by name for query and terminate; ProcessLookupError when no job of that name exists."""
        job = self._k.OpenJobObjectW(0x0004 | 0x0008, False, name)   # JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE
        if not job:
            error = self._error('OpenJobObjectW')
            if error.winerror == ERROR_FILE_NOT_FOUND:
                raise ProcessLookupError(f'no job {name}')
            raise error
        return job

    def assign_self(self, job) -> None:
        if not self._k.AssignProcessToJobObject(job, self._k.GetCurrentProcess()):
            raise self._error('AssignProcessToJobObject')

    def active(self, job) -> int:
        info = self._accounting()
        if not self._k.QueryInformationJobObject(job, 1, self._ctypes.byref(info), self._ctypes.sizeof(info), None):
            raise self._error('QueryInformationJobObject')   # 1: JobObjectBasicAccountingInformation
        return info.ActiveProcesses

    def terminate(self, job, code: int) -> None:
        if not self._k.TerminateJobObject(job, code & 0xFFFFFFFF):
            raise self._error('TerminateJobObject')

    def close(self, job) -> None:
        self._k.CloseHandle(job)

    def exit(self, code: int) -> None:
        """ExitProcess takes the full 32-bit code (an NTSTATUS such as 0xC000013A), which sys.exit cannot pass."""
        sys.stdout.flush(); sys.stderr.flush()
        self._k.ExitProcess(code & 0xFFFFFFFF)


def _std_handles() -> dict:
    """This process's own standard handles, passed explicitly so the target gets them whatever close_fds does."""
    handles = {}
    for key, fd in (('stdin', 0), ('stdout', 1), ('stderr', 2)):
        try:
            os.fstat(fd)
            handles[key] = fd
        except OSError:
            handles[key] = subprocess.DEVNULL
    return handles


def run(name: str, argv: list, kernel) -> int:
    """Create and join the job, run the target, end its leftovers; the exit code to report (see the module docstring)."""
    try:
        job = kernel.create(name)
    except OSError as exc:
        print(f'winjob: cannot create job {name}: {exc}', file=sys.stderr)
        return EXIT_JOB
    try:
        kernel.assign_self(job)
    except OSError as exc:
        print(f'winjob: cannot join job {name}: {exc}', file=sys.stderr)
        kernel.close(job)
        return EXIT_JOB
    try:
        target = subprocess.Popen(argv, **_std_handles())
    except OSError as exc:
        print(f'winjob: cannot start {argv[0]}: {exc}', file=sys.stderr)
        return EXIT_START
    code = target.wait()
    try:
        if kernel.active(job) > 1:   # the target left descendants running: end them (and this launcher) with its code
            kernel.terminate(job, code)
    except OSError as exc:
        print(f'winjob: cannot end the leftovers of job {name}: {exc}', file=sys.stderr)
    return code


def main(argv: list) -> int:
    # WI-WIN W9: standalone entry; same policy as winplat.configure_stdio (POSIX untouched).
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if callable(getattr(stream, 'reconfigure', None)):
                stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    if len(argv) < 3 or argv[1] != '--' or not NAME_RE.fullmatch(argv[0]):
        print('usage: winjob.py Local\\review-loop-<tag> -- <argv...>', file=sys.stderr)
        return EXIT_USAGE
    if os.name != 'nt':
        print('winjob: Job Objects need native Windows', file=sys.stderr)
        return EXIT_JOB
    kernel = Kernel()
    kernel.exit(run(argv[0], argv[2:], kernel))
    return EXIT_JOB   # not reached: ExitProcess does not return


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
