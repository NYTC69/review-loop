import hashlib, json, os
from datetime import datetime
from importlib import import_module
qe = import_module(('paired_session.' if __package__ else '') + 'q_evidence')
def commit_environment(co, root):
    stamp = '@' + str(int(co.state['started_at'])) + ' +0000'
    return qe.ct._git_env(GIT_DIR=str(root.git_dir), PATH=co.state['operator_programs']['path_env'],
                       GIT_AUTHOR_NAME='paired-session', GIT_AUTHOR_EMAIL='paired-session@localhost',
                       GIT_COMMITTER_NAME='paired-session', GIT_COMMITTER_EMAIL='paired-session@localhost',
                       GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
def prepare(co, c1, day, observed_test, atomic_json):
    if (co.state['status'] not in ('HOLD', 'DONE', 'ACCEPTED') or
            (co.state['status'] == 'HOLD' and co.state.get('hold_reason') not in (
                'PLAN approved; stopped by --stop-after-plan; resume enters EXEC',
                'fake lifecycle has reviewed EXEC; router binding is pending'))):
        raise ValueError('delivery preparation refuses this HOLD/state; abort or start a new run')
    bundle, root, revision = qe.review_bundle(co, c1, day, observed_test)
    env = commit_environment(co, root)
    message = ('paired-session close ' + co.state['item_uuid'] + '\n').encode()
    c2 = qe.ct._git(['commit-tree', '--no-gpg-sign', revision.tree_oid, '-p', c1, '-m', message.decode()], env=env)
    raw = qe.ct._git_bytes(['cat-file', 'commit', c2], env=env)
    live = co.operator_intent('accept', None, None)
    live = json.loads(json.dumps({k: v for k, v in live.items() if k not in ('state_sha256', 'digest')}))
    intent = {**live, **bundle['source']['proposal'], 'author': 'operator', 'day': day, 'ref': root.parent_ref,
              'c2': c2, 'c2_sha256': hashlib.sha256(raw).hexdigest(), 'status': 'PREPARED',
              'bundle_sha256': hashlib.sha256(json.dumps(bundle, sort_keys=True).encode()).hexdigest()}
    intent['digest'] = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
    path = co.evidence / 'delivery-intent.json'
    if path.exists() and json.loads(path.read_text()) != intent:
        raise ValueError('delivery intent changed; abort and start a new run')
    if co.state.get('fake_delivery_intent') not in (None, intent):
        raise ValueError('delivery state differs from intent')
    atomic_json(path, intent)
    co.state['fake_delivery_intent'] = intent
    if co.state['status'] == 'HOLD':
        co.state['status'], co.state['acceptance_state'] = 'DONE', 'PENDING'
    co.state.update(approved_snapshot=live['tree_sha256'], approved_manifest=live['tree_snapshot'])
    co.save()
    return json.loads(json.dumps(intent))
def accept(co, observed_test, atomic_json):
    old = co.state['fake_delivery_intent']
    if co.state['status'] != 'DONE' or co.state['acceptance_state'] != 'PENDING':
        raise ValueError('delivery accept requires DONE/PENDING; abort and start a new run')
    intent = prepare(co, old['c1'], old['day'], observed_test, atomic_json)
    if not co.args.expect or co.args.expect != intent['digest'] or co.args.override_rejection:
        raise ValueError('fake delivery requires its current intent digest and no rejection override')
    record = {'author': 'operator', 'uid': os.getuid(), 'timestamp': datetime.now().astimezone().isoformat(),
              'intent_digest': intent['digest'], 'c1': intent['c1'], 'c2': intent['c2'], 'q_oid': intent['q_oid']}
    atomic_json(co.evidence / 'delivery-acceptance.json', record)
    co.state['fake_delivery_acceptance'] = record
    co.state.setdefault('events', []).append(record)
    co.state['status'], co.state['acceptance_state'] = 'ACCEPTED', 'ACCEPTED'
    co.save()
    return 'ACCEPTED'
