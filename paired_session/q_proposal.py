import hashlib
from importlib import import_module
from pathlib import Path
import tempfile
prefix = 'paired_session.' if __package__ else ''
ct = import_module(prefix + 'candidate_tree')
ca = import_module(prefix + 'closeout_adapter')
def materialize(baseline, revision, frozen, c1, day):
    ct.verify_candidate_revision(baseline, revision)
    if frozen['head'] != baseline.parent_head:
        raise ValueError('Q source parent differs from frozen closeout')
    if frozen['close_adapter_sha256'] != hashlib.sha256(Path(ca.__file__).read_bytes()).hexdigest():
        raise ValueError('Q close adapter changed; start a new run')
    if not isinstance(c1, str) or len(c1) not in (40, 64) or set(c1) - set('0123456789abcdef'):
        raise ValueError('Q requires an exact C1 object ID')
    env = ct._git_env(GIT_DIR=str(baseline.git_dir))
    commit = ct._git_bytes(['cat-file', 'commit', c1], env=env)
    header = commit.decode().split('\n\n', 1)[0].splitlines()
    if ('tree ' + revision.tree_oid not in header or
            [row for row in header if row.startswith('parent ')] != ['parent ' + frozen['head']]):
        raise ValueError('Q C1 tree or parent differs from frozen P')
    entries = ct._tree_entries(env, revision.tree_oid)
    if not any(mode == '100644' and path == 'BACKLOG.md' for mode, oid, path in entries):
        raise ValueError('Q requires regular non-executable BACKLOG blob')
    raw = ct._git_bytes(['cat-file', 'blob', revision.tree_oid + ':BACKLOG.md'], env=env)
    body = raw.decode().partition('## P0\n')[2]
    if not raw.endswith(b'\n') or any(line.strip() not in ('', '(none)') and
            not line.startswith(('## ', '- ', ' ', '\t')) for line in body.splitlines()):
        raise ValueError('Q requires final newline and indented child text; repair the source and re-review P')
    q_bytes = ca.close_blob(raw, frozen, c1, day)
    with tempfile.TemporaryDirectory(dir=baseline.index.parent) as scratch:
        env = {**env, 'GIT_INDEX_FILE': str(Path(scratch) / 'index')}
        ct._git(['read-tree', revision.tree_oid], env=env)
        blob = ct._git_bytes(['hash-object', '-w', '--stdin'], env=env, input_bytes=q_bytes).decode().strip()
        ct._git(['update-index', '--add', '--cacheinfo', '100644', blob, 'BACKLOG.md'], env=env)
        q_oid = ct._git(['write-tree'], env=env)
        changed = ct._git(['diff-tree', '--no-commit-id', '--name-only', '-r', revision.tree_oid, q_oid], env=env)
        if changed != 'BACKLOG.md':
            raise ValueError('Q must differ from P only at BACKLOG.md')
    return {'status': 'UNREVIEWED', 'p_oid': revision.tree_oid, 'q_oid': q_oid, 'c1': c1,
            'parent': frozen['head'], 'backlog_blob': blob, 'item_id': frozen['item_id'],
            'close_adapter_sha256': frozen['close_adapter_sha256'],
            'c1_sha256': hashlib.sha256(commit).hexdigest()}
