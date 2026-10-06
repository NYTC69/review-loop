"""Read-only conservative scan of active Codex capability configuration."""
import hashlib
import json
import os, pwd
import subprocess
import sys
from pathlib import Path

# project_root_markers (HYGIENE-1, F4): it moves the project root past the first .git, where the project scan below stops
_CAPABILITIES = {'mcp_servers', 'notify', 'profile', 'permissions', 'default_permissions', 'project_root_markers'}
_REQUIREMENTS = {'sandbox_mode', 'approval_policy', 'allowed_sandbox_modes', 'features',
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


def _plugins_off(path):
    """CG-1: True only when this config.toml sets `[features] plugins = false` (or `features.plugins = false`), which makes cached bundles inert."""
    table, off = '', False
    try: lines = path.read_text('utf-8-sig').splitlines()
    except (OSError, UnicodeError): return False
    for line in lines:
        text = line.split('#', 1)[0].strip()
        if text.startswith('['):
            table = text.strip('[] ').replace(' ', '')
            continue
        key, _, value = (part.strip() for part in text.partition('='))
        if (table, key) in (('features', 'plugins'), ('', 'features.plugins')): off = value == 'false'
    return off


def _inspect_file(path, project=False, requirements=False):
    raw = path.read_bytes()
    keys = {_root_key(line) for line in raw.decode('utf-8-sig').splitlines()}
    keys.discard(None)
    active = {'mcp_servers'} if project else set(_CAPABILITIES)
    if requirements:
        active.update(_REQUIREMENTS)
    labels = {'MCP servers' if key == 'mcp_servers' else key for key in keys & active}
    return hashlib.sha256(raw).hexdigest(), labels


def inspect(code_home, workspace, *, mdm_run=None, platform=None, managed_root=None, launch_plugins_off=False):
    """launch_plugins_off (rel210-fixCG): the caller launches every Codex process with `-c features.plugins=false` (CG-1), which
    makes every cached bundle inert (CG evidence, conclusion 2); such a bundle is then recorded in plugin_bundles_inert, not an issue."""
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
    sources.update((p / '.codex/config.toml', not (project_dirs[-1] / '.git').exists(), False) for p in project_dirs)
    files, issues, inert = {}, [], {}
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
    for version in () if _plugins_off(home / 'config.toml') else (home / 'plugins/cache').glob('*/*/*'):
        for name in ('mcp.json', '.mcp.json', '.app.json', 'plugin.json', '.codex-plugin/plugin.json'):
            path = version / name
            try:
                if not path.is_file(): continue
                raw = path.read_bytes()
                if not (name in ('mcp.json', '.mcp.json', '.app.json') or b'mcpServers' in raw or b'"apps"' in raw): continue
                if launch_plugins_off: inert[str(path.resolve())] = hashlib.sha256(raw).hexdigest()   # found, recorded, inert
                else:
                    files[str(path.resolve())] = hashlib.sha256(raw).hexdigest()
                    issues.append('plugin MCP or app bundle configured in ' + str(path))
            except OSError:
                issues.append('cannot inspect plugin capability bundle: ' + str(path))
    if (platform or sys.platform) == 'darwin':
        managed = Path(managed_root or '/Library/Managed Preferences')
        user = pwd.getpwuid(os.getuid()).pw_name
        paths = (managed / 'com.openai.codex.plist', managed / user / 'com.openai.codex.plist',
                 Path('/Library/Preferences/com.openai.codex.plist'))
        for path in paths:
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                issues.append('cannot inspect managed Codex preferences: ' + str(path))
            else:
                issues.append('macOS managed Codex preferences present: ' + str(path))
        for key in ('config_toml_base64', 'requirements_toml_base64'):
            runner = mdm_run or subprocess.run
            try:
                result = runner(['/usr/bin/defaults', 'read', 'com.openai.codex', key], capture_output=True, timeout=5)
                if result.returncode and b'does not exist' in result.stderr:
                    continue
                if result.returncode: raise OSError('MDM read refused')
                issues.append('macOS MDM Codex policy configured: ' + key)
            except (OSError, subprocess.SubprocessError):
                issues.append('cannot verify macOS MDM Codex policy: ' + key)
    return {'status': 'FAIL' if issues else 'PASS', 'sources': files,
            'issues': sorted(set(issues)), 'plugin_bundles_inert': inert}
