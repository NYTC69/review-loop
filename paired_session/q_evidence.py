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


def review_bundle(co, c1, day, observed_test):
    """Re-read protected Q proofs before any fake delivery intent or Git write."""
    try:
        if co._program_state()[1]:
            raise ValueError('Q delivery program binding changed; repair and start a new run')
        source, root, revision = review_source(co, c1, day, observed_test)
        row, life = co.state['fake_q_bundle'], co.state['lifecycle']
        if str(uuid.UUID(row['id'])) != row['id']:
            raise ValueError('Q bundle ID is malformed')
        saved = json.loads((co.evidence / (row['id'] + '-q-bundle.json')).read_text())
        noops = json.loads(json.dumps([r for r in life['receipts'] if r['epoch'] == life['epoch'] and
                 r['stage'] in ('FINISH', 'POLISH-Q', 'DOCS')]))
        if (saved != row or row['status'] != 'REVIEWED' or row['source'] != source or
                row['source_id'] != source['review_id'] or row['p_noops'] != noops or
                [r['stage'] for r in noops] != ['FINISH', 'POLISH-Q', 'DOCS'] or
                co.state.get('fake_q_bundle_pending') or co.state.get('fake_q_pending') or
                co.state.get('pending_reviewer_result_sequence') or
                co.state['sequence'] != row['proofs'][-1]['sequence']):
            raise ValueError('Q bundle differs or is pending; abort and start a new run')
        if any(r.get('status') != 'READY' or r['candidate_oid'] != source['proposal']['p_oid'] or
               r['output_oid'] != r['candidate_oid'] or r['item_uuid'] != life['item_uuid'] or
               r['parent'] != life['parent'] for r in noops):
            raise ValueError('Q P no-op receipt changed')
        proofs = [source['proof'], *row['proofs']]
        if [proof['phase'] for proof in proofs] != ['Q', 'Q-GATE', 'Q-FINAL', 'Q-SECURITY']:
            raise ValueError('Q fresh role proofs missing or out of order')
        previous = source['review_after_sequence']
        for proof in proofs:
            turns = [t for t in co.state['turns'] if t['sequence'] == proof['sequence']]
            if len(turns) != 1:
                raise ValueError('Q turn is not unique')
            turn = turns[0]
            path = co.evidence / f"{turn['sequence']:03d}-{proof['phase'].lower()}-{proof['role']}.receipt.json"
            if (json.loads(path.read_text()) != turn or turn.get('error') or
                    turn['phase'] != proof['phase'] or turn['role'] != proof['role'] or
                    turn['workspace'] != str(root.root) or type(turn['sequence']) is not int or
                    turn['sequence'] <= previous or proof != co.q_proof(turn, revision.tree_oid) or
                    co.configured_test_failed(turn) or
                    not any(observed_test(c, co.args.test_command) for c in turn.get('observed_commands', []))):
                raise ValueError('Q disk turn/proof/test differs; abort and start a new run')
            previous = turn['sequence']
        if [proof['role'] for proof in proofs] != ['reviewer', 'gate', 'reviewer', 'reviewer']:
            raise ValueError('Q role ownership differs')
        paths = [r[2] for r in ct._tree_entries(ct._git_env(GIT_DIR=str(root.git_dir)), revision.tree_oid)]
        if row['scanned_paths'] != paths:
            raise ValueError('Q sensitive preflight manifest differs')
        return json.loads(json.dumps(row)), root, revision
    except (KeyError, TypeError, IndexError, AttributeError, OSError, json.JSONDecodeError) as error:
        raise ValueError('Q delivery proof missing or malformed; abort and start a new run') from error
