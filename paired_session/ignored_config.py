"""F3, conservative (Category B, report only): ignored executable-config files a writer turn creates or changes.

Snapshot and delta leave gitignored files out, so an author could write an editor task, an MCP server entry or a shell
hook that a later operator session or tool runs, and no reviewer would see it. Before and after each author, FINISH and
DOCS writer turn the coordinator takes lstat metadata (no content) of the executable-config candidates below that git
ignores, and reports the new or changed ones: a receipt field, a WARNING line, the delivery report and status. It never
holds (D-EFF: no new Category C gate; strict is frozen). `.git/hooks` and the other git control files are not listed
here: an author change there is already a HOLD (git_control_state).

author_write_guard.py (an offline content inventory of every ignored and untracked path plus Git metadata, failing
closed) is not used: it hashes all content of an unbounded set and raises on any error, while this report must stay
bounded, content-free and never stop a run."""
import os
import stat
import time
from pathlib import Path

CONFIG_FILES = ('.vscode/tasks.json', '.vscode/launch.json', '.vscode/settings.json', '.idea/workspace.xml', '.mcp.json',
                '.envrc', 'Makefile.local', '.pre-commit-config.yaml')
LIMIT, SCAN_LIMIT, SECONDS = 500, 5000, 2.0   # candidates, directory entries looked at, wall time: past any bound the
                                              # inventory is partial, and the receipt says so


def _lstat(path: Path):
    try:
        info = os.lstat(path)
        return [stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns, info.st_mode,
                os.readlink(path) if stat.S_ISLNK(info.st_mode) else '']
    except OSError: return None


def _real_dir(workspace: Path, relative: str) -> bool:
    """Every component of `relative` is a directory and none is a symlink: nothing outside the workspace is looked at."""
    path = workspace
    for part in Path(relative).parts:
        path = path / part
        try: mode = os.lstat(path).st_mode
        except OSError: return False
        if not stat.S_ISDIR(mode): return False
    return True


class _Budget:
    def __init__(self, deadline: float):
        self.deadline, self.seen = deadline, 0

    def within(self) -> bool:
        return self.seen <= SCAN_LIMIT and time.monotonic() <= self.deadline

    def spend(self) -> bool:   # one directory entry looked at; False once any bound is passed
        self.seen += 1
        return self.within()


def candidates(workspace: Path, deadline: float) -> tuple:
    """Existing candidate paths (workspace-relative), and whether the listing stayed within the bounds. Directories are
    read one entry at a time (scandir), each entry spending the shared budget, so no bound is checked only afterwards."""
    budget, found = _Budget(deadline), []
    for name in CONFIG_FILES:
        if not budget.spend(): return sorted(found), False
        parent = os.path.dirname(name)
        if (not parent or _real_dir(workspace, parent)) and os.path.lexists(workspace / name):
            found.append(name)
    if _real_dir(workspace, '.claude'):
        with os.scandir(workspace / '.claude') as entries:
            for entry in entries:
                if not budget.spend() or len(found) >= LIMIT: return sorted(found)[:LIMIT], False
                if entry.name.startswith('settings') and entry.name.endswith('.json'):
                    found.append('.claude/' + entry.name)
    pending = ['.claude/commands'] if _real_dir(workspace, '.claude/commands') else []
    while pending:   # depth-first, never into a symlinked directory
        directory = pending.pop()
        with os.scandir(workspace / directory) as entries:
            for entry in entries:
                if not budget.spend() or len(found) >= LIMIT: return sorted(found)[:LIMIT], False
                relative = directory + '/' + entry.name
                if entry.is_dir(follow_symlinks=False): pending.append(relative)
                else: found.append(relative)
    return sorted(found)[:LIMIT], budget.within() and len(found) <= LIMIT


def inventory(workspace: Path, ignored) -> dict:
    """{'entries': {path: lstat}} of the ignored candidates; `ignored(paths)` answers which paths git ignores.
    A partial or failed inventory carries a 'note' and never raises."""
    try:
        names, complete = candidates(workspace, time.monotonic() + SECONDS)
        kept = sorted(ignored(names)) if names else []
        entries = {name: _lstat(workspace / name) for name in kept}
    except (OSError, RuntimeError, ValueError) as exc:
        return {'entries': {}, 'note': f'ignored executable-config inventory failed ({type(exc).__name__}: {exc})'}
    return {'entries': entries, **({} if complete else {'note': f'ignored executable-config inventory partial (over {LIMIT} '
                                                              f'candidates, {SCAN_LIMIT} entries or {SECONDS:g} s)'})}


def written(before: dict, after: dict) -> list:
    """The ignored candidates that are new or changed after the turn (a removed one is not listed). After a partial
    baseline only the entries it holds are compared, so a file that existed before is never reported as new."""
    old = before.get('entries') or {}
    known = (lambda name: name in old) if before.get('note') else (lambda name: True)
    return sorted(name for name, meta in (after.get('entries') or {}).items()
                  if meta is not None and known(name) and old.get(name) != meta)
