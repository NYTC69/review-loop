"""D-EFF category A (docs/efficient-mode.md §4a): a read-only turn (reviewer, gate, shadow) must not change the workspace.

capture() runs before the turn and records HEAD, the branch, the index (a `git ls-files -s` digest and a byte copy of the index file)
and the worktree as a tree object (the index copied to a temporary file, `git add -A`, `git write-tree`). After a turn that changed the
workspace, evidence() writes the diff from that tree to the worktree's tree now, and restore() puts back only what capture() recorded
and then verifies that HEAD, the branch, the index and the worktree tree all equal the recorded values. Ignored files, submodule
contents and refs other than HEAD and its branch are not recorded. The detection is the coordinator's snapshot (content and, since
rel210-fixA, the executable bit) plus HEAD, the branch, the index and (roperm) the full permission bits of every tracked and
non-ignored untracked regular file; the recorded tree carries git file modes and the undo chmods the recorded bits back, so a
mode change of any bit is undone and verified. The undo touches only the paths whose content or mode differs between
the recorded tree and the tree now, and never deletes a path that was ignored when the turn began, whatever the turn did to the
ignore rules."""
import hashlib
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Optional

try:
    from paired_session import candidate_tree
except ImportError:   # run as a script from the package directory
    import candidate_tree


def _git(workspace: Path, *args, env: Optional[dict] = None, check: bool = True, input: Optional[str] = None, raw: bool = False) -> str:
    proc = subprocess.run(candidate_tree.git_command(*args, cwd=workspace), cwd=workspace, env={**candidate_tree.git_env(), **(env or {})},
                          input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='surrogateescape', timeout=300)
    if check and proc.returncode:
        raise RuntimeError(f'git {args[0]} failed: {proc.stderr.strip()[:300]}')
    return '' if proc.returncode else proc.stdout if raw else proc.stdout.strip()   # raw: a -z path list keeps edge whitespace


def _index_path(workspace: Path) -> Path:
    return Path(_git(workspace, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))


def _worktree_tree(workspace: Path, seed: Optional[Path], scratch: Path) -> str:
    """The tree of every tracked and non-ignored untracked file as it is on disk now (stat-cached through the seed index)."""
    scratch.unlink(missing_ok=True)
    if seed is not None and seed.exists(): shutil.copy2(seed, scratch)    # keep the mtime, so git's racy-entry check still holds
    env = {'GIT_INDEX_FILE': str(scratch)}
    _git(workspace, 'add', '-A', env=env)
    return _git(workspace, 'write-tree', env=env)


def marks(workspace: Path) -> dict:
    """HEAD, the branch and the index content (a stat refresh by `git status` changes none of them)."""
    return {'head': _git(workspace, 'rev-parse', '--verify', '-q', 'HEAD', check=False),
            'branch': _git(workspace, 'symbolic-ref', '-q', 'HEAD', check=False),
            'index': hashlib.sha256(_git(workspace, 'ls-files', '-s', '-z').encode('utf-8', 'surrogateescape')).hexdigest()}


def perms(workspace: Path) -> dict:
    """roperm: the permission bits (st_mode & 0o7777) of every tracked and non-ignored untracked regular file. Git records only
    the executable bit, so a chmod 0644 -> 0600 is invisible to the tree; one lstat per file, no content read."""
    result = {}
    for name in filter(None, _git(workspace, 'ls-files', '-co', '--exclude-standard', '-z', raw=True).split('\0')):
        try: info = os.lstat(workspace / name)
        except OSError: continue
        if stat.S_ISREG(info.st_mode): result[name] = stat.S_IMODE(info.st_mode)
    return result


def perm_changes(workspace: Path, recorded: dict, missing: bool = False) -> list:
    """The recorded regular files whose permission bits differ now. A file gone or no longer regular is the tree check's, unless
    missing=True (an unrestored record: such a path keeps its constraint until it is a regular file with its old bits again)."""
    changed = []
    for name, mode in (recorded.get('perms') or {}).items():
        try: info = os.lstat(workspace / name)
        except OSError:
            if missing: changed.append(name)
            continue
        if (stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) != mode) or (missing and not stat.S_ISREG(info.st_mode)):
            changed.append(name)
    return sorted(changed)


def moved(workspace: Path, recorded: dict) -> bool:
    return any(recorded[key] != value for key, value in marks(workspace).items()) or bool(perm_changes(workspace, recorded))


def state(workspace: Path, keep: Path) -> dict:
    index = _index_path(workspace)
    return {**marks(workspace), 'index_present': index.exists(), 'tree': _worktree_tree(workspace, index, keep / 'scratch-index')}


IGNORED_LIMIT, IGNORED_SECONDS = 2000, 2.0   # eff-e: past either bound the ignored entries are not checked (noted in the receipt)


def _lstat(path: Path):
    """Type, size, mtime_ns, mode and symlink target of one entry, or None when it is gone."""
    try:
        info = os.lstat(path)
        return [stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns, info.st_mode, os.readlink(path) if stat.S_ISLNK(info.st_mode) else '']
    except OSError: return None


def ignored_changes(workspace: Path, recorded: dict) -> list:
    """The recorded ignored entries (as git lists them; a collapsed directory is one entry) that changed or are gone."""
    return sorted(name for name, meta in (recorded.get('ignored_meta') or {}).items() if _lstat(workspace / name) != meta)


def capture(workspace: Path, keep: Path) -> dict:
    keep.mkdir(parents=True, exist_ok=True)
    index = _index_path(workspace)
    if index.exists(): shutil.copy2(index, keep / 'index')
    ignored = [name.rstrip('/') for name in _git(workspace, 'ls-files', '-o', '-i', '--exclude-standard', '--directory', '-z').split('\0') if name]
    meta, deadline = {}, time.monotonic() + IGNORED_SECONDS
    for name in ignored[:IGNORED_LIMIT]:
        if time.monotonic() > deadline: break
        meta[name] = _lstat(workspace / name)
    bounded = len(meta) == len(ignored)
    return {**state(workspace, keep), 'perms': perms(workspace), 'ignored': ignored, 'ignored_meta': meta if bounded else None,
            **({} if bounded else {'ignored_note': f'ignored set too large: not checked ({len(ignored)} entries)'})}


def evidence(workspace: Path, recorded: dict, keep: Path, path: Path) -> dict:
    """Write the turn's change (HEAD, branch, index and the worktree diff) to path; return the state now."""
    now = state(workspace, keep)
    lines = [f'{key}: {recorded[key]!r} -> {now[key]!r}' for key in ('head', 'branch', 'index', 'tree') if recorded[key] != now[key]]
    lines += [f'mode: {name} {recorded["perms"][name]:04o} -> {stat.S_IMODE(os.lstat(workspace / name).st_mode):04o}'
              for name in perm_changes(workspace, recorded)]   # roperm: bits the git diff below does not show
    diff =_git(workspace, 'diff', '--binary', '--no-ext-diff', '--no-textconv', recorded['tree'], now['tree'], check=False)
    path.write_text('\n'.join(lines) + '\n\n' + diff + '\n', errors='surrogateescape')
    return now


def restore(workspace: Path, recorded: dict, keep: Path) -> Optional[str]:
    """Put back HEAD, the branch, the worktree and the index from the records; None when the result verifies, else why not."""
    try:
        if recorded['branch']:
            _git(workspace, 'symbolic-ref', 'HEAD', recorded['branch'])
            if recorded['head']: _git(workspace, 'update-ref', recorded['branch'], recorded['head'])
            else: _git(workspace, 'update-ref', '-d', recorded['branch'], check=False)   # unborn before the turn
        elif recorded['head']:
            _git(workspace, 'update-ref', '--no-deref', 'HEAD', recorded['head'])
        changed = _git(workspace, 'diff', '--name-only', '--no-renames', '-z', recorded['tree'],
                       state(workspace, keep)['tree']).split('\0')
        kept = set(filter(None, _git(workspace, 'ls-tree', '-r', '-z', '--name-only', recorded['tree']).split('\0')))
        ignored = tuple(recorded.get('ignored', ()))
        for name in filter(None, changed):
            if name not in kept and not any(name == i or name.startswith(i + '/') for i in ignored) and \
                    ((workspace / name).is_symlink() or (workspace / name).exists()):
                (workspace / name).unlink()                               # a file the turn added
        env = {'GIT_INDEX_FILE': str(keep / 'restore-index')}
        (keep / 'restore-index').unlink(missing_ok=True)
        _git(workspace, 'read-tree', recorded['tree'], env=env)
        if back := [name for name in changed if name in kept]:          # only the changed paths: the rest keep their mtimes
            _git(workspace, 'checkout-index', '-f', '-z', '--stdin', env=env, input='\0'.join(back) + '\0')   # any number of paths
        index = _index_path(workspace)
        if recorded['index_present']: shutil.copy2(keep / 'index', index)
        else: index.unlink(missing_ok=True)
        for name in perm_changes(workspace, recorded):   # roperm: after checkout-index, which writes only git's 0644/0755
            os.chmod(workspace / name, recorded['perms'][name])
        now = state(workspace, keep)
        bits = perm_changes(workspace, recorded)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        return f'{type(exc).__name__}: {exc}'
    differs = [key for key in ('head', 'branch', 'index', 'tree') if recorded[key] != now[key]] + (['permission bits'] if bits else [])
    return f'{", ".join(differs)} still differ after the restore' if differs else None
