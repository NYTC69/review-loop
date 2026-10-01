"""Frozen hook/config inventory and deterministic unpublished C1 policy."""
import hashlib
import json
from importlib import import_module
prefix = 'paired_session.' if __package__ else ''
ct = import_module(prefix + 'candidate_tree')
hooks = import_module(prefix + 'hook_inventory_scan')
def freeze(co, atomic_json):
    if not co._fake_lifecycle:
        raise ValueError('publication sealing is fake-only')
    inventory = json.loads(json.dumps(hooks.inventory(co.workspace)))
    if any(mode & 0o111 and not name.endswith('.sample') for name, mode, digest in inventory['entries']):
        raise ValueError('hook runner not implemented; refuse active hooks')
    seal = {'inventory': inventory, 'message': 'paired-session item ' + co.state['item_uuid'] + '\n',
            'name': 'paired-session', 'email': 'paired-session@localhost',
            'date': '@' + str(int(co.state['started_at'])) + ' +0000'}
    if co.state.get('publication_seal') not in (None, seal):
        raise ValueError('publication seal changed')
    atomic_json(co.evidence / 'delivery-seal.json', seal)
    co.state['publication_seal'] = seal
    co.save()
def environment(co, root):
    seal = co.state['publication_seal']
    return ct._git_env(GIT_DIR=str(root.git_dir), PATH=co.state['operator_programs']['path_env'],
                      GIT_AUTHOR_NAME=seal['name'], GIT_AUTHOR_EMAIL=seal['email'],
                      GIT_COMMITTER_NAME=seal['name'], GIT_COMMITTER_EMAIL=seal['email'],
                      GIT_AUTHOR_DATE=seal['date'], GIT_COMMITTER_DATE=seal['date'])
def c1(co, root, revision):
    if not co._fake_lifecycle or co.state['lifecycle']['stage'] != 'STOP_BEFORE_DELIVERY':
        raise ValueError('C1 requires fake SECURITY completion')
    ct.verify_candidate_revision(root, revision)
    return ct._git(['commit-tree', '--no-gpg-sign', revision.tree_oid, '-p', root.parent_head,
                    '-m', co.state['publication_seal']['message']], env=environment(co, root))

def verify(co, root, proposal):
    seal = co.state['publication_seal']
    if json.loads((co.evidence / 'delivery-seal.json').read_text()) != seal:
        raise ValueError('publication seal differs from protected evidence')
    if json.loads(json.dumps(hooks.inventory(co.workspace))) != seal['inventory']:
        raise ValueError('frozen hook inventory changed')
    expected = ct._git(['commit-tree', '--no-gpg-sign', proposal['p_oid'], '-p', proposal['parent'],
                        '-m', seal['message']], env=environment(co, root))
    raw = ct._git_bytes(['cat-file', 'commit', proposal['c1']], env=environment(co, root))
    if expected != proposal['c1'] or hashlib.sha256(raw).hexdigest() != proposal['c1_sha256']:
        raise ValueError('C1 metadata/tree/parent differs from frozen policy')
    return json.loads(json.dumps(seal))
