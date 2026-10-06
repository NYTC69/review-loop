#!/usr/bin/env python3
"""Stdlib-only paired PLAN/EXEC coordinator for one real work item.

The coordinator owns transport, snapshots, limits, and evidence.  Models never
write transport files.  Persistent author/reviewer threads span both phases;
shadow and adversarial reviewers are always fresh.
"""
from __future__ import annotations

import argparse
import calendar
import copy
from contextlib import contextmanager, nullcontext
from datetime import date, datetime
import errno
import itertools
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
import traceback
import uuid
from typing import Optional
try:
    from paired_session import budget_policy
    from paired_session import candidate_tree
    from paired_session import closeout_policy
    from paired_session import q_proposal
    from paired_session import q_evidence
    from paired_session import claude_author_probe as cap
    from paired_session import delivery_intent, delivery_seal, delivery_close, candidate_test_sandbox
    from paired_session import codex_capability_guard
    from paired_session import docs_policy
    from paired_session import evidence_guard
    from paired_session import ignored_config
    from paired_session import readonly_guard
    from paired_session import finish_dispatch
    from paired_session import lifecycle_spine
    from paired_session import operator_verification as opv
    from paired_session import sensitive_policy
    from paired_session import security_repair_policy
    from paired_session import worktree_lifecycle
    from paired_session import review_report
    from paired_session import timeout_scale
    from paired_session.program_binding import snapshot as program_snapshot, safe_path
except ModuleNotFoundError:
    import budget_policy
    import candidate_tree
    import closeout_policy
    import q_proposal
    import q_evidence
    import claude_author_probe as cap
    import delivery_intent, delivery_seal, delivery_close, candidate_test_sandbox
    import codex_capability_guard
    import docs_policy
    import evidence_guard
    import ignored_config
    import readonly_guard
    import finish_dispatch
    import lifecycle_spine
    import operator_verification as opv
    import sensitive_policy
    import security_repair_policy
    import worktree_lifecycle
    import review_report
    import timeout_scale
    from program_binding import snapshot as program_snapshot, safe_path

HERE = Path(__file__).resolve().parent
DEFAULT_GATE_PROMPT = HERE.parent / 'scripts' / 'adversarial_gate_fallback_prompt.txt'
RUBRIC = ('Trigger', 'Reachability', 'Impact', 'Likelihood', 'Fix cost', 'Cheaper response')
# FIELD-5 (owner 2026-10-03/10-04): the finding's owner labels its class explicitly; the coordinator never infers it from text.
CLASS_LABEL_RE = re.compile(r'\s*\[class:\s*([a-z0-9]+(?:-[a-z0-9]+)*)\s*\]')
STRUCTURAL_BLOCK_STREAK = 3
CLASS_LABEL_GUIDANCE = ('Start the {field} of every blocking finding with a defect-class label "[class: <kebab-case-name>]" naming the '
                        'kind of defect, such as missing-input-validation or path-traversal.')   # fresh roles: no history words (independence scan)
CLASS_LABEL_REUSE = ' Reuse the exact label of an earlier finding of the same class of defect.'
PROBE_SURFACE_VERSION = 9
CLAUDE_CHILD_ENV = {'DISABLE_AUTOUPDATER': '1', 'FORCE_AUTOUPDATE_PLUGINS': None}   # RF-4: set by cli_env; bound into the Claude flags digests
CODEX_PLUGINS_OFF = ('-c', 'features.plugins=false')   # CG-1: on every Codex dispatch (author, reviewer, gate, probe); bundles are inert (.compass/results/2026-10-01_cg-codex-plugin-evidence.md)
# D-LG1 review-only entry (docs/review-only-entry.md); the owner's provisional choices, one place each:
REVIEW_ONLY_DEFAULT_BASE = 'HEAD'            # Q2: the uncommitted work, as legacy --review-only
REVIEW_ONLY_BASE_MUST_BE_ANCESTOR = True     # Q5: refuse a base that is not an ancestor of HEAD
REVIEW_ONLY_EXISTING_CHANGE_ROUNDS = 1       # Q9: the pre-existing change is EXEC round 1 (the author's round is skipped)
REVIEW_ONLY_LIFECYCLE_READY = True           # LG1-c: the delivery baseline is the base tree, so lifecycle on is supported
REVIEW_ONLY_DOCS_PRE_OWNED = True            # Q8: docs files the change already edits are DOCS's to extend and review, not a HOLD
def plugin_version() -> str:   # review-loop's own version, read at run time: a coordinator upgrade voids an old Claude author probe PASS
    try: return json.loads((Path(__file__).resolve().parent.parent / '.claude-plugin' / 'plugin.json').read_text())['version']
    except (OSError, ValueError, KeyError): return 'UNAVAILABLE'
CODEX_DEFAULT_MODEL = 'gpt-6.1-sol'   # ADR-12 (owner 2026-10-04); runs keep the models frozen in their state
CODEX_DEFAULT_MODEL_MIN_CLI = (0, 159, 2)
# Codex CLI versions whose sandbox contract 1C verified (0.157.0: docs/1c-safety-controls.md row 2; 0.160.0: the owner's real
# permission-probe runs with v2.9.6 on that CLI, p296.sh P1/P2 PASS (.compass/results/2026-10-04_daily-report.md T1), after the
# b296-f1e default_permissions profiles were checked there with `codex sandbox` and `codex debug prompt-input`; confirmed by the
# supervisor 2026-10-04 19:35: real v2.9.6 permission-probe attempt4, P1 Codex reviewer + P2 Codex gate PASS, ~/paired-runs/probe296/runs/).
VERIFIED_CODEX_CLI_VERSIONS = frozenset({'codex-cli 0.157.0', 'codex-cli 0.160.0'})
OPERATOR_ONLY_DESTS = frozenset({'accept_unverified_codex_cli', 'accept_unverified_claude_author', 'accept_probe_skip', 'reason'})  # command line only, plus any accept_*
UTC_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
def probe_cache_root() -> Path: return Path.home() / '.cache' / 'review-loop' / 'probe-pass'   # at call time: tests set HOME
def _lstat_present(path: Path) -> bool:   # F4: only a definite absence is False; EACCES and friends raise, so an unreadable cache is never mistaken for an empty one
    try: os.lstat(path)
    except (FileNotFoundError, NotADirectoryError): return False
    return True
def read_cache_entry(root: Path, name: str, limit: int = 1 << 20) -> bytes:   # P0-4b H2: fd walk from ~ (O_NOFOLLOW each step); every check is on the fd that is read
    fds, home = [], Path.home()
    try:
        fds.append(os.open(home, os.O_RDONLY | os.O_DIRECTORY))
        for part in root.relative_to(home).parts: fds.append(os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fds[-1]))
        fds.append(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1]))   # non-blocking: a FIFO is refused, never waited on
        for fd, kind in ((fds[-2], stat.S_ISDIR), (fds[-1], stat.S_ISREG)):
            if not kind((info := os.fstat(fd)).st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise OSError(errno.EPERM, 'entry is not a private regular file owned by this user in a private directory')
        if info.st_size > limit: raise OSError(errno.EFBIG, 'entry is larger than 1 MiB')
        with os.fdopen(os.dup(fds[-1]), 'rb') as handle: return handle.read(limit)
    finally:
        for fd in fds: os.close(fd)
def claude_cli_version(binary: str) -> str:
    try: return subprocess.run([binary, '--version'], text=True, capture_output=True, timeout=10 * timeout_scale.env_factor(), stdin=subprocess.DEVNULL, env=cli_env()).stdout.strip() or 'UNAVAILABLE'   # G-b: the Claude child env
    except (OSError, subprocess.SubprocessError): return 'UNAVAILABLE'
PLAN_STOP_REASON = 'PLAN approved; stopped by --stop-after-plan; resume enters EXEC'
MAX_RESUME_TIMEOUT_SECONDS = 7200
DEFAULT_EXEC_TURN_TIMEOUT_SECONDS = 7200
DEFAULT_SAFETY_MODE = 'efficient'   # D-EFF (docs/efficient-mode.md): sandboxes kept; no probe gate, log-only evidence guard; --strict opts in
MAX_EXEC_TURN_TIMEOUT_SECONDS = 14400
DEFAULT_TIMEOUT_SECONDS = 2700
MAX_TIMEOUT_SECONDS = 86400   # timeoutcap: one day per non-EXEC turn or coordinator test run (an existing run starts with --timeout 20000)
TIMEOUT_RANGE_ERROR = f'--timeout must be between 1 and {MAX_TIMEOUT_SECONDS} seconds'


def resolve_exec_turn_timeout(value, general_timeout):
    timeout = value if value is not None else min(max(DEFAULT_EXEC_TURN_TIMEOUT_SECONDS, general_timeout), MAX_EXEC_TURN_TIMEOUT_SECONDS)
    if not 1 <= timeout <= MAX_EXEC_TURN_TIMEOUT_SECONDS: raise ValueError(f'--exec-turn-timeout must be between 1 and {MAX_EXEC_TURN_TIMEOUT_SECONDS} seconds')
    return timeout
DEFAULT_MAX_REJECTIONS = 2
ROUND_LIMIT_REASONS = ('PLAN round limit reached', 'EXEC round limit reached', 'EXEC round limit reached after adversarial gate',
                       'EXEC round limit reached after a FINISH write', 'EXEC round limit reached after a DOCS write',
                       'EXEC round limit reached after a SECURITY-stage change')
OUTPUT_TAIL_LINES = 15
OUTPUT_TAIL_CHARS = 1500
ADVISORY_REVIEW_SEVERITIES = {'MINOR', 'LOW'}
# FIELD-12: the CLI's own Bash timeout heads the tool result ("Exit code 143\nCommand timed out after 10m 0s").
TOOL_TIMEOUT_RE = re.compile(r'\A(?:Exit code -?\d+\n)?Command timed out after ((?:\d+(?:\.\d+)?(?:ms|[hms])(?![a-z]) ?)+)')


def tool_timeout_seconds(rows: list) -> Optional[int]:
    """Seconds of the CLI tool timeout that ended one of these observed command rows, else None."""
    for row in rows:
        if match := TOOL_TIMEOUT_RE.match(str(row.get('output') or '')):
            units = {'h': 3600, 'm': 60, 's': 1, 'ms': 0.001}
            return -int(-sum(float(n) * units[u] for n, u in re.findall(r'([0-9.]+)(ms|[hms])', match.group(1))) // 1)   # whole seconds, rounded up
    return None
LEDGER_ID_RE = re.compile(r'\bF\d{3,}\b')
# Ledger ids are upper-case F###; the scan is case-insensitive elsewhere, so scope that
# alternative to upper case or an identifier such as `f720` (a 720p frame) trips it.
FRESH_HISTORY_RE = re.compile(
    r'\b(?-i:F\d{3,})\b|\bprior_findings\b|Open finding ledger|Delivered (?:plan )?review:'
    r'|response[ -]to[ -](?:reviewer|review|F\d+)'
    r'|(?:previous|prior|earlier|persistent|shadow|gate)[ -]+(?:review|verdict|finding)'
    r'|(?:reviewer|review)\s+(?:said|requested|asked|found|approved|rejected)', re.I)
BLOCKING_REVIEW_SEVERITIES = {'CRITICAL', 'MAJOR', 'SECURITY'}
# FIELD-23: delta.stat names every path in full (git shortens long ones to ".../tail"), so a path that exists at the base
# commit is recognised as repository text by the fresh scan.
STAT_FULL_PATHS = ('--stat=100000,100000', '--no-renames')   # a rename lists both full paths, not dir/{a => b}


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
APPROVE_CONVERSION_NOTE = ('An APPROVE that leaves any blocking or security-tagged finding open is never accepted (in PLAN and EXEC it is converted to REVISE): close the finding with evidence in prior_findings, or return REVISE.')   # persistent reviewer only: the shadow prompt must not mention prior_findings


class RunLeaseError(RuntimeError):
    pass


class WorktreeDeliveryHold(RuntimeError):
    """An auto_commit that cannot move HEAD as journaled; the accept HOLDs and a replay with the same --expect finishes."""


class RateLimitError(ValueError):
    pass


class RateLimitedTurn(RuntimeError):
    """ratelimit: a turn the provider rejected for a rate limit, as raised by invoke(). Only this exact type makes a typed
    rate-limit hold; an error that wraps it (a W writer's git guard) is a different hold and needs its own repair."""


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
        r"\byou(?:'| a)?ve hit (?:your )?(?:(?:usage|weekly|daily|monthly|session|opus|sonnet|\d+-hour|five-hour|"
        r"seven-day|7-day) )?limit\b|\binsufficient_quota\b)"
    )
    confirmed = bool(strong_message.search(stderr))
    if confirmed:
        reset_sources.append(stderr)
    known_codes = {'rate_limit', 'rate_limit_error', 'rate_limit_exceeded',
                   'insufficient_quota', 'usage_limit_exceeded', 'quota_exceeded'}
    epoch_hint, last_window = None, None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        info = event.get('rate_limit_info')   # RL-WEEKLY: Claude stream-json announces its limit window state
        if event.get('type') == 'rate_limit_event' and isinstance(info, dict):
            last_window = info   # only the LAST event counts: a later allowed/allowed_warning cancels an earlier rejection
            continue
        if event.get('is_api_error_message') is True and str(event.get('error', '')).strip().lower() in known_codes:
            confirmed = True   # the synthetic assistant message that carries the limit text
            content = (event.get('message') or {}).get('content') if isinstance(event.get('message'), dict) else None
            reset_sources += [str(part.get('text', '')) for part in content or [] if isinstance(part, dict)]
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
        statuses = [obj.get(key) for obj in objects for key in ('status', 'status_code', 'http_status', 'api_error_status')]
        message = error.get('message', '') if isinstance(error, dict) else event.get('message', '') or event.get('result', '')
        message = str(message)
        if (codes & known_codes or any(str(value) == '429' for value in statuses)
                or (not codes and strong_message.search(message))):
            confirmed = True
            reset_sources += [message, json.dumps(event, ensure_ascii=False)]   # the plain text first, for a clean hint
    if (last_window and last_window.get('status') == 'rejected' and last_window.get('isUsingOverage') is not True
            and last_window.get('overageStatus') not in ('allowed', 'allowed_warning')):   # gate B1: overage keeps running
        confirmed = True
        if isinstance(last_window.get('resetsAt'), (int, float)) and last_window['resetsAt'] > 0:
            epoch_hint = ('resets at ' + time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(last_window['resetsAt'])) +
                          f" ({last_window.get('rateLimitType') or 'limit'} window)")
    if not confirmed:
        return None
    reset_hint = epoch_hint   # an exact resetsAt beats a parsed phrase
    reset_cues = ('try again at', 'reset at', 'resets at', 'resets ', 'reset time',
                  'available again at', 'available at', 'retry after', 'retry-after', 'retry_after')
    for source in ([] if reset_hint else reset_sources):
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


def _codex_trust_paths(workspace) -> list[str]:
    """The paths Codex may record trust for: the workspace and, in a linked worktree, the main checkout root of its own repository."""
    ws = Path(workspace).resolve()
    paths = [str(ws)]
    try:
        proc = candidate_tree.run_bounded(candidate_tree.git_command('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=ws), cwd=ws, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        common = Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None
        if common is not None and common.is_absolute() and common.name == '.git' and (ws / '.git').is_file():
            paths.append(str(common.resolve().parent))
    except Exception:
        pass   # no readable repository: only the workspace itself can be an expected trust path
    return paths


def _trust_block_variants(path: str) -> list[str]:
    entry = '[projects.' + json.dumps(path) + ']\ntrust_level = "trusted"\n'
    return [lead + entry + trail for lead, trail in (('\n', ''), ('', '\n'), ('\n', '\n'), ('', ''))]


def _toml_top_level_at(text: str, offset: int) -> bool:
    """RF-6: True only when `offset` starts a line at TOML top level (no string, comment or bracket open); anything ambiguous is False."""
    i, depth = 0, 0
    while i < offset:
        c = text[i]
        if c == '#':
            i = text.find('\n', i)
            if i < 0: return False
            continue
        if c in '"\'':
            quote = text.startswith(c * 3, i)
            j = i + (3 if quote else 1)
            while True:
                if j >= len(text) or (not quote and text[j] == '\n'): return False
                if c == '"' and text[j] == '\\': j += 2; continue
                if text.startswith(c * 3, j) if quote else text[j] == c:
                    j += 3 if quote else 1
                    break
                j += 1
            if quote and text[j:j + 1] == c: return False   # four or five closing quotes: ambiguous
            if j > offset: return False
            i = j
            continue
        if c in '[{': depth += 1
        elif c in ']}':
            depth -= 1
            if depth < 0: return False
        i += 1
    return depth == 0 and (offset == 0 or text[offset - 1] == '\n')


def _only_codex_workspace_trust_append(before: dict, after: dict, workspaces) -> list[str]:
    """Exact block removal: `after` minus one trust block per trusted expected path (optional one blank line) must equal `before` byte for byte."""
    if after.get('raw') is None:
        return []
    before_raw, found = before.get('raw') or '', []
    for path in sorted({p for workspace in workspaces for p in _codex_trust_paths(workspace)}):
        header = '[projects.' + json.dumps(path) + ']'
        if any(line.strip() == header for line in before_raw.splitlines()):
            continue   # an older entry stays in place; a second one is a leftover below
        if any(block in after['raw'] for block in _trust_block_variants(path)):
            found.append(path)
    for chosen in itertools.product(*([block for block in _trust_block_variants(path) if block in after['raw']] for path in found)):
        rest, at_boundary = after['raw'], True
        for block in chosen:   # a block that starts a line and is followed by a table header (or the end) cannot adopt the keys of the table around it
            pos = rest.find(block)
            start, tail = pos + block.startswith('\n'), next((line.strip() for line in rest[pos + len(block):].splitlines() if line.strip()), '[')
            at_boundary = at_boundary and _toml_top_level_at(rest, start) and tail.startswith('[')
            rest = rest.replace(block, '', 1)
        if found and at_boundary and rest == before_raw:
            return found
    return []


def trust_entry_only_since_hash(path: Path, before_sha256: str, workspace: Path, raw: Optional[bytes] = None, extra=()) -> bool:
    """The file is the before-hash file plus exact trust blocks for this workspace's trust paths (and, FIELD-22, for the
    `extra` workspaces of other paired-session runs) and nothing else."""
    raw = path.read_bytes() if raw is None else raw   # F7: the caller passes the bytes it hashed, so check and hash agree
    entries = {root: ('[projects.' + json.dumps(root) + ']\ntrust_level = "trusted"\n').encode()
               for each in (workspace, *extra) for root in _codex_trust_paths(each)}
    present = [root for root, entry in entries.items() if raw.count(entry) == 1]
    if not extra:   # RF-7: the expected set, each path at most once, in any order (unchanged)
        candidates = (roots for count in range(1, len(present) + 1) for roots in itertools.permutations(present, count))
    else:   # FIELD-22: older trust blocks of lease-recorded workspaces may be present too, so search subsets in file order,
        # smallest first, under a fixed budget of attempts (fail closed past it); no cap on how many blocks were added
        ordered = sorted(present, key=lambda root: raw.index(entries[root]))
        candidates = (roots for count in range(1, len(ordered) + 1) for roots in itertools.combinations(ordered, count))
    budget = 400_000
    for roots in candidates:
        for variants in itertools.product(((b'', b''), (b'\n', b''), (b'', b'\n'), (b'\n', b'\n')), repeat=len(roots)):
            if extra:
                budget -= 1
                if budget < 0:
                    return False
            before = raw
            for root, (leading, trailing) in zip(roots, variants):
                block = leading + entries[root] + trailing
                if block not in before: break
                before = before.replace(block, b'', 1)
            else:
                if (hashlib.sha256(before).hexdigest() == before_sha256 or (before_sha256 is None and not before)) and _only_codex_workspace_trust_append({'raw': before.decode('utf-8')}, {'raw': raw.decode('utf-8')}, [workspace, *extra]): return True
    return False


PLUGIN_UPDATE_HINT = ' (likely a plugin auto-update outside the run; `resume` re-runs the turn on a fresh baseline)'


PLUGIN_UPDATE_FIELDS = ('version', 'installPath', 'gitCommitSha', 'lastUpdated')   # field21: what a normal plugin update rewrites


def normal_plugin_update(old: dict, new: dict) -> Optional[list]:
    """field21: the entries a NORMAL Claude Code plugin update changed in installed_plugins.json, or None for anything else. Same
    document apart from `plugins`, same plugin keys, same entries (count and keys); a changed entry differs only in the scalar
    values of PLUGIN_UPDATE_FIELDS, and a changed installPath is the canonical cache directory of that plugin and its new version
    (<plugins>/cache/<marketplace>/<plugin>/<version>), present on disk as a real directory."""
    if old.get('error') or new.get('error') or not isinstance(old.get('document'), dict) or not isinstance(new.get('document'), dict): return None
    before, after = old['document'], new['document']
    if before == after or {k: v for k, v in before.items() if k != 'plugins'} != {k: v for k, v in after.items() if k != 'plugins'}: return None
    plugins = (before.get('plugins'), after.get('plugins'))
    if not all(isinstance(p, dict) for p in plugins) or set(plugins[0]) != set(plugins[1]): return None
    cache = Path(new['path']).parent / 'cache' if new.get('path') else None
    updates = []
    for key in sorted(plugins[0]):
        rows = (plugins[0][key], plugins[1][key])
        if not all(isinstance(r, list) for r in rows) or len(rows[0]) != len(rows[1]): return None
        for index, (was, now) in enumerate(zip(*rows)):
            if not isinstance(was, dict) or not isinstance(now, dict) or set(was) != set(now): return None
            changed = sorted(field for field in was if was[field] != now[field])
            if not changed: continue
            if any(field not in PLUGIN_UPDATE_FIELDS or isinstance(was[field], (dict, list)) or isinstance(now[field], (dict, list)) for field in changed): return None
            if 'installPath' in changed:
                plugin, _, marketplace = key.rpartition('@')
                version = now.get('version')
                if not (cache and plugin and marketplace and isinstance(version, str) and isinstance(now['installPath'], str)
                        and version not in ('', '.', '..') and '/' not in version and '/' not in plugin and '/' not in marketplace):
                    return None
                canonical = cache / marketplace / plugin / version
                levels = (cache, cache / marketplace, cache / marketplace / plugin, canonical)   # every level a real directory: no link out
                if Path(now['installPath']) != canonical or any(level.is_symlink() or not level.is_dir() for level in levels): return None
            updates.append({'plugin': key, 'entry': index, 'fields': changed, 'version': [was.get('version'), now.get('version')]})
    return updates or None


def plugin_version_bump_only(findings: list, before: dict, after: dict) -> bool:
    """RF-4, widened by field21: the only finding is installed_plugins.json and it is a normal plugin update (normal_plugin_update)."""
    if [row['file'] for row in findings] != ['claude_plugins']: return False
    return normal_plugin_update(before['claude_plugins'], after['claude_plugins']) is not None


def concurrent_run_workspaces(own_workspace, live=True) -> dict:
    """FIELD-22: {workspace: run id} of OTHER paired-session runs, read from this user's workspace-lease directory without
    taking any lock (a probe lock could make that run's own lease acquisition fail). A lease file counts only when it is
    a private regular file owned by this user, its name is the lease path of the workspace it names (no stray entry),
    and the run dir it names holds a state.json naming that same workspace. live=True also needs the lease pid alive,
    that run's coordinator lock naming the same pid, and its state ACTIVE; live=False (an operator acknowledgment)
    accepts such a record of a run that has ended. Boundary: this is evidence of a paired-session coordinator of this
    user; a same-UID process could forge it, but such a process can edit config.toml directly anyway. The guard holds
    against sandboxed turns, which can write neither the lease directory nor the Codex home. Residual: a reused pid
    with a stale ACTIVE state and lock record counts as live."""
    try:
        own = Path(own_workspace).expanduser().resolve()
        lock_dir = workspace_lease_path(own).parent
        info = lock_dir.lstat()
    except (OSError, RunLeaseError):
        return {}
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        return {}
    found = {}
    for entry in sorted(lock_dir.glob('*.lock')):
        try:
            meta = entry.lstat()
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) & 0o077:
                continue
            record = json.loads(entry.read_text(encoding='utf-8', errors='replace')[:4096])
            if not isinstance(record, dict) or not isinstance(record.get('workspace'), str) or not isinstance(record.get('run_dir'), str):
                continue
            workspace, run_dir = Path(record['workspace']).resolve(), Path(record['run_dir'])
            if workspace == own or workspace_lease_path(workspace) != entry:
                continue
            state = json.loads((run_dir / 'state.json').read_text(encoding='utf-8'))
            if not isinstance(state, dict) or not isinstance(state.get('workspace'), str) or Path(state['workspace']).resolve() != workspace:
                continue
            if live:
                pid = record.get('pid')
                if type(pid) is not int or pid <= 1 or state.get('status') != 'ACTIVE':
                    continue
                lock = json.loads((run_dir / '.coordinator.lock').read_text(encoding='utf-8')[:1024])
                if not isinstance(lock, dict) or lock.get('pid') != pid:   # the same coordinator holds the run lease
                    continue
                try: os.kill(pid, 0)
                except ProcessLookupError: continue
                except PermissionError: pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RunLeaseError):
            continue
        found[str(workspace)] = run_dir.name
    return found


def attribute_global_config_changes(before: dict, after: dict, workspaces=(), concurrent=None) -> dict:
    """Attribute only the known Codex trust and Claude lastUpdated side effects. FIELD-22: concurrent = {workspace: run id}
    of other live paired-session runs (concurrent_run_workspaces); an exact trust append for one of them is expected too."""
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
            trusted_workspaces = _only_codex_workspace_trust_append(old, new, [*workspaces, *(concurrent or {})])
            own = {path for workspace in workspaces for path in _codex_trust_paths(workspace)}
            others = {path: run for workspace, run in (concurrent or {}).items() for path in _codex_trust_paths(workspace)}
            mine = [path for path in trusted_workspaces if path in own]
            theirs = [path for path in trusted_workspaces if path not in own]
            if mine:
                expected.append({'file': label, 'change': 'trusted-probe-workspace-entry',
                                 'workspaces': mine})
            if theirs:   # FIELD-22: another live paired-session run's own trust entry, on the shared Codex home
                expected.append({'file': label, 'change': 'trusted-concurrent-run-workspace', 'workspaces': theirs,
                                 'runs': sorted({others[path] for path in theirs})})
            if not trusted_workspaces:
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
            'expected_changes': expected, 'findings': findings, 'warnings': ['global config mutated by codex CLI trust persistence'] if any(row['file'] == 'codex_config' and row['change'] == 'trusted-probe-workspace-entry' for row in expected) else []}


def read_text_tail(path: Path, max_bytes: int = 65536) -> str:
    with path.open('rb') as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(max(0, size - max_bytes), os.SEEK_SET)
        return source.read(max_bytes).decode('utf-8', 'replace')


TRUST_HEADER_RE = re.compile(r'^\[projects\.("(?:[^"\\\n]|\\.)*")\]\s*$', re.M)
FOREIGN_TRUST_MAX_PATHS = 256   # bounds the work; a larger change stays a finding
FOREIGN_TRUST_MAX_BYTES = 1 << 20   # a default config over 1 MiB is not inspected; its change stays a finding


def _toml_top_level_line_starts(text: str) -> Optional[set]:
    """One pass: the offsets that start a line at TOML top level (outside strings, comments and brackets), the
    _toml_top_level_at rule for every line at once. Anything ambiguous (an unterminated string, four or five closing
    quotes, an unbalanced bracket) returns None, so the caller stays conservative."""
    starts, i, depth, n, line_start = set(), 0, 0, len(text), True
    while i < n:
        if line_start and depth == 0:
            starts.add(i)
        line_start = False
        c = text[i]
        if c == '\n':
            line_start, i = True, i + 1
            continue
        if c == '#':
            i = text.find('\n', i)
            if i < 0:
                break
            continue
        if c in '"\'':
            triple = text.startswith(c * 3, i)
            j = i + (3 if triple else 1)
            while True:
                if j >= n or (not triple and text[j] == '\n'):
                    return None
                if c == '"' and text[j] == '\\':
                    j += 2
                    continue
                if text.startswith(c * 3, j) if triple else text[j] == c:
                    j += 3 if triple else 1
                    break
                j += 1
            if triple and text[j:j + 1] == c:
                return None
            i = j
            continue
        if c in '[{':
            depth += 1
        elif c in ']}':
            depth -= 1
            if depth < 0:
                return None
        i += 1
    return starts


def _top_level_trust_headers(raw: str, starts: set) -> Optional[set]:
    """The paths of real top-level `[projects."<path>"]` headers (a match inside a multi-line string is not at a
    top-level line start); a top-level header whose quoted path does not decode returns None (stay conservative)."""
    paths = set()
    for match in TRUST_HEADER_RE.finditer(raw):
        if match.start() not in starts:
            continue
        try:
            path = json.loads(match.group(1))
        except ValueError:
            return None
        if not isinstance(path, str):
            return None
        paths.add(path)
    return paths


def _strip_trust_entries(raw: str, starts: set, paths) -> Optional[str]:
    """Remove one exact `[projects."<path>"]` + `trust_level = "trusted"` entry per path, each at a top-level line start
    and followed by another table header or the end (so it cannot adopt keys). Positions come from the one-pass scan of
    this same text; returns the rest, or None when an entry is missing or not at such a boundary."""
    cuts = []
    for path in paths:
        entry = '[projects.' + json.dumps(path) + ']\ntrust_level = "trusted"\n'
        at = raw.find(entry)
        while at >= 0 and at not in starts:
            at = raw.find(entry, at + 1)
        if at < 0:
            return None
        tail = next((line.strip() for line in raw[at + len(entry):].splitlines() if line.strip()), '[')
        if not tail.startswith('['):
            return None
        cuts.append((at, at + len(entry)))
    rest, last = [], 0
    for begin, finish in sorted(cuts):
        if begin < last:
            return None
        rest.append(raw[last:begin])
        last = finish
    return ''.join(rest) + raw[last:]


def _trust_path_key(path: str) -> str:
    """Compare paths as the file system would: resolved, no trailing slash, case-folded on macOS (case-insensitive by
    default). A path that cannot be resolved (a NUL byte, for example) raises ValueError."""
    key = os.path.realpath(path.rstrip('/') or '/')
    return key.casefold() if sys.platform == 'darwin' else key


def _blank_runs_normalized(text: str) -> str:
    """Removing a table leaves its separating blank lines; runs of blank lines (and the file's leading and trailing
    blank lines) do not change a TOML setting outside a multi-line string, so they are normalized before comparing."""
    return re.sub(r'\n(?:[ \t]*\n)+', '\n\n', '\n' + text.strip('\n') + '\n')


def default_home_foreign_trust_only(before: dict, after: dict, workspaces) -> Optional[dict]:
    """P0 (owner 2026-10-06): under an isolated CODEX_HOME the default ~/.codex/config.toml is not the config the run uses.
    A change there that only adds or removes whole `[projects."<path>"] trust_level = "trusted"` tables, for paths that are
    not this run's own trust paths (workspace, clone, linked-worktree main root), is another process: a Codex on the
    default home, or an operator restore. Returns {'added': [...], 'removed': [...]}; anything else returns None and stays
    a finding. A trust entry for this run's OWN path in the default home is not foreign: it would mean the turn's Codex
    wrote the default home despite CODEX_HOME, which is what the default-config snapshot exists to catch (R20-0a).
    Accepted residual of the comparison: only blank-line runs are normalized, so a change that adds or removes blank
    lines alone inside a multi-line string would also pass (it sets nothing the run uses)."""
    old, new = before.get('raw'), after.get('raw')
    if old is None or new is None or '\ufffd' in old or '\ufffd' in new:   # an undecodable file fails closed
        return None
    if max(len(old.encode('utf-8')), len(new.encode('utf-8'))) > FOREIGN_TRUST_MAX_BYTES:
        return None
    old_starts, new_starts = _toml_top_level_line_starts(old), _toml_top_level_line_starts(new)
    if old_starts is None or new_starts is None:
        return None
    old_heads, new_heads = _top_level_trust_headers(old, old_starts), _top_level_trust_headers(new, new_starts)
    if old_heads is None or new_heads is None:
        return None
    added, removed = sorted(new_heads - old_heads), sorted(old_heads - new_heads)
    if (not added and not removed) or len(added) + len(removed) > FOREIGN_TRUST_MAX_PATHS:
        return None
    try:
        own = {_trust_path_key(path) for workspace in workspaces for path in _codex_trust_paths(workspace)}
        if own & {_trust_path_key(path) for path in (*added, *removed)}:
            return None
    except ValueError:
        return None
    new_rest, old_rest = _strip_trust_entries(new, new_starts, added), _strip_trust_entries(old, old_starts, removed)
    if new_rest is None or old_rest is None or _blank_runs_normalized(new_rest) != _blank_runs_normalized(old_rest):
        return None
    return {'added': added, 'removed': removed}


def reclassify_default_home_trust(changes: dict, before: dict, after: dict, workspaces) -> dict:
    """P0: in place, a codex_default_config finding that is only foreign trust tables becomes the expected change
    `foreign-default-home-trust-entry` (recorded in the receipt, never voiding the turn). Only runs whose Codex home is
    not the default have that snapshot (global_config_snapshot), so a run on the default home is unchanged. Any other
    change to the default config stays a finding (decision (a): the run cannot tell it from its own escape)."""
    for row in [row for row in changes['findings'] if row['file'] == 'codex_default_config'
                and row.get('reason') == 'unexpected-content-change']:
        if (delta := default_home_foreign_trust_only(before['codex_default_config'], after['codex_default_config'], workspaces)):
            changes['findings'].remove(row)
            changes['expected_changes'].append({'file': 'codex_default_config', 'change': 'foreign-default-home-trust-entry',
                                                **delta})
    changes['status'] = 'FAIL' if changes['findings'] else 'PASS'
    return changes


READONLY_SCRATCH_ROLES = ('reviewer', 'shadow', 'gate', 'probe', 'gate-probe')   # b295-f1 FIELD-1: Codex read-only roles get a per-dispatch temp root
CODEX_READONLY_PROFILE = 'paired_session_readonly'
SCRATCH_PROBE_COMMAND = 'printf probe > "$TMPDIR/paired-session-scratch-probe"'
# FIELD-20 (poker-news-bot WI-101, 2026-10-05; real codex-cli 0.160.0 rehearsal in lane B's field20 report): Codex's exec_command
# returns after about 10 s with a session_id and no exit_code; a model that moves on without polling leaves a long test command
# running and the turn's end kills it, so no completed run is observed. Every role runs a command to completion within the
# dispatch timeout; the coordinator still requires one observed completed exit-0 run of the exact configured command.
LONG_COMMAND_RULE = ('Run each command to completion before the next one and never end your turn while a command is still running. '
                     'A result with a session_id and no exit_code is still running: poll that session until the result carries an '
                     'exit_code (code-mode cells of exactly: const r = await tools.write_stdin({"session_id":<that id>,"chars":"",'
                     '"yield_time_ms":30000}); text(JSON.stringify(r));). Polls are not extra commands. '
                     'A Bash tool call that takes a timeout parameter gets 600000 for a long command.')   # no vendor names: fresh-role scan


def command_not_completed(row: dict) -> bool:
    """FIELD-20: a Codex command with no exit status (still running or killed when the turn ended, or reported as -1). Claude rows
    carry -1 for every error, so they never count here; a Claude tool timeout is FIELD-12."""
    code = row.get('exit_code')
    return row.get('source') != 'Bash tool_use/tool_result' and (code is None or (type(code) is int and code < 0))


def codex_readonly_profile_args() -> list[str]:
    """b295-f1: replaces sandbox_mode="read-only" (Codex refuses both together): root and workspace read, only $TMPDIR (the dispatch's
    scratch root) writable, network off. Only a real permission-probe shows that `codex exec` honours it.
    b296-f1e: `codex exec` has no -P (codex-cli 0.160.0 exits 2); `default_permissions` selects the profile. An explicit --config
    outranks user, system and project config.toml; managed config, requirements and MDM may outrank it, and codex_capability_guard
    refuses a dispatch when they set permission keys."""
    name = CODEX_READONLY_PROFILE
    return ['--config', f'default_permissions="{name}"', '--config', f'permissions.{name}.filesystem={{":root"="read", ":tmpdir"="write", ":workspace_roots"={{"."="read"}}}}',
            '--config', f'permissions.{name}.network.enabled=false']


def scratch_listing(root: Path) -> tuple[list, list]:
    """Names under a scratch root (links not followed, at most 200) and the regular files there with more than one link."""
    names, linked = [], []
    if stat.S_IMODE(os.lstat(root).st_mode) != 0o700:   # R1 LOW-2: a role that chmods its own scratch could fake an EACCES "denial"
        raise ValueError('the scratch temp root mode changed during the turn: ' + str(root))
    def unreadable(error):   # R1 l1: a directory the role made unreadable could hide a link; fail the turn instead
        raise ValueError('unreadable entry in the scratch temp root: ' + str(error.filename))
    for parent, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        for name in sorted(dirs + files):
            path = Path(parent) / name
            names.append(str(path.relative_to(root)))
            try: info = path.lstat()
            except OSError as exc: raise ValueError('uninspectable entry in the scratch temp root: ' + str(path)) from exc   # R2 LOW-1
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                linked.append(names[-1])
    return sorted(names)[:200], linked


SANDBOX_DENIAL_MARKERS = ('operation not permitted', 'read-only file system', 'permission denied', 'deny file-write-create', 'deny file-write-data')


def explicit_denial(row: dict) -> bool:
    """b296-f1b: an observed command refused by the sandbox or the OS (EPERM/EACCES/EROFS wording and a non-zero exit), never a
    missing tool (exit 126/127, 'command not found'), a launch failure or any other error."""
    output = str(row.get('output', '')).lower()
    return (row.get('error') is True and type(row.get('exit_code')) is int and row['exit_code'] not in (0, 126, 127)
            and any(marker in output for marker in SANDBOX_DENIAL_MARKERS) and 'command not found' not in output)


def probe_tracked_file(workspace: Path) -> Optional[str]:
    """b296-f1f: the file the reviewer/gate probe's `git --literal-pathspecs checkout --` and `rm` legs target: a `git ls-files -z`
    entry that is a regular file on disk with no symlink on its path, and is safe as one shell word on one prompt line (printable, no
    leading '-'). The first one in ls-files order wins, preferring a file without unstaged changes (a broken surface would lose them),
    then one without pathspec/glob characters (b296-f1g, defense in depth: the checkout leg is already literal), then one that needs
    no shell quoting (the model must copy the command verbatim). None when the workspace has no such file. Read-only: nothing in the
    workspace is created or changed."""
    def names(*extra):
        proc = candidate_tree.run_bounded(candidate_tree.git_command('ls-files', '-z', *extra, cwd=workspace), cwd=workspace,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if proc.returncode:
            raise RuntimeError('git ls-files failed: ' + proc.stderr.decode(errors='replace').strip())
        return proc.stdout.decode(errors='surrogateescape').split('\0')
    root, modified, best = os.path.realpath(workspace), set(names('-m')), None
    for name in names():
        if not name or name.startswith('-') or not name.isprintable(): continue
        try: regular = stat.S_ISREG(os.lstat(workspace / name).st_mode)
        except OSError: continue
        if regular and os.path.realpath(workspace / name) == os.path.join(root, name):
            rank = (name in modified, any(c in name for c in ':*?[]\\'), shlex.quote(name) != name)
            if best is None or rank < best[0]: best = (rank, name)
            if not any(rank): break
    return best and best[1]


def probe_turn_status(allowed: bool, outcomes: dict, unchanged: bool, miss: str) -> str:
    """b296-f1b: PASS only when every attempt was denied; attempts that failed without an explicit denial (and nothing else wrong) give
    UNKNOWN, never PASS; anything else is FAIL."""
    if allowed and unchanged and not miss and all(outcome == 'denied' for outcome in outcomes.values()):
        return 'PASS'
    if allowed and unchanged and not miss and all(outcome in ('denied', 'unknown') for outcome in outcomes.values()):
        return 'UNKNOWN'
    return 'FAIL'


def _fchflags_zero(fd: int) -> None:
    """Clear BSD file flags (uchg, uappnd, ...) on an open fd; this Python has no os.fchflags, so libc's fchflags is called directly."""
    if hasattr(os, 'fchflags'):
        os.fchflags(fd, 0)
        return
    import ctypes, ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    if libc.fchflags(fd, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _open_entry_nofollow(parent_fd: int, name: str, info: os.stat_result, path: str, flags: int, mode: int) -> int:
    """Open one scratch entry relative to its parent with O_NOFOLLOW (after a no-follow chmod when it is unreadable) and check it is the
    entry just stat'ed."""
    try:
        fd = os.open(name, flags | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent_fd)
    except PermissionError:
        os.chmod(name, mode, dir_fd=parent_fd, follow_symlinks=False)
        fd = os.open(name, flags | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent_fd)
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        os.close(fd)
        raise OSError(errno.EBUSY, 'scratch entry changed while it was being removed', path)
    return fd


def remove_tree_nofollow(parent_fd: int, parent_path: str, name: str) -> None:
    """b296-f1b: remove `name` under `parent_fd` fd-relative and never following a link, whether or not a process survives the turn
    (R1 MEDIUM-1): every open is relative to the parent fd with O_NOFOLLOW and checked against the lstat just taken; modes and file
    flags are reset through that fd (fchmod, fchflags); a link or any other non-directory is unlinked as itself."""
    path = os.path.join(parent_path, name)
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        if getattr(info, 'st_flags', 0) and stat.S_ISREG(info.st_mode):   # a flagged file refuses unlink
            fd = _open_entry_nofollow(parent_fd, name, info, path, os.O_RDONLY, 0o600)
            try: _fchflags_zero(fd)
            finally: os.close(fd)
        os.unlink(name, dir_fd=parent_fd)
        return
    fd = _open_entry_nofollow(parent_fd, name, info, path, os.O_RDONLY | os.O_DIRECTORY, 0o700)
    try:
        if getattr(info, 'st_flags', 0):
            _fchflags_zero(fd)
        os.fchmod(fd, 0o700)
        for child in os.listdir(fd):
            remove_tree_nofollow(fd, path, child)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def run_dir_inodes(run_dir: Path, skip: set, own: Path) -> dict:
    """b296-f1: inode and ctime of every regular file under run_dir except `skip`, this turn's own `<own>.*` evidence files and the
    coordinator's own state.json / progress.jsonl.
    Making a hard link to a file, writing through one or changing its mode or times all change that inode's ctime, which no process can
    set back, so a link made, written through and removed within one turn still shows here."""
    found = {}
    for parent, dirs, files in os.walk(run_dir, followlinks=False):
        dirs[:] = [name for name in dirs if Path(parent, name) not in skip]
        for name in files:
            path = Path(parent, name)
            if (path in skip or (Path(parent) == own.parent and name.startswith(own.name + '.'))
                    or (Path(parent) == run_dir and name in ('state.json', 'progress.jsonl'))):   # b296-f1b LOW: exactly these two
                continue
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                found[str(path)] = (info.st_ino, info.st_ctime_ns)
    return found


DONTASK_SHELL_SYNTAX = re.compile(r'''\$\(|`|\||&&|;|[<>]|(?:^|[\s'"(])(?:for|while|until)\s''')   # field-a L2: a loop keyword, not check-for-x or /for/


def dontask_command_hint(commands) -> str:
    """N4-b: shell syntax a Claude reviewer or gate in dontAsk mode may refuse in its one exact allowlisted Bash call."""
    shaped = [command for command in commands if DONTASK_SHELL_SYNTAX.search(command)]
    return ('WARNING: a Claude reviewer or gate runs each test/reviewer command as one exact allowlisted Bash call in dontAsk mode; '
            'command substitution, pipes, `;`, `&&`, redirection or loops in ' + '; '.join(repr(command) for command in shaped) +
            ' can be refused before it runs. Put it in a script and configure `/bin/bash /absolute/path/to/script.sh`.') if shaped else ''


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


DETACH_ACTIONS = ('run', 'resume', 'reject', 'permission-probe')
DETACH_TURN = {'group': None, 'spawning': False}   # detach: the CLI turn in flight, for the SIGTERM handler


class DetachStop(BaseException):
    """detach: SIGTERM to a detached coordinator; a BaseException, so a dispatch kills its turn's process group as on Ctrl-C."""


def detach_paths(run_dir) -> tuple[Path, Path]:
    """detach: the per-user 0700 record and lock of a run dir's detached command (outside the run dir and its parent, which the
    Claude author probe watches); keyed by the resolved run dir, so relative and symlinked spellings share them."""
    root = Path(tempfile.gettempdir()).resolve() / f'paired-session-detached-{os.getuid()}'
    try: root.mkdir(mode=0o700)
    except FileExistsError: pass
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f'detach directory {root} must be a directory owned by this user with mode 0700')
    key = hashlib.sha256(str(Path(run_dir).expanduser().resolve()).encode()).hexdigest()[:16]
    return root / (key + '.json'), root / (key + '.lock')


def detach_holder(run_dir):
    """None when no detached command holds this run dir's lock; 'starting' while the holder has not published its own record
    (the caller marks the record `starting` under the lock, so an older run's record is never read as the holder's); else the
    holder's record. Only the lock holder writes the record."""
    record, lock = detach_paths(run_dir)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        try: data = json.loads(record.read_text())
        except (OSError, ValueError): return 'starting'
        ok = data.get('status') == 'running' and type(data.get('pid')) is int and type(data.get('sid')) is int
        return data if ok else 'starting'
    finally:
        os.close(fd)
    return None


def stop_turn_group(_signum=None, _frame=None):
    """detach: SIGTERM handler. Kill the turn in flight first (a stop between Popen and the dispatch's cleanup guard must not
    leave it running), then unwind like Ctrl-C: the dispatch keeps the turn active and reaps it. While a turn is being spawned
    the stop is only noted; the dispatch acts on it as soon as the group is known."""
    if DETACH_TURN['spawning']:
        DETACH_TURN['stop'] = True
        return
    if (group := DETACH_TURN['group']) is not None:
        try: os.killpg(group, signal.SIGKILL)
        except OSError: pass
    raise DetachStop()


def detach(raw_argv: list, run_dir: str) -> int:
    """FIELD-17 follow-up (docs/detach.md): a host that ends its turn signals the process group it started (Claude Code: SIGTERM)
    or kills the command (Codex: SIGKILL). setsid() and a second fork leave that tree; the grandchild runs the same command and
    records its exit code. Nothing else changes: lease, active/uncertain turns, cleanup and every check are those of a plain run."""
    record, lock = detach_paths(run_dir)
    lock_fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # inherited by the grandchild, which keeps it until it exits
    except BlockingIOError:
        os.close(lock_fd)
        held = detach_holder(run_dir)
        print(f"REFUSED: a detached command for this run dir is still running (pid {held.get('pid') if isinstance(held, dict) else held}); "
              'poll `status --brief` or use `stop`')
        return 2
    try:   # under the lock: an older run's record must never be read as this command's
        atomic_json(record, {'status': 'starting', 'since': time.time()})
    except OSError as exc:
        os.close(lock_fd)
        print('REFUSED: cannot write the detach record: ' + str(exc))
        return 2
    log = record.with_name(record.stem + time.strftime('-%Y%m%dT%H%M%S.log'))
    argv = [arg for arg in raw_argv if arg != '--detach']
    sys.stdout.flush(); sys.stderr.flush()
    ready, ready_w = os.pipe()
    if (child := os.fork()):
        os.close(ready_w); os.close(lock_fd)
        answer = b''
        while (chunk := os.read(ready, 4096)): answer += chunk
        os.close(ready)
        os.waitpid(child, 0)
        if not answer.startswith(b'ok '):
            print('REFUSED: the detached command did not start: ' + (answer.decode(errors='replace') or 'no answer'))
            return 2
        print(f'DETACHED: pid {answer[3:].decode()}; log {log}; poll `status --brief` (or RUN_DIR/state.json) until DONE or '
              'HOLD; stop with `stop`')
        return 0
    os.close(ready)
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(os.open(os.devnull, os.O_RDONLY), 0); os.dup2(out, 1); os.dup2(out, 2)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, stop_turn_group)
        atomic_json(record, {'pid': os.getpid(), 'sid': os.getsid(0), 'run_dir': str(Path(run_dir).expanduser().resolve()),
                             'argv': argv, 'log': str(log), 'status': 'running', 'started': time.time()})
    except BaseException as exc:   # the caller reports it; nothing has run
        os.write(ready_w, f'{type(exc).__name__}: {exc}'.encode()); os._exit(2)
    os.write(ready_w, f'ok {os.getpid()}'.encode()); os.close(ready_w)
    code = 1
    try:
        code = main(argv)
    except DetachStop:
        print('STOPPED: `stop` ended this detached command; a turn it interrupted stays active: check its process group, then '
              'resume --retry-uncertain or abort')
        code = 130
    except BaseException:
        traceback.print_exc()
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # the stop has been taken; finish the record
        sys.stdout.flush(); sys.stderr.flush()
        try: atomic_json(record, {**json.loads(record.read_text()), 'status': 'exited', 'exit_code': code, 'ended': time.time()})
        finally: os._exit(code)


def stop_detached(run_dir: str, wait: float = 60) -> int:
    """detach: SIGTERM to this run dir's detached coordinator, then wait until it has exited (it kills its turn's group first)."""
    record, _ = detach_paths(run_dir)
    held = detach_holder(run_dir)
    if held is None:
        print('NOTE: no detached command is running for this run dir')
        return 0
    if held == 'starting':
        print('HOLD: a detached command for this run dir is starting; retry stop in a moment')
        return 2
    pid = held['pid']
    try:   # the recorded session id guards against a pid reused after the holder exited between the read and the signal
        if os.getsid(pid) != held['sid']: raise ProcessLookupError(pid)
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass   # it exited on its own (a reused pid is never signalled); wait for its lock below
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and detach_holder(run_dir) is not None:
        time.sleep(0.2)
    if detach_holder(run_dir) is not None:
        print(f'HOLD: the detached command (pid {pid}) has not exited after {wait:.0f} s; check it before acting on this run')
        return 2
    print(f"STOPPED: detached command pid {pid} exited (exit {json.loads(record.read_text()).get('exit_code')})")
    return 0


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


class ProbeLockBusy(ValueError):
    """FIELD-13: the parent lock stayed busy past its wait bound; main prints REFUSED before any state or report (and before the run dir,
    unless the parent was missing: then the fresh, empty run dir stays)."""


PROBE_LOCK_POLL_SECONDS, PROBE_LOCK_WAIT_FACTOR = 1.0, 3


def probe_lock_dir() -> Path:   # FIELD-13: fixed and uid-keyed, so runs with any HOME, CODEX_HOME or workspace share it
    return Path('/tmp') / f'paired-session-probe-locks-{os.getuid()}'


def _frozen_probe_config(args: argparse.Namespace) -> tuple[str, float]:
    """FIELD-13: an existing run's saved author vendor and timeout (args keep the CLI/profile defaults until Coordinator restores the
    saved roles); an unreadable state counts as a Claude author, which only costs a wait."""
    vendor, timeout, path = args.author_vendor, args.timeout, Path(args.run_dir) / 'state.json'
    if path.exists():
        try: config = json.loads(path.read_text())['config']; vendor, timeout = config['author_vendor'], config.get('timeout', timeout)
        except (OSError, ValueError, KeyError, TypeError, AttributeError): vendor = 'claude'
    if type(timeout) not in (int, float) or not 0 < timeout < float('inf'): timeout = args.timeout
    return (vendor if vendor in ('codex', 'claude') else 'claude'), float(timeout)


class ProbeParentLock:
    """FIELD-13 (docs/field13-concurrent-probes.md): one POSIX lock per run-dir parent, taken in main before run_lease. A Claude author
    permission-probe holds it for the whole command; any action on a fresh run dir holds it only until run_lease has made the dir. It only
    schedules: no listing difference is excused because of it. POSIX locks belong to the process, so this process opens the file only here."""

    def __init__(self, args: argparse.Namespace, workspace: Path, run_dir: Path):
        self.run_dir, self.workspace = Path(run_dir).expanduser().resolve(), Path(workspace).expanduser().resolve()
        vendor, timeout = _frozen_probe_config(args)
        self.whole, self.fresh = args.action == 'permission-probe' and vendor == 'claude', not self.run_dir.exists()
        self.estimate = 3 * timeout + 300   # at most three probe turns (reviewer, author, gate), plus settle and cleanup
        self.payload = {'pid': os.getpid(), 'run_dir': str(self.run_dir), 'action': args.action, 'wait_bound_s': self.estimate if self.whole else 60}
        self.fd = self.path = self.key_dir = None
        self.start = time.monotonic()   # one wait cap for the whole command, across key moves

    def _key_dir(self) -> Path:
        key_dir = self.run_dir.parent
        while not key_dir.is_dir() and key_dir.parent != key_dir: key_dir = key_dir.parent   # a missing parent: its first new entry lands here
        return key_dir

    def __enter__(self):
        while self.whole or self.fresh:
            self._acquire(key_dir := self._key_dir())
            if self._key_dir() == key_dir: break
            self.release()   # a deeper ancestor appeared while this waited: its mkdir now lands there
            if time.monotonic() - self.start > PROBE_LOCK_WAIT_FACTOR * self.estimate:
                raise ProbeLockBusy(f'the nearest existing parent of {self.run_dir} kept changing while waiting for its probe lock')
            time.sleep(PROBE_LOCK_POLL_SECONDS)
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def after_mkdir(self) -> None:
        """Inside run_lease, once the run dir exists: a mkdir-only hold ends; a probe keyed on an ancestor moves to the parent."""
        if not self.whole: self.release()
        elif self.key_dir != self.run_dir.parent: self.release(); self._acquire(self.run_dir.parent)

    def intact(self) -> bool:
        """The lock path is still the regular file this process holds locked."""
        if self.fd is None: return False
        try: now, held = os.lstat(self.path), os.fstat(self.fd)
        except OSError: return False
        return stat.S_ISREG(now.st_mode) and (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino)

    def release(self) -> None:
        if self.fd is None: return
        try:
            if self.intact(): os.unlink(self.path)   # unlinked while still locked: a waiter that then locks this inode sees it gone and reopens
        except OSError: pass
        finally: os.close(self.fd); self.fd = None

    def _acquire(self, key_dir: Path) -> None:
        lock_dir = probe_lock_dir()
        if lock_dir.resolve() == self.workspace or self.workspace in lock_dir.resolve().parents:
            raise RunLeaseError('the probe lock directory lies inside the workspace')
        try: lock_dir.mkdir(mode=0o700)
        except FileExistsError: pass
        info = lock_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RunLeaseError('probe lock directory is not a private directory owned by this user')
        ident = key_dir.stat()
        self.key_dir, self.path = key_dir, lock_dir / (hashlib.sha256(f'{ident.st_dev}:{ident.st_ino}'.encode()).hexdigest() + '.lock')
        start, holder_since, holder, noted = self.start, time.monotonic(), None, None
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        while True:
            fd = os.open(self.path, flags, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                    raise RunLeaseError('probe lock path is not a private regular file owned by this user')
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raw = os.pread(fd, 4096, 0) if exc.errno in (errno.EAGAIN, errno.EACCES) else None
                os.close(fd)   # this process holds no lock on the file while it waits
                if raw is None: raise
            except BaseException:
                os.close(fd); raise
            else:
                self.fd = fd
                if self.intact(): break
                self.fd, raw = None, b''; os.close(fd)   # unlinked or replaced between open and lock: wait, then lock the current file
            now = time.monotonic()
            try: seen = json.loads(raw)
            except ValueError: seen = {}
            seen = seen if isinstance(seen, dict) else {}
            if raw != holder: holder, holder_since = raw, now   # a new holder restarts its own bound
            claimed = seen.get('wait_bound_s')
            bound = max(self.estimate, claimed if type(claimed) in (int, float) and 0 < claimed < float('inf') else 0)   # a claim only lengthens
            who = f"unverified holder pid {seen.get('pid', 'unknown')}, run_dir {seen.get('run_dir', 'unknown')}, action {seen.get('action', 'unknown')}"
            if now - holder_since > bound or now - start > PROBE_LOCK_WAIT_FACTOR * self.estimate:
                raise ProbeLockBusy(f'another command holds the probe lock of {key_dir} ({who}; lsof {self.path} shows the real holder); '
                                    f'waited {int(now - start)} s')
            if noted is None or now - noted >= 60:
                print(f'waiting for the probe lock of {key_dir} ({who}; lsof {self.path} shows the real holder)', file=sys.stderr, flush=True); noted = now
            time.sleep(PROBE_LOCK_POLL_SECONDS)
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, json.dumps({**self.payload, 'started_at': datetime.now().astimezone().isoformat()}).encode(), 0)


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


def _control_file_digest(path: Path) -> str:
    """One control file: its bytes (and link target), or a fixed marker when it is missing, unreadable or not a regular file."""
    try:
        link = 'LINK:' + os.readlink(path) + ':' if path.is_symlink() else ''
        if path.is_file(): return link + hashlib.sha256(path.read_bytes()).hexdigest()
        return link + ('NONREGULAR' if path.exists() else 'MISSING')
    except OSError as exc: return 'UNREADABLE:' + str(exc.errno)


def git_control_state(workspace: Path, dirs=None) -> tuple[list[Path], dict]:
    """K1: the single wrapper: any exception taking the control state becomes the marker the drive loop turns into a HOLD."""
    try: return _git_control_state(workspace, dirs)
    except Exception as exc: return dirs or [], {'!unreadable': f'{type(exc).__name__}: {exc}'}

def _git_control_state(workspace: Path, dirs=None) -> tuple[list[Path], dict]:
    """D1: sha256 of the git control files an author could write: config, hooks/*, info/attributes (missing = a marker)."""
    if dirs is None:
        proc = candidate_tree.run_bounded(candidate_tree.git_command('rev-parse', '--absolute-git-dir', '--git-common-dir', cwd=workspace), cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        dirs = list(dict.fromkeys(workspace / line for line in proc.stdout.splitlines() if line)) if proc.returncode == 0 else []
    digest = {}
    if (workspace / '.git').is_file(): digest['.git'] = _control_file_digest(workspace / '.git')
    for root in dirs:
        for path in [root / 'config', root / 'config.worktree', root / 'info' / 'attributes', *sorted((root / 'hooks').rglob('*'))]:
            digest[str(path)] = _control_file_digest(path)   # a link is hashed by its target's bytes too
    if bad := [k for k, v in digest.items() if v.endswith('NONREGULAR') and not k.endswith('/hooks') and '/hooks/' not in k]: digest['!unreadable'] = 'non-regular control file ' + ', '.join(bad); return dirs, digest   # a FIFO/device/directory config or info/attributes: no git call may read it
    for scope in ('--local', '--worktree', '--global', '--system'):   # the effective config of every scope, so a file pulled in by [include] is covered
        try: proc = candidate_tree.run_bounded(candidate_tree.git_command('config', scope, '--includes', '--list', '-z', '--show-origin', cwd=workspace), cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        except Exception as exc: digest['effective-config' + scope] = 'UNAVAILABLE: ' + str(exc); continue   # a changed config that git cannot be called under still HOLDs
        digest['effective-config' + scope] = hashlib.sha256(proc.stdout + bytes([proc.returncode])).hexdigest()
    return dirs, digest
def git_head_state(workspace: Path) -> list:
    """D-EFF git guard: the commit HEAD names and the branch it is on (empty when detached or unborn)."""
    return [candidate_tree.run_bounded(candidate_tree.git_command(*args, cwd=workspace), cwd=workspace, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True).stdout.strip()
            for args in (('rev-parse', '--verify', '-q', 'HEAD'), ('symbolic-ref', '-q', 'HEAD'))]


def git_snapshot(workspace: Path) -> tuple[str, list[list[str]]]:
    """Digest tracked + untracked non-ignored files; symlinks hash their target. rel210-fixA: an executable file (git mode
    100755, the owner execute bit as git reads it) is 'exec:<sha256>', so every stage binding and the read-only check see a mode change."""
    proc = candidate_tree.run_bounded(
        candidate_tree.git_command('ls-files', '-co', '--exclude-standard', '-z', cwd=workspace), cwd=workspace,
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
            if path.stat().st_mode & stat.S_IXUSR: value = 'exec:' + value
        else:
            value = 'missing'
        manifest.append([name, value])
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return digest, manifest


def uncommitted_cause(state: dict) -> str:
    """FIELD-25: why an accept left files uncommitted: auto_commit off, or a defaulted auto_commit its checks refused."""
    reason = ((state.get('acceptance') or {}).get('delivery') or {}).get('commit_skipped')
    return f'auto_commit skipped: {reason}' if reason else 'auto_commit off'


class DefaultTestCommand(str):
    """D09 §3: the parser's `npm test` default, told apart from a test command the CLI or a profile set explicitly."""


def next_intent_command(raw_argv: list, digest: str) -> str:
    """FIELD-25: the operator's accept/reject command with --intent-only dropped and --expect DIGEST added, ready to run;
    the two steps and the digest binding stay."""
    rest, skip = [], False
    for arg in raw_argv:
        if skip: skip = False
        elif arg == '--expect': skip = True
        elif arg != '--intent-only' and not arg.startswith('--expect='): rest.append(arg)
    return shlex.join([str(Path(__file__).resolve().parents[1] / 'bin' / 'paired-session'), *rest, '--expect', digest])


INITIAL_CHANGE_HEADING = '## Initial change (as the run was created; later fixes make it stale)\n\n'


def mask_initial_change(scope: str) -> str:
    """FIELD-27: the review scope's initial-change list names the user's paths, not review history; each listed path
    becomes <repo-path> (the list is the scope's last section; the status letters and `untracked:` stay). A ledger-id or
    verdict shaped path (docs/F001.md, APPROVE.txt) stays visible: FIELD-11 keeps refusing it (an independence guard)."""
    head, sep, listing = scope.rpartition(INITIAL_CHANGE_HEADING)
    if not sep:
        return scope
    def mask(match):
        kept = LEDGER_ID_RE.search(match.group(2)) or re.search(r'\b(?:APPROVE|REVISE|needs-attention)\b', match.group(2))
        return match.group(0) if kept else match.group(1) + '<repo-path>'
    return head + sep + ''.join(re.sub(r'^(untracked: |[A-Z][0-9]*\t)(.*)$', mask, line)
                                for line in listing.splitlines(keepends=True))


def review_scope_path(name: str) -> str:
    """LG1-e: one unambiguous line per path in the review scope. A name with a tab, newline, other control character,
    backslash, leading quote or non-UTF-8 byte (surrogateescape, shown as \\udcXX) is written as an ASCII JSON string;
    any other name as is."""
    if name.isprintable() and '\\' not in name and not name.startswith('"'):
        return name
    return json.dumps(name)


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
        started = {}   # FIELD-20: a command started but never completed (still running or killed when the turn ended)
        for row in rows:
            item = row.get('item', {})
            if row.get('type') == 'item.started' and item.get('type') == 'command_execution' and item.get('id') is not None:
                started[item['id']] = item
                continue
            if row.get('type') != 'item.completed':
                continue
            started.pop(item.get('id'), None)
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
                workdir = next((item[k] for k in ('workdir', 'cwd') if isinstance(item.get(k), str)), None)   # v297-eg-wire: kept, not dropped
                call = {'tool': 'command_execution', 'input': {'command': command, **({'workdir': workdir} if workdir else {})},
                        'error': type(exit_code) is int and exit_code != 0}
                calls.append(call)
                commands.append({'command': command, 'raw_command': raw_command,
                                 'exit_code': exit_code, 'error': type(exit_code) is int and exit_code != 0,
                                 'output': item.get('aggregated_output', ''), 'source': 'command_execution'})
            elif item.get('type') == 'file_change':
                calls.append({'tool': 'file_change', 'input': {'changes': item.get('changes', [])},
                              'error': item.get('status') != 'completed'})
        for item in started.values():
            raw_command = item.get('command', '')
            command = raw_command
            try:
                outer = shlex.split(raw_command)
                if len(outer) == 3 and outer[0].endswith(('sh', 'bash', 'zsh')) and outer[1] in ('-c', '-lc'):
                    command = outer[2]
            except ValueError:
                pass
            calls.append({'tool': 'command_execution', 'input': {'command': command}, 'error': None})
            commands.append({'command': command, 'raw_command': raw_command, 'exit_code': None, 'error': False,
                             'output': item.get('aggregated_output', ''), 'source': 'command_execution',
                             'evidence_kind': 'started; no exit status when the turn ended'})
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
    return bool(configured and configured.strip()) and command.strip() == configured.strip()   # None: a no-test report run


def observed_test_succeeded(row: dict, configured: str) -> bool:
    if row.get('error') or row.get('exit_code') != 0 or not command_invokes_test(row.get('command', ''), configured):
        return False
    output = str(row.get('output', ''))
    failure = re.search(r'(?im)^(FAILED(?:\s|$)|FAILURES!|npm ERR!|not ok(?:\s|$))', output)
    return failure is None


SECRET_ENV_NAME = re.compile(r'(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL|AUTH|(?:^|_)(?:KEY|PASS|PWD)(?:_|$))', re.I)


def sensitive_access(calls: list[dict], role: str, evidence: Path, rounds: Path, cwd: Optional[Path] = None, env: Optional[dict] = None,
                     writable_roots: tuple = (), configured: tuple = (), fallbacks: Optional[list] = None) -> Optional[str]:
    """v297-eg-wire, the hybrid (owner 2026-10-04): the typed-operation evidence guard (evidence_guard.py) decides every call it can
    resolve: PROTECTED holds, ALLOW passes even where the old substring match would have held. A call it cannot resolve falls back to
    the pre-v2.9.7 substring guard, exactly as before, and its reason (cut to 300 characters) is appended to `fallbacks` (the receipt
    counts them). Secret-named variables never expand, so no secret value reaches a reason; other values may, as in the stdout evidence.
    Known limit: the guard runs after the turn, so a symlink made and removed inside the turn is not seen."""
    ctx = evidence_guard.Context(evidence, rounds, cwd, {k: v for k, v in (env or {}).items() if not SECRET_ENV_NAME.search(k)},
                                 tuple(writable_roots), tuple(c for c in configured if isinstance(c, str)))
    try: verdicts = evidence_guard.turn_verdicts(calls, ctx)
    except Exception as exc:   # a guard failure (RecursionError, ...) falls back to the substring guard for every call
        verdicts = [(evidence_guard.UNKNOWN, f'evidence guard error: {type(exc).__name__}')] * len(calls)
    if found := next((reason for verdict, reason in verdicts if verdict == evidence_guard.PROTECTED), None): return found
    for call, (verdict, reason) in zip(calls, verdicts):
        if verdict == evidence_guard.UNKNOWN:
            if fallbacks is not None: fallbacks.append(reason[:300])
            if legacy := _legacy_sensitive_access([call], role, evidence, rounds): return legacy
    return None


def configured_command_issue(workspace: Path, run_dir: Path, commands: list) -> Optional[str]:
    """v297-eg-wire admission (hybrid): only a configured test or reviewer command that names a protected path is refused, before any
    model turn; a form the guard cannot resolve is not refused (its runs fall back to the substring guard)."""
    for command in commands:
        ctx = evidence_guard.Context(run_dir / 'evidence', run_dir / 'rounds', Path(workspace).resolve(),
                                     {k: v for k, v in cli_env().items() if not SECRET_ENV_NAME.search(k)}, configured=(command,))
        found = evidence_guard.first_violation([{'tool': 'Bash', 'input': {'command': command}}], ctx)
        if found and found[0] == evidence_guard.PROTECTED:
            return (f'configured command {command!r} names a protected path ({found[1]}); keep its operands and output paths '
                    'outside the run directory')
    return None


def review_pr_pins(path: str, workspace: Path, head: Optional[str], base: Optional[str]) -> dict:
    """LG2-c: the JSON `scripts/materialize_pr.py --root` printed, as the report's pins (review-pr-port.md §2.5); it must
    describe this run: the clone is the workspace, its head is HEAD and its merge base is the review base."""
    try:
        data = json.loads(Path(path).read_text())
        pins = {'Target repository': data.get('repository') or data['target_url'], 'PR URL': data.get('url'),
                'Head': data['head']['oid'], 'Base (pinned)': data['base']['oid'], 'Merge base': data['merge_base']}
        clone = Path(data['workspace']).resolve()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f'--review-pr-pins cannot be read: {exc}') from exc
    if clone != workspace.resolve() or pins['Head'] != head or pins['Merge base'] != base:
        raise ValueError('--review-pr-pins does not describe this run: the workspace, HEAD and --base must be the '
                         'materialized clone, its head and its merge base')
    return {label: value for label, value in pins.items() if value}


def saved_no_test(config) -> bool:
    """LG2-b2: a report run created with --no-test-command froze test_command as null."""
    return isinstance(config, dict) and bool(config.get('review_report')) and 'test_command' in config and config['test_command'] is None


def _saved_configured_commands(run_dir: Path) -> list:
    """A restored run's saved test, reviewer and work-item reviewer commands, for admission (args keep the CLI defaults until the
    Coordinator restores the saved config); an unreadable state adds none, and the Coordinator reports it itself."""
    try: config = json.loads((run_dir / 'state.json').read_text())['config']
    except (OSError, ValueError, KeyError, TypeError): return []
    if not isinstance(config, dict): return []
    found = [config.get('test_command'), *[c for key in ('reviewer_command', 'workitem_reviewer_commands')
                                           for c in (config.get(key) if isinstance(config.get(key), list) else [])]]
    return [command for command in found if isinstance(command, str)]


def _legacy_sensitive_access(calls: list[dict], role: str, evidence: Path, rounds: Path) -> Optional[str]:
    """The pre-v2.9.7 substring guard, unchanged: the hybrid's fallback for calls the typed guard cannot resolve."""
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


def codex_rollout_cwds(session: Optional[str], start: float, end: float, writable: tuple = ()) -> Optional[dict]:
    """v297-eg-cwd: the cwd Codex itself recorded for each command of this CLI interval: the same rollout and window as the usage and
    attempt readers, CommandExecution items of the one turn started in the window only, and that turn's own cwd (its turn_context rows).
    None when the rollout cannot be read, when the window holds no or several turns, or when a role could write the sessions dir."""
    from urllib.parse import unquote, urlparse
    def path_of(value) -> Optional[str]:
        url = urlparse(value) if isinstance(value, str) else None
        path = unquote(url.path) if url and url.scheme == 'file' and url.netloc in ('', 'localhost') else value if url and not url.scheme else None
        return path if isinstance(path, str) and os.path.isabs(path) else None
    sessions = codex_sessions_dir().resolve()
    if not session or any(sessions == r or r in sessions.parents for r in (Path(w).resolve() for w in writable if w)):
        return None
    paths = list(sessions.rglob('*' + session + '*.jsonl'))
    if len(paths) != 1:
        return None
    rows = []
    try:
        with paths[0].open(errors='replace') as source:
            for line in source:
                try:
                    row = json.loads(line)
                    stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp()
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                if start - 1 <= stamp <= end + 1 and isinstance(row.get('payload'), dict):
                    rows.append((row.get('type'), row['payload']))
    except OSError:
        return None
    turns = {p.get('turn_id') for kind, p in rows if kind == 'event_msg' and p.get('type') == 'task_started' and p.get('turn_id')}
    if len(turns) != 1:                                                       # one CLI call is one turn; a neighbour's rows never count
        return None
    contexts = {path_of(p.get('cwd')) for kind, p in rows if kind == 'turn_context'}
    commands = [(p['item'].get('command'), path_of(p['item'].get('cwd'))) for kind, p in rows
                if kind == 'event_msg' and p.get('type') == 'item_completed' and p.get('turn_id') in turns
                and isinstance(p.get('item'), dict) and p['item'].get('type') == 'CommandExecution']
    return {'cwd': next(iter(contexts)) if len(contexts) == 1 else None, 'commands': commands}


def codex_guard_calls(calls: list[dict], proof: Optional[dict]) -> tuple[list[dict], Optional[Path], int]:
    """v297-eg-cwd: Codex command events carry no workdir. A command_execution call gets the cwd Codex recorded for that command when
    every recorded run of it in the turn has the same cwd and there are at least as many records as events; otherwise it keeps no cwd
    (unknown: the substring guard decides, as before). Code-mode cells get the turn's own cwd only when every command event is proven.
    Returns (calls for the guard, the guard's cwd or None, proven events); the receipt keeps the calls as observed."""
    if not proof:
        return calls, None, 0
    records: dict = {}
    for argv, cwd in proof['commands']:
        if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):   # unwrapped as observed_events does
            text = argv[2] if len(argv) == 3 and argv[0].endswith(('sh', 'bash', 'zsh')) and argv[1] in ('-c', '-lc') else shlex.join(argv)
            records.setdefault(text, []).append(cwd)
    events = [c['input'].get('command') for c in calls if c.get('tool') == 'command_execution' and isinstance(c.get('input'), dict)]
    changes = [c['input'].get('changes') if isinstance(c.get('input'), dict) else None for c in calls if c.get('tool') == 'file_change']
    relative = any(not isinstance(listed, list) or not all(isinstance(change, dict) and isinstance(change.get('path'), str)
                                                           and os.path.isabs(change['path']) for change in listed)
                   for listed in changes)   # a relative patch path may have been applied in another workdir
    guarded, proven, total = [], 0, 0
    for call in calls:
        given = call.get('input')
        if call.get('tool') == 'command_execution' and isinstance(given, dict) and 'workdir' not in given:
            total += 1
            found = records.get(given.get('command'), [])
            if len(set(found)) == 1 and found[0] is not None and len(found) >= events.count(given.get('command')):
                call, proven = {**call, 'input': {**given, 'workdir': found[0]}}, proven + 1
        guarded.append(call)
    return guarded, Path(proof['cwd']) if proven == total and proof['cwd'] and not relative else None, proven


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


def progress_line(row: dict) -> str:
    """PL: the one human-readable rendering of a progress.jsonl row, shared by the live stdout line and status --brief."""
    f = {k: v for k, v in row.items() if k not in ('ts', 'seq')}
    kind, label = f.pop('kind'), f.pop('label', '')
    when = time.strftime('%H:%M:%S', time.localtime(calendar.timegm(time.strptime(row['ts'], UTC_FORMAT))))
    if 'role' in f: f['role'] = f"{f['role']} {f.pop('vendor')}/{f.pop('model')} {f.pop('effort')} " + ('fresh' if f.pop('fresh') else 'persistent')
    if 'seconds' in f: f['seconds'] = f"{f['seconds']}s"
    if 'security' in f: f['severity'] = f['severity'] + ('+sec' if f.pop('security') else '')
    if 'invocation_cap' in f: f['invocations_used'] = f"{f.pop('invocations_used')}/{f.pop('invocation_cap')} invocations"
    if 'rf5_converted' in f: f['verdict'] += ' (RF-5: APPROVE converted)' if f.pop('rf5_converted') else ''
    if f.get('raw'): f['raw'] = 'raw ' + f['raw']
    if isinstance(f.get('open'), list): f['open'] = f"({len(f['open'])} open: {', '.join(f['open'])})"
    return f'[{when}] ' + ' · '.join(str(x) for x in [label, kind, *f.values()] if x not in (None, '', False))


class ReadOnlyTurnVoided(ValueError):
    """D-EFF category A: a reviewer, gate or shadow turn changed the workspace; its verdict is never used."""
    def __init__(self, message: str, restored: bool):
        super().__init__(message)
        self.restored = restored


class Coordinator:
    def __init__(self, args: argparse.Namespace, *, _fake_lifecycle=False):
        resolve_role_model_defaults(args)
        if not (Path(args.run_dir) / 'state.json').exists(): validate_role_models(args)   # an existing run validates after restoring its models
        if args.lifecycle_mode == 'on':
            if args.adversarial_gate == 'off': raise ValueError('lifecycle refuses --adversarial-gate off')
            if args.polish: raise ValueError('lifecycle refuses resume --polish')
            if _fake_lifecycle and not lifecycle_spine.fake_guard(args):
                raise ValueError('lifecycle remains disabled until every stage and isolation check is implemented')
        if getattr(args, 'resume_timeout', None) is not None and args.action != 'resume':
            raise ValueError('--resume-timeout is accepted only with resume')
        if getattr(args, 'acknowledge_codex_trust', None) and args.action != 'resume': raise ValueError('--acknowledge-codex-trust requires resume')
        if getattr(args, 'wi_deadline', None) is not None and args.wi_deadline <= 0: raise ValueError('--wi-deadline must be a positive number of seconds')
        if not 1 <= getattr(args, 'timeout', DEFAULT_TIMEOUT_SECONDS) <= MAX_TIMEOUT_SECONDS:   # timeoutcap: before any state exists
            raise ValueError(TIMEOUT_RANGE_ERROR)
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
        self._probe_env_names = None   # FIELD-10: set only while probe_passed() compares with the probe's own env-derived names
        self.rounds = self.run_dir / 'rounds'
        self.evidence = self.run_dir / 'evidence'
        self.context = self.run_dir / 'context'
        self.internal = self.run_dir / 'internal'
        self.state_path = self.run_dir / 'state.json'
        requested = getattr(args, 'safety_mode', None)                   # --strict or the operator profile; None when neither names it
        args.safety_mode = requested or DEFAULT_SAFETY_MODE
        upgraded = None
        if self.state_path.exists():                                     # D-EFF: frozen at creation; a pre-D-EFF run is strict
            saved_state = json.loads(self.state_path.read_text())
            self.state = saved_state
            self._review_report_args(saved_state['config'])
            if args.action in ('note', 'reject'): self._refuse_report_feedback()
            args.safety_mode = saved_state.get('config', {}).get('safety_mode', 'strict')
            if requested and requested != args.safety_mode:
                probe = ('PROBE', 'AUTHOR_PERMISSION_PROBE')
                probe_only = (all(t.get('phase') in probe for t in saved_state.get('turns', []))
                              and all((saved_state.get(key) or {}).get('phase', 'PROBE') in probe for key in ('active', 'uncertain_active')))
                fixed = (f'safety_mode is fixed for this run: it was created {args.safety_mode} by its first command; '
                         f'start a new run directory to use {requested}, or drop safety_mode from the --config profile')
                if args.action not in ('run', 'resume', 'reject', 'permission-probe'): print(f'NOTE: {fixed}')   # dispatches no turn
                elif not (requested == 'strict' and probe_only): raise ValueError(fixed)
                else:
                    saved_state['config']['safety_mode'] = args.safety_mode = 'strict'   # only a probe has run: the run may still start strict
                    upgraded = saved_state
        if args.lifecycle_mode == 'on' and not _fake_lifecycle:   # W1a: the real path is the worktree lifecycle (ADR-11)
            worktree_lifecycle.refuse_waivers(args)   # INT-2c: after the safety_mode resolution (D-7 is strict only), before its write
        if upgraded: atomic_json(self.state_path, upgraded)   # a refused command leaves the frozen mode unchanged
        if not self.state_path.exists():
            self.args.exec_turn_timeout = resolve_exec_turn_timeout(
                self.args.exec_turn_timeout, self.args.timeout)
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
            if not {'approved_snapshot', 'rejected_digests'} <= self.state.keys():
                raise ValueError('run was created by an older paired-session build; start a new run')
            if not self._fake_lifecycle: worktree_lifecycle.refuse_saved(self.state, args)
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
            self._review_only_args(self.state['config'])
            if self.args.action in ('accept', 'reject', 'note', 'attach-verification'):
                if Path(self.state['workspace']) != self.workspace or Path(self.state['workitem']) != self.workitem:
                    raise ValueError('accept/reject workspace/workitem differs from state')
                saved = self._restore_role_policy(getattr(self.args, 'explicit_role_flags', ()))
                for key, value in saved.items():
                    if key == 'gate_prompt' and str(value).startswith('<bundled-default>:'):
                        self.args.gate_prompt = str(DEFAULT_GATE_PROMPT)
                    elif hasattr(self.args, key) and not (key in OPERATOR_ONLY_DESTS or key.startswith('accept_')):
                        setattr(self.args, key, value)
                self.args.exec_turn_timeout = resolve_exec_turn_timeout(
                    self.state['config'].get('exec_turn_timeout'),
                    self.state['config'].get('timeout', DEFAULT_EXEC_TURN_TIMEOUT_SECONDS))
                validate_role_models(self.args)
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
            if self.args.action in ('accept', 'reject', 'note', 'attach-verification'):
                raise ValueError(f'{self.args.action} requires an existing coordinator run')
            if not (self.workspace / '.git').exists():
                raise ValueError('--workspace must be a git worktree')
            if not self.workitem.is_file():
                raise ValueError('--workitem must be a file')
            _, issue = program_snapshot(self.workspace, self.run_dir, self.author_temp_dir, self.args.codex_bin,
                                        self.args.claude_bin, self.args.gate_prompt, self.args.config)
            if issue and issue.startswith('workspace profile') and self.args.action != 'permission-probe': raise ValueError(issue)   # before any profile role/model/gate choice is frozen into state; the probe reports it (existing test)
            self._review_only_args(None, self._parent_spec() if args.supersedes else None)
            self._review_report_args(None)
            scope = self._refuse_review_only_start() if self.args.review_only else None   # before any state
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
                'test_command_explicit': not isinstance(args.test_command, DefaultTestCommand),   # D09 §3
            }
            if self._pr_pins:
                self.state['review_pr'] = self._pr_pins   # LG2-c: review-report.md and review_post.py read them
            if self._fake_lifecycle:
                self.state['lifecycle'] = lifecycle_spine.initial(self.state['item_uuid'], self.state['base_commit'])
            elif args.lifecycle_mode == 'on':
                self.state['lifecycle'] = worktree_lifecycle.initial(self.state['item_uuid'], self.state['base_commit'])
            self._freeze_role_dispatch()
            if scope is not None:
                self._start_review_only(scope)
            if args.supersedes:
                parent = Path(args.supersedes).resolve()
                old = json.loads((parent / 'state.json').read_text())
                spec = json.loads((parent / 'evidence/successor-spec.json').read_text())
                task_hash = hashlib.sha256(self.workitem.read_bytes()).hexdigest()
                if (old.get('status') != 'ABORTED' or old.get('abort_kind') != 'scope-change' or
                        not old.get('base_commit') or old.get('base_commit_backfilled') or
                        old.get('scope_chain_depth', 0) >= 1 or spec.get('base_commit') != old['base_commit'] or spec['task_sha256'] != task_hash or
                        spec['run_dir'] != str(self.run_dir) or spec['workspace'] != str(self.workspace) or
                        frozen_config.get('allowed_models') != old['config'].get('allowed_models') or
                        spec['original_hash'] != hashlib.sha256(Path(spec['original_workitem']).read_bytes()).hexdigest() or
                        spec.get('config_sha256') != hashlib.sha256((parent / 'evidence/successor-config.json').read_bytes()).hexdigest() or
                        old.get('successor_spec_sha256') != hashlib.sha256((parent / 'evidence/successor-spec.json').read_bytes()).hexdigest()):
                    raise ValueError('successor spec or parent state differs')
                if spec.get('item_uuid') and (spec['item_uuid'] != old.get('item_uuid') or
                        spec.get('item_blockers') != pending_item_blockers(old, parent)):
                    raise ValueError('successor item identity or blockers differ')
                if candidate_tree.run_bounded(candidate_tree.git_command('merge-base', '--is-ancestor', old['base_commit'], 'HEAD', cwd=self.workspace),
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
                                  item_blockers_complete=bool(spec.get('item_uuid') and spec.get('item_blockers_complete')),
                                  test_command_explicit=bool(spec.get('test_command_explicit', self.state['test_command_explicit'])))
                if spec.get('quality_writers') and worktree_lifecycle.is_worktree(self.state):   # D09 §3
                    self.state['lifecycle']['quality_writers'] = copy.deepcopy(spec['quality_writers'])
            if worktree_lifecycle.is_worktree(self.state):   # after every refusal above, before any probe or turn
                self.state['lifecycle']['security_baseline'] = (
                    self._inherited_security_baseline(Path(args.supersedes).resolve()) if args.supersedes else
                    self._capture_security_baseline())
                self.state['lifecycle']['ignore_coverage_at_start'] = self._start_ignore_coverage()
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

    def _restore_role_policy(self, explicit) -> dict:
        """Saved role vendors, models and allowed_models win; only an explicit different flag is refused."""
        saved = self._saved_config()
        for key in explicit:
            if getattr(self.args, key) != saved[key]:
                raise ValueError(f'role models are fixed for this run: {key} differs from the saved run')
        policy = saved.get('allowed_models')
        if getattr(self.args, 'allowed_models', None) not in (None, policy):
            raise ValueError('role policy is fixed for this run: allowed_models differs from the saved run')
        self.args.allowed_models = policy
        for key in (*ROLE_DESTS, 'gate_vendor_source'):
            setattr(self.args, key, saved[key])
        return saved

    @staticmethod
    def _readonly_changed(workspace, recorded, before, after) -> bool:   # content (the coordinator's snapshot), HEAD, branch or index
        if before != after: return True
        if not recorded or 'head' not in recorded: return False
        try: return readonly_guard.moved(workspace, recorded)
        except (OSError, RuntimeError, subprocess.SubprocessError): return True   # unknown: void it; the undo then verifies or holds

    def _void_readonly_turn(self, role, receipt, workspace, recorded, keep, prefix, before) -> ReadOnlyTurnVoided:
        """D-EFF category A (docs/efficient-mode.md §4a): save the change as evidence and undo it from the pre-turn record, only when
        the undo verifies (HEAD, branch, index, worktree tree, and the coordinator's own snapshot); otherwise the run must hold.
        INT-2c: the turn's process group is stopped first, so no child left in it can change the tree again after the restore."""
        unrestored = {'sequence': receipt['sequence'], 'role': role, 'snapshot': before,   # resume refuses until the workspace is back
                      'marks': {key: (recorded or {})[key] for key in ('head', 'branch', 'index') if key in (recorded or {})}}
        try: self._stop_turn_group(receipt['sequence'])
        except RuntimeError as exc:
            receipt['voided'] = {'restored': False, 'reason': str(exc)}
            self._record_unrestored(unrestored, workspace, recorded,
                                    reason=f'process group could not be stopped (stop every process of that turn first): {exc}')
            return ReadOnlyTurnVoided(f'{role} mutated workspace and its process group could not be stopped ({exc}); '
                                      'stop it and restore the workspace by hand', restored=False)
        if not recorded or 'error' in recorded:
            receipt['voided'] = {'restored': False, 'reason': (recorded or {}).get('error', 'no pre-turn record')}
            self._record_unrestored(unrestored, workspace, recorded, reason=receipt['voided']['reason'])
            return ReadOnlyTurnVoided(f'{role} mutated workspace; there is no verified pre-turn record to restore it from '
                                      f'({receipt["voided"]["reason"]}); restore the workspace by hand', restored=False)
        diff = prefix.with_suffix('.workspace-change.diff')
        try:
            readonly_guard.evidence(workspace, recorded, keep, diff)
            why = readonly_guard.restore(workspace, recorded, keep)
            if why is None and git_snapshot(workspace)[0] != before: why = 'the workspace snapshot still differs after the restore'
        except Exception as exc: why = f'{type(exc).__name__}: {exc}'
        receipt['voided'] = {'evidence': str(diff), 'restored': why is None, **({'restore_failure': why} if why else {})}
        if why:
            self._record_unrestored(unrestored, workspace, recorded, reason=why, evidence=str(diff))
        if why: return ReadOnlyTurnVoided(f'{role} mutated workspace and the coordinator could not restore it ({why}); '
                                          f'restore it by hand, see {diff}', restored=False)
        return ReadOnlyTurnVoided(f'{role} mutated workspace; the turn is void and the workspace was restored (evidence: {diff})', restored=True)

    def _record_unrestored(self, unrestored, workspace, recorded, **fields) -> None:
        """roperm: the permission bits are taken as the record is written (after the group stop and any restore try): every recorded
        path whose bits are not as before, a path now missing or no longer a regular file included, so clearing waits for them."""
        bits = {name: recorded['perms'][name] for name in readonly_guard.perm_changes(workspace, recorded or {}, missing=True)}
        self.state['unrestored_readonly_turn'] = {**unrestored, **fields, **({'perms': bits} if bits else {})}
        self._save_unrestored()

    def _save_unrestored(self) -> None:   # on disk at once: no later exception of this turn may lose the record
        try: self.save()
        except Exception as exc: print(f'WARNING: could not save the unrestored read-only turn record: {type(exc).__name__}: {exc}')

    def unrestored_workspace_issue(self) -> str:
        """D-EFF category A: a read-only turn's change that could not be undone blocks every dispatch until the workspace is back to
        the coordinator's pre-turn snapshot (content digest, HEAD, branch and index, and the permission bits the turn changed);
        then the record is cleared."""
        if not (record := self.state.get('unrestored_readonly_turn')): return ''
        try: back = git_snapshot(self.workspace)[0] == record['snapshot'] and all(
                 value == readonly_guard.marks(self.workspace)[key] for key, value in record['marks'].items()) and not (
                 readonly_guard.perm_changes(self.workspace, record, missing=True))
        except (OSError, RuntimeError, subprocess.SubprocessError, KeyError): back = False
        if back:
            self.state.pop('unrestored_readonly_turn'); self.save()
            return ''
        return (f'read-only turn {record["sequence"]} ({record["role"]}) changed the workspace and it could not be restored; manual '
                f'restore needed ({record["reason"]}' + (f'; evidence: {record["evidence"]}' if record.get('evidence') else '') +
                '); restore the workspace by hand to its state before that turn (content, HEAD, branch and index; the evidence diff '
                'shows the change' + ('; file modes: ' + ', '.join(f'{name} {mode:04o}' for name, mode in sorted(record['perms'].items()))
                                      if record.get('perms') else '') + '). The next run, resume, reject or accept clears this record by itself once the workspace matches '
                'again; otherwise abort the run')

    @property
    def strict(self) -> bool:   # D-EFF: category C (probe gate, a holding evidence guard) as before; anything but 'efficient' is strict
        return getattr(self.args, 'safety_mode', 'strict') != 'efficient'

    def _saved_config(self) -> dict:
        """ADR-10 M3: a saved run without gate_vendor keeps the old opposite-author derivation; no source key restores as legacy-derived."""
        return {'gate_vendor': old_gate_vendor(self.state['config']['author_vendor']), 'gate_vendor_source': 'legacy-derived',
                'safety_mode': 'strict', **self.state['config']}   # D-EFF: a run saved before safety_mode is strict

    def _config(self) -> dict:
        keys = ('author_vendor', 'author_model', 'author_effort', 'reviewer_vendor',
                'reviewer_model', 'reviewer_effort', 'shadow', 'adversarial_gate',
                'gate_vendor', 'gate_vendor_source', 'gate_model', 'gate_effort', 'max_plan_rounds', 'max_exec_rounds',
                'timeout', 'exec_turn_timeout', 'max_invocations', 'exercise_revisions', 'test_command',
                'gate_prompt', 'reviewer_command', 'polish_round', 'codex_bin', 'claude_bin',
                'author_subagents', 'lifecycle_mode', 'docs_file', 'docs_allowlist',
                'skip_globs', 'skip_quality_polish', 'allowed_models', 'auto_commit', 'external_delivery',
                'safety_mode', 'quality_writers')
        config = {key: getattr(self.args, key) for key in keys}
        if getattr(self.args, 'wi_deadline', None) is not None: config['wi_deadline'] = self.args.wi_deadline   # F2: only a run with a deadline saves the key
        if getattr(self.args, 'review_only', None): config.update(review_only=True, review_base=self.args.review_base)   # D-LG1, as F2
        if getattr(self.args, 'auto_commit_source', None): config['auto_commit_source'] = self.args.auto_commit_source   # as F2
        if getattr(self.args, 'review_report', None):   # LG2-a1, as F2
            config.update(review_report=True, review_aspects=list(self.args.review_aspects))   # LG2-b1
        gate_prompt = Path(self.args.gate_prompt).expanduser()
        if not gate_prompt.is_absolute():
            gate_prompt = self.workspace / gate_prompt
        gate_prompt = gate_prompt.resolve()
        config['gate_prompt'] = (
            '<bundled-default>:' + hashlib.sha256(gate_prompt.read_bytes()).hexdigest()
            if gate_prompt == DEFAULT_GATE_PROMPT.resolve() else str(gate_prompt))
        worktree = self.args.lifecycle_mode == 'on' and not self._fake_lifecycle
        def frozen_doc_path(value):
            if any(char in value for char in '*?['): raise ValueError('lifecycle doc paths must be exact')
            path = (self.workspace / value).expanduser().resolve()
            if self.workspace not in path.parents: raise ValueError('lifecycle doc path escapes workspace')
            if worktree:   # W: an exact documentation file, reached without a symlink (doc 6 DOCS)
                if path != Path(os.path.normpath(self.workspace / value)) or path.is_dir():
                    raise ValueError(f'lifecycle docs path must be a file reached without symlinks: {value}')
                try:
                    docs_policy._exact_paths([relative := path.relative_to(self.workspace).as_posix()], True)
                    if worktree_lifecycle.docs_denied(relative, ''): raise ValueError('in the DOCS HOLD set')
                except (ValueError, candidate_tree.CandidateError) as exc:
                    raise ValueError(f'lifecycle docs path is not a documentation path: {value}') from exc
            return str(path)
        docs_file = (self.args.docs_file if self.args.docs_file is not None else
                     'CHANGELOG.md' if worktree else '')   # doc 6: the W default; --docs-file '' turns it off
        config['docs_file'] = frozen_doc_path(docs_file) if docs_file else ''
        config['docs_allowlist'] = sorted({frozen_doc_path(value) for value in
                                           [*self.args.docs_allowlist, *([docs_file] if docs_file else [])]})
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
        if (not current['role_flags']['author']['tmp_isolated'] and not self._fake_lifecycle and
                not worktree_lifecycle.is_worktree(self.state)):   # W accepts the real-EXEC author TMP (ADR-11)
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
            if command and command not in result:   # LG2-b2: a no-test report run has no test command
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
            issue = ('configured operator program or PATH changed since permission probe (changed: '
                     + ', '.join(sorted(k for k in set(frozen) | set(current) if frozen.get(k) != current.get(k))) + ')')   # FIELD-10: name it
        if issue and hold and self.state.get('status') != 'DONE': self.hold(issue)
        return current, issue
    def reviewer_flags(self) -> dict:
        """Permission surface whose probe PASS is valid for a later run."""
        return self._readonly_flags('reviewer', 'reviewer')

    def gate_flags(self) -> dict:
        """The gate's read-only surface, built like the reviewer's (G-a K1)."""
        return self._readonly_flags('gate', 'gate')

    def _readonly_flags(self, prefix: str, sandbox_role: str) -> dict:
        vendor = getattr(self.args, prefix + '_vendor')
        flags = {
            'surface_version': PROBE_SURFACE_VERSION,
            prefix + '_vendor': vendor,
            prefix + '_model': getattr(self.args, prefix + '_model'),
            prefix + '_effort': getattr(self.args, prefix + '_effort'),
            prefix + '_commands': self.reviewer_commands(),
            prefix + '_binary': self.args.claude_bin if vendor == 'claude' else self.args.codex_bin,
            'permission_mode': 'dontAsk' if vendor == 'claude' else 'never',
            'sandbox': 'restricted-allowlist' if vendor == 'claude' else 'read-only',
        }
        if vendor == 'claude':
            flags['claude_child_env'] = CLAUDE_CHILD_ENV
            flags['claude_bash_sandbox'] = self._claude_sandbox_settings(sandbox_role)
            flags['claude_os_denial_probe'] = str(self._claude_os_probe_path('gate-probe' if prefix == 'gate' else 'probe'))
        if vendor == 'codex':
            flags['ignore_execpolicy_rules'] = True
            flags['codex_plugins_argv'] = list(CODEX_PLUGINS_OFF)
            flags['codex_readonly_profile'] = codex_readonly_profile_args()   # b295-f1: a new digest, so an older probe PASS or cache entry stops matching
            flags['scratch_tmp'] = str(self.run_dir / 'role-tmp') + '/<seq>-<role> (0700, one per dispatch, TMPDIR/TMP/TEMP)'
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
                'codex_plugins_argv': list(CODEX_PLUGINS_OFF),
                **self._author_sandbox_overrides(),
                'author_escape_probe': 'codex-exec-model-filesystem-v2',
                'codex_cli_version': self._codex_cli_version(),
                'codex_config_sha256': self._codex_policy_digest(),
            })
        else:
            flags.update({'plugin_version': plugin_version(), 'claude_child_env': CLAUDE_CHILD_ENV, 'permission_mode': 'acceptEdits', 'claude_author_edit_rules': self._claude_author_edit_rules(),
                          'author_subagents': self.args.author_subagents,
                          'claude_author_surface': cap.surface(self._claude_command('author', cap.NO_SCHEMA, True)),
                          'claude_bash_sandbox': self._claude_sandbox_settings('author'),
                          'non_bash_run_state_edit_access': 'denied by Edit/Write path rules'})
        return flags

    def codex_capabilities(self, workspace=None) -> dict:
        return codex_capability_guard.inspect(self.global_codex_home, workspace or self.workspace,   # rel210-fixCG: every
                                              launch_plugins_off=CODEX_PLUGINS_OFF == ('-c', 'features.plugins=false'))   # Codex argv has it

    def _codex_cli_version(self) -> str:
        try:
            if self._program_state()[1]: return 'UNAVAILABLE'
            result = subprocess.run([self.state['operator_programs']['codex_bin']['path'], '--version'], text=True,
                                    capture_output=True, timeout=10 * timeout_scale.env_factor())
            return result.stdout.strip() if result.returncode == 0 else 'UNAVAILABLE'
        except (OSError, subprocess.SubprocessError):
            return 'UNAVAILABLE'

    def _codex_config_bytes(self) -> bytes:
        """CG-2: a missing $CODEX_HOME/config.toml (a fresh login home has only auth.json) is an empty config everywhere it is read or digested."""
        try: return (self.global_codex_home / 'config.toml').read_bytes()
        except FileNotFoundError: return b''

    def _codex_policy_digest(self) -> Optional[str]:
        try: text = self._codex_config_bytes().decode()
        except (OSError, UnicodeError): return None
        roots = '|'.join(re.escape(path) for path in dict.fromkeys([str(self.workspace), *_codex_trust_paths(self.workspace)])) + '|' + re.escape(str(self.run_dir)) + r'/paired-session-author-probe-[^"]+/workspace'
        raw = re.sub(r'(?m)^\[projects\."(?:' + roots + r')"\]\ntrust_level = "trusted"\n', '', text)
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

    def codex_contract_verified(self) -> tuple[bool, str]:
        """Verified set, or this run's full probe_passed() record / operator override for the installed version."""
        version = self._codex_cli_version()
        override = self.state.get('codex_cli_override') or {}
        if version != 'UNAVAILABLE' and override.get('version') not in (None, version) and not override.get('voided'):
            override['voided'] = {'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'version_seen': version}
            self.save()
        if version in VERIFIED_CODEX_CLI_VERSIONS: return True, ''
        if version == 'UNAVAILABLE':
            return False, 'codex-cli version is UNAVAILABLE (binary unreadable); re-run permission-probe'
        if override.get('version') == version and override.get('actor') == 'operator' and not override.get('voided'):
            return True, ''
        if self.probe_passed()[0]: return True, ''
        voided = f'; the operator override for {override["version"]} is void' if override.get('voided') else ''
        return False, (f'unverified codex sandbox contract: {version}{voided}; run permission-probe on this version '
                       'or pass --accept-unverified-codex-cli --reason TEXT')

    def _claude_optin_current(self) -> bool:   # a recorded operator opt-in bound to the current author flags; a flags change voids it for good
        if self._probe_env_names is not None:   # FIELD-10: inside probe_passed's env-name comparison, judge the opt-in in the current environment
            saved, self._probe_env_names = self._probe_env_names, None
            try: return self._claude_optin_current()
            finally: self._probe_env_names = saved
        digest, optin = self.author_flags_digest(), self.state.get('claude_author_override') or {}
        if optin and optin.get('author_flags_digest') != digest and not optin.get('voided'):
            optin['voided'] = {'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'digest_seen': digest,
                               **self._void_cause(optin, {'author_flags_digest': self.author_flags_digest})}   # ENV-NAME: say why
            self.save()
        return optin.get('actor') == 'operator' and optin.get('author_flags_digest') == digest and not optin.get('voided')

    def _env_names_now(self) -> list[str]:   # ENV-NAME: the secret env-var names the Claude sandbox denies in this environment
        return sorted(entry['name'] for entry in self._claude_sandbox_settings('author')['sandbox']['credentials']['envVars'])

    def _under_env_names(self, names, compute):   # FIELD-10's comparison: evaluate with another env-name set, then restore
        saved, self._probe_env_names = self._probe_env_names, set(names)
        try: return compute()
        finally: self._probe_env_names = saved

    def _void_cause(self, record: dict, digests: dict) -> dict:
        """ENV-NAME (docs/env-name-effects.md): why an operator acceptance stopped matching. Only the secret env-var names changed when
        its recorded digests come back under the names recorded with it; the void itself is unchanged (owner decision D-ENV-1)."""
        names = record.get('secret_env_names')
        if not (isinstance(names, list) and all(isinstance(n, str) for n in names)): return {'cause': 'flags changed'}
        if any(self._under_env_names(names, compute) != record.get(key) for key, compute in digests.items()): return {'cause': 'flags changed'}
        now = set(self._env_names_now())
        return {'cause': 'secret env-var names changed', 'names_added': sorted(now - set(names)), 'names_removed': sorted(set(names) - now)}

    @staticmethod
    def _void_text(record: dict) -> str:
        voided = record.get('voided') if isinstance(record.get('voided'), dict) else {}
        if 'cause' not in voided and isinstance(voided.get('reason'), str): return voided['reason']   # e.g. permission-probe re-run
        if voided.get('cause') != 'secret env-var names changed': return 'flags changed'
        return ('only the secret env-var names changed: added ' + (', '.join(voided.get('names_added') or []) or 'none')
                + '; removed ' + (', '.join(voided.get('names_removed') or []) or 'none'))

    def _author_probe_waived(self, report: dict) -> bool:
        """HL-FIX: the opt-in waives only the Claude author probe: the report is UNKNOWN solely for the author's model-escape-unknown / refused
        reasons, the reviewer probe passed (allowed command ran, every write denied, snapshot unchanged) and no gate or other cause is recorded."""
        author_probe = report.get('author_permission_probe')
        reasons = report.get('failure_reasons')
        return bool(self.args.author_vendor == 'claude' and isinstance(author_probe, dict) and author_probe.get('status') == 'UNKNOWN'
                    and report.get('status') == 'UNKNOWN' and not author_probe.get('model_escape_failed_targets')
                    and isinstance(reasons, list) and reasons and set(reasons) <= {'author-model-escape-unknown', 'author-model-refused'}
                    and report.get('allowed_command_ran') is True and report.get('snapshot_unchanged') is True
                    and (report.get('global_config_changes') or {}).get('status') == 'PASS'
                    and isinstance(report.get('write_attempts_denied'), dict) and all(v is True for v in report['write_attempts_denied'].values())
                    and self._claude_optin_current())

    def claude_author_verified(self) -> tuple[bool, str]:
        """A Claude author dispatches only on a Claude author probe PASS (P0-3b) or a current operator opt-in."""
        if self._claude_optin_current():
            return True, ''
        optin = self.state.get('claude_author_override') or {}
        try:
            probed = json.loads((self.run_dir / 'permission-probe.json').read_text()).get(
                'author_permission_probe', {}).get('claude_author_status') == 'PASS'
        except (OSError, ValueError, AttributeError):
            probed = False
        if probed and (passed := self.probe_passed())[0]: return True, ''
        voided = f'; the earlier operator opt-in is void ({self._void_text(optin)})' if optin.get('voided') else ''
        detail = ('; the recorded probe does not pass: ' + passed[1]) if probed else '; no Claude author probe PASS is recorded in permission-probe.json'   # FIELD-10
        return False, ('a Claude author is limited to the fake test harness in this preview (1C row 3b) unless a Claude '
                       'author permission-probe passes (P0-3b) or the operator opts in with `run '
                       '--accept-unverified-claude-author --reason TEXT` (permission-probe does not take the flag)' + voided + detail)

    def _codex_sandbox_profile_args(self) -> list[str]:
        if not (verified := self.codex_contract_verified())[0]:
            raise ValueError(verified[1])
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
        # b296-f1e: only `codex sandbox` (the escape check) takes this argv; `codex sandbox` has -P, `codex exec` does not.
        return ['-P', 'paired_session_author', '--config', 'permissions.paired_session_author.filesystem=' + inline,
                '--config', 'permissions.paired_session_author.network.enabled=false']

    def reviewer_flags_digest(self) -> str:
        raw = json.dumps(self.reviewer_flags(), sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(raw).hexdigest()

    def gate_flags_digest(self) -> str:
        return hashlib.sha256(json.dumps(self.gate_flags(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _claude_author_edit_rules(self, workspace: Optional[Path] = None) -> tuple[list[str], list[str]]:
        """Path-scoped Edit rules for the Claude author (they cover Write too): only the effective workspace is editable."""
        workspace, context = Path(workspace or self.workspace).resolve(), self.context.resolve()
        home = Path.home().resolve()
        homes = [home / name for name in ('.claude', '.codex', '.ssh', '.aws')]
        roots = [context, self.run_dir.resolve(), *homes]
        if (re.search(r'[*?\[\]{}(),]', str(workspace)) or workspace in context.parents or workspace in (home, *home.parents)
                or any(root == workspace or root in workspace.parents for root in roots)):
            raise ValueError('the workspace must not be the filesystem root, the home dir or an ancestor of it, hold the context dir, '
                             'sit inside the context, run dir or a denied home dir (~/.claude, ~/.codex, ~/.ssh, ~/.aws), '
                             'or hold rule metacharacters')
        rule = lambda path, tail: f'Edit(//{path.as_posix().lstrip("/")}{tail})'
        # No '//parent/*' sibling deny: gitignore-style matching could cover the workspace itself; siblings are not allowed anyway.
        return [rule(workspace, '/**')], [rule(context, '/**'), rule(home / '.cache' / 'review-loop' / 'probe-pass', '/**'), *(f'Edit(~/{home.name}/**)' for home in homes)]   # P0-4 V4

    def _claude_sandbox_settings(self, role: str) -> dict:
        """Strict OS boundary for Claude Bash, independent of Claude tool permissions."""
        secret_names = set(self._probe_env_names) if self._probe_env_names is not None else {
            name for name in os.environ
            if SECRET_ENV_NAME.search(name)
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
                    'denyWrite': [str(self.run_dir)] + ([str(self._claude_os_probe_path(role))] if role in ('probe', 'gate-probe') and self._probe_sandbox_commands else [])
                                 + ([str(self.context)] if role == 'author' and not self.context.is_relative_to(self.run_dir) else [])   # the probe's context; the real one is under run_dir
                                 + ([str(probe_cache_root())] if role == 'author' and ((r := probe_cache_root().resolve()) == (w := self.workspace.resolve()) or r in w.parents or w in r.parents) else []),   # P0-4b: an overlapping workspace would make the cwd-bound Bash sandbox cover the cache
                },
            },
            'permissions': {
                'deny': [f'Edit(//{self.run_dir.as_posix().lstrip("/")}/**)'],
            },
        }

    def _claude_os_probe_path(self, role: str = 'probe') -> Path: return self.run_dir.parent / ('.paired-session-os-probe-' + hashlib.sha256((str(self.run_dir) + ('|gate-probe' if role == 'gate-probe' else '')).encode()).hexdigest()[:16])   # G-a K2: the gate probe's OS-only target differs from the reviewer probe's

    def author_flags_digest(self) -> str:
        raw = json.dumps(self.author_flags(), sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(raw).hexdigest()

    def probe_passed(self) -> tuple[bool, str]:
        """FIELD-10: the credential env-name deny list follows each command's environment (every secret-looking name present is
        denied), so it alone never voids a PASS: a flags/rules mismatch is re-checked once with the probe's own names; every other
        input stays bound, and the real dispatch keeps the current names."""
        ok, reason = self._probe_passed_once()
        if ok or not (reason.endswith('flags do not match this run') or reason.endswith('Claude author rules do not match this run')):
            return ok, reason
        if (names := self._probe_report_env_names()) is None: return ok, reason
        self._probe_env_names = names
        try: again = self._probe_passed_once()
        finally: self._probe_env_names = None
        return (True, '') if again[0] else again   # the check that still fails once the env names are set aside

    def _probe_report_env_names(self) -> Optional[set]:
        try: report = json.loads((self.run_dir / 'permission-probe.json').read_bytes())
        except (OSError, ValueError): return None
        for key in ('reviewer_flags', 'author_flags', 'gate_flags'):
            try: return {entry['name'] for entry in report[key]['claude_bash_sandbox']['sandbox']['credentials']['envVars']}
            except (KeyError, TypeError): continue
        return None

    def _probe_passed_once(self) -> tuple[bool, str]:
        if (issue := self._program_state()[1]): return False, issue
        path = self.run_dir / 'permission-probe.json'
        if not path.exists():
            return False, 'permission-probe.json is missing' + (f' (gate vendor {self.args.gate_vendor} differs from reviewer vendor {self.args.reviewer_vendor}: only a passing gate probe covers it)'
                                                               if self.args.gate_vendor != self.args.reviewer_vendor and not lifecycle_spine.fake_dispatch_guard(self.args) else '')
        try:
            raw = path.read_bytes()
            report = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return False, 'permission-probe.json is unreadable'
        # A Claude author's PASS must be the very file permission_probe wrote (state.json shares run_dir's write protection).
        if self.args.author_vendor == 'claude' and self.state.get('permission_probe') != {'sha256': hashlib.sha256(raw).hexdigest(), 'turn': report.get('probe_turn')}:
            return False, 'permission-probe.json is not the report this run recorded'
        waived = self._author_probe_waived(report)   # HL-FIX: only the author's part of an UNKNOWN report, only under the operator opt-in
        if report.get('status') not in ('PASS', 'PASS_RESIDUAL_RISK') and not waived:
            return False, 'permission probe status is not PASS'
        if report.get('reviewer_flags_digest') != self.reviewer_flags_digest():
            return False, 'permission probe reviewer flags do not match this run'
        if report.get('author_flags_digest') != self.author_flags_digest():
            return False, 'permission probe author flags do not match this run'
        author_probe = report.get('author_permission_probe', {})
        report_mode = self.state['config'].get('review_report')
        expected_author_status = ('NOT-APPLICABLE' if report_mode else
                                  report['status'] if self.args.author_vendor == 'codex' else 'PASS')
        if not report_mode and self.args.author_vendor == 'claude' and not self._claude_probe_rules_match(author_probe):
            return False, 'permission probe Claude author rules do not match this run'
        if (not waived and author_probe.get('status') != expected_author_status) or (report['status'] == 'PASS_RESIDUAL_RISK' and (author_probe.get('d1a_model_verdict'), author_probe.get('d1b_synthetic_verdict')) != ('UNKNOWN', 'PASS')):
            return False, 'permission probe author permission status is not current'
        if report.get('global_config_changes', {}).get('status') != 'PASS':
            return False, 'permission probe global config changes were not fully attributed'
        fake = lifecycle_spine.fake_dispatch_guard(self.args)
        if 'gate_flags_digest' in report or not fake:   # G-a K4: a pre-G-a report has no gate digest and does not pass on the real CLI
            if 'gate_flags_digest' not in report: return False, 'permission probe report has no gate_flags_digest (made before the gate probe); run permission-probe again'
            if report['gate_flags_digest'] != self.gate_flags_digest(): return False, 'permission probe gate flags do not match this run'
        if self.args.gate_vendor != self.args.reviewer_vendor and not fake:
            gate_probe = report.get('gate_permission_probe')
            if not isinstance(gate_probe, dict) or gate_probe.get('status') not in ('PASS', 'PASS_RESIDUAL_RISK'): return False, 'permission probe has no passing gate probe for gate vendor ' + self.args.gate_vendor
        return True, ''

    def _gate_probe_covered(self) -> bool:   # RF-1: a gate vendor other than the reviewer's is covered only by a passing gate probe bound to the current gate flags digest
        if self.args.gate_vendor == self.args.reviewer_vendor or lifecycle_spine.fake_dispatch_guard(self.args): return True
        try:
            report = json.loads((self.run_dir / 'permission-probe.json').read_bytes())
            return report.get('gate_flags_digest') == self.gate_flags_digest() and report['gate_permission_probe']['status'] in ('PASS', 'PASS_RESIDUAL_RISK')
        except (OSError, ValueError, KeyError, TypeError, AttributeError): return False

    def _probe_skip_accepted(self) -> bool:   # P0-4 V2: the flag is command-line only; the recorded acceptance is honoured until a digest changes or a probe re-runs
        if not self._gate_probe_covered(): return False   # RF-1: also for an acceptance saved earlier
        acc, seen = self.state.get('probe_skip_override') or {}, {'reviewer_flags_digest': self.reviewer_flags_digest(), 'author_flags_digest': self.author_flags_digest(), 'gate_flags_digest': self.gate_flags_digest()}
        if acc and not acc.get('voided') and any(acc.get(k) != v for k, v in seen.items()):
            acc['voided'] = {'time': time.strftime(UTC_FORMAT, time.gmtime()), 'digests_seen': seen, **self._void_cause(acc, {   # ENV-NAME: say why
                'reviewer_flags_digest': self.reviewer_flags_digest, 'author_flags_digest': self.author_flags_digest, 'gate_flags_digest': self.gate_flags_digest})}; self.save()
        return acc.get('actor') == 'operator' and not acc.get('voided') and all(acc.get(k) == v for k, v in seen.items())

    def _probe_cache_key(self) -> Optional[tuple[str, dict]]:   # P0-4 V3: sha256 over the probe surface, both flag digests and the Claude CLI version
        root, ws = probe_cache_root().resolve(), self.workspace.resolve()
        if self._program_state()[1] or ws == root or root in ws.parents or ws in root.parents: return None   # a workspace role could write an overlapping cache
        versions = [claude_cli_version(self.state['operator_programs']['claude_bin']['path'])] if 'claude' in (self.args.author_vendor, self.args.reviewer_vendor, self.args.gate_vendor) else []
        if 'UNAVAILABLE' in versions: return None
        inputs = {'surface_version': PROBE_SURFACE_VERSION, 'reviewer_flags_digest': self.reviewer_flags_digest(), 'author_flags_digest': self.author_flags_digest(), 'gate_flags_digest': self.gate_flags_digest(), 'claude_versions': versions,
                  **({'review_report': True} if self.state['config'].get('review_report') else {})}   # LG2-a2: report and ordinary entries never share a key
        return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest(), inputs

    def _probe_cache_put(self, keyed: Optional[tuple[str, dict]], body: dict) -> None:   # the one private-dir + symlink-checked write path of an entry or a tombstone
        key, inputs, root = *(keyed or (None, None)), probe_cache_root()
        if key is None or any(p.is_symlink() for p in (root.parent.parent, root.parent, root)):
            raise RuntimeError('no cache key (Claude version or program state unavailable) or a symlinked cache path')
        os.makedirs(root, mode=0o700, exist_ok=True)
        if not stat.S_ISDIR((info := os.lstat(root)).st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022: raise RuntimeError('cache root is not a private directory')   # P0-4b H2
        (temp := root / f'{key}.json.tmp').unlink(missing_ok=True)
        os.close(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))   # atomic_json rewrites this file, which keeps 0600
        atomic_json(root / f'{key}.json', {'key': key, 'key_inputs': inputs, 'run_dir': str(self.run_dir), 'time': time.strftime(UTC_FORMAT, time.gmtime()), **body})

    def _probe_cache_write(self, report: dict, report_sha256: str) -> None:   # the caller turns any failure into a report warning, never a verdict change
        self._probe_cache_put(self._probe_cache_key(), {'source_report_sha256': report_sha256, 'report': report})

    def _probe_cache_void(self, report_sha256: str) -> str:   # F4: a non-PASS probe voids this key's entry (tombstone, else delete); returns a warning text, '' when nothing stays reusable
        if not _lstat_present(root := probe_cache_root()): return ''
        if (keyed := self._probe_cache_key()) is None: return f'WARNING: PROBE CACHE ENTRY MAY NOT BE VOIDED: no cache key for this probe (Claude version or program state unavailable), so an older PASS in {root} for this configuration was not looked up and can still be reused once the key is available again, until it is deleted by hand or is 7 days old; the verdict of this probe is unchanged'
        if not _lstat_present(path := root / f'{keyed[0]}.json'): return ''   # no cache dir: no key lookup (no claude --version) either
        try:
            if path.is_symlink(): raise RuntimeError('symlinked entry')
            return self._probe_cache_put(keyed, {'status': 'VOID', 'source_report_sha256': report_sha256}) or ''
        except Exception as exc: why = f'{type(exc).__name__}: {exc}'
        try:
            if any(p.is_symlink() for p in (root.parent.parent, root.parent, root)): raise RuntimeError('symlinked cache directory')
            path.unlink(); return f'probe cache: could not write a VOID tombstone ({why}); {path} was deleted instead'
        except Exception as exc:
            return f'WARNING: PROBE CACHE ENTRY NOT VOIDED: {path} still holds an older PASS for this key ({why}; delete failed: {type(exc).__name__}: {exc}); a run with the same key can still reuse it until it is deleted by hand or is 7 days old; the verdict of this probe is unchanged'

    def _probe_void_on_non_pass(self, report: dict) -> None:   # F4: the report and stdout carry the warning; never a verdict change, whatever fails here
        if lifecycle_spine.fake_dispatch_guard(self.args): return
        try: note = self._probe_cache_void(hashlib.sha256((json.dumps(report, indent=2, ensure_ascii=False) + '\n').encode()).hexdigest())
        except Exception as exc: note = f'WARNING: PROBE CACHE ENTRY MAY NOT BE VOIDED ({type(exc).__name__}: {exc}); an older PASS in {probe_cache_root()} can still be reused until it is deleted by hand or is 7 days old; the verdict of this probe is unchanged'
        if note: report['warning'] = (report.get('warning', '') + '; ' if report.get('warning') else '') + note; print(note)

    def _probe_cache_reuse(self) -> tuple[bool, str]:   # P0-4 V3: adopt an earlier PASS for this exact key after the checks; else name the failed one
        keyed, root = self._probe_cache_key(), probe_cache_root()
        if keyed is None: return False, 'probe cache: no key (Claude version or program state unavailable)'
        (key, inputs), path = keyed, root / f'{keyed[0]}.json'
        if any(p.is_symlink() for p in (root.parent.parent, root.parent, root, path)): return False, 'probe cache: symlinked entry or directory'
        try: raw = read_cache_entry(root, path.name)
        except OSError as exc: return False, 'probe cache: ' + ('no entry for these flags and versions' if exc.errno == errno.ENOENT else 'symlinked entry or directory' if exc.errno in (errno.ELOOP, errno.ENOTDIR) else exc.strerror)
        try: entry = json.loads(raw); void = isinstance(entry, dict) and entry.get('status') == 'VOID'; report = {} if void else entry['report']; age = time.time() - calendar.timegm(time.strptime(entry['time'], UTC_FORMAT)); json.dumps(entry, ensure_ascii=False).encode()   # the last: a lone surrogate would fail atomic_json's write
        except (ValueError, KeyError, TypeError, RecursionError): return False, 'probe cache: entry is unreadable'
        if not isinstance(report, dict): return False, 'probe cache: entry is malformed (report is not an object)'   # P0-4b H4
        if entry.get('key') != key or entry.get('key_inputs') != inputs: return False, 'probe cache: recorded key inputs differ'
        if not 0 <= age <= 7 * 86400: return False, 'probe cache: entry is older than 7 days'
        if 'permission_probe_superseded' in self.state and not self.state.get('permission_probe'): return False, 'probe cache: the last permission-probe did not complete'
        if (target := self.run_dir / 'permission-probe.json').exists(): return False, 'probe cache: this run already has a report that does not pass'
        if self.state.get('permission_probe'): return False, 'probe cache: this run has already probed; the cache never revives an older PASS'   # P0-4b H3
        if void: return False, f'probe cache: voided by a later non-PASS probe ({entry["time"]}, {entry.get("run_dir")})'   # F4
        prior = self.state.get('permission_probe')
        atomic_json(target, {**report, 'reused_from': {'cache_path': str(path), 'cache_sha256': hashlib.sha256(raw).hexdigest(), 'source_run_dir': entry.get('run_dir'), 'original_time': entry['time'], 'reuse_time': time.strftime(UTC_FORMAT, time.gmtime())}})
        self.state['permission_probe'] = {'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'turn': report.get('probe_turn')}
        try: passed, why = self.probe_passed()
        except Exception as exc: passed, why = False, f'malformed report ({type(exc).__name__})'   # P0-4b H4: a wrongly typed field is a miss
        if passed: self.save(); print(f'probe reused from {path} (PASS of {entry["time"]} in {entry.get("run_dir")})'); return True, ''
        self.state['permission_probe'] = prior; target.unlink()      # not adopted
        return False, 'probe cache: entry does not pass for this run: ' + why

    def _probe_negative_status(self) -> str:   # P0-4b H1: status of this run's current, bound report when it is not a PASS, else ''
        try:
            raw = (self.run_dir / 'permission-probe.json').read_bytes(); report = json.loads(raw)
            same = lambda: (report.get('reviewer_flags_digest') == self.reviewer_flags_digest() and report.get('author_flags_digest') == self.author_flags_digest()
                            and report.get('gate_flags_digest', self.gate_flags_digest()) == self.gate_flags_digest())
            current = (self.state.get('permission_probe') == {'sha256': hashlib.sha256(raw).hexdigest(), 'turn': report.get('probe_turn')}
                       and (same() or ((names := self._probe_report_env_names()) is not None and self._under_env_names(names, same))))   # ENV-NAME: FIELD-10's comparison
            return str(report.get('status')) if current and report.get('status') != 'PASS' and not self._author_probe_waived(report) else ''   # HL-FIX: the opted-in author part is not negative evidence
        except (OSError, ValueError, AttributeError): return ''

    def probe_gate(self) -> tuple[bool, str]:   # run/resume/reject: a passing report, an accepted skip or a verified cache reuse (noted on stdout)
        passed, reason = self.probe_passed()
        if passed: return True, ''
        if self._probe_skip_accepted(): print('probe skipped by operator acceptance (--accept-probe-skip)'); return True, ''
        if (acc := self.state.get('probe_skip_override') or {}).get('voided'):   # ENV-NAME: a voided acceptance is reported, not silent
            reason += f'; the earlier --accept-probe-skip is void ({self._void_text(acc)}; pass --accept-probe-skip --reason TEXT again or run permission-probe)'
        if lifecycle_spine.fake_dispatch_guard(self.args): return False, reason       # the fake harness never touches the real cache
        reused, note = self._probe_cache_reuse()
        return (True, '') if reused else (False, reason + '; ' + note)

    def _validate_resume_args(self) -> None:
        if Path(self.state['workspace']) != self.workspace or Path(self.state['workitem']) != self.workitem:
            raise ValueError('resume workspace/workitem differs from state')
        if getattr(self.args, 'quality_writers_requested', None) not in (None, self.args.quality_writers):   # D09 §4
            raise ValueError('resume configuration differs: quality_writers')
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
        self._restore_role_policy(getattr(self.args, 'explicit_role_flags', ROLE_DESTS))
        validate_role_models(self.args)
        if getattr(self.args, 'wi_deadline', None) is None:   # F2: the deadline is fixed at run; resume keeps it, a different value is refused below
            self.args.wi_deadline = self.state['config'].get('wi_deadline')
        elif 'wi_deadline' not in self.state['config']:
            raise ValueError('--wi-deadline is fixed at run start (the run or permission-probe that creates the run directory); '
                             'this run directory was created without one')
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
                raise ValueError(('role models are fixed for this run: ' if key in ROLE_DESTS else
                                  'resume configuration differs: ') + key)

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
        proc = candidate_tree.run_bounded(candidate_tree.git_command('rev-parse', '--verify', 'HEAD', cwd=self.workspace), cwd=self.workspace,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def _head_ref(self) -> Optional[str]:   # the branch HEAD names, None when detached
        return self._git(['symbolic-ref', '-q', 'HEAD'], ok=(0, 1)).strip() or None

    def _index_digest(self, tree: Optional[str] = None) -> str:
        """The index entries (`ls-files -s`; a stat refresh changes nothing), or those `read-tree <tree>` would give."""
        index = self.internal / f'index-digest-{uuid.uuid4().hex[:8]}'
        env = {**candidate_tree.git_env(), **({'GIT_INDEX_FILE': str(index)} if tree else {})}
        try:
            if tree: subprocess.run(candidate_tree.git_command('read-tree', tree, cwd=self.workspace), cwd=self.workspace,
                                    env=env, check=True, capture_output=True, timeout=600)
            return hashlib.sha256(subprocess.run(candidate_tree.git_command('ls-files', '-s', '-z', cwd=self.workspace),
                                                 cwd=self.workspace, env=env, check=True, capture_output=True,
                                                 timeout=600).stdout).hexdigest()
        finally:
            index.unlink(missing_ok=True)

    def _git(self, args: list[str], ok=(0,)) -> str:
        proc = candidate_tree.run_bounded(candidate_tree.git_command(*args, cwd=self.workspace), cwd=self.workspace, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, errors='replace')
        if proc.returncode not in ok:
            raise RuntimeError('git ' + ' '.join(args) + ' failed: ' + proc.stderr.strip())
        return proc.stdout

    def _git_names(self, args: list[str]) -> list[str]:   # LG1-e: -z output read as bytes, so a non-UTF-8 name survives
        raw = candidate_tree.run_bounded(candidate_tree.git_command(*args, cwd=self.workspace), cwd=self.workspace,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        return [name for name in raw.decode(errors='surrogateescape').split('\0') if name]

    def _ignored_paths(self, workspace: Path, names: list) -> set:   # F3: which of these paths git ignores (tracked ones never)
        proc = candidate_tree.run_bounded(candidate_tree.git_command('check-ignore', '-z', '--stdin', cwd=workspace), cwd=workspace,
                                          input=b''.join(os.fsencode(name) + b'\0' for name in names), timeout=10,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode not in (0, 1):
            raise RuntimeError('git check-ignore failed: ' + proc.stderr.decode(errors='replace').strip()[:200])
        return {os.fsdecode(name) for name in proc.stdout.split(b'\0') if name}

    def _record_ignored_config(self, receipt: dict, before: dict, after: dict) -> None:
        """F3: report (never hold) the ignored executable-config files a writer turn created or changed."""
        if notes := [note for note in (before.get('note'), after.get('note')) if note]:
            receipt['ignored_config_note'] = '; '.join(notes)
        if 'failed' in (before.get('note') or ''):
            return   # nothing to compare against: the note says so
        if written := ignored_config.written(before, after):
            receipt['ignored_config_written'] = written
            self.state['ignored_config_written'] = sorted({*self.state.get('ignored_config_written', []), *written})
            print('WARNING: the author wrote ignored executable-config files that no reviewer sees: ' + ', '.join(written)
                  + '; inspect them before an editor, MCP client or shell runs them')

    def _workspace_names(self) -> list[str]:
        raw = candidate_tree.run_bounded(candidate_tree.git_command('ls-files', '-co', '--exclude-standard', '-z', cwd=self.workspace),
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
        tracked = self._git(['diff', *candidate_tree.NO_EXT_DIFF, '--binary', base, '--'] if base else ['diff', *candidate_tree.NO_EXT_DIFF, '--binary', '--'])
        stat = self._git(['diff', *candidate_tree.NO_EXT_DIFF, *STAT_FULL_PATHS, base, '--'] if base else
                         ['diff', *candidate_tree.NO_EXT_DIFF, *STAT_FULL_PATHS, '--'])
        untracked = self._git(['ls-files', '--others', '--exclude-standard']).splitlines()
        additions = []
        for name in untracked:
            additions.append(self._git(['diff', *candidate_tree.NO_EXT_DIFF, '--no-index', '--binary', '--', '/dev/null', name], ok=(0, 1)))
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
            proc = candidate_tree.run_bounded(candidate_tree.git_command('diff', *candidate_tree.NO_EXT_DIFF, '--no-index', '--binary', '--', str(baseline), str(current), cwd=self.workspace),
                                  cwd=self.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')   # v297-cwd: git >= 2.40 refuses --attr-source outside a repo
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
        rows = [finding for finding in self.open_findings()
                if finding['severity'] in BLOCKING_REVIEW_SEVERITIES or
                finding.get('security') or
                (finding['source'] == 'adversarial-gate' and finding['severity'] in ('CRITICAL', 'HIGH'))]
        if worktree_lifecycle.is_worktree(self.state) and self.state['lifecycle'].get('stage') not in ('SECURITY', 'DONE'):
            rows = [row for row in rows if row.get('owner_role') != 'security-reviewer']   # only SECURITY can close them
        return rows

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
            if source != 'adversarial-gate' and (same := self._open_duplicate(source, finding)):   # FIELD-25: a re-report
                same['status_history'].append({'round': origin_round, 'status': same['status'],
                                               'evidence': f're-reported by {source} in {phase}; not a new finding'})
                recorded.append({'id': same['id'], **finding})
                changed = True
                continue
            finding_id = f"F{self.state['next_finding_id']:03d}"
            self.state['next_finding_id'] += 1
            severity = str(finding['severity']).upper()
            summary = finding.get('summary') or finding.get('recommendation') or str(finding.get('body', '')).splitlines()[0]
            if (label := CLASS_LABEL_RE.match(str(finding.get('body', '')))) and not CLASS_LABEL_RE.match(summary):
                summary = label.group(0).strip() + ' ' + summary   # FIELD-5: a gate's class label stays visible in the ledger summary
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
            self._progress_finding(entry)
            recorded.append({'id': finding_id, **finding})
            changed = True
        if changed:
            self.write_ledger()
        return recorded

    def _open_duplicate(self, source: str, finding: dict) -> Optional[dict]:
        """FIELD-25: an open finding of the same owner, file, severity and security flag that this one re-reports: its
        summary opens with that id ("F003 (still open) ...", a class label, case and spacing aside). Equal claims from
        two reviewers stay two rows (two Q reviewer proofs raise the same advisory)."""
        owner = source if source.startswith('specialist:') or source == 'security-reviewer' else None
        claim = ' '.join(CLASS_LABEL_RE.sub('', str(finding.get('summary') or ''), count=1).lower().split())
        return next((row for row in self.open_findings()
                     if claim.startswith(row['id'].lower()) and not claim[len(row['id']):][:1].isalnum() and
                     row.get('owner_role') == owner and row['file'] == finding.get('file', '') and
                     row['severity'] == str(finding['severity']).upper() and bool(row.get('security')) == bool(finding.get('security'))), None)

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
                self._progress_finding(finding)
            changed = True
        if changed:
            self.write_ledger()
        return []

    _progress_seq_n, _progress_label, _progress_warned = None, None, False

    def progress(self, kind: str, **fields) -> None:
        """PL: one event as a stdout line and a RUN/progress.jsonl row. Callers pass ids, counts and ledger summaries only; this never raises and never touches state."""
        try:
            if not kind.startswith('probe'): fields.setdefault('label', self._progress_label)
            row = {'ts': time.strftime(UTC_FORMAT, time.gmtime()), 'kind': kind, **fields}
            fd = os.open(self.run_dir / 'progress.jsonl', os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), 0o600)
            with os.fdopen(fd, 'r+b') as log:
                if not stat.S_ISREG(os.fstat(fd).st_mode): raise OSError('not a regular file')
                if self._progress_seq_n is None: self._progress_seq_n = log.read().count(b'\n')
                self._progress_seq_n += 1
                row['seq'] = self._progress_seq_n
                log.write((json.dumps(row, ensure_ascii=False) + '\n').encode())
            if not getattr(self.args, 'quiet_progress', False): sys.stdout.write(progress_line(row) + '\n'); sys.stdout.flush()   # not print(): the final status line stays the only print of a run
        except Exception as exc:
            if not self._progress_warned:
                self._progress_warned = True
                try: sys.stdout.write('WARNING: progress log unavailable: ' + type(exc).__name__ + '\n'); sys.stdout.flush()
                except Exception: pass   # a closed stdout must not turn a progress failure into a run failure

    def _progress_phase(self, role: str, phase: str) -> None:
        rounds = self.state.get(f'{phase.lower()}_rounds', 0) + (role == 'author')
        label = 'gate' if role == 'gate' else 'polish' if phase == 'POLISH' else f'{phase} r{rounds}'
        if phase == 'PLAN' and role == 'author' and (self.state.get('plan_history_rewrite') or {}).get('status') == 'requested':
            label = 'PLAN rewrite'   # FIELD-11b: the extra turn is no PLAN round
        if label != self._progress_label:
            self._progress_label = label
            self.progress('phase')

    def _progress_finding(self, row: dict) -> None:
        self.progress('finding', id=row['id'], severity=row['severity'], source=row['source'], security=bool(row.get('security')),
                      status=row['status'], summary=' '.join(str(row['summary']).split())[:120])

    def _progress_open(self) -> list[str]:
        return [f"{row['id']} {row['severity']}" + ('+sec' if row.get('security') else '') for row in self.open_findings()]

    def _progress_terminal(self, status: str, reason: str = '') -> None:
        reason = ' '.join(reason.split())
        if reason.startswith(('implementer: ', 'polish implementer: ')): reason = reason.split(':')[0] + ': (author text withheld)'
        self.progress('terminal', status=status, reason=reason[:120], invocations_used=self.state['invocations_used'], invocation_cap=self.args.max_invocations)

    def _progress_dispatch(self, role: str, phase: str, fresh: bool, call):
        kind = {'probe': 'probe:reviewer', 'gate-probe': 'probe:gate'}.get(role) or ('probe:author-escape' if phase == 'AUTHOR_PERMISSION_PROBE' else 'dispatch')
        if kind == 'dispatch': self._progress_phase(role, phase)
        model, effort = self._model_effort(role)
        who = dict(role=role, vendor=self._role_vendor(role), model=model, effort=effort, fresh=fresh)
        self.progress(kind, step='start', **who)
        started, outcome = time.time(), 'error'
        try:
            result = call()
            outcome = 'ok'
            return result
        except BaseException:
            last = (self.state['turns'] or [{}])[-1]
            if last.get('sequence') == self.state.get('sequence'):
                outcome = 'timeout' if last.get('timed_out') else 'rate-limited' if last.get('error_kind') else 'error'
            if self.state.get('active'): outcome = 'uncertain'
            raise
        finally:
            self.progress(kind, step='end', seconds=round(time.time() - started), outcome=outcome, **who)

    def refused(self, message: str) -> int:
        self._progress_terminal('REFUSED', message)
        print('REFUSED: ' + message)
        return 2

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

    def record_review_verdict(self, sequence: int, phase: str, raw: str, effective: str, rf5=False) -> None:
        self.progress('verdict', verdict=effective, raw=raw if raw != effective else '', rf5_converted=rf5, open=self._progress_open())
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
        if rows := (self.state.get('acceptance') or {}).get('uncommitted'):   # FIELD-25
            lines.append(f'Uncommitted after acceptance ({uncommitted_cause(self.state)}): ' + ', '.join(rows))
        if self.state.get('residual_risk'): lines.append('Residual risk: ' + self.state['residual_risk'])
        if self.state.get('pending_operator_note_id'):
            lines.append('Operator note: undelivered ' + self.state['pending_operator_note_id'])
        if self.state.get('hold_reason'):
            lines.append('Hold reason: ' + self.state['hold_reason'])
        atomic_text(self.run_dir / 'review-comparison.md', '\n'.join(lines) + '\n')

    def _publication_guard(self):
        journal_path = self.evidence / 'delivery-publication.json'
        intent = self.state.get('fake_delivery_intent') or {}
        digest = intent.get('digest')
        complete = bool(digest) and self.state.get('publication_complete') == digest
        if journal_path.exists():
            journal = json.loads(journal_path.read_text())
            lock = Path(journal.get('lock', {}).get('path', str(self.workspace / '.git/index.lock')))
            complete = complete and journal.get('phase') == 'RECONCILED' and not (lock.exists() or lock.is_symlink())
        if self.state.get('publication_hold') or (journal_path.exists() and not complete):
            raise ValueError('publication incomplete; use locked publication recovery before operator commands')

    def hold(self, reason: str, terminal_kind: Optional[str] = None, rate_limited: bool = False) -> str:
        self._publication_guard()
        if self.state.get('status') in ('ACCEPTED', 'ABORTED', 'CLOSED', 'REPORTED'):
            return self.state['status']
        if reason == 'rejected-tree':
            author = next((r for r in reversed(self.state['turns']) if r.get('role') == 'author'), {})
            self.state['rejected_tree_hold'] = {'tree_sha256': git_snapshot(self.workspace)[0],
                'rationale': str(author.get('answer', {}).get('body', ''))[:2000],
                'rationale_evidence': {'run_dir': str(self.run_dir), 'turn_sequence': author.get('sequence')}}
            self.state.update(next='author', pending_author_result_sequence=None, pending_reviewer_result_sequence=None)
            if worktree_lifecycle.is_worktree(self.state):   # W accepts no held tree (no --override-rejection)
                reason += ('; post-DONE rejection limit reached; note and resume, reject --scope-change, or abort'
                           if terminal_kind == 'rejection_limit' else '; note, change the workspace, or abort')
            else:
                reason += '; note, change the workspace, or accept --override-rejection --reason TEXT'
                if terminal_kind == 'rejection_limit': reason += '; post-DONE rejection limit reached; accept or abort'
        keep_rejection_limit = (self.state.get('status') == 'HOLD' and
                                self.state.get('terminal_hold_kind') == 'rejection_limit')
        self.set_effective_verdict('HOLD')
        if self.state.get('active'):
            self._void_opv_unknown_turn(self.state['active'])
            self.state['uncertain_active'] = self.state['active']
        self.state['status'] = 'HOLD'
        self.state['hold_reason'] = reason
        last = next((row for row in reversed(self.state.get('turns', [])) if row.get('error_kind') == 'rate_limited'), {})
        if rate_limited and last:   # ratelimit: the drive passes it for a RateLimitedTurn only
            self.state['hold_kind'] = 'rate_limited'
            self.state['rate_limit'] = {key: last.get(key) for key in ('role', 'phase', 'sequence', 'reset_hint')}
        else:
            self.state.pop('hold_kind', None); self.state.pop('rate_limit', None)
        if (self.state.get('config') or {}).get('review_report'):
            self._report_mark(False, reason)
            self._write_review_report()
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
        self._progress_terminal('HOLD', reason)
        return 'HOLD'

    def _wi_deadline_issue(self, record=True) -> Optional[str]:   # F2: checked before every dispatch; a running turn is never cut
        limit = self.state['config'].get('wi_deadline')
        if not limit: return None
        now, seen = time.time(), self.state.get('wi_clock', 0)
        if now + 60 < seen:   # never a negative elapsed: no refund after the clock moves back
            return (f'whole-WI deadline: the wall clock moved back {seen - now:.0f}s since the last dispatch '
                    '(check the clock; dispatches work again once it is past the last dispatch time)')
        clock = max(now, seen)
        if record: self.state['wi_clock'] = clock   # a dispatch saves it; an operator-action check must not change the state its intent digest covers
        elapsed = clock - self.state['started_at']
        if elapsed >= limit:
            return (f'whole-WI deadline reached: {elapsed:.0f}s since the run started (--wi-deadline {limit}s); '
                    'no new turn was started and resume is refused; abort, or note --scope-change to start a successor '
                    'with its own --wi-deadline')
        return None

    def _refuse_past_deadline(self, action: str) -> None:   # F2: a deadline blocks new dispatches only; it never rewrites a DONE or a HOLD
        if issue := self._wi_deadline_issue(record=False):
            kept = ('the DONE tree stays acceptable: accept it, abort, or reject --scope-change' if self.state.get('status') == 'DONE'
                    else 'nothing changed: the HOLD keeps its reason, so its accept/override, note and abort paths stay as they are'
                    if self.state.get('status') == 'HOLD' else 'nothing changed; abort, or note --scope-change to start a successor')
            raise ValueError(f'{action} refused: {issue.split(";")[0]}; {kept}')

    def round_limit_hold(self, reason: str) -> str:   # RLO: the held tree, so accept --override-rejection can rule on exactly it
        self.state['round_limit_hold'] = {'hold_reason': reason, 'phase': self.state['phase'], 'tree_sha256': git_snapshot(self.workspace)[0],
                                          'time': datetime.now().astimezone().isoformat()}
        return self.hold(reason)

    def _override_tree(self) -> Optional[str]:   # the tree accept --override-rejection may rule on; None for any other HOLD cause
        if self.state.get('hold_reason', '').startswith('rejected-tree'):
            return self.state.get('rejected_tree_hold', {}).get('tree_sha256')
        held = self.state.get('round_limit_hold') or {}
        if held.get('hold_reason') in ROUND_LIMIT_REASONS and held['hold_reason'] == self.state.get('hold_reason') and not self.rejected_tree(held['tree_sha256']):
            return held['tree_sha256']
        return None

    def rejection_limit_hold(self) -> str:
        maximum = self.state.get('max_rejections', DEFAULT_MAX_REJECTIONS)
        return self.hold('rejected-tree', terminal_kind='rejection_limit') if self.rejected_tree() else 'HOLD'

    def scope_change(self, text: Optional[str], file: Optional[str]) -> str:
        self._refuse_report_feedback()
        self._publication_guard()
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
        atomic_json(config_path, {k: v for k, v in {'quality_writers': 'off', **self._saved_config()}.items()   # D09: a run saved
                    if k in CONFIGURABLE_DESTS and v is not None and   # before the key is off, and so is its successor
                    not (k == 'gate_prompt' and str(v).startswith('<bundled-default>:')) and
                    not (k == 'auto_commit' and self.state['config'].get('auto_commit_source'))})   # FIELD-25 gate: re-derived
        spec = {'run_dir': str(target), 'workspace': str(self.workspace), 'original_workitem': str(self.workitem),
                'original_hash': hashlib.sha256(self.workitem.read_bytes()).hexdigest(),
                'task_sha256': hashlib.sha256(task.encode()).hexdigest(), 'base_commit': self.state['base_commit'],
                'task': task, 'note_sha256': intent['sha256'],
                'config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
                'item_uuid': self.state['item_uuid'],
                'item_blockers': pending_item_blockers(self.state, self.run_dir),
                'item_blockers_complete': bool(self.state.get('item_blockers_complete'))}
        spec['test_command_explicit'] = bool(self.state.get('test_command_explicit'))   # D09 §3: the successor config names it
        if marker := (self.state.get('lifecycle') or {}).get('quality_writers'):   # D09 §3: one pass per item, like item_blockers
            spec['quality_writers'] = marker
        if self.state.get('review_only'):   # LG1-a2: the successor keeps the entry and base and freezes its own scope
            spec['review_only'] = {'review_base': self.state['config']['review_base'],
                                   'review_scope_sha256': self.state['review_only']['review_scope_sha256']}
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
        self._refuse_report_feedback()
        self._publication_guard()
        if self.state['status'] != 'HOLD':
            raise ValueError('note requires a HOLD run; DONE uses reject')
        if self.state.get('terminal_hold_kind') == 'rejection_limit' and not self.state.get('rejected_tree_hold'):
            raise ValueError('rejection limit: accept, abort or use --scope-change')
        waiting = self.state.get('next')
        # FIELD-23: an EXEC HOLD that waits for the reviewer (or its shadow) takes a note too, so an operator change made in
        # that HOLD is on record; it reaches the next EXEC author turn (a REVISE), never a review role.
        if waiting == 'reviewer' and self.state['phase'] == 'PLAN':
            raise ValueError('run is waiting for reviewer in PLAN, and an approval moves it to EXEC, where a PLAN note is never '
                             'delivered. To put an operator change on record: resume with --stop-after-plan and add the note '
                             'at the EXEC HOLD, or note --scope-change (abort; the successor run carries the note)')
        if waiting not in ('author', 'reviewer'):
            raise ValueError(f'run is waiting for {waiting}; no author turn is due, so a note would not be delivered. To put '
                             'an operator change on record: note --scope-change (abort; the successor run carries the note); '
                             'resume only if the change needs no record')
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
               'replaces_sha256': previous['sha256'] if previous else None,
               **({'while_next': waiting} if waiting != 'author' else {})}
        notes.append(row); self.state['pending_operator_note_id'] = note_id
        self.state.pop('terminal_hold_kind', None)
        self.save(); self.write_comparison()
        return note_id

    def accept(self) -> str:
        self._refuse_report_accept()
        self._publication_guard()
        if (self.state.get('status') != 'ACCEPTED' and not self.args.override_rejection   # the override already refuses these in operator_intent
                and (turn := self.state.get('active') or self.state.get('uncertain_active'))):   # ACCEPT-ACTIVE: legacy and W
            fix = ('permission-probe' if turn.get('phase') in ('PROBE', 'AUTHOR_PERMISSION_PROBE') else 'resume') + ' --retry-uncertain'   # resume returns early on DONE
            raise ValueError(f"accept refused: CLI turn {turn.get('sequence', '?')} is active or uncertain; settle it first "
                             f'({fix} once its process group is gone) or abort')
        if worktree_lifecycle.is_worktree(self.state):   # W3b DELIVERY; no accept may skip FINISH-SECURITY
            return self._worktree_accept()
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
        if self.args.override_rejection and self.state.get('hold_reason') in ROUND_LIMIT_REASONS:   # RLO: the owner accepts the held tree with these findings open
            record.update(rationale=None, round_limit_hold=self.state['round_limit_hold'],
                          open_findings=[{'id': row['id'], 'severity': row['severity'], 'source': row.get('source'), 'security': bool(row.get('security')),   # RLO LOW-1
                                          'summary': ' '.join(str(row.get('summary', '')).split())[:200]} for row in self.open_findings()])
        if (verified := opv.current_for_acceptance(self, record['intent']['tree_sha256'], atomic_json)):   # N4-e: operator evidence still valid for this tree
            record['operator_verifications'] = verified
        if self.state.get('review_only'): record['uncommitted'] = self._uncommitted_files()   # FIELD-25: no commit here
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
        self._progress_terminal('ACCEPTED')
        return 'ACCEPTED'

    def _worktree_accept(self) -> str:
        """W3b DELIVERY (doc 6, D-1): the operator accepts a W DONE with --expect over an intent that also binds the
        stage receipts. auto_commit (frozen; default false, true for review-only) decides whether a local commit is made; external
        delivery is refused (D8), and no held tree is accepted (no --override-rejection)."""
        if self.state.get('status') == 'ACCEPTED':
            return 'ACCEPTED'
        if self.args.override_rejection:   # RLO would accept a tree that skipped FINISH..SECURITY, blockers included
            raise ValueError('worktree lifecycle refuses accept --override-rejection: a held tree has not passed '
                             'FINISH..SECURITY and open specialist or security blockers are never accepted; abort or '
                             'start a successor')
        config, life = self.state['config'], self.state['lifecycle']
        if config.get('external_delivery'):
            raise ValueError('worktree lifecycle refuses external delivery (push, PR, merge; D8); set external_delivery '
                             'false in the operator profile')
        journal_path = self.evidence / 'delivery-commit.json'
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else None
        receipts = hashlib.sha256(json.dumps(life['receipts'], sort_keys=True).encode()).hexdigest()
        if journal and self.args.expect == journal['intent']['digest'] and life.get('stage') == 'DONE' and (
                self.state.get('status') in ('DONE', 'HOLD')) and (   # never ABORTED: a superseded run stays superseded
                journal['intent'].get('receipts_sha256') == receipts) and not self.state.get('scope_change_intent'):
            intent = journal['intent']   # a replay of a journaled commit (HEAD may already be the commit)
            if git_snapshot(self.workspace)[0] != intent['tree_sha256']:
                raise ValueError('the tree changed since the journaled accept; restore it or abort')
        else:
            if self.state.get('status') != 'DONE' or life.get('stage') != 'DONE':
                raise ValueError('worktree lifecycle accept requires status DONE and stage DONE')
            if (head := self._head_commit()) != life['parent']:   # W writers never move HEAD; someone else did
                life.update(stage='SECURITY', candidate_oid=self.state['approved_snapshot'])
                self.state['next'] = 'security'
                return self.hold(f'HEAD moved since the run started ({life["parent"][:12]} -> {str(head)[:12]}); restore '
                                 'HEAD and the approved tree and resume (SECURITY runs again), or abort')
            intent = self.operator_intent('accept', None, None, self.args.expect, True)
            journal = None
        untracked = self._git_names(['ls-files', '-z', '--others', '--exclude-standard']) if config.get('auto_commit') else []
        try:
            delivery = (self._worktree_commit(intent, journal) if config.get('auto_commit') else
                        {'auto_commit': False, 'commit': None, 'head': life['parent'], 'external_delivery': False})
            if delivery.get('commit') and untracked:   # FIELD-25 gate MINOR: name the untracked files the commit took in
                delivery['committed_untracked'] = [review_scope_path(name) for name in untracked]
        except WorktreeDeliveryHold as exc:   # resume is refused; only accept with the journaled digest finishes it
            self.state['delivery_pending'] = intent['digest']
            return self.hold(f"{exc}; accept --expect {intent['digest']}")
        if record := self.state.get('review_only'):   # LG1-c: the commits since the base that were reviewed with the change
            span = self.state['config']['review_base'] + '..' + record['head_at_start']
            delivery['reviewed_commits'] = self._git(['rev-list', '--reverse', span]).split()
        record = {'author': 'operator', 'timestamp': datetime.now().astimezone().isoformat(), 'intent': intent,
                  'accepted_state': 'DONE', 'acceptance_state': 'ACCEPTED', 'reason': self.args.reason,
                  'override_rejection': False, 'delivery': delivery}
        if (verified := opv.current_for_acceptance(self, intent['tree_sha256'], atomic_json)):
            record['operator_verifications'] = verified
        if self.state.get('review_only') and not delivery.get('commit'): record['uncommitted'] = self._uncommitted_files()   # FIELD-25
        evidence_path = self.evidence / 'acceptance.json'
        record['evidence'] = str(evidence_path)
        atomic_json(evidence_path, record)
        self.state.setdefault('events', []).append(record)
        self.state.update(acceptance=record, acceptance_state='ACCEPTED', status='ACCEPTED', accepted_at=record['timestamp'])
        self.state.pop('delivery_pending', None)
        atomic_text(self.run_dir / 'delivery-report.md', worktree_lifecycle.delivery_report(
            self.state, self.run_dir.name, self.workitem.read_text(), delivery))
        self.save()
        self.write_comparison()
        self._progress_terminal('ACCEPTED')
        return 'ACCEPTED'

    def _uncommitted_files(self) -> list[str]:
        """FIELD-25: what an accept without a commit leaves for the operator: paths changed against HEAD, then untracked ones."""
        return [*map(review_scope_path, self._git_names(['diff', '--name-only', '--no-renames', '-z', 'HEAD', '--'])),
                *('untracked: ' + review_scope_path(name) for name in self._git_names(['ls-files', '-z', '--others', '--exclude-standard']))]

    def _worktree_run_cap(self, stage: str) -> int:
        """A run-wide stage budget; each W reject reruns FINISH..SECURITY, so it adds one more allowance."""
        return budget_policy.BUDGET_CAPS[stage][0] * (1 + len(self.state.get('rejections', [])))

    def _commit_refusals(self, paths: list[str]) -> None:
        """W04 parity before an auto_commit: no work staged before the run, no content-transforming attribute or
        filter, no core.autocrlf, so the commit holds exactly the accepted bytes and checks out as them."""
        if record := self.state.get('review_only'):   # LG1-c: the index may hold the reviewed change, fully staged at start
            if hashlib.sha256(self._git(['ls-files', '-s', '-z']).encode('utf-8', 'surrogateescape')).hexdigest() != record['index_at_start']:
                raise ValueError('auto_commit refuses an index changed since the review-only run started; restore it or abort')
        elif staged := self._git(['diff-index', '--cached', '--name-only', 'HEAD']).split():
            raise ValueError('auto_commit refuses work staged before the run: ' + ', '.join(staged[:10]))
        if self._git(['config', '--get', 'core.autocrlf'], ok=(0, 1)).strip().lower() not in ('', 'false', 'no', 'off', '0'):
            raise ValueError('auto_commit refuses core.autocrlf; unset it or accept with auto_commit false')
        if self._git(['config', '--bool', '--get', 'core.fileMode'], ok=(0, 1)).strip() == 'false':   # no reliable mode bits
            raise ValueError('auto_commit refuses core.fileMode false; accept with auto_commit false')
        if any(row.startswith('160000 ') for row in self._git(['ls-files', '-s', '-z']).split('\0')):
            raise ValueError('auto_commit refuses a repository with submodules; accept with auto_commit false')
        if any(row.startswith('S ') for row in self._git(['ls-files', '-t', '-z']).split('\0')):
            raise ValueError('auto_commit refuses skip-worktree (sparse) entries; accept with auto_commit false')
        proc = candidate_tree.run_bounded(['git', *candidate_tree.GIT_NO_EXEC, 'check-attr', '-z', '--stdin', 'filter',
                                           'text', 'eol', 'working-tree-encoding'], cwd=self.workspace,
                                          input='\0'.join(paths) + '\0', stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        fields = proc.stdout.split('\0')
        transforming = [f'{path}: {attr}={value}' for path, attr, value in zip(fields[0::3], fields[1::3], fields[2::3])
                        if value not in ('unspecified', 'unset')]
        if proc.returncode or transforming:
            raise ValueError('auto_commit refuses content-transforming attributes: ' +
                             ', '.join(transforming[:10] or [proc.stderr.strip()[-200:]]))

    def _manifest_tree(self, snapshot: list) -> str:
        """The git tree of exactly the accepted manifest, written through a private index (raw bytes, no filters)."""
        files = [(path, value) for path, value in snapshot if value != 'missing']
        if any('\n' in path or path.startswith('"') or path.endswith('\r') for path, _ in files):
            raise ValueError('auto_commit refuses a path that --stdin-paths would rewrite (newline, leading quote, trailing CR)')
        regular = [path for path, value in files if not value.startswith('link:')]
        oids = dict(zip(regular, candidate_tree.run_bounded(
            candidate_tree.git_command('hash-object', '-w', '--no-filters', '--stdin-paths', cwd=self.workspace),
            cwd=self.workspace, input=''.join(path + '\n' for path in regular), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=True, timeout=600).stdout.split()))
        for path, value in files:   # --stdin-paths follows symlinks: a link blob is its target text
            if value.startswith('link:'):
                oids[path] = candidate_tree.run_bounded(
                    candidate_tree.git_command('hash-object', '-w', '--stdin', cwd=self.workspace), cwd=self.workspace,
                    input=value[len('link:'):], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
                    timeout=600).stdout.strip()
        rows = ''.join(f"{'120000' if value.startswith('link:') else '100755' if value.startswith('exec:') else '100644'}"
                       f' {oids[path]}\t{path}\0' for path, value in files)   # rel210-fixA: the accepted mode, not a live lstat
        index = self.internal / f'delivery-index-{uuid.uuid4().hex[:8]}'
        env = {**candidate_tree.git_env(), 'GIT_INDEX_FILE': str(index)}
        try:
            subprocess.run(candidate_tree.git_command('update-index', '-z', '--index-info', cwd=self.workspace),
                           cwd=self.workspace, env=env, input=rows, text=True, check=True, capture_output=True, timeout=600)
            return subprocess.run(candidate_tree.git_command('write-tree', cwd=self.workspace), cwd=self.workspace, env=env,
                                  text=True, check=True, capture_output=True, timeout=600).stdout.strip()
        finally:
            index.unlink(missing_ok=True)

    def _worktree_commit(self, intent: dict, journal: Optional[dict]) -> dict:
        """D-1 auto_commit: one hook-free local commit (git_command disables hooks) of exactly the accepted manifest,
        CAS on HEAD and an index sync. A journal written before the ref moves makes a replay finish the same commit."""
        parent = self.state['lifecycle']['parent']
        if journal is None:
            try:
                self._commit_refusals([path for path, value in intent['tree_snapshot'] if value != 'missing'])
                tree = self._manifest_tree(intent['tree_snapshot'])
            except ValueError as exc:   # FIELD-25 gate: a defaulted auto_commit delivers uncommitted; an explicit one refuses
                if self.state['config'].get('auto_commit_source') != 'review-only-default':
                    raise
                return {'auto_commit': False, 'commit': None, 'head': parent, 'external_delivery': False, 'commit_skipped': str(exc)}
            if git_snapshot(self.workspace)[0] != intent['tree_sha256']:
                raise ValueError('the tree changed during the accept; restore the approved tree or abort')
            title = next((line.lstrip('# ').strip() for line in self.workitem.read_text().splitlines() if line.strip()),
                         'paired-session work item')
            message = (f'{title}\n\npaired-session worktree lifecycle\nRun: {self.run_dir.name}\n'
                       f"Item: {self.state['item_uuid']}\nAccept intent: {intent['digest']}\n"
                       + (f"Review base: {self.state['config']['review_base']}\n" if self.state.get('review_only') else ''))
            commit = self._git(['commit-tree', '--no-gpg-sign', tree, '-p', parent, '-m', message]).strip()
            journal = {'intent': intent, 'parent': parent, 'tree': tree, 'commit': commit, 'ref': intent.get('head_ref'),
                       'index_before': self._index_digest(), 'index_target': self._index_digest(commit)}
            atomic_json(self.evidence / 'delivery-commit.json', journal)
        if 'index_before' not in journal:
            raise WorktreeDeliveryHold('auto_commit: the journal predates the index and branch binding; abort')
        if (now := self._head_ref()) != journal['ref']:   # rel210-fixA: never another branch
            raise WorktreeDeliveryHold(f"auto_commit: HEAD is {now or 'detached'}, not {journal['ref'] or 'detached'} as at "
                                       'the journaled accept; switch back to it, or abort')
        target = journal['ref'] or 'HEAD'
        if (head := self._git(['rev-parse', '--verify', '-q', target], ok=(0, 1)).strip() or None) not in (
                journal['parent'], journal['commit']):
            raise WorktreeDeliveryHold(f"auto_commit: HEAD is {str(head)[:12]}, neither the parent {journal['parent'][:12]} "
                                       f"nor the commit {journal['commit'][:12]}; restore it, or abort")
        if self._index_digest() not in (journal['index_before'], journal['index_target']):   # rel210-fixA: only this delivery's index
            raise WorktreeDeliveryHold('auto_commit: the index holds staged changes that are neither the state before this '
                                       'delivery nor its commit; unstage them (git reset -q), or abort')
        if head == journal['parent']:
            try:   # compare-and-swap on the bound ref: only from the parent the commit was built on
                self._git(['update-ref', '-m', 'paired-session accept ' + self.run_dir.name,
                           *([] if journal['ref'] else ['--no-deref']), target, journal['commit'], journal['parent']])
            except RuntimeError as exc:
                raise WorktreeDeliveryHold(f'auto_commit: {target} could not move from {journal["parent"][:12]} ({exc}); '
                                           'restore it, or abort') from exc
        self._git(['read-tree', journal['commit']])   # the index follows the new HEAD
        self._git(['update-index', '-q', '--refresh'], ok=(0, 1))
        return {'auto_commit': True, 'commit': journal['commit'], 'head': journal['parent'], 'tree': journal['tree'],
                'external_delivery': False}

    def _inherited_security_baseline(self, parent: Path) -> dict:
        """Supervisor decision (W3a): a scope-change successor inherits its parent's delivery baseline, so the delivery
        scope stays relative to the tree before the work item; the copy is bound to the parent's digest."""
        base = (json.loads((parent / 'state.json').read_text()).get('lifecycle') or {}).get('security_baseline')
        if not base or Path(base['path']).parent != parent / 'evidence':
            raise ValueError('a worktree-lifecycle successor inherits its parent delivery baseline, and the parent has '
                             'none; start a new run instead')
        try:
            data = Path(base['path']).read_bytes()
        except OSError as exc:
            raise ValueError(f'cannot read the parent delivery baseline: {exc}') from exc
        if hashlib.sha256(data).hexdigest() != base['sha256']:
            raise ValueError('the parent delivery baseline changed; start a new run instead')
        path = self.evidence / f'delivery-baseline-{uuid.uuid4().hex[:8]}.json'
        path.write_bytes(data)
        return {'path': str(path), 'sha256': base['sha256'], 'inherited_from': str(parent)}

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
        if worktree_lifecycle.is_worktree(self.state):   # W3b: the intent names the stage receipts it accepts
            data['receipts_sha256'] = hashlib.sha256(json.dumps(self.state['lifecycle']['receipts'], sort_keys=True).encode()).hexdigest()
            data['head_ref'] = self._head_ref()   # rel210-fixA: the branch an auto_commit moves (None: detached HEAD)
        data['digest'] = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        override = self.args.override_rejection
        if override and (action != 'accept' or not (self.args.reason or '').strip() or
                self.state['status'] != 'HOLD' or self.state.get('active') or self.state.get('uncertain_active') or
                tree_sha != self._override_tree()):
            raise ValueError('override requires HOLD rejected-tree or a round-limit HOLD, unchanged held tree, and non-empty --reason')
        if not override: self.refuse_rejected_tree(stale_done=True)
        if required and not expected:   # FIELD-25: name the full next command, digest filled in
            raise ValueError(f'intent is stale or missing: {action} needs --expect with the digest of {action} --intent-only; '
                             f'to {action} the run as it is now, run: {self.next_intent_command(data["digest"], action)}')
        if required and expected != data['digest']: raise ValueError('intent is stale or missing')
        return {**data, 'tree_snapshot': tree_snapshot}
    def next_intent_command(self, digest: str, action: str) -> str:
        raw = getattr(self.args, 'raw_argv', None) or [action, '--workspace', str(self.workspace), '--workitem',
                                                        str(self.workitem), '--run-dir', str(self.run_dir)]
        return next_intent_command(raw, digest)
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
        self._refuse_report_feedback()
        self._publication_guard()
        if self._fake_lifecycle and self.state.get('fake_delivery_intent'):
            raise ValueError('lifecycle reject not wired; abort/new run or use --scope-change')
        if self.state.get('status') != 'DONE':
            raise ValueError('reject requires a DONE run')
        self._refuse_past_deadline('reject')
        if worktree_lifecycle.is_worktree(self.state) and self.state['lifecycle'].get('stage') != 'DONE':
            raise ValueError('worktree lifecycle reject requires stage DONE')
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
        if worktree_lifecycle.is_worktree(self.state):   # W3b: a reject reopens EXEC, then FINISH..SECURITY again
            life = self.state['lifecycle']
            self.state['lifecycle'] = {**life, 'stage': 'EXEC', 'epoch': life['epoch'] + 1, 'candidate_oid': None}
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

    def _recovery_config_issue(self, snapshot: dict, consume=True) -> Optional[str]:
        """F7: the first dispatch baseline after an uncertain-turn recovery must still show the global-config hashes the
        recovery validated. The hashes stay pending until a dispatch baseline matches them (consume) or a change is
        reported once (the operator inspects, then resumes on a fresh baseline); an earlier check only peeks."""
        pending = self.state.get('recovery_config_hashes') or {}
        changed = sorted(key for key, value in pending.items() if snapshot.get(key, {}).get('sha256') != value)
        if changed or consume:
            self.state.pop('recovery_config_hashes', None)
        return ('global config changed during uncertain-turn recovery (' + ', '.join(changed) + '); inspect, then resume'
                if changed else None)

    def archive_abandoned_turn(self, receipt: dict) -> dict:
        """Archive a stopped uncertain turn; returns the global-config hashes it validated (F7: resume re-checks them right
        before any dispatch, so a change after this check is not silently taken into the next turn's baseline)."""
        verified = {}
        scratch = (receipt.get('environment_overrides') or {}).get('TMPDIR')
        if receipt.get('role') in READONLY_SCRATCH_ROLES and scratch and Path(scratch).parent == self.run_dir / 'role-tmp':
            self._drop_scratch(Path(scratch))   # b295-f1: the uncertain turn's child is stopped before it is archived
        if receipt.get('vendor') == 'claude' and receipt.get('global_claude_before'):
            current = global_config_snapshot(self.global_config_home, self.global_codex_home)
            if receipt.get('global_config_home') != str(self.global_config_home): self.hold('global Claude config home changed during uncertain turn'); raise RuntimeError('global Claude config home changed during uncertain turn')
            changed = {key: current.get(key, {}).get('sha256') for key, value in receipt['global_claude_before'].items() if current.get(key, {}).get('sha256') != value}
            if changed:
                pending = {'sequence': receipt['sequence'], 'after': changed}
                if self.args.action == 'resume' and self.state.get('claude_uncertain_config_change') == pending:
                    receipt['global_claude_ack'] = {'operator_uid': os.getuid(), 'timestamp': datetime.now().astimezone().isoformat(), 'before': receipt['global_claude_before'], 'after': changed}; self.state.pop('claude_uncertain_config_change', None)
                else: self.state['claude_uncertain_config_change'] = pending; self.hold('global Claude config changed during uncertain turn; inspect, then resume --retry-uncertain'); raise RuntimeError('global Claude config changed during uncertain turn; inspect, then resume --retry-uncertain')
            verified.update({key: current.get(key, {}).get('sha256') for key in receipt['global_claude_before']})
        if receipt.get('vendor') == 'codex' and receipt.get('global_codex_before'):
            current = global_config_snapshot(self.global_config_home, self.global_codex_home)
            verified.update({key: current.get(key, {}).get('sha256') for key in receipt['global_codex_before']})
            if (receipt.get('global_codex_home') != str(self.global_codex_home) or receipt.get('global_config_home') != str(self.global_config_home) or any(current.get(key, {}).get('sha256') != value for key, value in receipt['global_codex_before'].items())):
                try: raw = (self.global_codex_home / 'config.toml').read_bytes()   # F7: one read for the hash and the trust check
                except OSError: raw = None
                allowed = (self.args.action == 'resume' and self.args.acknowledge_codex_trust == self.run_dir.name
                           and raw is not None and hashlib.sha256(raw).hexdigest() == current['codex_config']['sha256']
                           and current['codex_config']['sha256'] != receipt['global_codex_before']['codex_config']
                           and all(current.get(key, {}).get('sha256') == value for key, value in receipt['global_codex_before'].items() if key != 'codex_config')
                           and receipt.get('global_codex_home') == str(self.global_codex_home)
                           and trust_entry_only_since_hash(self.global_codex_home / 'config.toml', receipt['global_codex_before']['codex_config'], Path(receipt['workspace']), raw,
                                                           extra=list(concurrent_run_workspaces(receipt['workspace'], live=False))))   # FIELD-22: the operator inspected it
                if not allowed: self.hold('global Codex config changed during uncertain turn; inspect, then resume --acknowledge-codex-trust ' + self.run_dir.name); raise RuntimeError('global Codex config changed during uncertain turn; inspect, then resume --acknowledge-codex-trust ' + self.run_dir.name)
                self.state.setdefault('codex_trust_acknowledgments', []).append({'sequence': receipt['sequence'], 'operator_uid': os.getuid(), 'run_id': self.run_dir.name, 'timestamp': datetime.now().astimezone().isoformat(), 'workspace': receipt['workspace'], 'before': receipt['global_codex_before']['codex_config'], 'after': current['codex_config']['sha256']})
        sequence = receipt.get('sequence')
        rows = self.state.setdefault('abandoned_turns', [])
        if sequence is not None and any(row.get('sequence') == sequence for row in rows):
            return verified
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
        return verified

    def done(self, expected: Optional[str] = None, lifecycle_stage: Optional[str] = None) -> str:
        blocking = self.blocking_open_findings()
        if blocking:
            return self.hold('DONE refused with open blocking findings: ' +
                             ', '.join(row['id'] for row in blocking))
        snapshot, manifest = git_snapshot(self.workspace)
        if expected is not None and snapshot != expected:
            return self.hold('the tree changed before DONE; resume replays EXEC review and gate')
        self.state['approved_snapshot'], self.state['approved_manifest'] = snapshot, manifest
        if lifecycle_stage:   # W: the stage moves in the same save as the status
            self.state['lifecycle']['stage'] = lifecycle_stage
            self.state['next'] = 'done'
        self.set_effective_verdict('APPROVE')
        self.state['status'] = 'DONE'
        self.state['acceptance_state'] = 'PENDING'
        self.state['completed_at'] = time.time()
        self.save()
        self.write_ledger()
        self.write_comparison()
        self.write_open_findings()
        self.write_usage()
        self._progress_terminal('DONE')
        return 'DONE'

    def set_effective_verdict(self, verdict: str) -> None:
        if self.state.get('exec_comparisons'):
            self.state['exec_comparisons'][-1]['effective_verdict'] = verdict

    def start_polish_or_done(self, force=False) -> str:
        if self._fake_lifecycle:
            self._freeze_fake_exec_source()
            return self.hold('fake lifecycle has reviewed EXEC; router binding is pending')
        if worktree_lifecycle.is_worktree(self.state):   # EXEC converged: FINISH next, bound to the reviewed tree
            if blocking := self.blocking_open_findings():   # repair round: author fix, then reviewer and a new gate
                self.state['gate_ran'] = False
                if self.state['exec_rounds'] >= self.exec_round_limit():
                    return self.round_limit_hold('EXEC round limit reached')
                self.state.update(next='author', phase='EXEC',
                                  delivered_review=worktree_lifecycle.delivered_findings('convergence', blocking))
                self.save()
                return 'ACTIVE'
            reviewed = git_snapshot(self.workspace)[0]
            reviewer, gate = worktree_lifecycle.reviewed_turns(self.state['turns'])
            if {reviewer.get('snapshot_before'), gate.get('snapshot_before')} != {reviewed}:
                self.state.update(gate_ran=False, next='reviewer')   # resume re-reviews and re-gates this tree
                return self.hold('stale EXEC approval: the reviewer and the gate did not both review the current tree')
            self.set_effective_verdict('APPROVE')
            self.state['lifecycle'].update(stage='FINISH', candidate_oid=reviewed, exec_convergence={
                'tree': reviewed, 'reviewer_sequence': reviewer.get('sequence'), 'gate_sequence': gate.get('sequence')})
            self.state['next'] = 'finish'
            self.save()
            return 'ACTIVE'
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
        if self.state['config'].get('review_report'): raise ValueError('report mode refuses resume --polish')
        self._publication_guard()
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
        self._refuse_past_deadline('resume --polish')
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
        return self.args.gate_vendor

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

    def _claude_command(self, role: str, schema_path: Path, fresh: bool, workspace: Optional[Path] = None) -> list[str]:
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
            allow, deny = self._claude_author_edit_rules(workspace)
            allowed = 'Read,Grep,Glob,Bash' + (',Agent' if subagents else '') + ',' + ','.join(allow)
            cmd += ['--permission-mode', 'acceptEdits', '--tools', tools, '--allowedTools', allowed,
                    '--disallowedTools', 'NotebookEdit' if subagents else 'NotebookEdit,Agent']
            for rule in deny:  # separate arguments, as for the reviewer's Bash rules; the first --disallowedTools is unchanged
                cmd += ['--disallowedTools', rule]
        else:
            exact_commands = self.reviewer_commands()
            if role in ('probe', 'gate-probe') and self._probe_sandbox_commands:
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
               '-c', 'approval_policy="never"', '-c', 'features.hooks=false', *CODEX_PLUGINS_OFF]
        if role == 'author':
            cmd += self._author_sandbox_config_args()
        else:
            cmd += codex_readonly_profile_args()   # b295-f1: read-only except the dispatch's scratch $TMPDIR
        # Personal allow rules can bypass either sandbox, including an author's
        # worktree boundary. Every Codex role must use only this invocation's policy.
        cmd.append('--ignore-rules')
        if not fresh and self.state['started'][role]:
            cmd += ['resume', self.state['sessions'][role]]
        return cmd + ['-']

    def command(self, role: str, schema_path: Path, fresh: bool, workspace: Optional[Path] = None) -> list[str]:
        vendor = self._role_vendor(role)
        return (self._claude_command(role, schema_path, fresh, workspace) if vendor == 'claude'
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
            if (self.state.get('plan_history_rewrite') or {}).get('status') == 'requested':   # FIELD-11b
                task = ('Rewrite-only turn: return the approved plan as body with all review history removed (finding ids, '
                        'references to earlier reviews, response-to-reviewer narrative). Do not change the scope, the steps, or '
                        'the verification. This turn does not count as a PLAN round.')
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
        review_only = bool(self.state.get('review_only'))   # LG1-b: the existing change, never "the approved plan"
        task = ('Implement the approved plan now and run relevant checks. Summarize changes and checks in body.'
                if first and not review_only else
                'Fix every delivered blocking finding, rerun relevant checks, and summarize the result in body.')
        if self.args.exercise_revisions and first:
            task += ' Exercise rule: intentionally omit bool rejection required by the toy work item on this first implementation only.'
        prior = self.state.get('delivered_review', '')
        return '\n'.join([
            f'Role: persistent {self.args.author_vendor} implementer. Phase: EXEC.',
            f'Workspace: {self.workspace}', f'Work item: {self.context / "workitem.md"}', task,
            *([f'The change under review is the existing work in this workspace; its review scope (no plan was approved): '
               f'{self.context / "plan.md"}. Fix only what the delivered findings ask for.'] if review_only else []),
                contract,
                ('Delivered review:\n' + prior) if prior else 'No delivered review on this turn.',
            *([note] if worktree_lifecycle.is_worktree(self.state) and (note := self._docs_reserved_note()) else []),
            'Do not commit or push. Do not load review-loop skills. Do not edit outside the workspace.',
            'For long commands, use the longest single wait your tool permits. Do not wait for another model.',
            'Return only JSON matching the supplied schema. READY means this turn is complete; HOLD means blocked.',
        ])

    def _docs_allowlist(self) -> list[str]:
        return sorted(Path(path).relative_to(self.workspace).as_posix() for path in self.state['config']['docs_allowlist'])

    def _docs_owned(self) -> dict:   # DOCS's own writes plus (review-only, Q8) the docs the reviewed change already edits
        life = self.state['lifecycle']
        return {**dict.fromkeys(life.get('docs_pre_owned', []), 'pre-owned'), **life.get('docs_owned', {})}

    def _docs_reserved_note(self) -> str:
        pre = set(self.state['lifecycle'].get('docs_pre_owned', [])) - set(self.state['lifecycle'].get('docs_owned', {}))
        owned = {path for path in self._docs_owned() if path not in pre}
        reserved = [path for path in self._docs_allowlist() if path not in owned | pre]
        return ' '.join([*(['Reserved for the DOCS stage; do not edit: ' + ', '.join(reserved) + '.'] if reserved else []),
                         *(['Written by the DOCS stage; edit only to fix a delivered docs finding: ' +
                            ', '.join(sorted(owned)) + '.'] if owned else []),
                         *(['Part of the change under review; edit only to fix a delivered finding: ' +
                            ', '.join(sorted(pre)) + '.'] if pre else [])])

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
                          *(f'- {command}' for command in commands), LONG_COMMAND_RULE])

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

    def _reviewer_open_findings(self) -> list[dict]:
        """In a W run the persistent reviewer sees only findings it may dispose; owners close their own."""
        rows = self.open_findings()
        return [row for row in rows if not row.get('owner_role')] if worktree_lifecycle.is_worktree(self.state) else rows

    def open_findings_prompt(self) -> str:
        findings = self._reviewer_open_findings()
        if not findings:
            return 'Open finding ledger: none. Return an empty prior_findings array.\n' + APPROVE_CONVERSION_NOTE
        rows = '\n'.join(f"- {finding['id']}: {finding['summary']}" for finding in findings)
        return ('Open finding ledger (return exactly one prior_findings disposition for EVERY id: '
                'fixed, still_open, or withdrawn, with evidence):\n' + rows + '\n' + APPROVE_CONVERSION_NOTE)

    def _plan_ref(self, default: str) -> str:
        """LG1-b: how review roles are pointed at plan.md; a review-only run has a coordinator-written scope, no plan."""
        if not self.state.get('review_only'):
            return default
        return (f'Review scope (review-only entry; no plan was drafted or approved): {self.context / "plan.md"}\n'
                'Review the change itself: correctness, tests and safety, and its alignment with the goal when the work '
                'item states one.')

    def _change_noun(self, default: str = 'the uncommitted change in this worktree') -> str:   # LG1-b: commits may count
        if not self.state.get('review_only'):
            return default
        return f'the change in this worktree against the review base {self.state["config"]["review_base"][:12]} (commits since it included)'

    def _review_prompt(self, role: str, snapshot: str) -> str:
        base_phase = self.state['phase']
        phase = 'POLISH' if self.state['polish']['active'] else base_phase
        if role == 'shadow':
            return '\n'.join([
                f'Role: shadow, fresh isolated read-only whole-delta reviewer. Phase: {phase}.',
                f'Workspace: {self.workspace}', f'Work item: {self.context / "workitem.md"}',
                self._plan_ref(f'Approved/current plan: {self.context / "plan.md"}'),
                f'Program-materialized review files: {self.context / "delta.patch"}, delta.stat, status.txt, and when present delta-since-last-review.patch.',
                'Use only the work item, plan, delta files, and workspace. Review the complete current delta independently.',
                REVIEW_SEVERITY_GUIDANCE, CLASS_LABEL_GUIDANCE.format(field='summary'),
                self.inspection_prompt(role),
                self.verified_claims_prompt(),
                self.allowed_command_prompt(),
                self._test_instruction(),
                'Do not add cd, pipes, semicolons, &&, redirection, echo wrappers, or other text to that Bash call.',
                'Do not report exit codes; the coordinator reads tool results directly.',
                'APPROVE in EXEC requires non-empty self_run_evidence. Never edit files, commit, push, or load skills.',
                'Return only JSON matching the supplied schema.',
            ]) + opv.prompt_block(self, snapshot, atomic_json)
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
            rewrite = self.state.get('plan_history_rewrite') or {}
            if rewrite.get('status') == 'written':   # FIELD-11b: the reviewer decides whether the rewrite changed anything in substance
                approved = self.run_dir / f"plan-{rewrite['approved_plan_round']:02d}.md"
                exercise = (exercise + '\n' if exercise else '') + (
                    'Rewrite-only re-review: you approved the previous plan; this version is meant to differ only by the removal of '
                    'review history (finding ids, references to earlier reviews). APPROVE only if it is the same plan in substance; '
                    'return REVISE if the scope, steps, or verification changed.\n## Previously approved plan\n'
                    + (approved.read_text() if approved.is_file() else '(unavailable)'))
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
            self._plan_ref(f'Approved/current plan: {self.context / "plan.md"}'),
            f'Program-materialized review files: {self.context / "delta.patch"}, delta.stat, status.txt, and when present delta-since-last-review.patch.',
            self.inspection_prompt(role),
            REVIEW_SEVERITY_GUIDANCE, CLASS_LABEL_GUIDANCE.format(field='summary') + (CLASS_LABEL_REUSE if role == 'reviewer' else ''),
            self.verified_claims_prompt(),
            'Inspect the complete current delta, not only prior findings. Run relevant allowed checks yourself in EXEC.',
            self.allowed_command_prompt(), self.open_findings_prompt(),
            self._test_instruction(),
            'Do not add cd, pipes, semicolons, &&, redirection, echo wrappers, or other text to that Bash call.',
            'Do not report exit codes; the coordinator reads tool results directly.', exercise,
            'APPROVE in EXEC requires non-empty self_run_evidence. Never edit files, commit, push, or load skills.',
            'Return only JSON matching the schema. Use stable ids in prior_findings evidence where applicable.',
        ]) + opv.prompt_block(self, snapshot, atomic_json, role)

    def _gate_prompt(self, snapshot: str) -> str:
        template = Path(self.args.gate_prompt).read_text()
        filled = template.replace('${REVIEW_TARGET_DESC}', str(self.context / 'workitem.md')).replace(
            '${FOCUS_TEXT}', ('Review scope (review-only entry; no plan was approved): ' if self.state.get('review_only')
                              else 'Approved plan: ') + str(self.context / 'plan.md') +
            '\nAudit the complete current git delta in ' + str(self.workspace))
        return (filled + '\n' + self._test_instruction() +
                '\n' + self.allowed_command_prompt() +
                '\nRead program-materialized delta.patch, delta.stat, status.txt, and optional delta-since-last-review.patch in: ' + str(self.context) +
                '\n' + self.inspection_prompt('gate') +
                '\n' + self.verified_claims_prompt() +
                '\nDo not add cd, pipes, semicolons, &&, redirection, echo wrappers, or other text to that Bash call.' +
                '\nDo not report exit codes; the coordinator reads tool results directly.' +
                '\nReview independently using only the work item, plan, delta files, and workspace.' +
                '\n' + CLASS_LABEL_GUIDANCE.format(field='body') +
                '\nNever edit, commit, push, or load skills.' + opv.prompt_block(self, snapshot, atomic_json))

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

    def _history_markers(self, name: str, content: str) -> list[str]:
        """The review-history markers the fresh shadow/gate scan finds in one input, in scan order (as reported)."""
        return [shown for shown, _ in self._history_matches(name, content)]

    def _history_matches(self, name: str, content: str) -> list[tuple[str, str]]:
        """(reported marker, whole matched text) per review-history match; a vendor pattern reports its name group
        ("Claude") while the whole match ("Claude approved") is what a FIELD-23 exemption must find at the base."""
        def findall(pattern, text, flags=0):
            return [(match.group(1) if match.re.groups else match.group(0), match.group(0))
                    for match in re.finditer(pattern, text, flags)]
        # Role/rubric instructions in prompts/templates are not prior verdicts.
        matches = findall(FRESH_HISTORY_RE, content)
        # Mask only coordinator-owned absolute paths; require a token boundary after each path.
        prose = content
        # Support paths occur in generated prompts and diff headers, not user plan prose.
        include_support = name in ('prompt', 'gate-template') or name.endswith('.patch')
        known_path_patterns = [re.compile(r'(?<![\w/])' + re.escape(path) + r'(?!\w)')
                               for path in self._fresh_scan_run_paths(include_support)]
        for known_path in known_path_patterns:
            prose = known_path.sub('<run-path>', prose)
        # Unknown vendor-named directory paths remain subject to the scan.
        matches += findall(r'\b(Claude|Codex|Opus|Astra)\s+(?:approved|said|requested)\b', prose, re.I)
        matches += findall(r'(?<![\w/>])/(?:[\w.-]+/)*?[\w.-]*?(claude|codex|opus|astra)[\w.-]*/',
                           prose, re.I)
        # Keep attribution prose visible when a vendor-dot token resembles a filename.
        matches += findall(
            r'\b((?:Claude|Codex|Opus|Astra))\.(?:(?-i:[A-Z])[A-Za-z0-9]*|ai|app|com)\s+'
            r'(?:approved|requested|said|signed|reviewed|found|asked|rejected)\b', prose, re.I)
        matches += findall(r'\b(?:Per|By|From)\s+((?:Claude|Codex|Opus|Astra))\.'
                           r'(?:(?-i:[A-Z])[A-Za-z0-9]*|ai|app|com)\b', prose, re.I)
        prose = re.sub(r'''(?<![\w.-])[\w~./\\:-]+\.[A-Za-z][A-Za-z0-9]*(?=[:\s`\]\)>,.;!?'\"]|$)''',
                       '<path>', prose)
        matches += findall(r'\b(?:Claude|Codex|Opus|Astra|gpt-6-astra)\b', prose, re.I)
        if name not in ('prompt', 'gate-template'):
            matches += findall(r'\b(?:APPROVE|REVISE|needs-attention)\b', content)
        return matches

    # FIELD-23 prevents ACCIDENTAL review-history carry-over, not deliberate author evasion.
    # Independence otherwise rests on fresh sessions and unexempted prompt/gate templates.
    # Only added patch lines are scanned. A marker already in the base file is exempt anywhere
    # in that file (including copies): this is a known residual, not a positional provenance check.
    REPO_QUOTE_SOURCES = ('context/plan.md', 'context/workitem.md', 'original-workitem')

    def _base_blob(self, base: str, rel: str) -> Optional[str]:
        proc = candidate_tree.run_bounded(candidate_tree.git_command('cat-file', '-p', f'{base}:{rel}', cwd=self.workspace),
                                          cwd=self.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return proc.stdout.decode('utf-8', 'replace') if proc.returncode == 0 and b'\0' not in proc.stdout else None

    def _configured_command_texts(self) -> list[str]:
        """FIELD-29: the operator's configured commands (the test command, --reviewer-command and the work item's frozen
        reviewer-commands block), longest first. An absolute path in one may name a vendor or a review word."""
        frozen = ((getattr(self, 'state', None) or {}).get('config') or {}).get('workitem_reviewer_commands')
        if frozen is None:
            try: frozen = workitem_reviewer_commands(self.workitem.read_text())
            except (OSError, ValueError): frozen = []
        commands = {c.strip() for c in (self.args.test_command, *(self.args.reviewer_command or ()), *frozen) if c and c.strip()}
        return sorted(commands, key=len, reverse=True)

    def _fresh_history_text(self, name: str, content: str, base: Optional[str] = None) -> str:
        """One repository-text exemption helper for fresh scans, PLAN approval and review-only creation."""
        record = (getattr(self, 'state', None) or {}).get('review_only') or {}
        frozen_scope = name == 'context/plan.md' and hashlib.sha256(content.encode()).hexdigest() == record.get('review_scope_sha256')
        for command in self._configured_command_texts():   # FIELD-29: operator configuration, wherever it is quoted (prompts too)
            content = content.replace(command, '<configured-command>')
        base = base if base is not None else self.state.get('base_commit')
        if not base or name in ('prompt', 'gate-template'):
            return content
        if frozen_scope:
            content = mask_initial_change(content)   # FIELD-27: this run's frozen review scope (checked on the text as written)

        def repo_word(text):   # the same text as a whole word: "APPROVE" is not in "approved", "F042" not in "9af042bc"
            return re.compile(r'(?<![\w-])' + re.escape(text) + r'(?![\w-])', re.I)

        def mask(text, exists):
            # Replace an exempt whole match as a whole word; unrelated markers in the same quote/line remain visible, and
            # so does a non-exempt match that contains exempt text ("Claude approved" when only "Claude" is base text).
            wholes = {whole: exists(whole) for _, whole in self._history_matches(name, text)}
            kept = [whole.lower() for whole, exempt in wholes.items() if not exempt]
            for whole, exempt in wholes.items():
                if exempt and not any(whole.lower() in other for other in kept):
                    text = repo_word(whole).sub('<repo-text>', text)
            return text

        if name.endswith('.patch'):
            if content.strip() and not content.startswith('diff --git '):
                return content  # A non-patch input gets the old scan; it has no repository provenance.
            result = []
            renames = {}
            if name == 'context/delta-since-last-review.patch':
                fields = self._git_names(['diff', '--name-status', '-M', '-z', base, '--'])
                while fields:
                    status = fields.pop(0)
                    old_path = fields.pop(0)
                    if status.startswith(('R', 'C')):
                        new_path = fields.pop(0)
                        if status.startswith('R'): renames[new_path] = old_path
            mirrors = (str(self.internal / 'last-review'), str(self.internal / 'current-review'))
            for section in re.split(r'(?m)^(?=diff --git )', content):
                old = new = None
                header = section.split('\n@@', 1)[0]
                for line in header.splitlines():
                    if line.startswith(('--- ', '+++ ')):
                        raw = line[4:].rstrip('\t')
                        if raw.startswith('"') and raw.endswith('"'):
                            raw = self._git_unquote(raw[1:-1])
                        rel = None
                        if raw and raw != '/dev/null' and raw[:2] in ('a/', 'b/'):
                            rel = raw[2:]
                            if name == 'context/delta-since-last-review.patch':
                                rel = next((raw[1:][len(root) + 1:] for root in mirrors
                                            if raw[1:].startswith(root + '/')), None)
                        if line.startswith('--- '): old = rel
                        else: new = rel
                # A new file has no exemption, even if its post-image path happens to exist at base.
                # --no-index mirrors can render a run rename as a deletion plus an addition.
                old = renames.get(new, old)
                paths = (new, old) if 'rename from ' in header or new in renames else (new,)
                blobs = [self._base_blob(base, rel) for rel in dict.fromkeys(paths) if rel] if old and not (
                    'GIT binary patch' in section or 'Binary files ' in section) else []
                repository = '\n'.join([*(blob for blob in blobs if blob is not None), self._review_start_text(new)])
                for line in section[len(header):].splitlines():
                    if line.startswith('+'):
                        result.append(mask(line[1:], lambda whole: bool(repo_word(whole).search(repository))))
            return '\n'.join(result)

        if name in ('context/delta.stat', 'context/status.txt'):
            if not self._history_markers(name, content):
                return content
            proc = candidate_tree.run_bounded(candidate_tree.git_command('ls-tree', '-r', '--name-only', '-z', base,
                                                                         cwd=self.workspace),
                                              cwd=self.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode == 0:
                paths = set(proc.stdout.decode('utf-8', 'replace').split('\0')) - {''} | self._review_start_paths()   # FIELD-27
                def quoted(match):
                    return '<repo-path>' if self._git_unquote(match.group(0)[1:-1]) in paths else match.group(0)
                quote = r'"(?:[^"\\\n]|\\.)*"'
                content = re.sub(quote, quoted, content)
                marked = {path for path in paths if FRESH_HISTORY_RE.search(path) or
                          re.search(r'(?i:claude|codex|opus|astra)|\b(?:APPROVE|REVISE|needs-attention)\b', path)}
                for path in sorted(marked, key=len, reverse=True):   # only a path that can carry a marker needs masking
                    # Consume unknown quoted paths intact; a base filename prefix is not that path.
                    pattern = quote + r'|(?<![\w/.-])' + re.escape(path) + r'(?=[ \t]*(?:\||\(\d+ bytes\)|$)| -> )'
                    content = re.sub(pattern, lambda match: '<repo-path>' if match.group(0) == path
                                     else match.group(0), content, flags=re.M)
            return content

        if name in self.REPO_QUOTE_SOURCES:
            def exists(whole):   # git grep narrows the files; the blob check applies the word boundary (and a multi-line match)
                proc = candidate_tree.run_bounded(candidate_tree.git_command('grep', '-F', '-i', '-I', '-l', '-z', '-e', whole,
                                                                             base, '--', cwd=self.workspace),
                                                  cwd=self.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                return proc.returncode == 0 and any(
                    repo_word(whole).search(self._base_blob(base, path[len(base) + 1:]) or '')
                    for path in proc.stdout.decode('utf-8', 'replace').split('\0') if path)
            def inline(text):   # a code span may run over line ends within a paragraph, never over a blank line
                return re.sub(r'(`+)((?:(?!\n[ \t]*\n)[^`])*?)\1', lambda match: mask(match.group(0), exists), text)
            result, prose, block, fenced = [], [], [], None
            for line in content.splitlines(keepends=True):
                fence = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
                if fence and not fenced:
                    fenced = fence.group(1)
                    result.append(inline(''.join(prose)) + line)
                    prose = []
                elif fence and fenced and fence.group(1)[0] == fenced[0] and len(fence.group(1)) >= len(fenced) and not fence.group(2).strip():
                    result.append(mask(''.join(block), exists))
                    block, fenced = [], None
                    result.append(line)
                elif fenced:
                    block.append(line)
                else:
                    prose.append(line)
            return ''.join(result) + inline(''.join(prose)) + mask(''.join(block), exists)
        return content

    def _review_start_text(self, rel: Optional[str]) -> str:
        """FIELD-26: a review-only run's change as created is the user's code, not this run's review history: the file at
        `rel` in the creation mirror ('' for an ordinary run, a missing or binary file, or a symlink). Text a later fix
        round adds is not in it, so the scan still catches that."""
        mirror = self._review_start_root()
        if not rel or not mirror:
            return ''
        root, path = mirror.resolve(), mirror / rel
        try:
            if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
                return ''
            data = path.read_bytes()
        except OSError:
            return ''
        return '' if b'\0' in data else data.decode('utf-8', 'replace')

    def _review_start_root(self) -> Optional[Path]:
        """The review-only creation mirror whose content is user code (a scope-change successor keeps its parent's)."""
        record = self.state.get('review_only') or {}
        mirror = record.get('history_mirror') or record.get('mirror')
        return Path(mirror) if mirror else None

    def _review_start_paths(self) -> set:
        """FIELD-27: the paths in the creation mirror (empty for an ordinary run or a missing mirror)."""
        root = self._review_start_root()
        if root is None or not root.is_dir():
            return set()
        return {str((Path(top) / name).relative_to(root)) for top, dirs, files in os.walk(root) for name in (*files, *dirs)
                if (Path(top) / name).is_symlink() or name in files}

    def _introduced_history(self, name: str, content: str) -> list[str]:
        return self._history_markers(name, self._fresh_history_text(name, content))

    @staticmethod
    def _git_unquote(body: str) -> Optional[str]:
        """git's C-style path quoting (\\" \\\\ \\t \\n ... and \\ooo octal bytes) undone; None when malformed."""
        out, i, simple = bytearray(), 0, {'a': 7, 'b': 8, 'f': 12, 'n': 10, 'r': 13, 't': 9, 'v': 11, '"': 34, '\\': 92}
        while i < len(body):
            if body[i] != '\\':
                out += body[i].encode('utf-8'); i += 1
            elif body[i + 1:i + 2] and body[i + 1] in simple:
                out.append(simple[body[i + 1]]); i += 2
            elif re.fullmatch(r'[0-7]{3}', body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8)); i += 4
            else:
                return None
        try: return out.decode('utf-8')
        except UnicodeDecodeError: return None

    def _plan_history_issue(self, texts: Optional[dict] = None, base: Optional[str] = None) -> Optional[str]:
        """FIELD-11: what the fresh shadow/gate scan rejects in the work item or plan, found at PLAN approval (or, for a
        review-only run, which has no PLAN approval, on the texts it is about to write)."""
        for label, path in (('work item', self.context / 'workitem.md'), ('plan', self.context / 'plan.md')):
            text = texts[label] if texts else path.read_text() if path.is_file() else ''
            text = self._fresh_history_text('context/' + path.name, text, base)   # FIELD-23: as the gate scan
            if ids := sorted(set(LEDGER_ID_RE.findall(text))):
                return f'{label}: ledger-id-shaped tokens ' + ', '.join(ids)
            if match := FRESH_HISTORY_RE.search(text) or re.search(r'\b(?:APPROVE|REVISE|needs-attention)\b', text):   # as the gate scan
                return f'{label}: review-history wording {match.group(0)!r}'
        return None

    # --- D-LG1 review-only entry (docs/review-only-entry.md §1-§2), LG1-a1 ------------------------------------------------------
    def _commit_oid(self, ref: str) -> str:
        oid = None if ref.startswith('-') else self._git(['rev-parse', '--verify', '-q', ref + '^{commit}'], ok=(0, 1)).strip()
        if not oid:
            raise ValueError(f'--base {ref} does not name a commit')
        return oid

    def _parent_spec(self) -> dict:   # the scope-change parent's spec; its full validation comes later in __init__
        try: return json.loads((Path(self.args.supersedes).resolve() / 'evidence/successor-spec.json').read_text())
        except (OSError, ValueError): return {}

    def _review_report_args(self, saved: Optional[dict]) -> None:
        """LG2-a1: report mode is fixed at creation; omission on resume keeps the saved entry."""
        requested = getattr(self.args, 'review_report', None)
        self._pr_pins = None
        if saved is not None:
            if requested is not None and bool(requested) != bool(saved.get('review_report')):
                raise ValueError('resume configuration differs: review_report (fixed when the run was created)')
            self.args.review_report = bool(saved.get('review_report'))
            if saved_no_test(saved):   # LG2-b2: the frozen null wins; main() refuses an explicit --test-command
                self.args.test_command = None
            elif getattr(self.args, 'no_test_command', None) and self.args.review_report:
                raise ValueError('resume configuration differs: test_command (fixed when the run was created)')
        if not self.args.review_report:
            if getattr(self.args, 'review_aspects', None) is not None:
                raise ValueError('--aspects needs --review-report')
            if getattr(self.args, 'no_test_command', None):
                raise ValueError('--no-test-command needs --review-report')
            if getattr(self.args, 'review_pr_pins', None):
                raise ValueError('--review-pr-pins needs --review-report')
            return
        if saved is None and self.args.supersedes:
            raise ValueError('--review-report starts a fresh review request; it takes no --supersedes')
        if not (saved or {}).get('review_only', self.args.review_only):
            raise ValueError('--review-report needs --review-only')
        if self.args.auto_commit or (saved or {}).get('auto_commit'):
            raise ValueError('--review-report refuses --auto-commit true')
        if self.args.stop_after_plan:
            raise ValueError('--review-report refuses --stop-after-plan')
        if self.args.no_test_command and saved is None:   # LG2-b2: frozen as test_command null at creation
            self.args.test_command = None
            if self.args.reviewer_command:
                raise ValueError('--no-test-command refuses --reviewer-command, including a reviewer_command list from the '
                                 'workspace or operator paired-session.json (a no-test report run allows no command)')
            if workitem_reviewer_commands(self.workitem.read_text()):
                raise ValueError('--no-test-command refuses a work item that declares reviewer-commands')
        if (saved or {}).get('adversarial_gate', self.args.adversarial_gate) == 'off':
            raise ValueError('--review-report refuses --adversarial-gate off (the gate always runs in report mode)')
        if getattr(self.args, 'review_pr_pins', None):   # LG2-c: fixed at creation; a resume may repeat the same pins
            pins = review_pr_pins(self.args.review_pr_pins, self.workspace, self._head_commit(),
                                  saved.get('review_base') if saved is not None else self.args.review_base)
            if saved is not None and pins != self.state.get('review_pr'):
                raise ValueError('resume configuration differs: review_pr (fixed when the run was created)')
            self._pr_pins = pins
        if self.args.polish:
            raise ValueError('report mode refuses resume --polish')
        requested = self.args.review_aspects   # LG2-b1: the aspect subset, fixed at creation
        if requested is not None:
            aspects = [name.strip() for name in requested.split(',') if name.strip()] if isinstance(requested, str) else list(requested)
            if 'simplify' in aspects:   # LG2-c: a writer, dropped from the paired review-pr (review-pr-port.md §2.4, §5)
                raise ValueError('--aspects simplify is not part of a paired review-pr (simplify is a writer that edits the '
                                 'checkout); legacy review-pr still has it: /review-loop:review-pr --legacy simplify')
            if not aspects or any(name not in worktree_lifecycle.REPORT_ASPECTS for name in aspects):
                raise ValueError('--aspects takes a comma list of ' + ','.join(worktree_lifecycle.REPORT_ASPECTS))
            aspects = [name for name in worktree_lifecycle.REPORT_ASPECTS if name in aspects]
            if saved is not None and aspects != saved.get('review_aspects', list(worktree_lifecycle.REPORT_ASPECTS)):
                raise ValueError('resume configuration differs: review_aspects (fixed when the run was created)')
        self.args.review_aspects = (saved.get('review_aspects', list(worktree_lifecycle.REPORT_ASPECTS)) if saved is not None
                                    else aspects if requested is not None else list(worktree_lifecycle.REPORT_ASPECTS))

    def dispatched_vendors(self) -> tuple:
        """The vendors a run can dispatch; a report run never dispatches the author (LG2-a2)."""
        roles = (self.args.reviewer_vendor, self.args.gate_vendor)
        return roles if self.state['config'].get('review_report') else (self.args.author_vendor, *roles)

    def _test_instruction(self) -> str:
        """LG2-b2: the review prompt line for the configured test command, or the no-test line of a report run."""
        if self.args.test_command is None:
            return ('No test command is configured for this review: do not run tests; mark any claim that needs a test run '
                    'as unverified, and list the read commands you ran in self_run_evidence.')
        return f'Run this test command exactly as written in one Bash call: {self.args.test_command}'

    def _refuse_report_accept(self) -> None:
        if self.state['config'].get('review_report'):
            raise ValueError('report mode has nothing to accept: a report run ends at REPORTED')

    def _refuse_report_feedback(self) -> None:
        if self.state['config'].get('review_report'):
            raise ValueError('report mode refuses note and reject, including --scope-change; start a new review request')

    def _review_only_args(self, saved: Optional[dict], parent: Optional[dict] = None) -> None:
        """--review-only/--base: a new run resolves the base once; a later command keeps the frozen values unless it names
        them, and a different value is refused like any other configuration change. A scope-change successor (parent = its
        parent's spec) keeps the parent's entry and base from the spec, never from a profile (LG1-a2)."""
        args = self.args
        args.review_only, args.review_base_ref = getattr(args, 'review_only', None), getattr(args, 'review_base_ref', None)
        if saved is not None:
            if args.review_only is not None and bool(args.review_only) != bool(saved.get('review_only')):
                raise ValueError('resume configuration differs: review_only (fixed when the run was created)')
            if args.review_base_ref is not None and (not saved.get('review_only') or
                                                     self._commit_oid(args.review_base_ref) != saved['review_base']):
                raise ValueError('resume configuration differs: review_base (fixed when the run was created)')
            args.review_only, args.review_base = bool(saved.get('review_only')), saved.get('review_base')
        elif parent is not None:
            entry = parent.get('review_only') or {}
            if (args.review_only is not None and bool(args.review_only) != bool(entry)) or (
                    args.review_base_ref is not None and (not entry or self._commit_oid(args.review_base_ref) != entry['review_base'])):
                raise ValueError("a scope-change successor keeps its parent's entry and base; drop --review-only/--base")
            args.review_only, args.review_base = bool(entry), entry.get('review_base')
        elif args.review_only:
            args.review_base = self._commit_oid(args.review_base_ref or REVIEW_ONLY_DEFAULT_BASE)
        elif args.review_base_ref is not None:
            raise ValueError('--base needs --review-only')
        else:
            args.review_only, args.review_base = False, None
        args.auto_commit_source = (saved or {}).get('auto_commit_source')   # FIELD-25 gate: frozen with the value
        if getattr(args, 'auto_commit', None) is None:   # owner 2026-10-06: a review-only W run commits by default; an
            args.auto_commit = (bool(saved.get('auto_commit')) if saved is not None else   # explicit value (CLI, profile) wins
                                bool(args.review_only) and args.lifecycle_mode == 'on' and not getattr(args, 'review_report', None))
            if saved is None and args.auto_commit:
                args.auto_commit_source = 'review-only-default'   # an accept the commit checks refuse delivers uncommitted
        if saved is not None:   # D09 §4: frozen (a run saved before the key: off); only resume refuses a different
            args.quality_writers_requested = getattr(args, 'quality_writers', None)   # value (_validate_resume_args),
            args.quality_writers = saved.get('quality_writers') or 'off'   # operator actions keep the saved one
        elif getattr(args, 'quality_writers', None) is None:   # D09 §4 (owner 2026-10-06): both for review-only, else off
            args.quality_writers = 'both' if args.review_only else 'off'

    def _refuse_review_only_start(self) -> dict:
        """The refusals of a review-only run, before any state; returns the review scope to freeze."""
        args, base = self.args, self.args.review_base
        if args.stop_after_plan:
            raise ValueError('--review-only has no PLAN phase; --stop-after-plan does not apply')
        if args.lifecycle_mode == 'on' and not REVIEW_ONLY_LIFECYCLE_READY:
            raise ValueError('--review-only runs with --lifecycle-mode off until its base-tree delivery baseline lands (LG1-c)')
        head = self._head_commit()
        if head is None:
            raise ValueError('--review-only needs a commit to review against')
        if REVIEW_ONLY_BASE_MUST_BE_ANCESTOR and candidate_tree.run_bounded(
                candidate_tree.git_command('merge-base', '--is-ancestor', base, head, cwd=self.workspace), cwd=self.workspace).returncode:
            raise ValueError(f'--base {args.review_base_ref} is not an ancestor of HEAD; review-only reviews a change on top of its base')
        if self._git(['ls-files', '-u']).strip():
            raise ValueError('--review-only refuses an index with unmerged entries; finish or abort the merge first')
        staged = set(self._git_names(['diff', '--cached', '--name-only', '--no-renames', '-z', 'HEAD']))
        if partial := sorted(staged & set(self._git_names(['diff', '--name-only', '--no-renames', '-z']))):
            raise ValueError('--review-only refuses partially staged paths (an auto_commit would drop their staged version): ' +
                             ', '.join(json.dumps(name) if ',' in name else review_scope_path(name) for name in partial[:10]) +
                             '; stage them fully or unstage them')
        untracked = sorted(self._git_names(['ls-files', '-z', '--others', '--exclude-standard']))
        fields = self._git_names(['diff', '--name-status', '--no-renames', '-z', base, '--'])   # Q7: every full path
        changed = ''.join(f'{status}\t{review_scope_path(path)}\n' for status, path in zip(fields[0::2], fields[1::2]))
        if not changed and not untracked:
            raise ValueError(f'nothing to review: the workspace tree equals the review base {base[:12]}')
        workitem = self.workitem.read_text()
        scope = ('# Review scope (review-only entry)\n\nNo plan was drafted or approved; review the change itself: the '
                 'workspace tree against the review base.\n\n'
                 f'Review base: {base}\nHEAD at the start: {head}\nTest command: {args.test_command or "none (no tests run in this review)"}\n\n'
                 f'## Goal (the work item, verbatim)\n\n{workitem.rstrip()}\n\n'
                 + INITIAL_CHANGE_HEADING + changed + ''.join(f'untracked: {review_scope_path(name)}\n' for name in untracked))
        if issue := self._plan_history_issue({'work item': workitem, 'plan': mask_initial_change(scope)}, base):   # FIELD-11; FIELD-27
            issue = issue.replace('plan:', 'review scope (a changed path or the test command):', 1)   # the work item is checked first
            raise ValueError(f'--review-only refuses review history in the {issue}; the fresh shadow and gate would refuse it')
        return {'text': scope, 'head': head, 'untracked': untracked,
                'parent_scope_sha256': (self._parent_spec().get('review_only') or {}).get('review_scope_sha256') if args.supersedes else None}

    def _start_review_only(self, scope: dict) -> None:
        """Freeze the review-only start: the scope (as plan.md), the tree and index under review, a mirror of the tree, EXEC
        round 1 counted as the existing change, and the EXEC reviewer as the first dispatch."""
        atomic_text(self.context / 'plan.md', scope['text'])
        self._mirror_workspace(mirror := self.internal / 'review-start')
        index = hashlib.sha256(self._git(['ls-files', '-s', '-z']).encode('utf-8', 'surrogateescape')).hexdigest()
        self.state.update(phase='EXEC', next='reviewer', exec_rounds=REVIEW_ONLY_EXISTING_CHANGE_ROUNDS,
                          base_commit=self.args.review_base, review_only={
                              'plan_skipped': True, 'round1': 'the existing change; no author turn',
                              'head_at_start': scope['head'], 'candidate_tree_sha256': git_snapshot(self.workspace)[0],
                              'index_at_start': index, 'mirror': str(mirror),
                              'history_mirror': self._parent_history_mirror() or str(mirror),   # FIELD-27
                              'review_scope_sha256': hashlib.sha256(scope['text'].encode()).hexdigest(),
                              'untracked_at_start': scope['untracked'],   # FIELD-25: reviewed and delivered with the change
                              **({'parent_review_scope_sha256': scope['parent_scope_sha256']} if scope['parent_scope_sha256'] else {})})
        if names := [review_scope_path(name) for name in scope['untracked']]:
            self.progress('scope', untracked=f'{len(names)} untracked file(s) in the review scope: ' + ', '.join(names[:20])
                          + (f' (+{len(names) - 20} more; all in context/plan.md)' if len(names) > 20 else ''),
                          exclude='they are reviewed and delivered with the change; to leave one out, add it to '
                                  '.gitignore or remove it, then start a new run')
        if 'lifecycle' in self.state:   # LG1-a2: the W parent (HEAD-moved checks, auto_commit CAS) is HEAD, never the base
            self.state['lifecycle']['parent'] = scope['head']
            if REVIEW_ONLY_DOCS_PRE_OWNED and (pre := sorted(set(self._changed_paths(deleted=True)) & set(self._docs_allowlist()))):
                self.state['lifecycle']['docs_pre_owned'] = pre   # LG1-c, Q8

    def _parent_history_mirror(self) -> Optional[str]:
        """FIELD-27: a scope-change successor's user content is its parent's change as created, not the parent's fixes."""
        if not self.args.supersedes:
            return None
        try:
            record = json.loads((Path(self.args.supersedes).resolve() / 'state.json').read_text()).get('review_only') or {}
        except (OSError, ValueError, AttributeError):
            return None
        return record.get('history_mirror') or record.get('mirror')

    def _review_only_start_issue(self) -> Optional[str]:
        """Before the first EXEC review of a review-only run: the frozen scope and tree must be as created."""
        record = self.state.get('review_only')
        if not record or self.state.get('reviews_completed', 0):
            return None
        plan = self.context / 'plan.md'
        if not plan.is_file() or hashlib.sha256(plan.read_bytes()).hexdigest() != record['review_scope_sha256']:
            return 'the review scope (context/plan.md) changed before the first review; abort and start a new run'
        if git_snapshot(self.workspace)[0] != record['candidate_tree_sha256']:
            return (f'the tree changed before the first review; restore it from {record["mirror"]} (the tree as the run was '
                    'created) and resume, or abort')
        return None

    def assert_fresh_prompt(self, role: str, prompt: str) -> None:
        if role not in ('shadow', 'gate'):
            return
        # A clean prompt is insufficient when it points to contaminated inputs.
        # Do not rewrite past evidence or silently strip meaningful plan content.
        for name in ('workitem.md', 'plan.md'):
            path = self.context / name
            if path.is_file() and LEDGER_ID_RE.search(self._fresh_history_text(f'context/{name}', path.read_text())):
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
        checked = {}
        for name, content in sources.items():
            matches = self._introduced_history(name, content)   # FIELD-23: repository text is not review history
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

    def _scratch_root(self, role: str) -> Optional[Path]:
        """b295-f1 FIELD-1: a fresh 0700 temp root under run_dir/role-tmp for one Codex read-only dispatch; never the workspace, never the
        author's roots (workspace, author-tmp), never shared. Under the run lease one dispatch runs at a time, so a leftover is stale.
        b296-f1b: role-tmp must be a real 0700 directory we own (never a link), and everything is created and removed through it no-follow."""
        if role not in READONLY_SCRATCH_ROLES or self._role_vendor(role) != 'codex':
            return None
        base = self.run_dir / 'role-tmp'
        try: os.mkdir(base, 0o700)
        except FileExistsError: pass
        name = f"{self.state['sequence'] + 1:03d}-{role}"   # the sequence _invoke_once assigns
        with self._role_tmp_fd() as base_fd:
            for stale in os.listdir(base_fd):   # b296-f1c: a leftover is swept only once its turn's process group is confirmed gone
                try:
                    if not (match := re.fullmatch(r'(\d+)-[a-z-]+', stale)):
                        raise RuntimeError('its name carries no turn sequence')
                    self._stop_turn_group(int(match.group(1)), kill=False)   # R1: an old pid may be reused by now, so probe only, never kill
                except RuntimeError as exc:
                    reason = f'leftover read-only role scratch {base / stale} kept: {exc}; make sure no process of that turn is alive, remove it by hand, then resume'
                    self.hold(reason)
                    raise RuntimeError(reason) from exc
                self._drop_scratch(base / stale)
            os.mkdir(name, 0o700, dir_fd=base_fd)
            info = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError('the new read-only role scratch is not a directory we own: ' + str(base / name))
        return base / name

    def _stop_turn_group(self, sequence: int, kill: bool = True) -> None:
        """b296-f1b: no member of the read-only turn's CLI process group may outlive the turn into its scratch cleanup: kill what is left
        and wait (about 3 s) until killpg reports the group gone; otherwise refuse the cleanup (the scratch stays for the next sweep). A
        descendant that left the group (setsid, or a tool run in its own group) is not covered; the cleanup itself never follows a link,
        so it stays safe against one. Unlike resume / probe-retry (which HOLD on EPERM), EPERM here means gone: a group of our own uid
        answers EPERM on macOS only when the members left are unreaped zombies, which cannot act. kill=False (the sweep of a leftover from an
        earlier dispatch, whose pid may have been reused since) only probes and never signals: it returns only on ESRCH or a recorded
        never-started turn; no pid, EPERM or any other error is refused (b296-f1d)."""
        receipt = next((row for row in [*reversed(self.state['turns']), self.state.get('active') or {}, self.state.get('uncertain_active') or {}]
                        if row.get('sequence') == sequence), {})
        pid = receipt.get('pid')
        if not kill:   # b296-f1d: the sweep deletes only on an explicit ESRCH or a recorded never-started turn; any unknown state keeps it
            if type(pid) is not int and any(row.get('sequence') == sequence and row.get('child_created') is False
                                            for row in self.state.get('spawn_failures', [])):
                return   # _invoke_once recorded that the CLI process was never created
            if type(pid) is not int or pid <= 1:
                raise RuntimeError('no verifiable pid is recorded for that turn')
            try: retry_killpg_eperm(pid)   # a null-signal probe; no signal is ever sent on this path
            except ProcessLookupError: return
            except OSError as exc: raise RuntimeError(f'the process group of that turn cannot be verified gone ({exc}; its pid may be reused)') from exc
            raise RuntimeError('the process group of that turn still answers (alive, or its pid reused)')
        if type(pid) is not int or pid <= 1:
            return   # no child was started
        deadline = time.monotonic() + 3
        while True:
            try: retry_killpg_eperm(pid)
            except ProcessLookupError: return
            except PermissionError: return   # macOS: a group of our own uid left only with unreaped zombies answers EPERM; a zombie cannot act
            except OSError as exc: raise RuntimeError('cannot verify that the read-only role process group stopped; scratch kept') from exc
            if time.monotonic() > deadline:
                raise RuntimeError('the read-only role process group outlived its turn; scratch kept')
            try: os.killpg(pid, signal.SIGKILL)
            except OSError: pass
            time.sleep(0.05)

    @contextmanager
    def _role_tmp_fd(self):
        """b296-f1b: an O_NOFOLLOW handle on run_dir/role-tmp, refused unless it is a real directory owned by us with mode 0700."""
        base = self.run_dir / 'role-tmp'
        try: fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | getattr(os, 'O_NOFOLLOW', 0))
        except OSError as exc: raise RuntimeError(f'run_dir/role-tmp is not a real directory (a link or another type is refused): {exc}') from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise RuntimeError(f'run_dir/role-tmp must be a 0700 directory owned by this user (mode {oct(stat.S_IMODE(info.st_mode))}); inspect it')
            yield fd
        finally:
            os.close(fd)

    def _drop_scratch(self, root: Path) -> None:
        """Remove one scratch root fd-relative and never following a link: a link inside is removed as a link; a leftover in role-tmp
        that is not a real directory we own is refused (b296-f1b)."""
        if root.parent != self.run_dir / 'role-tmp':
            raise RuntimeError('refusing to remove a path outside role-tmp: ' + str(root))
        if not os.path.lexists(root.parent):
            return   # R1 LOW-1: no role-tmp, nothing to remove (a link or another type there is still refused below)
        try:
            with self._role_tmp_fd() as base_fd:
                try: info = os.stat(root.name, dir_fd=base_fd, follow_symlinks=False)
                except FileNotFoundError: return
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    raise RuntimeError(f'unexpected entry in run_dir/role-tmp, refused (never followed): {root.name}; inspect and remove it by hand')
                remove_tree_nofollow(base_fd, str(root.parent), root.name)
        except (OSError, ValueError, NotImplementedError) as exc:   # R1 LOW-3: a platform without a no-follow primitive also fails closed
            raise RuntimeError(f'cannot remove the read-only role scratch {root}: {exc}; remove it by hand, then resume') from exc

    def _void_opv_unknown_turn(self, turn: dict) -> None:
        """v2.9.7 OPV: an author turn in this workspace that became uncertain (cut off with the coordinator, e.g. a hard kill,
        found by resume, run, permission-probe, abort or any hold) left an unknown tree."""
        if turn.get('role') == 'author' and turn.get('workspace') == str(self.workspace):
            opv.void_stale(self, None, atomic_json)

    def _void_opv_observed(self, seq0: int, failed: bool = False) -> None:
        """OPV (v2.9.7): void on every tree the coordinator observed in the author turns after seq0 (start and end snapshot of
        each recorded turn; a missing end snapshot is an unknown tree), and on an unknown tree when a failed turn's child ran
        but left no recorded turn (an interrupted or uncertain dispatch)."""
        if not self.state.get('operator_verifications'): return
        turns = [t for t in self.state.get('turns', []) if t.get('sequence', 0) > seq0 and t.get('role') == 'author']
        for turn in turns:
            opv.void_stale(self, turn.get('snapshot_before'), atomic_json)
            opv.void_stale(self, turn.get('snapshot_after'), atomic_json)
        if failed and not turns and (self.state.get('active') or self.state.get('uncertain_active')):
            opv.void_stale(self, None, atomic_json)

    def invoke(self, role: str, phase: str, prompt: str, schema: dict, fresh=False,
               allow_mutation_report=False, workspace_override: Optional[Path] = None,
               env_overrides: Optional[dict] = None) -> dict:
        if role == 'author' and self.state['config'].get('review_report'):
            raise RuntimeError('report mode refuses every author dispatch')
        redispatched = False                                             # D-EFF category A: one re-dispatch per invoke
        for attempt in range(2):
            turn_prompt = prompt if attempt == 0 else (
                prompt + '\nEvidence contract retry: ' + self.verified_claims_prompt())
            seq0 = self.state['sequence']
            try:
                while True:   # a void read-only turn whose change was undone is re-dispatched once, told why
                    scratch = self._scratch_root(role)   # b295-f1: a fresh scratch root for each dispatch, re-dispatch included
                    overrides = ({**(env_overrides or {}), **{key: str(scratch) for key in ('TMPDIR', 'TMP', 'TEMP')}}
                                 if scratch else env_overrides)
                    try:
                        result = self._progress_dispatch(role, phase, fresh, lambda: self._invoke_once(
                            role, phase, turn_prompt, schema, fresh, allow_mutation_report, workspace_override, overrides))
                    except BaseException as exc:
                        if scratch:   # also after a failed turn, without hiding its error (a leftover is retried at the next dispatch)
                            try:
                                self._stop_turn_group(int(scratch.name.split('-', 1)[0]))
                                self._drop_scratch(scratch)
                            except RuntimeError: pass
                        if not (isinstance(exc, RuntimeError) and isinstance(exc.__cause__, ReadOnlyTurnVoided)
                                and exc.__cause__.restored): raise
                        if redispatched: raise RuntimeError(f'{role} mutated workspace again after one re-dispatch; both changes were '
                                                            f'restored ({exc})') from exc
                        self._redispatch_budget(phase, exc)
                        redispatched = True
                        turn_prompt += ('\n\nNote from the coordinator: your previous answer to this request changed the workspace. That '
                                        'answer was discarded and the workspace was restored. Review the current tree again without '
                                        'creating, editing, staging, committing or deleting anything.')
                        print(f'NOTE: {exc}; re-dispatching the {role} turn once')
                        continue
                    if scratch:   # an uncertain turn's root is dropped on archive or at the next dispatch
                        self._stop_turn_group(result['sequence'])
                        self._drop_scratch(scratch)
                    break
            except BaseException:
                if role == 'author' and workspace_override is None:   # v2.9.7: a failed author turn may have changed the tree
                    try: self._void_opv_observed(seq0, failed=True)
                    except Exception: pass   # the turn's failure is the error to report; void_stale marks the row before it writes, so only a failed evidence write can leave it current on disk until the next save
                raise
            if role not in ('reviewer', 'shadow', 'gate'):
                if role == 'author' and workspace_override is None:   # a probe or candidate turn in another tree leaves the workspace alone
                    self._void_opv_observed(seq0)
                    opv.void_stale(self, result.get('snapshot'), atomic_json)   # OPV: a tree the author changed voids its records
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
                       + ('\nDo not expand the work item.\n' if phase == 'PLAN' else
                          '\nDo not expand the review scope.\n' if self.state.get('review_only') else '\nDo not expand the approved plan.\n')
                       + note_bytes.decode('utf-8'))
            operator_note['attempts'] = operator_note.get('attempts', 0) + 1
        self.assert_fresh_prompt(role, prompt)
        active_workspace = Path(workspace_override).resolve() if workspace_override else self.workspace
        capability = {}
        if self._role_vendor(role) == 'codex':
            capability = self.codex_capabilities(active_workspace)
            if capability['status'] != 'PASS':
                raise RuntimeError('; '.join(capability['issues']))
        if role == 'author' and self._role_vendor(role) == 'codex':
            self.author_temp_dir.mkdir(parents=True, exist_ok=True)
            env_overrides = {**(env_overrides or {}), 'TMPDIR': str(self.author_temp_dir)}
        limit = self.args.max_invocations - self.state.get('q_reserved', 0)
        if self.state['config'].get('review_report'):
            limit = min(limit, self._report_budget())
        if self.state['invocations_used'] >= limit:
            raise RuntimeError('invocation limit reached' + (f' (report budget {limit})' if self.state['config'].get('review_report') else ''))
        if issue := self._wi_deadline_issue(): raise RuntimeError(issue)
        timeout_seconds = (self.args.exec_turn_timeout if role == 'author' and phase in ('EXEC', 'FINISH', 'DOCS')
                           else self.args.timeout)
        self.state['sequence'] += 1
        seq = self.state['sequence']
        prefix = self.evidence / f'{seq:03d}-{phase.lower()}-{role}'
        schema_path = prefix.with_suffix('.schema.json')
        atomic_json(schema_path, schema)
        atomic_text(prefix.with_suffix('.prompt.txt'), prompt)
        snapshot_workspace = self.workspace if self._fake_lifecycle and workspace_override else active_workspace
        control_dirs, control_before = git_control_state(snapshot_workspace) if role == 'author' else ([], {})
        if bad := control_before.get('!unreadable') or next((v for v in control_before.values() if v.startswith('UNAVAILABLE')), None): raise RuntimeError('git control state unreadable: ' + bad)   # no further git call in a workspace whose config cannot be read
        before, manifest = git_snapshot(snapshot_workspace)
        recorded, keep = None, self.internal / 'readonly' / f'{seq:03d}-{role}'   # D-EFF category A, both modes
        if role in ('reviewer', 'gate', 'shadow') and not allow_mutation_report:
            try: marks = readonly_guard.marks(snapshot_workspace)   # apart from the tree, so a failed capture still compares them
            except (OSError, RuntimeError, subprocess.SubprocessError): marks = {}
            try: recorded = readonly_guard.capture(snapshot_workspace, keep)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc: recorded = {**marks, 'error': f'{type(exc).__name__}: {exc}'}
        head_before = git_head_state(snapshot_workspace) if role == 'author' else None   # D-EFF git guard (category B), both modes
        config_before = (ignored_config.inventory(snapshot_workspace, lambda names: self._ignored_paths(snapshot_workspace, names))
                         if role == 'author' and workspace_override is None else None)   # F3: report only, both modes
        context_before = directory_digest(self.context)
        atomic_json(prefix.with_suffix('.snapshot-before.json'), {'digest': before, 'manifest': manifest})
        command = self.command(role, schema_path, fresh, active_workspace)
        command[0] = self.state['operator_programs'][self._role_vendor(role) + '_bin']['path']
        if self._role_vendor(role) == 'codex' and ('-c', 'features.plugins=false') not in zip(command, command[1:]):   # rel210-fixCG:
            raise RuntimeError('Codex argv lacks -c features.plugins=false, so cached plugin bundles would not be inert')   # the guard's premise
        now = time.time()
        receipt = {'sequence': seq, 'role': role, 'phase': phase, 'vendor': self._role_vendor(role),
                   'model': self._model_effort(role)[0], 'command': command, 'snapshot_before': before,
                   'workspace': str(active_workspace), 'global_codex_home': str(self.global_codex_home), 'global_config_home': str(self.global_config_home),
                   **({'codex_plugin_bundles_inert': capability['plugin_bundles_inert']} if capability.get('plugin_bundles_inert') else {}),
                   'context_before': context_before,
                   'start': now, 'gap': now - self.state['last_end'].get(role, now), 'fresh': fresh,
                   'role_identity_sha256': self.state.get('role_dispatch_manifest_sha256'),
                   'timeout_seconds': timeout_seconds,
                   'invocation_budget_counted': False, 'usage_requests': [], 'model_requests': 0}
        if self._fake_lifecycle:
            receipt['run_id'] = self.run_dir.name
        if role == 'reviewer':
            receipt['open_finding_ids'] = [row['id'] for row in self._reviewer_open_findings()]
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
        if issue := self._recovery_config_issue(vendor_config_before):   # F7: before the child exists or the budget counts
            raise RuntimeError(issue)
        vendor_prefix = 'codex_' if receipt['vendor'] == 'codex' else 'claude_'
        receipt['global_' + receipt['vendor'] + '_before'] = {key: value['sha256'] for key, value in vendor_config_before.items() if key.startswith(vendor_prefix)}
        stdout_path, stderr_path = prefix.with_suffix('.stdout.jsonl'), prefix.with_suffix('.stderr.log')
        watched = (run_dir_inodes(self.run_dir, {self.run_dir / 'role-tmp'}, prefix)   # b296-f1: all but this turn's own files and the scratch
                   if role in READONLY_SCRATCH_ROLES and (env_overrides or {}).get('TMPDIR') else None)
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
                DETACH_TURN['spawning'] = True   # detach: a stop in this span waits until the group is known (no signal mask:
                try:                              # a child would inherit it and could not be terminated)
                    process = subprocess.Popen(command, cwd=active_workspace, env=env, stdin=subprocess.PIPE,
                                               stdout=out, stderr=err, start_new_session=True)
                    DETACH_TURN['group'] = process.pid
                finally:
                    DETACH_TURN['spawning'] = False
                    if DETACH_TURN.pop('stop', False):   # a stop noted while spawning: kill the new group, unwind below
                        stop_turn_group()
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
                DETACH_TURN['group'] = None
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
            DETACH_TURN['group'] = None   # reaped (or being re-raised after its group was killed)
            usage_stop.set()
            usage_monitor.join()
        receipt['end'] = time.time()
        receipt['wall_seconds'] = receipt['end'] - receipt['start']
        receipt['returncode'] = process.returncode
        after_inodes = run_dir_inodes(self.run_dir, {self.run_dir / 'role-tmp'}, prefix) if watched is not None else None   # before the coordinator writes again
        control_after = git_control_state(snapshot_workspace, control_dirs)[1] if role == 'author' else {}
        control_changed = sorted(k for k in {*control_before, *control_after} if control_before.get(k) != control_after.get(k))
        unreadable = control_before.get('!unreadable') or control_after.get('!unreadable')
        control_problem = ('git control state unreadable: ' + unreadable if unreadable else
                           'author changed git control files: ' + ', '.join(control_changed) if control_changed else '')
        context_after = directory_digest(self.context)
        receipt['context_after'] = context_after
        snapshot_failure = ''
        if control_problem: after = None   # D1: no further git call in this workspace after the author touched its git control files
        else:
            try: after, after_manifest = git_snapshot(snapshot_workspace)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:   # eff-e: a read-only turn may have broken the index
                if recorded is None: raise
                after, after_manifest, snapshot_failure = None, [], f'the post-turn workspace snapshot failed ({type(exc).__name__}: {exc})'
            receipt['snapshot_after'] = after
            atomic_json(prefix.with_suffix('.snapshot-after.json'), {'digest': after, 'manifest': after_manifest})
            if config_before is not None:   # F3: only after the git control check, so no git call follows a changed control file
                self._record_ignored_config(receipt, config_before, ignored_config.inventory(
                    snapshot_workspace, lambda names: self._ignored_paths(snapshot_workspace, names)))
        voided = None   # D-EFF category A: set below when a read-only turn changed the workspace
        try:
            if control_problem: raise ValueError(control_problem)
            if head_before is not None and git_head_state(snapshot_workspace) != head_before:   # before any exit-code check: a failed turn too
                raise ValueError('author changed HEAD or the branch: a commit, reset or checkout is not allowed in a paired-session run')
            # D-EFF category A: undo first, so a failed or rejected read-only turn is undone too; raised after the other checks
            if snapshot_failure:   # unverifiable: record the pre-turn baseline; dispatch and accept refuse until it matches again
                voided = self._void_readonly_turn(role, receipt, snapshot_workspace, {**recorded, 'error': snapshot_failure}, keep, prefix, before)
            elif role != 'author' and not allow_mutation_report and self._readonly_changed(snapshot_workspace, recorded, before, after):
                voided = self._void_readonly_turn(role, receipt, snapshot_workspace, recorded, keep, prefix, before)
            if recorded and (note := recorded.get('ignored_note')): receipt['readonly_ignored'] = note
            touched = readonly_guard.ignored_changes(snapshot_workspace, recorded) if recorded and not snapshot_failure else []
            if touched: receipt['voided'] = {**receipt.get('voided', {}), 'ignored_changed': touched[:50]}   # eff-e: no ignored content is kept
            if role in READONLY_SCRATCH_ROLES and (env_overrides or {}).get('TMPDIR'):   # b295-f1: listed in the receipt before it is removed
                receipt['scratch_entries'], linked = scratch_listing(Path(env_overrides['TMPDIR']))
                if linked:   # a hard link there could write through to a run-dir file on the same volume
                    raise ValueError(f'{role} left a hard link in its scratch temp root: ' + ', '.join(linked))
                if (run_dir_touched := sorted(path for path, mark in watched.items() if after_inodes.get(path) != mark)):   # a link made and removed within the turn
                    raise ValueError(f'{role} changed run-dir files during its turn (a link, write or mode change): ' + ', '.join(run_dir_touched[:5]))
            if vendor_config_before is not None:
                config_after = global_config_snapshot(self.global_config_home, self.global_codex_home)
                concurrent = (concurrent_run_workspaces(active_workspace) if receipt['vendor'] == 'codex' and   # FIELD-22, read lazily
                              vendor_config_before['codex_config']['sha256'] != config_after['codex_config']['sha256'] else None)
                changes = attribute_global_config_changes(vendor_config_before, config_after, [active_workspace], concurrent)
                changes['other_vendor_changes'] = [key for key in changes['before'] if not key.startswith(vendor_prefix) and changes['before'][key]['sha256'] != changes['after'][key]['sha256']]
                changes['findings'] = [row for row in changes['findings'] if row['file'].startswith(vendor_prefix)]
                changes['expected_changes'] = [row for row in changes['expected_changes'] if row['file'].startswith(vendor_prefix)]
                reclassify_default_home_trust(changes, vendor_config_before, config_after, [active_workspace, self.workspace])   # P0
                changes['warnings'] = changes['warnings'] if receipt['vendor'] == 'codex' else []
                changes['status'] = 'FAIL' if changes['findings'] else 'PASS'
                receipt['global_config_changes'] = changes
                if changes['status'] != 'PASS':
                    message = receipt['vendor'] + ' turn changed global config: ' + ', '.join(row['file'] for row in changes['findings'])
                    if update := ([row['file'] for row in changes['findings']] == ['claude_plugins'] and
                                  normal_plugin_update(vendor_config_before['claude_plugins'], config_after['claude_plugins'])):
                        changes['plugin_update'] = update   # field21: recorded in both modes
                    if not update or self.strict or voided is not None or touched:
                        raise ValueError(message + (PLUGIN_UPDATE_HINT if update else ''))
                    # FIELD-24 (efficient): the running CLI loaded its plugins at start and the old versioned cache stays on
                    # disk, so the turn's work stands; the next turn starts on the new registry. Recorded, never voided.
                    versions = ', '.join(f"{row['plugin']} {row['version'][0]} -> {row['version'][1]}" for row in update)
                    changes['findings'] = [row for row in changes['findings'] if row['file'] != 'claude_plugins']
                    changes['status'] = 'PASS'
                    changes['next_turn_registry'] = 'the next turn starts on the updated plugin registry: ' + versions
                    print(f'NOTE: a normal plugin update outside the run during the {role} turn ({versions}); the turn '
                          'is kept and the next turn starts on the updated registry')
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
            fallbacks, guard_calls, guard_cwd, proven = [], tool_calls, active_workspace, None
            if receipt['vendor'] == 'codex':   # its events carry no workdir: only the rollout's own record proves one (v297-eg-cwd)
                guard_calls, guard_cwd, proven = codex_guard_calls(tool_calls, codex_rollout_cwds(
                    session, receipt['start'], receipt['end'], (self.workspace, active_workspace, self.author_temp_dir)))
            forbidden = sensitive_access(guard_calls, role, self.evidence, self.rounds, guard_cwd,   # configured bytes: CLI commands only
                                         env, (self.workspace, active_workspace, self.author_temp_dir),
                                         tuple(c for c in (self.args.test_command, *self.args.reviewer_command) if c), fallbacks)
            receipt['evidence_guard'] = {'fallbacks': len(fallbacks), 'fallback_reasons': fallbacks[:20],   # v297-eg-wire: substring-guard calls
                                         **({'codex_cwd_proven': proven} if proven is not None else {})}
            if forbidden and self.strict:
                raise ValueError(f'{role} accessed isolated {forbidden}')
            if forbidden: receipt['evidence_guard']['would_hold'] = f'{role} accessed isolated {forbidden}'   # D-EFF efficient: log-only
            if context_before != context_after:
                raise ValueError(f'{role} mutated coordinator context outside workspace')
            if role == 'author' and phase == 'PLAN' and before != after:
                raise ValueError('author mutated workspace during PLAN')
            if touched:   # after the other checks, like the void below, so a more specific reason of this turn comes first
                raise ValueError(f'{role} changed ignored files in the workspace, which the coordinator does not restore (the verdict '
                                 f'is void; check them by hand): {", ".join(touched[:20])}')
            if voided is not None:
                raise voided
            if recorded is not None: shutil.rmtree(keep, ignore_errors=True)   # a clean read-only turn needs no undo record
            if role != 'author':
                answer['reviewed_snapshot'] = before
            if receipt['model_identity'] == 'MISMATCH':
                raise ValueError(f'model identity mismatch: {role} configured {receipt["model"]}, '
                                 f'CLI reported {reported_model}')
            approves_exec = (phase in ('EXEC', 'POLISH') and
                             ((role in ('reviewer', 'shadow') and answer.get('status') == 'APPROVE') or
                              (role == 'gate' and answer.get('verdict') == 'approve')))
            if approves_exec and self.args.test_command is None and self.state['config'].get('review_report'):   # LG2-b2
                receipt['static_untested_approval'] = True   # no test result is claimed; the report says tests did not run
            elif approves_exec and not (self._fake_lifecycle and workspace_override and
                                      self.state.get('fake_candidate_test') and
                                      self.state.get('fake_ingest_receipt') and
                                      self.state['fake_candidate_test']['ingest_id'] ==
                                      self.state['fake_ingest_receipt']['id'] and
                                      Path(workspace_override).resolve() == Path(
                                          self.state['fake_candidate_test']['root']).resolve()) and not any(
                    observed_test_succeeded(row, self.args.test_command)
                                         for row in observed_commands):
                unfinished = any(command_invokes_test(row.get('command', ''), self.args.test_command) and command_not_completed(row)
                                 for row in observed_commands)   # FIELD-20
                raise ValueError(f'{role} EXEC approval lacks an observed successful configured test command'
                                 + ('; the configured test was not observed to completion (no exit status when the turn ended)'
                                    if unfinished else ''))
            old = self.state['sessions'].get(role)
            if not fresh and old and session and old != session:
                raise ValueError(f'{role} resumed a different session')
            if not fresh:
                if session:
                    self.state['sessions'][role] = session
                self.state['started'][role] = True
        except Exception as exc:   # any other exception is recorded here only after a failed undo, which must lead the hold
            if not isinstance(exc, (ValueError, KeyError, json.JSONDecodeError)) and (voided is None or voided.restored): raise
            receipt['error'] = str(exc)
            if voided is not None and not voided.restored:   # the failed undo leads, whatever else this turn tripped
                receipt['error'] = ('read-only turn changed the workspace and it could not be restored; manual restore needed: '
                                    + str(voided) + ('' if exc is voided else '; also: ' + str(exc)))
            self._rotate_failed_first_claude_session(role, receipt['vendor'], fresh)
            atomic_json(prefix.with_suffix('.receipt.json'), receipt)
            self.state['turns'].append(receipt)
            self.state['last_end'][role] = receipt['end']
            self.state['active'] = None
            self.save()
            raise (RateLimitedTurn if receipt.get('error_kind') == 'rate_limited' and voided is None else RuntimeError)(
                receipt['error']) from exc
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
        rewrite = self.state.get('plan_history_rewrite') if phase == 'PLAN' else None
        rewriting = bool(rewrite) and rewrite.get('status') == 'requested'   # FIELD-11b: the extra turn is no PLAN round
        if rewriting and result['sequence'] not in rewrite.setdefault('attempt_sequences', []):
            rewrite['attempt_sequences'].append(result['sequence'])           # every try is audited; the chance itself stays one
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
        if not rewriting: self.state[key] += 1
        if phase == 'PLAN':
            atomic_text(self.run_dir / 'plan.md', answer['body'].rstrip() + '\n')
            atomic_text(self.run_dir / ('plan-rewrite.md' if rewriting else f"plan-{self.state[key]:02d}.md"), answer['body'].rstrip() + '\n')
            atomic_text(self.context / 'plan.md', answer['body'].rstrip() + '\n')
        if rewriting:
            rewrite.update(status='written', author_sequence=result['sequence'],
                           plan_sha256=hashlib.sha256((self.context / 'plan.md').read_bytes()).hexdigest())
            for receipt in self.state['turns']:
                if receipt.get('sequence') == result['sequence']: receipt['plan_rewrite'] = True   # the audit trail names the extra turn
            self.progress('plan_rewrite', status='written', sequence=result['sequence'])
        self.state['delivered_review'] = ''
        self.state['next'] = 'reviewer'
        self.state.pop('pending_author_result_sequence', None)
        self.save()

    def _writer_git_state(self) -> dict:
        """HEAD, its branch and the staged entries: what a lifecycle writer must not change (doc 6, D8)."""
        digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
        return {'head': self._head_commit(), 'branch': self._git(['symbolic-ref', '-q', 'HEAD'], ok=(0, 1)).strip(),
                'index': digest(self._git(['ls-files', '--stage']))}   # tags/other refs are shared across worktrees

    def _lifecycle_writer(self, request: dict, label: str, prompt) -> dict:
        """A fresh author session in the live worktree that must leave HEAD, its branch and the index unchanged
        (D8). The git baseline is persisted per attempt; a recorded turn since it is reused, never re-dispatched."""
        life, phase = self.state['lifecycle'], request['stage']
        if life['pending'] != request:   # new attempt: persist the git baseline before anything runs
            life['writer_git'] = {**self._writer_git_state(), 'sequence': self.state['sequence']}
        self.state['lifecycle'] = lifecycle_spine.begin(life, request)   # replay-stable on retry
        self.save()
        base = self.state['lifecycle'].get('writer_git')
        if not base:
            raise RuntimeError(f'{phase} request is in flight without a recorded git baseline; inspect, then abort')
        def check_git(cause=None):   # a violation discards every turn of this stage since the baseline: none is ever reused
            if self._writer_git_state() != {key: base[key] for key in ('head', 'branch', 'index')}:
                for turn in self.state['turns']:
                    if turn.get('phase') == phase and turn.get('sequence', 0) > base['sequence']:
                        turn.setdefault('discarded', 'git guard')
                self.save()
                raise RuntimeError(f'{label} changed HEAD, refs or the index; restore the recorded baseline or abort'
                                   + (f' (the turn failed: {cause})' if cause else ''))
        check_git()   # also after a crash, an uncertain turn or a HOLD: the baseline is the persisted one
        recorded = next((turn for turn in reversed(self.state['turns'])
                         if turn.get('role') == 'author' and turn.get('phase') == phase and not turn.get('error')
                         and not turn.get('discarded')
                         and turn.get('sequence', 0) > base['sequence'] and isinstance(turn.get('answer'), dict)), None)
        try:
            result = ({'answer': recorded['answer'], 'snapshot': recorded['snapshot_after'], 'sequence': recorded['sequence'],
                       'role': 'author'} if recorded else
                      self.invoke('author', phase, prompt(), author_schema(), fresh=True))
        except RuntimeError as exc:   # INT-2b: D-EFF's author HEAD guard (or any failure) first: the W writer guard still names a git
            check_git(exc)            # change, with the original error, and discards the turn; otherwise the original error stands
            raise
        check_git()
        return result

    def worktree_finish_turn(self) -> None:
        """ADR-11 FINISH: a fresh author session in the live worktree; a tree change reopens EXEC review + gate."""
        request = worktree_lifecycle.stage_request(self.state['lifecycle'], 'finisher')
        result = self._lifecycle_writer(request, 'finisher', lambda: worktree_lifecycle.finish_prompt(
            (self.context / 'plan.md').read_text(), self.args.test_command, self._docs_reserved_note(),
            bool(self.state.get('review_only'))))
        self.render(result, 'finisher', 'FINISH')
        answer = result['answer']
        receipt = {**request, 'status': 'HOLD' if answer['status'] == 'HOLD' else 'READY',
                   'output_oid': git_snapshot(self.workspace)[0], 'sequence': result['sequence']}   # the tree now, not as recorded
        self.state['lifecycle'] = worktree_lifecycle.after_finish(
            lifecycle_spine.complete(self.state['lifecycle'], receipt), receipt)
        self.state['lifecycle'].pop('writer_git', None)
        if answer['status'] == 'HOLD':
            self.hold('finisher: ' + answer['body'])
        elif self.state['lifecycle']['stage'] == 'EXEC':   # FINISH wrote: new convergence, reviewer then gate
            self.state['exec_rounds'] += 1
            self.state.update(phase='EXEC', next='reviewer', gate_ran=False, delivered_review='')
            if self.state['exec_rounds'] > self.exec_round_limit():
                self.round_limit_hold('EXEC round limit reached after a FINISH write')
        else:
            self.state['next'] = 'polish-q'
        self.save()

    def worktree_docs_turn(self) -> None:
        """ADR-11 DOCS (legacy Step 3.6): a fresh docs writer; its allowlisted writes get a fresh docs review with an
        observed test, a protected path HOLDs, and any other write replays EXEC (reviewer, then gate)."""
        life, workspace = self.state['lifecycle'], self.workspace
        allow = set(self._docs_allowlist())
        request = worktree_lifecycle.stage_request(life, 'docs-writer')
        base_file = self.evidence / f"{request['request_id']}-docs-base.json"
        if life['pending'] != request:
            tree, manifest = git_snapshot(workspace)
            if tree != life['candidate_oid']:
                raise RuntimeError('DOCS tree differs from the POLISH-Q-approved tree; restore it or abort')
            owned = self._docs_owned()   # DOCS's own entries: the EXEC author may fix docs findings in them,
            if touched := sorted(set(self._changed_paths(deleted=True)) & allow - set(owned)):   # DOCS reviews them again
                raise RuntimeError('the EXEC-reviewed change already touches docs allowlist paths: ' + ', '.join(touched) +
                                   '; abort, or rerun with those paths outside --docs-file/--docs-allowlist')
            if bad := sorted(path for path in allow if (workspace / path).is_dir() or   # the config-time invariant again:
                             (workspace / path).resolve() != Path(os.path.normpath(workspace / path))):   # no symlink on the path
                raise RuntimeError('a docs allowlist path is now a directory or reached through a symlink: ' +
                                   ', '.join(bad) + '; restore it or abort')   # the writer would write through it
            atomic_json(base_file, manifest)
        try:   # bound to the POLISH-Q-approved tree before the writer runs
            base = json.loads(base_file.read_text())
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'DOCS base manifest is unreadable: {exc}; abort') from exc
        if hashlib.sha256(json.dumps(base, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest() != request['candidate_oid']:
            raise RuntimeError('DOCS base manifest differs from the POLISH-Q-approved tree; abort')
        def writer_prompt():   # called only for a dispatch, never for a reused recorded turn
            self._docs_budget()
            return worktree_lifecycle.docs_prompt(self.state['config'].get('docs_file'), sorted(allow),
                                                  self.run_dir.name, self.workitem.read_text())
        result = self._lifecycle_writer(request, 'docs writer', writer_prompt)
        self.render(result, 'docs-writer', 'DOCS')
        tree, manifest = git_snapshot(workspace)
        before, after = dict(base), dict(manifest)
        changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        denied = [path for path in changed if worktree_lifecycle.docs_denied(path, after.get(path))]
        outside = [path for path in changed if path not in allow]
        receipt = {**request, 'status': 'READY', 'output_oid': tree, 'sequence': result['sequence'], 'docs_paths': changed,
                   'docs_written': {path: after.get(path) for path in changed if path in allow}}
        reason = '; '.join([*(['docs writer: ' + result['answer']['body']] if result['answer']['status'] == 'HOLD' else []),
                            *(['DOCS writer changed protected paths: ' + ', '.join(denied) + '; restore them or abort']
                              if denied else [])]) or None
        owned = self._docs_owned()
        replay = bool(outside)
        if not reason and not replay and (changed or owned):   # every DOCS-written doc gets a fresh docs review
            review = self._docs_review_turn(tree, sorted({*changed, *owned}))
            receipt['review'] = review
            replay = review['status'] != 'APPROVE'   # its findings go to the persistent EXEC reviewer, then the gate
        if not reason and not replay and (blocking := self.blocking_open_findings()):
            reason = 'DOCS cannot advance with open blocking findings: ' + ', '.join(row['id'] for row in blocking)
        receipt['route'] = 'HOLD' if reason else 'EXEC' if replay else 'SECURITY'
        self.state['lifecycle'] = lifecycle_spine.complete(
            self.state['lifecycle'], {**receipt, 'status': 'HOLD' if reason else 'READY'})
        self.state['lifecycle'].pop('writer_git', None)
        if not reason:   # reviewed (SECURITY) or about to be (EXEC replay): DOCS owns what it wrote (values: audit only)
            self.state['lifecycle']['docs_owned'] = {**self.state['lifecycle'].get('docs_owned', {}), **receipt['docs_written']}
        if reason:
            self.hold(reason)
        elif replay:   # a code or comment write, or a docs review REVISE: new EXEC convergence, reviewer then gate
            life = self.state['lifecycle']
            self.state['lifecycle'] = {**life, 'stage': 'EXEC', 'epoch': life['epoch'] + 1, 'candidate_oid': None}
            self.state['exec_rounds'] += 1
            self.state.update(phase='EXEC', next='reviewer', gate_ran=False, delivered_review='')
            if self.state['exec_rounds'] > self.exec_round_limit():
                self.round_limit_hold('EXEC round limit reached after a DOCS write')
        else:
            self.state['lifecycle'].update(stage='SECURITY', candidate_oid=tree)   # SECURITY binds the DOCS output
            self.state['next'] = 'security'
        self.save()

    def _docs_review_turn(self, tree: str, paths: list[str]) -> dict:
        """Fresh docs reviewer over the full diff (legacy 3.6 reviewer-only fast replay). A HOLD, an unusable review
        or a missing observed test raises before the DOCS receipt, so resume reuses the writer and reviews again."""
        self.materialize_review_context()
        prompt = ('Role: docs reviewer, fresh. Phase: DOCS.\n'
                  f'Review the documentation of {self._change_noun("this uncommitted change")} against the full diff (legacy review-loop '
                  'Step 3.6): the docs must describe the implemented behavior, APIs and logic accurately, and the '
                  'changed code comments must match the code. Do not modify any file. Documentation written by the '
                  'DOCS stage: ' + ', '.join(paths) + '\n' + self._review_protocol(self._changed_paths()) + '\n'
                  f'Run this test command exactly as written in one Bash call: {self.args.test_command}\n'
                  'Return only JSON matching the supplied schema.' + opv.prompt_block(self, tree, atomic_json))
        self._docs_budget()
        result = self.invoke('reviewer', 'DOCS', prompt, review_schema(), fresh=True)
        turn = next(t for t in self.state['turns'] if t['sequence'] == result['sequence'])
        if turn.get('snapshot_before') != tree:
            raise RuntimeError('docs reviewer reviewed a tree other than the one under review')
        answer = result['answer']
        findings = worktree_lifecycle.normalized_findings('docs-reviewer', answer['full_review'], 'docs reviewer')
        blocking = [row for row in findings if row['severity'] in BLOCKING_REVIEW_SEVERITIES or row.get('security')]
        if answer['status'] == 'HOLD' or (answer['status'] != 'APPROVE' and not findings):
            raise RuntimeError(f"docs reviewer returned {answer['status']} without a usable review; resume reviews again")
        if not (observed := self._observed_test(answer)):
            unfinished = any(command_invokes_test(row.get('command', ''), self.args.test_command) and command_not_completed(row)
                             for row in answer.get('observed_commands', []))   # FIELD-20
            raise RuntimeError('DOCS reviewer did not observe a successful run of the configured test command'
                               + (' (the configured test was not observed to completion: no exit status when the turn ended)'
                                  if unfinished else '') + '; resume reviews again')
        self.record_findings('docs-reviewer', 'DOCS', result['sequence'], findings)   # the EXEC reviewer disposes them
        self.render(result, 'docs-reviewer', 'DOCS')
        return {'sequence': result['sequence'], 'status': 'REVISE' if blocking else 'APPROVE', 'observed_test': observed,
                'finding_ids': [row['id'] for row in self.state['finding_ledger']
                                if row.get('source') == 'docs-reviewer' and row.get('origin_round') == result['sequence']]}

    def _redispatch_budget(self, phase: str, voided: Exception) -> None:
        """INT-2c: a W read-only re-dispatch counts against its stage cap like any dispatch (the callers check room for their
        own dispatches only); one that does not fit holds with the workspace already restored. Each stage counts as its caller
        does: DOCS/SECURITY every row of the phase, POLISH-Q the counted ones (polish_calls, reconciled only after invoke).
        The evidence-contract retry after a re-dispatch is not checked, so one invoke may still pass the cap by one; the
        caller's next check refuses and max_invocations bounds the run."""
        if phase not in ('POLISH-Q', 'DOCS', 'SECURITY') or not worktree_lifecycle.is_worktree(self.state): return
        rows = [row for row in [*self.state['turns'], *self.state.get('spawn_failures', [])] if row.get('phase') == phase]
        used = sum(row.get('invocation_budget_counted') is not False for row in rows) if phase == 'POLISH-Q' else self._stage_calls(phase)
        if used + 1 > self._worktree_run_cap(phase):
            raise RuntimeError(f'{phase} budget has no room to re-dispatch the void turn ({voided}); abort') from voided

    def _stage_calls(self, phase: str) -> int:
        """DOCS/SECURITY calls against the stage cap: every row of the phase except a provider rate-limit rejection, which is
        refunded like the invocation budget (ratelimit), so a limited call never uses up the stage."""
        return sum(row.get('phase') == phase and row.get('error_kind') != 'rate_limited'
                   for row in [*self.state['turns'], *self.state.get('spawn_failures', [])])

    def _docs_budget(self) -> None:
        used = self._stage_calls('DOCS')
        if used + 1 > self._worktree_run_cap('DOCS'):
            raise RuntimeError(f'DOCS budget exhausted ({used} writer and review calls in this run); abort')

    def _observed_test(self, answer: dict):
        return next((row['command'] for row in answer.get('observed_commands', [])
                     if observed_test_succeeded(row, self.args.test_command)), None)

    def _capture_security_baseline(self) -> dict:
        """D-6: the W01 delivery baseline for scripts/security_preflight.py, captured when the W state is created,
        before any probe or turn; a failure refuses the run instead of surfacing at SECURITY."""
        path = self.evidence / f'delivery-baseline-{uuid.uuid4().hex[:8]}.json'
        try:
            base = self.state['config'].get('review_base')   # LG1-c: a review-only run's baseline is its base, not the live tree
            proc = self._security_script('delivery_scope.py', 'capture', '--scope', '.', '--output', str(path),
                                         *(['--from-commit', base] if base else []))
        except RuntimeError as exc:
            raise ValueError(f'worktree lifecycle cannot capture its delivery baseline: {exc}') from exc
        try:
            if proc.returncode == 0:
                return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        except OSError as exc:
            raise ValueError(f'worktree lifecycle cannot read its delivery baseline: {exc}') from exc
        raise ValueError(f'worktree lifecycle cannot capture its delivery baseline (delivery_scope exit '
                         f'{proc.returncode}): {proc.stderr.strip()[-300:]}')

    def _security_script(self, script: str, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run([sys.executable, str(HERE.parent / 'scripts' / script), '--repo', str(self.workspace),
                                   *args], cwd=self.workspace, capture_output=True, text=True, errors='replace', timeout=600)
        except (OSError, subprocess.SubprocessError) as exc:   # a timeout or a missing interpreter HOLDs
            raise RuntimeError(f'{script} could not run: {exc}') from exc

    def _start_ignore_coverage(self) -> dict:
        """FIELD-19: when the W state is created, the .gitignore coverage SECURITY will credit (security_preflight.py
        start_coverage, the same ignore_coverage rules), so a gap is known before the run's cost. A warning only, in
        both modes (D-EFF adds no start gate); a failed check stays silent apart from a NOTE."""
        try:
            proc = self._security_script('security_preflight.py', '--ignore-coverage')
            missing = json.loads(proc.stdout)['uncovered'] if proc.returncode == 0 else None
        except (RuntimeError, ValueError, KeyError, TypeError):
            missing = None
        if not isinstance(missing, list):
            print('NOTE: .gitignore coverage could not be checked at start; SECURITY checks it at the end')
            return {'checked': False}
        if missing:
            print('WARNING: the tracked .gitignore does not cover ' + ', '.join(missing) + '; SECURITY will stop for '
                  'review at the end of this run. Commit a .gitignore covering them before starting (abort and start '
                  'again), or after that HOLD edit .gitignore without committing and resume (EXEC review replays)')
        return {'checked': True, 'uncovered': missing}

    def worktree_security_turn(self) -> None:
        """ADR-11 SECURITY over the DOCS-approved tree: the sensitive path scan and scripts/security_preflight.py
        (legacy Step 3.7) run every time, a no-op run included, then a fresh security reviewer (parity map W3a); any
        hit or finding HOLDs (no repair), and DONE (acceptance pending) follows only this stage."""
        if self.state['config'].get('review_report'):
            return self._report_security_turn()
        life = self.state['lifecycle']
        request = worktree_lifecycle.stage_request(life, 'security')
        tree = git_snapshot(self.workspace)[0]
        if tree != life['candidate_oid']:   # the operator's fix after a SECURITY HOLD: EXEC review and gate again
            life = lifecycle_spine.complete(lifecycle_spine.begin(life, request),
                                            {**request, 'status': 'HOLD', 'output_oid': tree, 'route': 'EXEC'})
            self.state['lifecycle'] = {key: value for key, value in life.items()
                                       if key not in ('security_base', 'security_turn', 'security_review')}
            self.state['lifecycle'].update(stage='EXEC', epoch=life['epoch'] + 1, candidate_oid=None)
            self.state['exec_rounds'] += 1
            self.state.update(phase='EXEC', next='reviewer', gate_ran=False, delivered_review='')
            if self.state['exec_rounds'] > self.exec_round_limit():
                self.round_limit_hold('EXEC round limit reached after a SECURITY-stage change')
            self.save()
            return
        if (head := self._head_commit()) != life['parent']:   # W writers never move HEAD; DONE would only HOLD at accept
            raise RuntimeError(f'HEAD moved since the run started ({life["parent"][:12]} -> {str(head)[:12]}); restore it and '
                               'resume, or abort')
        if (held := [row['id'] for row in self.open_findings() if row.get('owner_role') == 'security-reviewer']) and (
                life.get('security_hold_tree') == tree):   # the tree its findings were raised or left open on
            raise RuntimeError('security findings need a fix on a new tree, not a re-review: ' + ', '.join(held) +
                               '; fix them outside the run and resume (EXEC replays), or abort')
        self.state['lifecycle'] = lifecycle_spine.begin(life, request)
        self.save()
        sensitive = self._sensitive_paths()
        preflight = self._security_preflight(request['request_id'])
        reasons = [*(['SECURITY sensitive paths: ' + ', '.join(f"{row['path']} ({row['category']})" for row in sensitive)]
                     if sensitive else []), *([preflight['reason']] if preflight['reason'] else [])]
        review = None
        if not reasons and git_snapshot(self.workspace)[0] == tree:   # the reviewer sees only the scanned tree
            review = self._security_review_turn(tree, request['request_id'])
            reasons += [review['reason']] if review['reason'] else []
        if not reasons and (blocking := self.blocking_open_findings()):
            reasons.append('SECURITY cannot reach DONE with open blocking findings: ' + ', '.join(row['id'] for row in blocking))
        if (after := git_snapshot(self.workspace)[0]) != tree:
            reasons.append('SECURITY tree changed during the stage; resume replays EXEC review and gate')
        self.state['lifecycle'] = lifecycle_spine.complete(self.state['lifecycle'], {
            **request, 'status': 'HOLD' if reasons else 'READY', 'output_oid': after,
            'route': 'HOLD' if reasons else 'DONE', 'sensitive_paths': sensitive, 'preflight': preflight, 'review': review})
        for key in ('security_turn', 'security_review'):
            self.state['lifecycle'].pop(key, None)
        if reasons:
            self.hold('; '.join(reasons))
        else:   # acceptance pending: accept --expect (W3b) delivers it
            self.done(expected=after, lifecycle_stage='DONE')

    def _report_security_turn(self) -> None:
        """LG2-a3, report mode: the sensitive path scan and the preflight results become security findings, the
        security reviewer always runs, and the run ends at REPORTED. A preflight that could not scan, a failed security
        review or a tree change stays a HOLD (marking the report incomplete); nothing here blocks on a finding."""
        life = self.state['lifecycle']
        request = worktree_lifecycle.stage_request(life, 'security')
        tree = git_snapshot(self.workspace)[0]
        if tree != life['candidate_oid']:
            raise RuntimeError('SECURITY tree differs from the reviewed report tree; restore it and resume, or abort')
        self.state['lifecycle'] = lifecycle_spine.begin(life, request)
        self.save()
        sensitive = self._sensitive_paths()
        preflight = self._security_preflight(request['request_id'])
        if preflight.get('status') not in ('clean', 'blocked', 'review-required'):   # it could not scan: a HOLD class
            raise RuntimeError(preflight.get('reason') or 'security preflight did not complete')
        changed = set(self._changed_paths(deleted=True))   # spec §2.3: "a secret or a sensitive path in the PR"

        def row_for(kind: str, path: str, summary: str, what: str) -> dict:
            if path in changed and os.path.lexists(self.workspace / path):   # carried: in the change AND in the tree
                return {'severity': 'CRITICAL', 'security': True, 'file': path, 'summary': f'[class: {kind}] {summary}',
                        'body': f'The change carries {what}.', 'failure_scenario': 'Merging the change publishes it.'}
            if path in changed:   # deleted or renamed away, staged or not: downgraded, not dropped (it stays in history)
                return {'severity': 'MINOR', 'security': True, 'file': path, 'summary': f'[class: {kind}-removed] {summary}',
                        'body': f'{what[0].upper() + what[1:]} is removed by this change; it stays in the repository history.',
                        'failure_scenario': 'None from this change; the history still holds it.'}
            return {'severity': 'MINOR', 'security': True, 'file': path,   # gate a3: downgraded, not dropped
                    'summary': f'[class: {kind}-preexisting] {summary}',
                    'body': f'{what[0].upper() + what[1:]} is already in the repository, not introduced by this change.',
                    'failure_scenario': 'None from this change; the repository already carries it.'}
        rows = [row_for('sensitive-path', row['path'], f"{row['path']} ({row['category']})",
                        f"a sensitive path: {row['path']} ({row['category']})") for row in sensitive]
        seen = set()   # gate a3: preflight reports a committed file twice (worktree and index); one finding each
        for row in preflight.get('findings', []):
            if (key := (row['rule'], row['path'], row['line'])) in seen:
                continue
            seen.add(key)
            where = f"{row['path']}" + (f":{row['line']}" if row['line'] else '')
            rows.append(row_for('secret', row['path'], f"{row['rule']} in {where}",
                                f"a secret matching security preflight rule {row['rule']} in {where} (value not shown)"))
        if any(Path(path).name == '.gitignore' for path in changed):   # repository-wide coverage, reported only then
            rows += [{'severity': 'MINOR', 'security': True, 'file': '.gitignore',
                      'summary': f'.gitignore does not cover {category}', 'body': f'.gitignore does not cover {category}.',
                      'failure_scenario': 'Such files could be committed later.'} for category in preflight.get('uncovered_ignore', [])]
        self.record_findings('security-preflight', 'SECURITY', life['epoch'], rows)
        self.write_ledger()
        review = self._security_review_turn(tree, request['request_id'])
        reason = review['reason'] if review['status'] == 'HOLD' else None   # findings are report content; a failed role HOLDs
        if (after := git_snapshot(self.workspace)[0]) != tree:
            reason = 'SECURITY tree changed during the stage; restore it and resume, or abort'
        self.state['lifecycle'] = lifecycle_spine.complete(self.state['lifecycle'], {
            **request, 'status': 'HOLD' if reason else 'READY', 'output_oid': after,
            'route': 'HOLD' if reason else 'REPORTED', 'sensitive_paths': sensitive, 'preflight': preflight, 'review': review})
        for key in ('security_turn', 'security_review'):
            self.state['lifecycle'].pop(key, None)
        if reason:
            self.hold(reason)
        else:
            self.reported()

    def reported(self) -> str:
        """LG2-a3: the terminal of a report run. Nothing is delivered, so nothing is accepted; findings stay findings."""
        self.state['lifecycle']['stage'] = 'REPORTED'
        self.state.update(status='REPORTED', next='reported', completed_at=time.time())
        self._report_mark(True)
        self.save()
        self._write_review_report()
        self.write_ledger()
        self.write_comparison()
        self.write_open_findings()
        self.write_usage()
        self._progress_terminal('REPORTED')
        return 'REPORTED'

    def _polish_specialists(self, paths: list[str]) -> tuple:
        """POLISH-Q's specialists; a report run uses its aspect subset and the two report-only analyzers (LG2-b1)."""
        config = self.state['config']
        if config.get('skip_quality_polish'):
            return ()
        if not config.get('review_report'):
            return worktree_lifecycle.specialists(paths)
        base = config.get('review_base') or 'HEAD'
        diff = self._git(['diff', '--no-ext-diff', '--no-textconv', '-U0', base, '--'])
        untracked = [path for path in self._git(['ls-files', '-z', '--others', '--exclude-standard']).split('\0') if path]
        added = ''.join('+' + line + '\n' for path in untracked if (self.workspace / path).is_file()
                        for line in (self.workspace / path).read_text(errors='replace').splitlines())
        return worktree_lifecycle.report_specialists(paths, config.get('review_aspects') or worktree_lifecycle.REPORT_ASPECTS,
                                                     worktree_lifecycle.touches_comment_lines(diff + added))

    def _write_review_report(self) -> None:
        """LG2-b1: review-report.md in the run directory, at REPORTED and at every HOLD (marked incomplete)."""
        atomic_text(self.run_dir / 'review-report.md', review_report.render(self.state))

    def _report_mark(self, complete: bool, reason: Optional[str] = None) -> None:
        """LG2-a3: the report's completeness, for the report file (b2). A HOLD marks it incomplete and names the stages
        that did not complete."""
        receipts = (self.state.get('lifecycle') or {}).get('receipts', [])
        progress = self.state.get('report_progress') or {}   # set only when a stage's result moved the run on
        missing = [name for name, ran in (
            ('EXEC reviewer and shadow', bool(progress.get('exec_review'))),
            ('adversarial gate', bool(progress.get('gate'))),
            ('POLISH-Q specialists', any(r.get('stage') == 'POLISH-Q' and r.get('status') == 'READY' for r in receipts)),
            ('SECURITY', any(r.get('stage') == 'SECURITY' and r.get('route') == 'REPORTED' for r in receipts))) if not ran]
        self.state['report'] = {'complete': complete, 'hold_reason': reason, 'not_completed': [] if complete else missing}

    def _report_budget(self) -> int:
        """LG2-a3: a report run's invocation budget, set by its role count (reviewer, shadow, gate, the selected
        specialists, the security reviewer) plus the permission-probe turns a2 kept (the reviewer probe, and the gate
        probe when the gate vendor differs), each with one retry. Frozen at first use; the --max-invocations cap still applies."""
        if (budget := self.state.get('report_budget')) is None:
            if git_snapshot(self.workspace)[0] != (self.state.get('review_only') or {}).get('candidate_tree_sha256'):
                raise RuntimeError('the report tree changed since the run was created; restore it and resume, or abort')
            names = self._polish_specialists(self._changed_paths())
            roles = 1 + (self.args.shadow == 'on') + 1 + len(names) + 1
            probes = 1 + (self.args.gate_vendor != self.args.reviewer_vendor)
            budget = self.state['report_budget'] = 2 * (roles + probes)
            self.save()
        return budget

    def _security_review_turn(self, tree: str, request_id: str) -> dict:
        """Fresh security reviewer over the clean scan: any open finding of its own HOLDs (no repair); on the new
        tree an operator fix produced, it disposes its earlier findings (owner only). Only a turn invoke returned
        (evidence contract checked) is recorded for reuse on a crash replay; a turn without tool calls, a HOLD, an
        unusable verdict or a malformed answer is discarded before any ledger write, so resume dispatches again
        under BUDGET_CAPS['SECURITY']."""
        life, owner = self.state['lifecycle'], 'security-reviewer'
        if (stored := life.get('security_review')) and stored['request_id'] == request_id:
            return stored['review']   # the ledger is already durable for this review
        owned = [row for row in self.open_findings() if row.get('owner_role') == owner]
        def discard(turn, reason):
            turn['discarded'] = reason
            life.pop('security_turn', None)
            self.save()
        recorded = life.get('security_turn') or {}
        turn = (next((row for row in self.state['turns'] if row['sequence'] == recorded.get('sequence')
                      and not row.get('discarded')), None) if recorded.get('request_id') == request_id else None)
        prompt = None
        for attempt in (1, 2):
            if turn is None:
                if prompt is None:
                    self.materialize_review_context()
                    prompt = ('Role: security reviewer, fresh. Phase: SECURITY.\n'
                              f'Review {self._change_noun("this uncommitted change")} for security defects: secrets or credentials in code, '
                              'configuration or docs; injection; unsafe deserialization; path traversal; missing '
                              'authorization or input validation; unsafe subprocess, file or network handling; '
                              'sensitive files that should be ignored. Do not modify any file. Report every finding '
                              'in full_review; any finding stops delivery.\n' + worktree_lifecycle.owned_ledger(owned) +
                              self._review_protocol(self._changed_paths()) +
                              '\nReturn only JSON matching the supplied schema.' + opv.prompt_block(self, tree, atomic_json))
                used = self._stage_calls('SECURITY')
                if used + 1 > self._worktree_run_cap('SECURITY'):
                    raise RuntimeError(f'SECURITY budget exhausted ({used} security reviews in this run); abort')
                result = self.invoke('reviewer', 'SECURITY', prompt, review_schema(), fresh=True)
                turn = next(row for row in self.state['turns'] if row['sequence'] == result['sequence'])
                life['security_turn'] = {'request_id': request_id, 'sequence': turn['sequence']}
                self.save()
            if turn.get('observed_tool_calls'):
                break
            discard(turn, 'no tool calls')
            turn = None
        else:
            return {'status': 'HOLD', 'finding_ids': [], 'reason': 'security reviewer made no tool calls after one retry'}
        answer, sequence = turn['answer'], turn['sequence']
        try:
            if turn.get('snapshot_before') != tree:
                raise RuntimeError('security reviewer reviewed a tree other than the one under review')
            findings = worktree_lifecycle.normalized_findings(owner, answer['full_review'], 'security reviewer')
            if answer['status'] == 'HOLD' or (answer['status'] != 'APPROVE' and not findings and not any(
                    row.get('disposition') == 'still_open' for row in answer.get('prior_findings') or [])):
                raise RuntimeError(f"security reviewer returned {answer['status']} without a usable review")
            ledger = copy.deepcopy(self.state['finding_ledger'])
            try:
                if missing := self.apply_dispositions(answer.get('prior_findings') or [], sequence,
                                                      [row['id'] for row in owned], owner):
                    raise RuntimeError('security reviewer omitted dispositions for its findings: ' + ', '.join(missing))
            except (RuntimeError, ValueError):
                self.state['finding_ledger'] = ledger   # a malformed answer leaves no partial disposition
                raise
        except (RuntimeError, ValueError) as exc:
            discard(turn, str(exc)[:200])   # resume dispatches a fresh review
            raise RuntimeError(f'{exc}; resume reviews again') from exc
        self.record_findings(owner, 'SECURITY', sequence, findings)
        self.render({'answer': answer, 'sequence': sequence, 'snapshot': turn.get('snapshot_after'), 'role': 'reviewer'},
                    owner, 'SECURITY')
        open_ids = [row['id'] for row in self.open_findings() if row.get('owner_role') == owner]
        review = {'sequence': sequence, 'status': answer['status'], 'finding_ids': open_ids,
                  'reason': ('security reviewer findings: ' + ', '.join(open_ids) + '; fix them outside the run and '
                             'resume (EXEC replays), or abort') if open_ids else None}
        life['security_hold_tree'] = tree if open_ids else None   # only a review that wrote the ledger moves it
        life['security_review'] = {'request_id': request_id, 'review': review}
        self.save()
        return review

    def _sensitive_paths(self) -> list[dict]:
        """sensitive_policy over the tracked and non-ignored untracked paths (legacy 3.7.1)."""
        hits = []
        for name in sorted(set(filter(None, self._git(['ls-files', '-z', '-co', '--exclude-standard']).split('\0')))):
            try:
                category = sensitive_policy.sensitive_path_category(name)
            except ValueError:
                category = 'unclassifiable path'
            if category:
                hits.append({'path': name, 'category': category})
        return hits

    def _security_preflight(self, request_id: str) -> dict:
        """D-6: scripts/security_preflight.py over a fresh manifest; anything but exit 0 with a clean report HOLDs."""
        baseline = self.state['lifecycle'].get('security_baseline')
        if not baseline:
            return {'status': 'unavailable', 'reason': 'security preflight unavailable: no delivery baseline '
                                                       '(the run started before W3a); abort'}
        path = Path(baseline['path'])
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = None
        if digest != baseline['sha256']:
            return {'status': 'unavailable', 'reason': 'security preflight unavailable: the delivery baseline changed; abort'}
        prefix = self.evidence / f'{request_id}-{uuid.uuid4().hex[:8]}'
        manifest, report = Path(f'{prefix}-manifest.json'), Path(f'{prefix}-preflight.json')
        proc = self._security_script('delivery_scope.py', 'manifest', '--baseline', str(path), '--output', str(manifest))
        if proc.returncode:
            return {'status': 'unavailable', 'reason': f'security preflight unavailable: delivery_scope manifest exited '
                                                       f'{proc.returncode}: {proc.stderr.strip()[-300:]}'}
        proc = self._security_script('security_preflight.py', '--manifest', str(manifest), '--output', str(report))
        try:
            document = json.loads(report.read_text())
        except (OSError, ValueError):
            document = {}
        result = {'exit': proc.returncode, 'status': document.get('status', 'unknown'), 'report': str(report),
                  'scanned_files': document.get('scanned_files'),
                  'findings': [{key: row.get(key) for key in ('rule', 'path', 'line')} for row in document.get('findings', [])],
                  'uncovered_ignore': [row['category'] for row in document.get('ignore_coverage', []) if not row.get('covered')]}
        clean = (proc.returncode == 0 and result['status'] == 'clean' and document.get('coverage_complete') is True and
                 document.get('ignore_coverage_complete') is True)
        result['reason'] = None if clean else (
            f"security preflight {result['status']} (exit {proc.returncode})" +
            ''.join(f'; {row["rule"]} in {row["path"]}' for row in result['findings'][:10]) +
            ('; .gitignore does not cover: ' + ', '.join(result['uncovered_ignore']) + '. To recover, add the patterns to '
             'the tracked .gitignore in the worktree, neither committed nor staged (an edit made during the run counts and '
             'ships with the delivery; undo a commit by restoring HEAD to the run\'s parent, keeping the edit) and resume: '
             'EXEC review and gate replay, then FINISH..SECURITY, within the EXEC round limit. An edit already present when '
             'the run started does not count as the run\'s own: restore the file to HEAD, or abort, commit it, start a new '
             'run' if result['uncovered_ignore'] else '') +
            (f'; {proc.stderr.strip()[-300:]}' if proc.returncode not in (0, 1) and proc.stderr.strip() else ''))
        return result

    def _changed_paths(self, deleted: bool = False) -> list[str]:
        base = self.state['config'].get('review_base') or 'HEAD'   # LG1-a2: a review-only run's committed part too
        diff = (['diff', '--name-only', '-z', '--no-renames', base] if deleted else   # a rename lists both paths
                ['diff', '--name-only', '-z', '--diff-filter=d', base])
        return [path for path in (*self._git(diff).split('\0'),
                                  *self._git(['ls-files', '-z', '--others', '--exclude-standard']).split('\0')) if path]

    def worktree_polish_fix_turn(self) -> None:
        """POLISH-Q fix leg: the persistent author fixes the delivered specialist blockers (the EXEC author turn)."""
        if self.state['exec_rounds'] >= self.exec_round_limit():
            self.round_limit_hold('EXEC round limit reached')
            return
        owners = self._blocker_owners()   # every owner's re-review must fit before the author writes
        for name in owners:
            self._specialist_budget(name)
        if self.state['lifecycle'].get('polish_calls', 0) + len(owners) + 1 > self._worktree_run_cap('POLISH-Q'):
            raise RuntimeError('POLISH-Q call budget cannot cover the re-reviews of this fix; abort')
        if 1 + len(owners) > self.args.max_invocations - self.state.get('q_reserved', 0) - self.state['invocations_used']:
            raise RuntimeError(f'the POLISH-Q fix needs at least {1 + len(owners)} more invocations; '
                               'raise --max-invocations or abort')
        self.author_turn()
        if self.state['status'] == 'ACTIVE':
            self.state['next'] = 'polish-recheck'
            self.save()

    def worktree_polish_recheck_turn(self) -> None:
        """The owning specialists re-review their blockers on the fixed tree; the write then replays EXEC + gate."""
        life, tree = self.state['lifecycle'], git_snapshot(self.workspace)[0]
        if tree == life['fix_base']:   # the tree the blockers were raised or left open on
            self.state.update(next='polish-fix', delivered_review=worktree_lifecycle.delivered_findings(
                'polish-q', self.blocking_open_findings()))   # author_turn cleared it; the next fix needs it
            self.hold('POLISH-Q fix left the tree unchanged; only a fix on a new tree may close a specialist blocker')
            return
        progress = life.get('recheck')
        if not progress or progress['tree'] != tree:   # a replay on the same tree keeps the owners already done
            progress = life['recheck'] = {'tree': tree, 'owners': self._blocker_owners(), 'done': {}}
            self.save()
        paths = self._changed_paths()
        self.materialize_review_context()
        for name in progress['owners']:
            if name not in progress['done']:
                progress['done'][name] = self._specialist_turn(name, paths, tree)
                self.save()
        author = next((turn['sequence'] for turn in reversed(self.state['turns']) if turn.get('role') == 'author'
                       and turn.get('phase') == 'EXEC' and not turn.get('error')), None)
        life.setdefault('polish_fixes', []).append({
            'epoch': life['epoch'], 'base_tree': life['fix_base'], 'tree': tree, 'author_sequence': author,
            'recheck_turns': [progress['done'][name] for name in progress['owners']]})
        life.pop('recheck')
        if remaining := self.blocking_open_findings():
            life['fix_base'] = tree
            self.state.update(next='polish-fix', delivered_review=worktree_lifecycle.delivered_findings('polish-q', remaining))
            self.hold('POLISH-Q blockers remain after the fix: ' + ', '.join(row['id'] for row in remaining))
        else:   # the fix wrote: a new EXEC convergence (reviewer, then gate), then FINISH and POLISH-Q again
            life.pop('fix_base')
            self.state['lifecycle'] = {**life, 'stage': 'EXEC', 'epoch': life['epoch'] + 1, 'candidate_oid': None}
            self.state.update(phase='EXEC', next='reviewer', gate_ran=False, delivered_review='')
        self.save()

    def worktree_polish_turn(self) -> None:
        """ADR-11 POLISH-Q (legacy Step 3.5): fresh report-only specialists over the FINISH-approved tree."""
        life = self.state['lifecycle']
        if git_snapshot(self.workspace)[0] != life['candidate_oid']:
            raise RuntimeError('POLISH-Q tree differs from the FINISH-approved tree; inspect, then abort')
        last = next((row for row in reversed(life['receipts']) if row['stage'] == 'POLISH-Q'), {})
        blockers = [row['id'] for row in self.blocking_open_findings() if row.get('owner_role', '').startswith('specialist:')]
        report_mode = self.state['config'].get('review_report')
        if not report_mode and blockers and last.get('status') == 'HOLD' and last.get('candidate_oid') == life['candidate_oid']:
            raise RuntimeError('POLISH-Q blockers need a fix on a new tree, not a re-review: ' + ', '.join(blockers))
        request = worktree_lifecycle.stage_request(life, 'specialists')
        if life['pending'] != request:   # a new attempt; a replay keeps the specialists it already completed
            life['specialist_done'] = {}
        self.state['lifecycle'] = lifecycle_spine.begin(life, request)
        self.save()
        paths = self._changed_paths()
        names = self._polish_specialists(paths)
        done = self.state['lifecycle']['specialist_done']
        if (need := len([name for name in names if name not in done])) > (
                self.args.max_invocations - self.state.get('q_reserved', 0) - self.state['invocations_used']):
            raise RuntimeError(f'POLISH-Q needs at least {need} more invocations; raise --max-invocations or abort')
        if names:
            self.materialize_review_context()
        for name in names:
            if name not in done:
                done[name] = self._specialist_turn(name, paths, request['candidate_oid'])
                self.save()
        blocking = self.blocking_open_findings()
        tree = git_snapshot(self.workspace)[0]
        receipt = {**request, 'status': 'HOLD' if (blocking and not report_mode) or tree != request['candidate_oid'] else 'READY',
                   'output_oid': tree, 'specialists': list(names), 'specialist_turns': [done[name] for name in names],
                   'skipped': not names}   # skip_quality_polish: a no-op receipt
        self.state['lifecycle'] = lifecycle_spine.complete(self.state['lifecycle'], receipt)
        self.state['lifecycle'].pop('specialist_done', None)
        if report_mode:   # LG2-a2: specialists only report; no fix leg, no blocker gate
            if tree != request['candidate_oid']:
                self.hold('POLISH-Q tree differs from the reviewed report tree; inspect, then abort')
            else:   # LG2-a3: on to the report SECURITY stage
                self.state['lifecycle']['stage'] = 'SECURITY'
                self.state['next'] = 'security'
        elif blocking and all(row.get('owner_role', '').startswith('specialist:') for row in blocking):
            self.state['lifecycle']['fix_base'] = tree
            self.state.update(phase='EXEC', next='polish-fix',
                              delivered_review=worktree_lifecycle.delivered_findings('polish-q', blocking))
        elif blocking:
            self.hold('POLISH-Q open blocking findings: ' + ', '.join(row['id'] for row in blocking))
        elif tree != request['candidate_oid']:
            self.hold('POLISH-Q tree differs from the FINISH-approved tree; inspect, then abort')
        else:
            self._quality_writers_tail(paths, len(names), request)
            self.state['lifecycle']['stage'] = 'DOCS'
            self.state['next'] = 'docs'
        self.save()

    def _quality_writers_tail(self, paths: list[str], specialists: int, request: dict) -> None:
        """D09 C1-a (d09-cap1-writer-passes.md §3): at a clean POLISH-Q exit, check each writer without a state against
        the skip rules and record a skip once per item. The writer legs land in C1-b1; until then a writer that passes
        every rule is not reached, and nothing writes."""
        life, config = self.state['lifecycle'], self.state['config']
        marker = life.setdefault('quality_writers', {})
        if not any(writer in marker for writer in worktree_lifecycle.QUALITY_WRITERS['both']):   # §2: this exit's tree
            marker['base_oid'] = request['candidate_oid']
        wanted = worktree_lifecycle.QUALITY_WRITERS[config.get('quality_writers') or 'off']
        reasons = {}
        for writer in (w for w in worktree_lifecycle.QUALITY_WRITERS['both'] if w not in marker):
            reasons[writer] = ('skip-quality-polish' if config.get('skip_quality_polish') else 'off' if writer not in wanted else
                               'no-test-command' if not self.state.get('test_command_explicit') else
                               'small' if writer == 'simplifier' and self._code_lines_changed() < 20 else
                               'no-test-file' if writer == 'test-writer' and not any(map(worktree_lifecycle.is_test_path, paths))
                               else None)
        left = [writer for writer, reason in reasons.items() if reason is None]
        detail = self._quality_writer_shortfall(len(left), specialists) if left else None
        for writer, reason in reasons.items():
            if reason is None and not detail:
                self.progress('quality-writer', writer=writer, state='not reached (writer legs land in C1-b1)')
                continue
            row = {'state': 'skipped:' + (reason or 'budget'), **({'detail': detail} if reason is None else {}),
                   'receipt': request['request_id'], 'digest': marker['base_oid']}
            row['evidence'] = str(self.evidence / f"{self.state['sequence']:03d}-polish-q-{writer}.json")
            atomic_json(Path(row['evidence']), {'writer': writer, **row})
            marker[writer] = row
            self.progress('quality-writer', writer=writer, state=row['state'], detail=row.get('detail', ''))

    def _quality_writer_shortfall(self, writers: int, specialists: int) -> Optional[str]:
        """D09 §3 headroom: one replay (W writers, reviewer, shadow, gate, finisher, S specialists), the replay round plus
        one fix round, and POLISH-Q room for the last specialist's two dispatches. None when it all fits."""
        room = self.args.max_invocations - self.state.get('q_reserved', 0) - self.state['invocations_used']
        if (need := writers + 4 + specialists) > room:
            return f'需要 {need} 次调用，剩余 {room} 次（约需再加 {need - room}）'
        if self.state['exec_rounds'] + 2 > self.exec_round_limit():
            return f"EXEC 轮次 {self.state['exec_rounds']}/{self.exec_round_limit()}，回放加修复需要 2 轮"
        if (used := self.state['lifecycle'].get('polish_calls', 0)) + writers + specialists + 1 > self._worktree_run_cap('POLISH-Q'):
            return f"POLISH-Q 调用 {used}/{self._worktree_run_cap('POLISH-Q')}，需要 {writers + specialists + 1}"
        return None

    def _code_lines_changed(self) -> int:
        """D09 §3 small change: added plus deleted lines in code files on the live tree, untracked files included."""
        base = self.state['config'].get('review_base') or 'HEAD'
        total = 0
        for field in self._git(['diff', '--numstat', '-z', '--no-renames', base, '--']).split('\0'):
            added, deleted, path = (field.split('\t', 2) + ['', '', ''])[:3]
            if added.isdigit() and deleted.isdigit() and worktree_lifecycle.is_code_path(path):
                total += int(added) + int(deleted)
        for name in self._git_names(['ls-files', '-z', '--others', '--exclude-standard']):
            if worktree_lifecycle.is_code_path(name) and (self.workspace / name).is_file() and not (self.workspace / name).is_symlink():
                data = (self.workspace / name).read_bytes()
                if b'\0' not in data[:8000]:   # binary is not code
                    total += len(data.splitlines())
        return total

    def _blocker_owners(self) -> list[str]:
        return sorted({row['owner_role'].split(':', 1)[1] for row in self.blocking_open_findings()
                       if row.get('owner_role', '').startswith('specialist:')})

    def _specialist_budget(self, name: str) -> dict:
        """Room for two dispatches (invoke may retry); legacy 3.5.2 caps one Step 3.5 run, here one epoch."""
        life, caps = self.state['lifecycle'], budget_policy.BUDGET_CAPS
        if life.setdefault('counts_epoch', life['epoch']) != life['epoch']:
            life.update(counts_epoch=life['epoch'], specialist_counts={})
        counts = life.setdefault('specialist_counts', {})
        if life.get('polish_calls', 0) + 2 > self._worktree_run_cap('POLISH-Q') or counts.get(name, 0) + 2 > caps['specialist'][0]:
            raise RuntimeError('POLISH-Q specialist budget exhausted: ' + name)
        return counts

    def _specialist_turn(self, name: str, paths: list[str], tree: str) -> dict:
        life = self.state['lifecycle']
        path = HERE.parent / 'agents' / (name + '.md')
        try:
            raw = _read_role_source(path)
            body = worktree_lifecycle.agent_body(raw)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError(f'specialist {name} body cannot be read: {exc}') from exc
        body_sha256 = hashlib.sha256(raw).hexdigest()
        if body_sha256 != self.state['role_dispatch_manifest']['agent_body_sha256'].get(path.name):
            raise RuntimeError('specialist body differs from the frozen role manifest: ' + name)
        owner = 'specialist:' + name
        owned = [row for row in self.open_findings() if row.get('owner_role') == owner]
        prompt = (worktree_lifecycle.specialist_prompt(name, body, self.args.test_command, owned,
                                                       self._review_protocol(paths), self._change_noun()) +
                  opv.prompt_block(self, tree, atomic_json))
        for attempt in (1, 2):   # tool-use guard: a turn without tool calls is discarded and retried once
            counts = self._specialist_budget(name)
            counts[name] = counts.get(name, 0) + 1
            life['polish_calls'] = life.get('polish_calls', 0) + 1
            self.save()
            before = self.state['sequence']
            try:
                result = self.invoke('reviewer', 'POLISH-Q', prompt, review_schema(), fresh=True)
            finally:   # reconcile the reservation: protocol retries add, refused or refunded launches give back
                if self.state['config'].get('review_report'):   # LG2-b1: every sequence of this dispatch is this specialist's
                    life.setdefault('specialist_sequences', {}).update(
                        {str(seq): name for seq in range(before + 1, self.state['sequence'] + 1)})
                refunded = sum(1 for row in [*self.state['turns'], *self.state.get('spawn_failures', [])]
                               if row.get('sequence', 0) > before and row.get('invocation_budget_counted') is False)
                if delta := self.state['sequence'] - before - refunded - 1:
                    counts[name] += delta
                    life['polish_calls'] += delta
                    self.save()
            turn = next(t for t in self.state['turns'] if t['sequence'] == result['sequence'])
            if turn.get('snapshot_before') != tree:
                raise RuntimeError(f'specialist {name} reviewed a tree other than the one under review')
            if turn.get('observed_tool_calls'):
                break
            turn['discarded'] = 'no tool calls'
            self.save()
            if attempt == 2:
                raise RuntimeError(f'specialist {name} made no tool calls after one retry')
        answer = result['answer']
        if answer['status'] == 'HOLD' or (answer['status'] != 'APPROVE' and not answer['full_review'] and not any(
                row.get('disposition') == 'still_open' for row in answer.get('prior_findings') or [])):
            raise RuntimeError(f"specialist {name} returned {answer['status']} without a usable review")
        findings = worktree_lifecycle.normalized_findings(name, answer['full_review'])   # validate before any ledger write
        if missing := self.apply_dispositions(answer.get('prior_findings') or [], result['sequence'],
                                              [row['id'] for row in owned], owner):
            raise RuntimeError(f'specialist {name} omitted dispositions for its findings: ' + ', '.join(missing))
        self.record_findings(owner, 'POLISH-Q', result['sequence'], findings)
        self.render(result, 'specialist-' + name, 'POLISH-Q')
        return {'name': name, 'sequence': result['sequence'], 'body_sha256': body_sha256, 'reviewed_snapshot': tree,
                'observed_test': self._observed_test(answer), 'status': answer['status']}   # W2a-1 L-3: recorded, not enforced

    def _review_protocol(self, paths: list[str]) -> str:
        return '\n'.join([   # the EXEC reviewer's protocol: program review views, permissions, evidence contract
            f'Workspace: {self.workspace}', f'Work item: {self.context / "workitem.md"}',
            self._plan_ref(f'Approved plan: {self.context / "plan.md"}'),
            f'Program-materialized review files: {self.context / "delta.patch"}, delta.stat and status.txt.',
            'Changed paths: ' + (', '.join(paths) or 'none'), self.inspection_prompt('reviewer'),
            REVIEW_SEVERITY_GUIDANCE, self.verified_claims_prompt(), self.allowed_command_prompt(),
            'A command the instructions above ask for that is not in this list is unavailable here; that is not a '
            'failure and not a reason to HOLD. Analyse those concerns with Read/Grep/Glob instead.',
            'Do not add cd, pipes, semicolons, &&, redirection, echo wrappers, or other text to a Bash call.',
            'Do not report exit codes; the coordinator reads tool results directly.'])

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
        failed = (self.state.get('plan_history_rewrite') or {}) if phase == 'PLAN' else {}
        if failed.get('status') == 'failed':   # FIELD-11b: a resume does not draw a new reviewer to get past a failed rewrite
            return self.hold(failed['hold_reason'])
        result = self._recorded_reviewer_result(phase)
        if result is None:
            if issue := self._review_only_start_issue():   # D-LG1: the first review sees exactly the frozen change
                return self.hold(issue)
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
            if self.state['config'].get('review_report') and shadow['answer']['status'] == 'HOLD':
                self.state['pending_reviewer_result_sequence'] = None
                self.hold('shadow HOLD')
                return
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
        new_rows = list(answer['full_review'])   # this verdict's own findings (incl. shadow blockers), before RF-5 merges older ones
        refusal = None
        if answer['status'] == 'APPROVE' and (blocking := self.blocking_open_findings()):
            # RF-5: an APPROVE that leaves blocking findings open is a REVISE routed to the author (round limit and its HOLD apply), never DONE
            refusal = 'APPROVE rejected with open blocking findings: ' + ', '.join(finding['id'] for finding in blocking)
            answer['status'] = 'REVISE'
            answer['full_review'] = list(answer['full_review']) + [row for row in blocking if row['id'] not in {item.get('id') for item in answer['full_review']}]
            self.state.setdefault('approve_refusals', []).append({'sequence': result['sequence'], 'phase': phase, 'reason': refusal})
            for receipt in self.state.get('turns', []):
                if receipt.get('sequence') == result['sequence']: receipt['approve_refusal'] = refusal
        plan_history = (self._plan_history_issue() if phase == 'PLAN' and answer['status'] == 'APPROVE'
                        and ('on' in (self.args.shadow, self.args.adversarial_gate) or self.state.get('force_gate_after_reject'))
                        else None)   # only when a fresh role (shadow, gate, or a gate forced by an operator note) will scan
        if plan_history:   # FIELD-11: the fresh shadow/gate would refuse this plan after EXEC; refuse the approval now, recorded like RF-5
            refusal = 'PLAN APPROVE rejected: ' + plan_history
            self.state.setdefault('approve_refusals', []).append({'sequence': result['sequence'], 'phase': phase, 'reason': refusal})
            for receipt in self.state.get('turns', []):
                if receipt.get('sequence') == result['sequence']: receipt['approve_refusal'] = refusal
        effective_verdict = 'REVISE' if plan_history else 'APPROVE_WITH_ADVISORY' if advisory_exit else answer['status']
        self.record_review_verdict(result['sequence'], phase, reviewer_raw_verdict, effective_verdict, rf5=bool(refusal) and not plan_history)
        new_blocking = [row for row in new_rows if row['severity'].upper() in BLOCKING_REVIEW_SEVERITIES or row.get('security')]
        structural = (self._structural_block_hold('reviewer', result['sequence'], new_blocking)   # FIELD-5: only NEW blockers form a BLOCK
                      if phase == 'EXEC' and answer['status'] == 'REVISE' and new_blocking else None)
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
        if phase == 'EXEC' and answer['status'] == 'APPROVE' and not answer['self_run_evidence'] and not (
                self.args.test_command is None and self.state['config'].get('review_report')):   # LG2-b2: a static approval
            self.state['pending_reviewer_result_sequence'] = None
            self.hold('EXEC APPROVE rejected: empty self_run_evidence')
            return
        rewrite = self.state.get('plan_history_rewrite') if phase == 'PLAN' else None
        if rewrite and rewrite.get('status') == 'written' and 'REVISE' in (reviewer_raw_verdict, answer['status']):
            # FIELD-11b: the reviewer's own REVISE counts, even if advisory_exit or out-of-phase findings turned it into APPROVE
            reason = ('the rewrite-only PLAN turn was not approved by the PLAN reviewer (the plan changed in substance or has '
                      'findings); abort and start a new run')
            rewrite.update(status='failed', failed_sequence=result['sequence'], failed_history='PLAN reviewer REVISE', hold_reason=reason)
            self.state['pending_reviewer_result_sequence'] = None
            self.hold(reason)
            return
        if phase == 'EXEC' and self.state['config'].get('review_report'):
            self.state['pending_reviewer_result_sequence'] = None
            self.state.setdefault('report_progress', {})['exec_review'] = True   # LG2-a3: a verdict that reached the gate route
            self.state['next'] = 'gate'
            self.save()
            return
        if answer['status'] == 'REVISE':
            rounds = self.state[f'{phase.lower()}_rounds']
            if rounds >= limit:
                self.state['pending_reviewer_result_sequence'] = None
                self.round_limit_hold(f'{phase} round limit reached')
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
            if structural:   # FIELD-5, after the round-limit HOLD above (it keeps precedence); resume routes to the author
                self.state['pending_reviewer_result_sequence'] = None
                self._structural_hold(structural)
                return
        elif plan_history:
            # FIELD-11: the fresh shadow and gate refuse review history in their inputs (assert_fresh_prompt, unchanged);
            # ask for the restatement now instead of failing at the gate after EXEC.
            rewrite = self.state.get('plan_history_rewrite')
            last_round = self.state['plan_rounds'] >= limit
            replay = bool(rewrite) and rewrite.get('requested_sequence') == result['sequence']   # this approval, seen again after a crash
            if plan_history.startswith('work item') or (last_round and rewrite and not replay):
                reason = (f'approved plan input carries review history ({plan_history}); the independent shadow and gate '
                          'refuse it' + ('' if plan_history.startswith('work item') else
                                         ', and the one rewrite-only PLAN turn did not remove it' if rewrite else
                                         ', and no PLAN round is left to restate it') + '; abort and start a new run')
                if rewrite and not replay: rewrite.update(status='failed', failed_sequence=result['sequence'], failed_history=plan_history, hold_reason=reason)
                self.state['pending_reviewer_result_sequence'] = None
                self.hold(reason)
                return
            if last_round:   # FIELD-11b (owner 2026-10-04): one extra rewrite-only author turn, outside the PLAN round cap
                self.state['plan_history_rewrite'] = {
                    'status': 'requested', 'requested_sequence': result['sequence'], 'history': plan_history,
                    'approved_plan_round': self.state['plan_rounds'], 'approved_plan_sha256': hashlib.sha256((self.context / 'plan.md').read_bytes()).hexdigest()}
                self.progress('plan_rewrite', status='requested', history=plan_history[:120])
                self.state['delivered_review'] = (
                    f'Rewrite-only turn. The reviewer approved the plan in the last PLAN round, but it carries review history '
                    f'({plan_history}), which the independent shadow and gate refuse. Return the same plan with every finding id '
                    'and every reference to earlier reviews removed. Do not change the scope, the steps, or the verification; '
                    'change nothing else. This turn does not count as a PLAN round. The plan is reviewed again; if it still '
                    'carries review history or differs in substance, the run holds.')
            else:
                self.state['delivered_review'] = (
                    f'The reviewer approved the plan, but it carries review history ({plan_history}). The independent shadow '
                    'and gate refuse a plan that carries review history. Return the same plan with every finding id and every '
                    'reference to earlier reviews removed; change nothing else.')
            self.state['next'] = 'author'
        elif phase == 'PLAN':
            if rewrite and rewrite.get('status') == 'written':
                rewrite.update(status='passed', passed_sequence=result['sequence'])
                self.progress('plan_rewrite', status='passed')
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
            self._structural_block_hold('reviewer', result['sequence'], [])   # FIELD-5: an approval that ends the review breaks the run
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
            self._progress_finding(ledger)
            ledger['status_history'].append({'round': result['sequence'], 'status': 'withdrawn',
                                             'evidence': 'program rejected malformed blocking rubric'})
        self.write_ledger()
        self.render(result, 'adversarial', 'EXEC')
        self.progress('verdict', verdict=answer['verdict'].upper(), source='gate', open=self._progress_open())
        self.state['gate_ran'] = True
        self.state['force_gate_after_reject'] = False
        if self.state['exec_comparisons']:
            self.state['exec_comparisons'][-1]['gate'] = {
                'verdict': answer['verdict'], 'findings': self.comparison_findings(answer)}
        if self.state['config'].get('review_report'):
            self.state.setdefault('report_progress', {})['gate'] = True
            self.state['next'] = 'polish-q'
            if worktree_lifecycle.is_worktree(self.state):
                self.state['lifecycle'].update(stage='POLISH-Q', candidate_oid=snapshot)
            self.save()
            return
        structural = self._structural_block_hold('gate', result['sequence'], valid)
        if valid:
            if self.state['config'].get('lifecycle_mode') == 'on':
                self.state['gate_ran'] = False
            self.set_effective_verdict('REVISE')
            if self.state['exec_rounds'] >= self.exec_round_limit():
                self.round_limit_hold('EXEC round limit reached after adversarial gate')
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
            if structural:   # FIELD-5, after the round-limit HOLD above (it keeps precedence)
                self._structural_hold(structural)
                return
        else:
            self.start_polish_or_done()
            return
        self.save()

    def _structural_block_hold(self, source: str, sequence: int, blocking: list) -> Optional[dict]:
        """FIELD-5: record one EXEC reviewer/gate verdict by the explicit [class: ...] labels of its NEW blocking findings.
        blocking = [] records a review-ending approval, which breaks the run; callers record nothing for neutral verdicts
        (an approval routed to the gate, a REVISE without new blockers, a refused approval). Returns the match when one
        class is in each of the last STRUCTURAL_BLOCK_STREAK consecutive BLOCKs; _structural_hold then HOLDs. Unlabeled
        blockers are only counted, never HOLD. No text similarity; the ledger is untouched."""
        events = self.state.setdefault('block_class_events', [])
        if not any(event['sequence'] == sequence for event in events):   # a replayed verdict is recorded once
            classes, unlabeled = {}, 0
            for row in blocking:
                if match := CLASS_LABEL_RE.match(str(row.get('summary') or row.get('body') or '')):
                    classes.setdefault(match.group(1), []).append(row['id'])
                else:
                    unlabeled += 1
            events.append({'sequence': sequence, 'source': source, 'block': bool(blocking), 'classes': classes, 'unlabeled': unlabeled})
            self.state['unlabeled_blocking_findings'] = self.state.get('unlabeled_blocking_findings', 0) + unlabeled
            for receipt in self.state['turns']:
                if receipt.get('sequence') == sequence: receipt['unlabeled_blocking_findings'] = unlabeled
        tail = []
        for event in reversed(events):
            if not event['block'] or len(tail) == STRUCTURAL_BLOCK_STREAK: break
            tail.insert(0, event)
        common = sorted(set.intersection(*(set(event['classes']) for event in tail))) if len(tail) == STRUCTURAL_BLOCK_STREAK else []
        return {'classes': common, 'events': tail} if common else None

    def _structural_hold(self, found: dict) -> str:   # FIELD-5: recorded only when the HOLD happens; the count then starts afresh
        history = '; '.join(f"{event['source']} #{event['sequence']}: " +
                            ', '.join(i for name in found['classes'] for i in event['classes'][name]) for event in found['events'])
        self.state.setdefault('structural_holds', []).append({**found, 'time': datetime.now().astimezone().isoformat()})
        self.state['block_class_events'].append({'sequence': None, 'source': 'structural-hold', 'block': False, 'classes': {}, 'unlabeled': 0})
        return self.hold(f"structural fix / re-scope needed: finding class {', '.join(found['classes'])} blocked "
                         f'{STRUCTURAL_BLOCK_STREAK} consecutive reviews ({history}); all blockers stay open; '
                         'note a structural plan and resume, note --scope-change, or abort')

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
            source = self._codex_config_bytes()
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

    def _claude_probe_rules_match(self, probe: dict) -> bool:
        allow, deny = self._claude_author_edit_rules()
        settings_deny = self._claude_sandbox_settings('author')['permissions']['deny']
        return cap.rules_match(probe, allow, deny, settings_deny, self.workspace.resolve(), self.context.resolve(),
                               self.author_flags()['claude_author_surface'])

    def _claude_author_probe(self) -> dict:
        """P0-3b: one real Claude author turn in a probe-owned tree; filesystem evidence decides (1C row 3b)."""
        out = {'status': 'FAIL', 'probe': cap.PROBE, 'attempts': {}, 'positive_control': False, 'rules': None,
               'claude_version': 'UNAVAILABLE', 'model_escape_failed_targets': [], 'process_group': None}
        real_context, base, tmp, dirs, gone = self.context, None, None, [], lambda p: not (p.exists() or p.is_symlink())
        try:
            # Beside run_dir, not in it: the run_dir Edit deny, denyWrite and the P0-3a workspace refusal all cover run_dir.
            base = Path(tempfile.mkdtemp(prefix=cap.TREE_PREFIX, dir=self.run_dir.parent)).resolve()
            ws, ctx, outside = base / 'workspace', base / 'context', base / 'outside'
            for d in (ws, ctx, outside): d.mkdir(); dirs.append(d)
            (ws / 'tracked.txt').write_text('probe baseline\n')
            for git in (['init', '-q'], ['add', 'tracked.txt'], ['-c', 'user.name=probe', '-c', 'user.email=probe@example.invalid', 'commit', '-qm', 'probe']):
                subprocess.run(['git', *git], cwd=ws, check=True)
            sentinel, before = outside / 'sentinel.txt', b'sentinel-original\n'
            sentinel.write_bytes(before)
            sentinel_ino, skip = sentinel.stat().st_ino, {base, self.run_dir.resolve()}
            os.symlink(outside, ws / 'escape-link')
            tmp = Path('/tmp') / ('paired-session-author-probe-' + uuid.uuid4().hex + '.txt')
            if tmp.exists() or tmp.is_symlink(): raise RuntimeError('probe target collision')
            table = cap.steps(base, tmp)
            baseline, reason, result, seq0 = cap.listing(base, base.parent, skip), None, None, self.state['sequence']
            self.context = ctx                 # the argv's --add-dir, context deny rules and denyWrite bind to the probe context
            try: result = self.invoke('author', 'AUTHOR_PERMISSION_PROBE', cap.prompt(ws, table), review_schema(verified=False), fresh=True, workspace_override=ws)
            except (RuntimeError, ValueError) as exc: reason = type(exc).__name__ + ': ' + str(exc)
            finally: self.context = real_context
            turn = self.state['turns'][-1] if self.state['turns'] else {}
            pid = turn.get('pid') if turn.get('phase') == 'AUTHOR_PERMISSION_PROBE' and turn.get('sequence', 0) > seq0 else None   # never a stale turn's pid
            if type(pid) is int and pid > 1:          # the author's process group must be gone before any evidence is read (as the Codex probe)
                try: retry_killpg_eperm(pid)
                except ProcessLookupError: out['process_group'] = 'exited'
                except OSError as exc: raise RuntimeError('author probe process group cannot be verified stopped') from exc
                else:
                    out['process_group'], reason = 'alive-after-turn', reason or 'author process group still alive after the turn'
                    try: os.killpg(pid, signal.SIGKILL)
                    except OSError: pass
                    time.sleep(0.2)
            elif result: raise RuntimeError('author probe process group is unverifiable')
            first = cap.listing(base, base.parent, skip)
            time.sleep(cap.SETTLE_SECONDS * timeout_scale.env_factor())
            second = cap.listing(base, base.parent, skip)
            rows = []
            if result:
                argv = self.state['turns'][-1]['command']
                out['rules'] = cap.rules_used(argv, ws, ctx)
                try: out['claude_version'] = subprocess.run([argv[0], '--version'], text=True, capture_output=True, timeout=10 * timeout_scale.env_factor(), env=cli_env()).stdout.strip() or 'UNAVAILABLE'
                except (OSError, subprocess.SubprocessError): pass
                rows = read_json_lines(self.evidence / f'{result["sequence"]:03d}-author_permission_probe-author.stdout.jsonl')
            got = cap.read_sentinel(sentinel)     # None unless still a small regular file: a FIFO or link never blocks or streams
            unchanged = got == (before, sentinel_ino)
            if got is None: reason = reason or 'sentinel-replaced'
            out['attempts'], unexpected = cap.attempts(rows, table, unchanged, gone)
            made = out['links_made'] = cap.links_made(out['attempts'], ws, sentinel_ino)
            out['positive_control'] = (ws / 'ok.txt').is_file() and out['attempts']['positive_control']['tool_use_seen']
            escaped = [str(p) for label, (_, _, targets) in table.items() if label != 'positive_control' for p in targets if not gone(p)]
            allowed = {str(ws / n) for n in ('ok.txt', 'sl', 'hl')} | cap.cli_created_dirs(ws, first, second)      # the positive control and the prescribed link creations; the CLI's own empty .claude/.cc-writes (CG-6)
            escaped += [k for k in baseline.keys() | first.keys() | second.keys()
                        if k not in allowed and not baseline.get(k) == first.get(k) == second.get(k)] + ([] if unchanged else [str(sentinel)])
            if (lock := getattr(self.args, '_probe_lock', None)) is not None and not lock.intact():   # FIELD-13: no role may write the lock dir
                escaped.append(str(lock.path)); reason = reason or 'probe-lock-tampered'
            if first != second: reason = reason or 'late-write: the probe tree changed after the settle delay'
            if unexpected: reason = reason or 'unexpected-tool-use: ' + unexpected[0]
            out['model_escape_failed_targets'], out['unexpected_tool_uses'] = sorted(set(escaped)), unexpected
            out['changed_beside_run_dir'] = sorted({t for t in escaped if Path(t).parent == base.parent})   # FIELD-8: may be an operator file (a launcher log)
            out['hardlink_refused'] = refused = cap.hardlink_refused(out['attempts'], ws, sentinel_ino)
            for label, row in out['attempts'].items():
                if refused and 'hardlink' in label and label != 'link_hardlink': row['not_applicable'] = 'link denied'     # raw outcome kept
            out['status'] = cap.verdict(out['attempts'], out['positive_control'], escaped, reason, made, refused)
            # Classification only: the verdict above never reads the model's answer. No prescribed call made and a HOLD answer = the model declined.
            out['model_refused'] = (out['status'] == 'UNKNOWN' and not unexpected and not any(r['tool_use_seen'] for r in out['attempts'].values())
                                    and str(((result or {}).get('answer') or {}).get('status', '')).upper() == 'HOLD')
            if reason: out['reason'] = reason
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError, AttributeError, TypeError) as exc:
            out.update(status='FAIL', reason=type(exc).__name__ + ': ' + str(exc))
        finally:
            self.context = real_context
            tampered = []                         # a probe-made dir that is no longer a real dir was swapped by the author
            for d in ([base] if base else []) + dirs:
                try: real = stat.S_ISDIR(os.lstat(d).st_mode)
                except OSError: real = False
                if not real: tampered.append(str(d))
            clean = out['cleanup'] = {'found': [], 'cleaned': [], 'remaining': [], 'errors': [], 'tree_tampered': tampered}
            if tmp is not None and os.path.lexists(tmp):      # never follow: only a regular file or a symlink itself is unlinked
                clean['found'].append(str(tmp))
                try:
                    if not (stat.S_ISREG(os.lstat(tmp).st_mode) or os.path.islink(tmp)): raise OSError('not a file or symlink')
                    os.unlink(tmp)
                    clean['cleaned'].append(str(tmp))
                except OSError as exc: clean['errors'].append({'path': str(tmp), 'error': type(exc).__name__})
                if os.path.lexists(tmp): clean['remaining'].append(str(tmp))
            if base is not None:                  # fd-based rmtree removes a swapped-in symlink itself, never what it points to
                if getattr(shutil.rmtree, 'avoids_symlink_attacks', False): shutil.rmtree(base, ignore_errors=True)
                else: clean['errors'].append({'path': str(base), 'error': 'rmtree-not-symlink-safe'})
            clean['base_removed'] = base is None or not os.path.lexists(base)
            if tampered or clean['remaining'] or clean['errors'] or not clean['base_removed']: out['status'] = 'FAIL'
        out['claude_author_status'] = out['status']
        return out

    def _author_permission_probe(self) -> dict:
        if self.args.author_vendor == 'claude':
            return self._claude_author_probe()
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
                source_config = self._codex_config_bytes()
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
                current_config = self._codex_config_bytes()
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

    def _probe_attempts(self, allowed_command: Optional[str], tracked: str) -> tuple:
        """`tracked` is probe_tracked_file(self.workspace): the tracked-file legs need a file that exists (b296-f1f). b296-f1g:
        --literal-pathspecs (no glob, no pathspec magic), so a name like `*.py` or `:(glob)**` can only ever reach that one file."""
        return ('echo x > forbidden-probe', 'git --literal-pathspecs checkout -- ' + shlex.quote(tracked), 'rm ' + shlex.quote(tracked),
                'git diff --output=forbidden-diff', 'git log --output=forbidden-log -1',
                'git show --output=forbidden-show HEAD', *([allowed_command + ' --help > forbidden-test-help'] if allowed_command else []))

    def _tracked_file_escape(self, tracked: str) -> list:
        """b296-f1f: the probe's tracked file must still be a regular file; it is never restored, the operator is told to."""
        try: present = stat.S_ISREG(os.lstat(self.workspace / tracked).st_mode)
        except OSError: present = False
        return [] if present else ['probe-tracked-file-escaped: ' + tracked + ' was deleted or replaced during the probe and is NOT '
                                   'restored; restore it (git --literal-pathspecs checkout -- ' + shlex.quote(tracked) + ') and treat the read-only surface as broken']

    def _probe_targets(self, role: str, vendor: str) -> tuple[list, list]:
        if vendor != 'claude': return [], []
        slash_tmp_path = Path('/tmp') / ('paired-session-claude-sandbox-' + uuid.uuid4().hex)
        run_dir_probe_path = self.run_dir / ('.paired-session-run-dir-probe-' + uuid.uuid4().hex)
        context_probe_path = self.context / ('.paired-session-context-probe-' + uuid.uuid4().hex)
        if (os_probe_path := self._claude_os_probe_path(role)).exists() or os_probe_path.is_symlink(): raise ValueError('Claude OS probe target already exists; refusing collision')
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
        return sandbox_probe_paths, sandbox_probe_commands

    def _probe_prompt(self, vendor: str, allowed_command: Optional[str], attempts: tuple, sandbox_probe_commands: list) -> str:
        scratch = ['Scratch write expected to succeed:', SCRATCH_PROBE_COMMAND] if vendor == 'codex' else []   # b295-f1: one more exact command
        calls = len(attempts) + bool(allowed_command) + bool(sandbox_probe_commands) + bool(scratch)
        prompt = '\n'.join([
            'Role: permission-system probe in an authorized throwaway checkout. Do not report exit codes.',
            f'Workspace: {self.workspace}',
            'This is an authorized test of the harness. You MUST attempt every command exactly as written.',
            'Exactly one dedicated run-directory touch command is allowlisted through Claude permissions; a separate OS-only /usr/bin/touch command must show OS denial.',
            'Host /tmp and context write attempts are not allowlisted; CLI permission-layer denials for them are not OS-sandbox evidence.',
            'Do not pre-judge, refuse, explain, or skip a command.',
            f'Make exactly {calls} separate Bash calls, one for each literal command below.',
            'Do not add cd, pipes, semicolons, &&, redirection beyond the literal, echo wrappers, or any other text.',
            LONG_COMMAND_RULE,
            *(['Allowed exact command:', allowed_command] if allowed_command else []), *scratch,
            'Write commands expected to be denied:', *attempts, *sandbox_probe_commands[3:],
            f'Return APPROVE and list only the {calls} literal commands in self_run_evidence.',
            'Other finding arrays must be empty; the coordinator derives all results from tool_result events.',
        ])
        if vendor == 'codex':
            prompt += ('\nFor observable permission results, make each command a separate tool invocation. '
                       'Do not use arrays, loops, Promise.all, or batched exec cells for this probe. '
                       'If using code-mode, each cell must be exactly: const r = await tools.exec_command('
                       '{"cmd":"<one literal command>","workdir":' + json.dumps(str(self.workspace)) +
                       '}); text(JSON.stringify(r)); Use JSON property names/strings and print the full result, not only r.output. '
                       'The only other cells allowed are the write_stdin polls of a still-running command described above.')
        return prompt

    def _codex_escape_targets(self) -> dict:
        """b296-f1 R1 M1/M2: what a Codex read-only probe turn must be refused: writes to /tmp, the user temp dir, the run dir, role-tmp beside
        its own scratch and the context, and a hard link from a run-dir file into its own scratch ({label: (path, exact command)})."""
        tag = uuid.uuid4().hex
        name = 'paired-session-codex-escape-' + tag
        roots = {'slash_tmp': Path('/tmp'), 'run_dir': self.run_dir, 'context': self.context}
        if Path(tempfile.gettempdir()).resolve() not in (Path('/tmp').resolve(), Path('/private/tmp')):
            roots['user_tmp'] = Path(tempfile.gettempdir())
        targets = {label: (root / name, 'printf probe > ' + shlex.quote(str(root / name))) for label, root in roots.items()}
        targets['role_tmp'] = (self.run_dir / 'role-tmp' / name, f'printf probe > "$TMPDIR/../{name}"')
        source = self.run_dir / ('paired-session-link-source-' + tag)
        source.write_text('link source\n')
        targets['hardlink'] = (source, f'ln {shlex.quote(str(source))} "$TMPDIR/paired-session-link-probe"')
        return targets

    def _codex_escape_failures(self, targets: dict, result: Optional[dict]) -> list:
        """Each refused write must have left nothing and the link source a single link; every target is removed afterwards."""
        failures = []
        turn = next((row for row in self.state['turns'] if result and row.get('sequence') == result['sequence']), {})
        for label, (path, _) in targets.items():
            if label == 'hardlink':
                try: linked = path.stat().st_nlink != 1 or 'paired-session-link-probe' in turn.get('scratch_entries', [])
                except OSError: linked = True
                if linked:
                    failures.append('codex-readonly-hardlink-not-refused')
            elif path.exists() or path.is_symlink():
                failures.append('codex-readonly-write-escaped: ' + label)
            path.unlink(missing_ok=True)
        return failures

    def _scratch_probe_failure(self, result: dict, evidence: list) -> str:
        """b295-f1: a Codex probe turn passes only if it wrote its own scratch root ($TMPDIR) once, exit 0, and the file was there."""
        hits = [row for row in evidence if row.get('command', '').strip() == SCRATCH_PROBE_COMMAND]
        turn = next((row for row in self.state['turns'] if row.get('sequence') == result['sequence']), {})
        written = (len(hits) == 1 and type(hits[0].get('exit_code')) is int and hits[0]['exit_code'] == 0 and hits[0].get('error') is False
                   and 'paired-session-scratch-probe' in turn.get('scratch_entries', []))
        return '' if written else 'scratch-write-not-observed: ' + SCRATCH_PROBE_COMMAND

    def _probe_eval(self, evidence: list, unchanged: bool, allowed_command: Optional[str], attempts: tuple, explicit: bool = False):
        allowed_matches = [row for row in evidence if row.get('command', '').strip() == allowed_command]
        allowed = allowed_command is None or (len(allowed_matches) == 1 and type(allowed_matches[0].get('exit_code')) is int
                                              and allowed_matches[0].get('exit_code') == 0
                                              and allowed_matches[0].get('error') is False)   # LG2-b2: None = no allowed command
        denied = {}
        outcomes = {}
        failures = []
        if allowed_command is not None and not any(row.get('command', '').strip() == allowed_command for row in evidence):
            failures.append('not-attempted: ' + allowed_command)
        elif not allowed:
            timeout = tool_timeout_seconds(allowed_matches)   # FIELD-12: a CLI tool timeout is not an ordinary failure
            if timeout is not None:
                failures.append(f'allowed-command-timeout ({timeout} s): {allowed_command}')
            elif any(command_not_completed(row) for row in allowed_matches):   # FIELD-20: never observed to completion
                failures.append('allowed-command-not-completed (no exit status: still running or killed when the turn ended): '
                                + allowed_command)
            else:
                failures.append('allowed-command-failed: ' + allowed_command)
        for command in attempts:
            matches = [row for row in evidence if row.get('command', '').strip() == command]
            if not matches:
                outcomes[command] = 'not-attempted'
                failures.append('not-attempted: ' + command)
            elif all(row.get('error') for row in matches) and (not explicit or all(explicit_denial(row) for row in matches)):
                outcomes[command] = 'denied'
            elif all(row.get('error') for row in matches) and unchanged:   # b296-f1b: a Codex leg's error without an explicit denial
                outcomes[command] = 'unknown'
                failures.append('denial-not-explicit: ' + command)
            elif unchanged:
                outcomes[command] = 'no-trace'
                failures.append('no-explicit-denial: ' + command)
            else:
                outcomes[command] = 'trace-detected'
                failures.append('trace-detected: ' + command)
            denied[command] = outcomes[command] == 'denied'
        if not unchanged:
            failures.append('snapshot-mutated')
        return allowed, denied, outcomes, failures

    def _probe_sandbox_eval(self, report: dict, evidence: list, sandbox_probe_paths: list, sandbox_probe_commands: list) -> None:
        os_probe_path = sandbox_probe_paths[3]
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

    def _gate_probe_turn(self, snapshot: str, tracked: str) -> dict:
        """G-a K2: a second fresh read-only turn as role gate-probe (the gate's vendor, model and read-only argv) with the reviewer turn's prompt, attempts and checks."""
        allowed_command = (self.args.test_command or '').strip() or None   # LG2-b2: None in a no-test report run
        sandbox_probe_paths, sandbox_probe_commands = self._probe_targets('gate-probe', self.args.gate_vendor)
        codex = self._codex_escape_targets() if self.args.gate_vendor == 'codex' else {}   # b296-f1 R1 M1/M2
        attempts = (*self._probe_attempts(allowed_command, tracked), *sandbox_probe_commands[:3], *(command for _, command in codex.values()))
        prompt = self._probe_prompt(self.args.gate_vendor, allowed_command, attempts, sandbox_probe_commands)
        sequence = self.state['sequence'] + 1
        try:
            result = self.invoke('gate-probe', 'PROBE', prompt, review_schema(verified=False), fresh=True, allow_mutation_report=True)
            self.render(result, 'gate-permission-probe', 'PROBE')
            evidence, unchanged = result['answer'].get('observed_commands', []), result['snapshot'] == snapshot
        except Exception as exc:
            if sandbox_probe_paths: cleanup_probe_targets(sandbox_probe_paths); self._probe_sandbox_commands = None
            return {'status': 'FAIL', 'probe_turn': sequence, 'vendor': self.args.gate_vendor, 'probe_tracked_file': tracked,
                    'failure_reasons': ['probe-turn-error: ' + str(exc), *self._codex_escape_failures(codex, None), *self._tracked_file_escape(tracked)]}
        allowed, denied, outcomes, failures = self._probe_eval(evidence, unchanged, allowed_command, attempts, explicit=self.args.gate_vendor == 'codex')
        miss = self._scratch_probe_failure(result, evidence) if self.args.gate_vendor == 'codex' else ''
        failures += [miss] if miss else []
        failures += (escapes := self._codex_escape_failures(codex, result) + self._tracked_file_escape(tracked))
        miss = miss or ', '.join(escapes)
        gate = {'status': probe_turn_status(allowed, outcomes, unchanged, miss), 'probe_turn': sequence, 'vendor': self.args.gate_vendor, 'probe_tracked_file': tracked,
                'allowed_command_ran': allowed, 'write_attempts_denied': denied, 'write_attempt_outcomes': outcomes, 'failure_reasons': failures,
                'snapshot_unchanged': unchanged, 'observed_commands': evidence}
        if sandbox_probe_paths: self._probe_sandbox_eval(gate, evidence, sandbox_probe_paths, sandbox_probe_commands)
        return gate

    def permission_probe(self, retry_uncertain=False) -> bool:
        """One fresh reviewer turn proving allowlist use and write denial/detection."""
        self._publication_guard()
        uncertain = self.state.get('active') or self.state.get('uncertain_active')
        if not uncertain:   # F2b: no FAIL record, no cache void, no rewritten HOLD after the deadline
            self._refuse_past_deadline('permission-probe')
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
            if isinstance(verified := self.archive_abandoned_turn(uncertain), dict) and verified:   # F7: checked against this probe's own baseline below
                self.state['recovery_config_hashes'] = verified
            self.state['active'] = None
            self.state['uncertain_active'] = None
            if self.state.get('hold_reason', '').startswith(
                    ('uncertain permission-probe turn', 'uncertain permission-probe process group',
                     'cannot verify uncertain permission-probe',
                     'uncertain in-flight CLI turn')):
                self.state['hold_reason'] = 'permission probe retry cleared; permission probe is pending'
            self.save()
            if past_deadline := self._wi_deadline_issue(record=False):   # F2b: settled and archived; no new probe, report or cache untouched
                self.hold(past_deadline)
                return False
        report_file = self.run_dir / 'permission-probe.json'      # P0-4 V0: an aborted re-probe must not leave the old PASS valid
        if report_file.exists(): os.replace(report_file, report_file.with_name('permission-probe.superseded.json'))
        self.state['permission_probe_superseded'] = self.state.pop('permission_probe', None)
        (self.state.get('probe_skip_override') or {}).setdefault('voided', {'time': time.strftime(UTC_FORMAT, time.gmtime()), 'reason': 'permission-probe re-run'}); self.save()
        snapshot, _ = git_snapshot(self.workspace)
        if (tracked := probe_tracked_file(self.workspace)) is None:   # b296-f1f: the git checkout/rm legs need a real tracked file (b296-f1g: a literal pathspec)
            self.hold('permission probe refused before any turn: the workspace has no tracked regular file on disk (git ls-files) '
                      "for the probe's git checkout/rm legs; commit at least one file, then re-run permission-probe")
            return False
        global_before = global_config_snapshot(self.global_config_home, self.global_codex_home)
        if issue := self._recovery_config_issue(global_before, consume=False):   # F7: invoke()'s own baseline consumes them
            self.hold(issue)
            return False
        allowed_command = (self.args.test_command or '').strip() or None   # LG2-b2: None in a no-test report run
        attempts = self._probe_attempts(allowed_command, tracked)
        sandbox_probe_paths, sandbox_probe_commands = self._probe_targets('probe', self.args.reviewer_vendor)
        codex = self._codex_escape_targets() if self.args.reviewer_vendor == 'codex' else {}   # b296-f1 R1 M1/M2
        attempts = (*attempts, *sandbox_probe_commands[:3], *(command for _, command in codex.values()))
        prompt = self._probe_prompt(self.args.reviewer_vendor, allowed_command, attempts, sandbox_probe_commands)
        base_report = {'status': 'FAIL', 'probe_turn': self.state['sequence'] + 1, 'probe_tracked_file': tracked, 'reviewer_flags': self.reviewer_flags(),
                       'reviewer_flags_digest': self.reviewer_flags_digest(),
                       'gate_flags': self.gate_flags(), 'gate_flags_digest': self.gate_flags_digest(),
                       'gate_permission_probe': ({'status': 'NOT_NEEDED', 'reason': 'gate vendor equals reviewer vendor'} if self.args.gate_vendor == self.args.reviewer_vendor else {'status': 'NOT-ATTEMPTED'}),
                       'author_flags': self.author_flags(),
                       'author_flags_digest': self.author_flags_digest(),
                       'author_permission_probe': {
                           'status': 'NOT-APPLICABLE' if self.state['config'].get('review_report') or self.args.author_vendor != 'codex' else 'NOT-ATTEMPTED'},
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
            tracked_escape = self._tracked_file_escape(tracked)
            report = {**base_report, 'allowed_command_ran': False,
                      'write_attempts_denied': {command: False for command in attempts},
                      'write_attempt_outcomes': {command: 'not-attempted' for command in attempts},
                      'failure_reasons': ['probe-turn-error: ' + str(exc), *self._codex_escape_failures(codex, None), *tracked_escape],
                      **({'message': tracked_escape[0]} if tracked_escape else {}),
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
            report['global_config_changes'] = reclassify_default_home_trust(attribute_global_config_changes(   # P0
                global_before, global_after, [self.workspace]), global_before, global_after, [self.workspace])
            if report['global_config_changes']['status'] != 'PASS':
                report['failure_reasons'].append('unexpected-global-config-change')
            if escaped_probe_targets:
                report['failure_reasons'].append('claude-sandbox-probe-write-escaped')
            if remaining_probe_targets:
                report['failure_reasons'].append('claude-sandbox-probe-cleanup-incomplete')
            if sandbox_probe_paths:
                report['failure_reasons'].append('claude-sandbox-write-not-denied-or-not-observed')
            self._probe_void_on_non_pass(report)   # F4
            atomic_json(self.run_dir / 'permission-probe.json', report)
            self.state['permission_probe'] = {'sha256': hashlib.sha256((self.run_dir / 'permission-probe.json').read_bytes()).hexdigest(), 'turn': probe_sequence}
            self.state['hold_reason'] = 'permission probe failed; inspect permission-probe.json' + ('; ' + tracked_escape[0] if tracked_escape else '')
            self.save()
            self.write_usage()
            return False
        allowed, denied, outcomes, failures = self._probe_eval(evidence, unchanged, allowed_command, attempts, explicit=self.args.reviewer_vendor == 'codex')
        miss = self._scratch_probe_failure(result, evidence) if self.args.reviewer_vendor == 'codex' else ''
        failures += [miss] if miss else []
        failures += (escapes := self._codex_escape_failures(codex, result) + (tracked_escape := self._tracked_file_escape(tracked)))
        miss = miss or ', '.join(escapes)
        try:
            author_probe = ({'status': 'NOT-APPLICABLE', 'reason': 'report mode has no author sandbox'}
                            if self.state['config'].get('review_report') else self._author_permission_probe())
        except Exception as exc:
            author_probe = {'status': 'FAIL', 'reason': type(exc).__name__ + ': ' + str(exc)}
        report = {**base_report, 'status': probe_turn_status(allowed, outcomes, unchanged, miss),
                  'allowed_command_ran': allowed, 'write_attempts_denied': denied,
                  'write_attempt_outcomes': outcomes, 'failure_reasons': failures,
                  'snapshot_unchanged': unchanged,
                  'observed_commands': evidence, 'claimed_self_run_evidence': claimed,
                  'author_permission_probe': author_probe,
                  **({'message': tracked_escape[0]} if tracked_escape else {}),
                  }
        if author_probe['status'] == 'FAIL':
            report['status'] = 'FAIL'
            report['failure_reasons'].append('author-permission-probe-failed')
        elif author_probe['status'] == 'PASS_RESIDUAL_RISK' and report['status'] == 'PASS':
            report['status'] = 'PASS_RESIDUAL_RISK'; report['residual_risk'] = author_probe['residual_risk']
        elif author_probe['status'] == 'UNKNOWN' and report['status'] == 'PASS':
            report['status'] = 'UNKNOWN'
            if author_probe.get('model_refused') is True:
                report['failure_reasons'].append('author-model-refused')
                report['message'] = 'The Claude author model declined to run the author probe (HOLD, no tool call made); the sandbox was not tested, no escape was observed. Re-run the probe.'
            else: report['failure_reasons'].append('author-model-escape-unknown')
        if author_probe.get('model_escape_failed_targets'): report['failure_reasons'].append('author-escape-write-observed: ' + ', '.join(author_probe['model_escape_failed_targets']))
        if not report['snapshot_unchanged']:
            report['status'] = 'FAIL'
        if sandbox_probe_paths: self._probe_sandbox_eval(report, evidence, sandbox_probe_paths, sandbox_probe_commands)
        gate_probe = self._gate_probe_turn(snapshot, tracked) if self.args.gate_vendor != self.args.reviewer_vendor else None
        if gate_probe:   # the overall status is the worse of the two turns: FAIL > UNKNOWN > PASS_RESIDUAL_RISK > PASS
            report['gate_permission_probe'] = gate_probe
            if 'message' not in report and (gone := [r for r in gate_probe['failure_reasons'] if r.startswith('probe-tracked-file-escaped: ')]): report['message'] = gone[0]
            if gate_probe['status'] != 'PASS':   # a gate turn is PASS, UNKNOWN or FAIL (probe_turn_status); only a Codex author's synthetic check gives PASS_RESIDUAL_RISK
                report['failure_reasons'].append('gate-permission-probe-' + gate_probe['status'].lower())
                report['status'] = 'FAIL' if 'FAIL' in (gate_probe['status'], report['status']) else 'UNKNOWN'
        global_after = global_config_snapshot(self.global_config_home, self.global_codex_home)
        if 'codex' in self.dispatched_vendors() and (after_guard := self.codex_capabilities())['status'] != 'PASS':   # CG-3: a Codex probe turn can add capabilities, so a PASS must predict the pre-dispatch guard
            report['status'] = 'FAIL'
            report['failure_reasons'].append('codex-capability-guard-after-probe: ' + '; '.join(after_guard['issues']))
        expected_trust_paths = [self.workspace]
        if author_probe.get('workspace'):
            expected_trust_paths.append(Path(author_probe['workspace']))
        report['global_config_changes'] = reclassify_default_home_trust(attribute_global_config_changes(   # P0
            global_before, global_after, expected_trust_paths), global_before, global_after, expected_trust_paths)
        if report['global_config_changes']['status'] != 'PASS':
            report['status'] = 'FAIL'
            report['failure_reasons'].append('unexpected-global-config-change')
        if report['status'] != 'PASS_RESIDUAL_RISK': report.pop('residual_risk', None)
        trust_warning = '; '.join(report['global_config_changes'].get('warnings', []))
        if trust_warning:
            trust_warning += ': ' + ', '.join(path for row in report['global_config_changes']['expected_changes'] if row['file'] == 'codex_config' for path in row['workspaces']); report['warning'] = trust_warning
        if report['status'] == 'PASS' and not author_probe.get('model_escape_failed_targets') and not lifecycle_spine.fake_dispatch_guard(self.args):    # P0-4 V3: never PASS_RESIDUAL_RISK / UNKNOWN
            try: self._probe_cache_write(report, hashlib.sha256((json.dumps(report, indent=2, ensure_ascii=False) + '\n').encode()).hexdigest())
            except Exception as exc: report['warning'] = (report.get('warning', '') + '; ' if report.get('warning') else '') + f'probe-pass cache not written: {exc}'
        else: self._probe_void_on_non_pass(report)   # F4
        atomic_json(self.run_dir / 'permission-probe.json', report)
        self.state['permission_probe'] = {'sha256': hashlib.sha256((self.run_dir / 'permission-probe.json').read_bytes()).hexdigest(), 'turn': probe_sequence}
        if (failed := author_probe.get('model_escape_failed_targets')):
            beside = [t for t in failed if t in (author_probe.get('changed_beside_run_dir') or [])] if not author_probe.get('unexpected_tool_uses') else []   # field-a L1
            hint = ('; ' + ', '.join(beside) + ' changed beside the run dir during the probe: if that is an operator-created file outside the run dir '
                    "(a launcher log in the run dir's parent) or another lane's probe in the same parent, write launcher logs outside the run dir's parent, "
                    'keep lanes in separate parents and re-run permission-probe') if beside else ''
            self.hold('1C FAIL: ' + ('operator-created file outside the run dir?' if beside == failed else 'escape write observed at ' + ', '.join(failed)) + hint); return False
        self.state['residual_risk'] = report.get('residual_risk')
        self.state['hold_reason'] = ('permission probe passed; run resume to continue'
                                     if report['status'] in ('PASS', 'PASS_RESIDUAL_RISK') else
                                     'permission probe failed; inspect permission-probe.json') + ('; ' + trust_warning if trust_warning else '') + ('; ' + report['residual_risk'] if report.get('residual_risk') else '') + ('; ' + report['message'] if report.get('message') else '')
        self.save()
        self.write_usage()
        return report['status'] in ('PASS', 'PASS_RESIDUAL_RISK')

    def drive(self) -> str:
        if self._fake_lifecycle:
            raise RuntimeError('fake lifecycle cannot enter legacy drive')
        # The one entry of every real author dispatch (run, resume, reject, resume_polish): author_turn and
        # polish_author_turn are reachable only from _drive_loop, which only drive()/fake_drive() call.
        # Residual (STRICT-NITS gate): a direct resume()/reject() call can change state before this refuses; the CLI precheck refuses first.
        if self.strict and not self.state['config'].get('review_report') and 'codex' in self.dispatched_vendors() \
                and not lifecycle_spine.fake_dispatch_guard(self.args) and not (ok := self.codex_contract_verified())[0]:
            raise ValueError(ok[1])
        if self.strict and not self.state['config'].get('review_report') and self.args.author_vendor == 'claude' and not lifecycle_spine.fake_dispatch_guard(self.args) \
                and not (ok := self.claude_author_verified())[0]:
            raise ValueError(ok[1])
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

    def _fake_resume_p(self):
        if not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args):
            raise ValueError('P resume is fake-only')
        for _ in range(3):
            if self.state['lifecycle']['stage'] == 'EXEC' and not self._fake_reentry():
                return 'HOLD'
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
        candidate_test_sandbox.run(self, None, cwd=checkout.root, env={}, timeout=5 * timeout_scale.env_factor())
        self.state['fake_q_pending'] = pending
        self.save()
        test = candidate_test_sandbox.run(self, command, cwd=checkout.root,
                              timeout=self.args.timeout, capture_output=True,
                              env={'PATH': os.defpath, 'HOME': os.devnull, 'PYTHONDONTWRITEBYTECODE': '1',
                                   'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'})
        candidate_tree.verify_candidate_revision(checkout, revision)
        receipt = {**pending, 'oid': oid, 'command': command, 'returncode': test.returncode,
                   'stdout_sha256': hashlib.sha256(test.stdout).hexdigest(),
                   'stderr_sha256': hashlib.sha256(test.stderr).hexdigest(),
                   'executable_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
                   'write_boundary': test.write_boundary}
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
                    turn['sequence'] <= pending['review_after_sequence'] or self.configured_test_failed(turn) or
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
    def fake_prepare_delivery(self, backlog_item=None, day=None):
        if not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args):
            raise ValueError('delivery drive is fake-only')
        frozen_item = self.state.get('closeout_item')
        if frozen_item and backlog_item is not None and backlog_item != frozen_item['item_id']:
            raise ValueError('backlog item differs from frozen item; use the original ID')
        existing = self.state.get('fake_delivery_intent')
        if existing:
            if self.state['status'] == 'CLOSED':
                self.fake_close(existing['digest'])
            return copy.deepcopy(existing)
        if not self.state.get('closeout_item'):
            life = self.state['lifecycle']
            if life != lifecycle_spine.initial(life['item_uuid'], life['parent']):
                raise ValueError('closeout item can only be frozen on a fresh lifecycle; abort and start a new run')
            if backlog_item is None:
                raise ValueError('delivery requires a backlog item ID; supply it before starting')
        day = day or self.state.get('fake_delivery_day') or datetime.now().date().isoformat()
        try:
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError('noncanonical day')
        except (ValueError, TypeError) as error:
            raise ValueError('invalid delivery day; supply YYYY-MM-DD before starting') from error
        if self.state.get('fake_delivery_day') not in (None, day):
            raise ValueError('delivery day changed; use the frozen day')
        self.state['fake_delivery_day'] = day
        self.save()
        if self.state['lifecycle']['candidate_oid'] is None:
            if self.fake_lifecycle_drive(backlog_item) != 'STOP_BEFORE_SECURITY':
                raise ValueError('delivery drive requires completed P stages')
        if self.state['lifecycle']['stage'] in ('EXEC', 'FINISH', 'POLISH-Q', 'DOCS'):
            self._fake_resume_p()
        ingest = self.state['fake_ingest_receipt']
        baseline = candidate_tree.baseline_from_binding(ingest['baseline'])
        revision = candidate_tree.CandidateRevision(self.state['lifecycle']['candidate_oid'],
                                                   tuple(ingest['manifest']), 0)
        if self.state['lifecycle']['stage'] in ('STOP_BEFORE_SECURITY', 'SECURITY'):
            def security(request):
                self._fake_dispatching = True
                try:
                    result = self.invoke('reviewer', 'SECURITY',
                        f'Role: reviewer, fresh. Phase: SECURITY. Candidate OID: {revision.tree_oid}.\n'
                        'Run this test command exactly as written in one Bash call: ' + self.args.test_command,
                        review_schema(),
                        fresh=True, workspace_override=baseline.root)
                finally:
                    self._fake_dispatching = False
                turn = next(t for t in self.state['turns'] if t['sequence'] == result['sequence'])
                if (turn.get('error') or turn['role'] != 'reviewer' or turn['phase'] != 'SECURITY' or
                        turn['workspace'] != str(baseline.root) or not self.configured_test_succeeded(turn) or
                        self.configured_test_failed(turn)):
                    raise ValueError('SECURITY turn lacks current-OID error-free test evidence; retry or abort')
                return {'status': result['answer']['status'], 'candidate_oid': revision.tree_oid,
                        'findings': result['answer']['full_review'], 'observed_tools': turn['observed_tool_calls']}
            self.fake_lifecycle_route(None, chain_only=True,
                security_context={'baseline': baseline, 'revision': revision, 'review': security})
        if self.state['lifecycle']['stage'] != 'STOP_BEFORE_DELIVERY':
            raise ValueError('delivery drive requires fresh SECURITY completion')
        c1 = delivery_seal.c1(self, baseline, revision)
        if not self.state.get('fake_q_review'):
            self.fake_q_review(c1, day)
        if not self.state.get('fake_q_bundle'):
            self.fake_q_complete(c1, day)
        return delivery_intent.prepare(self, c1, day, observed_test_succeeded, atomic_json)

    def fake_finish_delivery(self, expected_digest):
        if not self._fake_lifecycle or not lifecycle_spine.fake_dispatch_guard(self.args):
            raise ValueError('delivery drive is fake-only')
        if not expected_digest or expected_digest != (self.state.get('fake_delivery_intent') or {}).get('digest'):
            raise ValueError('delivery finish requires the exact accepted intent digest')
        try:
            from paired_session import delivery_recover
        except ModuleNotFoundError:
            import delivery_recover
        if self.state['status'] != 'CLOSED':
            if delivery_recover.reconcile(self, observed_test_succeeded, atomic_json) == 'HOLD':
                return 'HOLD'
        result = self.fake_close(expected_digest)
        facts = self.state['close_receipt']['facts']
        with run_lease(self.run_dir):
            atomic_text(self.run_dir / 'delivery-report.md',
                        f'已关闭工作项 {facts["item_uuid"]}。\nC1: {facts["c1"]}\nC2: {facts["c2"]}\n'
                        f'Q: {facts["q_oid"]}\n外部交付：关闭。\n')
        return result

    def fake_close(self, expected_digest, *, external_delivery=False):
        return delivery_close.close(self, expected_digest, atomic_json, external_delivery)

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
        candidate_test_sandbox.run(self, None, cwd=checkout.root, env={}, timeout=5 * timeout_scale.env_factor())
        self.state['fake_candidate_test_pending'] = test_id
        self.save()
        test = candidate_test_sandbox.run(self, command, cwd=checkout.root,
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
                        'write_boundary': test.write_boundary,
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
            if self.state['config'].get('review_report'):
                record = self.state.get('review_only') or {}   # no writer: the frozen tree holds before EVERY dispatch
                if git_snapshot(self.workspace)[0] != record.get('candidate_tree_sha256'):
                    return self.hold('the report tree changed since the run was created; restore it from '
                                     + str(record.get('mirror')) + ' and resume, or abort')
                if self.state['next'] not in ('reviewer', 'gate', 'polish-q', 'security') or self.state['phase'] != 'EXEC':
                    return self.hold('report mode refuses a writer or unsupported phase')
                if self.state['next'] in ('polish-q', 'security') and not worktree_lifecycle.is_worktree(self.state):
                    return self.hold('report POLISH-Q requires --lifecycle-mode on; start a new run')
            if self.state['next'] != 'author': self.refuse_rejected_tree(stale_done=True)
            if (self.state['next'] == 'reviewer' and worktree_lifecycle.is_worktree(self.state) and
                    self.state['lifecycle']['stage'] == 'POLISH-Q'):   # the fix author saved before polish-recheck
                self.state['next'] = 'polish-recheck'
            try:
                if self.state['next'] == 'author':
                    self.author_turn()
                elif self.state['next'] == 'reviewer':
                    self.reviewer_turn()
                elif self.state['next'] == 'gate':
                    self.gate_turn()
                elif self.state['next'] == 'finish' and worktree_lifecycle.is_worktree(self.state):
                    self.worktree_finish_turn()
                elif self.state['next'] == 'polish-q' and worktree_lifecycle.is_worktree(self.state):
                    self.worktree_polish_turn()
                elif self.state['next'] == 'polish-fix' and worktree_lifecycle.is_worktree(self.state):
                    self.worktree_polish_fix_turn()
                elif self.state['next'] == 'polish-recheck' and worktree_lifecycle.is_worktree(self.state):
                    self.worktree_polish_recheck_turn()
                elif self.state['next'] == 'docs' and worktree_lifecycle.is_worktree(self.state):
                    self.worktree_docs_turn()
                elif self.state['next'] == 'security' and worktree_lifecycle.is_worktree(self.state):
                    self.worktree_security_turn()
                else:
                    return self.hold('invalid next action')
            except RuntimeError as exc:
                return self.hold(str(exc), rate_limited=type(exc) is RateLimitedTurn)
        return self.state['status']

    def resume(self, retry_uncertain=False) -> str:
        self._publication_guard()
        if (pending := self.state.get('delivery_pending')) and self.state['status'] == 'HOLD':   # finishes only via accept
            raise ValueError(f'an auto_commit delivery is pending; restore HEAD and accept --expect {pending}, or abort')
        if self._fake_lifecycle: raise RuntimeError('fake lifecycle cannot enter legacy resume')
        if self.state['status'] == 'ACCEPTED': return 'ACCEPTED'
        if self.state['status'] == 'REPORTED': return 'REPORTED'   # LG2-a3: terminal
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
        if not (self.state.get('active') or self.state.get('uncertain_active')):
            self._refuse_past_deadline('resume')   # F2b: before any state change, so the HOLD (and RLO) survives
        past_deadline = self._wi_deadline_issue(record=False)   # with an uncertain turn: settle it below, then HOLD instead of a dispatch
        reason = self.state.get('hold_reason', '')
        if self.state.get('active'):
            self.state['uncertain_active'] = self.state['active']
            self.state['active'] = None
            self._void_opv_unknown_turn(self.state['uncertain_active'])
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
            if isinstance(verified := self.archive_abandoned_turn(uncertain), dict) and verified:   # F7: checked against the next turn's own baseline
                self.state['recovery_config_hashes'] = verified
            self._rotate_failed_first_claude_session(
                uncertain.get('role', ''), uncertain.get('vendor') or
                self._role_vendor(uncertain.get('role', '')), uncertain.get('fresh', False))
        if past_deadline:   # F2b: the stopped turn is archived, so note --scope-change and abort work; no new dispatch
            self.state['active'] = self.state['uncertain_active'] = None
            return self.hold(past_deadline)
        self.state['status'] = 'ACTIVE'
        self.state['hold_reason'] = ''
        self.state.pop('hold_kind', None); self.state.pop('rate_limit', None)
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
    # RF-4: a Claude child must not auto-update plugins mid-run (that rewrites installed_plugins.json and trips the global-config check).
    env['DISABLE_AUTOUPDATER'] = '1'
    env.pop('FORCE_AUTOUPDATE_PLUGINS', None)
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
                                      'accept', 'reject', 'note', 'status', 'attach-verification', 'stop'])
    p.add_argument('--detach', action='store_true',
                   help='run, resume, reject or permission-probe in a new session, outside the host\'s process tree, and return '
                        'at once with its pid and log; poll `status --brief`; `stop` ends it (docs/detach.md)')
    p.add_argument('--workspace', required=True)
    p.add_argument('--workitem', required=True)
    p.add_argument('--run-dir', required=True)
    p.add_argument('--supersedes', help='resolved parent run dir for a scope-change successor')
    p.add_argument('--config', help='JSON profile; defaults to <workspace>/.review-loop/paired-session.json')
    p.add_argument('--author-vendor', choices=['codex', 'claude'], default='codex')
    p.add_argument('--author-model', help='default: the vendor default (ADR-9); give the full model id the CLI reports')
    p.add_argument('--author-effort', default='medium')
    p.add_argument('--reviewer-vendor', choices=['codex', 'claude'], default='claude')
    p.add_argument('--reviewer-model', help='default: the vendor default (ADR-9); give the full model id the CLI reports')
    p.add_argument('--reviewer-effort', default='medium')
    p.add_argument('--gate-vendor', choices=['codex', 'claude'], help="defaults to the author's vendor (ADR-10); an explicit value is recorded as operator")
    p.add_argument('--gate-model', help='default: the vendor default (ADR-9); give the full model id the CLI reports')
    p.add_argument('--gate-effort', default='medium')
    p.add_argument('--shadow', choices=['on', 'off'], default='on')
    p.add_argument('--adversarial-gate', choices=['on', 'off'], default='on')
    p.add_argument('--polish-round', choices=['on', 'off'], default='on')
    p.add_argument('--lifecycle-mode', choices=['off', 'on'], default='off')
    p.add_argument('--strict', dest='safety_mode', action='store_const', const='strict', default=None,   # D-EFF: default efficient
                   help='also require a permission-probe PASS before dispatch and let the evidence guard hold (default: efficient; the sandboxes apply in both)')
    p.add_argument('--docs-file', default=None, help="default: CHANGELOG.md for a worktree-lifecycle run, else ''")
    p.add_argument('--docs-allowlist', action='append', default=[])
    p.add_argument('--skip-globs', action='append', default=[])
    p.add_argument('--skip-quality-polish', type=config_bool, default=False)
    p.add_argument('--quality-writers', choices=tuple(worktree_lifecycle.QUALITY_WRITERS), default=None,
                   help='worktree lifecycle POLISH-Q writer passes (default: both for --review-only, else off)')
    p.add_argument('--auto-commit', type=config_bool, default=None,
                   help='worktree lifecycle: accept --expect makes one hook-free local commit of the accepted tree '
                        '(default: true for a --review-only run with --lifecycle-mode on, else false)')
    p.add_argument('--external-delivery', type=config_bool, default=False,
                   help='push/PR/merge after acceptance; refused by the worktree lifecycle (D8)')
    p.add_argument('--gate-prompt', default=str(DEFAULT_GATE_PROMPT))
    p.add_argument('--max-plan-rounds', type=int, default=3)
    p.add_argument('--max-exec-rounds', type=int, default=4)
    p.add_argument('--max-invocations', type=int, default=25)
    p.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SECONDS,
                   help=f'per-dispatch timeout for every non-EXEC turn and the coordinator\'s own test runs '
                        f'(default {DEFAULT_TIMEOUT_SECONDS}, at most {MAX_TIMEOUT_SECONDS} seconds)')
    p.set_defaults(exec_turn_timeout_explicit=False)
    p.add_argument('--exec-turn-timeout', type=int, default=None, action=StoreExplicitInteger,
                   help=f'EXEC author turn timeout (default max({DEFAULT_EXEC_TURN_TIMEOUT_SECONDS}, --timeout), '
                        f'capped at {MAX_EXEC_TURN_TIMEOUT_SECONDS} seconds)')
    p.add_argument('--resume-timeout', type=int, default=None,
                   help=f'increase the saved timeout on resume, up to {MAX_RESUME_TIMEOUT_SECONDS} seconds')
    p.add_argument('--wi-deadline', type=int, default=None, metavar='SECONDS',
                   help='optional whole work-item wall-clock deadline from the run start (off by default); once it has '
                        'passed the run HOLDs at the next dispatch, never mid-turn; fixed at run, kept across HOLD, resume and restart')
    p.add_argument('--test-command', default=DefaultTestCommand('npm test'))   # D09: a default is not an explicit command
    p.add_argument('--reviewer-command', action='append', default=[],
                   help='additional exact Bash command allowed for read-only reviewers (repeatable)')
    p.add_argument('--codex-bin', default='codex')
    p.add_argument('--claude-bin', default='claude')
    p.add_argument('--exercise-revisions', action='store_true')
    p.add_argument('--stop-after-plan', action='store_true',
                   help='HOLD once right after PLAN approval; a later resume enters EXEC')
    p.add_argument('--review-only', action='store_true', default=None,
                   help='review the existing change (the workspace tree against --base) with no PLAN phase; fixed at run creation')
    p.add_argument('--review-report', action='store_true', default=None,
                   help='report on a --review-only change without writers; fixed at run creation')
    p.add_argument('--review-pr-pins', metavar='FILE', default=None,
                   help='report mode: the JSON scripts/materialize_pr.py --root printed, frozen as the report\'s PR pins')
    p.add_argument('--no-test-command', action='store_true', default=None,
                   help='report mode: run no test command (frozen as test_command null); approvals are static and untested')
    p.add_argument('--aspects', dest='review_aspects', metavar='LIST', default=None,
                   help='report mode: the specialist aspects, a comma list of code,errors,comments,types,tests (default all)')
    p.add_argument('--base', dest='review_base_ref', metavar='REF',
                   help=f'the review base of --review-only, an ancestor of HEAD (default {REVIEW_ONLY_DEFAULT_BASE})')
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
    p.add_argument('--accept-unverified-codex-cli', action='store_true',
                   help='operator override: run Codex roles (author, reviewer or gate) on an unverified codex-cli version (needs --reason)')
    p.add_argument('--accept-unverified-claude-author', action='store_true',
                   help='operator opt-in: run a Claude author with path-scoped Edit rules but no probe PASS (needs --reason; '
                        'with run, resume or reject). Persisted and re-applied on restore until the author flags change. '
                        'The Edit path boundary is not yet verified against symlink or hardlink redirection created '
                        'inside the workspace (P0-3b probes it)')
    p.add_argument('--accept-probe-skip', action='store_true',
                   help='operator acceptance: run/resume/reject without a passing permission-probe.json (needs --reason); voided for good '
                        'when a flags digest changes; the Codex CLI contract and Claude author gate still apply')
    p.add_argument('--reason', help='why the operator accepts the unverified codex-cli version, Claude author or probe skip; also the reason for accept --override-rejection')
    p.add_argument('--text', help='operator rejection note (reject action)')
    p.add_argument('--file', help='read operator rejection note from this file (reject action)')
    p.add_argument('--override-rejection', action='store_true', help='operator ruling on a held rejected tree')
    av = p.add_argument_group('attach-verification', 'operator evidence for the current tree (ACTIVE, HOLD or DONE before accept)')   # HELP-GROUP
    av.add_argument('--command', help='the command the operator ran outside the author sandbox')
    av.add_argument('--cwd', help='where it ran, the workspace or a directory inside it (default the workspace)')
    av.add_argument('--exit-code', type=int, help='its exit code')
    av.add_argument('--log', help='its log file, outside the workspace and run dir (copied into the run evidence)')
    av.add_argument('--log-sha256', help='sha256 of that log; a mismatch is refused')
    av.add_argument('--note', help='non-empty operator note')
    p.add_argument('--expect', help='operator intent digest required by accept/reject')
    p.add_argument('--intent-only', action='store_true', help='print an operator intent for confirmation')
    p.add_argument('--scope-change', action='store_true', help='end this run and print a successor command')
    p.add_argument('--quiet-progress', action='store_true', help='suppress the per-event progress lines (the final status line and RUN/progress.jsonl stay)')
    p.add_argument('--brief', nargs='?', type=int, const=20, default=None, metavar='N', help='status: print the last N (default 20) progress events instead of state.json')
    p.set_defaults(allowed_models=None)
    return p


CONFIGURABLE_DESTS = {
    'author_vendor', 'author_model', 'author_effort', 'reviewer_vendor',
    'reviewer_model', 'reviewer_effort', 'gate_vendor', 'gate_model', 'gate_effort', 'shadow',
    'allowed_models', 'adversarial_gate', 'polish_round', 'gate_prompt', 'max_plan_rounds',
    'max_exec_rounds', 'max_invocations', 'timeout', 'exec_turn_timeout', 'test_command',
    'reviewer_command', 'codex_bin', 'claude_bin', 'author_subagents',
    'lifecycle_mode', 'docs_file', 'docs_allowlist', 'skip_globs', 'skip_quality_polish', 'auto_commit', 'external_delivery',
    'safety_mode', 'quality_writers',
}


ROLE_DESTS = ('author_vendor', 'author_model', 'reviewer_vendor', 'reviewer_model', 'gate_vendor', 'gate_model')
MODEL_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}')


def old_gate_vendor(author_vendor: str) -> str:   # legacy opposite-vendor rule: only saved runs without gate_vendor (ADR-10 M3) and M4
    return 'claude' if author_vendor == 'codex' else 'codex'


def restores_run(args: argparse.Namespace) -> bool:
    return args.action in ('accept', 'reject', 'note', 'attach-verification', 'run', 'resume', 'permission-probe') and (Path(args.run_dir) / 'state.json').exists()


def validate_allowed_models(allowed) -> None:
    if allowed is not None and not (
            isinstance(allowed, dict) and set(allowed) <= {'codex', 'claude'} and all(
                isinstance(v, list) and all(isinstance(m, str) for m in v) for v in allowed.values())):
        raise ValueError('allowed_models must be an object {"codex": [...], "claude": [...]} of string lists')


def refuse_foreign_gate_model(args: argparse.Namespace) -> None:
    """ADR-10 M4: an explicit gate_model of the other vendor needs an explicit --gate-vendor (never applies to a restored run)."""
    model, given = getattr(args, 'gate_model', None), getattr(args, 'gate_vendor', None)
    vendor = given or args.author_vendor
    other = old_gate_vendor(vendor)
    if model is None or restores_run(args): return
    validate_allowed_models(getattr(args, 'allowed_models', None))   # structure first: a list or a null vendor value is a configuration refusal, never an AttributeError
    allowed = getattr(args, 'allowed_models', None) or {}
    if model in allowed.get(vendor, []): return
    if model in allowed.get(other, []) or model.startswith({'claude': 'claude-', 'codex': 'gpt-'}[other]):
        raise ValueError(f'gate_model {model} belongs to {other}, but the gate ' + (
            f'vendor is {vendor}; pass --gate-vendor {other} or a {vendor} model' if given else
            f"now defaults to the author's vendor {vendor}; pass --gate-vendor {other} to keep it"))


def resolve_role_model_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Apply the vendor default model (ADR-12; ADR-9 keeps the operator's choice) to every role without an explicit model."""
    model_for_vendor = {'claude': 'claude-opus-5-5', 'codex': CODEX_DEFAULT_MODEL}
    refuse_foreign_gate_model(args)
    if getattr(args, 'gate_vendor_source', None) is None:
        args.gate_vendor_source = 'default' if getattr(args, 'gate_vendor', None) is None else 'operator'
    if getattr(args, 'gate_vendor', None) is None:
        args.gate_vendor = args.author_vendor   # ADR-10: the gate defaults to the author's vendor
    role_vendors = {'author_model': args.author_vendor, 'reviewer_model': args.reviewer_vendor,
                    'gate_model': args.gate_vendor}
    for key, vendor in role_vendors.items():
        if getattr(args, key) is None:
            setattr(args, key, model_for_vendor[vendor])
            if vendor == 'codex':   # ADR-12: these roles need a CLI that runs the default model; a sorted list keeps args JSON-serializable
                args.defaulted_codex_models = sorted({*getattr(args, 'defaulted_codex_models', ()), key})
    return args


def codex_cli_semver(binary: str) -> Optional[tuple]:
    try: proc = subprocess.run([binary, '--version'], text=True, capture_output=True, stdin=subprocess.DEVNULL,
                               timeout=10 * timeout_scale.env_factor())
    except (OSError, subprocess.SubprocessError): return None
    out = proc.stdout if proc.returncode == 0 else ''
    match = re.search(r'codex-cli (\d+)\.(\d+)\.(\d+)', out)
    return tuple(int(part) for part in match.groups()) if match else None


def refuse_default_codex_model_on_old_cli(args: argparse.Namespace, codex_path: str) -> None:
    """ADR-12: a Codex role on the default model needs codex-cli >= CODEX_DEFAULT_MODEL_MIN_CLI; refuse before any state exists.
    codex_path is the operator program already bound by program_binding.snapshot: a workspace-named binary is never run."""
    flags = [f"--{key.replace('_', '-')}" for key in ('author_model', 'reviewer_model', 'gate_model')
             if key in getattr(args, 'defaulted_codex_models', set())]
    if not flags or (version := codex_cli_semver(codex_path)) is not None and version >= CODEX_DEFAULT_MODEL_MIN_CLI:
        return
    have = 'unreadable' if version is None else '.'.join(map(str, version))
    raise ValueError(f"the default Codex model {CODEX_DEFAULT_MODEL} needs codex-cli >= {'.'.join(map(str, CODEX_DEFAULT_MODEL_MIN_CLI))} "
                     f"(this CLI: {have}); upgrade the Codex CLI, or pass {' '.join(flag + ' gpt-6-luna' for flag in flags)}")


CLAUDE_CLI_ALIASES = {'default', 'best', 'opus', 'sonnet', 'haiku', 'fable', 'opusplan', 'opus[1m]', 'sonnet[1m]'}


def git_work_tree_root(path: Path) -> Optional[Path]:
    """FIELD-28: the nearest enclosing git work tree of `path` (a `.git` directory or file), or None. The role TMPDIRs sit
    under the run directory, so a project that refuses scratch space inside a repository falls back to /tmp there."""
    path = Path(path).resolve()
    return next((folder for folder in (path, *path.parents) if (folder / '.git').exists()), None)


def replace_override(state: dict, key: str, record: dict) -> None:
    """STRICT-NITS (P0-2 b): a new operator override record keeps the one it replaces (voided or not) in <key>_history."""
    if state.get(key):
        state.setdefault(key + '_history', []).append(state[key])
    state[key] = record


def validate_role_models(args: argparse.Namespace) -> None:
    """ADR-9: each role needs a well-formed model id, listed in allowed_models when that key is set."""
    allowed = getattr(args, 'allowed_models', None)
    validate_allowed_models(allowed)
    role_vendors = {'author_model': args.author_vendor, 'reviewer_model': args.reviewer_vendor,
                    'gate_model': args.gate_vendor}
    for key, vendor in role_vendors.items():
        model = getattr(args, key)
        if not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model):
            raise ValueError(f'{key} is not a well-formed model id: {model!r}')
        if allowed is not None and model not in allowed.get(vendor, []):
            raise ValueError(f'{key} {model} is not in allowed_models for the {vendor} role')
        if vendor == 'claude' and model.lower() in CLAUDE_CLI_ALIASES:   # HYGIENE-1: an alias would HOLD only after a wasted turn
            raise ValueError(f'{key} {model!r} is a Claude CLI alias, not a full model id (the run compares the id with the model '
                             'the CLI reports, so an alias would HOLD after its first turn); give the full id, for example claude-opus-5-5')


def gate_surface_issue(args: argparse.Namespace):
    return None   # G-a K5: a gate vendor other than the reviewer's is covered by the gate probe in permission-probe (probe_passed), not refused here


def _author_writable_profile(profile: Path, workspace: Path, run_dir: Optional[str]) -> bool:
    """ADR-11 D-4: a profile under the workspace or run dir (incl. author-tmp), compared by inode so case
    and symlink aliases on case-insensitive filesystems are caught too."""
    run_root = Path(run_dir).expanduser().resolve() if run_dir else None
    roots = [root for root in (workspace, run_root, (run_root / 'author-tmp').resolve() if run_root else None)
             if root is not None and root.exists()]   # author-tmp resolved too, as program_binding does
    return any(os.path.samefile(candidate, root) for path in (profile, profile.resolve())
               for candidate in (path, *path.parents) if candidate.exists() for root in roots)


def configure_parser(p: argparse.ArgumentParser, argv: list[str], ignore_profile: bool = False) -> argparse.ArgumentParser:
    """Load project defaults while preserving explicit CLI argument precedence."""
    if ignore_profile: return p
    bootstrap = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    bootstrap.add_argument('--workspace', required=True)
    bootstrap.add_argument('--config')
    bootstrap.add_argument('--run-dir')
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
    writable_profile = _author_writable_profile(config_path, workspace, known.run_dir)   # decided before reading
    try:
        values = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'cannot read paired-session config {config_path}: {exc}') from exc
    if not isinstance(values, dict):
        raise ValueError('paired-session config must be a JSON object')
    unknown = (set(values) - CONFIGURABLE_DESTS) | {k for k in values if k in OPERATOR_ONLY_DESTS or k.startswith('accept_')}
    if unknown:
        raise ValueError('unsupported paired-session config keys: ' + ', '.join(sorted(unknown)))
    if 'safety_mode' in values and (not known.config or writable_profile or values['safety_mode'] not in ('efficient', 'strict')):   # D-EFF
        raise ValueError('safety_mode is set only by --strict or an operator --config profile (efficient or strict) outside the workspace, '
                         'run dir and author temp, never by the workspace config')
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
        if writable_profile and key in worktree_lifecycle.PROFILE_KEYS:
            p.set_defaults(workspace_lifecycle_keys=sorted({*(p.get_default('workspace_lifecycle_keys') or ()), key}))
        if key == 'lifecycle_mode' and value == 'on' and writable_profile:
            raise ValueError('lifecycle remains disabled from a workspace profile; pass --lifecycle-mode on '
                             'or use an operator --config outside the workspace, run dir and author temp')
        if key == 'allowed_models':
            p.set_defaults(allowed_models=value)
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


def status_brief(run_dir: Path, count: int) -> int:
    """PL: the last `count` progress events as readable lines; read-only, so it works while a run holds the lease."""
    try:
        with os.fdopen(os.open(run_dir / 'progress.jsonl', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)), 'rb') as log:
            if not stat.S_ISREG(os.fstat(log.fileno()).st_mode): raise OSError('not a regular file')
            lines = log.read().splitlines()
    except OSError: lines = []
    shown = 0
    for line in lines[max(len(lines) - count, 0):]:
        try: print(progress_line(json.loads(line))); shown += 1
        except (ValueError, KeyError, TypeError): continue
    if not shown: print('no progress events yet')
    try: written = json.loads((run_dir / 'state.json').read_text()).get('ignored_config_written')
    except (OSError, ValueError, AttributeError): written = None
    if written:   # F3
        print('WARNING: ignored executable-config files written by the author: ' + ', '.join(written))
    return 0


def _execute_locked(args: argparse.Namespace) -> int:
    if args.scope_change and args.action not in ('note', 'reject'):
        raise ValueError('--scope-change requires note or reject')
    if args.action == 'status':
        return print((Path(args.run_dir) / 'state.json').read_text()) or 0
    co = Coordinator(args)
    co._publication_guard()
    if args.action == 'accept' and (args.text or args.file):   # N4-d: accept's intent digest covers --reason, never --text/--file (after the role restore checks)
        raise ValueError('accept takes no --text or --file; give the acceptance reason with --reason, the same on accept --intent-only and on accept')
    if (args.action in ('run', 'resume', 'reject', 'accept') or args.scope_change) and (issue := co.unrestored_workspace_issue()):   # D-EFF A, intents too
        return co.refused(issue)
    if args.action == 'accept': co._refuse_report_accept()   # LG2-a3: before an intent too
    if args.intent_only:
        intent = co.operator_intent(args.action, args.text, args.file)
        intent['next_command'] = co.next_intent_command(intent['digest'], args.action)   # FIELD-25: not part of the digest
        print(json.dumps(intent))
        sys.stderr.write('NEXT: ' + intent['next_command'] + '\n')
        return 0
    if (args.action in ('run', 'resume', 'permission-probe', 'reject') and not co.global_codex_home.is_dir()
            and 'codex' in co.dispatched_vendors()):   # FIELD-7: a clear message, not a CLI exit 1 (after --intent-only: field-a L5)
        return co.refused(f'CODEX_HOME {co.global_codex_home} is not an existing directory; create it (log in with CODEX_HOME set to it, '
                          'or copy auth.json and config.toml into it, directory 0700, files 0600) or '
                          + ('unset CODEX_HOME' if os.environ.get('CODEX_HOME') else 'set CODEX_HOME to an existing Codex home'))   # field-a L4
    if (args.action in ('run', 'resume', 'permission-probe', 'reject') and 'codex' in co.dispatched_vendors()
            and not os.environ.get('CODEX_HOME')):   # HYGIENE-1: the 2026-10-06 P0 cause (CODEX_HOME left unset); a warning only
        print('WARNING: CODEX_HOME is unset, so Codex uses the default ~/.codex: concurrent Codex runs on the default home write '
              'trust entries that can disturb each other; use an isolated absolute CODEX_HOME per run')
    if args.action in ('run', 'resume', 'permission-probe') and (repo := git_work_tree_root(co.run_dir)):   # FIELD-28, a warning only
        print(f'WARNING: the run directory {co.run_dir} is inside the git repository {repo}; tools and test suites that refuse '
              'scratch space inside a repository may fall back to /tmp, which the Codex read-only sandbox denies. Put the run root '
              'outside any repository.', file=sys.stderr)
    if (co.strict and not co.state['config'].get('review_report') and args.author_vendor == 'claude' and not lifecycle_spine.fake_dispatch_guard(args)  # restored from state; D-EFF: strict only
            and args.action in ('run', 'resume', 'reject') and not args.scope_change):  # only these can dispatch the author
        if args.accept_unverified_claude_author:
            replace_override(co.state, 'claude_author_override', {
                'reason': (args.reason or '').strip(), 'actor': 'operator', 'author_flags_digest': co.author_flags_digest(),
                'secret_env_names': co._env_names_now(), 'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            co.save()
        if not (claude_ok := co.claude_author_verified())[0]:
            return co.refused(claude_ok[1])
    if not co.strict and (args.accept_probe_skip or args.accept_unverified_codex_cli or args.accept_unverified_claude_author):
        print('NOTE: an efficient-mode run needs no probe waiver; the --accept-* flag is not recorded')   # D-EFF
    if args.scope_change:
        print(co.scope_change(args.text, args.file))
        return 0
    if args.action == 'note':
        print('NOTE: ' + co.note(args.text, args.file))
        return 0
    if args.action == 'attach-verification':
        print('VERIFICATION: ' + opv.attach(co, args, git_snapshot(co.workspace)[0], atomic_json))
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
        for row in (co.state.get('acceptance') or {}).get('operator_verifications', []):   # N4-e: the operator evidence this acceptance relies on
            print(f"VERIFICATION {row['id']} current for the accepted tree: `{row['command']}` exit {row['exit_code']}, log sha256 {row['log_sha256']}")
        if (delivery := (co.state.get('acceptance') or {}).get('delivery') or {}).get('commit'):   # FIELD-25 (a)
            print(f"COMMIT: {delivery['commit']} (auto_commit, parent {str(delivery.get('head'))[:12]}); not pushed"
                  + (f"; untracked files committed: {', '.join(delivery['committed_untracked'])}" if delivery.get('committed_untracked') else ''))
        if rows := (co.state.get('acceptance') or {}).get('uncommitted'):   # FIELD-25
            print(f'UNCOMMITTED: no commit was made ({uncommitted_cause(co.state)}); commit these yourself: ' + ', '.join(rows))
        print(status)
        return 0
    if args.action != 'abort' and (issue := gate_surface_issue(args)):
        return co.refused(issue)
    if (co.strict and not co.state['config'].get('review_report') and args.action in ('run', 'resume', 'reject')
            and 'codex' in co.dispatched_vendors()   # ROLE-NITS: a Codex gate (AAB) too; report runs keep skipping it (residual)
            and not lifecycle_spine.fake_dispatch_guard(args)):
        if args.accept_unverified_codex_cli and (version := co._codex_cli_version()) != 'UNAVAILABLE':
            replace_override(co.state, 'codex_cli_override', {
                'version': version, 'reason': args.reason.strip(), 'actor': 'operator',
                'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
            co.save()
        if not (verified := co.codex_contract_verified())[0]:
            return co.refused(verified[1])
    if co.strict and args.accept_probe_skip and args.action in ('run', 'resume', 'reject'):   # D-EFF: a probe waiver matters in strict only
        if (negative := co._probe_negative_status()):   # P0-4b H1: an acceptance never overrides current negative evidence
            return co.refused(f'the current permission probe is {negative}; fix the cause and re-run permission-probe')
        if not co._gate_probe_covered():
            return co.refused(f'gate vendor {args.gate_vendor} differs from reviewer vendor {args.reviewer_vendor}; only a passing gate probe bound to the current gate flags covers it, so run permission-probe (--accept-probe-skip does not)')
        replace_override(co.state, 'probe_skip_override', {'reason': args.reason.strip(), 'actor': 'operator', 'time': time.strftime(UTC_FORMAT, time.gmtime()), 'reviewer_flags_digest': co.reviewer_flags_digest(), 'author_flags_digest': co.author_flags_digest(), 'gate_flags_digest': co.gate_flags_digest(), 'secret_env_names': co._env_names_now()}); co.save()
    if args.action == 'reject':
        if co.strict and not args.skip_probe:   # D-EFF efficient: no permission probe is required
            passed, reason = co.probe_gate()
            if not passed:
                return co.refused(reason + '; run permission-probe before continuing')
        status = co.reject(args.text, args.file)
        if status == 'ACTIVE':
            status = co.drive()
        print(status + (' (acceptance pending)' if status == 'DONE' else
                        ': ' + co.state.get('hold_reason', '') if status == 'HOLD' else ''))
        return 0 if status in ('DONE', 'ACCEPTED', 'REPORTED') else 2   # LG2-a3: REPORTED is a normal end
    pending = co.state.get('uncertain_active') or {}
    before_config = pending.get('global_codex_before', {}).get('codex_config')
    changed_codex = (args.action == 'resume' and args.retry_uncertain and not args.polish
                     and pending.get('vendor') == 'codex'
                     and before_config and (co.run_dir / 'permission-probe.json').exists()
                     and global_config_snapshot(co.global_config_home, co.global_codex_home).get(
                         'codex_config', {}).get('sha256') != before_config)
    if co.strict and args.action in ('run', 'resume') and not args.skip_probe:
        passed, reason = co.probe_gate()
        if not passed and not changed_codex:
            return co.refused(reason + '; run permission-probe before continuing')
    co._probe_gate_required = co.strict and not args.skip_probe and not (args.action in ('run', 'resume') and co._probe_skip_accepted())
    if args.action == 'abort':
        if co.state.get('status') in ('ACCEPTED', 'CLOSED', 'REPORTED'):   # LG2-a3: REPORTED is terminal too
            print(co.state['status'])
            return 0
        suffix = ('; a prior CLI child may still be running; inspect uncertain_active before retry'
                  if co.state.get('active') or co.state.get('uncertain_active') else '')
        co.hold('aborted by operator' + suffix)
        print('HOLD: ' + co.state['hold_reason'])
        return 2
    print(f'GATE: {args.gate_vendor} {args.gate_model} (gate_vendor_source: {args.gate_vendor_source})')   # ADR-10 M2
    status = (co.drive() if args.action == 'run' else
              co.resume_polish() if args.polish else co.resume(args.retry_uncertain))
    print(status + (' (acceptance pending)' if status == 'DONE' else
                    ': ' + co.state.get('hold_reason', '') if status == 'HOLD' else ''))
    return 0 if status in ('DONE', 'ACCEPTED', 'REPORTED') else 2   # LG2-a3: REPORTED is a normal end


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
    args.raw_argv = raw_argv   # FIELD-25: the operator's own command, for the accept/reject next-command line
    if args.action == 'stop':   # detach: needs no lease; the detached command releases its own
        try: return stop_detached(args.run_dir)
        except ValueError as exc: print('REFUSED: ' + str(exc)); return 2
    if args.detach and args.action not in DETACH_ACTIONS:
        print('REFUSED: --detach is only for ' + ', '.join(DETACH_ACTIONS))
        return 2
    if not 1 <= args.timeout <= MAX_TIMEOUT_SECONDS:   # timeoutcap: before any lease, run dir or detached child
        print('REFUSED: ' + TIMEOUT_RANGE_ERROR)
        return 2
    if args.action == 'permission-probe' and not restores_run(args) and (program_snapshot(Path(args.workspace), Path(args.run_dir), Path(args.run_dir) / 'author-tmp', args.codex_bin, args.claude_bin, args.gate_prompt, args.config)[1] or '').startswith('workspace profile'):
        args = normalize_cli_paths(configure_parser(parser(), raw_argv, ignore_profile=True).parse_args(raw_argv))   # D3: the probe reports the refusal, but nothing from that profile reaches state
    args.explicit_role_flags = {a.dest for a in cli_parser._actions if a.dest in ROLE_DESTS and any(
        arg == o or arg.startswith(o + '=') for o in a.option_strings for arg in raw_argv)}
    roots = (Path(args.workspace), Path(args.run_dir))
    os.environ['PATH'] = safe_path(os.environ.get('PATH', ''), (*roots, roots[1] / 'author-tmp'))
    if args.skip_probe and not lifecycle_spine.fake_dispatch_guard(args):
        print('REFUSED: --skip-probe is limited to the fake test harness')
        return 2
    if args.scope_change and args.action not in ('note', 'reject'):
        print('REFUSED: --scope-change requires note or reject')
        return 2
    try: saved_null = restores_run(args) and saved_no_test(json.loads((Path(args.run_dir) / 'state.json').read_text()).get('config'))
    except (OSError, ValueError, AttributeError): saved_null = False   # the Coordinator reports an unreadable state itself
    if (saved_null or args.no_test_command) and any(a == '--test-command' or a.startswith('--test-command=') for a in raw_argv):
        print('REFUSED: --test-command conflicts with a no-test report run (--no-test-command)')
        return 2
    if args.no_test_command or saved_null:   # LG2-b2: the null sentinel before every precheck below
        args.test_command = None
    accepts = args.accept_unverified_codex_cli or args.accept_unverified_claude_author or args.accept_probe_skip
    if bool(accepts) != bool((args.reason or '').strip()) and not ((args.override_rejection or args.action == 'accept') and not accepts) or (accepts and args.action not in ('run', 'resume', 'reject')):   # N4-c: accept --reason X is the acceptance reason
        print('REFUSED: --accept-unverified-codex-cli / --accept-unverified-claude-author / --accept-probe-skip needs --reason and run, resume or reject')
        return 2
    try:
        resolve_role_model_defaults(args)
        if not (Path(args.run_dir) / 'state.json').exists():   # an existing run validates its restored, frozen models (ADR-9 M5)
            validate_role_models(args)
        if args.action in ('run', 'resume', 'permission-probe') and not restores_run(args):   # ADR-12: actions that create state
            workspace, run_dir = Path(args.workspace).expanduser().resolve(), Path(args.run_dir).expanduser().resolve()
            programs, issue = program_snapshot(workspace, run_dir, run_dir / 'author-tmp', args.codex_bin, args.claude_bin,
                                               args.gate_prompt, args.config)
            if not issue:   # a binding issue keeps its own refusal; only the bound program is run, fake harness included
                refuse_default_codex_model_on_old_cli(args, programs['codex_bin']['path'])
    except ValueError as exc:
        print('REFUSED: ' + str(exc))
        return 2
    if args.author_vendor == 'claude' and not lifecycle_spine.fake_dispatch_guard(args) \
            and args.action in ('run', 'resume', 'reject') and not args.accept_unverified_claude_author \
            and not args.review_report \
            and not restores_run(args) and (args.safety_mode or DEFAULT_SAFETY_MODE) == 'strict':   # D-EFF: a new run's probe gate (C)
        print('REFUSED: a Claude author is limited to the fake test harness in this preview (1C row 3b) unless a Claude '
              'author permission-probe passes (P0-3b) or the operator opts in with `run --accept-unverified-claude-author '
              '--reason TEXT` (permission-probe does not take the flag); no run state exists yet, so run permission-probe first')   # FIELD-10
        return 2
    if (args.action in ('run', 'permission-probe') and not restores_run(args) and 'claude' in (args.reviewer_vendor, args.gate_vendor)
            and (hint := dontask_command_hint([c for c in (args.test_command, *args.reviewer_command) if c]))):
        print(hint)
    if not restores_run(args) and (issue := gate_surface_issue(args)):
        print('REFUSED: ' + issue)
        return 2
    if args.polish and args.action != 'resume':
        parser().error('--polish is only valid with resume')
    if args.detach:   # after the synchronous refusals above, so a bad command line still fails in the caller's shell
        try: return detach(raw_argv, args.run_dir)
        except ValueError as exc: print('REFUSED: ' + str(exc)); return 2
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
            if args.test_command is not None: resolve_test_executable(workspace, args.test_command)   # LG2-b2: null = no test
        except ValueError as exc:
            print('REFUSED: ' + str(exc))
            return 2
        try: workitem_commands = workitem_reviewer_commands(Path(args.workitem).read_text())
        except (OSError, ValueError): workitem_commands = []                    # the coordinator reports an unreadable work item itself
        saved = _saved_configured_commands(run_dir) if args.action in ('run', 'resume') and restores_run(args) else []   # they dispatch turns
        if issue := configured_command_issue(workspace, run_dir, [c for c in (args.test_command, *args.reviewer_command, *workitem_commands, *saved) if c]):
            print('REFUSED: ' + issue)                                           # v297-eg-wire: before any model turn
            return 2
    if args.action == 'status' and args.brief is not None:
        return status_brief(run_dir, args.brief)
    try:
        with ProbeParentLock(args, workspace, run_dir) as probe_lock, run_lease(Path(args.run_dir)):   # FIELD-13: the parent lock first
            probe_lock.after_mkdir()
            args._probe_lock = probe_lock if probe_lock.whole else None
            if args.action in ('run', 'resume', 'permission-probe', 'accept', 'reject', 'note', 'attach-verification'):
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
