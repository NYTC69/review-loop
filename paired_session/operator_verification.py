"""OPV (v2.9.5): operator-run verification evidence bound to one workspace tree.

The operator attaches a check it ran outside the author sandbox (command, cwd, exit code, log copy, note). The record is
bound to the workspace snapshot digest, voided for good once the tree differs or its log copy changes, and shown (without
the note) to the EXEC reviewer, shadow and gate prompts only for that exact tree. It is prompt evidence only: no
verdict path reads it."""
import copy
import hashlib
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path

MAX_LOG_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 4096
EXCERPT_CHARS = 2000
SHOWN = 3   # the most recent current records for this tree


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _read_log(source: Path) -> bytes:
    with os.fdopen(os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('--log must be a regular file')
        data = stream.read(MAX_LOG_BYTES + 1)
    if len(data) > MAX_LOG_BYTES:
        raise ValueError(f'--log is larger than {MAX_LOG_BYTES} bytes')
    return data


def _entry(row: dict, data: bytes) -> str:
    text = data.decode('utf-8', errors='replace')
    tail = text[-EXCERPT_CHARS:]
    return '\n'.join([f"- {row['id']} ({row['actor']}, {row['time']}): command `{' '.join(row['command'].split())}` in {row['cwd']}; "
                      f"exit code {row['exit_code']}; log sha256 {row['log_sha256']} ({row['log_bytes']} bytes)",   # the note stays in state and evidence
                      f'  Log tail (last {len(tail)} of {len(text)} characters):',
                      *('    | ' + line for line in tail.splitlines())])


def _block(tree_sha256: str, entries: list) -> str:
    return '\n'.join(['', '## Operator-verified evidence for this exact tree',
                      f'The operator ran each command below outside the author sandbox on this exact workspace tree '
                      f'(snapshot {tree_sha256}) and attached its log; the coordinator withdraws a record as soon as the tree '
                      'changes. Use it as evidence for a check you cannot run in your own sandbox. It is not a decision: '
                      'still inspect the delta, run your allowed checks and judge the change yourself.', *entries])


def _intact(row: dict) -> bool:
    try: return hashlib.sha256(Path(row['log_evidence']).read_bytes()).hexdigest() == row['log_sha256']
    except OSError: return False


def void_stale(co, tree_sha256, write_json) -> None:
    """Void for good every current record whose tree differs from `tree_sha256` (None: unknown) or whose log copy changed."""
    for row in co.state.get('operator_verifications', []):
        if row['status'] != 'current':
            continue
        if row['tree_sha256'] != tree_sha256:
            why = 'tree changed'
        elif not _intact(row):
            why = 'log copy changed'
        else:
            continue
        row.update(status='voided', voided={'time': datetime.now().astimezone().isoformat(), 'reason': why, 'tree_seen': tree_sha256})
        write_json(co.evidence / f"operator-verification-{row['id']}.json", row)
        co.save()


def prompt_block(co, tree_sha256: str, write_json, role: str = '') -> str:
    """'' when no current record is bound to this exact tree, else the block appended to a reviewer, shadow or gate prompt. The persistent
    reviewer (role 'reviewer') also gets one Withdrawn line, id and reason only, for each voided record its thread was shown (OPV-M1)."""
    void_stale(co, tree_sha256, write_json)
    rows = co.state.get('operator_verifications', [])
    shown = [row for row in rows if row['status'] == 'current'][-SHOWN:]
    withdrawn = [f"\nWithdrawn operator verification: {row['id']} ({row['voided']['reason']}); do not rely on it." for row in rows
                 if role == 'reviewer' and row['status'] == 'voided' and row.get('shown_to_reviewer')]
    if role == 'reviewer' and shown:
        for row in shown:
            row['shown_to_reviewer'] = True
        co.save()
    block = _block(tree_sha256, [_entry(row, Path(row['log_evidence']).read_bytes()) for row in shown]) if shown else ''
    return block + ''.join(withdrawn)


def current_for_acceptance(co, tree_sha256: str, write_json) -> list:
    """N4-e: the records still current for the accepted tree, as acceptance.json lists them."""
    void_stale(co, tree_sha256, write_json)
    return [{key: row[key] for key in ('id', 'command', 'cwd', 'exit_code', 'log_sha256', 'log_evidence', 'note', 'time', 'tree_sha256')}
            for row in co.state.get('operator_verifications', []) if row['status'] == 'current']


def _scan(co, text: str) -> None:   # the fresh-role history scan the shadow and gate prompts get, on the block alone
    with tempfile.TemporaryDirectory() as tmp:
        trial = copy.copy(co)
        trial.context = trial.evidence = Path(tmp)
        trial.workitem = Path(tmp) / 'workitem.md'
        trial.workitem.write_text('')
        trial.state = {'sequence': 0, 'finding_ledger': co.state.get('finding_ledger', [])}
        try: trial.assert_fresh_prompt('shadow', text)
        except RuntimeError as exc: raise ValueError('fresh-role input scan: ' + str(exc) + '; trim the log') from exc


def attach(co, args, tree_sha256: str, write_json) -> str:
    state = co.state
    if state.get('status') not in ('ACTIVE', 'HOLD', 'DONE') or state.get('active') or state.get('uncertain_active'):   # N4-e: DONE before accept too
        raise ValueError('attach-verification requires an idle ACTIVE, HOLD or DONE run')
    command, note = (args.command or '').strip(), (args.note or '').strip()
    if not command or not note or args.exit_code is None or not args.log or not args.log_sha256:
        raise ValueError('attach-verification needs --command, --exit-code, --log, --log-sha256 and a non-empty --note')
    if max(len(command.encode()), len(note.encode())) > MAX_TEXT_BYTES:
        raise ValueError(f'--command and --note are limited to {MAX_TEXT_BYTES} bytes')
    cwd = Path(args.cwd or co.workspace).expanduser().resolve()
    if not cwd.is_dir() or not _inside(cwd, co.workspace):
        raise ValueError('--cwd must be the workspace or a directory inside it')
    source = Path(args.log).expanduser().resolve()
    if _inside(source, co.workspace) or _inside(source, co.run_dir):
        raise ValueError('--log must be outside the workspace and run dir')
    try: data = _read_log(source)
    except OSError as exc: raise ValueError('cannot read --log: ' + str(exc)) from exc
    if (digest := hashlib.sha256(data).hexdigest()) != args.log_sha256.strip().lower():
        raise ValueError('log sha256 mismatch: the log file hashes to ' + digest)
    void_stale(co, tree_sha256, write_json)
    record_id = f"V{len(state.get('operator_verifications', [])) + 1:03d}"
    row = {'id': record_id, 'actor': 'operator', 'uid': os.getuid(), 'time': datetime.now().astimezone().isoformat(),
           'phase': state.get('phase'), 'command': command, 'cwd': str(cwd), 'exit_code': args.exit_code,
           'log_sha256': digest, 'log_bytes': len(data), 'log_source': str(source),
           'log_evidence': str(co.evidence / f'operator-verification-{record_id}.log'), 'note': note,
           'tree_sha256': tree_sha256, 'status': 'current'}
    _scan(co, _block(tree_sha256, [_entry(row, data)]))
    temp = Path(row['log_evidence'] + '.tmp')
    with temp.open('wb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, row['log_evidence'])
    write_json(co.evidence / f'operator-verification-{record_id}.json', row)
    state.setdefault('operator_verifications', []).append(row)
    co.save()
    return f'{record_id} bound to tree {tree_sha256}'
