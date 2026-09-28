"""Exact documentation grants and mandatory downstream recheck policy."""

from typing import NamedTuple
from .candidate_tree import (CandidateError, CandidateRevision, _canonical_parts, _git, _git_env, _manifest,
                             _prefixes, _tree_entries, verify_candidate_revision)


DOCS_INVALIDATED = ('*',)
PROTECTED_PARTS = {'.git', '.gitignore', '.gitattributes', '.gitmodules', '.mailmap', '.claude',
                   '.env', '.npmrc', '.netrc', '.pypirc', '.review-loop', '.compass',
                   'agents', 'skills', 'agents.md',
                   'claude.md', 'claude.local.md', 'manifest.md', 'makefile', 'dockerfile'}
PROTECTED_DIRS = {'src', 'lib', 'bin', 'scripts', 'test', 'tests', '.agents', '.codex', '.github',
                  'testdata', 'fixtures', '__tests__', '__snapshots__', 'spec', 'requirements',
                  'constraints'}
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
                    any(part in PROTECTED_DIRS for part in parts) or
                    not value.casefold().endswith(DOCS_SUFFIXES) or
                    (basename.endswith('.txt') and
                     (any(term in basename for term in ('requirements', 'constraints')) or
                      basename == 'cmakelists.txt')) or
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


def validate_candidate_docs_change(baseline, before, approval, after, allowed_paths, *, exec_paths,
                                   finish_paths, polish_paths, closure_inputs, closure_uncertain):
    """Derive DOCS writes from scratch Git OIDs, never a caller-supplied path list."""
    verify_candidate_revision(baseline, after)
    if not isinstance(before, CandidateRevision) or not isinstance(exec_paths, (tuple, list)):
        raise CandidateError('DOCS prior revision or reviewed paths are malformed')
    expected = {'candidate_oid': before.tree_oid, 'run_id': baseline.run_dir.name,
                'workspace': str(baseline.workspace), 'run_dir': str(baseline.run_dir),
                'parent_head': baseline.parent_head, 'phase': 'POLISH-Q', 'status': 'APPROVE'}
    if (not isinstance(approval, dict) or
            any(approval.get(key) != value for key, value in expected.items()) or
            approval.get('blocking_findings') != [] or
            type(approval.get('epoch')) is not int or approval['epoch'] < 0):
        raise CandidateError('DOCS lacks approved prior candidate receipt')
    env = _git_env(GIT_DIR=str(baseline.git_dir), GIT_INDEX_FILE=str(baseline.index),
                   GIT_WORK_TREE=str(baseline.root), GIT_CEILING_DIRECTORIES=str(baseline.root.parent))
    before_oid = before.tree_oid
    if before_oid != baseline.tree_oid:
        ref = 'refs/paired-session/candidates/' + before_oid
        if _git(['rev-parse', '--verify', ref], env=env) != before_oid:
            raise CandidateError('prior DOCS candidate OID has no scratch ref')
    prior = _manifest(env, baseline.tree_oid, before_oid)
    if prior != before.manifest:
        raise CandidateError('prior DOCS candidate manifest differs from its OID')
    changes = _manifest(env, before_oid, after.tree_oid)
    paths = [row['path'] for row in changes]
    present = {path: mode for mode, _, path in _tree_entries(env, after.tree_oid)}
    if any(path in present and present[path] != '100644' for path in paths):
        raise CandidateError('DOCS candidate contains a non-regular documentation file')
    prior_paths = tuple(row['path'] for row in prior)
    return validate_docs_change(paths, allowed_paths, exec_paths=tuple(exec_paths) + prior_paths,
                                finish_paths=finish_paths, polish_paths=polish_paths,
                                closure_inputs=closure_inputs, closure_uncertain=closure_uncertain)
