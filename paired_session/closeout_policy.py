from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from importlib import import_module
ct = import_module("paired_session.candidate_tree" if __package__ else "candidate_tree")
def freeze_item(workspace, item_id):
    workspace = Path(workspace).resolve()
    backlog, view_path = workspace / 'BACKLOG.md', workspace / '.compass/backlog-last-view.json'
    if type(item_id) is not int or item_id < 1:
        raise ValueError('closeout requires a positive Compass item ID')
    if any(p.is_symlink() or not p.is_file() or p.resolve() != p for p in (backlog, view_path)):
        raise ValueError('closeout requires regular repo-root BACKLOG and local Compass view')
    raw, view_raw = backlog.read_bytes(), view_path.read_bytes()
    view = json.loads(view_raw)
    if not isinstance(view, dict) or view.get('source_path') != str(backlog):
        raise ValueError('closeout Compass view belongs to another BACKLOG')
    stamp = datetime.fromisoformat(str(view.get('generated_at', '')).replace('Z', '+00:00'))
    if stamp.tzinfo is None or not 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 600:
        raise ValueError('closeout needs a fresh Compass view; refresh it before starting a run')
    if ct._git(['status', '--porcelain=v1', '--untracked-files=all'], cwd=workspace):
        raise ValueError('closeout refuses dirty BACKLOG/workspace/index; restore or start a new run')
    head = ct._git(['rev-parse', '--verify', 'HEAD'], cwd=workspace)
    blob = ct._git(['rev-parse', head + ':BACKLOG.md'], cwd=workspace)
    git_dir = ct._git(['rev-parse', '--absolute-git-dir'], cwd=workspace)
    if ct._git_bytes(['cat-file', 'blob', blob], env=ct._git_env(GIT_DIR=git_dir)) != raw:
        raise ValueError('closeout BACKLOG differs from the committed blob')
    rows = view.get('items')
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('closeout Compass view items are malformed')
    selected = [r for r in rows if type(r.get('id')) is int and r.get('id') == item_id]
    if len(selected) != 1 or selected[0].get('section') not in ('P0', 'P1', 'P2', 'P3'):
        raise ValueError('closeout requires exactly one open Compass item')
    selected, section, matches = selected[0], None, []
    normalize = lambda title: ' '.join(title.strip('~').lower().split()).rstrip('.!?')
    wanted = normalize(str(selected.get('title_span', '')))
    lines = raw.decode('utf-8').splitlines(keepends=True)
    headings = [line.strip()[3:] for line in lines if line.startswith('## ')]
    if headings != ['P0', 'P1', 'P2', 'P3', 'Done'] or not wanted:
        raise ValueError('closeout BACKLOG sections/title are malformed')
    for index, line in enumerate(lines):
        if line.startswith('## '):
            section = line.strip()[3:]
        if section != 'Done' and line.startswith('- '):
            title = re.split(r'\((?:added|closed) ', line[2:], maxsplit=1)[0].strip().rstrip('.')
            if normalize(title) == wanted:
                matches.append((section, index, title))
    if len(matches) != 1 or matches[0][0] != selected['section']:
        raise ValueError('closeout item is stale or ambiguous; refresh the Compass view')
    return {'item_id': item_id, 'section': matches[0][0], 'line': matches[0][1], 'title': matches[0][2],
            'head': head, 'backlog_blob': blob, 'backlog_sha256': hashlib.sha256(raw).hexdigest(),
            'view_sha256': hashlib.sha256(view_raw).hexdigest(), 'view_timestamp': view['generated_at'],
            'adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
