"""Offline candidate-tree plumbing for the disabled E2E lifecycle.

This module does not enable lifecycle dispatch. Callers must enforce the later
writer/reviewer OS boundaries before treating any candidate as deliverable.
"""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import unicodedata


class CandidateError(ValueError):
    pass


@dataclass(frozen=True)
class CandidateBaseline:
    root: Path
    git_dir: Path
    index: Path
    parent_head: str
    parent_ref: str
    tree_oid: str
    live_index_sha256: str
    authorized_prefixes: tuple[str, ...]
    separate_filesystems: bool


def _git_env(**overrides):
    env = {name: value for name, value in os.environ.items() if not name.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
               GIT_OPTIONAL_LOCKS='0', HOME=os.devnull, XDG_CONFIG_HOME=os.devnull,
               **overrides)
    return env


def _git(args, *, cwd=None, env=None):
    command = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
               '-c', 'core.protectHFS=true', '-c', 'core.protectNTFS=true',
               '-c', 'core.attributesFile=/dev/null', *args]
    result = subprocess.run(command, cwd=cwd, env=env or _git_env(),
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise CandidateError('git candidate operation failed: ' + result.stderr.strip())
    return result.stdout.strip()


def _git_bytes(args, *, env):
    command = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
               '-c', 'core.protectHFS=true', '-c', 'core.protectNTFS=true',
               '-c', 'core.attributesFile=/dev/null', *args]
    result = subprocess.run(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise CandidateError('git candidate object read failed: ' + result.stderr.decode(errors='replace'))
    return result.stdout


def _outside(path: Path, root: Path) -> bool:
    if path == root or root in path.parents:
        return False
    for ancestor in (path, *path.parents):
        try:
            if ancestor.samefile(root): return False
        except OSError:
            pass
    return True


def _prefixes(values) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or not values:
        raise CandidateError('authorized prefixes must be a nonempty list')
    normalized = set()
    for value in values:
        if not isinstance(value, str) or not value or '\\' in value or any(char in value for char in '*?[]{}'):
            raise CandidateError('authorized prefix is not a safe path')
        path = PurePosixPath(value)
        if (path.is_absolute() or path.as_posix() != value or
                any(part in ('', '.', '..') or part.rstrip(' .').casefold() == '.git'
                    for part in value.split('/'))):
            raise CandidateError('authorized prefix is not a normalized relative path')
        normalized.add(value)
    return tuple(sorted(normalized))


def _index_entries(env):
    entries = []
    seen = set()
    for record in _git_bytes(['ls-files', '--stage', '-z'], env=env).split(b'\0'):
        if not record: continue
        metadata, path_bytes = record.split(b'\t', 1)
        mode, oid, stage = metadata.decode().split()
        path = os.fsdecode(path_bytes)
        folded = unicodedata.normalize('NFC', path).casefold()
        if stage != '0' or folded in seen or any(part.rstrip(' .').casefold() == '.git' for part in path.split('/')):
            raise CandidateError('candidate index has unmerged or ambiguous Git path')
        seen.add(folded)
        if mode == '160000': raise CandidateError('candidate contains gitlink')
        entries.append((mode, oid, path))
    return entries


def _validate_checkout(root: Path) -> None:
    seen = set()
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if any(part.rstrip(' .').casefold() == '.git' for part in relative.parts):
            raise CandidateError('candidate contains Git metadata alias')
        folded = str(relative).casefold()
        if folded in seen:
            raise CandidateError('candidate has case-folding path collision')
        seen.add(folded)
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode) and path.stat().st_nlink > 1:
            raise CandidateError('candidate contains hardlinked file')
        if stat.S_ISLNK(mode):
            target = os.readlink(path)
            if os.path.isabs(target) or '..' in PurePosixPath(target).parts:
                raise CandidateError('candidate symlink escapes lexical tree')
            resolved = (path.parent / target).resolve()
            if not _outside(resolved, root):
                continue
            raise CandidateError('candidate symlink escapes tree')
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise CandidateError('candidate contains unsupported file type')


def prepare_candidate_baseline(workspace: Path, run_dir: Path, candidate_parent: Path,
                               metadata_parent: Path, authorized_prefixes: tuple[str, ...]) -> CandidateBaseline:
    """Materialize a clean HEAD using a scratch Git directory and isolated index."""
    workspace, run_dir = workspace.resolve(), run_dir.resolve()
    candidate_parent, metadata_parent = candidate_parent.resolve(), metadata_parent.resolve()
    authorized_prefixes = _prefixes(authorized_prefixes)
    env = _git_env()
    git_dir_text = _git(['rev-parse', '--absolute-git-dir'], cwd=workspace, env=env)
    common_text = _git(['rev-parse', '--git-common-dir'], cwd=workspace, env=env)
    common_dir = Path(common_text)
    if not common_dir.is_absolute(): common_dir = workspace / common_dir
    worktree_roots = [Path(line[9:]) for line in
                      _git(['worktree', 'list', '--porcelain'], cwd=workspace, env=env).splitlines()
                      if line.startswith('worktree ')]
    protected = [workspace, run_dir, Path(git_dir_text).resolve(), common_dir.resolve(), *worktree_roots]
    if any(not _outside(parent, root) for parent in (candidate_parent, metadata_parent)
           for root in protected):
        raise CandidateError('candidate scratch must be outside workspace/worktrees, run and Git directories')
    for key in ('core.sparseCheckout', 'core.ignoreStat'):
        proc = subprocess.run(['git', '-C', str(workspace), 'config', '--bool', key], env=env,
                              text=True, capture_output=True)
        if proc.returncode == 0 and proc.stdout.strip() == 'true':
            raise CandidateError('candidate admission refuses sparse or ignored-stat index')
    labels = _git(['ls-files', '-v', '-z'], cwd=workspace, env=env).split('\0')
    if any(label and label[0] != 'H' for label in labels):
        raise CandidateError('candidate admission refuses hidden index path flags')
    if _git(['status', '--porcelain=v1', '--untracked-files=all'], cwd=workspace, env=env):
        raise CandidateError('candidate admission requires globally clean live worktree/index')
    head = _git(['rev-parse', '--verify', 'HEAD'], cwd=workspace, env=env)
    ref = _git(['symbolic-ref', '-q', 'HEAD'], cwd=workspace, env=env)
    tree = _git(['rev-parse', 'HEAD^{tree}'], cwd=workspace, env=env)
    index_text = _git(['rev-parse', '--git-path', 'index'], cwd=workspace, env=env)
    live_index = Path(index_text)
    if not live_index.is_absolute():
        live_index = workspace / live_index
    index_hash = hashlib.sha256(live_index.read_bytes()).hexdigest()
    candidate_base = metadata_base = None
    try:
        candidate_base = Path(tempfile.mkdtemp(prefix='paired-session-tree-', dir=candidate_parent))
        metadata_base = Path(tempfile.mkdtemp(prefix='paired-session-git-', dir=metadata_parent))
        root, git_dir, index = candidate_base / 'tree', metadata_base / 'repo.git', metadata_base / 'index'
        root.mkdir()
        _git(['init', '--bare', '-q', str(git_dir)], env=env)
        _git(['--git-dir', str(git_dir), 'fetch', '--no-tags', '--quiet', str(workspace), head], env=env)
        scratch_env = _git_env(GIT_DIR=str(git_dir), GIT_INDEX_FILE=str(index),
                               GIT_WORK_TREE=str(root), GIT_CEILING_DIRECTORIES=str(candidate_base.parent))
        _git(['read-tree', head], env=scratch_env)
        entries = _index_entries(scratch_env)
        for mode, oid, path in entries:
            attributes = _git(['check-attr', '--cached', 'text', 'eol', 'ident', 'filter',
                               'working-tree-encoding', '--', path], env=scratch_env)
            if any(line.rsplit(': ', 1)[-1] not in ('unspecified', 'unset')
                   for line in attributes.splitlines()):
                raise CandidateError('candidate has transforming Git attributes')
        _git(['checkout-index', '--all', '--prefix=' + str(root) + os.sep], env=scratch_env)
        if _git(['write-tree'], env=scratch_env) != tree:
            raise CandidateError('isolated index differs from frozen parent tree')
        _validate_checkout(root)
        for mode, oid, path in entries:
            target = root / path
            actual = os.fsencode(os.readlink(target)) if mode == '120000' else target.read_bytes()
            if actual != _git_bytes(['cat-file', 'blob', oid], env=scratch_env):
                raise CandidateError('candidate checkout bytes differ from indexed blob: ' + path)
            if mode in ('100644', '100755') and bool(target.stat().st_mode & 0o111) != (mode == '100755'):
                raise CandidateError('candidate checkout mode differs from indexed blob: ' + path)
        if (_git(['rev-parse', '--verify', 'HEAD'], cwd=workspace, env=env) != head or
                hashlib.sha256(live_index.read_bytes()).hexdigest() != index_hash or
                _git(['status', '--porcelain=v1', '--untracked-files=all'], cwd=workspace, env=env)):
            raise CandidateError('live HEAD/index/worktree changed during materialization')
        separate = root.stat().st_dev not in {git_dir.stat().st_dev, index.stat().st_dev,
            workspace.stat().st_dev, (run_dir if run_dir.exists() else run_dir.parent).stat().st_dev}
        return CandidateBaseline(root, git_dir, index, head, ref, tree, index_hash,
                                 tuple(authorized_prefixes), separate)
    except BaseException:
        for path in (candidate_base, metadata_base):
            if path is not None:
                shutil.rmtree(path, ignore_errors=True)
        raise
