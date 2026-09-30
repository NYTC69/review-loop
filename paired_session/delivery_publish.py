import os, json, tempfile
from importlib import import_module
dj = import_module(('paired_session.' if __package__ else '') + 'delivery_journal')
protocol = import_module(('paired_session.' if __package__ else '') + 'coordinator')
ct = dj.ct
def publish(co, observed_test, atomic_json):
    if (not co._fake_lifecycle or not protocol.lifecycle_spine.fake_dispatch_guard(co.args) or
            co.state['status'] != 'ACCEPTED' or co.state['acceptance_state'] != 'ACCEPTED'):
        raise ValueError('publication requires fake operator ACCEPTED state')
    with protocol.run_lease(co.run_dir), protocol.workspace_lease(co.workspace, co.run_dir):
        if json.loads(co.state_path.read_text()) != json.loads(json.dumps(co.state)):
            raise ValueError('saved state changed; reload before publication')
        try:
            row = dj.prepare(co, observed_test, atomic_json)
            root, revision, live = dj.verify(co, row)
            intent = row['intent']
            co.state['publication_hold'] = intent['digest']
            co.save()
            if ct._git(['rev-parse', 'HEAD'], env=live) != intent['parent']:
                raise ValueError('post-CAS replay requires reconciliation; operator recovery required')
            index = ct.Path(ct._git(['rev-parse', '--git-path', 'index'], env=live))
            if not index.is_absolute() or index.is_symlink() or index.stat().st_nlink != 1:
                raise ValueError('unsafe live index')
            lock = index.with_name(index.name + '.lock')
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            identity = os.fstat(fd)
            try:
                row['lock'] = {'pid': os.getpid(), 'dev': identity.st_dev, 'ino': identity.st_ino, 'path': str(lock)}
                atomic_json(co.evidence / 'delivery-publication.json', row)
                with tempfile.TemporaryDirectory(dir=index.parent, prefix='delivery-') as scratch:
                    alternate = ct.Path(scratch) / 'index'
                    alternate.write_bytes(index.read_bytes())
                    env = {**live, 'GIT_INDEX_FILE': str(alternate), 'GIT_WORK_TREE': str(co.workspace)}
                    ct.verify_candidate_revision(root, revision)
                    dj.verify(co, row)
                    pack = ct._git_bytes(['pack-objects', '--stdout', '--revs'],
                            env=dj.intent_api.commit_environment(co, root), input_bytes=(intent['c2'] + '\n').encode())
                    ct._git_bytes(['index-pack', '--strict', '--stdin'], env=live, input_bytes=pack)
                    ct.verify_candidate_revision(root, revision)
                    dj.verify(co, row)
                    ct._git(['update-ref', intent['ref'], intent['c2'], intent['parent']], env=live)
                    row['phase'] = 'PUBLISHED'
                    atomic_json(co.evidence / 'delivery-publication.json', row)
                    ct._git(['read-tree', '-m', '-u', intent['parent'], intent['c2']], env=env)
                    os.replace(alternate, index)
                    env['GIT_INDEX_FILE'] = str(index)
                    if (tuple(ct._index_entries(env)) != ct._tree_entries(env, intent['q_oid']) or
                            ct._git(['status', '--porcelain=v1', '--untracked-files=all'], env=env)):
                        raise ValueError('live tree/index reconciliation mismatch')
                    dj.verify(co, row)
                    row['phase'] = 'RECONCILED'
                    atomic_json(co.evidence / 'delivery-publication.json', row)
                    return row
            finally:
                os.close(fd)
                if lock.exists() and (lock.lstat().st_dev, lock.lstat().st_ino) == (identity.st_dev, identity.st_ino):
                    lock.unlink()
        except Exception as error:
            if json.loads(co.state_path.read_text()) != json.loads(json.dumps(co.state)):
                raise ValueError('state changed; refusing stale HOLD overwrite') from error
            co.state.update(status='HOLD', hold_reason='publication: ' + str(error),
                            publication_hold=co.state.get('fake_delivery_intent', {}).get('digest'))
            co.save()
            return 'HOLD'
