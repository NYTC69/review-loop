"""Fake-only persisted lifecycle receipts; stage routing is a later batch."""
import os
from pathlib import Path

def fake_guard(args):
    root_text = os.environ.get('FAKE_CODEX_TEST_ROOT')
    if not root_text: return False
    root = Path(root_text).resolve()
    if root in (Path('/'), Path.home().resolve()): return False
    if not Path(args.codex_bin).is_absolute() or not Path(args.claude_bin).is_absolute(): return False
    names = ('workspace', 'workitem', 'run_dir', 'codex_bin', 'claude_bin')
    return all(root in Path(getattr(args, name)).resolve().parents for name in names)

def initial(item_uuid, parent):
    return {'item_uuid': item_uuid, 'stage': 'EXEC', 'epoch': 0,
            'candidate_oid': None, 'parent': parent, 'receipts': [], 'pending': None}
def begin(life, request):
    expected = {key: life[key] for key in ('item_uuid', 'stage', 'epoch', 'candidate_oid', 'parent')}
    if not isinstance(request, dict) or any(request.get(key) != value for key, value in expected.items()):
        raise ValueError('lifecycle request differs from persisted item state')
    if not isinstance(request.get('request_id'), str) or not request['request_id']: raise ValueError('request ID')
    if life['pending'] == request: return life
    if life['pending'] or any(row['request_id'] == request['request_id'] for row in life['receipts']):
        raise ValueError('lifecycle request is pending or already completed')
    return {**life, 'pending': dict(request)}

def complete(life, receipt):
    if not isinstance(receipt, dict): raise ValueError('lifecycle receipt is malformed')
    matches = [row for row in life['receipts'] if row['request_id'] == receipt.get('request_id')]
    if matches:
        if matches[0] == receipt: return life
        raise ValueError('lifecycle receipt conflicts with persisted result')
    pending = life['pending']
    if not pending or any(key not in receipt or receipt[key] != value for key, value in pending.items()):
        raise ValueError('lifecycle receipt does not match persisted request')
    if receipt.get('status') not in ('READY', 'HOLD'): raise ValueError('lifecycle receipt has no terminal status')
    return {**life, 'pending': None, 'receipts': [*life['receipts'], dict(receipt)]}
