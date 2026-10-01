import json, os, stat
from importlib import import_module
dj = import_module(('paired_session.' if __package__ else '') + 'delivery_journal')
ct = dj.ct
def inspect(co, exact_q=False):
    row = json.loads((co.evidence / 'delivery-publication.json').read_text())
    if (co.state.get('publication_hold') not in (None, row['intent']['digest']) or
            co.state['status'] not in ('HOLD', 'ACCEPTED') or co.state['acceptance_state'] != 'ACCEPTED' or
            (co.state['status'] == 'HOLD' and co.state.get('publication_hold') != row['intent']['digest'])):
        raise ValueError('publication recovery lacks matching attributed acceptance')
    status = co.state['status']
    try:
        co.state['status'] = 'ACCEPTED'
        root, revision, live = dj.verify(co, row)
    finally:
        co.state['status'] = status
    live = {**live, 'GIT_WORK_TREE': str(co.workspace)}
    index = ct.Path(ct._git(['rev-parse', '--git-path', 'index'], env=live))
    if not index.is_absolute() or index.is_symlink() or index.stat().st_nlink != 1:
        raise ValueError('unsafe recovery index; inspect before recovery')
    if ((index.parent / 'info/attributes').exists() or
            any(k.lower().startswith('filter.') for k in
                ct._git(['config', '--name-only', '--list'], env=live).splitlines())):
        raise ValueError('live attributes/filter unsupported; inspect before recovery')
    objects = dj.intent_api.commit_environment(co, root)
    maps = [{p: (m, oid) for m, oid, p in ct._tree_entries(objects, oid)}
            for oid in (row['intent']['parent'], row['intent']['q_oid'])]
    entries = {p: (m, oid) for m, oid, p in ct._index_entries(live)}
    if any(entries.get(p) not in (maps[0].get(p), maps[1].get(p))
           for p in entries.keys() | maps[0].keys() | maps[1].keys()):
        raise ValueError('foreign recovery index entry; inspect before recovery')
    for p in maps[0].keys() | maps[1].keys():
        path = co.workspace / p
        if not path.exists() and not path.is_symlink():
            value = None
        else:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                    co.workspace not in path.resolve().parents):
                raise ValueError('unsafe recovery path: ' + p)
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as f:
                before = os.fstat(f.fileno())
                raw = f.read()
                after = os.fstat(f.fileno())
            if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
                    info.st_dev, info.st_ino, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('recovery bytes changed while reading: ' + p)
            value = ('100755' if info.st_mode & 0o111 else '100644', ct._object_oid(raw, root.object_format))
        if value not in (maps[0].get(p), maps[1].get(p)) or (exact_q and value != maps[1].get(p)):
            raise ValueError('foreign recovery bytes: ' + p)
    others = ct._git(['ls-files', '--others', '--exclude-standard', '-z'], env=live).split(chr(0))
    if any(p and p not in maps[0] and p not in maps[1] for p in others):
        raise ValueError('foreign untracked recovery paths; inspect before recovery')
    lock = index.with_name(index.name + '.lock')
    return row, root, revision, live, index, lock, maps
