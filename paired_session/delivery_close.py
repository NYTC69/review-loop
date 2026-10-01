"""Lease-protected fake CLOSE; no merge, push or worktree cleanup is dispatched."""
import copy
import hashlib
import json
import os
from datetime import datetime
from importlib import import_module

prefix = 'paired_session.' if __package__ else ''
proof = import_module(prefix + 'delivery_close_proof')
protocol = proof.protocol


def close(co, expected_digest, atomic_json, external_delivery=False):
    if not co._fake_lifecycle or not protocol.lifecycle_spine.fake_dispatch_guard(co.args):
        raise ValueError('CLOSE is fake-only; real activation is disabled')
    if external_delivery is not False or co.state['config'].get('external_delivery', False) is not False:
        raise ValueError('external delivery activation unavailable; use local CLOSE only')
    with protocol.run_lease(co.run_dir), protocol.workspace_lease(co.workspace, co.run_dir):
        if json.loads(co.state_path.read_text()) != json.loads(json.dumps(co.state)):
            raise ValueError('saved state changed; reload before CLOSE')
        journal = json.loads((co.evidence / 'delivery-publication.json').read_text())
        intent, source = journal['intent'], journal['proof_state']['lifecycle']
        if expected_digest != intent['digest'] or journal['acceptance']['uid'] != os.getuid():
            raise ValueError('CLOSE requires explicit operator intent digest and attributed acceptance')
        body = {k: v for k, v in intent.items() if k != 'digest'}
        frozen = co.state['closeout_item']
        if (hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != intent['digest'] or
                frozen['item_id'] != intent['item_id'] or co.state['item_uuid'] != intent['item_uuid'] or
                frozen['close_adapter_sha256'] != intent['close_adapter_sha256'] or
                hashlib.sha256(proof.ct.Path(proof.adapter.__file__).read_bytes()).hexdigest() !=
                intent['close_adapter_sha256']):
            raise ValueError('CLOSE immutable item/adapter/intent binding differs; inspect and recover')
        path = co.evidence / 'delivery-close.json'
        existing = json.loads(path.read_text()) if path.exists() else None
        old_status, old_life = co.state['status'], co.state['lifecycle']
        try:
            co.state.update(status='ACCEPTED', lifecycle=copy.deepcopy(source))
            facts = proof.verify(co)
        finally:
            co.state.update(status=old_status, lifecycle=old_life)
        objects = proof.recovery.dj.intent_api.commit_environment(
            co, proof.ct.baseline_from_binding(journal['baseline']))
        if (proof.ct._git(['diff-tree', '--no-commit-id', '--name-only', '-r',
                           intent['p_oid'], intent['q_oid']], env=objects) != 'BACKLOG.md' or
                proof.ct._git(['rev-parse', intent['q_oid'] + ':BACKLOG.md'], env=objects) != intent['backlog_blob']):
            raise ValueError('CLOSE Q differs beyond the reviewed Compass mutation')
        facts.update(item_id=intent['item_id'], item_uuid=intent['item_uuid'], external_delivery=False)
        receipt = {'status': 'CLOSED', 'author': 'operator', 'uid': os.getuid(), 'facts': facts,
                   'timestamp': existing['timestamp'] if existing else datetime.now().astimezone().isoformat()}
        rows = [{'request_id': intent['digest'] + '-' + stage.lower(), 'stage': stage, 'status': 'READY',
                 'epoch': source['epoch'], 'item_uuid': source['item_uuid'], 'parent': source['parent'],
                 'candidate_oid': intent['p_oid'] if stage == 'DELIVERY' else intent['q_oid'],
                 'output_oid': intent['q_oid'], 'intent_digest': intent['digest']} for stage in ('DELIVERY', 'CLOSE')]
        closed_life = {**source, 'stage': 'CLOSED', 'candidate_oid': intent['q_oid'],
                       'receipts': [*source['receipts'], *rows], 'pending': None}
        def normalize(value):
            return json.loads(json.dumps(value))

        if old_status == 'CLOSED':
            if (existing != receipt or co.state.get('close_receipt') != receipt or
                    normalize(old_life) != normalize(closed_life)):
                raise ValueError('CLOSE replay differs from protected receipt; inspect and recover')
            return 'CLOSED'
        if old_status != 'ACCEPTED' or normalize(old_life) != source or existing not in (None, receipt):
            raise ValueError('CLOSE source differs from accepted publication; inspect and recover')
        atomic_json(path, receipt)
        try:
            co.state.update(status='ACCEPTED', lifecycle=copy.deepcopy(source))
            if proof.verify(co) != {k: v for k, v in facts.items()
                                   if k not in ('item_id', 'external_delivery')}:
                raise ValueError('CLOSE tree changed before state publication; inspect and recover')
        finally:
            co.state.update(status=old_status, lifecycle=old_life)
        co.state.update(status='CLOSED', lifecycle=closed_life, close_receipt=receipt)
        co.state.setdefault('events', []).append(receipt)
        co.save()
        return 'CLOSED'
