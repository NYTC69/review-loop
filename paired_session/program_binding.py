import hashlib, json, os, shutil
from pathlib import Path
_PROFILE_PROGRAM_KEYS = ('codex_bin claude_bin gate_prompt reviewer_command test_command shadow adversarial_gate '
                         'author_vendor reviewer_vendor gate_vendor reviewer_model gate_model gate_effort '
                         'author_subagents allowed_models').split()
def safe_path(value, roots):
    paths = (Path(p).resolve() for p in value.split(os.pathsep) if p and os.path.isabs(p))
    return os.pathsep.join(dict.fromkeys(str(p) for p in paths
        if all(p != root and root not in p.parents for root in roots))) or '/usr/bin:/bin'
def resolve_binary(value, workspace, path_env):
    return (raw if (raw := Path(value).expanduser()).is_absolute() else workspace / raw if os.sep in value else
            Path(shutil.which(value, path=path_env) or workspace / value)).resolve()
def _record(path):
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None}
def snapshot(workspace, run_dir, author_tmp, codex_bin, claude_bin, gate_prompt, config_path):
    profile = workspace / Path(config_path or '.review-loop/paired-session.json').expanduser()
    roots = (workspace, Path(run_dir).resolve(), Path(author_tmp).resolve())
    if any(r == p or r in p.parents for p in (profile, profile.resolve()) for r in roots) and profile.is_file():
        if keys := set(_PROFILE_PROGRAM_KEYS).intersection(json.loads(profile.read_text())):
            return {}, 'workspace profile cannot select operator programs: ' + ', '.join(sorted(keys))
    path_env = safe_path(os.environ.get('PATH', ''), roots)
    records = {name: _record(resolve_binary(value, workspace, path_env)) for name, value in
               (('codex_bin', codex_bin), ('claude_bin', claude_bin))}
    records['gate_prompt'] = _record(Path(gate_prompt).resolve())
    for name, record in records.items():
        path = Path(record['path'])
        if not record['sha256'] or path in roots or any(root in path.parents for root in roots):
            return records, f'configured {name} is missing or inside an author-writable root: {path}'
    return {**records, 'path_env': path_env}, None
