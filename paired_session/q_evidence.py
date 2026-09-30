"""Protected Q source verification; this module never promotes or delivers Q."""
import hashlib
import json
from pathlib import Path
import uuid
from importlib import import_module
ct = import_module(('paired_session.' if __package__ else '') + 'candidate_tree')
def binding(co):
    data = {'config': co.state['config'], 'programs': co.state['operator_programs'],
            'plan': hashlib.sha256((co.context / 'plan.md').read_bytes()).hexdigest()}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
def review_source(co, c1, day, observed_test):
    proposal = co.fake_materialize_q(c1, day)
    try:
        row = co.state['fake_q_review']
        for key in ('id', 'review_id'):
            if str(uuid.UUID(row[key])) != row[key]:
                raise ValueError('Q receipt ID')
        saved = json.loads((co.evidence / (row['review_id'] + '-q-review.json')).read_text())
        test = json.loads((co.evidence / (row['id'] + '-q-test.json')).read_text())
        expected = {'run_id': co.run_dir.name, 'item_uuid': co.state['item_uuid'],
                    'epoch': co.state['lifecycle']['epoch'], 'oid': proposal['q_oid']}
        if (saved != row or row['status'] != 'UNREVIEWED' or row['proposal'] != proposal or
                any(row.get(k) != v for k, v in expected.items()) or
                any(row.get(k) != v for k, v in test.items()) or row['returncode'] != 0 or
                row['binding_sha256'] != binding(co)):
            raise ValueError('Q source differs from protected evidence or current binding')
        turns = [t for t in co.state['turns'] if t['sequence'] == row['sequence']]
        if (len(turns) != 1 or type(row['review_after_sequence']) is not int or
                type(row['sequence']) is not int or row['sequence'] <= row['review_after_sequence']):
            raise ValueError('Q sequence is not fresh and unique')
        turn = turns[0]
        if (turn['phase'] != 'Q' or turn['role'] != 'reviewer' or turn['workspace'] != row['root'] or
                turn.get('error') or not co.q_review_verdict(turn['answer']) or
                row.get('proof') != co.q_proof(turn, proposal['q_oid']) or
                not any(observed_test(c, co.args.test_command) for c in turn.get('observed_commands', []))):
            raise ValueError('Q reviewer phase/workspace/result/check evidence differs')
        if hashlib.sha256(Path(row['command'][0]).read_bytes()).hexdigest() != row['executable_sha256']:
            raise ValueError('Q test executable changed')
        base = ct.baseline_from_binding(co.state['fake_ingest_receipt']['baseline'])
        env = ct._git_env(GIT_DIR=str(base.git_dir))
        rev = ct.CandidateRevision(proposal['q_oid'], ct._manifest(env, base.tree_oid, proposal['q_oid']), 0)
        root = ct.replace(base, root=Path(row['root']), index=Path(row['index']),
                          root_identity=tuple(row['root_identity']),
                          authorized_prefixes=(*base.authorized_prefixes, 'BACKLOG.md'))
        ct.verify_candidate_revision(root, rev)
        return row, root, rev
    except (KeyError, TypeError, AttributeError, OSError, json.JSONDecodeError) as error:
        raise ValueError('Q source missing or malformed; abort and start a new run') from error
