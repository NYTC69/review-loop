#!/usr/bin/env python3
"""Bounded POSIX process-tree termination across nested process groups.

Children may call ``setsid()`` and leave their parent's process group.  A
single ``killpg(root_pid, ...)`` therefore is not a complete cleanup.  This
module repeatedly samples the process table during TERM and KILL grace periods,
retains every observed descendant as a future traversal root, and verifies that
the observed tree is gone before returning.
"""
from __future__ import annotations

import os
import signal
import shutil
import subprocess
import time

_ORIGINAL_POPEN = subprocess.Popen
_PS = next((path for path in ("/bin/ps", "/usr/bin/ps") if os.path.isfile(path)),
           shutil.which("ps"))


def _process_table():
    """Return ``pid -> (ppid, pgid, state)`` or ``None`` if unavailable."""
    if _PS is None:
        return None
    try:
        process = _ORIGINAL_POPEN(
            [_PS, "-axo", "pid=,ppid=,pgid=,state="],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        raw, _stderr = process.communicate(timeout=1)
        if process.returncode != 0:
            return None
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        return None
    except OSError:
        return None
    table = {}
    try:
        for line in raw.splitlines():
            parts = line.split(None, 3)
            if len(parts) != 4:
                continue
            pid, ppid, pgid = (int(value) for value in parts[:3])
            table[pid] = (ppid, pgid, parts[3].decode("ascii", errors="ignore"))
    except (TypeError, ValueError):
        return None
    return table


def _observed_tree(root_pid, known, table):
    """Find descendants of the root and of descendants seen in prior scans."""
    if table is None:
        return {}
    children = {}
    for pid, (ppid, _pgid, _state) in table.items():
        children.setdefault(ppid, set()).add(pid)
    frontier = {root_pid} | {pid for pid in known if pid in table}
    found = {}
    visited = set()
    while frontier:
        parent = frontier.pop()
        if parent in visited:
            continue
        visited.add(parent)
        if parent != root_pid and parent in table:
            _ppid, pgid, state = table[parent]
            found[parent] = (pgid, state)
        frontier.update(children.get(parent, ()))
    return found


def observe_process_tree(root_pid, known=()):
    """Return the currently visible tree while retaining earlier roots."""
    table = _process_table()
    if table is None:
        return None
    return _observed_tree(root_pid, set(known), table)


def wait_with_process_tree(process, timeout, *, poll_interval=0.1):
    """Bounded wait that remembers descendants before their parent exits."""
    if timeout <= 0 or poll_interval <= 0:
        raise ValueError("wait timeout and polling interval must be positive")
    deadline = time.monotonic() + timeout
    known = set()
    inspectable = True
    while True:
        observed = observe_process_tree(process.pid, known)
        if observed is None:
            inspectable = False
        else:
            known.update(observed)
        returncode = process.poll()
        if returncode is not None:
            return returncode, False, tuple(sorted(known)), inspectable
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, True, tuple(sorted(known)), inspectable
        try:
            returncode = process.wait(timeout=min(poll_interval, remaining))
            return returncode, False, tuple(sorted(known)), inspectable
        except subprocess.TimeoutExpired:
            pass


def _signal_observed(root_pid, observed, sig, *, include_root):
    """Signal isolated groups and individual PIDs, never the caller's group."""
    caller_pgid = os.getpgrp()
    groups = {root_pid} if include_root else set()
    groups.update(pgid for pgid, _state in observed.values() if pgid > 0)
    for pgid in groups:
        if pgid == caller_pgid:
            continue
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    for pid in observed:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def terminate_process_tree(process, *, known_pids=(), term_grace=3.0,
                           kill_grace=2.0, poll_interval=0.05):
    """Terminate ``process`` and every descendant observed during cleanup.

    Returns ``(returncode, cleanup_ok, observed_pids)``.  ``cleanup_ok`` is
    false when the process table cannot be inspected, the leader cannot be
    reaped within the deadline, or any observed non-zombie descendant remains.
    The caller must fail closed when it is false.
    """
    if term_grace < 0 or kill_grace < 0 or poll_interval <= 0:
        raise ValueError("cleanup deadlines must be nonnegative and polling positive")
    known = set(known_pids)
    inspectable = True

    def scan_and_signal(sig):
        nonlocal inspectable
        table = _process_table()
        if table is None:
            inspectable = False
            observed = {}
        else:
            observed = _observed_tree(process.pid, known, table)
            known.update(observed)
        _signal_observed(
            process.pid, observed, sig, include_root=process.poll() is None,
        )
        return observed

    # Snapshot before TERM so an already-detached native child is retained even
    # when its direct parent exits immediately after receiving the signal.
    initial = scan_and_signal(signal.SIGTERM)
    if process.poll() is not None and not initial:
        try:
            returncode = process.wait(timeout=max(poll_interval, 0.05))
        except subprocess.TimeoutExpired:
            returncode = None
        return returncode, bool(inspectable and returncode is not None), tuple(sorted(known))
    deadline = time.monotonic() + term_grace
    while time.monotonic() < deadline:
        observed = scan_and_signal(signal.SIGTERM)
        if process.poll() is not None and not observed:
            break
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    # Rescan after the TERM grace period.  This closes the common race where a
    # signal handler creates a new session/process group during shutdown.
    scan_and_signal(signal.SIGKILL)
    deadline = time.monotonic() + kill_grace
    observed = {}
    while time.monotonic() < deadline:
        observed = scan_and_signal(signal.SIGKILL)
        if process.poll() is not None and not observed:
            break
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    try:
        returncode = process.wait(timeout=max(poll_interval, 0.05))
    except subprocess.TimeoutExpired:
        returncode = None
    table = _process_table()
    if table is None:
        inspectable = False
        survivors = known
    else:
        final = _observed_tree(process.pid, known, table)
        # Zombies have exited and await a different parent; they cannot run or
        # mutate the workspace, so do not report them as live survivors.
        survivors = {pid for pid, (_pgid, state) in final.items()
                     if not state.startswith("Z")}
    return returncode, bool(inspectable and returncode is not None and not survivors), tuple(sorted(known))
