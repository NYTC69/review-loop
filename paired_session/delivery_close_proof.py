"""Read-only admission for CLOSE at an exactly reconciled, accepted Q tree."""
import hashlib
import json
from importlib import import_module

prefix = 'paired_session.' if __package__ else ''
recovery = import_module(prefix + 'delivery_recovery_state')
protocol = import_module(prefix + 'coordinator')
ct = recovery.ct
adapter = import_module(prefix + 'closeout_adapter')


def verify(co):
    if not co._fake_lifecycle or not protocol.lifecycle_spine.fake_dispatch_guard(co.args):
        raise ValueError('CLOSE is fake-only; real activation is disabled')
    row = json.loads((co.evidence / 'delivery-publication.json').read_text())
    intent = row['intent']
    if (row['phase'] != 'RECONCILED' or co.state.get('publication_complete') != intent['digest'] or
            co.state.get('publication_hold') or co.state['status'] != 'ACCEPTED' or
            co.state['acceptance_state'] != 'ACCEPTED' or co.state['lifecycle']['pending'] or
            co.state['lifecycle']['stage'] != 'STOP_BEFORE_DELIVERY' or co.blocking_open_findings()):
        raise ValueError('CLOSE requires reconciled acceptance; use locked recovery or abort')
    checked, root, revision, live, index, lock, maps = recovery.inspect(co, exact_q=True)
    if lock.exists() or lock.is_symlink():
        raise ValueError('CLOSE index lock remains; use locked recovery')
    if (ct._git(['rev-parse', 'HEAD'], env=live) != intent['c2'] or
            tuple(ct._index_entries(live)) != ct._tree_entries(live, intent['q_oid'])):
        raise ValueError('CLOSE HEAD/index differs from accepted Q; inspect and recover')
    objects = recovery.dj.intent_api.commit_environment(co, root)
    old = ct._git_bytes(['cat-file', 'blob', intent['p_oid'] + ':BACKLOG.md'], env=objects)
    expected = adapter.close_blob(old, co.state['closeout_item'], intent['c1'], intent['day'])
    actual = ct._git_bytes(['cat-file', 'blob', intent['q_oid'] + ':BACKLOG.md'], env=objects)
    if actual != expected or (co.workspace / 'BACKLOG.md').read_bytes() != expected:
        raise ValueError('CLOSE Compass mutation differs from reviewed Q; inspect and recover')
    return {'intent_digest': intent['digest'], 'item_uuid': co.state['item_uuid'],
            'parent': intent['parent'], 'ref': intent['ref'], 'p_oid': intent['p_oid'],
            'q_oid': intent['q_oid'], 'c1': intent['c1'], 'c2': intent['c2'],
            'acceptance': row['acceptance'], 'backlog_sha256': hashlib.sha256(expected).hexdigest(),
            'proof_sha256': hashlib.sha256(json.dumps(row['proof_state'], sort_keys=True).encode()).hexdigest()}
