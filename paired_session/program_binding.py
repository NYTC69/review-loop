import hashlib, json, os, re, shutil
from pathlib import Path
try:
    from paired_session import winplat
except ImportError:
    import winplat
_PROFILE_PROGRAM_KEYS = ('codex_bin claude_bin gemini_bin gate_prompt reviewer_command test_command shadow adversarial_gate '
                         'author_vendor author_model reviewer_vendor gate_vendor reviewer_model gate_model gate_effort '
                         'author_subagents allowed_models').split()
def safe_path(value, roots):
    paths = (Path(p).resolve() for p in value.split(os.pathsep) if p and os.path.isabs(p))
    return os.pathsep.join(dict.fromkeys(str(p) for p in paths
        if all(p != root and root not in p.parents for root in roots))) or (os.pathsep.join((str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'),
                               os.environ.get('SystemRoot', r'C:\Windows'))) if winplat.IS_WINDOWS else '/usr/bin:/bin')
def _windows_which(value, path_env):
    # Explicit PATH only: never let the workspace/current directory supply an operator program.
    extensions = os.environ.get('PATHEXT', '.COM;.EXE;.BAT;.CMD').split(';')
    for directory in path_env.split(os.pathsep):
        if not directory or not Path(directory).is_absolute(): continue
        base = Path(directory) / value
        candidates = ([base] if base.suffix else
                      [Path(str(base) + ext.lower()) for ext in extensions if ext] + [base])
        for candidate in candidates:
            if candidate.is_file(): return candidate
    return None

def resolve_binary(value, workspace, path_env):
    if winplat.IS_WINDOWS:
        raw = Path(value).expanduser()
        return (raw if raw.is_absolute() else workspace / raw if '/' in value or '\\' in value else
                _windows_which(value, path_env) or workspace / value).resolve()
    return (raw if (raw := Path(value).expanduser()).is_absolute() else workspace / raw if os.sep in value else
            Path(shutil.which(value, path=path_env) or workspace / value)).resolve()
def launch_vector(value, workspace=None, path_env=None):
    """No shell: native executable, or Node plus the known npm entry script. POSIX argv stays literal."""
    if not winplat.IS_WINDOWS: return [str(value)]
    workspace = Path(workspace or Path.cwd())
    path_env = os.environ.get('PATH', '') if path_env is None else path_env
    raw = Path(value).expanduser()
    if not raw.is_absolute() and '/' not in str(value) and '\\' not in str(value) and not _windows_which(str(value), path_env):
        raise ValueError(f'{value} not found on PATH')
    binary = resolve_binary(str(value), workspace, path_env)
    suffix = binary.suffix.lower()
    if suffix == '.exe' or (binary.parent.name == 'versions' and re.fullmatch(r'\d+\.\d+\.\d+', binary.name)):
        return [str(binary)]
    if suffix == '.js':
        script = binary
    elif suffix in ('.cmd', '.ps1', '.bat', '') and binary.stem.lower() in ('claude', 'codex'):
        name = binary.stem.lower()
        native = binary.with_name(name + '.exe')
        if native.is_file(): return [str(native.resolve())]
        package, entry = (('@anthropic-ai/claude-code', 'cli.js') if name == 'claude' else
                          ('@openai/codex', 'bin/codex.js'))
        package_root = binary.parent / 'node_modules' / package
        manifest = package_root / 'package.json'
        if manifest.is_file():
            declared = json.loads(manifest.read_text(encoding='utf-8')).get('bin')
            declared = declared.get(name) if isinstance(declared, dict) else declared
            if isinstance(declared, str) and declared: entry = declared
        script = package_root / entry
    else:
        raise ValueError(f'unsupported Windows CLI entry: {binary}; configure a native .exe or known npm shim')
    node = binary.parent / 'node.exe'
    if not node.is_file(): node = _windows_which('node.exe', path_env) or node
    if not script.is_file() or not node.is_file():
        raise ValueError(f'Windows CLI needs node.exe and npm entry script: {node}, {script}')
    return [str(node.resolve()), str(script.resolve())]

def program_argv(program):
    """Use a frozen record without rediscovering Node; strings support pre-binding version queries."""
    if isinstance(program, dict):
        return ([program['node']['path'], program['path']] if winplat.IS_WINDOWS and 'node' in program else
                [program['path']])
    return launch_vector(program)

def _record(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
def snapshot(workspace, run_dir, author_tmp, codex_bin, claude_bin, gate_prompt, config_path, gemini_bin=None):
    """codex_bin and claude_bin are always bound (V317-FB); gemini_bin only when given (ADR-21 G1: a role runs gemini).
    Every bound program and the gate prompt must exist outside the author-writable roots."""
    profile = workspace / Path(config_path or '.review-loop/paired-session.json').expanduser()
    roots = (workspace, Path(run_dir).resolve(), Path(author_tmp).resolve())
    if any(r == p or r in p.parents for p in (profile, profile.resolve()) for r in roots) and profile.is_file():
        if keys := set(_PROFILE_PROGRAM_KEYS).intersection(json.loads(profile.read_text(encoding='utf-8'))):
            return {}, 'workspace profile cannot select operator programs: ' + ', '.join(sorted(keys))
    path_env = safe_path(os.environ.get('PATH', ''), roots)
    records = {name: _record(resolve_binary(value, workspace, path_env)) for name, value in
               (('codex_bin', codex_bin), ('claude_bin', claude_bin), ('gemini_bin', gemini_bin)) if value is not None}
    if winplat.IS_WINDOWS:
        # gemini is refused on Windows before binding (ADR-21 amendment); only codex/claude get a launch vector.
        try:
            for name, value in (('codex_bin', codex_bin), ('claude_bin', claude_bin)):
                argv = launch_vector(value, workspace, path_env)
                records[name] = _record(Path(argv[-1]))
                if len(argv) == 2: records[name]['node'] = _record(Path(argv[0]))
        except (OSError, ValueError) as exc:
            return records, str(exc)
    records['gate_prompt'] = _record(Path(gate_prompt).resolve())
    bound = list(records.items())
    if winplat.IS_WINDOWS:
        bound += [(name + '.node', record['node']) for name, record in records.items() if 'node' in record]
    for name, record in bound:
        path = Path(record['path'])
        if not record['sha256'] or path in roots or any(root in path.parents for root in roots):
            return records, f'configured {name} is missing or inside an author-writable root: {path}'
    return {**records, 'path_env': path_env}, None
