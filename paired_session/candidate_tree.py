"""Offline candidate-tree plumbing for the disabled E2E lifecycle.

This module does not enable lifecycle dispatch. A future caller must positively
stop the writer process group and enforce OS denial of scratch Git/index writes
before ingest; no live route calls these helpers today.
"""

from dataclasses import dataclass, replace
import functools
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import uuid


class CandidateError(ValueError):
    pass


# Git config that stops a repo (whose .git/config an author may have written) from choosing a program git runs.
GIT_NO_EXEC = ('-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-c', 'core.pager=cat',
               '-c', 'diff.external=', '-c', 'core.sshCommand=false')
NO_EXT_DIFF = ('--no-ext-diff', '--no-textconv')

EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'

@functools.lru_cache(maxsize=None)
def _attr_source():   # git >= 2.40: read attributes from the empty tree, so no workspace .gitattributes can select a filter driver
    try: version = tuple(int(part) for part in subprocess.run(['git', '--version'], stdout=subprocess.PIPE, text=True).stdout.split()[2].split('.')[:2])
    except (OSError, IndexError, ValueError): return ()
    return ('--attr-source=' + EMPTY_TREE,) if version >= (2, 40) else ()


def git_env():
    return {**os.environ, 'GIT_ATTR_NOSYSTEM': '1', 'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': 'none'}   # no lazy fetch, no transport helper (ext::)

def _filter_overrides(cwd):   # G1: blank every filter driver the effective config defines, whatever attribute source selects it
    proc = run_bounded(['git', *GIT_NO_EXEC, '-c', 'core.attributesFile=/dev/null', 'config', '--includes', '--null', '--get-regexp',
                        r'^filter\..*\.(clean|smudge|process|required)$'], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace', timeout=10)
    names = dict.fromkeys(row.split('\n', 1)[0][len('filter.'):].rsplit('.', 1)[0] for row in proc.stdout.split('\0') if row)
    if not all(re.fullmatch(r'[A-Za-z0-9_.+-]+', name) for name in names):
        raise RuntimeError('unsupported git filter driver name')   # a RuntimeError, so the drive loop HOLDs on it
    return tuple(arg for name in names for arg in ('-c', f'filter.{name}.clean=', '-c', f'filter.{name}.smudge=', '-c', f'filter.{name}.process=', '-c', f'filter.{name}.required=false'))

def run_bounded(command, timeout=60, **kwargs):   # a FIFO or a stuck read in the workspace ends as a RuntimeError (the drive loop HOLDs), never a hang
    try: return subprocess.run(command, env=git_env(), timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired: raise RuntimeError('git call timed out in the workspace')


def git_command(*args, cwd=None):   # a workspace call passes cwd so its filter drivers are neutralised too
    return ['git', *GIT_NO_EXEC, '-c', 'core.attributesFile=/dev/null', *_attr_source(), *(_filter_overrides(cwd) if cwd else ()), *args]


@dataclass(frozen=True)
class CandidateBaseline:
    workspace: Path
    run_dir: Path
    root: Path
    git_dir: Path
    index: Path
    parent_head: str
    parent_ref: str
    tree_oid: str
    live_index_sha256: str
    authorized_prefixes: tuple[str, ...]
    separate_filesystems: bool
    root_identity: tuple[int, int]
    object_format: str
    parent_entries: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class CandidateRevision:
    tree_oid: str
    manifest: tuple[dict, ...]
    files_verified: int


def baseline_from_binding(binding: dict) -> CandidateBaseline:
    values = dict(binding)
    for key in ('workspace', 'run_dir', 'root', 'git_dir', 'index'):
        values[key] = Path(values[key])
    for key in ('root_identity', 'authorized_prefixes'):
        values[key] = tuple(values[key])
    values['parent_entries'] = tuple(tuple(row) for row in values['parent_entries'])
    return CandidateBaseline(**values)


def _git_env(**overrides):
    env = {name: value for name, value in os.environ.items() if not name.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
               GIT_OPTIONAL_LOCKS='0', GIT_LITERAL_PATHSPECS='1', GIT_ATTR_NOSYSTEM='1',
               GIT_NO_REPLACE_OBJECTS='1',
               HOME=os.devnull, XDG_CONFIG_HOME=os.devnull,
               **overrides)
    return env


def _git(args, *, cwd=None, env=None):
    command = ['git', *GIT_NO_EXEC,
               '-c', 'core.protectHFS=true', '-c', 'core.protectNTFS=true',
               '-c', 'core.attributesFile=/dev/null', '-c', 'core.ignorecase=false',
               '-c', 'core.precomposeunicode=false', '-c', 'core.symlinks=true',
               '-c', 'core.filemode=true', '-c', 'core.autocrlf=false',
               '-c', 'gc.auto=0', '-c', 'maintenance.auto=false',
               '-c', 'transfer.fsckObjects=true', *args]
    result = subprocess.run(command, cwd=cwd, env=env or _git_env(),
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise CandidateError('git candidate operation failed: ' + result.stderr.strip())
    return result.stdout.strip()


def _git_bytes(args, *, env, input_bytes=None):
    command = ['git', *GIT_NO_EXEC,
               '-c', 'core.protectHFS=true', '-c', 'core.protectNTFS=true',
               '-c', 'core.attributesFile=/dev/null', '-c', 'core.ignorecase=false',
               '-c', 'core.precomposeunicode=false', '-c', 'core.symlinks=true',
               '-c', 'core.filemode=true', '-c', 'core.autocrlf=false',
               '-c', 'gc.auto=0', '-c', 'maintenance.auto=false',
               '-c', 'transfer.fsckObjects=true', *args]
    result = subprocess.run(command, env=env, input=input_bytes,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise CandidateError('git candidate object read failed: ' + result.stderr.decode(errors='replace'))
    return result.stdout


def _indexed_blobs(env, entries):
    oids = list(dict.fromkeys(oid for _, oid, _ in entries))
    output = _git_bytes(['cat-file', '--batch'], env=env,
                        input_bytes=b''.join(oid.encode() + b'\n' for oid in oids))
    found, offset = {}, 0
    for oid in oids:
        end = output.find(b'\n', offset)
        header = output[offset:end].split()
        if end < 0 or len(header) != 3 or header[0].decode() != oid or header[1] != b'blob':
            raise CandidateError('candidate blob batch is malformed')
        size = int(header[2]); start = end + 1
        found[oid] = output[start:start + size]
        offset = start + size + 1
    if offset != len(output): raise CandidateError('candidate blob batch has trailing data')
    return found


def _object_oid(data: bytes, object_format: str) -> str:
    if object_format not in ('sha1', 'sha256'):
        raise CandidateError('unsupported Git object format')
    payload = b'blob ' + str(len(data)).encode() + b'\0' + data
    return hashlib.new(object_format, payload).hexdigest()


def _exact_paths(root: Path, entries) -> None:
    actual = {os.fsencode(path.relative_to(root).as_posix()) for path in root.rglob('*')}
    expected = set()
    for _, _, path in entries:
        parts = path.split('/')
        expected.update(os.fsencode('/'.join(parts[:index])) for index in range(1, len(parts) + 1))
    if actual != expected:
        raise CandidateError('candidate path-byte set differs from isolated index')


def _check_attributes(env, entries):
    names = b''.join(os.fsencode(path) + b'\0' for _, _, path in entries)
    attrs = ('text', 'crlf', 'eol', 'ident', 'filter', 'working-tree-encoding')
    output = _git_bytes(['check-attr', '--cached', '--stdin', '-z', *attrs], env=env,
                        input_bytes=names).split(b'\0')
    if output[-1:] == [b'']: output.pop()
    if len(output) != len(entries) * len(attrs) * 3:
        raise CandidateError('candidate attribute batch is incomplete')
    for offset in range(0, len(output), 3):
        if output[offset + 1].decode() not in attrs or output[offset + 2] not in (b'unspecified', b'unset'):
            raise CandidateError('candidate has transforming Git attributes')


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
    canonical = {}
    for value in values:
        if (not isinstance(value, str) or not value or value.startswith(':') or '\\' in value or
                any(ord(char) < 32 or char in '*?[]{}' for char in value)):
            raise CandidateError('authorized prefix is not a safe path')
        path = PurePosixPath(value)
        if (path.is_absolute() or path.as_posix() != value or
                any(part in ('', '.', '..') or part.rstrip(' .').casefold() == '.git'
                    for part in value.split('/'))):
            raise CandidateError('authorized prefix is not a normalized relative path')
        key = _canonical_parts(value)
        if key in canonical and canonical[key] != value:
            raise CandidateError('authorized prefixes have Unicode/case alias')
        canonical[key] = value; normalized.add(value)
    return tuple(sorted(normalized))


def _canonical_parts(path: str) -> tuple[str, ...]:
    return tuple(unicodedata.normalize('NFC', unicodedata.normalize('NFC', part).casefold())
                 for part in path.split('/'))


def _index_entries(env):
    entries = []
    seen = {}
    leaves = set()
    for record in _git_bytes(['ls-files', '--stage', '-z'], env=env).split(b'\0'):
        if not record: continue
        metadata, path_bytes = record.split(b'\t', 1)
        mode, oid, stage = metadata.decode().split()
        path = os.fsdecode(path_bytes)
        parts = path.split('/')
        folded = [unicodedata.normalize('NFC', unicodedata.normalize('NFC', part).casefold())
                  for part in parts]
        parents = [(tuple(folded[:index]), tuple(parts[:index])) for index in range(1, len(folded) + 1)]
        if (stage != '0' or path in leaves or
                any(key in seen and seen[key] != original for key, original in parents) or
                any(part.rstrip(' .').casefold() == '.git' for part in parts)):
            raise CandidateError('candidate index has unmerged or ambiguous Git path')
        seen.update(parents); leaves.add(path)
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
    object_format = _git(['rev-parse', '--show-object-format=storage'], cwd=workspace, env=env)
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
        _git(['init', '--bare', '--template=', '--object-format=' + object_format, '-q', str(git_dir)], env=env)
        _git(['--git-dir', str(git_dir), 'fetch', '--no-tags', '--quiet', str(workspace), head], env=env)
        _git(['--git-dir', str(git_dir), 'update-ref', 'refs/paired-session/parent', head], env=env)
        scratch_env = _git_env(GIT_DIR=str(git_dir), GIT_INDEX_FILE=str(index),
                               GIT_WORK_TREE=str(root), GIT_CEILING_DIRECTORIES=str(candidate_base.parent))
        _git(['read-tree', head], env=scratch_env)
        entries = _index_entries(scratch_env)
        _check_attributes(scratch_env, entries)
        _git(['checkout-index', '--all', '--prefix=' + str(root) + os.sep], env=scratch_env)
        if _git(['write-tree'], env=scratch_env) != tree:
            raise CandidateError('isolated index differs from frozen parent tree')
        _validate_checkout(root)
        _exact_paths(root, entries)
        blobs = _indexed_blobs(scratch_env, entries)
        for mode, oid, path in entries:
            target = root / path
            actual = os.fsencode(os.readlink(target)) if mode == '120000' else target.read_bytes()
            if actual != blobs[oid] or _object_oid(actual, object_format) != oid:
                raise CandidateError('candidate checkout bytes differ from indexed blob: ' + path)
            if mode in ('100644', '100755') and bool(target.stat().st_mode & 0o111) != (mode == '100755'):
                raise CandidateError('candidate checkout mode differs from indexed blob: ' + path)
        if (_git(['rev-parse', '--verify', 'HEAD'], cwd=workspace, env=env) != head or
                hashlib.sha256(live_index.read_bytes()).hexdigest() != index_hash or
                _git(['status', '--porcelain=v1', '--untracked-files=all'], cwd=workspace, env=env)):
            raise CandidateError('live HEAD/index/worktree changed during materialization')
        separate = root.stat().st_dev not in {git_dir.stat().st_dev, index.stat().st_dev,
            workspace.stat().st_dev, (run_dir if run_dir.exists() else run_dir.parent).stat().st_dev}
        identity = (root.lstat().st_dev, root.lstat().st_ino)
        return CandidateBaseline(workspace, run_dir, root, git_dir, index, head, ref,
                                 tree, index_hash, tuple(authorized_prefixes), separate, identity,
                                 object_format, tuple(entries))
    except BaseException:
        for path in (candidate_base, metadata_base):
            if path is not None:
                shutil.rmtree(path, ignore_errors=True)
        raise


def _candidate_files(baseline: CandidateBaseline) -> dict[str, tuple[str, bytes]]:
    root = baseline.root
    mode = root.lstat().st_mode
    if not stat.S_ISDIR(mode) or (root.lstat().st_dev, root.lstat().st_ino) != baseline.root_identity:
        raise CandidateError('candidate root identity changed')
    _validate_checkout(root)
    found = {}
    for path in root.rglob('*'):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode): continue
        if _outside(path.parent.resolve(), root): raise CandidateError('candidate path escaped root')
        relative = path.relative_to(root).as_posix()
        if stat.S_ISLNK(info.st_mode):
            found[relative] = ('120000', os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if not hasattr(os, 'O_NOFOLLOW'): raise CandidateError('no nofollow file reads on this platform')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise CandidateError('candidate file changed during open')
                data = handle.read()
                after = os.fstat(handle.fileno())
                if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise CandidateError('candidate file changed during read')
            found[relative] = ('100755' if info.st_mode & 0o111 else '100644', data)
        else:
            raise CandidateError('candidate has unsupported writer file')
    return found


def _assert_live_unchanged(baseline: CandidateBaseline, scratch_env) -> None:
    source_env = _git_env()
    if (_git(['rev-parse', '--verify', 'HEAD'], cwd=baseline.workspace, env=source_env) != baseline.parent_head or
            _git(['symbolic-ref', '-q', 'HEAD'], cwd=baseline.workspace, env=source_env) != baseline.parent_ref or
            _git(['rev-parse', 'refs/paired-session/parent'], env=scratch_env) != baseline.parent_head or
            _git(['status', '--porcelain=v1', '--untracked-files=all'], cwd=baseline.workspace, env=source_env)):
        raise CandidateError('live or scratch parent changed before candidate use')
    index_text = _git(['rev-parse', '--git-path', 'index'], cwd=baseline.workspace, env=source_env)
    live_index = Path(index_text)
    if not live_index.is_absolute(): live_index = baseline.workspace / live_index
    if hashlib.sha256(live_index.read_bytes()).hexdigest() != baseline.live_index_sha256:
        raise CandidateError('live index bytes changed before candidate use')


def _manifest(env, parent_tree: str, tree_oid: str) -> tuple[dict, ...]:
    raw = _git_bytes(['diff-tree', '--no-commit-id', '--name-status', '-r', '--no-renames', '-z',
                      parent_tree, tree_oid], env=env).split(b'\0')
    if raw[-1:] == [b'']: raw.pop()
    if len(raw) % 2: raise CandidateError('candidate manifest output is malformed')
    return tuple({'status': raw[i].decode().lstrip('\n'), 'path': os.fsdecode(raw[i + 1])}
                 for i in range(0, len(raw), 2))


def _manifest_from_entries(parent, current) -> tuple[dict, ...]:
    before = {path: (mode, oid) for mode, oid, path in parent}
    after = {path: (mode, oid) for mode, oid, path in current}
    result = []
    for path in sorted(set(before) | set(after), key=os.fsencode):
        if before.get(path) == after.get(path): continue
        status = ('A' if path not in before else 'D' if path not in after else
                  'T' if (before[path][0] == '120000') != (after[path][0] == '120000') else 'M')
        result.append({'status': status, 'path': path})
    return tuple(result)


def _tree_entries(env, tree_oid: str):
    result = []
    for record in _git_bytes(['ls-tree', '-r', '-z', '--full-tree', tree_oid], env=env).split(b'\0'):
        if not record: continue
        metadata, path = record.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        if kind != 'blob': raise CandidateError('candidate tree contains non-blob entry')
        result.append((mode, oid, os.fsdecode(path)))
    return tuple(sorted(result, key=lambda row: row[2]))


def _fresh_index(baseline: CandidateBaseline, env, files, index: Path, write_blobs: bool):
    stage_env = {**env, 'GIT_INDEX_FILE': str(index)}
    _git(['read-tree', '--empty'], env=stage_env)
    for path, (mode, data) in sorted(files.items()):
        oid = _object_oid(data, baseline.object_format)
        if write_blobs:
            written = _git_bytes(['hash-object', '-w', '--no-filters', '--stdin'],
                                 env=stage_env, input_bytes=data).decode().strip()
            if written != oid: raise CandidateError('candidate raw blob ID differs from writer bytes')
        _git(['update-index', '--add', '--cacheinfo', mode, oid, path], env=stage_env)
    return stage_env


def _assert_index_tree_consistent(env, entries, index: Path) -> None:
    fresh_index = index.with_name('index-check-' + uuid.uuid4().hex)
    try:
        fresh_env = {**env, 'GIT_INDEX_FILE': str(fresh_index)}
        _git(['read-tree', '--empty'], env=fresh_env)
        for mode, oid, path in entries:
            _git(['update-index', '--add', '--cacheinfo', mode, oid, path], env=fresh_env)
        if _git(['write-tree'], env=fresh_env) != _git(['write-tree'], env=env):
            raise CandidateError('scratch index cache-tree differs from visible entries')
    finally:
        fresh_index.unlink(missing_ok=True)


def _require_authorized(paths, prefixes) -> None:
    for path in paths:
        aliases = [prefix for prefix in prefixes
                   if _canonical_parts(path)[:len(_canonical_parts(prefix))] == _canonical_parts(prefix)]
        if aliases and not any(path == prefix or path.startswith(prefix + '/') for prefix in aliases):
            raise CandidateError('candidate writer used authorized-prefix alias: ' + path)
        if not aliases:
            raise CandidateError('candidate writer changed unauthorized path: ' + path)


def ingest_candidate_revision(baseline: CandidateBaseline) -> CandidateRevision:
    """Stage only authorized candidate bytes into the scratch index; leave live Git untouched."""
    env = _git_env(GIT_DIR=str(baseline.git_dir), GIT_INDEX_FILE=str(baseline.index),
                   GIT_WORK_TREE=str(baseline.root), GIT_CEILING_DIRECTORIES=str(baseline.root.parent))
    _assert_live_unchanged(baseline, env)
    prior = _index_entries(env)
    _assert_index_tree_consistent(env, prior, baseline.index)
    _require_authorized((row['path'] for row in _manifest_from_entries(baseline.parent_entries, prior)),
                        baseline.authorized_prefixes)
    present = _candidate_files(baseline)
    computed = tuple((mode, _object_oid(data, baseline.object_format), path)
                     for path, (mode, data) in sorted(present.items()))
    manifest = _manifest_from_entries(baseline.parent_entries, computed)
    _require_authorized((row['path'] for row in manifest), baseline.authorized_prefixes)
    _exact_paths(baseline.root, computed)
    staging_index = baseline.index.with_name('stage-' + uuid.uuid4().hex + '.index')
    try:
        stage_env = _fresh_index(baseline, env, present, staging_index, write_blobs=True)
        entries = _index_entries(stage_env)
        if tuple(entries) != computed: raise CandidateError('fresh candidate index differs from writer bytes')
        _check_attributes(stage_env, entries)
        _exact_paths(baseline.root, entries)
        current = _candidate_files(baseline)
        new_blobs = _indexed_blobs(stage_env, entries)
        for mode, oid, path in entries:
            if (current.get(path) != (mode, new_blobs[oid]) or
                    _object_oid(current[path][1], baseline.object_format) != oid):
                raise CandidateError('candidate bytes changed before new tree OID: ' + path)
        tree_oid = _git(['write-tree'], env=stage_env)
        if _tree_entries(stage_env, tree_oid) != computed or _manifest(stage_env, baseline.tree_oid, tree_oid) != manifest:
            raise CandidateError('candidate tree or manifest differs from verified entries')
        _git(['update-ref', 'refs/paired-session/candidates/' + tree_oid, tree_oid], env=stage_env)
        os.replace(staging_index, baseline.index)
        return CandidateRevision(tree_oid, manifest, len(entries))
    finally:
        staging_index.unlink(missing_ok=True)


def verify_candidate_revision(baseline: CandidateBaseline, revision: CandidateRevision) -> None:
    """Reject any post-ingest candidate or isolated-index drift before review."""
    env = _git_env(GIT_DIR=str(baseline.git_dir), GIT_INDEX_FILE=str(baseline.index),
                   GIT_WORK_TREE=str(baseline.root), GIT_CEILING_DIRECTORIES=str(baseline.root.parent))
    _assert_live_unchanged(baseline, env)
    files = _candidate_files(baseline)
    computed = tuple((mode, _object_oid(data, baseline.object_format), path)
                     for path, (mode, data) in sorted(files.items()))
    _exact_paths(baseline.root, computed)
    if _manifest_from_entries(baseline.parent_entries, computed) != revision.manifest:
        raise CandidateError('candidate manifest differs from reviewed OID')
    _require_authorized((row['path'] for row in revision.manifest), baseline.authorized_prefixes)
    visible = _index_entries(env)
    _assert_index_tree_consistent(env, visible, baseline.index)
    if tuple(visible) != computed:
        raise CandidateError('isolated index differs from reviewed candidate OID')
    blobs = _indexed_blobs(env, computed)
    if any(blobs[oid] != files[path][1] for mode, oid, path in computed):
        raise CandidateError('scratch blob differs from reviewed candidate bytes')
    if _git(['rev-parse', 'refs/paired-session/candidates/' + revision.tree_oid], env=env) != revision.tree_oid:
        raise CandidateError('reviewed candidate tree lost its scratch ref')
    staging_index = baseline.index.with_name('verify-' + uuid.uuid4().hex + '.index')
    try:
        fresh_env = _fresh_index(baseline, env, files, staging_index, write_blobs=False)
        _check_attributes(fresh_env, computed)
        if (_git(['write-tree'], env=fresh_env) != revision.tree_oid or
                _tree_entries(fresh_env, revision.tree_oid) != computed or
                _manifest(fresh_env, baseline.tree_oid, revision.tree_oid) != revision.manifest):
            raise CandidateError('candidate bytes differ from reviewed OID')
    finally:
        staging_index.unlink(missing_ok=True)


def rebuild_candidate_from_oid(baseline: CandidateBaseline,
                               revision: CandidateRevision) -> CandidateBaseline:
    """Rebuild a verified candidate into a fresh root without importing live bytes."""
    old_env = _git_env(GIT_DIR=str(baseline.git_dir), GIT_INDEX_FILE=str(baseline.index),
                       GIT_WORK_TREE=str(baseline.root))
    _assert_live_unchanged(baseline, old_env)
    if (_git(['rev-parse', 'refs/paired-session/candidates/' + revision.tree_oid], env=old_env)
            != revision.tree_oid or
            _git(['rev-parse', baseline.parent_head + '^{tree}'], env=old_env) != baseline.tree_oid or
            _tree_entries(old_env, baseline.tree_oid) != baseline.parent_entries or
            _manifest(old_env, baseline.tree_oid, revision.tree_oid) != revision.manifest):
        raise CandidateError('candidate rebuild OID differs from verified scratch history')
    _check_attributes(old_env, _tree_entries(old_env, revision.tree_oid))
    _require_authorized((row['path'] for row in revision.manifest), baseline.authorized_prefixes)
    parent = baseline.root.parent.parent.resolve()
    common = Path(_git(['rev-parse', '--git-common-dir'], cwd=baseline.workspace, env=_git_env()))
    if not common.is_absolute(): common = baseline.workspace / common
    linked = [Path(line[9:]) for line in _git(['worktree', 'list', '--porcelain'],
                                              cwd=baseline.workspace, env=_git_env()).splitlines()
              if line.startswith('worktree ')]
    protected = (baseline.workspace, baseline.run_dir, baseline.git_dir,
                 baseline.index.parent, common.resolve(), *linked)
    if any(not _outside(parent, path) for path in protected):
        raise CandidateError('candidate rebuild parent is protected')
    base = Path(tempfile.mkdtemp(prefix='paired-session-tree-', dir=parent))
    root = base / 'tree'
    index = baseline.index.with_name('rebuild-' + uuid.uuid4().hex + '.index')
    try:
        root.mkdir()
        env = _git_env(GIT_DIR=str(baseline.git_dir), GIT_INDEX_FILE=str(index),
                       GIT_WORK_TREE=str(root), GIT_CEILING_DIRECTORIES=str(base))
        _git(['read-tree', revision.tree_oid], env=env)
        _git(['checkout-index', '--all', '--prefix=' + str(root) + os.sep], env=env)
        separate = root.stat().st_dev not in {
            baseline.git_dir.stat().st_dev, index.stat().st_dev,
            baseline.workspace.stat().st_dev,
            (baseline.run_dir if baseline.run_dir.exists() else baseline.run_dir.parent).stat().st_dev}
        rebuilt = replace(baseline, root=root, index=index, separate_filesystems=separate,
                          root_identity=(root.lstat().st_dev, root.lstat().st_ino))
        verify_candidate_revision(rebuilt, revision)
        return rebuilt
    except BaseException:
        index.unlink(missing_ok=True)
        shutil.rmtree(base, ignore_errors=True)
        raise
