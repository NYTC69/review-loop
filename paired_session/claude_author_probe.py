"""The one piece of the former Claude author escape probe that the read-only guard still uses (CG-6).

The probe itself was removed in v3.7.0; readonly_guard.py imports cli_created_dirs to recognise the directories the sandboxed
Claude CLI creates in a workspace by itself.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path


def cli_created_dirs(ws: Path, *listings: dict) -> set:
    """CG-6: `<ws>/.claude` and `<ws>/.claude/.cc-writes` that the sandboxed Claude CLI creates itself. Each counts only if it is a real
    directory (a link or file never) owned by this uid in every listing and live; `.cc-writes` must also hold nothing in any listing."""
    claude, writes = ws / '.claude', ws / '.claude' / '.cc-writes'
    admitted = set()
    for path in (claude, writes):
        try: info = os.lstat(path)
        except OSError: continue
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and all(stat.S_ISDIR(l.get(str(path), (0,))[0]) for l in listings): admitted.add(str(path))
    if any(key.startswith(str(writes) + os.sep) for l in listings for key in l): admitted.discard(str(writes))
    return admitted
