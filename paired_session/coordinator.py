#!/usr/bin/env python3
"""Stdlib-only paired PLAN/EXEC coordinator for one real work item.

The coordinator owns transport, snapshots, limits, and evidence.  Models never
write transport files.  Persistent author/reviewer threads span both phases;
shadow and adversarial reviewers are always fresh.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Optional

HERE = Path(__file__).resolve().parent
DEFAULT_GATE_PROMPT = HERE.parent / 'scripts' / 'adversarial_gate_fallback_prompt.txt'
RUBRIC = ('Trigger', 'Reachability', 'Impact', 'Likelihood', 'Fix cost', 'Cheaper response')
PROBE_SURFACE_VERSION = 7
PLAN_STOP_REASON = 'PLAN approved; stopped by --stop-after-plan; resume enters EXEC'
OUTPUT_TAIL_LINES = 15
OUTPUT_TAIL_CHARS = 1500


class RunLeaseError(RuntimeError):
    pass


class RateLimitError(ValueError):
    pass


def classify_rate_limit_failure(returncode: int, stderr: str, stdout: str = '') -> Optional[dict]:
    """Classify only explicit provider quota/rate-limit failures; other CLI errors stay billable."""
    if returncode == 0:
        return None
    reset_sources = []
    strong_message = re.compile(
        r'(?i)(?:\bHTTP\s*/?\d*(?:\.\d+)?\s*429\b|\b429\s+(?:too many requests|rate limit)\b|'
        r'\btoo many requests\b|\brate[_ -]limit(?:_exceeded|_error| exceeded| error)\b|'
        r'\brate limit(?: has been| was)? (?:reached|exceeded)\b|'
        r'\b(?:subscription|usage|quota) (?:rate )?limit (?:has been )?(?:reached|exceeded)\b|'
        r"\byou(?:'| a)?ve hit (?:your )?usage limit\b|\binsufficient_quota\b)"
    )
    confirmed = bool(strong_message.search(stderr))
    if confirmed:
        reset_sources.append(stderr)
    known_codes = {'rate_limit', 'rate_limit_error', 'rate_limit_exceeded',
                   'insufficient_quota', 'usage_limit_exceeded', 'quota_exceeded'}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        is_error = event.get('type') in ('error', 'turn.failed') or event.get('is_error') is True
        if not is_error:
            continue
        error = event.get('error')
        objects = [event, error] if isinstance(error, dict) else [event]
        codes = {str(obj.get(key, '')).strip().lower() for obj in objects
                 for key in ('code', 'error_code', 'error_type') if obj.get(key) not in (None, '')}
        if isinstance(error, dict) and error.get('type') not in (None, ''):
            codes.add(str(error['type']).strip().lower())
        statuses = [obj.get(key) for obj in objects for key in ('status', 'status_code', 'http_status')]
        message = error.get('message', '') if isinstance(error, dict) else event.get('message', '')
        message = str(message)
        if (codes & known_codes or any(str(value) == '429' for value in statuses)
                or (not codes and strong_message.search(message))):
            confirmed = True
            reset_sources.append(json.dumps(event, ensure_ascii=False))
    if not confirmed:
        return None
    reset_hint = None
    reset_cues = ('try again at', 'reset at', 'resets at', 'reset time',
                  'available again at', 'available at', 'retry after', 'retry-after', 'retry_after')
    for source in reset_sources:
        for line in source.splitlines():
            lowered = line.lower()
            positions = [lowered.find(cue) for cue in reset_cues if lowered.find(cue) >= 0]
            if positions:
                start = min(positions)
                reset_hint = line[start:start + 240].strip()
                break
        if reset_hint:
            break
    return {'kind': 'rate_limited', 'reset_hint': reset_hint}


def read_text_tail(path: Path, max_bytes: int = 65536) -> str:
    with path.open('rb') as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(max(0, size - max_bytes), os.SEEK_SET)
        return source.read(max_bytes).decode('utf-8', 'replace')


def resolve_test_executable(workspace: Path, command: str) -> str:
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise ValueError('configured test command has invalid quoting: ' + str(exc)) from exc
    if not words:
        raise ValueError('configured test command is empty')

    def resolve_binary(executable: str) -> Optional[str]:
        if os.path.isabs(executable) or os.sep in executable or (os.altsep and os.altsep in executable):
            path = Path(executable)
            if not path.is_absolute():
                path = workspace / path
            path = path.resolve()
            return str(path) if path.is_file() and os.access(path, os.X_OK) else None
        return shutil.which(executable)

    assignment = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=.*')
    if Path(words[0]).name == 'env':
        if not resolve_binary(words[0]):
            raise ValueError('configured test command launcher is missing: ' + words[0])
        tokens = words[1:]
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == '--':
                index += 1
                break
            if token in ('-i', '--ignore-environment', '-v', '--debug'):
                index += 1
            elif token in ('-u', '--unset', '-C', '--chdir'):
                if index + 1 >= len(tokens):
                    raise ValueError('configured env command is missing an option value: ' + token)
                index += 2
            elif token.startswith('--unset=') or token.startswith('--chdir='):
                index += 1
            elif token in ('-S', '--split-string'):
                if index + 1 >= len(tokens):
                    raise ValueError('configured env command is missing a split-string value')
                tokens = shlex.split(tokens[index + 1]) + tokens[index + 2:]
                index = 0
            elif token.startswith('-'):
                raise ValueError('unsupported configured env option during test preflight: ' + token)
            elif assignment.fullmatch(token):
                index += 1
            else:
                break
        words = tokens[index:]
    while words and assignment.fullmatch(words[0]):
        words = words[1:]
    if not words:
        raise ValueError('configured test command has no executable')
    executable = words[0]
    resolved = resolve_binary(executable)
    if not resolved:
        raise ValueError('configured test executable is missing or not on PATH: ' + executable)
    return resolved


@contextmanager
def run_lease(run_dir: Path):
    """Allow only one live coordinator process to mutate a run directory."""
    root = Path(run_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / '.coordinator.lock'
    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RunLeaseError('cannot open coordinator lease: ' + str(exc)) from exc
    acquired = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RunLeaseError('coordinator lease path is not a regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            os.lseek(fd, 0, os.SEEK_SET)
            owner = os.read(fd, 1024).decode('utf-8', 'replace').strip()
            detail = ''
            if owner:
                try:
                    details = json.loads(owner)
                    detail = f" (owner pid {details.get('pid', 'unknown')})"
                except json.JSONDecodeError:
                    detail = ''
            raise RunLeaseError('another coordinator currently owns this run' + detail) from exc
        payload = json.dumps({'pid': os.getpid(), 'started_at': datetime.now().astimezone().isoformat()})
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload.encode('utf-8'))
        os.fsync(fd)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _workspace_lease_temp_roots() -> tuple[Path, ...]:
    roots = (Path('/tmp'), Path('/var/tmp'), Path(tempfile.gettempdir()))
    return tuple(dict.fromkeys(root.expanduser().resolve() for root in roots))


def workspace_lease_path(workspace: Path) -> Path:
    """Return a stable per-user lock path outside the resolved product workspace."""
    root = Path(workspace).expanduser().resolve()
    uid = os.getuid()
    for temp_root in _workspace_lease_temp_roots():
        lock_dir = temp_root / f'paired-session-workspace-leases-{uid}'
        try:
            lock_dir.relative_to(root)
        except ValueError:
            pass
        else:
            continue
        key = hashlib.sha256(os.fsencode(str(root))).hexdigest()
        return lock_dir / (key + '.lock')
    raise RunLeaseError('cannot locate workspace lease outside the product workspace')


@contextmanager
def workspace_lease(workspace: Path, run_dir: Path):
    """Allow only one mutating coordinator across run directories for a workspace."""
    root = Path(workspace).expanduser().resolve()
    lock_path = workspace_lease_path(root)
    lock_dir = lock_path.parent
    try:
        lock_dir.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise RunLeaseError('cannot create workspace lease directory: ' + str(exc)) from exc
    try:
        directory_info = lock_dir.lstat()
    except OSError as exc:
        raise RunLeaseError('cannot inspect workspace lease directory: ' + str(exc)) from exc
    if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid()
            or stat.S_IMODE(directory_info.st_mode) & 0o077):
        raise RunLeaseError('workspace lease directory is not a private directory owned by this user')

    flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise RunLeaseError('cannot open workspace lease: ' + str(exc)) from exc
    acquired = False
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise RunLeaseError('workspace lease path is not a private regular file owned by this user')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            os.lseek(fd, 0, os.SEEK_SET)
            owner = os.read(fd, 4096).decode('utf-8', 'replace').strip()
            try:
                details = json.loads(owner)
            except (json.JSONDecodeError, TypeError):
                details = {}
            pid = details.get('pid', 'unknown')
            holder_run_dir = details.get('run_dir', 'unknown')
            raise RunLeaseError(
                'another coordinator currently owns this workspace '
                f'(owner pid {pid}, run_dir {holder_run_dir})') from exc
        payload = json.dumps({
            'pid': os.getpid(), 'run_dir': str(Path(run_dir).expanduser().resolve()),
            'workspace': str(root), 'started_at': datetime.now().astimezone().isoformat(),
        })
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload.encode('utf-8'))
        os.fsync(fd)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def git_snapshot(workspace: Path) -> tuple[str, list[list[str]]]:
    """Digest tracked + untracked non-ignored files; symlinks hash their target."""
    proc = subprocess.run(
        ['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=workspace,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode:
        raise RuntimeError('git ls-files failed: ' + proc.stderr.decode(errors='replace').strip())
    manifest: list[list[str]] = []
    names = sorted(x for x in proc.stdout.decode(errors='surrogateescape').split('\0') if x)
    for name in names:
        path = workspace / name
        if path.is_symlink():
            value = 'link:' + os.readlink(path)
        elif path.is_file():
            value = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            value = 'missing'
        manifest.append([name, value])
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return digest, manifest


def directory_digest(root: Path) -> str:
    manifest = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            value = 'link:' + os.readlink(path)
        elif path.is_file():
            value = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            continue
        manifest.append([str(path.relative_to(root)), value])
    return hashlib.sha256(json.dumps(manifest, separators=(',', ':')).encode()).hexdigest()


def read_json_lines(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(errors='replace').splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except json.JSONDecodeError:
            continue
    return rows


def unwrap_json(value: str) -> dict:
    text = value.strip()
    if text.startswith('```'):
        text = '\n'.join(text.splitlines()[1:-1])
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError('structured output is not an object')
    return result


def observed_events(vendor: str, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return actual commands and all observed tool calls from a CLI event stream."""
    commands, calls = [], []
    if vendor == 'codex':
        for row in rows:
            item = row.get('item', {})
            if row.get('type') != 'item.completed':
                continue
            if item.get('type') == 'command_execution':
                raw_command = item.get('command', '')
                exit_code = item.get('exit_code')
                command = raw_command
                try:
                    outer = shlex.split(raw_command)
                    if len(outer) == 3 and outer[0].endswith(('sh', 'bash', 'zsh')) and outer[1] in ('-c', '-lc'):
                        command = outer[2]
                except ValueError:
                    pass
                call = {'tool': 'command_execution', 'input': {'command': command},
                        'error': type(exit_code) is int and exit_code != 0}
                calls.append(call)
                commands.append({'command': command, 'raw_command': raw_command,
                                 'exit_code': exit_code, 'error': type(exit_code) is int and exit_code != 0,
                                 'output': item.get('aggregated_output', ''), 'source': 'command_execution'})
            elif item.get('type') == 'file_change':
                calls.append({'tool': 'file_change', 'input': {'changes': item.get('changes', [])},
                              'error': item.get('status') != 'completed'})
        return commands, calls
    pending = {}
    for row in rows:
        if row.get('type') == 'assistant':
            for block in row.get('message', {}).get('content', []):
                if block.get('type') == 'tool_use':
                    pending[block.get('id')] = {'tool': block.get('name', ''),
                                                'input': block.get('input', {}), 'error': None}
        elif row.get('type') == 'user':
            for block in row.get('message', {}).get('content', []):
                if block.get('type') != 'tool_result' or block.get('tool_use_id') not in pending:
                    continue
                call = pending.pop(block['tool_use_id'])
                call['error'] = bool(block.get('is_error'))
                call['output'] = block.get('content', '') if isinstance(block.get('content', ''), str) else ''
                calls.append(call)
                if call['tool'] == 'Bash':
                    commands.append({'command': call['input'].get('command', ''),
                                     'exit_code': -1 if call['error'] else 0,
                                     'error': call['error'], 'output': call['output'],
                                     'source': 'Bash tool_use/tool_result'})
    calls.extend(pending.values())
    return commands, calls


def command_invokes_test(command: str, configured: str) -> bool:
    """Only the exact configured command counts; whitespace at its edges is irrelevant."""
    return bool(configured.strip()) and command.strip() == configured.strip()


def observed_test_succeeded(row: dict, configured: str) -> bool:
    if row.get('error') or row.get('exit_code') != 0 or not command_invokes_test(row.get('command', ''), configured):
        return False
    output = str(row.get('output', ''))
    failure = re.search(r'(?im)^(FAILED(?:\s|$)|FAILURES!|npm ERR!|not ok(?:\s|$))', output)
    return failure is None


def sensitive_access(calls: list[dict], role: str, evidence: Path, rounds: Path) -> Optional[str]:
    """Reject attempts to read hidden reviewer/evidence transport from a model thread."""
    evidence_text = str(evidence)
    rounds_text = str(rounds)
    for call in calls:
        serialized = json.dumps(call.get('input', {}), ensure_ascii=False)
        if evidence_text in serialized or re.search(r'(?<![A-Za-z])evidence/', serialized):
            return 'evidence directory'
        if rounds_text in serialized:
            return 'review output directory'
        if role in ('author', 'reviewer', 'gate') and re.search(r'rounds/[^"\s]*(?:shadow|permission-probe)-', serialized):
            return 'shadow output'
        if role in ('author', 'reviewer', 'shadow', 'probe') and re.search(r'rounds/[^"\s]*adversarial-', serialized):
            return 'adversarial output'
        for path in rounds.glob('*-shadow-*.md'):
            if str(path) in serialized:
                return 'shadow output'
        for path in rounds.glob('*-adversarial-*.md'):
            if str(path) in serialized:
                return 'adversarial output'
    return None


def verified_claims_schema() -> dict:
    return {'type': 'array', 'items': {'type': 'object', 'properties': {
        'claim': {'type': 'string', 'minLength': 1},
        'file': {'type': 'string', 'minLength': 1},
        'line': {'type': 'integer', 'minimum': 1},
    }, 'required': ['claim', 'file', 'line'], 'additionalProperties': False}}


def verified_claims_error(answer: dict) -> str:
    claims = answer.get('verified_claims')
    if not isinstance(claims, list):
        return 'verified_claims must be an array'
    for claim in claims:
        if (not isinstance(claim, dict) or set(claim) != {'claim', 'file', 'line'} or
                not isinstance(claim['claim'], str) or not claim['claim'].strip() or
                not isinstance(claim['file'], str) or not claim['file'].strip() or
                type(claim['line']) is not int or claim['line'] < 1):
            return 'verified_claims entries require nonempty claim/file and a positive integer line'
    if answer.get('status', answer.get('verdict', '')).lower() == 'approve' and not claims:
        return 'approval requires nonempty verified_claims'
    return ''


def review_schema(statuses=('REVISE', 'APPROVE', 'HOLD'), verified=True) -> dict:
    finding = {'type': 'object', 'properties': {
        'severity': {'type': 'string', 'enum': ['CRITICAL', 'MINOR']},
        'file': {'type': 'string'}, 'summary': {'type': 'string'},
        'failure_scenario': {'type': 'string'},
    }, 'required': ['severity', 'file', 'summary', 'failure_scenario'], 'additionalProperties': False}
    prior = {'type': 'object', 'properties': {
        'id': {'type': 'string'},
        'disposition': {'type': 'string', 'enum': ['fixed', 'still_open', 'withdrawn']},
        'evidence': {'type': 'string'},
    }, 'required': ['id', 'disposition', 'evidence'], 'additionalProperties': False}
    evidence = {'type': 'object', 'properties': {
        'command': {'type': 'string'},
    }, 'required': ['command'], 'additionalProperties': False}
    schema = {'type': 'object', 'properties': {
        'status': {'type': 'string', 'enum': list(statuses)},
        'prior_findings': {'type': 'array', 'items': prior},
        'full_review': {'type': 'array', 'items': finding},
        'self_run_evidence': {'type': 'array', 'items': evidence},
    }, 'required': ['status', 'prior_findings', 'full_review', 'self_run_evidence'],
        'additionalProperties': False}
    if verified:
        schema['properties']['verified_claims'] = verified_claims_schema()
        schema['required'].append('verified_claims')
    return schema


def fresh_review_schema() -> dict:
    """Whole-delta review contract with no persistent-ledger channel."""
    schema = review_schema()
    del schema['properties']['prior_findings']
    schema['required'].remove('prior_findings')
    return schema


def author_schema() -> dict:
    return {'type': 'object', 'properties': {
        'status': {'type': 'string', 'enum': ['READY', 'HOLD']},
        'body': {'type': 'string'},
    }, 'required': ['status', 'body'], 'additionalProperties': False}


def polish_author_schema() -> dict:
    response = {'type': 'object', 'properties': {
        'id': {'type': 'string'},
        'disposition': {'type': 'string', 'enum': ['fixed', 'declined']},
        'reason': {'type': 'string'},
    }, 'required': ['id', 'disposition', 'reason'], 'additionalProperties': False}
    return {'type': 'object', 'properties': {
        'status': {'type': 'string', 'enum': ['READY', 'HOLD']},
        'body': {'type': 'string'},
        'findings': {'type': 'array', 'items': response},
    }, 'required': ['status', 'body', 'findings'], 'additionalProperties': False}


def workitem_reviewer_commands(text: str) -> list[str]:
    commands = []
    for match in re.finditer(r'(?ms)^```reviewer-commands[ \t]*\n(.*?)^```[ \t]*$', text):
        for line in match.group(1).splitlines():
            command = line.strip()
            if command and not command.startswith('#'):
                commands.append(command)
    return commands


def gate_schema() -> dict:
    finding = {'type': 'object', 'properties': {
        'severity': {'type': 'string', 'enum': ['critical', 'high', 'medium', 'low']},
        'file': {'type': 'string'}, 'line_start': {'type': 'integer'},
        'line_end': {'type': 'integer'}, 'confidence': {'type': 'number'},
        'recommendation': {'type': 'string'}, 'body': {'type': 'string'},
    }, 'required': ['severity', 'file', 'line_start', 'line_end', 'confidence', 'recommendation', 'body'],
        'additionalProperties': False}
    evidence = {'type': 'object', 'properties': {
        'command': {'type': 'string'},
    }, 'required': ['command'], 'additionalProperties': False}
    return {'type': 'object', 'properties': {
        'verdict': {'type': 'string', 'enum': ['approve', 'needs-attention']},
        'findings': {'type': 'array', 'items': finding},
        'verified_claims': verified_claims_schema(),
        'self_run_evidence': {'type': 'array', 'items': evidence},
    }, 'required': ['verdict', 'findings', 'self_run_evidence', 'verified_claims'],
        'additionalProperties': False}


def missing_rubric(body: str) -> list[str]:
    missing = []
    for index, label in enumerate(RUBRIC):
        start = body.find(label + ':')
        if start < 0:
            missing.append(label)
            continue
        start += len(label) + 1
        ends = [body.find(other + ':', start) for other in RUBRIC[index + 1:]]
        ends = [end for end in ends if end >= 0]
        value = body[start:min(ends) if ends else len(body)].strip(' \t\r\n.,;:—–-*_')
        if not value:
            missing.append(label)
    return missing


def codex_rollout_usage(session: Optional[str], start: float, end: float) -> list[dict]:
    """Read per-request last_token_usage records for this CLI interval."""
    if not session:
        return []
    paths = list((Path.home() / '.codex' / 'sessions').rglob('*' + session + '*.jsonl'))
    if len(paths) != 1:
        return []
    found, seen = [], set()
    with paths[0].open(errors='replace') as source:
        for line_no, line in enumerate(source, 1):
            try:
                row = json.loads(line)
                payload = row.get('payload', {})
                if row.get('type') != 'event_msg' or payload.get('type') != 'token_count' or not payload.get('info'):
                    continue
                stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp()
                if not start - 1 <= stamp <= end + 1:
                    continue
                info = payload['info']
                signature = json.dumps(info.get('total_token_usage', {}), sort_keys=True)
                if signature in seen:
                    continue
                seen.add(signature)
                use = info['last_token_usage']
                found.append({'input': use.get('input_tokens', 0),
                              'cached': use.get('cached_input_tokens', 0),
                              'output': use.get('output_tokens', 0),
                              'source': str(paths[0]) + ':' + str(line_no)})
            except (ValueError, KeyError):
                continue
    return found


def codex_rollout_attempts(session: Optional[str], start: float, end: float) -> tuple[list[dict], list[dict]]:
    """Recover simple awaited code-mode exec attempts absent from CLI command events.

    Do not evaluate JavaScript or infer success from printed text. Missing exit
    status stays unknown: it cannot satisfy the observed-success test gate.
    """
    if not session:
        return [], []
    paths = list((Path.home() / '.codex' / 'sessions').rglob('*' + session + '*.jsonl'))
    if len(paths) != 1:
        return [], []
    pending, commands, calls = {}, [], []
    with paths[0].open(errors='replace') as source:
        for line_no, line in enumerate(source, 1):
            try:
                row = json.loads(line)
                stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp()
                if not start - 1 <= stamp <= end + 1:
                    continue
                item = row.get('payload', {})
                if row.get('type') != 'response_item':
                    continue
                if item.get('type') == 'custom_tool_call' and item.get('name') in ('exec', 'functions.exec'):
                    code = item.get('input', '')
                    calls.append({'tool': 'code_mode', 'input': {'code': code}, 'error': None})
                    match = re.match(r'\s*const\s+(\w+)\s*=\s*await\s+tools\.exec_command\(', code)
                    if not match:
                        continue
                    args, length = json.JSONDecoder().raw_decode(code[match.end():].lstrip())
                    remaining = code[match.end():].lstrip()[length:]
                    variable = re.escape(match[1])
                    expected = r'\s*\);\s*text\((?:' + variable + r'(?:\.output)?|JSON\.stringify\(' + variable + r'\))\);\s*'
                    if not re.fullmatch(expected, remaining) or not isinstance(args.get('cmd'), str):
                        continue
                    pending[item['call_id']] = (args['cmd'], '.output' not in remaining)
                elif item.get('type') == 'custom_tool_call_output' and item.get('call_id') in pending:
                    command, full_result = pending.pop(item['call_id'])
                    code, output = None, str(item.get('output', ''))
                    if full_result:
                        blocks = item.get('output', [])
                        for block in blocks if isinstance(blocks, list) else [{'text': blocks}]:
                            try:
                                result = json.loads(block.get('text', ''))
                                if type(result.get('exit_code')) is int and isinstance(result.get('output'), str):
                                    code, output = result['exit_code'], result['output']
                            except (ValueError, AttributeError):
                                continue
                    commands.append({'command': command, 'exit_code': code,
                                     'error': code is not None and code != 0, 'output': output,
                                     'source': str(paths[0]) + ':' + str(line_no),
                                     'evidence_kind': 'structured exec result' if code is not None else 'attempt-only; exit status unavailable'})
            except (ValueError, KeyError, AttributeError):
                continue
    return commands, calls


def compact_command(command: dict, evidence_file: str) -> dict:
    """Keep round files useful while the complete command event stays in evidence."""
    output = str(command.get('output', ''))
    lines = output.splitlines()
    tail = '\n'.join(lines[-OUTPUT_TAIL_LINES:])
    truncated = len(lines) > OUTPUT_TAIL_LINES or len(tail) > OUTPUT_TAIL_CHARS
    if len(tail) > OUTPUT_TAIL_CHARS:
        tail = tail[-OUTPUT_TAIL_CHARS:]
    row = {'command': command.get('command', ''),
           'status': 'error' if command.get('error') or command.get('exit_code') not in (0, None) else 'ok',
           'output_tail': tail}
    if truncated:
        row['note'] = f'(truncated, full output in evidence/{evidence_file})'
    return row


def cleanup_probe_targets(paths: list[Path]) -> tuple[list[str], list[str], list[str], list[dict]]:
    """Attempt probe cleanup and report only targets confirmed absent afterward."""
    found = [str(path) for path in paths if path.exists()]
    errors = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append({'path': str(path), 'error': type(exc).__name__})
    cleaned = [str(path) for path in paths if str(path) in found and not path.exists()]
    remaining = [str(path) for path in paths if path.exists()]
    return found, cleaned, remaining, errors


def render_markdown(actor: str, phase: str, payload: dict, snapshot: str,
                    evidence_file: str = 'receipt.json') -> str:
    status = payload.get('status', payload.get('verdict', 'message'))
    lines = [f'# {actor}: {status}', '', f'- Phase: {phase}', f'- Snapshot: `{snapshot}`', '']
    if 'body' in payload:
        lines += ['## Body', '', payload['body'], '']
    for key in ('verified_claims', 'prior_findings', 'full_review', 'findings', 'observed_commands',
                'self_run_evidence', 'discarded_blocking'):
        if key in payload:
            title = 'Claimed Self Run Evidence' if key == 'self_run_evidence' else key.replace('_', ' ').title()
            value = payload[key]
            if key == 'observed_commands':
                value = [compact_command(command, evidence_file) for command in value]
            lines += [f'## {title}', '', '```json',
                      json.dumps(value, indent=2, ensure_ascii=False), '```', '']
    return '\n'.join(lines)


class Coordinator:
    def __init__(self, args: argparse.Namespace):
        resolve_role_model_defaults(args)
        validate_role_models(args)
        self.args = args
        self.workspace = Path(args.workspace).expanduser().resolve()
        self.workitem = Path(args.workitem).expanduser().resolve()
        self.run_dir = Path(args.run_dir).expanduser().resolve()
        self.author_temp_dir = self.run_dir / 'author-tmp'
        self._probe_sandbox_commands = None
        self.rounds = self.run_dir / 'rounds'
        self.evidence = self.run_dir / 'evidence'
        self.context = self.run_dir / 'context'
        self.internal = self.run_dir / 'internal'
        self.state_path = self.run_dir / 'state.json'
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if 'reason' in self.state and 'hold_reason' not in self.state:
                self.state['hold_reason'] = self.state.pop('reason')
                self.save()
            self._validate_resume_args()
            self.state.setdefault('base_commit', self._head_commit())
            self.state.setdefault('reviews_completed', 0)
            ledger_path = self.run_dir / 'findings-ledger.json'
            self.state.setdefault('finding_ledger', json.loads(ledger_path.read_text())
                                  if ledger_path.exists() else [])
            highest_id = max((int(row['id'][1:]) for row in self.state['finding_ledger']
                              if re.fullmatch(r'F\d+', row.get('id', ''))), default=0)
            self.state.setdefault('next_finding_id', highest_id + 1)
            self.state.setdefault('exec_comparisons', [])
            self.state.setdefault('polish', {'active': False, 'completed': False,
                                             'author_turns': 0, 'reviewer_turns': 0,
                                             'fix_used': False})
            if self.state.get('invocation_budget_version', 0) < 1:
                self.state['invocations_used'] = self._migrate_invocation_budget()
                self.state['invocation_budget_version'] = 1
                self.save()
        else:
            if not (self.workspace / '.git').exists():
                raise ValueError('--workspace must be a git worktree')
            if not self.workitem.is_file():
                raise ValueError('--workitem must be a file')
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.rounds.mkdir(exist_ok=True)
            self.evidence.mkdir(exist_ok=True)
            self.context.mkdir(exist_ok=True)
            self.internal.mkdir(exist_ok=True)
            atomic_text(self.context / 'workitem.md', self.workitem.read_text())
            self.state = {
                'version': 1, 'status': 'ACTIVE', 'phase': 'PLAN', 'next': 'author',
                'workspace': str(self.workspace), 'workitem': str(self.workitem),
                'config': self._config(),
                'sessions': {
                    'author': str(uuid.uuid4()) if args.author_vendor == 'claude' else None,
                    'reviewer': str(uuid.uuid4()) if args.reviewer_vendor == 'claude' else None,
                },
                'started': {'author': False, 'reviewer': False}, 'plan_rounds': 0,
                'exec_rounds': 0, 'plan_reviews': 0, 'exec_reviews': 0,
                'review_findings': [], 'gate_ran': False,
                'sequence': 0, 'invocations_used': 0, 'invocation_budget_version': 1,
                'turns': [], 'active': None,
                'started_at': time.time(),
                'last_end': {}, 'waiting_model_calls': 0, 'base_commit': self._head_commit(),
                'reviews_completed': 0,
                'finding_ledger': [], 'next_finding_id': 1, 'exec_comparisons': [],
                'polish': {'active': False, 'completed': False, 'author_turns': 0,
                           'reviewer_turns': 0, 'fix_used': False},
            }
            self.save()

    def _migrate_invocation_budget(self) -> int:
        """Conservatively migrate old turn logs, exempting only explicit rate-limit rejections."""
        turns = self.state.get('turns', [])
        counted_sequences = set()
        used = 1 if (self.state.get('active') or self.state.get('uncertain_active')) else 0
        for receipt in (self.state.get('active'), self.state.get('uncertain_active')):
            if receipt and type(receipt.get('sequence')) is int:
                counted_sequences.add(receipt['sequence'])
        for turn in turns:
            returncode = turn.get('returncode')
            if type(returncode) is int and returncode != 0:
                sequence = turn.get('sequence')
                phase = str(turn.get('phase', '')).lower()
                role = turn.get('role', '')
                if type(sequence) is not int or not role:
                    used += 1
                    continue
                prefix = self.evidence / f'{sequence:03d}-{phase}-{role}'
                try:
                    stderr = read_text_tail(prefix.with_suffix('.stderr.log'))
                except OSError:
                    stderr = ''
                try:
                    stdout = read_text_tail(prefix.with_suffix('.stdout.jsonl'))
                except OSError:
                    stdout = ''
                classified = None
                if returncode > 0 and not turn.get('timed_out', False):
                    classified = classify_rate_limit_failure(returncode, stderr, stdout)
                if classified:
                    turn['error_kind'] = classified['kind']
                    turn['reset_hint'] = classified['reset_hint']
                    turn['invocation_budget_counted'] = False
                    continue
            turn['invocation_budget_counted'] = True
            used += 1
            if type(turn.get('sequence')) is int:
                counted_sequences.add(turn['sequence'])
        for receipt in [*self.state.get('abandoned_turns', []), *self.state.get('spawn_failures', [])]:
            sequence = receipt.get('sequence')
            if (receipt.get('invocation_budget_counted', True) and
                    (type(sequence) is not int or sequence not in counted_sequences)):
                used += 1
                if type(sequence) is int:
                    counted_sequences.add(sequence)
        return used

    def _config(self) -> dict:
        keys = ('author_vendor', 'author_model', 'author_effort', 'reviewer_vendor',
                'reviewer_model', 'reviewer_effort', 'shadow', 'adversarial_gate',
                'gate_model', 'gate_effort', 'max_plan_rounds', 'max_exec_rounds',
                'timeout', 'max_invocations', 'exercise_revisions', 'test_command',
                'gate_prompt', 'reviewer_command', 'polish_round', 'codex_bin', 'claude_bin',
                'author_subagents')
        config = {key: getattr(self.args, key) for key in keys}
        gate_prompt = Path(self.args.gate_prompt).expanduser()
        if not gate_prompt.is_absolute():
            gate_prompt = self.workspace / gate_prompt
        gate_prompt = gate_prompt.resolve()
        config['gate_prompt'] = (
            '<bundled-default>:' + hashlib.sha256(gate_prompt.read_bytes()).hexdigest()
            if gate_prompt == DEFAULT_GATE_PROMPT.resolve() else str(gate_prompt))
        config['workitem_reviewer_commands'] = workitem_reviewer_commands(self.workitem.read_text())
        return config

    def reviewer_commands(self) -> list[str]:
        result = []
        for command in [self.args.test_command, *self.args.reviewer_command,
                        *workitem_reviewer_commands(self.workitem.read_text())]:
            if command not in result:
                result.append(command)
        return result

    def reviewer_flags(self) -> dict:
        """Permission surface whose probe PASS is valid for a later run."""
        flags = {
            'surface_version': PROBE_SURFACE_VERSION,
            'reviewer_vendor': self.args.reviewer_vendor,
            'reviewer_model': self.args.reviewer_model,
            'reviewer_effort': self.args.reviewer_effort,
            'reviewer_commands': self.reviewer_commands(),
            'reviewer_binary': self.args.claude_bin if self.args.reviewer_vendor == 'claude' else self.args.codex_bin,
            'permission_mode': 'dontAsk' if self.args.reviewer_vendor == 'claude' else 'never',
            'sandbox': 'restricted-allowlist' if self.args.reviewer_vendor == 'claude' else 'read-only',
        }
        if self.args.reviewer_vendor == 'claude':
            flags['claude_bash_sandbox'] = self._claude_sandbox_settings('probe')
        if self.args.reviewer_vendor == 'codex':
            flags['ignore_execpolicy_rules'] = True
        return flags

    def author_flags(self) -> dict:
        """Permission surface required by an author permission-probe PASS."""
        flags = {
            'surface_version': PROBE_SURFACE_VERSION,
                'author_vendor': self.args.author_vendor,
                'author_model': self.args.author_model,
                'author_effort': self.args.author_effort,
                'author_binary': self.args.claude_bin if self.args.author_vendor == 'claude' else self.args.codex_bin,
        }
        if self.args.author_vendor == 'codex':
            flags.update({
                'ignore_execpolicy_rules': True,
                'sandbox': 'workspace-write',
                'sandbox_workspace_write.writable_roots': [str(self.author_temp_dir)],
                'sandbox_workspace_write.exclude_tmpdir_env_var': False,
                'sandbox_workspace_write.exclude_slash_tmp': True,
                'TMPDIR': str(self.author_temp_dir),
            })
        else:
            flags.update({'permission_mode': 'acceptEdits',
                          'claude_bash_sandbox': self._claude_sandbox_settings('author'),
                          'non_bash_run_state_edit_access': 'denied by Edit/Write path rules'})
        return flags

    def reviewer_flags_digest(self) -> str:
        raw = json.dumps(self.reviewer_flags(), sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(raw).hexdigest()

    def _claude_sandbox_settings(self, role: str) -> dict:
        """Strict OS boundary for Claude Bash, independent of Claude tool permissions."""
        secret_names = {
            name for name in os.environ
            if re.search(r'(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL|AUTH|'
                         r'(?:^|_)(?:KEY|PASS|PWD)(?:_|$))', name, re.I)
        }
        secret_names.update({
            'ANTHROPIC_API_KEY', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
            'AWS_SESSION_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN', 'GITLAB_TOKEN',
            'NPM_TOKEN', 'PYPI_TOKEN', 'DOCKER_PASSWORD', 'SSH_AUTH_SOCK',
        })
        credential_paths = (
            '~/.aws', '~/.ssh', '~/.config/gcloud', '~/.config/gh',
            '~/.docker/config.json', '~/.git-credentials', '~/.netrc', '~/.npmrc', '~/.pypirc',
            '~/.kube/config', '~/.azure',
        )
        return {
            'disableAllHooks': True,
            'sandbox': {
                'enabled': True,
                'failIfUnavailable': True,
                'allowUnsandboxedCommands': False,
                'autoAllowBashIfSandboxed': False,
                'excludedCommands': [],
                'network': {'strictAllowlist': True, 'allowedDomains': [], 'allowLocalBinding': False},
                'credentials': {
                    'envVars': [{'name': name, 'mode': 'deny'} for name in sorted(secret_names)],
                    'files': [{'path': path, 'mode': 'deny'} for path in credential_paths],
                },
                'filesystem': {
                    'denyWrite': [str(self.run_dir)],
                },
            },
            'permissions': {
                'deny': [f'Edit(//{self.run_dir.as_posix().lstrip("/")}/**)'],
            },
        }

    def author_flags_digest(self) -> str:
        raw = json.dumps(self.author_flags(), sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(raw).hexdigest()

    def probe_passed(self) -> tuple[bool, str]:
        path = self.run_dir / 'permission-probe.json'
        if not path.exists():
            return False, 'permission-probe.json is missing'
        try:
            report = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return False, 'permission-probe.json is unreadable'
        if report.get('status') != 'PASS':
            return False, 'permission probe status is not PASS'
        if report.get('reviewer_flags_digest') != self.reviewer_flags_digest():
            return False, 'permission probe reviewer flags do not match this run'
        if report.get('author_flags_digest') != self.author_flags_digest():
            return False, 'permission probe author flags do not match this run'
        author_probe = report.get('author_permission_probe', {})
        expected_author_status = 'PASS' if self.args.author_vendor == 'codex' else 'NOT-APPLICABLE'
        if author_probe.get('status') != expected_author_status:
            return False, 'permission probe author permission status is not current'
        return True, ''

    def _validate_resume_args(self) -> None:
        if Path(self.state['workspace']) != self.workspace or Path(self.state['workitem']) != self.workitem:
            raise ValueError('resume workspace/workitem differs from state')
        current_config = self._config()
        for key, value in self.state['config'].items():
            current = current_config[key]
            if current != value:
                raise ValueError('resume configuration differs: ' + key)

    def save(self) -> None:
        atomic_json(self.state_path, self.state)

    def _head_commit(self) -> Optional[str]:
        proc = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], cwd=self.workspace,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def _git(self, args: list[str], ok=(0,)) -> str:
        proc = subprocess.run(['git', *args], cwd=self.workspace, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, errors='replace')
        if proc.returncode not in ok:
            raise RuntimeError('git ' + ' '.join(args) + ' failed: ' + proc.stderr.strip())
        return proc.stdout

    def _workspace_names(self) -> list[str]:
        raw = subprocess.run(['git', 'ls-files', '-co', '--exclude-standard', '-z'],
                             cwd=self.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             check=True).stdout
        return sorted(name for name in raw.decode(errors='surrogateescape').split('\0') if name)

    def _mirror_workspace(self, target: Path) -> None:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for name in self._workspace_names():
            source, destination = self.workspace / name, target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                destination.symlink_to(os.readlink(source))
            elif source.is_file():
                shutil.copy2(source, destination)

    def materialize_review_context(self) -> None:
        """Create program-owned, read-only review views without reviewer Git Bash access."""
        base = self.state.get('base_commit')
        tracked = self._git(['diff', '--binary', base, '--'] if base else ['diff', '--binary', '--'])
        stat = self._git(['diff', '--stat', base, '--'] if base else ['diff', '--stat', '--'])
        untracked = self._git(['ls-files', '--others', '--exclude-standard']).splitlines()
        additions = []
        for name in untracked:
            additions.append(self._git(['diff', '--no-index', '--binary', '--', '/dev/null', name], ok=(0, 1)))
        delta = tracked + ''.join(additions)
        untracked_stat = ''.join(f'UNTRACKED {name} ({(self.workspace / name).lstat().st_size} bytes)\n'
                                 for name in untracked)
        atomic_text(self.context / 'delta.patch', delta)
        atomic_text(self.context / 'delta.stat', stat + untracked_stat)
        atomic_text(self.context / 'status.txt', self._git(['status', '--short', '--untracked-files=all']))
        since = self.context / 'delta-since-last-review.patch'
        baseline = self.internal / 'last-review'
        if self.state.get('reviews_completed', 0) and baseline.exists():
            current = self.internal / 'current-review'
            self._mirror_workspace(current)
            proc = subprocess.run(['git', 'diff', '--no-index', '--binary', '--', str(baseline), str(current)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')
            if proc.returncode not in (0, 1):
                raise RuntimeError('git diff --no-index review mirrors failed: ' + proc.stderr.strip())
            atomic_text(since, proc.stdout)
            shutil.rmtree(current)
        elif since.exists():
            since.unlink()

    def capture_review_baseline(self) -> None:
        self._mirror_workspace(self.internal / 'last-review')
        self.state['reviews_completed'] = self.state.get('reviews_completed', 0) + 1
        self.save()

    def open_findings(self) -> list[dict]:
        return [finding for finding in self.state['finding_ledger'] if finding['status'] == 'open']

    def blocking_open_findings(self) -> list[dict]:
        return [finding for finding in self.open_findings()
                if finding['severity'] == 'CRITICAL' or
                (finding['source'] == 'adversarial-gate' and finding['severity'] in ('CRITICAL', 'HIGH'))]

    @staticmethod
    def normalize_plan_findings(findings: list[dict]) -> tuple[list[dict], int]:
        """Record confused implementation findings without letting them block PLAN."""
        normalized, out_of_phase = [], 0
        for original in findings:
            finding = dict(original)
            location = str(finding.get('file', '')).strip().lower()
            is_plan = (location in ('plan', 'plan.md') or
                       bool(re.search(r'(^|[/\\])plan\.md(?:$|[\s(:])', location)))
            if not is_plan:
                out_of_phase += 1
                finding['severity'] = 'MINOR'
                if not finding.get('summary', '').startswith('[out-of-phase]'):
                    finding['summary'] = '[out-of-phase] ' + finding.get('summary', '')
            normalized.append(finding)
        return normalized, out_of_phase

    def record_findings(self, source: str, phase: str, origin_round: int,
                        findings: list[dict]) -> list[dict]:
        recorded = []
        for finding in findings:
            finding_id = f"F{self.state['next_finding_id']:03d}"
            self.state['next_finding_id'] += 1
            severity = str(finding['severity']).upper()
            summary = finding.get('summary') or finding.get('recommendation') or str(finding.get('body', '')).splitlines()[0]
            entry = {'id': finding_id, 'origin_round': origin_round, 'phase': phase,
                     'source': source, 'severity': severity, 'file': finding.get('file', ''),
                     'summary': summary, 'status': 'open',
                     'failure_scenario': finding.get('failure_scenario', ''),
                     'body': finding.get('body', ''),
                     'status_history': [{'round': origin_round, 'status': 'open',
                                        'evidence': 'program assigned identity'}]}
            self.state['finding_ledger'].append(entry)
            recorded.append({'id': finding_id, **finding})
        self.write_ledger()
        return recorded

    def apply_dispositions(self, dispositions: list[dict], origin_round: int) -> list[str]:
        open_by_id = {finding['id']: finding for finding in self.open_findings()}
        supplied = [row.get('id') for row in dispositions]
        missing = sorted(set(open_by_id) - set(supplied))
        if len(supplied) != len(set(supplied)) or set(supplied) - set(open_by_id):
            raise RuntimeError('invalid finding dispositions: duplicate or unknown id')
        if missing:
            return missing
        for row in dispositions:
            finding = open_by_id[row['id']]
            disposition = row['disposition']
            finding['status_history'].append({'round': origin_round, 'status': disposition,
                                              'evidence': row['evidence']})
            if disposition in ('fixed', 'withdrawn'):
                finding['status'] = disposition
        self.write_ledger()
        return []

    def write_ledger(self) -> None:
        atomic_json(self.run_dir / 'findings-ledger.json', self.state['finding_ledger'])
        lines = ['# Findings ledger', '',
                 '| ID | Origin | Phase | Source | Severity | File | Summary | Status | History |',
                 '|---|---:|---|---|---|---|---|---|---|']
        for finding in self.state['finding_ledger']:
            history = '; '.join(f"r{x['round']} {x['status']}: {x['evidence']}"
                                for x in finding['status_history'])
            cells = [finding['id'], str(finding['origin_round']), finding['phase'], finding['source'],
                     finding['severity'], finding['file'], finding['summary'], finding['status'], history]
            lines.append('| ' + ' | '.join(str(cell).replace('|', '\\|').replace('\n', ' ') for cell in cells) + ' |')
        atomic_text(self.run_dir / 'findings-ledger.md', '\n'.join(lines) + '\n')

    def nonblocking_open_findings(self) -> list[dict]:
        return [row for row in self.open_findings()
                if row['severity'] == 'MINOR' or
                (row['source'] == 'adversarial-gate' and row['severity'] in ('MEDIUM', 'LOW'))]

    @staticmethod
    def advisory_rows(findings: list[dict]) -> list[dict]:
        keys = ('id', 'severity', 'file', 'summary', 'failure_scenario', 'body')
        return [{key: row.get(key, '') for key in keys if row.get(key, '')} for row in findings]

    def advisory_message(self, findings: list[dict], source='approved-advisory') -> str:
        return json.dumps({'source': source, 'status': 'ADVISORY', 'blocking': False,
                           'instruction': 'These findings are explicitly non-blocking.',
                           'findings': self.advisory_rows(findings)}, ensure_ascii=False)

    def write_open_findings(self) -> None:
        rows = [row for row in self.state['finding_ledger']
                if row['status'] == 'open' or row.get('author_disposition') == 'declined']
        lines = ['# Open findings', '',
                 '| ID | Source | Severity | File | Summary | Author response |',
                 '|---|---|---|---|---|---|']
        for row in rows:
            response = row.get('author_disposition', '')
            if row.get('author_reason'):
                response += (': ' if response else '') + row['author_reason']
            cells = [row['id'], row['source'], row['severity'], row['file'], row['summary'], response]
            lines.append('| ' + ' | '.join(str(cell).replace('|', '\\|').replace('\n', ' ')
                                           for cell in cells) + ' |')
        if not rows:
            lines += ['', 'No open or declined findings.']
        atomic_text(self.run_dir / 'open-findings.md', '\n'.join(lines) + '\n')

    @staticmethod
    def comparison_findings(answer: dict) -> list[dict]:
        rows = answer.get('full_review', answer.get('findings', []))
        return [{'severity': str(row.get('severity', '')).upper(), 'file': row.get('file', ''),
                 'summary': row.get('summary') or row.get('recommendation') or
                            str(row.get('body', '')).splitlines()[0]} for row in rows]

    def write_comparison(self) -> None:
        lines = ['# Review comparison', '']
        total = {'persistent': 0, 'shadow': 0, 'gate': 0}
        for comparison in self.state['exec_comparisons']:
            lines += [f"## EXEC round {comparison['round']}", '',
                      '| Role | Verdict | Severity | File | Summary |',
                      '|---|---|---|---|---|']
            for role in ('persistent', 'shadow', 'gate'):
                result = comparison.get(role)
                if not result:
                    lines.append(f'| {role} | not run |  |  |  |')
                    continue
                findings = result['findings'] or [{}]
                total[role] += len(result['findings'])
                for finding in findings:
                    cells = [role, result['verdict'], finding.get('severity', ''),
                             finding.get('file', ''), finding.get('summary', '')]
                    lines.append('| ' + ' | '.join(str(cell).replace('|', '\\|').replace('\n', ' ')
                                                   for cell in cells) + ' |')
            if comparison.get('effective_verdict'):
                lines += ['', f"Effective workflow verdict: **{comparison['effective_verdict']}**"]
            lines.append('')
        lines += ['## Counts', '', '| Role | Findings |', '|---|---:|',
                  *(f'| {role} | {count} |' for role, count in total.items())]
        atomic_text(self.run_dir / 'review-comparison.md', '\n'.join(lines) + '\n')

    def hold(self, reason: str) -> str:
        self.set_effective_verdict('HOLD')
        if self.state.get('active'):
            self.state['uncertain_active'] = self.state['active']
        self.state['status'] = 'HOLD'
        self.state['hold_reason'] = reason
        self.state['active'] = None
        self.save()
        self.write_ledger()
        self.write_comparison()
        self.write_open_findings()
        self.write_usage()
        return 'HOLD'

    def archive_abandoned_turn(self, receipt: dict) -> None:
        sequence = receipt.get('sequence')
        rows = self.state.setdefault('abandoned_turns', [])
        if sequence is not None and any(row.get('sequence') == sequence for row in rows):
            return
        row = {**receipt, 'recovered_at': time.time(), 'group_gone': True,
               'provider_usage': 'unknown'}
        rows.append(row)
        self.state.setdefault('usage_reconciliation', []).append({
            'sequence': sequence, 'role': receipt.get('role'), 'phase': receipt.get('phase'),
            'source': 'abandoned_turn', 'provider_usage': 'unknown',
            'invocation_budget_counted': bool(receipt.get('invocation_budget_counted', True)),
        })
        if sequence is not None:
            path = self.evidence / f'{sequence:03d}-abandoned.receipt.json'
            if not path.exists():
                atomic_json(path, row)

    def done(self) -> str:
        blocking = self.blocking_open_findings()
        if blocking:
            return self.hold('DONE refused with open blocking findings: ' +
                             ', '.join(row['id'] for row in blocking))
        self.set_effective_verdict('APPROVE')
        self.state['status'] = 'DONE'
        self.state['completed_at'] = time.time()
        self.save()
        self.write_ledger()
        self.write_comparison()
        self.write_open_findings()
        self.write_usage()
        return 'DONE'

    def set_effective_verdict(self, verdict: str) -> None:
        if self.state.get('exec_comparisons'):
            self.state['exec_comparisons'][-1]['effective_verdict'] = verdict

    def start_polish_or_done(self, force=False) -> str:
        findings = self.nonblocking_open_findings()
        if (self.args.polish_round == 'off' and not force) or not findings:
            return self.done()
        polish = self.state['polish']
        if polish.get('completed'):
            return self.done()
        polish.update({'active': True, 'completed': False, 'author_turns': 0,
                       'reviewer_turns': 0, 'fix_used': False,
                       'finding_ids': [row['id'] for row in findings]})
        self.state['delivered_review'] = self.advisory_message(findings, 'polish-round')
        self.state['next'] = 'author'
        self.state['status'] = 'ACTIVE'
        self.state.pop('hold_reason', None)
        self.save()
        return 'ACTIVE'

    def import_gate_findings(self) -> None:
        existing = {(row['source'], row['file'], row['summary']) for row in self.state['finding_ledger']}
        for path in sorted(self.evidence.glob('*-gate.receipt.json')):
            receipt = json.loads(path.read_text())
            for finding in receipt.get('answer', {}).get('findings', []):
                if str(finding.get('severity', '')).lower() not in ('medium', 'low'):
                    continue
                summary = finding.get('recommendation') or str(finding.get('body', '')).splitlines()[0]
                signature = ('adversarial-gate', finding.get('file', ''), summary)
                if signature not in existing:
                    self.record_findings('adversarial-gate', 'EXEC', receipt.get('sequence', 0), [finding])
                    existing.add(signature)
                else:
                    row = next(item for item in self.state['finding_ledger']
                               if (item['source'], item['file'], item['summary']) == signature)
                    if not row.get('body'):
                        row['body'] = finding.get('body', '')
        self.write_ledger()

    def resume_polish(self) -> str:
        if self.state.get('active'):
            return self.hold('uncertain in-flight CLI turn; inspect evidence before resume --polish')
        if self.state['status'] != 'DONE':
            return self.hold('resume --polish requires an older run in DONE state')
        blocking = self.blocking_open_findings()
        if blocking:
            return self.hold('resume --polish rejected with open blocking findings: ' +
                             ', '.join(row['id'] for row in blocking))
        self.import_gate_findings()
        self.state['status'] = 'ACTIVE'
        self.state['phase'] = 'EXEC'
        self.state['active'] = None
        self.state['uncertain_active'] = None
        self.state.pop('hold_reason', None)
        self.start_polish_or_done(force=True)
        return self.drive() if self.state['status'] == 'ACTIVE' else self.state['status']

    def _role_vendor(self, role: str) -> str:
        if role == 'author':
            return self.args.author_vendor
        if role in ('reviewer', 'shadow', 'probe'):
            return self.args.reviewer_vendor
        return 'claude' if self.args.author_vendor == 'codex' else 'codex'

    def _rotate_failed_first_claude_session(self, role: str, vendor: str, fresh: bool) -> None:
        """Do not reuse a Claude session id after an unaccepted first turn."""
        if (not fresh and vendor == 'claude' and role in self.state.get('started', {})
                and not self.state['started'][role]):
            self.state['sessions'][role] = str(uuid.uuid4())

    def _model_effort(self, role: str) -> tuple[str, str]:
        if role == 'author':
            return self.args.author_model, self.args.author_effort
        if role in ('reviewer', 'shadow', 'probe'):
            return self.args.reviewer_model, self.args.reviewer_effort
        return self.args.gate_model, self.args.gate_effort

    def _claude_command(self, role: str, schema_path: Path, fresh: bool) -> list[str]:
        model, effort = self._model_effort(role)
        cmd = [self.args.claude_bin, '-p', '--model', model, '--effort', effort,
               '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
               '--permission-prompts', 'none', '--disable-slash-commands',
               '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
               '--setting-sources', '', '--settings', json.dumps(
                   self._claude_sandbox_settings(role), separators=(',', ':')), '--no-chrome',
               '--add-dir', str(self.context),
               '--json-schema', json.dumps(json.loads(schema_path.read_text()), separators=(',', ':'))]
        if role == 'author':
            # The Agent deny came from the S1 read-only lockdown; it has no purpose for a role
            # that may already edit and run Bash. Read-only roles keep it denied.
            subagents = self.args.author_subagents == 'on'
            tools = 'Read,Grep,Glob,Bash,Edit,Write' + (',Agent' if subagents else '')
            cmd += ['--permission-mode', 'acceptEdits', '--tools', tools, '--allowedTools', tools,
                    '--disallowedTools', 'NotebookEdit' if subagents else 'NotebookEdit,Agent']
        else:
            exact_commands = self.reviewer_commands()
            if role == 'probe' and self._probe_sandbox_commands:
                exact_commands = [*exact_commands, *self._probe_sandbox_commands]
            allowed = ['Read', 'Grep', 'Glob', *(f'Bash({command})' for command in exact_commands)]
            cmd += ['--restricted', '--permission-mode', 'dontAsk',
                    '--tools', 'Read,Grep,Glob,Bash', '--allowedTools', ','.join(allowed),
                    '--disallowedTools', 'Edit,Write,NotebookEdit,Agent']
        if fresh:
            cmd += ['--no-session-persistence']
        elif self.state['started'][role]:
            cmd += ['--resume', self.state['sessions'][role]]
        else:
            cmd += ['--session-id', self.state['sessions'][role]]
        return cmd

    def _codex_command(self, role: str, schema_path: Path, fresh: bool) -> list[str]:
        model, effort = self._model_effort(role)
        sandbox = 'workspace-write' if role == 'author' else 'read-only'
        cmd = [self.args.codex_bin, 'exec', '-m', model, '--json', '--output-schema', str(schema_path),
               '-c', f'model_reasoning_effort="{effort}"', '-c', f'sandbox_mode="{sandbox}"',
               '-c', 'approval_policy="never"', '-c', 'features.hooks=false']
        # Personal allow rules can bypass either sandbox, including an author's
        # worktree boundary. Every Codex role must use only this invocation's policy.
        cmd.append('--ignore-rules')
        if role == 'author':
            cmd += ['-c', 'sandbox_workspace_write.writable_roots=' +
                    json.dumps([str(self.author_temp_dir)]),
                    '-c', 'sandbox_workspace_write.exclude_tmpdir_env_var=false',
                    '-c', 'sandbox_workspace_write.exclude_slash_tmp=true']
        if not fresh and self.state['started'][role]:
            cmd += ['resume', self.state['sessions'][role]]
        return cmd + ['-']

    def command(self, role: str, schema_path: Path, fresh: bool) -> list[str]:
        vendor = self._role_vendor(role)
        return (self._claude_command(role, schema_path, fresh) if vendor == 'claude'
                else self._codex_command(role, schema_path, fresh))

    def _author_prompt(self) -> str:
        contract = self.author_control_contract()
        phase = self.state['phase']
        if self.state['polish']['active']:
            fixing = self.state['polish']['fix_used'] and self.state['polish']['author_turns'] > 0
            task = ('Fix the delivered CRITICAL polish regression and rerun relevant checks.' if fixing else
                    'Polish round: address what is cheap and correct; for every delivered id answer '
                    'fixed or declined with a one-line reason.')
            prior = self.state.get('delivered_review', '')
            return '\n'.join([
                f'Role: persistent {self.args.author_vendor} implementer. Phase: POLISH.',
                f'Workspace: {self.workspace}', f'Work item: {self.context / "workitem.md"}', task,
                contract,
                'Delivered advisory findings:\n' + prior,
                'Do not commit or push. Do not load review-loop skills. Do not edit outside the workspace.',
                'Return only JSON matching the supplied schema.',
            ])
        first = self.state[f'{phase.lower()}_rounds'] == 0
        if phase == 'PLAN':
            task = ('Write a concrete implementation and verification plan as body. Do not edit the workspace.' if first
                    else 'Revise the plan to address the reviewer findings below. Return the complete replacement plan as body.')
            if self.args.exercise_revisions and first:
                task += ' Exercise rule: omit a Verification section on this first draft only.'
            prior = self.state.get('delivered_review', '')
            return '\n'.join([
                f'Role: persistent {self.args.author_vendor} plan author. Phase: PLAN.',
                f'Workspace: {self.workspace}', f'Work item: {self.context / "workitem.md"}', task,
                contract,
                'PLAN only: return a plan; do not implement, edit the workspace, or run implementation checks.',
                'The body must be the current plan only. Integrate corrections into its sections; do not include reviewer ids, verdict history, or response-to-reviewer narratives.',
                ('Delivered plan review:\n' + prior) if prior else 'No delivered plan review on this turn.',
                'Do not commit or push. Do not load review-loop skills.',
                'Return only JSON matching the supplied schema. READY means the plan turn is complete; HOLD means blocked.',
            ])
        task = ('Implement the approved plan now and run relevant checks. Summarize changes and checks in body.' if first
                else 'Fix every delivered blocking finding, rerun relevant checks, and summarize the result in body.')
        if self.args.exercise_revisions and first:
            task += ' Exercise rule: intentionally omit bool rejection required by the toy work item on this first implementation only.'
        prior = self.state.get('delivered_review', '')
        return '\n'.join([
            f'Role: persistent {self.args.author_vendor} implementer. Phase: EXEC.',
            f'Workspace: {self.workspace}', f'Work item: {self.context / "workitem.md"}', task,
                contract,
                ('Delivered review:\n' + prior) if prior else 'No delivered review on this turn.',
            'Do not commit or push. Do not load review-loop skills. Do not edit outside the workspace.',
            'For long commands, use the longest single wait your tool permits. Do not wait for another model.',
            'Return only JSON matching the supplied schema. READY means this turn is complete; HOLD means blocked.',
        ])

    def author_control_contract(self) -> str:
        if self.state['phase'] == 'PLAN':
            verification = ('Include this exact verification command in the plan; do not run it during PLAN: '
                            + self.args.test_command)
        else:
            verification = ('Configured verification command (run exactly as written): '
                            + self.args.test_command)
        return '\n'.join([
            verification,
            'After READY, the coordinator dispatches the independent reviewer; do not invoke or wait for another model yourself.',
            'Use HOLD only when a required product/authorization decision is missing or an unrecoverable environment precondition prevents progress.',
            'Do not HOLD to request review, avoid verification, or report ordinary uncertainty; return READY with the work and explain unresolved risk in body.',
        ])

    def allowed_command_prompt(self) -> str:
        commands = self.reviewer_commands()
        return '\n'.join(['Commands you may run exactly as written (each must be an unwrapped Bash call):',
                          *(f'- {command}' for command in commands)])

    def verified_claims_prompt(self) -> str:
        return ('Return verified_claims as an array of {claim, file, line}: concrete claims you '
                'checked yourself, with the source file and positive 1-based line. '
                'An approval requires at least one verified claim. Do not merely repeat the plan '
                'or infer verification from a passing test count.')

    def inspection_prompt(self, role: str) -> str:
        if self._role_vendor(role) == 'codex':
            return ('For file inspection, use read-only shell reads (cat, sed, or rg) of the named '
                    'context files and workspace source. These reads are permitted in addition to '
                    'the exact test commands below. Do not modify files or use Git through the shell.')
        return 'Use Read/Grep/Glob on context and workspace; Git through Bash is intentionally unavailable.'

    def open_findings_prompt(self) -> str:
        findings = self.open_findings()
        if not findings:
            return 'Open finding ledger: none. Return an empty prior_findings array.'
        rows = '\n'.join(f"- {finding['id']}: {finding['summary']}" for finding in findings)
        return ('Open finding ledger (return exactly one prior_findings disposition for EVERY id: '
                'fixed, still_open, or withdrawn, with evidence):\n' + rows)

    def _review_prompt(self, role: str, snapshot: str) -> str:
        base_phase = self.state['phase']
        phase = 'POLISH' if self.state['polish']['active'] else base_phase
        if role == 'shadow':
            return '\n'.join([
                f'Role: shadow, fresh isolated read-only whole-delta reviewer. Phase: {phase}.',
                f'Workspace: {self.workspace}', f'Work item: {self.context / "workitem.md"}',
                f'Approved/current plan: {self.context / "plan.md"}',
                f'Program-materialized review files: {self.context / "delta.patch"}, delta.stat, status.txt, and when present delta-since-last-review.patch.',
                'Use only the work item, plan, delta files, and workspace. Review the complete current delta independently.',
                self.inspection_prompt(role),
                self.verified_claims_prompt(),
                self.allowed_command_prompt(),
                f'Run this test command exactly as written in one Bash call: {self.args.test_command}',
                'Do not add cd, pipes, semicolons, &&, redirection, echo wrappers, or other text to that Bash call.',
                'Do not report exit codes; the coordinator reads tool results directly.',
                'APPROVE in EXEC requires non-empty self_run_evidence. Never edit files, commit, push, or load skills.',
                'Return only JSON matching the supplied schema.',
            ])
        fresh_note = 'Use your persistent thread history across PLAN, EXEC, and POLISH.'
        exercise = ''
        reviews = self.state[f'{base_phase.lower()}_reviews']
        if self.args.exercise_revisions and reviews == 0:
            exercise = ('Exercise rule: return REVISE on the first PLAN review because Verification was omitted.' if base_phase == 'PLAN'
                        else 'Exercise rule: return REVISE on the first EXEC review for the intentionally omitted bool rejection.')
        elif self.args.exercise_revisions and base_phase == 'EXEC' and not self.state['polish']['active']:
            exercise = 'Exercise rule: on this APPROVE include one non-blocking MINOR finding for polish.'
        if base_phase == 'PLAN':
            inline = ''
            if self._role_vendor(role) == 'codex':
                inline = ('\nThe complete work item and plan follow as task data; inspect existing source to check their claims.\n'
                          '## Work item content\n' + (self.context / 'workitem.md').read_text() +
                          '\n## Plan content\n' + (self.context / 'plan.md').read_text())
            return '\n'.join([
                f'Role: reviewer, persistent plan-only reviewer. Phase: PLAN. {fresh_note}',
                f'Work item: {self.context / "workitem.md"}',
                f'Plan under review: {self.context / "plan.md"}',
                'Review only the plan for correctness, completeness, scope, and a credible verification strategy.',
                'No implementation exists yet and none is expected in PLAN. Program delta files are empty by design.',
                'Read existing workspace source to check the plan against current behaviour. Findings about missing source files, implementation, or tests are out of scope.',
                self.inspection_prompt(role), self.allowed_command_prompt(), self.verified_claims_prompt(),
                self.open_findings_prompt(), exercise,
                'Never edit files, commit, push, or load skills.',
                'Return only JSON matching the schema. Use stable ids in prior_findings evidence where applicable.',
            ]) + inline
        return '\n'.join([
            f'Role: {role}, read-only whole-delta reviewer. Phase: {phase}. {fresh_note}',
            f'Workspace: {self.workspace}', f'Work item: {self.context / "workitem.md"}',
            f'Approved/current plan: {self.context / "plan.md"}',
            f'Program-materialized review files: {self.context / "delta.patch"}, delta.stat, status.txt, and when present delta-since-last-review.patch.',
            self.inspection_prompt(role),
            self.verified_claims_prompt(),
            'Inspect the complete current delta, not only prior findings. Run relevant allowed checks yourself in EXEC.',
            self.allowed_command_prompt(), self.open_findings_prompt(),
            f'Run this test command exactly as written in one Bash call: {self.args.test_command}',
            'Do not add cd, pipes, semicolons, &&, redirection, echo wrappers, or other text to that Bash call.',
            'Do not report exit codes; the coordinator reads tool results directly.', exercise,
            'APPROVE in EXEC requires non-empty self_run_evidence. Never edit files, commit, push, or load skills.',
            'Return only JSON matching the schema. Use stable ids in prior_findings evidence where applicable.',
        ])

    def _gate_prompt(self, snapshot: str) -> str:
        template = Path(self.args.gate_prompt).read_text()
        filled = template.replace('${REVIEW_TARGET_DESC}', str(self.context / 'workitem.md')).replace(
            '${FOCUS_TEXT}', 'Approved plan: ' + str(self.context / 'plan.md') +
            '\nAudit the complete current git delta in ' + str(self.workspace))
        return (filled + '\nRun this test command exactly as written in one Bash call: ' + self.args.test_command +
                '\n' + self.allowed_command_prompt() +
                '\nRead program-materialized delta.patch, delta.stat, status.txt, and optional delta-since-last-review.patch in: ' + str(self.context) +
                '\n' + self.inspection_prompt('gate') +
                '\n' + self.verified_claims_prompt() +
                '\nDo not add cd, pipes, semicolons, &&, redirection, echo wrappers, or other text to that Bash call.' +
                '\nDo not report exit codes; the coordinator reads tool results directly.' +
                '\nReview independently using only the work item, plan, delta files, and workspace.' +
                '\nNever edit, commit, push, or load skills.')

    def assert_fresh_prompt(self, role: str, prompt: str) -> None:
        if role not in ('shadow', 'gate'):
            return
        # A clean prompt is insufficient when it points to contaminated inputs.
        # Do not rewrite past evidence or silently strip meaningful plan content.
        for name in ('workitem.md', 'plan.md'):
            path = self.context / name
            if path.is_file() and re.search(r'\bF\d{3,}\b', path.read_text()):
                raise RuntimeError(f'{role} independence check rejected ledger ids in context/{name}')
        leaked = sorted(set(re.findall(r'\bF\d{3,}\b', prompt)))
        summaries = [row['summary'] for row in self.state['finding_ledger']
                     if row.get('summary') and row['summary'] in prompt]
        forbidden_phrases = [phrase for phrase in
                             ('Open finding ledger', 'prior_findings', 'Delivered review:')
                             if phrase in prompt]
        if leaked or summaries or forbidden_phrases:
            details = leaked or forbidden_phrases or ['known finding summary']
            raise RuntimeError(f'{role} independence check rejected prompt leak: ' + ', '.join(details))
        # Inspect the literal launch inputs, not just the top-level prompt. Save
        # immutable copies outside context so a later audit can verify each launch.
        sources = {'prompt': prompt, 'original-workitem': self.workitem.read_text()}
        sources.update({f'context/{p.name}': p.read_text()
                        for p in sorted(self.context.iterdir()) if p.is_file()})
        if role == 'gate':
            sources['gate-template'] = Path(self.args.gate_prompt).read_text()
        # Ledger ids are upper-case F###; the scan is case-insensitive elsewhere, so scope that
        # alternative to upper case or an identifier such as `f720` (a 720p frame) trips it.
        history = re.compile(
            r'\b(?-i:F\d{3,})\b|\bprior_findings\b|Open finding ledger|Delivered (?:plan )?review:'
            r'|response[ -]to[ -](?:reviewer|review|F\d+)'
            r'|(?:previous|prior|earlier|persistent|shadow|gate)[ -]+(?:review|verdict|finding)'
            r'|(?:reviewer|review)\s+(?:said|requested|asked|found|approved|rejected)', re.I)
        checked = {}
        for name, content in sources.items():
            # Role/rubric instructions in prompts/templates are not prior verdicts.
            matches = history.findall(content)
            # A filename/path is not a reviewer attribution. Mask only lexical
            # path tokens for the name scan; history/ledger checks remain intact.
            prose = re.sub(r'''(?<![\w.-])[\w~./\\:-]+\.[A-Za-z][A-Za-z0-9]*(?=[:\s`\]\)>,.;!?'\"]|$)''',
                           '<path>', content)
            matches += re.findall(r'\b(?:Claude|Codex|Opus|Astra|gpt-6-astra)\b', prose, re.I)
            if name not in ('prompt', 'gate-template'):
                matches += re.findall(r'\b(?:APPROVE|REVISE|needs-attention)\b', content)
            if matches:
                raise RuntimeError(f'{role} independence check rejected history in {name}: {matches[0]}')
            checked[name] = {'sha256': hashlib.sha256(content.encode()).hexdigest(),
                             'content': content}
        atomic_json(self.evidence / f'{self.state["sequence"] + 1:03d}-{role}.independence-inputs.json',
                    {'role': role, 'checked_at': time.time(), 'status': 'PASS', 'inputs': checked})

    def _collect(self, vendor: str, path: Path) -> tuple[dict, Optional[str], list[dict], int, list[dict], list[dict]]:
        rows = read_json_lines(path)
        commands, calls = observed_events(vendor, rows)
        if vendor == 'codex':
            sessions = [r.get('thread_id') for r in rows if r.get('type') == 'thread.started' and r.get('thread_id')]
            texts = [r.get('item', {}).get('text') for r in rows
                     if r.get('type') == 'item.completed' and r.get('item', {}).get('type') == 'agent_message']
            totals = [r.get('usage', {}) for r in rows if r.get('type') == 'turn.completed']
            if not texts:
                raise ValueError('Codex emitted no agent_message')
            usage = [{'input': x.get('input_tokens', 0), 'cached': x.get('cached_input_tokens', 0),
                      'output': x.get('output_tokens', 0), 'source': 'turn.completed-fallback'} for x in totals]
            return (unwrap_json(texts[-1]), sessions[0] if sessions else None, usage,
                    max(1, len(totals)), commands, calls)
        results = [row for row in rows if row.get('type') == 'result']
        if not results or results[-1].get('is_error'):
            raise ValueError('Claude emitted no successful result')
        result = results[-1]
        if len(results) > 1 or (result.get('subagent_stats') or {}).get('started_in_background'):
            raise ValueError('Claude turn is not authoritative: multiple results or a background subagent')
        answer = result.get('structured_output')
        if not isinstance(answer, dict):
            answer = unwrap_json(result.get('result', ''))
        requests = []
        for row in rows:
            event = row.get('event', {})
            if row.get('type') == 'stream_event' and event.get('type') == 'message_start':
                use = event.get('message', {}).get('usage', {})
                requests.append({'input': use.get('input_tokens', 0) + use.get('cache_creation_input_tokens', 0) + use.get('cache_read_input_tokens', 0),
                                 'cached': use.get('cache_read_input_tokens', 0), 'output': use.get('output_tokens', 0), 'source': 'message_start'})
            elif row.get('type') == 'stream_event' and event.get('type') == 'message_delta' and requests:
                requests[-1]['output'] = event.get('usage', {}).get('output_tokens', requests[-1]['output'])
        if not requests:
            use = result.get('usage', {})
            requests = [{'input': use.get('input_tokens', 0) + use.get('cache_creation_input_tokens', 0) + use.get('cache_read_input_tokens', 0),
                         'cached': use.get('cache_read_input_tokens', 0), 'output': use.get('output_tokens', 0), 'source': 'result-fallback'}]
        # Subagent model requests emit no stream_event rows; they surface only as assistant rows
        # carrying parent_tool_use_id (one row per content block, same message id). The result's
        # modelUsage is cumulative across a resumed session, so it cannot supply a per-turn total.
        subagent = {}
        for index, row in enumerate(rows):
            if row.get('type') != 'assistant' or not row.get('parent_tool_use_id'):
                continue
            message = row.get('message', {})
            use = message.get('usage', {})
            key = message.get('id') or f'row-{index}'
            subagent[key] = {'input': use.get('input_tokens', 0) + use.get('cache_creation_input_tokens', 0) + use.get('cache_read_input_tokens', 0),
                             'cached': use.get('cache_read_input_tokens', 0),
                             'output': max(use.get('output_tokens', 0), subagent.get(key, {}).get('output', 0)),
                             'source': 'subagent-message'}
        requests += list(subagent.values())
        return answer, result.get('session_id'), requests, len(requests), commands, calls

    def invoke(self, role: str, phase: str, prompt: str, schema: dict, fresh=False,
               allow_mutation_report=False, workspace_override: Optional[Path] = None,
               env_overrides: Optional[dict] = None) -> dict:
        for attempt in range(2):
            turn_prompt = prompt if attempt == 0 else (
                prompt + '\nEvidence contract retry: ' + self.verified_claims_prompt())
            result = self._invoke_once(role, phase, turn_prompt, schema, fresh, allow_mutation_report,
                                       workspace_override, env_overrides)
            if role not in ('reviewer', 'shadow', 'gate'):
                return result
            answer = result['answer']
            effective = answer
            if role == 'reviewer' and phase == 'PLAN' and answer.get('status') == 'REVISE':
                findings = answer.get('full_review', [])
                _, out_of_phase = self.normalize_plan_findings(findings)
                if findings and out_of_phase == len(findings):
                    effective = {**answer, 'status': 'APPROVE'}
            error = verified_claims_error(effective)
            if not error:
                return result
            self.state['turns'][-1]['verified_claims_error'] = error
            prefix = self.evidence / f'{result["sequence"]:03d}-{phase.lower()}-{role}.receipt.json'
            atomic_json(prefix, self.state['turns'][-1])
            self.render(result, role + '-protocol-error', phase)
            self.save()
        raise RuntimeError(f'{role} verified_claims protocol error after one retry: {error}')

    def _invoke_once(self, role: str, phase: str, prompt: str, schema: dict, fresh=False,
                     allow_mutation_report=False, workspace_override: Optional[Path] = None,
                     env_overrides: Optional[dict] = None) -> dict:
        self.assert_fresh_prompt(role, prompt)
        active_workspace = Path(workspace_override).resolve() if workspace_override else self.workspace
        if role == 'author' and self._role_vendor(role) == 'codex':
            self.author_temp_dir.mkdir(parents=True, exist_ok=True)
            env_overrides = {**(env_overrides or {}), 'TMPDIR': str(self.author_temp_dir)}
        if self.state['invocations_used'] >= self.args.max_invocations:
            raise RuntimeError('invocation limit reached')
        self.state['sequence'] += 1
        seq = self.state['sequence']
        prefix = self.evidence / f'{seq:03d}-{phase.lower()}-{role}'
        schema_path = prefix.with_suffix('.schema.json')
        atomic_json(schema_path, schema)
        atomic_text(prefix.with_suffix('.prompt.txt'), prompt)
        before, manifest = git_snapshot(active_workspace)
        context_before = directory_digest(self.context)
        atomic_json(prefix.with_suffix('.snapshot-before.json'), {'digest': before, 'manifest': manifest})
        command = self.command(role, schema_path, fresh)
        now = time.time()
        receipt = {'sequence': seq, 'role': role, 'phase': phase, 'vendor': self._role_vendor(role),
                   'model': self._model_effort(role)[0], 'command': command, 'snapshot_before': before,
                   'context_before': context_before,
                   'start': now, 'gap': now - self.state['last_end'].get(role, now), 'fresh': fresh,
                   'invocation_budget_counted': False}
        if env_overrides:
            receipt['environment_overrides'] = dict(env_overrides)
        env = cli_env()
        if env_overrides:
            env.update(env_overrides)
        stdout_path, stderr_path = prefix.with_suffix('.stdout.jsonl'), prefix.with_suffix('.stderr.log')
        process = None
        try:
            with stdout_path.open('wb') as out, stderr_path.open('wb') as err:
                # Count durably before entering Popen: a crash during Popen is
                # ambiguous and must fail closed. Preparation failures above
                # have not consumed the budget.
                self.state['invocations_used'] += 1
                receipt['invocation_budget_counted'] = True
                self.state['active'] = receipt
                self.save()
                process = subprocess.Popen(command, cwd=active_workspace, env=env, stdin=subprocess.PIPE,
                                           stdout=out, stderr=err, start_new_session=True)
                receipt['pid'] = process.pid
                self.state['active'] = receipt
                self.save()
        except BaseException as exc:
            if process is not None:
                # A child exists: retain its counted receipt, then stop/reap its
                # process group when possible. Never classify this as spawn failure.
                self.state['active'] = receipt
                try:
                    self.save()
                except BaseException:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    process.wait()
                except BaseException:
                    pass
                raise
            if not isinstance(exc, (OSError, ValueError, subprocess.SubprocessError)):
                raise
            # These setup errors happen before Popen returns a child handle.
            # A persisted active receipt without a pid after a crash remains uncertain.
            if receipt['invocation_budget_counted']:
                self.state['invocations_used'] -= 1
            self.state['active'] = None
            failure = {**receipt, 'error': type(exc).__name__ + ': ' + str(exc),
                       'invocation_budget_counted': False, 'child_created': False,
                       'failed_at': time.time()}
            self.state.setdefault('spawn_failures', []).append(failure)
            self.state.setdefault('usage_reconciliation', []).append({
                'sequence': seq, 'role': role, 'phase': phase, 'source': 'spawn_failure',
                'provider_usage': 'none', 'invocation_budget_counted': False,
            })
            self.save()
            raise RuntimeError('CLI process failed to start: ' + str(exc)) from exc
        try:
            process.communicate(prompt.encode(), timeout=self.args.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass
            finally:
                process.wait()
            receipt['timed_out'] = True
        except BaseException:
            self.state['active'] = receipt
            try:
                self.save()
            except BaseException:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait()
            except BaseException:
                pass
            raise
        receipt['end'] = time.time()
        receipt['wall_seconds'] = receipt['end'] - receipt['start']
        receipt['returncode'] = process.returncode
        after, after_manifest = git_snapshot(active_workspace)
        context_after = directory_digest(self.context)
        receipt['snapshot_after'] = after
        receipt['context_after'] = context_after
        atomic_json(prefix.with_suffix('.snapshot-after.json'), {'digest': after, 'manifest': after_manifest})
        try:
            if process.returncode:
                error_text = read_text_tail(stderr_path)
                output_text = read_text_tail(stdout_path)
                limit = (None if process.returncode <= 0 or receipt.get('timed_out', False) else
                         classify_rate_limit_failure(process.returncode, error_text, output_text))
                if limit:
                    self.state['invocations_used'] -= 1
                    receipt['invocation_budget_counted'] = False
                    receipt['error_kind'] = limit['kind']
                    receipt['reset_hint'] = limit['reset_hint']
                    reset = limit['reset_hint'] or 'reset time unavailable'
                    raise RateLimitError('rate_limited: provider rejected the call; it did not consume the invocation budget; '
                                         + reset)
                raise ValueError(f'CLI exit {process.returncode}')
            answer, session, usage, requests, observed_commands, tool_calls = self._collect(receipt['vendor'], stdout_path)
            if receipt['vendor'] == 'codex':
                per_request = codex_rollout_usage(session, receipt['start'], receipt['end'])
                if per_request:
                    usage, requests = per_request, len(per_request)
                attempts, code_calls = codex_rollout_attempts(session, receipt['start'], receipt['end'])
                native_commands = {row['command'] for row in observed_commands}
                observed_commands.extend(row for row in attempts if row['command'] not in native_commands)
                tool_calls.extend(code_calls)
            receipt['usage_requests'] = usage
            receipt['model_requests'] = requests
            receipt['observed_tool_calls'] = tool_calls
            receipt['observed_commands'] = observed_commands
            answer['observed_commands'] = observed_commands
            forbidden = sensitive_access(tool_calls, role, self.evidence, self.rounds)
            if forbidden:
                raise ValueError(f'{role} accessed isolated {forbidden}')
            if context_before != context_after:
                raise ValueError(f'{role} mutated coordinator context outside workspace')
            if role == 'author' and phase == 'PLAN' and before != after:
                raise ValueError('author mutated workspace during PLAN')
            if role != 'author' and before != after and not allow_mutation_report:
                raise ValueError(f'{role} mutated workspace')
            if role != 'author':
                answer['reviewed_snapshot'] = before
            approves_exec = (phase in ('EXEC', 'POLISH') and
                             ((role in ('reviewer', 'shadow') and answer.get('status') == 'APPROVE') or
                              (role == 'gate' and answer.get('verdict') == 'approve')))
            if approves_exec and not any(observed_test_succeeded(row, self.args.test_command)
                                         for row in observed_commands):
                raise ValueError(f'{role} EXEC approval lacks an observed successful configured test command')
            if not fresh:
                old = self.state['sessions'].get(role)
                if old and session and old != session:
                    raise ValueError(f'{role} resumed a different session')
                if session:
                    self.state['sessions'][role] = session
                self.state['started'][role] = True
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            receipt['error'] = str(exc)
            self._rotate_failed_first_claude_session(role, receipt['vendor'], fresh)
            atomic_json(prefix.with_suffix('.receipt.json'), receipt)
            self.state['turns'].append(receipt)
            self.state['last_end'][role] = receipt['end']
            self.state['active'] = None
            self.save()
            raise RuntimeError(str(exc)) from exc
        receipt['answer'] = answer
        atomic_json(prefix.with_suffix('.receipt.json'), receipt)
        self.state['turns'].append(receipt)
        self.state['last_end'][role] = receipt['end']
        self.state['active'] = None
        self.save()
        return {'answer': answer, 'snapshot': after, 'sequence': seq, 'role': role}

    def render(self, result: dict, actor: str, phase: str) -> None:
        payload = result['answer']
        status = str(payload.get('status', payload.get('verdict', 'message'))).lower()
        path = self.rounds / f"{result['sequence']:02d}-{actor}-{status}.md"
        evidence_file = f"{result['sequence']:03d}-{phase.lower()}-{result.get('role', actor)}.receipt.json"
        atomic_text(path, render_markdown(actor, phase, payload, result['snapshot'], evidence_file))

    def author_turn(self) -> None:
        if self.state['polish']['active']:
            self.polish_author_turn()
            return
        phase = self.state['phase']
        result = self.invoke('author', phase, self._author_prompt(), author_schema())
        self.render(result, 'implementer', phase)
        answer = result['answer']
        if answer['status'] == 'HOLD':
            self.hold('implementer: ' + answer['body'])
            return
        key = f'{phase.lower()}_rounds'
        self.state[key] += 1
        if phase == 'PLAN':
            atomic_text(self.run_dir / 'plan.md', answer['body'].rstrip() + '\n')
            atomic_text(self.run_dir / f"plan-{self.state[key]:02d}.md", answer['body'].rstrip() + '\n')
            atomic_text(self.context / 'plan.md', answer['body'].rstrip() + '\n')
        self.state['delivered_review'] = ''
        self.state['next'] = 'reviewer'
        self.save()

    def polish_author_turn(self) -> None:
        polish = self.state['polish']
        fixing = polish['fix_used'] and polish['author_turns'] > 0
        schema = author_schema() if fixing else polish_author_schema()
        result = self.invoke('author', 'POLISH', self._author_prompt(), schema)
        self.render(result, 'implementer', 'POLISH')
        answer = result['answer']
        if answer['status'] == 'HOLD':
            self.hold('polish implementer: ' + answer['body'])
            return
        if fixing:
            if polish['author_turns'] >= 2:
                self.hold('polish author turn limit reached')
                return
        else:
            expected = set(polish['finding_ids'])
            supplied = [row['id'] for row in answer['findings']]
            if len(supplied) != len(set(supplied)) or set(supplied) != expected:
                self.hold('polish author response must cover every advisory id exactly once')
                return
            by_id = {row['id']: row for row in self.state['finding_ledger']}
            for response in answer['findings']:
                finding = by_id[response['id']]
                finding['author_disposition'] = response['disposition']
                finding['author_reason'] = response['reason']
                finding['status_history'].append({
                    'round': result['sequence'], 'status': 'author-' + response['disposition'],
                    'evidence': response['reason']})
            self.write_ledger()
        polish['author_turns'] += 1
        self.state['delivered_review'] = ''
        self.state['next'] = 'reviewer'
        self.save()

    def reviewer_turn(self) -> None:
        if self.state['polish']['active']:
            self.polish_reviewer_turn()
            return
        phase = self.state['phase']
        self.materialize_review_context()
        snapshot, _ = git_snapshot(self.workspace)
        result = self.invoke('reviewer', phase, self._review_prompt('reviewer', snapshot), review_schema())
        answer = result['answer']
        missing = self.apply_dispositions(answer['prior_findings'], result['sequence'])
        if missing:
            retry_prompt = (self._review_prompt('reviewer', snapshot) +
                            '\nYour rejected response omitted dispositions for: ' + ', '.join(missing) +
                            '. This is the one allowed protocol retry; include every open id.')
            result = self.invoke('reviewer', phase, retry_prompt, review_schema())
            answer = result['answer']
            missing = self.apply_dispositions(answer['prior_findings'], result['sequence'])
            if missing:
                self.hold('reviewer omitted open finding dispositions after retry: ' + ', '.join(missing))
                return
        if phase == 'PLAN':
            answer['full_review'], out_of_phase = self.normalize_plan_findings(answer['full_review'])
            if (answer['status'] == 'REVISE' and answer['full_review'] and
                    out_of_phase == len(answer['full_review'])):
                answer['status'] = 'APPROVE'
        persistent_verdict = answer['status']
        answer['full_review'] = self.record_findings('persistent-reviewer', phase,
                                                     result['sequence'], answer['full_review'])
        persistent_findings = list(answer['full_review'])
        self.render(result, 'supervisor', phase)
        shadow = None
        if phase == 'EXEC' and self.args.shadow == 'on':
            self.materialize_review_context()
            shadow_snapshot, _ = git_snapshot(self.workspace)
            shadow = self.invoke('shadow', phase, self._review_prompt('shadow', shadow_snapshot),
                                 fresh_review_schema(), fresh=True)
            shadow_rows = self.record_findings('fresh-shadow', phase, shadow['sequence'],
                                               shadow['answer']['full_review'])
            for row in shadow_rows:
                row['source'] = 'fresh-shadow'
            shadow['answer']['full_review'] = shadow_rows
            shadow_critical = [row for row in shadow_rows if row['severity'].upper() == 'CRITICAL']
            if shadow_critical:
                answer['full_review'].extend(shadow_critical)
                if answer['status'] == 'APPROVE':
                    answer['status'] = 'REVISE'
            self.render(shadow, 'shadow', phase)
        if phase == 'EXEC':
            comparison = {'round': self.state['exec_reviews'] + 1,
                          'effective_verdict': answer['status'],
                          'persistent': {'verdict': persistent_verdict,
                                         'findings': self.comparison_findings(
                                             {'full_review': persistent_findings})}}
            if shadow:
                comparison['shadow'] = {'verdict': shadow['answer']['status'],
                                        'findings': self.comparison_findings(shadow['answer'])}
            self.state['exec_comparisons'].append(comparison)
        self.capture_review_baseline()
        self.state[f'{phase.lower()}_reviews'] += 1
        if answer['status'] == 'HOLD':
            self.hold('reviewer HOLD')
            return
        if phase == 'EXEC' and answer['status'] == 'APPROVE' and not answer['self_run_evidence']:
            self.hold('EXEC APPROVE rejected: empty self_run_evidence')
            return
        blocking = self.blocking_open_findings()
        if answer['status'] == 'APPROVE' and blocking:
            self.hold('APPROVE rejected with open blocking findings: ' +
                      ', '.join(finding['id'] for finding in blocking))
            return
        if answer['status'] == 'REVISE':
            limit = self.args.max_plan_rounds if phase == 'PLAN' else self.args.max_exec_rounds
            rounds = self.state[f'{phase.lower()}_rounds']
            if rounds >= limit:
                self.hold(f'{phase} round limit reached')
                return
            self.state['review_findings'] = answer['full_review']
            self.state['delivered_review'] = json.dumps({
                'source': 'persistent-reviewer+fresh-shadow' if any(
                    row.get('source') == 'fresh-shadow' for row in answer['full_review'])
                    else 'persistent-reviewer', 'status': answer['status'],
                'findings': [{**{key: row[key] for key in
                                 ('id', 'severity', 'file', 'summary', 'failure_scenario')},
                              'source': row.get('source', 'persistent-reviewer')}
                             for row in answer['full_review']],
                'prior_findings': [{'id': row['id'], 'fixed': row['disposition'] != 'still_open',
                                    'evidence': row['evidence']} for row in answer['prior_findings']],
            }, ensure_ascii=False)
            self.state['next'] = 'author'
        elif phase == 'PLAN':
            advisory = self.nonblocking_open_findings()
            self.state['delivered_review'] = (self.advisory_message(advisory)
                                              if advisory else '')
            self.state['phase'] = 'EXEC'
            self.state['next'] = 'author'
            self.state['review_findings'] = []
            if self.args.stop_after_plan and not self.state.get('plan_stop_done'):
                self.state['plan_stop_done'] = True
                self.hold(PLAN_STOP_REASON)
                return
        elif answer['reviewed_snapshot'] != git_snapshot(self.workspace)[0]:
            self.hold('stale EXEC approval')
            return
        elif self.args.adversarial_gate == 'on' and not self.state['gate_ran']:
            advisory = self.nonblocking_open_findings()
            self.state['delivered_review'] = (self.advisory_message(advisory)
                                              if advisory else '')
            self.state['next'] = 'gate'
        else:
            self.start_polish_or_done()
            return
        self.save()

    def polish_reviewer_turn(self) -> None:
        polish = self.state['polish']
        if polish['reviewer_turns'] >= (2 if polish['fix_used'] else 1):
            self.hold('polish reviewer turn limit reached')
            return
        self.materialize_review_context()
        snapshot, _ = git_snapshot(self.workspace)
        prompt = self._review_prompt('reviewer', snapshot)
        result = self.invoke('reviewer', 'POLISH', prompt, review_schema())
        answer = result['answer']
        missing = self.apply_dispositions(answer['prior_findings'], result['sequence'])
        if missing:
            retry = prompt + ('\nYour rejected response omitted dispositions for: ' + ', '.join(missing) +
                              '. This is the one allowed protocol retry; include every open id.')
            result = self.invoke('reviewer', 'POLISH', retry, review_schema())
            answer = result['answer']
            missing = self.apply_dispositions(answer['prior_findings'], result['sequence'])
            if missing:
                self.hold('polish reviewer omitted open finding dispositions after retry: ' +
                          ', '.join(missing))
                return
        answer['full_review'] = self.record_findings('persistent-reviewer', 'POLISH',
                                                     result['sequence'], answer['full_review'])
        self.render(result, 'supervisor', 'POLISH')
        self.capture_review_baseline()
        polish['reviewer_turns'] += 1
        if answer['status'] == 'HOLD':
            self.hold('polish reviewer HOLD')
            return
        if answer['status'] == 'APPROVE' and not answer['self_run_evidence']:
            self.hold('POLISH APPROVE rejected: empty self_run_evidence')
            return
        blocking = self.blocking_open_findings()
        if answer['status'] == 'APPROVE':
            if blocking:
                self.hold('POLISH APPROVE rejected with open blocking findings: ' +
                          ', '.join(row['id'] for row in blocking))
                return
            polish['active'] = False
            polish['completed'] = True
            self.done()
            return
        new_critical = [row for row in answer['full_review'] if row['severity'] == 'CRITICAL']
        if answer['status'] == 'REVISE' and new_critical and not polish['fix_used']:
            polish['fix_used'] = True
            self.state['delivered_review'] = json.dumps({
                'source': 'polish-reviewer', 'status': 'REVISE', 'findings': new_critical,
            }, ensure_ascii=False)
            self.state['next'] = 'author'
            self.save()
            return
        self.hold('polish review did not approve; only one CRITICAL regression fix is allowed')

    def gate_turn(self) -> None:
        self.materialize_review_context()
        snapshot, _ = git_snapshot(self.workspace)
        result = self.invoke('gate', 'EXEC', self._gate_prompt(snapshot), gate_schema(), fresh=True)
        answer = result['answer']
        original_findings = list(answer['findings'])
        valid, discarded, valid_indexes, discarded_indexes = [], [], [], []
        for index, finding in enumerate(original_findings):
            if finding['severity'] in ('critical', 'high'):
                missing = missing_rubric(finding['body'])
                if missing:
                    discarded.append({**finding, 'discard_reason': 'missing: ' + ', '.join(missing)})
                    discarded_indexes.append(index)
                else:
                    valid.append(finding)
                    valid_indexes.append(index)
        answer['discarded_blocking'] = discarded
        recorded = self.record_findings('adversarial-gate', 'EXEC', result['sequence'], original_findings)
        answer['findings'] = recorded
        valid = [recorded[index] for index in valid_indexes]
        for index in discarded_indexes:
            ledger = next(row for row in self.state['finding_ledger'] if row['id'] == recorded[index]['id'])
            ledger['status'] = 'withdrawn'
            ledger['status_history'].append({'round': result['sequence'], 'status': 'withdrawn',
                                             'evidence': 'program rejected malformed blocking rubric'})
        self.write_ledger()
        self.render(result, 'adversarial', 'EXEC')
        self.state['gate_ran'] = True
        if self.state['exec_comparisons']:
            self.state['exec_comparisons'][-1]['gate'] = {
                'verdict': answer['verdict'], 'findings': self.comparison_findings(answer)}
        if valid:
            self.set_effective_verdict('REVISE')
            if self.state['exec_rounds'] >= self.args.max_exec_rounds:
                self.hold('EXEC round limit reached after adversarial gate')
                return
            advisory = [row for row in self.nonblocking_open_findings()
                        if row['id'] not in {finding['id'] for finding in valid}]
            self.state['delivered_review'] = json.dumps({
                'source': 'adversarial-gate', 'status': answer['verdict'],
                'findings': [{key: row[key] for key in ('id', 'severity', 'file', 'body')}
                             for row in valid],
                'advisory': self.advisory_rows(advisory),
            }, ensure_ascii=False)
            self.state['next'] = 'author'
        else:
            self.start_polish_or_done()
            return
        self.save()

    def _author_permission_probe(self) -> dict:
        if self.args.author_vendor != 'codex':
            return {'status': 'NOT-APPLICABLE', 'reason': 'author is not Codex'}
        try:
            self.author_temp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='paired-session-author-probe-',
                                             dir=self.run_dir) as base_text:
                base = Path(base_text)
                workspace = base / 'workspace'
                workspace.mkdir()
                subprocess.run(['git', 'init', '-q'], cwd=workspace, check=True)
                subprocess.run(['git', 'config', 'user.email', 'permission-probe@example.invalid'],
                               cwd=workspace, check=True)
                subprocess.run(['git', 'config', 'user.name', 'paired-session permission probe'],
                               cwd=workspace, check=True)
                (workspace / 'tracked.txt').write_text('probe baseline\n')
                subprocess.run(['git', 'add', 'tracked.txt'], cwd=workspace, check=True)
                subprocess.run(['git', 'commit', '-qm', 'probe baseline'], cwd=workspace, check=True)

                allowed_path = workspace / 'author-write-allowed.txt'
                tmpdir_name = 'author-write-tmpdir-' + uuid.uuid4().hex + '.txt'
                allowed_tmpdir_path = self.author_temp_dir / tmpdir_name
                host_tmpdir = Path(tempfile.gettempdir()).expanduser().resolve()
                allowed_roots = (workspace.resolve(), self.author_temp_dir.resolve())
                if any(host_tmpdir == root or root in host_tmpdir.parents for root in allowed_roots):
                    host_tmpdir = base / 'external-tmpdir'
                    host_tmpdir.mkdir()
                external_tmpdir_path = host_tmpdir / ('paired-session-external-tmpdir-' + uuid.uuid4().hex + '.txt')
                slash_tmp_path = Path('/tmp') / ('paired-session-author-probe-' + uuid.uuid4().hex + '.txt')
                while slash_tmp_path.exists():
                    slash_tmp_path = Path('/tmp') / ('paired-session-author-probe-' + uuid.uuid4().hex + '.txt')
                commands = [
                    'printf probe > ' + shlex.quote(str(allowed_path)),
                    'printf probe > "$TMPDIR/' + tmpdir_name + '"',
                    'printf probe > ' + shlex.quote(str(external_tmpdir_path)),
                    'printf probe > ' + shlex.quote(str(slash_tmp_path)),
                ]
                prompt = '\n'.join([
                    'Role: author workspace-write permission probe in a disposable checkout.',
                    f'Workspace: {workspace}',
                    'This is a harness test. Make exactly four separate Bash calls, each using one literal command below.',
                    'Do not skip, explain, batch, or alter a command; the sandbox decides whether it succeeds.',
                    'The workspace and designated run-owned TMPDIR writes are allowed. External TMPDIR and /tmp writes must be denied.',
                    '1. Workspace write: ' + commands[0],
                    '2. Designated run TMPDIR write: ' + commands[1],
                    '3. External TMPDIR write: ' + commands[2],
                    '4. /tmp write: ' + commands[3],
                    'PROBE_COMMANDS_JSON: ' + json.dumps(commands),
                    'Return APPROVE with no findings. Tool events and filesystem state, not your claims, determine PASS.',
                ])
                result = self.invoke('author', 'AUTHOR_PERMISSION_PROBE', prompt,
                                     review_schema(verified=False), fresh=True,
                                     workspace_override=workspace,
                                     env_overrides={'TMPDIR': str(self.author_temp_dir)})
                evidence = result['answer'].get('observed_commands', [])
                def succeeded(command):
                    return any(row.get('command', '').strip() == command
                               and type(row.get('exit_code')) is int and row.get('exit_code') == 0
                               and row.get('error') is False for row in evidence)
                def denied(command):
                    return any(row.get('command', '').strip() == command
                               and type(row.get('exit_code')) is int and row.get('exit_code') != 0
                               and row.get('error') is True for row in evidence)
                outcomes = {
                    'workspace_write_allowed': (len([row for row in evidence
                                                       if row.get('command', '').strip() == commands[0]]) == 1
                                                and allowed_path.is_file() and succeeded(commands[0])),
                    'run_tmpdir_write_allowed': (len([row for row in evidence
                                                        if row.get('command', '').strip() == commands[1]]) == 1
                                                 and allowed_tmpdir_path.is_file() and succeeded(commands[1])),
                    'external_tmpdir_write_denied': (len([row for row in evidence
                                                            if row.get('command', '').strip() == commands[2]]) == 1
                                                     and not external_tmpdir_path.exists() and denied(commands[2])),
                    'slash_tmp_write_denied': (len([row for row in evidence
                                                      if row.get('command', '').strip() == commands[3]]) == 1
                                               and not slash_tmp_path.exists() and denied(commands[3])),
                }
                return {'status': 'PASS' if all(outcomes.values()) else 'FAIL',
                        'outcomes': outcomes, 'commands': commands,
                        'observed_commands': evidence,
                        'workspace_snapshot': result['snapshot']}
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            return {'status': 'FAIL', 'reason': type(exc).__name__ + ': ' + str(exc)}
        finally:
            if 'slash_tmp_path' in locals() and slash_tmp_path.exists():
                slash_tmp_path.unlink()
            if 'allowed_tmpdir_path' in locals() and allowed_tmpdir_path.exists():
                allowed_tmpdir_path.unlink()
            if 'external_tmpdir_path' in locals() and external_tmpdir_path.exists():
                external_tmpdir_path.unlink()

    def permission_probe(self, retry_uncertain=False) -> bool:
        """One fresh reviewer turn proving allowlist use and write denial/detection."""
        uncertain = self.state.get('active') or self.state.get('uncertain_active')
        if uncertain:
            if uncertain.get('phase') not in ('PROBE', 'AUTHOR_PERMISSION_PROBE'):
                self.state['uncertain_active'] = uncertain
                self.hold('uncertain work-item turn must be resolved before a new permission probe')
                return False
            if not retry_uncertain:
                self.state['uncertain_active'] = uncertain
                self.hold('uncertain permission-probe turn; confirm its child stopped, then use permission-probe --retry-uncertain')
                return False
            pid = uncertain.get('pid')
            if type(pid) is not int or pid <= 1 or pid > 2**31 - 1:
                self.state['uncertain_active'] = uncertain
                self.hold('invalid uncertain permission-probe process-group id; refusing retry')
                return False
            try:
                # Provider CLIs start a new session, making the recorded leader
                # pid the group id. This detects remaining members of that group;
                # detached descendants are outside this check.
                os.killpg(pid, 0)
            except ProcessLookupError:
                pass
            except (OSError, OverflowError):
                self.state['uncertain_active'] = uncertain
                self.hold('cannot verify uncertain permission-probe process group is stopped')
                return False
            else:
                self.state['uncertain_active'] = uncertain
                self.hold('uncertain permission-probe process group is still alive; refusing concurrent retry')
                return False
            self.archive_abandoned_turn(uncertain)
            self.state['active'] = None
            self.state['uncertain_active'] = None
            if self.state.get('hold_reason', '').startswith(
                    ('uncertain permission-probe turn', 'uncertain permission-probe process group',
                     'cannot verify uncertain permission-probe',
                     'uncertain in-flight CLI turn')):
                self.state['hold_reason'] = 'permission probe retry cleared; permission probe is pending'
            self.save()
        snapshot, _ = git_snapshot(self.workspace)
        allowed_command = self.args.test_command.strip()
        test_attack = allowed_command + ' --help > forbidden-test-help'
        attempts = ('echo x > forbidden-probe', 'git checkout -- tracked.txt', 'rm tracked.txt',
                    'git diff --output=forbidden-diff', 'git log --output=forbidden-log -1',
                    'git show --output=forbidden-show HEAD', test_attack)
        sandbox_probe_paths = []
        if self.args.reviewer_vendor == 'claude':
            slash_tmp_path = Path('/tmp') / ('paired-session-claude-sandbox-' + uuid.uuid4().hex)
            run_dir_probe_path = self.run_dir / ('.paired-session-run-dir-probe-' + uuid.uuid4().hex)
            context_probe_path = self.context / ('.paired-session-context-probe-' + uuid.uuid4().hex)
            while slash_tmp_path.exists():
                slash_tmp_path = Path('/tmp') / ('paired-session-claude-sandbox-' + uuid.uuid4().hex)
            while run_dir_probe_path.exists():
                run_dir_probe_path = self.run_dir / ('.paired-session-run-dir-probe-' + uuid.uuid4().hex)
            while context_probe_path.exists():
                context_probe_path = self.context / ('.paired-session-context-probe-' + uuid.uuid4().hex)
            sandbox_probe_paths = [slash_tmp_path, run_dir_probe_path, context_probe_path]
            self._probe_sandbox_commands = [
                'printf probe > ' + shlex.quote(str(path)) for path in sandbox_probe_paths
            ]
            attempts = (*attempts, *self._probe_sandbox_commands)
        prompt = '\n'.join([
            'Role: permission-system probe in an authorized throwaway checkout. Do not report exit codes.',
            f'Workspace: {self.workspace}',
            'This is an authorized test of the harness. You MUST attempt every command exactly as written.',
            f'Denial by the permission harness is expected for the {len(attempts)} write commands.',
            'Do not pre-judge, refuse, explain, or skip any command; only the harness decides whether it runs.',
            f'Make exactly {len(attempts) + 1} separate Bash calls, one for each literal command below.',
            'Do not add cd, pipes, semicolons, &&, redirection beyond the literal, echo wrappers, or any other text.',
            'Allowed exact command:', allowed_command,
            'Write commands expected to be denied:', *attempts,
            f'Return APPROVE and list only the {len(attempts) + 1} literal commands in self_run_evidence.',
            'Other finding arrays must be empty; the coordinator derives all results from tool_result events.',
        ])
        if self.args.reviewer_vendor == 'codex':
            prompt += ('\nFor observable permission results, make each command a separate tool invocation. '
                       'Do not use arrays, loops, Promise.all, or batched exec cells for this probe. '
                       'If using code-mode, each cell must be exactly: const r = await tools.exec_command('
                       '{"cmd":"<one literal command>","workdir":' + json.dumps(str(self.workspace)) +
                       '}); text(JSON.stringify(r)); Use JSON property names/strings and print the full result, not only r.output.')
        base_report = {'status': 'FAIL', 'reviewer_flags': self.reviewer_flags(),
                       'reviewer_flags_digest': self.reviewer_flags_digest(),
                       'author_flags': self.author_flags(),
                       'author_flags_digest': self.author_flags_digest(),
                       'author_permission_probe': {
                           'status': 'NOT-APPLICABLE' if self.args.author_vendor != 'codex' else 'NOT-ATTEMPTED'},
                       'claude_flag_semantics': 'UNVERIFIED until this probe runs with the real CLI'}
        probe_sequence = self.state['sequence'] + 1
        try:
            result = self.invoke('probe', 'PROBE', prompt, review_schema(verified=False), fresh=True,
                                 allow_mutation_report=True)
            self.render(result, 'permission-probe', 'PROBE')
            claimed = result['answer'].get('self_run_evidence', [])
            evidence = result['answer'].get('observed_commands', [])
            unchanged = result['snapshot'] == snapshot
        except Exception as exc:
            escaped_probe_targets = []
            cleaned_probe_targets = []
            remaining_probe_targets = []
            cleanup_errors = []
            target_absent_before_cleanup = {}
            if sandbox_probe_paths:
                escaped_probe_targets = [str(path) for path in sandbox_probe_paths if path.exists()]
                target_absent_before_cleanup = {str(path): not path.exists() for path in sandbox_probe_paths}
                (escaped_probe_targets, cleaned_probe_targets,
                 remaining_probe_targets, cleanup_errors) = cleanup_probe_targets(sandbox_probe_paths)
                self._probe_sandbox_commands = None
            latest = next((turn for turn in reversed(self.state['turns'])
                           if turn.get('sequence') == probe_sequence), {})
            report = {**base_report, 'allowed_command_ran': False,
                      'write_attempts_denied': {command: False for command in attempts},
                      'write_attempt_outcomes': {command: 'not-attempted' for command in attempts},
                      'failure_reasons': ['probe-turn-error: ' + str(exc)],
                      'snapshot_unchanged': git_snapshot(self.workspace)[0] == snapshot,
                      'observed_commands': latest.get('observed_commands', []),
                      'claimed_self_run_evidence': [],
                      'claude_sandbox_escape_targets_found': escaped_probe_targets,
                      'claude_sandbox_escape_targets_cleaned': cleaned_probe_targets,
                      'claude_sandbox_escape_targets_remaining': remaining_probe_targets,
                      'claude_sandbox_cleanup_errors': cleanup_errors,
                      'claude_sandbox_write_denials': ({
                          'host_tmp': {'exact_command_observed_once': False, 'failed_at_os_sandbox': False,
                                       'target_absent': target_absent_before_cleanup.get(str(sandbox_probe_paths[0]), False)},
                          'run_dir': {'exact_command_observed_once': False, 'failed_at_os_sandbox': False,
                                      'target_absent': target_absent_before_cleanup.get(str(sandbox_probe_paths[1]), False)},
                          'context': {'exact_command_observed_once': False, 'failed_at_os_sandbox': False,
                                      'target_absent': target_absent_before_cleanup.get(str(sandbox_probe_paths[2]), False)},
                      } if sandbox_probe_paths else None),
                      'claude_sandbox_write_denied': False if sandbox_probe_paths else None}
            if escaped_probe_targets:
                report['failure_reasons'].append('claude-sandbox-probe-write-escaped')
            if remaining_probe_targets:
                report['failure_reasons'].append('claude-sandbox-probe-cleanup-incomplete')
            if sandbox_probe_paths:
                report['failure_reasons'].append('claude-sandbox-write-not-denied-or-not-observed')
            atomic_json(self.run_dir / 'permission-probe.json', report)
            self.state['hold_reason'] = 'permission probe failed; inspect permission-probe.json'
            self.save()
            self.write_usage()
            return False
        allowed_matches = [row for row in evidence if row.get('command', '').strip() == allowed_command]
        allowed = (len(allowed_matches) == 1 and type(allowed_matches[0].get('exit_code')) is int
                   and allowed_matches[0].get('exit_code') == 0
                   and allowed_matches[0].get('error') is False)
        denied = {}
        outcomes = {}
        failures = []
        if not any(row.get('command', '').strip() == allowed_command for row in evidence):
            failures.append('not-attempted: ' + allowed_command)
        elif not allowed:
            failures.append('allowed-command-failed: ' + allowed_command)
        for command in attempts:
            matches = [row for row in evidence if row.get('command', '').strip() == command]
            if not matches:
                outcomes[command] = 'not-attempted'
                failures.append('not-attempted: ' + command)
            elif all(row.get('error') for row in matches):
                outcomes[command] = 'denied'
            elif unchanged:
                outcomes[command] = 'no-trace'
                failures.append('no-explicit-denial: ' + command)
            else:
                outcomes[command] = 'trace-detected'
                failures.append('trace-detected: ' + command)
            denied[command] = outcomes[command] == 'denied'
        if not unchanged:
            failures.append('snapshot-mutated')
        try:
            author_probe = self._author_permission_probe()
        except Exception as exc:
            author_probe = {'status': 'FAIL', 'reason': type(exc).__name__ + ': ' + str(exc)}
        report = {**base_report, 'status': 'PASS' if allowed and all(denied.values()) else 'FAIL',
                  'allowed_command_ran': allowed, 'write_attempts_denied': denied,
                  'write_attempt_outcomes': outcomes, 'failure_reasons': failures,
                  'snapshot_unchanged': unchanged,
                  'observed_commands': evidence, 'claimed_self_run_evidence': claimed,
                  'author_permission_probe': author_probe,
                  }
        if author_probe['status'] == 'FAIL':
            report['status'] = 'FAIL'
            report['failure_reasons'].append('author-permission-probe-failed')
        if not report['snapshot_unchanged']:
            report['status'] = 'FAIL'
        if sandbox_probe_paths:
            checks = {}
            for label, path, command in zip(('host_tmp', 'run_dir', 'context'), sandbox_probe_paths,
                                            self._probe_sandbox_commands):
                matches = [row for row in evidence if row.get('command', '').strip() == command]
                output = '\n'.join(str(row.get('output', '')) for row in matches).lower()
                os_denial = any(marker in output for marker in
                                ('operation not permitted', 'read-only file system'))
                target_absent = not path.exists()
                checks[label] = {
                    'exact_command_observed_once': len(matches) == 1,
                    'failed_at_os_sandbox': (len(matches) == 1 and matches[0].get('error') is True
                                             and type(matches[0].get('exit_code')) is int
                                             and matches[0]['exit_code'] != 0 and os_denial),
                    'target_absent': target_absent,
                }
                if not all(checks[label].values()):
                    report['failure_reasons'].append('claude-sandbox-' + label + '-write-not-denied-or-not-observed')
            report['claude_sandbox_write_denials'] = checks
            report['claude_sandbox_write_denied'] = all(all(item.values()) for item in checks.values())
            (escaped_probe_targets, cleaned_probe_targets,
             remaining_probe_targets, cleanup_errors) = cleanup_probe_targets(sandbox_probe_paths)
            report['claude_sandbox_escape_targets_found'] = escaped_probe_targets
            report['claude_sandbox_escape_targets_cleaned'] = cleaned_probe_targets
            report['claude_sandbox_escape_targets_remaining'] = remaining_probe_targets
            report['claude_sandbox_cleanup_errors'] = cleanup_errors
            if escaped_probe_targets:
                report['failure_reasons'].append('claude-sandbox-probe-write-escaped')
                report['status'] = 'FAIL'
            if remaining_probe_targets:
                report['failure_reasons'].append('claude-sandbox-probe-cleanup-incomplete')
                report['status'] = 'FAIL'
            if not report['claude_sandbox_write_denied']:
                report['status'] = 'FAIL'
            self._probe_sandbox_commands = None
        atomic_json(self.run_dir / 'permission-probe.json', report)
        self.state['hold_reason'] = ('permission probe passed; run resume to continue'
                                     if report['status'] == 'PASS' else
                                     'permission probe failed; inspect permission-probe.json')
        self.save()
        self.write_usage()
        return report['status'] == 'PASS'

    def drive(self) -> str:
        if self.state.get('uncertain_active'):
            return self.hold('uncertain CLI turn; inspect evidence, then use resume --retry-uncertain')
        if self.state['active']:
            self.state['uncertain_active'] = self.state['active']
            return self.hold('uncertain in-flight CLI turn; inspect evidence, then use resume --retry-uncertain')
        while self.state['status'] == 'ACTIVE':
            try:
                if self.state['next'] == 'author':
                    self.author_turn()
                elif self.state['next'] == 'reviewer':
                    self.reviewer_turn()
                elif self.state['next'] == 'gate':
                    self.gate_turn()
                else:
                    return self.hold('invalid next action')
            except RuntimeError as exc:
                return self.hold(str(exc))
        return self.state['status']

    def resume(self, retry_uncertain=False) -> str:
        if self.state['status'] == 'DONE':
            blocking = self.blocking_open_findings()
            if blocking:
                return self.hold('DONE state rejected with open blocking findings: ' +
                                 ', '.join(row['id'] for row in blocking))
            return 'DONE'
        reason = self.state.get('hold_reason', '')
        if self.state.get('active'):
            self.state['uncertain_active'] = self.state['active']
            self.state['active'] = None
            if not retry_uncertain:
                return self.hold('uncertain in-flight CLI turn; inspect evidence, then use resume --retry-uncertain')
            self.save()
        uncertain = self.state.get('uncertain_active')
        if uncertain and not retry_uncertain:
            if not reason:
                self.state['hold_reason'] = 'uncertain CLI turn; inspect evidence, then use resume --retry-uncertain'
            self.state['status'] = 'HOLD'
            self.save()
            self.write_usage()
            return 'HOLD'
        pid = uncertain.get('pid') if uncertain else None
        if uncertain and (type(pid) is not int or pid <= 1 or pid > 2**31 - 1):
            return self.hold('uncertain CLI receipt has no verifiable pid; process existence cannot be verified; operator/manual resolution is required')
        if type(pid) is int:
            try:
                # Each provider CLI starts a new session, so its pid is also the
                # process-group id. Check the group to catch surviving descendants.
                os.killpg(pid, 0)
            except ProcessLookupError:
                pass
            except (OSError, OverflowError):
                return self.hold('cannot verify uncertain CLI process group is stopped')
            else:
                return self.hold('uncertain CLI process group is still alive; refusing concurrent replay')
        if uncertain:
            self.archive_abandoned_turn(uncertain)
            self._rotate_failed_first_claude_session(
                uncertain.get('role', ''), uncertain.get('vendor') or
                self._role_vendor(uncertain.get('role', '')), uncertain.get('fresh', False))
        self.state['status'] = 'ACTIVE'
        self.state['hold_reason'] = ''
        self.state['active'] = None
        self.state['uncertain_active'] = None
        self.save()
        return self.drive()

    def write_usage(self) -> None:
        groups: dict[str, dict] = {}
        for turn in self.state['turns']:
            key = turn['role'] + '/' + turn['phase']
            row = groups.setdefault(key, {'role': turn['role'], 'phase': turn['phase'], 'cli_turns': 0,
                                          'model_requests': 0, 'input_tokens': 0, 'cached_tokens': 0,
                                          'output_tokens': 0, 'wall_seconds': 0.0, 'gaps_seconds': []})
            row['cli_turns'] += 1
            row['model_requests'] += turn.get('model_requests', 0)
            row['wall_seconds'] += turn.get('wall_seconds', 0)
            row['gaps_seconds'].append(turn.get('gap', 0))
            for use in turn.get('usage_requests', []):
                row['input_tokens'] += use.get('input', 0)
                row['cached_tokens'] += use.get('cached', 0)
                row['output_tokens'] += use.get('output', 0)
        def total_rows(rows):
            total = {'cli_turns': 0, 'model_requests': 0, 'input_tokens': 0,
                     'cached_tokens': 0, 'output_tokens': 0, 'wall_seconds': 0.0,
                     'gaps_seconds': []}
            for row in rows:
                for field in ('cli_turns', 'model_requests', 'input_tokens', 'cached_tokens',
                              'output_tokens', 'wall_seconds'):
                    total[field] += row[field]
                total['gaps_seconds'].extend(row['gaps_seconds'])
            total['uncached_input_tokens'] = total['input_tokens'] - total['cached_tokens']
            return total
        role_totals = {}
        for role in sorted({row['role'] for row in groups.values()}):
            role_totals[role] = total_rows([row for row in groups.values() if row['role'] == role])
        overall = total_rows(list(groups.values()))
        for row in groups.values():
            row['uncached_input_tokens'] = row['input_tokens'] - row['cached_tokens']
        reconciliation_rows = [
            {field: row.get(field) for field in
             ('source', 'sequence', 'role', 'phase', 'invocation_budget_counted', 'provider_usage')}
            for row in self.state.get('usage_reconciliation', [])
        ]
        reconciled_sequences = {row.get('sequence') for row in reconciliation_rows}
        for turn in self.state['turns']:
            if (not turn.get('usage_requests') and
                    turn.get('sequence') not in reconciled_sequences):
                reconciliation_rows.append({
                    'source': 'cli_turn', 'sequence': turn.get('sequence'),
                    'role': turn.get('role'), 'phase': turn.get('phase'),
                    'invocation_budget_counted': bool(turn.get('invocation_budget_counted', True)),
                    'provider_usage': 'unknown',
                })
                reconciled_sequences.add(turn.get('sequence'))
        pending = self.state.get('uncertain_active') or self.state.get('active')
        represented_sequences = {row.get('sequence') for row in reconciliation_rows}
        if pending and pending.get('sequence') not in represented_sequences:
            reconciliation_rows.append({
                'source': 'uncertain_active', 'sequence': pending.get('sequence'),
                'role': pending.get('role'), 'phase': pending.get('phase'),
                'invocation_budget_counted': bool(pending.get('invocation_budget_counted', True)),
                'provider_usage': 'unknown',
            })
        turn_rows = []
        for turn in self.state['turns']:
            input_tokens = sum(use.get('input', 0) for use in turn.get('usage_requests', []))
            cached_tokens = sum(use.get('cached', 0) for use in turn.get('usage_requests', []))
            turn_rows.append({'turn': turn['sequence'], 'role': turn['role'], 'phase': turn['phase'],
                              'invocation_budget_counted': turn.get('invocation_budget_counted', True),
                              'error_kind': turn.get('error_kind'), 'reset_hint': turn.get('reset_hint'),
                              'requests': turn.get('model_requests', 0), 'input': input_tokens,
                              'cached': cached_tokens, 'uncached': input_tokens - cached_tokens,
                              'output': sum(use.get('output', 0) for use in turn.get('usage_requests', [])),
                              'wall_seconds': turn.get('wall_seconds', 0)})
        report = {'status': self.state['status'], 'waiting_model_calls': self.state['waiting_model_calls'],
                  'invocations_used': self.state.get('invocations_used', len(self.state['turns'])),
                  'max_invocations': self.args.max_invocations,
                  'total_cli_turns': len(self.state['turns']), 'by_role_phase': list(groups.values()),
                  'by_role': role_totals, 'overall': overall, 'turns': turn_rows,
                  'usage_reconciliation': reconciliation_rows,
                  'requests': [{'sequence': t['sequence'], 'role': t['role'], 'phase': t['phase'],
                                'usage': t.get('usage_requests', [])} for t in self.state['turns']]}
        atomic_json(self.run_dir / 'usage.json', report)
        lines = ['# Usage', '', f"- Status: {report['status']}", f"- CLI turns: {report['total_cli_turns']}",
                 f"- Invocation budget used: {report['invocations_used']} / {report['max_invocations']}",
                 f"- Model calls spent waiting: {report['waiting_model_calls']}", '',
                 '| Role / phase | CLI turns | Model requests | Input | Cached | Uncached input | Output | Wall seconds | Gaps seconds |',
                 '|---|---:|---:|---:|---:|---:|---:|---:|---|']
        for key, row in groups.items():
            lines.append(f"| {key} | {row['cli_turns']} | {row['model_requests']} | {row['input_tokens']} | {row['cached_tokens']} | {row['uncached_input_tokens']} | {row['output_tokens']} | {row['wall_seconds']:.3f} | {', '.join(f'{x:.3f}' for x in row['gaps_seconds'])} |")
        for role, row in role_totals.items():
            lines.append(f"| {role}/TOTAL | {row['cli_turns']} | {row['model_requests']} | {row['input_tokens']} | {row['cached_tokens']} | {row['uncached_input_tokens']} | {row['output_tokens']} | {row['wall_seconds']:.3f} | {', '.join(f'{x:.3f}' for x in row['gaps_seconds'])} |")
        lines.append(f"| OVERALL | {overall['cli_turns']} | {overall['model_requests']} | {overall['input_tokens']} | {overall['cached_tokens']} | {overall['uncached_input_tokens']} | {overall['output_tokens']} | {overall['wall_seconds']:.3f} | {', '.join(f'{x:.3f}' for x in overall['gaps_seconds'])} |")
        lines += ['', '## Per turn', '',
                  '| Turn | Role | Phase | Budget counted | Error kind | Reset hint | Requests | Input | Cached | Uncached | Output | Wall seconds |',
                  '|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---:|']
        for row in turn_rows:
            lines.append(f"| {row['turn']} | {row['role']} | {row['phase']} | {row['invocation_budget_counted']} | {row['error_kind'] or ''} | {row['reset_hint'] or ''} | {row['requests']} | {row['input']} | {row['cached']} | {row['uncached']} | {row['output']} | {row['wall_seconds']:.3f} |")
        lines += ['', '## Invocation budget reconciliation', '',
                  '| Source | Sequence | Role | Phase | Budget counted | Provider usage |',
                  '|---|---:|---|---|---|---|']
        for row in reconciliation_rows:
            lines.append(f"| {row['source']} | {row['sequence']} | {row['role']} | {row['phase']} | {row['invocation_budget_counted']} | {row['provider_usage']} |")
        lines += ['', 'Invocation budget counts durable starts, including unresolved uncertain_active receipts whose provider usage remains unknown.',
                  'Explicit provider rate-limit rejections are refunded and do not count against the invocation budget.',
                  'Token rows come from per-request Claude stream usage; Codex stdout fallback is marked in usage.json.',
                  'No dollar estimates are reported.']
        atomic_text(self.run_dir / 'usage.md', '\n'.join(lines) + '\n')


def cli_env() -> dict:
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # `claude -p` starts Agent subagents in the background by default: the turn then emits an
    # interim result before the subagent finishes and a second one afterwards. Foreground
    # subagents keep one authoritative result per turn and stream nested tool calls in order.
    env['CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'] = '1'
    return env


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(allow_abbrev=False)
    p.add_argument('action', choices=['run', 'resume', 'abort', 'snapshot', 'permission-probe'])
    p.add_argument('--workspace', required=True)
    p.add_argument('--workitem', required=True)
    p.add_argument('--run-dir', required=True)
    p.add_argument('--config', help='JSON profile; defaults to <workspace>/.review-loop/paired-session.json')
    p.add_argument('--author-vendor', choices=['codex', 'claude'], default='codex')
    p.add_argument('--author-model', help='defaults to the vendor-pinned ADR-5 model')
    p.add_argument('--author-effort', default='medium')
    p.add_argument('--reviewer-vendor', choices=['codex', 'claude'], default='claude')
    p.add_argument('--reviewer-model', help='defaults to the vendor-pinned ADR-5 model')
    p.add_argument('--reviewer-effort', default='medium')
    p.add_argument('--gate-model', help='defaults to the vendor-pinned ADR-5 model')
    p.add_argument('--gate-effort', default='medium')
    p.add_argument('--shadow', choices=['on', 'off'], default='on')
    p.add_argument('--adversarial-gate', choices=['on', 'off'], default='on')
    p.add_argument('--polish-round', choices=['on', 'off'], default='on')
    p.add_argument('--gate-prompt', default=str(DEFAULT_GATE_PROMPT))
    p.add_argument('--max-plan-rounds', type=int, default=3)
    p.add_argument('--max-exec-rounds', type=int, default=4)
    p.add_argument('--max-invocations', type=int, default=25)
    p.add_argument('--timeout', type=int, default=2700)
    p.add_argument('--test-command', default='npm test')
    p.add_argument('--reviewer-command', action='append', default=[],
                   help='additional exact Bash command allowed for read-only reviewers (repeatable)')
    p.add_argument('--codex-bin', default='codex')
    p.add_argument('--claude-bin', default='claude')
    p.add_argument('--exercise-revisions', action='store_true')
    p.add_argument('--stop-after-plan', action='store_true',
                   help='HOLD once right after PLAN approval; a later resume enters EXEC')
    p.add_argument('--author-subagents', choices=['on', 'off'], default='on',
                   help='let a Claude author spawn subagents (read-only roles never can)')
    p.add_argument('--retry-uncertain', action='store_true',
                   help='after confirming its child stopped, explicitly retry an uncertain run or permission-probe turn')
    p.add_argument('--polish', action='store_true',
                   help='with resume, run only the one-time polish round on an older DONE run')
    p.add_argument('--skip-probe', action='store_true',
                   help='explicitly bypass the permission-probe gate (tests only)')
    return p


CONFIGURABLE_DESTS = {
    'author_vendor', 'author_model', 'author_effort', 'reviewer_vendor',
    'reviewer_model', 'reviewer_effort', 'gate_model', 'gate_effort', 'shadow',
    'adversarial_gate', 'polish_round', 'gate_prompt', 'max_plan_rounds',
    'max_exec_rounds', 'max_invocations', 'timeout', 'test_command',
    'reviewer_command', 'codex_bin', 'claude_bin', 'author_subagents',
}


def resolve_role_model_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Apply ADR-5's vendor-pinned defaults when no model is explicitly selected."""
    model_for_vendor = {'claude': 'claude-opus-5-5', 'codex': 'gpt-6-luna'}
    role_vendors = {
        'author_model': args.author_vendor,
        'reviewer_model': args.reviewer_vendor,
        'gate_model': 'claude' if args.author_vendor == 'codex' else 'codex',
    }
    for key, vendor in role_vendors.items():
        if getattr(args, key) is None:
            setattr(args, key, model_for_vendor[vendor])
    return args


def validate_role_models(args: argparse.Namespace) -> None:
    """Enforce the currently accepted ADR-5 vendor/model pairing before a run."""
    model_for_vendor = {'claude': 'claude-opus-5-5', 'codex': 'gpt-6-luna'}
    role_vendors = {
        'author_model': args.author_vendor,
        'reviewer_model': args.reviewer_vendor,
        'gate_model': 'claude' if args.author_vendor == 'codex' else 'codex',
    }
    for key, vendor in role_vendors.items():
        expected = model_for_vendor[vendor]
        actual = getattr(args, key)
        if actual != expected:
            raise ValueError(f'{key} must be {expected} for the {vendor} role under ADR-5; got {actual}')


def configure_parser(p: argparse.ArgumentParser, argv: list[str]) -> argparse.ArgumentParser:
    """Load project defaults while preserving explicit CLI argument precedence."""
    bootstrap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    bootstrap.add_argument('--workspace', required=True)
    bootstrap.add_argument('--config')
    known, _ = bootstrap.parse_known_args(argv)
    workspace = Path(known.workspace).expanduser().resolve()
    config_path = (Path(known.config).expanduser() if known.config else
                   workspace / '.review-loop' / 'paired-session.json')
    if not config_path.is_absolute():
        config_path = workspace / config_path
    if not config_path.is_file():
        if known.config:
            raise ValueError(f'--config file does not exist: {config_path}')
        return p
    try:
        values = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'cannot read paired-session config {config_path}: {exc}') from exc
    if not isinstance(values, dict):
        raise ValueError('paired-session config must be a JSON object')
    unknown = set(values) - CONFIGURABLE_DESTS
    if unknown:
        raise ValueError('unsupported paired-session config keys: ' + ', '.join(sorted(unknown)))
    explicit = set()
    for action in p._actions:
        if action.dest in CONFIGURABLE_DESTS and any(
                arg == option or arg.startswith(option + '=')
                for option in action.option_strings for arg in argv):
            explicit.add(action.dest)
    actions = {action.dest: action for action in p._actions}
    for key, value in values.items():
        if key in explicit:
            continue
        action = actions[key]
        if action.dest == 'reviewer_command':
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError('config reviewer_command must be an array of strings')
        else:
            if action.type:
                if action.type is int and type(value) is not int:
                    raise ValueError(f'config {key} must be an integer')
                try:
                    value = value if action.type is int else action.type(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f'invalid value for config {key}: {value!r}') from exc
            elif not isinstance(value, str):
                raise ValueError(f'config {key} must be a string')
            if action.choices and value not in action.choices:
                raise ValueError(f'invalid config {key}: {value!r}; choose from {action.choices}')
        action.default = value
    return p


def normalize_cli_paths(args: argparse.Namespace) -> argparse.Namespace:
    """Resolve paths once so guards, leases, state and child commands agree."""
    args.workspace = str(Path(args.workspace).expanduser().resolve())
    args.workitem = str(Path(args.workitem).expanduser().resolve())
    args.run_dir = str(Path(args.run_dir).expanduser().resolve())
    gate_prompt = Path(args.gate_prompt).expanduser()
    if not gate_prompt.is_absolute():
        gate_prompt = Path(args.workspace) / gate_prompt
    args.gate_prompt = str(gate_prompt.resolve())
    return args


def _execute_locked(args: argparse.Namespace) -> int:
    co = Coordinator(args)
    if args.action == 'permission-probe':
        try:
            passed = co.permission_probe(retry_uncertain=args.retry_uncertain)
        except RuntimeError as exc:
            co.hold('permission probe: ' + str(exc))
            passed = False
        suffix = ': ' + co.state.get('hold_reason', '') if co.state.get('hold_reason') else ''
        print(('PASS' if passed else 'FAIL') + suffix)
        return 0 if passed else 2
    if args.action in ('run', 'resume') and not args.skip_probe:
        passed, reason = co.probe_passed()
        if not passed:
            print('REFUSED: ' + reason + '; run permission-probe before continuing')
            return 2
    if args.action == 'abort':
        suffix = ('; a prior CLI child may still be running; inspect uncertain_active before retry'
                  if co.state.get('active') or co.state.get('uncertain_active') else '')
        co.hold('aborted by operator' + suffix)
        print('HOLD: ' + co.state['hold_reason'])
        return 2
    status = (co.drive() if args.action == 'run' else
              co.resume_polish() if args.polish else co.resume(args.retry_uncertain))
    print(status + (': ' + co.state.get('hold_reason', '') if status == 'HOLD' else ''))
    return 0 if status == 'DONE' else 2


def main(argv=None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    cli_parser = parser()
    if '-h' in raw_argv or '--help' in raw_argv:
        cli_parser.parse_args(raw_argv)
    try:
        args = configure_parser(cli_parser, raw_argv).parse_args(raw_argv)
    except ValueError as exc:
        print('REFUSED: ' + str(exc))
        return 2
    args = normalize_cli_paths(args)
    try:
        resolve_role_model_defaults(args)
        validate_role_models(args)
    except ValueError as exc:
        print('REFUSED: ' + str(exc))
        return 2
    if args.polish and args.action != 'resume':
        parser().error('--polish is only valid with resume')
    if args.action == 'snapshot':
        print(git_snapshot(Path(args.workspace))[0])
        return 0
    if not Path(args.gate_prompt).is_file():
        print('REFUSED: gate prompt file is missing: ' + args.gate_prompt)
        return 2
    workspace = Path(args.workspace)
    run_dir = Path(args.run_dir)
    if run_dir == workspace or workspace in run_dir.parents:
        print('REFUSED: --run-dir must be outside --workspace')
        return 2
    if args.action in ('run', 'resume', 'permission-probe'):
        if re.search(r'[*?\[\]{}]', str(run_dir)):
            print('REFUSED: --run-dir must not contain glob metacharacters used by Claude Edit deny rules')
            return 2
        try:
            resolve_test_executable(workspace, args.test_command)
        except ValueError as exc:
            print('REFUSED: ' + str(exc))
            return 2
    try:
        with run_lease(Path(args.run_dir)), workspace_lease(workspace, run_dir):
            return _execute_locked(args)
    except ValueError as exc:
        print('REFUSED: ' + str(exc))
        return 2
    except (RunLeaseError, OSError) as exc:
        print('HOLD: ' + str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
