"""Content-free inventory of ignored workspace files and executable Git metadata."""
import hashlib
import os
import subprocess
from pathlib import Path
try:
    from paired_session.candidate_tree import GIT_NO_EXEC
except ImportError:
    from candidate_tree import GIT_NO_EXEC
def _raise(error):
    raise error
def _git(root, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    command = ['git', *GIT_NO_EXEC, '-c', 'core.attributesFile=/dev/null', '-C', str(root), *args]   # ls-files / rev-parse run no clean filter
    env['GIT_ATTR_NOSYSTEM'] = '1'
    result = subprocess.run(command, cwd=root, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if result.returncode or result.stderr:
        raise RuntimeError('metadata inventory Git command failed')
    return result.stdout

def _record(path):
    info = path.lstat()
    if path.is_symlink():
        data = os.readlink(path).encode('utf-8', 'surrogateescape')
        return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    digest = hashlib.sha256()
    if path.is_file():
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(65536), b''):
                digest.update(chunk)
    return {'size': info.st_size, 'sha256': digest.hexdigest()}


def _expand(paths):
    files = set()
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            for base, directories, names in os.walk(path, onerror=_raise, followlinks=False):
                files.update(Path(base) / name for name in names)
                files.update(Path(base) / name for name in directories if (Path(base) / name).is_symlink())
        else:
            files.add(path)
    return files

def snapshot(workspace):
    root = Path(workspace).resolve()
    names = _git(root, 'ls-files', '--others', '-z')
    if any(row.startswith(b'160000 ') for row in _git(root, 'ls-files', '--stage', '-z').split(b'\0')):
        raise RuntimeError('submodule gitlink cannot be inventoried')
    paths = {root / os.fsdecode(name) for name in names.split(b'\0') if name}
    git_dir = Path(os.fsdecode(_git(root, 'rev-parse', '--absolute-git-dir').strip())).resolve()
    common = Path(os.fsdecode(_git(root, 'rev-parse', '--git-common-dir').strip()))
    common = (common if common.is_absolute() else root / common).resolve()
    for base in {git_dir, common}:
        paths.add(base / 'config')
        paths.add(base / 'config.worktree')
        for name in ('hooks', 'info'):
            directory = base / name
            if directory.is_dir():
                paths.update(directory.iterdir())
    paths = _expand(paths)
    return {str(path): _record(path) for path in sorted(paths, key=str)
            if path.exists() or path.is_symlink()}
