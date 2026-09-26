"""Real-process checks for nested-session cleanup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from process_tree import terminate_process_tree, wait_with_process_tree  # noqa: E402


SPAWN_ON_TERM = r'''
import json, os, signal, sys, time
from pathlib import Path
ready, child_record = map(Path, sys.argv[1:])
def on_term(signum, frame):
    child = os.fork()
    if child == 0:
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child_record.write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}))
        time.sleep(30)
        os._exit(0)
signal.signal(signal.SIGTERM, on_term)
ready.write_text(str(os.getpid()))
time.sleep(30)
'''

EXIT_WITH_DETACHED_CHILD = r'''
import json, os, signal, sys, time
from pathlib import Path
record = Path(sys.argv[1])
child = os.fork()
if child == 0:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
    os._exit(0)
record.write_text(json.dumps({"pid": child}))
time.sleep(0.25)
'''


def _alive(pid):
    result = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.lstrip().startswith("Z")


def test_term_handler_cannot_strand_new_setsid_child(tmp_path):
    ready = tmp_path / "ready"
    child_record = tmp_path / "child.json"
    process = subprocess.Popen(
        [sys.executable, "-c", SPAWN_ON_TERM, str(ready), str(child_record)],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        returncode, cleanup_ok, observed = terminate_process_tree(
            process, term_grace=0.8, kill_grace=1.0, poll_interval=0.01,
        )
        assert returncode is not None
        assert cleanup_ok
        assert child_record.exists(), "test did not exercise spawn-during-TERM race"
        child = json.loads(child_record.read_text())["pid"]
        assert child in observed
        assert not _alive(child)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)


def test_tracking_before_natural_parent_exit_cleans_detached_child(tmp_path):
    child_record = tmp_path / "child.json"
    process = subprocess.Popen(
        [sys.executable, "-c", EXIT_WITH_DETACHED_CHILD, str(child_record)],
        start_new_session=True,
    )
    child = None
    try:
        returncode, timed_out, known, inspectable = wait_with_process_tree(
            process, 3, poll_interval=0.01,
        )
        assert returncode == 0 and not timed_out and inspectable
        child = json.loads(child_record.read_text())["pid"]
        assert child in known
        _code, cleanup_ok, observed = terminate_process_tree(
            process, known_pids=known, term_grace=0.5, kill_grace=1,
            poll_interval=0.01,
        )
        assert cleanup_ok
        assert child in observed
        assert not _alive(child)
    finally:
        if child and _alive(child):
            try:
                os.killpg(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
