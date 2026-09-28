"""Exact documentation grants and mandatory downstream recheck policy."""

from typing import NamedTuple
from .candidate_tree import _canonical_parts, _prefixes


DOCS_INVALIDATED = ('FINISH', 'POLISH-Q', 'DOCS', 'FINAL-REVIEW', 'TESTS', 'SECURITY')
PROTECTED_PARTS = {'.git', '.gitignore', '.gitattributes', '.gitmodules', '.mailmap', '.claude',
                   '.env', '.npmrc', '.netrc', '.pypirc', 'agents', 'skills', 'agents.md',
                   'claude.md', 'claude.local.md', 'manifest.md', 'makefile', 'dockerfile'}
PROTECTED_DIRS = {'src', 'lib', 'bin', 'scripts', 'test', 'tests', '.agents', '.codex', '.github'}
DOCS_SUFFIXES = ('.md', '.mdx', '.rst', '.txt', '.adoc')


class DocsChange(NamedTuple):
    paths: tuple[str, ...]
    requires_rechecks: bool
    invalidated_receipts: tuple[str, ...]


def _exact_paths(values, docs_only):
    if not isinstance(values, (list, tuple)):
        raise ValueError('paths must be a list or tuple')
    paths = set(_prefixes(values)) if values else set()
    if docs_only:
        for value in paths:
            parts = tuple(part.rstrip(' .').casefold() for part in value.split('/'))
            basename = value.rsplit('/', 1)[-1].casefold()
            if (any(part in PROTECTED_PARTS for part in parts) or
                    any(part in ('test', 'tests') for part in parts) or
                    parts[0] in PROTECTED_DIRS or not value.casefold().endswith(DOCS_SUFFIXES) or
                    (basename.endswith('.txt') and (basename.startswith('requirements') or
                     basename.startswith('constraints') or basename == 'cmakelists.txt')) or
                    parts[:2] == ('docs', 'protocol')):
                raise ValueError('protected documentation path')
    return paths


def validate_docs_change(changed_paths, allowed_paths, *, exec_paths, finish_paths,
                         polish_paths, closure_inputs, closure_uncertain):
    if type(closure_uncertain) is not bool:
        raise ValueError('prior write closure uncertainty must be boolean')
    if closure_uncertain:
        raise ValueError('prior write closure is uncertain')
    changed = _exact_paths(changed_paths, True)
    allowed = _exact_paths(allowed_paths, True)
    reviewed = set().union(*(_exact_paths(paths, False) for paths in
                             (exec_paths, finish_paths, polish_paths, closure_inputs)))
    allowed_keys = {_canonical_parts(path): path for path in allowed}
    if any(allowed_keys.get(_canonical_parts(path), path) != path for path in changed):
        raise ValueError('documentation path aliases its reserved path')
    unauthorized = changed - allowed
    if unauthorized:
        raise ValueError('documentation writer used an unreserved path: ' + sorted(unauthorized)[0])
    overlap = {path for path in changed for prior in reviewed
               if (_canonical_parts(path)[:len(_canonical_parts(prior))] == _canonical_parts(prior)
                   or _canonical_parts(prior)[:len(_canonical_parts(path))] == _canonical_parts(path))}
    if overlap:
        raise ValueError('documentation writer changed an EXEC-reviewed path: ' + sorted(overlap)[0])
    return DocsChange(tuple(sorted(changed)), bool(changed), DOCS_INVALIDATED if changed else ())
