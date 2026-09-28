import hashlib
import json
import re
from fnmatch import fnmatchcase
from pathlib import Path
from collections import namedtuple
try:
    from .candidate_tree import _canonical_parts, _prefixes
except ImportError:
    from candidate_tree import _canonical_parts, _prefixes
PATTERNS = json.loads(Path(__file__).with_name('security_ignore_patterns.json').read_text())
ReservedDocsRepair = type('ReservedDocsRepair', (ValueError,), {})
SecurityRepair = namedtuple('SecurityRepair',
    'digest fixer_paths approved_patterns consent_digest next_phase gate_ran '
    'invalidated_receipts security_review_required')
def _paths(values):
    if not isinstance(values, (list, tuple)): raise ValueError('paths must be a list or tuple')
    return tuple(_prefixes(values)) if values else ()
def _proposal(run_id, oid, proposals, repair_paths):
    if (not isinstance(run_id, str) or not run_id or any(ord(c) < 32 or ord(c) == 127 for c in run_id) or
            not isinstance(oid, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', oid)):
        raise ValueError('run identity and candidate OID are required')
    if not isinstance(proposals, (list, tuple)): raise ValueError('ignore proposal must be a list or tuple')
    records = []
    for entry in proposals:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3: raise ValueError('ignore proposal is malformed')
        category, pattern, matches = entry
        if category not in PATTERNS or pattern not in PATTERNS[category]: raise ValueError('outside frozen policy')
        records.append((category, pattern, _paths(matches)))
    return tuple(sorted(records)), _paths(repair_paths)
def repair_digest(run_id, oid, proposals, repair_paths):
    records, paths = _proposal(run_id, oid, proposals, repair_paths)
    payload = json.dumps((run_id, oid, records, paths), ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(payload).hexdigest()
def plan_security_repair(run_id, oid, proposals, repair_paths, allowed_paths,
                         reserved_docs, candidate_files, consent=None):
    records, paths = _proposal(run_id, oid, proposals, repair_paths)
    wanted = set(paths) | ({'.gitignore'} if records else set())
    leaves = set(_paths(candidate_files)) | {'.gitignore'}
    if not wanted or not wanted <= set(_paths(allowed_paths)) or not wanted <= leaves:
        raise ValueError('security repair lacks an exact candidate-file grant')
    if any(_canonical_parts(p)[-1] in ('.gitignore', '.gitattributes') for p in paths):
        raise ValueError('ignore repair requires frozen proposals; attributes edits are forbidden')
    if any(a[:len(b)] == b or b[:len(a)] == a for p in wanted for q in _paths(reserved_docs)
           for a, b in [(_canonical_parts(p), _canonical_parts(q))]):
        raise ReservedDocsRepair('reserved docs repair requires replayed DOCS writer')
    digest = repair_digest(run_id, oid, proposals, repair_paths)
    if any(set(m) != {f for f in leaves if (
        p.lstrip('!').casefold().rstrip('/') in _canonical_parts(f)[:-1] if p.endswith('/') else
        fnmatchcase(_canonical_parts(f)[-1], p.lstrip('!').casefold()))}
        for _, p, m in records): raise ValueError('tracked matches differ from candidate')
    needs_consent = any(c in {'Cloud credentials', 'Generic secret files'} or m or p.startswith('!')
                        for c, p, m in records)
    if needs_consent or consent is not None:
        command = hashlib.sha256(f'confirm-ignore --digest {digest}'.encode()).hexdigest()
        if not isinstance(consent, dict) or consent.get('decision') == 'decline':
            raise ValueError('operator declined ignore proposal' if consent else 'operator confirmation required')
        if (consent.get('decision') != 'confirm' or consent.get('digest') != digest or
                consent.get('run_id') != run_id or consent.get('actor') != 'operator' or
                not isinstance(consent.get('time'), str) or not consent['time'] or
                consent.get('command_sha256') != command):
            raise ValueError('operator confirmation for this OID and ignore proposal is required')
    return SecurityRepair(digest, tuple(sorted(wanted)), tuple((c, p) for c, p, _ in records),
                          digest if needs_consent else '', 'EXEC', False, ('*',), True)
