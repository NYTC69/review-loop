import json
import os
import tempfile

from importlib import import_module
safe_temp = import_module(('paired_session.' if __package__ else '') + 'safe_temp')
dp = import_module(('paired_session.' if __package__ else '') + 'delivery_publish')
drl = import_module(('paired_session.' if __package__ else '') + 'delivery_recovery_lock')
ct = dp.ct


def reconcile(co, observed_test, atomic_json):
    intent = co.state.get('fake_delivery_intent')
    if not isinstance(intent, dict) or not intent.get('parent'):
        raise ValueError('publication intent missing; inspect before recovery')
    if (co.state.get('pending_reviewer_result_sequence') is None and
            'pending_reviewer_result_sequence' not in json.loads(co.state_path.read_text())):
        co.state.pop('pending_reviewer_result_sequence', None)
    if co._head_commit() == co.state['fake_delivery_intent']['parent']:
        published = dp.publish(co, observed_test, atomic_json)
        if published == 'HOLD':
            return published
    with drl.locked(co, atomic_json) as (row, root, revision, live, index, lock, maps):
        if row['phase'] != 'RECONCILED':
            for name in maps[0].keys() | maps[1].keys():
                for parent in (co.workspace / name).parents:
                    if parent == co.workspace:
                        break
                    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                        raise ValueError('foreign recovery prefix: ' + str(parent))
            with safe_temp.directory(dir=index.parent, prefix='delivery-') as scratch:
                alternate = ct.Path(scratch) / 'index'
                env = {**live, 'GIT_INDEX_FILE': str(alternate)}
                ct._git(['read-tree', row['intent']['parent']], env=env)
                ct._check_attributes(env, ct._tree_entries(live, row['intent']['q_oid']))
                drl.drs.inspect(co)
                ct._git(['read-tree', '--reset', '-u', row['intent']['c2']], env=env)
                os.replace(alternate, index)
        drl.drs.inspect(co, exact_q=True)
        if tuple(ct._index_entries(live)) != ct._tree_entries(live, row['intent']['q_oid']):
            raise ValueError('recovery index differs from exact Q')
        row['phase'] = 'RECONCILED'
        atomic_json(co.evidence / 'delivery-publication.json', row)
        co.state.pop('hold_reason', None)
        co.state.update(status='ACCEPTED', publication_complete=row['intent']['digest'])
        co.state.pop('publication_hold', None)
        co.save()
        return row
