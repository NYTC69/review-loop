#!/usr/bin/env python3
"""Stdlib-only paired PLAN/EXEC coordinator for one real work item.

The coordinator owns transport, snapshots, limits, and evidence.  Models never
write transport files.  Persistent author/reviewer threads span both phases;
shadow and adversarial reviewers are always fresh.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager, nullcontext
from datetime import datetime
import errno
import difflib
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
import threading
import time
import uuid
from typing import Optional
try:
    from paired_session import budget_policy
    from paired_session import candidate_tree
    from paired_session import closeout_policy
    from paired_session import q_proposal
    from paired_session import q_evidence
    from paired_session import delivery_intent, delivery_seal
    from paired_session import codex_capability_guard
    from paired_session import docs_policy
    from paired_session import finish_dispatch
    from paired_session import lifecycle_spine
    from paired_session import sensitive_policy
    from paired_session import security_repair_policy
    from paired_session.program_binding import snapshot as program_snapshot, safe_path
except ModuleNotFoundError:
    import budget_policy
    import candidate_tree
    import closeout_policy
    import q_proposal
    import q_evidence
    import delivery_intent, delivery_seal
    import codex_capability_guard
    import docs_policy
    import finish_dispatch
    import lifecycle_spine
    import sensitive_policy
    import security_repair_policy
    from program_binding import snapshot as program_snapshot, safe_path

HERE = Path(__file__).resolve().parent
DEFAULT_GATE_PROMPT = HERE.parent / 'scripts' / 'adversarial_gate_fallback_prompt.txt'
RUBRIC = ('Trigger', 'Reachability', 'Impact', 'Likelihood', 'Fix cost', 'Cheaper response')
PROBE_SURFACE_VERSION = 9
PLAN_STOP_REASON = 'PLAN approved; stopped by --stop-after-plan; resume enters EXEC'
MAX_RESUME_TIMEOUT_SECONDS = 7200
DEFAULT_EXEC_TURN_TIMEOUT_SECONDS = 7200
MAX_EXEC_TURN_TIMEOUT_SECONDS = 14400


def resolve_exec_turn_timeout(value, general_timeout):
    timeout = value if value is not None else min(max(DEFAULT_EXEC_TURN_TIMEOUT_SECONDS, general_timeout), MAX_EXEC_TURN_TIMEOUT_SECONDS)
    if not 1 <= timeout <= MAX_EXEC_TURN_TIMEOUT_SECONDS: raise ValueError(f'--exec-turn-timeout must be between 1 and {MAX_EXEC_TURN_TIMEOUT_SECONDS} seconds')
    return timeout
DEFAULT_MAX_REJECTIONS = 2
OUTPUT_TAIL_LINES = 15
OUTPUT_TAIL_CHARS = 1500
ADVISORY_REVIEW_SEVERITIES = {'MINOR', 'LOW'}
BLOCKING_REVIEW_SEVERITIES = {'CRITICAL', 'MAJOR', 'SECURITY'}


def pending_item_blockers(state: dict, run_dir: Path) -> list[dict]:
    """Freeze open item-wide blockers for a successor without changing legacy dispatch."""
    rows = {}
    for source in (*state.get('item_blockers', []), *state.get('finding_ledger', [])):
        if source.get('status') not in ('open', 'awaiting-revalidation') or not (source.get('severity') in BLOCKING_REVIEW_SEVERITIES or
                source.get('security') or source.get('source') == 'adversarial-gate' and source.get('severity') in ('CRITICAL', 'HIGH', 'MEDIUM')):
            continue
        row = copy.deepcopy(source); row.setdefault('origin_run', str(run_dir))
        key = (row['origin_run'], row['id'])
        if key in rows and rows[key] != row: raise ValueError('item blocker identity changed')
        rows[key] = row
    return [rows[key] for key in sorted(rows)]


def _read_role_source(path: Path) -> bytes:
    nofollow = getattr(os, 'O_NOFOLLOW', None)
    if nofollow is None:
        raise ValueError('role source no-follow reads are unavailable')
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, 'O_CLOEXEC', 0) |
                     getattr(os, 'O_NONBLOCK', 0))
    except OSError as exc:
        raise ValueError('role source cannot be opened without following links') from exc
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        os.close(fd)
        raise ValueError('role source is not a regular file')
    with os.fdopen(fd, 'rb') as stream:
        content = stream.read()
        after = os.fstat(stream.fileno())
    fields = ('st_dev', 'st_ino', 'st_mtime_ns', 'st_ctime_ns', 'st_size')
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise ValueError('role source changed while reading')
    return content


def frozen_role_manifest(config: dict, role_flags: dict, agents_dir: Path,
                         gate_prompt: Path, required_agents: dict[str, str]) -> dict:
    agents_dir = Path(agents_dir)
    if (agents_dir.is_symlink() or not agents_dir.is_dir() or not required_agents or
            set(required_agents) != set(role_flags)):
        raise ValueError('required role-agent directory or mapping is missing')
    agents = {}
    for path in sorted(agents_dir.glob('*.md')):
        agents[path.name] = hashlib.sha256(_read_role_source(path)).hexdigest()
    if any(name not in agents for name in required_agents.values()):
        raise ValueError('required role-agent body is missing')
    prompt_path = Path(gate_prompt)
    if prompt_path.is_symlink():
        raise ValueError('gate prompt cannot be a symlink')
    prompt_hash = hashlib.sha256(_read_role_source(prompt_path)).hexdigest()
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True,
                                             separators=(',', ':')).encode()).hexdigest()
    role_bytes = json.dumps(role_flags, sort_keys=True, separators=(',', ':')).encode()
    return {'config_sha256': config_hash, 'role_flags': copy.deepcopy(role_flags),
            'role_flags_sha256': hashlib.sha256(role_bytes).hexdigest(),
            'role_agents': dict(required_agents), 'agents_dir': str(agents_dir.resolve()),
            'agent_body_sha256': agents, 'gate_prompt_path': str(prompt_path.resolve()),
            'gate_prompt_sha256': prompt_hash}
REVIEW_SEVERITY_GUIDANCE = ('CRITICAL, MAJOR, and SECURITY findings are blocking. Set security=true for any security issue, even when its impact severity is MINOR or LOW. Never lower severity to qualify for advisory handling.')


class RunLeaseError(RuntimeError):
    pass


class RateLimitError(ValueError):
    pass


def retry_killpg_eperm(pid: int, clock=None, sleep=None) -> None:
    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    deadline = clock() + 1.5
    while True:
        try:
            os.killpg(pid, 0)
            return
        except OSError as exc:
            if exc.errno != errno.EPERM:
                raise
            remaining = deadline - clock()
            if remaining <= 0:
                raise
            sleep(min(0.05, remaining))


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


def _global_file_snapshot(path: Path, parser=None) -> dict:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {'path': str(path), 'sha256': None, 'missing': True,
                'raw': None, 'document': None, 'error': None}
    except OSError as exc:
        return {'path': str(path), 'sha256': None, 'missing': False,
                'raw': None, 'document': None, 'error': type(exc).__name__}
    result = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
              'missing': False, 'raw': raw.decode('utf-8', 'replace'),
              'document': None, 'error': None}
    if parser:
        try:
            result['document'] = parser(result['raw'])
        except (TypeError, ValueError) as exc:
            result['error'] = type(exc).__name__
    return result


def global_config_snapshot(home: Optional[Path] = None, codex_home: Optional[Path] = None) -> dict:
    """Capture only the three user-global files checked by the installed probe."""
    home = (home or Path.home()).expanduser()
    codex_home = (codex_home or home / '.codex').expanduser()
    snapshots = {
        'codex_config': _global_file_snapshot(codex_home / 'config.toml'),
        'claude_settings': _global_file_snapshot(home / '.claude' / 'settings.json', json.loads),
        'claude_plugins': _global_file_snapshot(
            home / '.claude' / 'plugins' / 'installed_plugins.json', json.loads),
    }
    if codex_home.resolve() != (home / '.codex').resolve():
        snapshots['codex_default_config'] = _global_file_snapshot(home / '.codex' / 'config.toml')
    return snapshots


def _without_mapping_key(value, key: str):
    if isinstance(value, dict):
        return {name: _without_mapping_key(child, key)
                for name, child in value.items() if name != key}
    if isinstance(value, list):
        return [_without_mapping_key(child, key) for child in value]
    return value


def _changed_mapping_key_paths(before, after, key: str, path='$') -> list[str]:
    changes = []
    if isinstance(before, dict) and isinstance(after, dict):
        for name in set(before) | set(after):
            child_path = f'{path}.{name}'
            if name == key and before.get(name) != after.get(name):
                changes.append(child_path)
            elif name in before and name in after:
                changes.extend(_changed_mapping_key_paths(before[name], after[name], key, child_path))
    elif isinstance(before, list) and isinstance(after, list):
        for index, (left, right) in enumerate(zip(before, after)):
            changes.extend(_changed_mapping_key_paths(left, right, key, f'{path}[{index}]'))
    return changes


def _only_codex_workspace_trust_append(before: dict, after: dict, workspaces) -> list[str]:
    expected_workspaces = {str(Path(path).resolve()) for path in workspaces}
    if after.get('raw') is None:
        return []
    inserted = []
    before_lines = (before.get('raw') or '').splitlines(keepends=True)
    after_lines = after['raw'].splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    insertion_count = 0
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == 'equal':
            continue
        if tag != 'insert' or old_start != old_end:
            return []
        following = next((line.strip() for line in before_lines[old_start:] if line.strip()), None)
        if following is not None and not following.startswith('['): return []
        insertion_count += 1
        block = [line.strip() for line in after_lines[new_start:new_end] if line.strip()]
        if not block or len(block) % 2: return []
        inserted.extend(block)
    if not insertion_count:
        return []
    if not inserted or len(inserted) % 2:
        return []
    added = []
    for index in range(0, len(inserted), 2):
        header, setting = inserted[index:index + 2]
        path = next((path for path in expected_workspaces
                     if header == '[projects.' + json.dumps(path) + ']'), None)
        if (path is None or setting != 'trust_level = "trusted"' or path in added
                or any(line.strip() == header for line in before_lines)):
            return []
        added.append(path)
    return sorted(added)


def trust_entry_only_since_hash(path: Path, before_sha256: str, workspace: Path) -> bool:
    raw = path.read_bytes()
    entry = ('[projects.' + json.dumps(str(workspace.resolve())) + ']\ntrust_level = "trusted"\n').encode()
    if raw.count(entry) != 1: return False
    for leading in (b'', b'\n'):
        for trailing in (b'', b'\n'):
            block = leading + entry + trailing
            if block in raw:
                before = raw.replace(block, b'', 1)
                if hashlib.sha256(before).hexdigest() == before_sha256 and _only_codex_workspace_trust_append({'raw': before.decode('utf-8')}, {'raw': raw.decode('utf-8')}, [workspace]) == [str(workspace.resolve())]: return True
    return False


def attribute_global_config_changes(before: dict, after: dict, workspaces=()) -> dict:
    """Attribute only the known Codex trust and Claude lastUpdated side effects."""
    expected, findings = [], []
    public = {'before': {}, 'after': {}}
    for label in ('codex_config', 'claude_settings', 'claude_plugins',
                  *(['codex_default_config'] if 'codex_default_config' in before else [])):
        old, new = before[label], after[label]
        public['before'][label] = {'path': old['path'], 'sha256': old['sha256']}
        public['after'][label] = {'path': new['path'], 'sha256': new['sha256']}
        if old['sha256'] is not None and old['sha256'] == new['sha256']:
            continue
        if old.get('missing') and new.get('missing'):
            continue
        if old['error'] or new['error']:
            findings.append({'file': label, 'reason': 'unreadable-or-invalid'})
            continue
        if label == 'codex_config':
            trusted_workspaces = _only_codex_workspace_trust_append(old, new, workspaces)
            if trusted_workspaces:
                expected.append({'file': label, 'change': 'trusted-probe-workspace-entry',
                                 'workspaces': trusted_workspaces})
            else:
                findings.append({'file': label, 'reason': 'unexpected-content-change'})
        elif label == 'claude_plugins':
            old_doc, new_doc = old['document'], new['document']
            paths = _changed_mapping_key_paths(old_doc, new_doc, 'lastUpdated')
            if paths and _without_mapping_key(old_doc, 'lastUpdated') == _without_mapping_key(new_doc, 'lastUpdated'):
                expected.append({'file': label, 'change': 'plugin-lastUpdated', 'paths': sorted(paths)})
            else:
                findings.append({'file': label, 'reason': 'unexpected-content-change'})
        else:
            findings.append({'file': label, 'reason': 'unexpected-content-change'})
    return {'status': 'FAIL' if findings else 'PASS', **public,
            'expected_changes': expected, 'findings': findings, 'warnings': ['global config mutated by codex CLI trust persistence'] if any(row['file'] == 'codex_config' for row in expected) else []}


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
    """Return a per-user lock path keyed by the resolved workspace's filesystem identity."""
    root = Path(workspace).expanduser().resolve()
    try:
        identity = root.stat()
    except OSError as exc:
        raise RunLeaseError('cannot stat workspace for lease key: ' + str(exc)) from exc
    uid = os.getuid()
    for temp_root in _workspace_lease_temp_roots():
        lock_dir = temp_root / f'paired-session-workspace-leases-{uid}'
        try:
            lock_dir.relative_to(root)
        except ValueError:
            pass
        else:
            continue
        identity_key = f'{identity.st_dev}:{identity.st_ino}'
        key = hashlib.sha256(identity_key.encode('ascii')).hexdigest()
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


def reported_provider_model(vendor: str, path: Path) -> tuple[Optional[str], Optional[str]]:
    """Read only the top-level provider model identity from a valid CLI stream."""
    try:
        lines = path.read_text(errors='strict').splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not all(isinstance(row, dict) for row in rows):
        return None, None
    candidates = []
    for row in rows:
        if vendor == 'codex':
            if row.get('type') in ('thread.started', 'turn.started'):
                model = row.get('model') or row.get('turn', {}).get('model')
                if isinstance(model, str) and model:
                    candidates.append((model, row.get('type')))
        elif vendor == 'claude':
            if row.get('type') == 'system' and row.get('subtype') == 'init':
                model = row.get('model')
                if isinstance(model, str) and model:
                    candidates.append((model, 'session-init'))
            elif row.get('type') == 'stream_event' and not row.get('parent_tool_use_id'):
                event = row.get('event', {})
                if event.get('type') == 'message_start':
                    model = event.get('message', {}).get('model')
                    if isinstance(model, str) and model:
                        candidates.append((model, 'message_start'))
            elif row.get('type') == 'assistant' and not row.get('parent_tool_use_id'):
                model = row.get('message', {}).get('model')
                if isinstance(model, str) and model:
                    candidates.append((model, 'assistant'))
    real = [(model, source) for model, source in candidates if model != '<synthetic>']
    if not real or len({model for model, _ in real}) != 1:
        return None, None
    return real[0]


def model_identity_status(requested: str, reported: Optional[str]) -> str:
    if not requested or not reported or reported == '<synthetic>':
        return 'UNREPORTED'
    if (reported == requested or
            re.fullmatch(re.escape(requested) + r'-(?:\d{8}|\d{4}-\d{2}-\d{2})', reported)):
        return 'MATCH'
    return 'MISMATCH'


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
        'severity': {'type': 'string', 'enum': sorted(ADVISORY_REVIEW_SEVERITIES | BLOCKING_REVIEW_SEVERITIES)},
        'file': {'type': 'string'}, 'summary': {'type': 'string'},
        'failure_scenario': {'type': 'string'}, 'security': {'type': 'boolean'},
    }, 'required': ['severity', 'file', 'summary', 'failure_scenario', 'security'], 'additionalProperties': False}
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


def codex_sessions_dir() -> Path:
    return Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'sessions'


def codex_rollout_usage(session: Optional[str], start: float, end: float) -> list[dict]:
    """Read per-request last_token_usage records for this CLI interval."""
    if not session:
        return []
    paths = list(codex_sessions_dir().rglob('*' + session + '*.jsonl'))
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
    paths = list(codex_sessions_dir().rglob('*' + session + '*.jsonl'))
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
    def __init__(self, args: argparse.Namespace, *, _fake_lifecycle=False):
        resolve_role_model_defaults(args)
        validate_role_models(args)
        if args.lifecycle_mode == 'on':
            if args.adversarial_gate == 'off': raise ValueError('lifecycle refuses --adversarial-gate off')
            if args.polish: raise ValueError('lifecycle refuses resume --polish')
            if not (_fake_lifecycle and lifecycle_spine.fake_guard(args)):
                raise ValueError('lifecycle remains disabled until every stage and isolation check is implemented')
        if getattr(args, 'resume_timeout', None) is not None and args.action != 'resume':
            raise ValueError('--resume-timeout is accepted only with resume')
        if getattr(args, 'acknowledge_codex_trust', None) and args.action != 'resume': raise ValueError('--acknowledge-codex-trust requires resume')
        self.args = args
        self._fake_lifecycle = bool(_fake_lifecycle and args.lifecycle_mode=='on' and lifecycle_spine.fake_guard(args))
        self._fake_dispatching = False
        self._save_lock = threading.Lock()
        self.workspace = Path(args.workspace).expanduser().resolve()
        self.workitem = Path(args.workitem).expanduser().resolve()
        self.run_dir = Path(args.run_dir).expanduser().resolve()
        self.global_config_home = Path.home()
        if not Path(codex_home_text := os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')).expanduser().is_absolute(): raise ValueError('CODEX_HOME must be absolute so global Codex config changes can be monitored')
        self.global_codex_home = Path(codex_home_text).expanduser().resolve()
        self.author_temp_dir = self.run_dir / 'author-tmp'
        self._probe_sandbox_commands = None
        self.rounds = self.run_dir / 'rounds'
        self.evidence = self.run_dir / 'evidence'
        self.context = self.run_dir / 'context'
        self.internal = self.run_dir / 'internal'
        self.state_path = self.run_dir / 'state.json'
        if not self.state_path.exists():
            self.args.exec_turn_timeout = resolve_exec_turn_timeout(
                self.args.exec_turn_timeout, self.args.timeout)
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if not {'approved_snapshot', 'rejected_digests'} <= self.state.keys():
                raise ValueError('run was created by an older paired-session build; start a new run')
            if self.state.get('config', {}).get('lifecycle_mode') == 'on' and not self._fake_lifecycle:
                raise ValueError('saved lifecycle run cannot resume before all stages are implemented')
            self.state.setdefault('item_uuid', str(uuid.uuid5(uuid.NAMESPACE_URL, str(self.run_dir))))
            self.state.setdefault('item_blockers', [])
            self.state.setdefault('item_blockers_complete', False)
            if (self.state.get('supersedes') != (str(Path(args.supersedes).resolve()) if args.supersedes else None) or
                    (self.state.get('effective_task_sha256') and
                     self.state['effective_task_sha256'] != hashlib.sha256(self.workitem.read_bytes()).hexdigest())):
                raise ValueError('saved supersedes or effective task hash differs')
            if self.state.get('supersedes'):
                parent = Path(self.state['supersedes'])
                old = json.loads((parent / 'state.json').read_text())
                spec_path = parent / 'evidence/successor-spec.json'
                spec = json.loads(spec_path.read_text())
                claim = parent / 'evidence/successor-claim.json'
                if (old.get('status') != 'ABORTED' or spec.get('run_dir') != str(self.run_dir) or
                        spec.get('task_sha256') != self.state['effective_task_sha256'] or
                        self.state.get('base_commit') != old.get('base_commit') or
                        not claim.is_file() or json.loads(claim.read_text()).get('run_dir') != str(self.run_dir) or
                        spec.get('config_sha256') != hashlib.sha256((parent / 'evidence/successor-config.json').read_bytes()).hexdigest() or
                        old.get('successor_spec_sha256') != hashlib.sha256(spec_path.read_bytes()).hexdigest() or
                        spec.get('original_hash') != hashlib.sha256(Path(spec['original_workitem']).read_bytes()).hexdigest()):
                    raise ValueError('saved successor link differs')
                if spec.get('item_uuid') and (self.state['item_uuid'] != spec['item_uuid'] or old.get('item_uuid') != spec['item_uuid'] or
                        self.state.get('item_blockers') != spec.get('item_blockers') or spec['item_blockers'] != pending_item_blockers(old, parent) or
                        self.state.get('item_blockers_complete') != spec.get('item_blockers_complete')):
                    raise ValueError('saved successor item identity or blockers differ')
            if 'reason' in self.state and 'hold_reason' not in self.state:
                self.state['hold_reason'] = self.state.pop('reason')
                self.save()
            if self.args.action in ('accept', 'reject', 'note'):
                if Path(self.state['workspace']) != self.workspace or Path(self.state['workitem']) != self.workitem:
                    raise ValueError('accept/reject workspace/workitem differs from state')
                for key, value in self.state['config'].items():
                    if key == 'gate_prompt' and str(value).startswith('<bundled-default>:'):
                        self.args.gate_prompt = str(DEFAULT_GATE_PROMPT)
                    elif hasattr(self.args, key):
                        setattr(self.args, key, value)
                self.args.exec_turn_timeout = resolve_exec_turn_timeout(
                    self.state['config'].get('exec_turn_timeout'),
                    self.state['config'].get('timeout', DEFAULT_EXEC_TURN_TIMEOUT_SECONDS))
            else:
                self._validate_resume_args()
            scope_request = args.action in ('note', 'reject') and getattr(args, 'scope_change', False)
            if self.state.get('scope_change_intent') and not scope_request:
                raise ValueError('scope change is pending; finish that request')
            if self.state.get('status') == 'ABORTED' and not scope_request:
                raise ValueError('run was ABORTED by scope change; start its successor')
            if 'base_commit' not in self.state:
                self.state['base_commit_backfilled'] = True
            self.state.setdefault('base_commit', self._head_commit())
            self.state.setdefault('reviews_completed', 0)
            ledger_path = self.run_dir / 'findings-ledger.json'
            self.state.setdefault('finding_ledger', json.loads(ledger_path.read_text())
                                  if ledger_path.exists() else [])
            highest_id = max((int(row['id'][1:]) for row in self.state['finding_ledger']
                              if re.fullmatch(r'F\d+', row.get('id', ''))), default=0)
            self.state.setdefault('next_finding_id', highest_id + 1)
            self.state.setdefault('exec_comparisons', [])
            self.state.setdefault('review_verdicts', [])
            self.state.setdefault('reviewed_reviewer_sequences', [])
            self.state.setdefault('pending_reviewer_result_sequence', None)
            self.state.setdefault('acceptance_state', 'ACCEPTED' if self.state.get('status') == 'ACCEPTED' else
                                  'PENDING' if self.state.get('status') == 'DONE' else 'IN_PROGRESS')
            self.state.setdefault('polish', {'active': False, 'completed': False,
                                             'author_turns': 0, 'reviewer_turns': 0,
                                             'fix_used': False})
            if self.state.get('invocation_budget_version', 0) < 1:
                self.state['invocations_used'] = self._migrate_invocation_budget()
                self.state['invocation_budget_version'] = 1
                self.save()
        else:
            if self.args.action in ('accept', 'reject', 'note'):
                raise ValueError(f'{self.args.action} requires an existing coordinator run')
            if not (self.workspace / '.git').exists():
                raise ValueError('--workspace must be a git worktree')
            if not self.workitem.is_file():
                raise ValueError('--workitem must be a file')
            frozen_config = self._config()
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.rounds.mkdir(exist_ok=True)
            self.evidence.mkdir(exist_ok=True)
            self.context.mkdir(exist_ok=True)
            self.internal.mkdir(exist_ok=True)
            atomic_text(self.context / 'workitem.md', self.workitem.read_text())
            self.state = {
                'version': 1, 'status': 'ACTIVE', 'phase': 'PLAN', 'next': 'author',
                'workspace': str(self.workspace), 'workitem': str(self.workitem),
                'config': frozen_config,
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
                'item_uuid': str(uuid.uuid4()), 'item_blockers': [], 'item_blockers_complete': True,
                'review_verdicts': [],
                'reviewed_reviewer_sequences': [], 'pending_reviewer_result_sequence': None,
                'acceptance_state': 'IN_PROGRESS', 'approved_snapshot': None, 'rejected_digests': [],
                'polish': {'active': False, 'completed': False, 'author_turns': 0,
                           'reviewer_turns': 0, 'fix_used': False},
            }
            if self._fake_lifecycle:
                self.state['lifecycle'] = lifecycle_spine.initial(self.state['item_uuid'], self.state['base_commit'])
            self._freeze_role_dispatch()
            if args.supersedes:
                parent = Path(args.supersedes).resolve()
                old = json.loads((parent / 'state.json').read_text())
                spec = json.loads((parent / 'evidence/successor-spec.json').read_text())
                task_hash = hashlib.sha256(self.workitem.read_bytes()).hexdigest()
                if (old.get('status') != 'ABORTED' or old.get('abort_kind') != 'scope-change' or
                        not old.get('base_commit') or old.get('base_commit_backfilled') or
                        old.get('scope_chain_depth', 0) >= 1 or spec.get('base_commit') != old['base_commit'] or spec['task_sha256'] != task_hash or
                        spec['run_dir'] != str(self.run_dir) or spec['workspace'] != str(self.workspace) or
                        spec['original_hash'] != hashlib.sha256(Path(spec['original_workitem']).read_bytes()).hexdigest() or
                        spec.get('config_sha256') != hashlib.sha256((parent / 'evidence/successor-config.json').read_bytes()).hexdigest() or
                        old.get('successor_spec_sha256') != hashlib.sha256((parent / 'evidence/successor-spec.json').read_bytes()).hexdigest()):
                    raise ValueError('successor spec or parent state differs')
                if spec.get('item_uuid') and (spec['item_uuid'] != old.get('item_uuid') or
                        spec.get('item_blockers') != pending_item_blockers(old, parent)):
                    raise ValueError('successor item identity or blockers differ')
                if subprocess.run(['git', 'merge-base', '--is-ancestor', old['base_commit'], 'HEAD'],
                                  cwd=self.workspace).returncode:
                    raise ValueError('parent base_commit is not an ancestor of HEAD')
                with run_lease(parent):
                    claim = parent / 'evidence/successor-claim.json'
                    if claim.exists() and json.loads(claim.read_text())['run_dir'] != str(self.run_dir):
                        raise ValueError('parent already has a successor')
                    if not claim.exists():
                        atomic_json(claim, {'run_dir': str(self.run_dir)})
                self.state.update(base_commit=old['base_commit'], supersedes=str(parent),
                                  scope_chain_depth=old.get('scope_chain_depth', 0) + 1,
                                  effective_task_sha256=task_hash,
                                  item_uuid=old.get('item_uuid') or str(uuid.uuid5(uuid.NAMESPACE_URL, str(parent))),
                                  item_blockers=copy.deepcopy(spec.get('item_blockers', [])),
                                  item_blockers_complete=bool(spec.get('item_uuid') and spec.get('item_blockers_complete')))
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
                'timeout', 'exec_turn_timeout', 'max_invocations', 'exercise_revisions', 'test_command',
                'gate_prompt', 'reviewer_command', 'polish_round', 'codex_bin', 'claude_bin',
                'author_subagents', 'lifecycle_mode', 'docs_file', 'docs_allowlist',
                'skip_globs', 'skip_quality_polish')
        config = {key: getattr(self.args, key) for key in keys}
        gate_prompt = Path(self.args.gate_prompt).expanduser()
        if not gate_prompt.is_absolute():
            gate_prompt = self.workspace / gate_prompt
        gate_prompt = gate_prompt.resolve()
        config['gate_prompt'] = (
            '<bundled-default>:' + hashlib.sha256(gate_prompt.read_bytes()).hexdigest()
            if gate_prompt == DEFAULT_GATE_PROMPT.resolve() else str(gate_prompt))
        def frozen_doc_path(value):
            if any(char in value for char in '*?['): raise ValueError('lifecycle doc paths must be exact')
            path = (self.workspace / value).expanduser().resolve()
            if self.workspace not in path.parents: raise ValueError('lifecycle doc path escapes workspace')
            return str(path)
        config['docs_file'] = frozen_doc_path(self.args.docs_file) if self.args.docs_file else ''
        config['docs_allowlist'] = sorted({frozen_doc_path(value) for value in
                                           [*self.args.docs_allowlist, *([self.args.docs_file] if self.args.docs_file else [])]})
        config['workitem_reviewer_commands'] = workitem_reviewer_commands(self.workitem.read_text())
        return config

    def _author_tmp_isolated(self) -> bool:
        state_root = self.run_dir.resolve()
        temporary = self.author_temp_dir.resolve()
        try:
            return (state_root not in temporary.parents and temporary != state_root and
                    os.stat(temporary.parent).st_dev != os.stat(state_root).st_dev)
        except OSError:
            return False

    def _role_dispatch_manifest(self) -> dict:
        config = self.args
        author_vendor, reviewer_vendor = config.author_vendor, config.reviewer_vendor
        author = {'vendor': author_vendor, 'model': config.author_model,
                  'effort': config.author_effort,
                  'binary': config.claude_bin if author_vendor == 'claude' else config.codex_bin,
                  'sandbox': (self._author_sandbox_overrides() if author_vendor == 'codex'
                              else self._claude_sandbox_settings('author')),
                  'subagents': config.author_subagents,
                  'tmp_isolated': self._author_tmp_isolated()}
        reviewer = self.reviewer_flags()
        flags = {'author': author, 'reviewer': reviewer, 'shadow': copy.deepcopy(reviewer)}
        bodies = {'author': 'executor.md', 'reviewer': 'reviewer.md',
                  'shadow': 'reviewer.md'}
        prompt = Path(self.args.gate_prompt).expanduser()
        if str(self.state.get('config', {}).get('gate_prompt', '')).startswith('<bundled-default>:'):
            prompt = DEFAULT_GATE_PROMPT
        return frozen_role_manifest(self.state['config'], flags, HERE.parent / 'agents',
                                    prompt, bodies)

    def _freeze_role_dispatch(self) -> None:
        try:
            manifest = self._role_dispatch_manifest()
        except (OSError, ValueError):
            if self.state['config'].get('lifecycle_mode') == 'on':
                raise
            self.state['role_dispatch_unverified'] = True
            return
        self.state['role_dispatch_manifest'] = manifest
        self.state['role_dispatch_manifest_version'] = 1
        self.state['role_dispatch_manifest_sha256'] = hashlib.sha256(json.dumps(
            manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _verify_frozen_role_dispatch(self) -> None:
        if self.state.get('role_dispatch_unverified'):
            raise RuntimeError('role dispatch manifest is unverified; lifecycle is unavailable')
        current = self._role_dispatch_manifest()
        digest = hashlib.sha256(json.dumps(current, sort_keys=True,
                                           separators=(',', ':')).encode()).hexdigest()
        if (current != self.state.get('role_dispatch_manifest') or
                digest != self.state.get('role_dispatch_manifest_sha256') or
                self.state.get('role_dispatch_manifest_version') != 1):
            raise RuntimeError('frozen role dispatch changed; abort or start a new run')
        if not current['role_flags']['author']['tmp_isolated'] and not self._fake_lifecycle:
            raise RuntimeError('lifecycle author TMP is not isolated from coordinator state')

    def reviewer_commands(self) -> list[str]:
        current = workitem_reviewer_commands(self.workitem.read_text())
        frozen = self.state.get('config', {}).get('workitem_reviewer_commands')
        if frozen is None:
            if self.args.action != 'permission-probe':
                reason = 'reviewer allowlist is not frozen; run permission-probe before continuing'
                self.hold(reason)
                raise RuntimeError(reason)
            self.state['config']['workitem_reviewer_commands'] = frozen = current
            self.save()
        if current != frozen:
            reason = 'work-item reviewer allowlist changed; run permission-probe before continuing'
            self.hold(reason)
            raise RuntimeError(reason)
        result = []
        for command in [self.args.test_command, *self.args.reviewer_command, *frozen]:
            if command not in result:
                result.append(command)
        return result

    def _program_state(self, hold=False):
        current, issue = program_snapshot(self.workspace, self.run_dir, self.author_temp_dir,
                                    self.args.codex_bin, self.args.claude_bin, self.args.gate_prompt, self.args.config)
        frozen = self.state.get('operator_programs')
        if issue is None and (frozen is None or self.args.action == 'permission-probe'):
            self.state['operator_programs'] = current
            self.save()
        elif frozen is not None and frozen != current:
            issue = 'configured operator program or PATH changed since permission probe'
        if issue and hold and self.state.get('status') != 'DONE': self.hold(issue)
        return current, issue
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
            flags['claude_bash_sandbox'] = self._claude_sandbox_settings('reviewer')
            flags['claude_os_denial_probe'] = str(self._claude_os_probe_path())
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
                'operator_programs': self._program_state()[0],
        }
        if self.args.author_vendor == 'codex':
            flags.update({
                'ignore_execpolicy_rules': True,
                **self._author_sandbox_overrides(),
                'author_escape_probe': 'codex-exec-model-filesystem-v2',
                'codex_cli_version': self._codex_cli_version(),
                'codex_config_sha256': self._codex_policy_digest(),
            })
        else:
            flags.update({'permission_mode': 'acceptEdits',
                          'claude_bash_sandbox': self._claude_sandbox_settings('author'),
                          'non_bash_run_state_edit_access': 'denied by Edit/Write path rules'})
        return flags

    def codex_capabilities(self, workspace=None) -> dict:
        return codex_capability_guard.inspect(self.global_codex_home, workspace or self.workspace)

    def _codex_cli_version(self) -> str:
        try:
            if self._program_state()[1]: return 'UNAVAILABLE'
            result = subprocess.run([self.state['operator_programs']['codex_bin']['path'], '--version'], text=True,
                                    capture_output=True, timeout=10)
            return result.stdout.strip() if result.returncode == 0 else 'UNAVAILABLE'
        except (OSError, subprocess.SubprocessError):
            return 'UNAVAILABLE'

    def _codex_policy_digest(self) -> Optional[str]:
        path = self.global_codex_home / 'config.toml'
        if not path.is_file(): return None
        roots = re.escape(str(self.workspace)) + '|' + re.escape(str(self.run_dir)) + r'/paired-session-author-probe-[^"]+/workspace'
        raw = re.sub(r'(?m)^\[projects\."(?:' + roots + r')"\]\ntrust_level = "trusted"\n', '', path.read_text())
        return hashlib.sha256('\n'.join(line for line in raw.splitlines() if line.strip()).encode()).hexdigest()

    def _author_sandbox_overrides(self) -> dict:
        return {
            'sandbox': 'workspace-write',
            'sandbox_workspace_write.writable_roots': [str(self.author_temp_dir)],
            'sandbox_workspace_write.exclude_tmpdir_env_var': False,
            'sandbox_workspace_write.exclude_slash_tmp': True,
            'sandbox_workspace_write.network_access': False,
            'TMPDIR': str(self.author_temp_dir),
        }

    def _author_sandbox_config_args(self) -> list[str]:
        overrides = self._author_sandbox_overrides()
        args = []
        for key, value in overrides.items():
            if key == 'TMPDIR':
                continue
            config_key = 'sandbox_mode' if key == 'sandbox' else key
            args.extend(['-c', f'{config_key}={json.dumps(value)}'])
        return args

    def _author_environment(self) -> dict:
        return {**os.environ, 'TMPDIR': str(self.author_temp_dir)}

    def _codex_sandbox_profile_args(self) -> list[str]:
        if self._codex_cli_version() != 'codex-cli 0.157.0':
            raise ValueError('unsupported codex sandbox contract; expected codex-cli 0.157.0')
        policy = self._author_sandbox_overrides()
        if (policy['sandbox'] != 'workspace-write' or policy['sandbox_workspace_write.network_access'] is not False
                or policy['sandbox_workspace_write.exclude_tmpdir_env_var'] is not False
                or policy['sandbox_workspace_write.exclude_slash_tmp'] is not True):
            raise ValueError('author sandbox parameters cannot be represented by the synthetic profile')
        roots = {'.': 'write', '.git': 'read', '.agents': 'read', '.codex': 'read', '.aws': 'read'}
        filesystem = {':root': 'read', ':tmpdir': 'write',
                      **{str(Path(root).resolve()): 'write' for root in policy['sandbox_workspace_write.writable_roots']},
                      ':workspace_roots': roots}
        inline = '{' + ', '.join(json.dumps(k) + '=' + ('{' + ', '.join(json.dumps(r) + '=' + json.dumps(v) for r, v in value.items()) + '}' if isinstance(value, dict) else json.dumps(value)) for k, value in filesystem.items()) + '}'
        return ['-P', 'paired_session_author', '--config', 'permissions.paired_session_author.filesystem=' + inline,
                '--config', 'permissions.paired_session_author.network.enabled=false']

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
                    'denyWrite': [str(self.run_dir)] + ([str(self._claude_os_probe_path())] if role == 'probe' and self._probe_sandbox_commands else []),
                },
            },
            'permissions': {
                'deny': [f'Edit(//{self.run_dir.as_posix().lstrip("/")}/**)'],
            },
        }

    def _claude_os_probe_path(self) -> Path: return self.run_dir.parent / ('.paired-session-os-probe-' + hashlib.sha256(str(self.run_dir).encode()).hexdigest()[:16])

    def author_flags_digest(self) -> str:
        raw = json.dumps(self.author_flags(), sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(raw).hexdigest()

    def probe_passed(self) -> tuple[bool, str]:
        if (issue := self._program_state()[1]): return False, issue
        path = self.run_dir / 'permission-probe.json'
        if not path.exists():
            return False, 'permission-probe.json is missing'
        try:
            report = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return False, 'permission-probe.json is unreadable'
        if report.get('status') not in ('PASS', 'PASS_RESIDUAL_RISK'):
            return False, 'permission probe status is not PASS'
        if report.get('reviewer_flags_digest') != self.reviewer_flags_digest():
            return False, 'permission probe reviewer flags do not match this run'
        if report.get('author_flags_digest') != self.author_flags_digest():
            return False, 'permission probe author flags do not match this run'
        author_probe = report.get('author_permission_probe', {})
        expected_author_status = report['status'] if self.args.author_vendor == 'codex' else 'NOT-APPLICABLE'
        if author_probe.get('status') != expected_author_status or (report['status'] == 'PASS_RESIDUAL_RISK' and (author_probe.get('d1a_model_verdict'), author_probe.get('d1b_synthetic_verdict')) != ('UNKNOWN', 'PASS')):
            return False, 'permission probe author permission status is not current'
        if report.get('global_config_changes', {}).get('status') != 'PASS':
            return False, 'permission probe global config changes were not fully attributed'
        return True, ''

    def _validate_resume_args(self) -> None:
        if Path(self.state['workspace']) != self.workspace or Path(self.state['workitem']) != self.workitem:
            raise ValueError('resume workspace/workitem differs from state')
        if getattr(self.args, 'resume_timeout', None) is not None and self.args.action != 'resume':
            raise ValueError('--resume-timeout is accepted only with resume')
        exec_timeout_override = getattr(self.args, 'exec_turn_timeout_explicit', False)
        if self.args.action == 'resume':
            original_timeout = self.state['config']['timeout']
            requested_timeout = getattr(self.args, 'resume_timeout', None)
            if requested_timeout is not None:
                if requested_timeout < original_timeout or requested_timeout > MAX_RESUME_TIMEOUT_SECONDS:
                    raise ValueError('--resume-timeout must be between the saved timeout and '
                                     f'{MAX_RESUME_TIMEOUT_SECONDS} seconds')
                self.args.timeout = requested_timeout
            saved_config = self.state.get('config', {})
            saved_exec_timeout = saved_config.get('exec_turn_timeout')
            if saved_exec_timeout is None:
                saved_exec_timeout = resolve_exec_turn_timeout(
                    None, saved_config.get('timeout', DEFAULT_EXEC_TURN_TIMEOUT_SECONDS))
            requested_exec_timeout = (getattr(self.args, 'exec_turn_timeout', None)
                                      if exec_timeout_override else None)
            if requested_exec_timeout is None:
                self.args.exec_turn_timeout = saved_exec_timeout
            else:
                requested_exec_timeout = resolve_exec_turn_timeout(
                    requested_exec_timeout, self.args.timeout)
                if requested_exec_timeout < saved_exec_timeout:
                    raise ValueError(f'--exec-turn-timeout cannot lower saved value {saved_exec_timeout}')
        else:
            requested_exec_timeout = getattr(self.args, 'exec_turn_timeout', None)
            self.args.exec_turn_timeout = resolve_exec_turn_timeout(
                requested_exec_timeout, self.args.timeout)
        current_config = self._config()
        for key, value in self.state['config'].items():
            if (key == 'timeout' and self.args.action == 'resume' and
                    getattr(self.args, 'resume_timeout', None) is not None):
                continue
            if key == 'exec_turn_timeout' and self.args.action == 'resume' and exec_timeout_override:
                continue
            current = current_config[key]
            if key == 'workitem_reviewer_commands' and current != value:
                if self.args.action == 'permission-probe':
                    self.state['config'][key] = current
                    self.save()
                    continue
                reason = 'work-item reviewer allowlist changed; run permission-probe before continuing'
                self.hold(reason)
                raise RuntimeError(reason)
            if current != value:
                raise ValueError('resume configuration differs: ' + key)

    def save(self) -> None:
        with self._save_lock:
            atomic_json(self.state_path, self.state)

    def fake_lifecycle_event(self, event, value):
        if any(self.state.get(key) for key in ('fake_candidate_pending', 'fake_ingest_receipt',
                                               'fake_candidate_chain', 'fake_route_consumed')):
            raise ValueError('chain-only lifecycle events are router-owned')
        self._fake_lifecycle_event(event, value)

    def _fake_lifecycle_event(self, event, value):
        if not self._fake_lifecycle or 'lifecycle' not in self.state:
            raise ValueError('fake lifecycle state is unavailable')
        action = {'begin': lifecycle_spine.begin, 'receipt': lifecycle_spine.complete}.get(event)
        if action is None: raise ValueError('unknown fake lifecycle event')
        if (event == 'begin' and self.state.get('q_reserved') and
                self.state['invocations_used'] + self.state['q_reserved'] + 2 > self.args.max_invocations):
            raise ValueError('P budget exhausted before stage begin; abort and start a new run '
                             'with larger --max-invocations')
        self.state['lifecycle'] = action(self.state['lifecycle'], value)
        self.save()

    def _verify_reserved_docs_replay(self, baseline, revision, hits):
        life = self.state['lifecycle']
        marker = life['reserved_docs_constraint']
        fail = 'reserved docs constraint requires replayed DOCS receipt and retest'
        saved = json.loads((self.evidence / (marker['id'] + '-reserved-docs.json')).read_text())
        rows = life['receipts'][marker['receipt_count']:]
        test = self.state.get('fake_candidate_test') or {}
        chain = self.state.get('fake_candidate_chain') or {}
        if (hits or saved != json.loads(json.dumps(marker)) or marker['epoch'] >= life['epoch'] or
                marker['docs_file'] != Path(self.state['config']['docs_file']).relative_to(self.workspace).as_posix() or
                not rows or test.get('oid') != revision.tree_oid or test.get('returncode') != 0 or
                test.get('epoch') != life['epoch'] or chain.get('oid') != revision.tree_oid or
                chain.get('test_id') != test.get('id')):
            raise ValueError(fail)
        stored_test = json.loads((self.evidence / (test['id'] + '-oid-test.json')).read_text())
        if stored_test != json.loads(json.dumps(test)):
            raise ValueError(fail)
        env = candidate_tree._git_env(GIT_DIR=str(baseline.git_dir))
        def blob(oid):
            return next(((mode, digest) for mode, digest, path in
                         candidate_tree._tree_entries(env, oid) if path == marker['docs_file']), None)
        oid, changed_docs = marker['source_oid'], None
        for row in rows:
            if (row.get('candidate_oid') != oid or row.get('status') not in ('READY', 'APPROVE') or
                    row.get('stage') == 'SECURITY'):
                raise ValueError(fail)
            if row.get('stage') != 'DOCS' and blob(oid) != blob(row['output_oid']):
                raise ValueError(fail)
            if (row.get('stage') == 'DOCS' and row.get('docs_file') == marker['docs_file'] and
                    row.get('retested_oid') == row.get('output_oid') and
                    blob(oid) != blob(row['output_oid'])):
                changed_docs = row
            oid = row.get('output_oid')
        reviews = [row for row in rows if row.get('stage') == 'EXEC' and
                   isinstance(row.get('approval_proof'), dict) and
                   row['approval_proof'].get('fake_only') is True and
                   row['approval_proof'].get('convergence_id') == chain.get('id') and
                   row.get('candidate_oid') == revision.tree_oid]
        if (oid != revision.tree_oid or not changed_docs or not reviews or
                blob(marker['source_oid']) == blob(revision.tree_oid) or
                blob(changed_docs['output_oid']) != blob(revision.tree_oid)):
            raise ValueError(fail)
        if marker['owner'] == 'coordinator' and not self._coord_doc_blockers(marker['docs_file'], marker['owner_ids']):
            raise ValueError(fail)
        if marker['owner'] == 'security-reviewer':
            owner = life.get('awaiting_owner_reverify') or {}
            if (owner.get('owner') != 'security-reviewer' or
                    tuple(owner.get('finding_ids', ())) != tuple(marker['owner_ids'])):
                raise ValueError(fail)
            owner.update(repair_oid=changed_docs['output_oid'], request_id=changed_docs['request_id'],
                         reserved_docs_replay=True)
        elif marker['owner'] != 'coordinator':
            raise ValueError(fail)

    def fake_reserved_docs_begin_replay(self):
        if not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args):
            raise ValueError('reserved DOCS replay requires fake-only dispatch')
        life = self.state['lifecycle']
        marker = life.get('reserved_docs_constraint') or {}
        source_owner = life.get('awaiting_owner_reverify') or {}
        source_rows = self._coord_doc_blockers(marker.get('docs_file'), marker.get('owner_ids', ()))
        owned = (marker.get('owner') == 'coordinator' and source_rows or
                 marker.get('owner') == source_owner.get('owner') == 'security-reviewer' and
                 tuple(marker.get('owner_ids', ())) == tuple(source_owner.get('finding_ids', ())))
        if (life['stage'] != 'SECURITY' or life['pending'] or not owned or
                marker.get('source_oid') != life['candidate_oid']):
            raise ValueError('reserved DOCS replay lacks coordinator-owned source')
        self.state['lifecycle'] = {**life, 'stage': 'DOCS'}
        self.save()

    def fake_lifecycle_route(self, approval, outputs=None, finish_context=None, stub_mode=False,
                             polish_context=None, polish_stub=False, docs_context=None, docs_stub=False,
                             security_context=None, chain_only=False):
        if not self._fake_lifecycle: raise ValueError('fake lifecycle router is unavailable')
        if chain_only:
            if not lifecycle_spine.fake_dispatch_guard(self.args):
                raise ValueError('chain-only route requires fake providers')
            if outputs:
                raise ValueError('chain-only route cannot accept caller output OIDs')
            if approval is not None:
                raise ValueError('chain-only route rejects caller approval')
            if (life := self.state['lifecycle'])['stage'] == 'EXEC' and life['epoch'] and not self._fake_reentry():
                return 'HOLD'
            if self.state['lifecycle']['stage'] == 'EXEC':
                approval = self.fake_candidate_approval()
                if not self.state['fake_ingest_receipt'].get('chain_only'):
                    raise ValueError('chain-only route requires a chain-only ingest')
                chain_id = approval['proof']['convergence_id']
                if self.state.get('fake_route_consumed') == chain_id:
                    raise ValueError('chain-only EXEC approval was already consumed')
                self.state['fake_route_consumed'] = chain_id
                self.save()
            else:
                if self.state['lifecycle']['stage'] == 'STOP_BEFORE_SECURITY' and security_context is None:
                    raise ValueError('chain-only continuation needs SECURITY context or unused persisted approval')
                prior = next((row for row in reversed(self.state['lifecycle']['receipts'])
                              if row['stage'] == 'EXEC'), None)
                proof = prior.get('approval_proof') if prior else None
                if (not isinstance(proof, dict) or proof.get('fake_only') is not True or
                        not self.state.get('fake_route_consumed') or
                        not (self.state.get('fake_ingest_receipt') or {}).get('chain_only') or
                        proof.get('convergence_id') != self.state['fake_route_consumed']):
                    raise ValueError('chain-only continuation lacks consumed EXEC proof')
        else:
            chain_pending = self.state.get('fake_candidate_pending') or {}
            chain_ingest = self.state.get('fake_ingest_receipt') or {}
            if (chain_pending.get('chain_only') or chain_ingest.get('chain_only') or
                    self.state.get('fake_candidate_chain') or self.state.get('fake_route_consumed')):
                raise ValueError('persisted chain forbids a hand-built approval')
            proof = approval.get('proof') if isinstance(approval, dict) else None
            if isinstance(proof, dict) and proof.get('fake_only'):
                raise ValueError('fake chain proof requires chain-only route')
        outputs = outputs or {}
        emit = self._fake_lifecycle_event if chain_only else self.fake_lifecycle_event
        start_epoch = self.state['lifecycle']['epoch']
        while True:
            life = self.state['lifecycle']
            stage = life['stage']
            owner_marker = life.get('awaiting_owner_reverify', {})
            owner_waiting = (owner_marker.get('owner') == 'security-reviewer' and
                             bool(owner_marker.get('finding_ids')) and not any(
                row['id'] not in owner_marker['finding_ids'] for row in self.blocking_open_findings()))
            blockers_allowed = owner_waiting or self._coordinator_reserved_waiting(life, stage, security_context)
            if stage == 'STOP_BEFORE_SECURITY' and security_context:
                if self.blocking_open_findings() and not blockers_allowed:
                    raise ValueError('SECURITY has an upstream blocker')
                self.state['lifecycle'] = {**life, 'stage': 'SECURITY'}
                self.save()
                continue
            if stage in ('STOP_BEFORE_SECURITY', 'STOP_BEFORE_DELIVERY') or (
                    stage == 'EXEC' and life['epoch'] != start_epoch):
                if stage == 'EXEC' and chain_only:
                    if not self._fake_reentry(): return 'HOLD'
                return stage
            last = life['receipts'][-1] if life['receipts'] else {}
            replay_unfinished = (stage == 'DOCS' and life.get('reserved_docs_constraint') and
                                 not str(last.get('request_id', '')).endswith('-reserved-replay'))
            if (not replay_unfinished and
                    not life['pending'] and last.get('stage') == stage and
                    last.get('epoch') == life['epoch'] and last.get('candidate_oid') == life['candidate_oid']):
                if stage == 'POLISH-Q' and self.blocking_open_findings() and not blockers_allowed:
                    raise ValueError('POLISH-Q has an open blocker')
                self.state['lifecycle'] = lifecycle_spine.advance(life)
                self.save()
                continue
            if stage == 'EXEC':
                expected = {'status': 'APPROVE', 'epoch': life['epoch'],
                            'item_uuid': life['item_uuid'], 'run_id': self.run_dir.name,
                            'parent': life['parent']}
                if not isinstance(approval, dict) or any(approval.get(k) != v for k, v in expected.items()):
                    raise ValueError('fake EXEC approval is stale or malformed')
                oid = approval.get('candidate_oid')
                if not isinstance(oid, str) or not oid or life['candidate_oid'] not in (None, oid):
                    raise ValueError('fake EXEC approval differs from current OID')
                if life['candidate_oid'] is None:
                    self.state['lifecycle'] = {**life, 'candidate_oid': oid}
                    self.save()
                    life = self.state['lifecycle']
            request = {key: life[key] for key in ('item_uuid', 'stage', 'epoch', 'candidate_oid', 'parent')}
            suffix = '-repair' if stage == 'SECURITY' and owner_waiting else ''
            if stage == 'DOCS' and life.get('reserved_docs_constraint'):
                suffix = '-reserved-replay'
            request.update(request_id=f"fake-{stage}-{life['epoch']}{suffix}",
                           role='reviewer' if stage == 'EXEC' else stage)
            if life['pending'] and life['pending'] != request:
                raise ValueError('fake stage has a different pending request')
            if stage == 'FINISH' and life['pending']:
                raise ValueError('uncertain FINISH request requires operator resolution')
            if stage == 'FINISH' and not stub_mode and not finish_context:
                raise ValueError('FINISH requires a candidate-bound dispatch context')
            if stage == 'POLISH-Q' and life['pending']:
                raise ValueError('uncertain POLISH-Q request requires operator resolution')
            if stage == 'POLISH-Q' and self.blocking_open_findings() and not blockers_allowed:
                raise ValueError('POLISH-Q has an open blocker')
            if stage == 'POLISH-Q' and life.get('polish_calls', 0) >= budget_policy.BUDGET_CAPS['POLISH-Q'][0]:
                raise ValueError('POLISH-Q specialist budget exhausted')
            if stage == 'POLISH-Q' and not stub_mode and not polish_stub and not polish_context:
                raise ValueError('POLISH-Q requires fake specialists')
            if stage == 'POLISH-Q' and not stub_mode and not polish_stub:
                names = tuple(sorted(polish_context))
                counts = life.get('specialist_counts', {})
                used = life.get('polish_calls', 0)
                if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', name) for name in names):
                    raise ValueError('invalid specialist owner')
                if (used + len(names) > budget_policy.BUDGET_CAPS['POLISH-Q'][0] or
                        any(counts.get(name, 0) >= budget_policy.BUDGET_CAPS['specialist'][0] for name in names) or
                        self.state['invocations_used'] + len(names) * (3 if self.state.get('q_reserved') else 1) +
                        self.state.get('q_reserved', 0) > self.args.max_invocations):
                    raise ValueError('POLISH-Q specialist budget exhausted')
            if stage == 'DOCS' and (life['pending'] or (self.blocking_open_findings() and not blockers_allowed)):
                raise ValueError('DOCS has an uncertain request or open blocker')
            if stage == 'DOCS' and not stub_mode and not docs_stub:
                if not docs_context: raise ValueError('DOCS requires a candidate-bound fake writer')
                baseline, before = docs_context['baseline'], docs_context['before']
                if (baseline.workspace != self.workspace or baseline.run_dir != self.run_dir or
                        baseline.parent_head != life['parent'] or before.tree_oid != life['candidate_oid']):
                    raise ValueError('DOCS candidate differs from persisted item state')
                prior = life['receipts'][-1]
                replaying = bool(life.get('reserved_docs_constraint') and prior['stage'] == 'DOCS')
                if (prior['stage'] != 'POLISH-Q' and not replaying or
                        prior['output_oid'] != before.tree_oid or
                        prior['epoch'] != life['epoch'] or prior['status'] != 'READY'):
                    raise ValueError('DOCS lacks current POLISH-Q receipt')
                if not self.state['config']['docs_file']: raise ValueError('DOCS requires a frozen docs_file')
                path = Path(self.state['config']['docs_file']).relative_to(self.workspace).as_posix()
                replay = next((row for row in reversed(life['receipts'])
                               if row.get('stage') == 'DOCS' and row.get('output_oid') == before.tree_oid and
                               row.get('docs_file') == path), None)
            if stage == 'SECURITY':
                if not security_context:
                    raise ValueError('SECURITY requires a candidate-bound fake reviewer')
                if life['pending'] or (self.blocking_open_findings() and not blockers_allowed):
                    raise ValueError('SECURITY has an uncertain request or open blocker')
                baseline, revision = security_context['baseline'], security_context['revision']
                if (baseline.workspace != self.workspace or baseline.run_dir != self.run_dir or
                        revision.tree_oid != life['candidate_oid'] or baseline.parent_head != life['parent']):
                    raise ValueError('SECURITY candidate differs from persisted item state')
                candidate_tree.verify_candidate_revision(baseline, revision)
                paths = sorted(path.relative_to(baseline.root).as_posix()
                               for path in baseline.root.rglob('*') if not path.is_dir() or path.is_symlink())
                hits = {path: kind for path in paths
                        if (kind := sensitive_policy.sensitive_path_category(path))}
                if life.get('reserved_docs_constraint'):
                    self._verify_reserved_docs_replay(baseline, revision, hits)
                    constraint = life['reserved_docs_constraint']
                    if constraint['owner'] == 'coordinator':
                        blockers_allowed = bool(self._close_reserved_docs(constraint['owner_ids'], life['epoch'] + 1))
                    self.state['lifecycle'].pop('reserved_docs_constraint')
                    self.save()
                marker = life.get('awaiting_owner_reverify')
                if marker and marker.get('owner') == 'security-reviewer':
                    if owner_waiting and marker.get('check_failures', 0) >= 2:
                        raise ValueError('owner check retry limit reached; abort')
                    if marker.get('role_manifest_sha256') != self.state['role_dispatch_manifest_sha256']:
                        raise ValueError('SECURITY reviewer owner identity changed')
                    if not marker.get('repair_oid') and not security_context.get('repair'):
                        raise ValueError('SECURITY reviewer blocker requires a repair')
                    if marker.get('repair_oid'):
                        repairs = [i for i, row in enumerate(life['receipts'])
                                   if row.get('stage') == 'SECURITY' and
                                   row.get('request_id') == marker.get('request_id') and
                                   row.get('output_oid') == marker['repair_oid'] and
                                   row.get('security_repair') == marker.get('repair_digest')]
                        if not repairs and marker.get('reserved_docs_replay'):
                            repairs = [i for i, row in enumerate(life['receipts'])
                                       if row.get('stage') == 'DOCS' and
                                       row.get('request_id') == marker['request_id'] and
                                       row.get('output_oid') == marker['repair_oid']]
                        if len(repairs) != 1:
                            raise ValueError('SECURITY reviewer owner requires bound repair receipt')
                        replay_oid = marker['repair_oid']
                        for row in life['receipts'][repairs[0] + 1:]:
                            if (row.get('stage') not in ('EXEC', 'FINISH', 'POLISH-Q', 'DOCS') or
                                    row.get('candidate_oid') != replay_oid):
                                raise ValueError('SECURITY owner replay lineage differs')
                            replay_oid = row['output_oid']
                        if replay_oid != revision.tree_oid:
                            raise ValueError('SECURITY owner replay OID differs')
                    if marker.get('repair_oid') and security_context.get('repair'):
                        raise ValueError('SECURITY reviewer repair already dispatched')
                elif marker:
                    if (marker['repair_oid'] not in [row.get('output_oid') for row in life['receipts']] or
                            marker['request_id'] not in [row.get('request_id') for row in life['receipts']]):
                        raise ValueError('SECURITY owner marker lacks repair lineage')
                    if marker['preflight_paths'] and hits:
                        raise ValueError('SECURITY preflight owner rescan still has sensitive paths')
                    if marker['ignore_sha256']:
                        ignore = baseline.root / '.gitignore'
                        if (ignore.is_symlink() or not ignore.is_file() or
                                ignore.stat().st_mode & 0o111 or
                                hashlib.sha256(ignore.read_bytes()).hexdigest() != marker['ignore_sha256']):
                            raise ValueError('SECURITY ignore owner byte check differs')
                    self.state['lifecycle'].pop('awaiting_owner_reverify')
                    self.save()
                repair = security_context.get('repair')
                if hits and not repair:
                    raise ValueError('SECURITY sensitive candidate paths: ' + ', '.join(hits))
                if repair:
                    if owner_waiting and (hits or repair['proposals']):
                        raise ValueError('SECURITY repair cannot combine owner groups')
                    allowed = marker['paths'] if owner_waiting else repair['allowed_paths']
                    docs_file = self.state['config'].get('docs_file')
                    docs_path = Path(docs_file).relative_to(self.workspace).as_posix() if docs_file else None
                    frozen_docs = {Path(value).relative_to(self.workspace).as_posix()
                                   for value in self.state['config']['docs_allowlist']}
                    reserved = tuple(sorted(frozen_docs | set(
                        security_repair_policy._paths(repair['reserved_docs']))))
                    try:
                        plan = security_repair_policy.plan_security_repair(
                            self.run_dir.name, revision.tree_oid, repair['proposals'], repair['paths'],
                            allowed, reserved, paths, repair.get('consent'))
                    except security_repair_policy.ReservedDocsRepair as exc:
                        if (hits or (not owner_waiting and repair.get('owner_finding_ids')) or
                                (owner_waiting and tuple(repair.get('owner_finding_ids', ())) !=
                                     tuple(marker.get('finding_ids', ()))) or not docs_path or
                                tuple(repair['paths']) != (docs_path,) or repair['proposals'] or
                                life.get('reserved_docs_constraint') or not blockers_allowed):
                            raise ValueError('reserved docs repair needs one frozen docs_file') from exc
                        constraint = {'id': str(uuid.uuid4()), 'source_oid': revision.tree_oid,
                                      'docs_file': docs_path, 'epoch': life['epoch'],
                                      'owner': 'security-reviewer' if owner_waiting else 'coordinator',
                                      'owner_ids': tuple(marker['finding_ids']) if owner_waiting else tuple(
                                          row['id'] for row in self._coord_doc_blockers(docs_path)),
                                      'receipt_count': len(life['receipts'])}
                        atomic_json(self.evidence / (constraint['id'] + '-reserved-docs.json'), constraint)
                        self.state['lifecycle']['reserved_docs_constraint'] = constraint
                        self.save()
                        raise ValueError('reserved docs repair requires replayed DOCS receipt and retest') from exc
                    if not set(hits) <= set(plan.fixer_paths):
                        raise ValueError('SECURITY repair does not cover sensitive paths')
                    if not hits and not plan.approved_patterns and not owner_waiting:
                        raise ValueError('SECURITY repair has no frozen coordinator-owned blocker')
            if stage == 'FINISH' and not stub_mode:
                baseline, revision = finish_context['baseline'], finish_context['revision']
                if (revision.tree_oid != life['candidate_oid'] or baseline.parent_head != life['parent'] or
                        baseline.run_dir != self.run_dir or baseline.workspace != self.workspace):
                    raise ValueError('FINISH candidate differs from persisted item state')
                prior = next((row for row in reversed(life['receipts'])
                              if row['stage'] == 'EXEC' and row['epoch'] == life['epoch']), None)
                if not prior or not isinstance(prior.get('approval_proof'), dict):
                    raise ValueError('FINISH lacks persisted EXEC approval proof')
            emit('begin', request)
            output = outputs.get(stage, life['candidate_oid'])
            receipt = {**request, 'status': 'APPROVE' if stage == 'EXEC' else 'READY', 'output_oid': output}
            if stage == 'EXEC': receipt['approval_proof'] = approval.get('proof')
            if stage == 'FINISH' and not stub_mode:
                result = finish_dispatch.dispatch(
                    baseline, revision, prior['approval_proof'], life['epoch'],
                    self.state['role_dispatch_manifest_sha256'],
                    finish_context['launch'], finish_context['sandbox_stopped'])
                output = result['output_oid']
                if result['status'] == 'TESTS_REQUIRED' and finish_context.get('tested_oid') != output:
                    raise ValueError('FINISH current-OID tests are missing')
                receipt.update(output_oid=output, finish_result=result['status'])
            if stage == 'POLISH-Q' and not stub_mode and not polish_stub:
                for name in names:
                    call = {'role': 'specialist:' + name, 'candidate_oid': life['candidate_oid'],
                            'epoch': life['epoch'], 'run_id': self.run_dir.name}
                    used += 1
                    counts[name] = counts.get(name, 0) + 1
                    self.state['invocations_used'] += 1
                    self.state['lifecycle'].update(polish_calls=used, specialist_counts=counts)
                    self.save()
                    result = polish_context[name](call)
                    if result.get('candidate_oid') != life['candidate_oid']:
                        raise ValueError('specialist result has a stale OID')
                    if any(str(finding.get('severity', '')).upper() not in
                           (ADVISORY_REVIEW_SEVERITIES | BLOCKING_REVIEW_SEVERITIES)
                           for finding in result.get('findings', [])):
                        raise ValueError('specialist result has an unknown severity')
                    self.record_findings('specialist:' + name, 'POLISH-Q', life['epoch'] + 1,
                                         result.get('findings', []))
                    if any(row['source'].startswith('specialist:') for row in self.blocking_open_findings()):
                        self.state['lifecycle']['hold_reason'] = 'open specialist blocker'
                        self.save()
                        raise ValueError('POLISH-Q has an open specialist blocker')
                    if result.get('status') != 'APPROVE':
                        raise ValueError('specialist did not approve current OID')
                receipt.update(specialists=names, polish_calls=used)
            if stage == 'DOCS' and not stub_mode and not docs_stub:
                docs_context['write'](baseline.root, path)
                after = candidate_tree.ingest_candidate_revision(baseline)
                polish_approval = {'candidate_oid': before.tree_oid, 'run_id': self.run_dir.name,
                                   'workspace': str(self.workspace), 'run_dir': str(self.run_dir),
                                   'parent_head': life['parent'], 'phase': 'POLISH-Q',
                                   'status': 'APPROVE', 'blocking_findings': [], 'epoch': life['epoch']}
                change = docs_policy.validate_candidate_docs_change(
                    baseline, before, polish_approval, after, [path], exec_paths=(),
                    finish_paths=(), polish_paths=(), closure_inputs=(), closure_uncertain=False,
                    replay_receipt=replay)
                retested_oid = (None if chain_only else
                                docs_context['test'](after.tree_oid) if change.requires_rechecks else None)
                if change.requires_rechecks and not chain_only and retested_oid != after.tree_oid:
                    raise ValueError('DOCS write lacks current-OID retest')
                receipt.update(output_oid=after.tree_oid, docs_file=path, docs_paths=change.paths,
                               invalidated=change.invalidated_receipts, retested_oid=retested_oid,
                               fake_only=True)
            if stage == 'SECURITY':
                if repair:
                    source_env = candidate_tree._git_env(GIT_DIR=str(baseline.git_dir))
                    source_entries = candidate_tree._tree_entries(source_env, revision.tree_oid)
                    ignore_entry = next((row for row in source_entries if row[2] == '.gitignore'), None)
                    if ignore_entry and ignore_entry[0] != '100644':
                        raise ValueError('SECURITY ignore source is not a regular 100644 file')
                    old_ignore = (candidate_tree._indexed_blobs(source_env, (ignore_entry,))[ignore_entry[1]]
                                  if ignore_entry else b'')
                    if plan.approved_patterns:
                        lines = old_ignore.decode('utf-8').split('\n')
                        missing = [pattern for _, pattern in plan.approved_patterns if pattern not in lines]
                        suffix = (b'\n' if old_ignore and not old_ignore.endswith(b'\n') and missing else b'')
                        suffix += b''.join((pattern + '\n').encode() for pattern in missing)
                        expected_ignore = old_ignore + suffix
                    repair['write'](baseline.root, plan.fixer_paths)
                    blobs = candidate_tree._indexed_blobs(source_env, source_entries)
                    source_files = {path: (mode, blobs[oid]) for mode, oid, path in source_entries}
                    seen_files = candidate_tree._candidate_files(baseline)
                    changed = {path for path in set(seen_files) | set(source_files)
                               if seen_files.get(path) != source_files.get(path)}
                    if not changed or not changed <= set(plan.fixer_paths):
                        raise ValueError('SECURITY writer changed an ungranted candidate path')
                    if plan.approved_patterns and seen_files.get('.gitignore') != ('100644', expected_ignore):
                        raise ValueError('SECURITY ignore writer changed bytes beyond approved patterns')
                    after = candidate_tree.ingest_candidate_revision(baseline)
                    if after.tree_oid == revision.tree_oid or candidate_tree._candidate_files(baseline) != seen_files:
                        raise ValueError('SECURITY repair made no change or changed during ingest')
                    candidate_tree.verify_candidate_revision(baseline, after)
                    if plan.approved_patterns:
                        current = candidate_tree._tree_entries(source_env, after.tree_oid)
                        entry = next((row for row in current if row[2] == '.gitignore'), None)
                        if (not entry or entry[0] != '100644' or
                                candidate_tree._indexed_blobs(source_env, (entry,))[entry[1]] != expected_ignore):
                            raise ValueError('SECURITY ingested ignore bytes differ from approval')
                    self.state['gate_ran'] = False
                    receipt.update(output_oid=after.tree_oid, security_repair=plan.digest,
                                   consent_digest=plan.consent_digest, approved_patterns=plan.approved_patterns,
                                   invalidated=plan.invalidated_receipts, fake_only=True)
                    self.state['lifecycle']['awaiting_owner_reverify'] = {
                        'owners': tuple(name for name, needed in (
                            ('preflight', bool(hits)), ('ignore', bool(plan.approved_patterns))) if needed),
                        'preflight_paths': tuple(sorted(hits)),
                        'ignore_sha256': (hashlib.sha256(expected_ignore).hexdigest()
                                          if plan.approved_patterns else None),
                        'source_oid': revision.tree_oid, 'repair_oid': after.tree_oid,
                        'request_id': request['request_id'], 'consent_digest': plan.consent_digest}
                    if owner_waiting:
                        self.state['lifecycle']['awaiting_owner_reverify'] = {
                            **marker, 'repair_oid': after.tree_oid, 'request_id': request['request_id'],
                            'repair_digest': plan.digest}
                    emit('receipt', receipt)
                    continue
                review = security_context['review']({'candidate_oid': revision.tree_oid, 'paths': paths,
                                                     'run_id': self.run_dir.name, 'epoch': life['epoch'],
                                                     'prior_finding_ids': tuple(marker['finding_ids'])
                                                     if owner_waiting else ()})
                candidate_tree.verify_candidate_revision(baseline, revision)
                if (not isinstance(review, dict) or not isinstance(review.get('findings'), list) or
                        any(not isinstance(row, dict) or
                            str(row.get('severity', '')).upper() not in
                            (ADVISORY_REVIEW_SEVERITIES | BLOCKING_REVIEW_SEVERITIES)
                            for row in review['findings'])):
                    raise ValueError('SECURITY reviewer result is malformed')
                if review.get('candidate_oid') != revision.tree_oid or not review.get('observed_tools'):
                    raise ValueError('SECURITY review lacks current-OID inspection evidence')
                if owner_waiting:
                    dispositions = review.get('dispositions')
                    frozen = set(marker['finding_ids'])
                    if (not isinstance(dispositions, list) or
                            len(dispositions) != len(frozen) or
                            {row.get('id') for row in dispositions if isinstance(row, dict)} != frozen or
                            any(not isinstance(row, dict) or row.get('disposition') not in
                                ('fixed', 'withdrawn', 'still_open') or not row.get('evidence')
                                for row in dispositions) or
                            review.get('status') != 'APPROVE' and any(
                                row.get('disposition') in ('fixed', 'withdrawn') for row in dispositions)):
                        marker = {**self.state['lifecycle'].get('awaiting_owner_reverify', marker),
                                  'check_failures': marker.get('check_failures', 0) + 1}
                        self.state['lifecycle'].update(awaiting_owner_reverify=marker, pending=None)
                        self.save()
                        raise ValueError('SECURITY owner disposition is missing; ' +
                                         ('retry' if marker['check_failures'] < 2 else 'retry limit reached; abort'))
                    missing = self.apply_dispositions(dispositions, life['epoch'] + 1,
                                                      list(frozen), 'security-reviewer')
                    if missing:
                        raise ValueError('SECURITY owner disposition omitted frozen IDs')
                recorded = self.record_findings('security-reviewer', 'SECURITY', life['epoch'] + 1,
                                                review.get('findings', []))
                direct_blocking = any(str(row['severity']).upper() in BLOCKING_REVIEW_SEVERITIES or
                                      row.get('security') for row in review['findings'])
                still_open = owner_waiting and any(
                    row['id'] in frozen for row in self.blocking_open_findings())
                if direct_blocking or still_open:
                    blockers = [row for row in self.blocking_open_findings()
                                if row['source'] == 'security-reviewer']
                    previous = tuple(marker.get('repair_history', ())) if owner_waiting else ()
                    history = previous + ((marker['repair_oid'], marker['request_id']),) if owner_waiting else ()
                    self.state['lifecycle']['awaiting_owner_reverify'] = {
                        'owner': 'security-reviewer', 'source_oid': revision.tree_oid,
                        'finding_ids': tuple(row['id'] for row in blockers),
                        'paths': tuple(row.get('file', '') for row in blockers),
                        'role_manifest_sha256': self.state['role_dispatch_manifest_sha256'],
                        'repair_history': history}
                    owner_hold = {**request, 'status': 'HOLD', 'finding_ids': tuple(row['id'] for row in blockers)}
                    atomic_json(self.evidence / (request['request_id'] + '-owner-hold.json'), owner_hold)
                    self.state['lifecycle']['owner_hold_receipt'] = owner_hold
                    self.state['lifecycle']['pending'] = None
                    self.save()
                    raise ValueError('SECURITY review did not approve current OID')
                if direct_blocking or self.blocking_open_findings() or review.get('status') != 'APPROVE':
                    raise ValueError('SECURITY review did not approve current OID')
                if owner_waiting:
                    self.state['lifecycle'].pop('awaiting_owner_reverify')
                receipt.update(output_oid=revision.tree_oid, security_review='APPROVE',
                               scanned_paths=paths, fake_only=True)
            emit('receipt', receipt)

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

    def capture_review_baseline(self, sequence: Optional[int] = None,
                                phase: Optional[str] = None) -> None:
        self._mirror_workspace(self.internal / 'last-review')
        reviewed = self.state.setdefault('reviewed_reviewer_sequences', [])
        if sequence is None or sequence not in reviewed:
            self.state['reviews_completed'] = self.state.get('reviews_completed', 0) + 1
            if sequence is not None:
                reviewed.append(sequence)
                if phase:
                    key = f'{phase.lower()}_reviews'
                    self.state[key] = self.state.get(key, 0) + 1
        self.save()

    def open_findings(self) -> list[dict]:
        return [finding for finding in self.state['finding_ledger'] if finding['status'] == 'open']

    def blocking_open_findings(self) -> list[dict]:
        return [finding for finding in self.open_findings()
                if finding['severity'] in BLOCKING_REVIEW_SEVERITIES or
                finding.get('security') or
                (finding['source'] == 'adversarial-gate' and finding['severity'] in ('CRITICAL', 'HIGH'))]

    def _coord_doc_blockers(self, docs_path, ids=None):
        blockers = self.blocking_open_findings()
        rows = [row for row in blockers if row['source'] == 'coordinator' and row['file'] == docs_path]
        return rows if rows and rows == blockers and (ids is None or [r['id'] for r in rows] == list(ids)) else []

    def _close_reserved_docs(self, ids, round_number):
        changes = [{'id': item, 'disposition': 'fixed', 'evidence': 'reserved docs replay verified'} for item in ids]
        self.apply_dispositions(changes, round_number, list(ids), 'coordinator')

    def _coordinator_reserved_waiting(self, life, stage, security_context):
        config = self.state['config']
        path = Path(config['docs_file']).relative_to(self.workspace).as_posix() if config.get('docs_file') else None
        repair = security_context.get('repair') if security_context else {}
        return bool(self._coord_doc_blockers(path) and (
            (life.get('reserved_docs_constraint') or {}).get('owner') == 'coordinator' or
            stage in ('STOP_BEFORE_SECURITY', 'SECURITY') and
            tuple(repair.get('paths', ())) == (path,) and not repair.get('proposals')))

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
                        findings: list[dict],
                        finding_indexes: Optional[list[int]] = None) -> list[dict]:
        specialist_owner = source.split(':', 1)[1] if source.startswith('specialist:') else ''
        if source.startswith('specialist:') and not re.fullmatch(r'[A-Za-z0-9_.-]+', specialist_owner):
            raise RuntimeError('specialist finding has no owning role')
        recorded = []
        existing = {row['finding_index']: row for row in self.state['finding_ledger']
                    if row.get('source') == source and row.get('phase') == phase and
                    row.get('origin_round') == origin_round and
                    type(row.get('finding_index')) is int}
        changed = False
        for index, finding in enumerate(findings):
            finding_index = finding_indexes[index] if finding_indexes is not None else index
            prior = existing.get(finding_index)
            if prior:
                recorded.append({'id': prior['id'], **finding})
                continue
            finding_id = f"F{self.state['next_finding_id']:03d}"
            self.state['next_finding_id'] += 1
            severity = str(finding['severity']).upper()
            summary = finding.get('summary') or finding.get('recommendation') or str(finding.get('body', '')).splitlines()[0]
            entry = {'id': finding_id, 'origin_round': origin_round, 'phase': phase,
                     'source': source, 'finding_index': finding_index,
                     'severity': severity, 'file': finding.get('file', ''),
                     'summary': summary, 'status': 'open',
                     'security': bool(finding.get('security')),
                     'failure_scenario': finding.get('failure_scenario', ''),
                     'body': finding.get('body', ''),
                     'status_history': [{'round': origin_round, 'status': 'open',
                                        'evidence': 'program assigned identity'}]}
            if source.startswith('specialist:'):
                entry['owner_role'] = 'specialist:' + specialist_owner
            if source == 'security-reviewer': entry['owner_role'] = 'security-reviewer'
            self.state['finding_ledger'].append(entry)
            recorded.append({'id': finding_id, **finding})
            changed = True
        if changed:
            self.write_ledger()
        return recorded

    def apply_dispositions(self, dispositions: list[dict], origin_round: int,
                           expected_ids: Optional[list[str]] = None,
                           owner_role: Optional[str] = None) -> list[str]:
        open_by_id = {finding['id']: finding for finding in self.open_findings()}
        all_by_id = {finding['id']: finding for finding in self.state['finding_ledger']}
        supplied = [row.get('id') for row in dispositions]
        expected = set(expected_ids) if expected_ids is not None else set(open_by_id)
        missing = sorted(expected - set(supplied))
        if len(supplied) != len(set(supplied)):
            raise RuntimeError('invalid finding dispositions: duplicate or unknown id')
        if missing:
            return missing
        for row in dispositions:
            finding = all_by_id.get(row['id'])
            if finding is None or row['id'] not in expected:
                raise RuntimeError('invalid finding dispositions: duplicate or unknown id')
            if ('owner_role' in finding and finding['owner_role'] != owner_role and
                    row['disposition'] in ('fixed', 'withdrawn')):
                raise RuntimeError('finding disposition requires its owning specialist')
            if finding['id'] not in open_by_id and not any(
                    item.get('round') == origin_round and item.get('status') == row['disposition']
                    and item.get('evidence') == row['evidence']
                    for item in finding.get('status_history', [])):
                raise RuntimeError('invalid finding dispositions: duplicate or unknown id')
        changed = False
        for row in dispositions:
            finding = all_by_id[row['id']]
            disposition = row['disposition']
            if any(item.get('round') == origin_round and item.get('status') == disposition
                   and item.get('evidence') == row['evidence']
                   for item in finding.get('status_history', [])):
                continue
            finding['status_history'].append({'round': origin_round, 'status': disposition,
                                              'evidence': row['evidence']})
            if disposition in ('fixed', 'withdrawn'):
                finding['status'] = disposition
            changed = True
        if changed:
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
                     finding['severity'], finding['file'], finding['summary'],
                     finding['status'] + (' (advisory)' if finding.get('advisory') else ''), history]
            lines.append('| ' + ' | '.join(str(cell).replace('|', '\\|').replace('\n', ' ') for cell in cells) + ' |')
        atomic_text(self.run_dir / 'findings-ledger.md', '\n'.join(lines) + '\n')

    def nonblocking_open_findings(self) -> list[dict]:
        return [row for row in self.open_findings()
                if row['severity'] in ADVISORY_REVIEW_SEVERITIES or
                (row['source'] == 'adversarial-gate' and row['severity'] in ('MEDIUM', 'LOW'))]

    @staticmethod
    def findings_are_advisory(findings: list[dict]) -> bool:
        return bool(findings) and all(str(row.get('severity', '')).upper() in
                                      ADVISORY_REVIEW_SEVERITIES and not row.get('security')
                                      for row in findings)

    def record_review_verdict(self, sequence: int, phase: str, raw: str, effective: str) -> None:
        records = self.state.setdefault('review_verdicts', [])
        row = next((item for item in records if item.get('sequence') == sequence), None)
        data = {'sequence': sequence, 'phase': phase, 'reviewer_raw_verdict': raw,
                'effective_verdict': effective}
        row.update(data) if row else records.append(data)

    def mark_advisory_findings(self, findings: list[dict]) -> None:
        ids = {row['id'] for row in findings if row.get('id')}
        for row in self.state['finding_ledger']:
            if row['id'] in ids and row['status'] == 'open':
                row['advisory'] = True
        self.write_ledger()

    def configured_test_failed(self, answer: dict) -> bool:
        return any(command_invokes_test(row.get('command', ''), self.args.test_command)
                   and not observed_test_succeeded(row, self.args.test_command)
                   for row in answer.get('observed_commands', []))

    def configured_test_succeeded(self, answer: dict) -> bool:
        return any(observed_test_succeeded(row, self.args.test_command)
                   for row in answer.get('observed_commands', []))

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
                 '| ID | Source | Severity | File | Summary | Status | Author response |',
                 '|---|---|---|---|---|---|---|']
        for row in rows:
            response = row.get('author_disposition', '')
            if row.get('author_reason'):
                response += (': ' if response else '') + row['author_reason']
            status = row['status'] + (' (advisory)' if row.get('advisory') else '')
            cells = [row['id'], row['source'], row['severity'], row['file'], row['summary'], status, response]
            lines.append('| ' + ' | '.join(str(cell).replace('|', '\\|').replace('\n', ' ')
                                           for cell in cells) + ' |')
        if not rows:
            lines += ['', 'No open or declined findings.']
        atomic_text(self.run_dir / 'open-findings.md', '\n'.join(lines) + '\n')

    @staticmethod
    def comparison_findings(answer: dict) -> list[dict]:
        rows = answer.get('full_review', answer.get('findings', []))
        return [{'severity': str(row.get('severity', '')).upper(),
                 'security': bool(row.get('security')), 'file': row.get('file', ''),
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
        if self.state.get('review_verdicts'):
            lines += ['', '## Raw and effective reviewer verdicts', '',
                      '| Phase | Sequence | Reviewer raw | Effective reviewer decision |', '|---|---:|---|---|']
            lines.extend(f"| {row['phase']} | {row['sequence']} | {row['reviewer_raw_verdict']} | {row['effective_verdict']} |"
                         for row in self.state['review_verdicts'])
        lines += ['', f"Final coordinator status: **{self.state['status']}**"]
        lines.append('Acceptance state: **' + self.state.get('acceptance_state', 'IN_PROGRESS') + '**')
        if self.state.get('residual_risk'): lines.append('Residual risk: ' + self.state['residual_risk'])
        if self.state.get('pending_operator_note_id'):
            lines.append('Operator note: undelivered ' + self.state['pending_operator_note_id'])
        if self.state.get('hold_reason'):
            lines.append('Hold reason: ' + self.state['hold_reason'])
        atomic_text(self.run_dir / 'review-comparison.md', '\n'.join(lines) + '\n')

    def hold(self, reason: str, terminal_kind: Optional[str] = None) -> str:
        if self.state.get('status') in ('ACCEPTED', 'ABORTED'):
            return self.state['status']
        if reason == 'rejected-tree':
            author = next((r for r in reversed(self.state['turns']) if r.get('role') == 'author'), {})
            self.state['rejected_tree_hold'] = {'tree_sha256': git_snapshot(self.workspace)[0],
                'rationale': str(author.get('answer', {}).get('body', ''))[:2000],
                'rationale_evidence': {'run_dir': str(self.run_dir), 'turn_sequence': author.get('sequence')}}
            self.state.update(next='author', pending_author_result_sequence=None, pending_reviewer_result_sequence=None)
            reason += '; note, change the workspace, or accept --override-rejection --reason TEXT'
            if terminal_kind == 'rejection_limit': reason += '; post-DONE rejection limit reached; accept or abort'
        keep_rejection_limit = (self.state.get('status') == 'HOLD' and
                                self.state.get('terminal_hold_kind') == 'rejection_limit')
        self.set_effective_verdict('HOLD')
        if self.state.get('active'):
            self.state['uncertain_active'] = self.state['active']
        self.state['status'] = 'HOLD'
        self.state['hold_reason'] = reason
        if terminal_kind:
            self.state['terminal_hold_kind'] = terminal_kind
        elif not keep_rejection_limit:
            self.state.pop('terminal_hold_kind', None)
        self.state['active'] = None
        self.save()
        self.write_ledger()
        self.write_comparison()
        self.write_open_findings()
        self.write_usage()
        return 'HOLD'

    def rejection_limit_hold(self) -> str:
        maximum = self.state.get('max_rejections', DEFAULT_MAX_REJECTIONS)
        return self.hold('rejected-tree', terminal_kind='rejection_limit') if self.rejected_tree() else 'HOLD'

    def scope_change(self, text: Optional[str], file: Optional[str]) -> str:
        intent = self.state.get('scope_change_intent')
        target = self.run_dir.with_name(self.run_dir.name + '-successor')
        task_path = self.evidence / 'successor-workitem.md'; config_path = self.evidence / 'successor-config.json'
        common = ['--workspace', str(self.workspace), '--workitem', str(task_path), '--run-dir', str(target),
                  '--config', str(config_path), '--supersedes', str(self.run_dir)]
        if self.state['config'].get('exercise_revisions'): common.append('--exercise-revisions')
        entry = str(HERE.parent / 'bin/paired-session')
        command = lambda: 'Probe: ' + shlex.join([entry, 'permission-probe', *common]) + '\nStart: ' + shlex.join([entry, 'run', *common])
        if self.state['status'] == 'ABORTED':
            if not intent or intent['action'] != self.args.action or intent['source'] != (str(Path(file).expanduser().resolve()) if file else 'cli') or (text is not None and text != intent['text']): raise ValueError('different scope-change request')
            if not (self.run_dir / 'scope-change-report.md').is_file():
                self._write_scope_change_report(spec_path)
            return command()
        if not intent:
            allowed = ((self.args.action == 'note' and self.state['status'] in ('ACTIVE', 'HOLD') and
                        self.state.get('terminal_hold_kind') != 'rejection_limit') or
                       (self.args.action == 'reject' and (self.state['status'] == 'DONE' or
                        self.state.get('terminal_hold_kind') == 'rejection_limit')))
            if not allowed or self.state.get('active') or self.state.get('uncertain_active'):
                raise ValueError('scope change requires an idle non-terminal run or pending acceptance')
            if self.state.get('scope_chain_depth', 0) >= 1 or bool(text) == bool(file): raise ValueError('scope chain limit or note arguments invalid')
            note = Path(file).expanduser().read_bytes().decode('utf-8') if file else text or ''
            if not note.strip() or '```reviewer-commands' in note: raise ValueError('empty or command-bearing scope note')
            raw = json.loads(self.state_path.read_text())
            if not raw.get('base_commit') or raw.get('base_commit_backfilled'): raise ValueError('parent has no trustworthy base_commit')
            task = self.workitem.read_text() + '\n\n## Operator scope change\n' + note + '\n'
            if target.exists():
                raise ValueError('successor run dir already exists')
            with tempfile.TemporaryDirectory() as tmp:
                trial = copy.copy(self); trial.workitem = Path(tmp) / 'task.md'; trial.workitem.write_text(task)
                trial.context = Path(tmp); trial.evidence = Path(tmp); trial.run_dir = target
                trial.author_temp_dir = target / 'author-tmp'; trial.state = {'sequence': 0, 'finding_ledger': []}
                try: trial.assert_fresh_prompt('shadow', 'Clean prompt')
                except RuntimeError as exc: raise ValueError('fresh-role input scan: ' + str(exc)) from exc
            intent = {'text': note, 'sha256': hashlib.sha256(note.encode()).hexdigest(),
                      'task_sha256': hashlib.sha256(task.encode()).hexdigest(),
                      'action': self.args.action, 'source': str(Path(file).expanduser().resolve()) if file else 'cli',
                      'author': 'operator', 'timestamp': datetime.now().astimezone().isoformat()}
            self.state['scope_change_intent'] = intent; self.save()
        elif intent['action'] != self.args.action or intent['source'] != (str(Path(file).expanduser().resolve()) if file else 'cli') or (text is not None and text != intent['text']):
            raise ValueError('different scope-change request is pending')
        task = self.workitem.read_text() + '\n\n## Operator scope change\n' + intent['text'] + '\n'
        if hashlib.sha256(task.encode()).hexdigest() != intent.get('task_sha256'):
            raise ValueError('scope-change task changed after intent')
        atomic_text(self.evidence / 'scope-note.txt', intent['text']); atomic_text(task_path, task)
        atomic_json(config_path, {k: v for k, v in self.state['config'].items() if k in CONFIGURABLE_DESTS and v is not None and
                    not (k == 'gate_prompt' and str(v).startswith('<bundled-default>:'))})
        spec = {'run_dir': str(target), 'workspace': str(self.workspace), 'original_workitem': str(self.workitem),
                'original_hash': hashlib.sha256(self.workitem.read_bytes()).hexdigest(),
                'task_sha256': hashlib.sha256(task.encode()).hexdigest(), 'base_commit': self.state['base_commit'],
                'task': task, 'note_sha256': intent['sha256'],
                'config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
                'item_uuid': self.state['item_uuid'],
                'item_blockers': pending_item_blockers(self.state, self.run_dir),
                'item_blockers_complete': bool(self.state.get('item_blockers_complete'))}
        spec_path = self.evidence / 'successor-spec.json'; atomic_json(spec_path, spec)
        self._write_scope_change_report(spec_path)
        self.state.update(status='ABORTED', abort_kind='scope-change',
                          successor_spec_sha256=hashlib.sha256(spec_path.read_bytes()).hexdigest())
        self.save(); self.write_comparison(); self.write_open_findings(); self.write_usage()
        return command()

    def _write_scope_change_report(self, spec_path: Path) -> None:
        atomic_text(self.run_dir / 'scope-change-report.md', 'Superseded run; turns: ' + str(len(self.state['turns'])) +
                    '\nSuperseded acceptance: ' + self.state.get('acceptance_state', 'IN_PROGRESS') +
                    '\nOpen findings: ' + ', '.join(row['id'] for row in self.open_findings()) +
                    '\nSuccessor: ' + str(spec_path) + '\n')

    def note(self, text: Optional[str], file: Optional[str]) -> str:
        if self.state['status'] != 'HOLD':
            raise ValueError('note requires a HOLD run; DONE uses reject')
        if self.state.get('terminal_hold_kind') == 'rejection_limit' and not self.state.get('rejected_tree_hold'):
            raise ValueError('rejection limit: accept, abort or use --scope-change')
        if self.state.get('next') != 'author':
            raise ValueError(f"run is waiting for {self.state.get('next')}; resume first, or use --scope-change (not yet available; abort + new run)")
        if self.state.get('pending_author_result_sequence') is not None:
            raise ValueError('author READY receipt pending; resume first')
        phase = self.state['phase']
        if (phase not in ('PLAN', 'EXEC') or self.state['polish']['active'] or self.state.get('active') or
                self.state.get('uncertain_active') or self.state['invocations_used'] >= self.args.max_invocations or
                (phase == 'PLAN' and self.state['plan_rounds'] >= self.args.max_plan_rounds) or
                (phase == 'EXEC' and self.state['exec_rounds'] >= self.exec_round_limit())):
            raise ValueError('next author turn is unavailable; note remains undelivered')
        if bool(text) == bool(file):
            raise ValueError('note requires exactly one of --text or --file')
        if file:
            source = Path(file).expanduser().resolve()
            if source == self.workspace or self.workspace in source.parents or source == self.run_dir or self.run_dir in source.parents:
                raise ValueError('note source must be outside workspace and run dir')
            try:
                with source.open('rb') as stream:
                    raw = stream.read(16385)
            except OSError as exc:
                raise ValueError('cannot read note source') from exc
        else:
            raw = (text or '').encode('utf-8')
        try: decoded = raw.decode('utf-8')
        except UnicodeDecodeError as exc: raise ValueError('note must be UTF-8') from exc
        if not raw or len(raw) > 16384 or not decoded.strip():
            raise ValueError('note must be nonempty UTF-8 and at most 16384 bytes')
        notes = self.state.setdefault('operator_notes', [])
        note_id = f'N{len(notes) + 1:03d}'
        previous = next((row for row in notes if row['id'] == self.state.get('pending_operator_note_id')), None)
        path = self.evidence / f'operator-note-{note_id}.txt'; atomic_text(path, decoded)
        if previous:
            previous.update(status='replaced', replaced_by=note_id)
        row = {'id': note_id, 'author': 'operator', 'timestamp': datetime.now().astimezone().isoformat(),
               'target_phase': phase, 'sha256': hashlib.sha256(raw).hexdigest(), 'evidence': str(path),
               'status': 'pending', 'replaces_id': previous['id'] if previous else None,
               'replaces_sha256': previous['sha256'] if previous else None}
        notes.append(row); self.state['pending_operator_note_id'] = note_id
        self.state.pop('terminal_hold_kind', None)
        self.save(); self.write_comparison()
        return note_id

    def accept(self) -> str:
        if self.state.get('status') == 'ACCEPTED' and not self.args.override_rejection:
            return 'ACCEPTED'
        if self._fake_lifecycle and self.state.get('fake_delivery_intent'):
            return delivery_intent.accept(self, observed_test_succeeded, atomic_json)
        if self.state.get('status') != 'DONE' and not (
                self.state.get('status') == 'HOLD' and
                (self.state.get('terminal_hold_kind') == 'rejection_limit' or self.args.override_rejection)):
            raise ValueError('accept requires a DONE run')
        record = {'author': 'operator', 'timestamp': datetime.now().astimezone().isoformat(),
              'intent': self.operator_intent('accept', None, None, self.args.expect, not self.args.override_rejection),
                  'accepted_state': self.state['status'], 'acceptance_state': 'ACCEPTED'}
        record.update(reason=self.args.reason, override_rejection=self.args.override_rejection,
                      rationale=self.state.get('rejected_tree_hold', {}).get('rationale_evidence'))
        self.state.setdefault('events', []).append(record)
        evidence_path = self.evidence / 'acceptance.json'
        record['evidence'] = str(evidence_path)
        atomic_json(evidence_path, record)
        self.state['acceptance'] = record
        self.state['acceptance_state'] = 'ACCEPTED'
        self.state['status'] = 'ACCEPTED'
        self.state.pop('terminal_hold_kind', None)
        self.state['accepted_at'] = record['timestamp']
        self.save()
        self.write_comparison()
        return 'ACCEPTED'

    def operator_intent(self, action, text, file, expected=None, required=False) -> dict:
        if action not in ('accept', 'reject'): raise ValueError('intent action must be accept or reject')
        payload = (Path(file).expanduser().read_text() if file else text or self.args.reason or '').encode()
        index = self.workspace / self._git(['rev-parse', '--git-path', 'index']).strip()
        tree_sha, tree_snapshot = git_snapshot(self.workspace)
        data = {'action': action, 'uid': os.getuid(), 'run_id': str(self.run_dir), 'item_uuid': self.state['item_uuid'],
                'workspace': str(self.workspace), 'workitem': str(self.workitem), 'head': self._head_commit(),
                'tree_sha256': tree_sha, 'workitem_sha256': hashlib.sha256(self.workitem.read_bytes()).hexdigest(),
                'index_sha256': hashlib.sha256(index.read_bytes()).hexdigest(),
                'state_sha256': hashlib.sha256(json.dumps(self.state, sort_keys=True).encode()).hexdigest(),
                'payload_sha256': hashlib.sha256(payload).hexdigest()}
        data['digest'] = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        override = self.args.override_rejection
        if override and (action != 'accept' or not (self.args.reason or '').strip() or
                self.state['status'] != 'HOLD' or not self.state.get('hold_reason', '').startswith('rejected-tree') or
                self.state.get('active') or self.state.get('uncertain_active') or
                tree_sha != self.state.get('rejected_tree_hold', {}).get('tree_sha256')):
            raise ValueError('override requires HOLD rejected-tree, unchanged held tree, and non-empty --reason')
        if not override: self.refuse_rejected_tree(stale_done=True)
        if required and (not expected or expected != data['digest']): raise ValueError('intent is stale or missing')
        return {**data, 'tree_snapshot': tree_snapshot}
    def rejected_tree(self, digest: Optional[str] = None) -> bool:
        return (git_snapshot(self.workspace)[0] if digest is None else digest) in self.state['rejected_digests']
    def refuse_rejected_tree(self, stale_done: bool = False, allow_author: bool = False) -> None:
        digest, manifest = git_snapshot(self.workspace)
        if allow_author and not self.rejected_tree(digest): self.state.pop('terminal_hold_kind', None)
        if not allow_author and self.rejected_tree(digest):
            self.hold('rejected-tree', terminal_kind=self.state.get('terminal_hold_kind'))
            raise ValueError(self.state['hold_reason'])
        if (stale_done and not allow_author and self.state.get('acceptance_state') == 'PENDING'
                and not self.state['polish']['active'] and self.state['approved_snapshot'] != digest):
            old, new = dict(self.state.get('approved_manifest', [])), dict(manifest)
            changed = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
            tracked = set(subprocess.check_output(['git', 'ls-files', '-z'], cwd=self.workspace).decode().split('\0'))
            raise ValueError(f'stale: {len(changed & tracked)} tracked, {len(changed - tracked)} untracked drift; '
                             'restore the approved tree or start a new run')
    def reject(self, text: Optional[str], file: Optional[str]) -> str:
        if self._fake_lifecycle and self.state.get('fake_delivery_intent'):
            raise ValueError('lifecycle reject not wired; abort/new run or use --scope-change')
        if self.state.get('status') != 'DONE':
            raise ValueError('reject requires a DONE run')
        if bool(text) == bool(file):
            raise ValueError('reject requires exactly one of --text or --file')
        intent = self.operator_intent('reject', text, file, self.args.expect, required=True)
        if file:
            feedback = Path(file).expanduser().read_text()
            source = str(Path(file).expanduser().resolve())
        else:
            feedback = text or ''
            source = 'command-line text'
        if not feedback.strip():
            raise ValueError('rejection note must not be empty')
        rejections = self.state.setdefault('rejections', [])
        maximum = self.state.setdefault('max_rejections', DEFAULT_MAX_REJECTIONS)
        self.state['rejected_digests'].append(intent['tree_sha256'])
        if len(rejections) >= maximum:
            return self.hold('rejected-tree', terminal_kind='rejection_limit')
        rejection_id = f'R{len(rejections) + 1:03d}'
        record = {'id': rejection_id, 'author': 'operator',
                  'timestamp': datetime.now().astimezone().isoformat(), 'target_phase': 'EXEC',
                  'intent': intent,
                  'sha256': hashlib.sha256(feedback.encode('utf-8')).hexdigest(),
                  'source': source, 'text': feedback, 'status': 'pending'}
        evidence_path = self.evidence / f'rejection-{rejection_id}.json'
        record['evidence'] = str(evidence_path)
        atomic_json(evidence_path, record)
        rejections.append(record)
        self.state['acceptance_state'] = 'REJECTED'
        self.state['pending_rejection_id'] = rejection_id
        self.state.update(status='ACTIVE', phase='EXEC', next='author', gate_ran=False,
                          delivered_review='')
        self.state['force_gate_after_reject'] = True
        self.save()
        self.write_comparison()
        return 'ACTIVE'

    def archive_abandoned_turn(self, receipt: dict) -> None:
        if receipt.get('vendor') == 'claude' and receipt.get('global_claude_before'):
            current = global_config_snapshot(self.global_config_home, self.global_codex_home)
            if receipt.get('global_config_home') != str(self.global_config_home): self.hold('global Claude config home changed during uncertain turn'); raise RuntimeError('global Claude config home changed during uncertain turn')
            changed = {key: current.get(key, {}).get('sha256') for key, value in receipt['global_claude_before'].items() if current.get(key, {}).get('sha256') != value}
            if changed:
                pending = {'sequence': receipt['sequence'], 'after': changed}
                if self.args.action == 'resume' and self.state.get('claude_uncertain_config_change') == pending:
                    receipt['global_claude_ack'] = {'operator_uid': os.getuid(), 'timestamp': datetime.now().astimezone().isoformat(), 'before': receipt['global_claude_before'], 'after': changed}; self.state.pop('claude_uncertain_config_change', None)
                else: self.state['claude_uncertain_config_change'] = pending; self.hold('global Claude config changed during uncertain turn; inspect, then resume --retry-uncertain'); raise RuntimeError('global Claude config changed during uncertain turn; inspect, then resume --retry-uncertain')
        if receipt.get('vendor') == 'codex' and receipt.get('global_codex_before'):
            current = global_config_snapshot(self.global_config_home, self.global_codex_home)
            if (receipt.get('global_codex_home') != str(self.global_codex_home) or receipt.get('global_config_home') != str(self.global_config_home) or any(current.get(key, {}).get('sha256') != value for key, value in receipt['global_codex_before'].items())):
                allowed = (self.args.action == 'resume' and self.args.acknowledge_codex_trust == self.run_dir.name
                           and current['codex_config']['sha256'] != receipt['global_codex_before']['codex_config']
                           and all(current.get(key, {}).get('sha256') == value for key, value in receipt['global_codex_before'].items() if key != 'codex_config')
                           and receipt.get('global_codex_home') == str(self.global_codex_home)
                           and trust_entry_only_since_hash(self.global_codex_home / 'config.toml', receipt['global_codex_before']['codex_config'], Path(receipt['workspace'])))
                if not allowed: self.hold('global Codex config changed during uncertain turn; inspect, then resume --acknowledge-codex-trust ' + self.run_dir.name); raise RuntimeError('global Codex config changed during uncertain turn; inspect, then resume --acknowledge-codex-trust ' + self.run_dir.name)
                self.state.setdefault('codex_trust_acknowledgments', []).append({'sequence': receipt['sequence'], 'operator_uid': os.getuid(), 'run_id': self.run_dir.name, 'timestamp': datetime.now().astimezone().isoformat(), 'workspace': receipt['workspace'], 'before': receipt['global_codex_before']['codex_config'], 'after': current['codex_config']['sha256']})
        sequence = receipt.get('sequence')
        rows = self.state.setdefault('abandoned_turns', [])
        if sequence is not None and any(row.get('sequence') == sequence for row in rows):
            return
        usage = receipt.get('usage_requests', [])
        provider_usage = 'reported' if usage else 'unknown'
        row = {**receipt, 'recovered_at': time.time(), 'group_gone': True,
               'provider_usage': provider_usage}
        row['model_requests'] = len(usage)
        rows.append(row)
        self.state.setdefault('usage_reconciliation', []).append({
            'sequence': sequence, 'role': receipt.get('role'), 'phase': receipt.get('phase'),
            'source': 'abandoned_turn', 'provider_usage': provider_usage,
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
        self.state['approved_snapshot'], self.state['approved_manifest'] = git_snapshot(self.workspace)
        self.set_effective_verdict('APPROVE')
        self.state['status'] = 'DONE'
        self.state['acceptance_state'] = 'PENDING'
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
        if self._fake_lifecycle:
            self._freeze_fake_exec_source()
            return self.hold('fake lifecycle has reviewed EXEC; router binding is pending')
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
            for finding_index, finding in enumerate(receipt.get('answer', {}).get('findings', [])):
                if str(finding.get('severity', '')).lower() not in ('medium', 'low'):
                    continue
                summary = finding.get('recommendation') or str(finding.get('body', '')).splitlines()[0]
                signature = ('adversarial-gate', finding.get('file', ''), summary)
                if signature not in existing:
                    self.record_findings('adversarial-gate', 'EXEC', receipt.get('sequence', 0),
                                         [finding], finding_indexes=[finding_index])
                    existing.add(signature)
                else:
                    row = next(item for item in self.state['finding_ledger']
                               if (item['source'], item['file'], item['summary']) == signature)
                    if not row.get('body'):
                        row['body'] = finding.get('body', '')
        self.write_ledger()

    def resume_polish(self) -> str:
        if self.state['status'] == 'ACCEPTED': return 'ACCEPTED'
        if self.state.get('status') == 'HOLD' and self.state.get('terminal_hold_kind') == 'rejection_limit':
            return self.rejection_limit_hold()
        self.refuse_rejected_tree(stale_done=True)
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
            allowed = ['Read,Grep,Glob', *(f'Bash({command})' for command in exact_commands)]
            cmd += ['--restricted', '--permission-mode', 'dontAsk',
                    '--tools', 'Read,Grep,Glob,Bash']
            for rule in allowed:
                cmd += ['--allowedTools', rule]
            cmd += ['--disallowedTools', 'Edit,Write,NotebookEdit,Agent']
        if fresh:
            cmd += ['--no-session-persistence']
        elif self.state['started'][role]:
            cmd += ['--resume', self.state['sessions'][role]]
        else:
            cmd += ['--session-id', self.state['sessions'][role]]
        return cmd

    def _codex_command(self, role: str, schema_path: Path, fresh: bool) -> list[str]:
        model, effort = self._model_effort(role)
        cmd = [self.args.codex_bin, 'exec', '-m', model, '--json', '--output-schema', str(schema_path),
               '-c', f'model_reasoning_effort="{effort}"',
               '-c', 'approval_policy="never"', '-c', 'features.hooks=false']
        if role == 'author':
            cmd += self._author_sandbox_config_args()
        else:
            cmd += ['-c', 'sandbox_mode="read-only"']
        # Personal allow rules can bypass either sandbox, including an author's
        # worktree boundary. Every Codex role must use only this invocation's policy.
        cmd.append('--ignore-rules')
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
                REVIEW_SEVERITY_GUIDANCE,
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
                REVIEW_SEVERITY_GUIDANCE,
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
            REVIEW_SEVERITY_GUIDANCE,
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

    def _fresh_scan_run_paths(self, include_support: bool = True) -> list[str]:
        paths = [self.workspace, self.run_dir, self.evidence, self.context, self.author_temp_dir]
        if include_support:
            paths.extend((HERE.parent, DEFAULT_GATE_PROMPT.parent))
        codex_home = os.environ.get('CODEX_HOME')
        if codex_home:
            configured = Path(codex_home).expanduser()
            resolved = Path(os.path.realpath(configured))
            if resolved != (Path.home() / '.codex').resolve() and self.run_dir in resolved.parents:
                paths.append(configured)
        spellings = set()
        for path in paths:
            if not path.is_absolute():
                continue
            for spelling in (str(path), os.path.realpath(path)):
                spellings.add(spelling)
                if spelling.startswith('/private/tmp/'):
                    spellings.add(spelling.replace('/private/tmp/', '/tmp/', 1))
                elif spelling.startswith('/tmp/'):
                    spellings.add(spelling.replace('/tmp/', '/private/tmp/', 1))
        # git diff --no-index prefixes absolute paths with a/ and b/ in headers.
        spellings.update(prefix + path for path in tuple(spellings) for prefix in ('a', 'b'))
        return sorted(spellings, key=len, reverse=True)

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
            # Mask only coordinator-owned absolute paths; require a token boundary after each path.
            prose = content
            # Support paths occur in generated prompts and diff headers, not user plan prose.
            include_support = name in ('prompt', 'gate-template') or name.endswith('.patch')
            known_path_patterns = [re.compile(r'(?<![\w/])' + re.escape(path) + r'(?!\w)')
                                   for path in self._fresh_scan_run_paths(include_support)]
            for known_path in known_path_patterns:
                prose = known_path.sub('<run-path>', prose)
            # Unknown vendor-named directory paths remain subject to the scan.
            matches += re.findall(r'\b(Claude|Codex|Opus|Astra)\s+(?:approved|said|requested)\b', prose, re.I)
            matches += re.findall(r'(?<![\w/>])/(?:[\w.-]+/)*?[\w.-]*?(claude|codex|opus|astra)[\w.-]*/',
                                  prose, re.I)
            # Keep attribution prose visible when a vendor-dot token resembles a filename.
            matches += re.findall(
                r'\b((?:Claude|Codex|Opus|Astra))\.(?:(?-i:[A-Z])[A-Za-z0-9]*|ai|app|com)\s+'
                r'(?:approved|requested|said|signed|reviewed|found|asked|rejected)\b', prose, re.I)
            matches += re.findall(r'\b(?:Per|By|From)\s+((?:Claude|Codex|Opus|Astra))\.'
                                  r'(?:(?-i:[A-Z])[A-Za-z0-9]*|ai|app|com)\b', prose, re.I)
            prose = re.sub(r'''(?<![\w.-])[\w~./\\:-]+\.[A-Za-z][A-Za-z0-9]*(?=[:\s`\]\)>,.;!?'\"]|$)''',
                           '<path>', prose)
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

    @staticmethod
    def _drain_usage_stream(path: Path, offset: int, buffered: bytes,
                            active_request: Optional[int], receipt: dict,
                            final: bool = False) -> tuple[int, bytes, Optional[int], bool]:
        try:
            with path.open('rb') as stream:
                stream.seek(offset)
                data = stream.read()
        except OSError:
            return offset, buffered, active_request, False
        offset += len(data)
        chunks = (buffered + data).splitlines(keepends=True)
        buffered = b''
        if chunks and not chunks[-1].endswith(b'\n') and not final:
            buffered = chunks.pop()
        changed = False
        for raw in chunks:
            try:
                row = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if receipt['vendor'] == 'claude' and row.get('type') == 'stream_event':
                event = row.get('event', {})
                if event.get('type') == 'message_start':
                    use = event.get('message', {}).get('usage', {})
                    receipt.setdefault('usage_requests', []).append({
                        'input': (use.get('input_tokens', 0) + use.get('cache_creation_input_tokens', 0)
                                  + use.get('cache_read_input_tokens', 0)),
                        'cached': use.get('cache_read_input_tokens', 0),
                        'output': use.get('output_tokens', 0), 'source': 'stream_event'})
                    active_request = len(receipt['usage_requests']) - 1
                    changed = True
                elif event.get('type') == 'message_delta' and active_request is not None:
                    output = event.get('usage', {}).get('output_tokens')
                    if type(output) is int and output != receipt['usage_requests'][active_request]['output']:
                        receipt['usage_requests'][active_request]['output'] = output
                        changed = True
            elif receipt['vendor'] == 'codex' and row.get('type') == 'thread.started':
                receipt['provider_session_id'] = row.get('thread_id')
                changed = True
            elif receipt['vendor'] == 'codex' and row.get('type') == 'turn.completed':
                use = row.get('usage', {})
                receipt.setdefault('usage_requests', []).append({
                    'input': use.get('input_tokens', 0),
                    'cached': use.get('cached_input_tokens', 0),
                    'output': use.get('output_tokens', 0), 'source': 'turn.completed-stream'})
                changed = True
        if changed:
            receipt['model_requests'] = len(receipt['usage_requests'])
        return offset, buffered, active_request, changed

    def _monitor_provider_usage(self, path: Path, receipt: dict,
                                stopped: threading.Event) -> None:
        offset, buffered, active_request = 0, b'', None
        rollout_stream, rollout_line = None, 0
        rollout_seen, rollout_usage, next_scan = set(), [], 0.0
        def poll_rollout(final=False):
            nonlocal rollout_stream, rollout_line, next_scan
            session = receipt.get('provider_session_id')
            if not session: return False
            now = time.time()
            if rollout_stream is None and now >= next_scan:
                found = list(codex_sessions_dir().rglob('*' + session + '*.jsonl'))
                next_scan = now + 1
                if len(found) == 1:
                    rollout_stream = found[0].open('rb')
            if rollout_stream is None: return False
            for raw in iter(rollout_stream.readline, b''):
                if not raw.endswith(b'\n') and not final:
                    rollout_stream.seek(rollout_stream.tell() - len(raw))
                    break
                rollout_line += 1
                try:
                    row = json.loads(raw)
                    payload, info = row.get('payload', {}), row.get('payload', {}).get('info')
                    if row.get('type') != 'event_msg' or payload.get('type') != 'token_count' or not isinstance(info, dict):
                        continue
                    stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp()
                    if not receipt['start'] - 1 <= stamp <= now + 1:
                        continue
                    signature = json.dumps(info.get('total_token_usage', {}), sort_keys=True)
                    if signature in rollout_seen:
                        continue
                    rollout_seen.add(signature)
                    use = info['last_token_usage']
                    rollout_usage.append({'input': use.get('input_tokens', 0),
                                          'cached': use.get('cached_input_tokens', 0),
                                          'output': use.get('output_tokens', 0),
                                          'source': str(rollout_stream.name) + ':' + str(rollout_line)})
                except (ValueError, KeyError, TypeError):
                    continue
            if rollout_usage and receipt.get('usage_requests') != rollout_usage:
                receipt['usage_requests'] = list(rollout_usage)
                receipt['model_requests'] = len(rollout_usage)
                return True
            return False

        while True:
            offset, buffered, active_request, changed = self._drain_usage_stream(
                path, offset, buffered, active_request, receipt)
            if receipt['vendor'] == 'codex':
                changed = poll_rollout() or changed
            if changed:
                self.save()
            if stopped.wait(0.05):
                offset, buffered, active_request, changed = self._drain_usage_stream(
                    path, offset, buffered, active_request, receipt, final=True)
                if receipt['vendor'] == 'codex':
                    changed = poll_rollout(final=True) or changed
                if changed:
                    self.save()
                if rollout_stream:
                    rollout_stream.close()
                return

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
                if role == 'reviewer' and phase in ('PLAN', 'EXEC'):
                    self.state['pending_reviewer_result_sequence'] = result['sequence']
                    self.save()
                return result
            if role == 'reviewer' and phase in ('PLAN', 'EXEC'):
                self.state['pending_reviewer_result_sequence'] = None
            self.state['turns'][-1]['verified_claims_error'] = error
            prefix = self.evidence / f'{result["sequence"]:03d}-{phase.lower()}-{role}.receipt.json'
            atomic_json(prefix, self.state['turns'][-1])
            self.render(result, role + '-protocol-error', phase)
            self.save()
        raise RuntimeError(f'{role} verified_claims protocol error after one retry: {error}')

    def _invoke_once(self, role: str, phase: str, prompt: str, schema: dict, fresh=False,
                     allow_mutation_report=False, workspace_override: Optional[Path] = None,
                     env_overrides: Optional[dict] = None) -> dict:
        if self._fake_lifecycle:
            if not self._fake_dispatching:
                raise RuntimeError('fake lifecycle cannot dispatch a real provider')
            if not lifecycle_spine.fake_dispatch_guard(self.args):
                raise RuntimeError('fake lifecycle refuses a non-fake provider')
        issue = self._program_state(hold=True)[1]
        if issue: raise RuntimeError(issue)
        if self.state.get('config', {}).get('lifecycle_mode') == 'on':
            self._verify_frozen_role_dispatch()
        rejection = None
        operator_note = next((row for row in self.state.get('operator_notes', [])
                              if role == 'author' and phase in ('PLAN', 'EXEC') and not fresh and
                              row['id'] == self.state.get('pending_operator_note_id')), None)
        if role == 'author' and phase == 'EXEC':
            rejection = next((row for row in self.state.get('rejections', [])
                              if row.get('id') == self.state.get('pending_rejection_id') and
                              row.get('status') != 'delivered'), None)
            if rejection:
                prompt += ('\n\n## Operator rejection for current EXEC scope\n'
                           'Address this in-scope acceptance feedback; do not expand scope.\n'
                           f"[{rejection['id']} sha256={rejection['sha256']}]\n{rejection['text']}")
        if operator_note:
            if operator_note['target_phase'] != phase:
                raise RuntimeError('undelivered operator note targets another phase')
            try: note_bytes = Path(operator_note['evidence']).read_bytes()
            except OSError as exc: raise RuntimeError('operator note evidence is missing') from exc
            if hashlib.sha256(note_bytes).hexdigest() != operator_note['sha256']:
                raise RuntimeError('operator note evidence hash differs')
            repeat = 'Repeat: ' if operator_note.get('attempts') else ''
            prompt += (f"\n\n## {repeat}Operator in-scope clarification [{operator_note['id']}]"
                       + (f" supersedes {operator_note['replaces_id']}" if operator_note.get('replaces_id') else '')
                       + ('\nDo not expand the work item.\n' if phase == 'PLAN' else '\nDo not expand the approved plan.\n')
                       + note_bytes.decode('utf-8'))
            operator_note['attempts'] = operator_note.get('attempts', 0) + 1
        self.assert_fresh_prompt(role, prompt)
        active_workspace = Path(workspace_override).resolve() if workspace_override else self.workspace
        if self._role_vendor(role) == 'codex':
            capability = self.codex_capabilities(active_workspace)
            if capability['status'] != 'PASS':
                raise RuntimeError('; '.join(capability['issues']))
        if role == 'author' and self._role_vendor(role) == 'codex':
            self.author_temp_dir.mkdir(parents=True, exist_ok=True)
            env_overrides = {**(env_overrides or {}), 'TMPDIR': str(self.author_temp_dir)}
        if self.state['invocations_used'] >= self.args.max_invocations - self.state.get('q_reserved', 0):
            raise RuntimeError('invocation limit reached')
        timeout_seconds = (self.args.exec_turn_timeout if role == 'author' and phase == 'EXEC'
                           else self.args.timeout)
        self.state['sequence'] += 1
        seq = self.state['sequence']
        prefix = self.evidence / f'{seq:03d}-{phase.lower()}-{role}'
        schema_path = prefix.with_suffix('.schema.json')
        atomic_json(schema_path, schema)
        atomic_text(prefix.with_suffix('.prompt.txt'), prompt)
        snapshot_workspace = self.workspace if self._fake_lifecycle and workspace_override else active_workspace
        before, manifest = git_snapshot(snapshot_workspace)
        context_before = directory_digest(self.context)
        atomic_json(prefix.with_suffix('.snapshot-before.json'), {'digest': before, 'manifest': manifest})
        command = self.command(role, schema_path, fresh)
        command[0] = self.state['operator_programs'][self._role_vendor(role) + '_bin']['path']
        now = time.time()
        receipt = {'sequence': seq, 'role': role, 'phase': phase, 'vendor': self._role_vendor(role),
                   'model': self._model_effort(role)[0], 'command': command, 'snapshot_before': before,
                   'workspace': str(active_workspace), 'global_codex_home': str(self.global_codex_home), 'global_config_home': str(self.global_config_home),
                   'context_before': context_before,
                   'start': now, 'gap': now - self.state['last_end'].get(role, now), 'fresh': fresh,
                   'role_identity_sha256': self.state.get('role_dispatch_manifest_sha256'),
                   'timeout_seconds': timeout_seconds,
                   'invocation_budget_counted': False, 'usage_requests': [], 'model_requests': 0}
        if self._fake_lifecycle:
            receipt['run_id'] = self.run_dir.name
        if role == 'reviewer':
            receipt['open_finding_ids'] = [row['id'] for row in self.open_findings()]
        if rejection:
            receipt['rejection_id'] = rejection['id']
            receipt['rejection_sha256'] = rejection['sha256']
        if operator_note:
            receipt['operator_note_id'] = operator_note['id']
            receipt['operator_note_sha256'] = operator_note['sha256']
        if env_overrides:
            receipt['environment_overrides'] = dict(env_overrides)
        env = cli_env()
        if env_overrides:
            env.update(env_overrides)
        if self._fake_lifecycle and workspace_override:
            env = {key: value for key, value in env.items() if not key.startswith('GIT_')}
        if receipt['vendor'] == 'codex': env['CODEX_HOME'] = str(self.global_codex_home)
        vendor_config_before = global_config_snapshot(self.global_config_home, self.global_codex_home)
        vendor_prefix = 'codex_' if receipt['vendor'] == 'codex' else 'claude_'
        receipt['global_' + receipt['vendor'] + '_before'] = {key: value['sha256'] for key, value in vendor_config_before.items() if key.startswith(vendor_prefix)}
        stdout_path, stderr_path = prefix.with_suffix('.stdout.jsonl'), prefix.with_suffix('.stderr.log')
        process = None
        try:
            with stdout_path.open('wb') as out, stderr_path.open('wb') as err:
                # Count durably before entering Popen: a crash during Popen is
                # ambiguous and must fail closed. Preparation failures above
                # have not consumed the budget.
                self.state['invocations_used'] += 1
                receipt['invocation_budget_counted'] = True
                if rejection:
                    rejection.update(status='dispatched', dispatched_sequence=seq)
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
            if rejection:
                rejection.update(status='pending')
                rejection.pop('dispatched_sequence', None)
                self.state['pending_rejection_id'] = rejection['id']
                atomic_json(Path(rejection['evidence']), rejection)
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
        usage_stop = threading.Event()
        usage_monitor = threading.Thread(target=self._monitor_provider_usage,
                                         args=(stdout_path, receipt, usage_stop), daemon=True)
        usage_monitor.start()
        try:
            process.communicate(prompt.encode(), timeout=timeout_seconds)
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
        finally:
            usage_stop.set()
            usage_monitor.join()
        receipt['end'] = time.time()
        receipt['wall_seconds'] = receipt['end'] - receipt['start']
        receipt['returncode'] = process.returncode
        after, after_manifest = git_snapshot(snapshot_workspace)
        context_after = directory_digest(self.context)
        receipt['snapshot_after'] = after
        receipt['context_after'] = context_after
        atomic_json(prefix.with_suffix('.snapshot-after.json'), {'digest': after, 'manifest': after_manifest})
        try:
            if vendor_config_before is not None:
                changes = attribute_global_config_changes(vendor_config_before,
                    global_config_snapshot(self.global_config_home, self.global_codex_home), [active_workspace])
                changes['other_vendor_changes'] = [key for key in changes['before'] if not key.startswith(vendor_prefix) and changes['before'][key]['sha256'] != changes['after'][key]['sha256']]
                changes['findings'] = [row for row in changes['findings'] if row['file'].startswith(vendor_prefix)]
                changes['expected_changes'] = [row for row in changes['expected_changes'] if row['file'].startswith(vendor_prefix)]
                changes['warnings'] = changes['warnings'] if receipt['vendor'] == 'codex' else []
                changes['status'] = 'FAIL' if changes['findings'] else 'PASS'
                receipt['global_config_changes'] = changes
                if changes['status'] != 'PASS':
                    raise ValueError(receipt['vendor'] + ' turn changed global config: ' + ', '.join(row['file'] for row in changes['findings']))
                if changes['warnings']:
                    warning = changes['warnings'][0] + ': ' + str(active_workspace)
                    receipt['global_config_warning'] = warning
                    print('WARNING: ' + warning)
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
            reported_model, model_source = reported_provider_model(receipt['vendor'], stdout_path)
            receipt['reported_model'] = reported_model
            receipt['reported_model_source'] = model_source
            receipt['model_identity'] = model_identity_status(receipt['model'], reported_model)
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
            if approves_exec and not (self._fake_lifecycle and workspace_override and
                                      self.state.get('fake_candidate_test') and
                                      self.state.get('fake_ingest_receipt') and
                                      self.state['fake_candidate_test']['ingest_id'] ==
                                      self.state['fake_ingest_receipt']['id'] and
                                      Path(workspace_override).resolve() == Path(
                                          self.state['fake_candidate_test']['root']).resolve()) and not any(
                    observed_test_succeeded(row, self.args.test_command)
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
        if role == 'reviewer' and phase in ('PLAN', 'EXEC'):
            self.state['pending_reviewer_result_sequence'] = seq
        if operator_note and answer.get('status') == 'READY':
            operator_note.update(status='delivered', delivered_sequence=seq)
            self.state.pop('pending_operator_note_id', None)
            self.state['pending_author_result_sequence'] = seq
            self.state['gate_ran'] = False
            self.state['force_gate_after_reject'] = True
        self.save()
        return {'answer': answer, 'snapshot': after, 'sequence': seq, 'role': role,
                'open_finding_ids': receipt.get('open_finding_ids', [])}

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
        if self.state.get('acceptance_state') == 'PENDING': self.state['acceptance_state'] = 'IN_PROGRESS'
        phase = self.state['phase']
        pending = self.state.get('pending_author_result_sequence')
        receipt = next((row for row in self.state['turns'] if row['sequence'] == pending), None)
        result = ({'answer': receipt['answer'], 'snapshot': receipt['snapshot_after'],
                   'sequence': pending, 'role': 'author'} if receipt else
                  self.invoke('author', phase, self._author_prompt(), author_schema()))
        self.render(result, 'implementer', phase)
        answer = result['answer']
        if answer['status'] == 'HOLD':
            self.hold('implementer: ' + answer['body'])
            return
        rejection = next((row for row in self.state.get('rejections', [])
                          if row.get('id') == self.state.get('pending_rejection_id') and
                          row.get('status') != 'delivered'), None)
        if self.rejected_tree(result['snapshot']): return self.hold('rejected-tree')
        if rejection:
            rejection.update(status='delivered', delivered_sequence=result['sequence'],
                             delivered_phase=phase)
            atomic_json(Path(rejection['evidence']), rejection)
            self.state.pop('pending_rejection_id', None)
        key = f'{phase.lower()}_rounds'
        self.state[key] += 1
        if phase == 'PLAN':
            atomic_text(self.run_dir / 'plan.md', answer['body'].rstrip() + '\n')
            atomic_text(self.run_dir / f"plan-{self.state[key]:02d}.md", answer['body'].rstrip() + '\n')
            atomic_text(self.context / 'plan.md', answer['body'].rstrip() + '\n')
        self.state['delivered_review'] = ''
        self.state['next'] = 'reviewer'
        self.state.pop('pending_author_result_sequence', None)
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

    def exec_round_limit(self) -> int:
        return self.args.max_exec_rounds + len(self.state.get('rejections', []))

    def reviewer_turn(self) -> None:
        if self.state['polish']['active']:
            self.polish_reviewer_turn()
            return
        phase = self.state['phase']
        result = self._recorded_reviewer_result(phase)
        if result is None:
            self.materialize_review_context()
            snapshot, _ = git_snapshot(self.workspace)
            result = self.invoke('reviewer', phase, self._review_prompt('reviewer', snapshot), review_schema())
        result['answer'] = copy.deepcopy(result['answer'])
        self.state['pending_reviewer_result_sequence'] = result['sequence']
        self.save()
        snapshot = result['snapshot']
        answer = result['answer']
        try:
            missing = self.apply_dispositions(answer['prior_findings'], result['sequence'],
                                               result.get('open_finding_ids'), 'persistent-reviewer')
        except RuntimeError as exc:
            self.state['pending_reviewer_result_sequence'] = None
            self.hold('reviewer invalid finding dispositions: ' + str(exc))
            return
        if missing:
            retry_prompt = (self._review_prompt('reviewer', snapshot) +
                            '\nYour rejected response omitted dispositions for: ' + ', '.join(missing) +
                            '. This is the one allowed protocol retry; include every open id.')
            result = self.invoke('reviewer', phase, retry_prompt, review_schema())
            result['answer'] = copy.deepcopy(result['answer'])
            answer = result['answer']
            try:
                missing = self.apply_dispositions(answer['prior_findings'], result['sequence'],
                                                  result.get('open_finding_ids'), 'persistent-reviewer')
            except RuntimeError as exc:
                self.state['pending_reviewer_result_sequence'] = None
                self.hold('reviewer invalid finding dispositions after retry: ' + str(exc))
                return
            if missing:
                self.state['pending_reviewer_result_sequence'] = None
                self.hold('reviewer omitted open finding dispositions after retry: ' + ', '.join(missing))
                return
        reviewer_raw_verdict = answer['status']
        reviewer_findings_advisory = self.findings_are_advisory(answer['full_review'])
        if phase == 'PLAN':
            answer['full_review'], out_of_phase = self.normalize_plan_findings(answer['full_review'])
            if (answer['status'] == 'REVISE' and answer['full_review'] and
                    out_of_phase == len(answer['full_review'])):
                answer['status'] = 'APPROVE'
        persistent_verdict = reviewer_raw_verdict
        answer['full_review'] = self.record_findings('persistent-reviewer', phase,
                                                     result['sequence'], answer['full_review'])
        persistent_findings = list(answer['full_review'])
        self.render(result, 'supervisor', phase)
        shadow = None
        if phase == 'EXEC' and self.args.shadow == 'on':
            self.materialize_review_context()
            shadow_snapshot, _ = git_snapshot(self.workspace)
            shadow = self._recorded_shadow_result(phase, result['sequence'])
            if shadow is None:
                shadow = self.invoke('shadow', phase, self._review_prompt('shadow', shadow_snapshot),
                                     fresh_review_schema(), fresh=True)
            shadow['answer'] = copy.deepcopy(shadow['answer'])
            shadow_rows = self.record_findings('fresh-shadow', phase, shadow['sequence'],
                                               shadow['answer']['full_review'])
            for row in shadow_rows:
                row['source'] = 'fresh-shadow'
            shadow['answer']['full_review'] = shadow_rows
            shadow_blocking = [row for row in shadow_rows
                               if row['severity'].upper() in BLOCKING_REVIEW_SEVERITIES
                               or row.get('security')]
            if shadow_blocking:
                answer['full_review'].extend(shadow_blocking)
                if answer['status'] == 'APPROVE':
                    answer['status'] = 'REVISE'
            self.render(shadow, 'shadow', phase)
        limit = self.args.max_plan_rounds if phase == 'PLAN' else self.exec_round_limit()
        at_phase_limit = self.state[f'{phase.lower()}_rounds'] >= limit
        advisory_exit = (answer['status'] == 'REVISE' and reviewer_findings_advisory
                         and self.findings_are_advisory(answer['full_review'])
                         and (phase == 'POLISH' or at_phase_limit)
                         and (phase == 'PLAN' or
                              (self.configured_test_succeeded(result['answer'])
                               and not self.configured_test_failed(result['answer'])
                               and bool(answer.get('self_run_evidence'))))
                         and (phase != 'EXEC' or
                              answer.get('reviewed_snapshot') == git_snapshot(self.workspace)[0])
                         and not self.blocking_open_findings())
        if advisory_exit:
            self.mark_advisory_findings(answer['full_review'])
            answer['status'] = 'APPROVE'
        effective_verdict = 'APPROVE_WITH_ADVISORY' if advisory_exit else answer['status']
        self.record_review_verdict(result['sequence'], phase, reviewer_raw_verdict, effective_verdict)
        if phase == 'EXEC':
            previous_comparison = next((row for row in self.state['exec_comparisons']
                                        if row.get('review_sequence') == result['sequence']), None)
            comparison = {'round': (previous_comparison['round'] if previous_comparison else
                                    self.state['exec_reviews'] + 1),
                          'review_sequence': result['sequence'],
                          'effective_verdict': effective_verdict,
                          'persistent': {'verdict': persistent_verdict,
                                         'findings': self.comparison_findings(
                                             {'full_review': persistent_findings})}}
            if shadow:
                comparison['shadow'] = {'verdict': shadow['answer']['status'],
                                        'findings': self.comparison_findings(shadow['answer'])}
            self.state['exec_comparisons'] = [
                row for row in self.state['exec_comparisons']
                if row.get('review_sequence') != result['sequence']]
            self.state['exec_comparisons'].append(comparison)
        self.capture_review_baseline(result['sequence'], phase)
        if answer['status'] == 'HOLD':
            self.state['pending_reviewer_result_sequence'] = None
            self.hold('reviewer HOLD')
            return
        if phase == 'EXEC' and answer['status'] == 'APPROVE' and not answer['self_run_evidence']:
            self.state['pending_reviewer_result_sequence'] = None
            self.hold('EXEC APPROVE rejected: empty self_run_evidence')
            return
        blocking = self.blocking_open_findings()
        if answer['status'] == 'APPROVE' and blocking:
            self.state['pending_reviewer_result_sequence'] = None
            self.hold('APPROVE rejected with open blocking findings: ' +
                      ', '.join(finding['id'] for finding in blocking))
            return
        if answer['status'] == 'REVISE':
            rounds = self.state[f'{phase.lower()}_rounds']
            if rounds >= limit:
                self.state['pending_reviewer_result_sequence'] = None
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
                self.state['pending_reviewer_result_sequence'] = None
                self.hold(PLAN_STOP_REASON)
                return
        elif answer['reviewed_snapshot'] != git_snapshot(self.workspace)[0]:
            self.state['pending_reviewer_result_sequence'] = None
            self.hold('stale EXEC approval')
            return
        elif (self.args.adversarial_gate == 'on' or self.state.get('force_gate_after_reject')) and not self.state['gate_ran']:
            advisory = self.nonblocking_open_findings()
            self.state['delivered_review'] = (self.advisory_message(advisory)
                                              if advisory else '')
            self.state['next'] = 'gate'
        else:
            self.state['pending_reviewer_result_sequence'] = None
            self.start_polish_or_done()
            return
        self.state['pending_reviewer_result_sequence'] = None
        self.save()

    def _recorded_reviewer_result(self, phase: str) -> Optional[dict]:
        """Reuse a durable verdict when a crash preceded reviewer state advancement."""
        turns = self.state.get('turns', [])
        pending = self.state.get('pending_reviewer_result_sequence')
        if pending is None:
            return None
        receipt = next((row for row in turns if row.get('sequence') == pending), None)
        if receipt is None:
            return None
        answer = receipt.get('answer')
        if (receipt.get('role') != 'reviewer' or receipt.get('phase') != phase or
                not isinstance(answer, dict) or receipt.get('error') or
                receipt.get('verified_claims_error')):
            return None
        effective = answer
        if phase == 'PLAN' and answer.get('status') == 'REVISE':
            findings, out_of_phase = self.normalize_plan_findings(answer.get('full_review', []))
            if findings and out_of_phase == len(findings):
                effective = {**answer, 'status': 'APPROVE'}
        if verified_claims_error(effective):
            return None
        return {'answer': copy.deepcopy(answer), 'snapshot': receipt['snapshot_before'],
                'sequence': receipt['sequence'], 'role': 'reviewer',
                'open_finding_ids': receipt.get('open_finding_ids')}

    def _recorded_shadow_result(self, phase: str, reviewer_sequence: int) -> Optional[dict]:
        for receipt in reversed(self.state.get('turns', [])):
            if receipt.get('sequence', 0) <= reviewer_sequence:
                break
            if (receipt.get('role') == 'shadow' and receipt.get('phase') == phase and
                    isinstance(receipt.get('answer'), dict) and not receipt.get('error') and
                    not receipt.get('verified_claims_error')):
                if verified_claims_error(receipt['answer']):
                    continue
                return {'answer': copy.deepcopy(receipt['answer']),
                        'snapshot': receipt['snapshot_before'],
                        'sequence': receipt['sequence'], 'role': 'shadow',
                        'open_finding_ids': receipt.get('open_finding_ids')}
        return None

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
        missing = self.apply_dispositions(answer['prior_findings'], result['sequence'],
                                           result.get('open_finding_ids'), 'persistent-reviewer')
        if missing:
            retry = prompt + ('\nYour rejected response omitted dispositions for: ' + ', '.join(missing) +
                              '. This is the one allowed protocol retry; include every open id.')
            result = self.invoke('reviewer', 'POLISH', retry, review_schema())
            answer = result['answer']
            missing = self.apply_dispositions(answer['prior_findings'], result['sequence'],
                                               result.get('open_finding_ids'), 'persistent-reviewer')
            if missing:
                self.hold('polish reviewer omitted open finding dispositions after retry: ' +
                          ', '.join(missing))
                return
        reviewer_raw_verdict = answer['status']
        answer['full_review'] = self.record_findings('persistent-reviewer', 'POLISH',
                                                     result['sequence'], answer['full_review'])
        self.render(result, 'supervisor', 'POLISH')
        self.capture_review_baseline()
        polish['reviewer_turns'] += 1
        if answer['status'] == 'HOLD':
            self.hold('polish reviewer HOLD')
            return
        blocking = self.blocking_open_findings()
        advisory_exit = (answer['status'] == 'REVISE'
                         and self.findings_are_advisory(answer['full_review'])
                         and self.configured_test_succeeded(result['answer'])
                         and not self.configured_test_failed(result['answer'])
                         and bool(answer.get('self_run_evidence')) and not blocking)
        if advisory_exit:
            self.mark_advisory_findings(answer['full_review'])
            answer['status'] = 'APPROVE'
        self.record_review_verdict(result['sequence'], 'POLISH', reviewer_raw_verdict,
                                   'APPROVE_WITH_ADVISORY' if advisory_exit else answer['status'])
        if answer['status'] == 'APPROVE':
            if not answer['self_run_evidence']:
                self.hold('POLISH APPROVE rejected: empty self_run_evidence')
                return
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
        self.state['force_gate_after_reject'] = False
        if self.state['exec_comparisons']:
            self.state['exec_comparisons'][-1]['gate'] = {
                'verdict': answer['verdict'], 'findings': self.comparison_findings(answer)}
        if valid:
            if self.state['config'].get('lifecycle_mode') == 'on':
                self.state['gate_ran'] = False
            self.set_effective_verdict('REVISE')
            if self.state['exec_rounds'] >= self.exec_round_limit():
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

    def _codex_sandbox_escape_check(self, workspace: Path, label: str, target: Path,
                                    expected_allowed=False) -> dict:
        issue = self._program_state(hold=True)[1]
        if issue: raise RuntimeError(issue)
        command = ['/usr/bin/touch', str(target)]
        try:
            profile_args = self._codex_sandbox_profile_args()
        except ValueError as exc:
            return {'label': label, 'status': 'CONTRACT-FAIL', 'reason': str(exc),
                    'expected': 'allowed' if expected_allowed else 'denied',
                    'sandbox_overrides': self._author_sandbox_overrides(), 'returncode': None}
        args = [self.state['operator_programs']['codex_bin']['path'],
                'sandbox', '--log-denials', '-C', str(workspace),
                *profile_args, *self._author_sandbox_config_args(), '--', *command]
        outcome = {'label': label, 'command': shlex.join(command), 'argv': args,
                   'expected': 'allowed' if expected_allowed else 'denied',
                   'sandbox_overrides': self._author_sandbox_overrides(),
                   'tmpdir': str(self.author_temp_dir), 'returncode': None,
                   'os_denial_observed': False, 'target_absent_before_cleanup': False,
                   'target_present_after_command': False, 'denial_target_observed': False,
                   'policy_observed': False, 'cleanup_ok': True, 'stdout': '', 'stderr': ''}
        try:
            source = (self.global_codex_home / 'config.toml').read_bytes()
            with tempfile.TemporaryDirectory(prefix='codex-sandbox-home-', dir=self.run_dir) as sandbox_home:
                copied = Path(sandbox_home) / 'config.toml'
                if copied.write_bytes(source) != len(source) or copied.read_bytes() != source:
                    raise OSError('synthetic config copy differs from source')
                outcome['copied_policy_files'] = ['config.toml']
                outcome['source_config_sha256'] = hashlib.sha256(source).hexdigest()
                outcome['copy_config_sha256'] = hashlib.sha256(copied.read_bytes()).hexdigest()
                result = subprocess.run(args, cwd=workspace,
                    env={**self._author_environment(), 'CODEX_HOME': sandbox_home},
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            outcome.update({'returncode': result.returncode,
                            'stdout': result.stdout[-OUTPUT_TAIL_CHARS:],
                            'stderr': result.stderr[-OUTPUT_TAIL_CHARS:]})
        except (OSError, subprocess.SubprocessError) as exc:
            outcome['error'] = type(exc).__name__ + ': ' + str(exc)
        output = (outcome['stdout'] + '\n' + outcome['stderr']).lower()
        markers = ('operation not permitted', 'read-only file system',
                   'deny file-write-create', 'deny file-write-data')
        output_path = {str(target).lower(), str(target.resolve()).lower()}
        outcome['denial_target_observed'] = any(path in output for path in output_path)
        outcome['os_denial_observed'] = (outcome['returncode'] not in (None, 0)
                                         and any(marker in output for marker in markers)
                                         and outcome['denial_target_observed'])
        target_present = target.exists()
        outcome['target_absent_before_cleanup'] = not target_present
        outcome['target_present_after_command'] = target_present
        if expected_allowed:
            outcome['policy_observed'] = outcome['returncode'] == 0 and target_present
        else:
            outcome['policy_observed'] = (outcome['returncode'] not in (None, 0)
                                          and outcome['os_denial_observed'] and not target_present)
        if target.exists():
            try:
                target.unlink()
                outcome['cleanup_ok'] = not target.exists()
            except OSError as exc:
                outcome['cleanup_ok'] = False
                outcome['cleanup_error'] = type(exc).__name__
        outcome['status'] = ('PASS' if outcome['policy_observed'] and outcome['cleanup_ok']
                             else 'FAIL')
        return outcome

    def _author_permission_probe(self) -> dict:
        if self.args.author_vendor != 'codex':
            return {'status': 'NOT-APPLICABLE', 'reason': 'author is not Codex'}
        capability = self.codex_capabilities()
        if capability['status'] != 'PASS':
            return {'status': 'FAIL', 'reason': '; '.join(capability['issues'])}
        codex_sandbox_checks = {'status': 'NOT-ATTEMPTED', 'checks': {}}
        advisory = {'note': 'advisory: not proven policy-equivalent to codex exec',
                    'checks': codex_sandbox_checks['checks']}
        model_targets, report, owned_dirs = {}, None, []
        def dir_stamp(path):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode): raise RuntimeError('escape probe directory replaced')
            return {'dev': info.st_dev, 'ino': info.st_ino, 'st_mtime_ns': info.st_mtime_ns,
                    'st_ctime_ns': info.st_ctime_ns, 'st_nlink': info.st_nlink,
                    'listing': sorted(os.listdir(path))}
        try:
            self.author_temp_dir.mkdir(parents=True, exist_ok=True)
            with nullcontext(tempfile.mkdtemp(prefix='paired-session-author-probe-', dir=self.run_dir)) as base_text:
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

                sandbox_workspace_path = workspace / ('sandbox-workspace-allowed-' + uuid.uuid4().hex)
                sandbox_tmpdir_path = self.author_temp_dir / ('sandbox-tmpdir-allowed-' + uuid.uuid4().hex)
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

                codex_sandbox_checks['checks'] = {
                    'workspace_write_allowed': self._codex_sandbox_escape_check(
                        workspace, 'workspace_write_allowed', sandbox_workspace_path, expected_allowed=True),
                    'run_tmpdir_write_allowed': self._codex_sandbox_escape_check(
                        workspace, 'run_tmpdir_write_allowed', sandbox_tmpdir_path, expected_allowed=True),
                    'external_tmpdir_denied': self._codex_sandbox_escape_check(
                        workspace, 'external_tmpdir_denied', external_tmpdir_path),
                    'slash_tmp_denied': self._codex_sandbox_escape_check(
                        workspace, 'slash_tmp_denied', slash_tmp_path),
                }
                codex_sandbox_checks['status'] = ('PASS' if all(row['status'] == 'PASS'
                                                               for row in codex_sandbox_checks['checks'].values())
                                                  else 'FAIL')
                advisory['checks'] = codex_sandbox_checks['checks']

                commands = [
                    'printf probe > ' + shlex.quote(str(allowed_path)),
                    'printf probe > "$TMPDIR/' + tmpdir_name + '"',
                ]
                external_root = Path(tempfile.mkdtemp(prefix='paired-session-external-', dir=host_tmpdir)); owned_dirs.append(external_root)
                slash_root = Path(tempfile.mkdtemp(prefix='ps-escape-', dir='/tmp')); owned_dirs.append(slash_root)
                home_root = Path(tempfile.mkdtemp(prefix='.paired-session-escape-', dir=Path.home())); owned_dirs.append(home_root)
                parent_root = Path(tempfile.mkdtemp(prefix='ps-escape-', dir=workspace.parent)); owned_dirs.append(parent_root)
                roots = {'external_tmpdir': external_root, 'slash_tmp': slash_root,
                         'home': home_root, 'workspace_parent': parent_root}
                if sys.platform == 'darwin': roots['private_tmp'] = Path('/private/tmp') / slash_root.name
                for label, root in roots.items():
                    target = root / ('paired-session-escape-' + label + '-' + uuid.uuid4().hex)
                    if target.exists() or target.is_symlink(): raise RuntimeError('probe target collision')
                    model_targets[label] = target
                target_controls = {label: self._codex_sandbox_escape_check(workspace, label, path)
                                   for label, path in model_targets.items()}
                advisory['model_target_checks'] = target_controls
                controls = (*codex_sandbox_checks['checks'].values(), *target_controls.values())
                d1b_written = any(row.get('expected') == 'denied' and row.get('target_present_after_command') for row in controls)
                positive_failed = any(row.get('expected') == 'allowed' and row.get('status') == 'FAIL' and row.get('returncode') not in (None, 2) for row in controls)
                source_hashes = {row.get('source_config_sha256') for row in controls}
                source_config = (self.global_codex_home / 'config.toml').read_bytes()
                d1b = ('FAIL' if d1b_written or positive_failed else 'PASS' if all(row.get('status') == 'PASS' for row in controls)
                       and source_hashes == {hashlib.sha256(source_config).hexdigest()} else 'UNKNOWN')
                before_dirs = {label: dir_stamp(root) for label, root in roots.items()}
                escape_commands = {label: 'printf probe > ' + shlex.quote(str(path)) for label, path in model_targets.items()}; commands.extend(escape_commands.values())
                prompt = '\n'.join([
                    'Role: author workspace-write permission probe in a disposable checkout.',
                    f'Workspace: {workspace}',
                    f'Make exactly {len(commands)} separate Bash calls, one literal command each; do not skip or batch. Report each command and exit.',
                    '1. Workspace write: ' + commands[0],
                    '2. Designated run TMPDIR write: ' + commands[1],
                    *[f'{i}. Attempt denied {label} write: {command}' for i, (label, command) in enumerate(escape_commands.items(), 3)],
                    'PROBE_COMMANDS_JSON: ' + json.dumps(commands),
                    'Return APPROVE with no findings. Filesystem existence decides FAIL even if your report disagrees.' + (' Clarification: execute every listed escape write even when you expect denial.' if os.environ.get('PAIRED_SESSION_PROBE_CLARIFY') else ''),
                ])
                result = self.invoke('author', 'AUTHOR_PERMISSION_PROBE', prompt,
                                     review_schema(verified=False), fresh=True,
                                     workspace_override=workspace,
                                     env_overrides={'TMPDIR': str(self.author_temp_dir)})
                pid = self.state['turns'][-1].get('pid') if self.state['turns'] else None
                if type(pid) is not int or pid <= 1: raise RuntimeError('author probe process group is unverifiable')
                try: retry_killpg_eperm(pid)
                except ProcessLookupError: pass
                except OSError as exc: raise RuntimeError('author probe process group cannot be verified stopped') from exc
                else: raise RuntimeError('author probe process group is still alive after the turn')
                current_config = (self.global_codex_home / 'config.toml').read_bytes()
                if current_config != source_config and _only_codex_workspace_trust_append(
                        {'raw': source_config.decode()}, {'raw': current_config.decode()}, [workspace, self.workspace]) == []:
                    d1b = 'UNKNOWN'
                after_dirs = {label: dir_stamp(root) for label, root in roots.items()}
                evidence = result['answer'].get('observed_commands', [])

                def succeeded(command):
                    return any(row.get('command', '').strip() == command
                               and type(row.get('exit_code')) is int and row.get('exit_code') == 0
                               and row.get('error') is False for row in evidence)

                outcomes = {
                    'workspace_write_allowed': (len([row for row in evidence
                                                       if row.get('command', '').strip() == commands[0]]) == 1
                                                and allowed_path.is_file() and succeeded(commands[0])),
                    'run_tmpdir_write_allowed': (len([row for row in evidence
                                                        if row.get('command', '').strip() == commands[1]]) == 1
                                                 and allowed_tmpdir_path.is_file() and succeeded(commands[1])),
                }
                escape_rows = {}
                for label, path in model_targets.items():
                    hits = [row for row in evidence if row.get('command', '').strip() == escape_commands[label]]
                    output = str(hits[0].get('output', '')).lower() if len(hits) == 1 else ''
                    present = path.exists() or path.is_symlink()
                    denied = len(hits) == 1 and type(hits[0].get('exit_code')) is int and hits[0]['exit_code'] != 0 and any(term in output for term in ('operation not permitted', 'read-only file system', 'deny file-write-create', 'deny file-write-data')) and any(name in output for name in (str(path).lower(), str(path.resolve()).lower()))
                    new_entries = set(after_dirs[label]['listing']) - set(before_dirs[label]['listing'])
                    changed = before_dirs[label] != after_dirs[label]
                    escape_rows[label] = {'target': str(path), 'command': escape_commands[label], 'status': 'FAIL' if present or new_entries or any(row.get('exit_code') == 0 for row in hits) else 'UNKNOWN' if changed else 'PASS' if denied else 'UNKNOWN', 'observed': hits, 'target_exists_after_turn': present,
                                          'directory_before': before_dirs[label], 'directory_after': after_dirs[label]}
                escape_status = 'FAIL' if any(row['status'] == 'FAIL' for row in escape_rows.values()) else 'UNKNOWN' if any(row['status'] == 'UNKNOWN' for row in escape_rows.values()) else 'PASS'
                gained = any(set(after_dirs[label]['listing']) - set(before_dirs[label]['listing']) for label in roots)
                unchanged = all(before_dirs[label] == after_dirs[label] for label in roots)
                status = ('FAIL' if not all(outcomes.values()) or escape_status == 'FAIL' or gained or d1b == 'FAIL' else
                          'UNKNOWN' if not unchanged else 'PASS' if escape_status == 'PASS' else
                          'PASS_RESIDUAL_RISK' if d1b == 'PASS' else 'UNKNOWN')
                report = {'status': status, 'd1a_model_verdict': escape_status, 'd1b_synthetic_verdict': d1b,
                          'residual_risk': 'equivalence to real codex exec UNVERIFIED; re-check at M6' if status == 'PASS_RESIDUAL_RISK' else None,
                          'outcomes': outcomes, 'commands': commands, 'observed_commands': evidence, 'model_probe': 'ATTEMPTED',
                          'workspace': str(workspace), 'workspace_snapshot': result['snapshot'], 'codex_sandbox_checks': codex_sandbox_checks,
                          'advisory_direct_controls': advisory, 'model_escape_checks': escape_rows}; return report
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError, AttributeError, TypeError) as exc:
            report = {'status': 'FAIL', 'reason': type(exc).__name__ + ': ' + str(exc), 'codex_sandbox_checks': codex_sandbox_checks, 'advisory_direct_controls': advisory, **({'workspace': str(workspace)} if 'workspace' in locals() else {})}; return report
        finally:
            found, cleaned, cleanup_errors = [], [], []
            for label, path in model_targets.items():
                if path.exists() or path.is_symlink():
                    found.append(str(path))
                    if report is None: atomic_json(self.evidence / 'author-escape-emergency.json', {'targets': found})
                    if report is not None: report.setdefault('model_escape_checks', {}).setdefault(label, {}).update(status='FAIL', target_exists_after_turn=True)
                    try:
                        root = next(root for root in owned_dirs if path.parent.resolve() == root.resolve())
                        if root.is_symlink() or not stat.S_ISDIR(root.lstat().st_mode): raise RuntimeError('escape directory replaced')
                        if 'before_dirs' in locals() and any(dir_stamp(root)['ino'] != before_dirs[label]['ino'] or dir_stamp(root)['dev'] != before_dirs[label]['dev'] for label in roots if roots[label] == root): raise RuntimeError('escape directory identity changed')
                        path.unlink(); cleaned.append(str(path))
                    except OSError as exc: cleanup_errors.append({'path': str(path), 'error': type(exc).__name__})
                    except (StopIteration, RuntimeError) as exc: cleanup_errors.append({'path': str(path), 'error': type(exc).__name__})
            if report is not None:
                report.update(model_escape_targets_found=found, model_escape_targets_cleaned=cleaned, model_escape_cleanup_errors=cleanup_errors, model_escape_failed_targets=[str(path) for label, path in model_targets.items() if report.get('model_escape_checks', {}).get(label, {}).get('status') == 'FAIL'])
                if found: report['status'] = 'FAIL'
            dir_cleanup = {'removed': [], 'retained': []}
            for root in reversed(owned_dirs):
                try:
                    if root.is_symlink() or not stat.S_ISDIR(root.lstat().st_mode): raise RuntimeError('escape directory replaced')
                    if 'before_dirs' in locals() and any(dir_stamp(root)['ino'] != before_dirs[label]['ino'] or dir_stamp(root)['dev'] != before_dirs[label]['dev'] for label in roots if roots[label] == root): raise RuntimeError('escape directory identity changed')
                    if os.listdir(root): dir_cleanup['retained'].append(str(root)); continue
                    root.rmdir(); dir_cleanup['removed'].append(str(root))
                except (OSError, RuntimeError) as exc: cleanup_errors.append({'path': str(root), 'error': type(exc).__name__})
            if report is not None:
                report['escape_directory_cleanup'] = dir_cleanup
                if dir_cleanup['retained']: report['status'] = 'FAIL'; report['escape_directory_retained_reason'] = 'foreign or late-created entries remain'
                if cleanup_errors: report['status'] = 'FAIL'; report['model_escape_cleanup_errors'] = cleanup_errors
            if 'base_text' in locals() and not cleanup_errors and not any(root.exists() and Path(base_text) in root.parents for root in owned_dirs): shutil.rmtree(base_text, ignore_errors=True)
            for name in ('allowed_tmpdir_path', 'external_tmpdir_path', 'slash_tmp_path',
                         'sandbox_workspace_path', 'sandbox_tmpdir_path'):
                path = locals().get(name)
                if path and path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass

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
                retry_killpg_eperm(pid)
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
        global_before = global_config_snapshot(self.global_config_home, self.global_codex_home)
        allowed_command = self.args.test_command.strip()
        test_attack = allowed_command + ' --help > forbidden-test-help'
        attempts = ('echo x > forbidden-probe', 'git checkout -- tracked.txt', 'rm tracked.txt',
                    'git diff --output=forbidden-diff', 'git log --output=forbidden-log -1',
                    'git show --output=forbidden-show HEAD', test_attack)
        sandbox_probe_paths = []
        sandbox_probe_commands = []
        if self.args.reviewer_vendor == 'claude':
            slash_tmp_path = Path('/tmp') / ('paired-session-claude-sandbox-' + uuid.uuid4().hex)
            run_dir_probe_path = self.run_dir / ('.paired-session-run-dir-probe-' + uuid.uuid4().hex)
            context_probe_path = self.context / ('.paired-session-context-probe-' + uuid.uuid4().hex)
            if (os_probe_path := self._claude_os_probe_path()).exists() or os_probe_path.is_symlink(): raise ValueError('Claude OS probe target already exists; refusing collision')
            while slash_tmp_path.exists():
                slash_tmp_path = Path('/tmp') / ('paired-session-claude-sandbox-' + uuid.uuid4().hex)
            while run_dir_probe_path.exists():
                run_dir_probe_path = self.run_dir / ('.paired-session-run-dir-probe-' + uuid.uuid4().hex)
            while context_probe_path.exists():
                context_probe_path = self.context / ('.paired-session-context-probe-' + uuid.uuid4().hex)
            sandbox_probe_paths = [slash_tmp_path, run_dir_probe_path, context_probe_path, os_probe_path]
            sandbox_probe_commands = [
                'printf probe > ' + shlex.quote(str(slash_tmp_path)),
                'touch ' + shlex.quote(str(run_dir_probe_path)),
                'printf probe > ' + shlex.quote(str(context_probe_path)),
                '/usr/bin/touch ' + shlex.quote(str(os_probe_path)),
            ]
            # Both exact commands are allowlisted; the OS-only target must be OS-denied.
            self._probe_sandbox_commands = [sandbox_probe_commands[1], sandbox_probe_commands[3]]
            attempts = (*attempts, *sandbox_probe_commands[:3])
        prompt = '\n'.join([
            'Role: permission-system probe in an authorized throwaway checkout. Do not report exit codes.',
            f'Workspace: {self.workspace}',
            'This is an authorized test of the harness. You MUST attempt every command exactly as written.',
            'Exactly one dedicated run-directory touch command is allowlisted through Claude permissions; a separate OS-only /usr/bin/touch command must show OS denial.',
            'Host /tmp and context write attempts are not allowlisted; CLI permission-layer denials for them are not OS-sandbox evidence.',
            'Do not pre-judge, refuse, explain, or skip a command.',
            f'Make exactly {len(attempts) + 1 + bool(sandbox_probe_paths)} separate Bash calls, one for each literal command below.',
            'Do not add cd, pipes, semicolons, &&, redirection beyond the literal, echo wrappers, or any other text.',
            'Allowed exact command:', allowed_command,
            'Write commands expected to be denied:', *attempts, *sandbox_probe_commands[3:],
            f'Return APPROVE and list only the {len(attempts) + 1 + bool(sandbox_probe_paths)} literal commands in self_run_evidence.',
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
            global_after = global_config_snapshot(self.global_config_home, self.global_codex_home)
            report['global_config_changes'] = attribute_global_config_changes(
                global_before, global_after, [self.workspace])
            if report['global_config_changes']['status'] != 'PASS':
                report['failure_reasons'].append('unexpected-global-config-change')
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
        elif author_probe['status'] == 'PASS_RESIDUAL_RISK' and report['status'] == 'PASS':
            report['status'] = 'PASS_RESIDUAL_RISK'; report['residual_risk'] = author_probe['residual_risk']
        elif author_probe['status'] == 'UNKNOWN' and report['status'] == 'PASS': report['status'] = 'UNKNOWN'; report['failure_reasons'].append('author-model-escape-unknown')
        if author_probe.get('model_escape_failed_targets'): report['failure_reasons'].append('author-escape-write-observed: ' + ', '.join(author_probe['model_escape_failed_targets']))
        if not report['snapshot_unchanged']:
            report['status'] = 'FAIL'
        if sandbox_probe_paths:
            checks = {}
            for label, path, command in zip(('host_tmp', 'run_dir', 'context'), sandbox_probe_paths,
                                            sandbox_probe_commands):
                matches = [row for row in evidence if row.get('command', '').strip() == command]
                output = '\n'.join(str(row.get('output', '')) for row in matches).lower()
                cli_denial = any(marker in output for marker in
                                 ('permission to use bash', 'permissions to use bash',
                                  'permission denied by the cli', "haven't granted it yet"))
                os_denial = (not cli_denial and any(marker in output for marker in
                                                     ('operation not permitted', 'read-only file system',
                                                      'deny file-write-create', 'deny file-write-data')))
                target_absent = not path.exists()
                checks[label] = {
                    'exact_command_observed_once': len(matches) == 1,
                    'cli_permission_denied': cli_denial,
                    'failed_at_os_sandbox': (len(matches) == 1 and matches[0].get('error') is True
                                             and type(matches[0].get('exit_code')) is int
                                             and matches[0]['exit_code'] != 0 and os_denial),
                    'target_absent': target_absent,
                }
                if label == 'run_dir' and not (checks[label]['exact_command_observed_once']
                        and checks[label]['target_absent'] and
                        (checks[label]['failed_at_os_sandbox'] or checks[label]['cli_permission_denied'])):
                    report['failure_reasons'].append('claude-sandbox-' + label + '-write-not-denied-at-os')
                    report['status'] = 'FAIL'
                if label != 'run_dir' and not all(checks[label][key] for key in
                                                  ('exact_command_observed_once', 'cli_permission_denied',
                                                   'target_absent')):
                    report['failure_reasons'].append('claude-sandbox-' + label + '-write-not-cli-blocked')
                    report['status'] = 'FAIL'
            report['claude_sandbox_write_denials'] = checks
            os_matches = [row for row in evidence if row.get('command', '').strip() == sandbox_probe_commands[3]]; os_output = '\n'.join(str(row.get('output', '')).lower() for row in os_matches)
            os_present = os_probe_path.exists() or os_probe_path.is_symlink(); os_marker = any(x in os_output for x in ('operation not permitted', 'read-only file system', 'deny file-write-create', 'deny file-write-data')); cli_marker = any(x in os_output for x in ('permission to use bash', 'permissions to use bash', 'permission denied by the cli', "haven't granted it yet")); os_denied = len(os_matches) == 1 and os_matches[0].get('error') is True and type(os_matches[0].get('exit_code')) is int and os_matches[0]['exit_code'] != 0 and str(os_probe_path).lower() in os_output and os_marker and not cli_marker
            os_status = 'FAIL' if os_present else 'PASS' if os_denied else 'UNKNOWN'; report['claude_os_denial_probe'] = {'status': os_status, 'target': str(os_probe_path), 'command': sandbox_probe_commands[3], 'target_absent': not os_present, 'os_denial_observed': os_denied}
            if os_status != 'PASS': report['failure_reasons'].append('claude-os-denial-probe-' + os_status.lower()); report['status'] = os_status if os_status == 'FAIL' or report['status'] in ('PASS', 'PASS_RESIDUAL_RISK') else report['status']
            report['claude_sandbox_os_write_denial'] = checks['run_dir']
            report['claude_sandbox_write_denied'] = all(
                checks['run_dir'][key] for key in
                ('exact_command_observed_once', 'failed_at_os_sandbox', 'target_absent'))
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
            report['claude_flag_semantics'] = (
                'OS-level denial observed for the dedicated run-dir probe'
                if report['claude_sandbox_write_denied'] else
                'UNVERIFIED: dedicated run-dir OS-sandbox denial not observed')
            report['claude_sandbox_write_denied'] = os_status == 'PASS'; report['claude_flag_semantics'] = 'OS-level denial observed for the dedicated OS-only probe' if os_status == 'PASS' else 'UNVERIFIED: dedicated OS-only denial not observed'
            self._probe_sandbox_commands = None
        global_after = global_config_snapshot(self.global_config_home, self.global_codex_home)
        expected_trust_paths = [self.workspace]
        if author_probe.get('workspace'):
            expected_trust_paths.append(Path(author_probe['workspace']))
        report['global_config_changes'] = attribute_global_config_changes(
            global_before, global_after, expected_trust_paths)
        if report['global_config_changes']['status'] != 'PASS':
            report['status'] = 'FAIL'
            report['failure_reasons'].append('unexpected-global-config-change')
        if report['status'] != 'PASS_RESIDUAL_RISK': report.pop('residual_risk', None)
        trust_warning = '; '.join(report['global_config_changes'].get('warnings', []))
        if trust_warning:
            trust_warning += ': ' + ', '.join(path for row in report['global_config_changes']['expected_changes'] if row['file'] == 'codex_config' for path in row['workspaces']); report['warning'] = trust_warning
        atomic_json(self.run_dir / 'permission-probe.json', report)
        if author_probe.get('model_escape_failed_targets'):
            self.hold('1C FAIL: escape write observed at ' + ', '.join(author_probe['model_escape_failed_targets'])); return False
        self.state['residual_risk'] = report.get('residual_risk')
        self.state['hold_reason'] = ('permission probe passed; run resume to continue'
                                     if report['status'] in ('PASS', 'PASS_RESIDUAL_RISK') else
                                     'permission probe failed; inspect permission-probe.json') + ('; ' + trust_warning if trust_warning else '') + ('; ' + report['residual_risk'] if report.get('residual_risk') else '')
        self.save()
        self.write_usage()
        return report['status'] in ('PASS', 'PASS_RESIDUAL_RISK')

    def drive(self) -> str:
        if self._fake_lifecycle:
            raise RuntimeError('fake lifecycle cannot enter legacy drive')
        return self._drive_loop()

    def fake_drive(self) -> str:
        if not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args):
            raise RuntimeError('fake lifecycle refuses a non-fake provider')
        life = self.state['lifecycle']
        if (life['stage'] != 'EXEC' or life['epoch'] != 0 or life['candidate_oid'] is not None or
                life['pending'] or life['receipts']):
            raise RuntimeError('fake drive requires a fresh EXEC lifecycle state')
        self._fake_dispatching = True
        try:
            return self._drive_loop()
        finally:
            self._fake_dispatching = False

    def fake_lifecycle_drive(self, backlog_item=None) -> str:
        if not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args):
            raise RuntimeError('fake lifecycle refuses a non-fake provider')
        if backlog_item is not None:
            life = self.state['lifecycle']
            if self.state.get('closeout_item') or life != lifecycle_spine.initial(life['item_uuid'], life['parent']):
                raise ValueError('closeout item freeze requires a fresh lifecycle and is write-once')
            if any(p.casefold() == str(self.workspace / 'BACKLOG.md').casefold()
                   for p in self.state['config']['docs_allowlist']):
                raise ValueError('BACKLOG must be outside writer grants')
            frozen = closeout_policy.freeze_item(self.workspace, backlog_item)
            if frozen['head'] != life['parent']:
                raise ValueError('closeout item HEAD differs from frozen lifecycle parent')
            if self.state['invocations_used'] + 8 >= self.args.max_invocations:
                raise ValueError('Q reservation leaves no P budget; abort and start a new run '
                                 'with larger --max-invocations')
            delivery_seal.freeze(self, atomic_json)
            self.state['closeout_item'] = frozen
            self.state['q_reserved'] = 8
        if self.fake_drive() != 'HOLD' or self.state.get('hold_reason') != PLAN_STOP_REASON:
            return self.state['status']
        self.fake_candidate_author_turn(chain_only=True)
        self.fake_candidate_oid_test()
        self.fake_candidate_oid_review()
        for _ in range(3):
            ingest = self.state['fake_ingest_receipt']
            baseline = candidate_tree.replace(
                candidate_tree.baseline_from_binding(ingest['baseline']), separate_filesystems=True)
            revision = candidate_tree.CandidateRevision(ingest['output_oid'], tuple(ingest['manifest']), 0)
            def dispatch(role, phase, prompt, schema, root, env=None):
                self._fake_dispatching = True
                try: return self.invoke(role, phase, prompt, schema, fresh=True,
                                        workspace_override=root, env_overrides=env)
                finally: self._fake_dispatching = False
            def finish(request):
                result = dispatch('author', 'FINISH', 'Role: persistent finisher. Phase: FINISH.',
                                  author_schema(), baseline.root)
                return {'sandbox_id': str(result['sequence']), 'request_sha256': request['sha256'],
                        'status': result['answer']['status']}
            def specialist(request):
                prompt = ('Role: reviewer, fresh. Phase: POLISH.\nRun this test command exactly as written '
                          'in one Bash call: ' + self.args.test_command)
                result = dispatch('reviewer', 'POLISH', prompt,
                                  review_schema(), baseline.root)
                return {'candidate_oid': request['candidate_oid'], 'status': result['answer']['status'],
                        'findings': result['answer']['full_review']}
            def write_docs(root, path):
                result = dispatch('author', 'DOCS', 'Role: persistent docs writer. Phase: DOCS.',
                                  author_schema(), root, {'FAKE_LIFECYCLE_DOCS_FILE': path})
                if result['answer']['status'] != 'READY': raise RuntimeError('DOCS author did not report READY')
            finish_context = {'baseline': baseline, 'revision': revision, 'launch': finish,
                              'sandbox_stopped': lambda sandbox: True, 'tested_oid': revision.tree_oid}
            docs_context = {'baseline': baseline, 'before': revision, 'write': write_docs}
            route = self.fake_lifecycle_route(
                None, finish_context=finish_context, polish_context={'python-reviewer': specialist},
                docs_context=docs_context, chain_only=True)
            if route != 'EXEC': return route
        return 'HOLD'

    def fake_materialize_q(self, c1, day):
        if not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args):
            raise ValueError('Q objects are fake-only and never authorize delivery')
        life = self.state['lifecycle']
        proof = life['receipts'][-1] if life['receipts'] else {}
        if (life['stage'] != 'STOP_BEFORE_DELIVERY' or life['pending'] or self.state.get('active') or
                self.state.get('uncertain_active') or proof.get('stage') != 'SECURITY' or
                proof.get('status') != 'READY' or proof.get('security_review') != 'APPROVE' or
                proof.get('output_oid') != life['candidate_oid'] or
                not self.state.get('closeout_item') or self.blocking_open_findings()):
            raise ValueError('Q needs current SECURITY pass and frozen item; resolve blockers or abort')
        ingest = self.state['fake_ingest_receipt']
        baseline = candidate_tree.baseline_from_binding(ingest['baseline'])
        revision = candidate_tree.CandidateRevision(life['candidate_oid'], tuple(ingest['manifest']), 0)
        return q_proposal.materialize(baseline, revision, self.state['closeout_item'], c1, day)

    def q_review_verdict(self, answer):
        if answer.get('status') == 'APPROVE' and answer.get('full_review') == []:
            return 'APPROVE'
        if (answer.get('status') in ('APPROVE', 'REVISE') and
                self.findings_are_advisory(answer.get('full_review', []))):
            return 'APPROVE_WITH_ADVISORY'
        return None

    def q_proof(self, turn, oid):
        answer, role = turn['answer'], turn['role']
        findings = answer.get('findings', []) if role == 'gate' else answer.get('full_review', [])
        effective = self.q_review_verdict(answer) if role == 'reviewer' else None
        if (role == 'gate' and answer.get('verdict') in ('approve', 'needs-attention') and
                (answer['verdict'] == 'approve' or findings) and
                all(f.get('severity') == 'low' and not f.get('security') for f in findings)):
            effective = 'APPROVE_WITH_ADVISORY' if findings else 'APPROVE'
        if not effective:
            raise ValueError('Q verdict is blocking or empty; abort and start a new run')
        return {'role': role, 'phase': turn['phase'], 'sequence': turn['sequence'], 'oid': oid,
                'raw_verdict': answer.get('status', answer.get('verdict')), 'effective_verdict': effective,
                'advisories': copy.deepcopy(findings)}

    def fake_q_review(self, c1, day):
        proposal = self.fake_materialize_q(c1, day)
        if (self.state.get('fake_q_pending') or self.state.get('fake_q_review') or
                self.state.get('pending_reviewer_result_sequence') or self.state.get('q_reserved') != 8):
            raise ValueError('Q review pending or completed, or unreserved; abort and start a new run')
        ingest = self.state['fake_ingest_receipt']
        baseline = candidate_tree.baseline_from_binding(ingest['baseline'])
        env = candidate_tree._git_env(GIT_DIR=str(baseline.git_dir))
        oid = proposal['q_oid']
        revision = candidate_tree.CandidateRevision(
            oid, candidate_tree._manifest(env, baseline.tree_oid, oid), 0)
        baseline = candidate_tree.replace(baseline, authorized_prefixes=(*baseline.authorized_prefixes, 'BACKLOG.md'))
        candidate_tree._git(['update-ref', 'refs/paired-session/candidates/' + oid, oid], env=env)
        checkout = candidate_tree.rebuild_candidate_from_oid(baseline, revision)
        command = shlex.split(self.args.test_command)
        if not command or Path(command[0]).name == 'env':
            raise ValueError('Q test requires a direct executable')
        executable = Path(resolve_test_executable(checkout.root, self.args.test_command)).resolve()
        if any(root == executable or root in executable.parents for root in (self.workspace, self.run_dir)):
            raise ValueError('Q test executable is under writable operator roots')
        command[0] = str(executable)
        test_id = str(uuid.uuid4())
        pending = {'id': test_id, 'proposal': proposal, 'run_id': self.run_dir.name,
                   'item_uuid': self.state['item_uuid'], 'epoch': self.state['lifecycle']['epoch'],
                   'review_after_sequence': self.state['sequence'], 'binding_sha256': q_evidence.binding(self)}
        self.state['fake_q_pending'] = pending
        self.save()
        test = subprocess.run(command, cwd=checkout.root, timeout=self.args.timeout, capture_output=True,
                              env={'PATH': os.defpath, 'HOME': os.devnull, 'PYTHONDONTWRITEBYTECODE': '1',
                                   'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'})
        candidate_tree.verify_candidate_revision(checkout, revision)
        receipt = {**pending, 'oid': oid, 'command': command, 'returncode': test.returncode,
                   'stdout_sha256': hashlib.sha256(test.stdout).hexdigest(),
                   'stderr_sha256': hashlib.sha256(test.stderr).hexdigest(),
                   'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest()}
        atomic_json(self.evidence / (test_id + '-q-test.json'), receipt)
        if test.returncode:
            raise ValueError('Q tests failed; abort and start a new run')
        self._fake_dispatching = True
        try:
            self.state['q_reserved'] -= 2
            result = self.invoke('reviewer', 'Q',
                                 f'Role: reviewer, fresh. Phase: Q. Candidate Q OID: {oid}.\n'
                                 f'Run this test command exactly as written in one Bash call: {self.args.test_command}',
                                 review_schema(), fresh=True, workspace_override=checkout.root)
            candidate_tree.verify_candidate_revision(checkout, revision)
            turn = next(row for row in self.state['turns'] if row['sequence'] == result['sequence'])
            if (turn.get('error') or turn['phase'] != 'Q' or turn['workspace'] != str(checkout.root) or
                    turn['sequence'] <= pending['review_after_sequence'] or
                    not any(observed_test_succeeded(c, self.args.test_command)
                    for c in turn.get('observed_commands', [])) or
                    not self.q_review_verdict(result['answer'])):
                raise ValueError('Q reviewer did not approve; abort and start a new run')
            reviewed = {**receipt, 'review_id': str(uuid.uuid4()), 'sequence': result['sequence'],
                        'status': 'UNREVIEWED', 'root': str(checkout.root), 'index': str(checkout.index),
                        'root_identity': list(checkout.root_identity), 'proof': self.q_proof(turn, oid)}
            self.record_review_verdict(turn['sequence'], 'Q', turn['answer']['status'],
                                       reviewed['proof']['effective_verdict'])
            atomic_json(self.evidence / (reviewed['review_id'] + '-q-review.json'), reviewed)
            self.state['fake_q_review'] = reviewed
            self.state.pop('fake_q_pending')
            return reviewed
        finally:
            self._fake_dispatching = False
            self.state.pop('pending_reviewer_result_sequence', None)
            self.save()
    def fake_q_complete(self, c1, day):
        source, root, revision = q_evidence.review_source(self, c1, day, observed_test_succeeded)
        if (self._program_state()[1] or self.state.get('fake_q_bundle_pending') or
                self.state.get('fake_q_bundle') or self.state['sequence'] != source['sequence'] or
                self.state.get('q_reserved') != 6 or self.state['invocations_used'] + 6 > self.args.max_invocations):
            raise ValueError('Q completion uncertain or unreserved; abort and start a new run')
        life, p_oid = self.state['lifecycle'], source['proposal']['p_oid']
        noops = json.loads(json.dumps([r for r in life['receipts'] if r['epoch'] == life['epoch'] and
                 r['stage'] in ('FINISH', 'POLISH-Q', 'DOCS')]))
        if (len(noops) != 3 or {r['stage'] for r in noops} != {'FINISH', 'POLISH-Q', 'DOCS'} or
                any(r.get('status') != 'READY' or r['candidate_oid'] != p_oid or r['output_oid'] != p_oid or
                    r['item_uuid'] != life['item_uuid'] or r['parent'] != life['parent'] for r in noops) or
                not noops[0].get('finish_result') or not noops[1].get('specialists') or
                not noops[2].get('docs_file')):
            raise ValueError('Q requires current P no-op receipts; abort and start a new run')
        paths = candidate_tree._tree_entries(candidate_tree._git_env(GIT_DIR=str(root.git_dir)), revision.tree_oid)
        if any(sensitive_policy.sensitive_path_category(row[2]) for row in paths):
            raise ValueError('Q sensitive preflight blocked; abort and start a new run')
        template = Path(self.args.gate_prompt).read_text().replace('${REVIEW_TARGET_DESC}', str(root.root))
        gate_prompt = template.replace('${FOCUS_TEXT}', 'Audit complete Q tree ' + revision.tree_oid)
        prompts = [('gate', 'Q-GATE', gate_prompt, gate_schema()),
                   ('reviewer', 'Q-FINAL', 'Role: reviewer, fresh. Audit Q: regressions/docs/tests.', review_schema()),
                   ('reviewer', 'Q-SECURITY', 'Role: reviewer, fresh. Audit Q: secrets/escapes.', review_schema())]
        pending = {'id': str(uuid.uuid4()), 'source_id': source['review_id'], 'proofs': [], 'p_noops': noops}
        self.state['fake_q_bundle_pending'] = pending
        self._fake_dispatching = True
        try:
            previous = source['sequence']
            for role, phase, prompt, schema in prompts:
                self.state['q_reserved'] -= 2
                prompt += f'\n{phase}: Q={revision.tree_oid}; base={life["parent"]}; read task/plan in {self.context}\n'
                prompt += 'Run this test command exactly as written in one Bash call: ' + self.args.test_command
                result = self.invoke(role, phase, prompt, schema, fresh=True, workspace_override=root.root)
                candidate_tree.verify_candidate_revision(root, revision)
                turn = next(t for t in self.state['turns'] if t['sequence'] == result['sequence'])
                if (turn.get('error') or turn['role'] != role or turn['phase'] != phase or
                        turn['workspace'] != str(root.root) or turn['sequence'] <= previous):
                    raise ValueError('Q role did not approve exact tree; abort and start a new run')
                proof = self.q_proof(turn, revision.tree_oid)
                if (self.configured_test_failed(turn) or not any(
                        observed_test_succeeded(c, self.args.test_command) for c in turn.get('observed_commands', []))):
                    raise ValueError('Q role lacks observed checks; abort and start a new run')
                previous = turn['sequence']
                pending['proofs'].append(proof)
            q_evidence.review_source(self, c1, day, observed_test_succeeded)
            bundle = {**pending, 'status': 'REVIEWED', 'source': source, 'scanned_paths': [r[2] for r in paths]}
            atomic_json(self.evidence / (pending['id'] + '-q-bundle.json'), bundle)
            self.state['fake_q_bundle'] = bundle
            for proof in [source['proof'], *pending['proofs']]:
                rows = self.record_findings('q-' + proof['role'], proof['phase'],
                                            proof['sequence'], proof['advisories'])
                self.mark_advisory_findings(rows)
            self.state.pop('fake_q_bundle_pending')
            return bundle
        finally:
            self._fake_dispatching = False
            self.state.pop('pending_reviewer_result_sequence', None)
            self.save()

    def fake_candidate_author_turn(self, chain_only=False) -> dict:
        """Run one fake EXEC author against a clean isolated candidate root."""
        if (not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args) or
                self.state['phase'] != 'EXEC' or self.state['next'] != 'author' or
                self.state.get('status') != 'HOLD' or self.state.get('hold_reason') != PLAN_STOP_REASON or
                self.state.get('active') or self.state.get('uncertain_active') or
                self.state.get('pending_operator_note_id') or self.state.get('pending_rejection_id') or
                self.state.get('fake_candidate_pending') or self.state.get('fake_ingest_receipt')):
            raise RuntimeError('fake candidate author requires an unstarted EXEC turn')
        plan = (self.context / 'plan.md').read_bytes()
        docs_file = self.state['config'].get('docs_file')
        authorized = ['sum_ints.py', *([Path(docs_file).relative_to(self.workspace).as_posix()] if docs_file else [])]
        baseline = candidate_tree.prepare_candidate_baseline(
            self.workspace, self.run_dir, self.run_dir.parent, self.run_dir.parent, tuple(authorized))
        if chain_only and baseline.parent_head != self.state['lifecycle']['parent']:
            raise RuntimeError('fake candidate differs from frozen parent before author dispatch')
        request_id = str(uuid.uuid4())
        binding = {key: str(value) if isinstance(value, Path) else value
                   for key, value in vars(baseline).items()}
        self.state['fake_candidate_pending'] = {'request_id': request_id, 'baseline': binding,
            'plan_sha256': hashlib.sha256(plan).hexdigest(), 'epoch': self.state['lifecycle']['epoch'],
            'sequence': self.state['sequence'] + 1, 'item_uuid': self.state['item_uuid'],
            'chain_only': chain_only}
        self.state['sessions']['author'] = None
        self.state['started']['author'] = False
        self.save()
        prompt = (f'Role: persistent {self.args.author_vendor} implementer. Phase: EXEC.\n'
                  f'Workspace: {baseline.root}\nImplement approved plan (sha256={hashlib.sha256(plan).hexdigest()}):\n'
                  + plan.decode() + '\nReturn only JSON matching the supplied schema.')
        self._fake_dispatching = True
        try:
            result = self.invoke('author', 'EXEC', prompt, author_schema(),
                                 workspace_override=baseline.root,
                                 env_overrides={'FAKE_UNIQUE_CODEX_THREAD': '1'})
        finally:
            self._fake_dispatching = False
        if result['answer']['status'] != 'READY':
            raise RuntimeError('fake candidate author did not finish READY')
        revision = candidate_tree.ingest_candidate_revision(baseline)
        env = candidate_tree._git_env(GIT_DIR=str(baseline.git_dir))
        entries = candidate_tree._tree_entries(env, revision.tree_oid)
        digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
        receipt = {'id': request_id, 'run_id': self.run_dir.name, 'author_sequence': result['sequence'],
                   'input_oid': baseline.tree_oid, 'output_oid': revision.tree_oid,
                   'manifest_sha256': digest, 'manifest': revision.manifest,
                   'plan_sha256': hashlib.sha256(plan).hexdigest(),
                   'root': str(baseline.root), 'baseline': binding, 'chain_only': chain_only,
                   'epoch': self.state['lifecycle']['epoch'], 'item_uuid': self.state['item_uuid']}
        atomic_json(self.evidence / (request_id + '-ingest.json'), receipt)
        self.state['fake_ingest_receipt'] = receipt
        self.state.pop('fake_candidate_pending')
        self.save()
        return receipt

    def fake_candidate_oid_test(self) -> dict:
        """Run configured tests against a rebuilt checkout of the ingested OID."""
        ingest = self.state.get('fake_ingest_receipt')
        if (not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args) or
                not ingest or self.state.get('fake_candidate_test') or
                self.state.get('fake_candidate_test_pending') or
                self.state.get('fake_candidate_test_failed') or
                self.state.get('active') or self.state.get('uncertain_active')):
            raise RuntimeError('fake OID review requires a completed ingest')
        stored = json.loads((self.evidence / (ingest['id'] + '-ingest.json')).read_text())
        if stored != json.loads(json.dumps(ingest)): raise RuntimeError('persisted ingest evidence differs from state')
        baseline = candidate_tree.baseline_from_binding(ingest['baseline'])
        revision = candidate_tree.CandidateRevision(ingest['output_oid'], tuple(ingest['manifest']), 0)
        candidate_tree.verify_candidate_revision(baseline, revision)
        checkout = candidate_tree.rebuild_candidate_from_oid(baseline, revision)
        command = shlex.split(self.args.test_command)
        if not command or Path(command[0]).name == 'env': raise RuntimeError('fake OID test requires direct executable')
        executable = Path(resolve_test_executable(checkout.root, self.args.test_command)).resolve()
        if any(executable == root or root in executable.parents for root in (self.workspace, self.run_dir)):
            raise RuntimeError('OID test executable resolves outside candidate into writable roots')
        command[0] = str(executable)
        safe_path = [part for part in os.environ.get('PATH', '').split(os.pathsep)
                     if Path(part).is_absolute() and not any(
                         root == Path(part).resolve() or root in Path(part).resolve().parents
                         for root in (self.workspace, self.run_dir))]
        test_env = {'PATH': os.pathsep.join(safe_path), 'HOME': os.devnull,
                    'XDG_CONFIG_HOME': os.devnull, 'PYTHONDONTWRITEBYTECODE': '1',
                    'GIT_CEILING_DIRECTORIES': str(checkout.root.parent),
                    'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
        test_id = str(uuid.uuid4())
        self.state['fake_candidate_test_pending'] = test_id
        self.save()
        test = subprocess.run(command, cwd=checkout.root,
                              env=test_env,
                              capture_output=True, timeout=self.args.timeout)
        candidate_tree.verify_candidate_revision(checkout, revision)
        test_receipt = {'id': test_id, 'ingest_id': ingest['id'], 'oid': revision.tree_oid,
                        'command': command, 'returncode': test.returncode,
                        'root': str(checkout.root), 'root_identity': checkout.root_identity,
                        'index': str(checkout.index), 'run_id': self.run_dir.name,
                        'item_uuid': self.state['item_uuid'], 'epoch': self.state['lifecycle']['epoch'],
                        'manifest_sha256': ingest['manifest_sha256'],
                        'config_sha256': hashlib.sha256(json.dumps(
                            self.state['config'], sort_keys=True).encode()).hexdigest(),
                        'env_sha256': hashlib.sha256(json.dumps(test_env, sort_keys=True).encode()).hexdigest(),
                        'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
                        'stdout_sha256': hashlib.sha256(test.stdout).hexdigest(),
                        'stderr_sha256': hashlib.sha256(test.stderr).hexdigest()}
        atomic_json(self.evidence / (test_id + '-oid-test.json'), test_receipt)
        self.state.pop('fake_candidate_test_pending')
        if test.returncode:
            self.state['fake_candidate_test_failed'] = test_id
            self.save()
            raise RuntimeError('OID-bound coordinator test failed')
        self.state['fake_candidate_test'] = test_receipt
        self.save()
        return test_receipt

    def fake_reserved_docs_writer_ingest(self) -> dict:
        if not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args):
            raise RuntimeError('reserved DOCS ingest requires fake-only dispatch')
        life = self.state['lifecycle']
        prior = life['receipts'][-1] if life['receipts'] else {}
        old = self.state.get('fake_ingest_receipt') or {}
        marker = life.get('reserved_docs_constraint') or {}
        docs_file = marker.get('docs_file') or self.state['config'].get('docs_file')
        docs_path = Path(docs_file) if docs_file else None
        if docs_path and docs_path.is_absolute(): docs_path = docs_path.relative_to(self.workspace)
        docs_path = docs_path.as_posix() if docs_path else None
        if (life['stage'] != 'EXEC' or (marker and life['epoch'] <= marker['epoch']) or
                prior.get('stage') != 'DOCS' or prior.get('output_oid') != life['candidate_oid'] or
                prior.get('docs_file') != docs_path or not old.get('chain_only') or
                self.state.get('active') or self.state.get('uncertain_active')):
            raise RuntimeError('reserved DOCS ingest lacks a current writer receipt')
        baseline = candidate_tree.baseline_from_binding(old['baseline'])
        paths = tuple(sorted(set(baseline.authorized_prefixes) | {docs_path}))
        baseline = candidate_tree.replace(baseline, authorized_prefixes=paths)
        revision = candidate_tree.ingest_candidate_revision(baseline)
        if revision.tree_oid != prior['output_oid']:
            raise RuntimeError('reserved DOCS output differs from candidate OID')
        env = candidate_tree._git_env(GIT_DIR=str(baseline.git_dir))
        entries = candidate_tree._tree_entries(env, revision.tree_oid)
        binding = {key: str(value) if isinstance(value, Path) else value
                   for key, value in vars(baseline).items()}
        receipt = {**old, 'id': str(uuid.uuid4()), 'input_oid': old['output_oid'],
                   'output_oid': revision.tree_oid, 'manifest': revision.manifest,
                   'manifest_sha256': hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest(),
                   'baseline': binding, 'epoch': life['epoch'],
                   'source_writer_request_id': prior['request_id']}
        atomic_json(self.evidence / (receipt['id'] + '-ingest.json'), receipt)
        for key in ('fake_candidate_test', 'fake_candidate_review', 'fake_candidate_chain'):
            self.state.pop(key, None)
        self.state['fake_ingest_receipt'] = receipt
        self.save()
        return receipt

    def fake_finish_writer_ingest(self) -> dict:
        if not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args):
            raise RuntimeError('FINISH ingest requires fake-only dispatch')
        life = self.state['lifecycle']
        prior = life['receipts'][-1] if life['receipts'] else {}
        old = self.state.get('fake_ingest_receipt') or {}
        if (life['stage'] != 'EXEC' or life['epoch'] < 1 or prior.get('stage') != 'FINISH' or
                prior.get('output_oid') != life['candidate_oid'] or not old.get('chain_only') or
                self.state.get('active') or self.state.get('uncertain_active')):
            raise RuntimeError('FINISH ingest lacks a current writer receipt')
        baseline = candidate_tree.baseline_from_binding(old['baseline'])
        revision = candidate_tree.ingest_candidate_revision(baseline)
        if revision.tree_oid != prior['output_oid']:
            raise RuntimeError('FINISH output differs from candidate OID')
        env = candidate_tree._git_env(GIT_DIR=str(baseline.git_dir))
        entries = candidate_tree._tree_entries(env, revision.tree_oid)
        binding = {key: str(value) if isinstance(value, Path) else value
                   for key, value in vars(baseline).items()}
        receipt = {**old, 'id': str(uuid.uuid4()), 'input_oid': old['output_oid'],
                   'output_oid': revision.tree_oid, 'manifest': revision.manifest,
                   'manifest_sha256': hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest(),
                   'baseline': binding, 'epoch': life['epoch'],
                   'source_writer_request_id': prior['request_id']}
        atomic_json(self.evidence / (receipt['id'] + '-ingest.json'), receipt)
        for key in ('fake_candidate_test', 'fake_candidate_review', 'fake_candidate_chain'):
            self.state.pop(key, None)
        self.state['fake_ingest_receipt'] = receipt
        self.save()

    def _fake_reentry(self) -> bool:
        life = self.state['lifecycle']
        prior = life['receipts'][-1] if life['receipts'] else {}
        current = self.state.get('fake_ingest_receipt') or {}
        if current.get('source_writer_request_id') != prior.get('request_id'):
            if prior.get('stage') == 'DOCS': self.fake_reserved_docs_writer_ingest()
            elif prior.get('stage') == 'FINISH': self.fake_finish_writer_ingest()
            else:
                self.hold('EXEC receipt needs abort or new run' if prior.get('stage') == 'EXEC' else
                          'unsupported writer: abort or start a new run')
                return False
        if self.state.get('fake_candidate_test', {}).get('ingest_id') != self.state['fake_ingest_receipt']['id']:
            self.fake_candidate_oid_test()
        if self.state.get('fake_candidate_chain', {}).get('ingest_id') != self.state['fake_ingest_receipt']['id']:
            self.fake_candidate_oid_review()
        if prior.get('stage') == 'DOCS':
            test = self.state['fake_candidate_test']
            self.state['lifecycle']['receipts'][-1].update(
                retested_oid=test['oid'], retest_id=test['id'])
            self.save()
        return True

    def fake_candidate_oid_review(self) -> dict:
        ingest, test = self.state.get('fake_ingest_receipt'), self.state.get('fake_candidate_test')
        if (not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args) or
                not ingest or not test or self.state.get('fake_candidate_test_failed') or
                self.state.get('fake_candidate_review') or self.state.get('fake_candidate_review_rejected') or
                self.state.get('active') or
                self.state.get('uncertain_active') or self.state['lifecycle']['stage'] != 'EXEC'):
            raise RuntimeError('fake OID review requires a current successful test')
        identity = (self.run_dir.name, self.state['item_uuid'], self.state['lifecycle']['epoch'])
        if (any((ingest[k], test[k]) != (v, v) for k, v in zip(('run_id', 'item_uuid', 'epoch'), identity)) or
                test['ingest_id'] != ingest['id'] or
                test['oid'] != ingest['output_oid'] or test['manifest_sha256'] != ingest['manifest_sha256'] or
                test['config_sha256'] != hashlib.sha256(json.dumps(
                    self.state['config'], sort_keys=True).encode()).hexdigest() or
                hashlib.sha256((self.context / 'plan.md').read_bytes()).hexdigest() != ingest['plan_sha256']):
            raise RuntimeError('fake test receipt does not bind current ingest')
        saved = json.loads((self.evidence / (test['id'] + '-oid-test.json')).read_text())
        if saved != json.loads(json.dumps(test)): raise RuntimeError('test evidence differs from state')
        if hashlib.sha256(Path(test['command'][0]).read_bytes()).hexdigest() != test['executable_sha256']:
            raise RuntimeError('OID test executable changed')
        baseline = candidate_tree.baseline_from_binding(ingest['baseline'])
        revision = candidate_tree.CandidateRevision(ingest['output_oid'], tuple(ingest['manifest']), 0)
        checkout = candidate_tree.replace(baseline, root=Path(test['root']), index=Path(test['index']),
                                          root_identity=tuple(test['root_identity']))
        candidate_tree.verify_candidate_revision(checkout, revision)
        proof = (f'Phase: EXEC.\nWorkspace: {checkout.root}\nCandidate OID: {revision.tree_oid}; test: {test["id"]}\n'
                 f'Approved plan:\n{(self.context / "plan.md").read_text()}\nOID delta:\n{ingest["manifest"]}\n'
                 f'Run this test command exactly as written in one Bash call: {self.args.test_command}')
        self.state['fake_candidate_review_rejected'] = (revision.tree_oid, test['id'])
        self.save()
        self._fake_dispatching = True
        try:
            review = self.invoke('reviewer', 'EXEC', 'Role: reviewer, fresh. ' + proof,
                                 review_schema(), fresh=True, workspace_override=checkout.root)
            candidate_tree.verify_candidate_revision(checkout, revision)
            if review['answer']['status'] != 'APPROVE' or review['answer']['full_review']:
                raise RuntimeError('OID reviewer did not approve')
            reviewed = {'id': str(uuid.uuid4()), 'test_id': test['id'],
                        'oid': revision.tree_oid, 'sequence': review['sequence'],
                        'run_id': self.run_dir.name, 'item_uuid': identity[1], 'epoch': identity[2],
                        'ingest_id': ingest['id']}
            atomic_json(self.evidence / (reviewed['id'] + '-oid-review.json'), reviewed)
            self.state['fake_candidate_review'] = reviewed
            self.save()
            gate = self.invoke('gate', 'EXEC', 'You are an adversarial reviewer. ' + proof,
                               gate_schema(), fresh=True, workspace_override=checkout.root)
            candidate_tree.verify_candidate_revision(checkout, revision)
            if gate['answer']['verdict'] != 'approve' or gate['answer']['findings']:
                raise RuntimeError('OID gate did not approve')
            chain = {'id': str(uuid.uuid4()), 'ingest_id': ingest['id'], 'test_id': test['id'], 'identity': identity,
                     'review_id': reviewed['id'], 'gate_sequence': gate['sequence'], 'oid': revision.tree_oid,
                     'root': str(checkout.root), 'root_identity': checkout.root_identity}
            atomic_json(self.evidence / (chain['id'] + '-oid-chain.json'), chain)
            self.state.pop('fake_candidate_review_rejected')
            self.state['fake_candidate_chain'] = chain
            self.save()
        finally:
            self.state.pop('pending_reviewer_result_sequence', None)
            self._fake_dispatching = False
            self.save()

    def fake_candidate_approval(self) -> dict:
        try:
            return self._fake_candidate_approval()
        except candidate_tree.CandidateError as exc:
            raise RuntimeError(f'fake approval candidate changed: {exc}') from exc
        except (KeyError, TypeError, IndexError, OSError, ValueError) as exc:
            raise RuntimeError('fake approval receipt is missing or malformed') from exc

    def _fake_candidate_approval(self) -> dict:
        if not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args):
            raise RuntimeError('fake approval requires fake-only dispatch')
        state = self.state
        ingest, test = state.get('fake_ingest_receipt'), state.get('fake_candidate_test')
        reviewed, chain = state.get('fake_candidate_review'), state.get('fake_candidate_chain')
        life = state['lifecycle']
        blockers = self.blocking_open_findings()
        owner = life.get('awaiting_owner_reverify') or {}
        constraint = life.get('reserved_docs_constraint') or {}
        downstream = (constraint.get('owner') == owner.get('owner') == 'security-reviewer' and
                      set(constraint.get('owner_ids', ())) == {row['id'] for row in blockers} and
                      all(row['source'] == 'security-reviewer' for row in blockers))
        source_rows = self._coord_doc_blockers(constraint.get('docs_file'), constraint.get('owner_ids', ()))
        downstream = downstream or constraint.get('owner') == 'coordinator' and bool(source_rows)
        if (not all((ingest, test, reviewed, chain)) or state.get('fake_candidate_review_rejected') or
                state.get('active') or state.get('uncertain_active') or (blockers and not downstream) or
                state.get('fake_candidate_test_failed') or state.get('fake_candidate_test_pending') or
                state.get('fake_candidate_pending') or
                state.get('pending_reviewer_result_sequence') or test['returncode'] != 0):
            raise RuntimeError('fake approval lacks a clean receipt chain')
        if (state.get('status') != 'HOLD' or state.get('hold_reason') != PLAN_STOP_REASON or
                life.get('pending') or life.get('item_uuid') != state['item_uuid'] or
                state.get('fake_route_consumed') == chain.get('id')):
            raise RuntimeError('fake approval lifecycle is not ready for chain-only routing')
        if life['epoch'] == 0:
            if life.get('candidate_oid') is not None or life.get('receipts'):
                raise RuntimeError('fake approval initial lifecycle is not empty')
        else:
            prior = life['receipts'][-1] if life.get('receipts') else {}
            if (life.get('candidate_oid') != chain['oid'] or prior.get('stage') not in ('FINISH', 'DOCS') or
                    prior.get('output_oid') != chain['oid'] or
                    ingest.get('source_writer_request_id') != prior.get('request_id')):
                raise RuntimeError('fake approval has no current writer ingest')
        identity = [self.run_dir.name, state['item_uuid'], life['epoch']]
        if (list(chain['identity']) != identity or reviewed['run_id'] != identity[0] or
                reviewed['item_uuid'] != identity[1] or reviewed['epoch'] != identity[2] or
                chain['ingest_id'] != ingest['id'] or chain['test_id'] != test['id'] or
                test['ingest_id'] != ingest['id'] or
                chain['review_id'] != reviewed['id'] or reviewed['ingest_id'] != ingest['id'] or
                chain['oid'] != reviewed['oid'] or chain['oid'] != test['oid'] or
                chain['oid'] != ingest['output_oid'] or chain['root'] != test['root'] or
                list(chain['root_identity']) != list(test['root_identity']) or life['stage'] != 'EXEC'):
            raise RuntimeError('fake approval has stale or unrelated receipts')
        for row, suffix in ((ingest, 'ingest'), (test, 'oid-test'),
                            (reviewed, 'oid-review'), (chain, 'oid-chain')):
            saved = json.loads((self.evidence / (row['id'] + '-' + suffix + '.json')).read_text())
            if saved != json.loads(json.dumps(row)):
                raise RuntimeError('fake approval evidence differs from state')
        turns = {row['sequence']: row for row in state['turns']}
        reviewer, gate = turns[reviewed['sequence']], turns[chain['gate_sequence']]
        if (reviewer['role'] != 'reviewer' or gate['role'] != 'gate' or
                reviewer['phase'] != 'EXEC' or gate['phase'] != 'EXEC' or
                reviewer.get('error') or gate.get('error') or
                reviewer.get('verified_claims_error') or gate.get('verified_claims_error') or
                reviewer['answer']['status'] != 'APPROVE' or reviewer['answer']['full_review'] or
                gate['answer']['verdict'] != 'approve' or gate['answer']['findings'] or
                reviewed['sequence'] >= chain['gate_sequence']):
            raise RuntimeError('fake approval roles or verdicts differ')
        if any(row.get('run_id') != identity[0] or row.get('workspace') != test['root']
               for row in (reviewer, gate)): raise RuntimeError('fake approval turn identity differs')
        for row in (reviewer, gate):
            path = self.evidence / f"{row['sequence']:03d}-exec-{row['role']}.receipt.json"
            if json.loads(path.read_text()) != json.loads(json.dumps(row)):
                raise RuntimeError('fake approval turn evidence differs')
        if (hashlib.sha256((self.context / 'plan.md').read_bytes()).hexdigest() != ingest['plan_sha256'] or
                hashlib.sha256(json.dumps(state['config'], sort_keys=True).encode()).hexdigest() !=
                test['config_sha256'] or
                hashlib.sha256(Path(test['command'][0]).read_bytes()).hexdigest() != test['executable_sha256']):
            raise RuntimeError('fake approval inputs changed')
        if any(row['sequence'] > chain['gate_sequence'] and row['role'] == 'author'
               for row in state['turns']): raise RuntimeError('fake approval has a later author')
        if any(row['sequence'] > ingest['author_sequence'] and row['role'] in
               ('finisher', 'docs', 'security-writer') for row in state['turns']):
            raise RuntimeError('fake approval has a later writer')
        baseline = candidate_tree.baseline_from_binding(ingest['baseline'])
        revision = candidate_tree.CandidateRevision(chain['oid'], tuple(ingest['manifest']), 0)
        if baseline.parent_head != life['parent']:
            raise RuntimeError('fake approval differs from frozen parent')
        candidate_tree.verify_candidate_revision(baseline, revision)
        fields = {'run_id': identity[0], 'convergence_id': chain['id'], 'epoch': identity[2],
                  'candidate_oid': chain['oid'], 'parent_head': baseline.parent_head,
                  'workspace': str(self.workspace.resolve()), 'run_dir': str(self.run_dir.resolve()),
                  'phase': 'EXEC'}
        source = {'fake_only': True, 'ingest_id': ingest['id'], 'test_id': test['id']}
        proof = {**fields, **source, 'blocking_findings': [],
                 'downstream_open_findings': tuple(row['id'] for row in blockers),
                 'reviewer': {**fields, **source, 'role': 'reviewer', 'status': 'APPROVE',
                              'receipt_id': reviewed['id'], 'sequence': reviewed['sequence']},
                 'gate': {**fields, **source, 'role': 'gate', 'verdict': 'approve',
                          'sequence': chain['gate_sequence']}}
        return {'status': 'APPROVE', 'epoch': identity[2], 'item_uuid': identity[1],
                'run_id': identity[0], 'parent': baseline.parent_head,
                'candidate_oid': chain['oid'], 'proof': proof}

    def _freeze_fake_exec_source(self) -> None:
        self.state.pop('fake_exec_source', None)
        rows = [row for row in self.state['turns'] if row.get('phase') == 'EXEC']
        author = next((row for row in reversed(rows) if row['role'] == 'author'), None)
        if author is None:
            raise RuntimeError('fake EXEC has no author turn')
        later = [row for row in rows if row['sequence'] > author['sequence']]
        comparison = self.state['exec_comparisons'][-1] if self.state['exec_comparisons'] else {}
        verdict = comparison.get('effective_verdict')
        reviewer = next((row for row in later if row['role'] == 'reviewer' and
                         row['sequence'] == comparison.get('review_sequence')), None)
        gate = next((row for row in reversed(later) if row['role'] == 'gate' and not row.get('error') and
                     not row.get('verified_claims_error') and reviewer and
                     row['sequence'] > reviewer['sequence']), None)
        snapshot = git_snapshot(self.workspace)[0]
        if (not reviewer or not gate or verdict not in ('APPROVE', 'APPROVE_WITH_ADVISORY') or
                reviewer.get('error') or reviewer.get('verified_claims_error') or
                any(row.get('run_id') != self.run_dir.name for row in (author, reviewer, gate)) or
                reviewer.get('answer', {}).get('status') != 'APPROVE' or
                gate.get('answer', {}).get('verdict') != 'approve' or
                gate.get('answer', {}).get('findings') or
                any(row.get('snapshot_before') != snapshot or
                    row.get('answer', {}).get('reviewed_snapshot') != snapshot for row in (reviewer, gate))):
            raise RuntimeError('fake EXEC lacks current reviewer and gate receipts')
        self.state['fake_exec_source'] = {'run_id': self.run_dir.name,
                                          'author_sequence': author['sequence'],
                                          'reviewer_sequence': reviewer['sequence'],
                                          'gate_sequence': gate['sequence'],
                                          'workspace_snapshot': snapshot}
        self.save()

    def _drive_loop(self) -> str:
        if self.state.get('uncertain_active'):
            return self.hold('uncertain CLI turn; inspect evidence, then use resume --retry-uncertain')
        if self.state['active']:
            self.state['uncertain_active'] = self.state['active']
            return self.hold('uncertain in-flight CLI turn; inspect evidence, then use resume --retry-uncertain')
        while self.state['status'] == 'ACTIVE':
            if self.state['next'] != 'author': self.refuse_rejected_tree(stale_done=True)
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
        if self._fake_lifecycle: raise RuntimeError('fake lifecycle cannot enter legacy resume')
        if self.state['status'] == 'ACCEPTED': return 'ACCEPTED'
        self.refuse_rejected_tree(stale_done=True, allow_author=self.state.get('next') == 'author' or
                                  bool(self.state.get('uncertain_active') or self.state.get('active')))
        if self.args.acknowledge_codex_trust:
            if self.args.acknowledge_codex_trust != self.run_dir.name or not self.state.get('uncertain_active') or not self.state.get('hold_reason', '').startswith('global Codex config changed during uncertain turn'):
                raise ValueError('trust acknowledgment requires the named run and a prior uncertain trust HOLD')
            retry_uncertain = True
        if self.state['status'] == 'ABORTED':
            raise ValueError('run was ABORTED by scope change; start its successor')
        if self.state['status'] == 'ACCEPTED':
            return 'ACCEPTED'
        if self.state['status'] == 'HOLD' and self.state.get('terminal_hold_kind') == 'rejection_limit':
            return self.rejection_limit_hold()
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
                retry_killpg_eperm(pid)
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
        self.refuse_rejected_tree(stale_done=True, allow_author=self.state.get('next') == 'author')
        self.state['config']['timeout'] = self.args.timeout
        self.state['config']['exec_turn_timeout'] = self.args.exec_turn_timeout
        self.save()
        if uncertain and getattr(self, '_probe_gate_required', False):
            passed, reason = self.probe_passed()
            if not passed:
                return self.hold('permission probe stale after uncertain turn: ' + reason)
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
        abandoned_usage = []
        for turn in self.state.get('abandoned_turns', []):
            requests = turn.get('usage_requests', [])
            if requests:
                abandoned_usage.append({
                    'sequence': turn.get('sequence'), 'role': turn.get('role'),
                    'phase': turn.get('phase'), 'provider_usage': 'reported',
                    'requests': requests,
                    'input_tokens': sum(use.get('input', 0) for use in requests),
                    'cached_tokens': sum(use.get('cached', 0) for use in requests),
                    'output_tokens': sum(use.get('output', 0) for use in requests),
                })
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
                  'abandoned_turn_usage': abandoned_usage,
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
        lines += ['', '## Abandoned turn usage', '',
                  '| Sequence | Role | Phase | Requests | Input | Cached | Output |',
                  '|---:|---|---|---:|---:|---:|---:|']
        for row in abandoned_usage:
            lines.append(f"| {row['sequence']} | {row['role']} | {row['phase']} | {len(row['requests'])} | {row['input_tokens']} | {row['cached_tokens']} | {row['output_tokens']} |")
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


class StoreExplicitInteger(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, self.dest + '_explicit', True)


def config_bool(value):
    if type(value) is bool: return value
    if value in ('true', 'false'): return value == 'true'
    raise ValueError('expected true or false')


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(allow_abbrev=False)
    p.add_argument('action', choices=['run', 'resume', 'abort', 'snapshot', 'permission-probe',
                                      'accept', 'reject', 'note', 'status'])
    p.add_argument('--workspace', required=True)
    p.add_argument('--workitem', required=True)
    p.add_argument('--run-dir', required=True)
    p.add_argument('--supersedes', help='resolved parent run dir for a scope-change successor')
    p.add_argument('--config', help='JSON profile; defaults to <workspace>/.review-loop/paired-session.json')
    p.add_argument('--author-vendor', choices=['codex', 'claude'], default='codex')
    p.add_argument('--author-model', help='defaults to the vendor-pinned ADR-8 model')
    p.add_argument('--author-effort', default='medium')
    p.add_argument('--reviewer-vendor', choices=['codex', 'claude'], default='claude')
    p.add_argument('--reviewer-model', help='defaults to the vendor-pinned ADR-8 model')
    p.add_argument('--reviewer-effort', default='medium')
    p.add_argument('--gate-model', help='defaults to the vendor-pinned ADR-8 model')
    p.add_argument('--gate-effort', default='medium')
    p.add_argument('--shadow', choices=['on', 'off'], default='on')
    p.add_argument('--adversarial-gate', choices=['on', 'off'], default='on')
    p.add_argument('--polish-round', choices=['on', 'off'], default='on')
    p.add_argument('--lifecycle-mode', choices=['off', 'on'], default='off')
    p.add_argument('--docs-file', default='')
    p.add_argument('--docs-allowlist', action='append', default=[])
    p.add_argument('--skip-globs', action='append', default=[])
    p.add_argument('--skip-quality-polish', type=config_bool, default=False)
    p.add_argument('--gate-prompt', default=str(DEFAULT_GATE_PROMPT))
    p.add_argument('--max-plan-rounds', type=int, default=3)
    p.add_argument('--max-exec-rounds', type=int, default=4)
    p.add_argument('--max-invocations', type=int, default=25)
    p.add_argument('--timeout', type=int, default=2700)
    p.set_defaults(exec_turn_timeout_explicit=False)
    p.add_argument('--exec-turn-timeout', type=int, default=None, action=StoreExplicitInteger,
                   help=f'EXEC author turn timeout (default max({DEFAULT_EXEC_TURN_TIMEOUT_SECONDS}, --timeout), '
                        f'capped at {MAX_EXEC_TURN_TIMEOUT_SECONDS} seconds)')
    p.add_argument('--resume-timeout', type=int, default=None,
                   help=f'increase the saved timeout on resume, up to {MAX_RESUME_TIMEOUT_SECONDS} seconds')
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
    p.add_argument('--acknowledge-codex-trust', metavar='RUN_ID',
                   help='after inspecting an uncertain trust-only change, acknowledge this run ID on resume')
    p.add_argument('--polish', action='store_true',
                   help='with resume, run only the one-time polish round on an older DONE run')
    p.add_argument('--skip-probe', action='store_true',
                   help='explicitly bypass the permission-probe gate (tests only)')
    p.add_argument('--text', help='operator rejection note (reject action)')
    p.add_argument('--file', help='read operator rejection note from this file (reject action)')
    p.add_argument('--override-rejection', action='store_true', help='operator ruling on a held rejected tree')
    p.add_argument('--reason', help='attributed reason for an operator rejection override')
    p.add_argument('--expect', help='operator intent digest required by accept/reject')
    p.add_argument('--intent-only', action='store_true', help='print an operator intent for confirmation')
    p.add_argument('--scope-change', action='store_true', help='end this run and print a successor command')
    return p


CONFIGURABLE_DESTS = {
    'author_vendor', 'author_model', 'author_effort', 'reviewer_vendor',
    'reviewer_model', 'reviewer_effort', 'gate_model', 'gate_effort', 'shadow',
    'adversarial_gate', 'polish_round', 'gate_prompt', 'max_plan_rounds',
    'max_exec_rounds', 'max_invocations', 'timeout', 'exec_turn_timeout', 'test_command',
    'reviewer_command', 'codex_bin', 'claude_bin', 'author_subagents',
    'lifecycle_mode', 'docs_file', 'docs_allowlist', 'skip_globs', 'skip_quality_polish',
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
            raise ValueError(f'{key} must be {expected} for the {vendor} role under ADR-8; got {actual}')


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
        if action.dest in ('reviewer_command', 'docs_allowlist', 'skip_globs'):
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f'config {key} must be an array of strings')
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
    if args.supersedes: args.supersedes = str(Path(args.supersedes).expanduser().resolve())
    gate_prompt = Path(args.gate_prompt).expanduser()
    if not gate_prompt.is_absolute():
        gate_prompt = Path(args.workspace) / gate_prompt
    args.gate_prompt = str(gate_prompt.resolve())
    return args


def _execute_locked(args: argparse.Namespace) -> int:
    if args.scope_change and args.action not in ('note', 'reject'):
        raise ValueError('--scope-change requires note or reject')
    if args.action == 'status':
        return print((Path(args.run_dir) / 'state.json').read_text()) or 0
    co = Coordinator(args)
    if args.intent_only: return print(json.dumps(co.operator_intent(args.action, args.text, args.file))) or 0
    if args.scope_change:
        print(co.scope_change(args.text, args.file))
        return 0
    if args.action == 'note':
        print('NOTE: ' + co.note(args.text, args.file))
        return 0
    if args.action == 'permission-probe':
        try:
            passed = co.permission_probe(retry_uncertain=args.retry_uncertain)
        except RuntimeError as exc:
            if co.state.get('status') != 'DONE': co.hold('permission probe: ' + str(exc))
            passed = False
        suffix = ': ' + co.state.get('hold_reason', '') if co.state.get('hold_reason') else ''
        print(('PASS' if passed else 'FAIL') + suffix)
        return 0 if passed else 2
    if args.action == 'accept':
        status = co.accept()
        print(status)
        return 0
    if args.action == 'reject':
        if not args.skip_probe:
            passed, reason = co.probe_passed()
            if not passed:
                print('REFUSED: ' + reason + '; run permission-probe before continuing')
                return 2
        status = co.reject(args.text, args.file)
        if status == 'ACTIVE':
            status = co.drive()
        print(status + (' (acceptance pending)' if status == 'DONE' else
                        ': ' + co.state.get('hold_reason', '') if status == 'HOLD' else ''))
        return 0 if status in ('DONE', 'ACCEPTED') else 2
    pending = co.state.get('uncertain_active') or {}
    before_config = pending.get('global_codex_before', {}).get('codex_config')
    changed_codex = (args.action == 'resume' and args.retry_uncertain and not args.polish
                     and pending.get('vendor') == 'codex'
                     and before_config and (co.run_dir / 'permission-probe.json').exists()
                     and global_config_snapshot(co.global_config_home, co.global_codex_home).get(
                         'codex_config', {}).get('sha256') != before_config)
    if args.action in ('run', 'resume') and not args.skip_probe:
        passed, reason = co.probe_passed()
        if not passed and not changed_codex:
            print('REFUSED: ' + reason + '; run permission-probe before continuing')
            return 2
    co._probe_gate_required = not args.skip_probe
    if args.action == 'abort':
        if co.state.get('status') == 'ACCEPTED':
            print('ACCEPTED')
            return 0
        suffix = ('; a prior CLI child may still be running; inspect uncertain_active before retry'
                  if co.state.get('active') or co.state.get('uncertain_active') else '')
        co.hold('aborted by operator' + suffix)
        print('HOLD: ' + co.state['hold_reason'])
        return 2
    status = (co.drive() if args.action == 'run' else
              co.resume_polish() if args.polish else co.resume(args.retry_uncertain))
    print(status + (' (acceptance pending)' if status == 'DONE' else
                    ': ' + co.state.get('hold_reason', '') if status == 'HOLD' else ''))
    return 0 if status in ('DONE', 'ACCEPTED') else 2


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
    roots = (Path(args.workspace), Path(args.run_dir))
    os.environ['PATH'] = safe_path(os.environ.get('PATH', ''), (*roots, roots[1] / 'author-tmp'))
    if args.skip_probe and not lifecycle_spine.fake_dispatch_guard(args):
        print('REFUSED: --skip-probe is limited to the fake test harness')
        return 2
    if args.scope_change and args.action not in ('note', 'reject'):
        print('REFUSED: --scope-change requires note or reject')
        return 2
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
    if args.action == 'note' and not (run_dir / 'state.json').is_file():
        print('REFUSED: note requires an existing coordinator run')
        return 2
    if args.action in ('run', 'resume', 'permission-probe', 'reject', 'note'):
        if re.search(r'[*?\[\]{}]', str(run_dir)):
            print('REFUSED: --run-dir must not contain glob metacharacters used by Claude Edit deny rules')
            return 2
        try:
            resolve_test_executable(workspace, args.test_command)
        except ValueError as exc:
            print('REFUSED: ' + str(exc))
            return 2
    try:
        with run_lease(Path(args.run_dir)):
            if args.action in ('run', 'resume', 'permission-probe', 'accept', 'reject', 'note'):
                with workspace_lease(workspace, run_dir):
                    return _execute_locked(args)
            return _execute_locked(args)
    except ValueError as exc:
        print('REFUSED: ' + str(exc))
        return 2
    except (RunLeaseError, OSError, RuntimeError) as exc:
        print('HOLD: ' + str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
