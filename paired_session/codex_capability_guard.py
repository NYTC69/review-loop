"""Read-only conservative scan of active Codex capability configuration."""
import hashlib
import json
from pathlib import Path

_CAPABILITIES = {'mcp_servers', 'notify', 'profile'}
_REQUIREMENTS = {'sandbox_mode', 'approval_policy', 'allowed_sandbox_modes',
                 'allowed_permission_profiles', 'permissions'}


def _root_key(line):
    text = line.strip()
    if not text or text.startswith('#'):
        return None
    text = text.lstrip('[').split(']', 1)[0].strip() if text.startswith('[') else text.split('=', 1)[0].strip()
    if text.startswith('"'):
        return json.JSONDecoder().raw_decode(text)[0]
    if text.startswith("'"):
        return text[1:text.find("'", 1)]
    return text.split('.', 1)[0].strip()


def _inspect_file(path, project=False, requirements=False):
    raw = path.read_bytes()
    keys = {_root_key(line) for line in raw.decode('utf-8-sig').splitlines()}
    keys.discard(None)
    active = {'mcp_servers'} if project else set(_CAPABILITIES)
    if requirements:
        active.update(_REQUIREMENTS)
    labels = {'MCP servers' if key == 'mcp_servers' else key for key in keys & active}
    return hashlib.sha256(raw).hexdigest(), labels


def inspect(code_home, workspace):
    workspace, home = Path(workspace).resolve(), Path(code_home).resolve()
    sources = {(home / 'config.toml', False, False), (home / 'managed_config.toml', False, True),
               (home / 'requirements.toml', False, True), (Path('/etc/codex/config.toml'), False, False),
               (Path('/etc/codex/managed_config.toml'), False, True),
               (Path('/etc/codex/requirements.toml'), False, True)}
    project_dirs = []
    for parent in (workspace, *workspace.parents):
        project_dirs.append(parent)
        if (parent / '.git').exists():
            break
    else:
        project_dirs = [workspace]
    sources.update((parent / '.codex/config.toml', True, False) for parent in project_dirs)
    files, issues = {}, []
    for path, project, requirements in sorted(sources, key=lambda row: str(row[0])):
        try:
            if not path.is_file():
                continue
            digest, findings = _inspect_file(path, project, requirements)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            issues.append('cannot parse effective Codex config: ' + str(path))
            continue
        files[str(path.resolve())] = digest
        issues.extend(f'{key} configured in {path}' for key in sorted(findings))
    return {'status': 'FAIL' if issues else 'PASS', 'sources': files,
            'issues': sorted(set(issues))}
