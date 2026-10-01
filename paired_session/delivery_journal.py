import hashlib, json, importlib
from pathlib import Path
intent_api = importlib.import_module(('paired_session.' if __package__ else '') + 'delivery_intent')
ct, qe = intent_api.qe.ct, intent_api.qe
_KEYS = ('lifecycle', 'turns', 'sequence', 'fake_ingest_receipt', 'fake_q_review', 'fake_q_bundle',
         'active', 'uncertain_active', 'fake_q_pending', 'fake_q_bundle_pending', 'pending_reviewer_result_sequence')
def proof_state(co):
    return {'binding': qe.binding(co), **{key: co.state.get(key) for key in _KEYS}}
def prepare(co, observed_test, atomic_json):
    path = co.evidence / 'delivery-publication.json'
    if path.exists():
        row = json.loads(path.read_text())
        verify(co, row)
        return row
    old = co.state['fake_delivery_intent']
    intent = intent_api.prepare(co, old['c1'], old['day'], observed_test, atomic_json)
    bundle, root, revision = qe.review_bundle(co, intent['c1'], intent['day'], observed_test)
    paths = [*co.evidence.glob('*.json'), co.context / 'plan.md', root.git_dir / 'config']
    row = json.loads(json.dumps({'intent': intent, 'acceptance': co.state['fake_delivery_acceptance'], 'bundle': bundle,
           'baseline': {k: str(v) if isinstance(v, Path) else v for k, v in vars(root).items()},
           'manifest': revision.manifest, 'proof_state': proof_state(co), 'phase': 'PREPARED',
           'files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}))
    verify(co, row)
    atomic_json(path, row)
    return row
def verify(co, row):
    path = co.evidence / 'delivery-publication.json'
    if (not co._fake_lifecycle or co.state['acceptance_state'] != 'ACCEPTED' or
            (path.exists() and json.loads(path.read_text()) != row) or co.state['status'] != 'ACCEPTED'):
        raise ValueError('journal requires fake attributed acceptance')
    intent, acceptance = row['intent'], row['acceptance']
    if (co.state.get('fake_delivery_intent') != intent or co.state.get('fake_delivery_acceptance') != acceptance or
            acceptance['intent_digest'] != intent['digest'] or acceptance['author'] != 'operator' or
            any(acceptance[k] != intent[k] for k in ('c1', 'c2', 'q_oid')) or co._program_state()[1]):
        raise ValueError('journal acceptance/program binding differs; operator recovery required')
    if (row['bundle'] != co.state['fake_q_bundle'] or
            json.loads(json.dumps(proof_state(co))) != row['proof_state'] or
            any(hashlib.sha256(Path(p).read_bytes()).hexdigest() != digest for p, digest in row['files'].items())):
        raise ValueError('journal proof changed; operator recovery required')
    root = ct.baseline_from_binding(row['baseline'])
    revision = ct.CandidateRevision(intent['q_oid'], tuple(row['manifest']), 0)
    ct.verify_candidate_contents(root, revision)
    intent_api.seal.verify(co, root, row['bundle']['source']['proposal'])
    env = ct._git_env(GIT_DIR=str(co.workspace / '.git'), PATH=co.state['operator_programs']['path_env'])
    head = ct._git(['rev-parse', '--verify', 'HEAD'], env=env)
    if (row['phase'] not in ('PREPARED', 'PUBLISHED', 'RECONCILED') or
            head not in (intent['parent'], intent['c2']) or
            ct._git(['symbolic-ref', '-q', 'HEAD'], env=env) != intent['ref'] or
            (row['phase'] != 'PREPARED' and head != intent['c2'])):
        raise ValueError('publication HEAD/ref/phase drift; operator recovery required')
    objects = env if head == intent['c2'] else intent_api.commit_environment(co, root)
    for oid, tree, parent, digest in ((intent['c1'], intent['p_oid'], intent['parent'], intent['c1_sha256']),
                                    (intent['c2'], intent['q_oid'], intent['c1'], intent['c2_sha256'])):
        raw = ct._git_bytes(['cat-file', 'commit', oid], env=objects)
        headers = raw.split(b'\n\n', 1)[0].splitlines()
        if (hashlib.sha256(raw).hexdigest() != digest or
                [h for h in headers if h.startswith(b'tree ')] != [('tree ' + tree).encode()] or
                [h for h in headers if h.startswith(b'parent ')] != [('parent ' + parent).encode()]):
            raise ValueError('published commit chain differs from accepted intent')
    return root, revision, env
