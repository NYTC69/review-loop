"""Offline FINISH dispatch boundary; no live lifecycle route calls this module."""
import hashlib
import json
from pathlib import Path
import uuid

from paired_session import candidate_tree


class FinishDispatchError(ValueError):
    pass


def dispatch(baseline, revision, proof, epoch, role_manifest_sha256, launch, sandbox_stopped):
    """The injected launcher and stop checker are coordinator-owned, never model output."""
    root = Path(baseline.root).resolve()
    workspace = Path(baseline.workspace).resolve()
    run_dir = Path(baseline.run_dir).resolve()
    if not baseline.separate_filesystems or root == workspace or workspace in root.parents:
        raise FinishDispatchError('FINISH candidate is not isolated from the live workspace')
    if root == run_dir or run_dir in root.parents:
        raise FinishDispatchError('FINISH candidate overlaps run state')
    if not isinstance(proof, dict) or proof.get('blocking_findings') != []:
        raise FinishDispatchError('FINISH has open or unverified upstream blockers')
    run_id, convergence = proof.get('run_id'), proof.get('convergence_id')
    if run_id != run_dir.name or not isinstance(convergence, str) or not convergence:
        raise FinishDispatchError('FINISH run or convergence identity is missing')
    expected = {'run_id': run_id, 'convergence_id': convergence, 'epoch': epoch,
                'candidate_oid': revision.tree_oid, 'parent_head': baseline.parent_head,
                'workspace': str(workspace), 'run_dir': str(run_dir), 'phase': 'EXEC'}
    for role, field, value in (('reviewer', 'status', 'APPROVE'), ('gate', 'verdict', 'approve')):
        receipt = proof.get(role)
        if (not isinstance(receipt, dict) or receipt.get('role') != role or
                receipt.get(field) != value or any(receipt.get(key) != item for key, item in expected.items())):
            raise FinishDispatchError('FINISH lacks current-convergence reviewer and gate approval')
    if not isinstance(role_manifest_sha256, str) or len(role_manifest_sha256) != 64:
        raise FinishDispatchError('FINISH role identity is not frozen')
    candidate_tree.verify_candidate_revision(baseline, revision)
    request = {'request_id': uuid.uuid4().hex, 'role': 'finisher', 'phase': 'FINISH', 'fresh': True,
               'candidate_root': str(root), 'candidate_oid': revision.tree_oid, 'run_id': run_id,
               'epoch': epoch, 'convergence_id': convergence, 'parent_head': baseline.parent_head,
               'role_manifest_sha256': role_manifest_sha256,
               'authorized_prefixes': list(baseline.authorized_prefixes),
               'approval_sha256': hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()}
    request['sha256'] = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    turn = launch(request)
    if (not isinstance(turn, dict) or not isinstance(turn.get('sandbox_id'), str) or
            not turn['sandbox_id'] or turn.get('request_sha256') != request['sha256']):
        raise FinishDispatchError('FINISH launch receipt is unbound')
    if sandbox_stopped(turn['sandbox_id']) is not True:
        raise FinishDispatchError('FINISH sandbox processes are not proven stopped')
    if turn.get('status') != 'READY':
        raise FinishDispatchError('FINISH author did not report READY')
    current = candidate_tree.ingest_candidate_revision(baseline)
    changed = current.tree_oid != revision.tree_oid
    return {'status': 'EXEC_REVIEW_REQUIRED' if changed else 'TESTS_REQUIRED',
            'input_oid': revision.tree_oid, 'output_oid': current.tree_oid,
            'requires_fresh_exec_and_gate': changed, 'request_sha256': request['sha256']}
