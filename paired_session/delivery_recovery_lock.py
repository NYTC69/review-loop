import json, os, stat
from contextlib import contextmanager
from importlib import import_module
drs = import_module(('paired_session.' if __package__ else '') + 'delivery_recovery_state')
p = import_module(('paired_session.' if __package__ else '') + 'coordinator')
@contextmanager
def locked(co, atomic_json):
    if not co._fake_lifecycle or not p.lifecycle_spine.fake_dispatch_guard(co.args):
        raise ValueError('publication recovery is fake-only')
    with p.run_lease(co.run_dir), p.workspace_lease(co.workspace, co.run_dir):
        if json.loads(co.state_path.read_text()) != json.loads(json.dumps(co.state)):
            raise ValueError('saved state changed; reload before recovery')
        recorded = False
        try:
            row, root, revision, live, index, lock, maps = drs.inspect(co)
            attrs = drs.ct.Path(drs.ct._git(['rev-parse', '--git-path', 'info/attributes'], env=live))
            if attrs.exists() or attrs.is_symlink():
                raise ValueError('common-dir attributes unsupported; inspect before recovery')
            for name in maps[0].keys() | maps[1].keys():
                for parent in (co.workspace / name).parents:
                    if parent == co.workspace:
                        break
                    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                        raise ValueError('foreign recovery prefix: ' + str(parent))
            if lock.exists() or lock.is_symlink():
                owner, info = row.get('lock', {}), lock.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or str(lock) != owner.get('path') or
                        (info.st_dev, info.st_ino) != (owner.get('dev'), owner.get('ino')) or
                        lock.read_text() != owner.get('nonce')):
                    raise ValueError('unattributed index lock; inspect before recovery')
                lock.unlink()
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            try:
                nonce = os.urandom(32).hex()
                row['lock'] = {'pid': os.getpid(), 'dev': info.st_dev, 'ino': info.st_ino,
                               'path': str(lock), 'nonce': nonce}
                os.write(fd, nonce.encode())
                os.fsync(fd)
                atomic_json(co.evidence / 'delivery-publication.json', row)
                recorded = True
                drs.inspect(co)
                yield row, root, revision, live, index, lock, maps
            finally:
                os.close(fd)
                disk = json.loads(co.state_path.read_text())
                journal = json.loads((co.evidence / 'delivery-publication.json').read_text())
                if ((not recorded or (journal['phase'] == 'RECONCILED' and
                        disk.get('publication_complete') == row['intent']['digest'])) and lock.exists() and
                        (lock.lstat().st_dev, lock.lstat().st_ino) == (info.st_dev, info.st_ino)):
                    lock.unlink()
        except Exception as error:
            if not recorded:
                raise
            if json.loads(co.state_path.read_text()) != json.loads(json.dumps(co.state)):
                raise ValueError('state changed; refusing stale recovery HOLD') from error
            co.state.update(status='HOLD', hold_reason='publication recovery: ' + str(error),
                            publication_hold=row['intent']['digest'])
            co.save()
            raise
